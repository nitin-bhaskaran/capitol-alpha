"""
Capitol Trades scraper.
Scrapes https://www.capitoltrades.com/trades for recent disclosures.
Free, no API key needed.
"""

import json
import logging
from typing import List, Dict, Any

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

logger = logging.getLogger("capitol_alpha.capitol_trades")

BASE_URL = "https://www.capitoltrades.com/trades"


def fetch_capitol_trades(pages: int = 3) -> List[Dict[str, Any]]:
    """
    Scrape recent trades from Capitol Trades.
    Fetches the specified number of pages (default: 3, ~150 trades).
    """
    if requests is None or BeautifulSoup is None:
        logger.warning("requests/beautifulsoup4 not installed - skipping Capitol Trades")
        return []

    logger.info(f"Scraping Capitol Trades ({pages} pages)...")

    trades = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    for page in range(1, pages + 1):
        try:
            url = f"{BASE_URL}?page={page}"
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "lxml")

            # Capitol Trades renders a table with trade data
            # The structure may change — this targets the main trades table
            table = soup.find("table")
            if not table:
                logger.warning(f"No table found on page {page}")
                continue

            rows = table.find_all("tr")[1:]  # Skip header row
            for row in rows:
                try:
                    cells = row.find_all("td")
                    if len(cells) < 7:
                        continue

                    # Extract data from cells
                    # Column order: Politician, Traded Issuer, Published, Traded,
                    #               Filing Gap, Trade Size, Trade Type
                    politician_cell = cells[0]
                    issuer_cell = cells[1]
                    published_cell = cells[2]
                    traded_cell = cells[3]
                    gap_cell = cells[4]
                    size_cell = cells[5]
                    type_cell = cells[6]

                    # Politician name and party
                    name_tag = politician_cell.find("a") or politician_cell
                    raw_name = name_tag.get_text(strip=True)
                    name = normalize_politician_name(raw_name)

                    # Party indicator (usually a small badge)
                    party_badge = politician_cell.find(
                        "span", class_=lambda c: c and "party" in c.lower()
                    ) if politician_cell else None
                    party = ""
                    if party_badge:
                        party_text = party_badge.get_text(strip=True).upper()
                        if "R" in party_text:
                            party = "R"
                        elif "D" in party_text:
                            party = "D"

                    # Chamber
                    chamber = ""
                    chamber_badge = politician_cell.find(
                        "span", class_=lambda c: c and "chamber" in str(c).lower()
                    ) if politician_cell else None
                    if chamber_badge:
                        ct = chamber_badge.get_text(strip=True).lower()
                        chamber = "Senate" if "sen" in ct else "House"

                    # Issuer / ticker
                    ticker_tag = issuer_cell.find("span", class_=lambda c: c and "ticker" in str(c).lower())
                    ticker = ticker_tag.get_text(strip=True) if ticker_tag else ""
                    asset_desc = issuer_cell.get_text(strip=True)

                    # Dates
                    filing_date = parse_date(published_cell.get_text(strip=True))
                    tx_date = parse_date(traded_cell.get_text(strip=True))

                    # Size and type
                    size_text = size_cell.get_text(strip=True)
                    low, high = parse_amount_range(size_text)
                    tx_type = type_cell.get_text(strip=True)

                    if not ticker or not tx_type:
                        continue

                    trade = {
                        "source": "capitol_trades",
                        "politician_name": name,
                        "politician_party": party,
                        "politician_state": "",
                        "politician_chamber": chamber,
                        "ticker": ticker,
                        "asset_description": asset_desc,
                        "transaction_type": tx_type,
                        "transaction_date": tx_date,
                        "filing_date": filing_date,
                        "amount_range": size_text,
                        "amount_low": low,
                        "amount_high": high,
                        "owner": "",
                        "comment": "",
                        "source_url": BASE_URL,
                        "raw_json": json.dumps({"page": page, "name": name, "ticker": ticker}),
                    }
                    trades.append(trade)

                except Exception as e:
                    logger.debug(f"Skipping row: {e}")
                    continue

            logger.info(f"Page {page}: extracted {len(rows)} rows")

        except Exception as e:
            logger.warning(f"Failed to scrape Capitol Trades page {page}: {e}")
            continue

    logger.info(f"Total Capitol Trades scraped: {len(trades)}")
    return trades
