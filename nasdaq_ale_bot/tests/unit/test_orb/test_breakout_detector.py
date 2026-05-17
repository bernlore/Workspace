"""Tests for BreakoutDetector — 5-min aggregation, signal logic, LA guard."""

from datetime import datetime

import pytest

from nasdaq_ale_bot.strategies.orb.breakout_detector import (
    BreakoutDetector,
    BreakoutDirection,
    OpeningRangeNotFrozenError,
)
from nasdaq_ale_bot.strategies.orb.opening_range import ORState, OpeningRange

# 2 NQ ticks = 0.5 price units.
MIN_DIST = 0.5


def _frozen_or(bar, day: datetime, high: float = 17050.0, low: float = 17000.0):
    """A frozen OpeningRange with the given high/low."""
    orng = OpeningRange()
    mid = (high + low) / 2
    for i in range(15):
        if i == 0:
            orng.offer(bar(datetime(day.year, day.month, day.day, 9, 30),
                           mid, high, low, mid))
        else:
            orng.offer(bar(datetime(day.year, day.month, day.day, 9, 30 + i),
                            mid, mid, mid, mid))
    orng.offer(bar(datetime(day.year, day.month, day.day, 9, 45), mid, mid, mid, mid))
    assert orng.is_frozen
    return orng


def _feed_5min(detector, orng, bar, day, *, opens, highs, lows, closes,
               start_min=45):
    """Feed a 5-bar window; return the signal from the closing (:X4) bar."""
    sig = None
    for k in range(5):
        b = bar(datetime(day.year, day.month, day.day, 9, start_min + k),
                opens[k], highs[k], lows[k], closes[k])
        sig = detector.on_bar(b, orng)
    return sig


def test_5min_aggregation_and_no_signal_before_close(bar):
    day = datetime(2024, 1, 3)
    orng = _frozen_or(bar, day)
    det = BreakoutDetector(min_breakout_distance=MIN_DIST)
    # First four bars of the window must not finalize anything.
    for k in range(4):
        b = bar(datetime(2024, 1, 3, 9, 45 + k), 17025, 17026, 17024, 17025)
        assert det.on_bar(b, orng) is None


def test_long_signal_on_clean_breakout(bar):
    day = datetime(2024, 1, 3)
    orng = _frozen_or(bar, day, high=17050, low=17000)
    det = BreakoutDetector(min_breakout_distance=MIN_DIST)
    # 5-min bar: open 17051, close 17060 (body up, well above 17050.5).
    sig = _feed_5min(det, orng, bar, day,
                     opens=[17051, 17055, 17056, 17058, 17059],
                     highs=[17056, 17060, 17061, 17062, 17063],
                     lows=[17050, 17054, 17055, 17057, 17058],
                     closes=[17055, 17056, 17058, 17059, 17060])
    assert sig is not None
    assert sig.direction == BreakoutDirection.LONG
    assert sig.confirmation_close == 17060


def test_short_signal_on_clean_breakdown(bar):
    day = datetime(2024, 1, 3)
    orng = _frozen_or(bar, day, high=17050, low=17000)
    det = BreakoutDetector(min_breakout_distance=MIN_DIST)
    # 5-min bar: open 16999, close 16990 (body down, well below 16999.5).
    sig = _feed_5min(det, orng, bar, day,
                     opens=[16999, 16996, 16995, 16993, 16992],
                     highs=[17000, 16997, 16996, 16994, 16993],
                     lows=[16994, 16993, 16991, 16990, 16989],
                     closes=[16996, 16995, 16993, 16992, 16990])
    assert sig is not None
    assert sig.direction == BreakoutDirection.SHORT


def test_doji_breakout_rejected(bar):
    day = datetime(2024, 1, 3)
    orng = _frozen_or(bar, day, high=17050, low=17000)
    det = BreakoutDetector(min_breakout_distance=MIN_DIST, require_solid_body=True)
    # 5-min close 17060 clears the line, but open == close (doji body).
    sig = _feed_5min(det, orng, bar, day,
                     opens=[17060, 17055, 17056, 17058, 17059],
                     highs=[17061, 17061, 17062, 17063, 17064],
                     lows=[17050, 17054, 17055, 17057, 17058],
                     closes=[17055, 17056, 17058, 17059, 17060])
    # 5-min open = 17060 (first bar open), close = 17060 -> doji -> no signal.
    assert sig is None


def test_breakout_under_two_ticks_rejected(bar):
    day = datetime(2024, 1, 3)
    orng = _frozen_or(bar, day, high=17050, low=17000)
    det = BreakoutDetector(min_breakout_distance=MIN_DIST)
    # Close 17050.25 — only 1 tick above 17050, less than the 0.5 minimum.
    sig = _feed_5min(det, orng, bar, day,
                     opens=[17049, 17049.5, 17050, 17050, 17050],
                     highs=[17051, 17051, 17051, 17051, 17051],
                     lows=[17048, 17049, 17049.5, 17050, 17050],
                     closes=[17049.5, 17050, 17050, 17050, 17050.25])
    assert sig is None


def test_lookahead_guard_raises_when_or_not_frozen(bar):
    day = datetime(2024, 1, 3)
    forming_or = OpeningRange()           # never frozen — still FORMING
    assert forming_or.state == ORState.FORMING
    det = BreakoutDetector(min_breakout_distance=MIN_DIST)
    # Feed a 5-min window 09:40..09:44 — the :44 bar (44 % 5 == 4) finalizes
    # a 5-min bar; the guard must refuse to evaluate it.
    for k in range(4):
        det.on_bar(bar(datetime(2024, 1, 3, 9, 40 + k), 17025, 17026, 17024, 17025),
                   forming_or)
    with pytest.raises(OpeningRangeNotFrozenError):
        det.on_bar(bar(datetime(2024, 1, 3, 9, 44), 17025, 17026, 17024, 17025),
                   forming_or)
