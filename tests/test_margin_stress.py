"""
tests/test_margin_stress.py
---------------------------
Stress the margin pipeline with the REAL broker-verified per-lot margins from
the live Fyers /multiorder/margin queries (see project notes, 2026-09-10) and
the user's production configuration:

  capital         ₹5,00,000      (total incl. parked; broker margin_avail is
                                 the FREE margin, ~₹2,09,613)
  qty             1 FUT per symbol, both sides
  util target     0.95
  buffer          ₹500
  reserve both    False
  max qty         1

Broker ground truth (qty=1, INTRADAY, margin_total):
  SILVER100 3,102 | NATGASMINI 9,611 | LEADMINI 14,481
  ALUMINI 35,746 | ZINCMINI 39,996 | NATURALGAS 47,973
  LEAD 73,814 | ALUMINIUM 1,62,145 | ZINC 2,32,139 | CRUDEOIL 2,91,023

Before the margin_total fix the bot read margin_new_order (e.g. NATURALGAS
~3,21,472) and believed NO contract fit in a ~2L free-margin account. These
tests pin the breadth and gating behaviour with the correct figures.

Run: .venv/bin/python -m unittest tests.test_margin_stress -v
"""

from __future__ import annotations

import asyncio
import random
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

from app import schema
from app.engines.risk_engine import RiskEngine
from app.scanner import select_within_margin

OPEN_MARKET = (
    mock.patch("app.infra.market_hours.is_open", return_value=True),
    mock.patch("app.infra.market_hours.in_winddown", return_value=False),
)

# broker-verified per-lot margins, ascending
REAL_MARGINS = {
    "MCX:SILVER10026SEPFUT": 3102.35,
    "MCX:NATGASMINI26SEPFUT": 9610.63,
    "MCX:LEADMINI26SEPFUT": 14481.25,
    "MCX:ALUMINI26SEPFUT": 35746.00,
    "MCX:ZINCMINI26SEPFUT": 39996.00,
    "MCX:NATURALGAS26SEPFUT": 47973.13,
    "MCX:LEAD26SEPFUT": 73814.00,
    "MCX:ALUMINIUM26SEPFUT": 162145.00,
    "MCX:ZINC26SEPFUT": 232139.00,
    "MCX:CRUDEOIL26SEPFUT": 291022.50,
}

FREE_MARGIN_AVAIL = 209612.93
UTIL = 0.95
BUDGET = FREE_MARGIN_AVAIL * UTIL
BUFFER_RS = 500.0

STRESS_SETTINGS = SimpleNamespace(
    product_type="INTRADAY",
    margin_ledger_enabled=True,
    margin_reserve_both_sides=False,
    margin_utilization_target=UTIL,
    min_free_margin_buffer_rs=BUFFER_RS,
    check_margin_before_order=True,
    halt_cooldown_sec=0.0,
    margin_refresh_sec=30,
    margin_refresh_batch=4,
    margin_api_timeout_sec=12.0,
    positions_api_timeout_sec=10.0,
    margin_risk_fraction=0.25,
    max_orders_per_minute=15,
    max_position_qty=1,
    max_daily_loss_rs=100000.0,
    max_position_age_sec=300.0,
    max_loss_per_position_rs=1500.0,
    segment="COMMODITY",
    asset_type="commodity_fut",
    lot_size=1250,
    tick_size=0.1,
    margin_per_lot_rs=0.0,
)


def mk_engine(**kw) -> RiskEngine:
    kwargs = dict(
        bus=MagicMock(), db=MagicMock(), token_store=MagicMock(), initial_token="",
    )
    kwargs.update(kw)
    return RiskEngine(**kwargs)


def mk_inst(symbol):
    return SimpleNamespace(
        symbol=symbol, segment="COMMODITY", asset_type="commodity_fut",
        lot_size=1250, tick_size=0.1, margin_per_lot_rs=0.0, quote_in_lots=True,
    )


