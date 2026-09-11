# FYERS Market Making Bot — 7-Engine Low-Latency Architecture

A transparent, high-performance market-making bot for NSE Equities and MCX Commodities on the **FYERS API v3**, rebuilt as a decoupled 7-engine pipeline communicating over Redis pub/sub with `msgspec` and persisted into PostgreSQL.

```
 Market data ─▶ data_engine ─▶ Redis bus ─▶ signal_engine ─▶ cost_engine
                                                │                 │
                                                ▼                 ▼
                                        strategy_engine ◀── decision
                                                │
                                        execution_engine ─▶ FYERS orders
                                                │
                                        risk_engine (filters every quote/fill)
                                                │
                                        monitor_engine (heartbeats + /api + /ws)
```

## Architecture

Engines live in `app/engines/`, the composition root is `app/container.py`, shared messages are defined in `app/schema.py` as `msgspec.Struct`s, and inter-engine transport is `app/infra/redis.py` (TTL cache + pub/sub bus).

> **New to the codebase?** See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for a plain-language walkthrough of the full flow, what every file does, and the key Fyers broker quirks.
>
> **ML layer?** See [`docs/ML.md`](docs/ML.md) for the functional (no-class) adverse-selection model, the 27-dim order-book feature vector, the ONNX inference path (~20µs), and the offline training pipeline.

| Engine         | Responsibility                                                         | Subscribes        |
|----------------|------------------------------------------------------------------------|-------------------|
| `data`         | FYERS data WebSocket → `MarketTick` → snapshot per symbol              | — (publisher)     |
| `signal`       | Volatility widener + liquidity grade → `SignalMetrics` per symbol      | `fyers:market:*`  |
| `cost`         | Statutory fees, breakeven & required-spread ticks → `CostQuote`        | `fyers:market:*`  |
| `risk`         | Margin check, inventory cap, daily-loss kill-switch on every order/fill| commands/orders   |
| `execution`    | Places/cancels FYERS limit orders, tracks `OrderEvent`s                | `fyers:market:*`  |
| `strategy`     | Ranked scan → `MarketMakerStrategy` (bid/ask from signal + cost), top-N gate | `fyers:market:*`  |
| `monitor`      | Heartbeats, risk status, snapshot aggregation, `/api` + `/ws`          | `fyers:command:*` |

All timestamps written to PostgreSQL (`db/schema.sql`, `TIMESTAMPTZ` columns) go through `Database._ts()`. The FYERS access token is cached in Redis under a TTL key (`TokenStore`, `app/infra/auth.py`) — the data/order sockets read `token_store.client_id`.

## Asset / Segment Support (`app/infra/instrument.py`)

The platform is *asset friendly*: every strategy trades one instrument resolved through a per-instrument metadata model (`Symbol → Segment → AssetType`). Lot size, tick size, tick value and margin-per-lot are no longer global — they come from the instrument.

| `ASSET_TYPE`          | Market | Unit          | Position sizing                          |
|-----------------------|--------|---------------|------------------------------------------|
| `COMMODITY` (default) | MCX futures | lots | **Fixed 1 lot**, always margin-checked |
| `EQUITY_FUT` / `FUT`  | NSE index/stock futures | lots | **Dynamic** from margin + inventory + mid |
| `EQUITY`              | NSE cash equity | shares | **Dynamic** from margin + inventory + mid |

Dynamic size = `floor(margin_available × MARGIN_RISK_FRACTION ÷ margin_per_lot)`, clamped to `[1, MAX_POSITION_QTY − |inventory|]`, then **reduced by volatility**: each extra widening tick shrinks the size by `VOL_SIZE_REDUCTION_PER_TICK` (default 10%) so the book stays flat in wild moves. Until the broker returns a real margin figure, margin-per-lot is estimated from the notional at mid (`mid × lot_size × 0.10`) and the pre-trade margin gate still vetoes any order that is not covered.

