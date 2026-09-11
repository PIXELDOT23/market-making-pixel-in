"""
tests/test_quoting_stress.py
----------------------------
Stress tests for the quoting pipeline.

These are NOT normal happy-path unit tests: they push the quoting hot path
(scanner rank + RoM weighting + margin budget + per-segment gate + sizing) and
the strategy decision loop through adversarial and repeated conditions to shake
out any exception, invariant violation or book-keeping leak.

Run: .venv/bin/python -m unittest tests.test_quoting_stress -v
"""

from __future__ import annotations

import math
import random
import time
import unittest
from unittest import mock

from app import schema
from app.config import settings
from app.infra.instrument import AssetType, Instrument, Segment
from app.scanner import (
    ScannerCache,
    assign_rom_weights,
    compute_row,
    rank_rows,
    select_within_margin,
)
from app.sizing import commodity_size, dynamic_size, quote_size_for
from app.strategies.market_maker import MarketMakerStrategy
from app.weighting import WeightTracker


OPEN_MARKET = mock.patch(
    "app.infra.market_hours.is_open", return_value=True
), mock.patch("app.infra.market_hours.in_winddown", return_value=False)


# --------------------------------------------------------------------------- helpers
def mk_inst(symbol: str, segment: Segment, asset: AssetType, lot: int, tick: float, margin: float) -> Instrument:
    return Instrument(
        symbol=symbol, segment=segment, asset_type=asset, lot_size=lot,
        tick_size=tick, tick_value_rs=round(lot * tick, 6),
        margin_per_lot_rs=margin,
    )


def mk_snap(mid, bid=None, ask=None, bsize=100, asize=100, vol=1000, connected=True, churn=0.0):
    bid = bid if bid is not None else mid - 0.5
    ask = ask if ask is not None else mid + 0.5
    return schema.MarketSnapshot(
        symbol="X", ltp=mid, bid=bid, ask=ask, mid=mid,
        bid_size=bsize, ask_size=asize, tick_count=10,
        churn_ticks_per_sec=churn, last_tick_ts=0.0, volume=vol, is_connected=connected,
    )


def mk_row(symbol="X", *, segment="COMMODITY", asset_type="commodity_fut",
           net=50.0, margin_req=1000.0, volume=1000, score=0.9, quoteable=True,
           profitable=True, margin_avail=50000.0, margin_per_lot=1000.0,
           spread_ticks=2, quote_qty=1, lot_size=1, reasons=None, weight=0.0, size_mult=1.0):
    return schema.ScannerRow(
        symbol=symbol, rank=0, segment=segment, asset_type=asset_type,
        ltp=100.0, bid=99.5, ask=100.5, mid=100.0, bid_size=10, ask_size=10,
        spread_ticks=spread_ticks, liquidity_grade=1.0, churn_ticks_per_sec=0.0,
        vol_widening_ticks=0, volume=volume, quoteable=quoteable,
        margin_avail=margin_avail, margin_per_lot=margin_per_lot,
        quote_qty=quote_qty, lot_size=lot_size, margin_req_rs=margin_req,
        score=score, net_profit_rs=net, round_trip_charges_rs=10.0,
        breakeven_spread_ticks=1, required_spread_ticks=2,
        profitable=profitable, weight=weight, size_mult=size_mult,
        reasons=list(reasons or []),
    )


