#!/usr/bin/env python3
"""VWAP-regime falsification recon — Zarattini/Aziz thesis on NQ.

NOT a strategy build. No state machine. Vectorized per-session test of whether
NQ exhibits the "drift up when above session VWAP, drift down when below" effect
that the Zarattini/Aziz paper documents on QQQ.

Data : data/historical/NQ_1m_2022_2026.dbn.zst (front-month, RTH 09:30-16:00 NY)
Cost : locked $19 round-trip / NQ contract, $20 / point. 1 contract per trade.

PART 1  Paper-Figure-2 replication on NQ — bin every RTH 1-min bar's dollar
        change by the PRIOR bar's (close - VWAP) sign and sum.
        Thesis lives if above-VWAP-prior repricing is net positive AND
        below-VWAP-prior is net negative.

PART 2  Tradeable stop-and-reverse: enter LONG at next-bar open after a bar
        closes ABOVE VWAP; enter SHORT after a bar closes BELOW VWAP. Exit on
        the opposite close-through (also at next-bar open, kicking off the
        reverse trade) or force-flat at 15:59. Full-data trades/WR/avgNet/PF.

PART 3  Time-of-day x regime split — paper windows (Morning 09:30-12:00,
        Midday 12:00-15:00, Close 15:00-16:00) crossed with the
        TrendRegimeGate 10-day efficiency-ratio regime (range vs trending).

PART 4  Bootstrap-CI (10,000 iter) + null model (random entry within ±30 min,
        same direction, same exit rule) on the best PART-3 cell. Locked
        decision rule (chosen BEFORE the run, per task spec):
          CI spans zero  OR  null-model p > 0.05  ->  NO PULSE, no build.
          CI fully positive AND p < 0.05          ->  PULSE, build justified.

R-unit  net-R = net_pnl_usd / (ATR14_daily_pts * $20)  per trade's session day,
        i.e. "fraction of a 14-day daily ATR earned." No defined stop in a
        stop-and-reverse system, so ATR14 is the natural risk unit.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

# Reuse existing recon plumbing (data loader, daily bars, regime labels).
from mean_reversion_recon import (   # noqa: E402
    NY, load_nq_rth, daily_rth_bars, regime_labels,
    POINT_VALUE, COST_RT, RTH_OPEN, RTH_CLOSE, RTH_LAST_ENTRY,
)

# Time-of-day buckets (paper windows).
TOD_BOUNDS = [
    ("morning", time(9, 30), time(12, 0)),    # [09:30, 12:00)
    ("midday",  time(12, 0), time(15, 0)),    # [12:00, 15:00)
    ("close",   time(15, 0), time(16, 0)),    # [15:00, 16:00)
]
ATR_LOOKBACK = 14
BOOTSTRAP_ITERS = 10_000
NULL_ITERS = 2_000
NULL_OFFSET_MIN = 30
MIN_TRADES_FOR_BEST = 30


def tod_bucket(t: time) -> str:
    for label, lo, hi in TOD_BOUNDS:
        if lo <= t < hi:
            return label
    return "close" if t == time(15, 59) else "other"


# --------------------------------------------------------------------------
# ATR14 (daily) and per-session arrays
# --------------------------------------------------------------------------
def compute_atr14_usd(daily: pd.DataFrame) -> dict:
    """14-day daily ATR (true range) in $ — known at day open, no look-ahead."""
    h = daily["high"].to_numpy(float)
    l = daily["low"].to_numpy(float)
    c = daily["close"].to_numpy(float)
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    out: dict = {}
    for i, d in enumerate(daily["date"].tolist()):
        if i < ATR_LOOKBACK:
            out[d] = None
            continue
        atr = float(np.mean(tr[i - ATR_LOOKBACK:i]))
        out[d] = atr * POINT_VALUE if atr > 0 else None
    return out


def build_session(day: pd.DataFrame) -> dict:
    o = day["open"].to_numpy(float)
    h = day["high"].to_numpy(float)
    l = day["low"].to_numpy(float)
    c = day["close"].to_numpy(float)
    v = day["volume"].to_numpy(float)
    times = day["t"].tolist()
    n = len(c)
    tp = (h + l + c) / 3.0
    cv = np.cumsum(v)
    cvtp = np.cumsum(v * tp)
    vwap = np.where(cv > 0, cvtp / np.maximum(cv, 1e-12), c)
    side = np.where(c > vwap, 1, np.where(c < vwap, -1, 0)).astype(np.int8)

    # next_minus[i] = smallest j >= i with side[j] == -1, else -1
    # next_plus[i]  = smallest j >= i with side[j] == +1, else -1
    next_minus = np.full(n, -1, dtype=np.int64)
    next_plus = np.full(n, -1, dtype=np.int64)
    nm = -1
    np_ = -1
    for j in range(n - 1, -1, -1):
        if side[j] == -1:
            nm = j
        if side[j] == 1:
            np_ = j
        next_minus[j] = nm
        next_plus[j] = np_

    # Last bar index whose timestamp <= RTH_LAST_ENTRY (no entries after 15:50).
    last_entry_idx = -1
    for i, t in enumerate(times):
        if t <= RTH_LAST_ENTRY:
            last_entry_idx = i
    return {
        "o": o, "h": h, "l": l, "c": c, "v": v,
        "vwap": vwap, "side": side, "times": times,
        "next_minus": next_minus, "next_plus": next_plus,
        "n": n, "last_entry_idx": last_entry_idx,
    }


# --------------------------------------------------------------------------
# PART 1 — paper-figure-2 replication
# --------------------------------------------------------------------------
def part1_repricing(sessions: dict) -> dict:
    sum_above = 0.0
    sum_below = 0.0
    n_above = 0
    n_below = 0
    for s in sessions.values():
        n = s["n"]
        if n < 2:
            continue
        change = (s["c"][1:] - s["c"][:-1]) * POINT_VALUE      # length n-1
        prior_side = s["side"][:-1]
        mask_a = prior_side == 1
        mask_b = prior_side == -1
        sum_above += float(change[mask_a].sum())
        sum_below += float(change[mask_b].sum())
        n_above += int(mask_a.sum())
        n_below += int(mask_b.sum())
    return {
        "above_vwap_bars": n_above,
        "below_vwap_bars": n_below,
        "sum_change_usd_above_vwap": round(sum_above, 2),
        "sum_change_usd_below_vwap": round(sum_below, 2),
        "avg_change_per_bar_usd_above": round(sum_above / n_above, 4)
                                         if n_above else None,
        "avg_change_per_bar_usd_below": round(sum_below / n_below, 4)
                                         if n_below else None,
        "thesis_holds": (sum_above > 0 and sum_below < 0),
    }


# --------------------------------------------------------------------------
# PART 2 — tradeable stop-and-reverse
# --------------------------------------------------------------------------
def build_trades(sessions: dict, regimes: dict, atr_usd: dict) -> list[dict]:
    """Walk each session, record stop-and-reverse trades.

    Real-trade entries are bars k where side[k] != side[k-1] (both nonzero) —
    a close-through. Entry is at bar[k+1].open; exit on the next close-through
    (also at bar[k'+1].open) or at the last RTH bar's close (force-flat).
    """
    trades: list[dict] = []
    for date, s in sessions.items():
        n = s["n"]
        if n < 3:
            continue
        side = s["side"]
        o, c = s["o"], s["c"]
        times = s["times"]
        last_entry = s["last_entry_idx"]
        if last_entry < 1:
            continue
        regime = regimes.get(date)
        R_usd = atr_usd.get(date)

        # Locate close-through bars.
        prev = side[0]
        signals: list[tuple[int, int]] = []   # (bar k, new side)
        for i in range(1, n):
            if side[i] != 0 and prev != 0 and side[i] != prev:
                signals.append((i, int(side[i])))
            if side[i] != 0:
                prev = side[i]

        if not signals:
            continue

        # Walk signals, build trades. Entry at bar k+1 open if k+1 < n.
        cur_entry_idx = None
        cur_dir = None      # +1 LONG, -1 SHORT
        for k, new_side in signals:
            if k + 1 >= n or k > last_entry:
                continue
            entry_idx = k + 1
            entry_price = float(o[entry_idx])
            if cur_entry_idx is not None:
                # close the open trade at this same bar's open
                trades.append(_make_trade(
                    date, cur_dir, cur_entry_idx, entry_idx, entry_price,
                    o, c, times, regime, R_usd, "OPP_CLOSE_THRU"))
            cur_entry_idx = entry_idx
            cur_dir = new_side
        # Force-flat: close any open trade at last RTH bar's close.
        if cur_entry_idx is not None:
            exit_idx = n - 1
            exit_price = float(c[exit_idx])
            trades.append(_make_trade(
                date, cur_dir, cur_entry_idx, exit_idx, exit_price,
                o, c, times, regime, R_usd, "FORCE_FLAT"))
    return trades


def _make_trade(date, direction: int, entry_idx: int, exit_idx: int,
                exit_price: float, o, c, times, regime, R_usd, reason: str
                ) -> dict:
    entry_price = float(o[entry_idx])
    pnl_pts = (exit_price - entry_price) * direction
    net = pnl_pts * POINT_VALUE - COST_RT
    net_r = (net / R_usd) if (R_usd is not None and R_usd > 0) else None
    return {
        "date": str(date),
        "direction": "LONG" if direction == 1 else "SHORT",
        "entry_idx": int(entry_idx),
        "exit_idx": int(exit_idx),
        "entry_time": times[entry_idx].strftime("%H:%M"),
        "exit_time": times[exit_idx].strftime("%H:%M"),
        "tod": tod_bucket(times[entry_idx]),
        "entry": round(entry_price, 2),
        "exit": round(exit_price, 2),
        "exit_reason": reason,
        "bars_held": int(exit_idx - entry_idx),
        "net_pnl": round(net, 2),
        "net_r": round(net_r, 5) if net_r is not None else None,
        "regime": regime,
        "atr14_usd": round(R_usd, 2) if R_usd is not None else None,
    }


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------
def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    nets = [t["net_pnl"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    nrs = [t["net_r"] for t in trades if t["net_r"] is not None]
    avg_win = (gp / len(wins)) if wins else 0.0
    avg_loss = (gl / len(losses)) if losses else 0.0
    bars = [t["bars_held"] for t in trades]
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_net_per_trade": round(sum(nets) / n, 2),
        "total_net": round(sum(nets), 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "realized_rr": round(avg_win / avg_loss, 3) if avg_loss > 0 else None,
        "avg_net_r": round(sum(nrs) / len(nrs), 5) if nrs else None,
        "median_net_r": round(statistics.median(nrs), 5) if nrs else None,
        "avg_bars_held": round(sum(bars) / n, 1),
    }


def split_by_tod_regime(trades: list[dict]) -> dict:
    cells: dict = {}
    for tod in ("morning", "midday", "close"):
        for regime in ("range", "trending"):
            sub = [t for t in trades
                   if t["tod"] == tod and t["regime"] == regime]
            cells[f"{tod}_{regime}"] = summarize(sub)
    cells["unclassified_or_other_excluded"] = sum(
        1 for t in trades
        if t["tod"] not in ("morning", "midday", "close")
        or t["regime"] not in ("range", "trending"))
    return cells


# --------------------------------------------------------------------------
# PART 4 — bootstrap CI + null model
# --------------------------------------------------------------------------
def bootstrap_ci(net_rs: list[float], iters: int = BOOTSTRAP_ITERS) -> dict:
    arr = np.asarray(net_rs, dtype=float)
    n = len(arr)
    if n < 2:
        return {"n": n, "mean_net_r": None, "ci_low": None,
                "ci_high": None, "ci_spans_zero": None,
                "ci_fully_positive": None}
    rng = np.random.default_rng(2026)
    means = arr[rng.integers(0, n, size=(iters, n))].mean(axis=1)
    means.sort()
    lo = float(means[int(0.025 * iters)])
    hi = float(means[int(0.975 * iters)])
    return {
        "n": n,
        "iterations": iters,
        "mean_net_r": round(float(arr.mean()), 5),
        "ci_low": round(lo, 5),
        "ci_high": round(hi, 5),
        "ci_spans_zero": bool(lo < 0.0 < hi),
        "ci_fully_positive": bool(lo > 0.0),
    }


def null_model(real_trades: list[dict], sessions: dict,
               iters: int = NULL_ITERS, offset: int = NULL_OFFSET_MIN) -> dict:
    """Random-entry null: shift each real entry by U[-offset, +offset] minutes,
    same direction, same exit rule (next opposite close-through or force-flat).

    p = fraction of iterations where the mean null net-R >= mean real net-R.
    """
    real_nrs = [t["net_r"] for t in real_trades if t["net_r"] is not None]
    if len(real_nrs) < 30:
        return {"n_real": len(real_nrs), "p_value": None,
                "note": "too few real trades with defined net-R"}
    real_mean = float(np.mean(real_nrs))

    # Precompute per-real-trade the candidate entry-bar indices and the
    # constant R_usd needed for net-R.
    candidates: list[tuple[dict, int, int, float]] = []  # (session, dir, entry_idx_real, R_usd)
    trade_records: list[tuple[dict, int, np.ndarray, float]] = []  # (session, dir, cand_idxs, R_usd)
    for t in real_trades:
        if t["net_r"] is None:
            continue
        date = pd.to_datetime(t["date"]).date()
        s = sessions.get(date)
        if s is None or s["last_entry_idx"] < 1:
            continue
        n = s["n"]
        entry_real = t["entry_idx"]
        d = 1 if t["direction"] == "LONG" else -1
        lo_idx = max(1, entry_real - offset)
        hi_idx = min(n - 2, entry_real + offset, s["last_entry_idx"])
        if hi_idx < lo_idx:
            continue
        cands = np.arange(lo_idx, hi_idx + 1, dtype=np.int64)
        R = t["atr14_usd"]
        trade_records.append((s, d, cands, R))

    rng = np.random.default_rng(404)
    beats = 0
    null_means = np.empty(iters, dtype=float)
    for it in range(iters):
        total = 0.0
        m = 0
        for s, d, cands, R in trade_records:
            k = int(cands[rng.integers(0, len(cands))])
            n = s["n"]
            o, c_ = s["o"], s["c"]
            entry_idx = k    # we treat random k as the entry bar directly
            if entry_idx >= n:
                continue
            entry_price = float(o[entry_idx])
            if d == 1:
                sig = s["next_minus"][entry_idx]
            else:
                sig = s["next_plus"][entry_idx]
            if sig != -1 and sig + 1 < n:
                exit_price = float(o[sig + 1])
            else:
                exit_price = float(c_[n - 1])
            net = (exit_price - entry_price) * d * POINT_VALUE - COST_RT
            if R is not None and R > 0:
                total += net / R
                m += 1
        null_means[it] = total / m if m > 0 else 0.0
        if null_means[it] >= real_mean:
            beats += 1
    return {
        "n_real_trades": len(trade_records),
        "iterations": iters,
        "offset_minutes": offset,
        "real_mean_net_r": round(real_mean, 5),
        "null_mean_net_r_avg": round(float(np.mean(null_means)), 5),
        "p_value_real_geq_null": round(beats / iters, 4),
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    print("Loading NQ 1-min RTH bars ...")
    df = load_nq_rth()
    daily = daily_rth_bars(df)
    print(f"  RTH bars: {len(df)}  trading days: {len(daily)}  "
          f"range {daily['date'].iloc[0]} .. {daily['date'].iloc[-1]}")

    regimes = regime_labels(daily)
    atr14_usd = compute_atr14_usd(daily)
    n_range = sum(1 for v in regimes.values() if v == "range")
    n_trend = sum(1 for v in regimes.values() if v == "trending")
    print(f"  Regime — range: {n_range}  trending: {n_trend}  "
          f"unclassified: {len(regimes) - n_range - n_trend}")

    print("Building per-session arrays ...")
    sessions: dict = {}
    for date, day in df.groupby("date", sort=True):
        sessions[date] = build_session(day)
    print(f"  Sessions: {len(sessions)}")

    print("PART 1 — paper-figure-2 replication on NQ ...")
    part1 = part1_repricing(sessions)

    print("PART 2 — tradeable stop-and-reverse ...")
    trades = build_trades(sessions, regimes, atr14_usd)
    print(f"  Trades: {len(trades)}")
    part2 = summarize(trades)

    print("PART 3 — TOD x regime split ...")
    part3 = split_by_tod_regime(trades)

    # Select best PART-3 cell by avg_net_per_trade (min trade count).
    best_name, best_avg = None, None
    for name, s in part3.items():
        if not isinstance(s, dict):
            continue
        if s.get("trades", 0) >= MIN_TRADES_FOR_BEST:
            a = s["avg_net_per_trade"]
            if best_avg is None or a > best_avg:
                best_name, best_avg = name, a

    print("PART 4 — bootstrap + null model on best cell ...")
    if best_name is None:
        part4 = {"selected_cell": None,
                 "note": f"no cell has >= {MIN_TRADES_FOR_BEST} trades"}
        verdict = "NO PULSE — insufficient sample"
    else:
        tod_label, regime_label = best_name.split("_")
        cell_trades = [t for t in trades
                       if t["tod"] == tod_label and t["regime"] == regime_label]
        ci = bootstrap_ci([t["net_r"] for t in cell_trades
                           if t["net_r"] is not None])
        nm = null_model(cell_trades, sessions)
        ci_pos = bool(ci.get("ci_fully_positive"))
        p = nm.get("p_value_real_geq_null")
        p_sig = (p is not None and p < 0.05)
        if ci_pos and p_sig:
            verdict = "PULSE — CI fully positive AND null-model p < 0.05"
        else:
            reasons = []
            if not ci_pos:
                reasons.append("CI not fully positive")
            if not p_sig:
                reasons.append(f"null-model p={p}")
            verdict = "NO PULSE — " + ", ".join(reasons)
        part4 = {
            "selected_cell": best_name,
            "selection_basis": "highest avg_net_per_trade (TOD x regime cell)",
            "cell_avg_net_per_trade": best_avg,
            "bootstrap_ci_net_r": ci,
            "null_model": nm,
            "decision_rule": ("CI spans zero OR null p>0.05 -> NO PULSE; "
                              "CI fully positive AND p<0.05 -> PULSE"),
            "verdict": verdict,
        }

    payload = {
        "generated_at": datetime.now(NY).isoformat(),
        "purpose": ("VWAP-regime falsification — Zarattini/Aziz thesis on NQ. "
                    "Reconnaissance only, NOT a strategy build."),
        "data": {
            "source": "data/historical/NQ_1m_2022_2026.dbn.zst",
            "session": "RTH 09:30-16:00 NY",
            "rth_bars": len(df),
            "trading_days": len(daily),
            "date_range": [str(daily["date"].iloc[0]),
                           str(daily["date"].iloc[-1])],
        },
        "cost_model": {"round_trip_usd": COST_RT,
                       "point_value_usd": POINT_VALUE,
                       "contracts": 1},
        "trade_model": {
            "entry": ("close-through VWAP (signal bar closes on opposite side); "
                      "fill at next-bar open"),
            "exit": ("opposite close-through (also next-bar open, kicks off "
                     "the reverse trade) OR force-flat at 15:59"),
            "no_stop": ("stop-and-reverse — no fixed stop; net-R uses "
                        "14-day daily ATR x $20 as risk unit"),
        },
        "regime_classification": "TrendRegimeGate efficiency ratio (gates.py)",
        "tod_buckets": {
            "morning": "09:30-12:00",
            "midday": "12:00-15:00",
            "close": "15:00-16:00",
        },
        "part1_paper_repro": part1,
        "part2_tradeable_summary": part2,
        "part3_tod_regime_split": part3,
        "part4_pulse_test": part4,
    }
    out = REPO / "results" / "vwap_recon.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved: {out.relative_to(REPO)}")

    # ---- console report ----
    print()
    print("=" * 86)
    print("PART 1 — Paper-Figure-2 replication on NQ")
    print("=" * 86)
    print(f"  above-VWAP bars: {part1['above_vwap_bars']:>7}  "
          f"sum 1-min change: ${part1['sum_change_usd_above_vwap']:>12,.0f}  "
          f"avg/bar: ${part1['avg_change_per_bar_usd_above']:+.4f}")
    print(f"  below-VWAP bars: {part1['below_vwap_bars']:>7}  "
          f"sum 1-min change: ${part1['sum_change_usd_below_vwap']:>12,.0f}  "
          f"avg/bar: ${part1['avg_change_per_bar_usd_below']:+.4f}")
    print(f"  thesis holds (above>0 AND below<0): {part1['thesis_holds']}")

    print()
    print("=" * 86)
    print("PART 2 — Tradeable stop-and-reverse, full data")
    print("=" * 86)
    print(f"  trades         : {part2.get('trades')}")
    if part2.get("trades"):
        print(f"  WR             : {part2['win_rate']*100:.1f}%")
        print(f"  avg net/trade  : ${part2['avg_net_per_trade']:+.2f}")
        print(f"  total net      : ${part2['total_net']:+,.0f}")
        print(f"  profit factor  : {part2['profit_factor']}")
        print(f"  realized R:R   : {part2['realized_rr']}")
        print(f"  avg net-R      : {part2['avg_net_r']}")
        print(f"  avg bars held  : {part2['avg_bars_held']}")

    print()
    print("=" * 86)
    print("PART 3 — TOD x regime cells (avgNet$ / n)")
    print("=" * 86)
    print(f"{'cell':<22}{'trades':>8}{'WR':>8}{'avgNet$':>11}"
          f"{'PF':>7}{'avgNetR':>10}")
    for name in ("morning_range", "morning_trending",
                 "midday_range", "midday_trending",
                 "close_range", "close_trending"):
        s = part3.get(name, {})
        if s.get("trades", 0) == 0:
            print(f"{name:<22}{0:>8}")
            continue
        print(f"{name:<22}{s['trades']:>8}{s['win_rate']:>8.3f}"
              f"{s['avg_net_per_trade']:>11.2f}"
              f"{(s['profit_factor'] or 0):>7.2f}"
              f"{(s['avg_net_r'] or 0):>10.5f}")

    print()
    print("=" * 86)
    print("PART 4 — pulse test on best cell")
    print("=" * 86)
    if part4.get("selected_cell"):
        ci = part4["bootstrap_ci_net_r"]
        nm = part4["null_model"]
        print(f"  best cell      : {part4['selected_cell']}")
        print(f"  avgNet/trade   : ${part4['cell_avg_net_per_trade']:+.2f}")
        print(f"  bootstrap net-R: mean {ci['mean_net_r']}  "
              f"CI [{ci['ci_low']}, {ci['ci_high']}]  n={ci['n']}")
        print(f"  null model     : real {nm['real_mean_net_r']}  "
              f"null_avg {nm['null_mean_net_r_avg']}  "
              f"p={nm['p_value_real_geq_null']}")
        print(f"  VERDICT        : {part4['verdict']}")
    else:
        print(f"  {part4.get('note')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
