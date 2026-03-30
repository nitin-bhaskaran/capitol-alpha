"""
Senate Stock Watcher data ingestion.
Free API — bulk JSON from S3, no auth needed.
Source: https://senatestockwatcher.com/api
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

logger = logging.getLogger("capitol_alpha.senate_watcher")

DEFAULT_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/aggregate/all_transactions.json"
)


def fetch_senate_trades(url: str = DEFAULT_URL) -> List[Dict[str, Any]]:
    """
    Fetch all Senate trades from the S3 bulk JSON.

    The Senate data format is slightly different from House:
    each record has senator info plus a 'transactions' array.
    """
    logger.info("Fetching Senate trades from S3...")

    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        raw_records = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch Senate trades: {e}")
        return []

    logger.info(f"Downloaded {len(raw_records)} raw Senate records")

    trades = []
    for rec in raw_records:
        try:
            # Senate data: each record is a senator with nested transactions
            senator_name = normalize_politician_name(
                f"{rec.get('first_name', '')} {rec.get('last_name', '')}".strip()
            )
            if not senator_name:
                continue

            ptr_link = rec.get("ptr_link", "")
            filing_date = parse_date(rec.get("date_recieved", ""))  # sic: typo in API

            for tx in rec.get("transactions", []):
                ticker = tx.get("ticker", "").strip()
                if not ticker or ticker == "--" or ticker == "N/A":
                    continue

                tx_type = tx.get("type", "")
                if not tx_type:
                    continue

                amount_str = tx.get("amount", "")
                low, high = parse_amount_range(amount_str)

                trade = {
                    "source": "senate_watcher",
                    "politician_name": senator_name,
                    "politician_party": rec.get("party", ""),
                    "politician_state": rec.get("state", ""),
                    "politician_chamber": "Senate",
                    "ticker": ticker,
                    "asset_description": tx.get("asset_description", ""),
                    "transaction_type": tx_type,
                    "transaction_date": parse_date(tx.get("transaction_date", "")),
                    "filing_date": filing_date,
                    "amount_range": amount_str,
                    "amount_low": low,
                    "amount_high": high,
                    "owner": tx.get("owner", ""),
                    "comment": tx.get("comment", ""),
                    "source_url": ptr_link,
                    "raw_json": json.dumps(tx),
                }
                trades.append(trade)

        except Exception as e:
            logger.warning(f"Skipping malformed Senate record: {e}")
            continue

    logger.info(f"Parsed {len(trades)} valid Senate trades")
    return trades
