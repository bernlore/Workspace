"""MockBroker — protocol-compliant backtest broker (Phase 3 Step 2).

Fill model (delta 2 / §3.3):
  Long LIMIT entry at entry_price:
    if bar.open <= entry_price: fill at bar.open   (gap-down through limit)
    elif bar.low <= entry_price: fill at entry_price (normal touch)
    else: carry forward (untouched)

  Short LIMIT entry at entry_price:
    if bar.open >= entry_price: fill at bar.open   (gap-up through limit)
    elif bar.high >= entry_price: fill at entry_price (normal touch)
    else: carry forward (untouched)

Exit priority (SL wins — A22):
  Long:
    1. bar.open <= stop_price  → STOP_OUT at bar.open
    2. bar.low  <= stop_price  → STOP_OUT at stop_price
    3. bar.open >= take_profit → TAKE_PROFIT at bar.open
    4. bar.high >= take_profit → TAKE_PROFIT at take_profit_price
    5. No exit

  Short: mirror of above.

Flatten: positions are closed at bar.close on the next evaluate_fills call
after flatten() is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Literal

import structlog

from nasdaq_ale_bot.core.account_ledger import AccountLedger, OrderFillEvent
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.execution.broker import (
    Bar,
    BrokerProtocol,
    ContractSpec,
    OrderRef,
    OrderState,
    Position,
    TradingDay,
    _QuantizingBrokerMixin,
)

_log = structlog.get_logger(__name__)

# Round-trip commission charged on the EXIT side ($4.50 per contract — CME
# standard). Deducted from ``FillEvent.realized_pnl`` at the emit site so all
# downstream consumers (TradeRecord, ledger, equity) see net-of-fee pnl.
COMMISSION_PER_CONTRACT = Decimal("4.50")


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


# Mutable by design: modify_bracket_stop mutates stop_price in-place.
# Do not add frozen=True without also rewriting the modify path.
@dataclass
class BracketOrder:
    """Pending bracket awaiting fill on subsequent bars.

    Two entry modes:
      LIMIT   — fill when price touches ``entry_price`` (legacy semantics).
      MARKET  — fill at the next bar's open if ``|open - entry_price|`` is
                within ``slippage_max_price``; otherwise the order is
                cancelled (no fill, no carry-forward).
    """

    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    client_order_id: str
    armed_at_ts: datetime
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    slippage_max_price: Decimal = Decimal("0")


# Mutable by design: modify_bracket_stop mutates stop_price in-place.
# Do not add frozen=True without also rewriting the modify path.
@dataclass
class MockPosition:
    """Open position held by MockBroker."""

    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    client_order_id: str
    entry_bar_ts: datetime


@dataclass(frozen=True)
class FillEvent:
    """Emitted by MockBroker.evaluate_fills() for each fill on the current bar."""

    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal
    fill_price: Decimal
    fill_ts: datetime
    fill_reason: Literal["ENTRY", "STOP_OUT", "TAKE_PROFIT", "FLATTEN"]
    realized_pnl: Decimal  # 0 for ENTRY; signed for exits


# ---------------------------------------------------------------------------
# MockBroker
# ---------------------------------------------------------------------------


class MockBroker(_QuantizingBrokerMixin):
    """Protocol-compliant broker for backtesting. Implements all 14 BrokerProtocol methods.

    Fill model: "SL wins" on intrabar SL+TP collision (A22).
    Entry model: LIMIT at entry_price with gap-open fallback and indefinite carry-forward.

    Usage::

        ledger = AccountLedger(session_start_equity=Decimal("50000"), today=date.today())
        broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"))
        broker.place_bracket(...)
        fills = broker.evaluate_fills(bar)

    ``evaluate_fills`` is not part of BrokerProtocol; it is called by BacktestRunner
    once per bar.
    """

    def __init__(
        self,
        *,
        ledger: AccountLedger,
        initial_equity: Decimal,
        fill_model: Literal["sl_wins"] = "sl_wins",
        point_value: Decimal = Decimal("1"),
    ) -> None:
        self._ledger = ledger
        self._equity: Decimal = initial_equity
        self._fill_model = fill_model
        # Dollar value of one full point (1.0 price unit) per contract. For
        # equities (QQQ/SPY) point_value=1 reproduces share-cash semantics; for
        # futures (NQ=20, ES=50, MNQ=2) it scales pnl into account dollars.
        self._point_value: Decimal = point_value
        self._pending: list[BracketOrder] = []
        self._positions: dict[str, MockPosition] = {}  # keyed by client_order_id
        self._order_history: dict[str, OrderState] = {}
        self._pending_flatten_symbols: set[str | None] = set()
        # Accumulated commission across all exits (in account dollars).
        self._commission_total: Decimal = Decimal("0")

    # ------------------------------------------------------------------
    # Phase 3 unique method — called by BacktestRunner once per bar
    # ------------------------------------------------------------------

    def evaluate_fills(self, bar: Candle) -> list[FillEvent]:
        """Main fill evaluation loop.

        Priority order (§3.3):
          1. Flatten requests  — market close at bar.close
          2. Pending entries   — LIMIT semantics, gap-fill / carry-forward
          3. Stop-loss exits   — SL wins collision (A22)
          4. Take-profit exits — only if SL did not fire

        Fill ts: bar.ts (the evaluating bar's ts, UTC).
        """
        fills: list[FillEvent] = []

        # Phase 1: drain flatten requests (close at bar.close)
        for symbol in list(self._pending_flatten_symbols):
            for coid, pos in list(self._positions.items()):
                if symbol is None or pos.symbol == symbol:
                    fill = self._close_position(
                        pos, Decimal(str(bar.close)), bar.ts, "FLATTEN"
                    )
                    fills.append(fill)
                    del self._positions[coid]
            self._pending_flatten_symbols.discard(symbol)

        # Phase 2: process pending entries (LIMIT carries forward; MARKET is one-shot)
        still_pending: list[BracketOrder] = []
        for order in self._pending:
            fill = self._try_fill_entry(order, bar)
            if fill is None:
                if order.order_type == "MARKET":
                    # MARKET: one-shot; failed fill (gap past slippage cap) cancels.
                    self._order_history[order.client_order_id] = OrderState(
                        client_order_id=order.client_order_id,
                        status="CANCELLED",
                        filled_qty=Decimal("0"),
                        avg_fill_price=None,
                    )
                    _log.info(
                        "mock_broker.market_cancelled",
                        client_order_id=order.client_order_id,
                        bar_open=str(bar.open),
                        entry_ref=str(order.entry_price),
                        slippage_max=str(order.slippage_max_price),
                    )
                else:
                    still_pending.append(order)  # LIMIT carries forward
            else:
                fills.append(fill)
                self._positions[order.client_order_id] = MockPosition(
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    entry_price=fill.fill_price,
                    stop_price=order.stop_price,
                    take_profit_price=order.take_profit_price,
                    client_order_id=order.client_order_id,
                    entry_bar_ts=bar.ts,
                )
                self._order_history[order.client_order_id] = OrderState(
                    client_order_id=order.client_order_id,
                    status="FILLED",
                    filled_qty=order.qty,
                    avg_fill_price=fill.fill_price,
                )
        self._pending = still_pending

        # Phase 3: check exits for all open positions (SL first, then TP — SL wins)
        for coid, pos in list(self._positions.items()):
            exit_fill = self._try_fill_stop(pos, bar)
            if exit_fill is not None:
                fills.append(exit_fill)
                del self._positions[coid]
                continue
            exit_fill = self._try_fill_take_profit(pos, bar)
            if exit_fill is not None:
                fills.append(exit_fill)
                del self._positions[coid]

        # Dispatch every fill to ledger and update equity
        for fill in fills:
            self._dispatch_to_ledger(fill)

        return fills

    # ------------------------------------------------------------------
    # BrokerProtocol — 14 methods
    # ------------------------------------------------------------------

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
        order_type: Literal["LIMIT", "MARKET"] = "LIMIT",
        slippage_max_price: Decimal = Decimal("0"),
    ) -> OrderRef:
        """Append a BracketOrder to pending list; record in history as NEW."""
        order = BracketOrder(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            entry_price=entry,
            stop_price=stop,
            take_profit_price=take_profit,
            client_order_id=client_order_id,
            armed_at_ts=datetime.now(timezone.utc),
            order_type=order_type,
            slippage_max_price=slippage_max_price,
        )
        self._pending.append(order)
        self._order_history[client_order_id] = OrderState(
            client_order_id=client_order_id,
            status="NEW",
            filled_qty=Decimal("0"),
            avg_fill_price=None,
        )
        _log.debug(
            "mock_broker.place_bracket",
            symbol=symbol,
            side=side,
            entry=entry,
            client_order_id=client_order_id,
        )
        return OrderRef(client_order_id=client_order_id, broker_order_id=None)

    def modify_bracket_stop(self, order_id: str, new_stop_price: Decimal) -> None:
        """Update stop_price on open position matching client_order_id.

        Raises KeyError if no position found with that client_order_id.
        """
        if order_id in self._positions:
            self._positions[order_id].stop_price = new_stop_price
            return
        # Also check pending orders
        for order in self._pending:
            if order.client_order_id == order_id:
                order.stop_price = new_stop_price
                return
        raise KeyError(f"No position or pending order found for order_id={order_id!r}")

    def cancel_all(self, symbol: str | None = None) -> None:
        """Remove pending brackets for symbol (or all if symbol is None)."""
        if symbol is None:
            cancelled = list(self._pending)
            self._pending = []
        else:
            cancelled = [o for o in self._pending if o.symbol == symbol]
            self._pending = [o for o in self._pending if o.symbol != symbol]

        for order in cancelled:
            self._order_history[order.client_order_id] = OrderState(
                client_order_id=order.client_order_id,
                status="CANCELED",
                filled_qty=Decimal("0"),
                avg_fill_price=None,
            )

    def place_immediate(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        fill_price: Decimal,
        stop: Decimal,
        take_profit: Decimal,
        client_order_id: str,
        fill_ts: datetime,
    ) -> FillEvent:
        """Book a position immediately at ``fill_price`` and emit ENTRY fill.

        Bypasses the LIMIT/MARKET pending pipeline. Used when the SM zone
        monitor enters on the same bar as the retest detection — the
        retest bar's close is the fill price, no next-bar gap risk, no
        slippage cap. The returned FillEvent should be passed through the
        runner's fill processor so a TradeRecord stub is opened.
        """
        if client_order_id in self._positions:
            raise ValueError(
                f"place_immediate: position already exists for {client_order_id}"
            )
        self._positions[client_order_id] = MockPosition(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            entry_price=fill_price,
            stop_price=stop,
            take_profit_price=take_profit,
            client_order_id=client_order_id,
            entry_bar_ts=fill_ts,
        )
        self._order_history[client_order_id] = OrderState(
            client_order_id=client_order_id,
            status="FILLED",
            filled_qty=qty,
            avg_fill_price=fill_price,
        )
        fill = FillEvent(
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            fill_price=fill_price,
            fill_ts=fill_ts,
            fill_reason="ENTRY",
            realized_pnl=Decimal("0"),
        )
        self._dispatch_to_ledger(fill)
        _log.info(
            "mock_broker.place_immediate",
            client_order_id=client_order_id,
            side=side,
            fill_price=str(fill_price),
            stop=str(stop),
            take_profit=str(take_profit),
        )
        return fill

    def cancel_pending(self, client_order_id: str | None = None) -> int:
        """Cancel pending bracket(s). Returns number of orders cancelled.

        With ``client_order_id=None`` cancels all pending — used by the runner
        when a pre-fill SM transition (zone invalidated / session end) needs
        to drop a LIMIT before it can fill on a later bar.
        """
        if client_order_id is None:
            n = len(self._pending)
            for o in self._pending:
                self._order_history[o.client_order_id] = OrderState(
                    client_order_id=o.client_order_id,
                    status="CANCELLED",
                    filled_qty=Decimal("0"),
                    avg_fill_price=None,
                )
            self._pending = []
            return n
        keep: list[BracketOrder] = []
        cancelled = 0
        for o in self._pending:
            if o.client_order_id == client_order_id:
                self._order_history[o.client_order_id] = OrderState(
                    client_order_id=o.client_order_id,
                    status="CANCELLED",
                    filled_qty=Decimal("0"),
                    avg_fill_price=None,
                )
                cancelled += 1
            else:
                keep.append(o)
        self._pending = keep
        return cancelled

    def flatten(self, symbol: str | None = None) -> None:
        """Schedule position close at next evaluate_fills bar.close.

        Positions are closed when evaluate_fills is next called; the fill
        price is that bar's close. Pending entries for the symbol are also
        cancelled.
        """
        self._pending_flatten_symbols.add(symbol)
        # Cancel pending entries for the affected symbol(s)
        self.cancel_all(symbol)

    def get_positions(self) -> list[Position]:
        """Return current open positions as Position snapshots."""
        result: list[Position] = []
        for pos in self._positions.values():
            result.append(
                Position(
                    symbol=pos.symbol,
                    qty=pos.qty,
                    avg_entry_price=pos.entry_price,
                    unrealized_pnl_usd=Decimal("0"),  # not tracked intrabar
                )
            )
        return result

    def get_account_equity(self) -> Decimal:
        """Return current equity (initial equity + realized PnL)."""
        return self._equity

    def get_order(self, client_order_id: str) -> OrderState | None:
        """Return OrderState from history, or None if unknown."""
        return self._order_history.get(client_order_id)

    def get_trading_calendar(self, day: date) -> TradingDay:
        """Stub: backtest is always open."""
        return TradingDay(
            session_date=day,
            is_open=True,
            rth_open_utc=None,
            rth_close_utc=None,
        )

    def stream_bars(self, symbols: list[str]) -> AsyncIterator[Bar]:
        """Not supported; BacktestRunner provides bars directly."""
        raise NotImplementedError("BacktestRunner provides bars")

    def get_contract_spec(self, symbol: str) -> ContractSpec:
        """Not supported in Phase 3 equity backtest."""
        raise NotImplementedError(
            "Phase 3 is equities; use real broker for futures"
        )

    def get_session_pnl(self) -> Decimal:
        """Return realized PnL for today from ledger."""
        return self._ledger.realized_today

    def get_realtime_equity(self) -> Decimal:
        """Return current equity (same as get_account_equity in backtest)."""
        return self._equity

    def assert_market_open(self, ts: datetime) -> None:
        """No-op in backtest — market is always open."""

    def submit_market_flatten(self, symbol: str) -> OrderRef:
        """Delegate to flatten() and return a dummy OrderRef."""
        self.flatten(symbol)
        dummy_id = f"flatten-{symbol}"
        return OrderRef(client_order_id=dummy_id, broker_order_id=None)

    # ------------------------------------------------------------------
    # Private fill helpers
    # ------------------------------------------------------------------

    def _try_fill_entry(self, order: BracketOrder, bar: Candle) -> FillEvent | None:
        """Attempt entry fill on this bar.

        LIMIT (delta-2 semantics):
          Long:  open<=entry → fill at open; elif low<=entry → fill at entry; else carry.
          Short: open>=entry → fill at open; elif high>=entry → fill at entry; else carry.

        MARKET (one-shot at next bar's open, slippage-bounded):
          Long:  fill at bar.open IFF bar.open <= entry_price + slippage_max; else cancel.
          Short: fill at bar.open IFF bar.open >= entry_price - slippage_max; else cancel.
          ``entry_price`` is interpreted as the IFVG zone edge — the slippage cap
          gates how far past that edge we accept the open.
        """
        open_d = Decimal(str(bar.open))
        entry = order.entry_price

        if order.order_type == "MARKET":
            slip = order.slippage_max_price
            if order.side == "BUY":
                if open_d <= entry + slip:
                    fill_price = open_d
                else:
                    return None  # gap past slippage cap → cancel
            else:  # SELL
                if open_d >= entry - slip:
                    fill_price = open_d
                else:
                    return None
            return FillEvent(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                fill_price=fill_price,
                fill_ts=bar.ts,
                fill_reason="ENTRY",
                realized_pnl=Decimal("0"),
            )

        # LIMIT (legacy)
        low_d = Decimal(str(bar.low))
        high_d = Decimal(str(bar.high))
        if order.side == "BUY":
            if open_d <= entry:
                fill_price = open_d  # gap-down through limit
            elif low_d <= entry:
                fill_price = entry  # normal touch
            else:
                return None  # untouched, carry forward
        else:  # SELL
            if open_d >= entry:
                fill_price = open_d  # gap-up through limit
            elif high_d >= entry:
                fill_price = entry  # normal touch
            else:
                return None  # untouched, carry forward

        return FillEvent(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            fill_price=fill_price,
            fill_ts=bar.ts,
            fill_reason="ENTRY",
            realized_pnl=Decimal("0"),
        )

    def _try_fill_stop(self, pos: MockPosition, bar: Candle) -> FillEvent | None:
        """Check stop-loss exit (SL wins — A22).

        Long:
          1. bar.open <= stop_price → STOP_OUT at bar.open (gap down)
          2. bar.low  <= stop_price → STOP_OUT at stop_price
        Short:
          1. bar.open >= stop_price → STOP_OUT at bar.open (gap up)
          2. bar.high >= stop_price → STOP_OUT at stop_price
        """
        open_d = Decimal(str(bar.open))
        low_d = Decimal(str(bar.low))
        high_d = Decimal(str(bar.high))

        if pos.side == "BUY":
            if open_d <= pos.stop_price:
                fill_price = open_d
            elif low_d <= pos.stop_price:
                fill_price = pos.stop_price
            else:
                return None
        else:  # SELL
            if open_d >= pos.stop_price:
                fill_price = open_d
            elif high_d >= pos.stop_price:
                fill_price = pos.stop_price
            else:
                return None

        pnl = self._calc_pnl(pos, fill_price)
        net_pnl = pnl - COMMISSION_PER_CONTRACT * pos.qty
        return FillEvent(
            client_order_id=pos.client_order_id,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            fill_price=fill_price,
            fill_ts=bar.ts,
            fill_reason="STOP_OUT",
            realized_pnl=net_pnl,
        )

    def _try_fill_take_profit(self, pos: MockPosition, bar: Candle) -> FillEvent | None:
        """Check take-profit exit.

        Long:
          1. bar.open >= take_profit_price → TAKE_PROFIT at bar.open (gap up)
          2. bar.high >= take_profit_price → TAKE_PROFIT at take_profit_price
        Short:
          1. bar.open <= take_profit_price → TAKE_PROFIT at bar.open (gap down)
          2. bar.low  <= take_profit_price → TAKE_PROFIT at take_profit_price
        """
        open_d = Decimal(str(bar.open))
        high_d = Decimal(str(bar.high))
        low_d = Decimal(str(bar.low))

        if pos.side == "BUY":
            if open_d >= pos.take_profit_price:
                fill_price = open_d
            elif high_d >= pos.take_profit_price:
                fill_price = pos.take_profit_price
            else:
                return None
        else:  # SELL
            if open_d <= pos.take_profit_price:
                fill_price = open_d
            elif low_d <= pos.take_profit_price:
                fill_price = pos.take_profit_price
            else:
                return None

        pnl = self._calc_pnl(pos, fill_price)
        net_pnl = pnl - COMMISSION_PER_CONTRACT * pos.qty
        return FillEvent(
            client_order_id=pos.client_order_id,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            fill_price=fill_price,
            fill_ts=bar.ts,
            fill_reason="TAKE_PROFIT",
            realized_pnl=net_pnl,
        )

    def _close_position(
        self,
        pos: MockPosition,
        fill_price: Decimal,
        fill_ts: datetime,
        reason: Literal["STOP_OUT", "TAKE_PROFIT", "FLATTEN"],
    ) -> FillEvent:
        """Build a FillEvent for a closing trade.

        A round-trip commission of ``COMMISSION_PER_CONTRACT * qty`` is charged
        on the exit fill (entry side is free) — CME-standard fee structure.
        ``realized_pnl`` on the returned FillEvent is **net of commissions**
        so downstream TradeRecords / equity / ledger see the post-fee number.
        """
        pnl = self._calc_pnl(pos, fill_price)
        commission = COMMISSION_PER_CONTRACT * pos.qty
        net_pnl = pnl - commission
        return FillEvent(
            client_order_id=pos.client_order_id,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            fill_price=fill_price,
            fill_ts=fill_ts,
            fill_reason=reason,
            realized_pnl=net_pnl,
        )

    def _calc_pnl(self, pos: MockPosition, exit_price: Decimal) -> Decimal:
        """Compute signed realized PnL for a closing trade.

        BUY:  qty * point_value * (exit_price - entry_price)
        SELL: qty * point_value * (entry_price - exit_price)
        """
        if pos.side == "BUY":
            return pos.qty * self._point_value * (exit_price - pos.entry_price)
        return pos.qty * self._point_value * (pos.entry_price - exit_price)

    def _dispatch_to_ledger(self, fill: FillEvent) -> None:
        """Send fill to AccountLedger and update equity.

        Exit fills carry ``COMMISSION_PER_CONTRACT * qty`` already deducted
        from ``fill.realized_pnl`` (see :meth:`_close_position`); we surface
        the explicit commission on the ledger event for the audit trail and
        accumulate the running broker-level commission_total.
        """
        commission = (
            COMMISSION_PER_CONTRACT * fill.qty
            if fill.fill_reason != "ENTRY"
            else Decimal("0")
        )
        if commission > 0:
            self._commission_total += commission
        event = OrderFillEvent(
            fill_ts=fill.fill_ts,
            symbol=fill.symbol,
            side=fill.side,
            qty=fill.qty,
            fill_price=fill.fill_price,
            fees=commission,
            realized_pnl_delta=fill.realized_pnl,
        )
        self._ledger.on_fill(event)
        self._equity += fill.realized_pnl


# Verify at import time that MockBroker satisfies the protocol (dev guard).
assert isinstance(MockBroker.__new__(MockBroker), BrokerProtocol), (
    "MockBroker does not satisfy BrokerProtocol — check method signatures."
)
