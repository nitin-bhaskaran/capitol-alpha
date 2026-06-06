"""Feature generation for politician-disclosure event alpha."""

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.config import Config
from src.utils.helpers import (
    filing_gap_days,
    midpoint_amount,
    normalize_ticker,
    transaction_to_direction,
)


SECTOR_BY_TICKER = {
    "AAPL": "technology",
    "MSFT": "technology",
    "GOOG": "technology",
    "GOOGL": "technology",
    "META": "technology",
    "AMZN": "technology",
    "ADBE": "technology",
    "CRM": "technology",
    "NOW": "technology",
    "ORCL": "technology",
    "IBM": "technology",
    "CSCO": "technology",
    "PANW": "technology",
    "CRWD": "technology",
    "SNOW": "technology",
    "PLTR": "technology",
    "NET": "technology",
    "NVDA": "semiconductors",
    "AMD": "semiconductors",
    "INTC": "semiconductors",
    "TSM": "semiconductors",
    "AVGO": "semiconductors",
    "QCOM": "semiconductors",
    "TXN": "semiconductors",
    "ADI": "semiconductors",
    "MU": "semiconductors",
    "MRVL": "semiconductors",
    "LRCX": "semiconductors",
    "KLAC": "semiconductors",
    "ASML": "semiconductors",
    "AMAT": "semiconductors",
    "ON": "semiconductors",
    "NXPI": "semiconductors",
    "MCHP": "semiconductors",
    "SNDK": "semiconductors",
    "LITE": "semiconductors",
    "FN": "semiconductors",
    "LMT": "defense",
    "RTX": "defense",
    "NOC": "defense",
    "BA": "defense",
    "GD": "defense",
    "LHX": "defense",
    "HII": "defense",
    "LDOS": "defense",
    "SAIC": "defense",
    "KTOS": "defense",
    "AVAV": "defense",
    "JPM": "financials",
    "BAC": "financials",
    "GS": "financials",
    "MS": "financials",
    "C": "financials",
    "WFC": "financials",
    "V": "financials",
    "MA": "financials",
    "AXP": "financials",
    "BLK": "financials",
    "BX": "financials",
    "SCHW": "financials",
    "COF": "financials",
    "USB": "financials",
    "PNC": "financials",
    "CME": "financials",
    "XOM": "energy",
    "CVX": "energy",
    "COP": "energy",
    "SLB": "energy",
    "EOG": "energy",
    "OXY": "energy",
    "PSX": "energy",
    "VLO": "energy",
    "MPC": "energy",
    "HAL": "energy",
    "LNG": "energy",
    "KMI": "energy",
    "PFE": "healthcare",
    "JNJ": "healthcare",
    "UNH": "healthcare",
    "MRK": "healthcare",
    "ABBV": "healthcare",
    "LLY": "healthcare",
    "ABT": "healthcare",
    "MDT": "healthcare",
    "TMO": "healthcare",
    "DHR": "healthcare",
    "ISRG": "healthcare",
    "BMY": "healthcare",
    "AMGN": "healthcare",
    "GILD": "healthcare",
    "CVS": "healthcare",
    "ELV": "healthcare",
    "HUM": "healthcare",
    "CAT": "industrials",
    "DE": "industrials",
    "URI": "industrials",
    "HON": "industrials",
    "GE": "industrials",
    "ETN": "industrials",
    "EMR": "industrials",
    "WMT": "consumer",
    "COST": "consumer",
    "HD": "consumer",
    "LOW": "consumer",
    "MCD": "consumer",
    "TSLA": "consumer",
    "DIS": "communications",
    "NFLX": "communications",
    "T": "communications",
    "VZ": "communications",
    "CMCSA": "communications",
    "SPY": "broad_market",
    "QQQ": "broad_market",
    "IWM": "broad_market",
    "DIA": "broad_market",
    "VOO": "broad_market",
    "VTI": "broad_market",
    "IVV": "broad_market",
    "XLK": "technology",
    "XLF": "financials",
    "XLE": "energy",
    "XLI": "industrials",
    "XLV": "healthcare",
}


SECTOR_COMMITTEE_MAP = {
    "defense": ["Armed Services", "Appropriations", "Foreign Affairs"],
    "technology": ["Intelligence", "Judiciary", "Energy and Commerce"],
    "semiconductors": [
        "Armed Services",
        "Energy and Commerce",
        "Science, Space, and Technology",
    ],
    "financials": ["Financial Services", "Banking, Housing, and Urban Affairs"],
    "energy": ["Energy and Commerce", "Natural Resources"],
    "healthcare": [
        "Energy and Commerce",
        "Ways and Means",
        "Health, Education, Labor, and Pensions",
    ],
    "industrials": ["Transportation and Infrastructure", "Appropriations"],
    "consumer": ["Energy and Commerce"],
    "communications": ["Energy and Commerce", "Judiciary"],
    "broad_market": ["Appropriations", "Finance"],
}


