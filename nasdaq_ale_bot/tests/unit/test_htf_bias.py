"""Unit tests for bias/htf_bias.py (HTFBiasDetector two-stage gating)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import structlog
from structlog.testing import capture_logs

from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import (
    FlipState,
    HTFBias,
    HTFBiasDetector,
    HTFBiasState,
)
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.core.leg import Direction
from nasdaq_ale_bot.settings import InstrumentSpec


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="QQQ",
        tick=Decimal("0.01"),
        point_value=Decimal("1.0"),
        atr_ratio_vs_nq=Decimal("0.05"),
        calendar_id="NYSE",
    )


def _bar(ts: datetime, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=1000.0)


def _bar_4h(idx: int, o: float, h: float, lo: float, c: float) -> Candle:
    ts = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc) + timedelta(hours=4 * idx)
    return _bar(ts, o, h, lo, c)


def _bar_1h(idx: int, o: float, h: float, lo: float, c: float) -> Candle:
    ts = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc) + timedelta(hours=idx)
    return _bar(ts, o, h, lo, c)


def _bar_1d(day_offset: int, o: float, h: float, lo: float, c: float) -> Candle:
    ts = datetime(2024, 1, 2, 5, 0, tzinfo=timezone.utc) + timedelta(days=day_offset)
    return _bar(ts, o, h, lo, c)


def _feed_4h(d: HTFBiasDetector, bars: list[Candle]) -> None:
    for i, b in enumerate(bars):
        d._on_4h_close(b, i)


def _up_fvg_seed_bars() -> list[Candle]:
    """3 bars that form an UP FVG with bottom=100 (a.high) and top=105 (c.low)."""
    return [
        _bar_4h(0, 95.0, 100.0, 94.0, 99.0),     # a: high = 100
        _bar_4h(1, 99.0, 108.0, 98.0, 107.0),    # b: body spans (99,107)
        _bar_4h(2, 106.0, 110.0, 105.0, 109.0),  # c: low = 105 -> gap [100,105]
    ]


# ----------------------------------------------------------------------
# Smoke / initial state
# ----------------------------------------------------------------------


def test_first_session_starts_none() -> None:
    d = HTFBiasDetector(_make_instrument())
    st = d.state
    assert isinstance(st, HTFBiasState)
    assert st.bias == HTFBias.NONE
    assert st.flip_state == FlipState.INACTIVE
    assert st.pending_breach_4h_idx is None
    assert st.last_unmitigated_4h_fvg is None


# ----------------------------------------------------------------------
# FVG tracking
# ----------------------------------------------------------------------


def test_4h_unmitigated_fvg_detected() -> None:
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    fvg = d.state.last_unmitigated_4h_fvg
    assert fvg is not None
    assert fvg.direction == Direction.UP
    assert fvg.bottom == pytest.approx(100.0)
    assert fvg.top == pytest.approx(105.0)


def test_4h_fvg_mitigated_by_body_clears() -> None:
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    assert d.state.last_unmitigated_4h_fvg is not None
    # body [102,103] intersects gap [100,105] => mitigation
    mitigator = _bar_4h(3, 103.0, 104.0, 101.0, 102.0)
    d._on_4h_close(mitigator, 3)
    assert d.state.last_unmitigated_4h_fvg is None


def test_break_does_not_mitigate_fvg() -> None:
    """A body-close that overshoots below the gap leaves the FVG unmitigated."""
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    # body [94,96] entirely below bottom=100 => break, not mitigation
    breaker = _bar_4h(3, 96.0, 99.0, 93.0, 94.0)
    d._on_4h_close(breaker, 3)
    assert d.state.last_unmitigated_4h_fvg is not None
    assert d.state.flip_state == FlipState.PENDING


# ----------------------------------------------------------------------
# Two-bar confirmation
# ----------------------------------------------------------------------


def test_opposite_4h_body_flips_pending_direction() -> None:
    """Direction-driven PENDING: opposite-direction 4H bar flips PENDING dir."""
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    d._on_4h_close(_bar_4h(3, 96.0, 99.0, 93.0, 94.0), 3)  # body DOWN
    assert d.state.flip_state == FlipState.PENDING
    d._on_4h_close(_bar_4h(4, 106.0, 111.0, 104.0, 110.0), 4)  # body UP
    assert d.state.flip_state == FlipState.PENDING
    # Direction flipped to UP; breach_idx refreshed to bar 4.
    assert d.state.pending_breach_4h_idx == 4


def test_same_side_4h_body_refreshes_breach_idx() -> None:
    """Same-direction repeat keeps PENDING and updates breach_idx."""
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    d._on_4h_close(_bar_4h(3, 96.0, 99.0, 93.0, 94.0), 3)  # DOWN
    d._on_4h_close(_bar_4h(4, 97.0, 98.0, 92.0, 94.0), 4)  # DOWN
    assert d.state.flip_state == FlipState.PENDING
    # Same direction stays PENDING; breach_idx tracks the latest 4H close.
    assert d.state.pending_breach_4h_idx == 4


def test_4h_direction_pattern_pending_pending_pending() -> None:
    """down / up / down 4H sequence keeps state in PENDING throughout."""
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    d._on_4h_close(_bar_4h(3, 96.0, 99.0, 93.0, 94.0), 3)  # DOWN
    assert d.state.flip_state == FlipState.PENDING
    assert d.state.pending_breach_4h_idx == 3
    d._on_4h_close(_bar_4h(4, 106.0, 111.0, 104.0, 110.0), 4)  # UP
    assert d.state.flip_state == FlipState.PENDING
    assert d.state.pending_breach_4h_idx == 4
    # FVG survives because bar 4's body (106,110) is above the gap, not inside it
    assert d.state.last_unmitigated_4h_fvg is not None
    d._on_4h_close(_bar_4h(5, 97.0, 98.0, 92.0, 94.0), 5)  # DOWN
    assert d.state.flip_state == FlipState.PENDING
    assert d.state.pending_breach_4h_idx == 5


# ----------------------------------------------------------------------
# Higher-TF gating (Daily / 1H)
# ----------------------------------------------------------------------


def _drive_pending_confirmed(d: HTFBiasDetector) -> None:
    """Prime the detector with: UP FVG, bar 3 break, bar 4 same-side confirm."""
    _feed_4h(d, _up_fvg_seed_bars())
    d._on_4h_close(_bar_4h(3, 96.0, 99.0, 93.0, 94.0), 3)
    # Bar 4 body DOWN (open=97, close=94) -> same-side confirmation
    d._on_4h_close(_bar_4h(4, 97.0, 98.0, 92.0, 94.0), 4)


def test_pending_blocks_until_daily_agrees() -> None:
    d = HTFBiasDetector(_make_instrument())
    _drive_pending_confirmed(d)
    # 1H structure agrees with SHORT (LH + LL)
    d._on_1h_close(_bar_1h(0, 100.0, 105.0, 95.0, 102.0))
    d._on_1h_close(_bar_1h(1, 102.0, 104.0, 94.0, 98.0))  # LH + LL
    # Daily body UP (opposite) -> still PENDING
    d._on_daily_close(_bar_1d(0, 95.0, 110.0, 94.0, 108.0))
    assert d.state.flip_state == FlipState.PENDING
    assert d.state.bias == HTFBias.NONE


def test_pending_blocks_until_1h_structure_agrees() -> None:
    d = HTFBiasDetector(_make_instrument())
    _drive_pending_confirmed(d)
    # Daily body DOWN (agrees with SHORT)
    d._on_daily_close(_bar_1d(0, 108.0, 109.0, 90.0, 92.0))
    # 1H structure HH + HL (disagrees with SHORT)
    d._on_1h_close(_bar_1h(0, 100.0, 101.0, 99.0, 100.5))
    d._on_1h_close(_bar_1h(1, 100.5, 103.0, 100.0, 102.0))  # HH + HL
    assert d.state.flip_state == FlipState.PENDING
    assert d.state.bias == HTFBias.NONE


def test_active_promotion_emits_event() -> None:
    structlog.reset_defaults()
    d = HTFBiasDetector(_make_instrument())
    _drive_pending_confirmed(d)
    # Daily close sets PDH=109, PDL=90.
    d._on_daily_close(_bar_1d(0, 108.0, 109.0, 90.0, 92.0))
    with capture_logs() as cap:
        # Price drops below PDL=90 → promote PENDING(SHORT) → ACTIVE.
        d._check_flip_promotion(price=89.0)
    assert d.state.flip_state == FlipState.ACTIVE
    assert d.state.bias == HTFBias.SHORT
    active_events = [e for e in cap if e.get("event") == "BIAS_FLIP_ACTIVE"]
    assert len(active_events) == 1
    assert active_events[0]["direction"] == "DOWN"
    assert active_events[0]["bias"] == "SHORT"


def test_active_does_not_double_emit() -> None:
    """Further bars in the same direction do not re-emit BIAS_FLIP_ACTIVE."""
    d = HTFBiasDetector(_make_instrument())
    _drive_pending_confirmed(d)
    d._on_daily_close(_bar_1d(0, 108.0, 109.0, 90.0, 92.0))
    with capture_logs() as cap:
        d._check_flip_promotion(price=89.0)  # promotes
        d._check_flip_promotion(price=85.0)  # already ACTIVE — no re-emit
    active_events = [e for e in cap if e.get("event") == "BIAS_FLIP_ACTIVE"]
    assert len(active_events) == 1


def test_pending_event_emitted_on_break() -> None:
    structlog.reset_defaults()
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    with capture_logs() as cap:
        d._on_4h_close(_bar_4h(3, 96.0, 99.0, 93.0, 94.0), 3)
    events = [e for e in cap if e.get("event") == "BIAS_FLIP_PENDING"]
    assert len(events) == 1
    assert events[0]["direction"] == "DOWN"


# ----------------------------------------------------------------------
# 1H structure helper branches
# ----------------------------------------------------------------------


def test_1h_structure_requires_two_bars() -> None:
    d = HTFBiasDetector(_make_instrument())
    # No 1H bars yet -> structure is None
    assert d._one_h_structure() is None
    d._on_1h_close(_bar_1h(0, 100.0, 101.0, 99.0, 100.5))
    assert d._one_h_structure() is None


def test_1h_structure_mixed_returns_none() -> None:
    d = HTFBiasDetector(_make_instrument())
    d._on_1h_close(_bar_1h(0, 100.0, 105.0, 99.0, 101.0))
    # HH but LL -> mixed
    d._on_1h_close(_bar_1h(1, 101.0, 106.0, 95.0, 102.0))
    assert d._one_h_structure() is None


# ----------------------------------------------------------------------
# End-to-end via on_1m_bar (aggregation sanity)
# ----------------------------------------------------------------------


def test_on_1m_bar_feeds_all_three_aggregators() -> None:
    d = HTFBiasDetector(_make_instrument())
    # One 1m bar at a 4H boundary: no output yet (buckets initialized).
    start = datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)
    st = d.on_1m_bar(_bar(start, 100, 101, 99, 100))
    assert st.bias == HTFBias.NONE
    # Feed another at the next 4H boundary to force closes of the first buckets.
    nxt = datetime(2024, 1, 2, 16, 0, tzinfo=timezone.utc)
    st2 = d.on_1m_bar(_bar(nxt, 101, 102, 100, 101))
    assert isinstance(st2, HTFBiasState)


def test_doji_4h_body_leaves_pending_unchanged() -> None:
    """A doji (open==close) carries no body direction → state unchanged."""
    d = HTFBiasDetector(_make_instrument())
    _feed_4h(d, _up_fvg_seed_bars())
    d._on_4h_close(_bar_4h(3, 96.0, 99.0, 93.0, 94.0), 3)
    assert d.state.flip_state == FlipState.PENDING
    d._on_4h_close(_bar_4h(4, 110.0, 111.0, 109.0, 110.0), 4)  # doji
    # Doji has no direction signal; PENDING(DOWN) from bar 3 persists.
    assert d.state.flip_state == FlipState.PENDING
