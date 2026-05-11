"""Killzone time filter — DST transitions and NYSE early-close handling."""

from datetime import datetime
from zoneinfo import ZoneInfo

from nasdaq_ale_bot.filters.killzone import in_primary_killzone, in_secondary_killzone

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _ny(year, month, day, hh, mm) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=NY).astimezone(UTC)


def test_primary_08_59_excluded():
    assert in_primary_killzone(_ny(2024, 4, 15, 8, 59)) is False


def test_primary_09_00_included():
    assert in_primary_killzone(_ny(2024, 4, 15, 9, 0)) is True


def test_primary_12_59_59_included():
    t = datetime(2024, 4, 15, 12, 59, 59, tzinfo=NY).astimezone(UTC)
    assert in_primary_killzone(t) is True


def test_primary_13_00_excluded():
    assert in_primary_killzone(_ny(2024, 4, 15, 13, 0)) is False


def test_dst_march_transition():
    # 2024-03-10 DST spring forward at 02:00 ET; 09:30 ET is valid local time
    assert in_primary_killzone(_ny(2024, 3, 11, 9, 30)) is True  # Mon after DST start


def test_dst_november_transition():
    # 2024-11-03 DST fall back; Mon 2024-11-04 09:30 ET is standard time
    assert in_primary_killzone(_ny(2024, 11, 4, 9, 30)) is True


def test_secondary_default_day():
    assert in_secondary_killzone(_ny(2024, 4, 15, 13, 30)) is True
    assert in_secondary_killzone(_ny(2024, 4, 15, 15, 59)) is True
    assert in_secondary_killzone(_ny(2024, 4, 15, 16, 0)) is False


def test_nyse_early_close_2024_11_29_collapses_secondary():
    # 2024-11-29 (day after Thanksgiving) NYSE closes 13:00 ET.
    # session close = 13:00 < SECONDARY_START = 13:30 -> secondary collapses.
    assert in_secondary_killzone(_ny(2024, 11, 29, 13, 30)) is False
    assert in_secondary_killzone(_ny(2024, 11, 29, 13, 45)) is False


def test_nyse_early_close_primary_still_works():
    # Primary 09:30-11:00 is within the half day and should still fire
    assert in_primary_killzone(_ny(2024, 11, 29, 10, 0)) is True


def test_weekend_returns_false():
    # 2024-04-13 is a Saturday
    assert in_primary_killzone(_ny(2024, 4, 13, 10, 0)) is False
    assert in_secondary_killzone(_ny(2024, 4, 13, 14, 0)) is False
