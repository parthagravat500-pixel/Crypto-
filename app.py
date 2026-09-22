"""
Flask dashboard for the CoinDCX scalping signal engine -- single flat file
by design (no templates/ or static/ subfolders) so the whole project is a
handful of loose .py files at the repo root. That matters for deployment
via GitHub's mobile web upload, which does not reliably preserve nested
folder structures.

Local two-terminal use is unchanged:
    python engine.py     # terminal 1 -- generates signals
    python app.py         # terminal 2 -- serves the UI, on http://127.0.0.1:8765

Single-process use (e.g. on Render, see render.yaml):
    RUN_ENGINE_INPROCESS=1 gunicorn app:app --workers 1 --bind 0.0.0.0:$PORT

The dashboard is read-only: it reads engine.py's live_state.json snapshot
and the shared SQLite database, and for the chart view it also calls
CoinDCX's public candles endpoint directly. It never places orders.
"""

from __future__ import annotations

import json
import os
import time

from flask import Flask, Response, jsonify, request

import config
import coindcx_client as cdx
import indicators as ind
import storage

app = Flask(__name__)
STATE_PATH = config.STATE_PATH


def _read_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"connection_ok": False, "last_error": "engine has not written any state yet -- give it a minute",
                "updated_at": 0, "cards": [], "monitored_symbols": []}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"connection_ok": False, "last_error": "state file is being written, try again",
                "updated_at": 0, "cards": [], "monitored_symbols": []}


def _live_pnl_pct(card: dict) -> float:
    if card.get("direction") not in ("LONG", "SHORT") or not card.get("entry"):
        return 0.0
    entry = card["entry"]
    price = card["price"]
    if entry == 0:
        return 0.0
    if card["direction"] == "LONG":
        return round(100 * (price - entry) / entry, 3)
    return round(100 * (entry - price) / entry, 3)


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/state")
def api_state():
    state = _read_state()
    for card in state.get("cards", []):
        card["live_pnl_pct"] = _live_pnl_pct(card)
    stale = state.get("updated_at", 0) and (time.time() - state["updated_at"] > 60)
    state["stale"] = bool(stale)
    return jsonify(state)


@app.route("/api/signals/open")
def api_open_signals():
    rows = storage.get_open_signals()
    for r in rows:
        r["reasons"] = json.loads(r.get("reasons_json") or "[]")
    return jsonify(rows)


@app.route("/api/signals/history")
def api_signal_history():
    limit = int(request.args.get("limit", 50))
    rows = storage.get_recent_signals(limit)
    for r in rows:
        r["reasons"] = json.loads(r.get("reasons_json") or "[]")
    return jsonify(rows)


@app.route("/api/stats")
def api_stats():
    today_rows = storage.get_signals_today()
    all_resolved = storage.get_all_resolved_signals()
    overall = storage.compute_stats(today_rows)
    by_symbol = storage.compute_breakdown_by(all_resolved, lambda r: r["symbol"])
    by_direction = storage.compute_breakdown_by(all_resolved, lambda r: r["direction"])
    by_trend = storage.compute_breakdown_by(all_resolved, lambda r: r["trend_condition"] or "unknown")

    def hour_bucket(r):
        h = r.get("hour_of_day")
        return f"{h:02d}:00" if h is not None else "unknown"

    def rsi_bucket(r):
        v = r.get("rsi_at_entry")
        if v is None:
            return "unknown"
        if v < 40:
            return "<40 (oversold zone)"
        if v < 60:
            return "40-60 (neutral)"
        return ">60 (overbought zone)"

    def adx_bucket(r):
        v = r.get("adx_at_entry")
        if v is None:
            return "unknown"
        if v < 20:
            return "<20 (weak trend)"
        if v < 30:
            return "20-30 (moderate trend)"
        return ">30 (strong trend)"

    def volume_bucket(r):
        v = r.get("volume_ratio_at_entry")
        if v is None:
            return "unknown"
        if v < 1.1:
            return "low (<1.1x avg)"
        if v < 1.5:
            return "moderate (1.1-1.5x avg)"
        return "high (>1.5x avg)"

    by_hour = storage.compute_breakdown_by(all_resolved, hour_bucket)
    by_rsi = storage.compute_breakdown_by(all_resolved, rsi_bucket)
    by_adx = storage.compute_breakdown_by(all_resolved, adx_bucket)
    by_volume = storage.compute_breakdown_by(all_resolved, volume_bucket)

    return jsonify({
        "today": overall,
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "by_trend_condition": by_trend,
        "by_hour": by_hour,
        "by_rsi_range": by_rsi,
        "by_adx_range": by_adx,
        "by_volume_level": by_volume,
    })


