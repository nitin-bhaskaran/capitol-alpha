"""
Data pipeline orchestrator.
Coordinates ingestion from all sources and deduplicates into the database.
"""

import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional

from src.database import Database
from src.config import Config
from src.data.house_watcher import fetch_house_trades
from src.data.senate_watcher import fetch_senate_trades
from src.data.capitol_trades import fetch_capitol_trades
from src.data.trump_tracker import fetch_trump_trades_from_news

logger = logging.getLogger("capitol_alpha.pipeline")


class DataPipeline:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    def run_full_ingestion(self) -> Dict[str, int]:
        """
        Run all data sources and ingest into the database.
        Returns a summary of what was ingested.
        """
        logger.info("=" * 60)
        logger.info("Starting full data ingestion cycle")
        logger.info("=" * 60)

        summary = {
            "house_watcher": 0,
            "senate_watcher": 0,
            "capitol_trades": 0,
            "trump_tracker": 0,
            "total_new": 0,
            "total_duplicates": 0,
        }

        # 1. House Stock Watcher
        try:
            house_trades = fetch_house_trades(
                self.config.data_sources.house_watcher_url
            )
            new, dupes = self._ingest_trades(house_trades)
            summary["house_watcher"] = new
            summary["total_new"] += new
            summary["total_duplicates"] += dupes
            logger.info(f"House Watcher: {new} new, {dupes} duplicates")
        except Exception as e:
            logger.error(f"House Watcher ingestion failed: {e}")

        # 2. Senate Stock Watcher
        try:
            senate_trades = fetch_senate_trades(
                self.config.data_sources.senate_watcher_url
            )
            senate_trades = self._limit_senate_live_trades(senate_trades)
            new, dupes = self._ingest_trades(senate_trades)
            summary["senate_watcher"] = new
            summary["total_new"] += new
            summary["total_duplicates"] += dupes
            logger.info(f"Senate Watcher: {new} new, {dupes} duplicates")
        except Exception as e:
            logger.error(f"Senate Watcher ingestion failed: {e}")

        # 3. Capitol Trades (scraper)
        if self.config.data_sources.capitol_trades_enabled:
            try:
                ct_trades = fetch_capitol_trades(pages=3)
                new, dupes = self._ingest_trades(ct_trades)
                summary["capitol_trades"] = new
                summary["total_new"] += new
                summary["total_duplicates"] += dupes
                logger.info(f"Capitol Trades: {new} new, {dupes} duplicates")
            except Exception as e:
                logger.error(f"Capitol Trades scraping failed: {e}")

        # 4. Trump Family Tracker
        try:
            trump_trades = fetch_trump_trades_from_news()
            new, dupes = self._ingest_trades(trump_trades)
            summary["trump_tracker"] = new
            summary["total_new"] += new
            summary["total_duplicates"] += dupes
            logger.info(f"Trump Tracker: {new} new, {dupes} duplicates")
        except Exception as e:
            logger.error(f"Trump Tracker failed: {e}")

        logger.info(
            f"Ingestion complete: {summary['total_new']} new trades, "
            f"{summary['total_duplicates']} duplicates skipped"
        )
        return summary

    def _ingest_trades(self, trades: List[Dict[str, Any]]) -> tuple:
        """Ingest a list of trades. Returns (new_count, duplicate_count)."""
        if not trades:
            return 0, 0
        return self.db.insert_raw_trades(trades)

    def _limit_senate_live_trades(self, trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the live alert path focused on recent Senate disclosures."""
        original_count = len(trades)
        max_age_days = self.config.data_sources.senate_watcher_max_age_days
        max_records = self.config.data_sources.senate_watcher_max_records

        if max_age_days and max_age_days > 0:
            cutoff = date.today() - timedelta(days=max_age_days)
            recent = []
            missing_date_count = 0
            stale_count = 0

            for trade in trades:
                trade_date = self._best_trade_date(trade)
                if trade_date is None:
                    missing_date_count += 1
                    continue
                if trade_date >= cutoff:
                    recent.append(trade)
                else:
                    stale_count += 1

            trades = recent
            logger.info(
                "Senate Watcher live filter: %s parsed, %s recent, %s stale, "
                "%s missing date (max_age_days=%s)",
                original_count,
                len(trades),
                stale_count,
                missing_date_count,
                max_age_days,
            )

        trades = sorted(
            trades,
            key=lambda trade: self._best_trade_date(trade) or date.min,
            reverse=True,
        )

        if max_records and max_records > 0 and len(trades) > max_records:
            logger.info(
                "Senate Watcher live cap: keeping %s of %s recent rows",
                max_records,
                len(trades),
            )
            trades = trades[:max_records]

        return trades

    def _best_trade_date(self, trade: Dict[str, Any]) -> Optional[date]:
        raw_date = trade.get("filing_date") or trade.get("transaction_date")
        if not raw_date:
            return None
        try:
            return datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            return None
