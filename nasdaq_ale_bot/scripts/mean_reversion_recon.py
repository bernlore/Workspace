#!/usr/bin/env python3
"""Mean-reversion signal reconnaissance — vectorized pulse check on NQ.

NOT a strategy build. No state machine. This is a fast, per-session vectorized
test of whether the raw mean-reversion premise shows any pulse on NQ before a
full strategy is built. Pure reconnaissance — no parameter optimisation beyond
the explicitly listed Signal-A sigma sweep.

Data : data/historical/NQ_1m_2022_2026.dbn.zst (front-month, RTH 09:30-16:00 NY)
Cost : locked $19 round-trip / NQ contract ($9 commission + $10 slippage),
       $20 / point. 1 contract per trade. Applied as a flat deduction.

PART 1 — three raw reversion signals, full data 2022-2026:
  A  VWAP-extension fade   — fade price > X sigma from session VWAP (X=1.5/2/2.5)
  B  prior-day-level fade  — fade pokes above PDH / below PDL, prior day not a
                             trend day
  C  opening-range fade    — fade the 15-min OR breakout back into the range

PART 2 — regime split: re-run PART 1 split by the TrendRegimeGate 10-day
  efficiency ratio (range-bound vs trending days).

PART 3 — bootstrap 95% CI (10,000 iter) on net-R per trade for the signal with
  the best regime-filtered avg-net. CI spans zero -> no pulse. CI fully
  positive -> pulse, full build justified.

Unified, non-optimised trade model (the signal *definition*, not a tuned knob):
  entry  : next 1-min bar open after the trigger bar
  target : the mean reference — A: session VWAP (dynamic); B: prior-day
           midpoint; C: opening-range midpoint
  stop   : fixed, symmetric — same point distance on the far side of entry as
           the reference is on the near side (nominal 1:1 at entry)
  exit   : target touch, stop touch (stop checked first on a tie = pessimistic),
           or force-flat at 15:59 NY
  net    : (exit-entry)*dir*$20 - $19

Output: results/mean_reversion_recon.json
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")

POINT_VALUE = 20.0
COST_RT = 19.0                       # locked $19 round-trip / contract
RTH_OPEN = time(9, 30)
RTH_LAST_ENTRY = time(15, 50)        # no new entries after this
RTH_CLOSE = time(15, 59)             # force-flat bar
OR_BARS = 15                         # 09:30-09:44 opening range
VWAP_MIN_BARS = 30                   # sigma bands valid only after 30 RTH bars

SIGNAL_A_SIGMAS = [1.5, 2.0, 2.5]    # the ONLY swept parameter

# TrendRegimeGate regime classification (existing component, gates.py).
REGIME_LOOKBACK_DAYS = 10
REGIME_EFFICIENCY_RATIO = 3.0        # gate default

# Prior-day trend-day filter for Signal B (close in the extreme 25% of range).
TREND_DAY_EXTREME_FRAC = 0.25


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
def load_nq_rth() -> pd.DataFrame:
    """Load front-month NQ 1-min bars, RTH only, NY-tz, sorted."""
    import databento as db

    store = db.DBNStore.from_file(
        REPO / "data/historical/NQ_1m_2022_2026.dbn.zst")
    df = store.to_df().reset_index()
    mask = (df["symbol"].str.startswith("NQ", na=False)
            & ~df["symbol"].str.contains("-", na=False, regex=False))
    df = df[mask].sort_values(["ts_event", "volume"], ascending=[True, False])
    df = df.drop_duplicates(subset=["ts_event"], keep="first")
    df = df[["ts_event", "open", "high", "low", "close", "volume"]].copy()
    df["ts"] = df["ts_event"].dt.tz_convert(NY)
    df = df.drop(columns=["ts_event"])
    df["date"] = df["ts"].dt.date
    df["t"] = df["ts"].dt.time
    df = df[(df["t"] >= RTH_OPEN) & (df["t"] <= RTH_CLOSE)]
    return df.sort_values("ts").reset_index(drop=True)


def daily_rth_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Per-day RTH OHLC — used for PDH/PDL, trend-day, regime."""
    g = df.groupby("date")
    daily = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
    })
    return daily.reset_index()


