"""
Capitol Alpha — Main Orchestrator.

This is the entry point. It:
  1. Loads config
  2. Initialises database, T212 client, Telegram bot
  3. Runs the data pipeline on a schedule
  4. Scores new trades
  5. Sends alerts via Telegram
  6. Starts the dashboard web server
  7. Keeps everything running as a long-lived process (ideal for systemd on Pi)

Run with:
    python -m src.main
"""

import asyncio
import logging
import signal
import sys
import threading
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import load_config
from src.database import Database
from src.data.pipeline import DataPipeline
from src.scoring.engine import ScoringEngine
from src.alerts.telegram_bot import TelegramBot
from src.execution.trading212 import Trading212Client
from src.dashboard.app import create_dashboard_app
from src.utils.helpers import setup_logging

logger = logging.getLogger("capitol_alpha")


class CapitolAlpha:
    """Main application orchestrator."""

    def __init__(self):
        self.config = load_config()
        self.db = Database()
        self.pipeline = DataPipeline(self.db, self.config)
        self.scorer = ScoringEngine(self.db, self.config)

        # Trading212 client (optional — works without it)
        self.t212 = None
        if self.config.trading212.api_key:
            try:
                self.t212 = Trading212Client(self.config.trading212)
                logger.info("Trading212 client initialised")
            except Exception as e:
                logger.warning(f"Trading212 init failed (will continue without): {e}")

        # Telegram bot
        self.bot = TelegramBot(self.config, self.db, self.t212)

        # Scheduler
        self.scheduler = AsyncIOScheduler()

    async def run_cycle(self):
        """
        Run one full ingestion + scoring + alerting cycle.
        Called by the scheduler every N minutes.
        """
        logger.info("=" * 60)
        logger.info(f"Running cycle at {datetime.now().isoformat()}")
        logger.info("=" * 60)

        # Step 1: Ingest data from all sources
        try:
            summary = self.pipeline.run_full_ingestion()
            logger.info(f"Ingestion summary: {summary}")
        except Exception as e:
            logger.error(f"Ingestion failed: {e}", exc_info=True)
            return

        # Step 2: Score unscored trades
        try:
            new_signals = self.scorer.score_unscored_trades()
            logger.info(f"New signals: {len(new_signals)}")
        except Exception as e:
            logger.error(f"Scoring failed: {e}", exc_info=True)
            return

        # Step 3: Send alerts for actionable signals
        alertable = [
            s for s in new_signals
            if s.get("suggested_action") in ("ALERT_ONLY", "SUGGEST_TRADE", "HIGH_CONVICTION")
        ]

        for sig in alertable:
            try:
                await self.bot.send_signal_alert(sig)
            except Exception as e:
                logger.error(f"Failed to alert signal {sig.get('id')}: {e}")

        logger.info(f"Cycle complete: {len(alertable)} alerts sent")

    async def run(self):
        """Start the full application."""
        setup_logging()
        logger.info("🏛️ Capitol Alpha starting...")
        logger.info(f"Environment: {self.config.trading212.environment}")
        logger.info(f"Poll interval: {self.config.data_sources.poll_interval_minutes}m")

        # Start Telegram bot
        await self.bot.start()

        # Schedule recurring ingestion cycles
        self.scheduler.add_job(
            self.run_cycle,
            "interval",
            minutes=self.config.data_sources.poll_interval_minutes,
            id="data_cycle",
            next_run_time=datetime.now(),  # Run immediately on startup
        )

        # Reset T212 daily order counter at midnight
        if self.t212:
            self.scheduler.add_job(
                lambda: self.t212.reset_daily_counter(),
                "cron",
                hour=0,
                minute=0,
                id="reset_daily",
            )

        self.scheduler.start()

        # Start dashboard in a background thread
        try:
            dashboard = create_dashboard_app(self.db, self.config)
            dash_thread = threading.Thread(
                target=lambda: dashboard.run(
                    host="0.0.0.0", port=5055, debug=False, use_reloader=False
                ),
                daemon=True,
            )
            dash_thread.start()
            logger.info("Dashboard running at http://0.0.0.0:5055")
        except Exception as e:
            logger.warning(f"Dashboard failed to start: {e}")

        logger.info("Capitol Alpha is running. Press Ctrl+C to stop.")

        # Keep running
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
            self.scheduler.shutdown()
            await self.bot.stop()
            logger.info("Capitol Alpha stopped.")


def main():
    app = CapitolAlpha()
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
