"""Unit tests for backtest.metrics — StrategyMetrics + MetricsCalculator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nasdaq_ale_bot.backtest.metrics import (
    PROFIT_FACTOR_CAP,
    MetricsCalculator,
    StrategyMetrics,
)
from nasdaq_ale_bot.backtest.runner import TradeRecord


def _trade(
    pnl: Decimal,
    *,
    entry_offset_min: int = 0,
    hold_min: int = 5,
    entry: Decimal = Decimal("100"),
    exit_: Decimal | None = None,
    day_offset: int = 0,
) -> TradeRecord:
    base = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(days=day_offset)
    entry_ts = base + timedelta(minutes=entry_offset_min)
    exit_ts = entry_ts + timedelta(minutes=hold_min)
    if exit_ is None:
        exit_ = entry + pnl
    return TradeRecord(
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        symbol="QQQ",
        side="BUY",
        entry_price=entry,
        exit_price=exit_,
        qty=Decimal("1"),
        realized_pnl=pnl,
        exit_reason="target_hit" if pnl > 0 else "stop_out",
        param_set_hash=None,
    )


def test_wr_on_known_trade_list() -> None:
    """6 wins + 4 losses -> WR=0.6."""
    trades = [_trade(Decimal("1.0")) for _ in range(6)] + [
        _trade(Decimal("-1.0")) for _ in range(4)
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    assert m.wr == pytest.approx(0.6)


def test_avg_rr_mixed() -> None:
    """Mix of wins and losses — verify ratio averaging."""
    trades = [
        _trade(Decimal("1.2"), entry=Decimal("100"), exit_=Decimal("101.2")),
        _trade(Decimal("-1.0"), entry=Decimal("100"), exit_=Decimal("99")),
        _trade(Decimal("1.5"), entry=Decimal("100"), exit_=Decimal("101.5")),
        _trade(Decimal("-1.0"), entry=Decimal("100"), exit_=Decimal("99")),
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    # Each trade's ratio = pnl / |entry-exit| = 1 in absolute value.
    # Signed: +1, -1, +1, -1 -> mean = 0.0.
    assert m.avg_rr == pytest.approx(0.0)


def test_max_dd_synthetic_equity_curve() -> None:
    """Known equity curve with HWM=100_000, trough=97_000 -> MaxDD=3000 (3%)."""
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    curve = [
        (ts + timedelta(minutes=i), eq)
        for i, eq in enumerate(
            [Decimal("100000"), Decimal("100000"), Decimal("97000"),
             Decimal("98000"), Decimal("99000")]
        )
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=[], equity_curve=curve)
    assert m.max_dd_usd == Decimal("3000")
    assert m.max_dd_pct == pytest.approx(0.03)


def test_profit_factor_all_losses_returns_zero() -> None:
    trades = [_trade(Decimal("-1.0")) for _ in range(3)]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    assert m.profit_factor == 0.0


def test_profit_factor_all_wins_caps_at_999() -> None:
    trades = [_trade(Decimal("1.0")) for _ in range(3)]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    assert m.profit_factor == PROFIT_FACTOR_CAP


def test_profit_factor_mixed() -> None:
    """wins=3 losses=-1.5 -> pf = 3/1.5 = 2.0."""
    trades = [
        _trade(Decimal("2.0")),
        _trade(Decimal("1.0")),
        _trade(Decimal("-1.5")),
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    assert m.profit_factor == pytest.approx(2.0)


def test_sharpe_flat_equity_returns_zero() -> None:
    """Flat curve (zero returns stdev) -> Sharpe=0."""
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    curve = [(ts + timedelta(minutes=i), Decimal("50000")) for i in range(10)]
    calc = MetricsCalculator()
    m = calc.compute(trades=[], equity_curve=curve)
    assert m.sharpe == 0.0


def test_sharpe_single_sample_returns_zero() -> None:
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    curve = [(ts, Decimal("50000"))]
    calc = MetricsCalculator()
    m = calc.compute(trades=[], equity_curve=curve)
    assert m.sharpe == 0.0


def test_sharpe_positive_on_monotonic_growth() -> None:
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    curve = [
        (ts + timedelta(minutes=i), Decimal("50000") + Decimal(str(i * 10)))
        for i in range(20)
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=[], equity_curve=curve)
    assert m.sharpe > 0.0


def test_trades_per_day_normalization() -> None:
    """10 trades across 5 distinct days -> 2.0."""
    trades: list[TradeRecord] = []
    for d in range(5):
        for k in range(2):
            trades.append(_trade(Decimal("0.5"), day_offset=d, entry_offset_min=k))
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    assert m.trades_per_day == pytest.approx(2.0)


def test_avg_hold_minutes() -> None:
    trades = [
        _trade(Decimal("1"), hold_min=10),
        _trade(Decimal("1"), hold_min=20, entry_offset_min=60),
        _trade(Decimal("1"), hold_min=30, entry_offset_min=120),
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    assert m.avg_hold_minutes == pytest.approx(20.0)


def test_compute_returns_strategy_metrics_instance() -> None:
    calc = MetricsCalculator()
    m = calc.compute(trades=[], equity_curve=[])
    assert isinstance(m, StrategyMetrics)


def test_decimal_preserved_in_pnl_fields() -> None:
    trades = [_trade(Decimal("1.23")), _trade(Decimal("-0.50"))]
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    curve = [(ts + timedelta(minutes=i), Decimal("50000")) for i in range(3)]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=curve)
    assert isinstance(m.max_dd_usd, Decimal)
    assert isinstance(m.total_pnl_usd, Decimal)
    assert m.total_pnl_usd == Decimal("0.73")


def test_empty_inputs_produce_zero_metrics() -> None:
    calc = MetricsCalculator()
    m = calc.compute(trades=[], equity_curve=[])
    assert m.wr == 0.0
    assert m.avg_rr == 0.0
    assert m.profit_factor == 0.0
    assert m.sharpe == 0.0
    assert m.max_dd_usd == Decimal("0")
    assert m.max_dd_pct == 0.0
    assert m.trades_count == 0
    assert m.trades_per_day == 0.0
    assert m.avg_hold_minutes == 0.0
    assert m.total_pnl_usd == Decimal("0")


def test_avg_rr_zero_delta_trades_skipped() -> None:
    """Trades where entry==exit (zero delta) are skipped in avg_rr."""
    trades = [
        _trade(Decimal("0"), entry=Decimal("100"), exit_=Decimal("100")),
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=trades, equity_curve=[])
    assert m.avg_rr == 0.0


def test_sharpe_zero_equity_prev_is_skipped() -> None:
    """Defensive: prev==0 branch in Sharpe calculation."""
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    # Start at zero, jump up — first return is undefined, gets skipped.
    curve = [
        (ts, Decimal("0")),
        (ts + timedelta(minutes=1), Decimal("50000")),
        (ts + timedelta(minutes=2), Decimal("50010")),
        (ts + timedelta(minutes=3), Decimal("50020")),
    ]
    calc = MetricsCalculator()
    m = calc.compute(trades=[], equity_curve=curve)
    # Sharpe uses only the valid returns after prev became non-zero.
    assert isinstance(m.sharpe, float)