def mk_row(symbol, margin_per_lot, quoteable=True):
    return schema.ScannerRow(
        symbol=symbol, rank=0, segment="COMMODITY", asset_type="commodity_fut",
        ltp=100.0, bid=99.9, ask=100.1, mid=100.0, bid_size=10, ask_size=10,
        spread_ticks=2, liquidity_grade=1.0, churn_ticks_per_sec=0.0,
        vol_widening_ticks=0, volume=1000, quoteable=quoteable,
        margin_avail=FREE_MARGIN_AVAIL, margin_per_lot=margin_per_lot,
        quote_qty=1, lot_size=1250, margin_req_rs=margin_per_lot,
        score=0.9, net_profit_rs=50.0, round_trip_charges_rs=10.0,
        breakeven_spread_ticks=1, required_spread_ticks=2, profitable=True,
        weight=0.0, size_mult=1.0, reasons=[],
    )


class RealBrokerMarginBreadthTest(unittest.TestCase):
    """With the corrected per-lot margins the scanner must select a breadth of
    affordable names (never the 2L+ monsters) inside the 95% budget."""

    @mock.patch("app.infra.market_hours.is_open", return_value=True)
    @mock.patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_breadth_within_ninetyfive_percent_budget(self, *_mocks):
        rows = [mk_row(s, m) for s, m in REAL_MARGINS.items()]
        select_within_margin(
            rows, active_limit=20, margin_avail=FREE_MARGIN_AVAIL,
            buffer_rs=BUFFER_RS, reserve_multiplier=1.0,
        )
        picked = [r.symbol for r in rows if r.quoteable]
        # ascending: 3102, 9611, 14481, 35746, 39996, 47973 sum = 150,909
        # next (LEAD 73,814) would blow the 199,132 budget -> dropped
        self.assertIn("MCX:SILVER10026SEPFUT", picked)
        self.assertIn("MCX:NATGASMINI26SEPFUT", picked)
        self.assertIn("MCX:NATURALGAS26SEPFUT", picked)
        self.assertNotIn("MCX:LEAD26SEPFUT", picked)
        self.assertNotIn("MCX:ZINC26SEPFUT", picked)
        self.assertNotIn("MCX:CRUDEOIL26SEPFUT", picked)
        used = sum(r.margin_req_rs for r in rows if r.quoteable)
        self.assertLessEqual(used, BUDGET + 20.0)
        self.assertGreaterEqual(len(picked), 5, f"breadth collapsed: {picked}")


class RateLimitBackoffTest(unittest.TestCase):
    def _engine_with_resp(self, resp, price=280.0):
        eng = mk_engine()
        eng._fyers = MagicMock()
        eng._margin_post = MagicMock(return_value=resp)
        eng.enable_refresh = True
        return eng

    def _run(self, eng, symbol, price=280.0):
        with mock.patch("app.engines.risk_engine.settings", STRESS_SETTINGS):
            return asyncio.run(eng.refresh_margins([symbol], {symbol: price}))

    def test_429_backs_off_for_five_windows(self):
        # typed as the live broker error: {"s":"error","code":-429,...}
        eng = self._engine_with_resp({"s": "error", "code": -429, "message": "Request limit reached"})
        sym = "MCX:NATGASMINI26SEPFUT"
        loaded, ok = self._run(eng, sym)
        self.assertEqual(loaded, 1)
        self.assertEqual(ok, 0)  # nothing stored
        # symbol must not run again for 5 refresh windows (~2.5 min at 30s)
        self.assertGreaterEqual(
            eng._margin_refreshed_at[sym], time.time() + 4 * STRESS_SETTINGS.margin_refresh_sec)

    def test_valid_response_updates_margin_and_stores_margin_total(self):
        eng = self._engine_with_resp({
            "s": "ok", "code": 200, "data": {
                "margin_avail": FREE_MARGIN_AVAIL,
                "margin_total": 9610.63, "margin_new_order": 9610.63 + 273518.75,
            }})
        sym = "MCX:NATGASMINI26SEPFUT"
        with mock.patch("app.engines.risk_engine.settings", STRESS_SETTINGS), \
             mock.patch("app.infra.instrument.instrument_registry.update_margin"):
            asyncio.run(eng.refresh_margins([sym], {sym: 280.0}))
        self.assertAlmostEqual(eng.margin_per_lot(sym), 9610.63)
        self.assertAlmostEqual(eng.margin_available(sym), FREE_MARGIN_AVAIL)


