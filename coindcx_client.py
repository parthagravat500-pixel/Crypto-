"""CoinDCX public futures market-data client.

Uses the currently documented CoinDCX futures endpoints:
- active instruments: api.coindcx.com
- current prices: public.coindcx.com
- candlesticks: public.coindcx.com
- order book: public.coindcx.com

This module is read-only and never places orders.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

import config

log = logging.getLogger("coindcx_client")

API_BASE_URL = "https://api.coindcx.com"
PUBLIC_BASE_URL = "https://public.coindcx.com"

_session = requests.Session()
_session.headers.update({"User-Agent": "CoinDCX-Live-Scalper/2.0"})


class CoinDCXError(Exception):
    """Raised when a CoinDCX market-data request fails or is malformed."""


def _get_url(url: str, params: Optional[dict] = None):
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = _session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # network, HTTP, and decode errors are retried together
            last_exc = exc
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, config.MAX_RETRIES, exc)
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    raise CoinDCXError(f"GET {url} failed after {config.MAX_RETRIES} attempts: {last_exc}")


def get_active_futures_pairs() -> list[str]:
    data = _get_url(
        API_BASE_URL + "/exchange/v1/derivatives/futures/data/active_instruments",
        params={"margin_currency_short_name[]": "USDT"},
    )
    if not isinstance(data, list):
        raise CoinDCXError("Unexpected active futures instruments response shape")
    return [str(p) for p in data if isinstance(p, str) and p.startswith("B-") and p.endswith("_USDT")]


def get_current_futures_prices() -> dict:
    """Return CoinDCX futures RT price records keyed by pair.

    Common fields include:
      ls = last price, v = 24h volume, mp = mark price, pc = 24h % change.
    """
    data = _get_url(PUBLIC_BASE_URL + "/market_data/v3/current_prices/futures/rt")
    if not isinstance(data, dict) or not isinstance(data.get("prices"), dict):
        raise CoinDCXError("Unexpected futures current-prices response shape")
    return data["prices"]


_INTERVAL_TO_RESOLUTION = {
    "1m": ("1", 60),
    "5m": ("5", 5 * 60),
    "60m": ("60", 60 * 60),
    "1h": ("60", 60 * 60),
    "1d": ("1D", 24 * 60 * 60),
    "1D": ("1D", 24 * 60 * 60),
}


def get_candles(pair: str, interval: str = "1m", limit: int = 300) -> list:
    """Fetch futures OHLCV bars and return them oldest -> newest."""
    if interval not in _INTERVAL_TO_RESOLUTION:
        raise CoinDCXError(f"Unsupported candle interval: {interval}")
    resolution, seconds_per_bar = _INTERVAL_TO_RESOLUTION[interval]

    now_s = int(time.time()) + seconds_per_bar
    # Add a little margin so boundary bars do not cause us to receive one less candle.
    from_s = now_s - (max(1, int(limit)) + 5) * seconds_per_bar
    data = _get_url(
        PUBLIC_BASE_URL + "/market_data/candlesticks",
        params={
            "pair": pair,
            "from": from_s,
            "to": now_s,
            "resolution": resolution,
            "pcode": "f",
        },
    )
    if not isinstance(data, dict) or data.get("s") != "ok" or not isinstance(data.get("data"), list):
        raise CoinDCXError(f"Unexpected candlesticks response shape for {pair}")

    candles = []
    for raw in data["data"]:
        try:
            candles.append({
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float(raw.get("volume", 0) or 0),
                "time": int(raw["time"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c["time"])
    return candles[-limit:]


def get_orderbook(pair: str, depth: int = 10) -> dict:
    """Fetch the documented futures order-book snapshot.

    CoinDCX currently documents depths 10, 20, and 50 for this endpoint.
    """
    if depth <= 10:
        depth = 10
    elif depth <= 20:
        depth = 20
    else:
        depth = 50
    data = _get_url(f"{PUBLIC_BASE_URL}/market_data/v3/orderbook/{pair}-futures/{depth}")
    if not isinstance(data, dict) or not isinstance(data.get("bids"), dict) or not isinstance(data.get("asks"), dict):
        raise CoinDCXError(f"Unexpected orderbook response shape for {pair}")
    return data


def get_recent_trades(pair: str, limit: int = 50) -> list:
    # The documented endpoint does not expose a `limit` query parameter; slice locally.
    data = _get_url(
        API_BASE_URL + "/exchange/v1/derivatives/futures/data/trades",
        params={"pair": pair},
    )
    if not isinstance(data, list):
        raise CoinDCXError(f"Unexpected trade history response shape for {pair}")
    return data[-limit:]


def best_bid_ask(pair: str):
    book = get_orderbook(pair, depth=10)
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    if not bids or not asks:
        return None, None
    try:
        best_bid = max(float(p) for p in bids.keys())
        best_ask = min(float(p) for p in asks.keys())
    except (TypeError, ValueError):
        return None, None
    return best_bid, best_ask
