"""Tests for OpeningRange — accumulation, freeze, look-ahead contract."""

from datetime import datetime

from nasdaq_ale_bot.strategies.orb.opening_range import ORState, OpeningRange


def _or_bars(bar, day: datetime, base: float = 17000.0):
    """15 one-minute bars 09:30..09:44; price steps base..base+14."""
    out = []
    for i in range(15):
        price = base + i
        out.append(bar(datetime(day.year, day.month, day.day, 9, 30 + i),
                        price, price + 0.5, price - 0.5, price))
    return out


def test_builds_correct_or_from_15_bars(bar):
    day = datetime(2024, 1, 3)
    orng = OpeningRange()
    for b in _or_bars(bar, day):
        assert orng.offer(b) is True
    assert orng.state == ORState.FORMING
    assert orng.bar_count == 15
    # The first bar at/after 09:45 freezes the range.
    orng.offer(bar(datetime(2024, 1, 3, 9, 45), 17020, 17020, 17020, 17020))
    assert orng.state == ORState.FROZEN
    assert orng.is_frozen is True
    assert orng.high == 17014.5   # max high = (base+14) + 0.5
    assert orng.low == 16999.5    # min low  = base - 0.5
    assert orng.range == 15.0


def test_incomplete_data_aborts_to_invalid(bar):
    day = datetime(2024, 1, 3)
    orng = OpeningRange()
    for b in _or_bars(bar, day)[:12]:   # only 12 of the 15 bars
        orng.offer(b)
    orng.offer(bar(datetime(2024, 1, 3, 9, 45), 17020, 17020, 17020, 17020))
    assert orng.state == ORState.INVALID
    assert orng.is_frozen is False


def test_bar_exactly_at_0945_is_not_included(bar):
    day = datetime(2024, 1, 3)
    orng = OpeningRange()
    for b in _or_bars(bar, day):
        orng.offer(b)
    accepted = orng.offer(bar(datetime(2024, 1, 3, 9, 45, 0), 1, 1, 1, 1))
    assert accepted is False          # 09:45:00 is the window end (exclusive)
    assert orng.bar_count == 15       # still exactly 15 range bars
    assert orng.state == ORState.FROZEN


def test_pre_0930_bars_are_ignored(bar):
    orng = OpeningRange()
    accepted = orng.offer(bar(datetime(2024, 1, 3, 9, 29), 17000, 17001, 16999, 17000))
    assert accepted is False
    assert orng.bar_count == 0
    assert orng.state == ORState.FORMING


def test_offer_after_frozen_returns_false(bar):
    day = datetime(2024, 1, 3)
    orng = OpeningRange()
    for b in _or_bars(bar, day):
        orng.offer(b)
    orng.offer(bar(datetime(2024, 1, 3, 9, 45), 17020, 17020, 17020, 17020))
    assert orng.is_frozen
    # A later bar must not mutate a frozen range.
    assert orng.offer(bar(datetime(2024, 1, 3, 9, 46), 99999, 99999, 99999, 99999)) is False
    assert orng.high == 17014.5


def test_dst_summer_session_freezes_correctly(bar):
    # 2024-07-01 is EDT (UTC-4). The NY-time helper handles the offset, so
    # 09:30 ET still maps onto the range window.
    day = datetime(2024, 7, 1)
    orng = OpeningRange()
    for b in _or_bars(bar, day):
        assert orng.offer(b) is True
    orng.offer(bar(datetime(2024, 7, 1, 9, 45), 17020, 17020, 17020, 17020))
    assert orng.state == ORState.FROZEN
    assert orng.bar_count == 15
