"""
Unit tests for position sizing: the volatility-scaled dynamic size for
equity/equity-futures and the pinned-lot commodity policy. Wrong sizes here
mean margin blow-ups or wasted edge.

Run: .venv/bin/python -m unittest tests.test_sizing -v
"""

import unittest

from app.infra.instrument import AssetType, Instrument, Segment
from app.sizing import (
    _vol_scale,
    commodity_size,
    dynamic_size,
    quote_size_for,
)


def equity_fut_inst() -> Instrument:
    return Instrument(
        symbol="NSE:NIFTYAUGFUT", segment=Segment.EQUITY_FUT,
        asset_type=AssetType.EQUITY_FUT, lot_size=75, tick_size=0.05,
        tick_value_rs=3.75, margin_per_lot_rs=50000.0,
    )


def cash_equity_inst() -> Instrument:
    return Instrument(
        symbol="NSE:RELIANCE", segment=Segment.EQUITY,
        asset_type=AssetType.EQUITY, lot_size=1, tick_size=0.05,
        tick_value_rs=0.05, margin_per_lot_rs=5000.0, quote_in_lots=False,
    )


def commodity_inst() -> Instrument:
    return Instrument(
        symbol="MCX:NATURALGAS26SEPFUT", segment=Segment.COMMODITY,
        asset_type=AssetType.COMMODITY_FUT, lot_size=1250, tick_size=0.1,
        tick_value_rs=125.0, margin_per_lot_rs=10000.0,
    )


class VolScaleTest(unittest.TestCase):
    def test_no_vol_full_size(self):
        self.assertEqual(_vol_scale(0, 0.10), 1.0)

    def test_ten_percent_per_tick(self):
        self.assertAlmostEqual(_vol_scale(3, 0.10), 0.70)

    def test_floor_at_quarter(self):
        self.assertEqual(_vol_scale(50, 0.10), 0.25)

    def test_negative_ticks_treated_as_no_vol(self):
        self.assertEqual(_vol_scale(-1, 0.10), 1.0)


class DynamicSizeTest(unittest.TestCase):
    def test_margin_driven_size(self):
        size, reason = dynamic_size(
            equity_fut_inst(), margin_avail=100000.0, margin_fraction=0.2,
            margin_per_lot=5000.0, inventory=0, max_position_qty=10,
        )
        self.assertEqual(size, 4)
        self.assertIn("margin-based 4", reason)

    def test_volatility_scales_down(self):
        size, _ = dynamic_size(
            equity_fut_inst(), margin_avail=100000.0, margin_fraction=0.2,
            margin_per_lot=5000.0, inventory=0, max_position_qty=10,
            vol_widening_ticks=3, vol_reduction_per_tick=0.10,
        )
        self.assertEqual(size, 2)  # floor(4 * 0.70)

    def test_pinned_to_position_headroom(self):
        size, _ = dynamic_size(
            equity_fut_inst(), margin_avail=10000000.0, margin_fraction=0.9,
            margin_per_lot=5000.0, inventory=8, max_position_qty=10,
        )
        self.assertEqual(size, 2)  # headroom only

    def test_min_size_when_margin_thin(self):
        size, reason = dynamic_size(
            equity_fut_inst(), margin_avail=1000.0, margin_fraction=0.2,
            margin_per_lot=5000.0, inventory=0, max_position_qty=10,
        )
        self.assertEqual(size, 1)
        self.assertIn("min size", reason)

    def test_unknown_margin_per_lot_estimated_from_mid(self):
        size, _ = dynamic_size(
            equity_fut_inst(), margin_avail=100000.0, margin_fraction=0.2,
            margin_per_lot=0.0, inventory=0, max_position_qty=10, mid=1000.0,
        )
        # estimate = 10% of notional (1000 * 75) = 7500 -> 2 lots from 20000/7500
        self.assertEqual(size, 2)


class CommoditySizeTest(unittest.TestCase):
    def test_margin_backed_single_lot(self):
        qty, reason = commodity_size(
            commodity_inst(), margin_avail=50000.0, margin_fraction=1.0,
            margin_per_lot=10000.0, max_lots=1,
        )
        self.assertEqual(qty, 1)
        self.assertIn("margin-backed", reason)

    def test_insufficient_margin_stands_down(self):
        qty, reason = commodity_size(
            commodity_inst(), margin_avail=5000.0, margin_fraction=1.0,
            margin_per_lot=10000.0, max_lots=1,
        )
        self.assertEqual(qty, 0)
        self.assertIn("margin insufficient", reason)

    def test_unknown_margin_quotes_pinned_leniently(self):
        qty, reason = commodity_size(
            commodity_inst(), margin_avail=0.0, margin_fraction=1.0,
            margin_per_lot=0.0, max_lots=1,
        )
        self.assertEqual(qty, 1)
        self.assertIn("margin unknown", reason)


class QuoteSizeForDispatchTest(unittest.TestCase):
    def test_dispatch_commodity_to_pinned_policy(self):
        qty, _ = quote_size_for(
            commodity_inst(), margin_avail=50000.0, margin_fraction=1.0,
            margin_per_lot=10000.0, inventory=0, max_position_qty=1,
        )
        self.assertEqual(qty, 1)

    def test_dispatch_cash_equity_to_dynamic(self):
        size, _ = quote_size_for(
            cash_equity_inst(), margin_avail=100000.0, margin_fraction=0.2,
            margin_per_lot=5000.0, inventory=0, max_position_qty=20,
        )
        # 20000/5000 = 4 shares
        self.assertEqual(size, 4)


if __name__ == "__main__":
    unittest.main()