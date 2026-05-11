"""Unit tests for SMTTracker §A13 fail-closed UNAVAILABLE logic."""

from __future__ import annotations

from datetime import datetime, timezone


from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.core.smt_tracker import SMTTracker, SMTVerdict
from nasdaq_ale_bot.execution.gates import TradeIntent
from nasdaq_ale_bot.core.account_ledger import AccountLedger


def _mk(ts: datetime, o: float, h: float, lo: float, c: float, v: float = 100.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=v)


def _bar(ts: datetime, price: float = 100.0) -> Candle:
    return _mk(ts, price, price + 0.5, price - 0.5, price)


def _t(h: int, m: int) -> datetime:
    return datetime(2024, 1, 2, h, m, tzinfo=timezone.utc)


def _tracker() -> SMTTracker:
    return SMTTracker(primary_symbol="QQQ", correlated_symbol="SPY")


# ----------------------------------------------------------------------
# Two consecutive misses -> UNAVAILABLE
# ----------------------------------------------------------------------


def test_two_missing_blocks_latch_unavailable() -> None:
    """Misses at 14:32 and 14:33 latch UNAVAILABLE within the 14:30 window."""
    t = _tracker()
    t.on_1m_bar_pair(_bar(_t(14, 30)), _bar(_t(14, 30), 50.0), _t(14, 30))
    t.on_1m_bar_pair(_bar(_t(14, 31)), _bar(_t(14, 31), 50.0), _t(14, 31))
    # First miss (streak 0->1, forward fill)
    v1 = t.on_1m_bar_pair(None, _bar(_t(14, 32), 50.0), _t(14, 32))
    assert v1 != SMTVerdict.UNAVAILABLE  # still ok after 1st miss
    assert t._missing_streak_primary == 1
    # Second consecutive miss (streak 1->2, latch UNAVAILABLE)
    v2 = t.on_1m_bar_pair(None, _bar(_t(14, 33), 50.0), _t(14, 33))
    assert v2 == SMTVerdict.UNAVAILABLE


def test_two_correlated_missing_blocks_unavailable() -> None:
    """Correlated misses also trigger UNAVAILABLE."""
    t = _tracker()
    t.on_1m_bar_pair(_bar(_t(14, 30)), _bar(_t(14, 30), 50.0), _t(14, 30))
    t.on_1m_bar_pair(_bar(_t(14, 31)), None, _t(14, 31))   # 1st miss correlated
    v = t.on_1m_bar_pair(_bar(_t(14, 32)), None, _t(14, 32))  # 2nd miss
    assert v == SMTVerdict.UNAVAILABLE


def test_unavailable_persists_within_5m_window() -> None:
    """UNAVAILABLE from a miss within a window persists for intra-window queries."""
    t = _tracker()
    t.on_1m_bar_pair(_bar(_t(14, 30)), _bar(_t(14, 30), 50.0), _t(14, 30))
    t.on_1m_bar_pair(None, _bar(_t(14, 31), 50.0), _t(14, 31))  # 1st miss
    t.on_1m_bar_pair(None, _bar(_t(14, 32), 50.0), _t(14, 32))  # 2nd -> UNAVAILABLE
    # Remaining intra-window bars
    v3 = t.on_1m_bar_pair(_bar(_t(14, 33)), _bar(_t(14, 33), 50.0), _t(14, 33))
    v4 = t.on_1m_bar_pair(_bar(_t(14, 34)), _bar(_t(14, 34), 50.0), _t(14, 34))
    assert v3 == SMTVerdict.UNAVAILABLE
    assert v4 == SMTVerdict.UNAVAILABLE