def regime_labels(daily: pd.DataFrame) -> dict:
    """Label each day range/trending via the TrendRegimeGate efficiency ratio.

    efficiency = |net_move| / adr over the trailing 10 CLOSED daily bars
    (days D-10..D-1, so the label is known at day D's open — no look-ahead).
    """
    labels: dict = {}
    closes = daily["close"].to_numpy(dtype=float)
    highs = daily["high"].to_numpy(dtype=float)
    lows = daily["low"].to_numpy(dtype=float)
    dates = daily["date"].tolist()
    for i, d in enumerate(dates):
        if i < REGIME_LOOKBACK_DAYS:
            labels[d] = None
            continue
        c = closes[i - REGIME_LOOKBACK_DAYS:i]
        h = highs[i - REGIME_LOOKBACK_DAYS:i]
        lo = lows[i - REGIME_LOOKBACK_DAYS:i]
        if c[0] <= 0:
            labels[d] = None
            continue
        net_move = (c[-1] - c[0]) / c[0]
        ranges = (h - lo) / np.where(c > 0, c, np.nan)
        adr = np.nanmean(ranges)
        if not np.isfinite(adr) or adr <= 0:
            labels[d] = None
            continue
        efficiency = abs(net_move) / adr
        labels[d] = ("trending" if efficiency > REGIME_EFFICIENCY_RATIO
                     else "range")
    return labels


def trend_day_flags(daily: pd.DataFrame) -> dict:
    """True if a day closed in the extreme 25% of its RTH range (trend day)."""
    flags: dict = {}
    for r in daily.itertuples(index=False):
        rng = r.high - r.low
        if rng <= 0:
            flags[r.date] = False
            continue
        pos = (r.close - r.low) / rng
        flags[r.date] = (pos >= 1 - TREND_DAY_EXTREME_FRAC
                         or pos <= TREND_DAY_EXTREME_FRAC)
    return flags


# --------------------------------------------------------------------------
# Exit resolution (shared by all signals)
# --------------------------------------------------------------------------
def resolve_trade(direction: str, entry_idx: int, entry_price: float,
                  stop_price: float, target_fixed: float | None,
                  target_arr: np.ndarray | None,
                  h: np.ndarray, l: np.ndarray, c: np.ndarray,
                  end_idx: int) -> tuple[float, int, str]:
    """Scan forward from entry_idx; return (exit_price, exit_idx, reason).

    Stop is checked before target on a same-bar tie (pessimistic).
    """
    for j in range(entry_idx, end_idx + 1):
        tgt = target_fixed if target_fixed is not None else float(target_arr[j])
        if direction == "SHORT":
            if h[j] >= stop_price:
                return stop_price, j, "STOP"
            if l[j] <= tgt:
                return tgt, j, "TARGET"
        else:  # LONG
            if l[j] <= stop_price:
                return stop_price, j, "STOP"
            if h[j] >= tgt:
                return tgt, j, "TARGET"
    return float(c[end_idx]), end_idx, "TIME"


def make_trade(direction: str, date, entry_price: float, exit_price: float,
               ref_dist: float, reason: str, regime) -> dict:
    pnl_pts = (exit_price - entry_price) * (1.0 if direction == "LONG" else -1.0)
    net = pnl_pts * POINT_VALUE - COST_RT
    risk_usd = ref_dist * POINT_VALUE
    return {
        "date": str(date),
        "direction": direction,
        "entry": round(entry_price, 2),
        "exit": round(exit_price, 2),
        "exit_reason": reason,
        "net_pnl": round(net, 2),
        "risk_usd": round(risk_usd, 2),
        "net_r": round(net / risk_usd, 4) if risk_usd > 0 else 0.0,
        "regime": regime,
    }


# --------------------------------------------------------------------------
# Signal A — VWAP-extension fade
# --------------------------------------------------------------------------
def signal_a(df: pd.DataFrame, x_sigma: float, regimes: dict) -> list[dict]:
    trades: list[dict] = []
    for date, day in df.groupby("date", sort=True):
        o = day["open"].to_numpy(float)
        h = day["high"].to_numpy(float)
        l = day["low"].to_numpy(float)
        c = day["close"].to_numpy(float)
        v = day["volume"].to_numpy(float)
        times = day["t"].tolist()
        n = len(c)
        if n < VWAP_MIN_BARS + 2:
            continue
        tp = (h + l + c) / 3.0
        cv = np.cumsum(v)
        cvtp = np.cumsum(v * tp)
        cvtp2 = np.cumsum(v * tp * tp)
        with np.errstate(invalid="ignore", divide="ignore"):
            vwap = cvtp / cv
            var = cvtp2 / cv - vwap * vwap
        sigma = np.sqrt(np.clip(var, 0.0, None))
        regime = regimes.get(date)
        i = VWAP_MIN_BARS
        while i < n - 1:
            if times[i] > RTH_LAST_ENTRY or not np.isfinite(sigma[i]) \
                    or sigma[i] <= 0:
                i += 1
                continue
            band = x_sigma * sigma[i]
            direction = None
            if c[i] > vwap[i] + band:
                direction = "SHORT"
            elif c[i] < vwap[i] - band:
                direction = "LONG"
            if direction is None:
                i += 1
                continue
            entry_idx = i + 1
            entry = o[entry_idx]
            ref = vwap[i]                       # mean reference at signal
            ref_dist = abs(entry - ref)
            if ref_dist <= 0:
                i += 1
                continue
            stop = entry + ref_dist if direction == "SHORT" \
                else entry - ref_dist
            exit_price, exit_idx, reason = resolve_trade(
                direction, entry_idx, entry, stop, None, vwap,
                h, l, c, n - 1)
            trades.append(make_trade(direction, date, entry, exit_price,
                                     ref_dist, reason, regime))
            i = exit_idx + 1
    return trades


