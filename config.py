"""
Central configuration for the CoinDCX Scalping Signal Engine.

Nothing here needs an API key: everything the engine reads is from
CoinDCX's public, unauthenticated market-data endpoints. This is a
SIGNAL-ONLY system -- it never places, edits, or cancels an order.
"""

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
BASE_URL = "https://api.coindcx.com"
REQUEST_TIMEOUT = 6          # seconds, per HTTP call
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

# ---------------------------------------------------------------------------
# Universe selection
# ---------------------------------------------------------------------------
# We only look at USDT-margined futures pairs, identified in CoinDCX's
# markets_details response by their `pair` field starting with "B-" and
# ending in "_USDT" (e.g. "B-BTC_USDT"). This is confirmed by CoinDCX's own
# API docs, which use "B-BTC_USDT" as the example pair for /market_data/candles.
FUTURES_PAIR_PREFIX = "B-"
FUTURES_QUOTE_SUFFIX = "_USDT"

TOP_N_COINS = 5
UNIVERSE_RESCAN_SECONDS = 15 * 60     # recompute Top 5 every 15 minutes

# How many candidate pairs to screen when picking the Top 5. Screening every
# single futures pair on every rescan is expensive, so we screen a shortlist
# of generally-liquid names first. This list is just a starting shortlist --
# it does NOT hardcode the final Top 5, which is always computed from live
# volume/spread/volatility data.
CANDIDATE_SHORTLIST = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX",
    "LTC", "LINK", "MATIC", "DOT", "TRX", "SHIB", "NEAR", "BCH",
    "ETC", "APT", "ARB", "OP", "SUI", "INJ", "FIL", "ATOM",
]

# ---------------------------------------------------------------------------
# Candle fetch settings
# ---------------------------------------------------------------------------
CANDLE_INTERVAL_EXEC = "1m"
CANDLE_INTERVAL_CONFIRM_1 = "5m"     # not a native CoinDCX interval -> built by resampling 1m
CANDLE_INTERVAL_CONFIRM_2 = "15m"    # built by resampling 1m
CANDLES_FETCH_LIMIT = 300            # 1m candles fetched per poll (5 hours of history)

POLL_INTERVAL_SECONDS = 5            # how often we re-poll candles for monitored pairs
# CoinDCX's documented public sockets are Socket.IO based; the exact futures
# candlestick channel/event names are not published in a stable enough form
# to hardcode safely here, so this engine polls the REST candles endpoint
# instead of holding a websocket open. The polling interval above is short
# enough to catch every 1m candle close without hammering the API. See
# data_feed.py for a plug-in point if you want to swap in a websocket feed.

# ---------------------------------------------------------------------------
# Indicator periods
# ---------------------------------------------------------------------------
EMA_PERIODS = (9, 20, 50, 200)
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD = 14
VOLUME_SMA_PERIOD = 20
ADX_PERIOD = 14
BOLLINGER_PERIOD, BOLLINGER_STD = 20, 2
SWING_LOOKBACK = 3            # bars on each side to confirm a swing high/low

# ---------------------------------------------------------------------------
# No-trade / quality filters
# ---------------------------------------------------------------------------
MIN_ADX_FOR_TREND = 18         # below this, market considered too choppy
MAX_SPREAD_PCT = 0.15          # spread wider than this % of price -> skip
MIN_VOLUME_SMA_RATIO = 0.4     # current volume vs its own SMA; below -> "unusually low volume"
MAX_ATR_PCT_FOR_SANE_VOL = 4.0 # ATR as % of price above this -> "abnormal volatility"
MIN_RR_TP1 = 1.2               # minimum acceptable reward:risk to TP1

# Anti-chasing: if price has moved more than this multiple of ATR away from
# the EMA20 / breakout level in the direction of the prospective trade, we
# refuse to chase and emit WAIT FOR PULLBACK instead.
MAX_EXTENSION_ATR_MULTIPLE = 1.8

# ---------------------------------------------------------------------------
# Scoring model (must sum to 100)
# ---------------------------------------------------------------------------
SCORE_WEIGHTS = {
    "trend_alignment": 20,
    "market_structure": 15,
    "volume_confirmation": 15,
    "ema_setup": 10,
    "vwap": 10,
    "rsi": 10,
    "macd": 10,
    "candle_pattern": 10,
}
assert sum(SCORE_WEIGHTS.values()) == 100

MIN_CONFIDENCE_TO_DISPLAY = 70

# ---------------------------------------------------------------------------
# Stop loss / take profit
# ---------------------------------------------------------------------------
ATR_SL_BUFFER_MULTIPLE = 0.25   # extra buffer beyond swing low/high, in ATRs
TP1_R_MULTIPLE = 1.5
TP2_R_MULTIPLE = 2.0
MIN_ROOM_TO_SR_ATR_MULTIPLE = 0.8  # need at least this many ATRs of room to next S/R

# ---------------------------------------------------------------------------
# Signal lifecycle
# ---------------------------------------------------------------------------
BREAKEVEN_AFTER_TP1 = True     # move virtual SL to entry once TP1 hits, if structure supports it
SIGNAL_STALE_MINUTES = 20      # a WAITING signal not triggered within this window is CANCELLED
ENTRY_TRIGGER_TOLERANCE_PCT = 0.05  # price within this % of suggested entry counts as "triggered"

# ---------------------------------------------------------------------------
# Storage / dashboard
#
# All three are overridable by environment variable so the same code runs
# unchanged locally (two terminals) or as a single Render web service. On
# Render, PORT is injected automatically. DB_PATH and STATE_PATH should
# point at a mounted persistent disk (e.g. "/var/data/scalper_data.db") if
# you add one -- otherwise both reset on every redeploy/restart, since
# Render's default filesystem is ephemeral.
# ---------------------------------------------------------------------------
import os as _os

DB_PATH = _os.environ.get("DB_PATH", "scalper_data.db")
STATE_PATH = _os.environ.get(
    "STATE_PATH", _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "live_state.json")
)
DASHBOARD_HOST = _os.environ.get("HOST", "0.0.0.0")
DASHBOARD_PORT = int(_os.environ.get("PORT", 8765))

# Set to "1" to run the engine's polling loop as a background thread inside
# the dashboard's own process (used on Render, where free/starter plans
# don't support a separate background worker). Leave at "0" (default) for
# local two-terminal use: `python engine.py` + `python dashboard/app.py`.
RUN_ENGINE_INPROCESS = _os.environ.get("RUN_ENGINE_INPROCESS", "0") == "1"
