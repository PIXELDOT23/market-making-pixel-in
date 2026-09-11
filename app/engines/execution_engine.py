"""
app/engines/execution_engine.py
-------------------------------
EXECUTION ENGINE

Real-time WebSocket order management — the single place that talks to the
broker's order execution:

  * owns the FYERS order websocket (order_ws): instant fill / reject /
    cancel callbacks
  * submits limit orders, cancels, replaces
  * reconciles live quotes against broker orderbook
  * publishes OrderEvent msgspec structs + latest QuoteState on the bus
  * calls back into the RiskEngine for post-trade inventory/PnL updates
  * square-off (market flatten) for halts / emergency

Every placement goes through the RiskEngine pre-trade gate first.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import Any, Dict, List, Optional, Tuple

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import order_ws

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.auth import TokenStore
from app.infra.db import Database
from app.infra.instrument import AssetType, instrument_registry
from app.infra.redis import RedisBus
import app.infra.logging as log

# FYERS order status codes
STATUS_OPEN = 6
STATUS_FILLED = 2
STATUS_CANCELED = 1
STATUS_REJECTED = 5

_STATUS_LABELS = {
    STATUS_OPEN: "OPEN", STATUS_FILLED: "FILLED",
    STATUS_CANCELED: "CANCELED", STATUS_REJECTED: "REJECTED",
}


class ExecutionEngine(Engine):
    name = "execution_engine"

    def __init__(self, bus: RedisBus, db: Database, token_store: TokenStore, risk=None):
        super().__init__(bus, db)
        self.token_store = token_store
        self.risk = risk                       # RiskEngine (post-trade updates)
        self._ws: Optional[order_ws.FyersOrderSocket] = None
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._enqueued_events = 0
        self.dropped_events = 0
        self._last_drop_log_ts = 0.0
        # Dedicated REST pool: lives independently of the loop's default executor,
        # which uvicorn shuts down during serve() teardown — the flatten-in-finally
        # must still be able to reach the broker after that point.
        self._rpe = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="fyers-rest"
        )
        self._orders: Dict[str, schema.OrderEvent] = {}
        self._active_quotes: Dict[str, schema.QuoteState] = {}
        self._ref_prices: Dict[str, float] = {}   # symbol -> last mid for MTM
        self.fills_total = 0
        self.rejects_total = 0
        # Per-symbol reject circuit-breaker: after a hard broker rejection this
        # symbol is stood down for `settings.reject_cooldown_sec` so the whole
        # scanner stops hammering the broker (and the margin REST API) with the
        # same doomed order every decision cycle.
        self._reject_until: Dict[str, float] = {}
        self._reject_reason: Dict[str, str] = {}

    # ------------------------------------------------------------------ caller API (async)
    def _managed(self, symbol: str) -> bool:
        """True iff ``symbol`` is one the bot itself trades.

        Strict registry lookup that NEVER auto-registers: a position sitting on
        the broker book that is not in the bot's universe (e.g. a manually
        punched options order) must never be adopted, flattened, or squared off
        by a kill-switch / boot reconcile.
        """
        return instrument_registry.get(symbol) is not None

    def _instrument(self, symbol: str):
        from app.infra.instrument import build_instrument
        inst = instrument_registry.get(symbol)
        if inst is None:
            inst = instrument_registry.register(build_instrument(
                symbol,
                segment=settings.asset_type or settings.segment,
                lot_size=settings.lot_size,
                tick_size=settings.tick_size,
                margin_per_lot_rs=settings.margin_per_lot_rs,
            ))
        return inst

    def _round_tick(self, price: float, symbol: str) -> float:
        tick = self._instrument(symbol).tick_size
        return round(round(price / tick) * tick, 2)

    def _order_qty(self, symbol: str, qty: int) -> int:
        """FYERS ``qty`` semantics differ by segment:

          * equity futures -> underlying **shares** (qty % lot_size == 0;
            a 1-lot SBIN future order is qty=750).
          * MCX commodity  -> **lots** (minLotSize=1; order qty=1 = one
            contract, not 5000 units). The contract weight is ``qtyMultiplier``
            in the symbol master, so it must NOT be multiplied here.
          * cash equity    -> shares.

        Strategy sizes in lots, so convert through the multiplier only for the
        equity-futures case; commodities pass lots straight through."""
        inst = self._instrument(symbol)
        if inst.quote_in_lots:
            if inst.asset_type == AssetType.COMMODITY_FUT:
                return int(qty)
            return int(qty) * max(1, int(inst.lot_size))
        return int(qty)

    async def place_limit(
        self, strategy: str, symbol: str, qty: int, side: int, price: float,
        product_type: str = "INTRADAY",
    ) -> Optional[str]:
        verdict = await self.risk.check(
            symbol=symbol, strategy=strategy, side=side, qty=qty,
            price=price, mid=self._ref_prices.get(symbol),
            product_type=product_type,
        ) if self.risk else None
        if verdict is not None and not verdict.allowed:
            log.risk(f"[exec] blocked {strategy} {side:+d} {symbol}: {verdict.reason}")
            return None

        rounded = self._round_tick(price, symbol)
        side_name = "BUY" if side == 1 else "SELL"
        data = {
            "symbol": symbol, "qty": self._order_qty(symbol, qty), "type": 1, "side": side,
            "productType": product_type, "limitPrice": rounded, "stopPrice": 0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
            "stopLoss": 0, "takeProfit": 0,
        }
        try:
            resp = (await self._call("place_order", data)) or {}
        except Exception as exc:
            log.error(f"[exec] place_order exception: {exc}")
            self._tripped_reject(symbol, f"exception: {exc}")
            return None
        if resp.get("s") != "ok":
            log.error(f"[exec] place failed {side_name} {qty}@{rounded:.2f}: {resp}")
            self.rejects_total += 1
            self._tripped_reject(symbol, str(resp.get("message") or resp)[:160])
            return None
        order_id = str(resp.get("id"))
        log.trade(f"[exec] placed {side_name} {qty} {symbol} @ {rounded:.2f} | {order_id}")
        self._mark()
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        if not order_id:
            return False
        try:
            resp = (await self._call("cancel_order", {"id": order_id})) or {}
            if resp.get("s") == "ok":
                log.info(f"[exec] canceled {order_id}")
                return True
            log.error(f"[exec] cancel failed {order_id}: {resp}")
        except Exception as exc:
            log.error(f"[exec] cancel exception {order_id}: {exc}")
        return False

    async def replace_order(
        self, order_id: str, price: float, qty: Optional[int] = None,
        symbol: str = "", side: int = 0, product_type: str = "INTRADAY",
    ) -> bool:
        """Re-price a resting order in place (modify_order). Only safe on a
        fully-unfilled OPEN order — callers must check ``order_live_and_unfilled``
        first. Sends the same order shape as ``place_limit`` so the broker can
        accept the modification without resetting SL/TP/validity fields."""
        _symbol = symbol or settings.symbol
        if side == 0:
            ev = self._orders.get(order_id)
            side = ev.side if ev is not None else 0
        data = {
            "id": order_id, "type": 1,
            "limitPrice": self._round_tick(price, _symbol),
            "stopPrice": 0, "productType": product_type,
            "side": side, "validity": "DAY", "disclosedQty": 0,
        }
        if qty:
            data["qty"] = self._order_qty(_symbol, qty)
        try:
            resp = (await self._call("modify_order", data)) or {}
            ok = resp.get("s") == "ok"
            if ok:
                log.info(f"[exec] modified {order_id} -> {data['limitPrice']:.2f}")
            else:
                log.error(f"[exec] modify failed {order_id}: {resp}")
            return ok
        except Exception as exc:
            log.error(f"[exec] modify exception {order_id}: {exc}")
            return False

    async def square_off(self, strategy: str, symbol: str, qty: int, side: int, product_type: str = "INTRADAY"):
        """Market order to flatten (no risk gate — emergency)."""
        import json
        data = {
            "symbol": symbol, "qty": self._order_qty(symbol, qty), "type": 2, "side": side,
            "productType": product_type, "limitPrice": 0, "stopPrice": 0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
        }
        try:
            resp = (await self._call("place_order", data)) or {}
            log.trade(f"[exec] square-off market order {side:+d} {qty} {symbol}: {resp}")
        except Exception as exc:
            log.error(f"[exec] square-off exception: {exc}")

    async def cancel_open_orders(self):
        """Cancel every resting order we know about: tracked quotes + broker book."""
        ids: List[str] = []
        for qs in self._active_quotes.values():
            for oid in (qs.bid_id, qs.ask_id):
                if oid:
                    ids.append(oid)
        self._active_quotes.clear()
        for oid, ev in self._orders.items():
            if ev.status == STATUS_OPEN:
                ids.append(oid)
        for oid in dict.fromkeys(ids):
            await self.cancel_order(oid)
            ev = self._orders.get(oid)
            if ev is not None:
                ev.status = STATUS_CANCELED
                ev.status_label = "CANCELED"

        # anything else resting on the broker book — only for managed symbols;
        # a manually punched order on an unmanaged symbol is never cancelled
        try:
            book = (await self._call("orderbook", {}, get="orderbook")) or {}
            for o in book.get("orders", []):
                if int(o.get("status", 0)) != STATUS_OPEN:
                    continue
                if not self._managed(str(o.get("symbol", ""))):
                    continue
                await self.cancel_order(str(o.get("id")))
        except Exception as exc:
            log.error(f"[exec] cancel_open_orders book fetch: {exc}")

    async def flatten_all(self):
        """Cancel open orders and market-flatten net positions (kill switch).

        Only positions/orders on symbols the bot itself manages are touched; a
        manually punched order on any other symbol is left completely alone.
        """
        log.warn("[exec] FLATTEN ALL (kill switch)")
        try:
            await self.cancel_open_orders()

            resp = (await self._call("positions", {}, get="positions")) or {}
            for pos in resp.get("netPositions", []):
                if pos.get("netQty", 0) == 0:
                    continue
                sym = pos.get("symbol", "")
                if not self._managed(sym):
                    log.warn(
                        f"[exec] flatten: SKIP unmanaged position {sym} "
                        f"(manual order?) — not flattening"
                    )
                    continue
                raw = pos["netQty"]
                inst = self._instrument(sym)
                qty = self._position_lots(raw, inst)
                if qty <= 0:
                    # sub-lot remainder below one full lot: cannot be closed
                    # lawfully (NFO requires lot multiples) — never oversell.
                    log.warn(f"[exec] flatten_all: skip {sym} sub-lot remainder ({raw})")
                    continue
                side = -1 if raw > 0 else 1
                await self.square_off("killswitch", sym, qty, side)
        except Exception as exc:
            log.error(f"[exec] flatten_all exception: {exc}")

    def _position_lots(self, raw: int, inst) -> int:
        """Broker net position qty -> strategy lot count.

        NFO equity futures report positions in underlying **shares** (750 SBIN
        shares for one lot), while MCX commodities report **lots** directly
        (orders go in at minLotSize=1). Convert through the multiplier only
        for the equity-futures case — and FLOOR so a sub-lot remainder never
        rounds UP into an oversell (squaring off 1900 shares as 3 lots would
        sell 2250 and open an unintended 350-share short)."""
        if inst.quote_in_lots and inst.asset_type != AssetType.COMMODITY_FUT:
            mag = abs(raw) // max(1, int(getattr(inst, "lot_size", 1)))
            return mag
        return abs(raw)

    def _segment_of(self, symbol: str) -> str:
        """Exchange id ("NSE" | "MCX") for a broker symbol, falling back to the
        registered instrument's segment."""
        if not symbol:
            return ""
        prefix = str(symbol).split(":", 1)[0].upper()
        if prefix in ("NSE", "MCX"):
            return prefix
        try:
            inst = self._instrument(symbol)
        except Exception:
            inst = None
        if inst is not None:
            from app.infra import market_hours
            return market_hours.segment_id(inst.segment)
        return ""

    async def flatten_segment(self, exchange: str):
        """Wind-down flatten for ONE exchange only (NSE or MCX): cancel resting
        orders for that exchange's symbols and square off its net positions.

        While NSE enters its 15:15 wind-down and squares off, the MCX book that
        runs into the night is left completely untouched (and vice-versa)."""
        exchange = str(exchange).upper()
        if exchange not in ("NSE", "MCX"):
            log.error(f"[exec] flatten_segment: unknown exchange {exchange!r}")
            return
        log.warn(f"[exec] FLATTEN SEGMENT {exchange} (wind-down)")

        def _matches(sym: str) -> bool:
            return self._segment_of(sym) == exchange

        try:
            # 1) cancel resting orders for this segment: tracked quotes + tracked order ids
            ids: List[str] = []
            for qs in self._active_quotes.values():
                if not _matches(qs.symbol):
                    continue
                for oid in (qs.bid_id, qs.ask_id):
                    if oid:
                        ids.append(oid)
            for oid, ev in self._orders.items():
                if ev.status == STATUS_OPEN and _matches(ev.symbol):
                    ids.append(oid)
            for oid in dict.fromkeys(ids):
                await self.cancel_order(oid)
                ev = self._orders.get(oid)
                if ev is not None:
                    ev.status = STATUS_CANCELED
                    ev.status_label = "CANCELED"

            # anything else resting on the broker book for this segment
            try:
                book = (await self._call("orderbook", {}, get="orderbook")) or {}
                for o in book.get("orders", []):
                    if int(o.get("status", 0)) != STATUS_OPEN:
                        continue
                    o_sym = str(o.get("symbol", ""))
                    if not (_matches(o_sym) and self._managed(o_sym)):
                        continue
                    await self.cancel_order(str(o.get("id")))
            except Exception as exc:
                log.error(f"[exec] flatten_segment {exchange} book fetch: {exc}")

            # 2) square off net positions for this segment only
            resp = (await self._call("positions", {}, get="positions")) or {}
            for pos in resp.get("netPositions", []):
                if pos.get("netQty", 0) == 0:
                    continue
                raw = pos["netQty"]
                sym = pos.get("symbol", "")
                if not _matches(sym):
                    continue
                if not self._managed(sym):
                    log.warn(
                        f"[exec] flatten_segment: SKIP unmanaged position {sym} "
                        f"(manual order?) — not flattening"
                    )
                    continue
                inst = self._instrument(sym)
                qty = self._position_lots(raw, inst)
                if qty <= 0:
                    log.warn(f"[exec] flatten_segment: skip {sym} sub-lot remainder ({raw})")
                    continue
                side = -1 if raw > 0 else 1
                await self.square_off("wind_down", sym, qty, side)
        except Exception as exc:
            log.error(f"[exec] flatten_segment {exchange} exception: {exc}")

    # ------------------------------------------------------------------ REST call helper (runs in executor, always bounded)
    async def _call(self, method: str, data: Dict[str, Any] = None, get: Optional[str] = None, timeout: float = 10.0):
        if self._fyers is None:
            await self.attach()
        if self._fyers is None:
            return None
        try:
            if get:
                fn = getattr(self._fyers, get)
                return await asyncio.wait_for(
                    asyncio.wrap_future(self._rpe.submit(fn) if not data else self._rpe.submit(fn, data or {})),
                    timeout=timeout,
                )
            return await asyncio.wait_for(
                asyncio.wrap_future(self._rpe.submit(getattr(self._fyers, method), data or {})),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.error(f"[exec] REST {method or get} timed out ({timeout:.0f}s) — broker unresponsive")
            return None

    # ------------------------------------------------------------------ fyers wire
    @property
    def _fyers(self):
        return self.__dict__.get("_fyers_inst")

    @_fyers.setter
    def _fyers(self, value):
        self.__dict__["_fyers_inst"] = value

    async def attach(self):
        token = await self.token_store.ensure_valid()
        self._fyers = fyersModel.FyersModel(
            client_id=settings.client_id, token=token, is_async=False, log_path=""
        )

    def _on_order(self, message: Dict[str, Any]):
        orders_data = message.get("orders") or message
        if isinstance(orders_data, dict):
            self._queue_orders([orders_data])
        elif isinstance(orders_data, list):
            self._queue_orders(orders_data)

    def _queue_orders(self, orders: List[Dict[str, Any]]):
        if self._queue is None:
            return
        events: List[schema.OrderEvent] = []
        for o in orders:
            try:
                ev = schema.OrderEvent(
                    ts=time.time(),
                    broker_order_id=str(o.get("id", "")),
                    strategy=o.get("strategy", "unknown"),
                    symbol=o.get("symbol", ""),
                    side=int(o.get("side", 0)),
                    qty=int(o.get("qty", 0)),
                    status=int(o.get("status", 0)),
                    status_label=_STATUS_LABELS.get(int(o.get("status", 0)), "UNKNOWN"),
                    limit_price=float(o.get("limitPrice", 0.0)),
                    filled_qty=int(o.get("filledQty", 0)),
                    traded_price=float(o.get("tradedPrice", 0.0)),
                    raw=o,
                )
                events.append(ev)
            except Exception:
                continue
        if not events:
            return
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue_events, events)
        else:
            self._enqueue_events(events)

    def _enqueue_events(self, events: List[schema.OrderEvent]):
        """Bounded enqueue on the event-loop thread with drop-oldest backpressure."""
        q = self._queue
        if q is None:
            return
        for ev in events:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                else:
                    self.dropped_events += 1
                    now = time.time()
                    if now - self._last_drop_log_ts >= 5.0:
                        self._last_drop_log_ts = now
                        log.warn(
                            f"[exec] order event overload — dropping oldest "
                            f"(total drops={self.dropped_events}, enqueued={self._enqueued_events})"
                        )
            try:
                q.put_nowait(ev)
                self._enqueued_events += 1
            except asyncio.QueueFull:
                self.dropped_events += 1

    def _on_connect(self):
        self.status = "healthy"
        self.detail = ""
        log.success("[exec] Order WS connected")

    def _on_error(self, err):
        self.status = "degraded"
        self.detail = f"order ws error: {err}"

    async def _connect(self):
        token = await self.token_store.ensure_valid()
        self._ws = order_ws.FyersOrderSocket(
            access_token=f"{self.token_store.client_id}:{token}",
            write_to_file=False,
            log_path="",
            reconnect=True,
            on_orders=self._on_order,
            on_connect=self._on_connect,
            on_error=self._on_error,
        )
        self._ws.connect()
        await asyncio.sleep(1)
        self._ws.subscribe(data_type="OnOrders,OnTrades,OnPositions")

    # ------------------------------------------------------------------ main loop
    async def run(self):
        self._queue = asyncio.Queue(maxsize=5_000)
        self._loop = asyncio.get_running_loop()
        await self.attach()
        try:
            await self._connect()
        except Exception as exc:
            self.status = "degraded"
            self.detail = f"connect: {exc}"
        hb = asyncio.create_task(self._heartbeat_loop())

        # also listen for the raw FYERS data feed indirectly via risk ref prices
        async def market_handler(ch: str, raw: bytes):
            try:
                tick = schema.decode(schema.MarketTick, raw)
                self._ref_prices[tick.symbol] = tick.ltp
            except Exception:
                pass
        await self.bus.subscribe("fyers:market:*", handler=market_handler)

        while not self._stop.is_set():
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue
            try:
                await self._handle_event(ev)
            except Exception as exc:
                # Never let one bad fill/order event kill the whole order+fills
                # feed: quotes stay resting while the queue silently stops.
                self.status = "degraded"
                self.detail = f"event handling error: {exc!r}"
                log.error(f"[exec] _handle_event crashed: {exc!r}")
            self._mark()

        hb.cancel()
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:
                pass

    async def _handle_event(self, ev: schema.OrderEvent):
        self._orders[ev.broker_order_id] = ev

        filled = ev.filled_qty or (ev.qty if ev.status == STATUS_FILLED else 0)
        terminal_filled = (ev.status == STATUS_FILLED or (
            ev.status == STATUS_CANCELED and filled > 0
        ))
        if terminal_filled:
            # A partially-filled-then-CANCELED order must still be booked: the
            # broker holds the filled shares even though the order no longer
            # exists. Skipping it leaves the book at net=0 until the 15s REST
            # reconcile, letting a second lot fill on top of the unbooked one.
            if self.risk is not None:
                try:
                    await self.risk.on_fill(
                        side=ev.side, qty=filled,
                        price=ev.traded_price or ev.limit_price,
                        ref_price=self._ref_prices.get(ev.symbol, ev.traded_price or ev.limit_price),
                        symbol=ev.symbol,
                    )
                except Exception as exc:
                    ev.status_label = f"{ev.status_label} (fill-book error {exc!r})"
            self.fills_total += 1
            log.trade(
                f"[exec] FILL {ev.status_label} {ev.side:+d} {filled} "
                f"{ev.symbol} @ {ev.traded_price:.2f} | {ev.broker_order_id}"
            )
        elif ev.status == STATUS_REJECTED:
            self.rejects_total += 1
            log.error(f"[exec] REJECTED {ev.broker_order_id}: {ev.raw.get('message', '')}")

        try:
            await self.obs.publish_order(ev)
            await self.db.insert_order_event(ev)
        except Exception:
            pass

    # ------------------------------------------------------------------ convenience for strategies
    def _tripped_reject(self, symbol: str, reason: str):
        """Open the per-symbol reject circuit so the scanner stands this symbol
        down for the cooldown window instead of re-submitting a doomed order."""
        now = time.time()
        self._reject_until[symbol] = now + settings.reject_cooldown_sec
        if reason != self._reject_reason.get(symbol, ""):
            self._reject_reason[symbol] = reason
            log.warn(
                f"[exec] reject circuit tripped for {symbol} "
                f"({settings.reject_cooldown_sec:.0f}s cooldown): {reason}"
            )

    def reject_cooldown(self, symbol: str) -> float:
        """Seconds still left on the reject circuit for ``symbol`` (0 = clear)."""
        until = self._reject_until.get(symbol, 0.0)
        return max(0.0, until - time.time())

    def reject_reason(self, symbol: str) -> str:
        return self._reject_reason.get(symbol, "")

    # ------------------------------------------------------------------ order state helpers (idempotency)
    def tracked_order(self, order_id: str) -> Optional[schema.OrderEvent]:
        """The latest known state of an order from the order WebSocket, if seen."""
        return self._orders.get(order_id)

    def order_live_and_unfilled(self, order_id: str) -> bool:
        """True when the order is still resting on the broker and has NO fills —
        the only state where modifying it in place is safe."""
        ev = self._orders.get(order_id)
        return ev is not None and ev.status == STATUS_OPEN and (ev.filled_qty or 0) == 0

    def order_partially_filled(self, order_id: str) -> bool:
        ev = self._orders.get(order_id)
        return ev is not None and ev.status == STATUS_OPEN and (ev.filled_qty or 0) > 0

    def is_order_known_dead(self, order_id: str) -> bool:
        """The order feed has confirmed this id is gone (filled/canceled/rejected)
        — safe to forget it and place a replacement."""
        ev = self._orders.get(order_id)
        return ev is not None and ev.status in (STATUS_FILLED, STATUS_CANCELED, STATUS_REJECTED)

    def quote_state(self, strategy: str) -> Optional[schema.QuoteState]:
        return self._active_quotes.get(strategy)

    async def set_quote_state(
        self, strategy: str, symbol: str, bid_id: Optional[str], ask_id: Optional[str],
        bid_price: float, ask_price: float, qty: int, status: str = "active",
    ):
        qs = schema.QuoteState(
            strategy=strategy, symbol=symbol, ts=time.time(),
            bid_id=bid_id, ask_id=ask_id, bid_price=bid_price,
            ask_price=ask_price, as_qty=qty, status=status,
        )
        self._active_quotes[strategy] = qs
        return qs

    def heartbeat(self, latency_ms: float = 0.0) -> "schema.EngineHeartbeat":
        hb = super().heartbeat(latency_ms)
        if self.dropped_events:
            hb.detail = (hb.detail + " | " if hb.detail else "") + \
                f"events_in={self._enqueued_events} drops={self.dropped_events}"
        return hb