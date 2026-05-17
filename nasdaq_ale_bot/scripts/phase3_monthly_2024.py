#!/usr/bin/env python3
"""Per-month breakdown of 2024 with the best-IS combo. Diagnosis only.

Replays QQQ 2024-01-01 to 2024-06-28 with tol=1, rr_cap=1.3, cisd_lookback=20.
Bias detector is primed by feeding all bars from 2023-01-01 forward (so the
2024-01 month already has a warm 4H/Daily aggregator).

Reports:
  - per-month: trades, wins, losses, WR, avg_rr, PF, max_dd_usd
  - per-month QQQ regime: trend (close-vs-open of the month), ADR%
  - WR<40% months and trending-vs-ranging classification
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import mean

from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    qqq = BacktestRunner.load_bars_from_parquet(
        REPO / "data" / "historical" / "QQQ_1m_2023_2024H1.parquet"
    )
    spy = BacktestRunner.load_bars_from_parquet(
        REPO / "data" / "historical" / "SPY_1m_2023_2024H1.parquet"
    )

    cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
    inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg.setdefault("default_qty", Decimal("1"))
    cfg["ifvg_tolerance_ticks"] = 1
    cfg["rr_cap"] = Decimal("1.3")
    cfg["cisd_lookback_bars"] = 20

    ledger = AccountLedger(
        session_start_equity=Decimal("50000"), today=qqq[0].ts.date()
    )
    broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"))
    runner = BacktestRunner(
        bars_primary=qqq,
        bars_correlated=spy,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=inst,
        param_set_hash="month_2024",
    )
    res = runner.run()
    trades = [t for t in res.trades if t.entry_ts.date() >= date(2024, 1, 1)]
    eq_curve_2024 = [
        (ts, eq) for ts, eq in res.equity_curve if ts.date() >= date(2024, 1, 1)
    ]

    # Per-month bucketing
    by_month: dict[str, list] = defaultdict(list)
    for t in trades:
        key = f"{t.entry_ts.year}-{t.entry_ts.month:02d}"
        by_month[key].append(t)

    # Equity curve buckets per month for max_dd
    eq_by_month: dict[str, list] = defaultdict(list)
    for ts, eq in eq_curve_2024:
        key = f"{ts.year}-{ts.month:02d}"
        eq_by_month[key].append((ts, eq))

    # Daily QQQ bars from the 1m feed for regime metrics
    daily_2024: dict[date, dict[str, float]] = {}
    for b in qqq:
        d = b.ts.date()
        if d.year != 2024:
            continue
        rec = daily_2024.get(d)
        if rec is None:
            daily_2024[d] = {
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
            }
        else:
            rec["high"] = max(rec["high"], b.high)
            rec["low"] = min(rec["low"], b.low)
            rec["close"] = b.close

    # Per-month QQQ regime: monthly close vs open + average daily range %
    by_month_qqq: dict[str, list[date]] = defaultdict(list)
    for d in sorted(daily_2024):
        by_month_qqq[f"{d.year}-{d.month:02d}"].append(d)

    print(
        f"{'month':<8} {'trades':>6} {'W':>3} {'L':>3} {'WR':>6} "
        f"{'avg_rr':>8} {'PF':>6} {'maxDD':>7}  | {'QQQ_open':>9} {'QQQ_close':>10} "
        f"{'mret%':>7} {'ADR%':>6}  trend"
    )
    print("-" * 100)

    months = sorted(set(list(by_month.keys()) + list(by_month_qqq.keys())))
    summary_rows = []
    for m in months:
        ts_for_month = by_month.get(m, [])
        eq_curve = eq_by_month.get(m, [])
        metrics = MetricsCalculator().compute(trades=ts_for_month, equity_curve=eq_curve)
        wins = sum(1 for t in ts_for_month if t.realized_pnl > 0)
        losses = sum(1 for t in ts_for_month if t.realized_pnl <= 0)

        days = by_month_qqq.get(m, [])
        if days:
            mo_open = daily_2024[days[0]]["open"]
            mo_close = daily_2024[days[-1]]["close"]
            mret = (mo_close / mo_open - 1.0) * 100
            adrs = [
                (daily_2024[d]["high"] - daily_2024[d]["low"])
                / daily_2024[d]["open"]
                * 100
                for d in days
            ]
            adr_pct = mean(adrs)
            if abs(mret) < 1.0:
                trend = "sideways"
            elif mret > 0:
                trend = "up"
            else:
                trend = "down"
        else:
            mo_open = mo_close = mret = adr_pct = 0.0
            trend = "?"

        n = max(len(ts_for_month), 1)
        wr_str = f"{metrics.wr:.3f}" if ts_for_month else "  -  "
        rr_str = f"{metrics.avg_rr:+.4f}" if ts_for_month else "    - "
        pf_str = f"{metrics.profit_factor:.3f}" if ts_for_month else "  -  "
        dd_str = f"{float(metrics.max_dd_usd):.2f}" if eq_curve else "  -  "
        print(
            f"{m:<8} {len(ts_for_month):>6} {wins:>3} {losses:>3} {wr_str:>6} "
            f"{rr_str:>8} {pf_str:>6} {dd_str:>7}  | {mo_open:>9.2f} {mo_close:>10.2f} "
            f"{mret:>+7.2f} {adr_pct:>6.2f}  {trend}"
        )
        summary_rows.append(
            {
                "month": m,
                "trades": len(ts_for_month),
                "wr": metrics.wr if ts_for_month else None,
                "trend": trend,
                "mret_pct": mret,
                "adr_pct": adr_pct,
            }
        )

    print()
    print("WR < 40% months:")
    weak = [r for r in summary_rows if r["wr"] is not None and r["wr"] < 0.40]
    for r in weak:
        print(
            f"  {r['month']}: WR={r['wr']:.3f} trades={r['trades']} "
            f"trend={r['trend']} mret={r['mret_pct']:+.2f}% ADR={r['adr_pct']:.2f}%"
        )
    if not weak:
        print("  none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
