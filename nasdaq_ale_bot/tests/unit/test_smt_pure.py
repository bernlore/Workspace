"""Pure SMT divergence calculation."""

import pytest

from nasdaq_ale_bot.detection.smt_pure import SMTConfigError, detect_smt_divergence

from .conftest import mk_candle


def test_bearish_divergence():
    primary = [mk_candle(0, high=100, low=99, open_=99.5, close=100), mk_candle(1, high=101, low=100, open_=100.5, close=100.8)]
    correlated = [mk_candle(0, high=50, low=49, open_=49.5, close=50), mk_candle(1, high=49.5, low=48.5, open_=49, close=49)]
    out = detect_smt_divergence(primary, correlated, 1)
    assert out.bearish_divergence is True
    assert out.bullish_divergence is False


def test_bullish_divergence():
    primary = [mk_candle(0, high=101, low=100, open_=100.5, close=100.8), mk_candle(1, high=100.5, low=99, open_=100, close=99.5)]
    correlated = [mk_candle(0, high=50, low=49, open_=49.5, close=50), mk_candle(1, high=50.5, low=49.5, open_=50, close=50.2)]
    out = detect_smt_divergence(primary, correlated, 1)
    assert out.bullish_divergence is True
    assert out.bearish_divergence is False


def test_no_divergence():
    primary = [mk_candle(0, high=100, low=99, open_=99.5, close=100), mk_candle(1, high=101, low=100, open_=100.5, close=100.8)]
    correlated = [mk_candle(0, high=50, low=49, open_=49.5, close=50), mk_candle(1, high=51, low=50, open_=50.5, close=50.8)]
    out = detect_smt_divergence(primary, correlated, 1)
    assert out.bearish_divergence is False
    assert out.bullish_divergence is False


def test_missing_correlated_raises():
    primary = [mk_candle(0)]
    with pytest.raises(SMTConfigError):
        detect_smt_divergence(primary, None, 0)


def test_empty_series_raises():
    with pytest.raises(SMTConfigError):
        detect_smt_divergence([], [], 0)
