"""CandleView runtime look-ahead enforcement (A24)."""

import pytest

from nasdaq_ale_bot.core.candle_view import CandleView, LookAheadError

from .conftest import mk_candle


def _bars(n: int):
    return [mk_candle(i, open_=100 + i, close=100 + i) for i in range(n)]


def test_in_horizon_reads_ok():
    view = CandleView(_bars(5), i=3)
    assert view[0] is not None
    assert view[3] is not None
    assert len(view) == 4


def test_lookahead_raises():
    view = CandleView(_bars(5), i=3)
    with pytest.raises(LookAheadError):
        _ = view[4]


def test_negative_index_wraps_within_horizon():
    view = CandleView(_bars(5), i=3)
    assert view[-1] is view[3]
    assert view[-4] is view[0]


def test_negative_out_of_range_raises():
    view = CandleView(_bars(5), i=3)
    with pytest.raises(LookAheadError):
        _ = view[-5]


def test_slice_rejected():
    view = CandleView(_bars(5), i=3)
    with pytest.raises(TypeError):
        _ = view[0:2]