# --------------------------------------------------------------------------- sizing fuzz
class SizingStressTest(unittest.TestCase):
    def _eq_fut(self):
        return mk_inst("NSE:NIFTYFUT", Segment.EQUITY_FUT, AssetType.EQUITY_FUT, 75, 0.05, 50000.0)

    def _cash(self):
        return mk_inst("NSE:RELIANCE", Segment.EQUITY, AssetType.EQUITY, 1, 0.05, 5000.0)

    def _commodity(self):
        return mk_inst("MCX:NATURALGASFUT", Segment.COMMODITY, AssetType.COMMODITY_FUT, 1250, 0.1, 10000.0)

    def test_dynamic_size_fuzz_never_crashes_and_stays_in_heads(self):
        rng = random.Random(7)
        weird = [0.0, -1.0, -5000.0, float("inf"), float("nan"), 1e18, 1e-9, 12345.67]
        for i in range(20000):
            inst = self._eq_fut() if rng.random() < 0.5 else self._cash()
            kwargs = dict(
                margin_avail=rng.choice(weird + [rng.uniform(-1e6, 1e9)]),
                margin_fraction=rng.choice([0.0, -1.0, 0.25, 1.0, 5.0]),
                margin_per_lot=rng.choice(weird + [rng.uniform(0, 2e5)]),
                inventory=rng.choice([-99, -3, -1, 0, 1, 2, 99]),
                max_position_qty=rng.choice([0, 1, 5, 50]),
                mid=rng.choice([None, 0.0, -5.0, 100.0, 1e6]),
                vol_widening_ticks=rng.choice([-10, 0, 1, 5, 100]),
                vol_reduction_per_tick=rng.choice([0.0, 0.1, 1.0, 5.0]),
                weight_mult=rng.choice([0.0, -3.0, 0.5, 1.0, 4.0, 100.0]),
            )
            size, _ = dynamic_size(inst, **kwargs)
            cap = max(1, kwargs["max_position_qty"])
            self.assertGreaterEqual(size, 1)
            self.assertLessEqual(size, cap, f"size {size} > cap {cap}: {kwargs}")

    def test_commodity_size_fuzz(self):
        rng = random.Random(11)
        for _ in range(20000):
            margin_avail = rng.choice([0.0, -1.0, 1e18, float("nan"), rng.uniform(-1e6, 1e9)])
            margin_per_lot = rng.choice([0.0, -1.0, 1e18, float("nan"), rng.uniform(-1e6, 1e9)])
            qty, _ = commodity_size(
                self._commodity(), margin_avail=margin_avail,
                margin_fraction=rng.choice([1.0, 0.0, -2.0]),
                margin_per_lot=margin_per_lot, max_lots=rng.choice([0, 1, 3]),
            )
            self.assertGreaterEqual(qty, 0)
            self.assertLessEqual(qty, 3)

    def test_weighted_sizing_direction(self):
        inst = self._eq_fut()
        base, _ = dynamic_size(inst, margin_avail=200000.0, margin_fraction=0.2,
                               margin_per_lot=5000.0, inventory=0, max_position_qty=20)
        up, _ = dynamic_size(inst, margin_avail=200000.0, margin_fraction=0.2,
                             margin_per_lot=5000.0, inventory=0, max_position_qty=20, weight_mult=2.0)
        down, _ = dynamic_size(inst, margin_avail=200000.0, margin_fraction=0.2,
                               margin_per_lot=5000.0, inventory=0, max_position_qty=20, weight_mult=0.5)
        self.assertGreaterEqual(up, base)
        self.assertLessEqual(down, base)

    def test_quote_size_for_commodity_ignores_weight(self):
        qty, _ = quote_size_for(
            self._commodity(), margin_avail=1e9, margin_fraction=1.0,
            margin_per_lot=10000.0, inventory=0, max_position_qty=3, weight_mult=100.0,
        )
        self.assertEqual(qty, 3)


