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

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]

REPLAY_START = date(2022, 12, 26)
REPLAY_END = date(2025, 4, 30)

SPLITS = [
    ("A", date(2022, 12, 26), date(2023, 9, 30), date(2023, 10, 1), date(2024, 2, 29)),
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
    nq = BacktestRunner.load_bars_from_nq_csv(
        REPO / "data" / "historical" / "NQ_1min_2022_2025.csv"
    )
    es = BacktestRunner.load_bars_from_databento_csv(
        REPO / "data" / "historical" / "mes1123.csv", symbol_prefix="ES"
    )
    nq = _slice(nq, REPLAY_START, REPLAY_END)
    es_full = es  # ES range 2023-11-20..2024-11-19 — keep all; runner pads gaps.
    print(f"Replay window: {REPLAY_START} .. {REPLAY_END}")
    print(f"  NQ bars: {len(nq):,}   ES bars: {len(es_full):,}")

    cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
    inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
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
    # Tap SM events to record TrendRegimeGate rejections per bar.ts.
    trend_gate_rejects: list = []
    orig_on_bar = runner._state_machine.on_bar

    def _on_bar(bar):
        evs = orig_on_bar(bar)
        for e in evs:
            if e.reason == "gate_rejected:TrendRegimeGate":
                trend_gate_rejects.append(bar.ts)
        return evs

    runner._state_machine.on_bar = _on_bar  # noqa: SLF001
    result = runner.run()
    trades = result.trades
    print(f"Total trades over the replay: {len(trades)}")
    print(f"TrendRegimeGate blocks total: {len(trend_gate_rejects)}")

    def _trend_blocks_in(start: date, end: date) -> int:
        return sum(1 for ts in trend_gate_rejects if start <= ts.date() <= end)

    print()
    print(
        f"{'split':<6} {'window':<28} {'trades':>6} {'WR':>7} {'avg_rr':>9} "
        f"{'PF':>7} {'pnl$':>10} {'comm$':>8} {'blocks':>7}"
    )
    print("-" * 100)
    for tag, is_a, is_b, oos_a, oos_b in SPLITS:
        m_is, t_is = _metrics_subset(trades, is_a, is_b)
        m_oos, t_oos = _metrics_subset(trades, oos_a, oos_b)
        is_blocks = _trend_blocks_in(is_a, is_b)
        oos_blocks = _trend_blocks_in(oos_a, oos_b)
        print(
            f"{tag} IS  {str(is_a)+'..'+str(is_b):<26} "
            f"{m_is.trades_count:>6} {m_is.wr:>7.3f} "
            f"{m_is.avg_rr:>+9.4f} {m_is.profit_factor:>7.3f} "
            f"{float(m_is.total_pnl_usd):>10.0f} "
            f"{float(m_is.commission_total):>8.0f} "
            f"{is_blocks:>7}"
        )
        print(
            f"{tag} OOS {str(oos_a)+'..'+str(oos_b):<26} "
            f"{m_oos.trades_count:>6} {m_oos.wr:>7.3f} "
            f"{m_oos.avg_rr:>+9.4f} {m_oos.profit_factor:>7.3f} "
            f"{float(m_oos.total_pnl_usd):>10.0f} "
            f"{float(m_oos.commission_total):>8.0f} "
            f"{oos_blocks:>7}"
        )
        print("-" * 100)

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
        print("VERDICT: all three OOS windows below 40% WR — no mechanical edge.")
    elif above_50 >= 1:
        print(f"VERDICT: {above_50}/3 OOS windows above 50% WR — regime-dependent edge present.")
    else:
        print("VERDICT: mixed (some 40-50% range) — borderline; investigate per-regime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
