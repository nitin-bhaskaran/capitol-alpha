"""
Utility functions for Capitol Alpha.
"""

import re
import logging
from datetime import datetime
from typing import Tuple, Optional

logger = logging.getLogger("capitol_alpha")


class SensitiveLogFilter(logging.Filter):
    """Redact credentials that third-party clients may put into log messages."""

    TELEGRAM_BOT_URL = re.compile(r"(https://api\.telegram\.org/bot)[^/\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.TELEGRAM_BOT_URL.sub(r"\1<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def parse_amount_range(amount_str: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Parse STOCK Act amount ranges like '$1,001 - $15,000' into (low, high) floats.
    Returns (None, None) if unparseable.
    """
    if not amount_str:
        return None, None

    # Clean up the string
    cleaned = amount_str.replace(",", "").replace("$", "").strip()

    # Common patterns:
    # "$1,001 - $15,000"
    # "$15,001 - $50,000"
    # "$50,001 - $100,000"
    # "$100,001 - $250,000"
    # "$250,001 - $500,000"
    # "$500,001 - $1,000,000"
    # "$1,000,001 - $5,000,000"
    # "Over $5,000,000"

    match = re.match(r"(\d+)\s*-\s*(\d+)", cleaned)
    if match:
        return float(match.group(1)), float(match.group(2))

    if "over" in cleaned.lower():
        nums = re.findall(r"\d+", cleaned)
        if nums:
            val = float(nums[0])
            return val, val * 2  # Rough upper estimate

    return None, None


def midpoint_amount(low: Optional[float], high: Optional[float]) -> float:
    """Get midpoint of amount range, useful for scoring."""
    if low is not None and high is not None:
        return (low + high) / 2
    if low is not None:
        return low
    if high is not None:
        return high
    return 0.0


def normalize_politician_name(name: str) -> str:
    """
    Normalise politician names for matching across data sources.
    e.g., "Pelosi, Nancy" -> "Nancy Pelosi"
          "Hon. Nancy Pelosi" -> "Nancy Pelosi"
    """
    if not name:
        return ""

    # Remove titles
    name = re.sub(r"^(Hon\.|Rep\.|Sen\.|Representative|Senator)\s*", "", name.strip())

    # Handle "Last, First" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2:
            name = f"{parts[1]} {parts[0]}"

    # Clean up extra whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def parse_date(date_str: str) -> Optional[str]:
    """Try to parse various date formats into YYYY-MM-DD."""
    if not date_str:
        return None

    formats = [
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%dT%H:%M:%S",
        "%b %d, %Y",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            # Sanity check: reject dates before 2010 or far in future
            if dt.year < 2010 or dt.year > 2030:
                continue
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def filing_gap_days(transaction_date: str, filing_date: str) -> Optional[int]:
    """Calculate days between transaction and filing."""
    td = parse_date(transaction_date)
    fd = parse_date(filing_date)
    if not td or not fd:
        return None
    try:
        delta = datetime.strptime(fd, "%Y-%m-%d") - datetime.strptime(td, "%Y-%m-%d")
        return max(0, delta.days)
    except ValueError:
        return None


def transaction_to_direction(tx_type: str) -> str:
    """Map transaction types to BUY/SELL."""
    tx = tx_type.lower().strip()
    if any(kw in tx for kw in ["purchase", "buy"]):
        return "BUY"
    if any(kw in tx for kw in ["sale", "sell"]):
        return "SELL"
    return "UNKNOWN"


def setup_logging():
    """Configure logging for the application."""
    from src.config import LOG_DIR
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    sensitive_filter = SensitiveLogFilter()
    file_handler = logging.FileHandler(LOG_DIR / "capitol_alpha.log")
    stream_handler = logging.StreamHandler()
    file_handler.addFilter(sensitive_filter)
    stream_handler.addFilter(sensitive_filter)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[file_handler, stream_handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger("capitol_alpha")
