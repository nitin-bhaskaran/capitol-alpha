"""
Trading212 Official API Client.

Uses the official Trading212 API (https://docs.trading212.com):
  - Basic Auth with API Key + Secret
  - Market, Limit, Stop, Stop-Limit orders
  - Portfolio and position tracking
  - Rate limits respected

IMPORTANT: Always start with environment="demo" in config.
"""

import base64
import logging
import time
import requests
from typing import Optional, Dict, Any, List

from src.config import Trading212Config

logger = logging.getLogger("capitol_alpha.trading212")


class Trading212Client:
    """Client for the official Trading212 API."""

    def __init__(self, config: Trading212Config):
        self.config = config
        self.base_url = config.base_url
        self.session = requests.Session()

        # Build Basic Auth header
        credentials = f"{config.api_key}:{config.api_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        self.session.headers.update({
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/json",
        })

        # Rate limiting state
        self._last_order_time = 0
        self._orders_today = 0

        logger.info(
            f"Trading212 client initialised "
            f"(env={config.environment}, base={self.base_url})"
        )

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Make an authenticated API request with error handling."""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)

            # Log rate limit headers
            remaining = resp.headers.get("x-ratelimit-remaining", "?")
            logger.debug(f"{method} {path} -> {resp.status_code} (remaining: {remaining})")

            if resp.status_code == 429:
                reset_time = resp.headers.get("x-ratelimit-reset", "")
                logger.warning(f"Rate limited! Reset at: {reset_time}")
                return {"error": "rate_limited", "reset": reset_time}

            resp.raise_for_status()
            return resp.json() if resp.text else {}

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error {method} {path}: {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Request failed {method} {path}: {e}")
            return {"error": str(e)}

    # ── Account ───────────────────────────────────────────────────

    def get_account_summary(self) -> Dict[str, Any]:
        """Get account cash balance and investment summary."""
        return self._request("GET", "/api/v0/equity/account/summary")

    def get_account_cash(self) -> Dict[str, Any]:
        """Get available cash."""
        return self._request("GET", "/api/v0/equity/account/cash")

    # ── Instruments ───────────────────────────────────────────────

    def get_instruments(self) -> List[Dict]:
        """Get all tradable instruments. Cached — call sparingly."""
        result = self._request("GET", "/api/v0/equity/metadata/instruments")
        if isinstance(result, list):
            return result
        return result.get("items", result.get("data", []))

    def find_instrument(self, ticker: str) -> Optional[Dict]:
        """
        Find a Trading212 instrument by ticker symbol.
        T212 uses tickers like 'AAPL_US_EQ' internally.
        """
        instruments = self.get_instruments()
        ticker_upper = ticker.upper()

        # Try exact match first
        for inst in instruments:
            t212_ticker = inst.get("ticker", "")
            short_name = inst.get("shortName", "")
            if (
                t212_ticker == f"{ticker_upper}_US_EQ"
                or short_name.upper() == ticker_upper
            ):
                return inst

        # Fuzzy match
        for inst in instruments:
            if ticker_upper in inst.get("ticker", "").upper():
                return inst

        logger.warning(f"Instrument not found for ticker: {ticker}")
        return None

    # ── Positions ─────────────────────────────────────────────────

    def get_positions(self) -> List[Dict]:
        """Get all open positions."""
        result = self._request("GET", "/api/v0/equity/portfolio")
        if isinstance(result, list):
            return result
        return result.get("items", [])

    def get_position(self, ticker: str) -> Optional[Dict]:
        """Get position for a specific T212 ticker."""
        positions = self.get_positions()
        for pos in positions:
            if pos.get("ticker", "") == ticker:
                return pos
        return None

    # ── Orders ────────────────────────────────────────────────────

    def get_open_orders(self) -> List[Dict]:
        """Get all currently open/pending orders."""
        result = self._request("GET", "/api/v0/equity/orders")
        if isinstance(result, list):
            return result
        return result.get("items", [])

    def place_market_order(
        self,
        t212_ticker: str,
        quantity: float,
        extended_hours: bool = False,
    ) -> Dict[str, Any]:
        """
        Place a market order.
        Use positive quantity for BUY, negative for SELL.
        Rate limit: 1 req / 2s
        """
        self._enforce_rate_limit(min_interval=2.0)
        self._check_daily_limit()

        payload = {
            "ticker": t212_ticker,
            "quantity": quantity,
            "extendedHours": extended_hours,
        }
        logger.info(f"Placing MARKET order: {t212_ticker} qty={quantity}")
        result = self._request("POST", "/api/v0/equity/orders/market", json=payload)
        self._orders_today += 1
        return result

    def place_limit_order(
        self,
        t212_ticker: str,
        quantity: float,
        limit_price: float,
        extended_hours: bool = False,
    ) -> Dict[str, Any]:
        """
        Place a limit order.
        BUY: positive quantity, fills at limitPrice or lower.
        SELL: negative quantity, fills at limitPrice or higher.
        Rate limit: 1 req / 2s
        """
        self._enforce_rate_limit(min_interval=2.0)
        self._check_daily_limit()

        payload = {
            "ticker": t212_ticker,
            "quantity": quantity,
            "limitPrice": limit_price,
            "extendedHours": extended_hours,
        }
        logger.info(
            f"Placing LIMIT order: {t212_ticker} qty={quantity} @ {limit_price}"
        )
        result = self._request("POST", "/api/v0/equity/orders/limit", json=payload)
        self._orders_today += 1
        return result

    def place_stop_order(
        self,
        t212_ticker: str,
        quantity: float,
        stop_price: float,
    ) -> Dict[str, Any]:
        """
        Place a stop order (e.g., stop-loss).
        SELL stop-loss: negative quantity.
        Rate limit: 1 req / 2s
        """
        self._enforce_rate_limit(min_interval=2.0)

        payload = {
            "ticker": t212_ticker,
            "quantity": quantity,
            "stopPrice": stop_price,
        }
        logger.info(
            f"Placing STOP order: {t212_ticker} qty={quantity} stop={stop_price}"
        )
        return self._request("POST", "/api/v0/equity/orders/stop", json=payload)

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        """Cancel an open order."""
        logger.info(f"Cancelling order: {order_id}")
        return self._request("DELETE", f"/api/v0/equity/orders/{order_id}")

    # ── Order History ─────────────────────────────────────────────

    def get_order_history(self, limit: int = 50) -> List[Dict]:
        """Get historical orders."""
        result = self._request(
            "GET", f"/api/v0/equity/history/orders?limit={limit}"
        )
        return result.get("items", [])

    # ── Safety Controls ───────────────────────────────────────────

    def _enforce_rate_limit(self, min_interval: float = 2.0):
        """Ensure we don't exceed the order rate limit."""
        elapsed = time.time() - self._last_order_time
        if elapsed < min_interval:
            sleep_time = min_interval - elapsed
            logger.debug(f"Rate limiting: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)
        self._last_order_time = time.time()

    def _check_daily_limit(self):
        """Check if we've hit the daily order limit."""
        if self._orders_today >= self.config.max_daily_orders:
            raise RuntimeError(
                f"Daily order limit reached ({self.config.max_daily_orders}). "
                f"Increase max_daily_orders in config to override."
            )

    def reset_daily_counter(self):
        """Reset the daily order counter (call at midnight)."""
        self._orders_today = 0
