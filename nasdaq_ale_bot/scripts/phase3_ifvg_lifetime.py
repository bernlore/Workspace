#!/usr/bin/env python3
"""IFVG-lifetime diagnostic for Jan 2024 NQ — frequency root-cause analysis.

For each IFVG that fires in Jan 2024 NQ (best-IS params), record:
  - formation timestamp + NY hour bucket
  - bias direction
  - zone (top, bottom)
  - in_killzone_at_formation
  - time_to_first_retest_bars / minutes (None if never retested)
  - retest_in_killzone (True if first retest landed inside primary or secondary killzone)
  - bars_until_invalidated (price body-closes through far edge)
  - lifetime_minutes (min of retest, invalidation, end-of-day)

Diagnosis only — no code changes.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.strategies.nasdaqale import state_machine as sm_mod
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.filters.killzone import in_primary_killzone, in_secondary_killzone
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
JAN_START = date(2024, 1, 1)
JAN_END = date(2024, 1, 31)


def slice_jan(bars):
    return [b for b in bars if JAN_START <= b.ts.date() <= JAN_END]


def main() -> int:
    primary_all = BacktestRunner.load_bars_from_dbn(
        REPO / "data" / "historical" / "NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    correlated_all = BacktestRunner.load_bars_from_dbn(
        REPO / "data" / "historical" / "ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
    )
    qqq = slice_jan(primary_all)
    spy = slice_jan(correlated_all)
    print(f"Jan 2024 bars: NQ={len(qqq):,} ES={len(spy):,}")

    cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
    inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal("750")
    cfg.setdefault("default_qty", Decimal("1"))
    cfg["ifvg_tolerance_ticks"] = 1
    cfg["rr_cap"] = Decimal("1.1")
    cfg["cisd_lookback_bars"] = 20

    # Patch the IFVG handler so we capture the FVG zone at formation time.
    captures: list[dict] = []
    orig_ifvg = sm_mod._handle_ifvg_formation

    def wrap_ifvg(sm, view):
        result = orig_ifvg(sm, view)
        _, reason = result
        if reason == "ifvg_ready":
            setup = sm._active_setup
            bar = view[-1]
            # Re-derive nearest IFVG by re-running detect_ifvg with the same args
            # so we can read fvg.top/bottom (handler doesn't store the FVG dataclass).
            from nasdaq_ale_bot.core.leg import Direction
            from nasdaq_ale_bot.strategies.nasdaqale.detection.ifvg import CISDRange, detect_ifvg

            direction = Direction.UP if setup.bias == "LONG" else Direction.DOWN
            sweep_bar = view[setup.sweep_idx]
            sweep_price = (
                sweep_bar.low if setup.bias == "LONG" else sweep_bar.high
            )
            tol_ticks = int(sm._strategy_cfg.get("ifvg_tolerance_ticks", 0))
            tick = float(getattr(sm._instrument, "tick", 0.0) or 0.0)
            tol_offset = tol_ticks * tick
            ifvgs = detect_ifvg(
                view,
                setup.cisd_confirm_idx,
                CISDRange(start=setup.sweep_idx, end=setup.cisd_confirm_idx),
                sweep_price=sweep_price,
                direction=direction,
                tol_offset=tol_offset,
            )
            if ifvgs:
                fvg = ifvgs[0].fvg
                captures.append(
                    {
                        "ts_utc": bar.ts,
                        "bias": setup.bias,
                        "zone_top": fvg.top,
                        "zone_bottom": fvg.bottom,
                        "formation_idx": len(view) - 1,
                    }
                )
        return result

    sm_mod._handle_ifvg_formation = wrap_ifvg

    ledger = AccountLedger(
        session_start_equity=Decimal("50000"), today=qqq[0].ts.date()
    )
    pv = Decimal(str(getattr(inst, "point_value", 1)))
    broker = MockBroker(
        ledger=ledger, initial_equity=Decimal("50000"), point_value=pv
    )
    runner = BacktestRunner(
        bars_primary=qqq,
        bars_correlated=spy,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=inst,
        param_set_hash="ifvg_lifetime",
    )
    runner.run()
    print(f"Captured {len(captures)} IFVGs in Jan 2024")

    # Build a fast lookup from ts -> bar for retest scan.
    bars_by_ts = {b.ts: b for b in qqq}
    sorted_ts = sorted(bars_by_ts)

    # ------------------------------------------------------------------
    # Question 1 — formation hour histogram (NY ET)
    # ------------------------------------------------------------------
    hour_hist: Counter = Counter()
    in_killzone_at_form = 0
    for c in captures:
        ny_hour = c["ts_utc"].astimezone(NY).hour
        hour_hist[ny_hour] += 1
        if in_primary_killzone(c["ts_utc"]) or in_secondary_killzone(c["ts_utc"]):
            in_killzone_at_form += 1

    print("\n===== Q1: IFVG formation by NY ET hour =====")
    print(f"in killzone at formation: {in_killzone_at_form}/{len(captures)} ({in_killzone_at_form/max(len(captures),1)*100:.1f}%)")
    for h in range(0, 24):
        n = hour_hist.get(h, 0)
        bar = "#" * n
        flag = ""
        if 9 <= h < 11 or h == 11:  # primary killzone NY 09:00-11:30
            flag = " (primary)"
        elif 13 <= h < 16:
            flag = " (secondary)"
        print(f"  {h:02d}:00  {n:>3}  {bar}{flag}")

    # ------------------------------------------------------------------
    # Question 2 — IFVG lifetime: time-to-retest / time-to-invalidation
    # ------------------------------------------------------------------
    print("\n===== Q2: IFVG zone lifetime (after formation) =====")
    retest_durations_min: list[int] = []
    retests_in_killzone = 0
    retests_total = 0
    invalidated_total = 0
    survives_session = 0
    multi_hour_retests = 0
    for c in captures:
        form_ts = c["ts_utc"]
        bias = c["bias"]
        top = c["zone_top"]
        bottom = c["zone_bottom"]
        # Walk forward bars within same calendar day (NY date).
        ny_form_date = form_ts.astimezone(NY).date()
        # Build forward bar list from formation
        # Use sorted_ts so we can find next bars in O(log n)+iteration.
        from bisect import bisect_right
        i_start = bisect_right(sorted_ts, form_ts)
        retested = False
        invalidated = False
        retest_minute = None
        # Cap at end-of-NY-day (use 24h bound to be safe even with overnight bars).
        for ts in sorted_ts[i_start : i_start + 60 * 24]:
            ny_date = ts.astimezone(NY).date()
            if ny_date > ny_form_date:
                break
            bar = bars_by_ts[ts]
            # Retest semantics:
            #  LONG IFVG: bullish setup, zone is bearish-FVG-inverted; after
            #             formation price is ABOVE the zone (close > top). A
            #             retest means price's LOW dips back into [bottom, top].
            #  SHORT IFVG: mirror; price below zone, retest = HIGH rises into.
            if bias == "LONG":
                if bar.low <= top:  # touches the zone from above
                    retested = True
                    retest_minute = int(
                        (ts - form_ts).total_seconds() // 60
                    )
                    break
                if bar.close < bottom:  # invalidated — gap below far edge
                    invalidated = True
                    break
            else:  # SHORT
                if bar.high >= bottom:  # touches the zone from below
                    retested = True
                    retest_minute = int(
                        (ts - form_ts).total_seconds() // 60
                    )
                    break
                if bar.close > top:
                    invalidated = True
                    break
        if retested:
            retests_total += 1
            retest_durations_min.append(retest_minute or 0)
            # Compute the retest ts to check killzone window
            retest_ts = form_ts + timedelta(minutes=retest_minute or 0)
            if in_primary_killzone(retest_ts) or in_secondary_killzone(retest_ts):
                retests_in_killzone += 1
            if (retest_minute or 0) >= 60:
                multi_hour_retests += 1
        elif invalidated:
            invalidated_total += 1
        else:
            survives_session += 1

    n = len(captures) or 1
    print(f"Outcomes within same NY session:")
    print(f"  retested by price (touched the zone): {retests_total}/{n} ({retests_total/n*100:.1f}%)")
    print(f"  invalidated (body-closed through far edge): {invalidated_total}/{n} ({invalidated_total/n*100:.1f}%)")
    print(f"  survived to end of session:           {survives_session}/{n} ({survives_session/n*100:.1f}%)")
    if retest_durations_min:
        retest_durations_min.sort()
        med = retest_durations_min[len(retest_durations_min) // 2]
        avg = sum(retest_durations_min) / len(retest_durations_min)
        p25 = retest_durations_min[len(retest_durations_min) // 4]
        p75 = retest_durations_min[len(retest_durations_min) * 3 // 4]
        print(f"\nRetest delay (formation -> first retest, minutes):")
        print(f"  median = {med}  p25 = {p25}  p75 = {p75}  mean = {avg:.0f}")
        print(f"  retests landing INSIDE a killzone window: {retests_in_killzone}/{retests_total} ({retests_in_killzone/retests_total*100:.1f}%)")
        print(f"  retests >=60 min after formation: {multi_hour_retests}/{retests_total} ({multi_hour_retests/retests_total*100:.1f}%)")

    # ------------------------------------------------------------------
    # Q3 — what would 'carry-forward IFVG' do for trade frequency?
    # Count IFVGs whose first retest is inside a killzone (= entries we'd take
    # under retest semantics) vs the current "fire-on-formation-bar-only" count.
    # ------------------------------------------------------------------
    fire_on_formation_inside_killzone = in_killzone_at_form
    fire_on_first_retest_inside_killzone = retests_in_killzone
    print("\n===== Q3 : Implied entry-count if IFVG carried forward =====")
    print(f"current rule (entry only on formation bar, IFVG must be in killzone):")
    print(f"  candidates: {fire_on_formation_inside_killzone}")
    print(f"carry-forward rule (entry on first price retest into IFVG zone, retest must be in killzone):")
    print(f"  candidates: {fire_on_first_retest_inside_killzone}")
    print(f"  multiplier vs current: x{fire_on_first_retest_inside_killzone/max(fire_on_formation_inside_killzone,1):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
