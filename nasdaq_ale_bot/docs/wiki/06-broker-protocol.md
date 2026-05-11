# BrokerProtocol Interface (A20)

## Purpose

The strategy and detection layers never import broker-specific code. All broker interaction goes through a `typing.Protocol` with 9 methods. This enables:

1. **Instrument swap:** QQQ/SPY (Alpaca) today, MNQ/ES (Tradovate/IBKR) tomorrow -- no strategy changes
2. **Testability:** Mock the protocol in integration tests without touching live APIs
3. **Crash recovery:** Methods 5-7 enable reconnection reconciliation

## The 9 Methods

```python
class BrokerProtocol(Protocol):
    # --- Order lifecycle ---
    def place_bracket(self, symbol, side, qty, entry, stop, take_profit,
                      client_order_id) -> OrderRef: ...

    def modify_bracket_stop(self, order_id, new_stop_price) -> None: ...
        # Used for A17 BE moves

    def cancel_all(self, symbol: str | None = None) -> None: ...

    def flatten(self, symbol: str | None = None) -> None: ...

    # --- Account state ---
    def get_positions(self) -> list[Position]: ...
        # Reconnection reconciliation

    def get_account_equity(self) -> Decimal: ...
        # A14 percent-mode risk sizing (session start only)

    def get_order(self, client_order_id: str) -> OrderState | None: ...
        # A19 idempotency check on reconnect

    # --- Market data ---
    def get_trading_calendar(self, date: date) -> TradingDay: ...
        # NYSE early-close handling for A18 and killzone

    def stream_bars(self, symbols: list[str]) -> AsyncIterator[Bar]: ...
        # WS bar stream for Phase 4 live runner
```

## Location

- **Protocol definition:** `src/nasdaq_ale_bot/execution/broker.py` (Phase 2)
- **Alpaca implementation:** `src/nasdaq_ale_bot/execution/alpaca_client.py` (Phase 4)

## Key Design Decisions

**Why 9 methods, not fewer?**

- `modify_bracket_stop` is separate from `place_bracket` because BE moves happen after entry fill (A17)
- `get_order` enables idempotency checking: on reconnect, check if `client_order_id` already exists before re-submitting (A19)
- `get_trading_calendar` lets killzone and time-exit logic query early-close schedules without importing market calendar libraries directly

**Why `typing.Protocol`, not ABC?**

Structural subtyping -- the Alpaca client doesn't need to inherit from anything. Any class that implements the 9 methods satisfies the protocol. This makes it trivial to add a Tradovate adapter without touching the protocol definition.

**Server-side brackets:**

SL/TP evaluation happens on the broker side via bracket orders. Local code never evaluates stop touches (except BE at 50%-to-TP, which modifies the server-side stop). This means a local crash doesn't leave positions unprotected.
