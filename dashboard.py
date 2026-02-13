#!/usr/bin/env python3
"""Live web dashboard for PolySniper Straddle bot.

Endpoints:
- GET /           : HTML dashboard with live updates
- GET /api/status : live status per asset (JSON)
- GET /api/stats  : stats per asset + total (JSON)
- GET /api/trades : recent trades (JSON)
"""

from __future__ import annotations

import html
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from bot_registry import REGISTRY


app = FastAPI(title="ERNA Dashboard", docs_url=None, redoc_url=None)


@app.get("/api/status")
def api_status():
    return JSONResponse(REGISTRY.get_status())


@app.get("/api/stats")
def api_stats():
    return JSONResponse(REGISTRY.get_stats())


@app.get("/api/trades")
def api_trades(asset: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=2000)):
    asset_u = asset.upper() if asset else None
    return JSONResponse(REGISTRY.get_trades(asset=asset_u, limit=limit))


@app.get("/", response_class=HTMLResponse)
def index():
    html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{{DASHBOARD_TITLE}} - Live Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: #0f0f1a; color: #e0e0e0; padding: 20px;
        }
        h1 { color: #00d4ff; margin-bottom: 5px; font-size: 28px; }
        .subtitle { color: #888; font-size: 14px; margin-bottom: 20px; }
        h2 {
            color: #00d4ff; font-size: 18px; margin: 25px 0 15px 0;
            border-bottom: 1px solid #333; padding-bottom: 8px;
        }

        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); gap: 20px; }

        .card {
            background: #1a1a2e; border-radius: 12px;
            padding: 20px; border: 1px solid #333;
        }
        .card-header {
            display: flex; justify-content: space-between;
            align-items: center; margin-bottom: 15px;
        }
        .asset-name { font-size: 24px; font-weight: bold; color: #fff; }

        .status-badge {
            padding: 6px 14px; border-radius: 20px;
            font-size: 12px; font-weight: bold; text-transform: uppercase;
        }
        .status-WAITING { background: #1e3a5f; color: #5dade2; }
        .status-READY { background: #1e5f3a; color: #58d68d; }
        .status-HOLDING { background: #1e5f3a; color: #58d68d; }
        .status-PENDING_FILL { background: #5f4a1e; color: #f39c12; }
        .status-UNFILLED { background: #3d3d3d; color: #888; }
        .status-IDLE { background: #3d3d3d; color: #888; }
        .status-DISABLED { background: #5f1e1e; color: #e74c3c; }
        .status-ERROR { background: #5f1e1e; color: #e74c3c; }
        .status-BLACKLISTED { background: #4a1e5f; color: #9b59b6; }
        .status-CF_BLOCKED { background: #5f1e1e; color: #e74c3c; }
        .status-NO_BALANCE { background: #5f4a1e; color: #f39c12; }
        .status-ORDER_FAILED { background: #5f1e1e; color: #e74c3c; }

        .market-info {
            background: #252538; border-radius: 8px;
            padding: 12px; margin-bottom: 15px; font-size: 13px;
        }
        .market-info .label { color: #888; }
        .market-info .value { color: #fff; font-family: monospace; }
        .market-info .time-left { font-size: 20px; font-weight: bold; color: #f39c12; }
        .market-info .time-left.urgent { color: #e74c3c; }

        .prices-grid {
            display: grid; grid-template-columns: 1fr 1fr;
            gap: 15px; margin-bottom: 15px;
        }
        .price-box {
            background: #252538; border-radius: 8px;
            padding: 15px; text-align: center;
        }
        .price-box.yes { border-left: 4px solid #27ae60; }
        .price-box.no { border-left: 4px solid #e74c3c; }
        .price-box .token-label { font-size: 14px; color: #888; margin-bottom: 8px; }
        .price-box .prices { display: flex; justify-content: space-around; }
        .price-box .price-item { text-align: center; }
        .price-box .price-label { font-size: 11px; color: #666; text-transform: uppercase; }
        .price-box .price-value { font-size: 18px; font-weight: bold; font-family: monospace; }
        .price-box .price-value.ask { color: #e74c3c; }
        .price-box .price-value.bid { color: #27ae60; }

        .positions-grid {
            display: grid; grid-template-columns: 1fr 1fr;
            gap: 15px; margin-bottom: 10px;
        }

        .order-status {
            background: #252538; border-radius: 8px; padding: 12px;
        }
        .order-status.yes-pos { border-left: 3px solid #27ae60; }
        .order-status.no-pos { border-left: 3px solid #e74c3c; }
        .order-status .order-header {
            display: flex; justify-content: space-between;
            align-items: center; margin-bottom: 8px;
        }
        .order-status .order-title { font-weight: bold; font-size: 14px; }
        .order-status .order-title.yes-title { color: #27ae60; }
        .order-status .order-title.no-title { color: #e74c3c; }

        .order-badges { display: flex; gap: 6px; flex-wrap: wrap; }
        .order-badge {
            padding: 3px 8px; border-radius: 4px;
            font-size: 11px; font-weight: bold;
        }
        .order-badge.filled { background: #27ae60; color: #fff; }
        .order-badge.sold { background: #3498db; color: #fff; }
        .order-badge.pending { background: #f39c12; color: #000; }
        .order-badge.waiting { background: #555; color: #999; }
        .order-badge.winning { background: #27ae60; color: #fff; animation: pulse-green 1s infinite; }
        .order-badge.losing { background: #e74c3c; color: #fff; animation: pulse-red 1s infinite; }
        .order-badge.neutral { background: #f39c12; color: #000; }
        .order-badge.mg-step { background: #3d3d5c; color: #aaa; }

        @keyframes pulse-green { 0%, 100% { background: #27ae60; } 50% { background: #2ecc71; } }
        @keyframes pulse-red { 0%, 100% { background: #e74c3c; } 50% { background: #c0392b; } }

        .order-details { font-size: 11px; color: #888; font-family: monospace; line-height: 1.6; }
        .order-details .pnl-positive { color: #27ae60; font-weight: bold; }
        .order-details .pnl-negative { color: #e74c3c; font-weight: bold; }

        .stats-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px;
        }
        .stat-box {
            background: #1a1a2e; border-radius: 8px; padding: 15px; text-align: center;
        }
        .stat-box .stat-value { font-size: 28px; font-weight: bold; color: #00d4ff; }
        .stat-box .stat-label { font-size: 12px; color: #888; margin-top: 5px; }
        .stat-box.positive .stat-value { color: #27ae60; }
        .stat-box.negative .stat-value { color: #e74c3c; }

        .last-update { text-align: right; font-size: 11px; color: #555; margin-top: 20px; }

        .history-section { margin-top: 30px; }
        .history-controls {
            display: flex; gap: 10px; align-items: center;
            margin-bottom: 15px; flex-wrap: wrap;
        }
        .history-controls select {
            background: #252538; color: #e0e0e0;
            border: 1px solid #444; padding: 8px 12px;
            border-radius: 6px; font-size: 13px;
        }
        .trade-count { color: #888; font-size: 13px; margin-left: auto; }

        .history-table-wrapper {
            background: #1a1a2e; border-radius: 12px;
            overflow: hidden; max-height: 500px; overflow-y: auto;
        }
        .history-table { width: 100%; border-collapse: collapse; font-size: 12px; }
        .history-table th {
            background: #252538; color: #00d4ff;
            padding: 12px 8px; text-align: left;
            position: sticky; top: 0; font-weight: 600;
        }
        .history-table td { padding: 10px 8px; border-bottom: 1px solid #2a2a3e; color: #ccc; }
        .history-table tr:hover { background: #252538; }
        .history-table .result-WIN { color: #27ae60; font-weight: bold; }
        .history-table .result-LOSS { color: #e74c3c; font-weight: bold; }
        .history-table .pnl-positive { color: #27ae60; }
        .history-table .pnl-negative { color: #e74c3c; }
        .history-table .side-YES { color: #27ae60; font-weight: bold; }
        .history-table .side-NO { color: #e74c3c; font-weight: bold; }
        .history-table .market-slug { font-family: monospace; font-size: 10px; color: #666; }

        .no-data { color: #666; font-style: italic; padding: 20px; text-align: center; }

        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .live-indicator {
            display: inline-block; width: 8px; height: 8px;
            background: #27ae60; border-radius: 50%;
            margin-right: 8px; animation: pulse 1s infinite;
        }
    </style>
</head>
<body>
    <h1>🎯 {{DASHBOARD_TITLE}}</h1>
    <div class="subtitle"><span class="live-indicator"></span>Live Dashboard — Straddle: BUY YES + NO</div>

    <div id="assets-container" class="grid">
        <div class="no-data">Loading...</div>
    </div>

    <div style="margin-top: 30px;">
        <h2>📊 Statistics</h2>
        <div id="stats-container" class="stats-grid">
            <div class="no-data">Loading...</div>
        </div>
    </div>

    <div class="history-section">
        <h2>📜 Trade History</h2>
        <div class="history-controls">
            <select id="filter-asset" onchange="renderHistory()">
                <option value="">All Assets</option>
                <option value="BTC">BTC</option>
                <option value="ETH">ETH</option>
                <option value="SOL">SOL</option>
                <option value="XRP">XRP</option>
            </select>
            <select id="filter-side" onchange="renderHistory()">
                <option value="">All Sides</option>
                <option value="YES">YES</option>
                <option value="NO">NO</option>
            </select>
            <select id="filter-result" onchange="renderHistory()">
                <option value="">All Results</option>
                <option value="WIN">WIN</option>
                <option value="LOSS">LOSS</option>
            </select>
            <span id="trade-count" class="trade-count">0 trades</span>
        </div>
        <div id="history-container" class="history-table-wrapper">
            <table class="history-table">
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Asset</th>
                        <th>Side</th>
                        <th>Size</th>
                        <th>Buy Price</th>
                        <th>Sell Price</th>
                        <th>PnL</th>
                        <th>Result</th>
                        <th>M.Step</th>
                        <th>Market</th>
                    </tr>
                </thead>
                <tbody id="history-tbody"></tbody>
            </table>
        </div>
    </div>

    <div class="last-update" id="last-update"></div>

    <script>
        function formatTime(seconds) {
            if (seconds <= 0) return '0:00';
            const m = Math.floor(seconds / 60);
            const s = seconds % 60;
            return `${m}:${s.toString().padStart(2, '0')}`;
        }
        function formatPrice(p) { return p > 0 ? '$' + p.toFixed(4) : '-'; }

        function renderSidePosition(data, side) {
            const pfx = side.toLowerCase();
            const filled = data[pfx + '_filled'];
            const sold = data[pfx + '_sold'];
            const fillSize = data[pfx + '_filled_size'] || 0;
            const avgFill = data[pfx + '_avg_fill_price'] || 0;
            const buyPx = data[pfx + '_buy_price'] || 0;
            const sellTgt = data[pfx + '_sell_target'] || 0;
            const hitPx = data[pfx + '_target_hit_price'] || 0;
            const mgStep = data[pfx + '_martingale_step'] || 0;
            const orderSize = data[pfx + '_order_size'] || 0;
            const bid = data[pfx + '_bid'] || 0;
            const state = data.decision || 'IDLE';

            const sideClass = side === 'YES' ? 'yes-pos' : 'no-pos';
            const titleClass = side === 'YES' ? 'yes-title' : 'no-title';

            function getBadges() {
                let badges = `<span class="order-badge mg-step">M${mgStep}</span>`;
                if (sold) {
                    const px = hitPx > 0 ? ` @$${hitPx.toFixed(2)}` : '';
                    badges += `<span class="order-badge sold">TARGET${px}</span>`;
                } else if (filled) {
                    badges += '<span class="order-badge filled">FILLED</span>';
                    // PnL badge
                    if (bid > 0 && avgFill > 0) {
                        const diff = bid - avgFill;
                        const pct = ((diff / avgFill) * 100).toFixed(1);
                        if (diff > 0) badges += `<span class="order-badge winning">▲+${pct}%</span>`;
                        else if (diff < 0) badges += `<span class="order-badge losing">▼${pct}%</span>`;
                    }
                } else if (state === 'PENDING_FILL') {
                    badges += '<span class="order-badge pending">PENDING</span>';
                } else {
                    badges += '<span class="order-badge waiting">WAIT</span>';
                }
                return badges;
            }

            let detailsHtml = `Limit @$${buyPx}`;
            if (avgFill > 0) detailsHtml += ` | <strong style="color:#00d4ff">Fill @$${avgFill.toFixed(4)}</strong>`;
            detailsHtml += ` | Tgt @$${sellTgt} | Size: ${orderSize.toFixed ? orderSize.toFixed(1) : orderSize}`;

            if (filled && !sold && bid > 0 && avgFill > 0) {
                const livePnl = (fillSize * bid) - (fillSize * avgFill);
                const sign = livePnl >= 0 ? '+' : '';
                const cls = livePnl >= 0 ? 'pnl-positive' : 'pnl-negative';
                detailsHtml += `<br><span class="${cls}">Live PnL: ${sign}$${livePnl.toFixed(4)}</span>`;
            }

            return `
                <div class="order-status ${sideClass}">
                    <div class="order-header">
                        <span class="order-title ${titleClass}">${side}</span>
                        <div class="order-badges">${getBadges()}</div>
                    </div>
                    <div class="order-details">${detailsHtml}</div>
                </div>
            `;
        }

        function renderAssetCard(asset, data) {
            const state = data.decision || 'IDLE';
            const timeLeft = data.time_left || 0;
            const timeClass = timeLeft < 60 ? 'urgent' : '';

            return `
                <div class="card">
                    <div class="card-header">
                        <span class="asset-name">${asset}</span>
                        <span class="status-badge status-${state}">${state}</span>
                    </div>

                    <div class="market-info">
                        <div><span class="label">Market:</span> <span class="value">${data.market_slug || '-'}</span></div>
                        <div><span class="label">Question:</span> <span class="value">${data.market_question || '-'}</span></div>
                        <div style="margin-top: 8px;">
                            <span class="label">Time left:</span>
                            <span class="time-left ${timeClass}">${formatTime(timeLeft)}</span>
                        </div>
                    </div>

                    <div class="prices-grid">
                        <div class="price-box yes">
                            <div class="token-label">YES Token</div>
                            <div class="prices">
                                <div class="price-item">
                                    <div class="price-label">Ask</div>
                                    <div class="price-value ask">${formatPrice(data.yes_ask)}</div>
                                </div>
                                <div class="price-item">
                                    <div class="price-label">Bid</div>
                                    <div class="price-value bid">${formatPrice(data.yes_bid)}</div>
                                </div>
                            </div>
                        </div>
                        <div class="price-box no">
                            <div class="token-label">NO Token</div>
                            <div class="prices">
                                <div class="price-item">
                                    <div class="price-label">Ask</div>
                                    <div class="price-value ask">${formatPrice(data.no_ask)}</div>
                                </div>
                                <div class="price-item">
                                    <div class="price-label">Bid</div>
                                    <div class="price-value bid">${formatPrice(data.no_bid)}</div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="positions-grid">
                        ${renderSidePosition(data, 'YES')}
                        ${renderSidePosition(data, 'NO')}
                    </div>
                </div>
            `;
        }

        function renderStats(stats) {
            const total = stats.total || {};
            const pnlClass = (total.pnl || 0) >= 0 ? 'positive' : 'negative';
            return `
                <div class="stat-box">
                    <div class="stat-value">${total.trades || 0}</div>
                    <div class="stat-label">Total Trades</div>
                </div>
                <div class="stat-box positive">
                    <div class="stat-value">${total.wins || 0}</div>
                    <div class="stat-label">Wins</div>
                </div>
                <div class="stat-box negative">
                    <div class="stat-value">${total.losses || 0}</div>
                    <div class="stat-label">Losses</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">${(total.win_rate || 0).toFixed(1)}%</div>
                    <div class="stat-label">Win Rate</div>
                </div>
                <div class="stat-box ${pnlClass}">
                    <div class="stat-value">$${(total.pnl || 0).toFixed(2)}</div>
                    <div class="stat-label">Total PnL</div>
                </div>
            `;
        }

        let allTrades = [];

        function renderHistory() {
            const af = document.getElementById('filter-asset').value;
            const sf = document.getElementById('filter-side').value;
            const rf = document.getElementById('filter-result').value;

            let filtered = allTrades;
            if (af) filtered = filtered.filter(t => t.asset === af);
            if (sf) filtered = filtered.filter(t => t.side === sf);
            if (rf) filtered = filtered.filter(t => t.result === rf);

            document.getElementById('trade-count').textContent = `${filtered.length} trades`;

            const tbody = document.getElementById('history-tbody');
            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="10" class="no-data">No trades</td></tr>';
                return;
            }

            tbody.innerHTML = filtered.map(t => {
                const pnl = parseFloat(t.pnl) || 0;
                const pnlClass = pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
                const pnlSign = pnl >= 0 ? '+' : '';
                const sideClass = t.side === 'YES' ? 'side-YES' : 'side-NO';
                return `
                    <tr>
                        <td>${t.ts || '-'}</td>
                        <td><strong>${t.asset || '-'}</strong></td>
                        <td class="${sideClass}">${t.side || '-'}</td>
                        <td>${parseFloat(t.size || 0).toFixed(2)}</td>
                        <td>$${parseFloat(t.buy_price || 0).toFixed(4)}</td>
                        <td>$${parseFloat(t.sell_price || 0).toFixed(4)}</td>
                        <td class="${pnlClass}">${pnlSign}$${pnl.toFixed(4)}</td>
                        <td class="result-${t.result}">${t.result || '-'}</td>
                        <td>${t.martingale_step || 0}</td>
                        <td class="market-slug">${t.market_slug || '-'}</td>
                    </tr>
                `;
            }).join('');
        }

        async function updateDashboard() {
            try {
                const [statusResp, statsResp, tradesResp] = await Promise.all([
                    fetch('/api/status'), fetch('/api/stats'), fetch('/api/trades?limit=500')
                ]);
                const status = await statusResp.json();
                const stats = await statsResp.json();
                allTrades = await tradesResp.json();

                const container = document.getElementById('assets-container');
                const assets = Object.entries(status);
                if (assets.length === 0) {
                    container.innerHTML = '<div class="no-data">No bots running</div>';
                } else {
                    container.innerHTML = assets
                        .sort((a, b) => a[0].localeCompare(b[0]))
                        .map(([asset, data]) => renderAssetCard(asset, data))
                        .join('');
                }

                document.getElementById('stats-container').innerHTML = renderStats(stats);
                renderHistory();
                document.getElementById('last-update').textContent =
                    'Last update: ' + new Date().toLocaleTimeString();
            } catch (error) {
                console.error('Dashboard update error:', error);
            }
        }

        updateDashboard();
        setInterval(updateDashboard, 1000);
    </script>
</body>
</html>"""

    title = html.escape(REGISTRY.dashboard_title)
    return HTMLResponse(content=html_content.replace("{{DASHBOARD_TITLE}}", title))
