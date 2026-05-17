#!/usr/bin/env python3
"""Phase 4 Steps 2-4 — ORB walk-forward + Tradeify sim + robustness checks.

Runs in one pass:
  STEP 2  three-split walk-forward (OOS A/B/C + aggregate), per-split metrics
  CHECK A bootstrap 95% CI on OOS net-R per trade
  CHECK B null-model comparison (random entry within 30 min of the signal)
  STEP 3  Tradeify SELECT rolling simulation (90 / 180 / 365-day caps)
  VERDICT mechanical against the locked decision criteria

Outputs results/phase4_orb_tradeify_sim.json and
results/phase4_orb_vs_nasdaqale.json.
"""
from __future__ import annotations

import json
import logging
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.cost_model import load_cost_model
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config
from nasdaq_ale_bot.strategies.orb import load_orb_config
from nasdaq_ale_bot.strategies.orb.state_machine import (
    OrbStateMachine,
    compute_stop_target,
)

logging.basicConfig(level=logging.CRITICAL)
import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

REPO = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
REPLAY_START, REPLAY_END = date(2022, 1, 1), date(2025, 4, 25)

POINT_VALUE = 20.0
TICK = 0.25
SLIP = 0.25                       # 1 tick of slippage per side
COMMISSION_RT = 9.00              # $4.50/side x 2
BUFFER = 2 * TICK                 # 2-tick stop buffer
MAX_STOP = 50.0
RR = 1.5
BREAKEVEN_WR = 1.0 / (1.0 + RR)   # 0.40 for R:R 1:1.5
FORCE_FLAT = time(15, 45)

OOS_SPLITS = {
    "A": (date(2023, 10, 1), date(2024, 2, 29)),
    "B": (date(2024, 3, 1), date(2024, 6, 30)),
    "C": (date(2024, 10, 1), date(2025, 4, 25)),
}

