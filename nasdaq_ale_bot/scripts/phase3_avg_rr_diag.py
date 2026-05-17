#!/usr/bin/env python3
"""Diagnose avg_rr by capturing per-trade stop_price + risk + tp at entry time.

Runs the IS window (2023-01-01 .. 2024-02-29) with the best-IS params and
prints 3 sample winning trades with: entry, tp_at_entry, stop_at_entry,
risk_amount, actual_exit_price, booked_pnl, and the R-multiple computed two
ways (pnl/risk vs pnl/exit_delta).
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.strategies.nasdaqale import state_machine as sm_mod
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


qqq = slice_(
    BacktestRunner.load_bars_from_parquet(
        REPO / "data" / "historical" / "QQQ_1m_2023_2024H1.parquet"
    )
)
spy = slice_(
    BacktestRunner.load_bars_from_parquet(
        REPO / "data" / "historical" / "SPY_1m_2023_2024H1.parquet"
    )
)

cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
cfg["_bias_detector"] = HTFBiasDetector(inst)
cfg["_gate_list"] = GateList.base_list(cfg)
cfg.setdefault("default_qty", Decimal("1"))
# Best-IS params (rank 1 from the grid)
cfg["ifvg_tolerance_ticks"] = 1
cfg["rr_cap"] = Decimal("1.3")
cfg["cisd_lookback_bars"] = 20

# Capture stop+tp at the moment ENTRY_EXECUTION commits (order_submitted).
captures: list[dict] = []
orig_entry = sm_mod._handle_entry_execution


def wrap_entry(sm, view):
    setup = sm._active_setup
    if setup is None:
        return orig_entry(sm, view)
    pre_entry = setup.entry_price
    pre_stop = setup.stop_price
    pre_tp = setup.take_profit
    pre_bias = setup.bias
    result = orig_entry(sm, view)
    _, reason = result
    if reason == "order_submitted":
        bar = view[-1]
        captures.append({
            "armed_bar_ts": bar.ts,
            "side": "BUY" if pre_bias == "LONG" else "SELL",
            "entry_at_arm": float(pre_entry),
            "stop_at_arm": float(pre_stop),
            "tp_at_arm": float(pre_tp),
            "risk": abs(float(pre_entry) - float(pre_stop)),
        })
    return result


sm_mod._handle_entry_execution = wrap_entry

ledger = AccountLedger(session_start_equity=Decimal("50000"), today=qqq[0].ts.date())
broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"))
runner = BacktestRunner(
    bars_primary=qqq,
    bars_correlated=spy,
    mock_broker=broker,
    ledger=ledger,
    strategy_cfg=cfg,
    instrument_cfg=inst,
    param_set_hash="diag_avgrr",
)
result = runner.run()
trades = result.trades

# Pair each trade with its capture (latest capture preceding the entry_ts on the same side).
def find_cap(t):
    best = None
    for c in captures:
        if c["armed_bar_ts"] >= t.entry_ts:
            break
        if c["side"] != t.side:
            continue
        best = c
    return best


winners = [t for t in trades if t.realized_pnl > 0]
losers = [t for t in trades if t.realized_pnl <= 0]

print(f"Total IS trades: {len(trades)}")
print(f"  winners: {len(winners)}  losers: {len(losers)}  WR: {len(winners)/max(len(trades),1):.3f}")

# Aggregate R two ways
sum_r_risk = 0.0
sum_r_exit = 0.0
n_pairs = 0
for t in trades:
    c = find_cap(t)
    if c is None or c["risk"] == 0:
        continue
    pnl = float(t.realized_pnl)
    exit_delta = abs(float(t.entry_price) - float(t.exit_price))
    if exit_delta > 0:
        sum_r_exit += pnl / exit_delta
    sum_r_risk += pnl / c["risk"]
    n_pairs += 1
print(f"\nAggregate over {n_pairs} matched trades:")
print(f"  avg_rr (R = pnl / risk_at_arm): {sum_r_risk / n_pairs:+.4f}")
print(f"  avg_rr (R = pnl / exit_delta) : {sum_r_exit / n_pairs:+.4f}  <-- current metrics.py formula")

print("\n===== 3 SAMPLE WINNING IS TRADES =====")
for i, t in enumerate(winners[:3]):
    c = find_cap(t)
    print(f"\n--- Winner #{i+1} ---")
    print(f"  side             = {t.side}")
    print(f"  entry_ts         = {t.entry_ts.isoformat()}")
    print(f"  exit_ts          = {t.exit_ts.isoformat()}")
    print(f"  entry_price      = {t.entry_price}    (planned at arm: {c and c['entry_at_arm']})")
    print(f"  stop_price@arm   = {c and c['stop_at_arm']}")
    print(f"  tp_price@arm     = {c and c['tp_at_arm']}")
    print(f"  actual_exit      = {t.exit_price}    reason={t.exit_reason}")
    print(f"  realized_pnl     = {t.realized_pnl}")
    if c:
        risk = c["risk"]
        exit_delta = abs(float(t.entry_price) - float(t.exit_price))
        print(f"  risk_amount      = {risk:.4f}")
        print(f"  exit_delta       = {exit_delta:.4f}")
        print(f"  R via pnl/risk   = {float(t.realized_pnl)/risk:+.4f}  <-- correct R-multiple")
        print(f"  R via pnl/exit   = {(float(t.realized_pnl)/exit_delta) if exit_delta else 0:+.4f}  <-- metrics.py")

print("\n===== 3 SAMPLE LOSING IS TRADES =====")
for i, t in enumerate(losers[:3]):
    c = find_cap(t)
    print(f"\n--- Loser #{i+1} ---")
    print(f"  entry_price      = {t.entry_price}    stop@arm={c and c['stop_at_arm']}")
    print(f"  actual_exit      = {t.exit_price}    reason={t.exit_reason}")
    print(f"  realized_pnl     = {t.realized_pnl}")
    if c:
        risk = c["risk"]
        exit_delta = abs(float(t.entry_price) - float(t.exit_price))
        print(f"  risk_amount      = {risk:.4f}    exit_delta={exit_delta:.4f}")
        print(f"  R via pnl/risk   = {float(t.realized_pnl)/risk:+.4f}")
        print(f"  R via pnl/exit   = {(float(t.realized_pnl)/exit_delta) if exit_delta else 0:+.4f}")
