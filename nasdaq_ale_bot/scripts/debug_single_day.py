#!/usr/bin/env python3
"""Single-day replay for trade-count-explosion diagnosis."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

REPO = Path(__file__).resolve().parents[1]
TARGET = date(2024, 5, 2)


def main() -> int:
    inst = load_instruments_config(REPO / "config" / "instruments.yaml")
    cfg = load_strategy_config(REPO / "config" / "strategy.yaml")

    qqq = BacktestRunner.load_bars_from_parquet(REPO / "data" / "historical" / "QQQ_1m_2024H1.parquet")
    # Warm up detector with all bars up to and including TARGET
    day_bars = [b for b in qqq if b.ts.date() <= TARGET]
    same_day = [b for b in day_bars if b.ts.date() == TARGET]
    print(f"bars up to {TARGET}: {len(day_bars)} (target-day={len(same_day)})")

    cfg["_bias_detector"] = HTFBiasDetector(inst.primary)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["ifvg_tolerance_ticks"] = 2
    cfg["rr_cap"] = Decimal("1.3")
    cfg["cisd_lookback_bars"] = 20
    cfg["default_qty"] = Decimal("1")

    ledger = AccountLedger(session_start_equity=Decimal("50000"), today=TARGET)
    broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"))
    runner = BacktestRunner(
        bars_primary=day_bars,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=inst.primary,
        param_set_hash="debug",
    )
    result = runner.run()
    print(f"trades on {TARGET}: {len(result.trades)}")
    for i, t in enumerate(result.trades[:10], 1):
        print(f"  #{i}: {t.entry_ts.time()}..{t.exit_ts.time()} {t.side} entry={t.entry_price} exit={t.exit_price} pnl={t.realized_pnl} reason={t.exit_reason}")
    if len(result.trades) > 10:
        print(f"  ... {len(result.trades) - 10} more")

    sm = runner._state_machine
    print(f"SM._trades_today (after run): {sm._trades_today}")
    print(f"ledger has 'trades_today' attr: {hasattr(ledger, 'trades_today')}")
    print(f"gate_list has {len(cfg['_gate_list']._gates)} gates: {[g.name for g in cfg['_gate_list']._gates]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