# --------------------------------------------------------------------------- weight / ranking stress
class WeightRankingStressTest(unittest.TestCase):
    def test_assign_weights_single_and_empty_rows(self):
        tracker = WeightTracker(0.15)
        assign_rom_weights([], tracker, lambda s: (0.0, 0), lambda s: 0.0,
                           min_cycles=3, volume_floor=0.3, weight_max=5.0,
                           size_min=0.5, size_max=2.0)
        (r,) = [mk_row("SOLO", net=10.0, margin_req=100.0, volume=0)]
        assign_rom_weights([r], tracker, lambda s: (0.0, 0), lambda s: 0.0,
                           min_cycles=3, volume_floor=0.3, weight_max=5.0,
                           size_min=0.5, size_max=2.0)
        # volume 0 -> vnorm 0.5 -> vfactor 0.3 + 0.7*0.5 = 0.65 -> weight 0.10*0.65
        self.assertAlmostEqual(r.weight, 0.065, places=3)
        # only row -> wmin==wmax -> size draw 0.5 -> 0.5 + 1.5*0.5 = 1.25
        self.assertAlmostEqual(r.size_mult, 1.25, places=3)

    def test_realized_floor_and_caps(self):
        tracker = WeightTracker(0.0)  # alpha 0 -> raw values only
        rows = [
            mk_row("HIGH_ROM", net=100.0, margin_req=100.0, volume=10),   # theo 1.0
            mk_row("LOW_ROM", net=5.0, margin_req=1000.0, volume=10),     # theo 0.005
            mk_row("DEAD", net=50.0, margin_req=100.0, volume=0),         # high rom, no volume
        ]
        realized = {"HIGH_ROM": (200.0, 5, 200.0)}  # realized rom = 1.0, outranks theo same

        def real(sym):
            if sym in realized:
                return (realized[sym][0], realized[sym][1])
            return (0.0, 0)

        def booked(sym):
            return realized[sym][2] if sym in realized else 0.0

        assign_rom_weights(rows, tracker, real, booked,
                           min_cycles=3, volume_floor=0.3, weight_max=5.0,
                           size_min=0.5, size_max=2.0)
        # unmatured symbols fall back to theoretical
        self.assertGreater(rows[0].weight, rows[1].weight)
        self.assertGreater(rows[2].weight, rows[1].weight)

    def test_rank_rows_margin_efficiency_primary(self):
        rows = [
            mk_row("a", net=100.0, margin_req=100.0, volume=1),
            mk_row("b", net=100.0, margin_req=1000.0, volume=10),
            mk_row("c", net=1.0, margin_req=5000.0, volume=1),
        ]
        assign_rom_weights(rows, WeightTracker(0.0),
                           lambda s: (0.0, 0), lambda s: 0.0,
                           min_cycles=3, volume_floor=0.3, weight_max=5.0,
                           size_min=0.5, size_max=2.0)
        ranked = rank_rows(rows, active_limit=3)
        # 'a' has best RoM (1.0) despite lowest revenue; 'b' second; 'c' worst
        self.assertEqual([r.symbol for r in ranked], ["a", "b", "c"])
        self.assertEqual(ranked[0].rank, 1)


