#!/usr/bin/env python3
"""Three-split walk-forward validation on full 2022-12 .. 2025-04 NQ data.

Single replay with best-IS params (tol=2, rr=1.1, lookback=20). Trades are
then partitioned by entry-date into three IS/OOS pairs:

  Split A:  IS 2022-12-26..2023-09-30   OOS 2023-10-01..2024-02-29
  Split B:  IS 2023-01-01..2024-02-28   OOS 2024-03-01..2024-06-30  (= existing)
  Split C:  IS 2023-06-01..2024-09-30   OOS 2024-10-01..2025-04-25

Per IS and OOS slice we report: trades, WR, avg_rr, PF.

ES (Databento) coverage 2023-11-20..2024-11-19. Where ES is absent the SMT
direction filter is fail-open (A13) — informational only, not a bug.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]

REPLAY_START = date(2022, 1, 1)
REPLAY_END = date(2025, 4, 30)

SPLITS = [
    ("A", date(2022, 1, 1),   date(2023, 9, 30), date(2023, 10, 1), date(2024, 2, 29)),
    ("B", date(2023, 1, 1),   date(2024, 2, 28), date(2024, 3, 1),  date(2024, 6, 30)),
    ("C", date(2023, 6, 1),   date(2024, 9, 30), date(2024, 10, 1), date(2025, 4, 25)),
]

BEST_PARAMS = dict(tol=2, rr=Decimal("1.1"), lookback=20)


def _slice(bars, start: date, end: date):
    return [b for b in bars if start <= b.ts.date() <= end]


def _metrics_subset(trades, start: date, end: date):
    sub = [t for t in trades if start <= t.entry_ts.date() <= end]
    return MetricsCalculator(risk_per_trade_usd=Decimal("750")).compute(
        trades=sub, equity_curve=[]
    ), sub


def main() -> int:
    import sys

    ml_gate_enabled = "--ml-gate" in sys.argv
    out_suffix = "_with_ml_gate" if ml_gate_enabled else ""
    nq = BacktestRunner.load_bars_from_dbn(
        REPO / "data" / "historical" / "NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    es = BacktestRunner.load_bars_from_dbn(
        REPO / "data" / "historical" / "ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
    )
    nq = _slice(nq, REPLAY_START, REPLAY_END)
    es_full = es  # ES range 2023-11-20..2024-11-19 — keep all; runner pads gaps.
    print(f"Replay window: {REPLAY_START} .. {REPLAY_END}")
    print(f"  NQ bars: {len(nq):,}   ES bars: {len(es_full):,}")

    cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
    inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    if ml_gate_enabled:
        cfg["ml_regime_gate_enabled"] = True
        cfg["ml_regime_predictions_csv"] = str(
            REPO / "data" / "ml_session_predictions.csv"
        )
        print("MLRegimeGate: ENABLED")
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal("750")
    cfg["entry_order_type"] = "LIMIT"
    cfg["entry_slippage_ticks"] = 0
    cfg.setdefault("default_qty", Decimal("1"))
    cfg["ifvg_tolerance_ticks"] = BEST_PARAMS["tol"]
    cfg["rr_cap"] = BEST_PARAMS["rr"]
    cfg["cisd_lookback_bars"] = BEST_PARAMS["lookback"]

    pv = Decimal(str(getattr(inst, "point_value", 1)))
    ledger = AccountLedger(
        session_start_equity=Decimal("50000"), today=nq[0].ts.date()
    )
    broker = MockBroker(
        ledger=ledger, initial_equity=Decimal("50000"), point_value=pv
    )
    runner = BacktestRunner(
        bars_primary=nq,
        bars_correlated=es_full,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=inst,
        param_set_hash="walkfwd_3splits",
    )
    # Tap SM events to record TrendRegimeGate rejections per bar.ts and
    # SMT availability at order_submitted moments.
    trend_gate_rejects: list = []
    order_smt_log: list[tuple[date, bool]] = []  # (entry_date, smt_available)
    from collections import Counter

    funnel_counts: Counter = Counter()
    orig_on_bar = runner._state_machine.on_bar
    sm = runner._state_machine

    def _on_bar(bar):
        evs = orig_on_bar(bar)
        for e in evs:
            funnel_counts[e.reason] += 1
            if e.reason == "gate_rejected:TrendRegimeGate":
                trend_gate_rejects.append(bar.ts)
            elif e.reason == "order_submitted":
                tracker = sm._smt_tracker  # noqa: SLF001
                verdict = "NONE"
                if tracker is not None and hasattr(tracker, "verdict"):
                    try:
                        verdict = str(tracker.verdict).split(".")[-1]
                    except Exception:  # noqa: BLE001
                        verdict = "NONE"
                order_smt_log.append((bar.ts.date(), verdict != "NONE"))
        return evs

    runner._state_machine.on_bar = _on_bar  # noqa: SLF001
    result = runner.run()
    trades = result.trades
    print(f"Total trades over the replay: {len(trades)}")
    print(f"TrendRegimeGate blocks total: {len(trend_gate_rejects)}")
    print()
    print("=== Funnel counts (whole replay) ===")
    for key in (
        "bias_LONG", "bias_SHORT", "sweep_detected",
        "cisd_bullish", "cisd_bearish", "cisd_timeout",
        "ifvg_zone_armed", "no_ifvg", "ifvg_ready",
        "outside_killzone", "zone_filter_rejected", "smt_direction_rejected",
        "order_submitted", "target_hit", "stop_out",
    ):
        print(f"  {key:30s}: {funnel_counts[key]}")
    for k in sorted(funnel_counts):
        if k.startswith("gate_rejected"):
            print(f"  {k:30s}: {funnel_counts[k]}")
    print()

    def _trend_blocks_in(start: date, end: date) -> int:
        return sum(1 for ts in trend_gate_rejects if start <= ts.date() <= end)

    def _smt_avail_pct(start: date, end: date) -> tuple[int, int, float]:
        sub = [avail for d, avail in order_smt_log if start <= d <= end]
        n = len(sub)
        if n == 0:
            return (0, 0, 0.0)
        avail_n = sum(1 for a in sub if a)
        return (avail_n, n, 100.0 * avail_n / n)

    print()
    print(
        f"{'split':<6} {'window':<28} {'trades':>6} {'WR':>7} {'avg_rr':>9} "
        f"{'PF':>7} {'pnl$':>10} {'comm$':>8} {'blocks':>7} {'smt_av%':>8}"
    )
    print("-" * 110)
    json_splits: dict[str, dict] = {}
    for tag, is_a, is_b, oos_a, oos_b in SPLITS:
        m_is, t_is = _metrics_subset(trades, is_a, is_b)
        m_oos, t_oos = _metrics_subset(trades, oos_a, oos_b)
        is_blocks = _trend_blocks_in(is_a, is_b)
        oos_blocks = _trend_blocks_in(oos_a, oos_b)
        is_smt_n, is_smt_d, is_smt_pct = _smt_avail_pct(is_a, is_b)
        oos_smt_n, oos_smt_d, oos_smt_pct = _smt_avail_pct(oos_a, oos_b)
        print(
            f"{tag} IS  {str(is_a)+'..'+str(is_b):<26} "
            f"{m_is.trades_count:>6} {m_is.wr:>7.3f} "
            f"{m_is.avg_rr:>+9.4f} {m_is.profit_factor:>7.3f} "
            f"{float(m_is.total_pnl_usd):>10.0f} "
            f"{float(m_is.commission_total):>8.0f} "
            f"{is_blocks:>7} {is_smt_pct:>7.1f}%"
        )
        print(
            f"{tag} OOS {str(oos_a)+'..'+str(oos_b):<26} "
            f"{m_oos.trades_count:>6} {m_oos.wr:>7.3f} "
            f"{m_oos.avg_rr:>+9.4f} {m_oos.profit_factor:>7.3f} "
            f"{float(m_oos.total_pnl_usd):>10.0f} "
            f"{float(m_oos.commission_total):>8.0f} "
            f"{oos_blocks:>7} {oos_smt_pct:>7.1f}%"
        )
        print("-" * 110)
        json_splits[tag] = {
            "IS": {
                "window": [str(is_a), str(is_b)],
                "trades": m_is.trades_count,
                "wr": round(m_is.wr, 4),
                "avg_rr": round(m_is.avg_rr, 4),
                "profit_factor": round(m_is.profit_factor, 4),
                "total_pnl_usd": round(float(m_is.total_pnl_usd), 2),
                "commission_total_usd": round(float(m_is.commission_total), 2),
                "trend_regime_blocks": is_blocks,
                "smt_available": {
                    "n": is_smt_n,
                    "total": is_smt_d,
                    "pct": round(is_smt_pct, 2),
                },
            },
            "OOS": {
                "window": [str(oos_a), str(oos_b)],
                "trades": m_oos.trades_count,
                "wr": round(m_oos.wr, 4),
                "avg_rr": round(m_oos.avg_rr, 4),
                "profit_factor": round(m_oos.profit_factor, 4),
                "total_pnl_usd": round(float(m_oos.total_pnl_usd), 2),
                "commission_total_usd": round(float(m_oos.commission_total), 2),
                "trend_regime_blocks": oos_blocks,
                "smt_available": {
                    "n": oos_smt_n,
                    "total": oos_smt_d,
                    "pct": round(oos_smt_pct, 2),
                },
            },
        }

    print()
    print("=== OOS WR per split ===")
    oos_wr = []
    for tag, _is_a, _is_b, oos_a, oos_b in SPLITS:
        m_oos, _ = _metrics_subset(trades, oos_a, oos_b)
        oos_wr.append((tag, m_oos.wr, m_oos.profit_factor, m_oos.trades_count))
        flag = ""
        if m_oos.wr < 0.40:
            flag = " (< 40% — no edge)"
        elif m_oos.wr > 0.50:
            flag = " (> 50% — edge present)"
        print(
            f"  Split {tag}: WR={m_oos.wr:.3f}  PF={m_oos.profit_factor:.3f}  trades={m_oos.trades_count}{flag}"
        )

    below_40 = sum(1 for _, wr, _, _ in oos_wr if wr < 0.40)
    above_50 = sum(1 for _, wr, _, _ in oos_wr if wr > 0.50)
    print()
    if below_40 == 3:
        verdict_line = "all three OOS windows below 40% WR — no mechanical edge."
    elif above_50 >= 1:
        verdict_line = f"{above_50}/3 OOS windows above 50% WR — regime-dependent edge present."
    else:
        verdict_line = "mixed (some 40-50% range) — borderline; investigate per-regime."
    print(f"VERDICT: {verdict_line}")

    profitable_oos = sum(
        1 for tag in json_splits if json_splits[tag]["OOS"]["profit_factor"] > 1.0
    )

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "replay_window": [str(REPLAY_START), str(REPLAY_END)],
        "data_source": {
            "nq": "data/historical/NQ_1m_2022_2026.dbn.zst",
            "es": "data/historical/ES_1m_2022_2026.dbn.zst",
            "nq_bars": len(nq),
            "es_bars": len(es_full),
        },
        "config": {
            "best_params": {
                "ifvg_tolerance_ticks": BEST_PARAMS["tol"],
                "rr_cap": str(BEST_PARAMS["rr"]),
                "cisd_lookback_bars": BEST_PARAMS["lookback"],
            },
            "risk_per_trade_usd": "750",
            "commission_per_contract_usd": "4.50",
            "trend_filter_efficiency_ratio": float(
                cfg.get("trend_filter_efficiency_ratio", 3.0)
            ),
            "entry_order_type": "LIMIT",
        },
        "totals": {
            "trades_full_replay": len(trades),
            "trend_regime_blocks_total": len(trend_gate_rejects),
        },
        "splits": json_splits,
        "verdict": {
            "wr_below_40_count": below_40,
            "wr_above_50_count": above_50,
            "profitable_oos_pf_gt_1_count": profitable_oos,
            "summary": verdict_line,
        },
    }
    out_path = REPO / "results" / f"phase3_walk_forward_3splits{out_suffix}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Saved: {out_path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