Quantity semantics follow the market: commodity & equity futures quote in lots (the broker resolves the contract multiplier from the symbol), cash equity quotes raw shares (`lot = 1`). PnL, position reconciliation and `flatten_all` all divide by the instrument's lot size.

## Market-Making Scanner (`app/scanner.py`)

The strategy engine ranks the whole traded universe and lets only the top-N assets rest quotes:

- `StrategyEngine` recomputes a **ranked `ScannerRow`** per instrument every `SCANNER_REFRESH_SEC` (1 s default). Each row carries LTP, bid, ask, mid, spread (ticks), liquidity grade, churn, volatility widening, margin available/per-lot and the **dynamic quote size**.
- The composite `score` starts at 1.0 and is penalized for a stale feed, wide/non-existent spread, thin book, or heavy volatility; rank 1 = most attractive.
- Only `rank ≤ MAX_SCANNER_ACTIVE` (default 2) rows may rest quotes (`quoteable`); a strategy that drops out of the active set cancels its resting pair and stands down (`HOLD_RANK`).
- **Ranking is checked after charges.** Every refresh the scanner runs the post-statutory-cost economics at the live quote size: `net_profit_rs` (net/cycle if filled at the current book), `round_trip_charges_rs`, `breakeven_spread_ticks` and `required_spread_ticks` (breakeven + `MIN_PROFIT_MARGIN_TICKS`). A row whose net profit is below `MIN_NET_PROFIT_PER_CYCLE_RS` (default ₹25) is marked `profitable=false`, de-ranked and blocked from quoting - the same threshold the `MarketMakerStrategy` enforces per decision, so "ranked" always means "worth it after brokerage/charges".
- The scanner output ships in the pipeline snapshot (`PipelineSnapshot.scanner`, trimmed to the top-25) and via `GET /api/scanner?segment=&top=`, and `GET /api/asset/{symbol}` returns the full ranked-asset detail (order book depth, per-asset PnL, spread collected, strategy activity). The `ScannerPanel` lists ranks **ascending** (rank 1 first) with a per-segment tab and click-through detail.

## Whole-Market Takeover (`app/infra/master_contracts.py`)

With no explicit `UNIVERSE`, the bot builds its universe from the FYERS symbol masters (`public.fyers.in/sym_details/*_sym_master.json`, cached to `~/.fyers/sym_master`, refreshed daily):

- `NSE_ALL_EQUITY_SCAN=1` (default) — every live `NSE:*‑EQ` cash equity.
- `MCX_ALL_FUTURES_SCAN=1` (default) — every live MCX commodity future.
- `MCX_NEAR_CONTRACT_ONLY=1` (default) — MCX is narrowed to the **nearest expiring contract per underlying** (e.g. only `NATURALGAS26SEPFUT` of the whole NG calendar). Once a near contract expires, the next boot automatically rolls over to the new near contract. Lot sizes are taken from the master's `qtyMultiplier` (FYERS publishes `minLotSize=1` for MCX).

A strategy is created per instrument, all of them are subscribed on the data socket (the SDK chunks the subscription internally) and the scanner ranks the combined set once live ticks arrive. Explicit `UNIVERSE` entries are always merged in (explicit wins on conflicts). If a master download fails at boot the bot logs a warning and falls back to the configured universe.

Quoting is **margin-aware**: commodity-futures size is capped by `margin_available × margin_risk_fraction / margin_per_lot` and the strategy stands down (`HOLD_MARGIN`) when even one lot is not affordable. The API port is reserved **before** any engine boots, so a second `python3 run.py` aborts cleanly instead of starting engines and flattening the live book.

### Global multi-asset margin budget (`MARGIN_LEDGER`)

The scanner's greedy top-N selection is the **cross-asset margin budget**: it walks the rank-ordered rows and keeps a name quoteable only while the margin it consumes still fits inside the account. Two knobs make that budget money-safe:

