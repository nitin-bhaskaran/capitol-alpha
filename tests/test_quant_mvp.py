import asyncio
import tempfile
import unittest
import logging
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from src.alerts.telegram_bot import TelegramBot
from src.config import Config
from src.data import house_watcher, senate_watcher
from src.data.pipeline import DataPipeline
from src.database import Database
from src.execution.paper import PaperTradingService
from src.execution.quotes import MarketQuoteService
from src.main import CapitolAlpha
from src.scoring.engine import ScoringEngine
from src.scoring.features import build_event_features
from src.scoring.model import EventAlphaEnsemble
from src.utils.helpers import SensitiveLogFilter, normalize_ticker


def sample_trade():
    return {
        "source": "house_watcher",
        "politician_name": "Nancy Pelosi",
        "politician_party": "D",
        "politician_state": "CA",
        "politician_chamber": "House",
        "ticker": "NVDA",
        "asset_description": "NVIDIA Corporation",
        "transaction_type": "Purchase",
        "transaction_date": "2026-05-20",
        "filing_date": "2026-05-24",
        "amount_range": "$100,001 - $250,000",
        "amount_low": 100001.0,
        "amount_high": 250000.0,
        "owner": "",
        "comment": "",
        "source_url": "https://example.test",
        "raw_json": "{}",
    }


class FakeT212:
    def __init__(self, filled_value=None, min_trade_quantity=0.1):
        self.filled_value = filled_value
        self.min_trade_quantity = min_trade_quantity
        self.orders = []

    def find_instrument(self, ticker):
        return {
            "ticker": f"{ticker}_US_EQ",
            "minTradeQuantity": self.min_trade_quantity,
            "currencyCode": "USD",
        }

    def place_market_order(self, t212_ticker, quantity, extended_hours=False):
        self.orders.append({"ticker": t212_ticker, "quantity": quantity})
        return {
            "id": 12345,
            "status": "FILLED",
            "ticker": t212_ticker,
            "filledQuantity": quantity,
            "filledValue": self.filled_value if self.filled_value is not None else quantity * 250.0,
        }


class FakeQuote:
    def __init__(self, price_gbp=250.0):
        self.price_gbp = price_gbp
        self.requests = []

    def get_unit_price_gbp(self, ticker, instrument=None):
        self.requests.append({"ticker": ticker, "instrument": instrument})
        return self.price_gbp


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeQuoteSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if "query1.finance.yahoo.com" in url:
            return FakeResponse({
                "chart": {
                    "result": [{
                        "meta": {
                            "regularMarketPrice": 100.0,
                            "currency": "USD",
                        }
                    }]
                }
            })
        if "api.frankfurter.app" in url:
            return FakeResponse({"rates": {"GBP": 0.8}})
        return FakeResponse({})


class FakeSenateRequests:
    def __init__(self):
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return FakeResponse([{
            "transaction_date": "11/10/2020",
            "owner": "Spouse",
            "ticker": "NVDA",
            "asset_description": "NVIDIA Corporation",
            "type": "Purchase",
            "amount": "$15,001 - $50,000",
            "comment": "",
            "senator": "Nancy Pelosi",
            "ptr_link": "https://example.test/ptr",
        }])


class FakeHouseRequests:
    def __init__(self):
        self.called = False

    def get(self, url, headers=None, timeout=None):
        self.called = True
        return FakeResponse([])


class FakePaper:
    def execute_signal(self, signal_id):
        return {"ok": True, "order_id": 7, "status": "filled"}


class FakeLifecycleBot:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeScheduler:
    def __init__(self):
        self.running = False
        self.jobs = []
        self.shutdown_called = False

    def add_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))

    def start(self):
        self.running = True

    def shutdown(self, wait=True):
        self.shutdown_called = True
        self.running = False


class FakeDashboard:
    def run(self, *args, **kwargs):
        return None


class InterruptingPipeline:
    def run_full_ingestion(self):
        raise KeyboardInterrupt()


class FakeQuery:
    def __init__(self):
        self.edited = None

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text


