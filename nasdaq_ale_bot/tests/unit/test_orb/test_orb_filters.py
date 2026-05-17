"""Tests for the ORB opening-range size filters."""

from datetime import datetime

from nasdaq_ale_bot.strategies.orb.state_machine import OrbState

DAY = datetime(2024, 1, 3)


def _drive(sm, bars):
    for b in bars:
        sm.on_bar(b)


def test_skip_when_or_below_min_size(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = or_bars(DAY, high=17005, low=17000)        # range 5 < 10
    bars.append(bar(datetime(2024, 1, 3, 9, 45), 17003, 17003, 17003, 17003))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert sm.days_skipped_size == 1
    assert sm.days_or_valid == 0
    assert sm.trades == []


def test_skip_when_or_above_max_size(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = or_bars(DAY, high=17100, low=17000)        # range 100 > 80
    bars.append(bar(datetime(2024, 1, 3, 9, 45), 17050, 17050, 17050, 17050))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert sm.days_skipped_size == 1
    assert sm.trades == []


def test_skip_when_or_data_incomplete(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = or_bars(DAY, high=17030, low=17000)[:12]   # only 12 of 15 bars
    bars.append(bar(datetime(2024, 1, 3, 9, 45), 17015, 17015, 17015, 17015))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert sm.days_skipped_invalid == 1
    assert sm.trades == []


def test_valid_or_size_advances_to_waiting(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = or_bars(DAY, high=17020, low=17000)        # range 20, within [10, 80]
    bars.append(bar(datetime(2024, 1, 3, 9, 45), 17010, 17011, 17009, 17010))
    _drive(sm, bars)
    assert sm.state == OrbState.WAITING_FOR_BREAKOUT
    assert sm.days_or_valid == 1
    assert sm.days_skipped_size == 0
