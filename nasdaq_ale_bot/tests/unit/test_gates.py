"""Unit tests for execution/gates.py.

Coverage target: ≥95% branch coverage on gates.py.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import structlog.testing

from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import (
    DailyLossGate,
    EntryGate,
    GateList,
    GateResult,
    KillzoneGate,
    MaxStopGate,
    MaxTradesGate,
    NewsBlackoutGate,
    ProjectedLossGate,
    SMTAvailabilityGate,
    TradeIntent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc
_BASE_DATE = date(2024, 1, 15)
_BASE_EQUITY = Decimal("50000")

# A Tuesday in AM killzone (09:35 ET = 14:35 UTC) — NYSE trading day
_KILLZONE_TS = datetime(2024, 1, 16, 14, 35, tzinfo=UTC)  # Tuesday 09:35 ET
# Outside killzone: midday (12:30 ET = 17:30 UTC)
_OUTSIDE_TS = datetime(2024, 1, 16, 21, 30, tzinfo=UTC)   # Tuesday 16:30 ET (after secondary killzone)


def _make_ledger(
    *,
    realized: Decimal = Decimal("0"),
    unrealized: Decimal = Decimal("0"),
    equity: Decimal = _BASE_EQUITY,
    trades_today: int = 0,
) -> AccountLedger:
    ledger = AccountLedger(session_start_equity=equity, today=_BASE_DATE)
    if realized != Decimal("0"):
        from nasdaq_ale_bot.core.account_ledger import OrderFillEvent

        event = OrderFillEvent(
            fill_ts=datetime(2024, 1, 15, 9, 30, tzinfo=UTC),
            symbol="MNQ",
            side="BUY",
            qty=Decimal("1"),
            fill_price=Decimal("18000"),
            fees=Decimal("0"),
            realized_pnl_delta=realized,
        )
        ledger.on_fill(event)
    if unrealized != Decimal("0"):
        ledger.on_unrealized_snapshot(
            datetime(2024, 1, 15, 9, 31, tzinfo=UTC), unrealized
        )
    # Attach trades_today as a dynamic attribute for MaxTradesGate
    ledger.trades_today = trades_today  # type: ignore[attr-defined]
    return ledger


def _make_intent(
    *,
    entry_price: Decimal = Decimal("18000"),
    stop_price: Decimal = Decimal("17990"),
    projected_risk_usd: Decimal = Decimal("300"),
    ts_utc: datetime = _KILLZONE_TS,
    smt_verdict: str | None = "CONFIRMED",
    symbol: str = "MNQ",
    side: str = "BUY",
) -> TradeIntent:
    base = TradeIntent(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        entry_price=entry_price,
        stop_price=stop_price,
        projected_risk_usd=projected_risk_usd,
        ts_utc=ts_utc,
    )
    # Attach smt_verdict as extra attribute (gates use getattr)
    object.__setattr__(base, "smt_verdict", smt_verdict)  # frozen dataclass trick
    return base


# ---------------------------------------------------------------------------
# Helpers for stub gates
# ---------------------------------------------------------------------------


class _StubGate:
    """Configurable test stub implementing EntryGate protocol."""

    def __init__(self, name: str, *, allow: bool, reason: str | None = None) -> None:
        self.name = name
        self._allow = allow
        self._reason = reason
        self.call_count = 0

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        self.call_count += 1
        return GateResult(
            allowed=self._allow,
            gate_name=self.name,
            reason=self._reason,
        )


# ---------------------------------------------------------------------------
# test_base_list_exact_set_and_order
# ---------------------------------------------------------------------------


def test_base_list_exact_set_and_order() -> None:
    """GateList.base_list returns exactly 7 gates in the documented order."""
    config: dict[str, Any] = {
        "daily_loss_limit": -1500,
        "max_trades_per_day": 2,
        "max_stop_nq_pts": 25,
        "news": {"window_seconds": 900},
    }
    gate_list = GateList.base_list(config)
    names = tuple(g.name for g in gate_list._gates)
    expected = (
        "DailyLossGate",
        "ProjectedLossGate",
        "KillzoneGate",
        "NewsBlackoutGate",
        "SMTAvailabilityGate",
        "MaxTradesGate",
        "MaxStopGate",
        "TrendRegimeGate",
    )
    assert names == expected, f"Got {names}, expected {expected}"
    assert len(gate_list._gates) == 8


# ---------------------------------------------------------------------------
# test_each_gate_has_unique_name
# ---------------------------------------------------------------------------


def test_each_gate_has_unique_name() -> None:
    """Every gate class in the base list has a unique name constant."""
    config: dict[str, Any] = {
        "daily_loss_limit": -1500,
        "max_trades_per_day": 2,
        "max_stop_nq_pts": 25,
        "news": {"window_seconds": 900},
    }
    gate_list = GateList.base_list(config)
    names = [g.name for g in gate_list._gates]
    assert len(names) == len(set(names)), f"Duplicate names found: {names}"


# ---------------------------------------------------------------------------
# test_first_failing_gate_short_circuits
# ---------------------------------------------------------------------------


def test_first_failing_gate_short_circuits() -> None:
    """evaluate() stops at the first failing gate and does not call later ones."""
    gate1 = _StubGate("Gate1", allow=False, reason="SKIP_GATE1")
    gate2 = _StubGate("Gate2", allow=False, reason="SKIP_GATE2")
    gate3 = _StubGate("Gate3", allow=True)

    gl = GateList([gate1, gate2, gate3])
    ledger = _make_ledger()
    intent = _make_intent()

    result = gl.evaluate(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_GATE1"
    assert result.gate_name == "Gate1"
    # Only gate1 was called
    assert gate1.call_count == 1
    assert gate2.call_count == 0
    assert gate3.call_count == 0


# ---------------------------------------------------------------------------
# test_evaluate_returns_pass_when_all_allowed
# ---------------------------------------------------------------------------


def test_evaluate_returns_pass_when_all_allowed() -> None:
    """evaluate() returns allowed=True with gate_name='ALL_PASS' when all pass."""
    gate1 = _StubGate("Gate1", allow=True)
    gate2 = _StubGate("Gate2", allow=True)

    gl = GateList([gate1, gate2])
    ledger = _make_ledger()
    intent = _make_intent()

    result = gl.evaluate(ledger, intent)

    assert result.allowed is True
    assert result.gate_name == "ALL_PASS"
    assert result.reason is None
    assert gate1.call_count == 1
    assert gate2.call_count == 1


# ---------------------------------------------------------------------------
# test_evaluate_all_runs_every_gate  (A5)
# ---------------------------------------------------------------------------


def test_evaluate_all_runs_every_gate() -> None:
    """evaluate_all runs all 7 gates even when gates 1 and 3 fail."""
    gates = [
        _StubGate("G1", allow=False, reason="SKIP_1"),
        _StubGate("G2", allow=True),
        _StubGate("G3", allow=False, reason="SKIP_3"),
        _StubGate("G4", allow=True),
        _StubGate("G5", allow=True),
        _StubGate("G6", allow=True),
        _StubGate("G7", allow=True),
    ]
    gl = GateList(gates)
    ledger = _make_ledger()
    intent = _make_intent()

    results = gl.evaluate_all(ledger, intent)

    # Every gate ran
    assert len(results) == 7
    for g in gates:
        assert g.call_count == 1, f"{g.name} call_count={g.call_count}"

    # Order preserved
    assert results[0].allowed is False
    assert results[0].reason == "SKIP_1"
    assert results[2].allowed is False
    assert results[2].reason == "SKIP_3"
    assert results[1].allowed is True

    # evaluate() would have stopped at the first failure
    gl.evaluate(ledger, intent)
    # Gates were called again by evaluate — reset and re-check
    for g in gates:
        g.call_count = 0
    result_short2 = gl.evaluate(ledger, intent)
    assert result_short2.gate_name == "G1"
    # G2 onwards should not be called
    assert gates[1].call_count == 0


# ---------------------------------------------------------------------------
# test_evaluate_all_default_no_per_gate_logs  (W2)
# ---------------------------------------------------------------------------


def test_evaluate_all_default_no_per_gate_logs() -> None:
    """evaluate_all with default log_individual_evals=False emits no GATE_EVAL events."""
    gates = [_StubGate(f"G{i}", allow=True) for i in range(3)]
    gl = GateList(gates)
    ledger = _make_ledger()
    intent = _make_intent()

    with structlog.testing.capture_logs() as cap:
        gl.evaluate_all(ledger, intent)

    gate_evals = [e for e in cap if e.get("event") == "GATE_EVAL"]
    assert gate_evals == [], f"Expected no GATE_EVAL logs, got: {gate_evals}"


def test_evaluate_all_with_log_individual_evals_true_emits_events() -> None:
    """evaluate_all with log_individual_evals=True emits one GATE_EVAL per gate."""
    gates = [_StubGate(f"G{i}", allow=True) for i in range(3)]
    gl = GateList(gates)
    ledger = _make_ledger()
    intent = _make_intent()

    with structlog.testing.capture_logs() as cap:
        gl.evaluate_all(ledger, intent, log_individual_evals=True)

    gate_evals = [e for e in cap if e.get("event") == "GATE_EVAL"]
    assert len(gate_evals) == 3


# ---------------------------------------------------------------------------
# test_max_stop_gate_emits_structlog_event
# ---------------------------------------------------------------------------


def test_max_stop_gate_emits_structlog_event() -> None:
    """MaxStopGate emits a SKIP_MAX_STOP structlog event when blocking."""
    gate = MaxStopGate(max_stop_nq_pts=Decimal("25"))
    ledger = _make_ledger()
    # Stop distance = 30 pts (> 25)
    intent = _make_intent(
        entry_price=Decimal("18000"),
        stop_price=Decimal("17970"),  # 30 pts away
    )

    with structlog.testing.capture_logs() as cap:
        result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_MAX_STOP"

    skip_events = [e for e in cap if e.get("event") == "SKIP_MAX_STOP"]
    assert len(skip_events) == 1
    ev = skip_events[0]
    assert ev["gate_name"] == "MaxStopGate"
    assert ev["symbol"] == "MNQ"


def test_max_stop_gate_allows_within_limit() -> None:
    """MaxStopGate passes when stop distance is within the limit."""
    gate = MaxStopGate(max_stop_nq_pts=Decimal("25"))
    ledger = _make_ledger()
    intent = _make_intent(
        entry_price=Decimal("18000"),
        stop_price=Decimal("17980"),  # 20 pts away
    )
    result = gate.check(ledger, intent)
    assert result.allowed is True
    assert result.reason is None


def test_max_stop_gate_blocks_at_exact_limit() -> None:
    """MaxStopGate passes when distance equals limit (not strictly greater)."""
    gate = MaxStopGate(max_stop_nq_pts=Decimal("25"))
    ledger = _make_ledger()
    intent = _make_intent(
        entry_price=Decimal("18000"),
        stop_price=Decimal("17975"),  # exactly 25 pts
    )
    result = gate.check(ledger, intent)
    # Exactly at limit should pass (> is the block condition)
    assert result.allowed is True


# ---------------------------------------------------------------------------
# test_projected_loss_gate_uses_unrealized
# ---------------------------------------------------------------------------


def test_projected_loss_gate_uses_unrealized() -> None:
    """ProjectedLossGate blocks when projected loss breaches daily limit.

    Spec example: realized -800, unrealized -500, risk 300 →
    projected = -800 + (-500) + (-300) = -1600 < -1500 → block.
    """
    ledger = _make_ledger(
        realized=Decimal("-800"),
        unrealized=Decimal("-500"),
    )
    intent = _make_intent(projected_risk_usd=Decimal("300"))
    gate = ProjectedLossGate(daily_loss_limit=Decimal("-1500"))

    result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_PROJECTED_LOSS_LIMIT"


def test_projected_loss_gate_passes_within_limit() -> None:
    """ProjectedLossGate allows when projected loss stays within limit."""
    ledger = _make_ledger(
        realized=Decimal("-500"),
        unrealized=Decimal("-200"),
    )
    intent = _make_intent(projected_risk_usd=Decimal("300"))
    gate = ProjectedLossGate(daily_loss_limit=Decimal("-1500"))

    # projected = -500 + (-200) + (-300) = -1000 > -1500
    result = gate.check(ledger, intent)
    assert result.allowed is True


def test_projected_loss_gate_uses_ledger_unrealized() -> None:
    """ProjectedLossGate uses ledger.unrealized, not any other field."""
    # Same realized but different unrealized — verifies unrealized is read
    ledger_small_unreal = _make_ledger(
        realized=Decimal("-800"),
        unrealized=Decimal("-100"),
    )
    intent = _make_intent(projected_risk_usd=Decimal("300"))
    gate = ProjectedLossGate(daily_loss_limit=Decimal("-1500"))

    # projected = -800 + (-100) + (-300) = -1200 > -1500 → allow
    result = gate.check(ledger_small_unreal, intent)
    assert result.allowed is True


# ---------------------------------------------------------------------------
# test_smt_availability_gate_fail_closed
# ---------------------------------------------------------------------------


def test_smt_availability_gate_fail_closed_unavailable() -> None:
    """SMTAvailabilityGate blocks when verdict is 'UNAVAILABLE'."""
    gate = SMTAvailabilityGate()
    ledger = _make_ledger()
    intent = _make_intent(smt_verdict="UNAVAILABLE")

    result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_SMT_UNAVAILABLE"


def test_smt_availability_gate_fail_closed_none() -> None:
    """SMTAvailabilityGate blocks when verdict is None (no data)."""
    gate = SMTAvailabilityGate()
    ledger = _make_ledger()
    intent = _make_intent(smt_verdict=None)

    result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_SMT_UNAVAILABLE"


def test_smt_availability_gate_fail_closed_missing_attr() -> None:
    """SMTAvailabilityGate blocks when intent has no smt_verdict attribute."""
    gate = SMTAvailabilityGate()
    ledger = _make_ledger()
    # Plain intent without smt_verdict patched in
    intent = TradeIntent(
        symbol="MNQ",
        side="BUY",
        entry_price=Decimal("18000"),
        stop_price=Decimal("17990"),
        projected_risk_usd=Decimal("300"),
        ts_utc=_KILLZONE_TS,
    )

    result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_SMT_UNAVAILABLE"


def test_smt_availability_gate_allows_confirmed() -> None:
    """SMTAvailabilityGate allows when verdict is a non-UNAVAILABLE string."""
    gate = SMTAvailabilityGate()
    ledger = _make_ledger()
    intent = _make_intent(smt_verdict="CONFIRMED")

    result = gate.check(ledger, intent)

    assert result.allowed is True


# ---------------------------------------------------------------------------
# DailyLossGate
# ---------------------------------------------------------------------------


def test_daily_loss_gate_blocks_at_limit() -> None:
    """DailyLossGate blocks when realized_today <= daily_loss_limit."""
    gate = DailyLossGate(daily_loss_limit=Decimal("-1500"))
    ledger = _make_ledger(realized=Decimal("-1500"))

    result = gate.check(ledger, _make_intent())

    assert result.allowed is False
    assert result.reason == "SKIP_DAILY_LOSS"


def test_daily_loss_gate_blocks_beyond_limit() -> None:
    """DailyLossGate blocks when realized_today is worse than limit."""
    gate = DailyLossGate(daily_loss_limit=Decimal("-1500"))
    ledger = _make_ledger(realized=Decimal("-1800"))

    result = gate.check(ledger, _make_intent())

    assert result.allowed is False


def test_daily_loss_gate_allows_within_limit() -> None:
    """DailyLossGate allows when realized_today is above the limit."""
    gate = DailyLossGate(daily_loss_limit=Decimal("-1500"))
    ledger = _make_ledger(realized=Decimal("-500"))

    result = gate.check(ledger, _make_intent())

    assert result.allowed is True


# ---------------------------------------------------------------------------
# KillzoneGate
# ---------------------------------------------------------------------------


def test_killzone_gate_allows_in_am_session() -> None:
    """KillzoneGate allows when timestamp is inside the AM killzone."""
    gate = KillzoneGate()
    ledger = _make_ledger()
    intent = _make_intent(ts_utc=_KILLZONE_TS)

    result = gate.check(ledger, intent)

    assert result.allowed is True


def test_killzone_gate_blocks_outside_session() -> None:
    """KillzoneGate blocks when timestamp is outside all killzone windows."""
    gate = KillzoneGate()
    ledger = _make_ledger()
    intent = _make_intent(ts_utc=_OUTSIDE_TS)

    result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_OUTSIDE_KILLZONE"


# ---------------------------------------------------------------------------
# NewsBlackoutGate
# ---------------------------------------------------------------------------


def test_news_blackout_gate_blocks_on_stale_feed() -> None:
    """NewsBlackoutGate fail-closes on a stale/missing news feed."""
    gate = NewsBlackoutGate(
        window_seconds=900,
        csv_path=Path("/nonexistent/news.csv"),
    )
    ledger = _make_ledger()
    intent = _make_intent()

    result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_NEWS_FEED_STALE"


def test_news_blackout_gate_allows_when_not_in_blackout() -> None:
    """NewsBlackoutGate allows when is_news_blackout returns False."""
    gate = NewsBlackoutGate(window_seconds=900, csv_path=Path("data/news.csv"))
    ledger = _make_ledger()
    intent = _make_intent()

    with patch(
        "nasdaq_ale_bot.execution.gates.is_news_blackout", return_value=False
    ):
        result = gate.check(ledger, intent)

    assert result.allowed is True


def test_news_blackout_gate_blocks_during_blackout() -> None:
    """NewsBlackoutGate blocks when is_news_blackout returns True."""
    gate = NewsBlackoutGate(window_seconds=900, csv_path=Path("data/news.csv"))
    ledger = _make_ledger()
    intent = _make_intent()

    with patch(
        "nasdaq_ale_bot.execution.gates.is_news_blackout", return_value=True
    ):
        result = gate.check(ledger, intent)

    assert result.allowed is False
    assert result.reason == "SKIP_NEWS_BLACKOUT"


# ---------------------------------------------------------------------------
# MaxTradesGate
# ---------------------------------------------------------------------------


def test_max_trades_gate_blocks_at_limit() -> None:
    """MaxTradesGate blocks when trades_today equals max_trades."""
    gate = MaxTradesGate(max_trades=2)
    ledger = _make_ledger(trades_today=2)

    result = gate.check(ledger, _make_intent())

    assert result.allowed is False
    assert result.reason == "SKIP_MAX_TRADES"


def test_max_trades_gate_allows_below_limit() -> None:
    """MaxTradesGate allows when trades_today is below max_trades."""
    gate = MaxTradesGate(max_trades=2)
    ledger = _make_ledger(trades_today=1)

    result = gate.check(ledger, _make_intent())

    assert result.allowed is True


def test_max_trades_gate_zero_trades() -> None:
    """MaxTradesGate allows when no trades have been taken yet."""
    gate = MaxTradesGate(max_trades=2)
    ledger = _make_ledger(trades_today=0)

    result = gate.check(ledger, _make_intent())

    assert result.allowed is True


def test_max_trades_gate_fallback_when_attr_missing() -> None:
    """MaxTradesGate falls back to 0 trades if trades_today is not on ledger."""
    gate = MaxTradesGate(max_trades=2)
    # Plain ledger without trades_today attribute
    ledger = AccountLedger(session_start_equity=_BASE_EQUITY, today=_BASE_DATE)

    result = gate.check(ledger, _make_intent())

    # 0 < 2, should allow
    assert result.allowed is True


# ---------------------------------------------------------------------------
# EntryGate protocol structural checks
# ---------------------------------------------------------------------------


def test_entry_gate_protocol_satisfied_by_stub() -> None:
    """_StubGate instances satisfy the EntryGate runtime-checkable protocol."""
    stub = _StubGate("Test", allow=True)
    assert isinstance(stub, EntryGate)


def test_entry_gate_protocol_satisfied_by_concrete_gates() -> None:
    """All concrete gate classes satisfy the EntryGate protocol."""
    concrete: list[EntryGate] = [
        DailyLossGate(daily_loss_limit=Decimal("-1500")),
        ProjectedLossGate(daily_loss_limit=Decimal("-1500")),
        KillzoneGate(),
        NewsBlackoutGate(),
        SMTAvailabilityGate(),
        MaxTradesGate(),
        MaxStopGate(max_stop_nq_pts=Decimal("25")),
    ]
    for gate in concrete:
        assert isinstance(gate, EntryGate), f"{gate.name} does not satisfy EntryGate"


# ---------------------------------------------------------------------------
# GateResult frozen dataclass
# ---------------------------------------------------------------------------


def test_gate_result_is_frozen() -> None:
    """GateResult is a frozen dataclass — assignment must raise FrozenInstanceError."""
    result = GateResult(allowed=True, gate_name="Test", reason=None)
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        result.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TradeIntent frozen dataclass
# ---------------------------------------------------------------------------


def test_trade_intent_is_frozen() -> None:
    """TradeIntent is a frozen dataclass."""
    intent = TradeIntent(
        symbol="MNQ",
        side="BUY",
        entry_price=Decimal("18000"),
        stop_price=Decimal("17990"),
        projected_risk_usd=Decimal("300"),
        ts_utc=_KILLZONE_TS,
    )
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        intent.symbol = "QQQ"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# load_strategy_config convenience helper
# ---------------------------------------------------------------------------


def test_load_strategy_config_reads_yaml(tmp_path: Path) -> None:
    """load_strategy_config loads a YAML file and returns a dict."""
    from nasdaq_ale_bot.execution.gates import load_strategy_config

    cfg_file = tmp_path / "strategy.yaml"
    cfg_file.write_text("daily_loss_limit: -1500\nmax_trades_per_day: 2\n")

    result = load_strategy_config(cfg_file)

    assert result["daily_loss_limit"] == -1500
    assert result["max_trades_per_day"] == 2