@app.route("/api/chart/<symbol>")
def api_chart(symbol):
    pair = f"{config.FUTURES_PAIR_PREFIX}{symbol.upper()}{config.FUTURES_QUOTE_SUFFIX}"
    try:
        candles = cdx.get_candles(pair, interval=config.CANDLE_INTERVAL_EXEC, limit=200)
    except cdx.CoinDCXError as exc:
        return jsonify({"error": str(exc)}), 502

    if len(candles) < 20:
        return jsonify({"error": "not enough candle history yet"}), 200

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    ema9 = ind.ema(closes, 9)
    ema20 = ind.ema(closes, 20)
    ema50 = ind.ema(closes, 50)
    vwap = ind.vwap(highs, lows, closes, volumes)

    recent = [r for r in storage.get_recent_signals(200) if r["symbol"] == symbol.upper()][:20]
    markers = [
        {"time": int(r["created_at"] * 1000), "direction": r["direction"], "entry": r["entry"], "status": r["status"]}
        for r in recent
    ]
    open_signal = next((r for r in storage.get_open_signals() if r["symbol"] == symbol.upper()), None)

    return jsonify({
        "pair": pair, "candles": candles, "ema9": ema9, "ema20": ema20, "ema50": ema50, "vwap": vwap,
        "markers": markers, "open_signal": open_signal,
    })


# ---------------------------------------------------------------------------
# Optional: run the signal engine's polling loop as a background thread
# inside this same process. Used for single-process deployment (e.g. on
# Render, where a free/starter plan gives you one web service and no
# separate background worker). Controlled by RUN_ENGINE_INPROCESS=1.
#
# Runs at import time (not inside `if __name__ == "__main__"`) so it also
# fires when a WSGI server like gunicorn imports `app:app` directly rather
# than executing this file as a script. Guarded by a flag so re-imports
# don't start a second copy. Deploy with exactly one worker process
# (`gunicorn ... --workers 1`) -- more than one would start multiple
# independent engine loops writing conflicting signals to the same database.
# ---------------------------------------------------------------------------
_engine_thread_started = False


def _start_engine_in_background():
    global _engine_thread_started
    if _engine_thread_started:
        return
    _engine_thread_started = True
    import threading
    from engine import Engine

    def _run():
        Engine().run_forever()

    threading.Thread(target=_run, daemon=True, name="engine-loop").start()


if config.RUN_ENGINE_INPROCESS:
    _start_engine_in_background()


