#!/usr/bin/env python3
"""Constraint diagnostic — prop-firm vehicle sweep over EXISTING trade data.

NO new strategy code. This script replays the two already-built strategies via
their existing phase4 replay functions and then does pure post-hoc analysis of
the resulting trade distributions:

  - ORB OOS aggregate ...... 201 trades  (phase4_orb_steps234.run_orb + in_oos)
  - NasdaqAle V_A .......... 587 trades  (phase4_tradeify_va_final.replay_va_trades)

Both trade streams are deterministic functions of the locked strategy code +
config; nothing about the strategies is changed here. The only new logic is the
vehicle/instrument sweep (the diagnostic itself).

PART 1  Vehicle sweep, NQ cost model ($19 round-trip / contract).
PART 2  Vehicle sweep, MNQ cost model (micro, proportionally sized).
PART 3  Edge-target back-calculation — only reported as the binding spec if no
        PART 1/2 combination clears WIN% > 50%.

Tradeify SELECT vehicle specs (verified 2026-05-17, help.tradeify.co):
  SELECT 50k  : start $50,000  · target $3,000 · EOD trailing DD $2,000
  SELECT 100k : start $100,000 · target $6,000 · EOD trailing DD $3,000
  SELECT 150k : start $150,000 · target $9,000 · EOD trailing DD $4,500
  All: EOD trailing drawdown, no daily loss limit, 40% consistency rule,
  minimum 3 trading days, drawdown floor locks once it reaches start+$100.

Output: results/constraint_diagnostic.json
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

# Existing strategy replay code — imported, not rewritten.
import phase4_orb_steps234 as orb          # noqa: E402
import phase4_tradeify_va_final as va      # noqa: E402

NY = ZoneInfo("America/New_York")

# --------------------------------------------------------------------------
# Vehicle + cost-model constants
# --------------------------------------------------------------------------
VEHICLES = {
    "SELECT_50k":  {"start": 50_000.0,  "target": 3_000.0, "dd": 2_000.0},
    "SELECT_100k": {"start": 100_000.0, "target": 6_000.0, "dd": 3_000.0},
    "SELECT_150k": {"start": 150_000.0, "target": 9_000.0, "dd": 4_500.0},
}
DD_LOCK_OFFSET = 100.0          # floor locks at start + $100
CONSISTENCY_FRAC = 0.40         # 40% rule
MIN_TRADING_DAYS = 3            # Tradeify SELECT minimum

# Cost model — config/cost_model.yaml is the single source of truth.
#   NQ : $9 commission + $10 slippage  = $19 round-trip / contract.
#   MNQ: $1 commission + $1  slippage  = $2  round-trip / contract;
#        10 MNQ == 1 NQ of exposure -> $20 round-trip / NQ-equivalent.
NQ_COST_RT = 19.0
MNQ_COST_RT_PER_NQ_EQUIV = 20.0

WINDOW_DAYS = [30, 90, 180]
SIZINGS = [1, 2, 3]             # NQ-equivalent contracts

RR = 1.5                        # ORB R:R, used by PART 3


# --------------------------------------------------------------------------
# Trade extraction — per-1-NQ-contract GROSS dollar pnl + NY trade date
# --------------------------------------------------------------------------
def extract_orb_oos() -> tuple[list[tuple[date, float]], list[tuple[date, date]]]:
    """Return (sorted [(date, gross_pc)], contiguous OOS segments).

    gross_pc = gross dollar pnl for ONE NQ contract (costs re-added).
    The ORB OOS aggregate is split A+B (contiguous) and C, with a calendar
    gap Jul-Sep 2024. Rolling windows are placed inside contiguous segments
    only, so no challenge straddles the walk-forward gap.
    """
    all_trades, _ = orb.run_orb()
    oos = [t for t in all_trades
           if any(orb.in_oos(t, tag) for tag in ("A", "B", "C"))]
    rows: list[tuple[date, float]] = []
    for t in oos:
        qty = max(float(t.qty), 1.0)
        gross_pc = float(t.gross_pnl) / qty          # per 1 NQ, pre-cost
        rows.append((t.entry_ts.astimezone(NY).date(), gross_pc))
    rows.sort(key=lambda r: r[0])
    # Contiguous segments: merge A+B (Oct-2023..Jun-2024) and C (Oct-2024..).
    a0, a1 = orb.OOS_SPLITS["A"]
    b0, b1 = orb.OOS_SPLITS["B"]
    c0, c1 = orb.OOS_SPLITS["C"]
    segments = [(a0, b1), (c0, c1)]
    return rows, segments


def extract_va() -> tuple[list[tuple[date, float]], list[tuple[date, date]]]:
    """Return (sorted [(date, gross_pc)], single contiguous segment)."""
    trades = va.replay_va_trades()
    rows: list[tuple[date, float]] = []
    for t in trades:
        rows.append((t.entry_ts.date(), va.per_nq_gross(t)))
    rows.sort(key=lambda r: r[0])
    segments = [(rows[0][0], rows[-1][0])]
    return rows, segments


# --------------------------------------------------------------------------
# Rolling-challenge simulation
# --------------------------------------------------------------------------
def simulate_window(day_rows: list[tuple[date, float]], start: date,
                    window_days: int, vehicle: dict, sizing: int,
                    cost_rt: float) -> dict:
    """Simulate one prop-firm challenge over [start, start+window_days).

    day_rows : sorted [(date, gross_pc)] — gross pnl per 1 NQ contract.
    Net per trade for `sizing` NQ-equivalent contracts:
        net = sizing * (gross_pc - cost_rt)
    EOD trailing drawdown with floor lock at start+$100.
    """
    start_eq = vehicle["start"]
    target = vehicle["target"]
    dd = vehicle["dd"]
    end = start + timedelta(days=window_days)

    window = [(d, g) for d, g in day_rows if start <= d < end]
    by_day: dict[date, list[float]] = defaultdict(list)
    for d, g in window:
        by_day[d].append(g)

    cumulative = 0.0
    peak_eod = start_eq
    daily_pnl: dict[date, float] = defaultdict(float)
    result = "TIMEOUT"
    win_date: date | None = None
    fail_date: date | None = None

    for day in sorted(by_day):
        for gross_pc in by_day[day]:
            net = sizing * (gross_pc - cost_rt)
            cumulative += net
            daily_pnl[day] += net
            if cumulative >= target:
                result = "WIN"
                win_date = day
                break
        if result == "WIN":
            break
        # EOD trailing-drawdown check (floor locks at start + $100).
        eod_balance = start_eq + cumulative
        peak_eod = max(peak_eod, eod_balance)
        floor = min(peak_eod - dd, start_eq + DD_LOCK_OFFSET)
        if eod_balance <= floor:
            result = "FAIL"
            fail_date = day
            break

    trading_days = len(daily_pnl)
    best_day = max(daily_pnl.values()) if daily_pnl else 0.0
    consistency_ok = None
    mindays_ok = None
    if result == "WIN":
        mindays_ok = trading_days >= MIN_TRADING_DAYS
        consistency_ok = (cumulative > 0
                          and best_day <= CONSISTENCY_FRAC * cumulative)
    days_to_outcome = None
    if win_date is not None:
        days_to_outcome = (win_date - start).days
    elif fail_date is not None:
        days_to_outcome = (fail_date - start).days

    return {
        "start": str(start),
        "result": result,
        "final_pnl": round(cumulative, 2),
        "days_to_outcome": days_to_outcome,
        "trading_days": trading_days,
        "best_day_pnl": round(best_day, 2),
        "consistency_ok": consistency_ok,
        "mindays_ok": mindays_ok,
    }


def month_starts(seg_start: date, seg_end: date, window_days: int):
    """First-of-month window starts whose full window fits inside the segment."""
    y, m = seg_start.year, seg_start.month
    if seg_start.day > 1:                       # first whole month
        m += 1
        if m == 13:
            m, y = 1, y + 1
    while True:
        d = date(y, m, 1)
        if d + timedelta(days=window_days) > seg_end + timedelta(days=1):
            break
        if d >= seg_start:
            yield d
        m += 1
        if m == 13:
            m, y = 1, y + 1


def sweep_cell(day_rows, segments, window_days: int, vehicle: dict,
               sizing: int, cost_rt: float) -> dict:
    """Run every monthly rolling window for one (vehicle, window, sizing) cell."""
    sims: list[dict] = []
    for seg_start, seg_end in segments:
        for start in month_starts(seg_start, seg_end, window_days):
            sims.append(simulate_window(day_rows, start, window_days,
                                        vehicle, sizing, cost_rt))
    n = len(sims)
    wins = [s for s in sims if s["result"] == "WIN"]
    fails = [s for s in sims if s["result"] == "FAIL"]
    timeouts = [s for s in sims if s["result"] == "TIMEOUT"]
    win_days = [s["days_to_outcome"] for s in wins
                if s["days_to_outcome"] is not None]
    qualified = [s for s in wins
                 if s["consistency_ok"] and s["mindays_ok"]]
    return {
        "windows": n,
        "wins": len(wins),
        "fails": len(fails),
        "timeouts": len(timeouts),
        "win_pct": round(100 * len(wins) / n, 1) if n else None,
        "fail_pct": round(100 * len(fails) / n, 1) if n else None,
        "timeout_pct": round(100 * len(timeouts) / n, 1) if n else None,
        "avg_days_to_win": round(sum(win_days) / len(win_days), 1)
                           if win_days else None,
        "consistency_violations_among_wins":
            sum(1 for s in wins if s["consistency_ok"] is False),
        "mindays_violations_among_wins":
            sum(1 for s in wins if s["mindays_ok"] is False),
        "qualified_win_pct": round(100 * len(qualified) / n, 1) if n else None,
        "worst_final_pnl": round(min((s["final_pnl"] for s in sims),
                                     default=0.0), 2),
    }


def run_sweep(strategies: dict, cost_rt: float, instrument: str) -> list[dict]:
    """Full vehicle x window x sizing sweep for one cost model."""
    records: list[dict] = []
    for strat_name, (day_rows, segments) in strategies.items():
        for veh_name, vehicle in VEHICLES.items():
            for window_days in WINDOW_DAYS:
                for sizing in SIZINGS:
                    cell = sweep_cell(day_rows, segments, window_days,
                                      vehicle, sizing, cost_rt)
                    records.append({
                        "strategy": strat_name,
                        "instrument": instrument,
                        "vehicle": veh_name,
                        "window_days": window_days,
                        "sizing_nq_equiv": sizing,
                        **cell,
                    })
    return records


# --------------------------------------------------------------------------
# PART 3 — edge-target back-calculation (Monte-Carlo)
# --------------------------------------------------------------------------
def trades_per_90d(day_rows: list[tuple[date, float]],
                   segments: list[tuple[date, date]]) -> float:
    """Observed trade cadence, expressed as trades per 90 calendar days."""
    n = len(day_rows)
    span = sum((e - s).days + 1 for s, e in segments)
    return n * 90.0 / span if span else 0.0


def mc_win_pct(win_rate: float, risk: float, cadence: int,
               vehicle: dict, cost_rt: float, sims: int,
               rng: random.Random) -> float:
    """Monte-Carlo QUALIFIED WIN% for a synthetic R:R 1.5 strategy.

    Each trade (ORB cadence: one trade per trading day) is a win (+RR*risk)
    or loss (-risk) before costs; `cadence` trades are available per window.
    A challenge counts as a win only if it hits the target before the EOD
    trailing drawdown AND satisfies the 40% consistency rule AND uses at
    least MIN_TRADING_DAYS days — i.e. a genuinely payable pass.
    """
    start_eq = vehicle["start"]
    target = vehicle["target"]
    dd = vehicle["dd"]
    win_net = RR * risk - cost_rt
    loss_net = -risk - cost_rt
    wins = 0
    for _ in range(sims):
        cumulative = 0.0
        peak_eod = start_eq
        daily: list[float] = []          # one trade == one trading day (ORB)
        outcome = "TIMEOUT"
        for _ in range(cadence):
            net = win_net if rng.random() < win_rate else loss_net
            cumulative += net
            daily.append(net)
            if cumulative >= target:
                outcome = "WIN"
                break
            eod_balance = start_eq + cumulative
            peak_eod = max(peak_eod, eod_balance)
            floor = min(peak_eod - dd, start_eq + DD_LOCK_OFFSET)
            if eod_balance <= floor:
                outcome = "FAIL"
                break
        if outcome == "WIN" and cumulative > 0 \
                and len(daily) >= MIN_TRADING_DAYS \
                and max(daily) <= CONSISTENCY_FRAC * cumulative:
            wins += 1
    return 100.0 * wins / sims


def part3_edge_target(orb_rows, orb_segments) -> dict:
    """Back-calculate the edge spec needed for SELECT 50k WIN% > 60%."""
    vehicle = VEHICLES["SELECT_50k"]
    cadence = round(trades_per_90d(orb_rows, orb_segments))
    rng = random.Random(2026)
    SIMS = 4000
    TARGET_WIN = 60.0

    risk_grid = [50, 75, 100, 125, 150, 200, 250, 300]
    wr_grid = [round(0.40 + 0.02 * i, 2) for i in range(17)]   # 0.40..0.72

    frontier: list[dict] = []
    for risk in risk_grid:
        min_wr = None
        for wr in wr_grid:
            wp = mc_win_pct(wr, risk, cadence, vehicle, NQ_COST_RT, SIMS, rng)
            if wp > TARGET_WIN:
                min_wr = wr
                gross_edge = risk * (RR * wr - (1 - wr))      # = risk*(2.5wr-1)
                avg_net = gross_edge - NQ_COST_RT
                frontier.append({
                    "max_per_trade_risk_usd": risk,
                    "min_win_rate": wr,
                    "achieved_win_pct": round(wp, 1),
                    "required_gross_edge_per_trade_usd": round(gross_edge, 2),
                    "required_avg_net_pnl_per_trade_usd": round(avg_net, 2),
                })
                break
        if min_wr is None:
            frontier.append({
                "max_per_trade_risk_usd": risk,
                "min_win_rate": None,
                "achieved_win_pct": None,
                "note": "WIN% > 60% unreachable within WR grid (<=0.72)",
            })

    feasible = [f for f in frontier if f.get("min_win_rate") is not None]
    recommended = None
    if feasible:
        # Cleanest spec = the one demanding the lowest win rate.
        recommended = min(feasible, key=lambda f: f["min_win_rate"])

    # 40%-consistency feasibility: a single uniform-risk win is
    # 1.5*risk - cost; it must stay <= 40% of the $3,000 target.
    max_consistency_safe_risk = round(
        (CONSISTENCY_FRAC * vehicle["target"] + NQ_COST_RT) / RR, 2)

    return {
        "vehicle": "SELECT_50k",
        "method": ("Monte-Carlo QUALIFIED win (target before EOD trailing DD, "
                   "40% consistency rule + 3-day minimum enforced); 90-day "
                   "window, ORB cadence, R:R 1.5, NQ $19 cost, $100 floor "
                   "lock"),
        "cadence_trades_per_90d": cadence,
        "monte_carlo_sims_per_cell": SIMS,
        "target_win_pct": TARGET_WIN,
        "consistency_note": (
            f"At uniform R:R 1.5 sizing the 40% rule is non-binding for any "
            f"per-trade risk <= ${max_consistency_safe_risk} (a single win "
            f"then stays under 40% of the $3,000 target). Every risk level "
            f"in the frontier grid is consistency-safe by construction — "
            f"unlike the ORB stream, whose trade-size variance is what "
            f"breaks the rule."),
        "frontier": frontier,
        "recommended_spec": recommended,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    print("Replaying ORB (OOS aggregate) ...")
    orb_rows, orb_segments = extract_orb_oos()
    print(f"  ORB OOS trades: {len(orb_rows)}")
    print("Replaying NasdaqAle V_A ...")
    va_rows, va_segments = extract_va()
    print(f"  V_A trades: {len(va_rows)}")

    strategies = {
        "ORB_OOS": (orb_rows, orb_segments),
        "NasdaqAle_V_A": (va_rows, va_segments),
    }

    print("PART 1 — vehicle sweep, NQ cost model ...")
    part1 = run_sweep(strategies, NQ_COST_RT, "NQ")
    print("PART 2 — vehicle sweep, MNQ cost model ...")
    part2 = run_sweep(strategies, MNQ_COST_RT_PER_NQ_EQUIV, "MNQ")

    # Best across PART 1 + PART 2. Two metrics:
    #   raw WIN%       = hit the dollar target before the trailing DD.
    #   qualified WIN% = raw win that ALSO passes the 40% consistency rule
    #                    and the 3-trading-day minimum (a payable pass).
    # PART 3 is binding when no QUALIFIED combo clears 50% — a raw win that
    # the consistency rule voids is not a pass.
    all_cells = part1 + part2
    best_raw = max(all_cells, key=lambda c: c["win_pct"] or -1.0)
    best_qual = max(all_cells, key=lambda c: c["qualified_win_pct"] or -1.0)
    best_win = best_raw["win_pct"]
    best_qual_win = best_qual["qualified_win_pct"]
    part3_triggered = (best_qual_win is None) or (best_qual_win <= 50.0)

    # PART 2 question: does the micro cost stack lower FAIL%?
    nq_idx = {(c["strategy"], c["vehicle"], c["window_days"],
               c["sizing_nq_equiv"]): c for c in part1}
    fail_deltas: list[float] = []
    for c in part2:
        key = (c["strategy"], c["vehicle"], c["window_days"],
               c["sizing_nq_equiv"])
        nq = nq_idx.get(key)
        if nq and c["fail_pct"] is not None and nq["fail_pct"] is not None:
            fail_deltas.append(c["fail_pct"] - nq["fail_pct"])
    mnq_vs_nq = {
        "mean_fail_pct_delta_mnq_minus_nq":
            round(sum(fail_deltas) / len(fail_deltas), 3) if fail_deltas else None,
        "max_fail_pct_improvement":
            round(min(fail_deltas), 3) if fail_deltas else None,
        "cells_compared": len(fail_deltas),
        "interpretation": (
            "MNQ sized to equal NQ exposure costs $20 vs $19 round-trip per "
            "NQ-equivalent (10 micros: $10 commission + $10 slippage). The "
            "micro contract does NOT reduce the cost stack at equal exposure; "
            "it is ~$1/NQ-equivalent more expensive."),
    }

    print("PART 3 — edge-target back-calculation ...")
    part3 = part3_edge_target(orb_rows, orb_segments)

    payload = {
        "generated_at": datetime.now(NY).isoformat(),
        "purpose": ("Constraint diagnostic — post-hoc prop-firm vehicle sweep "
                    "over existing ORB-OOS and NasdaqAle-V_A trade streams. "
                    "No strategy code changed; trades replayed via the "
                    "phase4 scripts."),
        "inputs": {
            "orb_oos_trades": len(orb_rows),
            "va_trades": len(va_rows),
            "orb_oos_segments": [[str(a), str(b)] for a, b in orb_segments],
            "va_segment": [[str(a), str(b)] for a, b in va_segments],
            "note": ("results/phase4_orb_tradeify_sim.json and "
                     "results/phase4_tradeify_va_final.json hold only "
                     "aggregate summaries, not per-trade rows; the per-trade "
                     "streams were re-derived deterministically by replaying "
                     "the locked strategy code."),
        },
        "vehicles": VEHICLES,
        "cost_models": {
            "NQ_round_trip_usd": NQ_COST_RT,
            "MNQ_round_trip_per_nq_equiv_usd": MNQ_COST_RT_PER_NQ_EQUIV,
            "dd_floor_lock_offset_usd": DD_LOCK_OFFSET,
            "consistency_fraction": CONSISTENCY_FRAC,
            "min_trading_days": MIN_TRADING_DAYS,
        },
        "part1_nq_sweep": part1,
        "part2_mnq_sweep": part2,
        "part2_mnq_vs_nq": mnq_vs_nq,
        "part3_edge_target": part3,
        "summary": {
            "best_raw_win_pct_part1_part2": best_win,
            "best_qualified_win_pct_part1_part2": best_qual_win,
            "best_raw_cell": {k: best_raw[k] for k in
                              ("strategy", "instrument", "vehicle",
                               "window_days", "sizing_nq_equiv", "win_pct",
                               "fail_pct", "qualified_win_pct")},
            "best_qualified_cell": {k: best_qual[k] for k in
                                    ("strategy", "instrument", "vehicle",
                                     "window_days", "sizing_nq_equiv",
                                     "win_pct", "fail_pct",
                                     "qualified_win_pct")},
            "any_combo_raw_win_pct_over_50": (best_win is not None
                                              and best_win > 50.0),
            "any_combo_qualified_win_pct_over_50": (
                best_qual_win is not None and best_qual_win > 50.0),
            "part3_binding": part3_triggered,
        },
    }

    out = REPO / "results" / "constraint_diagnostic.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved: {out.relative_to(REPO)}")

    # ---- console summary ----
    print()
    print("=" * 74)
    print("BEST across PART 1 + PART 2")
    print("=" * 74)
    br = payload["summary"]["best_raw_cell"]
    bq = payload["summary"]["best_qualified_cell"]
    print(f"  best RAW       : {br['strategy']} / {br['instrument']} / "
          f"{br['vehicle']} / {br['window_days']}d / {br['sizing_nq_equiv']}x"
          f"  ->  WIN {br['win_pct']}%  (qualified {br['qualified_win_pct']}%)")
    print(f"  best QUALIFIED : {bq['strategy']} / {bq['instrument']} / "
          f"{bq['vehicle']} / {bq['window_days']}d / {bq['sizing_nq_equiv']}x"
          f"  ->  qualified {bq['qualified_win_pct']}%")
    print(f"  any combo RAW WIN% > 50%       : "
          f"{payload['summary']['any_combo_raw_win_pct_over_50']}")
    print(f"  any combo QUALIFIED WIN% > 50% : "
          f"{payload['summary']['any_combo_qualified_win_pct_over_50']}")
    print(f"  MNQ mean FAIL% delta : "
          f"{mnq_vs_nq['mean_fail_pct_delta_mnq_minus_nq']:+} pts")
    if part3["recommended_spec"]:
        r = part3["recommended_spec"]
        print(f"  PART 3 target spec   : risk<=${r['max_per_trade_risk_usd']}, "
              f"min WR {r['min_win_rate']}, gross edge "
              f"${r['required_gross_edge_per_trade_usd']}/trade, "
              f"avg net ${r['required_avg_net_pnl_per_trade_usd']}/trade")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