# Tradeify SELECT 50k
STARTING_EQUITY = 50_000.0
PROFIT_TARGET = 3_000.0
TRAILING_DD = 2_000.0
CONSISTENCY_FRAC = 0.40
WINDOW_FIRST, WINDOW_LAST = date(2022, 2, 1), date(2024, 12, 1)
TIME_CAPS = [90, 180, 365]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def run_orb():
    """Replay ORB over the full window. Returns (trades, day_bars)."""
    nq = BacktestRunner.load_bars_from_dbn(
        REPO / "data/historical/NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    nq = [b for b in nq if REPLAY_START <= b.ts.astimezone(NY).date() <= REPLAY_END]
    orb_cfg = load_orb_config(REPO / "config/orb_strategy.yaml")
    cost_model = load_cost_model(REPO / "config/cost_model.yaml", "nq")
    inst = load_instruments_config(REPO / "config/instruments.yaml").primary
    ledger = AccountLedger(session_start_equity=Decimal("50000"), today=nq[0].ts.date())
    broker = MockBroker(
        ledger=ledger, initial_equity=Decimal("50000"),
        point_value=Decimal(str(getattr(inst, "point_value", 20.0))),
        cost_model=cost_model,
    )
    sm = OrbStateMachine(
        config=orb_cfg, broker=broker, ledger=ledger,
        tick_size=float(getattr(inst, "tick", 0.25)),
        point_value=float(getattr(inst, "point_value", 20.0)),
        cost_model=cost_model, symbol="NQ",
    )
    for b in nq:
        sm.on_bar(b)
    day_bars: dict[date, list] = defaultdict(list)
    for b in nq:
        day_bars[b.ts.astimezone(NY).date()].append(b)
    return sm.trades, dict(day_bars)


def in_oos(t, split: str) -> bool:
    a, b = OOS_SPLITS[split]
    return a <= t.entry_ts.astimezone(NY).date() <= b


# ---------------------------------------------------------------------------
# STEP 2 — per-split metrics
# ---------------------------------------------------------------------------

def real_net_r(t) -> float:
    """Net result of a real trade expressed in R-multiples (per contract)."""
    risk = abs(t.planned_entry_price - t.stop_price) * POINT_VALUE
    if risk <= 0:
        return 0.0
    return (t.net_pnl / t.qty) / risk


def split_metrics(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    nets = [t.net_pnl for t in trades]
    grosses = [t.gross_pnl for t in trades]
    wins = [x for x in nets if x > 0]
    gp = sum(x for x in nets if x > 0)
    gl = abs(sum(x for x in nets if x < 0))
    cum = peak = maxdd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        maxdd = max(maxdd, peak - cum)
    wr = len(wins) / n
    return {
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": round(wr, 4),
        "breakeven_wr": round(BREAKEVEN_WR, 4),
        "wr_over_breakeven": round(wr / BREAKEVEN_WR, 3),
        "avg_gross_per_trade": round(sum(grosses) / n, 2),
        "avg_net_per_trade": round(sum(nets) / n, 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        "total_net_pnl": round(sum(nets), 2),
        "max_drawdown": round(maxdd, 2),
        "best_trade": round(max(nets), 2),
        "worst_trade": round(min(nets), 2),
    }


def or_range_distribution(trades: list) -> list:
    bands = [(10, 25), (25, 40), (40, 55), (55, 70), (70, 80.001)]
    out = []
    for lo, hi in bands:
        sub = [t for t in trades if lo <= t.or_range < hi]
        label = f"{lo}-{int(hi)}"
        if sub:
            nets = [t.net_pnl for t in sub]
            w = sum(1 for x in nets if x > 0)
            out.append({
                "band_pts": label, "trades": len(sub),
                "win_rate": round(w / len(sub), 4),
                "avg_net": round(sum(nets) / len(sub), 2),
            })
        else:
            out.append({"band_pts": label, "trades": 0,
                        "win_rate": None, "avg_net": None})
    return out


# ---------------------------------------------------------------------------
# CHECK A — bootstrap CI
# ---------------------------------------------------------------------------

def bootstrap_ci(net_rs: list[float], iters: int = 10_000) -> dict:
    n = len(net_rs)
    if n < 2:
        return {"n": n, "mean_net_r": None, "ci_low": None, "ci_high": None,
                "ci_spans_zero": None}
    rng = random.Random(42)
    means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += net_rs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * iters)]
    hi = means[int(0.975 * iters)]
    return {
        "n": n,
        "mean_net_r": round(sum(net_rs) / n, 4),
        "ci_low": round(lo, 4),
        "ci_high": round(hi, 4),
        "ci_spans_zero": lo < 0.0 < hi,
    }


# ---------------------------------------------------------------------------
# CHECK B — null model
# ---------------------------------------------------------------------------

def simulate_outcome(day_list: list, entry_idx: int, or_high: float,
                     or_low: float, direction: str) -> float:
    """Simulate one ORB trade entered at day_list[entry_idx].open.

    Returns net-R per contract (slippage + commission applied).
    """
    entry = day_list[entry_idx].open
    stop_price, stop_dist, target_price = compute_stop_target(
        direction=direction, entry_price=entry, or_high=or_high, or_low=or_low,
        buffer=BUFFER, max_stop_points=MAX_STOP, rr_multiple=RR,
    )
    dirmul = 1.0 if direction == "LONG" else -1.0
    exit_price = None
    for j in range(entry_idx + 1, len(day_list)):
        b = day_list[j]
        if b.ts.astimezone(NY).time() >= FORCE_FLAT:
            exit_price = b.close
            break
        if direction == "LONG":
            if b.low <= stop_price:
                exit_price = stop_price
                break
            if b.high >= target_price:
                exit_price = target_price
                break
        else:
            if b.high >= stop_price:
                exit_price = stop_price
                break
            if b.low <= target_price:
                exit_price = target_price
                break
    if exit_price is None:
        exit_price = day_list[-1].close
    entry_fill = entry + SLIP * dirmul          # buy higher / sell lower
    exit_fill = exit_price - SLIP * dirmul      # close: sell lower / buy higher
    net = (exit_fill - entry_fill) * dirmul * POINT_VALUE - COMMISSION_RT
    return net / (stop_dist * POINT_VALUE) if stop_dist > 0 else 0.0


def null_model(trades: list, day_bars: dict, iters: int = 1_000) -> dict:
    """Compare real ORB entry timing against random entry within +30 min."""
    rng = random.Random(123)
    real_rs: list[float] = []
    null_candidates: list[list[float]] = []
    for t in trades:
        dl = day_bars.get(t.session_date)
        if not dl:
            continue
        idx_by_ts = {b.ts: i for i, b in enumerate(dl)}
        real_idx = idx_by_ts.get(t.entry_ts)
        if real_idx is None:
            continue
        horizon = t.entry_ts + timedelta(minutes=30)
        cand = [i for i in range(real_idx, len(dl)) if dl[i].ts <= horizon]
        if not cand:
            continue
        real_rs.append(simulate_outcome(dl, real_idx, t.or_high, t.or_low,
                                         t.direction))
        null_candidates.append([
            simulate_outcome(dl, i, t.or_high, t.or_low, t.direction)
            for i in cand
        ])
    if not real_rs:
        return {"n_signals": 0, "p_value": None}
    real_mean = sum(real_rs) / len(real_rs)
    null_means = []
    for _ in range(iters):
        run = [rng.choice(c) for c in null_candidates]
        null_means.append(sum(run) / len(run))
    beat = sum(1 for m in null_means if m >= real_mean)
    return {
        "n_signals": len(real_rs),
        "real_mean_net_r": round(real_mean, 4),
        "null_mean_net_r_avg": round(sum(null_means) / iters, 4),
        "p_value": round(beat / iters, 4),
    }


# ---------------------------------------------------------------------------
# STEP 3 — Tradeify rolling simulation
# ---------------------------------------------------------------------------

def tradeify_window(trades_sorted: list, start: date, cap_days: int) -> dict:
    end = start + timedelta(days=cap_days)
    window = [t for t in trades_sorted
              if start <= t.entry_ts.astimezone(NY).date() < end]
    cumulative = 0.0
    peak_balance = STARTING_EQUITY
    daily: list[float] = []
    result = "TIMEOUT"
    days_to_win = None
    for t in window:
        cumulative += t.net_pnl
        daily.append(t.net_pnl)
        balance = STARTING_EQUITY + cumulative
        if cumulative >= PROFIT_TARGET:
            result = "WIN"
            days_to_win = (t.entry_ts.astimezone(NY).date() - start).days
            break
        if peak_balance - balance >= TRAILING_DD:
            result = "FAIL"
            break
        peak_balance = max(peak_balance, balance)
    consistency_violation = False
    if result == "WIN" and cumulative > 0 and daily:
        consistency_violation = max(daily) > CONSISTENCY_FRAC * cumulative
    max_consec_loss = cur = 0
    for x in daily:
        if x < 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0
    return {
        "result": result,
        "days_to_win": days_to_win,
        "final_pnl": round(cumulative, 2),
        "consistency_violation": consistency_violation,
        "max_consec_losses": max_consec_loss,
        "worst_day": round(min(daily), 2) if daily else 0.0,
    }


def month_starts(first: date, last: date):
    y, m = first.year, first.month
    while date(y, m, 1) <= last:
        yield date(y, m, 1)
        m += 1
        if m == 13:
            m, y = 1, y + 1


def tradeify_rolling(trades_sorted: list, cap_days: int) -> dict:
    sims = [tradeify_window(trades_sorted, s, cap_days)
            for s in month_starts(WINDOW_FIRST, WINDOW_LAST)]
    n = len(sims)
    wins = [s for s in sims if s["result"] == "WIN"]
    fails = [s for s in sims if s["result"] == "FAIL"]
    timeouts = [s for s in sims if s["result"] == "TIMEOUT"]
    win_days = [s["days_to_win"] for s in wins if s["days_to_win"] is not None]
    timeout_pnls = [s["final_pnl"] for s in timeouts]
    return {
        "cap_days": cap_days,
        "windows": n,
        "wins": len(wins),
        "fails": len(fails),
        "timeouts": len(timeouts),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "fail_rate": round(len(fails) / n, 4) if n else 0.0,
        "timeout_rate": round(len(timeouts) / n, 4) if n else 0.0,
        "avg_days_to_win": round(sum(win_days) / len(win_days), 1) if win_days else None,
        "median_timeout_pnl": round(statistics.median(timeout_pnls), 2) if timeout_pnls else None,
        "consistency_violations_among_wins": sum(1 for s in wins if s["consistency_violation"]),
        "worst_max_consec_losses": max((s["max_consec_losses"] for s in sims), default=0),
        "worst_single_day": round(min((s["worst_day"] for s in sims), default=0.0), 2),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Replaying ORB over 2022-01-01 .. 2025-04-25 ...")
    trades, day_bars = run_orb()
    trades.sort(key=lambda t: t.entry_ts)
    print(f"  ORB trades total: {len(trades)}")

    # ---- STEP 2 ----
    splits_out = {}
    oos_all: list = []
    for tag in ("A", "B", "C"):
        sub = [t for t in trades if in_oos(t, tag)]
        oos_all += sub
        splits_out[tag] = {
            "oos_window": [str(OOS_SPLITS[tag][0]), str(OOS_SPLITS[tag][1])],
            "metrics": split_metrics(sub),
            "or_range_distribution": or_range_distribution(sub),
            "check_a_bootstrap_ci": bootstrap_ci([real_net_r(t) for t in sub]),
            "check_b_null_model": null_model(sub, day_bars),
        }
    agg = {
        "metrics": split_metrics(oos_all),
        "or_range_distribution": or_range_distribution(oos_all),
        "check_a_bootstrap_ci": bootstrap_ci([real_net_r(t) for t in oos_all]),
        "check_b_null_model": null_model(oos_all, day_bars),
    }

    # ---- STEP 3 ----
    step3 = {f"{c}d": tradeify_rolling(trades, c) for c in TIME_CAPS}

    # ---- VERDICT ----
    avg_net = agg["metrics"]["avg_net_per_trade"]
    wr_180 = step3["180d"]["win_rate"]
    ci_spans_zero = bool(agg["check_a_bootstrap_ci"]["ci_spans_zero"])
    null_p = agg["check_b_null_model"]["p_value"]
    if avg_net > 10 and wr_180 > 0.60:
        raw = "VIABLE"
    elif 0 <= avg_net <= 10 and 0.40 <= wr_180 <= 0.60:
        raw = "MARGINAL"
    elif avg_net <= 0 or wr_180 < 0.40:
        raw = "NOT VIABLE"
    else:
        raw = "MARGINAL"
    not_verified = ci_spans_zero or (null_p is not None and null_p > 0.05)
    if raw == "NOT VIABLE":
        final = "NOT VIABLE"
    elif not_verified:
        final = "NOT VIABLE (not statistically verified)"
    else:
        final = raw
    verdict = {
        "avg_net_per_trade_oos": avg_net,
        "tradeify_180d_win_rate": wr_180,
        "raw_verdict": raw,
        "bootstrap_ci_spans_zero": ci_spans_zero,
        "null_model_p_value": null_p,
        "statistically_verified": not not_verified,
        "final_verdict": final,
    }

    payload = {
        "generated_at": datetime.now(NY).isoformat(),
        "strategy": "ORB 15-min NQ — mid-stop, R:R 1.5, $1000 risk budget",
        "cost_model": {"commission_rt": COMMISSION_RT, "slippage_per_side_ticks": 1},
        "replay": {"trades_total": len(trades),
                   "window": [str(REPLAY_START), str(REPLAY_END)]},
        "step2_walk_forward": {"splits": splits_out, "oos_aggregate": agg},
        "step3_tradeify_rolling": step3,
        "verdict": verdict,
    }
    out1 = REPO / "results" / "phase4_orb_tradeify_sim.json"
    out1.write_text(json.dumps(payload, indent=2))

    # ---- ORB vs NasdaqAle comparison ----
    na = {}
    na_path = REPO / "results" / "phase4_tradeify_va_final.json"
    if na_path.exists():
        na = json.loads(na_path.read_text())
    na_avg_net = na.get("part1_net_edge", {}).get("avg_net_pnl_per_trade_1nq")
    na_total = na.get("part1_net_edge", {}).get("total_net_pnl_1nq_full_replay")
    na_wr = (na.get("part2_no_time_limit", {}).get("1_NQ", {}) or {}).get("win_rate_pct")
    cmp_payload = {
        "generated_at": datetime.now(NY).isoformat(),
        "comparison": {
            "avg_net_per_trade": {
                "nasdaqale_va": na_avg_net, "orb": avg_net,
            },
            "total_oos_net_pnl": {
                "nasdaqale_va_full_replay_1nq": na_total,
                "orb_oos_aggregate": agg["metrics"]["total_net_pnl"],
            },
            "tradeify_win_rate": {
                "nasdaqale_va_1nq_pct": na_wr,
                "orb_180d_pct": round(wr_180 * 100, 2),
            },
        },
        "orb_verdict": final,
    }
    out2 = REPO / "results" / "phase4_orb_vs_nasdaqale.json"
    out2.write_text(json.dumps(cmp_payload, indent=2))

    # ---- print report ----
    print()
    print("=" * 78)
    print("STEP 2 — Walk-forward (OOS splits + aggregate)")
    print("=" * 78)
    hdr = (f"{'split':<10}{'trades':>7}{'WR':>8}{'BE-WR':>7}{'WR/BE':>7}"
           f"{'avgGross':>10}{'avgNet':>9}{'PF':>7}{'totNet':>10}{'maxDD':>9}")
    print(hdr)
    for tag in ("A", "B", "C"):
        m = splits_out[tag]["metrics"]
        if m.get("trades"):
            print(f"{tag+' OOS':<10}{m['trades']:>7}{m['win_rate']:>8.3f}"
                  f"{m['breakeven_wr']:>7.2f}{m['wr_over_breakeven']:>7.2f}"
                  f"{m['avg_gross_per_trade']:>10.2f}{m['avg_net_per_trade']:>9.2f}"
                  f"{(m['profit_factor'] or 0):>7.2f}{m['total_net_pnl']:>10.0f}"
                  f"{m['max_drawdown']:>9.0f}")
    am = agg["metrics"]
    print(f"{'AGGREGATE':<10}{am['trades']:>7}{am['win_rate']:>8.3f}"
          f"{am['breakeven_wr']:>7.2f}{am['wr_over_breakeven']:>7.2f}"
          f"{am['avg_gross_per_trade']:>10.2f}{am['avg_net_per_trade']:>9.2f}"
          f"{(am['profit_factor'] or 0):>7.2f}{am['total_net_pnl']:>10.0f}"
          f"{am['max_drawdown']:>9.0f}")

    print()
    print("OR-range distribution (OOS aggregate) — report only, not optimised:")
    for row in agg["or_range_distribution"]:
        wr = f"{row['win_rate']:.3f}" if row["win_rate"] is not None else "  -  "
        an = f"{row['avg_net']:+.2f}" if row["avg_net"] is not None else "  -  "
        print(f"  {row['band_pts']:<8} pts: {row['trades']:>3} trades  "
              f"WR={wr}  avgNet={an}")

    print()
    print("=" * 78)
    print("CHECK A — bootstrap 95% CI on net-R per trade")
    print("=" * 78)
    for tag in ("A", "B", "C"):
        ca = splits_out[tag]["check_a_bootstrap_ci"]
        print(f"  {tag} OOS  : mean={ca['mean_net_r']}  "
              f"CI=[{ca['ci_low']}, {ca['ci_high']}]  spans_zero={ca['ci_spans_zero']}")
    ca = agg["check_a_bootstrap_ci"]
    print(f"  AGGREGATE: mean={ca['mean_net_r']}  "
          f"CI=[{ca['ci_low']}, {ca['ci_high']}]  spans_zero={ca['ci_spans_zero']}")

    print()
    print("=" * 78)
    print("CHECK B — null model (random entry within 30 min of the signal)")
    print("=" * 78)
    for tag in ("A", "B", "C"):
        cb = splits_out[tag]["check_b_null_model"]
        print(f"  {tag} OOS  : real={cb.get('real_mean_net_r')}  "
              f"null_avg={cb.get('null_mean_net_r_avg')}  p={cb.get('p_value')}")
    cb = agg["check_b_null_model"]
    print(f"  AGGREGATE: real={cb.get('real_mean_net_r')}  "
          f"null_avg={cb.get('null_mean_net_r_avg')}  p={cb.get('p_value')}")

    print()
    print("=" * 78)
    print("STEP 3 — Tradeify SELECT rolling simulation (35 monthly windows)")
    print("=" * 78)
    print(f"{'cap':>6}{'WIN%':>8}{'FAIL%':>8}{'TIMEOUT%':>10}"
          f"{'avgDaysWin':>12}{'medTimeout$':>13}{'worstStreak':>12}")
    for c in TIME_CAPS:
        s = step3[f"{c}d"]
        print(f"{str(c)+'d':>6}{s['win_rate']*100:>8.1f}{s['fail_rate']*100:>8.1f}"
              f"{s['timeout_rate']*100:>10.1f}"
              f"{str(s['avg_days_to_win']):>12}{str(s['median_timeout_pnl']):>13}"
              f"{s['worst_max_consec_losses']:>12}")

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    print(f"  avg net / trade (OOS aggregate) : ${avg_net:+.2f}")
    print(f"  Tradeify 180-day win rate       : {wr_180*100:.1f}%")
    print(f"  Bootstrap CI spans zero         : {ci_spans_zero}")
    print(f"  Null-model p-value              : {null_p}")
    print(f"  Statistically verified          : {not not_verified}")
    print(f"  RAW verdict                     : {raw}")
    print(f"  FINAL VERDICT                   : {final}")
    print()
    print(f"Saved: {out1.relative_to(REPO)}")
    print(f"Saved: {out2.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
