"""FVG detection."""

from nasdaq_ale_bot.core.candle_view import CandleView
from nasdaq_ale_bot.core.leg import Direction
from nasdaq_ale_bot.strategies.nasdaqale.detection.fvg import detect_fvg

from .conftest import mk_candle


def test_bullish_fvg_detected():
    # bar0 high=100.5, bar2 low=102 -> gap [100.5, 102]
    bars = [
        mk_candle(0, open_=100, high=100.5, low=99.5, close=100),
        mk_candle(1, open_=101, high=102, low=100.5, close=101.5),
        mk_candle(2, open_=102, high=103, low=102, close=102.5),
    ]
    out = detect_fvg(CandleView(bars, 2), 2)
    assert len(out) == 1
    assert out[0].direction == Direction.UP
    assert out[0].bottom == 100.5
    assert out[0].top == 102


def test_bearish_fvg_detected():
    bars = [
        mk_candle(0, open_=100, high=100.5, low=99.5, close=100),
        mk_candle(1, open_=99, high=99.4, low=98, close=98.5),
        mk_candle(2, open_=98, high=99.4, low=97, close=97.5),
    ]
    out = detect_fvg(CandleView(bars, 2), 2)
    assert len(out) == 1
    assert out[0].direction == Direction.DOWN


def test_no_fvg():
    bars = [mk_candle(i, open_=100, close=100, high=101, low=99) for i in range(3)]
    out = detect_fvg(CandleView(bars, 2), 2)
    assert out == []


def test_not_enough_bars():
    bars = [mk_candle(0), mk_candle(1)]
    out = detect_fvg(CandleView(bars, 1), 1)
    assert out == []
