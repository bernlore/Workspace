#!/usr/bin/env python3
"""Count state-transition rejection reasons across the full 2024H1 replay.

Helps localize where the ICT detection funnel is losing candidates: sweep,
CISD, IFVG, zone filter, SMT direction, killzone, or gates.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]
qqq = BacktestRunner.load_bars_from_parquet(
    REPO / "data" / "historical" / "QQQ_1m_2024H1.parquet"
)
spy = BacktestRunner.load_bars_from_parquet(
    REPO / "data" / "historical" / "SPY_1m_2024H1.parquet"
)
cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
cfg["_bias_detector"] = HTFBiasDetector(inst)
cfg["_gate_list"] = GateList.base_list(cfg)
cfg.setdefault("default_qty", Decimal("1"))
cfg["ifvg_tolerance_ticks"] = 2
cfg["rr_cap"] = Decimal("1.3")
cfg["cisd_lookback_bars"] = 20

reasons: Counter = Counter()
ledger = AccountLedger(
    session_start_equity=Decimal("50000"), today=qqq[0].ts.date()
)
broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"))
runner = BacktestRunner(
    bars_primary=qqq,
    bars_correlated=spy,
    mock_broker=broker,
    ledger=ledger,
    strategy_cfg=cfg,
    instrument_cfg=inst,
    param_set_hash="diag",
)
sm = runner._state_machine
orig_on_bar = sm.on_bar


def patched(bar):
    evs = orig_on_bar(bar)
    for e in evs:
        reasons[e.reason] += 1
    return evs


sm.on_bar = patched
result = runner.run()
print(f"trades completed: {len(result.trades)}")
for r, c in reasons.most_common():
    print(f"  {r:40s} {c}")
