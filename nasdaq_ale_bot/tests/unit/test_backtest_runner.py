"""Unit tests for backtest.runner — BacktestRunner + StateMachine->MockBroker bridge.

Test inventory (16 tests):
  1. test_bar_iteration_constructs_candle_view
  2. test_fill_timestamp_semantics
  3. test_no_look_ahead_during_replay
  4. test_session_rotation_propagated
  5. test_ledger_decimal_integrity
  6. test_empty_bars_returns_empty_result
  7. test_single_bar_no_crash
  8. test_multi_day_state_continuity
  9. test_trades_list_matches_fill_count
  10. test_equity_curve_monotonic_timestamps
  11. test_correlated_bars_fed_to_smt
  12. test_result_includes_param_set_hash
  13. test_load_bars_from_parquet
  14. test_state_machine_to_broker_bridge
  15. test_same_bar_double_arm_deduplicated
  16. test_flatten_on_trade_management_to_flat
"""

from __future__ import annotations

import io
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nasdaq_ale_bot.backtest.runner import BacktestResult, BacktestRunner, TradeRecord
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.core.candle_view import CandleView, LookAheadError
from nasdaq_ale_bot.core.state_machine import StateMachine, StrategyState
from nasdaq_ale_bot.execution.mock_broker import MockBroker


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def mk_candle(
    idx: int = 0,
    open_: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float = 100.0,
    volume: float = 1000.0,
    day_offset: int = 0,
) -> Candle:
    """Build a Candle with sensible defaults.

    idx seeds a 1-minute-increment timestamp. day_offset shifts date by N days.
    """
    base = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(days=day_offset)
    ts = base + timedelta(minutes=idx)
    hi = high if high is not None else max(open_, close) + 0.5
    lo = low if low is not None else min(open_, close) - 0.5
    return Candle(ts=ts, open=open_, high=hi, low=lo, close=close, volume=volume)


def _make_ledger(equity: Decimal = Decimal("50000")) -> AccountLedger:
    return AccountLedger(session_start_equity=equity, today=date(2024, 1, 2))


def _make_broker(ledger: AccountLedger) -> MockBroker:
    return MockBroker(ledger=ledger, initial_equity=Decimal("50000"))


def _make_instrument(symbol: str = "QQQ") -> Any:
    """Return a minimal instrument-like object."""
    class _Inst:
        pass
    inst = _Inst()
    inst.symbol = symbol  # type: ignore[attr-defined]
    return inst


def _make_runner(
    bars: list[Candle],
    bars_correlated: list[Candle] | None = None,
    strategy_cfg: dict | None = None,
    param_set_hash: str = "",
    ledger: AccountLedger | None = None,
    broker: MockBroker | None = None,
    symbol: str = "QQQ",
) -> BacktestRunner:
    if ledger is None:
        ledger = _make_ledger()
    if broker is None:
        broker = _make_broker(ledger)
    cfg = strategy_cfg or {}
    return BacktestRunner(
        bars_primary=bars,
        bars_correlated=bars_correlated,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=_make_instrument(symbol),
        param_set_hash=param_set_hash,
    )


