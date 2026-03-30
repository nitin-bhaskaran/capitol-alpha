"""
Local Flask dashboard for Capitol Alpha.
Lightweight web UI for monitoring trades, signals, and portfolio.
Access at http://your-pi-ip:5055
"""

import json
import logging
from flask import Flask, render_template_string, jsonify

from src.database import Database
from src.config import Config

logger = logging.getLogger("capitol_alpha.dashboard")

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Capitol Alpha</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'SF Mono', 'Fira Code', monospace;
            background: #0a0a0f;
            color: #e0e0e0;
            padding: 20px;
        }
        h1 {
            color: #00ff88;
            font-size: 1.5rem;
            margin-bottom: 20px;
            border-bottom: 1px solid #1a1a2e;
            padding-bottom: 10px;
        }
        h2 { color: #888; font-size: 1rem; margin: 20px 0 10px; text-transform: uppercase; letter-spacing: 2px; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: #12121f;
            border: 1px solid #1a1a2e;
            border-radius: 8px;
            padding: 15px;
        }
        .stat-card .label { color: #666; font-size: 0.75rem; text-transform: uppercase; }
        .stat-card .value { color: #00ff88; font-size: 1.8rem; font-weight: bold; margin-top: 5px; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 30px;
        }
        th {
            background: #12121f;
            color: #666;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #1a1a2e;
        }
        td {
            padding: 10px;
            border-bottom: 1px solid #0f0f1a;
            font-size: 0.85rem;
        }
        tr:hover { background: #12121f; }
        .buy { color: #00ff88; }
        .sell { color: #ff4444; }
        .score-bar {
            display: inline-block;
            width: 60px;
            height: 8px;
            background: #1a1a2e;
            border-radius: 4px;
            overflow: hidden;
        }
        .score-bar .fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.3s;
        }
        .vip-badge {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.7rem;
            font-weight: bold;
        }
        .vip-1 { background: #ff880033; color: #ff8800; }
        .vip-2 { background: #0088ff33; color: #0088ff; }
        .refresh-btn {
            background: #1a1a2e;
            color: #00ff88;
            border: 1px solid #00ff8844;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            float: right;
        }
        .refresh-btn:hover { background: #00ff8822; }
    </style>
</head>
<body>
    <h1>🏛️ Capitol Alpha <button class="refresh-btn" onclick="location.reload()">Refresh</button></h1>

    <div class="stats-grid" id="stats"></div>

    <h2>Recent Signals</h2>
    <table id="signals-table">
        <thead>
            <tr>
                <th>Ticker</th>
                <th>Direction</th>
                <th>Politician</th>
                <th>Score</th>
                <th>VIP</th>
                <th>Action</th>
                <th>Status</th>
            </tr>
        </thead>
        <tbody id="signals-body"></tbody>
    </table>

    <h2>Recent Trades Ingested</h2>
    <table id="trades-table">
        <thead>
            <tr>
                <th>Date</th>
                <th>Politician</th>
                <th>Ticker</th>
                <th>Type</th>
                <th>Amount</th>
                <th>Source</th>
            </tr>
        </thead>
        <tbody id="trades-body"></tbody>
    </table>

    <script>
        async function loadData() {
            // Load stats
            const statsResp = await fetch('/api/stats');
            const stats = await statsResp.json();
            const statsHtml = Object.entries(stats).map(([key, val]) =>
                `<div class="stat-card">
                    <div class="label">${key.replace(/_/g, ' ')}</div>
                    <div class="value">${val}</div>
                </div>`
            ).join('');
            document.getElementById('stats').innerHTML = statsHtml;

            // Load signals
            const sigResp = await fetch('/api/signals');
            const signals = await sigResp.json();
            const sigHtml = signals.map(s => {
                const dirClass = s.direction === 'BUY' ? 'buy' : 'sell';
                const scoreColor = s.alpha_score > 0.6 ? '#00ff88' : s.alpha_score > 0.4 ? '#ffaa00' : '#666';
                const vipBadge = s.vip_tier === 1 ? '<span class="vip-badge vip-1">T1</span>' :
                                 s.vip_tier === 2 ? '<span class="vip-badge vip-2">T2</span>' : '';
                return `<tr>
                    <td><b>${s.ticker}</b></td>
                    <td class="${dirClass}">${s.direction}</td>
                    <td>${s.politician_name}</td>
                    <td>
                        <span class="score-bar"><span class="fill" style="width:${s.alpha_score*100}%;background:${scoreColor}"></span></span>
                        ${(s.alpha_score*100).toFixed(0)}%
                    </td>
                    <td>${vipBadge}</td>
                    <td>${s.suggested_action}</td>
                    <td>${s.status}</td>
                </tr>`;
            }).join('');
            document.getElementById('signals-body').innerHTML = sigHtml || '<tr><td colspan="7">No signals yet</td></tr>';

            // Load trades
            const trResp = await fetch('/api/trades');
            const trades = await trResp.json();
            const trHtml = trades.slice(0, 50).map(t => `<tr>
                <td>${t.transaction_date || '—'}</td>
                <td>${t.politician_name}</td>
                <td><b>${t.ticker}</b></td>
                <td>${t.transaction_type}</td>
                <td>${t.amount_range || '—'}</td>
                <td>${t.source}</td>
            </tr>`).join('');
            document.getElementById('trades-body').innerHTML = trHtml || '<tr><td colspan="6">No trades yet</td></tr>';
        }

        loadData();
        setInterval(loadData, 60000);  // Auto-refresh every minute
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
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            return jsonify([dict(r) for r in rows])

    @app.route("/api/trades")
    def api_trades():
        return jsonify(db.get_recent_trades(days=30))

    return app