- `MARGIN_RESERVE_BOTH_SIDES=1` (default) — a standing two-sided pair can fill **both** legs, so each selected name reserves `2 × margin_req_rs`.
- `MIN_FREE_MARGIN_BUFFER_RS=1000` — a free-cash buffer is charged into the budget too, so the top-N book never corners the account.

The selected symbols' reservations are pushed into a **margin ledger** in the risk engine (`sync_margin_bookings`). Every symbol then sizes and gates against `margin_remaining(symbol)` = broker avail − **other** symbols' bookings − buffer:

- `suggest_quote_qty` sizes from the aggregated remaining margin, so a quote on one symbol genuinely shrinks what every other symbol can quote.
- The pre-trade gate adds a `margin_budget` check that vetoes any order exceeding its remaining share.
- A fill releases the filled symbol's reservation (the broker now reports it as a real position); `RESET` clears the whole ledger.
- When the broker has not yet reported a margin figure (`margin_avail <= 0`) the budget is **lenient** — top-N is capped by count only and the per-order broker margin API remains the backstop.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt         # fyers-apiv3, psycopg, psycopg_pool, redis, msgspec, fastapi, uvicorn
```

Postgres + Redis are required. The canonical development database is the
system PostgreSQL 16 on `localhost:5432`:

```bash
PGPASSWORD=PixelIn psql -h localhost -p 5432 -U postgres -c "CREATE DATABASE pixel_in;"
PGPASSWORD=PixelIn psql -h localhost -p 5432 -U postgres -d pixel_in -f db/schema.sql
```

Postgres is optional at runtime: when `DATABASE_URL` is empty/unset or the
server is down, `Database` becomes a no-op and every engine keeps running
without persistence.

Configure via env (defaults in `app/config.py`):
```bash
export FYERS_CLIENT_ID="your_app_id-XXX"
export FYERS_SECRET_KEY="your_secret_key"
export FYERS_REDIRECT_URI="http://localhost:2000/callback"
export DATABASE_URL="postgresql://postgres:PixelIn@localhost:5432/pixel_in"
export REDIS_URL="redis://localhost:6379/0"

# segment / instrument (defaults to MCX natural gas commodity futures):
export SEGMENT="COMMODITY"                      # COMMODITY | EQUITY | EQUITY_FUT
export ASSET_TYPE="COMMODITY"                   # policy driver (see table above)
export SYMBOL="MCX:NATURALGAS26SEPFUT"          # FYERS symbol
export LOT_SIZE="1250"                          # 0 = auto-resolve from symbol
export TICK_SIZE="0.1"                          # 0 = auto-resolve from segment
export MARGIN_PER_LOT_RS="0"                    # broker margin for 1 lot (0 = learn from API)
export QUOTE_QTY="1"                            # lots quoted per side (commodity: fixed)
export MAX_POSITION_QTY="1"                     # hard cap on net inventory (units)

# dynamic sizing (equity / equity-futures only):
export MARGIN_RISK_FRACTION="0.25"              # fraction of free margin used
export VOL_SIZE_REDUCTION_PER_TICK="0.10"       # size shrink per extra vol tick

# multi-asset scanner (optional — empty = trade only SYMBOL):
# comma list of  symbol:segment:lot_size:tick_size  (segment token splits the
# FYERS symbol, which itself contains a colon, e.g. NSE:TCS-EQ:EQUITY:1:0.05)
export UNIVERSE="NSE:NIFTY50:EQUITY:1:0.05,NSE:TCS-EQ:EQUITY:1:0.05,NFO:CRUDEOIL26SEPFUT:COMMODITY:1:0.10"
export MAX_SCANNER_ACTIVE="2"                   # top-N assets may rest quotes
export SCANNER_REFRESH_SEC="1.0"                # re-rank cadence
export SCANNER_MIN_LIQUIDITY="0.05"             # min book depth ratio (0..1)
export MIN_NET_PROFIT_PER_CYCLE_RS="25"         # post-charges floor for a quote
                                                # (scanner profitability gate too)
