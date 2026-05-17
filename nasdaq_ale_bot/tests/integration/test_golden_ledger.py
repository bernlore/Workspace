"""Golden Ledger regression test (PLAN_PHASE3.md Step 7).

Drives a deterministic 12-bar synthetic series through BacktestRunner with
pinned GridParams(ifvg_tolerance_ticks=1, rr_cap=Decimal("1.3"),
cisd_lookback_bars=20) and compares the resulting trades and final ledger
state against a Decimal-exact JSON fixture.

The synthetic series is identical in shape to
``tests/integration/test_phase2_lifecycle.py`` — narrow-range bars 0..8,
wide-range sweep bar 9, bullish-close CISD bar 10, take-profit bar 11.

Regenerate the fixture with::

    GOLDEN_LEDGER_REGEN=1 python -m pytest \
        tests/integration/test_golden_ledger.py -k golden_ledger_pinned_params
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nasdaq_ale_bot.backtest.grid import GridParams, compute_param_set_hash
from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.strategies.nasdaqale.htf_bias import HTFBias
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.execution.mock_broker import MockBroker

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "golden_ledger_2024_01.json"
)

_PINNED = GridParams(
    ifvg_tolerance_ticks=1,
    rr_cap=Decimal("1.3"),
    cisd_lookback_bars=20,
)
_START_EQUITY = Decimal("10000")


class _MockBiasDetectorLong:
    def on_1m_bar(self, bar: Candle) -> Any:
        return SimpleNamespace(bias=HTFBias.LONG)


def _t(h: int, m: int, day: int = 2) -> datetime:
    return datetime(2024, 1, day, h, m, tzinfo=timezone.utc)


def _bar(ts: datetime, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=500.0)


def _synthetic_bars() -> list[Candle]:
    bars: list[Candle] = []
    for i in range(9):
        bars.append(_bar(_t(14, 30 + i), 100.0, 100.5, 99.5, 100.0))
    bars.append(_bar(_t(14, 39), 100.0, 105.0, 97.0, 102.0))
    bars.append(_bar(_t(14, 40), 101.0, 104.0, 99.0, 103.0))
    bars.append(_bar(_t(14, 41), 103.0, 108.5, 102.5, 107.0))
    return bars


def _run_pinned() -> dict[str, Any]:
    bars = _synthetic_bars()
    ledger = AccountLedger(
        session_start_equity=_START_EQUITY, today=date(2024, 1, 2)
    )
    broker = MockBroker(ledger=ledger, initial_equity=_START_EQUITY)
    cfg: dict[str, Any] = {
        "ifvg_tolerance_ticks": _PINNED.ifvg_tolerance_ticks,
        "rr_cap": _PINNED.rr_cap,
        "cisd_lookback_bars": _PINNED.cisd_lookback_bars,
        "_bias_detector": _MockBiasDetectorLong(),
        "default_qty": Decimal("1"),
    }
    runner = BacktestRunner(
        bars_primary=bars,
        mock_broker=broker,
        ledger=ledger,
        strategy_cfg=cfg,
        instrument_cfg=SimpleNamespace(symbol="QQQ", tick=Decimal("0.01")),
        param_set_hash=compute_param_set_hash(_PINNED),
    )
    result = runner.run()
    return {
        "param_set_hash": result.param_set_hash,
        "trades": [
            {
                "entry_ts": t.entry_ts.isoformat(),
                "exit_ts": t.exit_ts.isoformat(),
                "symbol": t.symbol,
                "side": t.side,
                "entry_price": str(t.entry_price),
                "exit_price": str(t.exit_price),
                "qty": str(t.qty),
                "realized_pnl": str(t.realized_pnl),
                "exit_reason": t.exit_reason,
            }
            for t in result.trades
        ],
        "ledger": {
            "realized_today": str(ledger.realized_today),
            "current_equity": str(ledger.current_equity),
            "high_watermark_equity": str(ledger.high_watermark_equity),
            "cumulative_profit": str(ledger.cumulative_profit),
        },
        "equity_curve_last": str(result.equity_curve[-1][1])
        if result.equity_curve
        else "0",
        "equity_curve_len": len(result.equity_curve),
    }


def _load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_golden_ledger_pinned_params() -> None:
    """Pinned-params run must match the committed golden fixture exactly."""
    got = _run_pinned()
    if os.environ.get("GOLDEN_LEDGER_REGEN") == "1":
        _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE.write_text(
            json.dumps(got, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        pytest.skip(f"regenerated fixture at {_FIXTURE}")
    if not _FIXTURE.exists():
        pytest.fail(
            f"golden fixture missing: {_FIXTURE} "
            "(run with GOLDEN_LEDGER_REGEN=1 to create)"
        )
    expected = _load_fixture()
    assert got == expected, (
        "Golden ledger diverged.  "
        "If intentional, regenerate with GOLDEN_LEDGER_REGEN=1."
    )


def test_golden_ledger_param_hash_is_stable() -> None:
    """Pinned param hash must not change across runs."""
    h1 = compute_param_set_hash(_PINNED)
    h2 = compute_param_set_hash(_PINNED)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hexdigest
