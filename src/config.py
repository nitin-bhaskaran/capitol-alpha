"""
Configuration loader for Capitol Alpha.
Reads config.yaml and provides typed dataclass access.
"""

import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
DB_FILE = PROJECT_ROOT / "data" / "cta.db"
LOG_DIR = PROJECT_ROOT / "logs"


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    admin_user_ids: List[int] = field(default_factory=list)


@dataclass
class Trading212Config:
    api_key: str = ""
    api_secret: str = ""
    environment: str = "demo"
    base_url: str = ""
    max_order_gbp: float = 500.0
    max_daily_orders: int = 5
    default_order_type: str = "limit"
    limit_offset_pct: float = 0.5

    def __post_init__(self):
        if not self.base_url:
            self.base_url = (
                "https://live.trading212.com"
                if self.environment == "live"
                else "https://demo.trading212.com"
            )


@dataclass
class ScoringConfig:
    vip_weight: float = 0.30
    committee_weight: float = 0.20
    filing_gap_weight: float = 0.20
    trade_size_weight: float = 0.15
    historical_alpha_weight: float = 0.15
    min_score_to_alert: float = 0.40
    min_score_to_suggest_trade: float = 0.60


@dataclass
class DataSourceConfig:
    house_watcher_url: str = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
    senate_watcher_url: str = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"
    quiver_api_token: str = ""
    finnhub_api_token: str = ""
    poll_interval_minutes: int = 30
    capitol_trades_enabled: bool = True


@dataclass
class VIPWatchlist:
    tier1_politicians: List[str] = field(default_factory=lambda: [
        "Donald J. Trump", "Donald Trump Jr.", "Eric Trump",
        "Ivanka Trump", "Jared Kushner",
        "Nancy Pelosi", "Dan Crenshaw", "Tommy Tuberville",
        "Marjorie Taylor Greene",
    ])
    tier2_politicians: List[str] = field(default_factory=lambda: [
        "Michael McCaul", "Josh Gottheimer", "Mark Green",
        "Ro Khanna", "Kevin Hern", "Scott Franklin", "John Curtis",
    ])
    high_signal_committees: List[str] = field(default_factory=lambda: [
        "Armed Services", "Financial Services", "Energy and Commerce",
        "Intelligence", "Ways and Means", "Appropriations",
        "Foreign Affairs", "Judiciary",
    ])


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    trading212: Trading212Config = field(default_factory=Trading212Config)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    data_sources: DataSourceConfig = field(default_factory=DataSourceConfig)
    vip_watchlist: VIPWatchlist = field(default_factory=VIPWatchlist)


def load_config(config_path: Optional[Path] = None) -> Config:
    """Load configuration from YAML file."""
    path = config_path or CONFIG_FILE
    if not path.exists():
        print(f"[WARNING] Config not found at {path} — using defaults.")
        print(f"  Copy config/config_template.yaml to config/config.yaml")
        return Config()

    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    config = Config()

    # Telegram
    tg = raw.get("telegram", {})
    config.telegram = TelegramConfig(
        bot_token=tg.get("bot_token", ""),
        chat_id=str(tg.get("chat_id", "")),
        admin_user_ids=tg.get("admin_user_ids", []),
    )

    # Trading212
    t = raw.get("trading212", {})
    config.trading212 = Trading212Config(
        api_key=t.get("api_key", ""),
        api_secret=t.get("api_secret", ""),
        environment=t.get("environment", "demo"),
        max_order_gbp=t.get("max_order_gbp", 500.0),
        max_daily_orders=t.get("max_daily_orders", 5),
        default_order_type=t.get("default_order_type", "limit"),
        limit_offset_pct=t.get("limit_offset_pct", 0.5),
    )

    # Scoring
    sc = raw.get("scoring", {})
    config.scoring = ScoringConfig(
        **{k: sc[k] for k in sc if hasattr(ScoringConfig, k)}
    )

    # Data sources
    ds = raw.get("data_sources", {})
    config.data_sources = DataSourceConfig(
        **{k: ds[k] for k in ds if hasattr(DataSourceConfig, k)}
    )

    # VIP Watchlist
    vip = raw.get("vip_watchlist", {})
    if vip:
        defaults = VIPWatchlist()
        config.vip_watchlist = VIPWatchlist(
            tier1_politicians=vip.get("tier1_politicians", defaults.tier1_politicians),
            tier2_politicians=vip.get("tier2_politicians", defaults.tier2_politicians),
            high_signal_committees=vip.get("high_signal_committees", defaults.high_signal_committees),
        )

    return config
