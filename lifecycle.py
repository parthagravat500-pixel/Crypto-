"""
Tracks each open signal's status against live price, tick by tick:

    WAITING    -> price hasn't reached the suggested entry zone yet
    ACTIVE     -> entry has triggered, trade is live
    TP1_HIT    -> first target reached (SL optionally moved to breakeven)
    TP_COMPLETE-> second target reached, trade fully closed out
    SL_HIT     -> stop loss reached
    CANCELLED  -> WAITING signal went stale without triggering

This module only ever reads live price and writes to the local database --
it never places, amends, or cancels a real exchange order.
"""

from __future__ import annotations

import logging
import time

import config
import storage

log = logging.getLogger("lifecycle")


def _within_tolerance(price: float, entry: float) -> bool:
    if entry == 0:
        return False
    return abs(price - entry) / entry * 100 <= config.ENTRY_TRIGGER_TOLERANCE_PCT


def _update_excursion(signal: dict, price: float) -> tuple:
    direction = signal["direction"]
    entry = signal["entry"]
    mfe = signal["mfe"] or 0.0
    mae = signal["mae"] or 0.0
    if direction == "LONG":
        favorable = price - entry
        adverse = entry - price
    else:
        favorable = entry - price
        adverse = price - entry
    mfe = max(mfe, favorable)
    mae = max(mae, adverse)
    return mfe, mae


def process_tick(signal: dict, current_price: float) -> dict:
    """
    Given a signal row (dict, as from storage) and the current live price,
    determines whether a status transition should happen. Returns a dict of
    fields to persist via storage.update_signal (empty dict if nothing
    changed besides excursion tracking, which is always included when it
    moved).
    """
    status = signal["status"]
    direction = signal["direction"]
    entry = signal["entry"]
    sl = signal["breakeven_sl"] if (signal.get("breakeven_sl") and status == "TP1_HIT") else signal["stop_loss"]
    tp1 = signal["tp1"]
    tp2 = signal["tp2"]

    updates: dict = {}
    mfe, mae = _update_excursion(signal, current_price)
    if mfe != (signal["mfe"] or 0.0) or mae != (signal["mae"] or 0.0):
        updates["mfe"] = mfe
        updates["mae"] = mae

    if status == "WAITING":
        age_minutes = (time.time() - signal["created_at"]) / 60
        if age_minutes > config.SIGNAL_STALE_MINUTES:
            updates["status"] = "CANCELLED"
            updates["resolved_at"] = time.time()
            updates["exit_reason"] = "signal went stale before entry was reached"
            return updates

        triggered = (
            _within_tolerance(current_price, entry)
            or (direction == "LONG" and current_price <= entry)
            or (direction == "SHORT" and current_price >= entry)
        )
        if triggered:
            updates["status"] = "ACTIVE"
            updates["triggered_at"] = time.time()
        return updates

    if status in ("ACTIVE", "TP1_HIT"):
        hit_sl = (direction == "LONG" and current_price <= sl) or (
            direction == "SHORT" and current_price >= sl
        )
        hit_tp2 = (direction == "LONG" and current_price >= tp2) or (
            direction == "SHORT" and current_price <= tp2
        )
        hit_tp1 = (direction == "LONG" and current_price >= tp1) or (
            direction == "SHORT" and current_price <= tp1
        )

        if hit_sl:
            updates["status"] = "SL_HIT"
            updates["resolved_at"] = time.time()
            updates["exit_price"] = current_price
            updates["exit_reason"] = (
                "stop loss hit after breakeven move" if status == "TP1_HIT" else "stop loss hit"
            )
            return updates

        if hit_tp2:
            updates["status"] = "TP_COMPLETE"
            updates["resolved_at"] = time.time()
            updates["exit_price"] = current_price
            updates["exit_reason"] = "take profit 2 reached"
            return updates

        if status == "ACTIVE" and hit_tp1:
            updates["status"] = "TP1_HIT"
            if config.BREAKEVEN_AFTER_TP1:
                updates["breakeven_sl"] = entry
            return updates

    return updates


def sweep(pair_to_price: dict):
    """
    Applies process_tick to every currently open signal, using the latest
    known price for its pair. pair_to_price: {"B-BTC_USDT": 65000.5, ...}
    """
    open_signals = storage.get_open_signals()
    for signal in open_signals:
        price = pair_to_price.get(signal["pair"])
        if price is None:
            continue
        updates = process_tick(signal, price)
        if updates:
            storage.update_signal(signal["id"], **updates)
            if "status" in updates:
                log.info(
                    "Signal #%s %s %s -> %s @ %.6f",
                    signal["id"], signal["symbol"], signal["direction"], updates["status"], price,
                )
