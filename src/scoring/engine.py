"""
Alpha Scoring Engine.

Scores each politician trade on a 0.0 — 1.0 scale based on:
  1. VIP tier (is this a high-signal politician?)
  2. Committee relevance (do they sit on a committee related to the stock's sector?)
  3. Filing gap (how quickly did they disclose? faster = more confident signal)
  4. Trade size (bigger = more conviction)
  5. Historical alpha (has this politician's past trades been profitable?)

The composite score determines the suggested action:
  - 0.0 — 0.39: Logged only, no alert
  - 0.40 — 0.59: Alert via Telegram (informational)
  - 0.60 — 0.79: Suggest trade via Telegram (approve/reject)
  - 0.80 — 1.0: High conviction — suggest trade with emphasis
"""

import json
import logging
from typing import Dict, Any, Optional, List

from src.config import Config
from src.database import Database
from src.utils.helpers import (
    filing_gap_days,
    midpoint_amount,
    transaction_to_direction,
)

logger = logging.getLogger("capitol_alpha.scoring")

# Sector-to-committee mapping for committee relevance scoring
SECTOR_COMMITTEE_MAP = {
    # Defense / aerospace tickers often correlate with Armed Services
    "LMT": ["Armed Services"],
    "RTX": ["Armed Services"],
    "NOC": ["Armed Services"],
    "BA": ["Armed Services"],
    "GD": ["Armed Services"],
    "LHX": ["Armed Services"],
    # Tech / social media — Intelligence, Judiciary
    "META": ["Intelligence", "Judiciary"],
    "GOOGL": ["Intelligence", "Judiciary"],
    "GOOG": ["Intelligence", "Judiciary"],
    "AMZN": ["Intelligence"],
    "MSFT": ["Intelligence"],
    "AAPL": ["Judiciary"],
    # Finance — Financial Services
    "JPM": ["Financial Services"],
    "BAC": ["Financial Services"],
    "GS": ["Financial Services"],
    "MS": ["Financial Services"],
    "C": ["Financial Services"],
    "WFC": ["Financial Services"],
    # Energy — Energy and Commerce
    "XOM": ["Energy and Commerce"],
    "CVX": ["Energy and Commerce"],
    "COP": ["Energy and Commerce"],
    "SLB": ["Energy and Commerce"],
    # Pharma / Healthcare — Energy and Commerce (health subcommittee)
    "PFE": ["Energy and Commerce"],
    "JNJ": ["Energy and Commerce"],
    "UNH": ["Energy and Commerce"],
    "MRK": ["Energy and Commerce"],
    "ABBV": ["Energy and Commerce"],
    "LLY": ["Energy and Commerce"],
}


