"""
app/engines/risk_engine.py
--------------------------
RISK ENGINE

Checks that every constraint is healthy and reliable for the overall engine —
margin, position/inventory, order throttle, daily loss. Sits between signal
generation and execution:

  * subscribes to the market + order feeds
  * evaluates a RiskVerdict for every proposed order (pre-trade gate)
  * tracks inventory & realized PnL from fill events (post-trade monitor)
  * publishes verdicts/metrics + persists to PostgreSQL
  * exposes a fast in-process `check(side, qty, price)` API used by strategies
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from fyers_apiv3 import fyersModel

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.auth import TokenStore
from app.infra.db import Database
from app.infra.redis import RedisBus
from app.infra import logging as log


class RiskEngine(Engine):
    name = "risk_engine"

    def __init__(self, bus: RedisBus, db: Database, token_store: TokenStore, initial_token: str = ""):
        super().__init__(bus, db)
        self.token_store = token_store
        self.halted = False
        self.halt_reasons: List[str] = []
        self.net_position = 0
        self.realized_pnl = 0.0
        self._order_timestamps: deque[float] = deque()
        self._last_margin_avail = 0.0
        self._last_margin_required = 0.0
        self._constraints: dict[str, schema.RiskConstraint] = {}
        self._inventory_open_ts: Optional[float] = None
        self._entry_price = 0.0
        self._margin_lru: Dict[str, schema.MarginCheck] = {}
        self._fyers = None if not initial_token else self._build_client(initial_token)

    def _build_client(self, token: str) -> fyersModel.FyersModel:
        return fyersModel.FyersModel(
            client_id=settings.client_id, token=token, is_async=False, log_path=""
        )

    async def attach(self):
        if self._fyers is None:
            token = await self.token_store.ensure_valid()
            self._fyers = self._build_client(token)

    # ------------------------------------------------------------------ accessors
    @property
    def constraints_healthy(self) -> bool:
        return all(c.healthy for c in self._constraints.values())

    def risk_status(self) -> str:
        return (
            f"pos={self.net_position} realized=₹{self.realized_pnl:,.2f} "
            f"halted={self.halted}"
        )

    # ------------------------------------------------------------------ constraint checks (pure)
    def _throttle_ok(self) -> Tuple[bool, str]:
        now = time.time()
        while self._order_timestamps and now - self._order_timestamps[0] > 60:
            self._order_timestamps.popleft()
        if len(self._order_timestamps) >= settings.max_orders_per_minute:
            return False, f"order throttle: {len(self._order_timestamps)} in 60s"
        return True, f"{len(self._order_timestamps)}/{settings.max_orders_per_minute} orders/min"

    def _inventory_ok(self, side: int) -> Tuple[bool, str]:
        if side == 1:
            ok = self.net_position < settings.max_position_qty
        else:
            ok = self.net_position > -settings.max_position_qty
        return ok, f"net_pos={self.net_position:+d} limit=±{settings.max_position_qty}"

    def _daily_loss_ok(self) -> Tuple[bool, str]:
        if self.halted:
            return False, "; ".join(self.halt_reasons)
        return True, f"realized=₹{self.realized_pnl:,.2f} limit=-₹{settings.max_daily_loss_rs:,.2f}"

    def _position_age_ok(self, mid: Optional[float]) -> Tuple[bool, str]:
        if self.net_position == 0 or self._inventory_open_ts is None:
            return True, "flat"
        if self._entry_price > 0 and mid and mid > 0:
            if self.net_position > 0:
                unrealized = (mid - self._entry_price) * settings.resolved_lot_size * self.net_position
            else:
                unrealized = (self._entry_price - mid) * settings.resolved_lot_size * abs(self.net_position)
            if unrealized <= -settings.max_loss_per_position_rs:
                return False, f"per-position stop: unrealized ₹{unrealized:,.2f}"
        age = time.time() - self._inventory_open_ts
        if age >= settings.max_position_age_sec:
            return False, f"position age {age:.0f}s >= {settings.max_position_age_sec:.0f}s"
        return True, f"age {age:.0f}s/{settings.max_position_age_sec:.0f}s"

    async def _margin_check(
        self, symbol: str, qty: int, side: int, price: float, product_type: str
    ) -> schema.MarginCheck:
        key = f"{symbol}|{qty}|{side}|{price:.2f}"
        cached = self._margin_lru.get(key)
        if cached is not None:
            return cached

        mc = schema.MarginCheck(
            symbol=symbol, qty=qty, side=side, limit_price=price,
            margin_avail=0.0, margin_required=0.0, margin_total=0.0,
            buffer_rs=settings.min_free_margin_buffer_rs, is_sufficient=True,
        )
        if not settings.check_margin_before_order or self._fyers is None:
            self._mark_constraint("margin_api", mc.is_sufficient, "disabled by config")
            return mc

        try:
            payload = {"data": [{
                "symbol": symbol, "qty": qty, "side": side, "type": 1,
                "productType": product_type,
                "limitPrice": round(float(price), 2),
                "stopLoss": 0.0, "stopPrice": 0.0, "takeProfit": 0.0,
            }]}
            if hasattr(self._fyers, "service") and hasattr(self._fyers.service, "post_call"):
                resp = self._fyers.service.post_call("/multiorder/margin", self._fyers.header, payload)
            else:
                import requests
                resp = requests.post(
                    "https://api-t1.fyers.in/api/v3/multiorder/margin",
                    headers={"Authorization": getattr(self._fyers, "header", "")},
                    json=payload, timeout=10,
                ).json()
            data = resp.get("data") or {}
            mc = schema.MarginCheck(
                symbol=symbol, qty=qty, side=side, limit_price=price,
                margin_avail=float(data.get("margin_avail", 0.0)),
                margin_required=float(data.get("margin_new_order", 0.0)),
                margin_total=float(data.get("margin_total", 0.0)),
                buffer_rs=settings.min_free_margin_buffer_rs,
                is_sufficient=float(data.get("margin_avail", 0.0))
                >= (float(data.get("margin_new_order", 0.0)) + settings.min_free_margin_buffer_rs),
                code=resp.get("code", 0), message=resp.get("message", ""),
            )
            self._last_margin_avail = mc.margin_avail
            self._last_margin_required = mc.margin_required
        except Exception as exc:
            mc = schema.MarginCheck(
                symbol=symbol, qty=qty, side=side, limit_price=price,
                margin_avail=0.0, margin_required=float("inf"),
                margin_total=float("inf"),
                buffer_rs=settings.min_free_margin_buffer_rs, is_sufficient=False,
                code=-1, message=str(exc),
            )
        self._margin_lru[key] = mc
        if len(self._margin_lru) > 256:
            self._margin_lru.pop(next(iter(self._margin_lru)))
        self._mark_constraint("margin", mc.is_sufficient, mc.message or f"req ₹{mc.margin_required:,.2f}")
        return mc

    # ------------------------------------------------------------------ pre-trade gate (strategy-facing)
    async def check(
        self,
        symbol: str,
        strategy: str,
        side: int,
        qty: int,
        price: float,
        mid: Optional[float] = None,
        product_type: str = "INTRADAY",
    ) -> schema.RiskVerdict:
        checks: List[schema.RiskConstraint] = []
        ok, detail = self._throttle_ok()
        checks.append(schema.RiskConstraint(name="throttle", healthy=ok, detail=detail))
        ok, detail = self._inventory_ok(side)
        checks.append(schema.RiskConstraint(name="inventory", healthy=ok, detail=detail))
        ok, detail = self._daily_loss_ok()
        checks.append(schema.RiskConstraint(name="daily_loss", healthy=ok, detail=detail))
        ok, detail = self._position_age_ok(mid)
        checks.append(schema.RiskConstraint(name="position_age", healthy=ok, detail=detail))

        from app.infra import market_hours

        ok = market_hours.is_open()
        checks.append(schema.RiskConstraint(
            name="market_hours", healthy=ok,
            detail=market_hours.session_label() if ok else "market session closed",
        ))
        self._mark_constraint("market_hours", ok, market_hours.session_label() if ok else "market session closed")

        margin = await self._margin_check(symbol, qty, side, price, product_type)
        checks.append(schema.RiskConstraint(
            name="margin", healthy=margin.is_sufficient,
            detail=f"req ₹{margin.margin_required:,.2f} avail ₹{margin.margin_avail:,.2f}",
        ))

        allowed = all(c.healthy for c in checks) and not self.halted
        reason = ""
        if not allowed:
            reason = next((c.name for c in checks if not c.healthy), "halted")

        verdict = schema.RiskVerdict(
            symbol=symbol, strategy=strategy, ts=time.time(), side=side,
            qty=qty, price=price, allowed=allowed, reason=reason, checks=checks,
        )
        self._mark()
        try:
            await self.obs.publish_verdict(verdict)
            await self.db.insert_risk_verdict(verdict)
        except Exception:
            pass
        return verdict

    # ------------------------------------------------------------------ post-trade (fill events from execution engine)
    async def on_fill(self, side: int, qty: int, price: float, ref_price: float):
        signed = qty if side == 1 else -qty
        multiplier = settings.resolved_lot_size
        prev_pos = self.net_position

        if prev_pos == 0 and signed != 0:
            self._inventory_open_ts = time.time()
            self._entry_price = price
        elif prev_pos != 0 and signed * prev_pos < 0:
            # closing/reducing an open position -> realize PnL on that part
            close_qty = min(abs(signed), abs(prev_pos))
            if close_qty > 0:
                direction = 1 if prev_pos > 0 else -1
                unit_pnl = (price - self._entry_price) * direction
                self.realized_pnl += round(unit_pnl * close_qty * multiplier, 2)

        new_pos = prev_pos + signed
        if new_pos == 0:
            self._inventory_open_ts = None
        elif prev_pos == 0 or signed * new_pos < 0:
            # position opened fresh or crossed zero: reset to this fill price
            self._entry_price = price
            if self._inventory_open_ts is None:
                self._inventory_open_ts = time.time()
        self.net_position = new_pos
        self._order_timestamps.append(time.time())

        if self.realized_pnl <= -abs(settings.max_daily_loss_rs):
            self.halt(f"daily loss limit: realized ₹{self.realized_pnl:,.2f}")
        self._mark_constraint("inventory", abs(self.net_position) <= settings.max_position_qty, f"pos={self.net_position:+d}")
        self._mark_constraint("daily_loss", not self.halted, f"realized ₹{self.realized_pnl:,.2f}")

    # ------------------------------------------------------------------ control
    def halt(self, reason: str):
        if not self.halted:
            self.halted = True
            self.halt_reasons.append(reason)
            self.status = "halted"
            self.detail = reason
            log.halt(reason)

    def reset(self):
        self.halted = False
        self.halt_reasons.clear()
        self.net_position = 0
        self.realized_pnl = 0.0
        self._inventory_open_ts = None
        self.status = "healthy"

    def _mark_constraint(self, name: str, healthy: bool, detail: str):
        self._constraints[name] = schema.RiskConstraint(name=name, healthy=healthy, detail=detail)

    # ------------------------------------------------------------------ market session awareness
    def update_market_state(self, is_open_now: bool):
        """Called by the Monitor engine so the session state shows up in the
        pre-trade gate and on the risk panel even when no tick is flowing."""
        if is_open_now:
            self._mark_constraint("market_hours", True, "session open")
        else:
            self._mark_constraint("market_hours", False, "session closed")

    async def run(self):
        """Heartbeat + subscribe to fill/order feed and reconcile inventory."""
        await self.attach()
        hb = asyncio.create_task(self._heartbeat_loop())
        while True:
            await self._process()
            self._mark()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                break
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue
        hb.cancel()

    async def _process(self):
        # every cycle: refresh margin-aware cash status (from last API call) and
        # reconcile inventory from broker positions if available.
        try:
            if self._fyers is not None:
                resp = self._fyers.positions()
                if resp.get("s") == "ok":
                    for pos in resp.get("netPositions", []):
                        if pos.get("symbol", "") == settings.symbol:
                            raw = pos.get("netQty", 0)
                            if settings.segment == "COMMODITY" and abs(raw) >= settings.resolved_lot_size:
                                net = int(round(raw / settings.resolved_lot_size))
                            else:
                                net = int(raw)
                            self.net_position = net
                            self.realized_pnl = float(pos.get("realized_profit", self.realized_pnl))
                            break
        except Exception:
            pass
        self._mark_constraint(
            "inventory", abs(self.net_position) <= settings.max_position_qty,
            f"net_pos={self.net_position:+d} limit=±{settings.max_position_qty}",
        )
        for name, c in self._constraints.items():
            if not c.healthy:
                self.status = "degraded"
                self.detail = f"constraint {name} failed"
                break
        else:
            if self.status != "halted":
                self.status = "healthy"
                self.detail = ""