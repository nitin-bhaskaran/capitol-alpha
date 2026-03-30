"""
House Stock Watcher data ingestion.
Free API — bulk JSON from S3, no auth needed.
Source: https://housestockwatcher.com/api
"""

import json
import logging
import requests
from typing import List, Dict, Any

from src.utils.helpers import (
    parse_amount_range,
    normalize_politician_name,
    parse_date,
)

logger = logging.getLogger("capitol_alpha.house_watcher")

DEFAULT_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/data/all_transactions.json"
)


def fetch_house_trades(url: str = DEFAULT_URL) -> List[Dict[str, Any]]:
    """
    Fetch all House representative trades from the S3 bulk JSON.

    Returns a list of dicts normalised to our raw_trades schema.
    """
    logger.info("Fetching House trades from S3...")

    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        raw_records = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch House trades: {e}")
        return []

    logger.info(f"Downloaded {len(raw_records)} raw House records")

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
            logger.warning(f"Skipping malformed House record: {e}")
            continue

    logger.info(f"Parsed {len(trades)} valid House trades")
    return trades
