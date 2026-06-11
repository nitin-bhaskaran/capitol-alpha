"""Senate Stock Watcher data ingestion."""

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

logger = logging.getLogger("capitol_alpha.senate_watcher")

LEGACY_S3_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/aggregate/all_transactions.json"
)
GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/timothycarambat/"
    "senate-stock-watcher-data/master/aggregate/all_transactions.json"
)
DEFAULT_URL = GITHUB_RAW_URL
REQUEST_HEADERS = {
    "User-Agent": "capitol-alpha/0.1 (+https://github.com/nitin-bhaskaran/capitol-alpha)",
    "Accept": "application/json,text/plain,*/*",
}


def fetch_senate_trades(url: str = DEFAULT_URL) -> List[Dict[str, Any]]:
    """
    Fetch all Senate trades from the bulk JSON mirror.

    The historical S3 endpoint can return 403. The current free mirror is the
    GitHub raw aggregate maintained with the Senate Stock Watcher data repo.
    """
    if requests is None:
        logger.warning("requests is not installed - skipping Senate Stock Watcher")
        return []

    logger.info("Fetching Senate trades...")

    try:
        raw_records = _fetch_first_json(_candidate_urls(url))
    except RuntimeError as e:
        logger.warning("Failed to fetch Senate trades: %s", e)
        return []

    logger.info("Downloaded %s raw Senate records", len(raw_records))

    trades = []
    for rec in raw_records:
        try:
            trades.extend(_parse_senate_record(rec))
        except Exception as e:
            logger.warning("Skipping malformed Senate record: %s", e)
            continue

    logger.info("Parsed %s valid Senate trades", len(trades))
    return trades


def _candidate_urls(url: str) -> List[str]:
    requested = (url or DEFAULT_URL or "").strip()
    if not requested or requested == LEGACY_S3_URL:
        return [GITHUB_RAW_URL]
    if requested == GITHUB_RAW_URL:
        return [GITHUB_RAW_URL]
    return [requested, GITHUB_RAW_URL]


def _fetch_first_json(urls: List[str]) -> List[Dict[str, Any]]:
    failures = []
    for source_url in urls:
        try:
            resp = requests.get(source_url, headers=REQUEST_HEADERS, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Senate trades source: %s", source_url)
            return data
        except Exception as exc:
            failures.append(f"{source_url}: {exc}")
            logger.warning("Senate source failed: %s: %s", source_url, exc)
    raise RuntimeError("; ".join(failures))


def _parse_senate_record(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if "transactions" in rec:
        senator_name = normalize_politician_name(
            f"{rec.get('first_name', '')} {rec.get('last_name', '')}".strip()
        )
        if not senator_name:
            return []
        filing_date = parse_date(rec.get("date_recieved", ""))  # sic: source typo
        trades = []
        for tx in rec.get("transactions", []):
            trade = _normalise_transaction(
                tx,
                senator_name,
                rec.get("party", ""),
                rec.get("state", ""),
                filing_date,
                rec.get("ptr_link", ""),
            )
            if trade:
                trades.append(trade)
        return trades

    senator_name = normalize_politician_name(rec.get("senator", ""))
    if not senator_name:
        return []
    filing_date = parse_date(
        rec.get("disclosure_date", "")
        or rec.get("date_recieved", "")
        or rec.get("filing_date", "")
        or rec.get("transaction_date", "")
    )
    trade = _normalise_transaction(
        rec,
        senator_name,
        rec.get("party", ""),
        rec.get("state", ""),
        filing_date,
        rec.get("ptr_link", ""),
    )
    return [trade] if trade else []


def _normalise_transaction(
    tx: Dict[str, Any],
    senator_name: str,
    party: str,
    state: str,
    filing_date: str,
    ptr_link: str,
) -> Dict[str, Any]:
    ticker = (tx.get("ticker") or "").strip()
    if not ticker or ticker == "--" or ticker == "N/A":
        return {}

    tx_type = tx.get("type", "")
    if not tx_type or tx_type == "N/A":
        return {}

    amount_str = tx.get("amount", "")
    low, high = parse_amount_range(amount_str)

    return {
        "source": "senate_watcher",
        "politician_name": senator_name,
        "politician_party": party,
        "politician_state": state,
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