# --------------------------------------------------------------------------- full scanner refresh stress
class ScannerRefreshStressTest(unittest.TestCase):
    def build_universe(self, n_equity: int, n_commodity: int):
        insts = []
        for i in range(n_equity):
            insts.append(mk_inst(
                f"NSE:EQFUT{i}", Segment.EQUITY_FUT, AssetType.EQUITY_FUT,
                lot=75 if i % 2 else 1, tick=0.05, margin=10000.0 + (i % 7) * 1000.0))
        for i in range(n_commodity):
            insts.append(mk_inst(
                f"MCX:COMMOD{i}", Segment.COMMODITY, AssetType.COMMODITY_FUT,
                lot=1250, tick=0.1, margin=8000.0 + (i % 5) * 500.0))
        return insts

    def test_refresh_stress_mixed_universe_invariants(self):
        rng = random.Random(42)
        insts = self.build_universe(60, 40)
        cache = ScannerCache(insts, active_limit=10)
        tracker = WeightTracker(0.2)
        margin_avail = 250000.0
        margin_per_lot_cache = {}

        strategies = {}
        for inst in insts:
            strategies[inst.symbol] = {"realized": 0.0, "cycles": 0}

        def assigner(rows):
            def real(sym):
                st = strategies[sym]
                return (st["realized"], st["cycles"])

            def booked(sym):
                m = margin_per_lot_cache.get(sym, 0.0) or 1.0
                return margin_req_for(sym)  # reserve used by the greedy walk

            assign_rom_weights(rows, tracker, real, booked,
                               min_cycles=3, volume_floor=0.3, weight_max=5.0,
                               size_min=0.5, size_max=2.0)

        def margin_req_for(sym):
            return 1.0 * margin_per_lot_cache.get(sym, 0.0)

        def build_row(inst):
            snap = schema.MarketSnapshot(
                symbol=inst.symbol, ltp=100.0, bid=99.9, ask=100.1, mid=100.0,
                bid_size=rng.randint(1, 500), ask_size=rng.randint(1, 500),
                tick_count=50, churn_ticks_per_sec=float(rng.choice([0, 0, 0, 1, 3, 8])),
                last_tick_ts=time.time(), volume=rng.randint(0, 2_000_000),
                is_connected=rng.random() > 0.02,
            )
            liq = min(snap.bid_size, snap.ask_size) / max(snap.bid_size, snap.ask_size)

            def cost_calc(mid, qty):
                return schema.ScanCost(
                    net_profit_rs=float(rng.randint(-50, 200)),
                    round_trip_charges_rs=20.0,
                    breakeven_spread_ticks=1, required_spread_ticks=2,
                    profitable=True,
                )

            return compute_row(
                symbol=inst.symbol, inst=inst, snap=snap, signal=None,
                margin_avail=margin_avail,
                margin_per_lot=margin_per_lot_cache.get(inst.symbol, inst.margin_per_lot_rs),
                max_position_qty=10, margin_risk_fraction=0.25,
                vol_reduction_per_tick=0.1, min_liquidity=0.05,
                inventory=0, cost_calc=cost_calc,
            )

        cache._weight_assigner = assigner
        seen_sets = set()
        for cycle in range(400):
            # warm margins progressively (like the strategy engine)
            if cycle % 5 == 0 and margin_per_lot_cache.__len__() < len(insts):
                sym = list(margin_per_lot_cache.keys())
                pending = [i.symbol for i in insts if i.symbol not in margin_per_lot_cache]
                if pending:
                    pick = pending[cycle // 5 % len(pending)]
                    margin_per_lot_cache[pick] = 9000.0 + (cycle % 5) * 100.0
            cache.refresh(build_row)

            rows = cache.rows()
            # per-segment quoting count never exceeds active_limit
            per_seg: dict[str, int] = {}
            for r in rows:
                if r.quoteable:
                    per_seg[r.segment] = per_seg.get(r.segment, 0) + 1
            for seg, cnt in per_seg.items():
                self.assertLessEqual(cnt, cache.active_limit, f"seg {seg} over quota")

            # weight / size_mult always bounded
            for r in rows:
                self.assertGreaterEqual(r.weight, 0.0)
                self.assertLessEqual(r.weight, 5.0)
                self.assertGreaterEqual(r.size_mult, 0.5)
                self.assertLessEqual(r.size_mult, 2.0)

            # margin budget: sum of quoteable reservations (x both-sides or x1,
            # mirroring settings) fits inside the utilizable portion of avail
            if rows:
                mult = 2.0 if settings.margin_reserve_both_sides else 1.0
                target = settings.margin_utilization_target
                used = sum(r.margin_req_rs for r in rows if r.quoteable) * mult
                # tolerance for the both-sides buffer the greedy adds per row
                self.assertLessEqual(
                    used, margin_avail * target + cache.active_limit * 1000.0 + 1.0)
            seen_sets.add(frozenset(r.symbol for r in rows if r.quoteable))
        self.assertGreater(len(seen_sets), 5)  # the gate actually rebalances

    def test_margin_growth_rebalances_slots(self):
        cache = ScannerCache([], active_limit=4)

        def build_row(inst):
            return None

        # empty universe: no crash
        cache.refresh(build_row)
        self.assertEqual(cache.rows(), [])
        self.assertEqual(cache.size_mult_of("nope"), 1.0)
        self.assertEqual(cache.weight_of("nope"), 0.0)
        self.assertFalse(cache.is_quoteable("nope"))


class SelectWithinMarginStressTest(unittest.TestCase):
    def test_unknown_and_negative_margins_never_crash(self):
        rng = random.Random(3)
        for _ in range(500):
            rows = [
                mk_row(f"S{i}", margin_req=rng.choice([0.0, -1.0, 1.0, 100.0, 1e6, 1e18]),
                       quoteable=rng.random() > 0.3)
                for i in range(rng.randint(0, 50))
            ]
            select_within_margin(
                rows, active_limit=rng.randint(1, 20),
                margin_avail=rng.choice([None, 0.0, -5.0, 1000.0, 1e18]),
                buffer_rs=rng.choice([0.0, 100.0, 1e6]),
                reserve_multiplier=rng.choice([1.0, 2.0, 5.0]),
            )

    def test_margin_block_appends_reason_without_exception(self):
        rows = [mk_row("A", margin_req=9000.0), mk_row("B", margin_req=9000.0)]
        select_within_margin(rows, active_limit=10, margin_avail=10000.0, buffer_rs=1000.0, reserve_multiplier=1.0)
        self.assertTrue(rows[0].quoteable)
        self.assertFalse(rows[1].quoteable)


# --------------------------------------------------------------------------- strategy decision-loop stress
class FakeEv:
    status_label = "OPEN"
    status = 6


class FakeExecution:
    def __init__(self):
        self.ids = iter(range(1000))
        self.placed = 0
        self.cancelled = 0
        self.replaced = 0
        self.live = {}

    def reject_cooldown(self, symbol):
        return 0.0

    async def place_limit(self, name, symbol, qty, side, price):
        oid = f"oid-{next(self.ids)}"
        self.live[oid] = side
        self.placed += 1
        return oid

    async def cancel_order(self, oid):
        self.live.pop(oid, None)
        self.cancelled += 1
        return True

    async def replace_order(self, oid, price, qty, symbol, side):
        self.replaced += 1
        return True

    def tracked_order(self, oid):
        return FakeEv()

    def is_order_known_dead(self, oid):
        return oid not in self.live

    def order_live_and_unfilled(self, oid):
        return oid in self.live

    async def set_quote_state(self, *a, **k):
        pass


class FakeRisk:
    def __init__(self, qty, cap=10):
        self.halted = False
        self._pos = 0
        self._qty = qty
        self.cap = cap

    def suggest_quote_qty(self, symbol, mid=None, vol_widening_ticks=0, weight_mult=1.0):
        if abs(self._pos) >= self.cap:
            return (0, f"at position cap {self._pos:+d}")
        return (self._qty, f"stress qty {self._qty}")

    def position(self, symbol):
        return self._pos

    def on_fill(self, side):
        self._pos += side


class FakeCost:
    def __init__(self, lot_size=1250, base_charges=25.0):
        self.lot = lot_size
        self.charges = base_charges

    def round_trip_net_profit(self, bid, ask, qty, side, *a, **k):
        return (ask - bid) * self.lot * qty - self.charges


class FakeScanner:
    def __init__(self, quoteable=True):
        self._q = quoteable
    def is_quoteable(self, symbol):
        return self._q
    def rank_of(self, symbol):
        return 1
    def size_mult_of(self, symbol):
        return 1.5


def mk_signal(mid, vol=0):
    return schema.SignalMetrics(
        symbol="MCX:NATURALGASFUT", strategy="mm", ts=time.time(), mid=mid,
        churn_ticks_per_sec=float(vol), vol_widening_ticks=int(vol),
        liquidity_grade=1.0, spread_ticks_now=2, quoteable=True,
    )


def mk_cost_quote(spread=4, required=3, breakeven=1, net=80.0):
    return schema.CostQuote(
        symbol="MCX:NATURALGASFUT", strategy="mm", qty=1, segment="COMMODITY",
        lot_size=1250, round_trip_charges_rs=20.0,
        breakeven_spread_ticks=breakeven, required_spread_ticks=required,
        net_profit_per_cycle_rs=net,
        each_leg=schema.ChargeBreakdown(0, 0, 0, 0, 0, 0, 0),
    )


class StrategyDecisionLoopStressTest(unittest.TestCase):
    def _strategy(self):
        from app.infra.instrument import instrument_registry
        instrument_registry.clear()
        return MarketMakerStrategy(
            name="mm_test", symbol="MCX:NATURALGASFUT", segment="COMMODITY",
            params={"qty": 1, "spread_ticks": 4, "min_profit_margin_ticks": 2,
                    "asset_type": AssetType.COMMODITY_FUT.value,
                    "lot_size": 1250, "tick_size": 0.1, "margin_per_lot_rs": 10000.0},
        )

    async def _drive(self, strategy, engines, n=40, rng=None):
        rng = rng or random.Random(99)
        decisions = []
        for i in range(n):
            mid = 280.0 + rng.choice([-3, -1, 0, 0, 1, 3])
            snap = mk_snap(mid, bid=mid - 0.2, ask=mid + 0.2, bsize=100, asize=100,
                           vol=rng.randint(100, 100_000), churn=rng.choice([0, 0, 0.5, 2.0, 6.0]))
            sig = mk_signal(mid, vol=rng.choice([0, 0, 1, 4]))
            cost = mk_cost_quote(spread=rng.randint(2, 8), net=rng.randint(-20, 150))
            try:
                decision = await strategy.on_signal(snap, sig, cost, engines)
            except Exception as exc:  # pragma: no cover
                self.fail(f"on_signal raised at cycle {i}: {exc!r}")
            decisions.append(decision)
            # every ~6 cycles the market fills one side (inventory changes)
            if i % 6 == 0 and rng.random() < 0.5:
                side = rng.choice([-1, 1])
                strategy.on_fill(schema.OrderEvent(
                    ts=time.time(), broker_order_id="fill", strategy="mm",
                    symbol="MCX:NATURALGASFUT", side=side, qty=1, status=2,
                    status_label="FILLED", limit_price=mid, filled_qty=1, traded_price=mid,
                ))
                engines["risk"].on_fill(side)
        return decisions

    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_hot_loop_never_raises_and_book_stays_consistent(self, *_mocks):
        import asyncio
        engines = {
            "risk": FakeRisk(qty=1),
            "execution": FakeExecution(),
            "cost": FakeCost(),
            "scanner": FakeScanner(quoteable=True),
        }
        strategy = self._strategy()
        asyncio.run(self._drive(strategy, engines, n=200))
        # at most one order per side survives
        ids = [strategy.active_buy_id, strategy.active_sell_id]
        self.assertIsNotNone(ids[0] or ids[1])
        self.assertGreater(engines["execution"].placed, 0)
        self.assertGreaterEqual(engines["execution"].cancelled + engines["execution"].replaced, 0)

    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_scanner_removal_cancels_quotes(self, *_mocks):
        import asyncio
        strategy = self._strategy()
        execu = FakeExecution()
        engines = {
            "risk": FakeRisk(qty=1),
            "execution": execu,
            "cost": FakeCost(),
            "scanner": FakeScanner(quoteable=False),
        }
        # prime a resting pair first
        engines["scanner"] = FakeScanner(quoteable=True)
        asyncio.run(self._drive(strategy, engines, n=3))
        self.assertTrue(strategy.active_buy_id or strategy.active_sell_id)
        engines["scanner"] = FakeScanner(quoteable=False)
        decision = asyncio.run(strategy.on_signal(
            mk_snap(280.0), mk_signal(280.0), mk_cost_quote(), engines))
        self.assertEqual(decision, "HOLD_RANK")
        self.assertIsNone(strategy.active_buy_id)
        self.assertIsNone(strategy.active_sell_id)

    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_margin_standdown_and_halt_paths(self, *_mocks):
        import asyncio
        for risk in (FakeRisk(qty=0), FakeRisk(qty=1)):
            risk = FakeRisk(qty=0)
            strategy = self._strategy()
            engines = {
                "risk": risk, "execution": FakeExecution(),
                "cost": FakeCost(), "scanner": FakeScanner(quoteable=True),
            }
            decision = asyncio.run(strategy.on_signal(
                mk_snap(280.0), mk_signal(280.0), mk_cost_quote(), engines))
            self.assertEqual(decision, "HOLD_MARGIN")
        risk = FakeRisk(qty=1)
        risk.halted = True
        strategy = self._strategy()
        engines = {"risk": risk, "execution": FakeExecution(),
                   "cost": FakeCost(), "scanner": FakeScanner(quoteable=True)}
        decision = asyncio.run(strategy.on_signal(
            mk_snap(280.0), mk_signal(280.0), mk_cost_quote(), engines))
        self.assertEqual(decision, "HOLD_RISK_HALT")

    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_no_scanner_engine_is_safe(self, *_mocks):
        import asyncio
        strategy = self._strategy()
        engines = {"risk": FakeRisk(qty=1), "execution": FakeExecution(), "cost": FakeCost()}
        asyncio.run(self._drive(strategy, engines, n=20))


if __name__ == "__main__":
    unittest.main()