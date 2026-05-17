"""Fair Value Gap detection on 3 consecutive bars."""

from dataclasses import dataclass

from nasdaq_ale_bot.core.candle_view import CandleView
from nasdaq_ale_bot.core.leg import Direction


@dataclass
class FVG:
    start_idx: int
    end_idx: int
    top: float
    bottom: float
    direction: Direction


def detect_fvg(view: CandleView, i: int) -> list[FVG]:
    """Return the FVG formed on the 3-bar window ending at i, if any."""
    if i < 2:
        return []
    a = view[i - 2]
    c = view[i]
    out: list[FVG] = []
    if a.high < c.low:
        out.append(FVG(start_idx=i - 2, end_idx=i, top=c.low, bottom=a.high, direction=Direction.UP))
    if a.low > c.high:
        out.append(FVG(start_idx=i - 2, end_idx=i, top=a.low, bottom=c.high, direction=Direction.DOWN))
    return out