# whole-market scan (default ON — everything live on NSE + MCX, no UNIVERSE):
export NSE_ALL_EQUITY_SCAN="1"                  # every live NSE cash equity
export MCX_ALL_FUTURES_SCAN="1"                 # every live MCX commodity future

# global multi-asset margin ledger (default ON):
export MARGIN_LEDGER="1"                        # cross-symbol margin budget
export MARGIN_RESERVE_BOTH_SIDES="1"            # reserve margin for BOTH quote legs
export MIN_FREE_MARGIN_BUFFER_RS="1000"         # free-cash cushion in the budget

# low-latency / scalability tuning:
export BUS_HANDLER_QUEUE="1024"                 # bounded FIFO per bus subscription
                                                # (slow consumer drops-newest w/ warn)
export POSITIONS_POLL_INTERVAL_SEC="15.0"       # /positions broker reconcile cadence
export ENGINE_HEARTBEAT_SEC="1.0"               # monitor heartbeat cadence
export DB_PENDING_CAP="200000"                  # flush-buffer cap before drop+warn
export DB_FLUSH_BATCH="200"                     # rows per flush batch
export DB_FLUSH_INTERVAL_SEC="1.0"              # flush cadence

# quoting / adverse-selection protection:
export OBI_GATE="1"                             # order-book-imbalance gate
export OBI_GATE_RATIO="3.0"                     # suppress the "hot" side at >= ratio
export VOLATILITY_HALT_QUOTING_TICKSPERSEC="0.0"# stop quoting above this churn (0 = off)
export INVENTORY_SKEW_QUOTING="1"               # skew the pair toward flattening
export INVENTORY_SKEW_TICKS_PER_LOT="1"         # skew intensity per inventory lot
export HALT_COOLDOWN_SEC="900.0"                # auto-recover a loss-halt after N s

# logging (app/infra/logging.py):
export LOG_LEVEL="INFO"                         # DEBUG | INFO | WARN | ERROR
export LOG_QUIET="0"                            # 1 = keep only trades/risk/warn/error
export LOG_FILE="/path/to/engine.log"           # rotating plain log (50MB x 5)
export LOG_FILE_JSON="0"                        # 1 = NDJSON lines into LOG_FILE

# ML data capture + inference (see docs/ML.md):
export ML_CAPTURE_ENABLED="1"                   # persist depth+features (default ON)
export ML_ENABLED="0"                           # 1 = also run model + widen book
export ML_MODEL_NAME="adverse_selection_v1"     # registry name for the deployed model
export ML_MODEL_PATH="models/adverse_selection_v1.onnx"   # leave unset -> auto-load registry
export ML_WIDEN_THRESHOLD="0.65"                # P(adverse) above this => widen
export ML_WIDEN_EXTRA_TICKS="1"                 # extra widening ticks when triggered
export ML_DEPTH_PERSIST_INTERVAL_SEC="1.0"      # order_book_depths cadence per symbol
export ML_FEATURE_PERSIST_INTERVAL_SEC="0.5"    # ml_features cadence per symbol
```

## Running

```bash
# daily OAuth (tokens expire at end of trading day, per FYERS v3 docs):
# auto flow — opens the FYERS login in the browser, captures the auth_code from
# the redirect, exchanges + validates + caches it (no copy/paste needed):
python3 scripts/fyers_login.py
python3 scripts/fyers_login.py --check      # verify cached token is still live

# manual alternatives:
python3 scripts/fyers_login.py --url        # print the browser login URL only
python3 scripts/fyers_login.py --auth-code <code>   # exchange a pasted auth_code

