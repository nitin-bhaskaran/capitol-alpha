"""
Trump Family Trade Tracker.

Trump family members are NOT members of Congress, so they don't appear in
House/Senate Stock Watcher data. Their trades surface through:
  1. Executive branch financial disclosures (OGE Form 278e)
  2. News reports and SEC filings
  3. Capitol Trades (which tracks some executive branch figures)
  4. Manual additions from public reporting

This module provides:
  - A scraper for news-reported Trump family trades
  - A manual entry mechanism for high-conviction signals
  - Integration with the same scoring pipeline as congressional trades
"""

import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

try:
    import requests
except ModuleNotFoundError:
    requests = None

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None

from src.utils.helpers import (
    parse_amount_range,
    normalize_politician_name,
    parse_date,
)

logger = logging.getLogger("capitol_alpha.trump_tracker")

# Known Trump family members and associates to track
TRUMP_FAMILY = {
    "Donald J. Trump": {"role": "President", "chamber": "Executive"},
    "Donald Trump Jr.": {"role": "Business (Trump Org)", "chamber": "Executive"},
    "Eric Trump": {"role": "Business (Trump Org)", "chamber": "Executive"},
    "Ivanka Trump": {"role": "Former Advisor", "chamber": "Executive"},
    "Jared Kushner": {"role": "Former Advisor / Affinity Partners", "chamber": "Executive"},
}

# RSS / news sources to monitor for Trump family financial moves
NEWS_SOURCES = [
    {
        "name": "Capitol Trades - Executive",
        "url": "https://www.capitoltrades.com/trades?chamber=executive",
        "type": "scrape",
    },
]


def fetch_trump_trades_from_news() -> List[Dict[str, Any]]:
    """
    Scrape news sources for Trump family trade disclosures.
    This is inherently less structured than congressional data,
    so we cast a wide net and filter aggressively.
    """
    if requests is None or BeautifulSoup is None:
        logger.warning("requests/beautifulsoup4 not installed - skipping Trump tracker")
        return []

    logger.info("Checking for Trump family trade disclosures...")
    trades = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # Try Capitol Trades executive branch page
    try:
        resp = requests.get(
            "https://www.capitoltrades.com/trades",
            params={"chamber": "executive"},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        table = soup.find("table")
        if table:
            rows = table.find_all("tr")[1:]
            for row in rows:
                try:
                    cells = row.find_all("td")
                    if len(cells) < 7:
                        continue

                    raw_name = cells[0].get_text(strip=True)
                    name = normalize_politician_name(raw_name)

                    # Only keep if it's a Trump family member
                    matched_name = _match_trump_family(name)
                    if not matched_name:
                        continue

                    ticker_tag = cells[1].find("span")
                    ticker = ticker_tag.get_text(strip=True) if ticker_tag else ""
                    asset_desc = cells[1].get_text(strip=True)

                    filing_date = parse_date(cells[2].get_text(strip=True))
                    tx_date = parse_date(cells[3].get_text(strip=True))
                    size_text = cells[5].get_text(strip=True)
                    low, high = parse_amount_range(size_text)
                    tx_type = cells[6].get_text(strip=True)

                    if not ticker:
                        continue

                    trade = {
                        "source": "trump_tracker",
                        "politician_name": matched_name,
                        "politician_party": "R",
                        "politician_state": "",
                        "politician_chamber": "Executive",
                        "ticker": ticker,
                        "asset_description": asset_desc,
                        "transaction_type": tx_type,
                        "transaction_date": tx_date,
                        "filing_date": filing_date,
                        "amount_range": size_text,
                        "amount_low": low,
                        "amount_high": high,
                        "owner": "Self",
                        "comment": f"Trump family: {TRUMP_FAMILY.get(matched_name, {}).get('role', '')}",
                        "source_url": "https://www.capitoltrades.com/trades?chamber=executive",
                        "raw_json": json.dumps({"name": matched_name, "ticker": ticker}),
                    }
                    trades.append(trade)
                    logger.info(f"TRUMP TRADE FOUND: {matched_name} {tx_type} {ticker}")

                except Exception as e:
                    logger.debug(f"Skipping executive row: {e}")
                    continue

    except Exception as e:
        logger.warning(f"Failed to scrape executive trades: {e}")

    logger.info(f"Trump family trades found: {len(trades)}")
    return trades


def create_manual_trump_trade(
    name: str,
    ticker: str,
    transaction_type: str,
    transaction_date: str,
    amount_range: str = "",
    comment: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Manually add a Trump family trade from news reporting.

    Use this when you see a credible report of a Trump family
    financial move that hasn't appeared in structured data yet.

    Example (from Telegram bot):
      /trump_trade "Donald J. Trump" AAPL Purchase 2026-03-15 "$1M-$5M" "Per WSJ report"
    """
    matched = _match_trump_family(name)
    if not matched:
        logger.warning(f"Name '{name}' not in Trump family watchlist")
        return None

    low, high = parse_amount_range(amount_range)

    return {
        "source": "trump_tracker_manual",
        "politician_name": matched,
        "politician_party": "R",
        "politician_state": "",
        "politician_chamber": "Executive",
        "ticker": ticker.upper().strip(),
        "asset_description": "",
        "transaction_type": transaction_type,
        "transaction_date": parse_date(transaction_date),
        "filing_date": parse_date(datetime.now().strftime("%Y-%m-%d")),
        "amount_range": amount_range,
        "amount_low": low,
        "amount_high": high,
        "owner": "Self",
        "comment": f"Manual entry: {comment}",
        "source_url": "",
        "raw_json": json.dumps({"manual": True, "comment": comment}),
    }


def _match_trump_family(name: str) -> Optional[str]:
    """
    Fuzzy match a name against the Trump family watchlist.
    Returns the canonical name or None.
    """
    name_lower = name.lower().strip()

    for canonical in TRUMP_FAMILY:
        if canonical.lower() in name_lower or name_lower in canonical.lower():
            return canonical

    # Partial matching for common variations
    partials = {
        "trump jr": "Donald Trump Jr.",
        "don jr": "Donald Trump Jr.",
        "eric trump": "Eric Trump",
        "ivanka": "Ivanka Trump",
        "kushner": "Jared Kushner",
        "trump, donald": "Donald J. Trump",
    }

    for partial, canonical in partials.items():
        if partial in name_lower:
            return canonical

    return None
