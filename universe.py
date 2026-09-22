"""Select the Top-N liquid CoinDCX USDT futures pairs.

The old implementation made dozens of candle/order-book calls during startup.
That was slow on a small Render instance and used endpoints CoinDCX no longer
publishes for futures market data. This version uses the official single
current-prices snapshot plus the active-instruments endpoint, then lets the
trade-time signal engine apply spread/volatility quality filters.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import config
import coindcx_client as cdx

log = logging.getLogger("universe")


@dataclass
class CandidateScore:
    pair: str
    symbol: str
    volume_24h: float
    spread_pct: float
    atr_pct: float
    last_price: float
    composite_score: float


def _pair_symbol(pair: str) -> str:
    return pair[len(config.FUTURES_PAIR_PREFIX):-len(config.FUTURES_QUOTE_SUFFIX)]


def select_top_pairs(n: int = None) -> list:
    n = n or config.TOP_N_COINS
    active = set(cdx.get_active_futures_pairs())
    prices = cdx.get_current_futures_prices()

    preferred = set(config.CANDIDATE_SHORTLIST)
    candidates = []
    for pair, row in prices.items():
        if pair not in active or not pair.startswith(config.FUTURES_PAIR_PREFIX) or not pair.endswith(config.FUTURES_QUOTE_SUFFIX):
            continue
        symbol = _pair_symbol(pair)
        if preferred and symbol not in preferred:
            continue
        if not isinstance(row, dict):
            continue
        try:
            volume = float(row.get("v", 0) or 0)
            last_price = float(row.get("ls", row.get("mp", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if volume <= 0 or last_price <= 0:
            continue
        candidates.append(CandidateScore(
            pair=pair,
            symbol=symbol,
            volume_24h=volume,
            spread_pct=0.0,
            atr_pct=0.0,
            last_price=last_price,
            composite_score=volume,
        ))

    # If CoinDCX renamed a preferred symbol, fall back to all active USDT futures.
    if len(candidates) < n:
        seen = {c.pair for c in candidates}
        for pair, row in prices.items():
            if pair in seen or pair not in active or not pair.startswith("B-") or not pair.endswith("_USDT"):
                continue
            if not isinstance(row, dict):
                continue
            try:
                volume = float(row.get("v", 0) or 0)
                last_price = float(row.get("ls", row.get("mp", 0)) or 0)
            except (TypeError, ValueError):
                continue
            if volume > 0 and last_price > 0:
                candidates.append(CandidateScore(pair, _pair_symbol(pair), volume, 0.0, 0.0, last_price, volume))

    candidates.sort(key=lambda c: c.volume_24h, reverse=True)
    top = candidates[:n]
    log.info("Selected top %d futures pairs: %s", len(top), [c.symbol for c in top])
    return top
