"""
Dynamic SL/TP calculation. No fixed percentage is ever used -- everything
is derived from ATR, the most recent confirmed swing high/low, and nearby
support/resistance, per spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import config
import indicators as ind
from signal_engine import TFSnapshot


@dataclass
class RiskPlan:
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    risk_per_unit: float
    rr_tp1: float
    rr_tp2: float
    valid: bool
    reject_reason: Optional[str] = None


def _room_to_level(entry: float, level, direction: str):
    if level is None:
        return None
    if direction == "LONG":
        room = level - entry
        return room if room > 0 else None
    room = entry - level
    return room if room > 0 else None


def build_risk_plan(m1: TFSnapshot, direction: str, entry: float) -> RiskPlan:
    i = m1.i
    atr_val = m1.atr[i]
    if atr_val is None or atr_val <= 0:
        return RiskPlan(entry, entry, entry, entry, 0, 0, 0, False, "ATR not available yet")

    buffer = config.ATR_SL_BUFFER_MULTIPLE * atr_val

    if direction == "LONG":
        swing = ind.last_swing_low(m1.lows, m1.swing_low_idx, i)
        if swing is None:
            stop_loss = entry - 1.5 * atr_val  # fallback when no confirmed swing yet
        else:
            stop_loss = swing[1] - buffer
        risk = entry - stop_loss
    else:
        swing = ind.last_swing_high(m1.highs, m1.swing_high_idx, i)
        if swing is None:
            stop_loss = entry + 1.5 * atr_val
        else:
            stop_loss = swing[1] + buffer
        risk = stop_loss - entry

    if risk <= 0:
        return RiskPlan(entry, stop_loss, entry, entry, 0, 0, 0, False, "computed risk distance is zero or negative")

    resistances, supports = ind.support_resistance_levels(
        m1.highs, m1.lows, m1.swing_high_idx, m1.swing_low_idx, i
    )

    if direction == "LONG":
        tp1_r = entry + config.TP1_R_MULTIPLE * risk
        tp2_r = entry + config.TP2_R_MULTIPLE * risk
        next_resistances = sorted([r for r in resistances if r > entry])
        ceiling = next_resistances[0] if next_resistances else None
        tp1 = min(tp1_r, ceiling) if ceiling else tp1_r
        tp2 = min(tp2_r, ceiling) if ceiling else tp2_r
        room = _room_to_level(entry, ceiling, "LONG")
    else:
        tp1_r = entry - config.TP1_R_MULTIPLE * risk
        tp2_r = entry - config.TP2_R_MULTIPLE * risk
        next_supports = sorted([s for s in supports if s < entry], reverse=True)
        floor = next_supports[0] if next_supports else None
        tp1 = max(tp1_r, floor) if floor else tp1_r
        tp2 = max(tp2_r, floor) if floor else tp2_r
        room = _room_to_level(entry, floor, "SHORT")

    if room is not None and room < config.MIN_ROOM_TO_SR_ATR_MULTIPLE * atr_val:
        return RiskPlan(
            entry, stop_loss, tp1, tp2, risk, 0, 0, False,
            "not enough room before the next support/resistance level to justify an entry",
        )

    rr_tp1 = abs(tp1 - entry) / risk
    rr_tp2 = abs(tp2 - entry) / risk

    if rr_tp1 < config.MIN_RR_TP1:
        return RiskPlan(
            entry, stop_loss, tp1, tp2, risk, rr_tp1, rr_tp2, False,
            f"reward:risk to TP1 ({rr_tp1:.2f}) is below the minimum acceptable {config.MIN_RR_TP1}",
        )

    return RiskPlan(entry, stop_loss, tp1, tp2, risk, rr_tp1, rr_tp2, True)
