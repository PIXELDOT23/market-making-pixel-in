"""
app/strategies/market_maker.py
------------------------------
The market-making strategy (original bot logic re-architected on the engine
layers). It:

  * waits for a fresh market snapshot
  * asks the CostEngine for the optimum quote pair
  * applies volatility widening via the latest SignalMetrics
  * asks the RiskEngine for a pre-trade verdict
  * asks the ExecutionEngine to place / cancel / replace resting quotes

Quantities/spread are configurable via params, defaulting to the settings.
"""

from __future__ import annotations

from typing import Dict, Any, Optional

from app.config import settings
from app.infra import logging as log
from app.infra.instrument import instrument_registry
from app.strategies.base import BaseStrategy


class MarketMakerStrategy(BaseStrategy):
    def __init__(self, name: str, symbol: str, segment: str, params: Optional[Dict[str, Any]] = None):
        super().__init__(name, symbol, segment, params)
        self.qty = int(self.params.get("qty", settings.quote_qty))
        self.spread_ticks = int(self.params.get("spread_ticks", settings.spread_ticks))
        self.min_profit_margin_ticks = int(self.params.get("min_profit_margin_ticks", settings.min_profit_margin_ticks))

        inst = instrument_registry.resolve(
            symbol, segment=params.get("asset_type", settings.asset_type) if params else settings.asset_type,
            lot_size=params.get("lot_size", settings.lot_size) if params else settings.lot_size,
            tick_size=params.get("tick_size", settings.tick_size) if params else settings.tick_size,
            margin_per_lot_rs=params.get("margin_per_lot_rs", settings.margin_per_lot_rs) if params else settings.margin_per_lot_rs,
        )
        self.instrument = inst

        # resting quote state for this strategy
        self.active_buy_id: Optional[str] = None
        self.active_sell_id: Optional[str] = None
        self.active_buy_price = 0.0
        self.active_sell_price = 0.0
        self.paused = False

    # ------------------------------------------------------------------ decision
    async def on_signal(self, snapshot, signal, cost, engines) -> str:
        if not self.enabled or self.paused or not snapshot.is_connected or snapshot.mid is None:
            return "HOLD"

        risk = engines["risk"]
        execu = engines["execution"]
        cost_engine = engines["cost"]
        scanner = engines.get("scanner")

        # Risk is halted (kill switch / daily loss): stand the whole loop down
        # without even attempting a place (each attempt re-runs risk.check +
        # logs a blocked verdict for every symbol, every decision cycle).
        if risk is not None and risk.halted:
            return "HOLD_RISK_HALT"

        # Reject circuit-breaker: the broker hard-rejected this symbol recently
        # (e.g. order placement not whitelisted). Stand it down for the cooldown
        # window instead of re-submitting every decision cycle into the same
        # rejection — that both avoids flooding the broker and keeps the
        # margin-check REST calls (each place_limit runs one) from piling up.
        if execu is not None and execu.reject_cooldown(self.symbol) > 0:
            return "HOLD_REJECT"

        # Scanner gate: only the top-N ranked assets may rest quotes. Drop out of
        # ranking -> cancel anything on the book and stand down until re-ranked.
        if scanner is not None and not scanner.is_quoteable(self.symbol):
            rank = scanner.rank_of(self.symbol)
            if self.active_buy_id or self.active_sell_id:
                await self._cancel_pair(execu)
                log.warn(f"[mm:{self.symbol}] dropped from active scanner rank {rank} -> cancelling quotes")
            return "HOLD_RANK"

        # Dynamic positional size from the size policy (margin + inventory +
        # mid + volatility). Commodity futures are also margin-aware: the sizer
        # returns 0 lots when the account can't cover even one lot. The RoM
        # weight from the scanner scales equity sizing toward the most
        # profitable-per-margin names (commodity sizing stays margin-capped
        # regardless of weight).
        wmult = scanner.size_mult_of(self.symbol) if scanner is not None else 1.0
        qty, size_reason = risk.suggest_quote_qty(
            self.symbol, mid=snapshot.mid,
            vol_widening_ticks=signal.vol_widening_ticks,
            weight_mult=wmult,
        )
        if int(qty or 0) <= 0:
            if self.active_buy_id or self.active_sell_id:
                await self._cancel_pair(execu)
            await self._reflect_state(execu, 0, status="closed")
            log.warn(f"[mm:{self.symbol}] HOLD_MARGIN: {size_reason}")
            return "HOLD_MARGIN"
        qty = max(1, int(qty))

        from app.infra import market_hours
        seg = self.instrument.segment

        # 0) session gate — per segment: when a segment's session is closed, or
        #    has entered its pre-close wind-down window (e.g. NSE at 15:15),
        #    the book stands down while other segments (MCX runs into the
        #    night) keep quoting. Never leave orders resting outside the
        #    segment's quote window, and never open one inside the wind-down.
        if not market_hours.is_open(seg) or market_hours.in_winddown(seg):
            if self.active_buy_id or self.active_sell_id:
                await self._cancel_pair(execu)
            await self._reflect_state(execu, qty, status="closed")
            return "HOLD_MARKET_CLOSED"

        # 1) vol-aware spread
        base_spread = cost.required_spread_ticks
        vol_ticks = signal.vol_widening_ticks
        spread = min(base_spread + vol_ticks, settings.max_spread_widen_ticks)
        spread = max(spread, cost.breakeven_spread_ticks + self.min_profit_margin_ticks)

        # 2) quote pair through cost engine
        tick_size = self.instrument.tick_size
        mid = snapshot.mid
        half = (spread * tick_size) / 2.0
        target_bid = round(round((mid - half) / tick_size) * tick_size, 2)
        target_ask = round(round((mid + half) / tick_size) * tick_size, 2)

        # 3) inventory-aware quoting. Inventory skew: instead of dropping the
        # "adding" side outright (except at the hard cap), shift the whole pair
        # toward flattening — long -> pair skews down, short -> pair skews up —
        # so the book keeps earning while making entries less attractive and
        # exits more attractive. The increasing side is cancelled only when the
        # position is already at max_position_qty (that side would be rejected
        # by the inventory risk gate anyway).
        inv = risk.position(self.symbol)
        cap = int(settings.max_position_qty)
        tolerance = settings.requote_tolerance_ticks * tick_size
        # Order-book-imbalance guard: when depth is lopsided >= obi_gate_ratio,
        # quoting the "hot" side (the side matching the heavy half) chases the
        # flood and gets filled adversely. Refuse that side; keep the other.
        obi_hot_side: Optional[int] = None
        if settings.obi_gate_enabled and settings.obi_gate_ratio > 1.0:
            if snapshot.bid_size > 0 and snapshot.ask_size > 0:
                hi, lo = max(snapshot.bid_size, snapshot.ask_size), min(snapshot.bid_size, snapshot.ask_size)
                if hi >= settings.obi_gate_ratio * lo:
                    obi_hot_side = 1 if snapshot.bid_size > snapshot.ask_size else -1
                    log.warn(
                        f"[mm:{self.symbol}] OBI gate: depth lopsided "
                        f"{max(snapshot.bid_size, snapshot.ask_size)}v{min(snapshot.bid_size, snapshot.ask_size)} "
                        f"(>{settings.obi_gate_ratio:.1f}x) — suppressing "
                        f"{'BUY' if obi_hot_side == 1 else 'SELL'} side"
                    )
        skew = 0.0
        if settings.inventory_skew_quoting and inv != 0:
            skew = min(
                abs(inv) * settings.inventory_skew_ticks_per_lot,
                settings.max_spread_widen_ticks,
            ) * tick_size
        if inv > 0:
            target_bid -= skew
            target_ask -= skew
        elif inv < 0:
            target_bid += skew
            target_ask += skew

        net = cost_engine.round_trip_net_profit(
            target_bid, target_ask, qty, 1,
            settings.product_type, self.segment,
            self.instrument.lot_size, settings.brokerage_per_order,
            exchange=str(self.symbol).split(":", 1)[0].upper(),
        )
        quote_ok = net >= settings.min_net_profit_per_cycle_rs
        if not quote_ok:
            # cancel any resting quote pair
            if self.active_buy_id or self.active_sell_id:
                await self._cancel_pair(execu)
            log.warn(
                f"[mm:{self.symbol}] skip: net ₹{net:.2f} < min ₹{settings.min_net_profit_per_cycle_rs:.2f} "
                f"(spread {spread}t mid {mid:.4f} tick {tick_size} skew {skew:.4f})"
            )
            return "SKIP"

        if inv == 0:
            want_buy = obi_hot_side != 1
            want_sell = obi_hot_side != -1
        elif inv > 0:
            want_buy = abs(inv) < cap and obi_hot_side != 1
            want_sell = obi_hot_side != -1
        else:
            want_buy = obi_hot_side != 1
            want_sell = abs(inv) < cap and obi_hot_side != -1

        if not want_buy and self.active_buy_id:
            await self._cancel_side(execu, 1)
        if not want_sell and self.active_sell_id:
            await self._cancel_side(execu, -1)

        buy_verb = sell_verb = None
        if want_buy:
            buy_verb = await self._sync_side(execu, 1, target_bid, qty, tolerance)
        if want_sell:
            sell_verb = await self._sync_side(execu, -1, target_ask, qty, tolerance)

        if buy_verb == "blocked" or sell_verb == "blocked":
            log.warn(
                f"[mm:{self.symbol}] order-idempotency hold — one side could not be "
                f"converged safely this cycle (unconfirmed cancel/live order); "
                f"will retry next decision cycle"
            )

        if inv == 0:
            decision = "OPEN_BID" if buy_verb == "placed" else (
                "OPEN_ASK" if sell_verb == "placed" else "QUOTE"
            )
        elif inv > 0:
            decision = "EXIT_SELL" if sell_verb == "placed" else "QUOTE"
        else:
            decision = "EXIT_BUY" if buy_verb == "placed" else "QUOTE"

        await self._reflect_state(execu, qty, status="active")
        if decision in ("OPEN_BID", "OPEN_ASK", "EXIT_SELL", "EXIT_BUY") \
                or buy_verb == "modified" or sell_verb == "modified":
            log.trade(
                f"[mm:{self.symbol}] {decision} qty={qty} "
                f"bid={self.active_buy_price:.4f} ask={self.active_sell_price:.4f} "
                f"inv={inv:+d} spread={spread}t mid={mid:.4f} skew={skew:.4f}"
            )
        return decision

    # ------------------------------------------------------------------ idempotent book-keeping
    def _side_ids(self, side: int):
        return (self.active_buy_id, self.active_buy_price) if side == 1 \
            else (self.active_sell_id, self.active_sell_price)

    def _set_side(self, side: int, oid: Optional[str], price: float):
        if side == 1:
            self.active_buy_id = oid
            self.active_buy_price = price
        else:
            self.active_sell_id = oid
            self.active_sell_price = price

    def _forget_side(self, side: int):
        self._set_side(side, None, 0.0)

    async def _reflect_state(self, execu, qty: int, status: str = "active"):
        await execu.set_quote_state(
            self.name, self.symbol, self.active_buy_id, self.active_sell_id,
            self.active_buy_price, self.active_sell_price, qty, status=status,
        )

    async def _cancel_side(self, execu, side: int) -> bool:
        """Cancel + forget one side idempotently: the id is only forgotten once
        the order feed has confirmed it dead, or the broker cancel succeeded.
        Never clears the book-keeping while the order may still be live."""
        oid, _ = self._side_ids(side)
        if not oid:
            return True
        if execu.is_order_known_dead(oid):
            self._forget_side(side)
            return True
        if await execu.cancel_order(oid):
            self._forget_side(side)
            self.quotes_cancelled += 1
            return True
        log.warn(
            f"[mm:{self.symbol}] cancel unconfirmed for "
            f"{'BUY' if side == 1 else 'SELL'} {oid} — keeping id, retry next cycle"
        )
        return False

    async def _cancel_pair(self, execu) -> bool:
        buy_ok = await self._cancel_side(execu, 1)
        sell_ok = await self._cancel_side(execu, -1)
        return buy_ok and sell_ok

    async def _sync_side(self, execu, side: int, target_price: float, qty: int, tolerance: float) -> str:
        """Idempotently converge the resting order on ``side`` to `target_price`.

        Returns one of:
            "placed"   — a new order now rests at target (or a dead id was
                         replaced with a fresh one)
            "modified" — the existing RESTING order was re-priced in place
            "kept"     — the existing order is already within tolerance
            "blocked"  — could NOT converge safely this cycle (order may still
                         be live on the broker / cancel unconfirmed / place
                         rejected); caller must NOT place a new order on this
                         side and should retry next decision cycle
        """
        oid, cur = self._side_ids(side)

        if oid is None:
            new_id = await execu.place_limit(self.name, self.symbol, qty, side, target_price)
            if new_id:
                self._set_side(side, new_id, target_price)
                self.quotes_placed += 1
                return "placed"
            return "blocked"

        ev = execu.tracked_order(oid)
        if ev is None:
            # Never seen on the order feed: cannot confirm it is gone, so never
            # stack a second order on a possibly-live broker order.
            log.warn(
                f"[mm:{self.symbol}] {'BUY' if side == 1 else 'SELL'} {oid} unseen on "
                f"the order feed — not placing on that side this cycle"
            )
            return "blocked"

        if execu.is_order_known_dead(oid):
            log.info(f"[mm:{self.symbol}] forgot {ev.status_label} order {oid}")
            self._forget_side(side)
            new_id = await execu.place_limit(self.name, self.symbol, qty, side, target_price)
            if new_id:
                self._set_side(side, new_id, target_price)
                self.quotes_placed += 1
                return "placed"
            return "blocked"

        # Still OPEN on the broker.
        if abs(target_price - cur) < tolerance:
            return "kept"

        if execu.order_live_and_unfilled(oid):
            # clean resting order: re-price in place (one broker round-trip)
            if await execu.replace_order(oid, target_price, qty, self.symbol, side=side):
                self._set_side(side, oid, target_price)
                return "modified"
            # modify failed -> fall through to a verified cancel + replace

        if not await execu.cancel_order(oid):
            log.warn(
                f"[mm:{self.symbol}] cancel failed for "
                f"{'BUY' if side == 1 else 'SELL'} {oid} — standing side down "
                f"this cycle (no double quote)"
            )
            return "blocked"
        self._forget_side(side)
        self.quotes_cancelled += 1
        new_id = await execu.place_limit(self.name, self.symbol, qty, side, target_price)
        if new_id:
            self._set_side(side, new_id, target_price)
            self.quotes_placed += 1
            return "placed"
        return "blocked"