#!/usr/bin/env python3
"""CHECK 3.5 — Apex viability re-test with V_A params and 5 sizing scenarios.

Uses Variant A: cisd_lookback=15, ifvg_tolerance=2, rr_cap=1.1 (best per Check 2).
Replays once at NQ $750 risk to harvest trades; then post-processes each trade
for 5 sizing scenarios and runs rolling 30-day Apex simulations on each.

Per-trade post-processing math
------------------------------
From each trade we know: entry_price, exit_price, stop_price, side, qty_replay,
realized_pnl. The per-NQ-contract dollar pnl (independent of qty) is
    per_nq_pnl = realized_pnl / qty_replay
which equals (exit-entry)*sign(side) * NQ_POINT_VALUE.

For a scenario with N contracts of instrument I (point_value PV):
    gross  = per_nq_pnl * N * (PV / NQ_POINT_VALUE)
    costs  = N * (COMM_PER_SIDE * 2 + SLIP_TICKS_PER_SIDE * 2 * TICK_VALUE_I)
    net    = gross - costs

Apex rules: WIN at +$3000 before -$2000 trailing DD; FAIL on -$2000 DD;
else NEITHER after 30 calendar days.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.CRITICAL)
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

REPO = Path(__file__).resolve().parents[1]
REPLAY_START, REPLAY_END = date(2022, 1, 1), date(2025, 4, 30)

# Apex 50k
STARTING_EQUITY = 50_000.0
PROFIT_TARGET = 3_000.0
TRAILING_DD_LIMIT = 2_000.0
WINDOW_DAYS = 30

# Instrument constants
NQ_POINT_VALUE = 20.0
NQ_TICK_VALUE = 5.0
MNQ_POINT_VALUE = 2.0
MNQ_TICK_VALUE = 0.50

COMM_PER_SIDE = 4.50
SLIP_TICKS_PER_SIDE = 1

# MockBroker already deducts $4.50 per contract from realized_pnl at exit
# (treats it as the full round-trip cost). To compute GROSS per-NQ-contract
# pnl we need to add that back, then apply the user-spec'd per-side commissions.
BROKER_DEDUCTED_COMM_PER_NQ_CONTRACT = 4.50

# Scenarios: name, instrument, qty, risk_budget, reject_if_risk_over_budget
SCENARIOS = [
    ("S1_1MNQ_r250",  "MNQ", 1, 250.0, False),
    ("S2_2MNQ_r500",  "MNQ", 2, 500.0, False),
    ("S3_3MNQ_r750",  "MNQ", 3, 750.0, False),
    ("S4_1NQ_r500",   "NQ",  1, 500.0, True),
    ("S5_1NQ_r750",   "NQ",  1, 750.0, True),
]


def replay_va_trades():
    nq = BacktestRunner.load_bars_from_dbn(
        REPO / "data/historical/NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    es = BacktestRunner.load_bars_from_dbn(
        REPO / "data/historical/ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
    )
    nq = [b for b in nq if REPLAY_START <= b.ts.date() <= REPLAY_END]

    cfg = load_strategy_config(REPO / "config/strategy.yaml")
    inst = load_instruments_config(REPO / "config/instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal("750")
    cfg["entry_order_type"] = "LIMIT"
    cfg["entry_slippage_ticks"] = 0
    cfg.setdefault("default_qty", Decimal("1"))
    # V_A parameters
    cfg["ifvg_tolerance_ticks"] = 2
    cfg["rr_cap"] = Decimal("1.1")
    cfg["cisd_lookback_bars"] = 15

    pv = Decimal(str(getattr(inst, "point_value", 1)))
    ledger = AccountLedger(session_start_equity=Decimal("50000"), today=nq[0].ts.date())
    broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"), point_value=pv)
    runner = BacktestRunner(
        bars_primary=nq, bars_correlated=es, mock_broker=broker, ledger=ledger,
        strategy_cfg=cfg, instrument_cfg=inst, param_set_hash="check35_va",
    )
    return runner.run().trades, nq


def trade_metrics(t):
    """Return (per_nq_contract_GROSS_pnl, stop_distance_pts).

    Re-adds the broker's $4.50/contract commission deduction so callers can
    apply scenario-specific commission models without double-counting.
    """
    qty = float(t.qty) if t.qty else 1.0
    qty = max(qty, 1.0)
    per_nq_net = float(t.realized_pnl) / qty
    per_nq_gross = per_nq_net + BROKER_DEDUCTED_COMM_PER_NQ_CONTRACT
    if t.stop_price is None:
        stop_dist = 0.0
    else:
        stop_dist = abs(float(t.entry_price) - float(t.stop_price))
    return per_nq_gross, stop_dist


def scenario_pnl(t, instrument: str, qty: int) -> float:
    """Net dollar pnl for `qty` contracts of `instrument` on this trade."""
    per_nq, _ = trade_metrics(t)
    if instrument == "NQ":
        pv = NQ_POINT_VALUE
        tick_val = NQ_TICK_VALUE
    else:
        pv = MNQ_POINT_VALUE
        tick_val = MNQ_TICK_VALUE
    gross = per_nq * qty * (pv / NQ_POINT_VALUE)
    costs = qty * (COMM_PER_SIDE * 2 + SLIP_TICKS_PER_SIDE * 2 * tick_val)
    return gross - costs


def trade_is_eligible(t, instrument: str, qty: int, budget: float, reject_overrisk: bool) -> bool:
    """Whether the trade fits the scenario's risk budget at the given qty."""
    if not reject_overrisk:
        return True
    _, stop_dist = trade_metrics(t)
    pv = NQ_POINT_VALUE if instrument == "NQ" else MNQ_POINT_VALUE
    risk_required = stop_dist * pv * qty
    return risk_required <= budget


