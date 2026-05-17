"""Inverse FVG detection within a CISD move.

The 3-bar imbalance is measured on BODIES (min/max of open/close) with up
to ``tol_offset`` allowed overlap — a 1m-friendly relaxation of the strict
3-bar wick-non-overlap FVG used by the HTF detector.
"""

from dataclasses import dataclass

from nasdaq_ale_bot.core.candle_view import CandleView
from nasdaq_ale_bot.core.leg import Direction
from .fvg import FVG


@dataclass
class CISDRange:
    start: int
    end: int


@dataclass
class IFVG:
    fvg: FVG
    breached_at: int
    distance_to_sweep: float


def _detect_body_imbalance(
    view: CandleView, i: int, tol_offset: float = 0.0
) -> list[FVG]:
    """3-bar imbalance using bodies (min/max of open/close), with tolerance.

    Bullish: ``c.body_low > a.body_high - tol_offset``.
    Bearish: ``a.body_low > c.body_high - tol_offset``.
    Returned FVG keeps ``top > bottom``.
    """
    if i < 2:
        return []
    a = view[i - 2]
    c = view[i]
    a_body_hi = max(a.open, a.close)
    a_body_lo = min(a.open, a.close)
    c_body_hi = max(c.open, c.close)
    c_body_lo = min(c.open, c.close)
    out: list[FVG] = []
    if c_body_lo > a_body_hi - tol_offset:
        out.append(
            FVG(
                start_idx=i - 2,
                end_idx=i,
                top=c_body_lo,
                bottom=a_body_hi,
                direction=Direction.UP,
            )
        )
    if a_body_lo > c_body_hi - tol_offset:
        out.append(
            FVG(
                start_idx=i - 2,
                end_idx=i,
                top=a_body_lo,
                bottom=c_body_hi,
                direction=Direction.DOWN,
            )
        )
    return out


def detect_ifvg(
    view: CandleView,
    i: int,
    cisd_range: CISDRange,
    sweep_price: float,
    direction: Direction,
    tol_offset: float = 0.0,
) -> list[IFVG]:
    """Return all IFVGs inside the CISD range, sorted by distance to sweep.

    For a bullish setup (direction=UP), an IFVG is a bearish 3-bar body
    imbalance inside the range whose top has been body-closed above by
    some later bar within the range. Mirror for bearish setups.
    """
    if cisd_range.end > i:
        from nasdaq_ale_bot.core.candle_view import LookAheadError

        raise LookAheadError(f"cisd_range.end {cisd_range.end} > horizon {i}")

    out: list[IFVG] = []
    for k in range(cisd_range.start + 2, cisd_range.end + 1):
        for fvg in _detect_body_imbalance(view, k, tol_offset):
            if direction == Direction.UP and fvg.direction == Direction.DOWN:
                for j in range(fvg.end_idx + 1, cisd_range.end + 1):
                    if view[j].close > fvg.top:
                        out.append(
                            IFVG(
                                fvg=fvg,
                                breached_at=j,
                                distance_to_sweep=abs(fvg.bottom - sweep_price),
                            )
                        )
                        break
            elif direction == Direction.DOWN and fvg.direction == Direction.UP:
                for j in range(fvg.end_idx + 1, cisd_range.end + 1):
                    if view[j].close < fvg.bottom:
                        out.append(
                            IFVG(
                                fvg=fvg,
                                breached_at=j,
                                distance_to_sweep=abs(fvg.top - sweep_price),
                            )
                        )
                        break

    out.sort(key=lambda x: x.distance_to_sweep)
    return out
