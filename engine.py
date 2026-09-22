"""
Main polling loop: refreshes the Top-N universe periodically, pulls fresh
1m/5m/15m candles for each monitored pair, runs the signal engine, opens
new WAITING signals, and sweeps every open signal's lifecycle against the
latest price -- all against CoinDCX's public REST endpoints only.

This process holds no exchange API key and issues no order-placing calls
of any kind. It is read-only against CoinDCX; the only thing it writes to
is the local SQLite file in storage.py.

Run directly:  python engine.py
The dashboard (dashboard/app.py) reads the same SQLite file and the
in-memory state file this writes, so it can run as a completely separate
process alongside this one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, is_dataclass

import config
import coindcx_client as cdx
import indicators as ind
import signal_engine as se
import risk_manager as rm
import storage
import lifecycle
import universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("engine")

STATE_PATH = config.STATE_PATH
_state_lock = threading.Lock()


class Engine:
    def __init__(self):
        self.monitored: list = []          # list of universe.CandidateScore
        self.last_universe_scan = 0.0
        self.latest_price: dict = {}       # pair -> last close price
        self.card_state: dict = {}         # symbol -> dict for dashboard cards
        self.connection_ok = True
        self.last_error = None
        storage.init_db()

    # -- universe -----------------------------------------------------
    def refresh_universe_if_needed(self):
        now = time.time()
        if now - self.last_universe_scan < config.UNIVERSE_RESCAN_SECONDS and self.monitored:
            return
        try:
            self.monitored = universe.select_top_pairs(config.TOP_N_COINS)
            self.last_universe_scan = now
            self.connection_ok = True
            self.last_error = None
        except cdx.CoinDCXError as exc:
            log.error("Universe refresh failed: %s", exc)
            self.connection_ok = False
            self.last_error = str(exc)

    # -- per-pair analysis ---------------------------------------------
    def analyze_pair(self, cand) -> dict:
        pair = cand.pair
        symbol = cand.symbol
        try:
            candles_1m = cdx.get_candles(pair, interval=config.CANDLE_INTERVAL_EXEC, limit=config.CANDLES_FETCH_LIMIT)
        except cdx.CoinDCXError as exc:
            log.warning("Candle fetch failed for %s: %s", pair, exc)
            return {"symbol": symbol, "pair": pair, "error": str(exc)}

        if len(candles_1m) < 60:
            return {"symbol": symbol, "pair": pair, "error": "insufficient candle history"}

        candles_5m = ind.resample_candles(candles_1m, 5)
        candles_15m = ind.resample_candles(candles_1m, 15)
        if len(candles_5m) < 20 or len(candles_15m) < 20:
            return {"symbol": symbol, "pair": pair, "error": "insufficient higher-timeframe history yet"}

        m1 = se.build_snapshot(candles_1m)
        m5 = se.build_snapshot(candles_5m)
        m15 = se.build_snapshot(candles_15m)

        current_price = m1.closes[m1.i]
        self.latest_price[pair] = current_price

        spread_pct = None
        try:
            bid, ask = cdx.best_bid_ask(pair)
            if bid and ask:
                spread_pct = 100 * (ask - bid) / ((ask + bid) / 2)
        except cdx.CoinDCXError as exc:
            log.debug("Orderbook fetch failed for %s: %s", pair, exc)

        decision = se.decide(m1, m5, m15, spread_pct)

        card = {
            "symbol": symbol,
            "pair": pair,
            "price": current_price,
            "direction": decision.direction,
            "confidence": decision.confidence,
            "reasons": decision.reasons,
            "no_trade_reason": decision.no_trade_reason,
            "wait_for_pullback": decision.wait_for_pullback,
            "updated_at": time.time(),
        }

        if decision.direction in ("LONG", "SHORT"):
            plan = rm.build_risk_plan(m1, decision.direction, current_price)
            if plan.valid:
                card.update({
                    "entry": plan.entry,
                    "stop_loss": plan.stop_loss,
                    "tp1": plan.tp1,
                    "tp2": plan.tp2,
                    "rr_tp1": round(plan.rr_tp1, 2),
                    "rr_tp2": round(plan.rr_tp2, 2),
                })
                self.maybe_open_signal(pair, symbol, decision, plan, m1)
            else:
                card["direction"] = "NO_TRADE"
                card["no_trade_reason"] = plan.reject_reason

        return card

    # -- signal creation (dedup against existing open signal) ----------
    def maybe_open_signal(self, pair, symbol, decision: se.SignalDecision, plan: rm.RiskPlan, m1: se.TFSnapshot):
        open_signals = storage.get_open_signals()
        already_open = any(s["pair"] == pair and s["status"] in ("WAITING", "ACTIVE", "TP1_HIT") for s in open_signals)
        if already_open:
            return

        i = m1.i
        vol_ratio = None
        if m1.vol_sma[i]:
            vol_ratio = round(m1.volumes[i] / m1.vol_sma[i], 2)

        indicators_snapshot = {
            "ema9": m1.ema9[i], "ema20": m1.ema20[i], "ema50": m1.ema50[i],
            "rsi": m1.rsi[i], "macd_hist": m1.macd_hist[i], "atr": m1.atr[i],
            "vwap": m1.vwap[i], "adx": m1.adx[i], "volume_ratio": vol_ratio,
        }
        trend_condition = "trending" if (m1.adx[i] or 0) >= config.MIN_ADX_FOR_TREND else "choppy"

        row = {
            "pair": pair, "symbol": symbol, "direction": decision.direction,
            "entry": plan.entry, "stop_loss": plan.stop_loss, "tp1": plan.tp1, "tp2": plan.tp2,
            "confidence": decision.confidence, "rr_tp1": plan.rr_tp1, "rr_tp2": plan.rr_tp2,
            "reasons_json": json.dumps(decision.reasons),
            "indicators_json": json.dumps(indicators_snapshot),
            "created_at": time.time(), "status": "WAITING",
            "hour_of_day": time.localtime().tm_hour,
            "adx_at_entry": m1.adx[i], "rsi_at_entry": m1.rsi[i],
            "volume_ratio_at_entry": vol_ratio, "trend_condition": trend_condition,
        }
        sid = storage.insert_signal(row)
        log.info(
            "New %s signal #%s for %s @ %.6f (confidence %d)",
            decision.direction, sid, symbol, plan.entry, decision.confidence,
        )

    # -- persistence of dashboard state --------------------------------
    def write_state(self):
        state = {
            "connection_ok": self.connection_ok,
            "last_error": self.last_error,
            "updated_at": time.time(),
            "cards": list(self.card_state.values()),
            "monitored_symbols": [c.symbol for c in self.monitored],
        }
        with _state_lock:
            with open(STATE_PATH, "w") as f:
                json.dump(state, f, default=str)

    # -- main loop -------------------------------------------------------
    def run_forever(self):
        log.info("Engine starting. Signal-only mode: no orders will ever be placed.")
        while True:
            loop_start = time.time()
            try:
                self.refresh_universe_if_needed()
                for cand in self.monitored:
                    card = self.analyze_pair(cand)
                    self.card_state[cand.symbol] = card

                if self.latest_price:
                    lifecycle.sweep(self.latest_price)

                self.write_state()
            except Exception:
                log.exception("Unhandled error in engine loop")
                self.connection_ok = False

            elapsed = time.time() - loop_start
            sleep_for = max(1.0, config.POLL_INTERVAL_SECONDS - elapsed)
            time.sleep(sleep_for)


if __name__ == "__main__":
    Engine().run_forever()
