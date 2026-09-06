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
from app.engines.base import Engine
from app.infra.auth import TokenStore
from app.infra.db import Database
from app.infra.redis import RedisBus


class DataEngine(Engine):
    name = "data_engine"

    def __init__(self, bus: RedisBus, db: Database, token_store: TokenStore):
        super().__init__(bus, db)
        self.token_store = token_store
        self._symbols: set[str] = set()
        self._snapshots: Dict[str, schema.MarketSnapshot] = {}
        self._price_history: Dict[str, Deque[tuple[float, float]]] = {}
        self.tick_count: Dict[str, int] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._ws: Optional[data_ws.FyersDataSocket] = None
        self._max_history = 10_000
        self.feed_latency_ms = 0.0

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
                last_tick_ts=0.0, is_connected=False)
            self._price_history[s] = deque(maxlen=self._max_history)
            self.tick_count[s] = 0
        if self._ws is not None and new_symbols:
            self._resubscribe()

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
    def _on_message(self, message: Dict[str, Any]):
        if message.get("type") != "sf" or "ltp" not in message:
            return
        symbol = message.get("symbol")
        if symbol not in self._symbols:
            return
        tick = schema.MarketTick(
            ts=time.time(),
            symbol=symbol,
            ltp=float(message["ltp"]),
            bid=float(message.get("bid_price", 0.0)) or None,
            ask=float(message.get("ask_price", 0.0)) or None,
            bid_size=int(message.get("bid_size", 0)),
            ask_size=int(message.get("ask_size", 0)),
        )
        if self._queue is not None:
            self._queue.put_nowait(tick)

    def _on_connect(self):
        self.status = "healthy"
        self.detail = ""
        self._last_ws_connect = time.time()

    def _on_error(self, err):
        self.status = "degraded"
        self.detail = f"ws error: {err}"

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
    def _churn(self, hist: Deque[tuple[float, float]], window_sec: float) -> float:
        now = time.time()
        cutoff = now - window_sec
        pts = [p for p in hist if p[0] >= cutoff]
        if len(pts) < 2:
            return 0.0
        dist = sum(abs(b - a) for (_, a), (_, b) in zip(pts, pts[1:]))
        elapsed = pts[-1][0] - pts[0][0]
        if elapsed <= 0 or dist <= 0:
            return 0.0
        return max(0.0, dist / elapsed / 0.10)  # ticks/sec at commodity tick, engine solves per symbol

    # ------------------------------------------------------------------ main loop
    async def run(self):
        self._queue = asyncio.Queue(maxsize=5_000)
        self.status = "starting"
        self._ws = await self._connect_socket()
        self._resubscribe()

        hb = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stop.is_set():
                tick = await self._queue.get()
                t0 = time.perf_counter()
                await self._ingest(tick)
                self.feed_latency_ms = (time.perf_counter() - t0) * 1000
                self._mark()
        finally:
            hb.cancel()

    async def _ingest(self, tick: schema.MarketTick):
        snap = self._snapshots[tick.symbol]
        snap.ltp = tick.ltp
        if tick.bid:
            snap.bid = tick.bid
        if tick.ask:
            snap.ask = tick.ask
        snap.bid_size = tick.bid_size
        snap.ask_size = tick.ask_size
        snap.mid = (snap.bid + snap.ask) / 2.0 if (snap.bid and snap.ask) else snap.ltp
        snap.last_tick_ts = tick.ts
        snap.is_connected = True
        self.tick_count[tick.symbol] += 1
        snap.tick_count = self.tick_count[tick.symbol]
        hist = self._price_history[tick.symbol]
        hist.append((tick.ts, tick.ltp))
        snap.churn_ticks_per_sec = self._churn(hist, 30.0)

        try:
            await self.obs.publish_market(tick)
            await self.bus.set(
                self.bus.channel_for("market-latest", tick.symbol), snap, ttl_sec=5
            )
            await self.db.insert_market_tick(tick)
        except Exception:
            pass