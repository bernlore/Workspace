"""Unit tests for core.state_machine — transitions + event emission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog.testing

from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.core.state_machine import (
    StateMachine,
    StrategyState,
)


def _bar(ts: datetime, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=low, close=c, volume=100.0)


def _make_sm(**kwargs) -> StateMachine:
    return StateMachine(
        strategy_cfg={"rr": {"target": 1.2, "floor": 1.1, "cap": 1.3}},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# test_one_event_per_transition
# ---------------------------------------------------------------------------


def test_state_transitions_emit_one_event_each() -> None:
    sm = _make_sm()

    # Override handlers to force BIAS -> SWEEP -> CISD in 3 bars
    from nasdaq_ale_bot.core.state_machine import (
        _handle_bias_determination,  # noqa: F401
    )

    calls = {"bias": 0, "sweep": 0}

    def bias(sm, view):
        calls["bias"] += 1
        return (StrategyState.WAITING_FOR_SWEEP, "bias_LONG")

    def sweep(sm, view):
        calls["sweep"] += 1
        return (StrategyState.CISD_CONFIRMATION, "sweep_detected")

    def noop(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = bias
    sm._handlers[StrategyState.WAITING_FOR_SWEEP] = sweep
    sm._handlers[StrategyState.CISD_CONFIRMATION] = noop

    with structlog.testing.capture_logs() as cap:
        events = sm.on_bar(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5))

    transitions = [e for e in cap if e.get("event") == "STATE_TRANSITION"]
    assert len(events) == len(transitions), (
        f"returned {len(events)} events, emitted {len(transitions)} logs"
    )


# ---------------------------------------------------------------------------
# test_full_lifecycle_synthetic  (AC 2)
# ---------------------------------------------------------------------------


def test_full_lifecycle_synthetic() -> None:
    """A 7-step forced lifecycle walks through every StrategyState."""
    sm = _make_sm()

    # Force a scripted progression via handler injection
    script = [
        (StrategyState.BIAS_DETERMINATION, StrategyState.WAITING_FOR_SWEEP, "bias"),
        (StrategyState.WAITING_FOR_SWEEP, StrategyState.CISD_CONFIRMATION, "sweep"),
        (StrategyState.CISD_CONFIRMATION, StrategyState.IFVG_FORMATION, "cisd"),
        (StrategyState.IFVG_FORMATION, StrategyState.ENTRY_EXECUTION, "ifvg"),
        (StrategyState.ENTRY_EXECUTION, StrategyState.TRADE_MANAGEMENT, "entry"),
        (StrategyState.TRADE_MANAGEMENT, StrategyState.FLAT, "exit"),
        (StrategyState.FLAT, StrategyState.BIAS_DETERMINATION, "rearm"),
    ]

    idx = {"i": 0}

    def make_handler(state):
        def h(sm, view):
            if idx["i"] < len(script):
                cur, nxt, reason = script[idx["i"]]
                if cur == state:
                    idx["i"] += 1
                    return (nxt, reason)
            return (state, "noop")
        return h

    for state in StrategyState:
        sm._handlers[state] = make_handler(state)

    ts = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    observed: list[tuple[str, str]] = []
    for i in range(10):
        events = sm.on_bar(_bar(ts + timedelta(minutes=i), 100, 101, 99, 100.5))
        for ev in events:
            observed.append((str(ev.from_state), str(ev.to_state)))

    # First 7 transitions should match the script
    expected = [(cur, nxt) for cur, nxt, _ in script]
    assert observed[: len(expected)] == [
        (str(a), str(b)) for a, b in expected
    ], f"observed={observed}"


# ---------------------------------------------------------------------------
# Threading contract (A4)
# ---------------------------------------------------------------------------


def test_threading_contract_documented() -> None:
    import inspect

    from nasdaq_ale_bot.core import state_machine as sm_mod

    doc = inspect.getdoc(sm_mod) or ""
    assert "single-threaded" in doc.lower()
    assert "A21" in doc


def test_cross_thread_call_raises() -> None:
    """A4 — on_bar raises ThreadingContractViolation from a foreign thread."""
    import threading

    from nasdaq_ale_bot.core.state_machine import ThreadingContractViolation

    sm = _make_sm()

    bar = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    raised: dict[str, Exception | None] = {"exc": None}

    def worker():
        try:
            sm.on_bar(bar)
        except BaseException as exc:  # noqa: BLE001 - test wants the raise
            raised["exc"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert isinstance(raised["exc"], ThreadingContractViolation)
    # State unchanged
    assert sm.state == StrategyState.BIAS_DETERMINATION


# ---------------------------------------------------------------------------
# Re-entry dedup (R6)
# ---------------------------------------------------------------------------


def test_re_entry_during_replay_safe() -> None:
    """Feeding the same bar twice produces zero events on the second call."""
    sm = _make_sm()

    def to_sweep(sm, view):
        return (StrategyState.WAITING_FOR_SWEEP, "bias_LONG")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = to_sweep
    sm._handlers[StrategyState.WAITING_FOR_SWEEP] = lambda sm, v: (sm.state, "hold")

    bar = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    ev1 = sm.on_bar(bar)
    assert len(ev1) == 1

    ev2 = sm.on_bar(bar)
    assert ev2 == []


def test_earlier_bar_dropped_during_replay() -> None:
    sm = _make_sm()
    bar1 = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    bar0 = _bar(datetime(2024, 1, 15, 14, 29, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    sm.on_bar(bar1)
    out = sm.on_bar(bar0)
    assert out == []


# ---------------------------------------------------------------------------
# Session rotation (AC 7)
# ---------------------------------------------------------------------------


def test_session_rotation_resets_counters() -> None:
    """Crossing 00:00 ET resets _trades_today and _am_order_placed."""

    class FakeLedger:
        def __init__(self):
            self.rotated_to: list = []

        def on_session_rotation(self, d, equity=None):
            self.rotated_to.append(d)

    ledger = FakeLedger()
    sm = _make_sm(ledger=ledger)

    # Prime day-1 state
    sm._trades_today = 2
    sm._am_order_placed = True

    # First bar on day 1 (Monday 09:30 ET = 14:30 UTC)
    bar1 = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    sm.on_bar(bar1)

    # Reset counters after first bar establishes session_date
    sm._trades_today = 2
    sm._am_order_placed = True

    # Next bar on Tuesday 14:30 UTC (next ET day)
    bar2 = _bar(datetime(2024, 1, 16, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    sm.on_bar(bar2)

    assert sm._trades_today == 0
    assert sm._am_order_placed is False
    assert ledger.rotated_to, "ledger.on_session_rotation was not invoked"


def test_session_rotation_idempotent_within_day() -> None:
    """Two bars on the same ET date do NOT rotate."""

    class FakeLedger:
        def __init__(self):
            self.rotated: int = 0

        def on_session_rotation(self, d, equity=None):
            self.rotated += 1

    ledger = FakeLedger()
    sm = _make_sm(ledger=ledger)

    ts = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    sm.on_bar(_bar(ts, 100, 101, 99, 100.5))
    sm.on_bar(_bar(ts + timedelta(minutes=1), 100, 101, 99, 100.5))

    # First bar triggers a rotate (None -> date); second must not re-trigger
    assert ledger.rotated == 1


def test_session_rotation_tolerates_ledger_error() -> None:
    """A ledger exception during rotation is swallowed (logged), not raised."""

    class BrokenLedger:
        def on_session_rotation(self, d, equity=None):
            raise RuntimeError("boom")

    sm = _make_sm(ledger=BrokenLedger())
    bar = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    # Must not raise
    sm.on_bar(bar)
    assert sm._session_date is not None


# ---------------------------------------------------------------------------
# Transitions emit STATE_TRANSITION events carrying the mandatory fields
# ---------------------------------------------------------------------------


def test_transition_event_fields_complete() -> None:
    sm = _make_sm()

    def bias(sm, view):
        return (StrategyState.WAITING_FOR_SWEEP, "bias_LONG")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = bias
    sm._handlers[StrategyState.WAITING_FOR_SWEEP] = lambda sm, v: (sm.state, "nz")

    ts = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    with structlog.testing.capture_logs() as cap:
        sm.on_bar(_bar(ts, 100, 101, 99, 100.5))

    transitions = [e for e in cap if e.get("event") == "STATE_TRANSITION"]
    assert len(transitions) == 1
    ev = transitions[0]
    assert ev["from_state"] == "BIAS_DETERMINATION"
    assert ev["to_state"] == "WAITING_FOR_SWEEP"
    assert ev["reason"] == "bias_LONG"
    assert ev["bar_ts"] == ts.isoformat()


# ---------------------------------------------------------------------------
# FLAT -> BIAS rearm default handler
# ---------------------------------------------------------------------------


def test_flat_handler_rearms_to_bias_determination() -> None:
    sm = _make_sm()
    sm.state = StrategyState.FLAT
    sm._session_date = datetime(2024, 1, 15, tzinfo=timezone.utc).astimezone().date()
    # Already in session — avoid day-rotation re-entry by matching bar date
    bar = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    events = sm.on_bar(bar)
    assert events, "FLAT state should transition back to BIAS_DETERMINATION"
    assert any(e.to_state == StrategyState.BIAS_DETERMINATION for e in events)


# ---------------------------------------------------------------------------
# Default handlers basic sanity
# ---------------------------------------------------------------------------


def test_default_bias_handler_without_detector_stays() -> None:
    sm = _make_sm()  # no bias_detector
    bar = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    events = sm.on_bar(bar)
    assert events == []
    assert sm.state == StrategyState.BIAS_DETERMINATION


def test_default_bias_detector_long_advances() -> None:
    class FakeBias:
        def on_1m_bar(self, bar):
            return type("S", (), {"bias": "LONG"})()

    sm = _make_sm(bias_detector=FakeBias())
    bar = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    events = sm.on_bar(bar)
    assert any(e.to_state == StrategyState.WAITING_FOR_SWEEP for e in events)


def test_default_bias_detector_error_stays() -> None:
    class BrokenBias:
        def on_1m_bar(self, bar):
            raise RuntimeError("boom")

    sm = _make_sm(bias_detector=BrokenBias())
    bar = _bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5)
    events = sm.on_bar(bar)
    assert events == []


# ---------------------------------------------------------------------------
# Direct handler exercises (coverage for scaffold logic)
# ---------------------------------------------------------------------------


def _drive(sm: StateMachine, bars: list[Candle]) -> list:
    out = []
    for b in bars:
        out.extend(sm.on_bar(b))
    return out


def test_default_waiting_for_sweep_advances_on_liquidity_sweep() -> None:
    class FakeBias:
        def on_1m_bar(self, bar):
            return type("S", (), {"bias": "LONG"})()

    sm = _make_sm(bias_detector=FakeBias())
    base = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    # Build a 3-bar swing-low fractal at bar[1] (low=99), unmitigated through bar[3],
    # then bar[4] pierces below 99 and body-closes back above → bullish sweep (UP).
    bars = [
        _bar(base + timedelta(minutes=0), 100.5, 101.0, 100.0, 100.5),
        _bar(base + timedelta(minutes=1),  99.5, 100.0,  99.0,  99.5),
        _bar(base + timedelta(minutes=2), 100.5, 101.0, 100.0, 100.5),
        _bar(base + timedelta(minutes=3), 100.0, 100.5,  99.5,  99.8),
        _bar(base + timedelta(minutes=4),  99.2,  99.3,  98.5,  99.2),
    ]
    events = _drive(sm, bars)
    to_states = [str(e.to_state) for e in events]
    assert "WAITING_FOR_SWEEP" in to_states
    assert "CISD_CONFIRMATION" in to_states


def test_default_cisd_confirms_on_bullish_close() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_cisd_confirmation

    sm = _make_sm()
    # bar[0] is the up-close reference (close>open, high=100). bar[1] is the sweep bar.
    # bar[2] body-closes above reference.high=100 → CISD confirmed.
    base = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    sm._bars.extend([
        _bar(base + timedelta(minutes=0), 99.0, 100.0, 98.5,  99.5),
        _bar(base + timedelta(minutes=1), 99.4,  99.6, 98.0,  99.0),
        _bar(base + timedelta(minutes=2), 99.5, 101.0, 99.5, 100.5),
    ])
    sm._active_setup = Setup(bias="LONG", sweep_idx=1)
    view = CandleViewOn(sm._bars, 2)
    new_state, reason = _handle_cisd_confirmation(sm, view)
    assert new_state == StrategyState.IFVG_FORMATION
    assert reason == "cisd_bullish"
    assert sm._active_setup.cisd_confirm_idx == 2


def test_default_cisd_confirms_on_bearish_close() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_cisd_confirmation

    sm = _make_sm()
    base = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    # bar[0] is the down-close reference (close<open, low=99). bar[2] closes below 99.
    sm._bars.extend([
        _bar(base + timedelta(minutes=0), 101.0, 101.5,  99.0, 100.0),
        _bar(base + timedelta(minutes=1), 100.5, 102.0, 100.0, 101.0),
        _bar(base + timedelta(minutes=2), 100.5, 101.0,  98.0,  98.5),
    ])
    sm._active_setup = Setup(bias="SHORT", sweep_idx=1)
    view = CandleViewOn(sm._bars, 2)
    new_state, reason = _handle_cisd_confirmation(sm, view)
    assert new_state == StrategyState.IFVG_FORMATION
    assert reason == "cisd_bearish"


def test_default_cisd_awaits_if_no_match() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_cisd_confirmation

    sm = _make_sm()
    sm.state = StrategyState.CISD_CONFIRMATION
    base = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    # bar[1] is the sweep bar; bar[2] does not break reference.high → awaiting.
    sm._bars.extend([
        _bar(base + timedelta(minutes=0), 99.0, 100.0, 98.5, 99.5),
        _bar(base + timedelta(minutes=1), 99.2,  99.5, 98.0, 99.2),
        _bar(base + timedelta(minutes=2), 99.3,  99.8, 99.0, 99.5),
    ])
    sm._active_setup = Setup(bias="LONG", sweep_idx=1)
    view = CandleViewOn(sm._bars, 2)
    new_state, reason = _handle_cisd_confirmation(sm, view)
    assert new_state == StrategyState.CISD_CONFIRMATION
    assert reason == "awaiting_cisd"


def test_default_cisd_no_setup_returns_to_bias() -> None:
    from nasdaq_ale_bot.core.state_machine import _handle_cisd_confirmation

    sm = _make_sm()
    sm._active_setup = None
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, _ = _handle_cisd_confirmation(sm, view)
    assert new_state == StrategyState.BIAS_DETERMINATION


def test_default_ifvg_formation_computes_long_tp() -> None:
    """Carry-forward IFVG: 1st handler call detects+stores zone (no transition);
    2nd call on a retest bar inside killzone transitions to ENTRY_EXECUTION."""
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_ifvg_formation

    sm = _make_sm()
    # 2024-01-16 is a Tuesday (NYSE trading day); 14:30 UTC = 09:30 ET → primary killzone.
    base = datetime(2024, 1, 16, 14, 30, tzinfo=timezone.utc)
    sm._bars.extend([
        _bar(base + timedelta(minutes=0), 100.0, 100.5, 99.9, 100.3),
        _bar(base + timedelta(minutes=1),  99.3,  99.4, 98.5,  98.7),
        _bar(base + timedelta(minutes=2),  97.5,  97.8, 97.0,  97.3),
        _bar(base + timedelta(minutes=3),  97.5, 100.2, 97.4, 100.1),  # formation
        _bar(base + timedelta(minutes=4), 100.1, 100.2, 99.5, 100.05),  # retest: low<=zone.top=100
    ])
    sm._active_setup = Setup(bias="LONG", sweep_idx=0, cisd_confirm_idx=3)

    # 1st call: handler detects zone, registers it in book, releases SM to BIAS.
    view = CandleViewOn(sm._bars, 3)
    state1, reason1 = _handle_ifvg_formation(sm, view)
    assert state1 == StrategyState.BIAS_DETERMINATION
    assert reason1 == "ifvg_zone_armed"
    assert sm._active_setup is None  # released
    assert len(sm._zone_book) == 1
    z = sm._zone_book[0]
    assert z.bias == "LONG"
    assert z.ifvg_zone_top == 100.0
    assert z.ifvg_zone_bottom == 97.5

    # On the retest bar the zone monitor (called from on_bar) triggers ENTRY.
    events: list = []
    sm._monitor_zone_book(sm._bars[-1], events)
    assert len(events) == 1
    assert events[0].to_state == StrategyState.ENTRY_EXECUTION
    assert events[0].reason == "ifvg_ready"
    assert sm._active_setup is not None
    # Entry sits AT the zone edge (LONG: top=100.0). Broker places a LIMIT
    # there with carry-forward; fills only when price wicks back to the level.
    assert sm._active_setup.entry_price == 100.0
    assert sm._active_setup.stop_price == 97.5
    assert sm._active_setup.entry_filled is False  # waiting for LIMIT fill
    assert sm._active_setup.take_profit is not None
    assert sm._active_setup.take_profit > sm._active_setup.entry_price
    assert sm._zone_book == []  # consumed


def test_default_ifvg_formation_computes_short_tp() -> None:
    """Mirror of the LONG carry-forward test."""
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_ifvg_formation

    sm = _make_sm()
    base = datetime(2024, 1, 16, 14, 30, tzinfo=timezone.utc)
    sm._bars.extend([
        _bar(base + timedelta(minutes=0), 97.5, 98.0, 97.2, 97.8),
        _bar(base + timedelta(minutes=1), 98.7, 99.0, 98.5, 98.9),
        _bar(base + timedelta(minutes=2), 99.8, 100.2, 99.5, 100.0),
        _bar(base + timedelta(minutes=3), 99.5, 100.0, 97.3, 97.5),  # formation
        _bar(base + timedelta(minutes=4), 97.6, 98.5, 97.5, 97.7),   # retest: high>=zone.bottom=97.8
    ])
    sm._active_setup = Setup(bias="SHORT", sweep_idx=0, cisd_confirm_idx=3)

    view = CandleViewOn(sm._bars, 3)
    state1, reason1 = _handle_ifvg_formation(sm, view)
    assert state1 == StrategyState.BIAS_DETERMINATION
    assert reason1 == "ifvg_zone_armed"
    assert len(sm._zone_book) == 1
    z = sm._zone_book[0]
    assert z.bias == "SHORT"
    assert z.ifvg_zone_top == 99.8
    assert z.ifvg_zone_bottom == 97.8

    events: list = []
    sm._monitor_zone_book(sm._bars[-1], events)
    assert len(events) == 1
    assert events[0].to_state == StrategyState.ENTRY_EXECUTION
    assert sm._active_setup is not None
    # SHORT entry sits AT the zone edge (bottom=97.8); stop above zone.top.
    assert sm._active_setup.entry_price == 97.8
    assert sm._active_setup.stop_price == 99.8
    assert sm._active_setup.entry_filled is False
    assert sm._active_setup.take_profit is not None
    assert sm._active_setup.take_profit < sm._active_setup.entry_price


def test_default_ifvg_no_setup_returns_to_bias() -> None:
    from nasdaq_ale_bot.core.state_machine import _handle_ifvg_formation

    sm = _make_sm()
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, _ = _handle_ifvg_formation(sm, view)
    assert new_state == StrategyState.BIAS_DETERMINATION


def test_default_entry_execution_increments_trades() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_entry_execution

    # 2024-01-16 is a Tuesday (regular trading day); 14:30 UTC = 09:30 ET
    # places the bar inside the primary killzone so the gate-wired handler
    # progresses to TRADE_MANAGEMENT instead of rejecting with outside_killzone.
    sm = _make_sm(gate_list=object())
    sm._active_setup = Setup(bias="LONG", entry_price=100, stop_price=99, take_profit=101.2)
    sm._bars.append(_bar(datetime(2024, 1, 16, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, _ = _handle_entry_execution(sm, view)
    assert new_state == StrategyState.TRADE_MANAGEMENT
    assert sm._trades_today == 1
    assert sm._active_setup.entry_bar_ts is not None


def test_default_entry_execution_no_setup_returns_to_bias() -> None:
    from nasdaq_ale_bot.core.state_machine import _handle_entry_execution

    sm = _make_sm()
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, _ = _handle_entry_execution(sm, view)
    assert new_state == StrategyState.BIAS_DETERMINATION


def test_default_trade_management_long_stop_out() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_trade_management

    sm = _make_sm()
    sm._active_setup = Setup(
        bias="LONG", entry_price=100, stop_price=99, take_profit=101,
        entry_filled=True,
    )
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 100, 98, 98.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, reason = _handle_trade_management(sm, view)
    assert new_state == StrategyState.FLAT
    assert reason == "stop_out"


def test_default_trade_management_long_target_hit() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_trade_management

    sm = _make_sm()
    sm._active_setup = Setup(
        bias="LONG", entry_price=100, stop_price=99, take_profit=101,
        entry_filled=True,
    )
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 102, 100, 101.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, reason = _handle_trade_management(sm, view)
    assert new_state == StrategyState.FLAT
    assert reason == "target_hit"


def test_default_trade_management_short_stop_out() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_trade_management

    sm = _make_sm()
    sm._active_setup = Setup(
        bias="SHORT", entry_price=100, stop_price=101, take_profit=99,
        entry_filled=True,
    )
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 102, 100, 101.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, reason = _handle_trade_management(sm, view)
    assert new_state == StrategyState.FLAT
    assert reason == "stop_out"


def test_default_trade_management_short_target_hit() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_trade_management

    sm = _make_sm()
    sm._active_setup = Setup(
        bias="SHORT", entry_price=100, stop_price=101, take_profit=99,
        entry_filled=True,
    )
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 100, 98, 98.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, reason = _handle_trade_management(sm, view)
    assert new_state == StrategyState.FLAT
    assert reason == "target_hit"


def test_default_trade_management_holds_in_between() -> None:
    from nasdaq_ale_bot.core.state_machine import Setup, _handle_trade_management

    sm = _make_sm()
    sm.state = StrategyState.TRADE_MANAGEMENT
    sm._active_setup = Setup(
        bias="LONG", entry_price=100, stop_price=99, take_profit=101,
        entry_filled=True,
    )
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 100.5, 99.5, 100.2))
    view = CandleViewOn(sm._bars, 0)
    new_state, reason = _handle_trade_management(sm, view)
    assert new_state == StrategyState.TRADE_MANAGEMENT
    assert reason == "managing"


def test_default_trade_management_no_setup_flat() -> None:
    from nasdaq_ale_bot.core.state_machine import _handle_trade_management

    sm = _make_sm()
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, _ = _handle_trade_management(sm, view)
    assert new_state == StrategyState.FLAT


def test_waiting_for_sweep_awaits_when_no_setup() -> None:
    from nasdaq_ale_bot.core.state_machine import _handle_waiting_for_sweep

    sm = _make_sm()
    sm._bars.append(_bar(datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100.5))
    view = CandleViewOn(sm._bars, 0)
    new_state, _ = _handle_waiting_for_sweep(sm, view)
    assert new_state == StrategyState.BIAS_DETERMINATION


# Import alias for the lightweight CandleView constructor
from nasdaq_ale_bot.core.candle_view import CandleView as CandleViewOn  # noqa: E402
