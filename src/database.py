"""SQLite database and ledger primitives for Capitol Alpha."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS raw_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    politician_name TEXT NOT NULL,
    politician_party TEXT,
    politician_state TEXT,
    politician_chamber TEXT,
    ticker TEXT,
    asset_description TEXT,
    transaction_type TEXT NOT NULL,
    transaction_date TEXT,
    filing_date TEXT,
    amount_range TEXT,
    amount_low REAL,
    amount_high REAL,
    owner TEXT,
    comment TEXT,
    source_url TEXT,
    raw_json TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, politician_name, ticker, transaction_date, transaction_type)
);

CREATE INDEX IF NOT EXISTS idx_raw_ticker ON raw_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_raw_politician ON raw_trades(politician_name);
CREATE INDEX IF NOT EXISTS idx_raw_date ON raw_trades(transaction_date);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_trade_id INTEGER NOT NULL REFERENCES raw_trades(id),
    ticker TEXT NOT NULL,
    politician_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    alpha_score REAL NOT NULL,
    score_breakdown TEXT,
    vip_tier INTEGER,
    filing_gap_days INTEGER,
    suggested_action TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    alerted_at TEXT,
    decided_at TEXT,
    model_version TEXT,
    expected_excess_return REAL,
    confidence REAL,
    tradeable INTEGER DEFAULT 0,
    rank_order INTEGER,
    execution_mode TEXT,
    decision_reason TEXT,
    feature_hash TEXT,
    UNIQUE(raw_trade_id)
);

CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_rank ON signals(rank_order);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    t212_order_id TEXT,
    ticker TEXT NOT NULL,
    t212_ticker TEXT,
    direction TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity REAL,
    limit_price REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    fill_price REAL,
    fill_quantity REAL,
    error_message TEXT,
    submitted_at TEXT,
    filled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS politicians (
    name TEXT PRIMARY KEY,
    party TEXT,
    state TEXT,
    chamber TEXT,
    committees TEXT,
    historical_win_rate REAL,
    historical_avg_return REAL,
    total_trades_tracked INTEGER DEFAULT 0,
    is_vip_tier1 INTEGER DEFAULT 0,
    is_vip_tier2 INTEGER DEFAULT 0,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    signals_generated INTEGER DEFAULT 0,
    alerts_sent INTEGER DEFAULT 0,
    trades_approved INTEGER DEFAULT 0,
    trades_rejected INTEGER DEFAULT 0,
    trades_executed INTEGER DEFAULT 0,
    total_invested REAL DEFAULT 0.0,
    realized_pnl REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version TEXT NOT NULL,
    train_start TEXT,
    train_end TEXT,
    validation_metrics_json TEXT,
    calibration_metrics_json TEXT,
    metadata_json TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_model_runs_active ON model_runs(active, created_at);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    raw_trade_id INTEGER NOT NULL REFERENCES raw_trades(id),
    feature_hash TEXT NOT NULL,
    model_version TEXT NOT NULL,
    features_json TEXT NOT NULL,
    market_context_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(raw_trade_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_feature_snapshots_hash ON feature_snapshots(feature_hash);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    execution_mode TEXT NOT NULL,
    broker TEXT NOT NULL,
    broker_order_id TEXT,
    ticker TEXT NOT NULL,
    t212_ticker TEXT,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity REAL,
    intended_notional_gbp REAL,
    estimated_price REAL,
    limit_price REAL,
    fill_price REAL,
    fill_quantity REAL,
    slippage_bps REAL DEFAULT 0.0,
    fees_gbp REAL DEFAULT 0.0,
    pnl_gbp REAL DEFAULT 0.0,
    rejection_reason TEXT,
    request_json TEXT,
    response_json TEXT,
    submitted_at TEXT,
    filled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_signal ON paper_orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status);
CREATE INDEX IF NOT EXISTS idx_paper_orders_created ON paper_orders(created_at);

CREATE TABLE IF NOT EXISTS paper_positions (
    ticker TEXT PRIMARY KEY,
    t212_ticker TEXT,
    quantity REAL NOT NULL DEFAULT 0,
    avg_price REAL,
    notional_gbp REAL NOT NULL DEFAULT 0,
    realized_pnl_gbp REAL NOT NULL DEFAULT 0,
    unrealized_pnl_gbp REAL NOT NULL DEFAULT 0,
    sector TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    horizon_days INTEGER NOT NULL,
    forward_return REAL,
    benchmark_return REAL,
    excess_return REAL,
    drawdown REAL,
    attribution_tags TEXT,
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(signal_id, horizon_days)
);

CREATE TABLE IF NOT EXISTS validation_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL UNIQUE,
    trading_days INTEGER NOT NULL DEFAULT 0,
    trade_decisions INTEGER NOT NULL DEFAULT 0,
    net_pnl_gbp REAL NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0,
    hit_rate REAL,
    duplicate_incidents INTEGER NOT NULL DEFAULT 0,
    ledger_mismatch_count INTEGER NOT NULL DEFAULT 0,
    calibration_ok INTEGER NOT NULL DEFAULT 0,
    live_ready INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    metrics_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


SIGNAL_EXTRA_COLUMNS = {
    "model_version": "TEXT",
    "expected_excess_return": "REAL",
    "confidence": "REAL",
    "tradeable": "INTEGER DEFAULT 0",
    "rank_order": "INTEGER",
    "execution_mode": "TEXT",
    "decision_reason": "TEXT",
    "feature_hash": "TEXT",
}


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        from src.config import DB_FILE

        self.db_path = db_path or DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            self._ensure_columns(conn, "signals", SIGNAL_EXTRA_COLUMNS)
            conn.execute(
                "INSERT OR REPLACE INTO schema_info (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, self._now()),
            )

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, columns: Dict[str, str]):
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _json(self, value: Any) -> str:
        return json.dumps(value, sort_keys=True, default=str)

    # Raw trades

    def insert_raw_trade(self, trade: Dict[str, Any]) -> Optional[int]:
        """Insert a raw trade. Returns row ID or None if duplicate."""
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO raw_trades (
                    source, politician_name, politician_party, politician_state,
                    politician_chamber, ticker, asset_description, transaction_type,
                    transaction_date, filing_date, amount_range, amount_low, amount_high,
                    owner, comment, source_url, raw_json
                ) VALUES (
                    :source, :politician_name, :politician_party, :politician_state,
                    :politician_chamber, :ticker, :asset_description, :transaction_type,
                    :transaction_date, :filing_date, :amount_range, :amount_low, :amount_high,
                    :owner, :comment, :source_url, :raw_json
                )
            """, trade)
            return cur.lastrowid if cur.rowcount > 0 else None

    def get_unscored_trades(self) -> List[Dict]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT rt.* FROM raw_trades rt
                LEFT JOIN signals s ON s.raw_trade_id = rt.id
                WHERE s.id IS NULL
                  AND rt.ticker IS NOT NULL
                  AND rt.ticker != '--'
                  AND rt.ticker != ''
                ORDER BY rt.transaction_date DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_recent_trades(self, days: int = 30) -> List[Dict]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT rt.*, s.alpha_score, s.status as signal_status, s.direction
                FROM raw_trades rt
                LEFT JOIN signals s ON s.raw_trade_id = rt.id
                WHERE rt.transaction_date >= date('now', ?)
                ORDER BY rt.transaction_date DESC
            """, (f"-{days} days",)).fetchall()
            return [dict(r) for r in rows]

    # Signals and model artifacts

    def insert_signal(self, signal: Dict[str, Any]) -> int:
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO signals (
                    raw_trade_id, ticker, politician_name, direction,
                    alpha_score, score_breakdown, vip_tier, filing_gap_days,
                    suggested_action, model_version, expected_excess_return,
                    confidence, tradeable, rank_order, execution_mode,
                    decision_reason, feature_hash
                ) VALUES (
                    :raw_trade_id, :ticker, :politician_name, :direction,
                    :alpha_score, :score_breakdown, :vip_tier, :filing_gap_days,
                    :suggested_action, :model_version, :expected_excess_return,
                    :confidence, :tradeable, :rank_order, :execution_mode,
                    :decision_reason, :feature_hash
                )
            """, signal)
            return cur.lastrowid

    def update_signal_status(self, signal_id: int, status: str):
        with self.connection() as conn:
            now = self._now()
            ts_field = {
                "alerted": "alerted_at",
                "approved": "decided_at",
                "rejected": "decided_at",
                "executed": "decided_at",
            }.get(status)
            if ts_field:
                conn.execute(
                    f"UPDATE signals SET status = ?, {ts_field} = ? WHERE id = ?",
                    (status, now, signal_id),
                )
            else:
                conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))

    def get_pending_signals(self) -> List[Dict]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT * FROM signals
                WHERE status = 'pending'
                ORDER BY tradeable DESC, rank_order ASC, alpha_score DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_signal_by_id(self, signal_id: int) -> Optional[Dict]:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def get_signal_event(self, signal_id: int) -> Optional[Dict]:
        with self.connection() as conn:
            row = conn.execute("""
                SELECT s.*, rt.source, rt.asset_description, rt.transaction_type,
                       rt.transaction_date, rt.filing_date, rt.amount_range,
                       rt.amount_low, rt.amount_high, rt.politician_party,
                       rt.politician_state, rt.politician_chamber
                FROM signals s
                JOIN raw_trades rt ON rt.id = s.raw_trade_id
                WHERE s.id = ?
            """, (signal_id,)).fetchone()
            return dict(row) if row else None

    def insert_feature_snapshot(self, snapshot: Dict[str, Any]) -> int:
        payload = {
            **snapshot,
            "features_json": self._json(snapshot.get("features", {})),
            "market_context_json": self._json(snapshot.get("market_context", {})),
        }
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT OR REPLACE INTO feature_snapshots (
                    signal_id, raw_trade_id, feature_hash, model_version,
                    features_json, market_context_json
                ) VALUES (
                    :signal_id, :raw_trade_id, :feature_hash, :model_version,
                    :features_json, :market_context_json
                )
            """, payload)
            return cur.lastrowid

    def insert_model_run(self, run: Dict[str, Any]) -> int:
        payload = {
            **run,
            "validation_metrics_json": self._json(run.get("validation_metrics", {})),
            "calibration_metrics_json": self._json(run.get("calibration_metrics", {})),
            "metadata_json": self._json(run.get("metadata", {})),
        }
        with self.connection() as conn:
            if payload.get("active"):
                conn.execute("UPDATE model_runs SET active = 0")
            cur = conn.execute("""
                INSERT INTO model_runs (
                    model_version, train_start, train_end, validation_metrics_json,
                    calibration_metrics_json, metadata_json, active
                ) VALUES (
                    :model_version, :train_start, :train_end, :validation_metrics_json,
                    :calibration_metrics_json, :metadata_json, :active
                )
            """, payload)
            return cur.lastrowid

    def get_active_model_run(self) -> Optional[Dict]:
        with self.connection() as conn:
            row = conn.execute("""
                SELECT * FROM model_runs
                WHERE active = 1
                ORDER BY created_at DESC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    # Legacy executions

    def insert_execution(self, execution: Dict[str, Any]) -> int:
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT INTO executions (
                    signal_id, ticker, t212_ticker, direction,
                    order_type, quantity, limit_price, status
                ) VALUES (
                    :signal_id, :ticker, :t212_ticker, :direction,
                    :order_type, :quantity, :limit_price, 'pending'
                )
            """, execution)
            return cur.lastrowid

    def update_execution(self, exec_id: int, updates: Dict[str, Any]):
        with self.connection() as conn:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [exec_id]
            conn.execute(f"UPDATE executions SET {set_clause} WHERE id = ?", values)

    # Paper ledger

    def insert_paper_order(self, order: Dict[str, Any]) -> int:
        payload = {
            **order,
            "request_json": self._json(order.get("request", {})),
            "response_json": self._json(order.get("response", {})),
        }
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT INTO paper_orders (
                    signal_id, execution_mode, broker, broker_order_id, ticker,
                    t212_ticker, side, order_type, status, quantity,
                    intended_notional_gbp, estimated_price, limit_price, fill_price,
                    fill_quantity, slippage_bps, fees_gbp, pnl_gbp, rejection_reason,
                    request_json, response_json, submitted_at, filled_at
                ) VALUES (
                    :signal_id, :execution_mode, :broker, :broker_order_id, :ticker,
                    :t212_ticker, :side, :order_type, :status, :quantity,
                    :intended_notional_gbp, :estimated_price, :limit_price, :fill_price,
                    :fill_quantity, :slippage_bps, :fees_gbp, :pnl_gbp, :rejection_reason,
                    :request_json, :response_json, :submitted_at, :filled_at
                )
            """, payload)
            return cur.lastrowid

    def update_paper_order(self, order_id: int, updates: Dict[str, Any]):
        with self.connection() as conn:
            clean_updates = {}
            for key, value in updates.items():
                if key in {"request", "response"}:
                    clean_updates[f"{key}_json"] = self._json(value)
                else:
                    clean_updates[key] = value
            set_clause = ", ".join(f"{k} = ?" for k in clean_updates)
            conn.execute(
                f"UPDATE paper_orders SET {set_clause} WHERE id = ?",
                list(clean_updates.values()) + [order_id],
            )

    def upsert_paper_position(self, position: Dict[str, Any]):
        with self.connection() as conn:
            payload = {**position, "updated_at": self._now()}
            conn.execute("""
                INSERT INTO paper_positions (
                    ticker, t212_ticker, quantity, avg_price, notional_gbp,
                    realized_pnl_gbp, unrealized_pnl_gbp, sector, updated_at
                ) VALUES (
                    :ticker, :t212_ticker, :quantity, :avg_price, :notional_gbp,
                    :realized_pnl_gbp, :unrealized_pnl_gbp, :sector, :updated_at
                )
                ON CONFLICT(ticker) DO UPDATE SET
                    t212_ticker = excluded.t212_ticker,
                    quantity = excluded.quantity,
                    avg_price = excluded.avg_price,
                    notional_gbp = excluded.notional_gbp,
                    realized_pnl_gbp = excluded.realized_pnl_gbp,
                    unrealized_pnl_gbp = excluded.unrealized_pnl_gbp,
                    sector = excluded.sector,
                    updated_at = excluded.updated_at
            """, payload)

    def get_paper_positions(self) -> List[Dict]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT * FROM paper_positions
                WHERE quantity != 0
                ORDER BY notional_gbp DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_recent_paper_orders(self, limit: int = 100) -> List[Dict]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT po.*, s.confidence, s.expected_excess_return, s.decision_reason
                FROM paper_orders po
                LEFT JOIN signals s ON s.id = po.signal_id
                ORDER BY po.created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def insert_outcome(self, outcome: Dict[str, Any]) -> int:
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT OR REPLACE INTO outcomes (
                    signal_id, horizon_days, forward_return, benchmark_return,
                    excess_return, drawdown, attribution_tags, computed_at
                ) VALUES (
                    :signal_id, :horizon_days, :forward_return, :benchmark_return,
                    :excess_return, :drawdown, :attribution_tags, :computed_at
                )
            """, {**outcome, "computed_at": outcome.get("computed_at", self._now())})
            return cur.lastrowid

    def insert_validation_report(self, report: Dict[str, Any]) -> int:
        payload = {**report, "metrics_json": self._json(report.get("metrics", {}))}
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT OR REPLACE INTO validation_reports (
                    report_date, trading_days, trade_decisions, net_pnl_gbp,
                    max_drawdown_pct, hit_rate, duplicate_incidents,
                    ledger_mismatch_count, calibration_ok, live_ready, notes,
                    metrics_json
                ) VALUES (
                    :report_date, :trading_days, :trade_decisions, :net_pnl_gbp,
                    :max_drawdown_pct, :hit_rate, :duplicate_incidents,
                    :ledger_mismatch_count, :calibration_ok, :live_ready, :notes,
                    :metrics_json
                )
            """, payload)
            return cur.lastrowid

    def get_latest_validation_report(self) -> Optional[Dict]:
        with self.connection() as conn:
            row = conn.execute("""
                SELECT * FROM validation_reports
                ORDER BY report_date DESC, created_at DESC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    # Dashboard/reporting

    def get_dashboard_stats(self) -> Dict:
        with self.connection() as conn:
            total_trades = conn.execute("SELECT COUNT(*) FROM raw_trades").fetchone()[0]
            total_signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status='pending'"
            ).fetchone()[0]
            approved = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status='approved'"
            ).fetchone()[0]
            paper_orders = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
            filled = conn.execute("""
                SELECT COUNT(*) FROM paper_orders
                WHERE status IN ('filled', 'submitted', 'confirmed')
            """).fetchone()[0]
            net_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl_gbp), 0) FROM paper_orders"
            ).fetchone()[0]
            return {
                "total_trades_ingested": total_trades,
                "total_signals": total_signals,
                "pending_signals": pending,
                "approved_signals": approved,
                "paper_orders": paper_orders,
                "paper_orders_active": filled,
                "paper_net_pnl_gbp": round(net_pnl or 0, 2),
            }

    def get_model_health(self) -> Dict[str, Any]:
        with self.connection() as conn:
            active = self.get_active_model_run()
            snapshots = conn.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()[0]
            outcomes = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
            avg_conf = conn.execute(
                "SELECT AVG(confidence) FROM signals WHERE confidence IS NOT NULL"
            ).fetchone()[0]
            return {
                "active_model": active.get("model_version") if active else None,
                "feature_snapshots": snapshots,
                "outcomes": outcomes,
                "avg_confidence": round(avg_conf or 0, 3),
            }
