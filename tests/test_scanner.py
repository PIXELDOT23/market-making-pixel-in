"""
Unit tests for the scanner's pure scoring function (`compute_row`) and the
cross-asset rank sort (`rank_rows`). These decide which assets get quoted, so
a wrong weight here eats money before any order is placed.

Run: .venv/bin/python -m unittest tests.test_scanner -v
"""

import unittest
from unittest import mock

from app import schema
from app.infra.instrument import AssetType, Instrument, Segment
from app.scanner import compute_row, rank_rows, select_within_margin, ScannerCache
from app.config import settings


def commodity_inst(**kw) -> Instrument:
    base = dict(
        symbol="MCX:NATURALGAS26SEPFUT", segment=Segment.COMMODITY,
        asset_type=AssetType.COMMODITY_FUT, lot_size=1250, tick_size=0.1,
        tick_value_rs=125.0, margin_per_lot_rs=10000.0,
    )
    base.update(kw)
    return Instrument(**base)


def snap(mid=280.4, bsize=50, asize=50, connected=True, churn=0.0, volume=1000, spread_ticks=1) -> schema.MarketSnapshot:
    half = 0.05 * spread_ticks
    return schema.MarketSnapshot(
        symbol="MCX:NATURALGAS26SEPFUT", ltp=mid, bid=mid - half, ask=mid + half,
        mid=mid, bid_size=bsize, ask_size=asize, tick_count=50,
        churn_ticks_per_sec=churn, last_tick_ts=0.0, volume=volume, is_connected=connected,
    )


def signal(vol=0, quoteable=True, liq=1.0) -> schema.SignalMetrics:
    return schema.SignalMetrics(
        symbol="MCX:NATURALGAS26SEPFUT", strategy="mm", ts=0.0, mid=None,
        churn_ticks_per_sec=float(vol), vol_widening_ticks=int(vol),
        liquidity_grade=liq, spread_ticks_now=2, quoteable=quoteable,
    )


def row(**kw) -> schema.ScannerRow:
    params = dict(
        inst=commodity_inst(), snap=snap(), signal=signal(),
        margin_avail=50000.0, margin_per_lot=10000.0, max_position_qty=10,
        margin_risk_fraction=0.2, vol_reduction_per_tick=0.1, min_liquidity=0.5,
    )
    params.update(kw)
    return compute_row(symbol="MCX:NATURALGAS26SEPFUT", **params)


