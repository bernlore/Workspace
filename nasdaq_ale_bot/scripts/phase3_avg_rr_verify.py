#!/usr/bin/env python3
"""Replay IS window with the best-IS combo and print MetricsCalculator output."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]
IS_START = date(2023, 1, 1)
IS_END = date(2024, 2, 29)


def slice_(bars):
    return [b for b in bars if IS_START <= b.ts.date() <= IS_END]


qqq = slice_(BacktestRunner.load_bars_from_parquet(
    REPO / "data" / "historical" / "QQQ_1m_2023_2024H1.parquet"
))
spy = slice_(BacktestRunner.load_bars_from_parquet(
    REPO / "data" / "historical" / "SPY_1m_2023_2024H1.parquet"
))

cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
cfg["_bias_detector"] = HTFBiasDetector(inst)
cfg["_gate_list"] = GateList.base_list(cfg)
cfg.setdefault("default_qty", Decimal("1"))
cfg["ifvg_tolerance_ticks"] = 1
cfg["rr_cap"] = Decimal("1.3")
cfg["cisd_lookback_bars"] = 20

ledger = AccountLedger(session_start_equity=Decimal("50000"), today=qqq[0].ts.date())
broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"))
runner = BacktestRunner(
    bars_primary=qqq,
    bars_correlated=spy,
    mock_broker=broker,
    ledger=ledger,
    strategy_cfg=cfg,
    instrument_cfg=inst,
    param_set_hash="verify_avgrr",
)
res = runner.run()
m = MetricsCalculator().compute(trades=res.trades, equity_curve=res.equity_curve)

trades = res.trades
n_with_stop = sum(1 for t in trades if t.stop_price is not None)
print(f"Best-IS combo: tol=1, rr_cap=1.3, cisd_lookback=20")
print(f"IS trades:  {m.trades_count}  (with stop_price plumbed: {n_with_stop})")
print(f"WR:         {m.wr:.4f}")
print(f"avg_rr:     {m.avg_rr:+.4f}    (was +0.0690 with old formula)")
print(f"PF:         {m.profit_factor:.4f}")
print(f"max_dd_usd: {m.max_dd_usd}")
print(f"sharpe:     {m.sharpe:+.4f}")
