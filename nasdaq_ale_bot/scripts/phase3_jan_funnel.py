#!/usr/bin/env python3
"""Single-month (2024-01) detection funnel diagnostic.

Reports: bars → killzone-bars → sweeps → CISD → IFVG → bias_ok → trades.
Diagnosis only; no code changes.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import FlipState, HTFBias, HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.filters.killzone import in_primary_killzone, in_secondary_killzone
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.ERROR)

REPO = Path(__file__).resolve().parents[1]
JAN_START = date(2024, 1, 1)
JAN_END = date(2024, 1, 31)


def slice_jan(bars):
    return [b for b in bars if JAN_START <= b.ts.date() <= JAN_END]


primary_all = BacktestRunner.load_bars_from_dbn(
    REPO / "data" / "historical" / "NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
)
correlated_all = BacktestRunner.load_bars_from_dbn(
    REPO / "data" / "historical" / "ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
)
qqq = slice_jan(primary_all)
spy = slice_jan(correlated_all)
print(f"Jan 2024 bars: NQ={len(qqq)} ES={len(spy)}")

cfg = load_strategy_config(REPO / "config" / "strategy.yaml")
inst = load_instruments_config(REPO / "config" / "instruments.yaml").primary
cfg["_bias_detector"] = HTFBiasDetector(inst)
cfg["_gate_list"] = GateList.base_list(cfg)
cfg.setdefault("default_qty", Decimal("1"))
cfg["ifvg_tolerance_ticks"] = 1
cfg["rr_cap"] = Decimal("1.1")
cfg["cisd_lookback_bars"] = 20
# LIMIT at zone edge with carry-forward — true ICT retest semantics.
cfg["entry_order_type"] = "LIMIT"
cfg["entry_slippage_ticks"] = 0
cfg["risk_per_trade_usd"] = Decimal("750")

ledger = AccountLedger(session_start_equity=Decimal("50000"), today=qqq[0].ts.date())
broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"))
runner = BacktestRunner(
    bars_primary=qqq,
    bars_correlated=spy,
    mock_broker=broker,
    ledger=ledger,
    strategy_cfg=cfg,
    instrument_cfg=inst,
    param_set_hash="diag_jan",
)
sm = runner._state_machine
detector: HTFBiasDetector = cfg["_bias_detector"]
orig_on_bar = sm.on_bar

reasons: Counter = Counter()
killzone_bars = 0
am_killzone = 0
pm_killzone = 0
bias_active_bars = 0
bias_pending_bars = 0
bias_inactive_bars = 0


def patched(bar):
    global killzone_bars, am_killzone, pm_killzone
    global bias_active_bars, bias_pending_bars, bias_inactive_bars
    in_am = in_primary_killzone(bar.ts)
    in_pm = in_secondary_killzone(bar.ts)
    if in_am or in_pm:
        killzone_bars += 1
    if in_am:
        am_killzone += 1
    if in_pm:
        pm_killzone += 1
    evs = orig_on_bar(bar)
    fs = detector._flip_state
    bias = detector._bias
    if fs == FlipState.ACTIVE and bias in (HTFBias.LONG, HTFBias.SHORT):
        bias_active_bars += 1
    elif fs == FlipState.PENDING:
        bias_pending_bars += 1
    else:
        bias_inactive_bars += 1
    for e in evs:
        reasons[e.reason] += 1
    return evs


sm.on_bar = patched
result = runner.run()

print("\n========== Jan 2024 Funnel ==========")
print(f"Total bars                : {len(qqq)}")
print(f"Bars in primary killzone  : {am_killzone}  (09:00-13:00 ET)")
print(f"Bars in secondary killzone: {pm_killzone}  (13:30-16:00 ET)")
print(f"Bars in any killzone      : {killzone_bars}")
print()
print(f"Bias state distribution (per bar):")
print(f"  ACTIVE (LONG/SHORT)     : {bias_active_bars}")
print(f"  PENDING (awaiting confirm): {bias_pending_bars}")
print(f"  INACTIVE                : {bias_inactive_bars}")
print()
print(f"State transitions during Jan 2024:")
print(f"  bias_LONG               : {reasons['bias_LONG']}")
print(f"  bias_SHORT              : {reasons['bias_SHORT']}")
print(f"  sweep_detected          : {reasons['sweep_detected']}")
print(f"  cisd_bullish            : {reasons['cisd_bullish']}")
print(f"  cisd_bearish            : {reasons['cisd_bearish']}")
print(f"  cisd_timeout            : {reasons['cisd_timeout']}")
print(f"  ifvg_zone_armed (carry-fwd) : {reasons['ifvg_zone_armed']}")
# Monitor-side outcomes log via _log.info; not in event reasons.
print(f"  no_ifvg                  : {reasons['no_ifvg']}")
print(f"  ifvg_ready (zone monitor -> ENTRY): {reasons['ifvg_ready']}")
print(f"  no_ifvg                 : {reasons['no_ifvg']}")
print(f"  outside_killzone        : {reasons['outside_killzone']}")
print(f"  zone_filter_rejected    : {reasons['zone_filter_rejected']}")
print(f"  smt_direction_rejected  : {reasons['smt_direction_rejected']}")
gate_keys = [k for k in reasons if k.startswith('gate_rejected')]
for k in gate_keys:
    print(f"  {k:24s}: {reasons[k]}")
print(f"  order_submitted         : {reasons['order_submitted']}")
print(f"  target_hit              : {reasons['target_hit']}")
print(f"  stop_out                : {reasons['stop_out']}")
print()
print(f"Trades executed (round-trip): {len(result.trades)}")
print()
print("===== One-line funnel =====")
print(
    f"bars={len(qqq)}  killzone={killzone_bars}  bias_active={bias_active_bars}  "
    f"sweeps={reasons['sweep_detected']}  cisd={reasons['cisd_bullish']+reasons['cisd_bearish']}  "
    f"ifvg={reasons['ifvg_ready']}  trades={len(result.trades)}"
)
