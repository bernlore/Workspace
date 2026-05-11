"""Minimal PDH/PDL + 3-bar swing high/low tracker.

Feeds :func:`nasdaq_ale_bot.detection.sweep.detect_sweep` with real liquidity
levels instead of scaffold volatility heuristics. Mitigated levels (body-close
through price) are dropped so the sweep detector's level list stays bounded.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from .candle import Candle
from .liquidity import LiquidityKind, LiquidityLevel

_NY = ZoneInfo("America/New_York")


class LiquidityTracker:
    """Per-day PDH/PDL rotation + 3-bar fractal swing levels."""

    def __init__(self, *, max_levels: int = 50) -> None:
        self._max_levels = max_levels
        self._day: date | None = None
        self._day_high: float | None = None
        self._day_low: float | None = None
        self._pdh: LiquidityLevel | None = None
        self._pdl: LiquidityLevel | None = None
        self._bars: list[Candle] = []
        self._swings: list[LiquidityLevel] = []

    def on_bar(self, bar: Candle) -> None:
        bar_day = bar.ts.astimezone(_NY).date()
        if self._day is None:
            self._day, self._day_high, self._day_low = bar_day, bar.high, bar.low
        elif bar_day != self._day:
            self._pdh = LiquidityLevel(
                kind=LiquidityKind.PDH, price=self._day_high, ts=bar.ts
            )
            self._pdl = LiquidityLevel(
                kind=LiquidityKind.PDL, price=self._day_low, ts=bar.ts
            )
            self._day, self._day_high, self._day_low = bar_day, bar.high, bar.low
        else:
            if bar.high > self._day_high:
                self._day_high = bar.high
            if bar.low < self._day_low:
                self._day_low = bar.low

        self._bars.append(bar)
        if len(self._bars) >= 3:
            left, mid, right = self._bars[-3], self._bars[-2], self._bars[-1]
            if mid.high > left.high and mid.high > right.high:
                self._swings.append(
                    LiquidityLevel(
                        kind=LiquidityKind.SWING_HIGH, price=mid.high, ts=mid.ts
                    )
                )
            if mid.low < left.low and mid.low < right.low:
                self._swings.append(
                    LiquidityLevel(
                        kind=LiquidityKind.SWING_LOW, price=mid.low, ts=mid.ts
                    )
                )
            surviving: list[LiquidityLevel] = []
            for lvl in self._swings:
                if lvl.kind == LiquidityKind.SWING_HIGH and bar.close > lvl.price:
                    continue
                if lvl.kind == LiquidityKind.SWING_LOW and bar.close < lvl.price:
                    continue
                surviving.append(lvl)
            self._swings = surviving[-self._max_levels :]

        if len(self._bars) > 200:
            self._bars = self._bars[-100:]

    def current_levels(self) -> list[LiquidityLevel]:
        out: list[LiquidityLevel] = list(self._swings)
        if self._pdh is not None:
            out.append(self._pdh)
        if self._pdl is not None:
            out.append(self._pdl)
        return out
