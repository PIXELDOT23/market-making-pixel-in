"""
Unit tests for the global multi-asset margin ledger in the risk engine: the
aggregated "remaining margin" each symbol sees after OTHER names have reserved
theirs, and the pre-trade budget gate.

Run: .venv/bin/python -m unittest tests.test_risk_margin -v
"""

import asyncio
import importlib.util
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.engines.risk_engine import RiskEngine

# Legacy prototype scripts live under legacy/ and reference each other with
# top-level imports ("import config", "import logger as log"). Register them in
# sys.modules under their original names so margin.py's imports resolve and the
# tests below can patch the code actually being exercised.
_LEGACY_DIR = pathlib.Path(__file__).resolve().parents[1] / "legacy"


def _load_legacy(name):
    spec = importlib.util.spec_from_file_location(name, _LEGACY_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_config_legacy = _load_legacy("config")
_load_legacy("logger")

# public alias patched by the qty-conversion tests below
config_legacy = _config_legacy


def mk_engine(**kw) -> RiskEngine:
    kwargs = dict(
        bus=MagicMock(), db=MagicMock(), token_store=MagicMock(), initial_token="",
    )
    kwargs.update(kw)
    return RiskEngine(**kwargs)


# Every setting the risk check() path reads while the broker/FYERS is absent.
LEDGER_ON = SimpleNamespace(
    margin_ledger_enabled=True,
    margin_reserve_both_sides=True,
    margin_utilization_target=1.0,
    min_free_margin_buffer_rs=1000.0,
    check_margin_before_order=False,
    halt_cooldown_sec=0.0,
    max_orders_per_minute=15,
    max_position_qty=10,
    max_daily_loss_rs=100000.0,
    max_position_age_sec=300.0,
    max_loss_per_position_rs=1500.0,
    segment="COMMODITY",
    asset_type="commodity_fut",
    lot_size=1250,
    tick_size=0.1,
    margin_per_lot_rs=5000.0,
)


class MarginRemainingTest(unittest.TestCase):
    def test_own_booking_not_subtracted_from_own_remaining(self):
        eng = mk_engine()
        eng._last_margin_avail = 100000.0
        eng.sync_margin_bookings({"A": 20000.0, "B": 30000.0})
        with patch("app.engines.risk_engine.settings", LEDGER_ON):
            # A: 100000 - (B 30000) - buffer 1000 = 69000
            self.assertAlmostEqual(eng.margin_remaining("A"), 69000.0)
            # B: 100000 - (A 20000) - 1000 = 79000
            self.assertAlmostEqual(eng.margin_remaining("B"), 79000.0)

    def test_unknown_broker_margin_stays_lenient(self):
        eng = mk_engine()
        eng._last_margin_avail = 0.0
        eng.sync_margin_bookings({"A": 20000.0, "B": 30000.0})
        with patch("app.engines.risk_engine.settings", LEDGER_ON):
            self.assertEqual(eng.margin_remaining("A"), 0.0)  # not negative, no veto

    def test_ledger_disabled_returns_raw_avail(self):
        eng = mk_engine()
        eng._last_margin_avail = 100000.0
        eng.sync_margin_bookings({"A": 20000.0, "B": 30000.0})
        off = SimpleNamespace(**_LEDGER_OFF())
        with patch("app.engines.risk_engine.settings", off):
            self.assertEqual(eng.margin_remaining("A"), 100000.0)

    def test_bookings_sync_replaces_set_and_tracks_total(self):
        eng = mk_engine()
        eng.sync_margin_bookings({"A": 10.0, "B": 20.0})
        self.assertAlmostEqual(eng.booked_margin_total(), 30.0)
        eng.sync_margin_bookings({"C": 5.0})
        self.assertAlmostEqual(eng.booked_margin_total(), 5.0)
        self.assertEqual(eng.booked_margin_for("A"), 0.0)
        self.assertAlmostEqual(eng.booked_margin_for("C"), 5.0)

    def test_zero_or_negative_bookings_filtered(self):
        eng = mk_engine()
        eng.sync_margin_bookings({"A": 0.0, "B": -10.0, "C": 7.0})
        self.assertAlmostEqual(eng.booked_margin_total(), 7.0)


class PreTradeBudgetGateTest(unittest.TestCase):
    def _run_check(self, eng, qty):
        with patch("app.engines.risk_engine.settings", LEDGER_ON), \
             patch("app.infra.market_hours.is_open", return_value=True), \
             patch("app.infra.market_hours.in_winddown", return_value=False):
            return asyncio.run(eng.check(
                symbol="MCX:NATURALGAS26SEPFUT", strategy="mm", side=1,
                qty=qty, price=280.0,
            ))

    def test_order_beyond_remaining_share_is_vetoed(self):
        eng = mk_engine()
        eng._last_margin_avail = 10000.0
        eng.sync_margin_bookings({"A": 4000.0})
        verdict = self._run_check(eng, qty=5)
        self.assertFalse(verdict.allowed)
        budget = next(c for c in verdict.checks if c.name == "margin_budget")
        self.assertFalse(budget.healthy)
        # 5 lots needs 5*5000*2 + 1000 = 51000 > remaining 5000

    def test_small_order_within_remaining_passes_budget_gate(self):
        eng = mk_engine()
        eng._last_margin_avail = 100000.0
        eng.sync_margin_bookings({"A": 4000.0})
        verdict = self._run_check(eng, qty=1)
        budget = next(c for c in verdict.checks if c.name == "margin_budget")
        self.assertTrue(budget.healthy)
        # 1 lot needs 1*5000*2 + 1000 = 11000 <= remaining 95000


def _LEDGER_OFF() -> dict:
    d = dict(vars(LEDGER_ON))
    d["margin_ledger_enabled"] = False
    return d


# ---------------------------------------------------------------------------
# Regression: broker /multiorder/margin field semantics.
#
# The broker returns BOTH fields for a single 1-lot request:
#   margin_total       -> the marginal margin of THIS order (scales with qty)
#   margin_new_order   -> the account's projected margin AFTER the order
#                         (existing parked margin + this order's margin)
# A client who holds unrelated positions (e.g. manual option positions) parks
# real margin, so `margin_new_order` is inflated by exactly that parked amount.
# The engine must store `margin_total` as the per-lot figure and use it for the
# pre-trade sufficiency check — otherwise every symbol shows ~parked+req and
# the gate thinks the account can afford nothing.
# ---------------------------------------------------------------------------
MARGIN_SETTINGS = SimpleNamespace(
    product_type="INTRADAY",
    margin_ledger_enabled=True,
    margin_reserve_both_sides=False,
    margin_utilization_target=1.0,
    min_free_margin_buffer_rs=500.0,
    check_margin_before_order=True,
    halt_cooldown_sec=0.0,
    margin_refresh_sec=30,
    margin_refresh_batch=10,
    margin_api_timeout_sec=12.0,
    positions_api_timeout_sec=10.0,
    max_orders_per_minute=15,
    max_position_qty=10,
    max_daily_loss_rs=100000.0,
    max_position_age_sec=300.0,
    max_loss_per_position_rs=1500.0,
    segment="COMMODITY",
    asset_type="commodity_fut",
    lot_size=1250,
    tick_size=0.1,
    margin_per_lot_rs=0.0,
)


class BrokerMarginFieldSemanticsTest(unittest.TestCase):
    def _engine_with_margin_resp(self, margin_total, margin_new_order, margin_avail=200000.0):
        eng = mk_engine()
        eng._fyers = MagicMock()
        eng._margin_post = MagicMock(return_value={
            "s": "ok", "code": 200, "data": {
                "margin_avail": margin_avail,
                "margin_total": margin_total,
                "margin_new_order": margin_new_order,
            },
        })
        return eng

    @patch("app.infra.market_hours.is_open", return_value=True)
    @patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_refresh_stores_margin_total_not_projected_new_order(self, *_):
        # NATGASMINI 1 lot: broker says margin_total=9610 (real), but the parked
        # NIFTY options inflate margin_new_order to 9610 + 273518.75 = 283129.
        eng = self._engine_with_margin_resp(
            margin_total=9610.625, margin_new_order=283129.375)
        with patch("app.engines.risk_engine.settings", MARGIN_SETTINGS):
            loaded, ok = asyncio.run(eng.refresh_margins(["MCX:NATGASMINI26SEPFUT"], {
                "MCX:NATGASMINI26SEPFUT": 268.0,
            }))
        self.assertEqual(loaded, 1)
        self.assertEqual(ok, 1)
        # The real per-lot margin must be the marginal one (9610), never the
        # account-projection (283129) that includes parked margin.
        self.assertAlmostEqual(
            eng.margin_per_lot("MCX:NATGASMINI26SEPFUT"), 9610.625)

    @patch("app.infra.market_hours.is_open", return_value=True)
    @patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_pretrade_gate_uses_margin_total_for_sufficiency(self, *_):
        # avail 200000, real per-lot 9610 + 500 buffer -> plenty of room.
        eng = self._engine_with_margin_resp(
            margin_total=9610.625, margin_new_order=283129.375)
        with patch("app.engines.risk_engine.settings", MARGIN_SETTINGS), \
             patch("app.infra.market_hours.is_open", return_value=True), \
             patch("app.infra.market_hours.in_winddown", return_value=False):
            verdict = asyncio.run(eng.check(
                symbol="MCX:NATGASMINI26SEPFUT", strategy="mm", side=1,
                qty=1, price=268.0,
            ))
        margin = [c for c in verdict.checks if c.name == "margin"][0]
        self.assertTrue(margin.healthy, margin.detail)

    @patch("app.infra.market_hours.is_open", return_value=True)
    @patch("app.infra.market_hours.in_winddown", return_value=False)
    def test_pretrade_gate_rejects_when_margin_total_exceeds_avail(self, *_):
        inst = SimpleNamespace(
            symbol="MCX:NATGASMINI26SEPFUT", segment="COMMODITY",
            asset_type="commodity_fut", lot_size=250, tick_size=0.1,
            margin_per_lot_rs=0.0, quote_in_lots=True,
        )
        eng = self._engine_with_margin_resp(
            margin_total=9610.625, margin_new_order=283129.375, margin_avail=9000.0)
        eng._instrument = MagicMock(return_value=inst)
        with patch("app.engines.risk_engine.settings", MARGIN_SETTINGS), \
             patch("app.infra.market_hours.is_open", return_value=True), \
             patch("app.infra.market_hours.in_winddown", return_value=False):
            verdict = asyncio.run(eng.check(
                symbol="MCX:NATGASMINI26SEPFUT", strategy="mm", side=1,
                qty=1, price=268.0,
            ))
        margin = [c for c in verdict.checks if c.name == "margin"][0]
        self.assertFalse(margin.healthy, margin.detail)


class LegacyMarginModuleTest(unittest.TestCase):
    """The standalone margin.py helpers must also use margin_total semantics."""

    def test_single_order_uses_margin_total_not_new_order(self):
        # Do not import in module scope (module name has no package).
        import importlib.util
        import pathlib
        from types import SimpleNamespace as NS
        spec = importlib.util.spec_from_file_location(
            "legacy_margin", pathlib.Path(__file__).resolve().parents[1] / "legacy/margin.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class FakeFyers:
            header = "fake-auth-token"

            class service:
                @staticmethod
                def post_call(*a, **k):
                    return {
                        "s": "ok", "code": 200, "data": {
                            "margin_avail": 200000.0,
                            "margin_total": 9610.625,
                            "margin_new_order": 283129.375,
                        },
                    }
        req = mod.get_order_margin(
            FakeFyers(), symbol="MCX:NATGASMINI26SEPFUT", qty=1, side=1,
            order_type=1, product_type="INTRADAY", limit_price=268.0,
            buffer_rs=500.0,
        )
        self.assertAlmostEqual(req.margin_required, 9610.625)
        self.assertTrue(req.is_sufficient)


class FillUnitConsistencyTest(unittest.TestCase):
    """Fills arrive in broker units: MCX lots vs NFO equity-future SHARES.

    on_fill() must normalise equity-future fills (1 lot = lot_size shares) back
    to lots so the inventory gate + realized PnL use the same units as the
    15s broker reconciliation in _process().
    """

    def _mk_fill_engine(self, symbol, asset_type, lot_size):
        eng = mk_engine()
        inst = SimpleNamespace(
            symbol=symbol, segment="NFO", asset_type=asset_type,
            lot_size=lot_size, tick_size=0.05, margin_per_lot_rs=0.0,
            quote_in_lots=True,
        )
        eng._instrument = MagicMock(return_value=inst)
        return eng

    def test_equity_future_fill_shares_normalised_to_lots(self):
        eng = self._mk_fill_engine("NSE:SBIN26SEPFUT", "equity_fut", 750)
        with patch("app.engines.risk_engine.settings", LEDGER_ON):
            asyncio.run(eng.on_fill(side=1, qty=750, price=300.0, ref_price=300.0,
                                    symbol="NSE:SBIN26SEPFUT"))
        self.assertEqual(eng.position("NSE:SBIN26SEPFUT"), 1)
        # closing the 1-lot position with a +2 Rs move realizes 2*1*750 = 1500
        with patch("app.engines.risk_engine.settings", LEDGER_ON):
            asyncio.run(eng.on_fill(side=-1, qty=750, price=302.0, ref_price=302.0,
                                    symbol="NSE:SBIN26SEPFUT"))
        self.assertEqual(eng.position("NSE:SBIN26SEPFUT"), 0)
        self.assertAlmostEqual(eng.realized_pnl_for("NSE:SBIN26SEPFUT"), 1500.0)

    def test_mcx_commodity_fill_lots_passed_through(self):
        eng = self._mk_fill_engine("MCX:NATGASMINI26SEPFUT", "commodity_fut", 250)
        with patch("app.engines.risk_engine.settings", LEDGER_ON):
            asyncio.run(eng.on_fill(side=1, qty=1, price=250.0, ref_price=250.0,
                                    symbol="MCX:NATGASMINI26SEPFUT"))
        self.assertEqual(eng.position("MCX:NATGASMINI26SEPFUT"), 1)
        with patch("app.engines.risk_engine.settings", LEDGER_ON):
            asyncio.run(eng.on_fill(side=-1, qty=1, price=254.0, ref_price=254.0,
                                    symbol="MCX:NATGASMINI26SEPFUT"))
        self.assertEqual(eng.position("MCX:NATGASMINI26SEPFUT"), 0)
        # 4.0 Rs/pt * 1 lot * 250 units = 1000
        self.assertAlmostEqual(eng.realized_pnl_for("MCX:NATGASMINI26SEPFUT"), 1000.0)


class PreTradeBufferNotDoubleCountedTest(unittest.TestCase):
    def test_charge_within_remaining_that_only_covers_order_plus_global_buffer(self):
        # remaining already netted the global buffer, so an order exactly inside
        # the remaining share must pass even though charge+buffer > remaining.
        eng = mk_engine()
        eng._last_margin_avail = 100000.0
        eng.sync_margin_bookings({"A": 4000.0})  # remaining = 100000 - 4000 - 1000 = 95000
        per_lot = SimpleNamespace(margin_per_lot_rs=0.0)
        with patch("app.engines.risk_engine.settings", LEDGER_ON), \
             patch("app.infra.market_hours.is_open", return_value=True), \
             patch("app.infra.market_hours.in_winddown", return_value=False):
            eng._instrument = MagicMock(return_value=SimpleNamespace(
                symbol="MCX:NATURALGAS26SEPFUT", segment="COMMODITY",
                asset_type="commodity_fut", lot_size=1250, tick_size=0.1,
                margin_per_lot_rs=0.0, quote_in_lots=True))
            eng._margin_per_lot_by_symbol["MCX:NATURALGAS26SEPFUT"] = 47499.9
            # LEDGER_ON reserves BOTH sides (mult=2.0): charge = 2*47499.9 =
            # 94999.8 <= remaining 95000 -> must pass. The old code added the
            # global buffer a second time -> 95999.8 > 95000 -> wrongly vetoed.
            verdict = asyncio.run(eng.check(
                symbol="MCX:NATURALGAS26SEPFUT", strategy="mm", side=1,
                qty=1, price=280.0,
            ))
        budget = next(c for c in verdict.checks if c.name == "margin_budget")
        self.assertTrue(budget.healthy, budget.detail)


class LegacyNfoQtyConversionTest(unittest.TestCase):
    """Legacy scripts order/margin calls must send NFO equity-futures qty in
    underling SHARES (1 lot = LOT_SIZE shares), never bare lots — Fyers rejects
    qty=1 with "-50 not a multiple of minimum lot size"."""

    @staticmethod
    def _load_module():
        import importlib.util
        import pathlib
        spec = importlib.util.spec_from_file_location(
            "legacy_margin_mod", pathlib.Path(__file__).resolve().parents[1] / "legacy/margin.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @patch.object(config_legacy, "LOT_SIZE", 750)
    @patch.object(config_legacy, "SEGMENT", "EQUITY")
    def test_broker_qty_nfo_converts_lots_to_shares(self, *_):
        self.assertEqual(config_legacy.broker_qty(1), 750)
        self.assertEqual(config_legacy.broker_qty(2), 1500)
        self.assertEqual(config_legacy.broker_qty(1, "NSE:SBIN26SEPFUT"), 750)
        self.assertEqual(config_legacy.broker_qty(1, "NSE:SBIN-EQ"), 1)

    @patch.object(config_legacy, "LOT_SIZE", 1250)
    def test_broker_qty_commodity_and_cash_pass_through(self, *_):
        self.assertEqual(config_legacy.broker_qty(1, "MCX:NATURALGAS26SEPFUT"), 1)
        self.assertEqual(config_legacy.broker_qty(3, "MCX:NATGASMINI26SEPFUT"), 3)
        self.assertEqual(config_legacy.broker_qty(5, "NSE:SBIN-EQ"), 5)

    @patch.object(config_legacy, "LOT_SIZE", 750)
    @patch.object(config_legacy, "SEGMENT", "EQUITY")
    def test_margin_payload_sends_shares_for_nfo(self, *_):
        mod = self._load_module()
        seen = {}

        class CapFyers:
            header = "fake-auth-token"

            class service:
                @staticmethod
                def post_call(url, header, payload):
                    qty = payload["data"][0]["qty"]
                    seen["qty"] = qty
                    return {
                        "s": "ok", "code": 200, "data": {
                            "margin_avail": 200000.0,
                            "margin_total": float(qty) * 100.0,  # Rs per share
                            "margin_new_order": 200000.0 + float(qty) * 100.0,
                        },
                    }

        req = mod.get_order_margin(
            CapFyers(), symbol="NSE:SBIN26SEPFUT", qty=1, side=1,
            product_type="INTRADAY", limit_price=100.0, buffer_rs=500.0,
        )
        self.assertEqual(seen["qty"], 750)          # 1 lot -> 750 shares
        self.assertAlmostEqual(req.margin_required, 75000.0)  # 750 * 100


if __name__ == "__main__":
    unittest.main()