SECTOR_KEYWORDS = {
    "semiconductors": [
        "semiconductor",
        "chip",
        "microchip",
        "integrated circuit",
        "photonics",
    ],
    "technology": [
        "software",
        "cloud",
        "cybersecurity",
        "internet",
        "technology",
        "data",
        "artificial intelligence",
    ],
    "defense": ["defense", "aerospace", "missile", "weapons", "shipbuilding"],
    "financials": ["bank", "financial", "capital", "payments", "exchange"],
    "energy": ["energy", "oil", "gas", "pipeline", "solar", "renewable"],
    "healthcare": ["pharma", "biotech", "medical", "health", "therapeutics"],
    "industrials": ["industrial", "machinery", "construction", "railroad"],
    "consumer": ["retail", "restaurant", "consumer", "automotive"],
    "communications": ["media", "telecom", "entertainment", "streaming"],
}


SOURCE_QUALITY = {
    "house_watcher": 0.90,
    "senate_watcher": 0.90,
    "capitol_trades": 0.76,
    "trump_tracker": 0.45,
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def amount_score(midpoint: float) -> float:
    if midpoint <= 0:
        return 0.10
    if midpoint < 15_000:
        return 0.25
    if midpoint < 50_000:
        return 0.40
    if midpoint < 100_000:
        return 0.58
    if midpoint < 250_000:
        return 0.75
    if midpoint < 500_000:
        return 0.88
    return 1.0


def filing_speed_score(gap: Optional[int]) -> float:
    if gap is None:
        return 0.35
    if gap <= 5:
        return 1.0
    if gap <= 15:
        return 0.78
    if gap <= 30:
        return 0.52
    if gap <= 45:
        return 0.30
    return 0.12


def vip_score_and_tier(politician: str, config: Config) -> Tuple[float, Optional[int]]:
    name_lower = politician.lower()
    for vip_name in config.vip_watchlist.tier1_politicians:
        if vip_name.lower() in name_lower or name_lower in vip_name.lower():
            return 1.0, 1
    for vip_name in config.vip_watchlist.tier2_politicians:
        if vip_name.lower() in name_lower or name_lower in vip_name.lower():
            return 0.68, 2
    return 0.25, None


def infer_sector(ticker: str, asset_description: str = "") -> str:
    sector = SECTOR_BY_TICKER.get(ticker.upper())
    if sector:
        return sector

    description = (asset_description or "").lower()
    for candidate, keywords in SECTOR_KEYWORDS.items():
        if any(keyword in description for keyword in keywords):
            return candidate
    return "unknown"


def committee_score(ticker: str, config: Config, asset_description: str = "") -> float:
    sector = infer_sector(ticker, asset_description)
    relevant = SECTOR_COMMITTEE_MAP.get(sector, [])
    if not relevant:
        return 0.36
    watched = set(config.vip_watchlist.high_signal_committees)
    return 0.82 if any(comm in watched for comm in relevant) else 0.38


def recency_score(filing_date: Optional[str]) -> float:
    if not filing_date:
        return 0.4
    try:
        fd = datetime.strptime(filing_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.4
    age_days = max(0, (datetime.now(timezone.utc) - fd).days)
    return clamp(math.exp(-age_days / 21.0), 0.05, 1.0)


def feature_hash(features: Dict[str, Any]) -> str:
    canonical = json.dumps(features, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_event_features(trade: Dict[str, Any], config: Config) -> Dict[str, Any]:
    ticker = normalize_ticker(trade.get("ticker") or "")
    asset_description = trade.get("asset_description") or ""
    politician = trade.get("politician_name") or ""
    direction = transaction_to_direction(trade.get("transaction_type", ""))
    gap = filing_gap_days(trade.get("transaction_date", ""), trade.get("filing_date", ""))
    midpoint = midpoint_amount(trade.get("amount_low"), trade.get("amount_high"))
    vip, tier = vip_score_and_tier(politician, config)
    sector = infer_sector(ticker, asset_description)
    source = trade.get("source") or "unknown"
    source_quality = SOURCE_QUALITY.get(source, 0.55)
    chamber = (trade.get("politician_chamber") or "unknown").lower()

    features = {
        "ticker": ticker,
        "sector": sector,
        "source": source,
        "source_quality": source_quality,
        "politician_name": politician,
        "politician_chamber": chamber,
        "politician_party": trade.get("politician_party") or "",
        "direction": direction,
        "is_buy": 1 if direction == "BUY" else 0,
        "is_sell": 1 if direction == "SELL" else 0,
        "filing_gap_days": gap,
        "filing_speed_score": filing_speed_score(gap),
        "amount_midpoint": midpoint,
        "amount_score": amount_score(midpoint),
        "vip_score": vip,
        "vip_tier": tier,
        "committee_score": committee_score(ticker, config, asset_description),
        "recency_score": recency_score(trade.get("filing_date")),
    }
    features["feature_hash"] = feature_hash(features)
    return features
