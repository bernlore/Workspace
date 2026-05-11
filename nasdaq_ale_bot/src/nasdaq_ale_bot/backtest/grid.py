"""GridHarness — sweep parameter grid on IS window, rank by composite score.

Grid axes (PLAN_PHASE3.md §3.5, total 27 = 3 x 3 x 3):
  - ifvg_tolerance_ticks  -> {0, 1, 2}
  - rr_cap                -> {Decimal("1.1"), Decimal("1.2"), Decimal("1.3")}  (per A7)
  - cisd_lookback_bars    -> {10, 15, 20}

Composite score weights (module constants — discretionary per ADR §9):
  score = WR*0.5 + PF_norm*0.3 - MaxDD_norm*0.2
  PF normalised against a 3.0 cap, MaxDD normalised against USD 5000.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.core import STRATEGY_VERSION
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.mock_broker import MockBroker

if TYPE_CHECKING:
    import pandas as pd

    from nasdaq_ale_bot.core.candle import Candle


# ---------------------------------------------------------------------------
# Composite-score constants (discretionary — see ADR §9 Consequences)
# ---------------------------------------------------------------------------

COMPOSITE_WR_WEIGHT = 0.5
COMPOSITE_PF_WEIGHT = 0.3
COMPOSITE_DD_PENALTY = 0.2

PF_NORMALIZATION_CAP = Decimal("3.0")
MAXDD_NORMALIZATION_USD = Decimal("5000")

# Disqualify param sets that fire too few IS trades to draw any statistical
# signal from. With a 14-month IS window the floor stays at 10 — small enough
# to admit slow regimes, large enough to reject 1-of-3 luck wins.
MIN_TRADES_IS = 10
# Composite-score floors — a regime that fires plenty of trades but doesn't
# carry a positive expectancy is not a viable IS pick. Disqualify rather than
# rank against other low-quality combos.
MIN_WR_IS = 0.45
MIN_PF_IS = 1.0
DISQUALIFIED_SCORE = -999.0

DEFAULT_START_EQUITY = Decimal("50000")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridParams:
    """One point in the parameter grid."""

    ifvg_tolerance_ticks: int
    rr_cap: Decimal
    cisd_lookback_bars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ifvg_tolerance_ticks": self.ifvg_tolerance_ticks,
            "rr_cap": str(self.rr_cap),
            "cisd_lookback_bars": self.cisd_lookback_bars,
        }


@dataclass
class ParamResult:
    params: GridParams
    param_set_hash: str
    metrics: dict[str, Any]
    composite_score: float
    rank: int = 0


@dataclass
class GridResult:
    all_results: list[ParamResult] = field(default_factory=list)
    top_3: list[ParamResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deterministic param_set_hash
# ---------------------------------------------------------------------------


def compute_param_set_hash(
    params: GridParams, strategy_version: str = STRATEGY_VERSION
) -> str:
    """Deterministic sha256 of (sorted-params JSON | strategy_version)."""
    payload = json.dumps(params.to_dict(), sort_keys=True)
    digest = hashlib.sha256(f"{payload}|{strategy_version}".encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# GridHarness
# ---------------------------------------------------------------------------


class GridHarness:
    """Sweep parameter grid, rank by composite score, expose Top-3."""

    DEFAULT_IFVG_TOLERANCE: list[int] = [0, 1, 2]
    # Per ASSUMPTIONS.md §A7 the cap floor=1.1 / target=1.2 / cap=1.3.
    # rr_cap=1.5 is out of spec and excluded from the sweep.
    DEFAULT_RR_CAP: list[Decimal] = [Decimal("1.1"), Decimal("1.2"), Decimal("1.3")]
    DEFAULT_CISD_LOOKBACK: list[int] = [10, 15, 20]

    def __init__(
        self,
        *,
        bars_primary: list["Candle"],
        bars_correlated: list["Candle"] | None = None,
        base_strategy_cfg: dict[str, Any] | None = None,
        instrument_cfg: Any,
        ifvg_tolerance_values: list[int] | None = None,
        rr_cap_values: list[Decimal] | None = None,
        cisd_lookback_values: list[int] | None = None,
        start_equity: Decimal = DEFAULT_START_EQUITY,
    ) -> None:
        self._bars_primary = bars_primary
        self._bars_correlated = bars_correlated
        self._base_cfg = dict(base_strategy_cfg or {})
        self._instrument_cfg = instrument_cfg
        self._ifvg_values = ifvg_tolerance_values or list(self.DEFAULT_IFVG_TOLERANCE)
        self._rr_values = rr_cap_values or list(self.DEFAULT_RR_CAP)
        self._cisd_values = cisd_lookback_values or list(self.DEFAULT_CISD_LOOKBACK)
        self._start_equity = start_equity
        # If the strategy uses risk-based futures sizing, pass the budget into
        # the metrics calculator so avg_rr is reported in unit-less R-multiples.
        rpt = self._base_cfg.get("risk_per_trade_usd")
        risk_usd = Decimal(str(rpt)) if rpt is not None else None
        self._metrics_calc = MetricsCalculator(risk_per_trade_usd=risk_usd)

    def enumerate_params(self) -> list[GridParams]:
        combos = itertools.product(
            self._ifvg_values, self._rr_values, self._cisd_values
        )
        return [
            GridParams(
                ifvg_tolerance_ticks=tol,
                rr_cap=rr,
                cisd_lookback_bars=cisd,
            )
            for tol, rr, cisd in combos
        ]

    def run(self) -> GridResult:
        """Run BacktestRunner for each param combo; return ranked result."""
        params_list = self.enumerate_params()
        if not self._bars_primary:
            return GridResult(all_results=[], top_3=[])

        results: list[ParamResult] = []
        for params in params_list:
            param_hash = compute_param_set_hash(params)
            metrics_dict = self._run_single(params, param_hash)
            trades_count = int(metrics_dict.get("trades_count", 0))
            wr = float(metrics_dict.get("wr", 0.0))
            pf = float(metrics_dict.get("profit_factor", 0.0))
            if (
                trades_count < MIN_TRADES_IS
                or wr < MIN_WR_IS
                or pf < MIN_PF_IS
            ):
                score = DISQUALIFIED_SCORE
            else:
                score = self.composite_score(
                    wr=wr,
                    pf=pf,
                    max_dd_usd=float(metrics_dict["max_dd_usd"]),
                )
            results.append(
                ParamResult(
                    params=params,
                    param_set_hash=param_hash,
                    metrics=metrics_dict,
                    composite_score=score,
                )
            )

        # Rank: descending composite_score; tie-break by ascending param_set_hash.
        results.sort(
            key=lambda r: (-r.composite_score, r.param_set_hash)
        )
        for i, r in enumerate(results, start=1):
            r.rank = i

        return GridResult(all_results=results, top_3=results[:3])

    def _run_single(
        self, params: GridParams, param_hash: str
    ) -> dict[str, Any]:
        """Instantiate fresh ledger+broker+runner; return metrics dict."""
        cfg = dict(self._base_cfg)
        cfg["ifvg_tolerance_ticks"] = params.ifvg_tolerance_ticks
        cfg["rr_cap"] = params.rr_cap
        cfg["cisd_lookback_bars"] = params.cisd_lookback_bars

        from datetime import date as _date
        start_date = self._bars_primary[0].ts.date() if self._bars_primary else _date.today()
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
            bars_primary=self._bars_primary,
            bars_correlated=self._bars_correlated,
            mock_broker=broker,
            ledger=ledger,
            strategy_cfg=cfg,
            instrument_cfg=self._instrument_cfg,
            param_set_hash=param_hash,
        )
        result = runner.run()
        metrics = self._metrics_calc.compute(
            trades=result.trades,
            equity_curve=result.equity_curve,
        )
        return {
            "wr": metrics.wr,
            "avg_rr": metrics.avg_rr,
            "max_dd_usd": metrics.max_dd_usd,
            "max_dd_pct": metrics.max_dd_pct,
            "profit_factor": metrics.profit_factor,
            "sharpe": metrics.sharpe,
            "trades_count": metrics.trades_count,
            "trades_per_day": metrics.trades_per_day,
            "avg_hold_minutes": metrics.avg_hold_minutes,
            "total_pnl_usd": metrics.total_pnl_usd,
        }

    @staticmethod
    def composite_score(
        wr: float, pf: float, max_dd_usd: float
    ) -> float:
        pf_norm = min(pf / float(PF_NORMALIZATION_CAP), 1.0)
        dd_norm = min(max_dd_usd / float(MAXDD_NORMALIZATION_USD), 1.0)
        return (
            wr * COMPOSITE_WR_WEIGHT
            + pf_norm * COMPOSITE_PF_WEIGHT
            - dd_norm * COMPOSITE_DD_PENALTY
        )

    def to_dataframe(self, result: GridResult) -> "pd.DataFrame":
        """Convert to ranked DataFrame for results/phase3_grid_results.csv."""
        import pandas as pd

        rows: list[dict[str, Any]] = []
        for r in result.all_results:
            rows.append(
                {
                    "param_set_hash": r.param_set_hash,
                    "ifvg_tolerance_ticks": r.params.ifvg_tolerance_ticks,
                    "rr_cap": str(r.params.rr_cap),
                    "cisd_lookback_bars": r.params.cisd_lookback_bars,
                    "wr": r.metrics.get("wr", 0.0),
                    "avg_rr": r.metrics.get("avg_rr", 0.0),
                    "max_dd_usd": str(r.metrics.get("max_dd_usd", 0)),
                    "profit_factor": r.metrics.get("profit_factor", 0.0),
                    "sharpe": r.metrics.get("sharpe", 0.0),
                    "trades_count": r.metrics.get("trades_count", 0),
                    "composite_score": r.composite_score,
                    "rank": r.rank,
                }
            )
        return pd.DataFrame(rows)
