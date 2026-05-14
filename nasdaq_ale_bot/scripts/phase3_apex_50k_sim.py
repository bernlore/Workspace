#!/usr/bin/env python3
"""Apex 50k monthly challenge simulation.

Replays QQQ 2024-01-01..2025-12-31 with the best-IS params (tol=1, rr=1.3,
cisd_lookback=20). Each calendar month is then re-played as an independent
Apex attempt with rules:

  start = $50,000
  per-trade risk = $750 (we scale each historical R-multiple by $750)
  pass = balance >= +$3,000 from start ($53,000)
  daily loss limit = -$1,000 (stop trading that day if reached)
  trailing DD = -$2,500 from running HWM (FAIL if breached)
  max 2 trades per day
  attempt window = the full calendar month

No detection or strategy parameters changed.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]

START_BALANCE = 50_000.0
RISK_PER_TRADE = 750.0
PROFIT_TARGET = 3_000.0      # → balance >= 53_000 = PASS
DAILY_LOSS_LIMIT = -1_000.0  # halt trading that day if daily PnL <= -1000
TRAILING_DD = 2_500.0        # FAIL if balance < hwm - 2500
MAX_TRADES_PER_DAY = 2


def replay_full_window():
    nq = BacktestRunner.load_bars_from_dbn(
        REPO / "data" / "historical" / "NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    es = BacktestRunner.load_bars_from_dbn(
        REPO / "data" / "historical" / "ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
    )
    cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
    inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal(str(RISK_PER_TRADE))
    # LIMIT at zone edge with carry-forward — true ICT retest semantics.
    cfg["entry_order_type"] = "LIMIT"
    cfg["entry_slippage_ticks"] = 0
    cfg.setdefault("default_qty", Decimal("1"))
    # Best-IS pick from the latest phase3_oos_verdict.json (NQ/ES + IMMEDIATE).
    cfg["ifvg_tolerance_ticks"] = 2
    cfg["rr_cap"] = Decimal("1.2")
    cfg["cisd_lookback_bars"] = 20

    pv = Decimal(str(getattr(inst, "point_value", 1)))
    ledger = AccountLedger(
        session_start_equity=Decimal("50000"), today=nq[0].ts.date()
    )
    broker = MockBroker(
        ledger=ledger, initial_equity=Decimal("50000"), point_value=pv
    )
    runner = BacktestRunner(
        bars_primary=nq,
        bars_correlated=es,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=inst,
        param_set_hash="apex_50k",
    )
    return runner.run()


def trade_pnl_dollars(t) -> float | None:
    """Real-dollar PnL from broker fill. With dynamic qty + point_value the
    broker already books the trade in account dollars (≈ ±$750 on a stop or
    ≈ +$750·rr_cap on a TP, modulo float-quantisation and gap-overs)."""
    return float(t.realized_pnl)


def simulate_month(month_trades: list, month_label: str) -> dict:
    """Run one Apex 50k attempt over the trades of a single calendar month."""
    balance = START_BALANCE
    hwm = balance
    daily_pnl = 0.0
    daily_count = 0
    current_day: date | None = None
    n_trades_taken = 0
    n_trades_skipped_daycap = 0
    n_trades_skipped_dayloss = 0
    wins = 0
    losses = 0
    daily_limit_days: set[date] = set()
    peak_balance = balance
    min_balance = balance
    result: str | None = None
    fail_reason: str | None = None

    for t in month_trades:
        d = t.entry_ts.date()
        if d != current_day:
            current_day = d
            daily_pnl = 0.0
            daily_count = 0

        # Rule: max 2 trades per day
        if daily_count >= MAX_TRADES_PER_DAY:
            n_trades_skipped_daycap += 1
            continue
        # Rule: stop trading that day if running daily PnL hit the limit
        if daily_pnl <= DAILY_LOSS_LIMIT:
            n_trades_skipped_dayloss += 1
            continue

        pnl = trade_pnl_dollars(t)
        if pnl is None:
            continue

        balance += pnl
        daily_pnl += pnl
        daily_count += 1
        n_trades_taken += 1
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        peak_balance = max(peak_balance, balance)
        min_balance = min(min_balance, balance)
        hwm = max(hwm, balance)

        if daily_pnl <= DAILY_LOSS_LIMIT:
            daily_limit_days.add(d)

        # FAIL on trailing DD before checking PASS (DD takes precedence)
        if balance < hwm - TRAILING_DD:
            result = "FAIL"
            fail_reason = "trailing_DD"
            break
        # PASS gate
        if balance - START_BALANCE >= PROFIT_TARGET:
            result = "PASS"
            break

    if result is None:
        # End of month with neither PASS nor DD breach
        result = "NO_PASS"
        if balance < START_BALANCE:
            fail_reason = "in_drawdown_no_target"
        elif balance == START_BALANCE:
            fail_reason = "flat"
        else:
            fail_reason = "below_target"

    total_pnl = balance - START_BALANCE
    max_dd = peak_balance - min_balance
    wr = wins / max(n_trades_taken, 1) if n_trades_taken else 0.0
    return {
        "month": month_label,
        "trades": n_trades_taken,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "total_pnl": total_pnl,
        "peak_balance": peak_balance,
        "min_balance": min_balance,
        "max_dd": max_dd,
        "daily_limit_days": len(daily_limit_days),
        "skipped_daycap": n_trades_skipped_daycap,
        "skipped_dayloss": n_trades_skipped_dayloss,
        "result": result,
        "reason": fail_reason,
    }


def main() -> int:
    print("Replaying 2024-01-01..2025-12-31 with best-IS params …")
    res = replay_full_window()
    trades_all = list(res.trades)
    trades_2024_25 = [
        t for t in trades_all
        if 2024 <= t.entry_ts.year <= 2025
    ]
    print(f"  total trades in 2024-2025: {len(trades_2024_25)}")
    n_with_stop = sum(1 for t in trades_2024_25 if t.stop_price is not None)
    print(f"  trades with stop_price plumbed: {n_with_stop}")

    by_month: dict[str, list] = defaultdict(list)
    for t in trades_2024_25:
        key = f"{t.entry_ts.year}-{t.entry_ts.month:02d}"
        by_month[key].append(t)

    months: list[str] = []
    for y in (2024, 2025):
        for m in range(1, 13):
            months.append(f"{y}-{m:02d}")

    print()
    print(
        f"{'month':<8} {'trades':>6} {'WR':>6} {'pnl':>10} {'peak':>10} "
        f"{'maxDD':>8} {'lim_days':>9} {'result':>8}  reason"
    )
    print("-" * 95)

    rows = []
    pass_n = fail_n = nopass_n = 0
    fail_reasons: dict[str, int] = defaultdict(int)
    pnls = []
    for m in months:
        attempt = simulate_month(by_month.get(m, []), m)
        rows.append(attempt)
        pnls.append((m, attempt["total_pnl"]))
        if attempt["result"] == "PASS":
            pass_n += 1
        elif attempt["result"] == "FAIL":
            fail_n += 1
            fail_reasons[attempt["reason"]] += 1
        else:
            nopass_n += 1
            fail_reasons[attempt["reason"]] += 1
        print(
            f"{attempt['month']:<8} {attempt['trades']:>6} "
            f"{attempt['wr']:>6.3f} "
            f"{attempt['total_pnl']:>+10.2f} "
            f"{attempt['peak_balance']:>10.2f} "
            f"{attempt['max_dd']:>8.2f} "
            f"{attempt['daily_limit_days']:>9} "
            f"{attempt['result']:>8}  "
            f"{attempt['reason'] or ''}"
        )

    print()
    print(f"=== SUMMARY (24 monthly attempts) ===")
    print(f"PASS: {pass_n}   FAIL: {fail_n}   NO_PASS (timeout): {nopass_n}")
    if fail_reasons:
        print("Reason distribution among non-PASS:")
        for reason, count in sorted(fail_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")
    if pnls:
        best = max(pnls, key=lambda x: x[1])
        worst = min(pnls, key=lambda x: x[1])
        avg = sum(p for _, p in pnls) / len(pnls)
        print(f"Best month PnL : {best[0]} = {best[1]:+.2f}")
        print(f"Worst month PnL: {worst[0]} = {worst[1]:+.2f}")
        print(f"Mean monthly PnL: {avg:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
