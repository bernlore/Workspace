#!/usr/bin/env python3
"""CHECK 2 — Parameter stability sweep.

Runs three-split walk-forward with three shifted-param variants and reports
whether Split A OOS and Split C OOS stay profitable across all of them.

Baseline (already known): tol=2, rr_cap=1.1, lookback=20.
Variants:
  A: cisd_lookback_bars = 15
  B: cisd_lookback_bars = 25
  C: rr_cap = 1.2

For each variant, prints the same per-split table as phase3_walk_forward_3splits
and a stability verdict at the end.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
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
SPLITS = [
    ("A", date(2022, 1, 1),   date(2023, 9, 30), date(2023, 10, 1), date(2024, 2, 29)),
    ("B", date(2023, 1, 1),   date(2024, 2, 28), date(2024, 3, 1),  date(2024, 6, 30)),
    ("C", date(2023, 6, 1),   date(2024, 9, 30), date(2024, 10, 1), date(2025, 4, 25)),
]

VARIANTS = [
    ("BASE",      {"cisd_lookback_bars": 20, "rr_cap": Decimal("1.1"), "ifvg_tolerance_ticks": 2}),
    ("V_A_lb15",  {"cisd_lookback_bars": 15, "rr_cap": Decimal("1.1"), "ifvg_tolerance_ticks": 2}),
    ("V_B_lb25",  {"cisd_lookback_bars": 25, "rr_cap": Decimal("1.1"), "ifvg_tolerance_ticks": 2}),
    ("V_C_rr12",  {"cisd_lookback_bars": 20, "rr_cap": Decimal("1.2"), "ifvg_tolerance_ticks": 2}),
]


def run_variant(nq, es, params: dict) -> dict:
    cfg = load_strategy_config(REPO / "config/strategy.yaml")
    inst = load_instruments_config(REPO / "config/instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal("750")
    cfg["entry_order_type"] = "LIMIT"
    cfg["entry_slippage_ticks"] = 0
    cfg.setdefault("default_qty", Decimal("1"))
    cfg.update(params)

    pv = Decimal(str(getattr(inst, "point_value", 1)))
    ledger = AccountLedger(session_start_equity=Decimal("50000"), today=nq[0].ts.date())
    broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"), point_value=pv)
    runner = BacktestRunner(
        bars_primary=nq, bars_correlated=es, mock_broker=broker, ledger=ledger,
        strategy_cfg=cfg, instrument_cfg=inst, param_set_hash=f"stab_{params}",
    )
    res = runner.run()
    trades = res.trades

    splits_out = {}
    for tag, is_a, is_b, oos_a, oos_b in SPLITS:
        oos_trades = [t for t in trades if oos_a <= t.entry_ts.date() <= oos_b]
        m = MetricsCalculator(risk_per_trade_usd=Decimal("750")).compute(
            trades=oos_trades, equity_curve=[]
        )
        splits_out[tag] = {
            "trades": m.trades_count,
            "wr": round(m.wr, 3),
            "pf": round(m.profit_factor, 3),
            "pnl": round(float(m.total_pnl_usd), 0),
        }
    return splits_out


def main() -> int:
    nq = BacktestRunner.load_bars_from_dbn(
        REPO/"data/historical/NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    es = BacktestRunner.load_bars_from_dbn(
        REPO/"data/historical/ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
    )
    nq = [b for b in nq if REPLAY_START <= b.ts.date() <= REPLAY_END]
    print(f"NQ bars: {len(nq):,}   ES bars: {len(es):,}")

    results = {}
    for name, params in VARIANTS:
        print(f"\nRunning variant {name}: {params}")
        results[name] = run_variant(nq, es, params)

    print()
    print("=" * 90)
    print(f"{'variant':<14} {'split':<6} {'trades':>7} {'WR':>6} {'PF':>6} {'pnl$':>10}")
    print("-" * 90)
    for name, _ in VARIANTS:
        for tag in ["A", "B", "C"]:
            r = results[name][tag]
            print(f"{name:<14} {tag+' OOS':<6} {r['trades']:>7} {r['wr']:>6.3f} "
                  f"{r['pf']:>6.3f} {r['pnl']:>10.0f}")
        print("-" * 90)

    print()
    print("=== Stability verdict (Split A OOS + Split C OOS) ===")
    bad = []
    for name, _ in VARIANTS:
        a, c = results[name]["A"], results[name]["C"]
        a_pos = a["pf"] > 1.0
        c_pos = c["pf"] > 1.0
        flag = "STABLE" if (a_pos and c_pos) else "UNSTABLE"
        if not (a_pos and c_pos):
            bad.append(name)
        print(f"  {name:<14}  A_OOS PF={a['pf']:.3f}  C_OOS PF={c['pf']:.3f}  -> {flag}")

    print()
    if not bad:
        print("VERDICT: parameters STABLE — A_OOS and C_OOS profitable across all variants.")
    else:
        print(f"VERDICT: INSTABILITY in variant(s): {bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
