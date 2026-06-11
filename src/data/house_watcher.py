"""House Stock Watcher data ingestion."""

import json
import logging
from typing import Any, Dict, List

try:
    import requests
except ModuleNotFoundError:
    requests = None

from src.utils.helpers import (
    normalize_politician_name,
    parse_amount_range,
    parse_date,
)

logger = logging.getLogger("capitol_alpha.house_watcher")

LEGACY_S3_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/data/all_transactions.json"
)
DEFAULT_URL = ""
REQUEST_HEADERS = {
    "User-Agent": "capitol-alpha/0.1 (+https://github.com/nitin-bhaskaran/capitol-alpha)",
    "Accept": "application/json,text/plain,*/*",
}


def fetch_house_trades(url: str = DEFAULT_URL) -> List[Dict[str, Any]]:
    """
    Fetch all House representative trades from a configured bulk JSON source.

    The historical House Stock Watcher S3 feed now returns 403, so the default
    config leaves this direct feed disabled and relies on Capitol Trades for
    current House coverage.
    """
    if requests is None:
        logger.warning("requests is not installed - skipping House Stock Watcher")
        return []

    source_url = (url or DEFAULT_URL or "").strip()
    if not source_url:
        logger.info("House Stock Watcher direct feed disabled; Capitol Trades remains enabled")
        return []

    if source_url == LEGACY_S3_URL:
        logger.warning(
            "House Stock Watcher legacy S3 feed is retired/forbidden; "
            "skipping direct House fetch and relying on Capitol Trades"
        )
        return []

    logger.info("Fetching House trades from configured source...")

    try:
        resp = requests.get(source_url, headers=REQUEST_HEADERS, timeout=60)
        resp.raise_for_status()
        raw_records = resp.json()
    except Exception as e:
        logger.warning("Failed to fetch House trades from %s: %s", source_url, e)
        return []

    logger.info("Downloaded %s raw House records", len(raw_records))

    trades = []
    for rec in raw_records:
        try:
            ticker = rec.get("ticker", "").strip()
            if not ticker or ticker == "--" or ticker == "N/A":
                continue

            tx_type = rec.get("type", "")
            if not tx_type:
                continue

            name = normalize_politician_name(rec.get("representative", ""))
            if not name:
                continue

            amount_str = rec.get("amount", "")
            low, high = parse_amount_range(amount_str)

            trade = {
                "source": "house_watcher",
                "politician_name": name,
                "politician_party": rec.get("party", ""),
                "politician_state": rec.get("state", ""),
                "politician_chamber": "House",
                "ticker": ticker,
                "asset_description": rec.get("asset_description", ""),
                "transaction_type": tx_type,
                "transaction_date": parse_date(rec.get("transaction_date", "")),
                "filing_date": parse_date(rec.get("disclosure_date", "")),
                "amount_range": amount_str,
                "amount_low": low,
                "amount_high": high,
                "owner": rec.get("owner", ""),
                "comment": rec.get("comment", ""),
                "source_url": rec.get("ptr_link", ""),
                "raw_json": json.dumps(rec),
            }
            trades.append(trade)

        except Exception as e:
            logger.warning("Skipping malformed House record: %s", e)
            continue

    logger.info("Parsed %s valid House trades", len(trades))
    return trades
