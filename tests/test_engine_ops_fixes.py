"""
tests/test_engine_ops_fixes.py
------------------------------
Regression tests for the second-pass operations fixes:

  * shares_to_lots() flooring (equity futures): sub-lot NFO fills never book a
    giant false lot count, and flatten can never oversell into a short.
  * _position_lots uses floor (no banker's-round oversell on flatten).
  * OrderEngine _handle_event books a partially-filled-then-CANCELED order.
  * SignalEngine measures spread/churn against the per-symbol tick size, not
    one global tick.
  * EngineManager supervisor restarts a dead engine task.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import schema
from app.infra.instrument import shares_to_lots, instrument_registry

FILL_SETTINGS = SimpleNamespace(
    max_position_qty=1,
    max_daily_loss_rs=1_000_000_000.0,
)


def mk_equity_fut_inst(lot_size=750):
    return SimpleNamespace(
        symbol="NSE:SBIN26SEPFUT", segment="EQUITY_FUT", asset_type="equity_fut",
        lot_size=lot_size, tick_size=0.05, margin_per_lot_rs=0.0, quote_in_lots=True,
    )


def mk_mcx_inst(lot_size=250):
    return SimpleNamespace(
        symbol="MCX:NATGASMINI26SEPFUT", segment="COMMODITY", asset_type="commodity_fut",
        lot_size=lot_size, tick_size=0.10, margin_per_lot_rs=0.0, quote_in_lots=True,
    )


class SharesToLotsTest(unittest.TestCase):
    def test_floor_never_rounds_sub_lot_up(self):
        self.assertEqual(shares_to_lots(750, 750), 1)
        self.assertEqual(shares_to_lots(1500, 750), 2)
        self.assertEqual(shares_to_lots(1900, 750), 2)   # round() gave 3 -> oversell
        self.assertEqual(shares_to_lots(1150, 750), 1)   # round() gave 2 -> oversell
        self.assertEqual(shares_to_lots(400, 750), 0)    # sub-lot: invisible, not "400 lots"

    def test_signed_floor_preserves_side(self):
        self.assertEqual(shares_to_lots(-1900, 750), -2)
        self.assertEqual(shares_to_lots(-400, 750), 0)

    def test_non_positive_lot_size_passthrough(self):
        self.assertEqual(shares_to_lots(5, 0), 5)


class SubLotNfoFillTest(unittest.TestCase):
    """A partial equity-future fill (400 of 750 shares) must NOT book as
    ``net=400`` FALSE lots — the inventory gate (limit ±1) would trip and the
    position would strand/to overshoot quoting at the same time."""

    def _engine(self, inst):
        from app.engines.risk_engine import RiskEngine
        eng = RiskEngine(bus=MagicMock(), db=MagicMock(), token_store=MagicMock())
        eng._instrument = MagicMock(return_value=inst)
        return eng

    def test_sub_lot_fill_does_not_false_lots(self):
        eng = self._engine(mk_equity_fut_inst())
        with patch("app.engines.risk_engine.settings", FILL_SETTINGS):
            asyncio.run(eng.on_fill(side=1, qty=400, price=300.0, ref_price=300.0,
                                    symbol="NSE:SBIN26SEPFUT"))
        self.assertEqual(eng.position("NSE:SBIN26SEPFUT"), 0)   # not 400
        self.assertTrue(eng.constraints_healthy)

    def test_sub_lot_fills_converge_via_broker_reconcile(self):
        # Floor-per-fill keeps the int-lot book invariant; a sub-lot remainder
        # is picked up by the 15s broker reconciliation and normalised once the
        # accumulated shares equal a whole lot.
        from app.engines.risk_engine import RiskEngine
        eng = RiskEngine(bus=MagicMock(), db=MagicMock(), token_store=MagicMock())
        eng._instrument = MagicMock(return_value=mk_equity_fut_inst())
        eng._fyers = MagicMock()
        eng._fyers.positions.return_value = {
            "s": "ok",
            "netPositions": [{"symbol": "NSE:SBIN26SEPFUT", "netQty": 1900}],
        }
        SET = SimpleNamespace(
            symbol="NSE:SBIN26SEPFUT", positions_poll_interval_sec=0.01,
            positions_api_timeout_sec=10.0,
            max_position_qty=1, max_daily_loss_rs=1_000_000_000.0,
        )
        with patch("app.engines.risk_engine.settings", SET):
            asyncio.run(eng._process())
        self.assertEqual(eng.position("NSE:SBIN26SEPFUT"), 2)


class FlattenNeverOversellsTest(unittest.TestCase):
    """kill-switch / wind-down flatten must close whole lots WITHOUT rounding up
    into an opposite-side short (round(1900/750)=3 -> sells 2250 on 1900)."""

    def _exec(self):
        from app.engines.execution_engine import ExecutionEngine
        return ExecutionEngine(
            bus=MagicMock(), db=MagicMock(), token_store=MagicMock(), risk=MagicMock(),
        )

    def test_position_lots_floors_equity_futures(self):
        eng = self._exec()
        self.assertEqual(eng._position_lots(1900, mk_equity_fut_inst()), 2)
        self.assertEqual(eng._position_lots(1150, mk_equity_fut_inst()), 1)
        self.assertEqual(eng._position_lots(400, mk_equity_fut_inst()), 0)

    def test_position_lots_passthrough_mcx_lots(self):
        eng = self._exec()
        self.assertEqual(eng._position_lots(3, mk_mcx_inst()), 3)

    def test_flatten_all_never_sends_oversized_lots(self):
        eng = self._exec()
        calls = []
        async def cancel_none():
            return None
        eng.cancel_open_orders = cancel_none
        eng._managed = MagicMock(return_value=True)
        eng._instrument = MagicMock(return_value=mk_equity_fut_inst())

        async def fake_call(*a, **k):
            return {"netPositions": [{"symbol": "NSE:SBIN26SEPFUT", "netQty": 1900}]}
        eng._call = fake_call

        async def square(why, sym, qty, side):
            calls.append((why, sym, qty, side))
        eng.square_off = square
        asyncio.run(eng.flatten_all())
        self.assertEqual(calls, [("killswitch", "NSE:SBIN26SEPFUT", 2, -1)])

    def test_flatten_skips_sub_lot_remainder(self):
        eng = self._exec()
        calls = []
        eng.cancel_open_orders = MagicMock()
        eng._managed = MagicMock(return_value=True)
        eng._instrument = MagicMock(return_value=mk_equity_fut_inst())

        async def fake_call(*a, **k):
            return {"netPositions": [{"symbol": "NSE:SBIN26SEPFUT", "netQty": 400}]}
        eng._call = fake_call

        async def square(why, sym, qty, side):
            calls.append((why, sym, qty, side))
        eng.square_off = square
        asyncio.run(eng.flatten_all())
        self.assertEqual(calls, [])   # sub-lot cannot be lawfully closed -> no short


class PartialFillThenCancelTest(unittest.TestCase):
    """A CANCELED order that already filled 400 of 750 shares must be booked:
    the broker holds those shares even though the order is gone. Before this
    fix the book stayed net=0 until the 15s REST reconcile, letting a second
    lot fill on top of the unbooked one."""

    def test_canceled_with_fills_is_booked(self):
        from app.engines.execution_engine import ExecutionEngine
        risk = MagicMock()
        eng = ExecutionEngine(bus=MagicMock(), db=MagicMock(), token_store=MagicMock(), risk=risk)
        ev = schema.OrderEvent(
            ts=0.0, broker_order_id="O1", strategy="mm", symbol="NSE:SBIN26SEPFUT",
            side=1, qty=750, status=1, status_label="CANCELED",
            limit_price=300.0, filled_qty=400, traded_price=300.0, raw={},
        )
        asyncio.run(eng._handle_event(ev))
        risk.on_fill.assert_called_once()
        call_kw = risk.on_fill.call_args.kwargs
        self.assertEqual(call_kw["qty"], 400)
        self.assertEqual(call_kw["side"], 1)
        self.assertEqual(call_kw["symbol"], "NSE:SBIN26SEPFUT")

    def test_rejected_with_zero_fills_not_booked(self):
        from app.engines.execution_engine import ExecutionEngine
        risk = MagicMock()
        eng = ExecutionEngine(bus=MagicMock(), db=MagicMock(), token_store=MagicMock(), risk=risk)
        ev = schema.OrderEvent(
            ts=0.0, broker_order_id="O2", strategy="mm", symbol="MCX:NATGASMINI26SEPFUT",
            side=-1, qty=1, status=5, status_label="REJECTED",
            limit_price=250.0, filled_qty=0, traded_price=0.0, raw={},
        )
        asyncio.run(eng._handle_event(ev))
        risk.on_fill.assert_not_called()


class PerSymbolTickSignalTest(unittest.TestCase):
    def _signal(self):
        from app.engines.signal_engine import SignalEngine
        return SignalEngine(bus=MagicMock(), db=MagicMock(), tick_size=0.05)

    def _tick(self, symbol, bid, ask, ltp):
        return SimpleNamespace(
            symbol=symbol, ts=0.0, ltp=ltp, bid=bid, ask=ask,
            bid_size=10, ask_size=10,
        )

    def test_spread_measured_with_per_symbol_tick(self):
        sfx = self._signal()
        by_sym = {
            "MCX:X26SEPFUT": SimpleNamespace(tick_size=0.10),
            "NSE:Y26SEPFUT": SimpleNamespace(tick_size=0.05),
        }
        with patch.object(instrument_registry, "get", side_effect=lambda s: by_sym.get(s)):
            mcx = sfx.compute(self._tick("MCX:X26SEPFUT", 100.00, 100.10, 100.05))
            nfo = sfx.compute(self._tick("NSE:Y26SEPFUT", 100.00, 100.10, 100.05))
        # same 0.10 raw spread: 1 tick on MCX (tick 0.10), 2 ticks on NFO
        self.assertEqual(mcx.spread_ticks_now, 1)
        self.assertEqual(nfo.spread_ticks_now, 2)


class SupervisorRestartTest(unittest.TestCase):
    def test_dead_engine_task_is_restarted(self):
        from app.container import EngineManager

        class Flaky:
            def __init__(self):
                self._stop = asyncio.Event()
                self._task = None
                self.status = "healthy"
                self.detail = ""

            async def _run_wrapper(self):
                raise RuntimeError("boom")

        async def scenario():
            mgr = EngineManager.__new__(EngineManager)
            mgr._tasks_done = False
            mgr._tasks = []
            eng = Flaky()
            eng._task = asyncio.create_task(eng._run_wrapper(), name="engine:x")
            first = eng._task
            sup = asyncio.create_task(mgr._supervise("x", eng, restart_sec=0.05))
            await asyncio.sleep(0.4)
            result = (
                eng.status == "healthy"
                and eng.detail.startswith("restarted")
                and eng._task is not None
                and eng._task is not first   # a NEW task was created (one restart, at least)
            )
            mgr._tasks_done = True
            sup.cancel()
            try:
                await sup
            except asyncio.CancelledError:
                pass
            return result

        self.assertTrue(asyncio.run(scenario()))


if __name__ == "__main__":
    unittest.main()