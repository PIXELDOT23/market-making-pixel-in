# Project Architecture — Plain Language Guide

## What this does

This is a **low-latency automated market maker** that trades NFO (NSE equity futures) and MCX (commodity futures) on the Fyers broker, using one shared capital pool of ₹5,00,000.

It quotes two-sided (buy and sell) continuously, with real-time risk gates, margin checks, and an emergency kill switch that flattens everything on Ctrl+C, power loss, or market close.

---

## How to start it

```bash
# 1. Log in once per day (saves a Redis token + mirrors to a file)
.venv/bin/python scripts/auth_login.py

# 2. Start the live bot
.venv/bin/python run.py
```

Dashboard: open `http://127.0.0.1:8000/dashboard` in a browser.

---

## High-level flow

```
run.py                        ← ENTRY POINT
  │
  ├─► app/container.py         boots 7 engines + 1 API server
  │     │
  │     ├─ DataEngine          market tick feed (Fyers WebSocket)
  │     ├─ SignalEngine        spread/churn/volatility per symbol
  │     ├─ StrategyEngine      decides: quote, hold, flatten
  │     ├─ ExecutionEngine     sends orders to broker (Fyers REST + WS)
  │     ├─ RiskEngine          pre-trade gate + post-trade fill book + flatten
  │     ├─ CostEngine          slippage/cost model (not active yet)
  │     └─ MonitorEngine       15s REST broker reconcile + session windows
  │
  └─► app/api.py               FastAPI server (dashboard, REST, WebSocket)
```

Data flows through Redis pub/sub messages (not direct calls). Each engine runs as an independent asyncio task.

---

## What each engine does

### DataEngine — the eyes
Connects to the Fyers market WebSocket. Every tick is published on `fyers:market:{symbol}` for other engines to consume.

### SignalEngine — the ears
Listens to market ticks. For each symbol it computes:
- **spread** (in ticks) — is the market wide enough to make money on?
- **churn** (ticks/sec of price movement) — is the market moving too fast to quote safely?
- **volatility widening** — demand a wider spread in fast markets

If a symbol fails these gates, the strategy engine won't quote it.

### StrategyEngine — the brain
Runs the `MarketMaker` strategy. Each decision cycle:
1. Checks the spread/churn/volatility signal (from SignalEngine)
2. Computes the best bid/ask prices
3. Asks RiskEngine: "is this order allowed?" (margin, inventory, daily loss limits)
4. Sends the order to ExecutionEngine

### ExecutionEngine — the hands
Places limit orders on the Fyers REST API. Listens to the Fyers order WebSocket for fill/reject/cancel events. Updates the risk book on every fill via `risk.on_fill()`.

### RiskEngine — the heart
Runs two parallel jobs:

