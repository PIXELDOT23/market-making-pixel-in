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

| Engine         | Responsibility                                                         | Subscribes        |
|----------------|------------------------------------------------------------------------|-------------------|
| `data`         | FYERS data WebSocket → `MarketTick` → snapshot per symbol              | — (publisher)     |
| `signal`       | Volatility widener + liquidity grade → `SignalMetrics` per symbol      | `fyers:market:*`  |
| `cost`         | Statutory fees, breakeven & required-spread ticks → `CostQuote`        | `fyers:market:*`  |
| `risk`         | Margin check, inventory cap, daily-loss kill-switch on every order/fill| commands/orders   |
| `execution`    | Places/cancels FYERS limit orders, tracks `OrderEvent`s                | `fyers:market:*`  |
| `strategy`     | `MarketMakerStrategy` decides bid/ask pair from signal + cost          | `fyers:market:*`  |
| `monitor`      | Heartbeats, risk status, snapshot aggregation, `/api` + `/ws`          | `fyers:command:*` |

All timestamps written to PostgreSQL (`db/schema.sql`, `TIMESTAMPTZ` columns) go through `Database._ts()`. The FYERS access token is cached in Redis under a TTL key (`TokenStore`, `app/infra/auth.py`) — the data/order sockets read `token_store.client_id`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt         # fyers-apiv3, psycopg, psycopg_pool, redis, msgspec, fastapi, uvicorn
```

Postgres + Redis are required:
```bash
createdb market_making
psql -d market_making -f db/schema.sql
```

Configure via env (defaults in `app/config.py`):
```bash
export FYERS_CLIENT_ID="your_app_id-XXX"
export FYERS_SECRET_KEY="your_secret_key"
export FYERS_REDIRECT_URI="http://localhost:2000/callback"
export DATABASE_URL="postgresql://user@localhost:5432/market_making"
export REDIS_URL="redis://localhost:6379/0"
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

The login helper runs a tiny listener on `FYERS_REDIRECT_URI` (`localhost:2000`)
only while it waits for the redirect; if no token is needed it exits immediately.
It caches the token both to `fyers_access_token.txt` (legacy) and to Redis at
`fyers:token:<app_id>` (the `TokenStore` the engines read). If a server boots
with no valid cached token it refuses to start rather than hanging on an
interactive prompt; seed the token first with the script above.

### API surface (`app/api.py`)

- `GET /api/health`, `/api/pipeline` (full `PipelineSnapshot`), `/api/strategies`, `/api/decisions`, `/api/markets`, `/api/risk`, `/api/orders`, `/api/db/top-tables`
- `POST /api/commands/{PAUSE_STRATEGY|RESUME_STRATEGY|FLATTEN|HALT_ALL|RESET}?target=*`
- `WS /ws/live` — 1 Hz `PipelineSnapshot` frames (JSON text)
- `WS /ws/trades` — streamed `OrderEvent` frames (JSON text)

## Frontend (`frontend/`)

Vite + React + Tailwind CSS + motion.dev terminal-style monitor:

```bash
cd frontend && npm install && npm run dev
```

Vite proxies `/api` and `/ws` to the FastAPI gateway on `localhost:8000`. `npm run build && npm run lint` produce checks.

## Smoke Test (no live FYERS required)

`scripts/smoke_test.py` boots all 7 engines against **real Redis + real PostgreSQL** with the FYERS sockets stubbed, injects 30 synthetic ticks, verifies signals → cost → risk → strategy quotes → fake fill, and asserts the pipeline snapshot:

```bash
FYERS_CLIENT_ID=smoketest DATABASE_URL=postgresql://... REDIS_URL=redis://localhost:6379/1 \
  python3 scripts/smoke_test.py
```

Note: Redis pub/sub has no retention — the test waits for all engine subscriptions before injecting ticks.

## Legacy files

`bot.py` (polling market maker), `main.py` (single-process WebSocket engine), `margin.py`, `cost_model.py`, `risk_manager.py` remain for historical reference; the active code path is `run.py` → `app/`.

## Risk Controls (in `app/engines/risk_engine.py`)

- Realized PnL is booked only when a position closes (`on_fill`), using average entry vs exit price — opening a position realizes nothing.
- Inventory cap (`MAX_POSITION_QTY`) and daily-loss kill-switch (`MAX_DAILY_LOSS_RS`) halt trading via `fyers:command:*`.
- Pre-trade margin sufficiency via FYERS `multiorder/margin`, buffered by `MIN_FREE_MARGIN_BUFFER_RS`.