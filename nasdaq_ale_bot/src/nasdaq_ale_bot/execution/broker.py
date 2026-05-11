"""Broker-side data contracts and :class:`BrokerProtocol` definition.

Phase 1 shipped only an empty package.  Phase 2 lands the full 14-method
protocol, a ``_QuantizingBrokerMixin`` for Decimal quantisation at the
adapter boundary (A1 — second Decimal boundary), and an ``AlpacaBrokerStub``
that satisfies the protocol for type-checking purposes (all bodies raise
``NotImplementedError`` — the real adapter lands in Phase 4).

A1 Decimal-boundary contract
----------------------------
Every concrete broker adapter (AlpacaBroker, TradovateBroker, RithmicBroker,
etc.) MUST inherit :class:`_QuantizingBrokerMixin` and route every float
that leaves the adapter as a Decimal through ``_q_usd`` or ``_q_tick``.
This is the only allowed float->Decimal boundary outside
``OrderFillEvent.from_floats``.

O4 method-name pinning
----------------------
Tests pin the exact 14-method name set; adding a legitimate 15th method
requires an explicit set update documented in the ADR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import AsyncIterator, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MarketClosedError(RuntimeError):
    """Raised by ``assert_market_open`` when ``ts`` is outside RTH."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractSpec:
    """Per-instrument contract metadata returned by :meth:`get_contract_spec`.

    Used by Phase 5 (Apex) for tick-value calculations and margin logic.
    ``tick_value_usd`` MUST be Decimal-quantised at the broker-adapter
    boundary (A1).
    """

    symbol: str
    tick_size: Decimal
    tick_value_usd: Decimal
    contract_size: int
    rth_session: str
    maintenance_window: str | None


@dataclass(frozen=True)
class OrderRef:
    """Opaque handle to a placed bracket order."""

    client_order_id: str
    broker_order_id: str | None


@dataclass(frozen=True)
class OrderState:
    """Snapshot of an order's lifecycle state."""

    client_order_id: str
    status: str  # NEW | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED
    filled_qty: Decimal
    avg_fill_price: Decimal | None


@dataclass(frozen=True)
class Position:
    """Open position snapshot."""

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    unrealized_pnl_usd: Decimal


@dataclass(frozen=True)
class TradingDay:
    """Broker trading-calendar entry."""

    session_date: date
    is_open: bool
    rth_open_utc: datetime | None
    rth_close_utc: datetime | None


@dataclass(frozen=True)
class Bar:
    """Streaming bar payload."""

    symbol: str
    ts_utc: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int


# ---------------------------------------------------------------------------
# BrokerProtocol (14 methods)
# ---------------------------------------------------------------------------


@runtime_checkable
class BrokerProtocol(Protocol):
    """The single broker-facing contract consumed by the engine.

    Method-name set is pinned by :func:`test_broker_protocol_method_name_set_pinned`.
    """

    # --- Existing 9 from §A20 ---
    def place_bracket(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        entry: Decimal,
        stop: Decimal,
        take_profit: Decimal,
        client_order_id: str,
    ) -> OrderRef: ...

    def modify_bracket_stop(
        self, order_id: str, new_stop_price: Decimal
    ) -> None: ...

    def cancel_all(self, symbol: str | None = None) -> None: ...

    def flatten(self, symbol: str | None = None) -> None: ...

    def get_positions(self) -> list[Position]: ...

    def get_account_equity(self) -> Decimal: ...

    def get_order(self, client_order_id: str) -> OrderState | None: ...

    def get_trading_calendar(self, day: date) -> TradingDay: ...

    def stream_bars(self, symbols: list[str]) -> AsyncIterator[Bar]: ...

    # --- NEW in Phase 2 ---
    def get_contract_spec(self, symbol: str) -> ContractSpec: ...

    def get_session_pnl(self) -> Decimal: ...

    def get_realtime_equity(self) -> Decimal: ...

    def assert_market_open(self, ts: datetime) -> None: ...

    def submit_market_flatten(self, symbol: str) -> OrderRef: ...