# ---------------------------------------------------------------------------
# The whole UI, inlined. Plain string (not an f-string) on purpose -- the
# CSS and JS below are full of literal { } braces.
# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>CoinDCX Live Scalper</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root {
  --bg: #0b0f14;
  --panel: #121820;
  --panel-border: #1f2a37;
  --text: #e6edf3;
  --text-dim: #8b98a5;
  --green: #22c55e;
  --red: #ef4444;
  --amber: #f59e0b;
  --blue: #3b82f6;
  --radius: 12px;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  padding-bottom: 40px;
}
.topbar {
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
  padding: 16px 20px; border-bottom: 1px solid var(--panel-border);
  position: sticky; top: 0; background: rgba(11, 15, 20, 0.92); backdrop-filter: blur(6px); z-index: 10;
}
.topbar-title { display: flex; align-items: center; gap: 10px; }
.topbar-title h1 { font-size: 18px; margin: 0; letter-spacing: 0.02em; }
.logo-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }
.topbar-status { display: flex; gap: 8px; }
.badge { font-size: 12px; padding: 5px 10px; border-radius: 999px; border: 1px solid var(--panel-border); white-space: nowrap; }
.badge-market { color: var(--text-dim); }
.badge-connected { color: var(--green); border-color: rgba(34,197,94,0.35); background: rgba(34,197,94,0.08); }
.badge-disconnected { color: var(--red); border-color: rgba(239,68,68,0.35); background: rgba(239,68,68,0.08); }
.badge-unknown { color: var(--text-dim); }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
.cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-bottom: 24px; }
.coin-card { background: var(--panel); border: 1px solid var(--panel-border); border-radius: var(--radius); padding: 14px; cursor: pointer; transition: border-color 0.15s ease, transform 0.15s ease; }
.coin-card:hover { border-color: #33415a; transform: translateY(-1px); }
.coin-card.selected { border-color: var(--blue); }
.coin-card-top { display: flex; justify-content: space-between; align-items: baseline; }
.coin-symbol { font-weight: 700; font-size: 16px; }
.coin-price { font-size: 13px; color: var(--text-dim); }
.coin-direction { margin-top: 8px; font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 6px; }
.dir-long { color: var(--green); }
.dir-short { color: var(--red); }
.dir-no-trade { color: var(--text-dim); }
.coin-confidence { font-size: 12px; color: var(--text-dim); margin-top: 2px; }
.coin-levels { margin-top: 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 4px 10px; font-size: 12px; }
.coin-levels div span.label { color: var(--text-dim); margin-right: 4px; }
.coin-pnl { margin-top: 8px; font-size: 13px; font-weight: 600; }
.pnl-pos { color: var(--green); }
.pnl-neg { color: var(--red); }
.coin-waiting { margin-top: 8px; font-size: 12px; color: var(--text-dim); font-style: italic; }
.chart-section { background: var(--panel); border: 1px solid var(--panel-border); border-radius: var(--radius); padding: 14px; margin-bottom: 24px; }
.chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.chart-header h2 { margin: 0; font-size: 15px; }
.symbol-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.symbol-tab { font-size: 12px; padding: 5px 10px; border-radius: 999px; border: 1px solid var(--panel-border); cursor: pointer; color: var(--text-dim); }
.symbol-tab.active { color: var(--text); border-color: var(--blue); background: rgba(59,130,246,0.1); }
#chart-container { height: 380px; margin-top: 10px; }
.chart-legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--text-dim); margin-top: 8px; }
.chart-legend span.swatch { display: inline-block; width: 10px; height: 3px; margin-right: 4px; vertical-align: middle; }
.panel { background: var(--panel); border: 1px solid var(--panel-border); border-radius: var(--radius); padding: 14px; margin-bottom: 24px; }
.panel h2 { margin: 0 0 10px 0; font-size: 15px; }
.signal-list { display: flex; flex-direction: column; gap: 8px; }
.signal-row { display: grid; grid-template-columns: 90px 70px 1fr 100px; gap: 10px; align-items: center; font-size: 12px; padding: 10px; border: 1px solid var(--panel-border); border-radius: 8px; }
.signal-row .sym { font-weight: 700; font-size: 13px; }
.status-pill { font-size: 11px; padding: 3px 8px; border-radius: 999px; text-align: center; border: 1px solid var(--panel-border); }
.status-WAITING { color: var(--amber); border-color: rgba(245,158,11,0.4); }
.status-ACTIVE { color: var(--blue); border-color: rgba(59,130,246,0.4); }
.status-TP1_HIT { color: var(--green); border-color: rgba(34,197,94,0.4); }
.status-TP_COMPLETE { color: var(--green); border-color: rgba(34,197,94,0.4); }
.status-SL_HIT { color: var(--red); border-color: rgba(239,68,68,0.4); }
.status-CANCELLED { color: var(--text-dim); }
.signal-reason { color: var(--text-dim); }
.signal-levels { color: var(--text-dim); text-align: right; white-space: nowrap; }
.empty-state { color: var(--text-dim); font-size: 13px; padding: 12px 0; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 18px; }
.stat-tile { border: 1px solid var(--panel-border); border-radius: 8px; padding: 10px; text-align: center; }
.stat-tile .value { font-size: 20px; font-weight: 700; }
.stat-tile .label { font-size: 11px; color: var(--text-dim); margin-top: 2px; }
.breakdowns { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
.breakdown-block h3 { font-size: 12px; color: var(--text-dim); margin: 0 0 6px 0; text-transform: uppercase; letter-spacing: 0.04em; }
.breakdown-list { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.breakdown-row { display: flex; justify-content: space-between; padding: 4px 6px; border-radius: 6px; background: rgba(255,255,255,0.02); }
.disclaimer { color: var(--text-dim); font-size: 11px; text-align: center; margin-top: 20px; line-height: 1.5; }
@media (max-width: 480px) {
  .cards-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .signal-row { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-title">
    <span class="logo-dot"></span>
    <h1>CoinDCX LIVE SCALPER</h1>
  </div>
  <div class="topbar-status">
    <span id="conn-badge" class="badge badge-unknown">connecting...</span>
    <span class="badge badge-market">Market: LIVE</span>
  </div>
</header>

<main>
  <section id="cards" class="cards-grid"></section>

  <section class="chart-section">
    <div class="chart-header">
      <h2 id="chart-title">Chart</h2>
      <div id="chart-symbol-tabs" class="symbol-tabs"></div>
    </div>
    <div id="chart-container"></div>
    <div id="chart-legend" class="chart-legend"></div>
  </section>

  <section class="panel">
    <h2>Active Signals</h2>
    <div id="active-signals" class="signal-list"></div>
  </section>

  <section class="panel">
    <h2>Signal History</h2>
    <div id="signal-history" class="signal-list"></div>
  </section>

  <section class="panel">
    <h2>Win / Loss Statistics</h2>
    <div id="stats-grid" class="stats-grid"></div>
    <div class="breakdowns">
      <div class="breakdown-block"><h3>By Coin</h3><div id="breakdown-symbol" class="breakdown-list"></div></div>
      <div class="breakdown-block"><h3>By Direction</h3><div id="breakdown-direction" class="breakdown-list"></div></div>
      <div class="breakdown-block"><h3>Trend Condition</h3><div id="breakdown-trend" class="breakdown-list"></div></div>
      <div class="breakdown-block"><h3>ADX Range</h3><div id="breakdown-adx" class="breakdown-list"></div></div>
      <div class="breakdown-block"><h3>RSI Range</h3><div id="breakdown-rsi" class="breakdown-list"></div></div>
      <div class="breakdown-block"><h3>Volume Level</h3><div id="breakdown-volume" class="breakdown-list"></div></div>
    </div>
  </section>

  <footer class="disclaimer">
    Signal-only tool. No orders are ever placed automatically. Confidence scores describe how many
    independent factors aligned -- they are not a guaranteed probability of profit. You decide
    whether and how to act on any signal.
  </footer>
</main>

<script>
const STATE_POLL_MS = 4000;
const CHART_POLL_MS = 8000;

let selectedSymbol = null;
let chart = null;
let candleSeries = null;
let ema9Series = null;
let ema20Series = null;
let ema50Series = null;
let vwapSeries = null;
let priceLines = [];

function fmt(n, digits) {
  digits = digits || 4;
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  const num = Number(n);
  if (Math.abs(num) >= 1000) return num.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return num.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function directionLabel(dir) {
  if (dir === "LONG") return { icon: "\\uD83D\\uDFE2", text: "LONG", cls: "dir-long" };
  if (dir === "SHORT") return { icon: "\\uD83D\\uDD34", text: "SHORT", cls: "dir-short" };
  return { icon: "\\u26AA", text: "NO TRADE", cls: "dir-no-trade" };
}

function renderCards(cards) {
  const container = document.getElementById("cards");
  container.innerHTML = "";
  if (!cards || cards.length === 0) {
    container.innerHTML = '<div class="empty-state">Waiting for the engine to publish its first scan...</div>';
    return;
  }
  cards.forEach((card) => {
    const dir = directionLabel(card.direction);
    const div = document.createElement("div");
    div.className = "coin-card" + (card.symbol === selectedSymbol ? " selected" : "");
    div.onclick = () => selectSymbol(card.symbol);

    let levelsHtml = "";
    let pnlHtml = "";
    if (card.direction === "LONG" || card.direction === "SHORT") {
      levelsHtml = '<div class="coin-levels">' +
          '<div><span class="label">Entry</span>' + fmt(card.entry) + '</div>' +
          '<div><span class="label">SL</span>' + fmt(card.stop_loss) + '</div>' +
          '<div><span class="label">TP1</span>' + fmt(card.tp1) + '</div>' +
          '<div><span class="label">TP2</span>' + fmt(card.tp2) + '</div>' +
        '</div>';
      const pnl = card.live_pnl_pct || 0;
      const pnlCls = pnl >= 0 ? "pnl-pos" : "pnl-neg";
      pnlHtml = '<div class="coin-pnl ' + pnlCls + '">' + (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + '% live</div>';
    } else {
      const reason = card.wait_for_pullback ? "WAIT FOR PULLBACK" : (card.no_trade_reason || "Waiting for setup");
      levelsHtml = '<div class="coin-waiting">' + reason + '</div>';
    }

    const errHtml = card.error ? '<div class="coin-waiting">' + card.error + '</div>' : levelsHtml;
    const confHtml = card.direction !== "NO_TRADE" ? (' &middot; ' + card.confidence) : "";

    div.innerHTML =
      '<div class="coin-card-top"><span class="coin-symbol">' + card.symbol + '</span>' +
      '<span class="coin-price">' + fmt(card.price) + '</span></div>' +
      '<div class="coin-direction ' + dir.cls + '">' + dir.icon + ' ' + dir.text + confHtml + '</div>' +
      errHtml + pnlHtml;
    container.appendChild(div);
  });

  if (!selectedSymbol && cards.length) {
    selectSymbol(cards[0].symbol);
  }
}

function renderConnectionBadge(state) {
  const badge = document.getElementById("conn-badge");
  if (state.stale || !state.connection_ok) {
    badge.textContent = state.connection_ok ? "stale (engine slow to update)" : "disconnected";
    badge.className = "badge badge-disconnected";
  } else {
    badge.textContent = "Connected";
    badge.className = "badge badge-connected";
  }
}

function renderSignalRow(sig) {
  const dir = directionLabel(sig.direction);
  const reasons = (sig.reasons || []).slice(0, 2).join("; ");
  const div = document.createElement("div");
  div.className = "signal-row";
  div.innerHTML =
    '<div class="sym">' + dir.icon + ' ' + sig.symbol + '</div>' +
    '<div class="status-pill status-' + sig.status + '">' + sig.status.replace("_", " ") + '</div>' +
    '<div class="signal-reason">' + (reasons || "--") + '</div>' +
    '<div class="signal-levels">E ' + fmt(sig.entry) + ' &middot; SL ' + fmt(sig.stop_loss) + '<br>TP1 ' + fmt(sig.tp1) + ' &middot; TP2 ' + fmt(sig.tp2) + '</div>';
  return div;
}

function renderList(elId, rows) {
  const el = document.getElementById(elId);
  el.innerHTML = "";
  if (!rows || rows.length === 0) {
    el.innerHTML = '<div class="empty-state">Nothing here yet.</div>';
    return;
  }
  rows.forEach((r) => el.appendChild(renderSignalRow(r)));
}

function renderStatTile(container, value, label) {
  const div = document.createElement("div");
  div.className = "stat-tile";
  div.innerHTML = '<div class="value">' + value + '</div><div class="label">' + label + '</div>';
  container.appendChild(div);
}

function renderStats(data) {
  const grid = document.getElementById("stats-grid");
  grid.innerHTML = "";
  const t = data.today;
  renderStatTile(grid, t.signals_total, "Signals Today");
  renderStatTile(grid, t.wins, "Winning Signals");
  renderStatTile(grid, t.losses, "Losing Signals");
  renderStatTile(grid, t.win_rate + "%", "Win Rate");
  renderStatTile(grid, t.avg_rr, "Avg R:R");
  renderStatTile(grid, (t.net_r_multiple >= 0 ? "+" : "") + t.net_r_multiple + "R", "Net Theoretical Return");

  renderBreakdown("breakdown-symbol", data.by_symbol);
  renderBreakdown("breakdown-direction", data.by_direction);
  renderBreakdown("breakdown-trend", data.by_trend_condition);
  renderBreakdown("breakdown-adx", data.by_adx_range);
  renderBreakdown("breakdown-rsi", data.by_rsi_range);
  renderBreakdown("breakdown-volume", data.by_volume_level);
}

function renderBreakdown(elId, groups) {
  const el = document.getElementById(elId);
  el.innerHTML = "";
  const keys = Object.keys(groups || {});
  if (keys.length === 0) {
    el.innerHTML = '<div class="empty-state">No resolved signals yet.</div>';
    return;
  }
  keys.forEach((k) => {
    const g = groups[k];
    const row = document.createElement("div");
    row.className = "breakdown-row";
    row.innerHTML = '<span>' + k + '</span><span>' + g.win_rate + '% (' + g.wins + 'W/' + g.losses + 'L)</span>';
    el.appendChild(row);
  });
}

function ensureChart() {
  if (chart) return;
  if (typeof LightweightCharts === "undefined") {
    throw new Error("charting library did not load");
  }
  const container = document.getElementById("chart-container");
  chart = LightweightCharts.createChart(container, {
    layout: { background: { color: "#121820" }, textColor: "#8b98a5" },
    grid: { vertLines: { color: "#1f2a37" }, horzLines: { color: "#1f2a37" } },
    rightPriceScale: { borderColor: "#1f2a37" },
    timeScale: { borderColor: "#1f2a37", timeVisible: true, secondsVisible: false },
    autoSize: true,
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
    wickUpColor: "#22c55e", wickDownColor: "#ef4444",
  });
  ema9Series = chart.addLineSeries({ color: "#f59e0b", lineWidth: 1, priceLineVisible: false });
  ema20Series = chart.addLineSeries({ color: "#3b82f6", lineWidth: 1, priceLineVisible: false });
  ema50Series = chart.addLineSeries({ color: "#a855f7", lineWidth: 1, priceLineVisible: false });
  vwapSeries = chart.addLineSeries({ color: "#14b8a6", lineWidth: 1, priceLineVisible: false, lineStyle: 2 });

  document.getElementById("chart-legend").innerHTML =
    '<span><span class="swatch" style="background:#f59e0b"></span>EMA 9</span>' +
    '<span><span class="swatch" style="background:#3b82f6"></span>EMA 20</span>' +
    '<span><span class="swatch" style="background:#a855f7"></span>EMA 50</span>' +
    '<span><span class="swatch" style="background:#14b8a6"></span>VWAP</span>';
}

function clearPriceLines() {
  priceLines.forEach((pl) => candleSeries.removePriceLine(pl));
  priceLines = [];
}

function addPriceLine(price, color, title) {
  if (price === null || price === undefined) return;
  const pl = candleSeries.createPriceLine({ price: price, color: color, lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: title });
  priceLines.push(pl);
}

async function loadChart(symbol) {
  document.getElementById("chart-title").textContent = symbol + " \\u00b7 1m";
  try {
    ensureChart();
  } catch (e) {
    console.error("chart init failed", e);
    document.getElementById("chart-container").innerHTML =
      '<div style="padding:20px;color:#8b98a5;font-size:13px;">Chart library failed to load ' +
      '(likely a slow or blocked connection to the CDN). Price cards and signals above are ' +
      'still live -- try reloading the page, or switching wifi/mobile data.</div>';
    return;
  }
  try {
    const res = await fetch("/api/chart/" + symbol);
    const data = await res.json();
    if (data.error) {
      console.error("chart api error", data.error);
      document.getElementById("chart-legend").innerHTML =
        '<span style="color:#ef4444;">Chart data error: ' + data.error + '</span>';
      return;
    }
    if (!data.candles || data.candles.length === 0) {
      document.getElementById("chart-legend").innerHTML =
        '<span style="color:#8b98a5;">No candle data returned for ' + symbol + '.</span>';
      return;
    }

    const candleData = data.candles.map((c) => ({ time: Math.floor(c.time / 1000), open: c.open, high: c.high, low: c.low, close: c.close }));
    candleSeries.setData(candleData);

    const toLineData = (series) =>
      data.candles.map((c, i) => ({ time: Math.floor(c.time / 1000), value: series[i] })).filter((p) => p.value !== null && p.value !== undefined);

    ema9Series.setData(toLineData(data.ema9));
    ema20Series.setData(toLineData(data.ema20));
    ema50Series.setData(toLineData(data.ema50));
    vwapSeries.setData(toLineData(data.vwap));

    clearPriceLines();
    if (data.open_signal) {
      const s = data.open_signal;
      addPriceLine(s.entry, "#3b82f6", "ENTRY");
      addPriceLine(s.stop_loss, "#ef4444", "SL");
      addPriceLine(s.tp1, "#22c55e", "TP1");
      addPriceLine(s.tp2, "#22c55e", "TP2");
    }

    const markers = (data.markers || []).map((m) => ({
      time: Math.floor(m.time / 1000),
      position: m.direction === "LONG" ? "belowBar" : "aboveBar",
      color: m.direction === "LONG" ? "#22c55e" : "#ef4444",
      shape: m.direction === "LONG" ? "arrowUp" : "arrowDown",
      text: m.direction === "LONG" ? "LONG" : "SHORT",
    }));
    candleSeries.setMarkers(markers);
  } catch (e) {
    console.error("chart load failed", e);
    document.getElementById("chart-legend").innerHTML =
      '<span style="color:#ef4444;">Chart failed to render: ' + (e && e.message ? e.message : e) + '</span>';
  }
}

function selectSymbol(symbol) {
  selectedSymbol = symbol;
  loadChart(symbol);
  renderSymbolTabs();
}

function renderSymbolTabs() {
  const tabs = document.getElementById("chart-symbol-tabs");
  const cards = window.__lastCards || [];
  tabs.innerHTML = "";
  cards.forEach((c) => {
    const tab = document.createElement("div");
    tab.className = "symbol-tab" + (c.symbol === selectedSymbol ? " active" : "");
    tab.textContent = c.symbol;
    tab.onclick = () => selectSymbol(c.symbol);
    tabs.appendChild(tab);
  });
}

async function pollState() {
  try {
    const res = await fetch("/api/state");
    const state = await res.json();
    renderConnectionBadge(state);
    window.__lastCards = state.cards || [];
    renderCards(state.cards);
    renderSymbolTabs();
  } catch (e) {
    console.error("state poll failed", e);
  }
}

async function pollSignals() {
  try {
    const [openRes, histRes, statsRes] = await Promise.all([
      fetch("/api/signals/open"),
      fetch("/api/signals/history?limit=30"),
      fetch("/api/stats"),
    ]);
    renderList("active-signals", await openRes.json());
    renderList("signal-history", await histRes.json());
    renderStats(await statsRes.json());
  } catch (e) {
    console.error("signals poll failed", e);
  }
}

pollState();
pollSignals();
setInterval(pollState, STATE_POLL_MS);
setInterval(pollSignals, STATE_POLL_MS);
setInterval(() => { if (selectedSymbol) loadChart(selectedSymbol); }, CHART_POLL_MS);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False)
