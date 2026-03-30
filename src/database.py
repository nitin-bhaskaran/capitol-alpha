"""
SQLite database for Capitol Alpha.
Zero-config, single-file, perfect for Raspberry Pi.
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

SCHEMA_VERSION = 1

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
    UNIQUE(raw_trade_id)
);

CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);

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
"""


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        from src.config import DB_FILE
        self.db_path = db_path or DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_info (version) VALUES (?)",
                (SCHEMA_VERSION,),
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

    # ── Raw Trades ────────────────────────────────────────────────

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
        """Get raw trades that don't have a signal yet."""
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
        """Get recent trades for the dashboard."""
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT rt.*, s.alpha_score, s.status as signal_status, s.direction
                FROM raw_trades rt
                LEFT JOIN signals s ON s.raw_trade_id = rt.id
                WHERE rt.transaction_date >= date('now', ?)
                ORDER BY rt.transaction_date DESC
            """, (f"-{days} days",)).fetchall()
            return [dict(r) for r in rows]

    # ── Signals ───────────────────────────────────────────────────

    def insert_signal(self, signal: Dict[str, Any]) -> int:
        with self.connection() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO signals (
                    raw_trade_id, ticker, politician_name, direction,
                    alpha_score, score_breakdown, vip_tier, filing_gap_days,
                    suggested_action
                ) VALUES (
                    :raw_trade_id, :ticker, :politician_name, :direction,
                    :alpha_score, :score_breakdown, :vip_tier, :filing_gap_days,
                    :suggested_action
                )
            """, signal)
            return cur.lastrowid

    def get_pending_signals(self) -> List[Dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE status = 'pending' ORDER BY alpha_score DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_signal_status(self, signal_id: int, status: str):
        with self.connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            ts_field = {
                "alerted": "alerted_at",
                "approved": "decided_at",
                "rejected": "decided_at",
            }.get(status)
            if ts_field:
                conn.execute(
                    f"UPDATE signals SET status = ?, {ts_field} = ? WHERE id = ?",
                    (status, now, signal_id),
                )
            else:
                conn.execute(
                    "UPDATE signals SET status = ? WHERE id = ?",
                    (status, signal_id),
                )

    def get_signal_by_id(self, signal_id: int) -> Optional[Dict]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE id = ?", (signal_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── Executions ────────────────────────────────────────────────

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
            conn.execute(
                f"UPDATE executions SET {set_clause} WHERE id = ?", values
            )

    # ── Stats ─────────────────────────────────────────────────────

    def get_dashboard_stats(self) -> Dict:
        with self.connection() as conn:
            total_trades = conn.execute("SELECT COUNT(*) FROM raw_trades").fetchone()[0]
            total_signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM signals WHERE status='pending'").fetchone()[0]
            approved = conn.execute("SELECT COUNT(*) FROM signals WHERE status='approved'").fetchone()[0]
            executed = conn.execute("SELECT COUNT(*) FROM executions WHERE status='filled'").fetchone()[0]
            return {
                "total_trades_ingested": total_trades,
                "total_signals": total_signals,
                "pending_signals": pending,
                "approved_signals": approved,
                "executed_trades": executed,
            }
