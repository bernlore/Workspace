"""Unit tests for execution/broker.py — BrokerProtocol + Quantizing mixin."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from nasdaq_ale_bot.execution.broker import (
    BROKER_PROTOCOL_METHODS,
    AlpacaBrokerStub,
    BrokerProtocol,
    ContractSpec,
    _QuantizingBrokerMixin,
)

# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------

_EXPECTED_METHODS = {
    "place_bracket",
    "modify_bracket_stop",
    "cancel_all",
    "flatten",
    "get_positions",
    "get_account_equity",
    "get_order",
    "get_trading_calendar",
    "stream_bars",
    "get_contract_spec",
    "get_session_pnl",
    "get_realtime_equity",
    "assert_market_open",
    "submit_market_flatten",
}


def _protocol_methods() -> set[str]:
    return {
        name
        for name, obj in inspect.getmembers(BrokerProtocol)
        if inspect.isfunction(obj) and not name.startswith("_")
    }


def test_broker_protocol_has_14_methods() -> None:
    methods = _protocol_methods()
    assert len(methods) == 14, f"Expected 14, got {len(methods)}: {sorted(methods)}"


def test_broker_protocol_method_name_set_pinned() -> None:
    """O4 — pins the exact set of method names.

    Adding a legitimate 15th method requires updating both _EXPECTED_METHODS
    here and BROKER_PROTOCOL_METHODS in broker.py, and documenting the
    change in the ADR method-addition policy.
    """
    methods = _protocol_methods()
    assert methods == _EXPECTED_METHODS
    assert BROKER_PROTOCOL_METHODS == frozenset(_EXPECTED_METHODS)


def test_alpaca_stub_satisfies_protocol() -> None:
    stub = AlpacaBrokerStub()
    assert isinstance(stub, BrokerProtocol)


# ---------------------------------------------------------------------------
# Apex-path methods raise NotImplementedError on stub
# ---------------------------------------------------------------------------


def test_apex_methods_raise_not_implemented() -> None:
    stub = AlpacaBrokerStub()
    with pytest.raises(NotImplementedError):
        stub.get_contract_spec("MNQ")
    with pytest.raises(NotImplementedError):
        stub.get_session_pnl()
    with pytest.raises(NotImplementedError):
        stub.get_realtime_equity()
    with pytest.raises(NotImplementedError):
        stub.assert_market_open(datetime.now(timezone.utc))
    with pytest.raises(NotImplementedError):
        stub.submit_market_flatten("MNQ")


def test_existing_methods_also_raise_on_stub() -> None:
    """All 9 pre-existing protocol methods also raise NotImplementedError."""
    stub = AlpacaBrokerStub()
    with pytest.raises(NotImplementedError):
        stub.place_bracket(
            symbol="MNQ",
            side="BUY",
            qty=Decimal("1"),
            entry=Decimal("18000"),
            stop=Decimal("17990"),
            take_profit=Decimal("18020"),
            client_order_id="x",
        )
    with pytest.raises(NotImplementedError):
        stub.modify_bracket_stop("x", Decimal("17990"))
    with pytest.raises(NotImplementedError):
        stub.cancel_all()
    with pytest.raises(NotImplementedError):
        stub.flatten()
    with pytest.raises(NotImplementedError):
        stub.get_positions()
    with pytest.raises(NotImplementedError):
        stub.get_account_equity()
    with pytest.raises(NotImplementedError):
        stub.get_order("x")
    with pytest.raises(NotImplementedError):
        stub.get_trading_calendar(date(2024, 1, 15))
    with pytest.raises(NotImplementedError):
        stub.stream_bars(["MNQ"])


# ---------------------------------------------------------------------------
# _QuantizingBrokerMixin boundary tests (A1)
# ---------------------------------------------------------------------------


class _FakeAdapter(_QuantizingBrokerMixin):
    """Minimal adapter that exercises the quantising helpers."""

    def get_account_equity_from_float(self, raw: float) -> Decimal:
        return self._q_usd(raw)

    def get_realtime_equity_from_float(self, raw: float) -> Decimal:
        return self._q_usd(raw)

    def get_session_pnl_from_float(self, raw: float) -> Decimal:
        return self._q_usd(raw)

    def tick_value_from_float(self, raw: float, tick: Decimal) -> Decimal:
        return self._q_tick(raw, tick)


def test_broker_adapter_returns_quantized_decimal() -> None:
    """A1 — every Decimal-returning method output matches its quantisation."""
    adapter = _FakeAdapter()

    # USD quantisation — the IEEE-754 hazard 0.1 + 0.2 = 0.30000000000000004
    # must still land cleanly on 0.30
    usd_cases = [
        (0.1 + 0.2, Decimal("0.30")),
        (12345.6789, Decimal("12345.68")),
        (0.005, Decimal("0.00")),  # banker's rounding: 0.005 -> 0.00
        (0.015, Decimal("0.02")),  # banker's rounding: 0.015 -> 0.02
        (-250.127, Decimal("-250.13")),
    ]
    for raw, expected in usd_cases:
        result = adapter.get_account_equity_from_float(raw)
        assert isinstance(result, Decimal)
        assert result == expected, f"for {raw}: got {result}, expected {expected}"
        assert result == result.quantize(Decimal("0.01"))

    # Tick quantisation — e.g. NQ tick size 0.25
    tick_val = adapter.tick_value_from_float(18000.37, Decimal("0.25"))
    assert isinstance(tick_val, Decimal)
    assert tick_val.quantize(Decimal("0.25")) == tick_val


def test_q_tick_rejects_non_decimal_tick() -> None:
    adapter = _FakeAdapter()
    with pytest.raises(TypeError):
        adapter._q_tick(1.0, 0.25)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dataclass smoke tests
# ---------------------------------------------------------------------------


def test_contract_spec_is_frozen() -> None:
    spec = ContractSpec(
        symbol="MNQ",
        tick_size=Decimal("0.25"),
        tick_value_usd=Decimal("0.50"),
        contract_size=2,
        rth_session="CME_GLOBEX",
        maintenance_window="17:00-18:00 ET",
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        spec.symbol = "ES"  # type: ignore[misc]