class ComputeRowScoringTest(unittest.TestCase):
    def test_healthy_market_scores_full_and_quoteable(self):
        def cost_calc(mid, qty):
            return schema.ScanCost(
                net_profit_rs=10.0, round_trip_charges_rs=20.0,
                breakeven_spread_ticks=1, required_spread_ticks=2, profitable=True,
            )
        r = row(cost_calc=cost_calc)
        self.assertEqual(r.score, 1.0)
        self.assertTrue(r.quoteable)
        self.assertTrue(r.profitable)

    def test_disconnected_feed_never_quoteable(self):
        r = row(snap=snap(connected=False))
        self.assertFalse(r.quoteable)
        self.assertEqual(r.score, 0.1)  # 1.0 - 0.9
        self.assertIn("feed down", r.reasons)

    def test_thin_book_penalized_and_blocked(self):
        r = row(snap=snap(bsize=25, asize=100), signal=signal(liq=0.25), min_liquidity=0.5)
        # liq 0.25 < 0.5 -> -0.4, not quoteable
        self.assertFalse(r.quoteable)
        self.assertLess(r.score, 0.7)

    def test_vol_widening_penalty_capped(self):
        r = row(signal=signal(vol=10))
        # -min(0.6, 1.2) -> exactly 0.6 lower on a clean 1-tick book
        self.assertAlmostEqual(r.score, 0.4, places=3)
        self.assertIn("vol widen 10t", r.reasons)

    def test_signal_blocked_any_reason_not_quoteable(self):
        r = row(signal=signal(quoteable=False))
        self.assertFalse(r.quoteable)

    def test_unprofitable_cost_blocked_and_deranked(self):
        def cost_calc(mid, qty):
            return schema.ScanCost(
                net_profit_rs=-5.0, round_trip_charges_rs=30.0,
                breakeven_spread_ticks=2, required_spread_ticks=3, profitable=False,
            )
        r = row(cost_calc=cost_calc)
        self.assertFalse(r.quoteable)
        self.assertAlmostEqual(r.score, 0.6, places=3)  # 1.0 - 0.4
        self.assertIn("charges eat spread", " ".join(r.reasons))

    def test_margin_below_one_lot_blocks_commodity(self):
        r = row(margin_avail=5000.0, margin_per_lot=10000.0)
        self.assertFalse(r.quoteable)
        self.assertIn("margin < 1 lot", r.reasons)

    def test_stood_down_row_still_reports_per_lot_req(self):
        # A margin-blocked commodity (quote_qty 0) must still show the 1-lot
        # requirement so the dashboard "req" column is never blank.
        r = row(margin_avail=5000.0, margin_per_lot=10000.0)
        self.assertEqual(r.quote_qty, 0)
        self.assertAlmostEqual(r.margin_req_rs, 10000.0)

    def test_spread_ticks_from_book(self):
        r = row(snap=snap(mid=280.4, spread_ticks=2))
        self.assertEqual(r.spread_ticks, 2)

    def test_commodity_sizes_up_to_margin_headroom(self):
        # Commodity is no longer hard-pinned at 1 lot: with 40k margin and
        # 10k/lot the scanner reserves/marks 4 lots so the greedy budget and the
        # live commodity strategy sizing agree.
        r = row(margin_avail=40000.0, margin_per_lot=10000.0, max_position_qty=10)
        self.assertTrue(r.quoteable)
        self.assertEqual(r.quote_qty, 4)
        self.assertAlmostEqual(r.margin_req_rs, 40000.0)

    def test_commodity_zero_lot_on_insufficient_margin(self):
        r = row(margin_avail=9000.0, margin_per_lot=10000.0)
        self.assertFalse(r.quoteable)
        self.assertIn("margin < 1 lot", " ".join(r.reasons))


class GreedyBudgetTargetTest(unittest.TestCase):
    """select_within_margin receives the utilization-target-scaled budget from
    ScannerCache.refresh, so e.g. 0.95 keeps 5% of the account free while still
    deploying almost everything."""

    def test_scan_cache_scales_budget_by_target(self):
        def mk(symbol, req):
            return schema.ScannerRow(
                symbol=symbol, rank=0, segment="COMMODITY", asset_type="commodity_fut",
                ltp=1.0, bid=0.9, ask=1.1, mid=1.0, bid_size=10, ask_size=10,
                spread_ticks=2, liquidity_grade=1.0, churn_ticks_per_sec=0.0,
                vol_widening_ticks=0, quoteable=True, margin_avail=1000.0,
                margin_per_lot=req, quote_qty=1, lot_size=1, score=0.9,
                profitable=True, volume=100, margin_req_rs=req,
            )

        rows = [mk("A", 300.0), mk("B", 300.0)]
        # the greedy budget is the utilisation target x avail: at 50% only A fits
        budget = 1000.0 * 0.5
        select_within_margin(rows, 10, budget)
        self.assertTrue(rows[0].quoteable)
        self.assertFalse(rows[1].quoteable)
        self.assertIn("margin block", " ".join(rows[1].reasons))
        # without the target (full avail) both fit
        rows2 = [mk("A", 300.0), mk("B", 300.0)]
        select_within_margin(rows2, 10, 1000.0)
        self.assertTrue(all(r.quoteable for r in rows2))


