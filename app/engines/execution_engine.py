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

    # ------------------------------------------------------------------ caller API (async)
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

        rounded = round(round(price / settings.resolved_tick_size) * settings.resolved_tick_size, 2)
        side_name = "BUY" if side == 1 else "SELL"
        data = {
            "symbol": symbol, "qty": qty, "type": 1, "side": side,
            "productType": product_type, "limitPrice": rounded, "stopPrice": 0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
            "stopLoss": 0, "takeProfit": 0,
        }
        try:
            resp = (await self._call("place_order", data)) or {}
        except Exception as exc:
            log.error(f"[exec] place_order exception: {exc}")
            return None
        if resp.get("s") != "ok":
            log.error(f"[exec] place failed {side_name} {qty}@{rounded:.2f}: {resp}")
            self.rejects_total += 1
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
        self, order_id: str, price: float, qty: Optional[int] = None
    ) -> bool:
        data = {"id": order_id, "type": 1, "limitPrice": round(round(price / settings.resolved_tick_size) * settings.resolved_tick_size, 2)}
        if qty:
            data["qty"] = qty
        try:
            resp = (await self._call("modify_order", data)) or {}
            return resp.get("s") == "ok"
        except Exception:
            return False

    async def square_off(self, strategy: str, symbol: str, qty: int, side: int, product_type: str = "INTRADAY"):
        """Market order to flatten (no risk gate — emergency)."""
        import json
        data = {
            "symbol": symbol, "qty": qty, "type": 2, "side": side,
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
            self._orders[oid].status = STATUS_CANCELED
            self._orders[oid].status_label = "CANCELED"

        # anything else resting on the broker book
        try:
            book = (await self._call("orderbook", {}, get="orderbook")) or {}
            for o in book.get("orders", []):
                if int(o.get("status", 0)) == STATUS_OPEN:
                    await self.cancel_order(str(o.get("id")))
        except Exception as exc:
            log.error(f"[exec] cancel_open_orders book fetch: {exc}")

    async def flatten_all(self):
        """Cancel all open orders and market-flatten net positions (kill switch)."""
        log.warn("[exec] FLATTEN ALL (kill switch)")
        try:
            await self.cancel_open_orders()

            resp = (await self._call("positions", {}, get="positions")) or {}
            for pos in resp.get("netPositions", []):
                if pos.get("netQty", 0) == 0:
                    continue
                raw = pos["netQty"]
                qty = int(round(abs(raw) / settings.resolved_lot_size)) if settings.segment == "COMMODITY" and abs(raw) >= settings.resolved_lot_size else abs(raw)
                side = -1 if raw > 0 else 1
                await self.square_off("killswitch", pos.get("symbol"), qty, side)
            await self._call("exit_positions", {}, get="exit_positions")
        except Exception as exc:
            log.error(f"[exec] flatten_all exception: {exc}")

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
                self._queue.put_nowait(ev)
            except Exception:
                pass

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
            await self._handle_event(ev)
            self._mark()

        hb.cancel()
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:
                pass

    async def _handle_event(self, ev: schema.OrderEvent):
        self._orders[ev.broker_order_id] = ev

        if ev.status == STATUS_FILLED:
            if self.risk is not None:
                await self.risk.on_fill(
                    side=ev.side, qty=ev.filled_qty or ev.qty,
                    price=ev.traded_price or ev.limit_price,
                    ref_price=self._ref_prices.get(ev.symbol, ev.traded_price or ev.limit_price),
                )
            self.fills_total += 1
            log.trade(
                f"[exec] FILL {ev.status_label} {ev.side:+d} {ev.filled_qty or ev.qty} "
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