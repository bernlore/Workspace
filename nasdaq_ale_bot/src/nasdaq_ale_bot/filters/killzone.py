"""Killzone time filters (America/New_York, half-open intervals, NYSE calendar-aware)."""

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

PRIMARY_START = time(9, 0)
PRIMARY_END = time(13, 0)         # extended for CME futures (lunch IFVG cluster)
SECONDARY_START = time(13, 30)
SECONDARY_END_BASE = time(16, 0)  # base PM end; shrunk to close on early-close days
REGULAR_CLOSE = time(16, 0)


@lru_cache(maxsize=1)
def _nyse():
    import pandas_market_calendars as mcal

    return mcal.get_calendar("NYSE")


@lru_cache(maxsize=4096)
def _session_close(d: date) -> time | None:
    """Return the NYSE session close time for d in NY-local, or None if not a trading day."""
    cal = _nyse()
    sched = cal.schedule(start_date=d.isoformat(), end_date=d.isoformat())
    if sched.empty:
        return None
    close_utc = sched.iloc[0]["market_close"].to_pydatetime()
    close_ny = close_utc.astimezone(NY)
    return close_ny.time()


def _to_ny(ts_utc: datetime) -> datetime:
    if ts_utc.tzinfo is None:
        raise ValueError("ts must be timezone-aware")
    return ts_utc.astimezone(NY)


def _is_trading_day(ts_ny: datetime) -> bool:
    return _session_close(ts_ny.date()) is not None


def in_primary_killzone(ts_utc: datetime) -> bool:
    """Return True iff ts is inside [09:30, 11:00) ET on a trading day."""
    t = _to_ny(ts_utc)
    if not _is_trading_day(t):
        return False
    return PRIMARY_START <= t.time() < PRIMARY_END


def in_secondary_killzone(ts_utc: datetime) -> bool:
    """Return True iff ts is inside [13:30, min(session_close-15min, 15:45)) ET.

    On NYSE early-close days the upper bound shrinks; if the shrunk bound falls at
    or before 13:30, the secondary killzone collapses to empty and returns False.
    """
    t = _to_ny(ts_utc)
    close = _session_close(t.date())
    if close is None:
        return False
    # Cap at session close on early-close days, otherwise SECONDARY_END_BASE.
    upper = close if close < SECONDARY_END_BASE else SECONDARY_END_BASE
    if upper <= SECONDARY_START:
        return False
    return SECONDARY_START <= t.time() < upper
