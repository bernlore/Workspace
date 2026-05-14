#!/usr/bin/env python3
"""Phase 3 detection quality audit.

Replays the OOS window (2024-05-01..2024-06-30) with the winning IS params
(ifvg_tolerance=2, rr_cap=1.2, cisd_lookback=15) and captures per-trade
setup context: sweep bar, CISD reference candle, IFVG zone, HTF Bias at
entry, SMT verdict at entry, entry zone relative to swept-range Fib 0.5.

Does NOT modify detection logic — wraps handlers with a tee-style capture.
"""

from __future__ import annotations

import json
import logging
import random
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core import state_machine as sm_mod
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.state_machine import StrategyState
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "historical"
RESULTS_DIR = REPO_ROOT / "results"

OOS_START = date(2024, 3, 1)
OOS_END = date(2024, 6, 30)

# Best-IS params from phase3_oos_verdict.json
BEST_IFVG_TOL = 2
BEST_RR_CAP = Decimal("1.2")
BEST_CISD_LOOKBACK = 15


def install_capture(captures: dict, sweep_ctx: dict, cisd_ctx: dict) -> None:
    """Monkey-patch handlers to capture sweep bar, CISD bar, IFVG bar, bias, smt."""
    orig_waiting = sm_mod._handle_waiting_for_sweep
    orig_cisd = sm_mod._handle_cisd_confirmation
    orig_ifvg = sm_mod._handle_ifvg_formation
    orig_entry = sm_mod._handle_entry_execution

    def wrap_waiting(sm, view):
        result = orig_waiting(sm, view)
        new_state, reason = result
        if reason == "sweep_detected":
            bar = view[-1]
            sweep_ctx["last_sweep_bar"] = {
                "ts": bar.ts.isoformat(),
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "open": bar.open,
            }
        return result

    def wrap_cisd(sm, view):
        result = orig_cisd(sm, view)
        _, reason = result
        if reason in ("cisd_bullish", "cisd_bearish"):
            bar = view[-1]
            cisd_ctx["last_cisd_bar"] = {
                "ts": bar.ts.isoformat(),
                "close": bar.close,
                "open": bar.open,
                "body_close_up": bar.close > bar.open,
                "body_close_down": bar.close < bar.open,
            }
        return result

    def wrap_ifvg(sm, view):
        result = orig_ifvg(sm, view)
        _, reason = result
        if reason == "ifvg_ready":
            bar = view[-1]
            setup = sm._active_setup
            tol_ticks = int(sm._strategy_cfg.get("ifvg_tolerance_ticks", 0))
            tick = float(getattr(sm._instrument, "tick", 0.0) or 0.0)
            tol_offset = tol_ticks * tick
            if setup.bias == "LONG":
                ifvg_upper = bar.close
                ifvg_lower = bar.low - tol_offset
            else:
                ifvg_upper = bar.high + tol_offset
                ifvg_lower = bar.close
            cisd_ctx["last_ifvg_bar"] = {
                "ts": bar.ts.isoformat(),
                "upper": ifvg_upper,
                "lower": ifvg_lower,
                "tol_ticks": tol_ticks,
                "tol_offset": tol_offset,
            }
        return result

    def wrap_entry(sm, view):
        # Capture context BEFORE calling original so we snapshot pre-gate state.
        setup = sm._active_setup
        bar = view[-1]
        bias_state = None
        try:
            if sm._bias_detector is not None:
                # Read current bias without re-feeding the bar (detector already saw it).
                bias_state = getattr(sm._bias_detector, "_state", None) \
                    or getattr(sm._bias_detector, "state", None)
        except Exception:
            bias_state = None
        bias_at_entry = "NONE"
        if bias_state is not None:
            b = getattr(bias_state, "bias", None)
            if b is not None:
                bias_at_entry = str(b).split(".")[-1]
        smt_verdict = "NONE"
        if sm._smt_tracker is not None and hasattr(sm._smt_tracker, "verdict"):
            try:
                smt_verdict = str(sm._smt_tracker.verdict).split(".")[-1]
            except Exception:
                smt_verdict = "NONE"
        result = orig_entry(sm, view)
        _, reason = result
        # Only record if trade actually committed (order_submitted).
        if reason == "order_submitted" and setup is not None:
            # Fib 0.5 of pre-sweep dealing range — matches state_machine's filter.
            sweep_bar = sweep_ctx.get("last_sweep_bar")
            fib_05 = None
            in_discount = None
            in_premium = None
            if sweep_bar is not None and setup.sweep_idx is not None:
                look = 30
                start = max(0, setup.sweep_idx - look)
                pre_bars = [view[k] for k in range(start, setup.sweep_idx + 1)]
                if setup.bias == "LONG":
                    leg_lo = float(view[setup.sweep_idx].low)
                    leg_hi = max(b.high for b in pre_bars)
                    fib_05 = (leg_lo + leg_hi) / 2.0
                    in_discount = setup.entry_price <= fib_05
                else:
                    leg_hi = float(view[setup.sweep_idx].high)
                    leg_lo = min(b.low for b in pre_bars)
                    fib_05 = (leg_lo + leg_hi) / 2.0
                    in_premium = setup.entry_price >= fib_05
            captures.setdefault("_list", []).append({
                "arming_bar_ts": bar.ts,
                "arming_bar_iso": bar.ts.isoformat(),
                "entry_price": setup.entry_price,
                "stop_price": setup.stop_price,
                "take_profit": setup.take_profit,
                "side": "BUY" if setup.bias == "LONG" else "SELL",
                "bias_direction": setup.bias,
                "htf_bias_at_entry": bias_at_entry,
                "smt_at_entry": smt_verdict,
                "sweep_bar": dict(sweep_bar) if sweep_bar else None,
                "cisd_bar": dict(cisd_ctx.get("last_cisd_bar") or {}) or None,
                "ifvg_zone": dict(cisd_ctx.get("last_ifvg_bar") or {}) or None,
                "fib_0_5_of_sweep_range": fib_05,
                "in_discount_long": in_discount,
                "in_premium_short": in_premium,
            })
        return result

    sm_mod._handle_waiting_for_sweep = wrap_waiting
    sm_mod._handle_cisd_confirmation = wrap_cisd
    sm_mod._handle_ifvg_formation = wrap_ifvg
    sm_mod._handle_entry_execution = wrap_entry
    # Also rebuild handler tables on any already-constructed SM? We construct AFTER patch.


