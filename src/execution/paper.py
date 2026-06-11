"""Paper/demo execution service with local ledger authority."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.config import Config
from src.database import Database
from src.execution.quotes import MarketQuoteService
from src.execution.trading212 import Trading212Client
from src.scoring.features import SECTOR_BY_TICKER
from src.utils.helpers import normalize_ticker


ACTIVE_ORDER_STATUSES = {
    "submitted",
    "confirmed",
    "new",
    "filled",
    "partially_filled",
    "partial",
}


class RiskGate:
    """Conservative risk gate for long-only paper/demo execution."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    def evaluate(self, signal: Dict[str, Any], intended_notional: float) -> Tuple[bool, str]:
        if signal.get("direction") != "BUY":
            return False, "Long-only mode blocks non-BUY signals."
        if not signal.get("tradeable"):
            return False, "Signal is not model-tradeable."
        if (signal.get("confidence") or 0) < self.config.model.min_trade_confidence:
            return False, "Confidence is below execution threshold."
        if (signal.get("expected_excess_return") or 0) < self.config.model.min_expected_excess_return:
            return False, "Expected excess return is below execution hurdle."

        positions = self.db.get_paper_positions()
        if len(positions) >= self.config.risk.max_positions:
            held = {p["ticker"] for p in positions}
            if signal["ticker"] not in held:
                return False, "Max position count reached."

        equity = max(1.0, self.config.risk.starting_equity_gbp)
        max_single = equity * self.config.risk.max_single_name_exposure_pct / 100.0
        existing = next((p for p in positions if p["ticker"] == signal["ticker"]), None)
        current_notional = existing["notional_gbp"] if existing else 0.0
        if current_notional + intended_notional > max_single:
            return False, "Single-name exposure limit would be exceeded."

        sector = SECTOR_BY_TICKER.get(signal["ticker"], "unknown")
        sector_notional = sum(
            p["notional_gbp"] for p in positions if (p.get("sector") or "unknown") == sector
        )
        max_sector = equity * self.config.risk.max_sector_exposure_pct / 100.0
        if sector_notional + intended_notional > max_sector:
            return False, "Sector exposure limit would be exceeded."

        return True, "Risk gate passed."


