"""Unit tests for SMTTracker clock-anchored 5m aggregation (§A12)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.core.smt_tracker import SMTTracker, SMTVerdict


def _mk(ts: datetime, o: float, h: float, lo: float, c: float, v: float = 100.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=v)


def _bar(ts: datetime, price: float = 100.0) -> Candle:
    return _mk(ts, price, price + 0.5, price - 0.5, price)


def _t(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2024, 1, 2, h, m, s, tzinfo=timezone.utc)


def _tracker() -> SMTTracker:
    return SMTTracker(primary_symbol="QQQ", correlated_symbol="SPY")


# ----------------------------------------------------------------------
# Anchor detection
# ----------------------------------------------------------------------


def test_is_anchor_boundary_at_5m_marks() -> None:
    t = _tracker()
    assert t._is_anchor_boundary(_t(14, 30)) is True
    assert t._is_anchor_boundary(_t(14, 35)) is True
    assert t._is_anchor_boundary(_t(14, 31)) is False
    assert t._is_anchor_boundary(_t(14, 34)) is False


def test_anchor_for_off_boundary_snaps_down() -> None:
    t = _tracker()
    assert t._anchor_for(_t(14, 32)) == _t(14, 30)
    assert t._anchor_for(_t(14, 34)) == _t(14, 30)
    assert t._anchor_for(_t(14, 35)) == _t(14, 35)


# ----------------------------------------------------------------------
# Clock-anchored 5m close
# ----------------------------------------------------------------------


def test_clock_anchored_5m_boundaries() -> None:
    """1m bars 14:30-14:34 (5 bars) form anchor 14:30; first bar of 14:35 triggers close."""
    t = _tracker()
    for m in range(30, 35):
        v = t.on_1m_bar_pair(_bar(_t(14, m)), _bar(_t(14, m), 50.0), _t(14, m))
        assert v == SMTVerdict.NONE  # no second 5m bar yet
    # no 5m bar emitted yet (close triggered on NEXT anchor)
    assert len(t._primary_5m_buffer) == 0
    # feed first bar of 14:35 -> triggers close of 14:30
    t.on_1m_bar_pair(_bar(_t(14, 35)), _bar(_t(14, 35), 50.0), _t(14, 35))
    assert len(t._primary_5m_buffer) == 1
    assert t._primary_5m_buffer[0].ts == _t(14, 30)


def test_5m_bar_aggregates_ohlcv_correctly() -> None:
    t = _tracker()
    prices = [100.0, 101.0, 99.5, 102.0, 100.5]
    for i, p in enumerate(prices):
        t.on_1m_bar_pair(
            _mk(_t(14, 30 + i), p, p + 1.0, p - 1.0, p, 200.0),
            _bar(_t(14, 30 + i), 50.0),
            _t(14, 30 + i),
        )
    # trigger close
    t.on_1m_bar_pair(_bar(_t(14, 35)), _bar(_t(14, 35), 50.0), _t(14, 35))
    bar = t._primary_5m_buffer[0]
    assert bar.open == pytest.approx(100.0)    # first bar
    assert bar.close == pytest.approx(100.5)   # last bar
    assert bar.high == pytest.approx(103.0)    # max high = 102 + 1
    assert bar.low == pytest.approx(98.5)      # min low = 99.5 - 1
    assert bar.volume == pytest.approx(1000.0) # 5 bars * 200


def test_verdict_latches_at_5m_close() -> None:
    """Verdict is set on close and returned unchanged for intra-5m bars."""
    t = _tracker()
    # Build up TWO 5m bars to get a divergence result.
    # Window 1: primary HH, correlated LH -> bearish divergence at window 2
    # Window 1 (14:30-14:34): primary high=102, corr high=50
    for m in range(30, 35):
        t.on_1m_bar_pair(_bar(_t(14, m), 100.0), _bar(_t(14, m), 48.0), _t(14, m))
    # Window 2 (14:35-14:39): primary high=105 (HH vs 102), corr high=46 (LH vs 50) -> bearish
    for m in range(35, 40):
        t.on_1m_bar_pair(
            _mk(_t(14, m), 103.0, 105.0, 102.5, 103.5),
            _mk(_t(14, m), 44.0, 46.0, 43.5, 44.5),
            _t(14, m),
        )
    # Before close of window 2 (no trigger bar yet) -- verdict is still NONE (window 1 produced NONE)
    assert t._latched_verdict == SMTVerdict.NONE
    # Trigger close of window 2 with first bar of 14:40
    t.on_1m_bar_pair(_bar(_t(14, 40), 103.0), _bar(_t(14, 40), 44.0), _t(14, 40))
    assert t._latched_verdict == SMTVerdict.BEARISH_DIVERGENCE
    # Intra-5m bars of window 3 do not change verdict
    t.on_1m_bar_pair(_bar(_t(14, 41), 103.0), _bar(_t(14, 41), 44.0), _t(14, 41))
    assert t._latched_verdict == SMTVerdict.BEARISH_DIVERGENCE


def test_properties_exposed() -> None:
    t = _tracker()
    assert t.primary_symbol == "QQQ"
    assert t.correlated_symbol == "SPY"
    assert t.verdict == SMTVerdict.NONE


# ----------------------------------------------------------------------
# Forward fill
# ----------------------------------------------------------------------


def test_forward_fill_one_missing_verdict_computable() -> None:
    """Primary missing at 14:32; forward-fill keeps verdict non-UNAVAILABLE."""
    t = _tracker()
    # Window 1
    for m in range(30, 35):
        t.on_1m_bar_pair(_bar(_t(14, m), 100.0), _bar(_t(14, m), 50.0), _t(14, m))
    # Window 2: primary bar at 14:32 is None (forward fill)
    for m in range(35, 40):
        primary = None if m == 37 else _bar(_t(14, m), 102.0)
        t.on_1m_bar_pair(primary, _bar(_t(14, m), 50.0), _t(14, m))
    # Trigger close
    t.on_1m_bar_pair(_bar(_t(14, 40), 102.0), _bar(_t(14, 40), 50.0), _t(14, 40))
    # Not UNAVAILABLE (only 1 miss, which was forward-filled)
    assert t._latched_verdict != SMTVerdict.UNAVAILABLE


def test_forward_fill_synthetic_bar_uses_last_close() -> None:
    """The synthetic bar has OHLC == previous bar's close."""
    t = _tracker()
    last_close = 101.5
    t.on_1m_bar_pair(
        _mk(_t(14, 30), 100.0, 101.5, 99.5, last_close),
        _bar(_t(14, 30), 50.0),
        _t(14, 30),
    )
    # Feed a missing primary bar at 14:31
    t.on_1m_bar_pair(None, _bar(_t(14, 31), 50.0), _t(14, 31))
    # The in-flight primary list should have the synthetic bar
    assert len(t._in_flight_primary_1m) == 2
    synth = t._in_flight_primary_1m[1]
    assert synth.open == pytest.approx(last_close)
    assert synth.high == pytest.approx(last_close)
    assert synth.low == pytest.approx(last_close)
    assert synth.close == pytest.approx(last_close)
    assert synth.volume == 0.0