def slice_by_date(bars, start: date, end: date):
    return [b for b in bars if start <= b.ts.date() <= end]


def main() -> int:
    instruments_cfg = load_instruments_config(REPO_ROOT / "config" / "instruments.yaml")
    strategy_cfg = load_strategy_config(REPO_ROOT / "config" / "strategy.yaml")
    primary = instruments_cfg.primary
    correlated = instruments_cfg.correlated

    nq_path = DATA_DIR / "NQ_1m_2022_2026.dbn.zst"
    es_path = DATA_DIR / "ES_1m_2022_2026.dbn.zst"
    qqq_all = BacktestRunner.load_bars_from_dbn(nq_path, symbol_prefix="NQ")
    spy_all = BacktestRunner.load_bars_from_dbn(es_path, symbol_prefix="ES")
    # Replay end-to-end so the bias detector has the same warm-up the pipeline
    # gives it via its grid runs; we filter captures to OOS at the end.
    qqq_full = qqq_all
    spy_full = spy_all
    print(f"Full bars: NQ={len(qqq_full)} ES={len(spy_full)}")

    cfg = dict(strategy_cfg)
    cfg["_bias_detector"] = HTFBiasDetector(primary)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal("750")
    cfg.setdefault("default_qty", Decimal("1"))
    cfg["ifvg_tolerance_ticks"] = BEST_IFVG_TOL
    cfg["rr_cap"] = BEST_RR_CAP
    cfg["cisd_lookback_bars"] = BEST_CISD_LOOKBACK

    captures: dict[str, Any] = {}
    sweep_ctx: dict[str, Any] = {}
    cisd_ctx: dict[str, Any] = {}
    install_capture(captures, sweep_ctx, cisd_ctx)

    start_equity = Decimal("50000")
    point_value = Decimal(str(getattr(primary, "point_value", 1)))
    ledger = AccountLedger(session_start_equity=start_equity, today=qqq_full[0].ts.date())
    broker = MockBroker(
        ledger=ledger, initial_equity=start_equity, point_value=point_value
    )
    runner = BacktestRunner(
        bars_primary=qqq_full,
        bars_correlated=spy_full,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=primary,
        param_set_hash="audit",
    )
    result = runner.run()
    all_trades = result.trades
    # Filter trades to OOS window only.
    trades = [t for t in all_trades if OOS_START <= t.entry_ts.date() <= OOS_END]
    cap_list = captures.get("_list", [])
    cap_list.sort(key=lambda c: c["arming_bar_ts"])
    print(f"Full trades: {len(all_trades)}  OOS trades: {len(trades)}")
    print(f"Captured entry contexts: {len(cap_list)}")

    # Correlate each trade with the latest capture whose arming_ts < trade.entry_ts
    # and side matches. Captures that don't match any trade are orphaned (unfilled).
    def match_capture(trade) -> dict[str, Any]:
        want_side = trade.side  # "BUY" or "SELL"
        best = None
        for c in cap_list:
            if c["arming_bar_ts"] >= trade.entry_ts:
                break
            if c["side"] != want_side:
                continue
            best = c
        return best or {}

    per_trade: list[dict[str, Any]] = []
    for t in trades:
        ctx = match_capture(t)
        per_trade.append({
            "entry_ts": t.entry_ts.isoformat(),
            "exit_ts": t.exit_ts.isoformat(),
            "side": t.side,
            "entry_price_fill": str(t.entry_price),
            "exit_price": str(t.exit_price),
            "exit_reason": t.exit_reason,
            "pnl": str(t.realized_pnl),
            **ctx,
        })

    # Aggregate bias/smt distributions across all trades.
    bias_counts: dict[str, int] = {}
    smt_counts: dict[str, int] = {}
    for t in per_trade:
        bias_counts[t.get("htf_bias_at_entry", "NONE")] = \
            bias_counts.get(t.get("htf_bias_at_entry", "NONE"), 0) + 1
        smt_counts[t.get("smt_at_entry", "NONE")] = \
            smt_counts.get(t.get("smt_at_entry", "NONE"), 0) + 1
    n = len(per_trade)

    # Pick 5 random trades deterministically.
    rng = random.Random(42)
    sample_idx = sorted(rng.sample(range(n), min(5, n)))
    sample = [per_trade[i] for i in sample_idx]

    # Cross-tab: SMT alignment with bias (divergence direction matches bias?)
    smt_aligned = 0
    smt_opposite = 0
    smt_none = 0
    zone_compliant_longs = 0
    zone_compliant_shorts = 0
    zone_violation_longs = 0
    zone_violation_shorts = 0
    for t in per_trade:
        bias = t.get("bias_direction")
        smt = t.get("smt_at_entry")
        if smt == "NONE":
            smt_none += 1
        elif bias == "LONG" and smt == "BULLISH_DIVERGENCE":
            smt_aligned += 1
        elif bias == "SHORT" and smt == "BEARISH_DIVERGENCE":
            smt_aligned += 1
        elif bias == "LONG" and smt == "BEARISH_DIVERGENCE":
            smt_opposite += 1
        elif bias == "SHORT" and smt == "BULLISH_DIVERGENCE":
            smt_opposite += 1
        if bias == "LONG":
            if t.get("in_discount_long") is True:
                zone_compliant_longs += 1
            elif t.get("in_discount_long") is False:
                zone_violation_longs += 1
        elif bias == "SHORT":
            if t.get("in_premium_short") is True:
                zone_compliant_shorts += 1
            elif t.get("in_premium_short") is False:
                zone_violation_shorts += 1

    out = {
        "total_oos_trades": n,
        "bias_distribution": bias_counts,
        "smt_distribution": smt_counts,
        "bias_pct": {k: round(v / n * 100, 1) for k, v in bias_counts.items()} if n else {},
        "smt_pct": {k: round(v / n * 100, 1) for k, v in smt_counts.items()} if n else {},
        "smt_aligned_with_bias": smt_aligned,
        "smt_opposite_to_bias": smt_opposite,
        "smt_none": smt_none,
        "zone_compliance": {
            "longs_in_discount": zone_compliant_longs,
            "longs_NOT_in_discount": zone_violation_longs,
            "shorts_in_premium": zone_compliant_shorts,
            "shorts_NOT_in_premium": zone_violation_shorts,
        },
        "sample_indices": sample_idx,
        "sample_trades": sample,
        "all_trades": per_trade,
    }
    out_path = RESULTS_DIR / "phase3_detection_audit.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"Wrote {out_path}")

    # Pretty-print the 5-trade table + aggregates.
    print("\n===== 5 RANDOM OOS TRADES =====\n")
    for i, t in zip(sample_idx, sample):
        print(f"--- Trade #{i+1} ---")
        print(f"  entry_ts     = {t['entry_ts']}")
        print(f"  side         = {t['side']}  (bias={t.get('bias_direction')})")
        print(f"  entry_price  = {t['entry_price_fill']}  stop={t.get('stop_price')}  tp={t.get('take_profit')}")
        print(f"  exit_ts      = {t['exit_ts']}  exit_price={t['exit_price']}  reason={t['exit_reason']}  pnl={t['pnl']}")
        sb = t.get("sweep_bar")
        if sb:
            print(f"  sweep_bar    ts={sb['ts']}  H={sb['high']:.2f} L={sb['low']:.2f} C={sb['close']:.2f} O={sb.get('open', 0):.2f}")
        else:
            print("  sweep_bar    = MISSING")
        cb = t.get("cisd_bar")
        if cb:
            print(f"  cisd_bar     ts={cb.get('ts')}  C={cb.get('close')} O={cb.get('open')}  body_close_up={cb.get('body_close_up')}")
        else:
            print("  cisd_bar     = MISSING")
        iz = t.get("ifvg_zone")
        if iz:
            print(f"  ifvg_zone    ts={iz.get('ts')}  upper={iz.get('upper'):.4f} lower={iz.get('lower'):.4f} tol_ticks={iz.get('tol_ticks')} tol_offset={iz.get('tol_offset')}")
        else:
            print("  ifvg_zone    = MISSING")
        print(f"  htf_bias_at_entry = {t.get('htf_bias_at_entry')}")
        print(f"  smt_at_entry      = {t.get('smt_at_entry')}")
        fib = t.get("fib_0_5_of_sweep_range")
        print(f"  fib_0.5_of_sweep_range = {fib}")
        print(f"  in_discount(long)  = {t.get('in_discount_long')}")
        print(f"  in_premium(short)  = {t.get('in_premium_short')}")
        print("")

    print("===== AGGREGATE =====")
    print(f"Total OOS trades: {n}")
    print(f"HTF Bias distribution: {bias_counts}")
    print(f"HTF Bias pct: {out['bias_pct']}")
    print(f"SMT distribution: {smt_counts}")
    print(f"SMT pct: {out['smt_pct']}")
    print(f"SMT aligned with bias: {smt_aligned} ({round(smt_aligned/n*100,1)}%)")
    print(f"SMT opposite to bias: {smt_opposite} ({round(smt_opposite/n*100,1)}%)")
    print(f"SMT NONE (no divergence): {smt_none} ({round(smt_none/n*100,1)}%)")
    print(f"Longs in discount (Fib 0.5 OK): {zone_compliant_longs}/{zone_compliant_longs+zone_violation_longs}")
    print(f"Shorts in premium (Fib 0.5 OK): {zone_compliant_shorts}/{zone_compliant_shorts+zone_violation_shorts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
