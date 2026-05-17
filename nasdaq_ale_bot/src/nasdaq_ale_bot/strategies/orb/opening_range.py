"""Opening Range — accumulate and freeze the first 15 one-minute bars.

Look-ahead contract (AI_INSIGHTS #4 / spec §9.3): the range freezes at the
first bar whose NY time is at or after the window end (09:45 by default).
The bar that triggers the freeze is NOT part of the range. Breakout logic
must verify the range is FROZEN before acting on it.
"""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from zoneinfo import ZoneInfo

from nasdaq_ale_bot.core.candle import Candle

NY = ZoneInfo("America/New_York")


class ORState(StrEnum):
    """Lifecycle of an opening range."""

    FORMING = "FORMING"   # still accumulating the 15 bars
    FROZEN = "FROZEN"     # 15 bars accumulated, high/low/range computed
    INVALID = "INVALID"   # fewer than 15 bars by window end — no trade today


class OpeningRange:
    """Accumulates the opening-range bars for a single session, then freezes.

    Usage: call :meth:`offer` with every 1-minute bar of the session. Bars
    before the window start are ignored; bars within ``[start, end)`` are
    accumulated; the first bar at NY time ``>= end`` freezes the range.
    """

    def __init__(
        self,
        *,
        start_et: time = time(9, 30),
        duration_minutes: int = 15,
    ) -> None:
        self._start = start_et
        self._duration = duration_minutes
        end_total = start_et.hour * 60 + start_et.minute + duration_minutes
        self._end = time(end_total // 60, end_total % 60)
        self._bars: list[Candle] = []
        self.state: ORState = ORState.FORMING
        self.high: float | None = None
        self.low: float | None = None
        self.range: float | None = None

    @property
    def is_frozen(self) -> bool:
        return self.state == ORState.FROZEN

    @property
    def bar_count(self) -> int:
        return len(self._bars)

    def offer(self, bar: Candle) -> bool:
        """Offer a 1-minute bar to the range.

        Returns ``True`` iff the bar was accumulated into the range. A bar at
        or after the window end triggers the freeze and is itself rejected
        (it belongs to the post-range trading window).
        """
        if self.state != ORState.FORMING:
            return False
        t = bar.ts.astimezone(NY).time()
        if t < self._start:
            return False  # pre-market / pre-window bar — ignore
        if t < self._end:
            self._bars.append(bar)
            return True
        # First bar at/after the window end — freeze.
        self._freeze()
        return False

    def _freeze(self) -> None:
        if len(self._bars) < self._duration:
            # Incomplete data — abort the day's setup (spec: no trade).
            self.state = ORState.INVALID
            return
        self.high = max(b.high for b in self._bars)
        self.low = min(b.low for b in self._bars)
        self.range = self.high - self.low
        self.state = ORState.FROZEN
