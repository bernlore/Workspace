#!/usr/bin/env python3
"""CHECK 3 — Apex 50k Challenge: rolling 30-day window simulation.

Simulates the Apex Trader Funded 50k Evaluation across every monthly start
from 2022-02 through 2025-03 (rolling 30 calendar-day windows).

Sizing model: 1 MNQ contract per trade (point_value=$2, vs NQ=$20). So per
trade we take the NQ replay trade's per-contract dollar pnl and divide by 10.
Commissions: $4.50 per contract per side = $9.00 round-trip.
Slippage:    1 tick per side = 2 ticks total; MNQ tick value=$0.50 -> $1.00.
Net per-trade pnl applied to account: per_mnq_pnl - $10.

Apex rules (simplified for one-shot challenge):
  WIN  = cumulative pnl reaches +$3,000 before trailing-DD hit
  FAIL = trailing drawdown (peak_pnl - cumulative_pnl) reaches $2,000
  NEITHER = neither condition met by day 30

Reports:
  - Total windows simulated
  - Win rate / Fail rate
  - Avg days to win
  - Worst trailing DD observed
  - Verdict: <40% / 40-60% / >60%
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.CRITICAL)
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

REPO = Path(__file__).resolve().parents[1]
REPLAY_START, REPLAY_END = date(2022, 1, 1), date(2025, 4, 30)

# Apex 50k constants
STARTING_EQUITY = 50_000.0
PROFIT_TARGET = 3_000.0
TRAILING_DD_LIMIT = 2_000.0
WINDOW_DAYS = 30

# Sizing constants (MNQ)
NQ_POINT_VALUE = 20.0
MNQ_POINT_VALUE = 2.0
COMMISSION_PER_SIDE = 4.50
N_SIDES = 2  # entry + exit
MNQ_TICK_VALUE = 0.50
SLIPPAGE_TICKS_PER_SIDE = 1


def replay_trades():
    nq = BacktestRunner.load_bars_from_dbn(
        REPO/"data/historical/NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    es = BacktestRunner.load_bars_from_dbn(
        REPO/"data/historical/ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
    )
    nq = [b for b in nq if REPLAY_START <= b.ts.date() <= REPLAY_END]

    cfg = load_strategy_config(REPO/"config/strategy.yaml")
    inst = load_instruments_config(REPO/"config/instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal("750")
    cfg["entry_order_type"] = "LIMIT"
    cfg["entry_slippage_ticks"] = 0
    cfg.setdefault("default_qty", Decimal("1"))
    cfg["ifvg_tolerance_ticks"] = 2
    cfg["rr_cap"] = Decimal("1.1")
    cfg["cisd_lookback_bars"] = 20

    pv = Decimal(str(getattr(inst, "point_value", 1)))
    ledger = AccountLedger(session_start_equity=Decimal("50000"), today=nq[0].ts.date())
    broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"), point_value=pv)
    runner = BacktestRunner(
        bars_primary=nq, bars_correlated=es, mock_broker=broker, ledger=ledger,
        strategy_cfg=cfg, instrument_cfg=inst, param_set_hash="check3_apex",
    )
    res = runner.run()
    return res.trades


def mnq_net_pnl(t) -> float:
    """Convert NQ replay trade to net MNQ-contract pnl after costs."""
    # Per-NQ-contract dollar pnl from the replay.
    qty = float(t.qty) if t.qty else 1.0
    if qty == 0:
        return 0.0
    per_nq_contract_pnl = float(t.realized_pnl) / qty
    per_mnq_pnl = per_nq_contract_pnl * (MNQ_POINT_VALUE / NQ_POINT_VALUE)
    costs = (
        COMMISSION_PER_SIDE * N_SIDES
        + SLIPPAGE_TICKS_PER_SIDE * N_SIDES * MNQ_TICK_VALUE
    )
    return per_mnq_pnl - costs


def simulate_window(trades, start: date, end: date) -> dict:
    """Run one rolling-30-day Apex simulation."""
    cumulative = 0.0
    peak = 0.0
    worst_dd = 0.0
    result = "NEITHER"
    days_to_win = None
    win_day = None
    trades_taken = 0

    in_window = [t for t in trades if start <= t.entry_ts.date() <= end]
    for t in in_window:
        net = mnq_net_pnl(t)
        cumulative += net
        peak = max(peak, cumulative)
        dd = peak - cumulative
        worst_dd = max(worst_dd, dd)
        trades_taken += 1
        if dd >= TRAILING_DD_LIMIT:
            result = "FAIL"
            break
        if cumulative >= PROFIT_TARGET:
            result = "WIN"
            win_day = t.exit_ts.date()
            days_to_win = (win_day - start).days
            break

    return {
        "start": str(start),
        "end": str(end),
        "result": result,
        "trades_taken": trades_taken,
        "cumulative_pnl": round(cumulative, 2),
        "peak": round(peak, 2),
        "worst_dd": round(worst_dd, 2),
        "days_to_win": days_to_win,
    }


def month_starts(first: date, last: date):
    """Yield first-of-month dates in [first, last]."""
    y, m = first.year, first.month
    while True:
        d = date(y, m, 1)
        if d > last:
            break
        yield d
        m += 1
        if m == 13:
            m = 1
            y += 1


def main() -> int:
    print("Replaying NQ/ES to get trade list...")
    trades = replay_trades()
    print(f"  Trades available: {len(trades)}")

    results: list[dict] = []
    for start in month_starts(date(2022, 2, 1), date(2025, 3, 1)):
        end = start + timedelta(days=WINDOW_DAYS - 1)
        sim = simulate_window(trades, start, end)
        results.append(sim)

    n = len(results)
    wins = [r for r in results if r["result"] == "WIN"]
    fails = [r for r in results if r["result"] == "FAIL"]
    neither = [r for r in results if r["result"] == "NEITHER"]
    win_rate = len(wins) / n * 100
    fail_rate = len(fails) / n * 100
    neither_rate = len(neither) / n * 100
    avg_days_to_win = (
        sum(r["days_to_win"] for r in wins) / len(wins)
    ) if wins else None
    worst_dd_observed = max((r["worst_dd"] for r in results), default=0.0)

    print()
    print(f"{'window_start':<13} {'result':<7} {'trades':>6} {'cum_pnl':>9} "
          f"{'peak':>7} {'worst_dd':>9} {'days':>6}")
    print("-" * 70)
    for r in results:
        print(f"{r['start']:<13} {r['result']:<7} {r['trades_taken']:>6} "
              f"{r['cumulative_pnl']:>9.0f} {r['peak']:>7.0f} "
              f"{r['worst_dd']:>9.0f} {str(r['days_to_win'] or '-'):>6}")
    print("-" * 70)
    print()
    print(f"Total windows simulated     : {n}")
    print(f"WIN  ({PROFIT_TARGET:.0f} before -{TRAILING_DD_LIMIT:.0f} DD): "
          f"{len(wins)}  ({win_rate:.1f}%)")
    print(f"FAIL (trailing DD hit first): {len(fails)}  ({fail_rate:.1f}%)")
    print(f"NEITHER (30d elapsed)       : {len(neither)}  ({neither_rate:.1f}%)")
    if avg_days_to_win is not None:
        print(f"Avg days to win when winning: {avg_days_to_win:.1f}")
    else:
        print("Avg days to win when winning: n/a (no wins)")
    print(f"Worst trailing DD observed  : ${worst_dd_observed:.0f}")

    print()
    if win_rate < 40:
        verdict = "NOT VIABLE for one-shot $29 Apex challenge (win rate < 40%)."
    elif win_rate < 60:
        verdict = "MARGINAL — paper trading required before one-shot real challenge."
    else:
        verdict = "REAL SHOT at one-shot success (win rate > 60%)."
    print(f"VERDICT: {verdict}")

    out = REPO/"results"/"phase4_check3_apex_rolling.json"
    out.write_text(json.dumps({
        "config": {
            "starting_equity": STARTING_EQUITY,
            "profit_target": PROFIT_TARGET,
            "trailing_dd_limit": TRAILING_DD_LIMIT,
            "window_days": WINDOW_DAYS,
            "commission_per_side": COMMISSION_PER_SIDE,
            "slippage_ticks_per_side": SLIPPAGE_TICKS_PER_SIDE,
            "mnq_point_value": MNQ_POINT_VALUE,
        },
        "summary": {
            "windows": n,
            "wins": len(wins),
            "fails": len(fails),
            "neither": len(neither),
            "win_rate_pct": round(win_rate, 2),
            "fail_rate_pct": round(fail_rate, 2),
            "avg_days_to_win": avg_days_to_win,
            "worst_dd_observed": worst_dd_observed,
            "verdict": verdict,
        },
        "windows": results,
    }, indent=2))
    print(f"Saved: {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
