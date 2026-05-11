#!/usr/bin/env python3
"""vectorbt cross-check for MetricsCalculator (PLAN_PHASE3.md Step 6).

Generates a deterministic synthetic price series, runs a long-only SMA-cross
strategy through our MetricsCalculator, then through vectorbt Portfolio, and
compares WR / Profit Factor / Max DD / Sharpe within a 1% relative tolerance.

Exit codes:
    0  metrics within tolerance
    1  metrics diverge beyond tolerance
    2  vectorbt (or numpy/pandas) not installed

Usage:
    python scripts/metrics_crosscheck.py [--fast-sma 20] [--slow-sma 50]
                                         [--bars 500] [--tol 0.01]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from nasdaq_ale_bot.backtest.metrics import MetricsCalculator
from nasdaq_ale_bot.backtest.runner import TradeRecord

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def _synthetic_prices(n: int, seed: int = 42) -> pd.Series:
    """Deterministic random-walk close series, 1m bars from 2024-01-02 14:30Z."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(loc=0.00002, scale=0.0015, size=n)
    prices = 100.0 * np.exp(np.cumsum(rets))
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])
    return pd.Series(prices, index=idx, name="close")


def _sma_cross_signals(close: pd.Series, fast: int, slow: int) -> tuple[pd.Series, pd.Series]:
    sma_fast = close.rolling(fast).mean()
    sma_slow = close.rolling(slow).mean()
    prev_fast = sma_fast.shift(1)
    prev_slow = sma_slow.shift(1)
    entries = (prev_fast <= prev_slow) & (sma_fast > sma_slow)
    exits = (prev_fast >= prev_slow) & (sma_fast < sma_slow)
    return entries.fillna(False), exits.fillna(False)


def _our_trades(close: pd.Series, entries: pd.Series, exits: pd.Series) -> list[TradeRecord]:
    trades: list[TradeRecord] = []
    entry_ts: datetime | None = None
    entry_price: Decimal | None = None
    for ts, price in close.items():
        if entry_ts is None and bool(entries.loc[ts]):
            entry_ts = ts.to_pydatetime()
            entry_price = Decimal(str(round(float(price), 4)))
        elif entry_ts is not None and bool(exits.loc[ts]):
            exit_price = Decimal(str(round(float(price), 4)))
            assert entry_price is not None
            pnl = exit_price - entry_price
            trades.append(
                TradeRecord(
                    entry_ts=entry_ts,
                    exit_ts=ts.to_pydatetime(),
                    symbol="SYN",
                    side="BUY",
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=Decimal("1"),
                    realized_pnl=pnl,
                    exit_reason="sma_cross",
                    param_set_hash=None,
                )
            )
            entry_ts = None
            entry_price = None
    return trades


def _our_equity(close: pd.Series, trades: list[TradeRecord], start_eq: Decimal) -> list[tuple[datetime, Decimal]]:
    eq = start_eq
    curve: list[tuple[datetime, Decimal]] = []
    # Step equity after each closed trade; pad between trades with last equity.
    trade_exit_idx = {t.exit_ts: t.realized_pnl for t in trades}
    for ts in close.index:
        pydt = ts.to_pydatetime()
        if pydt in trade_exit_idx:
            eq = eq + trade_exit_idx[pydt]
        curve.append((pydt, eq))
    return curve


def _vbt_metrics(close: pd.Series, entries: pd.Series, exits: pd.Series) -> dict[str, float]:
    import vectorbt as vbt

    pf = vbt.Portfolio.from_signals(
        close, entries=entries, exits=exits, init_cash=50_000, fees=0.0, freq="1min"
    )
    stats = pf.stats()
    wr = float(stats.get("Win Rate [%]", 0.0)) / 100.0
    pf_val = float(stats.get("Profit Factor", 0.0) or 0.0)
    max_dd = float(stats.get("Max Drawdown [%]", 0.0)) / 100.0
    sharpe = float(stats.get("Sharpe Ratio", 0.0) or 0.0)
    return {"wr": wr, "profit_factor": pf_val, "max_dd_pct": max_dd, "sharpe": sharpe}


def _within(a: float, b: float, tol: float) -> bool:
    if a == 0.0 and b == 0.0:
        return True
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom <= tol


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=500)
    ap.add_argument("--fast-sma", type=int, default=20)
    ap.add_argument("--slow-sma", type=int, default=50)
    ap.add_argument("--tol", type=float, default=0.01)
    args = ap.parse_args()

    try:
        import vectorbt  # noqa: F401
    except ImportError:
        log.error("vectorbt not installed. Run: pip install 'vectorbt>=0.26,<1.0'")
        return 2

    close = _synthetic_prices(args.bars)
    entries, exits = _sma_cross_signals(close, args.fast_sma, args.slow_sma)

    trades = _our_trades(close, entries, exits)
    start_eq = Decimal("50000")
    equity = _our_equity(close, trades, start_eq)
    ours = MetricsCalculator().compute(trades=trades, equity_curve=equity)

    theirs = _vbt_metrics(close, entries, exits)

    checks = [
        ("wr", ours.wr, theirs["wr"]),
        ("profit_factor", ours.profit_factor, theirs["profit_factor"]),
        ("max_dd_pct", ours.max_dd_pct, theirs["max_dd_pct"]),
        ("sharpe", ours.sharpe, theirs["sharpe"]),
    ]
    all_ok = True
    for name, a, b in checks:
        ok = _within(a, b, args.tol)
        all_ok &= ok
        log.info("%-14s ours=%.6f  vectorbt=%.6f  %s", name, a, b, "OK" if ok else "DIVERGE")
    log.info("trades=%d tol=%.3f", len(trades), args.tol)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
