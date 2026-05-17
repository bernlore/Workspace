#!/usr/bin/env python3
"""Final viability test — V_A params + Tradeify SELECT rules.

PART 1 — Net edge per trade (full replay 2022-01-01..2025-04-25, V_A params).
PART 2 — Tradeify SELECT simulation, NO time limit (capped 365d), 3 sizings.
PART 3 — Time-budget reality check: outcomes capped at 90 / 180 / 365 days.

Tradeify SELECT rules modelled:
  start $50,000 · target +$3,000 · $2,000 EOD trailing drawdown ·
  no daily loss limit · 40% consistency rule (best single day must be
  < 40% of total profit at payout, else flagged).

EOD trailing DD: the drawdown trails END-OF-DAY balance only. Intraday
dips do not fail the account; the peak and the DD check both happen at EOD.

Sizing: fixed N NQ contracts (avg stop ~4.34 pts ~= $87 risk per 1 NQ).
Per-trade net pnl for N contracts:
    net = N * (per_nq_gross_pnl - COMMISSION_RT - SLIPPAGE_RT)
where per_nq_gross re-adds the broker's in-sim $4.50/contract deduction.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import Counter, defaultdict
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
REPLAY_START, REPLAY_END = date(2022, 1, 1), date(2025, 4, 25)

# Tradeify SELECT 50k
STARTING_EQUITY = 50_000.0
PROFIT_TARGET = 3_000.0
TRAILING_DD_LIMIT = 2_000.0
CONSISTENCY_MAX_DAY_FRAC = 0.40

# Cost model (per Apex/Tradeify spec): $4.50/side commission, 1 tick/side slippage.
NQ_TICK_VALUE = 5.0
COMMISSION_RT = 4.50 * 2          # $9 round-trip per NQ contract
SLIPPAGE_RT = 1 * 2 * NQ_TICK_VALUE  # 1 tick/side -> $10 round-trip per NQ
BROKER_DEDUCTED_COMM = 4.50       # mock_broker deducts this per contract at exit

WINDOW_STARTS_FIRST = date(2022, 2, 1)
WINDOW_STARTS_LAST = date(2024, 12, 1)
SIZINGS = [1, 2, 3]  # NQ contracts
TIME_CAPS = [90, 180, 365]


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
    cfg["ifvg_tolerance_ticks"] = 2
    cfg["rr_cap"] = Decimal("1.1")
    cfg["cisd_lookback_bars"] = 15

    pv = Decimal(str(getattr(inst, "point_value", 1)))
    ledger = AccountLedger(session_start_equity=Decimal("50000"), today=nq[0].ts.date())
    broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"), point_value=pv)
    runner = BacktestRunner(
        bars_primary=nq, bars_correlated=es, mock_broker=broker, ledger=ledger,
        strategy_cfg=cfg, instrument_cfg=inst, param_set_hash="tradeify_final",
    )
    return runner.run().trades


def per_nq_gross(t) -> float:
    """Gross per-NQ-contract dollar pnl (broker commission re-added)."""
    qty = max(float(t.qty) if t.qty else 1.0, 1.0)
    return float(t.realized_pnl) / qty + BROKER_DEDUCTED_COMM


def net_pnl_1nq(t) -> float:
    """Net pnl for 1 NQ contract after spec commission + slippage."""
    return per_nq_gross(t) - COMMISSION_RT - SLIPPAGE_RT


# ---------------------------------------------------------------------------
# PART 1
# ---------------------------------------------------------------------------

def part1(trades) -> dict:
    grosses = [per_nq_gross(t) for t in trades]
    nets = [net_pnl_1nq(t) for t in trades]
    n = len(trades)
    profitable = sum(1 for x in nets if x > 0)

    # Histogram bins for 1-NQ net pnl
    bins = [
        ("< -200", lambda x: x < -200),
        ("-200..-100", lambda x: -200 <= x < -100),
        ("-100..0", lambda x: -100 <= x < 0),
        ("0..100", lambda x: 0 <= x < 100),
        ("100..200", lambda x: 100 <= x < 200),
        ("200..300", lambda x: 200 <= x < 300),
        (">= 300", lambda x: x >= 300),
    ]
    hist = {label: sum(1 for x in nets if pred(x)) for label, pred in bins}

    return {
        "total_trades": n,
        "avg_gross_pnl_per_trade": round(sum(grosses) / n, 2),
        "commission_cost_rt": COMMISSION_RT,
        "slippage_cost_rt_1nq": SLIPPAGE_RT,
        "avg_net_pnl_per_trade_1nq": round(sum(nets) / n, 2),
        "median_net_pnl_per_trade_1nq": round(statistics.median(nets), 2),
        "pct_profitable_trades": round(100 * profitable / n, 2),
        "net_pnl_histogram_1nq": hist,
        "total_net_pnl_1nq_full_replay": round(sum(nets), 2),
    }


# ---------------------------------------------------------------------------
# PART 2 / 3 — Tradeify SELECT simulation
# ---------------------------------------------------------------------------

def simulate(trades_sorted, start: date, n_contracts: int, day_cap: int) -> dict:
    """Run a Tradeify SELECT sim from `start`, capped at `day_cap` days.

    Returns result dict with outcome WIN / FAIL / TIMEOUT and diagnostics.
    """
    end_limit = start + timedelta(days=day_cap)
    cumulative = 0.0
    peak_eod = STARTING_EQUITY
    daily_pnl: dict[date, float] = defaultdict(float)
    result = "TIMEOUT"
    win_date = None
    fail_date = None
    last_date = None

    # Group trades by NY date within window
    window_trades = [
        t for t in trades_sorted
        if start <= t.entry_ts.date() < end_limit
    ]
    by_day: dict[date, list] = defaultdict(list)
    for t in window_trades:
        by_day[t.entry_ts.date()].append(t)

    for day in sorted(by_day):
        last_date = day
        for t in by_day[day]:
            net = net_pnl_1nq(t) * n_contracts
            cumulative += net
            daily_pnl[day] += net
            if cumulative >= PROFIT_TARGET:
                result = "WIN"
                win_date = day
                break
        if result == "WIN":
            break
        # EOD trailing-DD check
        eod_balance = STARTING_EQUITY + cumulative
        peak_eod = max(peak_eod, eod_balance)
        if peak_eod - eod_balance >= TRAILING_DD_LIMIT:
            result = "FAIL"
            fail_date = day
            break

    # Consistency rule (only meaningful on WIN)
    consistency_violation = False
    best_day_pnl = max(daily_pnl.values()) if daily_pnl else 0.0
    if result == "WIN" and cumulative > 0:
        consistency_violation = best_day_pnl > CONSISTENCY_MAX_DAY_FRAC * cumulative

    days_to_outcome = None
    if win_date:
        days_to_outcome = (win_date - start).days
    elif fail_date:
        days_to_outcome = (fail_date - start).days

    return {
        "start": str(start),
        "result": result,
        "n_contracts": n_contracts,
        "day_cap": day_cap,
        "final_pnl": round(cumulative, 2),
        "days_to_outcome": days_to_outcome,
        "best_day_pnl": round(best_day_pnl, 2),
        "consistency_violation": consistency_violation,
        "trades_taken": len(window_trades) if result != "WIN" else None,
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


def summarize(sims: list[dict]) -> dict:
    n = len(sims)
    wins = [s for s in sims if s["result"] == "WIN"]
    fails = [s for s in sims if s["result"] == "FAIL"]
    timeouts = [s for s in sims if s["result"] == "TIMEOUT"]
    win_days = [s["days_to_outcome"] for s in wins if s["days_to_outcome"] is not None]
    timeout_pnls = [s["final_pnl"] for s in timeouts]
    worst = min((s["final_pnl"] for s in sims), default=0.0)
    consistency_flags = sum(1 for s in wins if s["consistency_violation"])
    return {
        "windows": n,
        "wins": len(wins),
        "fails": len(fails),
        "timeouts": len(timeouts),
        "win_rate_pct": round(100 * len(wins) / n, 2) if n else 0,
        "fail_rate_pct": round(100 * len(fails) / n, 2) if n else 0,
        "timeout_rate_pct": round(100 * len(timeouts) / n, 2) if n else 0,
        "avg_days_to_win": round(sum(win_days) / len(win_days), 1) if win_days else None,
        "median_timeout_final_pnl": round(statistics.median(timeout_pnls), 2) if timeout_pnls else None,
        "worst_final_pnl": round(worst, 2),
        "consistency_violations_among_wins": consistency_flags,
    }


def main() -> int:
    print("Replaying V_A (cisd_lookback=15)...")
    trades = replay_va_trades()
    trades_sorted = sorted(trades, key=lambda t: t.entry_ts)
    print(f"  V_A trades: {len(trades)}")

    # ---- PART 1 ----
    p1 = part1(trades)
    print()
    print("=" * 70)
    print("PART 1 — Net edge per trade (1 NQ contract)")
    print("=" * 70)
    print(f"  Total trades              : {p1['total_trades']}")
    print(f"  Avg gross pnl / trade     : ${p1['avg_gross_pnl_per_trade']:+.2f}")
    print(f"  Commission cost (RT)      : ${p1['commission_cost_rt']:.2f}")
    print(f"  Slippage cost (RT, 1 NQ)  : ${p1['slippage_cost_rt_1nq']:.2f}")
    print(f"  Avg NET pnl / trade       : ${p1['avg_net_pnl_per_trade_1nq']:+.2f}")
    print(f"  Median NET pnl / trade    : ${p1['median_net_pnl_per_trade_1nq']:+.2f}")
    print(f"  % profitable trades       : {p1['pct_profitable_trades']:.1f}%")
    print(f"  Total NET pnl full replay : ${p1['total_net_pnl_1nq_full_replay']:+.2f}")
    print(f"  Net pnl histogram (1 NQ):")
    for label, count in p1["net_pnl_histogram_1nq"].items():
        bar = "#" * (count * 50 // max(p1["net_pnl_histogram_1nq"].values()))
        print(f"    {label:>12}: {count:>4}  {bar}")

    edge_positive = p1["avg_net_pnl_per_trade_1nq"] > 0
    print()
    if edge_positive:
        print(f"  EDGE: avg net ${p1['avg_net_pnl_per_trade_1nq']:+.2f}/trade > $0 "
              f"-> positive net edge exists.")
    else:
        print(f"  EDGE: avg net ${p1['avg_net_pnl_per_trade_1nq']:+.2f}/trade <= $0 "
              f"-> NO positive net edge. Apex/Tradeify path effectively impossible "
              f"regardless of time limit.")

    # ---- PART 2 — no time limit (365d cap) ----
    print()
    print("=" * 70)
    print("PART 2 — Tradeify SELECT, NO time limit (365-day cap)")
    print("=" * 70)
    part2 = {}
    for n_ct in SIZINGS:
        sims = [
            simulate(trades_sorted, s, n_ct, 365)
            for s in month_starts(WINDOW_STARTS_FIRST, WINDOW_STARTS_LAST)
        ]
        summ = summarize(sims)
        part2[f"{n_ct}_NQ"] = {"summary": summ, "windows": sims}
        print(f"\n  {n_ct} NQ contract(s) — risk ~${87*n_ct}/trade:")
        print(f"    Windows={summ['windows']}  WIN={summ['wins']} ({summ['win_rate_pct']}%)  "
              f"FAIL={summ['fails']} ({summ['fail_rate_pct']}%)  "
              f"TIMEOUT={summ['timeouts']} ({summ['timeout_rate_pct']}%)")
        print(f"    Avg days to win        : {summ['avg_days_to_win']}")
        print(f"    Median timeout pnl     : {summ['median_timeout_final_pnl']}")
        print(f"    Worst final pnl        : ${summ['worst_final_pnl']:.0f}")
        print(f"    Consistency violations : {summ['consistency_violations_among_wins']} of {summ['wins']} wins")

    # ---- PART 3 — time-budget reality check ----
    print()
    print("=" * 70)
    print("PART 3 — Time-budget reality check (90 / 180 / 365 day caps)")
    print("=" * 70)
    part3 = {}
    for n_ct in SIZINGS:
        part3[f"{n_ct}_NQ"] = {}
        print(f"\n  {n_ct} NQ contract(s):")
        print(f"    {'cap':>6}  {'WIN%':>6}  {'FAIL%':>6}  {'TIMEOUT%':>9}  "
              f"{'avg_days':>9}  {'worst$':>8}")
        for cap in TIME_CAPS:
            sims = [
                simulate(trades_sorted, s, n_ct, cap)
                for s in month_starts(WINDOW_STARTS_FIRST, WINDOW_STARTS_LAST)
            ]
            summ = summarize(sims)
            part3[f"{n_ct}_NQ"][f"{cap}d"] = summ
            print(f"    {cap:>5}d  {summ['win_rate_pct']:>5.1f}%  "
                  f"{summ['fail_rate_pct']:>5.1f}%  {summ['timeout_rate_pct']:>8.1f}%  "
                  f"{str(summ['avg_days_to_win']):>9}  {summ['worst_final_pnl']:>8.0f}")

    # ---- save ----
    payload = {
        "params": {"cisd_lookback_bars": 15, "ifvg_tolerance_ticks": 2, "rr_cap": "1.1"},
        "cost_model": {
            "commission_rt_per_nq": COMMISSION_RT,
            "slippage_rt_per_nq": SLIPPAGE_RT,
        },
        "tradeify_select_rules": {
            "starting_equity": STARTING_EQUITY,
            "profit_target": PROFIT_TARGET,
            "trailing_dd_limit": TRAILING_DD_LIMIT,
            "trailing_dd_type": "EOD",
            "consistency_max_day_fraction": CONSISTENCY_MAX_DAY_FRAC,
        },
        "part1_net_edge": p1,
        "part2_no_time_limit": {k: v["summary"] for k, v in part2.items()},
        "part3_time_budget": part3,
        "data_note": (
            "Replay data ends 2025-04-25. Windows starting in late 2024 have "
            "< 365 days of trade data available; their 365-day cap is "
            "effectively truncated by data availability."
        ),
    }
    out = REPO / "results" / "phase4_tradeify_va_final.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved: {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