**Pre-trade gate** (every quote request):
- margin check (real-time Fyers `/multiorder/margin` call, cached for ~30s per symbol)
- inventory check (never hold more than 1 lot)
- daily loss check
- throttle check (max orders per minute)
- margin budget check (all positions combined can't use more than 95% of ₹5L)

**Post-trade book** (on every fill):
- tracks position (lot count), entry price, realized PnL
- enforces inventory limit
- triggers daily-loss halt if needed

**Broker reconciliation** (every 15 seconds):
- polls Fyers `/positions` REST API
- normalises NFO shares → lots (floor division)
- overwrites the book to match the broker (safety net)

### MonitorEngine — the guard
Monitors trading session windows (NSE 9:15-15:15, MCX 9:00-23:30). Triggers wind-down flattening at the segment close time. Reports engine health to the dashboard.

### CostEngine
Cost/slippage model. Currently stubbed; reserved for future use.

---

## Key files and what they do

### Entry points
| File | Purpose |
|------|---------|
| `run.py` | **Live entry point.** Boots all engines + API server. What you run in production. |
| `app/container.py` | Composition root. Creates all engines, wires them to Redis/Postgres, starts engine supervisor (restarts dead engines automatically). |
| `app/api.py` | FastAPI server. Serves dashboard HTML, REST snapshots, WebSocket live feed. |

### Engines
| File | Purpose |
|------|---------|
| `app/engines/data_engine.py` | Market tick feed (Fyers WebSocket). Publishes ticks on Redis. |
| `app/engines/signal_engine.py` | Spread/churn/volatility signal per symbol. Determines `quoteable` flag. |
| `app/engines/strategy_engine.py` | Market-maker strategy loop. Decide spread, side, qty. |
| `app/engines/execution_engine.py` | Order placement (REST) + fill tracking (WS). `_position_lots()` converts broker units to strategy lots. `flatten_all()` kills all positions. |
| `app/engines/risk_engine.py` | Margin ledger, pre-trade gate, post-trade book, broker reconcile, daily-loss halt, halt auto-recovery. |
| `app/engines/monitor_engine.py` | Session windows, segment wind-down, engine health, REST reconcile (positions + PnL). |
| `app/engines/base.py` | Base `Engine` class. Heartbeat, `_run_wrapper` with exception capture, `start()`/`stop()`. |

### Infrastructure
| File | Purpose |
|------|---------|
| `app/infra/instrument.py` | `Instrument` dataclass (lot_size, tick_size, asset_type, segment). `InstrumentRegistry` — symbol → instrument mapping. `shares_to_lots()` helper. |
| `app/infra/master_contracts.py` | Fetches NFO/MCX master contracts from Fyers at boot (lot sizes, tick sizes, expiries). |
| `app/infra/auth.py` | `TokenStore` — Redis-backed Fyers access token with TTL expiry + distributed lock (single re-auth at a time). |
| `app/infra/redis.py` | Redis pub/sub bus. `RedisBus` (connect, publish, subscribe, unsubscribe). `ObservableTickBus` for the dashboard live feed. |
| `app/infra/db.py` | PostgreSQL (optional). Logs heartbeats, risk verdicts, order events, signals to Postgres. No-op if DATABASE_URL is not set. |
| `app/infra/market_hours.py` | NSE/MCX session windows, wind-down detection, `is_open()`, `segment_status()`. |
| `app/infra/logging.py` | ANSI color terminal logging. |

### Config and sizing
| File | Purpose |
|------|---------|
| `app/config.py` | All settings (dataclass). Every setting has a sensible default; override via env vars. See "Key settings" below. |
| `app/scanner.py` | Ranks instruments by priority. Computes margin per lot. Marks `quoteable=True/False`. |
| `app/sizing.py` | Dynamic position sizing (currently hard-capped at `MAX_POSITION_QTY=1`). |
| `app/weighting.py` | Segment budget allocation. |
| `app/schema.py` | All shared data types (MarketTick, OrderEvent, RiskVerdict, etc.) — msgspec for low-latency serialisation. |

### Scripts (one-off utilities)
| File | Purpose |
|------|---------|
| `scripts/auth_login.py` | Daily Fyers OAuth login. Saves token to Redis + mirrors to `fyers_access_token.txt`. Run once per day before starting the bot. |
| `scripts/fyers_login.py` | Alternative Fyers v3 login helper (same flow, slightly different implementation). |
| `scripts/smoke_test.py` | Boots all engines against real Redis/Postgres but stubs the Fyers SDK (no live orders). Verifies the full pipeline wiring. |
| `scripts/stress_test.py` | Data + execution engine stress test. Verifies `asyncio.Queue` overflow handling (drop-oldest). |

### Legacy standalone scripts (historical reference)
| File | Purpose |
|------|---------|
| `legacy/bot.py` | Original polling market maker (pre-async). For historical reference only. |
| `legacy/main.py` | Single-process WebSocket market maker (pre-architecture). |
| `legacy/config.py` | Original settings + `broker_qty()`/`is_equity_future()` helpers. |
| `legacy/margin.py` | Original margin calculator (uses `margin_total`, not `margin_new_order`). |
| `legacy/auth.py` | Original Fyers login (writes to `fyers_access_token.txt`). |
| `legacy/cost_model.py` | Original slippage model. |
| `legacy/risk_manager.py` | Original risk gate. |
| `legacy/logger.py` | ANSI color terminal logging (original). |

These are NOT used by the live bot. They are kept for reference.

---

## Trade flow — step by step

```
1.  DataEngine receives a tick for MCX:NATURALGAS26SEPFUT
2.  SignalEngine computes: spread = 4 ticks, churn = 0.2 t/s, quoteable = True
3.  StrategyEngine picks: bid 280.00, ask 284.00, qty = 1 lot
4.  RiskEngine.check() is called:
      - throttle: OK (not exceeded max_orders_per_minute)
      - inventory: OK (net position = 0, limit ±1)
      - daily_loss: OK (₹0 realized today)
      - margin_api: OK (margin_total ₹47,973 < avail ₹209,613 - buffer ₹500)
      - margin_budget: OK (remaining ₹209,613 - ₹0 booked by others)
      → VERDICT: ALLOWED
5.  ExecutionEngine.place_order(MCX:NATGAS, BUY, 1 lot, limit 280.00)
6.  Fyers fills instantly → OrderEvent(status=FILLED, filledQty=1)
7.  ExecutionEngine._handle_event() → risk.on_fill(side=1, qty=1, price=280.00)
8.  RiskEngine.on_fill():
      - pos[natgas] goes from 0 → +1 lot
      - realized PnL not yet (still open position)
      - inventory constraint: OK (net = +1 ≤ 1)
9.  StrategyEngine now quotes: bid 280.00, ask 284.00 but ONLY on the sell side
    (inventory at max = 1 → can only reduce)
10. MonitorEngine polls broker positions every 15s → confirms netQty matches book
```

---

## Key settings (env vars)

| Setting | Env var | Default | Meaning |
|---------|---------|---------|---------|
| `symbol` | `SYMBOL` | `MCX:NATURALGAS26SEPFUT` | Primary instrument |
| `capital_rs` | `CAPITAL_RS` | `500000` | Account capital (for sizing display) |
| `max_position_qty` | `MAX_POSITION_QTY` | `1` | Max lots per position (per side) |
| `margin_utilization_target` | `MARGIN_UTILIZATION_TARGET` | `0.95` | Use up to 95% of free margin |
| `margin_reserve_both_sides` | `MARGIN_RESERVE_BOTH_SIDES` | `false` | true = reserve margin for both buy + sell |
| `min_free_margin_buffer_rs` | `MIN_FREE_MARGIN_BUFFER_RS` | `500` | Hard floor: ₹500 always kept free |
| `margin_refresh_sec` | `MARGIN_REFRESH_SEC` | `30` | How often to re-query per-lot margin |
| `check_margin_before_order` | `CHECK_MARGIN_BEFORE_ORDER` | `true` | true = real-time margin gate |
| `product_type` | `PRODUCT_TYPE` | `INTRADAY` | Fyers product type (must be INTRADAY) |
| `flatten_orphans_at_boot` | `FLATTEN_ORPHANS_AT_BOOT` | `true` | Square off leftover positions from a previous run |

---

## Fyers broker quirks

**NFO equity futures = shares, MCX = lots:**
- NFO (NSE): order qty is in **underlying shares**. SBIN lot = 750 shares, so qty=750 for 1 lot. If you send qty=75 → Fyers rejects: `-50 not a multiple of minimum lot size 75`.
- MCX: order qty is in **lots** directly. NATGASMINI lot size is 1 lot = 250 units. qty=1 is correct.

**`margin_total` not `margin_new_order`:**
- Broker returns both fields. `margin_total` = the order's own marginal margin (scales with qty, the correct field). `margin_new_order` = parked margin + order margin (inflated by ₹273K from manual NIFTY positions — wrong for per-order sizing).

**Rate limit (-429):**
- Fyers imposes strict rate limits on `/multiorder/margin`. On -429, the bot backs off that symbol for 5× the normal refresh interval (~150 seconds). A timeout is treated the same way.

---

## Test suite

125 tests covering:
- `test_risk_margin.py` — broker margin field semantics, shares→lots conversion, NFO qty in legacy scripts, budget gate double-counting
- `test_margin_stress.py` — real broker margin figures (NATGASMINI ₹9,611, NATURALGAS ₹47,973, etc.), rate-limit backoff, margin-refresh storm
- `test_engine_ops_fixes.py` — flatten oversell prevention, partial-fill-then-cancel booking, per-symbol tick sizing, engine supervisor restart
- `test_quoting_stress.py` — quote stress scenarios
- `test_engine_gating.py` — engine startup gating
- `test_engine_task_drain.py` — event queue drain
- `test_flatten_scope.py` — flatten scope (NSE vs MCX only)
- `test_scanner.py` — scanner ranking and margin per lot
- `test_cost_engine.py` — cost engine
- `test_sizing.py` — position sizing
- `test_signal_engine.py` — signal generation
- `test_market_hours.py` — session window logic

Run: `.venv/bin/python -m unittest discover -s tests`
