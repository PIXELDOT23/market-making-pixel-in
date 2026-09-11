"""
Unit tests for the transaction-cost math (the module that decides whether a
quote pair is worth taking). Pure math — no Redis/DB, no network.

Run: .venv/bin/python -m unittest tests.test_cost_engine -v
"""

import unittest
from unittest.mock import MagicMock

from app.engines.cost_engine import CostEngine


def make_cost() -> CostEngine:
    return CostEngine(bus=MagicMock(), db=MagicMock())


COMMODITY = "COMMODITY"


class OrderChargesTest(unittest.TestCase):
    """One-leg charge breakdown per segment/statute."""

    def test_commodity_sell_levies_ctt_and_buy_levies_stamp(self):
        cost = make_cost()
        turnover = 280.4 * 1 * 1250
        buy = cost.order_charges(
            280.4, 1, "BUY", segment=COMMODITY, lot_size=1250, exchange="MCX"
        )
        sell = cost.order_charges(
            280.4, 1, "SELL", segment=COMMODITY, lot_size=1250, exchange="MCX"
        )
        # CTT is SELL-only on commodities; stamp duty BUY-only.
        self.assertEqual(buy.stt_or_ctt, 0.0)
        self.assertGreater(sell.stt_or_ctt, 0.0)
        self.assertGreater(buy.stamp, 0.0)
        self.assertEqual(sell.stamp, 0.0)
        # sanity: turnover matches notional, and charges are small relative to it
        self.assertEqual(round(buy.turnover, 2), round(turnover, 2))
        self.assertLess(buy.total - buy.brokerage - buy.stamp, turnover * 0.001)

    def test_brokerage_capped_at_max(self):
        cost = make_cost()
        # hugely notional -> raw brokerage would exceed the cap of 20
        big = cost.order_charges(100000, 5, "SELL", segment=COMMODITY, lot_size=10)
        self.assertLessEqual(big.brokerage, 20.0)
        self.assertGreater(big.brokerage, 0.0)

    def test_gst_is_18pct_of_brokerage_txn_sebi(self):
        cost = make_cost()
        c = cost.order_charges(280.4, 1, "BUY", segment=COMMODITY, lot_size=1250)
        self.assertAlmostEqual(c.gst, round((c.brokerage + c.txn + c.sebi) * 0.18, 2), places=2)
        self.assertEqual(round(c.gst, 2), c.gst)


class RoundTripCostTest(unittest.TestCase):
    def test_round_trip_is_sum_of_both_legs(self):
        cost = make_cost()
        buy = cost.order_charges(280.0, 1, "BUY", segment=COMMODITY, lot_size=1250)
        sell = cost.order_charges(280.0, 1, "SELL", segment=COMMODITY, lot_size=1250)
        expected = round(buy.total + sell.total, 2)
        self.assertEqual(
            cost.round_trip_cost(280.0, 1, segment=COMMODITY, lot_size=1250),
            expected,
        )

    def test_never_negative_on_zero_price_components(self):
        cost = make_cost()
        self.assertEqual(
            cost.round_trip_cost(0.0, 1, segment=COMMODITY, lot_size=1250), 0.0
        )


class BreakevenSpreadTicksTest(unittest.TestCase):
    def test_breakeven_grows_with_costs(self):
        cost = make_cost()
        bt = cost.breakeven_spread_ticks(
            price=280.4, qty=1, product_type="INTRADAY", tick_size=0.1,
            segment=COMMODITY, lot_size=1250, max_brokerage=20.0,
        )
        self.assertGreaterEqual(bt, 1)
        # costs are fixed-ish per trade (~₹40 round trip); at ₹125/tick that is 1 tick
        self.assertEqual(bt, 1)

    def test_breakeven_scales_with_tick_value(self):
        cost = make_cost()
        thin = cost.breakeven_spread_ticks(
            price=280.4, qty=1, product_type="INTRADAY", tick_size=0.1,
            segment=COMMODITY, lot_size=50, max_brokerage=20.0,
        )
        thick = cost.breakeven_spread_ticks(
            price=280.4, qty=1, product_type="INTRADAY", tick_size=0.1,
            segment=COMMODITY, lot_size=12500, max_brokerage=20.0,
        )
        self.assertGreater(thin, thick)  # smaller tick value -> more ticks to break even


class RoundTripNetProfitTest(unittest.TestCase):
    def test_long_profit_matches_price_diff_net_of_charges(self):
        cost = make_cost()
        charges = cost.round_trip_cost(280.0, 1, segment=COMMODITY, lot_size=1250)
        net = cost.round_trip_net_profit(
            280.0, 281.0, 1, 1, segment=COMMODITY, lot_size=1250
        )
        # gross move 1 tick (₹1250); profit = gross - round-trip charges
        self.assertAlmostEqual(net, 1250.0 - charges, delta=0.5)
        self.assertGreater(charges, 0.0)

    def test_short_profits_when_price_falls(self):
        cost = make_cost()
        self.assertGreater(
            cost.round_trip_net_profit(281.0, 280.0, 1, -1, segment=COMMODITY, lot_size=1250),
            0,
        )
        self.assertLess(
            cost.round_trip_net_profit(280.0, 281.0, 1, -1, segment=COMMODITY, lot_size=1250),
            0,
        )


class ProfitableQuotePairTest(unittest.TestCase):
    def test_widens_until_min_net_profit(self):
        cost = make_cost()
        bid, ask, spread, net = cost.profitable_quote_pair(
            mid=280.4, base_spread_ticks=1, qty=1, tick_size=0.1,
            segment=COMMODITY, lot_size=1250, product_type="INTRADAY",
            max_brokerage=20.0, min_net_profit=50.0, max_widen_ticks=10,
        )
        self.assertGreaterEqual(net, 50.0)
        self.assertLessEqual(spread, 10)
        self.assertLess(bid, ask)


if __name__ == "__main__":
    unittest.main()