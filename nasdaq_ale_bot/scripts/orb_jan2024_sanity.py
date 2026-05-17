#!/usr/bin/env python3
"""ORB sanity check — replay January 2024 NQ through the ORB state machine.

Pure ORB baseline: no news gate (spec §9.2), unified cost model (spec §9.1).
Reports per-day funnel counts and a trade-by-trade list.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.cost_model import load_cost_model
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config
from nasdaq_ale_bot.strategies.orb import load_orb_config
from nasdaq_ale_bot.strategies.orb.state_machine import OrbStateMachine

logging.basicConfig(level=logging.CRITICAL)
import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

REPO = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
JAN_START, JAN_END = date(2024, 1, 1), date(2024, 1, 31)


def main() -> int:
    nq = BacktestRunner.load_bars_from_dbn(
        REPO / "data/historical/NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    jan = [b for b in nq if JAN_START <= b.ts.astimezone(NY).date() <= JAN_END]
    print(f"January 2024 NQ bars: {len(jan):,}")

    orb_cfg = load_orb_config(REPO / "config/orb_strategy.yaml")
    cost_model = load_cost_model(REPO / "config/cost_model.yaml", "nq")
    inst = load_instruments_config(REPO / "config/instruments.yaml").primary
    tick = float(getattr(inst, "tick", 0.25))
    point_value = float(getattr(inst, "point_value", 20.0))

    ledger = AccountLedger(
        session_start_equity=Decimal("50000"), today=jan[0].ts.date()
    )
    broker = MockBroker(
        ledger=ledger, initial_equity=Decimal("50000"),
        point_value=Decimal(str(point_value)), cost_model=cost_model,
    )
    sm = OrbStateMachine(
        config=orb_cfg, broker=broker, ledger=ledger,
        tick_size=tick, point_value=point_value, cost_model=cost_model,
        symbol="NQ",
    )

    for b in jan:
        sm.on_bar(b)

    trading_days = len({b.ts.astimezone(NY).date() for b in jan})
    print()
    print("=== January 2024 ORB funnel ===")
    print(f"  NY trading days in window   : {trading_days}")
    print(f"  Days OR valid (size filter) : {sm.days_or_valid}")
    print(f"  Days skipped — OR size      : {sm.days_skipped_size}")
    print(f"  Days skipped — incomplete OR: {sm.days_skipped_invalid}")
    print(f"  Days with breakout signal   : {sm.days_with_signal}")
    print(f"  Days skipped — sizing       : {sm.days_skipped_sizing}")
    print(f"  Days with completed trade   : {len(sm.trades)}")

    print()
    print("=== Trade-by-trade ===")
    hdr = (f"{'date':<11} {'sig_et':<7} {'dir':<6} {'qty':>3} "
           f"{'entry':>9} {'stop':>9} {'target':>9} {'OR_rng':>7} "
           f"{'stop_d':>7} {'rr':>5} {'exit':<12} {'gross$':>9} {'net$':>9}")
    print(hdr)
    print("-" * len(hdr))
    total_net = 0.0
    total_gross = 0.0
    wins = 0
    for t in sm.trades:
        sig_et = t.signal_ts.astimezone(NY).strftime("%H:%M")
        stop_d = abs(t.entry_price - t.stop_price)
        # rr_actual on the PLANNED (pre-slippage) levels — must be exactly 1.5.
        rr_actual = (
            abs(t.target_price - t.planned_entry_price)
            / abs(t.planned_entry_price - t.stop_price)
        )
        total_net += t.net_pnl
        total_gross += t.gross_pnl
        if t.net_pnl > 0:
            wins += 1
        print(f"{t.session_date.isoformat():<11} {sig_et:<7} {t.direction:<6} "
              f"{t.qty:>3} {t.entry_price:>9.2f} {t.stop_price:>9.2f} "
              f"{t.target_price:>9.2f} {t.or_range:>7.2f} {stop_d:>7.2f} "
              f"{rr_actual:>5.2f} {str(t.exit_reason):<12} "
              f"{t.gross_pnl:>9.2f} {t.net_pnl:>9.2f}")
    print("-" * len(hdr))

    n = len(sm.trades)
    if n:
        losses = n - wins
        print()
        print("=== January 2024 aggregate ===")
        print(f"  Total trades      : {n}")
        print(f"  Wins / Losses     : {wins} / {losses}")
        print(f"  Win rate          : {100*wins/n:.1f}%")
        print(f"  Avg gross / trade : ${total_gross/n:+.2f}")
        print(f"  Avg net / trade   : ${total_net/n:+.2f}")
        print(f"  Total net pnl     : ${total_net:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
