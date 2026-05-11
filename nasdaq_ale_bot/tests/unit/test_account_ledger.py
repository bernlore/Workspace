"""Unit tests for core/account_ledger.py."""

from __future__ import annotations

import dataclasses
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nasdaq_ale_bot.core.account_ledger import (
    AccountLedger,
    OrderFillEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc
_BASE_DATE = date(2024, 1, 15)
_BASE_EQUITY = Decimal("50000")


def _ts(offset_seconds: int = 0) -> datetime:
    return datetime(2024, 1, 15, 9, 30, tzinfo=UTC) + timedelta(seconds=offset_seconds)


def _make_ledger(equity: Decimal = _BASE_EQUITY) -> AccountLedger:
    return AccountLedger(session_start_equity=equity, today=_BASE_DATE)


def _make_fill(realized_pnl_delta: Decimal = Decimal("100")) -> OrderFillEvent:
    return OrderFillEvent(
        fill_ts=_ts(),
        symbol="MNQ",
        side="BUY",
        qty=Decimal("1"),
        fill_price=Decimal("18000"),
        fees=Decimal("0.5"),
        realized_pnl_delta=realized_pnl_delta,
    )


# ---------------------------------------------------------------------------
# 1. test_decimal_only_inputs_rejected
# ---------------------------------------------------------------------------


def test_decimal_only_inputs_rejected() -> None:
    """Passing a float realized_pnl_delta to on_fill raises TypeError."""
    ledger = _make_ledger()
    # Build a valid fill event then force a float into realized_pnl_delta
    # via dataclasses.replace (frozen dataclass allows this at construction).
    good_event = _make_fill()
    bogus_event = dataclasses.replace(good_event, realized_pnl_delta=0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ledger.on_fill(bogus_event)


# ---------------------------------------------------------------------------
# 2. test_from_floats_quantizes
# ---------------------------------------------------------------------------


def test_from_floats_quantizes() -> None:
    """from_floats quantizes 0.1 + 0.2 to Decimal('0.30000000')."""
    event = OrderFillEvent.from_floats(
        fill_ts=_ts(),
        symbol="MNQ",
        side="BUY",
        qty=1.0,
        fill_price=18000.0,
        fees=0.5,
        realized_pnl_delta=0.1 + 0.2,
    )
    assert event.realized_pnl_delta == Decimal("0.30000000")


# ---------------------------------------------------------------------------
# 3. test_hwm_monotonicity_under_replay
# ---------------------------------------------------------------------------


def test_hwm_monotonicity_under_replay() -> None:
    """HWM must be non-decreasing across 1000 snapshots with shuffled timestamps."""
    ledger = _make_ledger()
    rng = random.Random(42)

    base_ts = datetime(2024, 1, 15, 9, 30, tzinfo=UTC)
    # Generate 1000 (timestamp, unrealized) pairs with monotonically increasing ts
    monotonic_snapshots = [
        (base_ts + timedelta(seconds=i), Decimal(str(rng.uniform(-500, 2000))))
        for i in range(1000)
    ]
    # Shuffle to create out-of-order scenario
    shuffled = monotonic_snapshots[:]
    rng.shuffle(shuffled)

    hwm_values: list[Decimal] = []
    dropped = 0

    for snap_ts, unreal in shuffled:
        prev_last = ledger._last_snapshot_ts  # type: ignore[attr-defined]

        ledger.on_unrealized_snapshot(snap_ts, unreal)

        after_last = ledger._last_snapshot_ts  # type: ignore[attr-defined]
        if after_last == prev_last:
            # snapshot was dropped
            dropped += 1

        hwm_values.append(ledger.high_watermark_equity)

    # HWM must be non-decreasing
    for i in range(1, len(hwm_values)):
        assert hwm_values[i] >= hwm_values[i - 1], (
            f"HWM regressed at index {i}: {hwm_values[i]} < {hwm_values[i-1]}"
        )

    # Shuffled sequence must have had some out-of-order drops
    assert dropped > 0, "Expected some out-of-order snapshots to be dropped"


# ---------------------------------------------------------------------------
# 4. test_eod_rotation
# ---------------------------------------------------------------------------


def test_eod_rotation() -> None:
    """on_session_rotation correctly advances metrics and resets daily state."""
    ledger = _make_ledger()

    # Make some fills
    ledger.on_fill(_make_fill(Decimal("300")))
    ledger.on_fill(_make_fill(Decimal("200")))
    assert ledger.realized_today == Decimal("500")

    hwm_before = ledger.high_watermark_equity
    window_start_before = ledger.profit_window_start_date

    new_equity = Decimal("50500")
    new_today = date(2024, 1, 16)
    ledger.on_session_rotation(new_today, new_equity)

    # realized_today reset
    assert ledger.realized_today == Decimal("0")
    # unrealized reset
    assert ledger.unrealized == Decimal("0")
    # best_day_profit updated (500 > 0)
    assert ledger.best_day_profit == Decimal("500")
    # cumulative_profit advanced
    assert ledger.cumulative_profit == Decimal("500")
    # HWM preserved (not reset)
    assert ledger.high_watermark_equity == hwm_before
    # profit_window_start_date unchanged
    assert ledger.profit_window_start_date == window_start_before
    # session_start_equity updated
    assert ledger.session_start_equity == new_equity


# ---------------------------------------------------------------------------
# 5. test_hwm_never_regresses_due_to_rounding
# ---------------------------------------------------------------------------


def test_hwm_never_regresses_due_to_rounding() -> None:
    """Decimal pairs that would float-regress must not raise LedgerInvariantError."""
    ledger = _make_ledger(Decimal("10000"))

    # Feed a snapshot that advances HWM
    ts1 = _ts(1)
    # 0.1 + 0.2 in float is 0.30000000000000004; as Decimal("0.3") it is exact
    unrealized_1 = Decimal("0.3")
    ledger.on_unrealized_snapshot(ts1, unrealized_1)
    hwm_after_first = ledger.high_watermark_equity

    # Feed an equal Decimal value — must not cause any error
    ts2 = _ts(2)
    unrealized_2 = Decimal("0.30000000")
    # This equals unrealized_1 in Decimal, so equity stays the same -> no HWM change
    ledger.on_unrealized_snapshot(ts2, unrealized_2)

    # No regression
    assert ledger.high_watermark_equity >= hwm_after_first


# ---------------------------------------------------------------------------
# 6. test_session_start_snapshot_immutable_within_session
# ---------------------------------------------------------------------------


def test_session_start_snapshot_immutable_within_session() -> None:
    """session_start_equity is read-only between rotations."""
    equity = Decimal("55000")
    ledger = _make_ledger(equity)

    # Fills and snapshots must not alter session_start_equity
    ledger.on_fill(_make_fill(Decimal("100")))
    ledger.on_unrealized_snapshot(_ts(1), Decimal("50"))

    assert ledger.session_start_equity == equity

    # Only rotation replaces it
    new_equity = Decimal("55100")
    ledger.on_session_rotation(date(2024, 1, 16), new_equity)
    assert ledger.session_start_equity == new_equity


# ---------------------------------------------------------------------------
# 7. test_current_equity_computed
# ---------------------------------------------------------------------------


def test_current_equity_computed() -> None:
    """current_equity == session_start_equity + realized_today + unrealized."""
    ledger = _make_ledger(Decimal("50000"))
    ledger.on_fill(_make_fill(Decimal("250")))
    ledger.on_unrealized_snapshot(_ts(1), Decimal("75"))

    expected = Decimal("50000") + Decimal("250") + Decimal("75")
    assert ledger.current_equity == expected


# ---------------------------------------------------------------------------
# 8. test_on_unrealized_snapshot_out_of_order_dropped
# ---------------------------------------------------------------------------


def test_on_unrealized_snapshot_out_of_order_dropped() -> None:
    """An earlier snapshot_ts after a later one must be silently dropped."""
    ledger = _make_ledger()
    ts_later = _ts(10)
    ts_earlier = _ts(5)

    ledger.on_unrealized_snapshot(ts_later, Decimal("200"))
    assert ledger.unrealized == Decimal("200")

    ledger.on_unrealized_snapshot(ts_earlier, Decimal("999"))
    # Still the first value — second was dropped
    assert ledger.unrealized == Decimal("200")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_fill_ts_must_be_tz_aware() -> None:
    """OrderFillEvent rejects naive fill_ts."""
    with pytest.raises(ValueError, match="timezone-aware"):
        OrderFillEvent(
            fill_ts=datetime(2024, 1, 15, 9, 30),  # naive
            symbol="MNQ",
            side="BUY",
            qty=Decimal("1"),
            fill_price=Decimal("18000"),
            fees=Decimal("0.5"),
            realized_pnl_delta=Decimal("0"),
        )


def test_session_start_equity_must_be_decimal() -> None:
    """AccountLedger.__init__ asserts Decimal type for session_start_equity."""
    with pytest.raises(AssertionError):
        AccountLedger(session_start_equity=50000.0, today=_BASE_DATE)  # type: ignore[arg-type]


def test_best_day_profit_not_updated_when_smaller() -> None:
    """best_day_profit is not overwritten if today's realized < current best."""
    ledger = _make_ledger()
    # First day: 500 profit
    ledger.on_fill(_make_fill(Decimal("500")))
    ledger.on_session_rotation(date(2024, 1, 16), Decimal("50500"))
    assert ledger.best_day_profit == Decimal("500")

    # Second day: 100 profit (< 500)
    ledger.on_fill(_make_fill(Decimal("100")))
    ledger.on_session_rotation(date(2024, 1, 17), Decimal("50600"))
    # best_day_profit must stay at 500
    assert ledger.best_day_profit == Decimal("500")
    assert ledger.cumulative_profit == Decimal("600")
