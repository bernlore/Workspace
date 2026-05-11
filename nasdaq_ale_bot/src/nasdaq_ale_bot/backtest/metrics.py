"""MetricsCalculator — compute strategy metrics from trade list and equity curve.

Consumed by GridHarness (composite score inputs) and WalkForwardController
(OOS verdict).  See PLAN_PHASE3.md §4 Step 4.

Edge cases (pinned by tests):
  - Zero trades       -> WR=0.0, avg_rr=0.0, PF=0.0, Sharpe=0.0, max_dd=0.
  - All losses        -> PF=0.0 (0 / abs(sum_losses)).
  - All wins          -> PF=999.0 (capped).
  - Flat equity curve -> Sharpe=0.0.
  - Single trading day -> trades_per_day == trades_count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nasdaq_ale_bot.backtest.runner import TradeRecord


PROFIT_FACTOR_CAP = 999.0
# Round-trip commission per contract — mirrors mock_broker.COMMISSION_PER_CONTRACT.
# Kept here so MetricsCalculator can derive ``commission_total`` from a list of
# TradeRecords alone (no broker handle required), making the metric usable in
# unit tests with hand-built fixtures.
COMMISSION_PER_CONTRACT = Decimal("4.50")


@dataclass(frozen=True)
class StrategyMetrics:
    """Pinned metric set — column names match PLAN.md §3 / Step 4 spec."""

    wr: float
    avg_rr: float
    max_dd_usd: Decimal
    max_dd_pct: float
    profit_factor: float
    sharpe: float
    trades_count: int
    trades_per_day: float
    avg_hold_minutes: float
    total_pnl_usd: Decimal
    commission_total: Decimal


class MetricsCalculator:
    """Stateless calculator. Annualization factor is configurable (default 252)."""

    def __init__(
        self,
        *,
        annualization_factor: int = 252,
        risk_per_trade_usd: Decimal | None = None,
    ) -> None:
        self._af = annualization_factor
        # When set, ``avg_rr`` is computed as ``realized_pnl / risk_per_trade_usd``
        # for unit-less R-multiples in the dollar-based sizing regime. When None
        # we fall back to the price-point denominator (entry−stop), which is
        # appropriate for QQQ-era qty=1 fixtures.
        self._risk_usd: Decimal | None = risk_per_trade_usd

    def compute(
        self,
        *,
        trades: list["TradeRecord"],
        equity_curve: list[tuple[datetime, Decimal]],
    ) -> StrategyMetrics:
        wr = self._compute_wr(trades)
        avg_rr = self._compute_avg_rr(trades, self._risk_usd)
        max_dd_usd, max_dd_pct = self._compute_max_dd(equity_curve)
        pf = self._compute_profit_factor(trades)
        sharpe = self._compute_sharpe(equity_curve, self._af)

        trades_count = len(trades)
        trades_per_day = self._compute_trades_per_day(trades)
        avg_hold_minutes = self._compute_avg_hold_minutes(trades)
        total_pnl_usd = sum(
            (t.realized_pnl for t in trades), start=Decimal("0")
        )
        commission_total = sum(
            (COMMISSION_PER_CONTRACT * t.qty for t in trades),
            start=Decimal("0"),
        )

        return StrategyMetrics(
            wr=wr,
            avg_rr=avg_rr,
            max_dd_usd=max_dd_usd,
            max_dd_pct=max_dd_pct,
            profit_factor=pf,
            sharpe=sharpe,
            trades_count=trades_count,
            trades_per_day=trades_per_day,
            avg_hold_minutes=avg_hold_minutes,
            total_pnl_usd=total_pnl_usd,
            commission_total=commission_total,
        )

    # ---- static helpers -------------------------------------------------

    @staticmethod
    def _compute_wr(trades: list["TradeRecord"]) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.realized_pnl > Decimal("0"))
        return wins / len(trades)

    @staticmethod
    def _compute_avg_rr(
        trades: list["TradeRecord"],
        risk_per_trade_usd: Decimal | None = None,
    ) -> float:
        """Mean of per-trade R-multiple.

        Three regimes:
          1. ``risk_per_trade_usd`` provided → ``R = realized_pnl / risk_usd``.
             Correct for the futures-sizing world where ``realized_pnl`` is in
             dollars (qty × point_value × Δprice) and risk_usd is the bet size.
             Returns unit-less R per trade (winner ≈ +rr_cap, loser ≈ -1).
          2. ``stop_price`` set on TradeRecord → ``R = pnl / |entry - stop|``.
             Correct for QQQ qty=1 where pnl is per-share dollars and the
             denominator is per-share risk.
          3. Neither → ``R = pnl / |entry - exit|``. Legacy fallback.
        """
        if not trades:
            return 0.0
        risk_usd_f = (
            float(risk_per_trade_usd) if risk_per_trade_usd is not None else None
        )
        ratios: list[float] = []
        for t in trades:
            if risk_usd_f is not None and risk_usd_f > 0:
                r = float(t.realized_pnl) / risk_usd_f
                ratios.append(r)
                continue
            stop = getattr(t, "stop_price", None)
            if stop is not None:
                denom = abs(float(t.entry_price) - float(stop))
            else:
                denom = abs(float(t.entry_price) - float(t.exit_price))
            if denom == 0:
                continue
            ratios.append(float(t.realized_pnl) / denom)
        if not ratios:
            return 0.0
        return sum(ratios) / len(ratios)

    @staticmethod
    def _compute_max_dd(
        equity_curve: list[tuple[datetime, Decimal]],
    ) -> tuple[Decimal, float]:
        """Peak-to-trough drawdown. Returns (usd, fraction_of_hwm)."""
        if not equity_curve:
            return Decimal("0"), 0.0
        hwm = equity_curve[0][1]
        max_dd_usd = Decimal("0")
        max_dd_pct = 0.0
        for _, equity in equity_curve:
            if equity > hwm:
                hwm = equity
            dd = hwm - equity
            if dd > max_dd_usd:
                max_dd_usd = dd
                if hwm > 0:
                    max_dd_pct = float(dd) / float(hwm)
        return max_dd_usd, max_dd_pct

    @staticmethod
    def _compute_profit_factor(trades: list["TradeRecord"]) -> float:
        if not trades:
            return 0.0
        wins_sum = Decimal("0")
        losses_sum = Decimal("0")
        for t in trades:
            if t.realized_pnl > 0:
                wins_sum += t.realized_pnl
            else:
                losses_sum += t.realized_pnl  # negative
        if losses_sum == 0:
            return 0.0 if wins_sum == 0 else PROFIT_FACTOR_CAP
        pf = float(wins_sum) / float(abs(losses_sum))
        return min(pf, PROFIT_FACTOR_CAP)

    @staticmethod
    def _compute_sharpe(
        equity_curve: list[tuple[datetime, Decimal]], af: int
    ) -> float:
        """Annualised Sharpe on bar-to-bar equity returns."""
        if len(equity_curve) < 2:
            return 0.0
        returns: list[float] = []
        prev = equity_curve[0][1]
        for _, eq in equity_curve[1:]:
            if prev == 0:
                prev = eq
                continue
            r = float(eq - prev) / float(prev)
            returns.append(r)
            prev = eq
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(variance)
        if std == 0.0:
            return 0.0
        return (mean / std) * math.sqrt(af)

    @staticmethod
    def _compute_trades_per_day(trades: list["TradeRecord"]) -> float:
        if not trades:
            return 0.0
        distinct_days = {t.entry_ts.date() for t in trades}
        return len(trades) / len(distinct_days)

    @staticmethod
    def _compute_avg_hold_minutes(trades: list["TradeRecord"]) -> float:
        if not trades:
            return 0.0
        total_seconds = sum(
            (t.exit_ts - t.entry_ts).total_seconds() for t in trades
        )
        return (total_seconds / len(trades)) / 60.0
