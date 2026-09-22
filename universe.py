"""
Selects the Top N USDT-margined futures pairs to monitor, based on:
  1. 24h trading volume (liquidity proxy)
  2. Tight bid/ask spread
  3. Sufficient 1-minute volatility (ATR% of price) for scalping
  4. The pair actually returning usable candle data

We start from a shortlist of generally-liquid coins (config.CANDIDATE_SHORTLIST)
rather than screening every futures pair on CoinDCX on every rescan, which
would be hundreds of HTTP calls each cycle. The shortlist only bounds what
gets *screened* -- ranking and selection are always computed live.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import config
import coindcx_client as cdx
import indicators as ind

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
    # "B-BTC_USDT" -> "BTC"
    body = pair[len(config.FUTURES_PAIR_PREFIX):-len(config.FUTURES_QUOTE_SUFFIX)]
    return body


def _screen_pair(pair: str, ticker_by_market: dict, markets_details_by_pair: dict) -> Optional[CandidateScore]:
    try:
        candles = cdx.get_candles(pair, interval=config.CANDLE_INTERVAL_EXEC, limit=60)
        if len(candles) < 20:
            return None
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        atr_series = ind.atr(highs, lows, closes, period=14)
        last_atr = next((v for v in reversed(atr_series) if v is not None), None)
        last_price = closes[-1]
        if last_atr is None or last_price <= 0:
            return None
        atr_pct = 100 * last_atr / last_price

        bid, ask = cdx.best_bid_ask(pair)
        if bid is None or ask is None or bid <= 0:
            return None
        spread_pct = 100 * (ask - bid) / ((ask + bid) / 2)

        market_meta = markets_details_by_pair.get(pair, {})
        coindcx_name = market_meta.get("coindcx_name", "")
        ticker_row = ticker_by_market.get(coindcx_name, {})
        volume_24h = float(ticker_row.get("volume", 0) or 0)

        return CandidateScore(
            pair=pair,
            symbol=_pair_symbol(pair),
            volume_24h=volume_24h,
            spread_pct=spread_pct,
            atr_pct=atr_pct,
            last_price=last_price,
            composite_score=0.0,
        )
    except cdx.CoinDCXError as exc:
        log.warning("Screening failed for %s: %s", pair, exc)
        return None


def _rank(candidates: list) -> list:
    """
    Composite score: normalize volume (higher better), spread (lower
    better), and volatility (target ~0.15-0.8% ATR per 1m candle -- enough
    to scalp, not so much it's unstable). Coins outside a sane spread/ATR
    band are dropped before ranking.
    """
    usable = [
        c for c in candidates
        if c.spread_pct <= config.MAX_SPREAD_PCT * 3  # generous screen; final trade-time filter is stricter
        and 0.03 <= c.atr_pct <= config.MAX_ATR_PCT_FOR_SANE_VOL
        and c.volume_24h > 0
    ]
    if not usable:
        return []

    max_vol = max(c.volume_24h for c in usable)
    min_spread = min(c.spread_pct for c in usable) or 0.0001
    for c in usable:
        vol_norm = c.volume_24h / max_vol if max_vol else 0
        spread_norm = min_spread / c.spread_pct if c.spread_pct else 0
        # volatility "goodness": closer to a comfortable scalping band scores higher
        target = 0.35
        vol_atr_norm = max(0.0, 1 - abs(c.atr_pct - target) / target)
        c.composite_score = 0.5 * vol_norm + 0.3 * spread_norm + 0.2 * vol_atr_norm

    usable.sort(key=lambda c: c.composite_score, reverse=True)
    return usable


def select_top_pairs(n: int = None) -> list:
    """Returns a ranked list of CandidateScore, best first, length <= n."""
    n = n or config.TOP_N_COINS
    markets_details = cdx.get_markets_details()
    futures_pairs = cdx.list_active_usdt_futures_pairs(markets_details)
    markets_details_by_pair = {m["pair"]: m for m in futures_pairs}

    shortlist_pairs = [
        m["pair"] for m in futures_pairs
        if _pair_symbol(m["pair"]) in config.CANDIDATE_SHORTLIST
    ]
    if not shortlist_pairs:
        # Fallback: shortlist symbols didn't match any live futures pair
        # naming; screen whatever active USDT futures pairs exist instead.
        shortlist_pairs = [m["pair"] for m in futures_pairs][:40]

    ticker = cdx.get_ticker()
    ticker_by_market = {t["market"]: t for t in ticker}

    candidates = []
    for pair in shortlist_pairs:
        result = _screen_pair(pair, ticker_by_market, markets_details_by_pair)
        if result:
            candidates.append(result)

    ranked = _rank(candidates)
    top = ranked[:n]
    log.info("Selected top %d pairs: %s", len(top), [c.symbol for c in top])
    return top
