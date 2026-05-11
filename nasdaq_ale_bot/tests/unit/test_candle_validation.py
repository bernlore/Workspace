"""Candle pydantic model validators."""

from datetime import datetime, timezone

import pytest

from nasdaq_ale_bot.core.candle import Candle


def test_tz_aware_accepted():
    ts = datetime(2024, 3, 10, 14, 0, tzinfo=timezone.utc)
    c = Candle(ts=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=10)
    assert c.ts.tzinfo is not None


def test_tz_naive_rejected():
    with pytest.raises(ValueError):
        Candle(
            ts=datetime(2024, 3, 10, 14, 0),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10,
        )


def test_high_below_body_rejected():
    with pytest.raises(ValueError):
        Candle(
            ts=datetime(2024, 3, 10, 14, 0, tzinfo=timezone.utc),
            open=1.0,
            high=0.9,
            low=0.5,
            close=1.5,
            volume=10,
        )


def test_low_above_body_rejected():
    with pytest.raises(ValueError):
        Candle(
            ts=datetime(2024, 3, 10, 14, 0, tzinfo=timezone.utc),
            open=2.0,
            high=3.0,
            low=2.5,
            close=1.5,
            volume=10,
        )


def test_negative_volume_rejected():
    with pytest.raises(ValueError):
        Candle(
            ts=datetime(2024, 3, 10, 14, 0, tzinfo=timezone.utc),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=-1,
        )
