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
import math
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from fyers_apiv3 import fyersModel

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.auth import TokenStore
from app.infra.db import Database
from app.infra.instrument import AssetType, shares_to_lots, instrument_registry
from app.infra.redis import RedisBus
from app.infra import logging as log
from app import sizing as sizing_mod


class RiskEngine(Engine):
    name = "risk_engine"

    def __init__(self, bus: RedisBus, db: Database, token_store: TokenStore, initial_token: str = ""):
        super().__init__(bus, db)
        self.token_store = token_store
        self.halted = False
        self.halt_reasons: List[str] = []
        # per-symbol book state (multi-asset friendly)
        self._positions: Dict[str, dict] = {}
        self._order_timestamps: deque[float] = deque()
        self._last_margin_avail = 0.0
        self._last_margin_required = 0.0
        # per-symbol account view used by the position sizer
        self._margin_avail_by_symbol: Dict[str, float] = {}
        self._margin_per_lot_by_symbol: Dict[str, float] = {}
        self._margin_refreshed_at: Dict[str, float] = {}
        self._last_mid_by_symbol: Dict[str, float] = {}
        self._constraints: dict[str, schema.RiskConstraint] = {}
        self._margin_lru: Dict[str, schema.MarginCheck] = {}
        self._fyers = None if not initial_token else self._build_client(initial_token)
        # Daily-loss baseline. Realized PnL is only meaningful *relative to this
        # anchor*: the broker's realized_profit already contains any losses
        # incurred before this process started, so halting on the absolute value
        # would trip the daily-loss gate on the first fill of a fresh boot (or
        # right after RESET). We anchor at boot and at every RESET.
        self._loss_anchor_rs = 0.0
        # Seed per-symbol realized PnL from the broker exactly ONCE per boot so
        # a mid-day RESET truly clears a daily-loss halt instead of the next
        # positions() reconciliation re-arming it from stale broker totals.
        self._seeded_realized = False
        # When the halt fired; drives auto-recovery (halt_cooldown_sec).
        self._halted_at = 0.0
        # Throttle the /positions reconcile (see `_process`); 0.0 pins the first
        # poll to the first cycle so boot reconciles immediately.
        self._last_positions_poll = 0.0
        # Global multi-asset margin budget. Every symbol the scanner selects as
        # quoteable books its quote margin here (mirror of the scanner's greedy
        # reservation). `margin_remaining(symbol)` then reports the broker avail
        # minus everything OTHER symbols have booked, so live per-symbol sizing
        # and the pre-trade gate both respect the aggregated budget — one name
        # can never silently swallow the margin another already reserved.
        self._booked_margin: Dict[str, float] = {}
        self._booked_total = 0.0

    # ------------------------------------------------------------------ symbol book state
    def _pos(self, symbol: str) -> dict:
        return self._positions.setdefault(symbol, {
            "net": 0, "realized": 0.0, "entry": 0.0, "open_ts": None,
        })

    def position(self, symbol: str) -> int:
        return self._pos(symbol)["net"]

    def realized_pnl_for(self, symbol: str) -> float:
        return self._pos(symbol)["realized"]

    def asset_pnl(self, symbol: str, mid: Optional[float] = None) -> schema.AssetPnl:
        """Per-symbol PnL + inventory for the ranked-asset detail view.

        Realized PnL is the net *spread collected* (already after charges).
        Unrealized is marked against the latest mid when a position is open.
        """
        p = self._pos(symbol)
        inst = self._instrument(symbol)
        pos = p["net"]
        mark = mid or self.mid_price(symbol)
        unrealized = 0.0
        if pos != 0 and p["entry"] > 0 and mark and mark > 0:
            unrealized = (mark - p["entry"]) * inst.lot_size * pos if pos > 0 \
                else (p["entry"] - mark) * inst.lot_size * abs(pos)
        total = p["realized"] + unrealized
        age = (time.time() - p["open_ts"]) if p["open_ts"] else 0.0
        return schema.AssetPnl(
            symbol=symbol,
            position=pos,
            entry=round(p["entry"], 4),
            realized_pnl_rs=round(p["realized"], 2),
            unrealized_pnl_rs=round(unrealized, 2),
            total_pnl_rs=round(total, 2),
            open_age_sec=round(max(0.0, age), 1),
            lot_size=inst.lot_size,
            last_fill_price=round(p.get("last_fill", 0.0), 4),
        )

    @property
    def net_position(self) -> int:
        """Primary-symbol net position (monitor/metrics back-compat)."""
        return self._pos(settings.symbol)["net"]

    @property
    def realized_pnl(self) -> float:
        """Aggregate realized PnL across every traded symbol."""
        return round(sum(p["realized"] for p in self._positions.values()), 2)

    @property
    def gross_position_qty(self) -> int:
        """Total absolute inventory across all symbols (portfolio exposure)."""
        return sum(abs(p["net"]) for p in self._positions.values())

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

    def _instrument(self, symbol: str):
        inst = instrument_registry.get(symbol)
        if inst is None:
            inst = instrument_registry.resolve(
                symbol,
                segment=settings.asset_type or settings.segment,
                lot_size=settings.lot_size,
                tick_size=settings.tick_size,
                margin_per_lot_rs=settings.margin_per_lot_rs,
            )
        return inst

    def margin_available(self, symbol: str) -> float:
        return self._margin_avail_by_symbol.get(symbol, self._last_margin_avail)

    # ------------------------------------------------------------------ margin ledger (multi-asset budget)
    def sync_margin_bookings(self, bookings: Dict[str, float]):
        """Replace the scanner's quote reservations per symbol.

        Called by the strategy engine after each scanner rebuild with the
        reservation each selected (quoteable) symbol consumes. Stored amounts
        are already reserve-multiplied, so the summed ledger stays consistent
        with the scanner's greedy budget.
        """
        self._booked_margin = {
            sym: float(v) for sym, v in (bookings or {}).items() if v > 0
        }
        self._booked_total = sum(self._booked_margin.values())

    def booked_margin_total(self) -> float:
        return self._booked_total

    def booked_margin_for(self, symbol: str) -> float:
        return self._booked_margin.get(symbol, 0.0)

    def margin_remaining(self, symbol: str) -> float:
        """Aggregated budget view: broker avail minus OTHER symbols' bookings
        minus the free-margin buffer.

        A symbol does not subtract its own reservation (that margin is already
        earmarked for it), so the greedy scanner budget and the live sizer stay
        consistent: the sum of the top-N reservations never exceeds the account
        margin, and every symbol only ever sizes within what is left over. When
        the ledger is disabled, or the broker figure is unknown (<= 0), this
        returns the raw avail (lenient — the per-order broker gate backstops).
        """
        if not settings.margin_ledger_enabled:
            return self.margin_available(symbol)
        raw = self.margin_available(symbol)
        if not math.isfinite(raw):
            return 0.0
        if raw <= 0:
            return raw
        # Deploy at most the configured utilisation target of the account, so
        # e.g. 0.95 keeps a 5% buffer free for margin spikes/slippage instead
        # of running the whole available balance to zero.
        raw *= settings.margin_utilization_target
        others = self._booked_total - self.booked_margin_for(symbol)
        return max(0.0, raw - others - settings.min_free_margin_buffer_rs)

    def margin_per_lot(self, symbol: str) -> float:
        """Real broker margin for ONE lot of ``symbol`` (rupees).

        Resolution order: per-symbol broker margin learned via
        ``refresh_margins`` / ``_margin_check`` -> configured per-instrument
        value -> a per-symbol estimate from the notional contract value at mid.
        NEVER another symbol's margin: the old ``next(iter(...))`` fallback
        leaked one symbol's figure to every other row, which is why every NFO
        future showed the same (wrong) margin-required in the scanner.
        """
        inst = self._instrument(symbol)
        known = self._margin_per_lot_by_symbol.get(symbol)
        if known is not None and math.isfinite(known):
            return known
        if inst.margin_per_lot_rs > 0:
            return inst.margin_per_lot_rs
        estimate = sizing_mod._estimate_margin_per_lot(self.mid_price(symbol), inst.lot_size)
        if estimate > 0:
            return estimate
        return 0.0

    def mid_price(self, symbol: str) -> Optional[float]:
        return self._last_mid_by_symbol.get(symbol)

    def suggest_quote_qty(
        self, symbol: str, mid: Optional[float] = None,
        vol_widening_ticks: int = 0,
        weight_mult: float = 1.0,
    ) -> Tuple[int, str]:
        """Ask the size policy how many lots/shares to quote for ``symbol``.

        Dynamic size = *aggregated remaining* margin x MARGIN_RISK_FRACTION /
        margin_per_lot, then reduced by volatility widening and clamped by the
        position cap minus current inventory. ``margin_remaining`` backs out the
        margin every OTHER selected symbol has already reserved (plus the free-
        margin buffer), so no symbol can size as if it owns the whole account.

        ``weight_mult`` scales the margin-derived size by the symbol's RoM
        weight (from the scanner) so higher cycle-profit-per-margin names quote
        more lots; it defaults to 1.0 (neutral).
        """
        inst = self._instrument(symbol)
        margin_avail = self.margin_remaining(symbol)
        margin_per_lot = self.margin_per_lot(symbol) or inst.margin_per_lot_rs
        return sizing_mod.quote_size_for(
            inst=inst,
            margin_avail=margin_avail,
            margin_fraction=settings.margin_risk_fraction,
            margin_per_lot=margin_per_lot,
            inventory=self.position(symbol),
            max_position_qty=settings.max_position_qty,
            mid=mid or self.mid_price(symbol),
            vol_widening_ticks=vol_widening_ticks,
            vol_reduction_per_tick=settings.vol_size_reduction_per_tick,
            weight_mult=weight_mult,
        )

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

    def _inventory_ok(self, symbol: str, side: int) -> Tuple[bool, str]:
        pos = self.position(symbol)
        if side == 1:
            ok = pos < settings.max_position_qty
        else:
            ok = pos > -settings.max_position_qty
        return ok, f"{symbol} net_pos={pos:+d} limit=±{settings.max_position_qty}"

    def _daily_loss_ok(self) -> Tuple[bool, str]:
        # Daily loss is measured RELATIVE to the anchor (boot / last RESET), so
        # losses the account carried before this run (broker realized_profit)
        # never trip the gate, and a RESET gives a genuinely clean slate.
        loss_since_anchor = self._loss_anchor_rs - self.realized_pnl
        if self.halted:
            return False, "; ".join(self.halt_reasons)
        if loss_since_anchor >= settings.max_daily_loss_rs:
            return False, (
                f"daily loss ₹{loss_since_anchor:,.2f} "
                f">= limit ₹{settings.max_daily_loss_rs:,.2f}"
            )
        return True, (
            f"since anchor ₹{self.realized_pnl - self._loss_anchor_rs:,.2f} "
            f"limit -₹{settings.max_daily_loss_rs:,.2f}"
        )

    def _position_age_ok(self, symbol: str, mid: Optional[float]) -> Tuple[bool, str]:
        pos = self.position(symbol)
        p = self._pos(symbol)
        if pos == 0 or p["open_ts"] is None:
            return True, "flat"
        lot = self._instrument(symbol).lot_size
        if p["entry"] > 0 and mid and mid > 0:
            if pos > 0:
                unrealized = (mid - p["entry"]) * lot * pos
            else:
                unrealized = (p["entry"] - mid) * lot * abs(pos)
            if unrealized <= -settings.max_loss_per_position_rs:
                return False, f"{symbol} per-position stop: unrealized ₹{unrealized:,.2f}"
        age = time.time() - p["open_ts"]
        if age >= settings.max_position_age_sec:
            return False, f"{symbol} position age {age:.0f}s >= {settings.max_position_age_sec:.0f}s"
        return True, f"{symbol} age {age:.0f}s/{settings.max_position_age_sec:.0f}s"

    def _margin_post(self, payload: dict) -> dict:
        """POST one margin basket to the broker margin calculator."""
        if hasattr(self._fyers, "service") and hasattr(self._fyers.service, "post_call"):
            return self._fyers.service.post_call("/multiorder/margin", self._fyers.header, payload)
        import requests
        return requests.post(
            "https://api-t1.fyers.in/api/v3/multiorder/margin",
            headers={"Authorization": str(getattr(self._fyers, "header", ""))},
            json=payload, timeout=10,
        ).json()

    def _order_qty(self, symbol: str, qty: int, inst: Optional[object] = None) -> int:
        """FYERS ``qty`` for an order/margin quote depends on the segment:

          * NFO equity futures -> underlying **shares** (the AVL SDK enforces
            ``qty % lot_size == 0``; e.g. SBIN24JUNFUT qty=750 for one lot).
          * MCX commodities    -> **lots** directly (the broker master carries
            ``minLotSize=1`` and the contract weight lives in ``qtyMultiplier``;
            a 1-lot aluminium order is qty=1, NOT 5000). Multiplying by the
            multiplier here inflates margin/order size ~lot_size-fold.
          * cash equity        -> shares.

        Lot-based strategy quantities are converted here so the margin gate and
        the live order agree with the broker's own per-segment semantics."""
        inst = inst or self._instrument(symbol)
        if getattr(inst, "quote_in_lots", False):
            if getattr(inst, "asset_type", None) == AssetType.COMMODITY_FUT:
                return int(qty)
            return int(qty) * max(1, int(getattr(inst, "lot_size", 1)))
        return int(qty)

    async def refresh_margins(
        self, symbols, prices: Optional[Dict[str, float]] = None
    ) -> Tuple[int, int]:
        """Fetch REAL broker per-lot margin for up to ``margin_refresh_batch``
        symbols via ``/multiorder/margin`` (one request per symbol — the
        endpoint only returns basket totals, so a per-symbol figure needs its
        own call).

        Returns ``(checked, ok)``. Each symbol is re-queried at most once per
        ``settings.margin_refresh_sec``; across cycles the whole universe
        warms up in rank order (caller passes ranked symbols). The broker's
        ``margin_total`` for a single 1-lot request is stored as the per-lot
        margin, so scanner rows show the broker's figure.

        IMPORTANT: ``margin_new_order`` must NOT be used here. The broker
        returns it as the PROJECTED account margin after adding the order
        (existing/parked margin + this order's margin), so a client who holds
        unrelated positions gets an inflated figure. ``margin_total`` is the
        marginal margin required for THIS order and scales linearly with qty.
        """
        if not symbols or self._fyers is None:
            return (0, 0)
        await self.attach()
        if self._fyers is None or not settings.check_margin_before_order:
            return (0, 0)
        now = time.time()
        due = [
            s for s in symbols
            if now - self._margin_refreshed_at.get(s, 0.0) >= settings.margin_refresh_sec
        ]
        checked = ok = 0
        for sym in due[: max(1, int(settings.margin_refresh_batch))]:
            inst = self._instrument(sym)
            price = (prices or {}).get(sym) or self.mid_price(sym)
            if not price or price <= 0:
                continue
            try:
                resp = await asyncio.wait_for(asyncio.to_thread(self._margin_post, {"data": [{
                    "symbol": sym, "qty": self._order_qty(sym, 1, inst),
                    "side": 1, "type": 1, "productType": settings.product_type,
                    "limitPrice": round(float(price), 2),
                    "stopLoss": 0.0, "stopPrice": 0.0, "takeProfit": 0.0,
                }]}), timeout=settings.margin_api_timeout_sec)
                checked += 1
                code = resp.get("code", 0)
                # Broker rate-limit (HTTP 429 / -429): back off this symbol for
                # several refresh windows instead of hammering the endpoint on
                # every cycle. The 15k -429 margin responses seen in the live
                # log were each symbol retrying instantly after a stall.
                if code == -429 or "429" in str(code):
                    self._margin_refreshed_at[sym] = now + 5 * settings.margin_refresh_sec
                    continue
                data = resp.get("data") or {}
                self._margin_refreshed_at[sym] = now
                margin = float(data.get("margin_total", 0.0) or 0.0)
                avail = float(data.get("margin_avail", 0.0) or 0.0)
                margin = margin if math.isfinite(margin) else 0.0
                avail = avail if math.isfinite(avail) else 0.0
                if margin > 0:
                    self._margin_per_lot_by_symbol[sym] = margin
                    self._margin_avail_by_symbol[sym] = avail
                    self._last_margin_avail = avail
                    self._last_margin_required = margin
                    instrument_registry.update_margin(sym, margin)
                    ok += 1
            except asyncio.TimeoutError:
                # A wedged broker socket must back off like -429 rather than
                # retry instantly every refresh cycle.
                self._margin_refreshed_at[sym] = now + 5 * settings.margin_refresh_sec
                log.warn(f"[risk] margin refresh {sym}: timed out ({settings.margin_api_timeout_sec:.0f}s) — backing off")
            except Exception as exc:
                log.warn(f"[risk] margin refresh {sym}: {exc!r}")
        return (checked, ok)

    async def _margin_check(
        self, symbol: str, qty: int, side: int, price: float, product_type: str
    ) -> schema.MarginCheck:
        key = f"{symbol}|{qty}|{side}|{price:.2f}|{product_type}"
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
            inst = self._instrument(symbol)
            order_qty = self._order_qty(symbol, qty, inst)
            payload = {"data": [{
                "symbol": symbol, "qty": order_qty, "side": side, "type": 1,
                "productType": product_type,
                "limitPrice": round(float(price), 2),
                "stopLoss": 0.0, "stopPrice": 0.0, "takeProfit": 0.0,
            }]}
            resp = await asyncio.wait_for(
                asyncio.to_thread(self._margin_post, payload),
                timeout=settings.margin_api_timeout_sec,
            )
            data = resp.get("data") or {}
            mc = schema.MarginCheck(
                symbol=symbol, qty=qty, side=side, limit_price=price,
                margin_avail=float(data.get("margin_avail", 0.0)),
                margin_required=float(data.get("margin_total", 0.0)),
                margin_total=float(data.get("margin_total", 0.0)),
                buffer_rs=settings.min_free_margin_buffer_rs,
                is_sufficient=float(data.get("margin_avail", 0.0))
                >= (float(data.get("margin_total", 0.0)) + settings.min_free_margin_buffer_rs),
                code=resp.get("code", 0), message=resp.get("message", ""),
            )
            self._last_margin_avail = mc.margin_avail
            self._last_margin_required = mc.margin_required
            self._margin_avail_by_symbol[symbol] = mc.margin_avail
            if order_qty > 0 and mc.margin_required > 0:
                lots = int(qty) if getattr(inst, "quote_in_lots", False) else max(1, int(qty))
                self._margin_per_lot_by_symbol[symbol] = mc.margin_required / lots
                instrument_registry.update_margin(symbol, mc.margin_required / lots)
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
        self._auto_recover()
        if mid is not None:
            self._last_mid_by_symbol[symbol] = mid
        checks: List[schema.RiskConstraint] = []
        ok, detail = self._throttle_ok()
        checks.append(schema.RiskConstraint(name="throttle", healthy=ok, detail=detail))
        ok, detail = self._inventory_ok(symbol, side)
        checks.append(schema.RiskConstraint(name="inventory", healthy=ok, detail=detail))
        ok, detail = self._daily_loss_ok()
        checks.append(schema.RiskConstraint(name="daily_loss", healthy=ok, detail=detail))
        ok, detail = self._position_age_ok(symbol, mid)
        checks.append(schema.RiskConstraint(name="position_age", healthy=ok, detail=detail))

        from app.infra import market_hours

        seg = self._instrument(symbol).segment
        ok = market_hours.is_open(seg) and not market_hours.in_winddown(seg)
        label = market_hours.session_label(seg)
        checks.append(schema.RiskConstraint(
            name="market_hours", healthy=ok,
            detail=label if ok else "segment closed/wind-down",
        ))
        self._mark_constraint("market_hours", ok, label if ok else "segment closed/wind-down")

        # Fast path: while the engine is halted there is no point spending a
        # margin API call (or the pattern matching below) on a verdict that is
        # already decided — return the halt immediately.
        if self.halted:
            reason = "; ".join(self.halt_reasons) or "halted"
            verdict = schema.RiskVerdict(
                symbol=symbol, strategy=strategy, ts=time.time(), side=side,
                qty=qty, price=price, allowed=False, reason=reason, checks=checks,
            )
            self._mark_constraint("daily_loss", False, reason)
            self._mark()
            try:
                await self.obs.publish_verdict(verdict)
            except Exception:
                pass
            return verdict

        margin = await self._margin_check(symbol, qty, side, price, product_type)
        checks.append(schema.RiskConstraint(
            name="margin", healthy=margin.is_sufficient,
            detail=f"req ₹{margin.margin_required:,.2f} avail ₹{margin.margin_avail:,.2f}",
        ))

        # Global multi-asset margin budget: this order must fit inside the
        # margin that remains after OTHER selected symbols have reserved theirs.
        # The broker margin API is the per-order truth; this gate is the
        # cross-symbol prevention (a symbol may not exceed its remaining share).
        budget_ok = True
        budget_detail = "budget: pre-trade margin gate"
        if settings.margin_ledger_enabled:
            inst = self._instrument(symbol)
            per_lot = self.margin_per_lot(symbol) or inst.margin_per_lot_rs
            raw = self.margin_available(symbol)
            remaining = self.margin_remaining(symbol)
            mult = 2.0 if settings.margin_reserve_both_sides else 1.0
            # margin_remaining() already nets the free-margin buffer, so the
            # order charge must NOT subtract it again or the buffer is counted
            # per position as well as once globally.
            charge = max(0.0, qty * per_lot) * mult
            if raw > 0 and charge > 0 and charge > remaining:
                budget_ok = False
            budget_detail = (
                f"remaining ₹{remaining:,.0f} after ₹{self._booked_total - self.booked_margin_for(symbol):,.0f} booked by others"
                if remaining > 0 else
                f"remaining ₹{remaining:,.0f} (broker margin not yet reported)"
            )
        checks.append(schema.RiskConstraint(
            name="margin_budget", healthy=budget_ok, detail=budget_detail,
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
    async def on_fill(self, side: int, qty: int, price: float, ref_price: float, symbol: str = ""):
        sym = symbol or settings.symbol
        p = self._pos(sym)
        inst = self._instrument(sym)
        multiplier = inst.lot_size
        raw = qty if side == 1 else -qty
        signed = raw
        if inst.quote_in_lots and inst.asset_type != AssetType.COMMODITY_FUT:
            # Equity futures report fills in UNDERLYING SHARES; the position
            # book and every gate below use LOTS (same rule as the 15s broker
            # reconciliation in _process). Flooring (shares_to_lots) never
            # rounds a sub-lot up: a partial 400-share fill must not book as
            # "400 lots" (trips the inventory gate) nor as a rounded 1 lot
            # (inflates realized PnL). Without this a 1-lot NFO fill would land
            # as qty=750 etc.
            signed = shares_to_lots(raw, inst.lot_size)
        prev_pos = p["net"]
        realized_before = p["realized"]

        if prev_pos == 0 and signed != 0:
            p["open_ts"] = time.time()
            p["entry"] = price
        elif prev_pos != 0 and signed * prev_pos < 0:
            # closing/reducing an open position -> realize PnL on that part
            close_qty = min(abs(signed), abs(prev_pos))
            if close_qty > 0:
                direction = 1 if prev_pos > 0 else -1
                unit_pnl = (price - p["entry"]) * direction
                p["realized"] = round(p["realized"] + unit_pnl * close_qty * multiplier, 2)

        new_pos = prev_pos + signed
        if new_pos == 0:
            p["open_ts"] = None
        elif prev_pos == 0 or signed * new_pos < 0:
            # position opened fresh or crossed zero: reset to this fill price
            p["entry"] = price
            if p["open_ts"] is None:
                p["open_ts"] = time.time()
        p["net"] = new_pos
        p["last_fill"] = price
        self._order_timestamps.append(time.time())

        # The fill converts a reserved quote into a real (broker-reported)
        # position, freeing the symbol's scanner reservation so the remaining
        # budget is truthful for the OTHER names this cycle. The scanner rebases
        # the booking from the new position within a refresh anyway.
        if sym in self._booked_margin:
            del self._booked_margin[sym]
            self._booked_total = sum(self._booked_margin.values())

        log.trade(
            f"[risk] fill {sym} {'BUY' if side == 1 else 'SELL'} {qty}@{price:.2f} "
            f"lot={multiplier} -> pos {prev_pos:+d}>{new_pos:+d} "
            f"realized ₹{realized_before:,.2f}>{p['realized']:,.2f}"
        )

        if self._loss_anchor_rs - self.realized_pnl >= abs(settings.max_daily_loss_rs):
            self.halt(
                f"daily loss limit: ₹{self.realized_pnl - self._loss_anchor_rs:,.2f} "
                f"since anchor"
            )
        self._mark_constraint(
            "inventory", abs(new_pos) <= settings.max_position_qty, f"{sym} pos={new_pos:+d}"
        )
        self._mark_constraint(
            "daily_loss", not self.halted,
            f"since anchor ₹{self.realized_pnl - self._loss_anchor_rs:,.2f}",
        )
        return new_pos

    # ------------------------------------------------------------------ control
    def halt(self, reason: str):
        if not self.halted:
            self.halted = True
            self.halt_reasons.append(reason)
            self._halted_at = time.time()
            self.status = "halted"
            self.detail = reason
            log.halt(reason)

    def _auto_recover(self):
        """Time-based halt recovery: a daily-loss / engine halt auto-clears
        after ``settings.halt_cooldown_sec`` so the bot isn't dead for the rest
        of the day. Recovery never skips the other gates (market-hours, reject
        circuit, inventory) — those still veto every order, and the position
        remains whatever the broker holds. Trades nothing itself."""
        if (
            self.halted
            and settings.halt_cooldown_sec > 0
            and self._halted_at > 0
            and time.time() - self._halted_at >= settings.halt_cooldown_sec
        ):
            waited = time.time() - self._halted_at
            log.warn(
                f"[risk] halt cooldown elapsed ({waited:.0f}s) — auto-recovering; "
                f"use RESET to fully reset the book anchors"
            )
            self.halted = False
            self.halt_reasons.clear()
            self.status = "healthy"
            self.detail = "auto-recovered after halt cooldown"

    def reset(self):
        self.halted = False
        self.halt_reasons.clear()
        self._positions.clear()
        self._halted_at = 0.0
        self._loss_anchor_rs = self.realized_pnl
        self._booked_margin.clear()
        self._booked_total = 0.0
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

        # Keep the per-symbol mid cache fresh from the live market feed so
        # margin estimation (and thus the scanner "Mgn ₹" column) is never
        # blank while a symbol has a price — even before any order is placed.
        async def market_handler(ch: str, raw: bytes):
            try:
                tick = schema.decode(schema.MarketTick, raw)
                mid = tick.ltp
                if tick.bid and tick.ask:
                    mid = (tick.bid + tick.ask) / 2.0
                if mid is not None:
                    self._last_mid_by_symbol[tick.symbol] = mid
            except Exception:
                pass
        await self.bus.subscribe("fyers:market:*", handler=market_handler)

        hb = asyncio.create_task(self._heartbeat_loop())
        while True:
            self._auto_recover()
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
        # reconcile inventory from broker positions for every symbol we track.
        # The /positions poll is throttled — the order WebSocket already drives
        # inventory in real time; this REST call is only a manual-trade /
        # broker-reconcile safety net, so hammering it every second is waste.
        interval = settings.positions_poll_interval_sec
        try:
            now = time.time()
            if self._fyers is not None and interval > 0 and (
                now - self._last_positions_poll >= interval
            ):
                self._last_positions_poll = now
                resp = await asyncio.wait_for(
                    asyncio.to_thread(self._fyers.positions),
                    timeout=settings.positions_api_timeout_sec,
                )
                if resp.get("s") == "ok":
                    broker_net = {
                        p.get("symbol", ""): p
                        for p in resp.get("netPositions", [])
                    }
                    symbols = list(self._positions) or [settings.symbol]
                    for symbol in symbols:
                        inst = self._instrument(symbol)
                        pos = broker_net.get(symbol)
                        if not pos:
                            continue
                        raw = int(pos.get("netQty", 0) or 0)
                        if inst.quote_in_lots and inst.asset_type != AssetType.COMMODITY_FUT:
                            # Broker net positions for NFO equity futures are in
                            # underlying SHARES; always divide down to whole lots
                            # (floor) so a sub-lot remainder is never mistaken
                            # for a huge lot count. Mirrors on_fill.
                            net = shares_to_lots(raw, inst.lot_size)
                        else:
                            net = raw
                        self._pos(symbol)["net"] = net
                        # Seed realized PnL from the broker exactly once per
                        # boot. Re-syncing it every cycle would resurrect a
                        # pre-existing daily loss after every RESET and re-arm
                        # the daily-loss halt from stale totals.
                        if not self._seeded_realized:
                            self._pos(symbol)["realized"] = float(
                                pos.get("realized_profit", 0.0) or 0.0
                            )
                    if not self._seeded_realized and self._positions:
                        self._seeded_realized = True
                        # Baseline the daily-loss gate against the account's
                        # carried PnL, so only NEW losses since boot count.
                        self._loss_anchor_rs = self.realized_pnl
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