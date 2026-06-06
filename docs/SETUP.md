# Capitol Alpha Setup Guide

This guide covers the local-first MVP: run Capitol Alpha on this machine, start Trading212 demo or local paper trading, collect outcomes, and only consider live trading after validation gates pass.

## Prerequisites

- Python 3.11+
- Internet access for disclosure ingestion and Trading212 demo execution
- Optional Telegram bot credentials for alerts and approve/execute callbacks
- Optional Trading212 demo API key for broker-backed paper trading

## Local Setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config\config_template.yaml config\config.yaml
```

On Linux or Raspberry Pi:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/config_template.yaml config/config.yaml
```

Edit `config/config.yaml` and set:

```yaml
execution:
  mode: "t212_demo"
  live_enabled: false
```

Use `local_paper` if you want to validate without sending orders to Trading212 demo.

## Trading212 Demo

Create an API key from the Trading212 app and keep it in your local config. Use the demo environment first.

The app supports these execution modes:

- `disabled`: no execution.
- `local_paper`: simulated local ledger fills.
- `t212_demo`: Trading212 demo orders plus local ledger.
- `t212_live`: live orders, locked by validation gates.

Do not set `execution.live_enabled: true` until the validation report says live is ready and you have manually reviewed the ledger.

## Telegram

Telegram is optional but useful for reviewing signals.

1. Create a bot with `@BotFather`.
2. Add `bot_token`, `chat_id`, and `admin_user_ids` to `config/config.yaml`.
3. Start the app with `python -m src.main`.

The `EXECUTE NOW` button now routes through the paper/demo execution layer. It will not place live orders unless live mode is explicitly configured and unlocked.

## Running The App

```bash
python -m src.main
```

Expected behavior:

- Disclosure feeds are ingested.
- Features are generated and hashed.
- Signals are ranked by the event-alpha model.
- Risk gates decide whether a signal is tradeable.
- Demo/local paper orders are written to the local ledger.
- Dashboard is available at `http://localhost:5055`.

## Research Workflow

Backfill outcomes from local prices:

```bash
python -m src.research backfill-outcomes --prices data/prices.csv
```

Train the model artifact:

```bash
python -m src.research train --output models/event_alpha_model.json
```

Generate a validation report:

```bash
python -m src.research report
```

Price CSV format:

```text
ticker,date,close,benchmark_close
AAPL,2026-06-01,195.00,5200.00
```

`benchmark_close` is optional. If absent, the report records raw forward returns as excess returns.

## Quote Sizing

Trading212 orders are submitted by quantity, while the risk config is expressed in GBP notional caps. Before sending a Trading212 demo/live market order, Capitol Alpha fetches a quote, converts it to GBP if needed, and sizes the quantity so it stays under `execution.max_order_gbp_demo` or `execution.max_order_gbp_live`.

Set `data_sources.finnhub_api_token` for the preferred quote path. If it is blank, the app tries a no-key Yahoo chart fallback and Frankfurter FX conversion. If no quote is available, the ledger records a rejection and no broker order is sent.

## Validation Gates

Live Trading212 execution remains blocked until a validation report confirms all gates:

- At least 10 trading days of paper/demo trading.
- No critical execution bugs or duplicate-order incidents.
- Orders are recorded in both broker/demo history and local ledger when using `t212_demo`.
- Positive net paper P&L after estimated slippage.
- Max paper drawdown within configured limits.
- At least 20 paper trade decisions, or a documented insufficient-signal warning.
- Calibration report exists for the active model.
- Manual review confirms no obvious data leakage, ticker mapping errors, or stale disclosure handling.

## Dashboard

Open:

```text
http://localhost:5055
```

Tabs:

- Overview
- Signals
- Paper Ledger
- Model
- Live Readiness

## Raspberry Pi Service

After local validation, you can run the same app as a Pi service.

Create `/etc/systemd/system/capitol-alpha.service`:

```ini
[Unit]
Description=Capitol Alpha
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/capitol-alpha
Environment=PATH=/home/pi/capitol-alpha/.venv/bin:/usr/bin:/bin
ExecStart=/home/pi/capitol-alpha/.venv/bin/python -m src.main
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/pi/capitol-alpha/data /home/pi/capitol-alpha/logs

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable capitol-alpha
sudo systemctl start capitol-alpha
journalctl -u capitol-alpha -f
```

## Troubleshooting

If `python -m src.main` reports a missing package, run `pip install -r requirements.txt` inside the virtual environment.

If Trading212 rejects an order, check execution mode, API environment, API permissions, ticker instrument mapping, and rate limits.

If no trades execute, inspect the dashboard model/risk reasons first. Conservative gates are expected to reject weak or uncalibrated signals.

If the dashboard is unavailable, check that port `5055` is free or change the dashboard port in config.
