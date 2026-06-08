"""HTML parsing helpers for scraper-style data sources."""

import logging
from typing import Any

try:
    from bs4 import BeautifulSoup
    from bs4.exceptions import FeatureNotFound
except ModuleNotFoundError:
    BeautifulSoup = None
    FeatureNotFound = Exception


def html_parser_available() -> bool:
    return BeautifulSoup is not None


def parse_html(html: str, logger: logging.Logger) -> Any:
    """Parse HTML with lxml when available, otherwise use the stdlib parser."""
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is not installed")

    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        logger.info("lxml parser unavailable; falling back to html.parser")
        return BeautifulSoup(html, "html.parser")