class RankRowsTest(unittest.TestCase):
    def test_profitable_rows_sort_first_then_volume_then_score(self):
        def mk(symbol, profitable, volume, score):
            return schema.ScannerRow(
                symbol=symbol, rank=0, segment="COMMODITY", asset_type="commodity_fut",
                ltp=1.0, bid=0.9, ask=1.1, mid=1.0, bid_size=10, ask_size=10,
                spread_ticks=2, liquidity_grade=1.0, churn_ticks_per_sec=0.0,
                vol_widening_ticks=0, quoteable=True, margin_avail=1.0,
                margin_per_lot=1.0, quote_qty=1, lot_size=1, score=score,
                profitable=profitable, volume=volume,
            )
        rows = [
            mk("lo-vol-prof", True, 1, 0.9),
            mk("lo-sc-prof", True, 10, 0.5),
            mk("hi-vol-noprof", False, 9999, 0.9),
            mk("hi-vol-prof", True, 100, 0.8),
        ]
        ranked = rank_rows(rows, active_limit=4)
        self.assertEqual(ranked[0].symbol, "hi-vol-prof")     # profitable, most volume
        self.assertEqual(ranked[1].symbol, "lo-sc-prof")      # profitable, mid volume even at low score
        self.assertEqual(ranked[2].symbol, "lo-vol-prof")
        self.assertEqual(ranked[3].symbol, "hi-vol-noprof")   # non-profitable last regardless of volume

    def test_margin_unknown_is_lenient_capped_by_count_only(self):
        def mk(symbol, req):
            return schema.ScannerRow(
                symbol=symbol, rank=0, segment="COMMODITY", asset_type="commodity_fut",
                ltp=1.0, bid=0.9, ask=1.1, mid=1.0, bid_size=10, ask_size=10,
                spread_ticks=2, liquidity_grade=1.0, churn_ticks_per_sec=0.0,
                vol_widening_ticks=0, quoteable=True, margin_avail=0.0,
                margin_per_lot=0.0, quote_qty=1, lot_size=1, score=0.9,
                profitable=True, volume=100, margin_req_rs=req,
            )
        rows = [mk("A", 5000.0), mk("B", 9000.0), mk("C", 7000.0)]
        select_within_margin(rows, active_limit=2, margin_avail=0.0, buffer_rs=1000.0)
        # unknown margin -> no budget, but active_limit still cuts at 2
        self.assertTrue(rows[0].quoteable)
        self.assertTrue(rows[1].quoteable)
        self.assertFalse(rows[2].quoteable)

    def test_known_margin_stands_down_overflow_rows(self):
        def mk(symbol, req):
            return schema.ScannerRow(
                symbol=symbol, rank=0, segment="COMMODITY", asset_type="commodity_fut",
                ltp=1.0, bid=0.9, ask=1.1, mid=1.0, bid_size=10, ask_size=10,
                spread_ticks=2, liquidity_grade=1.0, churn_ticks_per_sec=0.0,
                vol_widening_ticks=0, quoteable=True, margin_avail=10000.0,
                margin_per_lot=1.0, quote_qty=1, lot_size=1, score=0.9,
                profitable=True, volume=100, margin_req_rs=req,
            )
        rows = [mk("A", 6000.0), mk("B", 7000.0)]
        select_within_margin(rows, active_limit=10, margin_avail=10000.0)
        self.assertTrue(rows[0].quoteable)
        self.assertFalse(rows[1].quoteable)
        self.assertIn("margin block", " ".join(rows[1].reasons))

    def test_both_sides_multiplier_and_buffer_charge_into_budget(self):
        def mk(symbol, req):
            return schema.ScannerRow(
                symbol=symbol, rank=0, segment="COMMODITY", asset_type="commodity_fut",
                ltp=1.0, bid=0.9, ask=1.1, mid=1.0, bid_size=10, ask_size=10,
                spread_ticks=2, liquidity_grade=1.0, churn_ticks_per_sec=0.0,
                vol_widening_ticks=0, quoteable=True, margin_avail=12000.0,
                margin_per_lot=1.0, quote_qty=1, lot_size=1, score=0.9,
                profitable=True, volume=100, margin_req_rs=req,
            )
        rows = [mk("A", 5000.0), mk("B", 1000.0)]
        # A charges 5000*2 + 1000 buffer = 11000 -> fits; B would push 13000 > 12000
        select_within_margin(rows, active_limit=10, margin_avail=12000.0,
                             buffer_rs=1000.0, reserve_multiplier=2.0)
        self.assertTrue(rows[0].quoteable)
        self.assertFalse(rows[1].quoteable)


if __name__ == "__main__":
    unittest.main()