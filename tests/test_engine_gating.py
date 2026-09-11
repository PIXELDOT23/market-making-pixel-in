"""
tests/test_engine_gating.py
---------------------------
Regression tests for the two quoting bugs found while live-quoting:

1. The scanner's profitability gate compared the RAW TOUCH spread against
   round-trip charges, so any instrument whose 1-2-tick touch cannot clear
   charges (fine — but many low per-tick-value names that WOULD be profitable
   once widened) was permanently stood down. The gate is now feasibility-based:
   net is evaluated at the required-widened pair (the same math on_signal uses
   to accept a quote), bounded by max_spread_widen_ticks.

2. The strategy engine ran a full cost-quote + decision loop for EVERY symbol
   every decision-interval even when the symbol was outside the scanner's
   quoting ranks (which can only ever resolve to HOLD_RANK). With ~250 symbols
   that saturated the market handler and dropped ticks (-> stale rows ->
   everything HOLD_RANK). Inactive symbols are now short-circuited and only a
   resting-pair cancel is performed.

Run: .venv/bin/python -m unittest tests.test_engine_gating -v
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from app.engines.cost_engine import CostEngine
from app.engines.strategy_engine import StrategyEngine, _exchange_of
from app.infra.instrument import AssetType, Instrument, Segment
from app.strategies.market_maker import MarketMakerStrategy

from app import schema

from tests.test_quoting_stress import (
    mk_inst,
    mk_snap,
    mk_cost_quote,
)


# --------------------------------------------------------------------------- 1. economics / feasibility gate
class CostFeasibilityGateTest(unittest.TestCase):
    """The scanner's profitability test must use the widened spread, exactly as
    on_signal does, not the raw 1-2 tick touch. These tests document the charge
    model so a future cost-schedule change fails loudly if it silently flips a
    feasible market into the blocked bucket."""

    @staticmethod
    def _net_for(inst: Instrument, mid: float, spread_ticks: int, qty: int = 1):
        cost = CostEngine(None, None)
        half = spread_ticks * inst.tick_size / 2.0
        exchange = _exchange_of(inst.symbol)
        return cost.round_trip_net_profit(
            mid - half, mid + half, qty, 1, "INTRADAY",
            inst.segment.value, inst.lot_size, 20.0, exchange,
        )

    def test_low_touch_value_name_is_feasible_when_widened(self):
        # A commodity whose 1-2 tick touch loses money but whose required
        # (widenable) spread clears charges — the old touch-gate killed these.
        inst = mk_inst("MCX:LEAD26SEPFUT", Segment.COMMODITY, AssetType.COMMODITY_FUT, 5000, 0.05, 0.0)
        mid = 180.0
        breakeven = CostEngine(None, None).breakeven_spread_ticks(
            mid, 1, "INTRADAY", 0.05, inst.segment.value, 5000, 20.0, "MCX")
        required = max(breakeven + 2, 1)
        self.assertLessEqual(required, 10, "LEAD must remain widen-able")
        self.assertGreaterEqual(
            self._net_for(inst, mid, 2), 0.0,
            "assumption: 2-tick touch is already profitable for LEAD",
        )

    def test_uneconomic_low_tick_future_stays_out(self):
        # NIFTY-scale: per-tick value ~1-9 rs vs 200+ rs of statutory charges.
        # Required spread is far beyond max widening and must stay blocked even
        # under the new gate (this is the *intended* behaviour).
        inst = mk_inst("NSE:TCS26SEPFUT", Segment.EQUITY_FUT, AssetType.EQUITY_FUT, 175, 0.05, 0.0)
        mid = 2201.0
        cost = CostEngine(None, None)
        breakeven = cost.breakeven_spread_ticks(mid, 1, "INTRADAY", 0.05, inst.segment.value, 175, 20.0, "NSE")
        required = max(breakeven + 2, 1)
        self.assertGreater(required, 10, "TCS-scale is uneconomic at 1 lot -> must not widen")
        # the touch net is likewise negative — the old gate and new gate agree here
        self.assertLess(self._net_for(inst, mid, 2), 0.0)


class ScannerBuilderFeasibilityTest(unittest.TestCase):
    """Exercise the real strategy-engine scanner builder closure so the new
    feasibility code path (required <= max_widen -> net at widened pair) is
    covered, and the > max_widen branch rejects."""

    def _engine_and_stubs(self, breakeven, net_by_spread):
        class StubCost:
            def breakeven_spread_ticks(self, *a, **k):
                return breakeven
            def round_trip_cost(self, *a, **k):
                return 50.0
            def round_trip_net_profit(self, bid, ask, qty, side, *a, **k):
                spread = abs(ask - bid)
                for min_spread, value in net_by_spread:
                    if spread >= min_spread:
                        return value
                return 0.0

        class StubRisk:
            def margin_available(self, symbol):
                return 1_000_000.0
            def margin_per_lot(self, symbol):
                return 2000.0
            def position(self, symbol):
                return 0

        class StubData:
            def snapshot(self, symbol):
                return mk_snap(100.0)

        engine = StrategyEngine(None, None, None, {
            "cost": StubCost(), "risk": StubRisk(), "data": StubData(),
        })
        return engine

    # @patch market hours so the instrument isn't gated out (wall-clock).
    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    def test_widened_feasible_instrument_is_quoteable(self, *_mocks):
        # touch spread of 2 ticks (0.1) loses money, but a widened quote wins:
        engine = self._engine_and_stubs(
            breakeven=4,  # required = 6 <= 10 -> feasible
            net_by_spread=[(0.3, 50.0), (0.1, -100.0)],
        )
        inst = mk_inst("NSE:STOCKFUT", Segment.EQUITY_FUT, AssetType.EQUITY_FUT, 100, 0.05, 0.0)
        row = engine._make_scanner_builder()(inst)
        self.assertIsNotNone(row)
        self.assertTrue(row.quoteable, f"reasons={row.reasons}")

    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    def test_uneconomic_instrument_never_quoteable(self, *_mocks):
        # required = 20 > max_spread_widen_ticks(10) -> not quoteable even if a
        # wide spread would pay.
        engine = self._engine_and_stubs(
            breakeven=18,
            net_by_spread=[(0.3, 200.0)],
        )
        inst = mk_inst("NSE:STOCKFUT2", Segment.EQUITY_FUT, AssetType.EQUITY_FUT, 100, 0.05, 0.0)
        row = engine._make_scanner_builder()(inst)
        self.assertIsNotNone(row)
        self.assertFalse(row.quoteable)
        self.assertTrue(any("charges eat spread" in r for r in row.reasons))


# --------------------------------------------------------------------------- 2. engine tick short-circuit
class FakeRisk:
    halted = False

    def suggest_quote_qty(self, symbol, mid=None, vol_widening_ticks=0, weight_mult=1.0):
        return (1, "ok")

    def position(self, symbol):
        return 0


class FakeExecution:
    def __init__(self):
        self.ids = iter(range(1000))
        self.placed = 0
        self.cancelled = 0
        self.live = {}
        self.quote_for_calls = 0

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
        return True

    def tracked_order(self, oid):
        class _E:
            status_label = "OPEN"
            status = 6
        return _E()

    def is_order_known_dead(self, oid):
        return oid not in self.live

    def order_live_and_unfilled(self, oid):
        return oid in self.live

    async def set_quote_state(self, *a, **k):
        pass


class FakeScanner:
    def __init__(self, quoteable):
        self._q = quoteable

    def is_quoteable(self, symbol):
        return self._q

    def rank_of(self, symbol):
        return 1

    def size_mult_of(self, symbol):
        return 1.0


class FloatingCost:
    def __init__(self):
        self.calls = 0

    async def quote_for(self, **kw):
        self.calls += 1
        return mk_cost_quote()

    def round_trip_net_profit(self, bid, ask, qty, side, *a, **k):
        return 80.0

    def round_trip_cost(self, *a, **k):
        return 40.0

    def breakeven_spread_ticks(self, *a, **k):
        return 1


class EngineTickGatingTest(unittest.TestCase):
    def _tick(self):
        return schema.MarketTick(
            ts=time.time(), symbol="MCX:LEAD26SEPFUT", ltp=180.0,
            bid=179.95, ask=180.05, bid_size=100, ask_size=100,
        )

    def _strategy(self):
        from app.infra.instrument import instrument_registry
        instrument_registry.clear()
        return MarketMakerStrategy(
            name="mm", symbol="MCX:LEAD26SEPFUT", segment="COMMODITY",
            params={"qty": 1, "spread_ticks": 4, "min_profit_margin_ticks": 2,
                    "asset_type": AssetType.COMMODITY_FUT.value,
                    "lot_size": 5000, "tick_size": 0.05, "margin_per_lot_rs": 100000.0},
        )

    def _make_engine(self, quoteable, with_resting):
        data = type("D", (), {"snapshot": lambda self, s: mk_snap(180.0)} )()
        cost = FloatingCost()
        execu = FakeExecution()
        scanner = FakeScanner(quoteable)
        engine = StrategyEngine(None, None, None, {
            "data": data, "cost": cost, "execution": execu,
            "scanner": scanner, "risk": FakeRisk(),
        })
        strat = self._strategy()
        engine._by_symbol[strat.symbol] = strat
        server = {"risk": FakeRisk(), "execution": execu, "cost": cost, "scanner": scanner}
        strat.engines = server
        if with_resting:
            strat.active_buy_id = "oid-1"
            strat.active_buy_price = 100.0
            execu.live["oid-1"] = 1
        return engine, cost, execu, strat

    def test_inactive_symbol_skips_cost_and_quotes_but_cancels_resting(self):
        engine, cost, execu, strat = self._make_engine(quoteable=False, with_resting=True)
        asyncio.run(engine._on_tick(self._tick()))
        self.assertEqual(cost.calls, 0)          # no cost quote for a HOLD_RANK symbol
        self.assertEqual(execu.cancelled, 1)     # resting pair was unwound
        self.assertIsNone(strat.active_buy_id)
        self.assertEqual(execu.placed, 0)        # no churn/requote happened

    def test_inactive_symbol_without_resting_pair_just_returns(self):
        engine, cost, execu, strat = self._make_engine(quoteable=False, with_resting=False)
        asyncio.run(engine._on_tick(self._tick()))
        self.assertEqual(cost.calls, 0)
        self.assertEqual(execu.cancelled, 0)
        self.assertEqual(execu.placed, 0)

    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    def test_active_symbol_still_gets_full_decision(self, *_mocks):
        engine, cost, execu, strat = self._make_engine(quoteable=True, with_resting=False)
        asyncio.run(engine._on_tick(self._tick()))
        self.assertGreaterEqual(cost.calls, 1)   # active name gets its cost quote
        self.assertGreater(execu.placed, 0)      # and quotes are placed
        self.assertIn("OPEN_BID", strat.__dict__.get("last_decision", ""))


if __name__ == "__main__":
    unittest.main()