# boot all 7 engines + FastAPI monitor gateway (REST + WebSocket)
python3 run.py
```

The strategy universe resolves its instrument from the same env: `SEGMENT` /
`ASSET_TYPE`, `SYMBOL`, `LOT_SIZE`, `TICK_SIZE` and `MARGIN_PER_LOT_RS` are picked
up at boot and drive the position size. Examples for each segment:

```bash
# Commodity future — fixed 1 lot, always margin-checked (current default)
ASSET_TYPE=COMMODITY SYMBOL=MCX:NATURALGAS26SEPFUT python3 run.py

# Equity future — dynamic lots from margin + inventory + mid
ASSET_TYPE=EQUITY_FUT SYMBOL=NSE:NIFTY26SEPFUT LOT_SIZE=75 TICK_SIZE=0.05 \
  MARGIN_RISK_FRACTION=0.25 MAX_POSITION_QTY=5 python3 run.py

# Cash equity — dynamic shares from margin + inventory + mid
ASSET_TYPE=EQUITY SYMBOL=NSE:RELIANCE-EQ LOT_SIZE=1 \
  MARGIN_RISK_FRACTION=0.25 MAX_POSITION_QTY=20 python3 run.py

# Multi-asset — one market-making strategy per universe entry, scanner ranks
# all of them and only the top-2 may quote
UNIVERSE="NSE:NIFTY50:EQUITY:1:0.05,NSE:TCS-EQ:EQUITY:1:0.05,NFO:CRUDEOIL26SEPFUT:COMMODITY:1:0.10,NFO:NIFTY26SEPFUT:EQUITY_FUT:75:0.05" \
  MAX_SCANNER_ACTIVE=2 python3 run.py

# Whole-market takeover — same scanner, entire NSE equity + MCX futures universe
NSE_ALL_EQUITY_SCAN=1 MCX_ALL_FUTURES_SCAN=1 MAX_SCANNER_ACTIVE=2 python3 run.py
```

The login helper runs a tiny listener on `FYERS_REDIRECT_URI` (`localhost:2000`)
only while it waits for the redirect; if no token is needed it exits immediately.
It caches the token both to `fyers_access_token.txt` (legacy) and to Redis at
`fyers:token:<app_id>` (the `TokenStore` the engines read). If a server boots
with no valid cached token it refuses to start rather than hanging on an
interactive prompt; seed the token first with the script above.

### API surface (`app/api.py`)

- `GET /api/health`, `/api/pipeline` (full `PipelineSnapshot`), `/api/strategies`, `/api/decisions`, `/api/markets`, `/api/scanner?segment=&top=`, `/api/asset/{symbol}`, `/api/risk`, `/api/orders`, `/api/db/top-tables`
- `POST /api/commands/{PAUSE_STRATEGY|RESUME_STRATEGY|FLATTEN|HALT_ALL|RESET}?target=*`
- `WS /ws/live` — 1 Hz `PipelineSnapshot` frames (JSON text)
- `WS /ws/trades` — streamed `OrderEvent` frames (JSON text)

## Frontend (`frontend/`)

Vite + React + Tailwind CSS + motion.dev terminal-style monitor with an **electric-blue theme and a left navigation rail**. The Scanner view shows the whole-market rank (ascending, NSE Equity / MCX Futures tabs: rank, LTP, bid, ask, spread, **net ₹/cycle after charges**, charges ₹, breakeven ticks, liquidity, size, score, status); clicking a rank opens the per-asset detail panel — live order book ladder, PnL (spread collected = realized net), after-charges economics and strategy activity.

```bash
cd frontend && npm install && npm run dev
```

Vite proxies `/api` and `/ws` to the FastAPI gateway on `localhost:8000`. `npm run build && npm run lint` produce checks.

## Smoke Test (no live FYERS required)

`scripts/smoke_test.py` boots all 7 engines against **real Redis + real PostgreSQL** with the FYERS sockets stubbed, injects 30 synthetic ticks, verifies signals → cost → risk → strategy quotes → fake fill, and asserts the pipeline snapshot:

```bash
FYERS_CLIENT_ID=smoketest DATABASE_URL=postgresql://postgres:PixelIn@localhost:5432/pixel_in \
  REDIS_URL=redis://localhost:6379/1 python3 scripts/smoke_test.py
