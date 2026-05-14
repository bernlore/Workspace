#!/usr/bin/env python3
"""Phase 3 end-to-end pipeline orchestrator.

Thin wiring only — NO logic changes to Phase 3 modules.

Steps:
    1. Load instruments.yaml (primary=QQQ, correlated=SPY).
    2. Load strategy.yaml.
    3. Load QQQ + SPY parquet bars from data/historical/.
    4. Build HTFBiasDetector + GateList.base_list(cfg) and inject via
       strategy_cfg["_bias_detector"] / ["_gate_list"].
    5. Run WalkForwardController.run():
         IS = 2024-01-01..2024-04-30, OOS = 2024-05-01..2024-06-30.
    6. Write results/phase3_oos_verdict.json (by controller) +
       results/phase3_grid_results.csv (ranked full grid).

Usage:
    python scripts/run_phase3_pipeline.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.grid import GridHarness
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.backtest.walk_forward import WalkForwardController
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "historical"
RESULTS_DIR = REPO_ROOT / "results"
OOS_VERDICT_PATH = RESULTS_DIR / "phase3_oos_verdict.json"
GRID_CSV_PATH = RESULTS_DIR / "phase3_grid_results.csv"

IS_START = date(2023, 1, 1)
IS_END = date(2024, 2, 28)
OOS_START = date(2024, 3, 1)
OOS_END = date(2024, 6, 30)


def main() -> int:
    instruments_cfg = load_instruments_config(REPO_ROOT / "config" / "instruments.yaml")
    strategy_cfg = load_strategy_config(REPO_ROOT / "config" / "strategy.yaml")

    primary = instruments_cfg.primary
    correlated = instruments_cfg.correlated
    log.info("primary=%s correlated=%s", primary.symbol, correlated.symbol)

    nq_path = DATA_DIR / "NQ_1m_2022_2026.dbn.zst"
    es_path = DATA_DIR / "ES_1m_2022_2026.dbn.zst"
    log.info("loading bars %s + %s", nq_path.name, es_path.name)
    qqq_bars = BacktestRunner.load_bars_from_dbn(nq_path, symbol_prefix="NQ")
    spy_bars = BacktestRunner.load_bars_from_dbn(es_path, symbol_prefix="ES")
    log.info("loaded NQ=%d ES=%d bars", len(qqq_bars), len(spy_bars))

    strategy_cfg["_bias_detector"] = HTFBiasDetector(primary)
    strategy_cfg["_gate_list"] = GateList.base_list(strategy_cfg)
    # Apex risk-per-trade: NQ has point_value=$20, so qty is computed dynamically
    # in BacktestRunner._size_for_trade based on stop distance.
    strategy_cfg["risk_per_trade_usd"] = Decimal("750")
    # LIMIT at zone edge with carry-forward — true ICT retest semantics.
    strategy_cfg["entry_order_type"] = "LIMIT"
    strategy_cfg["entry_slippage_ticks"] = 0
    strategy_cfg.setdefault("default_qty", Decimal("1"))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    controller = WalkForwardController(
        bars_primary=qqq_bars,
        bars_correlated=spy_bars,
        instrument_cfg=primary,
        base_strategy_cfg=strategy_cfg,
        is_start=IS_START,
        is_end=IS_END,
        oos_start=OOS_START,
        oos_end=OOS_END,
        output_path=OOS_VERDICT_PATH,
    )
    log.info(
        "running walk-forward: IS=%s..%s OOS=%s..%s",
        IS_START, IS_END, OOS_START, OOS_END,
    )
    result = controller.run()

    harness_view = GridHarness(
        bars_primary=[],
        instrument_cfg=primary,
        base_strategy_cfg={},
    )
    df = harness_view.to_dataframe(result.grid_result)
    df.to_csv(GRID_CSV_PATH, index=False)
    log.info("wrote %s rows=%d", GRID_CSV_PATH.name, len(df))
    log.info("wrote %s", OOS_VERDICT_PATH.name)

    total_is_trades = sum(int(r.metrics.get("trades_count", 0)) for r in result.grid_result.all_results)
    oos_trades = int(result.oos.metrics.get("trades_count", 0)) if result.oos else 0
    log.info(
        "grid combos=%d total_IS_trades=%d OOS_trades=%d oos_passed=%s warnings=%s",
        len(result.grid_result.all_results),
        total_is_trades,
        oos_trades,
        result.oos.passed if result.oos else None,
        result.warnings,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
