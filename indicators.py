"""
Pure-Python technical indicators. Every function takes plain lists (oldest
value first, matching candles as returned by coindcx_client.get_candles)
and returns a list of the same length, front-padded with None wherever the
indicator isn't yet defined. Working in plain lists (no numpy/pandas)
keeps the dependency footprint of this project to just `requests` and the
dashboard's `flask`.
"""

from __future__ import annotations

import math
from typing import Optional


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------

def sma(values, period):
    out = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# RSI (Wilder's smoothing)
# ---------------------------------------------------------------------------

def rsi(closes, period: int = 14):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)

    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def macd(closes, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    n = len(closes)
    macd_line = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    # signal line = EMA of macd_line, computed only over its defined tail
    first_valid = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line = [None] * n
    if first_valid is not None:
        tail = [v for v in macd_line[first_valid:] if v is not None]
        sig_tail = ema(tail, signal)
        for offset, val in enumerate(sig_tail):
            signal_line[first_valid + offset] = val

    hist = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist[i] = macd_line[i] - signal_line[i]

    return macd_line, signal_line, hist


# ---------------------------------------------------------------------------
# ATR (Wilder's smoothing)
# ---------------------------------------------------------------------------

def true_range(highs, lows, closes):
    n = len(highs)
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    return tr


def atr(highs, lows, closes, period: int = 14):
    n = len(highs)
    out = [None] * n
    if n <= period:
        return out
    tr = true_range(highs, lows, closes)
    avg = sum(tr[1:period + 1]) / period
    out[period] = avg
    for i in range(period + 1, n):
        avg = (avg * (period - 1) + tr[i]) / period
        out[i] = avg
    return out


# ---------------------------------------------------------------------------
# VWAP (anchored to the start of the supplied candle window)
# ---------------------------------------------------------------------------

def vwap(highs, lows, closes, volumes):
    """
    Anchored VWAP over the candle window passed in. engine.py anchors this
    to the start of the current UTC day among the fetched candles so it
    behaves like a normal session VWAP.
    """
    n = len(closes)
    out = [None] * n
    cum_pv = 0.0
    cum_vol = 0.0
    for i in range(n):
        typical = (highs[i] + lows[i] + closes[i]) / 3
        cum_pv += typical * volumes[i]
        cum_vol += volumes[i]
        out[i] = (cum_pv / cum_vol) if cum_vol > 0 else None
    return out


# ---------------------------------------------------------------------------
# ADX / DI
# ---------------------------------------------------------------------------

def adx(highs, lows, closes, period: int = 14):
    """Returns (plus_di, minus_di, adx) lists."""
    n = len(highs)
    plus_di = [None] * n
    minus_di = [None] * n
    adx_out = [None] * n
    if n <= period * 2:
        return plus_di, minus_di, adx_out

    tr = true_range(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    smoothed_tr = sum(tr[1:period + 1])
    smoothed_plus_dm = sum(plus_dm[1:period + 1])
    smoothed_minus_dm = sum(minus_dm[1:period + 1])

    dx_values = [None] * n

    def _fill_di_dx(i, s_tr, s_pdm, s_mdm):
        pdi = 100 * (s_pdm / s_tr) if s_tr else 0.0
        mdi = 100 * (s_mdm / s_tr) if s_tr else 0.0
        plus_di[i] = pdi
        minus_di[i] = mdi
        denom = pdi + mdi
        dx_values[i] = 100 * abs(pdi - mdi) / denom if denom else 0.0

    _fill_di_dx(period, smoothed_tr, smoothed_plus_dm, smoothed_minus_dm)

    for i in range(period + 1, n):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[i]
        _fill_di_dx(i, smoothed_tr, smoothed_plus_dm, smoothed_minus_dm)

    # ADX = smoothed average of DX, starting one full `period` after DX begins
    dx_start = period
    dx_valid = [v for v in dx_values[dx_start:] if v is not None]
    if len(dx_valid) >= period:
        first_adx = sum(dx_valid[:period]) / period
        adx_out[dx_start + period - 1] = first_adx
        prev = first_adx
        for offset in range(period, len(dx_valid)):
            prev = (prev * (period - 1) + dx_valid[offset]) / period
            adx_out[dx_start + offset] = prev

    return plus_di, minus_di, adx_out


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def bollinger_bands(closes, period: int = 20, num_std: float = 2):
    mid = sma(closes, period)
    n = len(closes)
    upper = [None] * n
    lower = [None] * n
    for i in range(n):
        if mid[i] is None:
            continue
        window = closes[i - period + 1: i + 1]
        mean = mid[i]
        variance = sum((x - mean) ** 2 for x in window) / period
        std = math.sqrt(variance)
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return upper, mid, lower


# ---------------------------------------------------------------------------
# Swing highs / lows and market structure
# ---------------------------------------------------------------------------

def swing_points(highs, lows, lookback: int = 3):
    """
    Returns (swing_high_indices, swing_low_indices): a bar is a swing high
    if its high is the max within +/- `lookback` bars, and a swing low if
    its low is the min within +/- `lookback` bars.
    """
    n = len(highs)
    swing_high_idx = []
    swing_low_idx = []
    for i in range(lookback, n - lookback):
        window_high = highs[i - lookback: i + lookback + 1]
        window_low = lows[i - lookback: i + lookback + 1]
        if highs[i] == max(window_high):
            swing_high_idx.append(i)
        if lows[i] == min(window_low):
            swing_low_idx.append(i)
    return swing_high_idx, swing_low_idx


def last_swing_low(lows, swing_low_idx, before_index: int):
    candidates = [i for i in swing_low_idx if i <= before_index]
    if not candidates:
        return None
    idx = candidates[-1]
    return idx, lows[idx]


def last_swing_high(highs, swing_high_idx, before_index: int):
    candidates = [i for i in swing_high_idx if i <= before_index]
    if not candidates:
        return None
    idx = candidates[-1]
    return idx, highs[idx]


def market_structure_label(highs, lows, swing_high_idx, swing_low_idx, at_index: int) -> str:
    """
    Compares the two most recent confirmed swing highs (<= at_index) and the
    two most recent confirmed swing lows to classify structure as one of:
    'HH_HL' (uptrend), 'LH_LL' (downtrend), 'MIXED', or 'INSUFFICIENT_DATA'.
    """
    recent_highs = [i for i in swing_high_idx if i <= at_index][-2:]
    recent_lows = [i for i in swing_low_idx if i <= at_index][-2:]
    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return "INSUFFICIENT_DATA"

    higher_high = highs[recent_highs[-1]] > highs[recent_highs[-2]]
    higher_low = lows[recent_lows[-1]] > lows[recent_lows[-2]]
    lower_high = highs[recent_highs[-1]] < highs[recent_highs[-2]]
    lower_low = lows[recent_lows[-1]] < lows[recent_lows[-2]]

    if higher_high and higher_low:
        return "HH_HL"
    if lower_high and lower_low:
        return "LH_LL"
    return "MIXED"


def support_resistance_levels(highs, lows, swing_high_idx, swing_low_idx, at_index: int, lookback_points: int = 6):
    """Recent swing highs/lows treated as resistance/support levels."""
    res_idx = [i for i in swing_high_idx if i <= at_index][-lookback_points:]
    sup_idx = [i for i in swing_low_idx if i <= at_index][-lookback_points:]
    resistances = sorted({round(highs[i], 8) for i in res_idx}, reverse=True)
    supports = sorted({round(lows[i], 8) for i in sup_idx}, reverse=True)
    return resistances, supports


# ---------------------------------------------------------------------------
# Candle shape metrics
# ---------------------------------------------------------------------------

def candle_metrics(o: float, h: float, l: float, c: float) -> dict:
    full_range = h - l
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if full_range <= 0:
        return {"body_pct": 0.0, "upper_wick_pct": 0.0, "lower_wick_pct": 0.0, "bullish": c >= o}
    return {
        "body_pct": 100 * body / full_range,
        "upper_wick_pct": 100 * upper_wick / full_range,
        "lower_wick_pct": 100 * lower_wick / full_range,
        "bullish": c >= o,
    }


def is_bullish_engulfing(prev: dict, cur: dict) -> bool:
    return (
        prev["close"] < prev["open"]
        and cur["close"] > cur["open"]
        and cur["close"] >= prev["open"]
        and cur["open"] <= prev["close"]
    )


def is_bearish_engulfing(prev: dict, cur: dict) -> bool:
    return (
        prev["close"] > prev["open"]
        and cur["close"] < cur["open"]
        and cur["open"] >= prev["close"]
        and cur["close"] <= prev["open"]
    )


# ---------------------------------------------------------------------------
# Resampling 1m candles into higher timeframes (5m / 15m)
# ---------------------------------------------------------------------------

def resample_candles(candles_1m: list, minutes: int) -> list:
    """
    Groups consecutive 1m candles (oldest-first, each with a ms 'time') into
    `minutes`-long buckets aligned to epoch boundaries, the same way exchange
    charting normally aligns higher timeframes.
    """
    if not candles_1m:
        return []
    bucket_ms = minutes * 60_000
    buckets = {}
    order = []
    for c in candles_1m:
        bucket_start = (c["time"] // bucket_ms) * bucket_ms
        if bucket_start not in buckets:
            buckets[bucket_start] = []
            order.append(bucket_start)
        buckets[bucket_start].append(c)

    out = []
    for bucket_start in order:
        group = buckets[bucket_start]
        out.append({
            "time": bucket_start,
            "open": group[0]["open"],
            "high": max(g["high"] for g in group),
            "low": min(g["low"] for g in group),
            "close": group[-1]["close"],
            "volume": sum(g["volume"] for g in group),
        })
    return out