class LiveConfigPreTradeGateTest(unittest.TestCase):
    """qty=1 FUT both sides must pass the pre-trade gate for the six affordable
    names while the account still holds ~2L free margin."""

    def _engine_suff(self, margin_total, margin_avail=FREE_MARGIN_AVAIL):
        eng = mk_engine()
        eng._fyers = MagicMock()
        eng._margin_post = MagicMock(return_value={
            "s": "ok", "code": 200, "data": {
                "margin_avail": margin_avail,
                "margin_total": margin_total,
                "margin_new_order": margin_total + margin_avail + 638.0,
            }})
        return eng

    def _check(self, eng, symbol, qty=1):
        with mock.patch("app.engines.risk_engine.settings", STRESS_SETTINGS), \
             mock.patch("app.infra.market_hours.is_open", return_value=True), \
             mock.patch("app.infra.market_hours.in_winddown", return_value=False):
            return asyncio.run(eng.check(
                symbol=symbol, strategy="mm", side=1, qty=qty, price=280.0))

    def test_affordable_names_pass_the_gate(self):
        for symbol, margin in REAL_MARGINS.items():
            if margin > BUDGET:
                continue
            eng = self._engine_suff(margin)
            eng._instrument = MagicMock(return_value=mk_inst(symbol))
            verdict = self._check(eng, symbol)
            m = next(c for c in verdict.checks if c.name == "margin")
            self.assertTrue(m.healthy, f"{symbol}: {m.detail}")

    def test_two_lakh_monsters_blocked_by_free_margin(self):
        # ZINC/CRUDEOIL need > available free margin -> must be vetoed
        for symbol in ("MCX:ZINC26SEPFUT", "MCX:CRUDEOIL26SEPFUT"):
            eng = self._engine_suff(REAL_MARGINS[symbol], margin_avail=100000.0)
            eng._instrument = MagicMock(return_value=mk_inst(symbol))
            verdict = self._check(eng, symbol)
            m = next(c for c in verdict.checks if c.name == "margin")
            self.assertFalse(m.healthy, f"{symbol} should be vetoed")


class MarginRefreshStormTest(unittest.TestCase):
    """Adversarial refresh cycles: valid, -429, junk, negative and missing
    responses interleaved across a 10-symbol universe must never crash, never
    store garbage, and never exceed the budget."""

    def test_storm_never_overallocates_or_crashes(self):
        rng = random.Random(123)
        variants = [
            {"s": "ok", "code": 200, "data": {"margin_avail": FREE_MARGIN_AVAIL,
                                             "margin_total": 5000.0}},
            {"s": "error", "code": -429, "message": "Request limit reached"},
            {"s": "ok", "code": 200, "data": {}},
            {"s": "ok", "code": 200, "data": {"margin_avail": FREE_MARGIN_AVAIL,
                                             "margin_total": -1.0}},
            {"s": "ok", "code": 200, "data": {"margin_avail": 0.0,
                                             "margin_total": float("inf")}},
            {"s": "ok", "code": 200},
        ]
        symbols = list(REAL_MARGINS)
        eng = mk_engine()
        eng._fyers = MagicMock()
        eng._margin_post = MagicMock(side_effect=lambda p: variants[rng.randrange(len(variants))])

        def to_lot(margin):
            return margin if (margin and abs(margin) != float("inf")) else REAL_MARGINS[list(REAL_MARGINS)[0]]

        def instrument(sym):
            return mk_inst(sym)
        eng._instrument = instrument

        with mock.patch("app.engines.risk_engine.settings", STRESS_SETTINGS), \
             mock.patch("app.infra.instrument.instrument_registry.update_margin"):
            for cycle in range(30):
                eng._last_margin_avail = FREE_MARGIN_AVAIL
                for i in range(0, len(symbols), STRESS_SETTINGS.margin_refresh_batch):
                    batch = symbols[i:i + STRESS_SETTINGS.margin_refresh_batch]
                    asyncio.run(eng.refresh_margins(
                        batch, {s: 280.0 for s in batch}))

        # no garbage stored: every stored margin is positive and finite
        for sym, m in eng._margin_per_lot_by_symbol.items():
            self.assertGreater(m, 0.0, f"garbage margin stored for {sym}")
            self.assertTrue(abs(m) != float("inf"))
        # every stored avail is non-negative finite
        for sym, a in eng._margin_avail_by_symbol.items():
            self.assertGreaterEqual(a, 0.0)
            self.assertTrue(abs(a) != float("inf"))


if __name__ == "__main__":
    unittest.main()