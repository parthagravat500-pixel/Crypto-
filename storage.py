"""
SQLite persistence for signals and their outcomes. This is the single
source of truth the dashboard's stats and adaptive-filtering breakdowns are
computed from -- nothing about win rate / P&L is ever hard-coded or
simulated; it is always derived from rows written here as real signals
actually resolve.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,               -- LONG | SHORT
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    tp1 REAL NOT NULL,
    tp2 REAL NOT NULL,
    confidence INTEGER NOT NULL,
    rr_tp1 REAL,
    rr_tp2 REAL,
    reasons_json TEXT,
    indicators_json TEXT,                  -- snapshot of key indicator values at signal time
    created_at REAL NOT NULL,              -- unix epoch seconds
    status TEXT NOT NULL,                  -- WAITING | ACTIVE | TP1_HIT | TP_COMPLETE | SL_HIT | CANCELLED
    triggered_at REAL,
    resolved_at REAL,
    exit_price REAL,
    exit_reason TEXT,
    mfe REAL DEFAULT 0,                    -- max favorable excursion, in price units
    mae REAL DEFAULT 0,                    -- max adverse excursion, in price units
    breakeven_sl REAL,                     -- SL after being moved to breakeven post-TP1
    hour_of_day INTEGER,
    adx_at_entry REAL,
    rsi_at_entry REAL,
    volume_ratio_at_entry REAL,
    trend_condition TEXT                   -- 'trending' | 'choppy'
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
"""


@contextmanager
def _conn():
    with _lock:
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)


def insert_signal(row: dict) -> int:
    fields = [
        "pair", "symbol", "direction", "entry", "stop_loss", "tp1", "tp2",
        "confidence", "rr_tp1", "rr_tp2", "reasons_json", "indicators_json",
        "created_at", "status", "hour_of_day", "adx_at_entry", "rsi_at_entry",
        "volume_ratio_at_entry", "trend_condition",
    ]
    values = [row.get(f) for f in fields]
    placeholders = ",".join("?" for _ in fields)
    with _conn() as conn:
        cur = conn.execute(
            f"INSERT INTO signals ({','.join(fields)}) VALUES ({placeholders})", values
        )
        return cur.lastrowid


def update_signal(signal_id: int, **fields):
    if not fields:
        return
    set_clause = ",".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [signal_id]
    with _conn() as conn:
        conn.execute(f"UPDATE signals SET {set_clause} WHERE id=?", values)


def get_signal(signal_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return dict(row) if row else None


def get_open_signals() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE status IN ('WAITING','ACTIVE','TP1_HIT') ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_signals(limit: int = 50) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_signals_today() -> list:
    midnight = time.time() - (time.time() % 86400)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE created_at >= ? ORDER BY created_at DESC", (midnight,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_resolved_signals() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE status IN ('TP_COMPLETE','SL_HIT','CANCELLED') "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def compute_stats(rows: list) -> dict:
    """
    Pure function over a list of signal rows (dicts) -> win/loss stats.
    A signal counts as a "win" if it hit TP1 or TP2 (status TP1_HIT counts
    as a partial win once triggered; TP_COMPLETE is a full win), and a
    "loss" if SL_HIT. CANCELLED / WAITING signals are excluded from win
    rate since they never triggered a real outcome.
    """
    decided = [r for r in rows if r["status"] in ("TP_COMPLETE", "SL_HIT", "TP1_HIT")]
    wins = [r for r in decided if r["status"] in ("TP_COMPLETE", "TP1_HIT")]
    losses = [r for r in decided if r["status"] == "SL_HIT"]
    total = len(decided)
    win_rate = (len(wins) / total * 100) if total else 0.0

    rr_values = [r["rr_tp1"] for r in decided if r.get("rr_tp1")]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

    net_r = 0.0
    for r in decided:
        risk = abs(r["entry"] - r["stop_loss"]) or 1e-9
        if r["status"] == "SL_HIT":
            net_r -= 1.0
        elif r["status"] == "TP_COMPLETE":
            net_r += abs(r["tp2"] - r["entry"]) / risk
        elif r["status"] == "TP1_HIT":
            net_r += abs(r["tp1"] - r["entry"]) / risk

    return {
        "signals_total": len(rows),
        "signals_decided": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_rr": round(avg_rr, 2),
        "net_r_multiple": round(net_r, 2),
    }


def compute_breakdown_by(rows: list, key_fn) -> dict:
    """Groups resolved rows by key_fn(row) and computes stats per group."""
    groups: dict = {}
    for r in rows:
        if r["status"] not in ("TP_COMPLETE", "SL_HIT", "TP1_HIT"):
            continue
        key = key_fn(r)
        groups.setdefault(key, []).append(r)
    return {k: compute_stats(v) for k, v in groups.items()}
