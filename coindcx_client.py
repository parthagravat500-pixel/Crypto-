"""
Thin wrapper around CoinDCX's public (unauthenticated) REST endpoints.

Every endpoint used here is documented at https://docs.coindcx.com/ under
"Public endpoints". No API key or secret is required or accepted -- this
client can only read market data, never place, edit, or cancel orders.

Endpoints used:
    GET /exchange/ticker                -> 24h ticker (per market)
    GET /exchange/v1/markets_details    -> pair metadata (incl. futures pairs)
    GET /market_data/candles            -> OHLCV candles (spot & futures pairs)
    GET /market_data/orderbook          -> live order book (for spread)
    GET /market_data/trade_history      -> recent trades
"""

from __future__ import annotations

import time
import logging
from typing import Optional

import requests

import config

log = logging.getLogger("coindcx_client")


class CoinDCXError(Exception):
    """Raised when the CoinDCX API can't be reached or returns bad data."""


def _get(path: str, params: Optional[dict] = None):
    url = config.BASE_URL + path
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything
            last_exc = exc
            log.warning(
                "GET %s failed (attempt %d/%d): %s",
                path, attempt, config.MAX_RETRIES, exc,
            )
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    raise CoinDCXError(f"GET {path} failed after {config.MAX_RETRIES} attempts: {last_exc}")


def get_markets_details() -> list:
    """Full metadata for every market, spot and futures."""
    data = _get("/exchange/v1/markets_details")
    if not isinstance(data, list):
        raise CoinDCXError("Unexpected markets_details response shape")
    return data


def get_ticker() -> list:
    """24h ticker snapshot for every market (spot-style fields)."""
    data = _get("/exchange/ticker")
    if not isinstance(data, list):
        raise CoinDCXError("Unexpected ticker response shape")
    return data


def get_candles(pair: str, interval: str = "1m", limit: int = 300) -> list:
    """
    OHLCV candles for a pair. Returned newest-first by CoinDCX; we flip to
    oldest-first here since that's what every indicator in this project
    expects.

    Each candle dict has keys: open, high, low, close, volume, time (ms).
    """
    data = _get(
        "/market_data/candles",
        params={"pair": pair, "interval": interval, "limit": limit},
    )
    if not isinstance(data, list):
        raise CoinDCXError(f"Unexpected candles response shape for {pair}")
    candles = list(reversed(data))  # oldest -> newest
    return candles


def get_orderbook(pair: str, depth: int = 5) -> dict:
    """Live order book. depth must be one of [1,5,10,20,50,100,200]."""
    data = _get("/market_data/orderbook", params={"pair": pair, "depth": depth})
    if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
        raise CoinDCXError(f"Unexpected orderbook response shape for {pair}")
    return data


def get_recent_trades(pair: str, limit: int = 50) -> list:
    data = _get("/market_data/trade_history", params={"pair": pair, "limit": limit})
    if not isinstance(data, list):
        raise CoinDCXError(f"Unexpected trade_history response shape for {pair}")
    return data


def best_bid_ask(pair: str):
    """Returns (best_bid, best_ask) as floats, or (None, None) on empty book."""
    book = get_orderbook(pair, depth=5)
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    if not bids or not asks:
        return None, None
    best_bid = max(float(p) for p in bids.keys())
    best_ask = min(float(p) for p in asks.keys())
    return best_bid, best_ask


def list_active_usdt_futures_pairs(markets_details: list) -> list:
    """
    Filter markets_details down to active USDT-margined futures pairs, i.e.
    pair starts with the futures prefix ("B-") and ends with "_USDT", and
    status == "active".
    """
    out = []
    for m in markets_details:
        pair = m.get("pair", "")
        if (
            pair.startswith(config.FUTURES_PAIR_PREFIX)
            and pair.endswith(config.FUTURES_QUOTE_SUFFIX)
            and m.get("status") == "active"
        ):
            out.append(m)
    return out
