"""Trading212 official API client.

Uses the official Trading212 Public API:
  - HTTP Basic auth with API key and secret
  - Demo and live base URLs
  - Equity market, limit, stop, and stop-limit order endpoints
  - Local rate limiting around order placement

Start with environment="demo" in config.
"""

import base64
import logging
import time
from typing import Any, Dict, List, Optional

try:
    import requests
except ModuleNotFoundError:
    requests = None

from src.config import Trading212Config

logger = logging.getLogger("capitol_alpha.trading212")


class Trading212Client:
    """Client for the official Trading212 API."""

    def __init__(self, config: Trading212Config):
        if requests is None:
            raise RuntimeError("requests is required to use Trading212Client")

        self.config = config
        self.base_url = config.base_url
        self.session = requests.Session()

        credentials = f"{config.api_key}:{config.api_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        self.session.headers.update({
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/json",
        })

        self._last_order_time = 0.0
        self._orders_today = 0

        logger.info(
            "Trading212 client initialised (env=%s, base=%s)",
            config.environment,
            self.base_url,
        )

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Make an authenticated API request with error handling."""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
            remaining = resp.headers.get("x-ratelimit-remaining", "?")
            logger.debug("%s %s -> %s (remaining: %s)", method, path, resp.status_code, remaining)

            if resp.status_code == 429:
                reset_time = resp.headers.get("x-ratelimit-reset", "")
                logger.warning("Rate limited. Reset at: %s", reset_time)
                return {"error": "rate_limited", "reset": reset_time}

            resp.raise_for_status()
            return resp.json() if resp.text else {}

        except requests.exceptions.HTTPError as e:
            logger.error("HTTP error %s %s: %s", method, path, e)
            return {"error": str(e)}
        except Exception as e:
            logger.error("Request failed %s %s: %s", method, path, e)
            return {"error": str(e)}

    def get_account_summary(self) -> Dict[str, Any]:
        """Get account investment summary."""
        return self._request("GET", "/api/v0/equity/account/summary")

    def get_account_cash(self) -> Dict[str, Any]:
        """Get available cash."""
        return self._request("GET", "/api/v0/equity/account/cash")

    def get_instruments(self) -> List[Dict[str, Any]]:
        """Get all tradable instruments. Call sparingly."""
        result = self._request("GET", "/api/v0/equity/metadata/instruments")
        if isinstance(result, list):
            return result
        return result.get("items", result.get("data", []))

    def find_instrument(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Find a Trading212 instrument by ticker symbol."""
        instruments = self.get_instruments()
        ticker_upper = ticker.upper()

        for inst in instruments:
            t212_ticker = inst.get("ticker", "")
            short_name = inst.get("shortName", "")
            if t212_ticker == f"{ticker_upper}_US_EQ" or short_name.upper() == ticker_upper:
                return inst

        for inst in instruments:
            if ticker_upper in inst.get("ticker", "").upper():
                return inst

        logger.warning("Instrument not found for ticker: %s", ticker)
        return None

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions."""
        result = self._request("GET", "/api/v0/equity/positions")
        if isinstance(result, dict) and result.get("error"):
            result = self._request("GET", "/api/v0/equity/portfolio")
        if isinstance(result, list):
            return result
        return result.get("items", [])

    def get_position(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get position for a specific Trading212 ticker."""
        for pos in self.get_positions():
            if pos.get("ticker", "") == ticker:
                return pos
        return None

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get currently open or pending orders."""
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
        """Place a market order. Positive quantity buys, negative quantity sells."""
        self._enforce_rate_limit(min_interval=2.0)
        self._check_daily_limit()

        payload = {
            "ticker": t212_ticker,
            "quantity": quantity,
            "extendedHours": extended_hours,
        }
        logger.info("Placing MARKET order: %s qty=%s", t212_ticker, quantity)
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
        """Place a limit order. Positive quantity buys, negative quantity sells."""
        self._enforce_rate_limit(min_interval=2.0)
        self._check_daily_limit()

        payload = {
            "ticker": t212_ticker,
            "quantity": quantity,
            "limitPrice": limit_price,
            "extendedHours": extended_hours,
        }
        logger.info("Placing LIMIT order: %s qty=%s @ %s", t212_ticker, quantity, limit_price)
        result = self._request("POST", "/api/v0/equity/orders/limit", json=payload)
        self._orders_today += 1
        return result

    def place_stop_order(
        self,
        t212_ticker: str,
        quantity: float,
        stop_price: float,
    ) -> Dict[str, Any]:
        """Place a stop order."""
        self._enforce_rate_limit(min_interval=2.0)

        payload = {
            "ticker": t212_ticker,
            "quantity": quantity,
            "stopPrice": stop_price,
        }
        logger.info("Placing STOP order: %s qty=%s stop=%s", t212_ticker, quantity, stop_price)
        return self._request("POST", "/api/v0/equity/orders/stop", json=payload)

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        """Cancel an open order."""
        logger.info("Cancelling order: %s", order_id)
        return self._request("DELETE", f"/api/v0/equity/orders/{order_id}")

    def get_order_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get historical orders."""
        result = self._request("GET", f"/api/v0/equity/history/orders?limit={limit}")
        if isinstance(result, list):
            return result
        return result.get("items", [])

    def _enforce_rate_limit(self, min_interval: float = 2.0):
        """Ensure order requests are not sent too quickly."""
        elapsed = time.time() - self._last_order_time
        if elapsed < min_interval:
            sleep_time = min_interval - elapsed
            logger.debug("Rate limiting: sleeping %.1fs", sleep_time)
            time.sleep(sleep_time)
        self._last_order_time = time.time()

    def _check_daily_limit(self):
        """Check the local daily order cap."""
        if self._orders_today >= self.config.max_daily_orders:
            raise RuntimeError(
                f"Daily order limit reached ({self.config.max_daily_orders}). "
                "Increase max_daily_orders in config to override."
            )

    def reset_daily_counter(self):
        """Reset the daily order counter."""
        self._orders_today = 0
