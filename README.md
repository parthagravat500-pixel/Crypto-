# CoinDCX Live Scalper (Signal-Only)

A disciplined, real-time crypto scalping *assistant* for CoinDCX USDT-M
futures. It watches the Top 5 most liquid/tradeable pairs on 1-minute
candles (confirmed by 5m/15m structure), scores each candidate LONG/SHORT
setup against eight independent factors, and only ever displays a signal
when enough of them genuinely agree. Otherwise it says **NO TRADE** — which
is the expected, healthy output most of the time.

**This tool never places, edits, or cancels a real order.** It only reads
CoinDCX's public market-data endpoints (no API key needed) and shows you
Entry / SL / TP1 / TP2 / confidence / reasoning so you can decide.

## What's here

```
config.py          all tunable thresholds in one place
coindcx_client.py   thin wrapper over CoinDCX's public REST endpoints
indicators.py       EMA/RSI/MACD/ATR/VWAP/ADX/Bollinger/swings, pure Python
signal_engine.py    the 8-factor scoring model -> LONG/SHORT/NO_TRADE
risk_manager.py     dynamic ATR + swing + S/R based SL/TP1/TP2
universe.py         picks the Top 5 pairs by volume/spread/volatility
lifecycle.py        tracks WAITING -> ACTIVE -> TP1_HIT -> TP_COMPLETE/SL_HIT/CANCELLED
storage.py          SQLite persistence + win/loss + adaptive-filter stats
engine.py           the main loop -- run this to generate signals
app.py              Flask + lightweight-charts dashboard (UI is inlined in this one file)
```

Everything is a loose file at the repo root -- **no subfolders anywhere.**
That's deliberate: GitHub's mobile web upload doesn't reliably preserve
nested folder structures (files silently land flattened into the repo
root, which breaks imports), so this project is built to survive that.

## Deploying on Render (no computer needed once it's live)

This is the setup for running it 24/7 and only ever touching it from a
phone browser. One Render **web service** runs both the engine and the
dashboard together in a single process (Render's free and Starter plans
don't include a separate background worker, so the engine's polling loop
runs as a background thread inside the same process as the Flask app --
see `RUN_ENGINE_INPROCESS` in `config.py` and the bottom of `app.py`).

1. **Push every file at the repo root into a GitHub repo** -- there are no
   folders to worry about, so on mobile: create a new repo on github.com,
   use "Add file -> Upload files", and select every file from the unzipped
   folder at once (multi-select in the file picker). If you already have a
   repo from a previous attempt, **delete any stray files in it first**
   (anything that isn't one of the files listed above -- old `dashboard/`
   remnants, a loose `index.html`/`app.js`/`style.css` at the root, etc.)
   so nothing conflicts with this version.
2. **On Render:** New -> Blueprint -> connect that repo. Render will read
   `render.yaml` and set everything up (build command, start command, the
   `RUN_ENGINE_INPROCESS=1` env var). If you'd rather configure by hand
   instead of using the Blueprint: New -> Web Service -> connect the repo,
   Build Command `pip install -r requirements.txt`, Start Command
   `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`,
   and add the env var `RUN_ENGINE_INPROCESS=1`.
3. **Keep `--workers 1`.** More than one gunicorn worker would start
   multiple independent copies of the engine loop, all writing signals to
   the same database at once.
4. Deploy. Render gives you a public URL
   (`https://<your-service-name>.onrender.com`) -- open that in Safari on
   your iPhone. Add it to your home screen (Share -> Add to Home Screen)
   for an app-like icon.

If a deploy fails, check the logs (your service page -> the failed deploy
-> logs) for the actual error rather than guessing -- `ModuleNotFoundError`
almost always means a file didn't actually make it into the GitHub repo
the way you expected.

**Two things to know about Render's free tier before you rely on this:**

- **It sleeps.** A free web service spins down after 15 minutes with no
  inbound HTTP traffic, which would kill the engine thread too. The usual
  fix is a free external "uptime pinger" (e.g. cron-job.org or
  UptimeRobot) hitting your Render URL every 5-10 minutes to keep it awake
  -- a single service pinged this way stays within the free tier's 750
  instance-hours/month. It's a real workaround, not a hack that breaks
  anything, but it does mean occasional short gaps around a restart.