def simulate_window(trades, start: date, end: date) -> dict:
    """One 30-day Apex simulation; trades already filtered + pnl-scored."""
    cumulative, peak, worst_dd = 0.0, 0.0, 0.0
    result = "NEITHER"
    days_to_win = None
    trades_taken = 0
    in_win = [t for t in trades if start <= t["entry_date"] <= end]
    for t in in_win:
        cumulative += t["net_pnl"]
        peak = max(peak, cumulative)
        dd = peak - cumulative
        worst_dd = max(worst_dd, dd)
        trades_taken += 1
        if dd >= TRAILING_DD_LIMIT:
            result = "FAIL"
            break
        if cumulative >= PROFIT_TARGET:
            result = "WIN"
            days_to_win = (t["exit_date"] - start).days
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
    y, m = first.year, first.month
    while True:
        d = date(y, m, 1)
        if d > last:
            break
        yield d
        m += 1
        if m == 13:
            m, y = 1, y + 1


def main() -> int:
    print("Replay with V_A params (cisd_lookback=15)...")
    trades, nq = replay_va_trades()
    print(f"  V_A trades: {len(trades)}")

    # Trade-level analytics
    stop_dists = []
    for t in trades:
        _, sd = trade_metrics(t)
        stop_dists.append(sd)
    avg_stop = sum(stop_dists) / len(stop_dists) if stop_dists else 0.0
    rejected_at_12_5 = sum(1 for sd in stop_dists if sd > 12.5)
    rejected_at_25_0 = sum(1 for sd in stop_dists if sd > 25.0)
    rejected_at_37_5 = sum(1 for sd in stop_dists if sd > 37.5)

    # Trade frequency per month (over 40 months of replay = 2022-01..2025-04)
    months = (REPLAY_END.year - REPLAY_START.year) * 12 + (REPLAY_END.month - REPLAY_START.month) + 1
    trades_per_month = len(trades) / months

    print()
    print("=== Trade-level analytics (V_A replay) ===")
    print(f"  Avg trade frequency        : {trades_per_month:.2f} trades/month "
          f"({len(trades)} over {months} months)")
    print(f"  Avg stop distance          : {avg_stop:.2f} NQ pts")
    print(f"  Trades rejected at 12.5pts : {rejected_at_12_5}/{len(trades)} "
          f"({100*rejected_at_12_5/len(trades):.1f}%) <- 1 NQ @ $250 risk cap")
    print(f"  Trades rejected at 25.0pts : {rejected_at_25_0}/{len(trades)} "
          f"({100*rejected_at_25_0/len(trades):.1f}%) <- 1 NQ @ $500 risk cap")
    print(f"  Trades rejected at 37.5pts : {rejected_at_37_5}/{len(trades)} "
          f"({100*rejected_at_37_5/len(trades):.1f}%) <- 1 NQ @ $750 risk cap")

    # Build per-scenario trade lists (eligibility + scaled pnl)
    scenario_results: dict[str, dict] = {}
    print()
    print(f"{'scenario':<16} {'eligible':>9} {'avg_net':>9} "
          f"{'wins':>5} {'fails':>6} {'neither':>8} {'win%':>6} "
          f"{'avg_days':>9} {'worst_dd':>9}")
    print("-" * 105)

    for tag, instrument, qty, budget, reject in SCENARIOS:
        eligible_records = []
        for t in trades:
            if not trade_is_eligible(t, instrument, qty, budget, reject):
                continue
            net = scenario_pnl(t, instrument, qty)
            eligible_records.append({
                "entry_date": t.entry_ts.date(),
                "exit_date": t.exit_ts.date(),
                "net_pnl": net,
            })
        avg_net = (sum(r["net_pnl"] for r in eligible_records) / len(eligible_records)) if eligible_records else 0.0

        sims = []
        for start in month_starts(date(2022, 2, 1), date(2025, 3, 1)):
            end = start + timedelta(days=WINDOW_DAYS - 1)
            sims.append(simulate_window(eligible_records, start, end))

        wins = [s for s in sims if s["result"] == "WIN"]
        fails = [s for s in sims if s["result"] == "FAIL"]
        neither = [s for s in sims if s["result"] == "NEITHER"]
        win_rate = len(wins) / len(sims) * 100 if sims else 0
        fail_rate = len(fails) / len(sims) * 100 if sims else 0
        neither_rate = len(neither) / len(sims) * 100 if sims else 0
        best_window = max(sims, key=lambda s: s["peak"]) if sims else None
        avg_days_to_win = (
            sum(s["days_to_win"] for s in wins) / len(wins)
        ) if wins else None
        worst_dd = max((s["worst_dd"] for s in sims), default=0.0)

        print(f"{tag:<16} {len(eligible_records):>9} {avg_net:>9.2f} "
              f"{len(wins):>5} {len(fails):>6} {len(neither):>8} {win_rate:>5.1f}% "
              f"{(avg_days_to_win if avg_days_to_win is not None else 0):>9.1f} "
              f"{worst_dd:>9.0f}")

        if win_rate > 60:
            verdict = "VIABLE (>60%)"
        elif win_rate >= 40:
            verdict = "MARGINAL (40-60%)"
        else:
            verdict = "NOT VIABLE (<40%)"

        scenario_results[tag] = {
            "instrument": instrument,
            "qty": qty,
            "risk_budget": budget,
            "reject_overrisk": reject,
            "eligible_trades": len(eligible_records),
            "avg_net_pnl_per_trade": round(avg_net, 2),
            "windows": len(sims),
            "wins": len(wins),
            "fails": len(fails),
            "neither": len(neither),
            "win_rate_pct": round(win_rate, 2),
            "fail_rate_pct": round(fail_rate, 2),
            "neither_rate_pct": round(neither_rate, 2),
            "avg_days_to_win": avg_days_to_win,
            "worst_dd_observed": worst_dd,
            "best_window": best_window,
            "verdict": verdict,
        }

    print()
    print("=== Decision per scenario ===")
    for tag, res in scenario_results.items():
        print(f"  {tag:<16}: WR={res['win_rate_pct']:5.1f}%  "
              f"FR={res['fail_rate_pct']:5.1f}%  avg_net=${res['avg_net_pnl_per_trade']:+7.2f}/trade  "
              f"-> {res['verdict']}")

    payload = {
        "params": {"cisd_lookback_bars": 15, "ifvg_tolerance_ticks": 2, "rr_cap": "1.1"},
        "replay": {
            "trades_total": len(trades),
            "months_covered": months,
            "trades_per_month": round(trades_per_month, 2),
            "avg_stop_distance_pts": round(avg_stop, 2),
            "rejected_at_12_5_pts": rejected_at_12_5,
            "rejected_at_25_0_pts": rejected_at_25_0,
            "rejected_at_37_5_pts": rejected_at_37_5,
        },
        "scenarios": scenario_results,
    }
    out = REPO / "results" / "phase4_check35_apex_sizing.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved: {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