# --------------------------------------------------------------------------
# Signal B — prior-day-level fade
# --------------------------------------------------------------------------
def signal_b(df: pd.DataFrame, daily: pd.DataFrame, regimes: dict,
             trend_days: dict) -> list[dict]:
    trades: list[dict] = []
    drows = list(daily.itertuples(index=False))
    prior = {drows[i].date: drows[i - 1] for i in range(1, len(drows))}
    for date, day in df.groupby("date", sort=True):
        pd_bar = prior.get(date)
        if pd_bar is None or trend_days.get(pd_bar.date, False):
            continue
        pdh, pdl = float(pd_bar.high), float(pd_bar.low)
        pdmid = (pdh + pdl) / 2.0
        if pdh <= pdl:
            continue
        o = day["open"].to_numpy(float)
        h = day["high"].to_numpy(float)
        l = day["low"].to_numpy(float)
        c = day["close"].to_numpy(float)
        times = day["t"].tolist()
        n = len(c)
        regime = regimes.get(date)
        i = 0
        while i < n - 1:
            if times[i] > RTH_LAST_ENTRY:
                break
            direction = None
            if h[i] > pdh:
                direction = "SHORT"
            elif l[i] < pdl:
                direction = "LONG"
            if direction is None:
                i += 1
                continue
            entry_idx = i + 1
            entry = o[entry_idx]
            ref_dist = abs(entry - pdmid)
            if ref_dist <= 0:
                i += 1
                continue
            stop = entry + ref_dist if direction == "SHORT" \
                else entry - ref_dist
            exit_price, exit_idx, reason = resolve_trade(
                direction, entry_idx, entry, stop, pdmid, None,
                h, l, c, n - 1)
            trades.append(make_trade(direction, date, entry, exit_price,
                                     ref_dist, reason, regime))
            i = exit_idx + 1
    return trades


# --------------------------------------------------------------------------
# Signal C — opening-range fade (the inverse of ORB)
# --------------------------------------------------------------------------
def signal_c(df: pd.DataFrame, regimes: dict) -> list[dict]:
    trades: list[dict] = []
    for date, day in df.groupby("date", sort=True):
        o = day["open"].to_numpy(float)
        h = day["high"].to_numpy(float)
        l = day["low"].to_numpy(float)
        c = day["close"].to_numpy(float)
        times = day["t"].tolist()
        n = len(c)
        if n < OR_BARS + 2:
            continue
        orh = float(h[:OR_BARS].max())
        orl = float(l[:OR_BARS].min())
        ormid = (orh + orl) / 2.0
        if orh <= orl:
            continue
        regime = regimes.get(date)
        i = OR_BARS
        while i < n - 1:
            if times[i] > RTH_LAST_ENTRY:
                break
            direction = None
            if c[i] > orh:
                direction = "SHORT"
            elif c[i] < orl:
                direction = "LONG"
            if direction is None:
                i += 1
                continue
            entry_idx = i + 1
            entry = o[entry_idx]
            ref_dist = abs(entry - ormid)
            if ref_dist <= 0:
                i += 1
                continue
            stop = entry + ref_dist if direction == "SHORT" \
                else entry - ref_dist
            exit_price, exit_idx, reason = resolve_trade(
                direction, entry_idx, entry, stop, ormid, None,
                h, l, c, n - 1)
            trades.append(make_trade(direction, date, entry, exit_price,
                                     ref_dist, reason, regime))
            i = exit_idx + 1
    return trades


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
    net_rs = [t["net_r"] for t in trades]
    reasons: dict = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    avg_win = (gp / len(wins)) if wins else 0.0
    avg_loss = (gl / len(losses)) if losses else 0.0
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_net_per_trade": round(sum(nets) / n, 2),
        "total_net": round(sum(nets), 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "realized_rr": round(avg_win / avg_loss, 3) if avg_loss > 0 else None,
        "avg_net_r": round(sum(net_rs) / n, 4),
        "median_net_r": round(statistics.median(net_rs), 4),
        "avg_risk_usd": round(sum(t["risk_usd"] for t in trades) / n, 2),
        "exit_reasons": reasons,
    }


