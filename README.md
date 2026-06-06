# Capitol Alpha

Local-first quant paper-trading system for US politician-disclosure event alpha.

The MVP path is Trading212 demo execution plus an independent local SQLite ledger. The live path is deliberately locked until the paper/demo validation gates pass.

## What It Does

Capitol Alpha ingests politician stock disclosures, generates event-alpha features, ranks disclosures with an ensemble model, applies long-only risk gates, and records every paper/demo order and outcome for later model improvement.

Execution does not depend on LLM analysis. Models produce signals, the risk engine gates them, Trading212 demo or local paper execution places the order, and the local ledger remains authoritative.

```text
Disclosure feeds -> Feature snapshots -> Event-alpha ensemble -> Risk gates
       |                                                        |
       v                                                        v
   SQLite raw trades                                  Paper/demo execution
       |                                                        |
       v                                                        v
 Dashboard + reports <- Outcomes + validation gates <- Local ledger
```

## Execution Modes

Configured in `config/config.yaml`:

- `disabled`: score and alert only.
- `local_paper`: simulated fills in the local ledger only.
- `t212_demo`: Trading212 demo orders plus local ledger. This is the default.
- `t212_live`: live Trading212 orders, blocked unless validation gates pass and `execution.live_enabled: true`.

Live execution requires 10 trading days of demo/paper validation, positive net paper P&L, no critical execution incidents, local/broker ledger consistency, calibration evidence, and manual review.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config/config_template.yaml config/config.yaml
# Add Telegram and Trading212 demo keys if you want alerts/demo orders.

python -m src.main
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config\config_template.yaml config\config.yaml
python -m src.main
```

The dashboard runs at `http://localhost:5055` by default.

## Research Commands

Backfill outcomes from a local price CSV:

```bash
python -m src.research backfill-outcomes --prices data/prices.csv
```

Train a model artifact from recorded outcomes:

```bash
python -m src.research train --output models/event_alpha_model.json
```

Generate the daily paper-trading validation report:

```bash
python -m src.research report
```

The expected price CSV columns are `ticker,date,close` with optional `benchmark_close`.

## Model Stack

The scoring layer is an event-alpha ensemble:

- Event-study labels for 1d, 5d, and 20d forward excess returns.
- Bayesian credibility by source, politician, sector, committee, chamber, party, and filing delay reliability.
- Cross-sectional ranking of current disclosures.
- Meta-labelling to separate interesting disclosures from tradeable events.
- Calibration metadata before live readiness can be granted.

The default artifact in `models/event_alpha_model.json` is intentionally conservative and marks calibration as incomplete until real outcomes are collected.

## Long-Only Trading Constraints

The MVP assumes a Stock ISA-style long-only equity workflow:

- BUY disclosures can become trade candidates.
- SELL/disposal disclosures become `REDUCE_IF_HELD`, `AVOID`, or `NO_TRADE`.
- No short-selling assumption is made.
- Position sizing is capped by hard risk limits and conservative fractional Kelly settings.

## Key Files

```text
config/config_template.yaml     Configuration template
models/event_alpha_model.json   Default model artifact
src/main.py                     Long-running app
src/config.py                   Config loader and defaults
src/database.py                 SQLite schema, migrations, ledger queries
src/scoring/features.py         Disclosure feature generation
src/scoring/model.py            Event-alpha ensemble model
src/scoring/engine.py           Ranking and signal persistence
src/execution/paper.py          Risk gates and paper/demo execution
src/execution/trading212.py     Trading212 API client
src/alerts/telegram_bot.py      Telegram approvals and execute handler
src/dashboard/app.py            Operational dashboard
src/research/cli.py             Backfill, train, and report commands
tests/test_quant_mvp.py         MVP validation tests
```

## Safety Defaults

The shipped defaults are designed to start validation quickly without live-market risk:

- `execution.mode: "t212_demo"`
- `execution.live_enabled: false`
- 10 trading days required before live readiness
- Local ledger required even when broker demo execution is used
- Long-only risk gates
- Conservative model thresholds

API keys belong only in local config files and should not be committed.
