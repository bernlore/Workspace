"""Breakout detection — 5-minute aggregation + opening-range breakout signal.

1-minute bars are aggregated into clock-anchored 5-minute bars (09:45-09:49,
09:50-09:54, ...). A 5-minute bar finalizes when the 1-minute bar at
NY-minute % 5 == 4 (:49, :54, :59, ...) is consumed; the breakout condition
is then checked against the frozen opening range.

Look-ahead guard (spec §9.3): a breakout check on an opening range that is
not yet FROZEN raises :class:`OpeningRangeNotFrozenError` — fail-closed,
never silently proceed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.strategies.orb.opening_range import ORState, OpeningRange

NY = ZoneInfo("America/New_York")


class OpeningRangeNotFrozenError(RuntimeError):
    """Raised when a 5-minute breakout check runs before the OR is frozen."""


class BreakoutDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class BreakoutSignal:
    """A confirmed opening-range breakout on a closed 5-minute bar."""

    direction: BreakoutDirection
    confirmation_ts: datetime    # ts of the closed 5-min bar
    confirmation_close: float    # the 5-min close that confirmed
    five_min_open: float
    five_min_high: float
    five_min_low: float


class BreakoutDetector:
    """Aggregates 1-min bars to 5-min and detects the opening-range breakout.

    ``min_breakout_distance`` is in price units (e.g. 0.5 = 2 NQ ticks): the
    5-min close must clear the range edge by at least this much.
    """

    def __init__(
        self,
        *,
        min_breakout_distance: float,
        require_solid_body: bool = True,
    ) -> None:
        self._min_dist = min_breakout_distance
        self._require_solid_body = require_solid_body
        self._anchor: int | None = None   # NY minute-of-day of current 5-min window
        self._buf: list[Candle] = []

    def on_bar(
        self, bar: Candle, opening_range: OpeningRange
    ) -> BreakoutSignal | None:
        """Consume one 1-minute bar.

        Returns a :class:`BreakoutSignal` when a closed 5-minute bar breaks
        the opening range, else ``None``. Raises
        :class:`OpeningRangeNotFrozenError` if a 5-minute bar closes while the
        opening range is not yet frozen.
        """
        ny = bar.ts.astimezone(NY)
        minute_of_day = ny.hour * 60 + ny.minute
        anchor = (minute_of_day // 5) * 5
        if anchor != self._anchor:
            self._anchor = anchor
            self._buf = []
        self._buf.append(bar)
        if ny.minute % 5 != 4:
            return None  # 5-min window not yet closed

        # 5-minute window complete — finalize and reset the buffer.
        five_min = self._aggregate(self._buf)
        self._buf = []

        # Look-ahead guard — never evaluate a breakout on an unfrozen range.
        if opening_range.state != ORState.FROZEN:
            raise OpeningRangeNotFrozenError(
                f"breakout check at {bar.ts.isoformat()} but opening range "
                f"state is {opening_range.state}, expected FROZEN"
            )
        return self._check(five_min, opening_range)

    @staticmethod
    def _aggregate(bars: list[Candle]) -> Candle:
        """Aggregate the buffered 1-minute bars into one 5-minute bar."""
        return Candle(
            ts=bars[0].ts,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
        )

    def _check(
        self, five: Candle, orange: OpeningRange
    ) -> BreakoutSignal | None:
        assert orange.high is not None and orange.low is not None
        long_break = five.close > orange.high + self._min_dist
        short_break = five.close < orange.low - self._min_dist
        body_up = five.close > five.open
        body_down = five.close < five.open

        if long_break and (body_up or not self._require_solid_body):
            return BreakoutSignal(
                direction=BreakoutDirection.LONG,
                confirmation_ts=five.ts,
                confirmation_close=five.close,
                five_min_open=five.open,
                five_min_high=five.high,
                five_min_low=five.low,
            )
        if short_break and (body_down or not self._require_solid_body):
            return BreakoutSignal(
                direction=BreakoutDirection.SHORT,
                confirmation_ts=five.ts,
                confirmation_close=five.close,
                five_min_open=five.open,
                five_min_high=five.high,
                five_min_low=five.low,
            )
        return None
