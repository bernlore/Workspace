"""WalkForwardController — IS/OOS split with single-shot OOS evaluation.

See PLAN_PHASE3.md §3.6 / Step 8.

Contract:
  * In-Sample (IS)  = 2024-01..2024-04  -> full GridHarness sweep.
  * Out-of-Sample   = 2024-05..2024-06  -> best-IS params ONLY, ONE pass.
  * OOS gate        = WR >= 0.55 (configurable).

Single-shot OOS is enforced at runtime: ``run_oos()`` raises if called twice
on the same controller instance.  This is the discipline guard — silent
iterative tuning on OOS data is a strictly worse protocol than no walk-forward
at all (over-fitting you cannot detect).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nasdaq_ale_bot.backtest.grid import (
    DEFAULT_START_EQUITY,
    GridHarness,
    GridResult,
    ParamResult,
    compute_param_set_hash,
)
from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.mock_broker import MockBroker

if TYPE_CHECKING:
    from nasdaq_ale_bot.core.candle import Candle


OOS_WR_THRESHOLD_DEFAULT = 0.55


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class OOSResult:
    params: ParamResult
    metrics: dict[str, Any]
    wr: float
    passed: bool


@dataclass
class WalkForwardResult:
    grid_result: GridResult
    best_is: ParamResult | None
    oos: OOSResult | None
    wr_threshold: float
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    verdict_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# WalkForwardController
# ---------------------------------------------------------------------------


class WalkForwardController:
    def __init__(
        self,
        *,
        bars_primary: list["Candle"],
        bars_correlated: list["Candle"] | None = None,
        instrument_cfg: Any,
        base_strategy_cfg: dict[str, Any] | None = None,
        is_start: date,
        is_end: date,
        oos_start: date,
        oos_end: date,
        wr_threshold: float = OOS_WR_THRESHOLD_DEFAULT,
        start_equity: Decimal = DEFAULT_START_EQUITY,
        output_path: Path | None = None,
    ) -> None:
        if is_end < is_start:
            raise ValueError("is_end < is_start")
        if oos_end < oos_start:
            raise ValueError("oos_end < oos_start")
        if oos_start <= is_end:
            raise ValueError(
                f"oos_start={oos_start} must follow is_end={is_end} (no overlap)"
            )
        self._bars_primary = bars_primary
        self._bars_correlated = bars_correlated
        self._instrument_cfg = instrument_cfg
        self._base_cfg = dict(base_strategy_cfg or {})
        self._is_start, self._is_end = is_start, is_end
        self._oos_start, self._oos_end = oos_start, oos_end
        self._wr_threshold = wr_threshold
        self._start_equity = start_equity
        self._output_path = output_path
        self._oos_consumed = False

    # ------------------------------------------------------------------
    # Split
    # ------------------------------------------------------------------

    def split(self) -> tuple[list["Candle"], list["Candle"]]:
        is_bars = [
            b for b in self._bars_primary
            if self._is_start <= b.ts.date() <= self._is_end
        ]
        oos_bars = [
            b for b in self._bars_primary
            if self._oos_start <= b.ts.date() <= self._oos_end
        ]
        return is_bars, oos_bars

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self) -> WalkForwardResult:
        is_bars, oos_bars = self.split()
        warnings: list[str] = []

        harness = GridHarness(
            bars_primary=is_bars,
            bars_correlated=self._bars_correlated,
            instrument_cfg=self._instrument_cfg,
            base_strategy_cfg=self._base_cfg,
            start_equity=self._start_equity,
        )
        grid_result = harness.run()

        best_is = grid_result.top_3[0] if grid_result.top_3 else None
        if best_is is None:
            warnings.append("grid_produced_no_results")

        oos_result: OOSResult | None = None
        if best_is is not None and oos_bars:
            oos_result = self._run_oos(best_is, oos_bars)
        elif not oos_bars:
            warnings.append("oos_window_empty")

        verdict_path: Path | None = None
        if self._output_path is not None:
            verdict_path = self._write_verdict(
                best_is=best_is, oos=oos_result, warnings=warnings
            )

        return WalkForwardResult(
            grid_result=grid_result,
            best_is=best_is,
            oos=oos_result,
            wr_threshold=self._wr_threshold,
            is_start=self._is_start,
            is_end=self._is_end,
            oos_start=self._oos_start,
            oos_end=self._oos_end,
            verdict_path=verdict_path,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # OOS — single-shot
    # ------------------------------------------------------------------

    def _run_oos(
        self, best: ParamResult, oos_bars: list["Candle"]
    ) -> OOSResult:
        if self._oos_consumed:
            raise RuntimeError(
                "OOS already evaluated on this controller — single-shot protocol"
            )
        self._oos_consumed = True

        params = best.params
        cfg = dict(self._base_cfg)
        cfg["ifvg_tolerance_ticks"] = params.ifvg_tolerance_ticks
        cfg["rr_cap"] = params.rr_cap
        cfg["cisd_lookback_bars"] = params.cisd_lookback_bars

        start_date = oos_bars[0].ts.date()
        ledger = AccountLedger(
            session_start_equity=self._start_equity, today=start_date
        )
        pv_raw = getattr(self._instrument_cfg, "point_value", None)
        point_value = Decimal(str(pv_raw)) if pv_raw is not None else Decimal("1")
        broker = MockBroker(
            ledger=ledger,
            initial_equity=self._start_equity,
            point_value=point_value,
        )
        runner = BacktestRunner(
            bars_primary=oos_bars,
            bars_correlated=self._bars_correlated,
            mock_broker=broker,
            ledger=ledger,
            strategy_cfg=cfg,
            instrument_cfg=self._instrument_cfg,
            param_set_hash=compute_param_set_hash(params),
        )
        result = runner.run()
        rpt = cfg.get("risk_per_trade_usd")
        risk_usd = Decimal(str(rpt)) if rpt is not None else None
        metrics = MetricsCalculator(risk_per_trade_usd=risk_usd).compute(
            trades=result.trades, equity_curve=result.equity_curve
        )
        passed = metrics.wr >= self._wr_threshold
        return OOSResult(
            params=best,
            metrics={
                "wr": metrics.wr,
                "avg_rr": metrics.avg_rr,
                "max_dd_usd": metrics.max_dd_usd,
                "profit_factor": metrics.profit_factor,
                "sharpe": metrics.sharpe,
                "trades_count": metrics.trades_count,
            },
            wr=metrics.wr,
            passed=passed,
        )

    # ------------------------------------------------------------------
    # Verdict artifact
    # ------------------------------------------------------------------

    def _write_verdict(
        self,
        *,
        best_is: ParamResult | None,
        oos: OOSResult | None,
        warnings: list[str],
    ) -> Path:
        assert self._output_path is not None
        payload: dict[str, Any] = {
            "wr_threshold": self._wr_threshold,
            "is_window": [self._is_start.isoformat(), self._is_end.isoformat()],
            "oos_window": [self._oos_start.isoformat(), self._oos_end.isoformat()],
            "best_is_params": best_is.params.to_dict() if best_is else None,
            "best_is_param_set_hash": best_is.param_set_hash if best_is else None,
            "best_is_composite_score": best_is.composite_score if best_is else None,
            "oos_wr": oos.wr if oos else None,
            "oos_passed": oos.passed if oos else False,
            "oos_metrics": _jsonify(oos.metrics) if oos else None,
            "warnings": warnings,
        }
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self._output_path


def _jsonify(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out
