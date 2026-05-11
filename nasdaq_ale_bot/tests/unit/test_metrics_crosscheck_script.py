"""Unit tests for scripts/metrics_crosscheck.py helper functions.

Does not require vectorbt. Verifies the synthetic-data + SMA-cross pipeline
that feeds our MetricsCalculator produces self-consistent trades and equity.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "metrics_crosscheck.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("metrics_crosscheck", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["metrics_crosscheck"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def test_synthetic_prices_deterministic(mod) -> None:
    a = mod._synthetic_prices(100, seed=7)
    b = mod._synthetic_prices(100, seed=7)
    assert len(a) == 100
    assert (a.values == b.values).all()


def test_sma_cross_signals_emit_at_least_one_pair(mod) -> None:
    close = mod._synthetic_prices(500, seed=3)
    entries, exits = mod._sma_cross_signals(close, fast=10, slow=30)
    assert entries.sum() >= 1
    assert exits.sum() >= 1


def test_our_trades_round_trip(mod) -> None:
    close = mod._synthetic_prices(500, seed=11)
    entries, exits = mod._sma_cross_signals(close, fast=10, slow=30)
    trades = mod._our_trades(close, entries, exits)
    for t in trades:
        assert t.exit_ts > t.entry_ts
        assert isinstance(t.realized_pnl, Decimal)
        assert t.side == "BUY"


def test_our_equity_monotone_in_size(mod) -> None:
    close = mod._synthetic_prices(300, seed=9)
    entries, exits = mod._sma_cross_signals(close, fast=10, slow=30)
    trades = mod._our_trades(close, entries, exits)
    equity = mod._our_equity(close, trades, Decimal("50000"))
    assert len(equity) == len(close)
    # Equity changes only at trade exits.
    changes = sum(1 for i in range(1, len(equity)) if equity[i][1] != equity[i - 1][1])
    assert changes == len(trades)


def test_within_tolerance_helper(mod) -> None:
    assert mod._within(1.0, 1.005, 0.01)
    assert not mod._within(1.0, 1.05, 0.01)
    assert mod._within(0.0, 0.0, 0.01)


def test_main_exits_2_when_vectorbt_missing(mod, monkeypatch) -> None:
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _blocked(name, *a, **kw):
        if name == "vectorbt":
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _blocked)
    monkeypatch.setattr(sys, "argv", ["metrics_crosscheck.py", "--bars", "60"])
    rc = mod.main()
    assert rc == 2
