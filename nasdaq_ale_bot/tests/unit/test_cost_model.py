"""Tests for the unified cost model (config/cost_model.yaml + MockBroker).

Key guarantee (AI_INSIGHTS #3): a flat round-trip on 1 NQ contract — enter
and exit at the same intended price — costs exactly $19.00 net of price
movement ($9 commission + $10 slippage).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.execution.cost_model import CostModel, load_cost_model
from nasdaq_ale_bot.execution.mock_broker import MockBroker

REPO = Path(__file__).resolve().parents[2]
COST_MODEL_PATH = REPO / "config" / "cost_model.yaml"
_T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def _flat_bar(i: int, price: float) -> Candle:
    return Candle(
        ts=_T0 + timedelta(minutes=i),
        open=price, high=price, low=price, close=price, volume=100.0,
    )


# --- CostModel loader -------------------------------------------------------

def test_load_cost_model_nq() -> None:
    cm = load_cost_model(COST_MODEL_PATH, "nq")
    assert cm.instrument == "nq"
    assert cm.commission_per_side_per_contract == Decimal("4.50")
    assert cm.slippage_ticks_per_side == 1
    assert cm.tick_value_usd == Decimal("5.00")
    assert cm.commission_round_trip == Decimal("9.00")


def test_load_cost_model_mnq() -> None:
    cm = load_cost_model(COST_MODEL_PATH, "mnq")
    assert cm.commission_round_trip == Decimal("1.00")
    assert cm.tick_value_usd == Decimal("0.50")


def test_load_cost_model_unknown_instrument_raises() -> None:
    with pytest.raises(KeyError):
        load_cost_model(COST_MODEL_PATH, "spx")


def test_slippage_price_uses_tick_size() -> None:
    cm = load_cost_model(COST_MODEL_PATH, "nq")
    # NQ tick size = tick_value / point_value = 5.00 / 20 = 0.25.
    assert cm.slippage_price(Decimal("0.25")) == Decimal("0.25")


# --- $19 round-trip via MockBroker -----------------------------------------

def _round_trip_pnl(cost_model: CostModel | None, point_value: Decimal) -> Decimal:
    """Enter 1 contract and exit at the SAME price; return realized pnl."""
    ledger = AccountLedger(
        session_start_equity=Decimal("50000"), today=_T0.date()
    )
    broker = MockBroker(
        ledger=ledger,
        initial_equity=Decimal("50000"),
        point_value=point_value,
        cost_model=cost_model,
    )
    broker.place_bracket(
        symbol="NQ", side="BUY", qty=Decimal("1"),
        entry=Decimal("20000"), stop=Decimal("19000"),
        take_profit=Decimal("21000"),
        client_order_id="rt-1", order_type="MARKET",
        slippage_max_price=Decimal("100"),
    )
    # Bar 0: MARKET entry fills at open (20000), slipped by the cost model.
    broker.evaluate_fills(_flat_bar(0, 20000.0))
    # Bar 1: flatten — exit at bar.close (20000), slipped by the cost model.
    broker.flatten()
    fills = broker.evaluate_fills(_flat_bar(1, 20000.0))
    exits = [f for f in fills if f.fill_reason != "ENTRY"]
    assert len(exits) == 1
    return exits[0].realized_pnl


def test_nq_flat_round_trip_costs_exactly_19() -> None:
    cm = load_cost_model(COST_MODEL_PATH, "nq")
    pnl = _round_trip_pnl(cm, point_value=Decimal("20"))
    # $9 commission + 2 ticks slippage * $5/tick = $10  ->  -$19.00.
    assert pnl == Decimal("-19.00")


def test_mnq_flat_round_trip_costs_exactly_2() -> None:
    cm = load_cost_model(COST_MODEL_PATH, "mnq")
    pnl = _round_trip_pnl(cm, point_value=Decimal("2"))
    # $1 commission + 2 ticks slippage * $0.50/tick = $1  ->  -$2.00.
    assert pnl == Decimal("-2.00")


def test_legacy_round_trip_unchanged_without_cost_model() -> None:
    # No cost model: legacy behaviour — $4.50 once at exit, no slippage.
    pnl = _round_trip_pnl(None, point_value=Decimal("20"))
    assert pnl == Decimal("-4.50")
