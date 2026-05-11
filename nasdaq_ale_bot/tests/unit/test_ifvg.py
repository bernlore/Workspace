"""IFVG detection within a CISD move."""

import pytest

from nasdaq_ale_bot.core.candle_view import CandleView, LookAheadError
from nasdaq_ale_bot.core.leg import Direction
from nasdaq_ale_bot.detection.ifvg import CISDRange, detect_ifvg

from .conftest import mk_candle


def _build_bullish_ifvg_scene():
    # Bearish FVG requires bar[i-2].low > bar[i].high with bar[i-1] between.
    bars = [
        mk_candle(0, open_=100.0, high=100.5, low=99.9, close=100.3),       # idx0 low=99.9
        mk_candle(1, open_=99.3, high=99.4, low=98.5, close=98.7),          # idx1 middle
        mk_candle(2, open_=97.5, high=97.8, low=97.0, close=97.3),          # idx2 high=97.8 < 99.9 -> bearish FVG
        # now a body-close back above fvg.top=99.9
        mk_candle(3, open_=97.5, high=100.2, low=97.4, close=100.1),
    ]
    return bars


def test_bullish_setup_finds_one_ifvg():
    bars = _build_bullish_ifvg_scene()
    view = CandleView(bars, 3)
    out = detect_ifvg(view, 3, CISDRange(start=0, end=3), sweep_price=97.0, direction=Direction.UP)
    assert len(out) == 1
    assert out[0].breached_at == 3


def test_no_ifvg_when_no_bearish_fvg():
    bars = [
        mk_candle(0, open_=100, high=100.5, low=99.5, close=100),
        mk_candle(1, open_=100, high=100.5, low=99.5, close=100),
        mk_candle(2, open_=100, high=100.5, low=99.5, close=100),
        mk_candle(3, open_=100, high=100.5, low=99.5, close=100),
    ]
    view = CandleView(bars, 3)
    out = detect_ifvg(view, 3, CISDRange(start=0, end=3), sweep_price=99.0, direction=Direction.UP)
    assert out == []


def test_lookahead_raises_when_cisd_range_exceeds_i():
    bars = [mk_candle(i) for i in range(5)]
    view = CandleView(bars, 2)
    with pytest.raises(LookAheadError):
        detect_ifvg(view, 2, CISDRange(start=0, end=3), sweep_price=100.0, direction=Direction.UP)
