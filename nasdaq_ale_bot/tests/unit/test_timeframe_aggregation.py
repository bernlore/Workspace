"""Unit tests for bias/timeframe.py (TimeframeAggregator, DailyAggregator)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from nasdaq_ale_bot.bias.timeframe import DailyAggregator, TimeframeAggregator
from nasdaq_ale_bot.core.candle import Candle

_NY = ZoneInfo("America/New_York")


def _mk(ts: datetime, o: float, h: float, lo: float, c: float, v: float = 100.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=v)


def _feed_1m_series(
    agg: TimeframeAggregator | DailyAggregator,
    start: datetime,
    count: int,
    *,
    base: float = 100.0,
    step: float = 0.0,
) -> list[Candle]:
    emitted: list[Candle] = []
    for i in range(count):
        ts = start + timedelta(minutes=i)
        bar = _mk(ts, base + i * step, base + i * step + 0.5, base + i * step - 0.5, base + i * step + 0.1)
        out = agg.on_1m_bar(bar)
        if out is not None:
            emitted.append(out)
    return emitted


# ----------------------------------------------------------------------
# TimeframeAggregator
# ----------------------------------------------------------------------


def test_rejects_non_positive_minutes() -> None:
    with pytest.raises(ValueError):
        TimeframeAggregator(minutes=0)
    with pytest.raises(ValueError):
        TimeframeAggregator(minutes=-5)


def test_rejects_non_divisor_minutes() -> None:
    with pytest.raises(ValueError):
        TimeframeAggregator(minutes=7)


def test_1h_bucket_emits_at_next_hour_boundary() -> None:
    agg = TimeframeAggregator(minutes=60)
    start = datetime(2024, 1, 2, 14, 0, tzinfo=timezone.utc)
    emitted = _feed_1m_series(agg, start, count=61)  # 60 in first hour + 1 in next
    assert len(emitted) == 1
    bar = emitted[0]
    assert bar.ts == start
    assert bar.open == 100.0  # first bar open
    # close is the close of the 60th minute bar (i=59), which is 100.1
    assert bar.close == pytest.approx(100.1)
    # 60 1m bars * volume=100
    assert bar.volume == pytest.approx(60 * 100.0)


def test_4h_bucket_aligns_to_utc_block() -> None:
    agg = TimeframeAggregator(minutes=240)
    # start off-boundary; bucket should snap to 12:00 UTC
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    _feed_1m_series(agg, start, count=10)
    closed = agg.force_close()
    assert closed is not None
    assert closed.ts == datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)


def test_force_close_on_empty_is_none() -> None:
    agg = TimeframeAggregator(minutes=60)
    assert agg.force_close() is None


def test_high_low_aggregation_tracks_extremes() -> None:
    agg = TimeframeAggregator(minutes=60)
    base = datetime(2024, 1, 2, 14, 0, tzinfo=timezone.utc)
    agg.on_1m_bar(_mk(base, 100, 101, 99, 100))
    agg.on_1m_bar(_mk(base + timedelta(minutes=1), 100, 105, 98, 102))
    agg.on_1m_bar(_mk(base + timedelta(minutes=2), 102, 103, 95, 101))
    closed = agg.force_close()
    assert closed is not None
    assert closed.high == 105
    assert closed.low == 95
    assert closed.open == 100
    assert closed.close == 101


def test_multiple_buckets_emitted_sequentially() -> None:
    agg = TimeframeAggregator(minutes=60)
    start = datetime(2024, 1, 2, 14, 0, tzinfo=timezone.utc)
    emitted = _feed_1m_series(agg, start, count=180)  # 3 hours
    assert len(emitted) == 2  # first two closed, third still in-flight
    assert emitted[0].ts == start
    assert emitted[1].ts == start + timedelta(hours=1)


# ----------------------------------------------------------------------
# DailyAggregator
# ----------------------------------------------------------------------


def test_daily_empty_force_close_none() -> None:
    agg = DailyAggregator()
    assert agg.force_close() is None


def test_daily_bucket_rolls_at_ny_midnight() -> None:
    agg = DailyAggregator()
    # 2024-01-02 04:59 UTC -> NY 23:59 of 2024-01-01 (EST, UTC-5)
    ts_a = datetime(2024, 1, 2, 4, 59, tzinfo=timezone.utc)
    ts_b = datetime(2024, 1, 2, 5, 0, tzinfo=timezone.utc)  # NY 00:00 of 2024-01-02
    out_a = agg.on_1m_bar(_mk(ts_a, 100, 101, 99, 100))
    assert out_a is None
    out_b = agg.on_1m_bar(_mk(ts_b, 100, 101, 99, 100))
    assert out_b is not None
    # closed bar's ts is NY midnight of 2024-01-01 in UTC => 05:00 UTC
    expected_ny_midnight = datetime(2024, 1, 1, tzinfo=_NY).astimezone(timezone.utc)
    assert out_b.ts == expected_ny_midnight


def test_daily_bucket_survives_dst_transition() -> None:
    """Spring-forward 2024-03-10: NY advances from 02:00 to 03:00 EST->EDT."""
    agg = DailyAggregator()
    # feed bars from before and after the DST transition, all within NY 2024-03-10
    # NY 2024-03-10 00:00 -> UTC 2024-03-10 05:00 (EST)
    # NY 2024-03-10 04:00 -> UTC 2024-03-10 08:00 (EDT)
    ts_early = datetime(2024, 3, 10, 6, 0, tzinfo=timezone.utc)
    ts_late = datetime(2024, 3, 10, 8, 30, tzinfo=timezone.utc)
    assert ts_early.astimezone(_NY).date() == date(2024, 3, 10)
    assert ts_late.astimezone(_NY).date() == date(2024, 3, 10)
    agg.on_1m_bar(_mk(ts_early, 100, 101, 99, 100))
    agg.on_1m_bar(_mk(ts_late, 100, 102, 98, 101))
    # A bar on the next NY day triggers emit
    ts_next = datetime(2024, 3, 11, 5, 0, tzinfo=timezone.utc)
    out = agg.on_1m_bar(_mk(ts_next, 102, 103, 101, 102))
    assert out is not None
    assert out.open == 100
    assert out.close == 101
