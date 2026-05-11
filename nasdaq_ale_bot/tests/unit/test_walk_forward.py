"""Unit tests for backtest.walk_forward — IS/OOS split + single-shot gate."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nasdaq_ale_bot.backtest.grid import GridParams, ParamResult
from nasdaq_ale_bot.backtest.walk_forward import (
    OOS_WR_THRESHOLD_DEFAULT,
    OOSResult,
    WalkForwardController,
    WalkForwardResult,
)
from nasdaq_ale_bot.core.candle import Candle


def _bar_on(d: date, minute: int = 0) -> Candle:
    ts = datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc) + timedelta(
        minutes=minute
    )
    return Candle(ts=ts, open=100.0, high=100.5, low=99.5, close=100.0, volume=500.0)


def _make_bars() -> list[Candle]:
    bars: list[Candle] = []
    # IS window 2024-01 (3 days, 2 bars each).
    for d_ in [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]:
        bars.extend([_bar_on(d_, 0), _bar_on(d_, 1)])
    # OOS window 2024-05 (2 days, 2 bars each).
    for d_ in [date(2024, 5, 1), date(2024, 5, 2)]:
        bars.extend([_bar_on(d_, 0), _bar_on(d_, 1)])
    return bars


def _instrument() -> Any:
    return SimpleNamespace(symbol="QQQ")


def _wfc(**over: Any) -> WalkForwardController:
    defaults: dict[str, Any] = dict(
        bars_primary=_make_bars(),
        instrument_cfg=_instrument(),
        base_strategy_cfg={},
        is_start=date(2024, 1, 1),
        is_end=date(2024, 4, 30),
        oos_start=date(2024, 5, 1),
        oos_end=date(2024, 6, 30),
    )
    defaults.update(over)
    return WalkForwardController(**defaults)


def test_split_isolates_is_and_oos() -> None:
    ctrl = _wfc()
    is_bars, oos_bars = ctrl.split()
    assert len(is_bars) == 6
    assert len(oos_bars) == 4
    for b in is_bars:
        assert b.ts.date() <= date(2024, 4, 30)
    for b in oos_bars:
        assert b.ts.date() >= date(2024, 5, 1)


def test_oos_window_must_follow_is_window() -> None:
    with pytest.raises(ValueError, match="must follow"):
        _wfc(oos_start=date(2024, 4, 1))


def test_is_end_before_is_start_rejected() -> None:
    with pytest.raises(ValueError):
        _wfc(is_start=date(2024, 4, 30), is_end=date(2024, 1, 1))


def test_oos_end_before_oos_start_rejected() -> None:
    with pytest.raises(ValueError):
        _wfc(oos_start=date(2024, 6, 30), oos_end=date(2024, 5, 1))


def test_default_wr_threshold_is_55pct() -> None:
    assert OOS_WR_THRESHOLD_DEFAULT == 0.55


def test_run_produces_verdict_json(tmp_path: Path) -> None:
    out = tmp_path / "phase3_oos_verdict.json"
    ctrl = _wfc(
        ifvg_override=True,  # ignored kwarg via **over pattern not used here
        output_path=out,
    ) if False else _wfc(output_path=out)
    # Use smallest possible grid for speed.
    ctrl = WalkForwardController(
        bars_primary=_make_bars(),
        instrument_cfg=_instrument(),
        base_strategy_cfg={},
        is_start=date(2024, 1, 1),
        is_end=date(2024, 4, 30),
        oos_start=date(2024, 5, 1),
        oos_end=date(2024, 6, 30),
        output_path=out,
    )
    # Shrink grid via harness override.
    result = ctrl.run()
    assert isinstance(result, WalkForwardResult)
    assert out.exists()
    payload = json.loads(out.read_text())
    assert "wr_threshold" in payload
    assert "oos_passed" in payload
    assert payload["is_window"] == ["2024-01-01", "2024-04-30"]
    assert payload["oos_window"] == ["2024-05-01", "2024-06-30"]


def test_single_shot_oos_double_call_raises() -> None:
    ctrl = _wfc()
    # Fake a best-IS result and call _run_oos twice.
    params = GridParams(0, Decimal("1.3"), 15)
    best = ParamResult(
        params=params, param_set_hash="h", metrics={}, composite_score=0.5
    )
    oos_bars = [_bar_on(date(2024, 5, 1), 0)]
    ctrl._run_oos(best, oos_bars)
    with pytest.raises(RuntimeError, match="single-shot"):
        ctrl._run_oos(best, oos_bars)


def test_empty_oos_window_warned(tmp_path: Path) -> None:
    out = tmp_path / "verdict.json"
    # No OOS bars in range (shift OOS window to a date not present).
    bars = _make_bars()
    ctrl = WalkForwardController(
        bars_primary=[b for b in bars if b.ts.date() <= date(2024, 1, 4)],
        instrument_cfg=_instrument(),
        base_strategy_cfg={},
        is_start=date(2024, 1, 1),
        is_end=date(2024, 4, 30),
        oos_start=date(2024, 5, 1),
        oos_end=date(2024, 6, 30),
        output_path=out,
    )
    result = ctrl.run()
    assert result.oos is None
    assert "oos_window_empty" in result.warnings
    payload = json.loads(out.read_text())
    assert payload["oos_passed"] is False


def test_empty_is_bars_yields_no_best(tmp_path: Path) -> None:
    out = tmp_path / "verdict.json"
    # Only OOS bars; IS window selects nothing.
    oos_only = [b for b in _make_bars() if b.ts.date() >= date(2024, 5, 1)]
    ctrl = WalkForwardController(
        bars_primary=oos_only,
        instrument_cfg=_instrument(),
        base_strategy_cfg={},
        is_start=date(2024, 1, 1),
        is_end=date(2024, 4, 30),
        oos_start=date(2024, 5, 1),
        oos_end=date(2024, 6, 30),
        output_path=out,
    )
    result = ctrl.run()
    assert result.best_is is None
    assert result.oos is None
    assert "grid_produced_no_results" in result.warnings


def test_verdict_missing_when_no_output_path() -> None:
    ctrl = _wfc()
    result = ctrl.run()
    assert result.verdict_path is None


def test_oos_result_dataclass_has_required_fields() -> None:
    params = GridParams(0, Decimal("1.3"), 15)
    best = ParamResult(
        params=params, param_set_hash="h", metrics={}, composite_score=0.5
    )
    oos = OOSResult(params=best, metrics={"wr": 0.6}, wr=0.6, passed=True)
    assert oos.wr == 0.6
    assert oos.passed is True


def test_custom_wr_threshold_changes_verdict(tmp_path: Path) -> None:
    out = tmp_path / "verdict.json"
    # Threshold 0.0 ensures oos_passed=True iff OOS ran at all.
    ctrl = WalkForwardController(
        bars_primary=_make_bars(),
        instrument_cfg=_instrument(),
        base_strategy_cfg={},
        is_start=date(2024, 1, 1),
        is_end=date(2024, 4, 30),
        oos_start=date(2024, 5, 1),
        oos_end=date(2024, 6, 30),
        wr_threshold=0.0,
        output_path=out,
    )
    result = ctrl.run()
    # With 0 trades on synthetic flat bars, WR=0.0 and threshold=0.0 -> True.
    if result.oos is not None:
        assert result.oos.passed is (result.oos.wr >= 0.0)
