"""Liquidity sweep detection."""

from datetime import datetime, timezone

from nasdaq_ale_bot.core.candle_view import CandleView
from nasdaq_ale_bot.core.leg import Direction
from nasdaq_ale_bot.core.liquidity import LiquidityKind, LiquidityLevel
from nasdaq_ale_bot.strategies.nasdaqale.detection.sweep import detect_sweep

from .conftest import mk_candle

_TS = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def _level(price: float, kind: LiquidityKind = LiquidityKind.PDL) -> LiquidityLevel:
    return LiquidityLevel(kind=kind, price=price, ts=_TS)


def test_bullish_sweep_detected():
    # level at 100.00, bar pokes low to 99.95 (5 ticks penetration), closes at 100.20
    bar = mk_candle(0, open_=100.1, high=100.3, low=99.95, close=100.2)
    out = detect_sweep(CandleView([bar], 0), 0, [_level(100.0)], tick_size=0.01, min_penetration_ticks=2)
    assert out.swept is True
    assert out.direction == Direction.UP
    assert out.penetration_ticks >= 2


def test_bearish_sweep_detected():
    bar = mk_candle(0, open_=100.0, high=100.15, low=99.8, close=99.85)
    out = detect_sweep(CandleView([bar], 0), 0, [_level(100.1)], tick_size=0.01, min_penetration_ticks=2)
    assert out.swept is True
    assert out.direction == Direction.DOWN


def test_insufficient_penetration_rejected():
    # only 1 tick below level
    bar = mk_candle(0, open_=100.05, high=100.2, low=99.99, close=100.1)
    out = detect_sweep(CandleView([bar], 0), 0, [_level(100.0)], tick_size=0.01, min_penetration_ticks=2)
    assert out.swept is False


def test_no_level_reclaim_rejected():
    # bar pierces below level but closes below it -> no bullish sweep (no reclaim)
    # high stays below level to avoid accidentally triggering a bearish sweep
    bar = mk_candle(0, open_=99.95, high=99.98, low=99.8, close=99.85)
    out = detect_sweep(CandleView([bar], 0), 0, [_level(100.0)], tick_size=0.01, min_penetration_ticks=2)
    assert out.swept is False


def test_empty_levels():
    bar = mk_candle(0)
    out = detect_sweep(CandleView([bar], 0), 0, [], tick_size=0.01)
    assert out.swept is False