```

Note: Redis pub/sub has no retention — the test waits for all engine subscriptions before injecting ticks.

## Legacy files

The standalone legacy prototypes (`bot.py`, `main.py`, `config.py`, `margin.py`, `auth.py`, `cost_model.py`, `risk_manager.py`, `logger.py`) have been moved to `legacy/`. They are NOT used by the live bot (`run.py` → `app/`); they are kept for historical reference.

## Risk Controls (in `app/engines/risk_engine.py`)

- Realized PnL is booked only when a position closes (`on_fill`), using average entry vs exit price — opening a position realizes nothing.
- Inventory cap (`MAX_POSITION_QTY`) and daily-loss kill-switch (`MAX_DAILY_LOSS_RS`) halt trading via `fyers:command:*`. Halts auto-recover after `HALT_COOLDOWN_SEC` (default 900s) — the position stays flat and the market-hours / reject-circuit gates still apply after recovery.
- Pre-trade margin sufficiency via FYERS `multiorder/margin`, buffered by `MIN_FREE_MARGIN_BUFFER_RS`.
- **Global multi-asset margin ledger**: the scanner's greedy budget reserves every selected name's quote margin (×`MARGIN_RESERVE_BOTH_SIDES`, + buffer); the risk engine serves `margin_remaining(symbol)` to per-symbol sizing and vetoes any order beyond its remaining share (`margin_budget` pre-trade check). See *Global multi-asset margin budget* above.
- **Idempotent quoting** (`app/strategies/market_maker.py`): every quote/cancel/replace is keyed off the live order feed (`order_live_and_unfilled`). A symbol with an unseen/unconfirmed order stands down for the cycle instead of re-submitting, and re-pricing uses modify-first (`replace_order`) so the spread is never left wide.
- **Order-book-imbalance gate** (`OBI_GATE`): when one side of the book is `OBI_GATE_RATIO`× heavier, quoting into the "hot" side is suppressed (adverse-selection protection). Volatility can also stop quoting entirely above `VOLATILITY_HALT_QUOTING_TICKSPERSEC` churn.
- **Position sizing** lives in `app/sizing.py`: commodity futures are pinned to the configured quote size (1 lot), equity / equity futures derive their size from `margin_remaining × MARGIN_RISK_FRACTION ÷ margin_per_lot` bounded by `MAX_POSITION_QTY − |inventory|` and shrunk by volatility (`VOL_SIZE_REDUCTION_PER_TICK` per widening tick). The risk engine learns `margin_available` / `margin_per_lot` per symbol from each margin API response and feeds the strategy via `suggest_quote_qty(symbol, mid, vol_widening_ticks)`, then applies inventory skew (`INVENTORY_SKEW_QUOTING`) to pull the book toward flattening instead of dumping the adding side.
- **Account state is per symbol**: inventories, realized PnL, entry prices and position age are tracked independently per symbol, and broker positions are reconciled per symbol every `POSITIONS_POLL_INTERVAL_SEC` (default 15s — the order WebSocket stays the real-time source) so a multi-asset universe never mixes books.

## Bus throttling (low latency)

The Redis bus (`app/infra/redis.py`) gives **every subscription a bounded FIFO queue and its own worker**. The pump only enqueues — it never awaits a handler, so a slow consumer can't stall the feed. When a handler falls behind it drops the *newest* events (throttled warning) instead of stalling upstream; per-subscription ordering is preserved and subscriptions run concurrently.

## Unit tests

Stdlib `unittest`, no live infra required — pure money-math coverage (cost engine, scanner scoring/rank/margin budget, sizing, market-hours DST boundaries, signal churn/halting, risk margin ledger):

```bash
.venv/bin/python -m unittest discover -s tests -v
```