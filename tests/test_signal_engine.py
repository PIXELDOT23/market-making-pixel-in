"""
Unit tests for signal-engine live volatility (churn) measurement and the
volatility-widening / halting rules that gate quoting in fast markets.

Run: .venv/bin/python -m unittest tests.test_signal_engine -v
"""

import unittest
from collections import deque
from unittest.mock import MagicMock, patch

from app.engines.signal_engine import SignalEngine


def mk_engine(**kw) -> SignalEngine:
    kwargs = dict(bus=MagicMock(), db=MagicMock(), tick_size=0.1)
    kwargs.update(kw)
    return SignalEngine(**kwargs)


class ChurnFromHistTest(unittest.TestCase):
    def test_flat_price_is_zero_churn(self):
        with patch("app.engines.signal_engine.time.time", return_value=1000.0):
            eng = mk_engine()
            hist = deque([(900.0, 100.0), (950.0, 100.0), (990.0, 100.0)])
            self.assertEqual(eng._churn_from_hist(hist, 120.0, 0.1), 0.0)

    def test_distance_over_elapsed_in_ticks(self):
        with patch("app.engines.signal_engine.time.time", return_value=1000.0):
            eng = mk_engine()
            # two ticks of movement (101->100 and back) across 90s
            hist = deque([(900.0, 100.0), (950.0, 101.0), (990.0, 100.0)])
            self.assertAlmostEqual(
                eng._churn_from_hist(hist, 120.0, 0.1), 2.0 / 90.0 / 0.1, places=6
            )

    def test_insufficient_history_is_zero(self):
        with patch("app.engines.signal_engine.time.time", return_value=1000.0):
            eng = mk_engine()
            self.assertEqual(eng._churn_from_hist(deque(), 120.0, 0.1), 0.0)
            self.assertEqual(eng._churn_from_hist(deque([(999.0, 100.0)]), 120.0, 0.1), 0.0)

    def test_stale_samples_outside_window_are_trimmed(self):
        with patch("app.engines.signal_engine.time.time", return_value=1000.0):
            eng = mk_engine()
            # 850 is before cutoff (880) -> only 900/950 count: 1 tick over 50s
            hist = deque([(850.0, 100.0), (900.0, 101.0), (950.0, 100.0)])
            self.assertAlmostEqual(eng._churn_from_hist(hist, 120.0, 0.1), 1.0 / 50.0 / 0.1, places=6)

    def test_zero_elapsed_gives_zero(self):
        with patch("app.engines.signal_engine.time.time", return_value=1000.0):
            eng = mk_engine()
            hist = deque([(999.0, 100.0), (999.0, 101.0)])
            self.assertEqual(eng._churn_from_hist(hist, 120.0, 0.1), 0.0)


class ComputeHaltingTest(unittest.TestCase):
    def _patch_settings(self, halt_tps):
        from types import SimpleNamespace
        from app.engines import signal_engine
        return patch(
            "app.engines.signal_engine.settings",
            SimpleNamespace(
                volatility_window_sec=120.0,
                volatility_widen_factor=0.5,
                max_spread_widen_ticks=10,
                volatility_halt_quoting_tps=halt_tps,
            ),
        )

    def test_excessive_churn_halts_quoting(self):
        from app import schema
        eng = mk_engine()
        with self._patch_settings(halt_tps=0.04):
            with patch("app.engines.signal_engine.time.time", return_value=1000.0):
                eng._churn_hist["MCX:TEST"] = deque(
                    [(900.0, 100.0), (920.0, 102.0), (940.0, 100.0), (960.0, 102.0), (980.0, 100.0)]
                )
                tick = schema.MarketTick(
                    symbol="MCX:TEST", ts=1000.0, ltp=100.0, bid=99.9, ask=100.1,
                    bid_size=10, ask_size=10, volume=100,
                )
                sig = eng.compute(tick)
                # ~8 ticks of movement over 100s = 0.08 ticks/s > 0.04 threshold
                self.assertFalse(sig.quoteable)
                self.assertIn("excessive churn", sig.reasons)

    def test_calm_market_stays_quoteable(self):
        from app import schema
        eng = mk_engine()
        with self._patch_settings(halt_tps=5.0):
            with patch("app.engines.signal_engine.time.time", return_value=1000.0):
                eng._churn_hist["MCX:TEST"] = deque(
                    [(900.0, 100.0), (1000.0, 100.2)]
                )
                tick = schema.MarketTick(
                    symbol="MCX:TEST", ts=1000.0, ltp=100.0, bid=99.9, ask=100.1,
                    bid_size=10, ask_size=10, volume=100,
                )
                sig = eng.compute(tick)
                self.assertTrue(sig.quoteable)


if __name__ == "__main__":
    unittest.main()