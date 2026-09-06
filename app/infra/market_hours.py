"""
app/infra/market_hours.py
-------------------------
Exchange + instrument-aware trading session model.

The default asset (MCX:NATURALGAS futures) trades:
  * Mondays -> Fridays (Indian market holidays are taken by the exchange)
  * 09:00 IST open
  * close 23:30 IST while US DST is in effect, else 23:55 IST
    (US DST: second Sunday in March .. first Sunday in November)

Strategies gate quoting on this, the RiskEngine exposes a `market_hours`
pre-trade constraint, and the MonitorEngine flattens the whole book when the
session is about to end / has ended.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def _us_dst_in_effect(now_utc: datetime) -> bool:
    """US DST runs from the 2nd Sunday of March to the 1st Sunday of November."""
    year = now_utc.year

    def _second_sunday(month: int) -> datetime:
        first = datetime(year, month, 1, tzinfo=UTC)
        return datetime(year, month, 1 + ((6 - first.weekday()) % 7) + 7, tzinfo=UTC)

    def _first_sunday(month: int) -> datetime:
        first = datetime(year, month, 1, tzinfo=UTC)
        return datetime(year, month, 1 + ((6 - first.weekday()) % 7), tzinfo=UTC)

    return _second_sunday(3) <= now_utc < _first_sunday(11)


def _close_hhmm(now_utc: datetime) -> tuple[int, int]:
    close = settings.session_close
    if not _us_dst_in_effect(now_utc) and settings.session_close_dst_disabled:
        close = settings.session_close_dst_disabled
    hh, mm = (int(x) for x in close.split(":"))
    return hh, mm


def _open_hhmm() -> tuple[int, int]:
    hh, mm = (int(x) for x in settings.session_open.split(":"))
    return hh, mm


def now_utc() -> datetime:
    return datetime.now(UTC)


def is_open(now: Optional[datetime] = None) -> bool:
    now = now or now_utc()
    cur_ist = now.astimezone(IST)
    if cur_ist.weekday() not in settings.session_weekdays_set:
        return False
    cur = (cur_ist.hour, cur_ist.minute)
    if cur < _open_hhmm():
        return False
    if cur >= _close_hhmm(now):
        return False
    return True


def seconds_until_close(now: Optional[datetime] = None) -> float:
    now = now or now_utc()
    if not is_open(now):
        return 0.0
    cur_ist = now.astimezone(IST)
    hh, mm = _close_hhmm(now)
    close_dt = cur_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return max(0.0, (close_dt - cur_ist).total_seconds())


def ist_date(now: Optional[datetime] = None) -> str:
    now = now or now_utc()
    return now.astimezone(IST).strftime("%Y-%m-%d")


def session_label() -> str:
    open_hhmm = settings.session_open
    hh, mm = _close_hhmm(now_utc())
    return f"MCX {open_hhmm}-{hh:02d}:{mm:02d} IST Mon-Fri"