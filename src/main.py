"""Capitol Alpha main orchestrator.

This entry point:
  1. Loads config.
  2. Initialises database, Trading212 client, Telegram bot, and paper ledger.
  3. Runs the data pipeline on a schedule.
  4. Scores new trades.
  5. Sends alerts via Telegram.
  6. Starts the dashboard web server.
  7. Keeps everything running as a long-lived process.

Run with:
    python -m src.main
"""

import asyncio
import logging
import threading
from datetime import datetime

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ModuleNotFoundError:
    AsyncIOScheduler = None

from src.alerts.telegram_bot import TelegramBot
from src.config import load_config
from src.database import Database
from src.execution.paper import PaperTradingService
from src.execution.trading212 import Trading212Client
from src.scoring.engine import ScoringEngine
from src.utils.helpers import setup_logging

try:
    from src.data.pipeline import DataPipeline
except ModuleNotFoundError:
    DataPipeline = None

try:
    from src.dashboard.app import create_dashboard_app
except ModuleNotFoundError:
    create_dashboard_app = None

logger = logging.getLogger("capitol_alpha")


class CapitolAlpha:
    """Main application orchestrator."""

    def __init__(self):
        if AsyncIOScheduler is None:
            raise RuntimeError("apscheduler is required. Run: pip install -r requirements.txt")
        if DataPipeline is None:
            raise RuntimeError("data ingestion dependencies are required. Run: pip install -r requirements.txt")

        self.config = load_config()
        self.db = Database()
        self.pipeline = DataPipeline(self.db, self.config)
        self.scorer = ScoringEngine(self.db, self.config)

        self.t212 = None
        if self.config.trading212.api_key:
            try:
                self.t212 = Trading212Client(self.config.trading212)
                logger.info("Trading212 client initialised")
            except Exception as e:
                logger.warning("Trading212 init failed; continuing without it: %s", e)

        self.paper = PaperTradingService(self.db, self.config, self.t212)
        self.bot = TelegramBot(self.config, self.db, self.t212, self.paper)
        self.scheduler = AsyncIOScheduler()

    async def run_cycle(self):
        """Run one ingestion, scoring, and alerting cycle."""
        logger.info("=" * 60)
        logger.info("Running cycle at %s", datetime.now().isoformat())
        logger.info("=" * 60)

        try:
            summary = self.pipeline.run_full_ingestion()
            logger.info("Ingestion summary: %s", summary)
        except Exception as e:
            logger.error("Ingestion failed: %s", e, exc_info=True)
            return

        try:
            new_signals = self.scorer.score_unscored_trades()
            logger.info("New signals: %s", len(new_signals))
        except Exception as e:
            logger.error("Scoring failed: %s", e, exc_info=True)
            return

        alertable = [
            s for s in new_signals
            if s.get("suggested_action")
            in ("ALERT_ONLY", "SUGGEST_TRADE", "HIGH_CONVICTION", "REDUCE_IF_HELD")
        ]

        for sig in alertable:
            try:
                await self.bot.send_signal_alert(sig)
            except Exception as e:
                logger.error("Failed to alert signal %s: %s", sig.get("id"), e)

        logger.info("Cycle complete: %s alerts sent", len(alertable))

    async def run(self):
        """Start the full application."""
        setup_logging()
        logger.info("Capitol Alpha starting")
        logger.info("Environment: %s", self.config.trading212.environment)
        logger.info("Execution mode: %s", self.config.execution.mode)
        logger.info("Poll interval: %sm", self.config.data_sources.poll_interval_minutes)

        await self.bot.start()

        self.scheduler.add_job(
            self.run_cycle,
            "interval",
            minutes=self.config.data_sources.poll_interval_minutes,
            id="data_cycle",
            next_run_time=datetime.now(),
        )

        if self.t212:
            self.scheduler.add_job(
                lambda: self.t212.reset_daily_counter(),
                "cron",
                hour=0,
                minute=0,
                id="reset_daily",
            )

        self.scheduler.start()

        try:
            if create_dashboard_app is None:
                raise RuntimeError("Flask is not installed")
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
            logger.warning("Dashboard failed to start: %s", e)

        logger.info("Capitol Alpha is running. Press Ctrl+C to stop.")

        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("Shutdown requested")
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown requested")
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Stop background services without surfacing Ctrl+C tracebacks."""
        logger.info("Shutting down...")
        try:
            if getattr(self.scheduler, "running", False):
                self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning("Scheduler shutdown failed: %s", e)

        try:
            await self.bot.stop()
        except Exception as e:
            logger.warning("Telegram shutdown failed: %s", e)

        logger.info("Capitol Alpha stopped.")


def main():
    app = CapitolAlpha()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