- **The filesystem is ephemeral on free (and on Starter without an add-on
  disk).** `scalper_data.db` -- your entire signal history and win/loss
  stats -- gets wiped on every redeploy and on Render's periodic restarts.
  Given the spec's performance-logging and adaptive-filtering sections are
  meant to accumulate over time, this matters. If that history matters to
  you, upgrade to the **Starter plan ($7/mo at time of writing -- confirm
  current pricing in Render's dashboard)**, which doesn't sleep, and attach
  a small **persistent disk** (Render dashboard -> your service -> Disks),
  mounted at e.g. `/var/data`, then set the env vars
  `DB_PATH=/var/data/scalper_data.db` and
  `STATE_PATH=/var/data/live_state.json` so the database and latest
  snapshot survive restarts.

If you just want to see it work first before paying anything, free tier +
an uptime pinger is a completely reasonable way to try it out.

## Running it locally instead (optional)

Requires Python 3.10+.

```bash
cd coindcx_scalper
python3 -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt
```

## Run it

Two processes, in two terminals, from inside `coindcx_scalper/`:

```bash
# Terminal 1 -- the engine: polls CoinDCX, computes signals, writes to SQLite
python engine.py

# Terminal 2 -- the dashboard: reads that data, serves the UI
python app.py
```

Then open **http://127.0.0.1:8765** in your browser.

The two processes are decoupled on purpose: the engine can keep running
(e.g. in `tmux`/`screen`/a systemd service) whether or not you have the
dashboard open, and you can restart the dashboard without losing any
signal history (it's all in `scalper_data.db`).

## How it decides

For every monitored pair, on every poll:

1. **Hard filters first** (`signal_engine.check_no_trade_filters`): weak
   ADX, abnormally low volume, or abnormal ATR% immediately forces
   `NO TRADE` — no scoring happens at all.
2. **Two directions are scored independently** (LONG and SHORT), each out
   of 100, across: trend alignment (5m+15m), 1m market structure, volume
   confirmation, EMA9/20 setup, VWAP side, RSI momentum, MACD histogram,
   and candle/rejection shape. Weights match the spec (20/15/15/10/10/10/10/10).
3. The better-scoring direction must clear `MIN_CONFIDENCE_TO_DISPLAY`
   (default 70) or the result is `NO TRADE`.
4. **Anti-chasing check**: if price is already more than
   `MAX_EXTENSION_ATR_MULTIPLE` ATRs away from EMA20 in the trade
   direction, the engine refuses to chase and returns `NO TRADE` with
   `wait_for_pullback: true` instead.
5. If a direction survives all of that, `risk_manager.build_risk_plan`
   computes SL (beyond the last confirmed swing low/high plus an ATR
   buffer), TP1/TP2 (ATR-multiple, capped by the next real support/
   resistance level), and rejects the trade if reward:risk to TP1 is below
   `MIN_RR_TP1` or there isn't enough room before the next S/R level.
6. A genuinely new signal is written to SQLite as `WAITING`. Every poll
   after that, `lifecycle.py` checks live price against it and moves it
   through `ACTIVE` → (`TP1_HIT`, with SL optionally moved to breakeven) →
   `TP_COMPLETE` / `SL_HIT`, or `CANCELLED` if it never triggers within
   `SIGNAL_STALE_MINUTES`.

Nothing here is a fixed percentage SL/TP — it's always derived from ATR,
the actual swing structure, and nearby S/R, per the spec.

## Win/loss stats and adaptive filtering

Every stat on the dashboard (win rate, avg R:R, net theoretical return, and
the by-coin / by-direction / by-trend / by-ADX / by-RSI / by-volume
breakdowns) is computed live from `storage.py`'s SQLite rows as signals
actually resolve — nothing is hard-coded or simulated. A signal only
contributes to these stats once it reaches `TP1_HIT`, `TP_COMPLETE`, or
`SL_HIT`; a `WAITING` signal that goes stale and is `CANCELLED` is excluded
from win-rate math since it never actually triggered a real outcome.

No optimization loop reads these stats back into the live scoring model —
that would be look-ahead bias. They're for *you* to review periodically and
decide, by hand, whether to retune `config.py` (e.g. "my SHORT signals in
choppy conditions are losing money — raise `MIN_ADX_FOR_TREND` for shorts").

## Data source note

CoinDCX's documented public sockets are Socket.IO-based, and the exact
futures candlestick channel/event names aren't published in a form stable
enough to hang a signal engine on. So this engine polls the REST
`/market_data/candles` endpoint every `POLL_INTERVAL_SECONDS` (default 5s)
instead of holding a websocket open — frequent enough to catch every 1m
close without hammering the API. If you want to swap in a websocket feed
later, `coindcx_client.get_candles` / `engine.analyze_pair` are the two
places to change.

5m and 15m candles are not separately fetched — they're built by
resampling the fetched 1m candles (`indicators.resample_candles`), which
is how most exchange UIs build higher timeframes too.

## Extending to real order execution (disabled by default)

The architecture is deliberately modular so this can be added later:
`engine.py` already separates *decide* (`signal_engine.decide`) from *size
and place* (which doesn't exist yet). A future `order_executor.py` could
subscribe to newly-`WAITING` signals and place real orders through
CoinDCX's authenticated endpoints (`/exchange/v1/orders/create`, etc.) —
but that requires your API key/secret, real capital at risk, and should be
opt-in and reviewed carefully before ever being enabled. It is **not**
wired up in this codebase.

## Tuning

Everything that shapes signal frequency and quality lives in `config.py`:
`MIN_CONFIDENCE_TO_DISPLAY`, `MIN_ADX_FOR_TREND`, `MAX_SPREAD_PCT`,
`MIN_RR_TP1`, `MAX_EXTENSION_ATR_MULTIPLE`, the whole `SCORE_WEIGHTS` dict,
etc. Nothing else needs touching to adjust behavior.

## Limitations to know about

- This was built and unit-tested against synthetic data in a sandboxed
  environment with no outbound access to `api.coindcx.com` — the API
  client's request/response handling was verified against CoinDCX's
  published API docs, and every indicator/scoring/risk/lifecycle function
  was verified against hand-built and randomized test data, but the full
  pipeline has not yet been run against live CoinDCX data. Run
  `python engine.py` for a few minutes on your machine and watch the logs
  before trusting it unattended.
- Support/resistance and swing detection use a simple fixed-lookback
  fractal method (`SWING_LOOKBACK`, default 3 bars each side). It's
  intentionally simple and inspectable rather than a black box.
- The Top-5 universe is picked from a shortlist of generally-liquid coins
  (`config.CANDIDATE_SHORTLIST`) rather than screening every futures pair
  on CoinDCX every cycle, to keep API usage reasonable. Edit that list to
  widen the search.