class ScoringEngine:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.weights = config.scoring
        self.vip = config.vip_watchlist

    def score_unscored_trades(self) -> List[Dict[str, Any]]:
        """
        Score all raw trades that don't have a signal yet.
        Returns the list of new signals created.
        """
        unscored = self.db.get_unscored_trades()
        logger.info(f"Scoring {len(unscored)} unscored trades")

        new_signals = []
        for trade in unscored:
            signal = self._score_trade(trade)
            if signal:
                signal_id = self.db.insert_signal(signal)
                if signal_id:
                    signal["id"] = signal_id
                    new_signals.append(signal)

        logger.info(f"Created {len(new_signals)} new signals")
        return new_signals

    def _score_trade(self, trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Score a single trade and return a signal dict."""
        ticker = trade.get("ticker", "")
        politician = trade.get("politician_name", "")
        direction = transaction_to_direction(trade.get("transaction_type", ""))

        if direction == "UNKNOWN":
            return None

        # ── Component scores (each 0.0 — 1.0) ────────────────────

        # 1. VIP score
        vip_score, vip_tier = self._score_vip(politician)

        # 2. Committee relevance
        committee_score = self._score_committee(politician, ticker)

        # 3. Filing gap (faster disclosure = higher score)
        gap = filing_gap_days(
            trade.get("transaction_date", ""),
            trade.get("filing_date", ""),
        )
        gap_score = self._score_filing_gap(gap)

        # 4. Trade size (bigger = more conviction)
        mid_amount = midpoint_amount(trade.get("amount_low"), trade.get("amount_high"))
        size_score = self._score_trade_size(mid_amount)

        # 5. Historical alpha (placeholder — needs backtested data)
        hist_score = self._score_historical(politician)

        # ── Composite score ───────────────────────────────────────

        composite = (
            vip_score * self.weights.vip_weight
            + committee_score * self.weights.committee_weight
            + gap_score * self.weights.filing_gap_weight
            + size_score * self.weights.trade_size_weight
            + hist_score * self.weights.historical_alpha_weight
        )

        # Normalise to 0-1 range
        max_possible = (
            self.weights.vip_weight
            + self.weights.committee_weight
            + self.weights.filing_gap_weight
            + self.weights.trade_size_weight
            + self.weights.historical_alpha_weight
        )
        alpha_score = round(composite / max_possible, 3) if max_possible > 0 else 0

        # ── Determine suggested action ────────────────────────────

        if alpha_score >= self.weights.min_score_to_suggest_trade:
            suggested_action = "SUGGEST_TRADE"
            if alpha_score >= 0.80:
                suggested_action = "HIGH_CONVICTION"
        elif alpha_score >= self.weights.min_score_to_alert:
            suggested_action = "ALERT_ONLY"
        else:
            suggested_action = "LOG_ONLY"

        breakdown = {
            "vip": round(vip_score, 3),
            "committee": round(committee_score, 3),
            "filing_gap": round(gap_score, 3),
            "trade_size": round(size_score, 3),
            "historical": round(hist_score, 3),
        }

        return {
            "raw_trade_id": trade["id"],
            "ticker": ticker,
            "politician_name": politician,
            "direction": direction,
            "alpha_score": alpha_score,
            "score_breakdown": json.dumps(breakdown),
            "vip_tier": vip_tier,
            "filing_gap_days": gap,
            "suggested_action": suggested_action,
        }

    def _score_vip(self, politician: str) -> tuple:
        """Score based on VIP watchlist tier. Returns (score, tier)."""
        name_lower = politician.lower()

        for vip_name in self.vip.tier1_politicians:
            if vip_name.lower() in name_lower or name_lower in vip_name.lower():
                return 1.0, 1

        for vip_name in self.vip.tier2_politicians:
            if vip_name.lower() in name_lower or name_lower in vip_name.lower():
                return 0.7, 2

        return 0.2, None

    def _score_committee(self, politician: str, ticker: str) -> float:
        """
        Score based on whether the politician sits on a committee
        relevant to the stock's sector.
        """
        relevant_committees = SECTOR_COMMITTEE_MAP.get(ticker.upper(), [])
        if not relevant_committees:
            return 0.3  # No committee mapping — neutral score

        # Check if the politician is on any of the relevant committees
        # For now, we use the high_signal_committees list as a proxy
        # TODO: Enrich with actual politician->committee membership data
        for comm in relevant_committees:
            if comm in self.vip.high_signal_committees:
                return 0.8

        return 0.3

    def _score_filing_gap(self, gap_days: Optional[int]) -> float:
        """
        Score based on filing gap. Under STOCK Act, they have 45 days.
        Faster disclosure suggests more confidence / less to hide.
        """
        if gap_days is None:
            return 0.3  # Unknown gap — neutral

        if gap_days <= 5:
            return 1.0   # Filed within 5 days — very fast, high signal
        elif gap_days <= 15:
            return 0.8   # Filed within 2 weeks — good
        elif gap_days <= 30:
            return 0.5   # Within a month — average
        elif gap_days <= 45:
            return 0.3   # Near the deadline — less confident
        else:
            return 0.1   # Late filing — low confidence

    def _score_trade_size(self, midpoint: float) -> float:
        """Score based on trade size midpoint. Bigger = more conviction."""
        if midpoint <= 0:
            return 0.1
        elif midpoint < 15_000:
            return 0.2   # $1K - $15K — small
        elif midpoint < 50_000:
            return 0.4   # $15K - $50K — moderate
        elif midpoint < 100_000:
            return 0.6   # $50K - $100K — significant
        elif midpoint < 250_000:
            return 0.8   # $100K - $250K — large
        elif midpoint < 500_000:
            return 0.9   # $250K - $500K — very large
        else:
            return 1.0   # $500K+ — massive conviction

    def _score_historical(self, politician: str) -> float:
        """
        Score based on politician's historical trading performance.
        TODO: Build a backtester that computes win rates per politician.
        For now, returns a default score.
        """
        # Known strong performers get a boost
        strong_performers = {
            "nancy pelosi": 0.9,
            "dan crenshaw": 0.7,
            "tommy tuberville": 0.6,
        }
        name_lower = politician.lower()
        for known, score in strong_performers.items():
            if known in name_lower:
                return score

        return 0.5  # Default — neutral
