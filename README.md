# Capitol Alpha

**Congress trades alpha generator — ingest, score, alert via Telegram, execute via Trading212.**

Tracks US politician stock trades (including Trump family), scores them for alpha potential, sends Telegram alerts with approve/reject buttons, and executes via Trading212's official API.

## Architecture

```
┌─────────────────┐    ┌──────────────┐    ┌───────────────┐    ┌──────────────┐
│  Data Pipeline   │───>│   Scoring    │───>│   Telegram    │───>│  Trading212  │
│                  │    │   Engine     │    │   Bot         │    │  Execution   │
│ - House Watcher  │    │              │    │               │    │              │
│ - Senate Watcher │    │ - VIP Weight │    │ - Signal Push │    │ - Semi-Auto  │
│ - Capitol Trades │    │ - Committee  │    │ - Approve     │    │ - Limit Ord  │
│ - Trump Family   │    │ - Filing Gap │    │ - Reject      │    │ - Market Ord │
│   Tracker        │    │ - Trade Size │    │ - Status      │    │ - Portfolio  │
└─────────────────┘    └──────────────┘    └───────────────┘    └──────────────┘
        |                                                              |
        v                                                              v
┌─────────────────┐                                          ┌──────────────┐
│   SQLite DB     │                                          │  Dashboard   │
│  (Local Store)  │                                          │  (Flask Web) │
└─────────────────┘                                          └──────────────┘
```

## Quick Start (Raspberry Pi)

```bash
# 1. Clone
git clone https://github.com/nitin-bhaskaran/capitol-alpha.git
cd capitol-alpha

# 2. Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp config/config_template.yaml config/config.yaml
nano config/config.yaml  # Add your API keys

# 4. Run
python -m src.main
```

See `docs/SETUP.md` for detailed Raspberry Pi deployment with systemd.

## Data Sources

| Source | Cost | Coverage | Update Frequency |
|--------|------|----------|-----------------|
| House Stock Watcher | Free | House reps | Daily |
| Senate Stock Watcher | Free | Senators | Daily |
| Capitol Trades (scrape) | Free | Both chambers | ~Daily |
| Trump Family Tracker | Free | Executive branch | On filing |
| QuiverQuant API | Paid (optional) | Enhanced data | Real-time |

## Trading212 Integration

Uses the **official Trading212 API** (not unofficial hacks):
- Market orders, Limit orders, Stop orders
- Portfolio sync and position tracking
- Basic Auth with API Key + Secret
- Rate limited: respect 1 req/2s for orders

**Always start with `environment: demo` in config until you trust the system.**

## Key Files

```
src/
  config.py          # Configuration loader
  database.py        # SQLite schema and queries
  main.py            # Main orchestrator / entry point
  data/
    house_watcher.py # House Stock Watcher ingestion
    senate_watcher.py# Senate Stock Watcher ingestion
    capitol_trades.py# Capitol Trades scraper
    trump_tracker.py # Trump family trade tracker
  scoring/
    engine.py        # Alpha scoring engine
  alerts/
    telegram_bot.py  # Telegram bot with inline keyboards
  execution/
    trading212.py    # Trading212 API client
  dashboard/
    app.py           # Flask dashboard (optional)
```
