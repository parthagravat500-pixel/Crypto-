"""
Turns computed indicators into a scored LONG / SHORT / NO TRADE decision.

Design intent (per spec): a signal should only fire when *several
independent* factors agree. No single indicator can push confidence past
the display threshold on its own -- each category is capped at its own
weight (see config.SCORE_WEIGHTS), so a lone strong reading in one category
still can't reach MIN_CONFIDENCE_TO_DISPLAY alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import config
import indicators as ind


@dataclass
class TFSnapshot:
    """Indicator snapshot for one timeframe, at its most recent closed bar."""
    closes: list
    highs: list
    lows: list
    volumes: list
    ema9: list
    ema20: list
    ema50: list
    ema200: list
    rsi: list
    macd_line: list
    macd_signal: list
    macd_hist: list
    atr: list
    vwap: list
    vol_sma: list
    plus_di: list
    minus_di: list
    adx: list
    bb_upper: list
    bb_mid: list
    bb_lower: list
    swing_high_idx: list
    swing_low_idx: list

    @property
    def i(self) -> int:
        return len(self.closes) - 1


def build_snapshot(candles: list) -> TFSnapshot:
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    ema9 = ind.ema(closes, 9)
    ema20 = ind.ema(closes, 20)
    ema50 = ind.ema(closes, 50)
    ema200 = ind.ema(closes, 200)
    rsi = ind.rsi(closes, config.RSI_PERIOD)
    macd_line, macd_signal, macd_hist = ind.macd(
        closes, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL
    )
    atr = ind.atr(highs, lows, closes, config.ATR_PERIOD)
    vwap = ind.vwap(highs, lows, closes, volumes)
    vol_sma = ind.sma(volumes, config.VOLUME_SMA_PERIOD)
    plus_di, minus_di, adx = ind.adx(highs, lows, closes, config.ADX_PERIOD)
    bb_upper, bb_mid, bb_lower = ind.bollinger_bands(
        closes, config.BOLLINGER_PERIOD, config.BOLLINGER_STD
    )
    swing_high_idx, swing_low_idx = ind.swing_points(highs, lows, config.SWING_LOOKBACK)

    return TFSnapshot(
        closes=closes, highs=highs, lows=lows, volumes=volumes,
        ema9=ema9, ema20=ema20, ema50=ema50, ema200=ema200,
        rsi=rsi, macd_line=macd_line, macd_signal=macd_signal, macd_hist=macd_hist,
        atr=atr, vwap=vwap, vol_sma=vol_sma,
        plus_di=plus_di, minus_di=minus_di, adx=adx,
        bb_upper=bb_upper, bb_mid=bb_mid, bb_lower=bb_lower,
        swing_high_idx=swing_high_idx, swing_low_idx=swing_low_idx,
    )


@dataclass
class ScoreBreakdown:
    direction: str                  # "LONG" | "SHORT"
    total: int = 0
    parts: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)


def _trend_alignment_score(m1: TFSnapshot, m5: TFSnapshot, m15: TFSnapshot, direction: str):
    """Higher-timeframe confirmation: 5m and 15m trend should agree with the trade direction."""
    weight = config.SCORE_WEIGHTS["trend_alignment"]

    def tf_bias(snap: TFSnapshot):
        i = snap.i
        if snap.ema20[i] is None or snap.ema50[i] is None:
            return None
        if snap.ema20[i] > snap.ema50[i]:
            return "up"
        if snap.ema20[i] < snap.ema50[i]:
            return "down"
        return None

    bias5 = tf_bias(m5)
    bias15 = tf_bias(m15)
    wanted = "up" if direction == "LONG" else "down"

    agree_count = sum(1 for b in (bias5, bias15) if b == wanted)
    if agree_count == 2:
        return weight, "5m and 15m trend both support this direction"
    if agree_count == 1:
        return round(weight * 0.5), "one higher timeframe (5m or 15m) supports this direction"
    return 0, "higher timeframes do not confirm this direction"


def _structure_score(m1: TFSnapshot, direction: str):
    weight = config.SCORE_WEIGHTS["market_structure"]
    label = ind.market_structure_label(m1.highs, m1.lows, m1.swing_high_idx, m1.swing_low_idx, m1.i)
    if direction == "LONG" and label == "HH_HL":
        return weight, "1m structure shows Higher-High / Higher-Low"
    if direction == "SHORT" and label == "LH_LL":
        return weight, "1m structure shows Lower-High / Lower-Low"
    if label == "MIXED":
        return round(weight * 0.4), "1m structure is mixed but leans in this direction"
    return 0, "1m structure does not confirm this direction"


def _volume_score(m1: TFSnapshot, direction: str):
    weight = config.SCORE_WEIGHTS["volume_confirmation"]
    i = m1.i
    if m1.vol_sma[i] is None or m1.vol_sma[i] == 0:
        return 0, "not enough volume history yet"
    vol_ratio = m1.volumes[i] / m1.vol_sma[i]
    candle_bullish = m1.closes[i] >= m1.closes[i - 1] if i > 0 else True
    aligned = (direction == "LONG" and candle_bullish) or (direction == "SHORT" and not candle_bullish)
    if aligned and vol_ratio >= 1.5:
        return weight, "volume is well above average on a candle in this direction"
    if aligned and vol_ratio >= 1.1:
        return round(weight * 0.6), "volume is modestly above average in this direction"
    return 0, "volume does not confirm this move"


def _ema_setup_score(m1: TFSnapshot, direction: str):
    weight = config.SCORE_WEIGHTS["ema_setup"]
    i = m1.i
    e9, e20 = m1.ema9[i], m1.ema20[i]
    if e9 is None or e20 is None:
        return 0, "not enough history for EMA9/EMA20"
    if direction == "LONG" and e9 >= e20:
        rising = m1.ema20[i] > m1.ema20[i - 5] if i >= 5 and m1.ema20[i - 5] is not None else False
        return (weight if rising else round(weight * 0.6)), (
            "EMA9 above/crossing EMA20, EMA20 rising" if rising else "EMA9 above EMA20"
        )
    if direction == "SHORT" and e9 <= e20:
        falling = m1.ema20[i] < m1.ema20[i - 5] if i >= 5 and m1.ema20[i - 5] is not None else False
        return (weight if falling else round(weight * 0.6)), (
            "EMA9 below/crossing EMA20, EMA20 falling" if falling else "EMA9 below EMA20"
        )
    return 0, "EMA9/EMA20 alignment does not confirm this direction"


def _vwap_score(m1: TFSnapshot, direction: str):
    weight = config.SCORE_WEIGHTS["vwap"]
    i = m1.i
    if m1.vwap[i] is None:
        return 0, "VWAP not available"
    price = m1.closes[i]
    if direction == "LONG" and price >= m1.vwap[i]:
        return weight, "price above/reclaiming VWAP"
    if direction == "SHORT" and price <= m1.vwap[i]:
        return weight, "price below/losing VWAP"
    return 0, "price on wrong side of VWAP"


def _rsi_score(m1: TFSnapshot, direction: str):
    weight = config.SCORE_WEIGHTS["rsi"]
    i = m1.i
    r = m1.rsi[i]
    if r is None:
        return 0, "RSI not available"
    prev_r = m1.rsi[i - 1] if i > 0 and m1.rsi[i - 1] is not None else r
    if direction == "LONG":
        if 40 <= r <= 68 and r >= prev_r:
            return weight, "RSI turning up without being overbought"
        if r < 40 and r >= prev_r:
            return round(weight * 0.5), "RSI recovering from oversold"
    else:
        if 32 <= r <= 60 and r <= prev_r:
            return weight, "RSI turning down without being oversold"
        if r > 60 and r <= prev_r:
            return round(weight * 0.5), "RSI easing from overbought"
    return 0, "RSI momentum does not confirm this direction"


def _macd_score(m1: TFSnapshot, direction: str):
    weight = config.SCORE_WEIGHTS["macd"]
    i = m1.i
    if m1.macd_hist[i] is None or (i > 0 and m1.macd_hist[i - 1] is None):
        return 0, "MACD not available"
    hist_now, hist_prev = m1.macd_hist[i], m1.macd_hist[i - 1]
    if direction == "LONG" and hist_now > hist_prev:
        return weight, "MACD histogram improving"
    if direction == "SHORT" and hist_now < hist_prev:
        return weight, "MACD histogram weakening"
    return 0, "MACD momentum does not confirm this direction"


def _candle_pattern_score(cm: dict, prev_cm, direction: str):
    weight = config.SCORE_WEIGHTS["candle_pattern"]
    if direction == "LONG":
        if cm["bullish"] and cm["lower_wick_pct"] >= 35:
            return weight, "strong lower-wick rejection on a bullish candle"
        if cm["bullish"] and cm["body_pct"] >= 55:
            return round(weight * 0.7), "strong bullish-bodied candle"
    else:
        if not cm["bullish"] and cm["upper_wick_pct"] >= 35:
            return weight, "strong upper-wick rejection on a bearish candle"
        if not cm["bullish"] and cm["body_pct"] >= 55:
            return round(weight * 0.7), "strong bearish-bodied candle"
    return 0, "candle shape does not show a clear rejection/continuation pattern"


@dataclass
class SignalDecision:
    direction: str                    # "LONG" | "SHORT" | "NO_TRADE"
    confidence: int
    reasons: list
    wait_for_pullback: bool = False
    no_trade_reason: Optional[str] = None


def score_direction(m1: TFSnapshot, m5: TFSnapshot, m15: TFSnapshot, direction: str, cm: dict, prev_cm) -> ScoreBreakdown:
    sb = ScoreBreakdown(direction=direction)
    scorers = [
        ("trend_alignment", lambda: _trend_alignment_score(m1, m5, m15, direction)),
        ("market_structure", lambda: _structure_score(m1, direction)),
        ("volume_confirmation", lambda: _volume_score(m1, direction)),
        ("ema_setup", lambda: _ema_setup_score(m1, direction)),
        ("vwap", lambda: _vwap_score(m1, direction)),
        ("rsi", lambda: _rsi_score(m1, direction)),
        ("macd", lambda: _macd_score(m1, direction)),
        ("candle_pattern", lambda: _candle_pattern_score(cm, prev_cm, direction)),
    ]
    for name, fn in scorers:
        points, reason = fn()
        sb.parts[name] = points
        sb.total += points
        if points > 0:
            sb.reasons.append(reason)
    return sb


def check_no_trade_filters(m1: TFSnapshot) -> Optional[str]:
    """Hard filters that veto a trade regardless of score. Returns a reason string, or None if clear."""
    i = m1.i
    if m1.adx[i] is not None and m1.adx[i] < config.MIN_ADX_FOR_TREND:
        return f"ADX {m1.adx[i]:.1f} indicates weak/choppy conditions"
    if m1.vol_sma[i] and m1.volumes[i] / m1.vol_sma[i] < config.MIN_VOLUME_SMA_RATIO:
        return "volume is unusually low versus its recent average"
    if m1.atr[i] is not None and m1.closes[i] > 0:
        atr_pct = 100 * m1.atr[i] / m1.closes[i]
        if atr_pct > config.MAX_ATR_PCT_FOR_SANE_VOL:
            return f"ATR is {atr_pct:.2f}% of price -- abnormal volatility"
    return None


def check_anti_chasing(m1: TFSnapshot, direction: str) -> bool:
    """
    Returns True if price has already extended too far from EMA20 in the
    trade direction (i.e. we'd be chasing), meaning we should emit
    WAIT FOR PULLBACK instead of a fresh entry.
    """
    i = m1.i
    if m1.ema20[i] is None or m1.atr[i] is None or m1.atr[i] == 0:
        return False
    extension = m1.closes[i] - m1.ema20[i]
    extension_atrs = abs(extension) / m1.atr[i]
    if extension_atrs < config.MAX_EXTENSION_ATR_MULTIPLE:
        return False
    if direction == "LONG" and extension > 0:
        return True
    if direction == "SHORT" and extension < 0:
        return True
    return False


def decide(m1: TFSnapshot, m5: TFSnapshot, m15: TFSnapshot, spread_pct) -> SignalDecision:
    i = m1.i
    if i < 1:
        return SignalDecision("NO_TRADE", 0, [], no_trade_reason="insufficient candle history")

    hard_stop = check_no_trade_filters(m1)
    if hard_stop:
        return SignalDecision("NO_TRADE", 0, [], no_trade_reason=hard_stop)

    if spread_pct is not None and spread_pct > config.MAX_SPREAD_PCT:
        return SignalDecision(
            "NO_TRADE", 0, [], no_trade_reason=f"spread {spread_pct:.3f}% is too wide"
        )

    # Use the previous close as an approximation for "open" of the current
    # bar's shape analysis when the raw open isn't separately tracked.
    o_cur = m1.closes[i - 1]
    cm = ind.candle_metrics(o_cur, m1.highs[i], m1.lows[i], m1.closes[i])
    prev_cm = None
    if i >= 2:
        prev_cm = ind.candle_metrics(m1.closes[i - 2], m1.highs[i - 1], m1.lows[i - 1], m1.closes[i - 1])

    long_score = score_direction(m1, m5, m15, "LONG", cm, prev_cm)
    short_score = score_direction(m1, m5, m15, "SHORT", cm, prev_cm)

    best = long_score if long_score.total >= short_score.total else short_score

    if best.total < config.MIN_CONFIDENCE_TO_DISPLAY:
        return SignalDecision(
            "NO_TRADE", best.total, best.reasons,
            no_trade_reason="conditions weak or conflicting -- fewer than the required independent factors aligned",
        )

    if check_anti_chasing(m1, best.direction):
        return SignalDecision(
            "NO_TRADE", best.total, best.reasons,
            wait_for_pullback=True,
            no_trade_reason="price already extended from EMA20 -- WAIT FOR PULLBACK instead of chasing",
        )

    return SignalDecision(best.direction, min(best.total, 100), best.reasons)