class PaperTradingService:
    """Executes approved signals through local paper or Trading212 demo/live mode."""

    def __init__(
        self,
        db: Database,
        config: Config,
        t212: Optional[Trading212Client] = None,
        quote_service: Optional[MarketQuoteService] = None,
    ):
        self.db = db
        self.config = config
        self.t212 = t212
        self.quote_service = quote_service or MarketQuoteService(config)
        self.risk = RiskGate(db, config)

    def execute_signal(self, signal_id: int) -> Dict[str, Any]:
        signal = self.db.get_signal_by_id(signal_id)
        if not signal:
            return self._rejected_stub(signal_id, "Signal not found.")
        signal = self._normalised_signal(signal)

        duplicate = self._get_active_order(signal_id)
        if duplicate:
            return self._duplicate_order_result(duplicate)

        mode = self.config.execution.mode
        if mode == "disabled":
            return self._record_rejection(signal, "Execution mode is disabled.")
        if mode == "t212_live" and not self._live_allowed():
            return self._record_rejection(signal, "Live execution gate is locked.")
        if mode in {"t212_demo", "t212_live"}:
            env_allowed, env_reason = self._environment_matches_mode(mode)
            if not env_allowed:
                return self._record_rejection(signal, env_reason)

        notional = self._target_notional()
        allowed, reason = self.risk.evaluate(signal, notional)
        if not allowed:
            return self._record_rejection(signal, reason)

        if mode == "local_paper":
            return self._execute_local(signal, notional)
        if mode in {"t212_demo", "t212_live"}:
            return self._execute_trading212(signal, notional, mode)
        return self._record_rejection(signal, f"Unknown execution mode: {mode}")

    def _execute_local(self, signal: Dict[str, Any], notional: float) -> Dict[str, Any]:
        quantity = self.config.execution.default_demo_quantity
        estimated_price = notional / max(quantity, 0.000001)
        now = self._now()
        order_id = self.db.insert_paper_order({
            "signal_id": signal["id"],
            "execution_mode": "local_paper",
            "broker": "local",
            "broker_order_id": f"LOCAL-{signal['id']}-{int(datetime.now().timestamp())}",
            "ticker": signal["ticker"],
            "t212_ticker": None,
            "side": "BUY",
            "order_type": "market",
            "status": "filled",
            "quantity": quantity,
            "intended_notional_gbp": notional,
            "estimated_price": estimated_price,
            "limit_price": None,
            "fill_price": estimated_price,
            "fill_quantity": quantity,
            "slippage_bps": 0.0,
            "fees_gbp": 0.0,
            "pnl_gbp": 0.0,
            "rejection_reason": None,
            "request": {"local": True},
            "response": {"status": "filled"},
            "submitted_at": now,
            "filled_at": now,
        })
        self._upsert_position(signal, None, quantity, estimated_price, notional)
        self.db.update_signal_status(signal["id"], "executed")
        return {"ok": True, "order_id": order_id, "status": "filled", "reason": "Local paper fill."}

    def _execute_trading212(
        self,
        signal: Dict[str, Any],
        notional: float,
        mode: str,
    ) -> Dict[str, Any]:
        if not self.t212:
            return self._record_rejection(signal, "Trading212 client is not configured.")

        instrument = self.t212.find_instrument(signal["ticker"])
        if not instrument:
            return self._record_rejection(signal, f"Trading212 instrument not found for {signal['ticker']}.")

        t212_ticker = instrument.get("ticker")
        estimated_price = self._estimate_unit_price(signal, instrument)
        if estimated_price is None:
            return self._record_rejection(
                signal,
                "Cannot enforce notional cap without an account-currency price estimate.",
            )

        quantity, sizing_error = self._order_quantity(instrument, notional, estimated_price)
        if sizing_error:
            return self._record_rejection(signal, sizing_error)

        estimated_notional = float(quantity or 0) * estimated_price
        order_type = self.config.execution.default_order_type.lower()
        request = {
            "ticker": t212_ticker,
            "quantity": quantity,
            "order_type": order_type,
            "mode": mode,
            "notional_cap_gbp": notional,
            "estimated_price": estimated_price,
            "estimated_notional_gbp": estimated_notional,
        }

        now = self._now()
        if order_type == "market":
            response = self.t212.place_market_order(t212_ticker, quantity)
        else:
            return self._record_rejection(signal, "MVP execution supports market orders only.")

        status = self._normalise_status(response)
        broker_order_id = str(response.get("id") or response.get("orderId") or "")
        fill_quantity = self._float_or_none(response.get("filledQuantity"))
        fill_value = self._float_or_none(response.get("filledValue"))
        fill_price = self._first_float(
            response,
            ("fillPrice", "filledPrice", "averagePrice", "avgFillPrice", "price"),
        )
        if fill_price is None and fill_quantity and fill_value is not None:
            fill_price = fill_value / max(fill_quantity, 0.000001)
        actual_notional = fill_value
        if actual_notional is None and fill_price is not None and fill_quantity:
            actual_notional = fill_price * fill_quantity

        order_id = self.db.insert_paper_order({
            "signal_id": signal["id"],
            "execution_mode": mode,
            "broker": "trading212",
            "broker_order_id": broker_order_id,
            "ticker": signal["ticker"],
            "t212_ticker": t212_ticker,
            "side": "BUY",
            "order_type": order_type,
            "status": status,
            "quantity": quantity,
            "intended_notional_gbp": notional,
            "estimated_price": estimated_price,
            "limit_price": None,
            "fill_price": fill_price,
            "fill_quantity": fill_quantity,
            "slippage_bps": 0.0,
            "fees_gbp": 0.0,
            "pnl_gbp": 0.0,
            "rejection_reason": response.get("error"),
            "request": request,
            "response": response,
            "submitted_at": now,
            "filled_at": now if status == "filled" else None,
        })

        if status == "filled" and fill_price and fill_quantity and actual_notional is not None:
            self._upsert_position(
                signal,
                t212_ticker,
                fill_quantity,
                fill_price,
                actual_notional,
            )
            self.db.update_signal_status(signal["id"], "executed")
        elif status == "rejected":
            self.db.update_signal_status(signal["id"], "rejected")
        else:
            self.db.update_signal_status(signal["id"], "approved")

        return {"ok": "error" not in response, "order_id": order_id, "status": status, "response": response}

    def _record_rejection(self, signal: Dict[str, Any], reason: str) -> Dict[str, Any]:
        order_id = self.db.insert_paper_order({
            "signal_id": signal["id"],
            "execution_mode": self.config.execution.mode,
            "broker": "none",
            "broker_order_id": None,
            "ticker": signal["ticker"],
            "t212_ticker": None,
            "side": signal.get("direction") or "UNKNOWN",
            "order_type": self.config.execution.default_order_type,
            "status": "rejected",
            "quantity": 0,
            "intended_notional_gbp": 0,
            "estimated_price": None,
            "limit_price": None,
            "fill_price": None,
            "fill_quantity": None,
            "slippage_bps": 0.0,
            "fees_gbp": 0.0,
            "pnl_gbp": 0.0,
            "rejection_reason": reason,
            "request": {},
            "response": {"error": reason},
            "submitted_at": self._now(),
            "filled_at": None,
        })
        self.db.update_signal_status(signal["id"], "rejected")
        return {"ok": False, "order_id": order_id, "status": "rejected", "reason": reason}

    def _rejected_stub(self, signal_id: int, reason: str) -> Dict[str, Any]:
        return {"ok": False, "signal_id": signal_id, "status": "rejected", "reason": reason}

    def _normalised_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        clean = dict(signal)
        clean["ticker"] = normalize_ticker(clean.get("ticker") or "")
        return clean

    def _get_active_order(self, signal_id: int) -> Optional[Dict[str, Any]]:
        orders = self.db.get_recent_paper_orders(limit=500)
        return next(
            (
                order for order in orders
                if order["signal_id"] == signal_id
                and (order["status"] or "").lower() in ACTIVE_ORDER_STATUSES
            ),
            None,
        )

    def _duplicate_order_result(self, order: Dict[str, Any]) -> Dict[str, Any]:
        status = (order.get("status") or "").lower()
        return {
            "ok": status == "filled",
            "order_id": order["id"],
            "status": status,
            "reason": "Existing paper order already recorded for signal.",
        }

    def _live_allowed(self) -> bool:
        if not self.config.execution.live_enabled:
            return False
        report = self.db.get_latest_validation_report()
        return bool(report and report.get("live_ready"))

    def _environment_matches_mode(self, mode: str) -> Tuple[bool, str]:
        environment = (self.config.trading212.environment or "").lower()
        if mode == "t212_demo" and environment != "demo":
            return False, "t212_demo requires trading212.environment='demo'."
        if mode == "t212_live" and environment != "live":
            return False, "t212_live requires trading212.environment='live'."
        return True, "Trading212 environment matches execution mode."

    def _target_notional(self) -> float:
        if self.config.execution.mode == "t212_live":
            return self.config.execution.max_order_gbp_live
        return self.config.execution.max_order_gbp_demo

    def _order_quantity(
        self,
        instrument: Dict[str, Any],
        notional: float,
        estimated_price: float,
    ) -> Tuple[Optional[float], Optional[str]]:
        if estimated_price <= 0:
            return None, "Price estimate must be positive to size broker order."

        min_qty = float(instrument.get("minTradeQuantity") or 0.0)
        if not self.config.execution.allow_fractional_shares:
            min_qty = max(1.0, min_qty)

        max_quantity = notional / estimated_price
        if max_quantity <= 0:
            return None, "Configured order notional is too small."

        if self.config.execution.allow_fractional_shares:
            quantity = int(max_quantity * 1_000_000) / 1_000_000
            if quantity < min_qty:
                return None, "Minimum broker quantity would exceed configured notional cap."
        else:
            quantity = int(max_quantity)
            if quantity < min_qty:
                return None, "Whole-share quantity would exceed configured notional cap."

        estimated_notional = quantity * estimated_price
        if estimated_notional > notional + 0.01:
            return None, "Calculated broker quantity would exceed configured notional cap."
        return quantity, None

    def _estimate_unit_price(
        self,
        signal: Dict[str, Any],
        instrument: Dict[str, Any],
    ) -> Optional[float]:
        price = self.quote_service.get_unit_price_gbp(signal["ticker"], instrument)
        if price and price > 0:
            return price

        price = self._first_float(
            instrument,
            (
                "estimatedPriceGbp",
                "priceEstimateGbp",
                "currentPriceGbp",
                "currentPrice",
                "lastPrice",
                "ask",
                "bid",
                "price",
            ),
        )
        if price and price > 0:
            return price

        existing = next(
            (p for p in self.db.get_paper_positions() if p["ticker"] == signal["ticker"]),
            None,
        )
        if existing and existing.get("avg_price"):
            return float(existing["avg_price"])

        if self.t212 and hasattr(self.t212, "get_position"):
            position = self.t212.get_position(instrument.get("ticker"))
            if position:
                return self._first_float(
                    position,
                    (
                        "currentPriceGbp",
                        "currentPrice",
                        "averagePrice",
                        "avgPrice",
                        "price",
                    ),
                )
        return None

    def _first_float(self, payload: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
        for key in keys:
            value = self._float_or_none(payload.get(key))
            if value is not None:
                return value
        return None

    def _float_or_none(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, dict):
            for nested_key in ("amount", "value", "price"):
                parsed = self._float_or_none(value.get(nested_key))
                if parsed is not None:
                    return parsed
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalise_status(self, response: Dict[str, Any]) -> str:
        if response.get("error"):
            return "rejected"
        raw = str(response.get("status") or "submitted").lower()
        return raw.replace("partially_filled", "partial").replace(" ", "_")

    def _upsert_position(
        self,
        signal: Dict[str, Any],
        t212_ticker: Optional[str],
        quantity: float,
        price: float,
        notional: float,
    ):
        existing = next(
            (p for p in self.db.get_paper_positions() if p["ticker"] == signal["ticker"]),
            None,
        )
        old_qty = existing["quantity"] if existing else 0.0
        old_notional = existing["notional_gbp"] if existing else 0.0
        new_qty = old_qty + quantity
        new_notional = old_notional + notional
        avg_price = new_notional / max(new_qty, 0.000001)
        self.db.upsert_paper_position({
            "ticker": signal["ticker"],
            "t212_ticker": t212_ticker,
            "quantity": new_qty,
            "avg_price": avg_price if price else None,
            "notional_gbp": new_notional,
            "realized_pnl_gbp": existing["realized_pnl_gbp"] if existing else 0.0,
            "unrealized_pnl_gbp": existing["unrealized_pnl_gbp"] if existing else 0.0,
            "sector": SECTOR_BY_TICKER.get(signal["ticker"], "unknown"),
        })

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
