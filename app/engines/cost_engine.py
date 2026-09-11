"""
app/engines/cost_engine.py
--------------------------
TRANSACTION COST ENGINE

Computes the full statutory cost of a transaction for a given asset and
strategy, and the resulting quoting constraints:

  * per-leg ChargeBreakdown (brokerage, exchange txn, STT/CTT, SEBI,
    stamp, GST) for NSE Equity / MCX Commodity
  * round-trip cost, breakeven spread (ticks), required spread, net profit
  * cost quotes are published on the Redis bus + persisted; a small LRU
    keeps the hot pricing path from recomputing constants.

Reuses the charge schedule from the original cost_model (kept identical).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Set

import msgspec

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.db import Database
from app.infra.redis import RedisBus
from app.infra.instrument import instrument_registry
import app.infra.logging as log


# ---- statutory rate constants (matched against broker long-form charge breaks) ----
GST_RATE = 0.18
SEBI_RATE = 0.000001
# NSE cash equities exchange txn charge (₹2.97 per lakh).
NSE_TXN_RATE = 0.0000297
# NSE equity futures exchange txn charge (₹22.3 per lakh) — the current NSE
# F&O equity txn rate. Applies to stock/index futures on NFO.
NSE_FUT_TXN_RATE = 0.0000223
# BSE equity futures exchange txn charge (₹0.5 per lakh).
BSE_FUT_TXN_RATE = 0.000005
# NSE investor-protection / IPFT levy (₹1 per lakh), equities only.
IPFT_RATE = 0.000001
STT_EQUITY_INTRADAY = 0.00025
STT_EQUITY_DELIVERY = 0.001
# Equity futures STT is 0.05% on the SELL side only (index futures 0.025%).
STT_EQUITY_FUT = 0.0005
STAMP_EQUITY_INTRADAY = 0.00003
STAMP_EQUITY_DELIVERY = 0.00015
STAMP_EQUITY_FUT = 0.00002
MCX_TXN_RATE = 0.000026
CTT_COMMODITY_RATE = 0.0001
STAMP_COMMODITY_RATE = 0.00002

# Recompute+publish a symbol's cost quote at most this often. Cost is driven by
# constants + slow-moving mid, so recomputing on every tick would flood the bus
# and DB writer under a whole-market scan without any informational gain.
_QUOTE_THROTTLE_SEC = 2.0


class CostEngine(Engine):
    name = "cost_engine"

    def __init__(self, bus: RedisBus, db: Database):
        super().__init__(bus, db)
        self._lru: "OrderedDict[str, schema.ChargeBreakdown]" = OrderedDict()
        self._lru_capacity = 512
        self._last_quote_ts: Dict[str, float] = {}
        self._market_tasks: "Set[asyncio.Task]" = set()
        self.computed_count = 0

    # ------------------------------------------------------------------ lifecycle loop
    async def run(self):
        """Consume market ticks and emit a cost quote per symbol when asked."""
        def _consume(t: asyncio.Task):
            self._market_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.error(f"[cost] market task error: {exc!r}")

        def handler(ch: str, raw: bytes):
            if self._stop.is_set():
                return
            # Per-symbol throttle: most ticks fall through without spawning any
            # task, so a whole-market scan cannot pile up concurrent publishes.
            symbol = ch.rsplit(":", 1)[-1]
            if time.time() - self._last_quote_ts.get(symbol, 0.0) < _QUOTE_THROTTLE_SEC:
                return
            self._last_quote_ts[symbol] = time.time()
            try:
                task = asyncio.create_task(self._on_market(ch, raw))
                self._market_tasks.add(task)
                task.add_done_callback(_consume)
            except Exception:
                pass

        await self.bus.subscribe("fyers:market:*", handler=handler)
        hb = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        finally:
            hb.cancel()
            await self._shutdown_market_tasks()

    async def _shutdown_market_tasks(self):
        """Cancel and await every live cost-quote task so none is left pending
        when the event loop closes (the manager awaits the engine task first, so
        this runs while the loop can still resume awaits)."""
        tasks = list(self._market_tasks)
        self._market_tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass

    async def _on_market(self, channel: str, raw: bytes):
        try:
            tick = schema.decode(schema.MarketTick, raw)
        except Exception:
            return
        mid = tick.bid + (tick.ask - tick.bid) / 2.0 if (tick.bid and tick.ask) else tick.ltp
        # Prefer the instrument registry's per-symbol segment/lot/tick so NFO
        # futures are charged with the futures schedule instead of the global
        # COMMODITY default (the scanner/strategy paths resolve per instrument).
        inst = instrument_registry.get(tick.symbol)
        segment = inst.segment.value if inst is not None else settings.segment
        lot_size = inst.lot_size if inst is not None else settings.resolved_lot_size
        tick_size = inst.tick_size if inst is not None else settings.resolved_tick_size
        try:
            prefix = str(tick.symbol).split(":", 1)[0].upper()
            exchange = prefix if prefix in ("NSE", "BSE", "MCX") else ""
            await self.quote_for(
                symbol=tick.symbol,
                strategy="mm_natgas",
                qty=int(settings.lot_size) if settings.lot_size else 1,
                segment=segment,
                lot_size=lot_size,
                tick_size=tick_size,
                price=float(mid or 0.0),
                exchange=exchange,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ core math
    def order_charges(
        self,
        price: float,
        qty: int,
        side: str,
        product_type: str = "INTRADAY",
        segment: str = "COMMODITY",
        lot_size: int = 1,
        max_brokerage: float = 20.0,
        exchange: str = "",
    ) -> schema.ChargeBreakdown:
        key = f"{price}|{qty}|{side}|{product_type}|{segment}|{lot_size}|{exchange}"
        cached = self._lru.get(key)
        if cached is not None:
            return cached

        turnover = price * qty * lot_size
        side_u, seg_u, prod_u = side.upper(), segment.upper(), product_type.upper()
        exch_u = str(exchange or "").upper()

        if prod_u == "DELIVERY" and seg_u == "EQUITY":
            brokerage = min(round(turnover * 0.003, 2), max_brokerage)
        else:
            brokerage = min(round(turnover * 0.0003, 2), max_brokerage)
        brokerage = max(0.0, brokerage)

        ipft = 0.0
        if seg_u == "COMMODITY":
            txn = round(turnover * MCX_TXN_RATE, 2)
            stt_or_ctt = round(turnover * CTT_COMMODITY_RATE, 2) if side_u == "SELL" else 0.0
            stamp = round(turnover * STAMP_COMMODITY_RATE, 2) if side_u == "BUY" else 0.0
        elif seg_u == "EQUITY_FUT":
            # futures statute: exchange txn per exchange, STT on sell only
            # (0.05% equity futures / 0.025% index futures), stamp on buy.
            if exch_u == "BSE":
                txn = round(turnover * BSE_FUT_TXN_RATE, 2)
                ipft = 0.0
            else:
                txn = round(turnover * NSE_FUT_TXN_RATE, 2)
                ipft = round(turnover * IPFT_RATE, 2)
            stt_or_ctt = round(turnover * STT_EQUITY_FUT, 2) if side_u == "SELL" else 0.0
            stamp = round(turnover * STAMP_EQUITY_FUT, 2) if side_u == "BUY" else 0.0
        else:
            txn = round(turnover * NSE_TXN_RATE, 2)
            ipft = round(turnover * IPFT_RATE, 2)
            if prod_u == "DELIVERY":
                stt_or_ctt = round(turnover * STT_EQUITY_DELIVERY, 2)
                stamp = round(turnover * STAMP_EQUITY_DELIVERY, 2) if side_u == "BUY" else 0.0
            else:
                stt_or_ctt = round(turnover * STT_EQUITY_INTRADAY, 2) if side_u == "SELL" else 0.0
                stamp = round(turnover * STAMP_EQUITY_INTRADAY, 2) if side_u == "BUY" else 0.0

        sebi = round(turnover * SEBI_RATE, 2)
        gst = round((brokerage + txn + sebi) * GST_RATE, 2)

        breakdown = schema.ChargeBreakdown(
            turnover=round(turnover, 2), brokerage=brokerage, txn=txn,
            stt_or_ctt=stt_or_ctt, sebi=sebi, stamp=stamp, gst=gst, ipft=ipft,
        )
        self._lru[key] = breakdown
        if len(self._lru) > self._lru_capacity:
            self._lru.popitem(last=False)
        return breakdown

    def round_trip_cost(
        self,
        price: float,
        qty: int,
        product_type: str = "INTRADAY",
        segment: str = "COMMODITY",
        lot_size: int = 1,
        max_brokerage: float = 20.0,
        exchange: str = "",
    ) -> float:
        buy = self.order_charges(price, qty, "BUY", product_type, segment, lot_size, max_brokerage, exchange)
        sell = self.order_charges(price, qty, "SELL", product_type, segment, lot_size, max_brokerage, exchange)
        return round(buy.total + sell.total, 2)

    def round_trip_net_profit(
        self,
        entry_price: float,
        exit_price: float,
        qty: int,
        side: int,
        product_type: str = "INTRADAY",
        segment: str = "COMMODITY",
        lot_size: int = 1,
        max_brokerage: float = 20.0,
        exchange: str = "",
    ) -> float:
        if side == 1:
            buy_price, sell_price = entry_price, exit_price
            gross = (exit_price - entry_price) * lot_size * qty
        else:
            buy_price, sell_price = exit_price, entry_price
            gross = (entry_price - exit_price) * lot_size * qty
        buy_total = self.order_charges(buy_price, qty, "BUY", product_type, segment, lot_size, max_brokerage, exchange).total
        sell_total = self.order_charges(sell_price, qty, "SELL", product_type, segment, lot_size, max_brokerage, exchange).total
        return round(gross - buy_total - sell_total, 2)

    def breakeven_spread_ticks(
        self,
        price: float,
        qty: int,
        product_type: str,
        tick_size: float,
        segment: str,
        lot_size: int,
        max_brokerage: float,
        exchange: str = "",
    ) -> int:
        cost = self.round_trip_cost(price, qty, product_type, segment, lot_size, max_brokerage, exchange)
        tick_value = tick_size * lot_size * qty
        if tick_value <= 0:
            return 1
        return max(1, math.ceil(cost / tick_value))

    # ------------------------------------------------------------------ asset/strategy quote
    async def quote_for(
        self,
        symbol: str,
        strategy: str,
        qty: int,
        segment: str,
        lot_size: int,
        tick_size: float,
        price: float,
        min_profit_margin_ticks: int = 2,
        max_brokerage: float = 20.0,
        product_type: str = "INTRADAY",
        exchange: str = "",
    ) -> schema.CostQuote:
        t0 = time.perf_counter()
        round_trip = self.round_trip_cost(price, qty, product_type, segment, lot_size, max_brokerage, exchange)
        breakeven = self.breakeven_spread_ticks(
            price, qty, product_type, tick_size, segment, lot_size, max_brokerage, exchange
        )
        required = max(breakeven + min_profit_margin_ticks, 1)

        buy = self.order_charges(price, qty, "BUY", product_type, segment, lot_size, max_brokerage, exchange)
        sell = self.order_charges(price, qty, "SELL", product_type, segment, lot_size, max_brokerage, exchange)

        # net profit per cycle at the required spread mid-price
        half = (required * tick_size) / 2.0
        bid = price - half
        ask = price + half
        net = self.round_trip_net_profit(
            bid, ask, qty, 1, product_type, segment, lot_size, max_brokerage, exchange
        )

        cq = schema.CostQuote(
            symbol=symbol,
            strategy=strategy,
            qty=qty,
            segment=segment,
            lot_size=lot_size,
            round_trip_charges_rs=round_trip,
            breakeven_spread_ticks=breakeven,
            required_spread_ticks=required,
            net_profit_per_cycle_rs=net,
            each_leg=schema.ChargeBreakdown(
                turnover=buy.turnover,
                brokerage=buy.brokerage + sell.brokerage,
                txn=buy.txn + sell.txn,
                stt_or_ctt=buy.stt_or_ctt + sell.stt_or_ctt,
                sebi=buy.sebi + sell.sebi,
                stamp=buy.stamp + sell.stamp,
                gst=buy.gst + sell.gst,
                ipft=buy.ipft + sell.ipft,
            ),
        )
        self.computed_count += 1
        self._mark()
        try:
            await self.bus.publish(self.bus.channel_for("cost", symbol), cq, ttl_sec=300)
            await self.db.insert_cost_quote(cq)
        except Exception:
            pass
        return cq

    # ------------------------------------------------------------------ pricing helpers for strategies
    def profitable_quote_pair(
        self,
        mid: float,
        base_spread_ticks: int,
        qty: int,
        tick_size: float,
        segment: str,
        lot_size: int,
        product_type: str,
        max_brokerage: float,
        min_net_profit: float,
        max_widen_ticks: int,
    ) -> tuple[float, float, int, float]:
        spread = max(1, base_spread_ticks)
        cap = max(spread, max_widen_ticks)
        bid = ask = mid
        net = 0.0
        while spread <= cap:
            half = (spread * tick_size) / 2.0
            bid = round(round((mid - half) / tick_size) * tick_size, 2)
            ask = round(round((mid + half) / tick_size) * tick_size, 2)
            net = self.round_trip_net_profit(bid, ask, qty, 1, product_type, segment, lot_size, max_brokerage)
            if net >= min_net_profit:
                break
            spread += 1
        return bid, ask, spread, net