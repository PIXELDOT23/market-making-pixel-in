"""
Unit tests for market-hours: US-DST determination (NSE/MCX close-shift logic)
and session open/close windows.

Run: .venv/bin/python -m unittest tests.test_market_hours -v
"""

import unittest
from datetime import datetime, timezone

from app.infra import market_hours
from app.infra.market_hours import (
    _sess,
    _us_dst_in_effect,
    is_open,
    segment_id,
    Session,
)

UTC = timezone.utc

# US DST in 2026: beginning 2nd Sunday of March (Mar 8), end 1st Sunday of Nov (Nov 1).


class UsDstTest(unittest.TestCase):
    def test_summer_is_dst(self):
        self.assertTrue(_us_dst_in_effect(datetime(2026, 6, 1, tzinfo=UTC)))

    def test_deep_winter_is_not_dst(self):
        self.assertFalse(_us_dst_in_effect(datetime(2026, 1, 15, tzinfo=UTC)))

    def test_march_boundary_flips_in(self):
        self.assertFalse(_us_dst_in_effect(datetime(2026, 3, 7, tzinfo=UTC)))
        self.assertTrue(_us_dst_in_effect(datetime(2026, 3, 8, 0, 0, tzinfo=UTC)))

    def test_november_boundary_flips_out(self):
        self.assertTrue(_us_dst_in_effect(datetime(2026, 10, 31, tzinfo=UTC)))
        self.assertFalse(_us_dst_in_effect(datetime(2026, 11, 1, 0, 0, tzinfo=UTC)))


class SessionCloseDstTest(unittest.TestCase):
    def test_mcx_close_shifts_with_us_dst(self):
        sess = Session("09:00", "23:30", "23:55")
        self.assertEqual(sess.close(datetime(2026, 6, 1, tzinfo=UTC)), (23, 30))
        self.assertEqual(sess.close(datetime(2026, 1, 15, tzinfo=UTC)), (23, 55))

    def test_session_without_override_never_shifts(self):
        sess = Session("09:15", "15:30")
        self.assertEqual(sess.close(datetime(2026, 6, 1, tzinfo=UTC)), (15, 30))
        self.assertEqual(sess.close(datetime(2026, 1, 15, tzinfo=UTC)), (15, 30))


class IsOpenTest(unittest.TestCase):
    def test_nse_mid_session_open(self):
        # 04:00 UTC == 09:30 IST -> inside 09:15-15:30
        self.assertTrue(is_open("NSE", datetime(2026, 9, 9, 4, 0, tzinfo=UTC)))

    def test_nse_before_open_closed(self):
        # 03:00 UTC == 08:30 IST
        self.assertFalse(is_open("NSE", datetime(2026, 9, 9, 3, 0, tzinfo=UTC)))

    def test_weekend_closed(self):
        # 2026-09-12 is a Saturday
        self.assertFalse(is_open("NSE", datetime(2026, 9, 12, 4, 0, tzinfo=UTC)))

    def test_mcx_default_segment_open(self):
        # 2026-09-09 11:00 IST is a normal MCX morning
        self.assertTrue(is_open(None, datetime(2026, 9, 9, 5, 30, tzinfo=UTC)))

    def test_loaded_sessions_from_settings(self):
        sess = _sess("MCX")
        self.assertEqual(sess.open, (9, 0))


class SegmentIdTest(unittest.TestCase):
    def test_enums_resolve_to_exchange(self):
        from app.infra.instrument import Segment

        self.assertEqual(segment_id(Segment.EQUITY), "NSE")
        self.assertEqual(segment_id(Segment.EQUITY_FUT), "NSE")
        self.assertEqual(segment_id(Segment.COMMODITY), "MCX")

    def test_strings_normalise(self):
        self.assertEqual(segment_id("NSE"), "NSE")
        self.assertEqual(segment_id("mcx"), "MCX")
        self.assertEqual(segment_id(""), "MCX")
        self.assertEqual(segment_id("FOO"), "MCX")


class DstCrossCheckTest(unittest.TestCase):
    def test_session_close_consistent_with_dst(self):
        """The MCX override must agree with whether DST is in effect at that time."""
        for when in (
            datetime(2026, 3, 8, 12, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
            datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        ):
            mcx = _sess("MCX")
            if _us_dst_in_effect(when):
                self.assertEqual(mcx.close(when), (23, 30))
            else:
                self.assertEqual(mcx.close(when), (23, 55))


if __name__ == "__main__":
    unittest.main()