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

import time
from typing import Dict, Any, Optional, Tuple

from app import schema
from app.config import settings
from app.strategies.base import BaseStrategy


class MarketMakerStrategy(BaseStrategy):
    def __init__(self, name: str, symbol: str, segment: str, params: Optional[Dict[str, Any]] = None):
        super().__init__(name, symbol, segment, params)
        self.qty = int(self.params.get("qty", settings.quote_qty))
        self.spread_ticks = int(self.params.get("spread_ticks", settings.spread_ticks))
        self.min_profit_margin_ticks = int(self.params.get("min_profit_margin_ticks", settings.min_profit_margin_ticks))

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
        data = engines["data"]
        cost_engine = engines["cost"]

        from app.infra import market_hours

        # 0) session gate — never leave orders resting outside the exchange session;
        #    cancel anything already on the book and stand down.
        if not market_hours.is_open():
            if self.active_buy_id or self.active_sell_id:
                await self._cancel_pair(execu)
                self.quotes_cancelled += 2
            await execu.set_quote_state(
                self.name, self.symbol, None, None, 0.0, 0.0, self.qty, status="closed"
            )
            return "HOLD_MARKET_CLOSED"

        # 1) vol-aware spread
        base_spread = cost.required_spread_ticks
        vol_ticks = signal.vol_widening_ticks
        spread = min(base_spread + vol_ticks, settings.max_spread_widen_ticks)
        spread = max(spread, cost.breakeven_spread_ticks + self.min_profit_margin_ticks)

        # 2) quote pair through cost engine
        tick_size = settings.resolved_tick_size
        mid = snapshot.mid
        half = (spread * tick_size) / 2.0
        target_bid = round(round((mid - half) / tick_size) * tick_size, 2)
        target_ask = round(round((mid + half) / tick_size) * tick_size, 2)

        net = cost_engine.round_trip_net_profit(
            target_bid, target_ask, self.qty, 1,
            settings.product_type, self.segment,
            settings.resolved_lot_size, settings.brokerage_per_order,
        )
        quote_ok = net >= settings.min_net_profit_per_cycle_rs
        if not quote_ok:
            # cancel any resting quote pair
            if self.active_buy_id or self.active_sell_id:
                await self._cancel_pair(execu)
                self.quotes_cancelled += 2
            return "SKIP"

        # 3) inventory-aware quoting
        inv = risk.net_position
        tolerance = settings.requote_tolerance_ticks * tick_size
        decision = "QUOTE"

        if inv == 0:
            # entry pair
            if self.active_buy_id and abs(target_bid - self.active_buy_price) >= tolerance:
                await execu.cancel_order(self.active_buy_id)
                self.quotes_cancelled += 1
                self.active_buy_id = None
            if not self.active_buy_id:
                self.active_buy_id = await execu.place_limit(
                    self.name, self.symbol, self.qty, 1, target_bid
                )
                self.active_buy_price = target_bid if self.active_buy_id else 0.0
                if self.active_buy_id:
                    self.quotes_placed += 1
                    decision = "OPEN_BID"

            if self.active_sell_id and abs(target_ask - self.active_sell_price) >= tolerance:
                await execu.cancel_order(self.active_sell_id)
                self.quotes_cancelled += 1
                self.active_sell_id = None
            if not self.active_sell_id:
                self.active_sell_id = await execu.place_limit(
                    self.name, self.symbol, self.qty, -1, target_ask
                )
                self.active_sell_price = target_ask if self.active_sell_id else 0.0
                if self.active_sell_id:
                    self.quotes_placed += 1
                    decision = "OPEN_ASK"

        elif inv > 0:
            # long: only exit SELL
            if self.active_buy_id:
                await execu.cancel_order(self.active_buy_id)
                self.active_buy_id = None
                self.quotes_cancelled += 1
            if self.active_sell_id and abs(target_ask - self.active_sell_price) >= tolerance:
                await execu.cancel_order(self.active_sell_id)
                self.quotes_cancelled += 1
                self.active_sell_id = None
            if not self.active_sell_id:
                self.active_sell_id = await execu.place_limit(
                    self.name, self.symbol, self.qty, -1, target_ask
                )
                self.active_sell_price = target_ask if self.active_sell_id else 0.0
                if self.active_sell_id:
                    self.quotes_placed += 1
                    decision = "EXIT_SELL"

        elif inv < 0:
            if self.active_sell_id:
                await execu.cancel_order(self.active_sell_id)
                self.active_sell_id = None
                self.quotes_cancelled += 1
            if self.active_buy_id and abs(target_bid - self.active_buy_price) >= tolerance:
                await execu.cancel_order(self.active_buy_id)
                self.quotes_cancelled += 1
                self.active_buy_id = None
            if not self.active_buy_id:
                self.active_buy_id = await execu.place_limit(
                    self.name, self.symbol, self.qty, 1, target_bid
                )
                self.active_buy_price = target_bid if self.active_buy_id else 0.0
                if self.active_buy_id:
                    self.quotes_placed += 1
                    decision = "EXIT_BUY"

        await execu.set_quote_state(
            self.name, self.symbol, self.active_buy_id, self.active_sell_id,
            self.active_buy_price, self.active_sell_price, self.qty,
        )
        return decision

    async def _cancel_pair(self, execu):
        for oid in (self.active_buy_id, self.active_sell_id):
            if oid:
                await execu.cancel_order(oid)
        self.active_buy_id = self.active_sell_id = None
        self.active_buy_price = self.active_sell_price = 0.0