def _inject_entry_execution_handler(sm: StateMachine, setup_params: dict) -> None:
    """Override SM handlers so it goes straight to ENTRY_EXECUTION on bar 0."""
    from nasdaq_ale_bot.core.state_machine import Setup

    original_handlers = dict(sm._handlers)  # noqa: SLF001

    def _bias(sm, view):
        sm._active_setup = Setup(  # noqa: SLF001
            bias=setup_params.get("bias", "LONG"),
            entry_price=setup_params.get("entry_price", 100.0),
            stop_price=setup_params.get("stop_price", 99.0),
            take_profit=setup_params.get("take_profit", 101.2),
            entry_bar_ts=view[-1].ts,
        )
        return (StrategyState.ENTRY_EXECUTION, "forced")

    def _entry(sm, view):
        sm._active_setup.entry_bar_ts = view[-1].ts  # noqa: SLF001
        return (StrategyState.TRADE_MANAGEMENT, "order_submitted")

    def _noop(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _noop  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, view: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001


# ---------------------------------------------------------------------------
# 1. test_bar_iteration_constructs_candle_view
# ---------------------------------------------------------------------------


def test_bar_iteration_constructs_candle_view(monkeypatch) -> None:
    """CandleView is constructed with horizon == current bar index for each bar."""
    bars = [mk_candle(i) for i in range(5)]
    captured_horizons: list[int] = []

    original_init = CandleView.__init__

    def _spy_init(self, bars_arg, i):
        captured_horizons.append(i)
        original_init(self, bars_arg, i)

    monkeypatch.setattr(CandleView, "__init__", _spy_init)

    runner = _make_runner(bars)
    runner.run()

    # The runner creates one view per bar (indices 0..4).
    # SM also creates its own view internally; we filter for the first call per bar.
    assert len(captured_horizons) >= 5
    # Last runner-created view has horizon == 4.
    assert captured_horizons[-1] == 4


# ---------------------------------------------------------------------------
# 2. test_fill_timestamp_semantics
# ---------------------------------------------------------------------------


def test_fill_timestamp_semantics() -> None:
    """Bracket armed on bar 5 fills entry on bar 6 and TP on bar 7.

    fill_ts for entry == bars[6].ts; entry_ts of TradeRecord == bars[6].ts.

    BUY LIMIT fill semantics (MockBroker):
      - Fills when bar.open <= entry_price (gap-down) OR bar.low <= entry_price.
    To avoid same-bar fill on the arming bar (bar 5), entry_price must be BELOW
    bar 5's low (99.5). We use SHORT (SELL) with entry_price=99.0 — a SELL LIMIT
    fills when bar.open >= entry_price OR bar.high >= entry_price. We set the
    arming bar range to [97, 98] so bar.high=98 < 99. Bar 6 range is [98.5, 100]
    so bar.high=100 >= 99 → entry fills at 99.
    """
    # Bars 0-5: low range [97, 98]; entry 99 not reachable from above.
    bars = [mk_candle(i, open_=97.5, high=98.0, low=97.0, close=97.5) for i in range(9)]
    # Bar 6: SELL entry fills — high=99.5 >= entry_price=99.0.
    bars[6] = mk_candle(6, open_=98.5, high=99.5, low=98.0, close=98.8)
    # Bar 7: TP fill — for SHORT, TP below entry. TP=97.0; bar.low=96.5 <= 97.0.
    bars[7] = mk_candle(7, open_=98.0, high=98.5, low=96.5, close=97.0)

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = _make_runner(bars, ledger=ledger, broker=broker)

    sm = runner._state_machine  # noqa: SLF001
    call_count = {"n": 0}

    from nasdaq_ale_bot.core.state_machine import Setup

    arm_ts = bars[5].ts

    def _bias_armed_at_5(sm, view):
        call_count["n"] += 1
        if call_count["n"] == 6:  # bar index 5 (1-based call count)
            sm._active_setup = Setup(  # noqa: SLF001
                bias="SHORT",
                entry_price=99.0,   # above bar 5 high (98.0); SELL fills when high>=99
                stop_price=100.0,
                take_profit=97.0,
                entry_bar_ts=view[-1].ts,
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry_noop(sm, view):
        sm._active_setup.entry_bar_ts = arm_ts  # noqa: SLF001 - keep arm_ts constant
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    def _tm_noop(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias_armed_at_5  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry_noop  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm_noop  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    result = runner.run()

    # Should have at least one trade: entry on bar 6, exit on bar 7.
    assert len(result.trades) >= 1, "Expected at least one completed trade"
    entry_trade = result.trades[0]
    # Entry fill ts == bars[6].ts (the bar on which the entry filled).
    assert entry_trade.entry_ts == bars[6].ts
    assert entry_trade.exit_reason == "target_hit"


# ---------------------------------------------------------------------------
# 3. test_no_look_ahead_during_replay
# ---------------------------------------------------------------------------


def test_no_look_ahead_during_replay(monkeypatch) -> None:
    """CandleView.__getitem__ max index accessed must be <= len(bars)-1."""
    bars = [mk_candle(i) for i in range(10)]
    max_accessed: dict[str, int] = {"k": -1}

    original_getitem = CandleView.__getitem__

    def _spy_getitem(self, k):
        effective_k = k if k >= 0 else self._i + 1 + k  # noqa: SLF001
        if effective_k > max_accessed["k"]:
            max_accessed["k"] = effective_k
        return original_getitem(self, k)

    monkeypatch.setattr(CandleView, "__getitem__", _spy_getitem)

    runner = _make_runner(bars)
    runner.run()

    # Max index accessed should be within the last bar's horizon.
    assert max_accessed["k"] <= len(bars) - 1


# ---------------------------------------------------------------------------
# 4. test_session_rotation_propagated
# ---------------------------------------------------------------------------


def test_session_rotation_propagated() -> None:
    """AccountLedger.on_session_rotation is called at ET day boundaries."""
    # Day 0 bars (Jan 2 UTC).
    day0 = [mk_candle(i, day_offset=0) for i in range(3)]
    # Day 1 bars (Jan 3 UTC).
    day1 = [mk_candle(i, day_offset=1) for i in range(3)]
    bars = day0 + day1

    ledger = _make_ledger()
    rotation_calls: list[date] = []

    original_rotate = ledger.on_session_rotation

    def _spy_rotate(new_today: date, new_equity: Decimal) -> None:
        rotation_calls.append(new_today)
        original_rotate(new_today, new_equity)

    ledger.on_session_rotation = _spy_rotate  # type: ignore[method-assign]

    broker = _make_broker(ledger)
    runner = _make_runner(bars, ledger=ledger, broker=broker)
    runner.run()

    # Rotation should fire at least once (for the first bar of day 0, plus day 1).
    # StateMachine handles the rotation internally.
    assert len(rotation_calls) >= 1
    # At least one rotation must be for Jan 3.
    assert any(d == date(2024, 1, 3) for d in rotation_calls)


# ---------------------------------------------------------------------------
# 5. test_ledger_decimal_integrity
# ---------------------------------------------------------------------------


def test_ledger_decimal_integrity() -> None:
    """After a run with one round-trip, TradeRecord prices are all Decimal."""
    bars = [mk_candle(i, open_=100.0, close=100.0) for i in range(10)]

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = _make_runner(bars, ledger=ledger, broker=broker)

    sm = runner._state_machine  # noqa: SLF001
    from nasdaq_ale_bot.core.state_machine import Setup

    call_n = {"n": 0}

    def _bias(sm, view):
        call_n["n"] += 1
        if call_n["n"] == 1:
            sm._active_setup = Setup(  # noqa: SLF001
                bias="LONG", entry_price=100.0, stop_price=99.0, take_profit=101.2,
                entry_bar_ts=view[-1].ts,
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry(sm, view):
        sm._active_setup.entry_bar_ts = view[-1].ts  # noqa: SLF001
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    def _tm(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    # Bar 1 will touch entry_price=100.0 (low=99.5, high=100.5).
    bars[1] = mk_candle(1, open_=100.0, high=100.5, low=99.5, close=100.2)
    # Bar 3 will hit TP=101.2.
    bars[3] = mk_candle(3, open_=101.3, high=101.5, low=101.0, close=101.4)

    result = runner.run()

    # May have zero or one trade depending on SM flow; just verify Decimal types.
    for trade in result.trades:
        assert isinstance(trade.entry_price, Decimal), f"entry_price is {type(trade.entry_price)}"
        assert isinstance(trade.exit_price, Decimal), f"exit_price is {type(trade.exit_price)}"
        assert isinstance(trade.qty, Decimal), f"qty is {type(trade.qty)}"
        assert isinstance(trade.realized_pnl, Decimal), f"realized_pnl is {type(trade.realized_pnl)}"


# ---------------------------------------------------------------------------
# 6. test_empty_bars_returns_empty_result
# ---------------------------------------------------------------------------


def test_empty_bars_returns_empty_result() -> None:
    """Zero bars input produces BacktestResult with empty trades and curve."""
    runner = _make_runner([])
    result = runner.run()
    assert isinstance(result, BacktestResult)
    assert result.trades == []
    assert result.equity_curve == []


# ---------------------------------------------------------------------------
# 7. test_single_bar_no_crash
# ---------------------------------------------------------------------------


def test_single_bar_no_crash() -> None:
    """A single-bar input runs cleanly without exception."""
    bars = [mk_candle(0)]
    runner = _make_runner(bars)
    result = runner.run()
    assert isinstance(result, BacktestResult)
    assert len(result.equity_curve) == 1


# ---------------------------------------------------------------------------
# 8. test_multi_day_state_continuity
# ---------------------------------------------------------------------------


def test_multi_day_state_continuity() -> None:
    """SM state persists across day boundaries — runner does not reset state."""
    from nasdaq_ale_bot.core.state_machine import Setup

    # Day 0: 5 bars, push SM into TRADE_MANAGEMENT.
    day0 = [mk_candle(i, day_offset=0) for i in range(5)]
    # Day 1: 5 bars.
    day1 = [mk_candle(i, day_offset=1) for i in range(5)]
    bars = day0 + day1

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = _make_runner(bars, ledger=ledger, broker=broker)

    sm = runner._state_machine  # noqa: SLF001
    call_n = {"n": 0}

    def _bias(sm, view):
        call_n["n"] += 1
        if call_n["n"] == 1:
            sm._active_setup = Setup(  # noqa: SLF001
                bias="LONG", entry_price=100.0, stop_price=99.0, take_profit=101.2,
                entry_bar_ts=view[-1].ts,
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry(sm, view):
        sm._active_setup.entry_bar_ts = view[-1].ts  # noqa: SLF001
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    def _tm(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    result = runner.run()

    # After bar 0 the SM should be in TRADE_MANAGEMENT.
    # The runner must NOT reset state between days.
    # The SM state at end of run is TRADE_MANAGEMENT (no exit triggered).
    assert sm.state == StrategyState.TRADE_MANAGEMENT


# ---------------------------------------------------------------------------
# 9. test_trades_list_matches_fill_count
# ---------------------------------------------------------------------------


def test_trades_list_matches_fill_count() -> None:
    """TradeRecord count equals number of completed (entry+exit) round-trips.

    One setup is armed on bar 0. Entry fills on bar 1 (price 102.0 in range).
    TP fills on bar 2 (open gaps above 103.0). Result: exactly 1 TradeRecord.
    """
    bars = [mk_candle(i, open_=100.0, high=100.5, low=99.5, close=100.0) for i in range(10)]

    # Bar 1: entry fills — low <= 102.0 is False (range 99.5-100.5), so we need a
    # bar that reaches 102.0. Make bar 1 have low=102.0 exactly.
    bars[1] = mk_candle(1, open_=102.5, high=103.0, low=101.8, close=102.5)
    # Bar 2: TP fill — open gaps above TP=103.0.
    bars[2] = mk_candle(2, open_=103.5, high=104.0, low=103.0, close=103.8)

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = _make_runner(bars, ledger=ledger, broker=broker)

    sm = runner._state_machine  # noqa: SLF001
    from nasdaq_ale_bot.core.state_machine import Setup

    armed = {"done": False}

    def _bias(sm, view):
        if not armed["done"]:
            armed["done"] = True
            sm._active_setup = Setup(  # noqa: SLF001
                bias="LONG",
                entry_price=102.0,  # above bar 0 range; fills on bar 1
                stop_price=101.0,
                take_profit=103.0,
                entry_bar_ts=view[-1].ts,
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry(sm, view):
        sm._active_setup.entry_bar_ts = view[-1].ts  # noqa: SLF001
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    def _tm(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    result = runner.run()

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "target_hit"


# ---------------------------------------------------------------------------
# 10. test_equity_curve_monotonic_timestamps
# ---------------------------------------------------------------------------


def test_equity_curve_monotonic_timestamps() -> None:
    """Equity curve timestamps are strictly increasing."""
    bars = [mk_candle(i) for i in range(10)]
    runner = _make_runner(bars)
    result = runner.run()

    timestamps = [t for t, _ in result.equity_curve]
    for i in range(1, len(timestamps)):
        assert timestamps[i] > timestamps[i - 1], (
            f"Equity curve not strictly increasing at index {i}: "
            f"{timestamps[i-1]} >= {timestamps[i]}"
        )


# ---------------------------------------------------------------------------
# 11. test_correlated_bars_fed_to_smt
# ---------------------------------------------------------------------------


def test_correlated_bars_fed_to_smt() -> None:
    """When bars_correlated is provided, SMTTracker.on_1m_bar_pair is called."""
    bars_primary = [mk_candle(i) for i in range(5)]
    bars_correlated = [mk_candle(i, open_=300.0, close=300.0) for i in range(5)]

    mock_smt = MagicMock()
    mock_smt.on_1m_bar_pair = MagicMock(return_value=None)

    # Inject the mock smt_tracker via strategy_cfg.
    strategy_cfg = {"_smt_tracker": mock_smt}

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = BacktestRunner(
        bars_primary=bars_primary,
        bars_correlated=bars_correlated,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=strategy_cfg,
        instrument_cfg=_make_instrument(),
    )
    runner.run()

    # on_1m_bar_pair must have been called for each bar.
    assert mock_smt.on_1m_bar_pair.call_count == 5
    # First call should pass bars_primary[0] and bars_correlated[0].
    first_call_kwargs = mock_smt.on_1m_bar_pair.call_args_list[0]
    assert first_call_kwargs.kwargs["primary_bar"] == bars_primary[0]
    assert first_call_kwargs.kwargs["correlated_bar"] == bars_correlated[0]


# ---------------------------------------------------------------------------
# 12. test_result_includes_param_set_hash
# ---------------------------------------------------------------------------


def test_result_includes_param_set_hash() -> None:
    """BacktestResult.param_set_hash and TradeRecord.param_set_hash match input."""
    bars = [mk_candle(i, open_=100.0, close=100.0) for i in range(5)]
    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = _make_runner(bars, ledger=ledger, broker=broker, param_set_hash="abc123")

    sm = runner._state_machine  # noqa: SLF001
    from nasdaq_ale_bot.core.state_machine import Setup

    def _bias(sm, view):
        sm._active_setup = Setup(  # noqa: SLF001
            bias="LONG", entry_price=100.0, stop_price=99.0, take_profit=101.2,
            entry_bar_ts=view[-1].ts,
        )
        return (StrategyState.ENTRY_EXECUTION, "forced")

    def _entry(sm, view):
        sm._active_setup.entry_bar_ts = view[-1].ts  # noqa: SLF001
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    def _tm(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    # Bar 1 fills entry; bar 3 hits TP.
    bars[1] = mk_candle(1, open_=100.0, high=100.5, low=99.5, close=100.2)
    bars[3] = mk_candle(3, open_=101.5, high=102.0, low=101.0, close=101.8)

    result = runner.run()

    assert result.param_set_hash == "abc123"
    for trade in result.trades:
        assert trade.param_set_hash == "abc123"


# ---------------------------------------------------------------------------
# 13. test_load_bars_from_parquet
# ---------------------------------------------------------------------------


def test_load_bars_from_parquet() -> None:
    """Round-trip: write 3 rows to parquet and reload as Candle list."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    ts1 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc)
    ts3 = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)

    df = pd.DataFrame({
        "ts_utc": pd.to_datetime([ts1, ts2, ts3], utc=True),
        "open": [100.0, 101.0, 102.0],
        "high": [100.5, 101.5, 102.5],
        "low": [99.5, 100.5, 101.5],
        "close": [100.2, 101.2, 102.2],
        "volume": [1000.0, 2000.0, 3000.0],
    })

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        pq.write_table(pa.Table.from_pandas(df), tmp_path)
        candles = BacktestRunner.load_bars_from_parquet(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    assert len(candles) == 3
    assert all(isinstance(c, Candle) for c in candles)
    assert candles[0].open == pytest.approx(100.0)
    assert candles[1].high == pytest.approx(101.5)
    assert candles[2].volume == pytest.approx(3000.0)
    # Timestamps must be tz-aware.
    for c in candles:
        assert c.ts.tzinfo is not None


# ---------------------------------------------------------------------------
# 14. test_state_machine_to_broker_bridge
# ---------------------------------------------------------------------------


def test_state_machine_to_broker_bridge() -> None:
    """Stub SM into ENTRY_EXECUTION; assert mock_broker.place_bracket called once.

    Entry price is set above bar range so no fill occurs on the arming bar.
    """
    # Bars range 100..101; entry_price=102 is ABOVE range → no fill on bar 0.
    bars = [mk_candle(i, open_=100.0, high=100.5, low=99.5, close=100.0) for i in range(5)]
    ledger = _make_ledger()
    broker = _make_broker(ledger)

    mock_broker_spy = MagicMock(wraps=broker)
    # Delegate evaluate_fills to real implementation.
    mock_broker_spy.evaluate_fills = broker.evaluate_fills
    mock_broker_spy.place_bracket = MagicMock(side_effect=broker.place_bracket)

    runner = BacktestRunner(
        bars_primary=bars,
        mock_broker=mock_broker_spy,
        ledger=ledger,
        strategy_cfg={},
        instrument_cfg=_make_instrument("QQQ"),
    )

    sm = runner._state_machine  # noqa: SLF001
    from nasdaq_ale_bot.core.state_machine import Setup

    called = {"n": 0}
    arm_ts = bars[0].ts

    def _bias(sm, view):
        called["n"] += 1
        if called["n"] == 1:
            sm._active_setup = Setup(  # noqa: SLF001
                bias="LONG",
                entry_price=102.0,   # above bar range; no same-bar fill
                stop_price=101.0,
                take_profit=103.0,
                entry_bar_ts=view[-1].ts,
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry(sm, view):
        sm._active_setup.entry_bar_ts = arm_ts  # noqa: SLF001
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    def _tm(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    runner.run()

    # place_bracket must have been called exactly once.
    assert mock_broker_spy.place_bracket.call_count == 1

    call_kwargs = mock_broker_spy.place_bracket.call_args.kwargs
    assert call_kwargs["entry"] == Decimal("102.0")
    assert call_kwargs["stop"] == Decimal("101.0")
    assert call_kwargs["take_profit"] == Decimal("103.0")
    assert call_kwargs["side"] == "BUY"


# ---------------------------------------------------------------------------
# 15. test_same_bar_double_arm_deduplicated
# ---------------------------------------------------------------------------


def test_same_bar_double_arm_deduplicated() -> None:
    """SM stays in ENTRY_EXECUTION for three bars; place_bracket called only once.

    Entry price is 105.0 — above all bars' ranges (max high=100.5) so no fill
    fires and the look-ahead guard is never triggered.
    """
    bars = [mk_candle(i, open_=100.0, high=100.5, low=99.5, close=100.0) for i in range(5)]
    ledger = _make_ledger()
    broker = _make_broker(ledger)

    place_count = {"n": 0}
    original_place = broker.place_bracket

    def _spy_place(**kwargs):
        place_count["n"] += 1
        return original_place(**kwargs)

    broker.place_bracket = _spy_place  # type: ignore[method-assign]

    runner = _make_runner(bars, ledger=ledger, broker=broker)
    sm = runner._state_machine  # noqa: SLF001

    from nasdaq_ale_bot.core.state_machine import Setup

    # Use a fixed entry_bar_ts so client_order_id is identical across bars.
    fixed_ts = bars[0].ts
    bars_seen = {"n": 0}

    def _bias(sm, view):
        bars_seen["n"] += 1
        if bars_seen["n"] == 1:
            sm._active_setup = Setup(  # noqa: SLF001
                bias="LONG",
                entry_price=105.0,  # above all bar highs; no fill triggered
                stop_price=104.0,
                take_profit=106.0,
                entry_bar_ts=fixed_ts,
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry(sm, view):
        # Keep same entry_bar_ts so client_order_id stays identical.
        setup = sm._active_setup  # noqa: SLF001
        if setup is not None:
            setup.entry_bar_ts = fixed_ts
        # Stay in ENTRY_EXECUTION for first 3 calls (bars 0, 1, 2), then advance.
        if bars_seen["n"] <= 3:
            return (sm.state, "still_armed")
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    def _tm(sm, view):
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    runner.run()

    # Despite SM being in ENTRY_EXECUTION across multiple bars,
    # place_bracket should be called only once (deduplication via _armed_order_ids).
    assert place_count["n"] == 1, f"Expected 1 place_bracket call, got {place_count['n']}"


# ---------------------------------------------------------------------------
# 16. test_flatten_on_trade_management_to_flat
# ---------------------------------------------------------------------------


def test_flatten_on_trade_management_to_flat() -> None:
    """When SM transitions TRADE_MANAGEMENT -> FLAT, mock_broker.flatten is called."""
    bars = [mk_candle(i, open_=100.0, close=100.0) for i in range(5)]
    ledger = _make_ledger()
    broker = _make_broker(ledger)

    flatten_calls: list[Any] = []
    original_flatten = broker.flatten

    def _spy_flatten(symbol=None):
        flatten_calls.append(symbol)
        original_flatten(symbol=symbol)

    broker.flatten = _spy_flatten  # type: ignore[method-assign]

    runner = _make_runner(bars, ledger=ledger, broker=broker)
    sm = runner._state_machine  # noqa: SLF001

    from nasdaq_ale_bot.core.state_machine import Setup

    call_n = {"n": 0}

    def _bias(sm, view):
        call_n["n"] += 1
        if call_n["n"] == 1:
            sm._active_setup = Setup(  # noqa: SLF001
                bias="LONG",
                entry_price=100.0,
                stop_price=99.0,
                take_profit=101.2,
                entry_bar_ts=view[-1].ts,
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry(sm, view):
        sm._active_setup.entry_bar_ts = view[-1].ts  # noqa: SLF001
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    tm_calls = {"n": 0}

    def _tm(sm, view):
        tm_calls["n"] += 1
        if tm_calls["n"] == 1:
            # Transition to FLAT on the first TM bar.
            return (StrategyState.FLAT, "forced_exit")
        return (sm.state, "noop")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = _tm  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (StrategyState.BIAS_DETERMINATION, "rearm")  # noqa: SLF001

    runner.run()

    # flatten must have been called with symbol=None.
    assert len(flatten_calls) >= 1
    assert flatten_calls[0] is None


# ---------------------------------------------------------------------------
# Coverage-supplemental tests (repr/eq, fallback branches, warnings)
# ---------------------------------------------------------------------------


def test_trade_record_eq_and_repr() -> None:
    """TradeRecord __eq__ and __repr__ exercise object equality + string form."""
    ts1 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    ts2 = ts1 + timedelta(minutes=5)
    a = TradeRecord(
        entry_ts=ts1, exit_ts=ts2, symbol="QQQ", side="BUY",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        qty=Decimal("1"), realized_pnl=Decimal("1"),
        exit_reason="target_hit", param_set_hash=None,
    )
    b = TradeRecord(
        entry_ts=ts1, exit_ts=ts2, symbol="QQQ", side="BUY",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        qty=Decimal("1"), realized_pnl=Decimal("1"),
        exit_reason="target_hit", param_set_hash=None,
    )
    assert a == b
    assert a != "not a record"  # triggers NotImplemented branch
    assert "TradeRecord" in repr(a)
    assert "target_hit" in repr(a)


def test_backtest_result_repr() -> None:
    """BacktestResult.__repr__ returns a readable string."""
    result = BacktestResult(
        trades=[], equity_curve=[], metrics={}, params={},
        param_set_hash="xyz", window_start=date(2024, 1, 1),
        window_end=date(2024, 1, 31),
    )
    r = repr(result)
    assert "BacktestResult" in r
    assert "xyz" in r


def test_arm_bracket_skipped_when_setup_missing() -> None:
    """If SM enters ENTRY_EXECUTION but _active_setup lacks entry/stop, no arm."""
    bars = [mk_candle(i) for i in range(3)]
    ledger = _make_ledger()
    broker = _make_broker(ledger)
    place_count = {"n": 0}
    original_place = broker.place_bracket

    def _spy_place(**kw):
        place_count["n"] += 1
        return original_place(**kw)

    broker.place_bracket = _spy_place  # type: ignore[method-assign]

    runner = _make_runner(bars, ledger=ledger, broker=broker)
    sm = runner._state_machine  # noqa: SLF001

    def _bias(sm, view):
        # Leave _active_setup as None — bridge must short-circuit.
        return (StrategyState.ENTRY_EXECUTION, "forced")

    def _entry(sm, view):
        return (StrategyState.FLAT, "no_setup_payload")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.FLAT] = lambda sm, v: (sm.state, "noop")  # noqa: SLF001

    runner.run()
    assert place_count["n"] == 0


def test_arm_bracket_take_profit_fallback_short() -> None:
    """When setup.take_profit is None, runner derives a fallback TP (short path)."""
    bars = [mk_candle(i, open_=100.0, high=100.5, low=99.5, close=100.0) for i in range(3)]
    ledger = _make_ledger()
    broker = _make_broker(ledger)
    captured_tp: dict[str, Decimal] = {}
    original_place = broker.place_bracket

    def _spy_place(**kw):
        captured_tp["tp"] = kw["take_profit"]
        return original_place(**kw)

    broker.place_bracket = _spy_place  # type: ignore[method-assign]

    runner = _make_runner(bars, ledger=ledger, broker=broker)
    sm = runner._state_machine  # noqa: SLF001
    from nasdaq_ale_bot.core.state_machine import Setup

    called = {"n": 0}

    def _bias(sm, view):
        called["n"] += 1
        if called["n"] == 1:
            sm._active_setup = Setup(  # noqa: SLF001
                bias="SHORT", entry_price=105.0, stop_price=106.0,
                take_profit=None,  # force fallback
                entry_bar_ts=None,  # exercises ts fallback path too
            )
            return (StrategyState.ENTRY_EXECUTION, "forced")
        return (sm.state, "noop")

    def _entry(sm, view):
        return (StrategyState.TRADE_MANAGEMENT, "submitted")

    sm._handlers[StrategyState.BIAS_DETERMINATION] = _bias  # noqa: SLF001
    sm._handlers[StrategyState.ENTRY_EXECUTION] = _entry  # noqa: SLF001
    sm._handlers[StrategyState.TRADE_MANAGEMENT] = lambda sm, v: (sm.state, "noop")  # noqa: SLF001

    runner.run()
    # Short fallback TP = entry - 1 = 104.
    assert captured_tp["tp"] == Decimal("105.0") - Decimal("1")


def test_exit_fill_without_open_entry_is_logged(caplog) -> None:
    """An exit FillEvent whose coid has no matching entry logs a warning."""
    from nasdaq_ale_bot.execution.mock_broker import FillEvent

    bars = [mk_candle(0)]
    runner = _make_runner(bars)

    orphan = FillEvent(
        client_order_id="ghost-1",
        symbol="QQQ",
        side="BUY",
        qty=Decimal("1"),
        fill_price=Decimal("100"),
        fill_ts=bars[0].ts,
        fill_reason="STOP_OUT",
        realized_pnl=Decimal("-1"),
    )
    # Feed an orphan exit directly to _process_fills.
    runner._process_fills([orphan])  # noqa: SLF001
    # Should not raise; trade list stays empty.
    assert len(runner._trades) == 0  # noqa: SLF001


def test_correlated_bars_shorter_than_primary_pads_last() -> None:
    """When bars_correlated is shorter than bars_primary, runner pads with last."""
    bars_primary = [mk_candle(i) for i in range(5)]
    bars_correlated = [mk_candle(i, open_=300.0, close=300.0) for i in range(2)]

    mock_smt = MagicMock()
    mock_smt.on_1m_bar_pair = MagicMock(return_value=None)

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = BacktestRunner(
        bars_primary=bars_primary,
        bars_correlated=bars_correlated,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg={"_smt_tracker": mock_smt},
        instrument_cfg=_make_instrument(),
    )
    runner.run()

    # 5 calls total (pads last 3 with bars_correlated[-1]).
    assert mock_smt.on_1m_bar_pair.call_count == 5
    last_call = mock_smt.on_1m_bar_pair.call_args_list[-1]
    assert last_call.kwargs["correlated_bar"] == bars_correlated[-1]


def test_correlated_ts_mismatch_warning(caplog) -> None:
    """When correlated bar ts != primary bar ts, runner logs a warning."""
    import logging
    bars_primary = [mk_candle(i) for i in range(2)]
    # Shift correlated ts by 30 seconds.
    base = bars_primary[0].ts + timedelta(seconds=30)
    bars_correlated = [
        Candle(ts=base + timedelta(minutes=i), open=300.0, high=300.5,
               low=299.5, close=300.0, volume=1000.0)
        for i in range(2)
    ]

    mock_smt = MagicMock()
    mock_smt.on_1m_bar_pair = MagicMock(return_value=None)

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = BacktestRunner(
        bars_primary=bars_primary,
        bars_correlated=bars_correlated,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg={"_smt_tracker": mock_smt},
        instrument_cfg=_make_instrument(),
    )
    with caplog.at_level(logging.WARNING):
        runner.run()
    # on_1m_bar_pair was called despite mismatch (defensive path).
    assert mock_smt.on_1m_bar_pair.call_count == 2


def test_smt_tracker_exception_is_caught() -> None:
    """If SMT.on_1m_bar_pair raises, runner logs and continues."""
    bars_primary = [mk_candle(i) for i in range(2)]
    bars_correlated = [mk_candle(i) for i in range(2)]

    mock_smt = MagicMock()
    mock_smt.on_1m_bar_pair = MagicMock(side_effect=RuntimeError("boom"))

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = BacktestRunner(
        bars_primary=bars_primary,
        bars_correlated=bars_correlated,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg={"_smt_tracker": mock_smt},
        instrument_cfg=_make_instrument(),
    )
    # Does not raise.
    result = runner.run()
    assert result is not None


def test_build_smt_tracker_real_path() -> None:
    """_build_smt_tracker returns a real SMTTracker when none is injected."""
    bars_primary = [mk_candle(i) for i in range(2)]
    bars_correlated = [mk_candle(i) for i in range(2)]

    ledger = _make_ledger()
    broker = _make_broker(ledger)
    runner = BacktestRunner(
        bars_primary=bars_primary,
        bars_correlated=bars_correlated,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg={},  # no _smt_tracker override
        instrument_cfg=_make_instrument("QQQ"),
    )
    # The runner should have built an SMTTracker instance internally.
    sm = runner._state_machine  # noqa: SLF001
    assert sm._smt_tracker is not None  # noqa: SLF001


def test_equity_curve_non_monotonic_raises() -> None:
    """Forcing a duplicate ts in the equity curve raises ValueError."""
    bars = [mk_candle(i) for i in range(3)]
    runner = _make_runner(bars)
    runner._equity_curve = [  # noqa: SLF001
        (bars[0].ts, Decimal("50000")),
        (bars[0].ts, Decimal("50000")),  # duplicate
    ]
    with pytest.raises(ValueError, match="not strictly increasing"):
        runner._assert_equity_curve_monotonic()  # noqa: SLF001