def split_by_regime(trades: list[dict]) -> dict:
    out: dict = {}
    for label in ("range", "trending"):
        sub = [t for t in trades if t["regime"] == label]
        out[label] = summarize(sub)
    unclassified = sum(1 for t in trades if t["regime"] is None)
    out["unclassified_trades_excluded"] = unclassified
    return out


def bootstrap_ci(net_rs: list[float], iters: int = 10_000) -> dict:
    n = len(net_rs)
    if n < 2:
        return {"n": n, "mean_net_r": None, "ci_low": None, "ci_high": None,
                "ci_spans_zero": None}
    rng = np.random.default_rng(2026)
    arr = np.asarray(net_rs, dtype=float)
    means = arr[rng.integers(0, n, size=(iters, n))].mean(axis=1)
    means.sort()
    lo = float(means[int(0.025 * iters)])
    hi = float(means[int(0.975 * iters)])
    return {
        "n": n,
        "iterations": iters,
        "mean_net_r": round(float(arr.mean()), 4),
        "ci_low": round(lo, 4),
        "ci_high": round(hi, 4),
        "ci_spans_zero": bool(lo < 0.0 < hi),
        "ci_fully_positive": bool(lo > 0.0),
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
    trend_days = trend_day_flags(daily)
    n_range = sum(1 for v in regimes.values() if v == "range")
    n_trend = sum(1 for v in regimes.values() if v == "trending")
    print(f"  Regime days — range-bound: {n_range}  trending: {n_trend}  "
          f"unclassified: {len(regimes) - n_range - n_trend}")

    # ---- PART 1 — raw signals ----
    print("PART 1 — raw signals ...")
    variants: dict[str, list[dict]] = {}
    for x in SIGNAL_A_SIGMAS:
        variants[f"A_vwap_fade_{x}sigma"] = signal_a(df, x, regimes)
    variants["B_prior_day_level_fade"] = signal_b(df, daily, regimes, trend_days)
    variants["C_opening_range_fade"] = signal_c(df, regimes)

    part1 = {name: summarize(tr) for name, tr in variants.items()}

    # ---- PART 2 — regime split ----
    print("PART 2 — regime split ...")
    part2 = {name: split_by_regime(tr) for name, tr in variants.items()}

    # ---- PART 3 — bootstrap CI on best regime-filtered signal ----
    print("PART 3 — bootstrap CI ...")
    MIN_TRADES = 30
    best_name, best_avg = None, None
    for name, rs in part2.items():
        rb = rs["range"]
        if rb.get("trades", 0) >= MIN_TRADES:
            avg = rb["avg_net_per_trade"]
            if best_avg is None or avg > best_avg:
                best_name, best_avg = name, avg
    def verdict_for(ci: dict) -> str:
        if ci["ci_spans_zero"]:
            return "NO PULSE — CI spans zero"
        if ci["ci_fully_positive"]:
            return "PULSE — CI fully positive, full build justified"
        return "NEGATIVE — CI fully below zero"

    part3: dict
    if best_name is None:
        part3 = {"selected_signal": None,
                 "note": (f"No signal has >= {MIN_TRADES} range-bound-regime "
                          f"trades; bootstrap not meaningful.")}
    else:
        rb_trades = [t for t in variants[best_name] if t["regime"] == "range"]
        ci = bootstrap_ci([t["net_r"] for t in rb_trades])
        part3 = {
            "selected_signal": best_name,
            "selection_basis": "best range-bound-regime avg_net_per_trade",
            "range_regime_avg_net_per_trade": best_avg,
            "bootstrap_ci_net_r": ci,
            "verdict": verdict_for(ci),
        }
        # Anomaly check — the single highest-avg-net (signal, regime) cell
        # overall, even if it sits OUTSIDE the range-bound reversion premise.
        best_cell = None
        for name, rs in part2.items():
            for reg in ("range", "trending"):
                s = rs[reg]
                if s.get("trades", 0) >= MIN_TRADES:
                    a = s["avg_net_per_trade"]
                    if best_cell is None or a > best_cell[2]:
                        best_cell = (name, reg, a)
        if best_cell and (best_cell[0] != best_name or best_cell[1] != "range"):
            cname, creg, cavg = best_cell
            cell_trades = [t for t in variants[cname] if t["regime"] == creg]
            cci = bootstrap_ci([t["net_r"] for t in cell_trades])
            part3["anomaly_highest_avg_net_cell"] = {
                "signal": cname,
                "regime": creg,
                "avg_net_per_trade": cavg,
                "note": ("highest dollar avg-net cell overall; "
                         "outside the range-bound reversion premise"),
                "bootstrap_ci_net_r": cci,
                "verdict": verdict_for(cci),
            }

    payload = {
        "generated_at": datetime.now(NY).isoformat(),
        "purpose": ("Mean-reversion signal reconnaissance — vectorized pulse "
                    "check, NOT a strategy build. No state machine."),
        "data": {
            "source": "data/historical/NQ_1m_2022_2026.dbn.zst",
            "session": "RTH 09:30-16:00 NY",
            "rth_bars": len(df),
            "trading_days": len(daily),
            "date_range": [str(daily["date"].iloc[0]),
                           str(daily["date"].iloc[-1])],
        },
        "cost_model": {"round_trip_usd": COST_RT, "point_value_usd": POINT_VALUE,
                       "contracts": 1},
        "trade_model": {
            "entry": "next 1-min bar open after trigger bar",
            "target": "mean reference (A: VWAP dynamic; B: prior-day mid; "
                      "C: opening-range mid)",
            "stop": "fixed, symmetric to entry reference distance (nominal 1:1)",
            "exit": "target / stop (stop wins same-bar tie) / force-flat 15:59",
        },
        "regime_classification": {
            "method": "TrendRegimeGate efficiency ratio (gates.py)",
            "lookback_days": REGIME_LOOKBACK_DAYS,
            "efficiency_ratio_threshold": REGIME_EFFICIENCY_RATIO,
            "days_range_bound": n_range,
            "days_trending": n_trend,
        },
        "part1_raw_signals": part1,
        "part2_regime_split": part2,
        "part3_bootstrap": part3,
    }
    out = REPO / "results" / "mean_reversion_recon.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved: {out.relative_to(REPO)}")

    # ---- console report ----
    print()
    print("=" * 86)
    print("PART 1 — raw signals (full data, after $19 cost)")
    print("=" * 86)
    print(f"{'signal':<28}{'trades':>8}{'WR':>8}{'avgNet$':>10}"
          f"{'PF':>8}{'realRR':>9}{'avgNetR':>10}")
    for name, s in part1.items():
        if s.get("trades", 0) == 0:
            print(f"{name:<28}{'0':>8}")
            continue
        print(f"{name:<28}{s['trades']:>8}{s['win_rate']:>8.3f}"
              f"{s['avg_net_per_trade']:>10.2f}{(s['profit_factor'] or 0):>8.2f}"
              f"{(s['realized_rr'] or 0):>9.2f}{s['avg_net_r']:>10.4f}")
    print()
    print("=" * 86)
    print("PART 2 — regime split (avgNet$ / trades  per regime)")
    print("=" * 86)
    print(f"{'signal':<28}{'range avgNet':>16}{'range n':>10}"
          f"{'trend avgNet':>16}{'trend n':>10}")
    for name, rs in part2.items():
        rb, tr = rs["range"], rs["trending"]
        rb_a = rb.get("avg_net_per_trade", 0) if rb.get("trades") else 0
        tr_a = tr.get("avg_net_per_trade", 0) if tr.get("trades") else 0
        print(f"{name:<28}{rb_a:>16.2f}{rb.get('trades', 0):>10}"
              f"{tr_a:>16.2f}{tr.get('trades', 0):>10}")
    print()
    print("=" * 86)
    print("PART 3 — bootstrap CI on best regime-filtered signal")
    print("=" * 86)
    if part3.get("selected_signal"):
        ci = part3["bootstrap_ci_net_r"]
        print(f"  signal     : {part3['selected_signal']}")
        print(f"  range avgNet: ${part3['range_regime_avg_net_per_trade']}/trade")
        print(f"  net-R       : mean {ci['mean_net_r']}  "
              f"CI [{ci['ci_low']}, {ci['ci_high']}]  n={ci['n']}")
        print(f"  VERDICT     : {part3['verdict']}")
    else:
        print(f"  {part3['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