def test_unavailable_clears_after_clean_5m_window() -> None:
    """A full clean window following an UNAVAILABLE window resets the verdict."""
    t = _tracker()
    # Window 1 (14:30): set UNAVAILABLE mid-window
    t.on_1m_bar_pair(_bar(_t(14, 30)), _bar(_t(14, 30), 50.0), _t(14, 30))
    t.on_1m_bar_pair(None, _bar(_t(14, 31), 50.0), _t(14, 31))
    t.on_1m_bar_pair(None, _bar(_t(14, 32), 50.0), _t(14, 32))  # UNAVAILABLE
    for m in range(33, 35):
        t.on_1m_bar_pair(_bar(_t(14, m)), _bar(_t(14, m), 50.0), _t(14, m))
    # Window 2 (14:35): clean
    for m in range(35, 40):
        t.on_1m_bar_pair(_bar(_t(14, m), 100.0), _bar(_t(14, m), 50.0), _t(14, m))
    # Trigger close of window 2 -> recomputes
    v = t.on_1m_bar_pair(_bar(_t(14, 40)), _bar(_t(14, 40), 50.0), _t(14, 40))
    assert v != SMTVerdict.UNAVAILABLE


def test_no_prior_bar_first_miss_is_unavailable() -> None:
    """Missing on the very first bar (no history to forward-fill) -> UNAVAILABLE."""
    t = _tracker()
    # No bars fed yet; first pair has a missing primary
    t.on_1m_bar_pair(None, _bar(_t(14, 30), 50.0), _t(14, 30))
    # Two consecutive misses at start => UNAVAILABLE immediately
    v = t.on_1m_bar_pair(None, _bar(_t(14, 31), 50.0), _t(14, 31))
    assert v == SMTVerdict.UNAVAILABLE


def test_streak_resets_after_present_bar() -> None:
    """A present bar after two misses resets the streak for future windows."""
    t = _tracker()
    t.on_1m_bar_pair(_bar(_t(14, 30)), _bar(_t(14, 30), 50.0), _t(14, 30))
    t.on_1m_bar_pair(None, _bar(_t(14, 31), 50.0), _t(14, 31))
    t.on_1m_bar_pair(None, _bar(_t(14, 32), 50.0), _t(14, 32))  # UNAVAILABLE latched
    # Present bar resets primary streak
    t.on_1m_bar_pair(_bar(_t(14, 33)), _bar(_t(14, 33), 50.0), _t(14, 33))
    assert t._missing_streak_primary == 0


# ----------------------------------------------------------------------
# Gate integration
# ----------------------------------------------------------------------


def test_unavailable_verdict_blocks_smt_gate() -> None:
    """SMTVerdict.UNAVAILABLE fed into TradeIntent blocks SMTAvailabilityGate."""
    from decimal import Decimal
    from datetime import date

    ledger = AccountLedger(
        session_start_equity=Decimal("10000"),
        today=date(2024, 1, 2),
    )
    intent = TradeIntent(
        symbol="QQQ",
        side="BUY",
        entry_price=Decimal("440"),
        stop_price=Decimal("438"),
        projected_risk_usd=Decimal("50"),
        ts_utc=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        smt_verdict=SMTVerdict.UNAVAILABLE,
    )
    from nasdaq_ale_bot.execution.gates import SMTAvailabilityGate
    gate = SMTAvailabilityGate()
    result = gate.check(ledger, intent)
    assert not result.allowed
    assert result.reason == "SKIP_SMT_UNAVAILABLE"


def test_non_unavailable_verdict_passes_smt_gate() -> None:
    from decimal import Decimal
    from datetime import date

    ledger = AccountLedger(
        session_start_equity=Decimal("10000"),
        today=date(2024, 1, 2),
    )
    for good_verdict in (SMTVerdict.BULLISH_DIVERGENCE, SMTVerdict.BEARISH_DIVERGENCE, SMTVerdict.NONE):
        intent = TradeIntent(
            symbol="QQQ",
            side="BUY",
            entry_price=Decimal("440"),
            stop_price=Decimal("438"),
            projected_risk_usd=Decimal("50"),
            ts_utc=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            smt_verdict=good_verdict,
        )
        from nasdaq_ale_bot.execution.gates import SMTAvailabilityGate
        gate = SMTAvailabilityGate()
        result = gate.check(ledger, intent)
        assert result.allowed, f"Expected gate to pass for {good_verdict}"


def test_force_close_returns_verdict() -> None:
    t = _tracker()
    t.on_1m_bar_pair(_bar(_t(14, 30)), _bar(_t(14, 30), 50.0), _t(14, 30))
    v = t.force_close()
    assert isinstance(v, SMTVerdict)