class QuantMvpTests(unittest.TestCase):
    def make_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Database(Path(tmp.name) / "test.db")

    def test_feature_generation_from_disclosure(self):
        config = Config()
        features = build_event_features(sample_trade(), config)

        self.assertEqual(features["ticker"], "NVDA")
        self.assertEqual(features["direction"], "BUY")
        self.assertEqual(features["sector"], "semiconductors")
        self.assertEqual(features["vip_tier"], 1)
        self.assertGreater(features["filing_speed_score"], 0.7)
        self.assertEqual(len(features["feature_hash"]), 64)

    def test_ticker_normalization_strips_capitol_trades_suffixes(self):
        self.assertEqual(normalize_ticker("MSFT:US"), "MSFT")
        self.assertEqual(normalize_ticker("AAPL_US_EQ"), "AAPL")
        config = Config()
        features = build_event_features({**sample_trade(), "ticker": "AMD:US"}, config)
        self.assertEqual(features["ticker"], "AMD")
        self.assertEqual(features["sector"], "semiconductors")

    def test_model_outputs_tradeable_shape(self):
        config = Config()
        trade = {**sample_trade(), "id": 1}
        result = EventAlphaEnsemble(config).score_trade(trade, rank_order=1)

        self.assertEqual(result["model_version"], config.model.default_model_version)
        self.assertEqual(result["direction"], "BUY")
        self.assertIn(result["suggested_action"], {"SUGGEST_TRADE", "HIGH_CONVICTION", "ALERT_ONLY", "LOG_ONLY"})
        self.assertGreaterEqual(result["confidence"], 0)
        self.assertLessEqual(result["confidence"], 1)
        self.assertIn("_features", result)

    def test_quote_service_fetches_market_price_and_converts_to_gbp(self):
        session = FakeQuoteSession()
        service = MarketQuoteService(Config(), session=session)

        price = service.get_unit_price_gbp("NVDA", {"currencyCode": "USD"})

        self.assertAlmostEqual(price, 80.0)
        self.assertEqual(len(session.calls), 2)

    def test_sensitive_log_filter_redacts_telegram_bot_token(self):
        record = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='POST https://api.telegram.org/bot123456:secret/getMe "HTTP/1.1 200 OK"',
            args=(),
            exc_info=None,
        )

        SensitiveLogFilter().filter(record)

        self.assertIn("bot<redacted>/getMe", record.getMessage())
        self.assertNotIn("123456:secret", record.getMessage())

    def test_house_legacy_s3_is_skipped_without_request(self):
        fake_requests = FakeHouseRequests()

        with patch.object(house_watcher, "requests", fake_requests):
            trades = house_watcher.fetch_house_trades(house_watcher.LEGACY_S3_URL)

        self.assertEqual(trades, [])
        self.assertFalse(fake_requests.called)

    def test_senate_legacy_s3_uses_github_raw_fallback(self):
        fake_requests = FakeSenateRequests()

        with patch.object(senate_watcher, "requests", fake_requests):
            trades = senate_watcher.fetch_senate_trades(senate_watcher.LEGACY_S3_URL)

        self.assertEqual(fake_requests.urls, [senate_watcher.GITHUB_RAW_URL])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["ticker"], "NVDA")
        self.assertEqual(trades[0]["politician_chamber"], "Senate")

    def test_senate_live_filter_keeps_recent_rows_and_caps(self):
        config = Config()
        config.data_sources.senate_watcher_max_age_days = 30
        config.data_sources.senate_watcher_max_records = 1
        pipeline = DataPipeline(db=None, config=config)
        older = sample_trade()
        older.update({
            "source": "senate_watcher",
            "ticker": "OLD",
            "transaction_date": "2020-01-01",
            "filing_date": "2020-01-01",
        })
        recent = sample_trade()
        recent.update({
            "source": "senate_watcher",
            "ticker": "NEW",
            "transaction_date": (date.today() - timedelta(days=2)).isoformat(),
            "filing_date": (date.today() - timedelta(days=2)).isoformat(),
        })
        newest = sample_trade()
        newest.update({
            "source": "senate_watcher",
            "ticker": "NEWEST",
            "transaction_date": date.today().isoformat(),
            "filing_date": date.today().isoformat(),
        })

        filtered = pipeline._limit_senate_live_trades([older, recent, newest])

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["ticker"], "NEWEST")

    def test_batch_raw_trade_insert_counts_duplicates(self):
        db = self.make_db()
        first = sample_trade()
        duplicate = sample_trade()

        new_count, dupe_count = db.insert_raw_trades([first, duplicate])

        self.assertEqual(new_count, 1)
        self.assertEqual(dupe_count, 1)

    def test_scoring_ignores_stale_senate_backlog(self):
        db = self.make_db()
        config = Config()
        config.data_sources.senate_watcher_max_age_days = 30
        trade = sample_trade()
        trade.update({
            "source": "senate_watcher",
            "transaction_date": "2020-01-01",
            "filing_date": "2020-01-01",
        })
        db.insert_raw_trade(trade)

        signals = ScoringEngine(db, config).score_unscored_trades()

        self.assertEqual(signals, [])

    def test_scoring_persists_signal_and_feature_snapshot(self):
        db = self.make_db()
        config = Config()
        db.insert_raw_trade(sample_trade())

        signals = ScoringEngine(db, config).score_unscored_trades()
        self.assertEqual(len(signals), 1)

        with db.connection() as conn:
            snapshot_count = conn.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()[0]
            model_runs = conn.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]
        self.assertEqual(snapshot_count, 1)
        self.assertEqual(model_runs, 1)

    def test_trading212_demo_execution_records_ledger(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        config.execution.default_demo_quantity = 0.1
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        quote = FakeQuote(price_gbp=250.0)
        result = PaperTradingService(db, config, FakeT212(), quote).execute_signal(signal["id"])

        self.assertTrue(result["ok"])
        orders = db.get_recent_paper_orders()
        positions = db.get_paper_positions()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "filled")
        self.assertLessEqual(orders[0]["quantity"] * orders[0]["estimated_price"], config.execution.max_order_gbp_demo)
        self.assertEqual(quote.requests[0]["ticker"], "NVDA")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticker"], "NVDA")

    def test_trading212_execution_normalizes_existing_suffix_signal(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        trade_id = db.insert_raw_trade(sample_trade())
        signal_id = db.insert_signal({
            "raw_trade_id": trade_id,
            "ticker": "MSFT:US",
            "politician_name": "Nancy Pelosi",
            "direction": "BUY",
            "alpha_score": 0.9,
            "score_breakdown": "{}",
            "vip_tier": 1,
            "filing_gap_days": 4,
            "suggested_action": "SUGGEST_TRADE",
            "model_version": "test",
            "expected_excess_return": 0.05,
            "confidence": 0.9,
            "tradeable": 1,
            "rank_order": 1,
            "execution_mode": "t212_demo",
            "decision_reason": "test",
            "feature_hash": "hash",
        })
        broker = FakeT212()
        quote = FakeQuote(price_gbp=250.0)

        result = PaperTradingService(db, config, broker, quote).execute_signal(signal_id)

        self.assertTrue(result["ok"])
        self.assertEqual(broker.orders[0]["ticker"], "MSFT_US_EQ")
        self.assertEqual(quote.requests[0]["ticker"], "MSFT")
        self.assertEqual(db.get_recent_paper_orders()[0]["ticker"], "MSFT")

    def test_trading212_position_uses_actual_fill_value(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(
            db,
            config,
            FakeT212(filled_value=90.0),
            FakeQuote(price_gbp=250.0),
        ).execute_signal(signal["id"])

        self.assertTrue(result["ok"])
        position = db.get_paper_positions()[0]
        self.assertAlmostEqual(position["notional_gbp"], 90.0)
        self.assertAlmostEqual(position["avg_price"], 225.0)

    def test_trading212_rejects_when_notional_cannot_be_enforced(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        broker = FakeT212()
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(db, config, broker, FakeQuote(price_gbp=None)).execute_signal(signal["id"])

        self.assertFalse(result["ok"])
        self.assertIn("Cannot enforce notional cap", result["reason"])
        self.assertEqual(broker.orders, [])

    def test_trading212_rejects_when_min_quantity_exceeds_notional(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        config.execution.max_order_gbp_demo = 100.0
        broker = FakeT212(min_trade_quantity=0.1)
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(db, config, broker, FakeQuote(price_gbp=2000.0)).execute_signal(signal["id"])

        self.assertFalse(result["ok"])
        self.assertIn("Minimum broker quantity", result["reason"])
        self.assertEqual(broker.orders, [])

    def test_duplicate_execute_returns_existing_order_without_rejecting_signal(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]
        service = PaperTradingService(db, config, FakeT212(), FakeQuote(price_gbp=250.0))

        first = service.execute_signal(signal["id"])
        second = service.execute_signal(signal["id"])

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(len(db.get_recent_paper_orders()), 1)
        self.assertEqual(db.get_signal_by_id(signal["id"])["status"], "executed")

    def test_sell_signal_is_rejected_by_long_only_gate(self):
        db = self.make_db()
        config = Config()
        trade = {**sample_trade(), "transaction_type": "Sale"}
        db.insert_raw_trade(trade)
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(db, config, FakeT212()).execute_signal(signal["id"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("Long-only", result["reason"])

    def test_demo_mode_rejects_live_trading212_environment(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        config.trading212.environment = "live"
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(db, config, FakeT212()).execute_signal(signal["id"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("requires trading212.environment='demo'", result["reason"])


class TelegramExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_callback_uses_paper_service(self):
        bot = TelegramBot(Config(), Database(Path(tempfile.mkdtemp()) / "test.db"), paper=FakePaper())
        query = FakeQuery()

        await bot._handle_execute(query, 42)

        self.assertIn("EXECUTION RECORDED", query.edited)
        self.assertIn("Ledger order id: 7", query.edited)


class AppLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_handles_cancelled_sleep_as_clean_shutdown(self):
        app = object.__new__(CapitolAlpha)
        app.config = Config()
        app.t212 = None
        app.bot = FakeLifecycleBot()
        app.scheduler = FakeScheduler()
        app.db = Database(Path(tempfile.mkdtemp()) / "test.db")
        app._shutdown_requested = False

        async def cancel_sleep(_seconds):
            raise asyncio.CancelledError()

        with (
            patch("src.main.setup_logging"),
            patch("src.main.create_dashboard_app", return_value=FakeDashboard()),
            patch("src.main.asyncio.sleep", cancel_sleep),
        ):
            await app.run()

        self.assertTrue(app.bot.started)
        self.assertTrue(app.bot.stopped)
        self.assertTrue(app.scheduler.shutdown_called)

    async def test_run_cycle_handles_keyboard_interrupt_as_shutdown(self):
        app = object.__new__(CapitolAlpha)
        app.pipeline = InterruptingPipeline()
        app._shutdown_requested = False

        await app.run_cycle()

        self.assertTrue(app._shutdown_requested)


if __name__ == "__main__":
    unittest.main()
