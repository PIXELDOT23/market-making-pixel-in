"""
app/engines/data_engine.py
--------------------------
DATA ENGINE

Fetches real-time WebSocket market data as requested by the strategy engine.
Responsibilities:
  * Keeps an active FYERS data socket (data_ws).
  * Subscribes/unsubscribes symbols based on the assets live strategies need.
  * Captures every tick as an msgspec MarketTick and pushes it to:
      - the Redis market channel  (other engines / processes consume it)
      - the in-memory snapshot   (`market-latest:<symbol>`) for ultra-low latency
      - PostgreSQL batched writer (analytic history)
  * Measures per-symbol churn (ticks/sec) consumed by the signal & risk engines.

The Fyers library calls callbacks from its own threads; we hand them into the
event loop through a thread-safe asyncio queue (no lock contention in hot path).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from fyers_apiv3.FyersWebsocket import data_ws

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.auth import TokenStore
from app.infra.db import Database
from app.infra.redis import RedisBus
from app.ml import engine as ml_engine
import app.infra.logging as log

# Maximum number of concurrent in-flight Redis fan-out publishes. Kept well
# under the bus client's pool cap so a whole-market scan (hundreds of symbols)
# can never exhaust the connection pool and wedge the Redis pump.
_FANOUT_CONCURRENCY = 24
# Coalescing backoff between drain iterations for a symbol's fan-out task.
_FANOUT_BACKOFF = 0.005
# How often the (otherwise unread) market-latest snapshot key is rewritten.
_LATEST_CACHE_SEC = 5.0


class DataEngine(Engine):
    name = "data_engine"

    def __init__(self, bus: RedisBus, db: Database, token_store: TokenStore):
        super().__init__(bus, db)
        self.token_store = token_store
        self._symbols: set[str] = set()
        self._snapshots: Dict[str, schema.MarketSnapshot] = {}
        self._price_history: Dict[str, Deque[tuple[float, float]]] = {}
        self._depth_bids: Dict[str, List[schema.DepthLevel]] = {}
        self._depth_asks: Dict[str, List[schema.DepthLevel]] = {}
        self._symbol_tick: Dict[str, float] = {}
        self.tick_count: Dict[str, int] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[data_ws.FyersDataSocket] = None
        self._max_history = 10_000
        self.feed_latency_ms = 0.0
        # backpressure / load metrics
        self._enqueued_total = 0
        self.dropped_ticks = 0
        self.ingested_total = 0
        self.fanout_published = 0
        self._fanout_active: set[str] = set()
        self._fanout_pending: Dict[str, schema.MarketTick] = {}
        self._fanout_tasks: Dict[str, asyncio.Task] = {}
        self._fanout_slots: Optional[asyncio.Semaphore] = None
        self._latest_set_ts: Dict[str, float] = {}
        self._last_drop_log_ts = 0.0

    # ------------------------------------------------------------------ symbol management
    def register_symbols(self, symbols: List[str]):
        new_symbols = list(set(symbols) - self._symbols)
        if not new_symbols:
            return
        self._symbols.update(new_symbols)
        for s in new_symbols:
            self._snapshots[s] = schema.MarketSnapshot(
                symbol=s, ltp=None, bid=None, ask=None, mid=None,
                bid_size=0, ask_size=0, tick_count=0, churn_ticks_per_sec=0.0,
                volume=0, last_tick_ts=0.0, is_connected=False)
            self._price_history[s] = deque(maxlen=self._max_history)
            self._depth_bids[s] = []
            self._depth_asks[s] = []
            self.tick_count[s] = 0
        log.info(f"[data] registered {len(new_symbols)} symbol(s) -> total {len(self._symbols)}")
        if self._ws is not None and new_symbols:
            self._resubscribe()

    def set_tick_sizes(self, by_symbol: Dict[str, float]):
        """Per-symbol tick size used to normalise churn (ticks/sec) and spreads."""
        self._symbol_tick.update(by_symbol)

    @property
    def symbols(self) -> List[str]:
        return list(self._symbols)

    def snapshot(self, symbol: str) -> Optional[schema.MarketSnapshot]:
        base = self._snapshots.get(symbol)
        if base is None:
            return None
        return schema.MarketSnapshot(
            **{name: getattr(base, name) for name in type(base).__struct_fields__}
        )

    # ------------------------------------------------------------------ fyers callbacks (sdk thread)
    def _parse_depth(self, message: Dict[str, Any]) -> Tuple[List[schema.DepthLevel], List[schema.DepthLevel]]:
        """Best-N order book from the FYERS SymbolUpdate ``depth`` payload."""
        if not isinstance(message.get("depth"), dict):
            return [], []
        try:
            bids: List[schema.DepthLevel] = []
            for lvl in message["depth"].get("bid", []) or []:
                bids.append(schema.DepthLevel(
                    price=float(lvl.get("price", 0.0)),
                    qty=int(lvl.get("qty", 0)),
                    orders=int(lvl.get("orders", 0)),
                ))
            asks: List[schema.DepthLevel] = []
            for lvl in message["depth"].get("ask", []) or []:
                asks.append(schema.DepthLevel(
                    price=float(lvl.get("price", 0.0)),
                    qty=int(lvl.get("qty", 0)),
                    orders=int(lvl.get("orders", 0)),
                ))
            return bids, asks
        except Exception:
            return [], []

    def _on_message(self, message: Dict[str, Any]):
        if message.get("type") != "sf" or "ltp" not in message:
            return
        symbol = message.get("symbol")
        if symbol not in self._symbols:
            return
        bids, asks = self._parse_depth(message)
        # top-of-book can be zero/absent on equities — fall back to the head of
        # the depth book so the spread is still computable.
        bid = float(message.get("bid_price", 0.0)) or (float(bids[0].price) if bids else 0.0)
        ask = float(message.get("ask_price", 0.0)) or (float(asks[0].price) if asks else 0.0)
        bid_size = int(message.get("bid_size", 0)) or (bids[0].qty if bids else 0)
        ask_size = int(message.get("ask_size", 0)) or (asks[0].qty if asks else 0)
        tick = schema.MarketTick(
            ts=time.time(),
            symbol=symbol,
            ltp=float(message["ltp"]),
            bid=bid or None,
            ask=ask or None,
            bid_size=bid_size,
            ask_size=ask_size,
            # FYERS decodes the day's traded volume in the full-mode sf frame
            # under `vol_traded_today` (map.json index 1); the `volume` key does
            # not exist on this feed and used to leave the UI permanently at 0.
            volume=int(message.get("vol_traded_today") or message.get("volume", 0) or 0),
        )
        if self._queue is not None and self._loop is not None:
            # SDK callbacks run on Fyers's own threads — every shared-state read
            # and write happens inside a single callback on the event loop thread
            # so asyncio.Queue and the depth books are never touched cross-thread.
            self._loop.call_soon_threadsafe(self._apply_message, symbol, bids, asks, tick)

    def _apply_message(self, symbol: str, bids: List[schema.DepthLevel], asks: List[schema.DepthLevel], tick: schema.MarketTick):
        if bids:
            self._depth_bids[symbol] = bids
        if asks:
            self._depth_asks[symbol] = asks
        # FYERS streams the full 5-level book on a separate depth feed whose
        # payload does not carry a symbol, so it cannot be picked up reliably
        # here. When no depth book has arrived for a symbol, surface the live
        # top-of-book (bid_price/ask_price + sizes, which the sf frames DO
        # carry) as a best-level ladder so the UI order book is never empty.
        if not self._depth_bids.get(symbol) and tick.bid:
            self._depth_bids[symbol] = [schema.DepthLevel(price=tick.bid, qty=tick.bid_size, orders=0)]
        if not self._depth_asks.get(symbol) and tick.ask:
            self._depth_asks[symbol] = [schema.DepthLevel(price=tick.ask, qty=tick.ask_size, orders=0)]
        self._enqueue_tick(tick)

    def _enqueue_tick(self, tick: schema.MarketTick):
        """Bounded enqueue with drop-oldest backpressure: never raises QueueFull."""
        q = self._queue
        if q is None:
            return
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                self.dropped_ticks += 1
                self._maybe_log_drop()
        try:
            q.put_nowait(tick)
            self._enqueued_total += 1
        except asyncio.QueueFull:
            self.dropped_ticks += 1
            self._maybe_log_drop()

    def _maybe_log_drop(self):
        now = time.time()
        if now - self._last_drop_log_ts < 5.0:
            return
        self._last_drop_log_ts = now
        log.warn(
            f"[data] feed overloaded — dropping oldest ticks "
            f"(total drops={self.dropped_ticks}, queued={self._enqueued_total}, "
            f"ingested={self.ingested_total})"
        )

    def _on_connect(self):
        self.status = "healthy"
        self.detail = ""
        self._last_ws_connect = time.time()
        log.info(f"[data] FYERS data WebSocket CONNECTED (subscribed to {len(self._symbols)} symbol(s))")

    def _on_error(self, err):
        self.status = "degraded"
        self.detail = f"ws error: {err}"
        log.warn(f"[data] websocket error: {err} (status={self.status})")

    # ------------------------------------------------------------------ socket lifecycle
    async def _connect_socket(self) -> data_ws.FyersDataSocket:
        token = await self.token_store.ensure_valid()
        ws = data_ws.FyersDataSocket(
            access_token=f"{self.token_store.client_id}:{token}",
            write_to_file=False,
            log_path="",
            litemode=False,
            reconnect=True,
            on_connect=self._on_connect,
            on_message=self._on_message,
            on_error=self._on_error,
        )
        ws.connect()
        return ws

    def _resubscribe(self):
        if self._ws is not None and self._symbols:
            try:
                self._ws.subscribe(symbols=list(self._symbols), data_type="SymbolUpdate")
            except Exception:
                pass

    # ------------------------------------------------------------------ math
    def _churn(self, hist: Deque[tuple[float, float]], window_sec: float, tick_size: float) -> float:
        now = time.time()
        cutoff = now - window_sec
        pts = [p for p in hist if p[0] >= cutoff]
        if len(pts) < 2:
            return 0.0
        dist = sum(abs(b - a) for (_, a), (_, b) in zip(pts, pts[1:]))
        elapsed = pts[-1][0] - pts[0][0]
        if elapsed <= 0 or dist <= 0:
            return 0.0
        return max(0.0, dist / elapsed / max(tick_size, 1e-9))

    # ------------------------------------------------------------------ main loop
    async def run(self):
        self._queue = asyncio.Queue(maxsize=settings.data_feed_buffer)
        self._loop = asyncio.get_running_loop()
        self.status = "starting"
        self._ws = await self._connect_socket()
        self._resubscribe()

        hb = asyncio.create_task(self._heartbeat_loop())
        self._fanout_slots = asyncio.Semaphore(_FANOUT_CONCURRENCY)
        try:
            while not self._stop.is_set():
                tick = await self._queue.get()
                t0 = time.perf_counter()
                try:
                    await self._ingest(tick)
                except Exception as exc:
                    log.error(f"[data] ingest error: {exc!r}")
                    self.status = "degraded"
                    self.detail = f"ingest: {exc!r}"
                self.feed_latency_ms = (time.perf_counter() - t0) * 1000
                self._mark()
        finally:
            hb.cancel()
            await self._shutdown_fanout()

    async def _ingest(self, tick: schema.MarketTick):
        snap = self._snapshots.get(tick.symbol)
        if snap is None:
            return
        snap.ltp = tick.ltp
        if tick.bid:
            snap.bid = tick.bid
        if tick.ask:
            snap.ask = tick.ask
        snap.bid_size = tick.bid_size
        snap.ask_size = tick.ask_size
        snap.volume = tick.volume
        snap.mid = (snap.bid + snap.ask) / 2.0 if (snap.bid and snap.ask) else snap.ltp
        snap.last_tick_ts = tick.ts
        snap.is_connected = True
        snap.bids = list(self._depth_bids.get(tick.symbol, []))
        snap.asks = list(self._depth_asks.get(tick.symbol, []))
        self.tick_count[tick.symbol] += 1
        snap.tick_count = self.tick_count[tick.symbol]
        hist = self._price_history[tick.symbol]
        hist.append((tick.ts, tick.ltp))
        snap.churn_ticks_per_sec = self._churn(
            hist, 30.0, self._symbol_tick.get(tick.symbol, 0.10)
        )
        self.ingested_total += 1

        # --- ML: compute features + inference on every tracked tick ---
        if ml_engine.is_active(tick.symbol):
            try:
                bids_dicts = [{"price": b.price, "qty": b.qty, "orders": b.orders} for b in snap.bids]
                asks_dicts = [{"price": a.price, "qty": a.qty, "orders": a.orders} for a in snap.asks]
                ml_result = ml_engine.on_tick(
                    symbol=tick.symbol,
                    ts=tick.ts,
                    ltp=tick.ltp,
                    bid=snap.bid or 0.0,
                    ask=snap.ask or 0.0,
                    mid=snap.mid or tick.ltp,
                    bids=bids_dicts,
                    asks=asks_dicts,
                    tick_size=self._symbol_tick.get(tick.symbol, 0.10),
                    churn_5s=self._churn(hist, 5.0, self._symbol_tick.get(tick.symbol, 0.10)),
                    churn_30s=snap.churn_ticks_per_sec,
                )
                if ml_result is not None:
                    snap.ml_adverse_prob = ml_result["adverse_prob"]
                    snap.ml_should_widen = ml_result["should_widen"]
                    snap.ml_extra_ticks = ml_result["extra_ticks"]
            except Exception as exc:
                log.warn(f"[data] ML tick error ({tick.symbol}): {exc!r}")

        # cheap in-memory persistence (batched writer — no network round-trip)
        await self.db.insert_market_tick(tick)

        # coalesced Redis fan-out: one live task per symbol drains to the latest
        # tick so publish/set never block this consumer loop. Concurrency is
        # bounded by a shared semaphore (see _flush_fanout).
        self._fanout_pending[tick.symbol] = tick
        self._spawn_fanout(tick.symbol)

    def _spawn_fanout(self, symbol: str):
        if self._stop.is_set():
            return
        if symbol in self._fanout_active:
            return
        if self._fanout_slots is None:
            self._fanout_slots = asyncio.Semaphore(_FANOUT_CONCURRENCY)
        self._fanout_active.add(symbol)
        self._fanout_tasks[symbol] = asyncio.create_task(self._flush_fanout(symbol))

    async def _shutdown_fanout(self):
        """Cancel and await every live fan-out task so that no task is left
        pending when the event loop closes (the manager awaits the engine task
        first, so this runs while the loop can still resume awaits)."""
        self._fanout_pending.clear()
        tasks = list(self._fanout_tasks.values())
        self._fanout_tasks.clear()
        self._fanout_active.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass

    async def _flush_fanout(self, symbol: str):
        try:
            while True:
                tick = self._fanout_pending.pop(symbol, None)
                if tick is None:
                    await asyncio.sleep(_FANOUT_BACKOFF)
                    if symbol not in self._fanout_pending:
                        break
                    continue
                try:
                    # Bounded publish concurrency: the fan-out stays per-symbol
                    # (each symbol keeps an independent fast cadence) but the
                    # number of in-flight Redis commands is capped so a
                    # whole-market scan can never exhaust the shared pool.
                    async with self._fanout_slots:
                        await self.obs.publish_market(tick)
                    now = time.time()
                    if now - self._latest_set_ts.get(symbol, 0.0) >= _LATEST_CACHE_SEC:
                        self._latest_set_ts[symbol] = now
                        snap = self._snapshots.get(symbol)
                        if snap is not None:
                            await self.bus.set(
                                self.bus.channel_for("market-latest", symbol), snap, ttl_sec=5
                            )
                    self.fanout_published += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        finally:
            self._fanout_active.discard(symbol)
            # a tick that landed during the final drain still needs publishing
            # — respawn while running, never after a stop() has been requested.
            if symbol in self._fanout_pending and not self._stop.is_set():
                self._fanout_active.add(symbol)
                self._fanout_tasks[symbol] = asyncio.create_task(self._flush_fanout(symbol))

    def heartbeat(self, latency_ms: float = 0.0) -> "schema.EngineHeartbeat":
        hb = super().heartbeat(latency_ms)
        if self.dropped_ticks:
            hb.detail = (hb.detail + " | " if hb.detail else "") + \
                f"ticks_in={self._enqueued_total} drops={self.dropped_ticks} ingested={self.ingested_total}"
        return hb