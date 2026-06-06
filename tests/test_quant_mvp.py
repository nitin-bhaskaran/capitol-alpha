import tempfile
import unittest
from pathlib import Path

from src.alerts.telegram_bot import TelegramBot
from src.config import Config
from src.database import Database
from src.execution.paper import PaperTradingService
from src.scoring.engine import ScoringEngine
from src.scoring.features import build_event_features
from src.scoring.model import EventAlphaEnsemble


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
    def __init__(self, price=250.0, filled_value=None, min_trade_quantity=0.1):
        self.price = price
        self.filled_value = filled_value
        self.min_trade_quantity = min_trade_quantity
        self.orders = []

    def find_instrument(self, ticker):
        return {
            "ticker": f"{ticker}_US_EQ",
            "minTradeQuantity": self.min_trade_quantity,
            "currentPrice": self.price,
        }

    def place_market_order(self, t212_ticker, quantity, extended_hours=False):
        self.orders.append({"ticker": t212_ticker, "quantity": quantity})
        return {
            "id": 12345,
            "status": "FILLED",
            "ticker": t212_ticker,
            "filledQuantity": quantity,
            "filledValue": self.filled_value if self.filled_value is not None else quantity * self.price,
        }


class FakeT212WithoutPrice(FakeT212):
    def find_instrument(self, ticker):
        return {"ticker": f"{ticker}_US_EQ", "minTradeQuantity": self.min_trade_quantity}


class FakePaper:
    def execute_signal(self, signal_id):
        return {"ok": True, "order_id": 7, "status": "filled"}


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

        result = PaperTradingService(db, config, FakeT212()).execute_signal(signal["id"])

        self.assertTrue(result["ok"])
        orders = db.get_recent_paper_orders()
        positions = db.get_paper_positions()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "filled")
        self.assertLessEqual(orders[0]["quantity"] * orders[0]["estimated_price"], config.execution.max_order_gbp_demo)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticker"], "NVDA")

    def test_trading212_position_uses_actual_fill_value(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(db, config, FakeT212(price=250.0, filled_value=90.0)).execute_signal(signal["id"])

        self.assertTrue(result["ok"])
        position = db.get_paper_positions()[0]
        self.assertAlmostEqual(position["notional_gbp"], 90.0)
        self.assertAlmostEqual(position["avg_price"], 225.0)

    def test_trading212_rejects_when_notional_cannot_be_enforced(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        broker = FakeT212WithoutPrice()
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(db, config, broker).execute_signal(signal["id"])

        self.assertFalse(result["ok"])
        self.assertIn("Cannot enforce notional cap", result["reason"])
        self.assertEqual(broker.orders, [])

    def test_trading212_rejects_when_min_quantity_exceeds_notional(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        config.execution.max_order_gbp_demo = 100.0
        broker = FakeT212(price=2000.0, min_trade_quantity=0.1)
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]

        result = PaperTradingService(db, config, broker).execute_signal(signal["id"])

        self.assertFalse(result["ok"])
        self.assertIn("Minimum broker quantity", result["reason"])
        self.assertEqual(broker.orders, [])

    def test_duplicate_execute_returns_existing_order_without_rejecting_signal(self):
        db = self.make_db()
        config = Config()
        config.execution.mode = "t212_demo"
        db.insert_raw_trade(sample_trade())
        signal = ScoringEngine(db, config).score_unscored_trades()[0]
        service = PaperTradingService(db, config, FakeT212())

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


if __name__ == "__main__":
    unittest.main()
