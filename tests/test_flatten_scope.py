"""Regression tests: flatten/kill-switch paths must ONLY touch positions and
orders on symbols the bot itself trades (the registered universe). A manually
punched order on any other symbol (e.g. an options position taken by hand) must
never be adopted, cancelled, or squared off.

This guards the boot-time orphan reconcile, HALT_ALL / FLATTEN commands,
wind-down flatten_segment and the shutdown kill switch.
"""

import asyncio
import unittest

from app.engines.execution_engine import ExecutionEngine
from app.infra.auth import TokenStore
from app.infra.db import Database
from app.infra.instrument import Segment, build_instrument, instrument_registry
from app.infra.redis import RedisBus


class _Exec(ExecutionEngine):
    """ExecutionEngine stub: captures cancel/square-off calls, fakes the broker."""

    def __init__(self):
        super().__init__(RedisBus(), Database(), TokenStore(RedisBus()))
        self.actions = []

    async def _call(self, method, data=None, get=None, timeout=10.0):
        if get == "positions":
            return {
                "netPositions": [
                    {"symbol": "NSE:MANAGED26SEPFUT", "netQty": 175},
                    {"symbol": "NSE:NIFTY2691523450PE", "netQty": 650},
                    {"symbol": "MCX:LEAD26SEPFUT", "netQty": 1},
                ]
            }
        if get == "orderbook":
            return {
                "orders": [
                    {"id": "mgd1", "symbol": "NSE:MANAGED26SEPFUT", "status": 6},
                    {"id": "mnl1", "symbol": "NSE:NIFTY2691523450PE", "status": 6},
                ]
            }
        return {}

    async def cancel_order(self, oid):
        self.actions.append(("cancel", oid))

    async def square_off(self, strategy, symbol, qty, side, product_type="INTRADAY"):
        self.actions.append(("so", symbol, side, qty))

    async def attach(self):
        pass


class FlattenManagedScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        instrument_registry.clear()
        instrument_registry.register(
            build_instrument("NSE:MANAGED26SEPFUT", Segment.EQUITY_FUT, 175, 0.05, 50000.0)
        )
        instrument_registry.register(
            build_instrument("MCX:LEAD26SEPFUT", Segment.COMMODITY, 25, 0.05, 24900.0)
        )

    @classmethod
    def tearDownClass(cls):
        instrument_registry.clear()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_flatten_all_touches_only_managed_symbols(self):
        e = _Exec()
        self._run(e.flatten_all())
        symbols = [a[1] for a in e.actions if a[0] == "so"]
        self.assertIn("NSE:MANAGED26SEPFUT", symbols)
        self.assertIn("MCX:LEAD26SEPFUT", symbols)
        self.assertNotIn("NSE:NIFTY2691523450PE", symbols)

    def test_flatten_all_cancels_only_managed_orders(self):
        e = _Exec()
        self._run(e.flatten_all())
        cancelled = [a[1] for a in e.actions if a[0] == "cancel"]
        self.assertIn("mgd1", cancelled)
        self.assertNotIn("mnl1", cancelled)

    def test_flatten_segment_skips_unmanaged(self):
        e = _Exec()
        self._run(e.flatten_segment("NSE"))
        actions = e.actions
        self.assertTrue(any(a[0] == "so" and a[1] == "NSE:MANAGED26SEPFUT" for a in actions))
        self.assertFalse(any(a[0] == "so" and "NIFTY2691523450PE" in a[1] for a in actions))
        self.assertTrue(any(a[0] == "cancel" and a[1] == "mgd1" for a in actions))
        self.assertFalse(any(a[0] == "cancel" and a[1] == "mnl1" for a in actions))


if __name__ == "__main__":
    unittest.main(verbosity=2)