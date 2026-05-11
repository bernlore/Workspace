"""Unit tests for backtest.grid — GridHarness + composite_score + hash."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from nasdaq_ale_bot.backtest.grid import (
    COMPOSITE_DD_PENALTY,
    COMPOSITE_PF_WEIGHT,
    COMPOSITE_WR_WEIGHT,
    MAXDD_NORMALIZATION_USD,
    PF_NORMALIZATION_CAP,
    GridHarness,
    GridParams,
    GridResult,
    ParamResult,
    compute_param_set_hash,
)
from nasdaq_ale_bot.core.candle import Candle


def _mk_bar(i: int) -> Candle:
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(minutes=i)
    return Candle(ts=ts, open=100.0, high=100.5, low=99.5, close=100.0, volume=1000.0)


def _mk_instrument(symbol: str = "QQQ") -> Any:
    class _I:
        pass
    i = _I()
    i.symbol = symbol  # type: ignore[attr-defined]
    return i


def test_default_grid_produces_27_sets() -> None:
    bars = [_mk_bar(i) for i in range(3)]
    harness = GridHarness(
        bars_primary=bars,
        instrument_cfg=_mk_instrument(),
        base_strategy_cfg={},
    )
    assert len(harness.enumerate_params()) == 27


def test_custom_grid_respects_overrides() -> None:
    bars = [_mk_bar(i) for i in range(3)]
    harness = GridHarness(
        bars_primary=bars,
        instrument_cfg=_mk_instrument(),
        base_strategy_cfg={},
        ifvg_tolerance_values=[0, 1],  # 2 x 3 x 3 = 18
    )
    assert len(harness.enumerate_params()) == 18


def test_composite_score_formula() -> None:
    """WR=0.6, PF=2.0, MaxDD=2000 -> 0.6*0.5 + (2/3)*0.3 + (2000/5000)*(-0.2) = 0.42."""
    score = GridHarness.composite_score(wr=0.6, pf=2.0, max_dd_usd=2000.0)
    expected = (
        0.6 * COMPOSITE_WR_WEIGHT
        + (2.0 / float(PF_NORMALIZATION_CAP)) * COMPOSITE_PF_WEIGHT
        - (2000.0 / float(MAXDD_NORMALIZATION_USD)) * COMPOSITE_DD_PENALTY
    )
    assert score == pytest.approx(expected)
    assert score == pytest.approx(0.42)


def test_composite_score_caps_pf_and_dd() -> None:
    """PF > 3 clamps to 1.0; MaxDD > 5000 clamps to 1.0."""
    score = GridHarness.composite_score(wr=1.0, pf=999.0, max_dd_usd=99999.0)
    expected = 1.0 * COMPOSITE_WR_WEIGHT + 1.0 * COMPOSITE_PF_WEIGHT - 1.0 * COMPOSITE_DD_PENALTY
    assert score == pytest.approx(expected)


def test_top_3_selection_by_score() -> None:
    """Five ParamResults with known scores; Top-3 are the three highest."""
    result = GridResult()
    scores = [0.1, 0.5, 0.3, 0.9, 0.7]
    for i, s in enumerate(scores):
        p = GridParams(
            ifvg_tolerance_ticks=i,
            rr_cap=Decimal("1.3"),
            cisd_lookback_bars=15,
        )
        result.all_results.append(
            ParamResult(
                params=p,
                param_set_hash=f"hash-{i}",
                metrics={},
                composite_score=s,
            )
        )
    # Manual sort + top_3 like run() does.
    result.all_results.sort(key=lambda r: (-r.composite_score, r.param_set_hash))
    result.top_3 = result.all_results[:3]
    assert [r.composite_score for r in result.top_3] == [0.9, 0.7, 0.5]


def test_tie_break_by_hash() -> None:
    """Two results with identical scores are ordered by ascending hash."""
    p1 = GridParams(0, Decimal("1.3"), 15)
    p2 = GridParams(1, Decimal("1.3"), 15)
    a = ParamResult(params=p1, param_set_hash="aaa", metrics={}, composite_score=0.5)
    b = ParamResult(params=p2, param_set_hash="bbb", metrics={}, composite_score=0.5)
    results = [b, a]
    results.sort(key=lambda r: (-r.composite_score, r.param_set_hash))
    assert results[0].param_set_hash == "aaa"
    assert results[1].param_set_hash == "bbb"


def test_param_set_hash_includes_strategy_version() -> None:
    p = GridParams(0, Decimal("1.3"), 15)
    h1 = compute_param_set_hash(p, strategy_version="1.0.0")
    h2 = compute_param_set_hash(p, strategy_version="1.0.1")
    assert h1 != h2


def test_param_set_hash_deterministic() -> None:
    p = GridParams(0, Decimal("1.3"), 15)
    a = compute_param_set_hash(p, strategy_version="x")
    b = compute_param_set_hash(p, strategy_version="x")
    assert a == b


def test_grid_params_to_dict_round_trip() -> None:
    p = GridParams(ifvg_tolerance_ticks=1, rr_cap=Decimal("1.3"), cisd_lookback_bars=20)
    d = p.to_dict()
    assert d["ifvg_tolerance_ticks"] == 1
    assert d["rr_cap"] == "1.3"
    assert d["cisd_lookback_bars"] == 20


def test_grid_on_empty_bars() -> None:
    harness = GridHarness(
        bars_primary=[],
        instrument_cfg=_mk_instrument(),
        base_strategy_cfg={},
    )
    result = harness.run()
    assert result.all_results == []
    assert result.top_3 == []


def test_run_end_to_end_minimal() -> None:
    """Run harness with a tiny 1-param grid on 10 flat bars — smoke test."""
    bars = [_mk_bar(i) for i in range(10)]
    harness = GridHarness(
        bars_primary=bars,
        instrument_cfg=_mk_instrument(),
        base_strategy_cfg={},
        ifvg_tolerance_values=[0],
        rr_cap_values=[Decimal("1.3")],
        cisd_lookback_values=[15],
    )
    result = harness.run()
    assert len(result.all_results) == 1
    assert len(result.top_3) == 1
    assert result.all_results[0].rank == 1
    assert "wr" in result.all_results[0].metrics


def test_to_dataframe_columns() -> None:
    """DataFrame has all expected columns."""
    bars = [_mk_bar(i) for i in range(5)]
    harness = GridHarness(
        bars_primary=bars,
        instrument_cfg=_mk_instrument(),
        base_strategy_cfg={},
        ifvg_tolerance_values=[0, 1],
        rr_cap_values=[Decimal("1.3")],
        cisd_lookback_values=[15],
    )
    result = harness.run()
    df = harness.to_dataframe(result)
    expected_cols = {
        "param_set_hash",
        "ifvg_tolerance_ticks",
        "rr_cap",
        "cisd_lookback_bars",
        "wr",
        "avg_rr",
        "max_dd_usd",
        "profit_factor",
        "sharpe",
        "trades_count",
        "composite_score",
        "rank",
    }
    assert expected_cols.issubset(set(df.columns))
    assert len(df) == 2  # two param combos


def test_default_rr_cap_values_match_plan() -> None:
    """Default RR cap axis matches A7 {1.1, 1.2, 1.3} (cap=1.3 ceiling)."""
    assert GridHarness.DEFAULT_RR_CAP == [
        Decimal("1.1"),
        Decimal("1.2"),
        Decimal("1.3"),
    ]


def test_default_ifvg_and_cisd_axes_match_plan() -> None:
    assert GridHarness.DEFAULT_IFVG_TOLERANCE == [0, 1, 2]
    assert GridHarness.DEFAULT_CISD_LOOKBACK == [10, 15, 20]
