"""Clock-anchored 1m -> 1h / 4h / 1d aggregation for HTF bias detection.

Clock anchors (deterministic, session-agnostic):

* 1h / 4h: aligned to UTC-hour blocks (``00:00, 01:00, ...`` for 1h;
  ``00:00, 04:00, 08:00, 12:00, 16:00, 20:00`` UTC for 4h).
* 1d: aligned to midnight America/New_York (DST-aware) so daily bars
  agree with the state-machine session rotation (§A16).

All inputs and outputs are :class:`nasdaq_ale_bot.core.candle.Candle`; the
caller feeds 1-minute bars in chronological order and receives the
higher-TF closed bar exactly once, on the first 1m bar *of the following
bucket*.  Within a bucket, ``open`` is frozen at the first bar, ``close``
updates every call, and ``high/low/volume`` are reduced.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ..core.candle import Candle

_NY = ZoneInfo("America/New_York")


class TimeframeAggregator:
    """Stream 1-minute bars into a fixed-width minute bucket.

    Parameters
    ----------
    minutes:
        Bucket size in minutes. ``60`` for 1h, ``240`` for 4h.  Must be
        a positive divisor of the 1440-minute day so UTC bucket starts
        line up with clock-round boundaries.
    """

    __slots__ = (
        "_minutes",
        "_bucket_start",
        "_open",
        "_high",
        "_low",
        "_close",
        "_volume",
    )

    def __init__(self, minutes: int) -> None:
        if minutes <= 0:
            raise ValueError("TimeframeAggregator minutes must be positive")
        if 1440 % minutes != 0:
            raise ValueError(
                "TimeframeAggregator minutes must divide 1440 evenly "
                f"(got {minutes})"
            )
        self._minutes = minutes
        self._bucket_start: datetime | None = None
        self._open: float = 0.0
        self._high: float = 0.0
        self._low: float = 0.0
        self._close: float = 0.0
        self._volume: float = 0.0

    def on_1m_bar(self, bar: Candle) -> Candle | None:
        """Feed a 1m bar; return the *previous* bucket's closed bar on roll."""
        start = self._bucket_start_for(bar.ts)
        if self._bucket_start is None:
            self._init_bucket(start, bar)
            return None
        if start != self._bucket_start:
            closed = self._emit_closed()
            self._init_bucket(start, bar)
            return closed
        self._update_bucket(bar)
        return None

    def force_close(self) -> Candle | None:
        """Emit the in-flight bucket and reset (end-of-feed flush)."""
        if self._bucket_start is None:
            return None
        return self._emit_closed()

    def _bucket_start_for(self, ts: datetime) -> datetime:
        minute_of_day = ts.hour * 60 + ts.minute
        aligned = (minute_of_day // self._minutes) * self._minutes
        return ts.replace(
            hour=aligned // 60,
            minute=aligned % 60,
            second=0,
            microsecond=0,
        )

    def _init_bucket(self, start: datetime, bar: Candle) -> None:
        self._bucket_start = start
        self._open = bar.open
        self._high = bar.high
        self._low = bar.low
        self._close = bar.close
        self._volume = bar.volume

    def _update_bucket(self, bar: Candle) -> None:
        if bar.high > self._high:
            self._high = bar.high
        if bar.low < self._low:
            self._low = bar.low
        self._close = bar.close
        self._volume += bar.volume

    def _emit_closed(self) -> Candle:
        assert self._bucket_start is not None  # for mypy
        closed = Candle(
            ts=self._bucket_start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
        self._bucket_start = None
        return closed


class DailyAggregator:
    """Stream 1-minute bars into an America/New_York calendar-day bucket.

    DST-aware: the bucket key is ``bar.ts.astimezone(America/New_York).date()``
    so spring-forward and fall-back days are handled correctly by
    :mod:`zoneinfo`.  The emitted daily bar's ``ts`` is the UTC moment
    corresponding to *midnight NY* of the bucketed day.
    """

    __slots__ = (
        "_bucket_day",
        "_bucket_start_utc",
        "_open",
        "_high",
        "_low",
        "_close",
        "_volume",
    )

    def __init__(self) -> None:
        self._bucket_day: date | None = None
        self._bucket_start_utc: datetime | None = None
        self._open: float = 0.0
        self._high: float = 0.0
        self._low: float = 0.0
        self._close: float = 0.0
        self._volume: float = 0.0

    def on_1m_bar(self, bar: Candle) -> Candle | None:
        ny_date = bar.ts.astimezone(_NY).date()
        if self._bucket_day is None:
            self._init_bucket(ny_date, bar)
            return None
        if ny_date != self._bucket_day:
            closed = self._emit_closed()
            self._init_bucket(ny_date, bar)
            return closed
        self._update_bucket(bar)
        return None

    def force_close(self) -> Candle | None:
        if self._bucket_day is None:
            return None
        return self._emit_closed()

    def _init_bucket(self, ny_date: date, bar: Candle) -> None:
        self._bucket_day = ny_date
        ny_midnight = datetime(
            ny_date.year, ny_date.month, ny_date.day, tzinfo=_NY
        )
        self._bucket_start_utc = ny_midnight.astimezone(timezone.utc)
        self._open = bar.open
        self._high = bar.high
        self._low = bar.low
        self._close = bar.close
        self._volume = bar.volume

    def _update_bucket(self, bar: Candle) -> None:
        if bar.high > self._high:
            self._high = bar.high
        if bar.low < self._low:
            self._low = bar.low
        self._close = bar.close
        self._volume += bar.volume

    def _emit_closed(self) -> Candle:
        assert self._bucket_start_utc is not None
        closed = Candle(
            ts=self._bucket_start_utc,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
        self._bucket_day = None
        self._bucket_start_utc = None
        return closed
