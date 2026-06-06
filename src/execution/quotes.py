"""Market quote helpers for broker order sizing."""

import logging
import time
from typing import Any, Dict, Optional, Tuple

try:
    import requests
except ModuleNotFoundError:
    requests = None

from src.config import Config

logger = logging.getLogger("capitol_alpha.quotes")


class MarketQuoteService:
    """Fetches account-currency price estimates used to cap quantity orders."""

    def __init__(self, config: Config, session: Optional[Any] = None):
        self.config = config
        self.session = session or (requests.Session() if requests else None)
        self._quote_cache: Dict[str, Tuple[float, float]] = {}
        self._fx_cache: Dict[str, Tuple[float, float]] = {}
        self.cache_seconds = 60

    def get_unit_price_gbp(
        self,
        ticker: str,
        instrument: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """Return a best-effort latest unit price in GBP."""
        if not self.session:
            return None

        ticker = ticker.upper()
        cached = self._cache_get(self._quote_cache, ticker)
        if cached is not None:
            return cached

        quote = self._fetch_finnhub_quote(ticker, instrument)
        if quote is None:
            quote = self._fetch_yahoo_quote(ticker, instrument)
        if quote is None:
            return None

        price, currency = quote
        price_gbp = self._convert_to_gbp(price, currency)
        if price_gbp is None or price_gbp <= 0:
            return None

        self._quote_cache[ticker] = (price_gbp, time.time())
        return price_gbp

    def _fetch_finnhub_quote(
        self,
        ticker: str,
        instrument: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[float, str]]:
        token = self.config.data_sources.finnhub_api_token
        if not token:
            return None

        try:
            resp = self.session.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": token},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
            price = self._float_or_none(payload.get("c"))
            if price is None or price <= 0:
                return None
            return price, self._instrument_currency(instrument, "USD")
        except Exception as exc:
            logger.warning("Finnhub quote failed for %s: %s", ticker, exc)
            return None

    def _fetch_yahoo_quote(
        self,
        ticker: str,
        instrument: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[float, str]]:
        try:
            resp = self.session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"range": "1d", "interval": "1d"},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
            result = (payload.get("chart", {}).get("result") or [None])[0]
            if not result:
                return None

            meta = result.get("meta", {})
            price = self._first_float(
                meta,
                ("regularMarketPrice", "postMarketPrice", "preMarketPrice", "previousClose"),
            )
            if price is None or price <= 0:
                return None
            currency = str(
                meta.get("currency") or self._instrument_currency(instrument, "USD")
            ).upper()
            return price, currency
        except Exception as exc:
            logger.warning("Yahoo quote failed for %s: %s", ticker, exc)
            return None

    def _convert_to_gbp(self, price: float, currency: str) -> Optional[float]:
        currency = (currency or "GBP").upper()
        if currency == "GBP":
            return price
        if currency == "GBX":
            return price / 100.0

        rate = self._fx_rate(currency, "GBP")
        if rate is None:
            return None
        return price * rate

    def _fx_rate(self, source_currency: str, target_currency: str) -> Optional[float]:
        pair = f"{source_currency}_{target_currency}"
        cached = self._cache_get(self._fx_cache, pair)
        if cached is not None:
            return cached

        try:
            resp = self.session.get(
                "https://api.frankfurter.app/latest",
                params={"from": source_currency, "to": target_currency},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
            rate = self._float_or_none(payload.get("rates", {}).get(target_currency))
            if rate is None or rate <= 0:
                return None
            self._fx_cache[pair] = (rate, time.time())
            return rate
        except Exception as exc:
            logger.warning("FX lookup failed for %s/%s: %s", source_currency, target_currency, exc)
            return None

    def _cache_get(self, cache: Dict[str, Tuple[float, float]], key: str) -> Optional[float]:
        item = cache.get(key)
        if not item:
            return None
        value, created_at = item
        if time.time() - created_at > self.cache_seconds:
            cache.pop(key, None)
            return None
        return value

    def _instrument_currency(
        self,
        instrument: Optional[Dict[str, Any]],
        default: str,
    ) -> str:
        if not instrument:
            return default
        return str(instrument.get("currencyCode") or default).upper()

    def _first_float(self, payload: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
        for key in keys:
            value = self._float_or_none(payload.get(key))
            if value is not None:
                return value
        return None

    def _float_or_none(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
