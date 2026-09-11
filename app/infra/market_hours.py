"""
app/infra/market_hours.py
-------------------------
Exchange + instrument-aware trading session model, per segment.

Sessions (all IST, Mon-Fri):
  * NSE  cash / equity futures  -> 09:15 IST open, 15:30 IST close (fixed)
  * MCX  commodity futures      -> 09:00 IST open; close 23:30 IST while US
    DST is in effect, else 23:55 IST (per MCX circulars)

Because the two segments run on independent clocks, gating is **segment-aware**:
when NSE has closed for the day the equity book stands down while the MCX book
keeps quoting until its own session ends. The legacy monosedet functions
(``is_open``/``session_label``/``seconds_until_close`` without a segment)
default to the MCX session — the original single-asset behavior.

Strategies gate quoting on this, the RiskEngine exposes a `market_hours`
pre-trade constraint, and the MonitorEngine flattens the book when a segment's
session is about to end / has ended.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from app.infra.instrument import Segment

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

# NSE and MCX weekdays are the same; MCX reuses the legacy SESSION_WEEKDAYS.
_WEEKDAYS = {0, 1, 2, 3, 4}


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


class Session:
    """One segment's trading window. ``close_non_dst`` overrides ``close`` when
    US DST is not in effect (MCX shifts its close with US markets)."""
    __slots__ = ("_open", "_close", "_close_non_dst")

    def __init__(self, open_hhmm: str, close_hhmm: str, close_non_dst: Optional[str] = None):
        self._open = self._parse(open_hhmm)
        self._close = self._parse(close_hhmm)
        self._close_non_dst = self._parse(close_non_dst) if close_non_dst else None

    @staticmethod
    def _parse(hhmm: str) -> tuple[int, int]:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return hh, mm

    def close(self, now: Optional[datetime] = None) -> tuple[int, int]:
        if self._close_non_dst is not None and not _us_dst_in_effect(now or now_utc()):
            return self._close_non_dst
        return self._close

    @property
    def open(self) -> tuple[int, int]:
        return self._open

    @property
    def label(self) -> str:
        return f"{self._open[0]:02d}:{self._open[1]:02d}-{self._close[0]:02d}:{self._close[1]:02d} IST Mon-Fri"


def _load_sessions() -> Dict[str, Session]:
    from app.config import settings
    mcx_close_dst = settings.session_close
    mcx_close_no_dst = settings.session_close_dst_disabled
    return {
        "NSE": Session(settings.nse_session_open, settings.nse_session_close),
        "MCX": Session(settings.session_open, mcx_close_dst, mcx_close_no_dst),
    }


_SESSIONS: Dict[str, Session] = {}


def _ensure_sessions() -> None:
    if not _SESSIONS:
        _SESSIONS.update(_load_sessions())


def _sess(segment: str) -> Session:
    _ensure_sessions()
    return _SESSIONS.get(segment, _SESSIONS["MCX"])


def segment_id(segment: object) -> str:
    """Normalise a Segment enum / string into an exchange id (NSE | MCX)."""
    seg = segment
    if isinstance(seg, Segment):
        return "NSE" if seg in (Segment.EQUITY, Segment.EQUITY_FUT) else "MCX"
    s = str(seg or "").upper()
    if "NSE" in s:
        return "NSE"
    return "MCX"


def now_utc() -> datetime:
    return datetime.now(UTC)


def _open_internal(exchange: str, now: Optional[datetime]) -> bool:
    now = now or now_utc()
    cur_ist = now.astimezone(IST)
    if cur_ist.weekday() not in _WEEKDAYS:
        return False
    sess = _sess(exchange)
    cur = (cur_ist.hour, cur_ist.minute)
    return sess.open <= cur and cur < sess.close(now)


def is_open(segment: object = None, now: Optional[datetime] = None) -> bool:
    """Session-open check for one segment (default: MCX / legacy session)."""
    return _open_internal(segment_id(segment), now)


def seconds_until_close(segment: object = None, now: Optional[datetime] = None) -> float:
    now = now or now_utc()
    if not _open_internal(segment_id(segment), now):
        return 0.0
    sess = _sess(segment_id(segment))
    hh, mm = sess.close(now)
    close_dt = now.astimezone(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return max(0.0, (close_dt - now.astimezone(IST)).total_seconds())


def in_winddown(segment: object = None, now: Optional[datetime] = None) -> bool:
    """True once a segment has entered its pre-close wind-down window (open but
    ``CLOSE_WINDDOWN_SECONDS`` or less until the session close). Quoting must
    stop and the segment's positions must be squared off from this point on."""
    from app.config import settings
    if settings.close_winddown_seconds <= 0 or not is_open(segment, now):
        return False
    return 0 < seconds_until_close(segment, now) <= settings.close_winddown_seconds


def session_label(segment: object = None) -> str:
    return _sess(segment_id(segment)).label


def open_segments(now: Optional[datetime] = None) -> List[str]:
    _ensure_sessions()
    now = now or now_utc()
    out: List[str] = []
    for exchange in _SESSIONS:
        if _open_internal(exchange, now):
            out.append(exchange)
    return out


def any_open(now: Optional[datetime] = None) -> bool:
    return bool(open_segments(now))


def segment_status(now: Optional[datetime] = None) -> List[dict]:
    """One entry per segment for the API/UI: label, open flag, seconds to close."""
    _ensure_sessions()
    now = now or now_utc()
    status: List[dict] = []
    for exchange in _SESSIONS:
        opening = _open_internal(exchange, now)
        status.append({
            "segment": exchange,
            "label": _sess(exchange).label,
            "open": opening,
            "close_in_sec": round(seconds_until_close(exchange, now), 1) if opening else 0.0,
        })
    return status


def ist_date(now: Optional[datetime] = None) -> str:
    now = now or now_utc()
    return now.astimezone(IST).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ legacy aliases
def session_duration_sec(segment: object = None) -> float:
    """Seconds between session open and close for a segment (for scheduling)."""
    sess = _sess(segment_id(segment))
    hh, mm = sess.open
    chh, cmm = sess.close(None)
    return float((chh * 60 + cmm) - (hh * 60 + mm)) * 60.0