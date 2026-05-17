"""Liquidity sweep detection: wick pierces, body closes back inside."""

from dataclasses import dataclass

from nasdaq_ale_bot.core.candle_view import CandleView
from nasdaq_ale_bot.core.leg import Direction
from nasdaq_ale_bot.core.liquidity import LiquidityLevel


@dataclass
class SweepResult:
    swept: bool
    level: LiquidityLevel | None
    direction: Direction | None
    penetration_ticks: float


NO_SWEEP = SweepResult(swept=False, level=None, direction=None, penetration_ticks=0.0)


def detect_sweep(
    view: CandleView,
    i: int,
    levels: list[LiquidityLevel],
    tick_size: float,
    min_penetration_ticks: int = 2,
) -> SweepResult:
    """Detect a liquidity sweep at bar i.

    Bearish sweep (sell-side taken from below... no, sell-side is BELOW, so a
    bullish sweep takes sell-side with a wick below the level and body back above).
    Bullish sweep: wick dips below level by >= min penetration AND close > level.
    Bearish sweep: wick pokes above level by >= min penetration AND close < level.
    Returns the match with the largest penetration if multiple fire.
    """
    bar = view[i]
    min_dist = min_penetration_ticks * tick_size
    best: SweepResult = NO_SWEEP
    for lvl in levels:
        # bullish sweep (took sell-side below level)
        below = lvl.price - bar.low
        if below >= min_dist and bar.close > lvl.price:
            ticks = below / tick_size
            if ticks > best.penetration_ticks:
                best = SweepResult(True, lvl, Direction.UP, ticks)
        # bearish sweep (took buy-side above level)
        above = bar.high - lvl.price
        if above >= min_dist and bar.close < lvl.price:
            ticks = above / tick_size
            if ticks > best.penetration_ticks:
                best = SweepResult(True, lvl, Direction.DOWN, ticks)
    return best
