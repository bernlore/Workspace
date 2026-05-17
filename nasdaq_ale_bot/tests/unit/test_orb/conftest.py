"""Shared fixtures for the ORB test suite."""

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.execution.cost_model import load_cost_model
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.strategies.orb import load_orb_config
from nasdaq_ale_bot.strategies.orb.state_machine import OrbStateMachine

NY = ZoneInfo("America/New_York")
_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def bar():
    """Factory — build a Candle at a NY wall-clock time (naive datetime)."""

    def _make(ny_naive: datetime, o: float, h: float, lo: float, c: float,
              v: float = 100.0) -> Candle:
        utc = ny_naive.replace(tzinfo=NY).astimezone(timezone.utc)
        return Candle(
            ts=utc, open=float(o), high=float(h),
            low=float(lo), close=float(c), volume=float(v),
        )

    return _make


@pytest.fixture
def or_bars(bar):
    """Factory — the 15 opening-range bars (09:30..09:44) with a given hi/lo.

    Bar 0 carries the high/low extremes; the remaining 14 are flat at the
    midpoint, so the resulting range is exactly ``high - low``.
    """

    def _make(day: datetime, high: float, low: float):
        mid = (high + low) / 2
        out = []
        for i in range(15):
            ts = datetime(day.year, day.month, day.day, 9, 30 + i)
            if i == 0:
                out.append(bar(ts, mid, high, low, mid))
            else:
                out.append(bar(ts, mid, mid, mid, mid))
        return out

    return _make


@pytest.fixture
def flat_run(bar):
    """Factory — flat 1-minute bars across [start_dt, end_dt) at one price."""

    def _make(start_dt: datetime, end_dt: datetime, price: float):
        out = []
        t = start_dt
        while t < end_dt:
            out.append(bar(t, price, price + 0.25, price - 0.25, price))
            t += timedelta(minutes=1)
        return out

    return _make


@pytest.fixture
def orb_config():
    return load_orb_config(_REPO / "config" / "orb_strategy.yaml")


@pytest.fixture
def cost_model_nq():
    return load_cost_model(_REPO / "config" / "cost_model.yaml", "nq")


@pytest.fixture
def make_orb_sm(orb_config, cost_model_nq):
    """Factory — a fresh OrbStateMachine wired with broker + ledger + NQ costs.

    Pass ``risk_per_trade`` to override the config's risk budget (used to
    exercise the sizing-skip path).
    """

    def _make(risk_per_trade: float | None = None) -> OrbStateMachine:
        cfg = orb_config
        if risk_per_trade is not None:
            cfg = copy.deepcopy(orb_config)
            cfg["risk"]["risk_per_trade_usd"] = risk_per_trade
        ledger = AccountLedger(
            session_start_equity=Decimal("50000"),
            today=datetime(2024, 1, 2).date(),
        )
        broker = MockBroker(
            ledger=ledger,
            initial_equity=Decimal("50000"),
            point_value=Decimal("20"),
            cost_model=cost_model_nq,
        )
        return OrbStateMachine(
            config=cfg, broker=broker, ledger=ledger,
            tick_size=0.25, point_value=20.0, cost_model=cost_model_nq,
            symbol="NQ",
        )

    return _make
