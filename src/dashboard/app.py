"""Local Flask dashboard for Capitol Alpha."""

import logging

from flask import Flask, jsonify, render_template_string

from src.config import Config
from src.database import Database

logger = logging.getLogger("capitol_alpha.dashboard")

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Capitol Alpha</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Inter, Arial, sans-serif;
            background: #0b0d10;
            color: #e6e8eb;
        }
        header {
            padding: 18px 24px;
            border-bottom: 1px solid #27303a;
            background: #11161c;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
        }
        h1 { font-size: 20px; margin: 0; font-weight: 700; }
        main { padding: 18px 24px 32px; }
        button {
            background: #1f7a5a;
            color: white;
            border: 0;
            border-radius: 6px;
            padding: 8px 12px;
            cursor: pointer;
        }
        .tabs {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 16px;
        }
        .tab {
            background: #151b22;
            color: #cbd2d9;
            border: 1px solid #27303a;
        }
        .tab.active { background: #1f7a5a; color: white; }
        .panel { display: none; }
        .panel.active { display: block; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }
        .stat-card {
            background: #151b22;
            border: 1px solid #27303a;
            border-radius: 8px;
            padding: 12px;
            min-height: 78px;
        }
        .label { color: #8b96a3; font-size: 12px; text-transform: uppercase; }
        .value { color: #f4f7f9; font-size: 22px; font-weight: 700; margin-top: 6px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td {
            border-bottom: 1px solid #27303a;
            padding: 9px 8px;
            text-align: left;
            font-size: 13px;
            vertical-align: top;
        }
        th { color: #8b96a3; background: #11161c; font-size: 12px; }
        .buy, .ok { color: #35d08f; }
        .sell, .bad { color: #ff6b6b; }
        .muted { color: #8b96a3; }
        .pill {
            display: inline-block;
            padding: 3px 7px;
            border-radius: 999px;
            background: #27303a;
            color: #d5dbe1;
            font-size: 12px;
        }
        .tradeable { background: #123d30; color: #71e2b1; }
        .blocked { background: #3d2626; color: #ffb3b3; }
    </style>
</head>
<body>
    <header>
        <h1>Capitol Alpha</h1>
        <button onclick="loadData()">Refresh</button>
    </header>
    <main>
        <div class="tabs">
            <button class="tab active" onclick="showTab('overview', this)">Overview</button>
            <button class="tab" onclick="showTab('signals', this)">Signals</button>
            <button class="tab" onclick="showTab('ledger', this)">Paper Ledger</button>
            <button class="tab" onclick="showTab('model', this)">Model</button>
            <button class="tab" onclick="showTab('readiness', this)">Live Readiness</button>
        </div>

        <section id="overview" class="panel active">
            <div class="stats-grid" id="stats"></div>
            <h2>Recent Trades</h2>
            <table><thead><tr><th>Date</th><th>Politician</th><th>Ticker</th><th>Type</th><th>Amount</th><th>Source</th></tr></thead><tbody id="trades-body"></tbody></table>
        </section>

        <section id="signals" class="panel">
            <table><thead><tr><th>Rank</th><th>Ticker</th><th>Direction</th><th>Politician</th><th>Score</th><th>Expected</th><th>Confidence</th><th>Action</th><th>Status</th><th>Reason</th></tr></thead><tbody id="signals-body"></tbody></table>
        </section>

        <section id="ledger" class="panel">
            <h2>Paper Orders</h2>
            <table><thead><tr><th>Created</th><th>Ticker</th><th>Broker</th><th>Mode</th><th>Status</th><th>Qty</th><th>Notional</th><th>Reason</th></tr></thead><tbody id="orders-body"></tbody></table>
            <h2>Paper Positions</h2>
            <table><thead><tr><th>Ticker</th><th>Qty</th><th>Avg Price</th><th>Notional</th><th>Sector</th><th>Updated</th></tr></thead><tbody id="positions-body"></tbody></table>
        </section>

        <section id="model" class="panel">
            <div class="stats-grid" id="model-stats"></div>
        </section>

        <section id="readiness" class="panel">
            <div class="stats-grid" id="readiness-stats"></div>
            <p class="muted" id="readiness-notes"></p>
        </section>
    </main>

    <script>
        function showTab(id, button) {
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            button.classList.add('active');
        }
        function statCards(data) {
            return Object.entries(data || {}).map(([key, val]) =>
                `<div class="stat-card"><div class="label">${key.replaceAll('_', ' ')}</div><div class="value">${val ?? '-'}</div></div>`
            ).join('');
        }
        function pct(value) {
            if (value === null || value === undefined) return '-';
            return `${(value * 100).toFixed(1)}%`;
        }
        async function loadData() {
            const [stats, signals, trades, orders, positions, model, readiness] = await Promise.all([
                fetch('/api/stats').then(r => r.json()),
                fetch('/api/signals').then(r => r.json()),
                fetch('/api/trades').then(r => r.json()),
                fetch('/api/paper-orders').then(r => r.json()),
                fetch('/api/positions').then(r => r.json()),
                fetch('/api/model').then(r => r.json()),
                fetch('/api/live-readiness').then(r => r.json())
            ]);
            document.getElementById('stats').innerHTML = statCards(stats);
            document.getElementById('model-stats').innerHTML = statCards(model);
            document.getElementById('readiness-stats').innerHTML = statCards(readiness);
            document.getElementById('readiness-notes').textContent = readiness.notes || 'No validation report yet.';

            document.getElementById('signals-body').innerHTML = signals.map(s => `
                <tr>
                    <td>${s.rank_order ?? '-'}</td>
                    <td><b>${s.ticker}</b></td>
                    <td class="${s.direction === 'BUY' ? 'buy' : 'sell'}">${s.direction}</td>
                    <td>${s.politician_name}</td>
                    <td>${pct(s.alpha_score)}</td>
                    <td>${pct(s.expected_excess_return)}</td>
                    <td>${pct(s.confidence)}</td>
                    <td><span class="pill ${s.tradeable ? 'tradeable' : 'blocked'}">${s.suggested_action}</span></td>
                    <td>${s.status}</td>
                    <td class="muted">${s.decision_reason || '-'}</td>
                </tr>`).join('') || '<tr><td colspan="10">No signals yet</td></tr>';

            document.getElementById('trades-body').innerHTML = trades.slice(0, 50).map(t => `
                <tr><td>${t.transaction_date || '-'}</td><td>${t.politician_name}</td><td><b>${t.ticker}</b></td><td>${t.transaction_type}</td><td>${t.amount_range || '-'}</td><td>${t.source}</td></tr>`
            ).join('') || '<tr><td colspan="6">No trades yet</td></tr>';

            document.getElementById('orders-body').innerHTML = orders.map(o => `
                <tr><td>${o.created_at || '-'}</td><td><b>${o.ticker}</b></td><td>${o.broker}</td><td>${o.execution_mode}</td><td>${o.status}</td><td>${o.quantity ?? '-'}</td><td>${o.intended_notional_gbp ?? '-'}</td><td class="muted">${o.rejection_reason || '-'}</td></tr>`
            ).join('') || '<tr><td colspan="8">No paper orders yet</td></tr>';

            document.getElementById('positions-body').innerHTML = positions.map(p => `
                <tr><td><b>${p.ticker}</b></td><td>${p.quantity}</td><td>${p.avg_price ?? '-'}</td><td>${p.notional_gbp}</td><td>${p.sector || '-'}</td><td>${p.updated_at}</td></tr>`
            ).join('') || '<tr><td colspan="6">No paper positions yet</td></tr>';
        }
        loadData();
        setInterval(loadData, 60000);
    </script>
</body>
</html>
"""


def create_dashboard_app(db: Database, config: Config) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/stats")
    def api_stats():
        return jsonify(db.get_dashboard_stats())

    @app.route("/api/signals")
    def api_signals():
        with db.connection() as conn:
            rows = conn.execute("""
                SELECT * FROM signals
                ORDER BY created_at DESC
                LIMIT 150
            """).fetchall()
            return jsonify([dict(r) for r in rows])

    @app.route("/api/trades")
    def api_trades():
        return jsonify(db.get_recent_trades(days=30))

    @app.route("/api/paper-orders")
    def api_paper_orders():
        return jsonify(db.get_recent_paper_orders(limit=150))

    @app.route("/api/positions")
    def api_positions():
        return jsonify(db.get_paper_positions())

    @app.route("/api/model")
    def api_model():
        return jsonify(db.get_model_health())

    @app.route("/api/live-readiness")
    def api_live_readiness():
        report = db.get_latest_validation_report()
        if not report:
            return jsonify({
                "live_ready": 0,
                "required_days": config.execution.validation_days_required,
                "notes": "No validation report has been generated.",
            })
        return jsonify(report)

    return app
