"""Shared test helpers."""

from datetime import datetime, timedelta, timezone

from nasdaq_ale_bot.core.candle import Candle


def mk_candle(
    idx: int = 0,
    open_: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float = 100.0,
    volume: float = 1000.0,
) -> Candle:
    """Build a Candle with sensible defaults for testing.

    `idx` seeds a deterministic 1-minute-increment UTC timestamp.
    If high/low are None, they're stretched to satisfy OHLC invariants.
    """
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(minutes=idx)
    hi = high if high is not None else max(open_, close) + 0.5
    lo = low if low is not None else min(open_, close) - 0.5
    return Candle(ts=ts, open=open_, high=hi, low=lo, close=close, volume=volume)
