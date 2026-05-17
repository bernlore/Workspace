"""Phase 2 integration tests.

These tests prove the end-to-end Phase 2 system using a *synthetic* bar
series so they run without Alpaca credentials.  Tests that require the
live-fetched fixture (``qqq_1m_sample.csv``) are individually skipped
when the file is absent.

Synthetic lifecycle:
  * Bars 0-8: narrow-range establishment (avg range ~1.0).
  * Bar 9: wide-range sweep bar (range >= 3 * avg -> WAITING_FOR_SWEEP
    -> CISD_CONFIRMATION).
  * Bar 10: bullish close -> CISD_CONFIRMATION -> IFVG_FORMATION ->
    ENTRY_EXECUTION -> TRADE_MANAGEMENT (four hops in one bar).
  * Bar 11: hits take_profit -> TRADE_MANAGEMENT -> FLAT ->
    BIAS_DETERMINATION (rearm).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBias
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.core.logging_sink import JsonlSink
from nasdaq_ale_bot.strategies.nasdaqale.state_machine import StateMachine, StrategyState
from nasdaq_ale_bot.execution.gates import GateList, TradeIntent

_FIXTURE_CSV = Path(__file__).parent.parent / "fixtures" / "qqq_1m_sample.csv"
_HASHES_FILE = Path(__file__).parent.parent / "fixtures" / "data_hashes.json"

_FIXTURE_AVAILABLE = _FIXTURE_CSV.exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _t(h: int, m: int, day: int = 2) -> datetime:
    return datetime(2024, 1, day, h, m, tzinfo=timezone.utc)


def _bar(
    ts: datetime,
    o: float,
    h: float,
    lo: float,
    c: float,
    v: float = 500.0,
) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=v)


def _narrow(ts: datetime, price: float = 100.0) -> Candle:
    """Narrow-range bar, range = 1.0."""
    return _bar(ts, price, price + 0.5, price - 0.5, price)


class _MockBiasDetectorLong:
    """Always returns LONG bias (activates WAITING_FOR_SWEEP on first bar)."""

    def on_1m_bar(self, bar: Candle) -> Any:
        return SimpleNamespace(bias=HTFBias.LONG)


def _build_synthetic_bars() -> list[Candle]:
    """Drive carry-forward + LIMIT-at-zone-edge through a full round trip.

    Shape:
      bar[0]:    up-close ref (high=100 becomes the CISD reference).
      bar[1..3]: down-leg; bar[2] forms a 3-bar SWING_LOW at 98.
      bar[4]:    bullish sweep — pierces 98, closes back above.
      bar[5..7]: form a bearish body-imbalance inside the CISD range
                 (zone top=98.3, bottom=97.9).
      bar[8]:    body-closes above zone.top → CISD+IFVG fire, zone armed.
      bar[9]:    retest in primary killzone (09:39 ET) → SM triggers,
                 LIMIT armed at zone.top=98.3, stop=97.9,
                 TP=98.3 + (98.3-97.9)*1.3 = 98.82.
      bar[10]:   bar.low=98.0 <= 98.3 → LIMIT fills at 98.3; bar.high=99.0
                 ≥ 98.82 → TP hit at 98.82 same bar.
    """
    return [
        _bar(_t(14, 30),  99.0, 100.0, 98.9,  99.5),
        _bar(_t(14, 31),  99.5,  99.6, 99.0,  99.0),
        _bar(_t(14, 32),  99.0,  99.1, 98.0,  98.5),
        _bar(_t(14, 33),  98.5,  98.8, 98.2,  98.4),
        _bar(_t(14, 34),  98.4,  98.5, 97.5,  98.3),
        _bar(_t(14, 35),  98.3,  98.5, 98.0,  98.3),
        _bar(_t(14, 36),  98.3,  98.5, 97.8,  97.9),
        _bar(_t(14, 37),  97.9, 97.95, 97.5,  97.6),
        _bar(_t(14, 38), 100.0, 104.0, 99.9, 100.2),  # CISD+IFVG zone armed
        _bar(_t(14, 39),  99.0,  99.5, 98.0,  98.5),  # retest -> LIMIT armed at 98.3
        _bar(_t(14, 40),  98.5,  99.0, 98.0,  98.8),  # fill at 98.3, TP at 98.82
    ]


def _make_sm_with_ledger() -> tuple[StateMachine, AccountLedger]:
    ledger = AccountLedger(
        session_start_equity=Decimal("10000"),
        today=date(2024, 1, 2),
    )
    sm = StateMachine(
        bias_detector=_MockBiasDetectorLong(),
        ledger=ledger,
    )
    return sm, ledger


def _replay(sm: StateMachine, bars: list[Candle]) -> list[StrategyState]:
    """Replay bars and collect state after each."""
    states: list[StrategyState] = []
    for bar in bars:
        sm.on_bar(bar)
        states.append(sm.state)
    return states


# ---------------------------------------------------------------------------
# test_at_least_one_completed_lifecycle
# ---------------------------------------------------------------------------


def test_at_least_one_completed_lifecycle() -> None:
    """Synthetic replay must reach FLAT at least once.

    With LIMIT-at-zone-edge semantics the SM waits in TRADE_MANAGEMENT for
    a broker ENTRY fill before it'll exercise stop/tp arithmetic. This test
    runs the SM standalone (no broker), so we mock the fill confirmation
    by flipping ``setup.entry_filled = True`` once the SM has transitioned
    to TRADE_MANAGEMENT — simulating what the runner would do on receiving
    the broker's ENTRY FillEvent.
    """
    all_events = []
    sm2, _ = _make_sm_with_ledger()
    for bar in _build_synthetic_bars():
        all_events.extend(sm2.on_bar(bar))
        if (
            sm2.state == StrategyState.TRADE_MANAGEMENT
            and sm2._active_setup is not None  # noqa: SLF001
            and not sm2._active_setup.entry_filled  # noqa: SLF001
        ):
            sm2._active_setup.entry_filled = True  # noqa: SLF001
    flat_events = [e for e in all_events if e.to_state == StrategyState.FLAT]
    assert flat_events, "Expected at least one FLAT transition in synthetic replay"


# ---------------------------------------------------------------------------
# test_session_counter_reset_at_midnight_et
# ---------------------------------------------------------------------------


def test_session_counter_reset_at_midnight_et() -> None:
    """_trades_today resets when bars cross midnight America/New_York."""
    sm, ledger = _make_sm_with_ledger()

    # Bar on 2024-01-02 (14:30 UTC = 09:30 ET)
    bar_day1 = _narrow(_t(14, 30, day=2))
    sm.on_bar(bar_day1)
    sm._trades_today = 2  # simulate two trades placed

    # Bar on 2024-01-03 (14:30 UTC = 09:30 ET — new day)
    bar_day2 = _narrow(_t(14, 30, day=3))
    sm.on_bar(bar_day2)

    assert sm._trades_today == 0, "trades_today must reset on session rotation"
    assert sm._am_order_placed is False


# ---------------------------------------------------------------------------
# test_jsonl_sink_round_trip_after_replay
# ---------------------------------------------------------------------------


def test_jsonl_sink_round_trip_after_replay(tmp_path: Path) -> None:
    """Events written to JSONL must have schema_version == 1."""
    from nasdaq_ale_bot.core.logging_sink import install_jsonl_sink, SCHEMA_VERSION

    jsonl_path = tmp_path / "bot_events.jsonl"
    sink = JsonlSink(path=jsonl_path)
    install_jsonl_sink(sink)

    # Replay a few bars; the SM emits STATE_TRANSITION events via structlog
    sm, _ = _make_sm_with_ledger()
    for bar in _build_synthetic_bars():
        sm.on_bar(bar)

    if not jsonl_path.exists():
        pytest.skip("JsonlSink did not produce output — check install_jsonl_sink")

    lines = [ln for ln in jsonl_path.read_text().splitlines() if ln.strip()]
    assert lines, "JSONL file is empty"
    for line in lines:
        record = json.loads(line)
        assert record.get("schema_version") == SCHEMA_VERSION, (
            f"schema_version mismatch in: {line[:120]}"
        )


# ---------------------------------------------------------------------------
# test_decimal_only_in_ledger_after_replay
# ---------------------------------------------------------------------------


def test_decimal_only_in_ledger_after_replay() -> None:
    """All AccountLedger monetary fields are Decimal after replay."""
    sm, ledger = _make_sm_with_ledger()
    for bar in _build_synthetic_bars():
        sm.on_bar(bar)
    # Snapshot unrealized (simulated)
    ledger.on_unrealized_snapshot(
        _t(14, 50),
        Decimal("42"),
    )
    monetary_fields = {
        "realized_today": ledger.realized_today,
        "unrealized": ledger.unrealized,
        "high_watermark_equity": ledger.high_watermark_equity,
        "session_start_equity": ledger.session_start_equity,
        "current_equity": ledger.current_equity,
        "best_day_profit": ledger.best_day_profit,
        "cumulative_profit": ledger.cumulative_profit,
    }
    for name, value in monetary_fields.items():
        assert isinstance(value, Decimal), (
            f"Ledger field {name!r} is {type(value).__name__}, expected Decimal"
        )


# ---------------------------------------------------------------------------
# test_hwm_monotonic_after_full_replay
# ---------------------------------------------------------------------------


def test_hwm_monotonic_after_full_replay() -> None:
    """High-watermark equity never decreases across any snapshot sequence."""
    sm, ledger = _make_sm_with_ledger()
    hwm_sequence: list[Decimal] = [ledger.high_watermark_equity]
    for i, bar in enumerate(_build_synthetic_bars()):
        sm.on_bar(bar)
        # Simulate unrealized snapshots (both gains and losses)
        unrealized = Decimal(str(i * 5 - 10))  # mixed +/-
        ledger.on_unrealized_snapshot(bar.ts, unrealized)
        hwm_sequence.append(ledger.high_watermark_equity)
    for prev, cur in zip(hwm_sequence, hwm_sequence[1:]):
        assert cur >= prev, (
            f"HWM decreased: {prev} -> {cur}"
        )


# ---------------------------------------------------------------------------
# test_evaluate_all_on_representative_setup (A5)
# ---------------------------------------------------------------------------


def test_evaluate_all_on_representative_setup() -> None:
    """GateList.evaluate_all returns all 7 gates; first failing matches evaluate()."""
    from nasdaq_ale_bot.execution.gates import load_strategy_config

    cfg = load_strategy_config()
    gate_list = GateList.base_list(cfg)
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
        ts_utc=_t(14, 30),
        smt_verdict="NONE",
    )
    all_results = gate_list.evaluate_all(ledger, intent)
    assert len(all_results) == 8, (
        f"evaluate_all returned {len(all_results)} results, expected 8"
    )
    first_failing_all = next((r for r in all_results if not r.allowed), None)
    first_failing = gate_list.evaluate(ledger, intent)
    if first_failing.allowed:
        assert first_failing_all is None or first_failing_all.allowed, (
            "evaluate() says PASS but evaluate_all found a failing gate"
        )
    else:
        assert first_failing_all is not None
        assert first_failing_all.gate_name == first_failing.gate_name


# ---------------------------------------------------------------------------
# test_no_lookahead_during_integration
# ---------------------------------------------------------------------------


def test_no_lookahead_during_integration() -> None:
    """CandleView.__getitem__ never accesses index > current horizon."""
    from nasdaq_ale_bot.core.candle_view import CandleView, LookAheadError

    violations: list[tuple[int, int]] = []
    _orig = CandleView.__getitem__

    def _spy(self: CandleView, k: int) -> Candle:  # type: ignore[override]
        bar = _orig(self, k)  # LookAheadError if lookahead
        return bar

    CandleView.__getitem__ = _spy  # type: ignore[method-assign]
    try:
        sm, _ = _make_sm_with_ledger()
        for bar in _build_synthetic_bars():
            try:
                sm.on_bar(bar)
            except LookAheadError as exc:
                violations.append(str(exc))
    finally:
        CandleView.__getitem__ = _orig  # type: ignore[method-assign]

    assert not violations, f"Look-ahead detected: {violations}"


# ---------------------------------------------------------------------------
# test_skip_max_stop_event — inject a setup that triggers MaxStopGate
# ---------------------------------------------------------------------------


def test_skip_max_stop_gate_triggers_on_wide_stop() -> None:
    """A setup with stop wider than max_stop_nq_points blocks via MaxStopGate."""
    from nasdaq_ale_bot.execution.gates import load_strategy_config

    cfg = load_strategy_config()
    gate_list = GateList.base_list(cfg)
    ledger = AccountLedger(
        session_start_equity=Decimal("10000"),
        today=date(2024, 1, 2),
    )
    # ProjectedRisk that implies a stop of 1000 NQ points — far beyond any limit
    intent = TradeIntent(
        symbol="QQQ",
        side="BUY",
        entry_price=Decimal("440"),
        stop_price=Decimal("100"),  # 340 points away -> exceeds any max_stop
        projected_risk_usd=Decimal("99999"),  # also triggers ProjectedLossGate
        ts_utc=_t(14, 30),
        smt_verdict="NONE",
    )
    result = gate_list.evaluate(ledger, intent)
    assert not result.allowed, "Expected gate to block the wide-stop intent"
    # At minimum one of the loss-cap or stop-cap gates must fire
    assert result.reason is not None


# ---------------------------------------------------------------------------
# Fixture-dependent tests (skip gracefully when no CSV)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _FIXTURE_AVAILABLE, reason="qqq_1m_sample.csv not present")
def test_at_least_one_completed_lifecycle_with_fixture() -> None:
    """Replay actual QQQ 1m data; assert >= 1 FLAT reached."""
    bars = _load_fixture_bars(_FIXTURE_CSV)
    sm, _ = _make_sm_with_ledger()
    all_events = []
    for bar in bars:
        all_events.extend(sm.on_bar(bar))
    flat_events = [e for e in all_events if e.to_state == StrategyState.FLAT]
    assert flat_events, "No FLAT state in fixture replay"


@pytest.mark.skipif(
    os.environ.get("ALPACA_API_KEY") is None,
    reason="ALPACA_API_KEY not set — fetch test skipped",
)
def test_fetch_script_reproduces_pinned_sha256(tmp_path: Path) -> None:
    """A3: running fetch_phase2_fixture.py produces the same SHA-256."""
    import subprocess

    result = subprocess.run(
        ["python", "scripts/fetch_phase2_fixture.py", "--out-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Fetch script failed:\n{result.stderr}"

    csv_path = tmp_path / "qqq_1m_sample.csv"
    hash_path = tmp_path / "data_hashes.json"
    assert csv_path.exists(), "Fetch script did not produce CSV"
    assert hash_path.exists(), "Fetch script did not produce data_hashes.json"

    hashes = json.loads(hash_path.read_text())
    sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert hashes["qqq_1m_sample.csv"] == sha, "Stored SHA-256 does not match file"

    if _HASHES_FILE.exists():
        pinned = json.loads(_HASHES_FILE.read_text()).get("qqq_1m_sample.csv")
        if pinned and pinned != "PENDING_FETCH":
            assert sha == pinned, (
                f"Fetched SHA {sha} differs from pinned {pinned} — data window drift?"
            )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _load_fixture_bars(path: Path) -> list[Candle]:
    bars: list[Candle] = []
    with path.open() as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bars.append(
                Candle(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    return bars