# Pinned method-name set (O4)
BROKER_PROTOCOL_METHODS: frozenset[str] = frozenset(
    {
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
)


# ---------------------------------------------------------------------------
# _QuantizingBrokerMixin (A1 Decimal-boundary helpers)
# ---------------------------------------------------------------------------


_USD_QUANT = Decimal("0.01")


class _QuantizingBrokerMixin:
    """Decimal-quantisation helpers for concrete broker adapters (A1).

    Any adapter method returning a Decimal (equity, PnL, tick values)
    MUST route its raw float / numeric input through :meth:`_q_usd` or
    :meth:`_q_tick` so the result is guaranteed to:
        * be an instance of Decimal (not float)
        * be quantised to cent precision (USD) or the instrument's
          tick precision (non-USD)

    Tested by ``test_broker_adapter_returns_quantized_decimal``.
    """

    @staticmethod
    def _q_usd(raw: float | int | Decimal | str) -> Decimal:
        """Quantise to USD cents (two decimal places) using HALF_EVEN."""
        return Decimal(str(raw)).quantize(_USD_QUANT, rounding=ROUND_HALF_EVEN)

    @staticmethod
    def _q_tick(
        raw: float | int | Decimal | str, tick: Decimal
    ) -> Decimal:
        """Quantise to an instrument-specific tick precision."""
        if not isinstance(tick, Decimal):
            raise TypeError("tick must be Decimal")
        return Decimal(str(raw)).quantize(tick, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# AlpacaBrokerStub — protocol-compliant placeholder
# ---------------------------------------------------------------------------


class AlpacaBrokerStub(_QuantizingBrokerMixin):
    """Stub that satisfies :class:`BrokerProtocol` for type-checking.

    Phase 4 ships the real implementation.  Every method raises
    ``NotImplementedError`` so the engine bootstrap fails loudly if the
    stub is ever used for live trading.
    """

    def place_bracket(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        entry: Decimal,
        stop: Decimal,
        take_profit: Decimal,
        client_order_id: str,
    ) -> OrderRef:
        raise NotImplementedError("AlpacaBrokerStub: place_bracket")

    def modify_bracket_stop(
        self, order_id: str, new_stop_price: Decimal
    ) -> None:
        raise NotImplementedError("AlpacaBrokerStub: modify_bracket_stop")

    def cancel_all(self, symbol: str | None = None) -> None:
        raise NotImplementedError("AlpacaBrokerStub: cancel_all")

    def flatten(self, symbol: str | None = None) -> None:
        raise NotImplementedError("AlpacaBrokerStub: flatten")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError("AlpacaBrokerStub: get_positions")

    def get_account_equity(self) -> Decimal:
        raise NotImplementedError("AlpacaBrokerStub: get_account_equity")

    def get_order(self, client_order_id: str) -> OrderState | None:
        raise NotImplementedError("AlpacaBrokerStub: get_order")

    def get_trading_calendar(self, day: date) -> TradingDay:
        raise NotImplementedError("AlpacaBrokerStub: get_trading_calendar")

    def stream_bars(self, symbols: list[str]) -> AsyncIterator[Bar]:
        raise NotImplementedError("AlpacaBrokerStub: stream_bars")

    # --- NEW in Phase 2 ---

    def get_contract_spec(self, symbol: str) -> ContractSpec:
        raise NotImplementedError("AlpacaBrokerStub: get_contract_spec")

    def get_session_pnl(self) -> Decimal:
        raise NotImplementedError("AlpacaBrokerStub: get_session_pnl")

    def get_realtime_equity(self) -> Decimal:
        raise NotImplementedError("AlpacaBrokerStub: get_realtime_equity")

    def assert_market_open(self, ts: datetime) -> None:
        raise NotImplementedError("AlpacaBrokerStub: assert_market_open")

    def submit_market_flatten(self, symbol: str) -> OrderRef:
        raise NotImplementedError("AlpacaBrokerStub: submit_market_flatten")
