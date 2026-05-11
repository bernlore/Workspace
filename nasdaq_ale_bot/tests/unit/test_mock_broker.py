"""Unit tests for execution/mock_broker.py — 100% branch coverage required.

Test list (21 tests):
  1.  test_mock_broker_implements_protocol
  2.  test_all_14_methods_callable
  3.  test_place_bracket_queues_order
  4.  test_limit_entry_fills_in_range
  5.  test_limit_entry_gap_fills_at_open
  6.  test_limit_entry_untouched_carries_forward
  7.  test_sl_wins_when_both_hit
  8.  test_tp_fills_when_sl_not_hit
  9.  test_gap_open_past_sl_fills_at_open
  10. test_gap_open_past_tp_fills_at_open
  11. test_entry_and_exit_same_bar
  12. test_no_partial_fills
  13. test_modify_bracket_stop_updates_position
  14. test_modify_bracket_stop_updates_pending
  15. test_modify_bracket_stop_raises_key_error
  16. test_flatten_closes_all_positions
  17. test_cancel_all_removes_pending
  18. test_get_positions_returns_open
  19. test_get_account_equity_matches_ledger
  20. test_get_order_returns_history
  21. test_calendar_stream_are_stubs
  22. test_fill_events_dispatched_to_ledger
  23. test_fill_timestamp_is_bar_ts
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.execution.broker import (
    BROKER_PROTOCOL_METHODS,
    BrokerProtocol,
    OrderRef,
)
from nasdaq_ale_bot.execution.mock_broker import MockBroker

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc
_TODAY = date(2024, 1, 15)
_INIT_EQUITY = Decimal("50000")

_BASE_TS = datetime(2024, 1, 15, 14, 30, tzinfo=UTC)


def _ts(offset_minutes: int = 0) -> datetime:
    return _BASE_TS + timedelta(minutes=offset_minutes)


def _ledger(equity: Decimal = _INIT_EQUITY) -> AccountLedger:
    return AccountLedger(session_start_equity=equity, today=_TODAY)


def _broker(equity: Decimal = _INIT_EQUITY) -> MockBroker:
    return MockBroker(ledger=_ledger(equity), initial_equity=equity)


def _candle(
    open_: float,
    high: float,
    low: float,
    close: float,
    idx: int = 0,
) -> Candle:
    """Build a Candle. high >= max(open, close), low <= min(open, close) required."""
    return Candle(
        ts=_ts(idx),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
    )


def _place(
    broker: MockBroker,
    *,
    symbol: str = "AAPL",
    side: str = "BUY",
    qty: Decimal = Decimal("10"),
    entry: Decimal = Decimal("100"),
    stop: Decimal = Decimal("98"),
    tp: Decimal = Decimal("104"),
    coid: str = "order-1",
) -> OrderRef:
    return broker.place_bracket(
        symbol=symbol,
        side=side,
        qty=qty,
        entry=entry,
        stop=stop,
        take_profit=tp,
        client_order_id=coid,
    )


# ---------------------------------------------------------------------------
# 1. test_mock_broker_implements_protocol
# ---------------------------------------------------------------------------


def test_mock_broker_implements_protocol() -> None:
    broker = _broker()
    assert isinstance(broker, BrokerProtocol)


# ---------------------------------------------------------------------------
# 2. test_all_14_methods_callable
# ---------------------------------------------------------------------------


def test_all_14_methods_callable() -> None:
    broker = _broker()
    for method_name in BROKER_PROTOCOL_METHODS:
        assert hasattr(broker, method_name), f"Missing method: {method_name}"
        assert callable(getattr(broker, method_name))


# ---------------------------------------------------------------------------
# 3. test_place_bracket_queues_order
# ---------------------------------------------------------------------------


def test_place_bracket_queues_order() -> None:
    broker = _broker()
    ref = _place(broker)
    assert isinstance(ref, OrderRef)
    assert ref.client_order_id == "order-1"
    assert len(broker._pending) == 1
    assert broker._pending[0].client_order_id == "order-1"
    order_state = broker.get_order("order-1")
    assert order_state is not None
    assert order_state.status == "NEW"
    assert order_state.filled_qty == Decimal("0")


# ---------------------------------------------------------------------------
# 4. test_limit_entry_fills_in_range  (delta 2: normal touch)
# ---------------------------------------------------------------------------


def test_limit_entry_fills_in_range() -> None:
    """Long LIMIT at 100; bar [99, 101] — fill at 100 (entry_price)."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("98"), tp=Decimal("104"))
    # Bar with low=99, high=101, open=101 — open > entry, low <= entry
    bar = _candle(open_=101.0, high=101.5, low=99.0, close=101.0, idx=1)
    fills = broker.evaluate_fills(bar)
    assert len(fills) == 1
    entry_fill = fills[0]
    assert entry_fill.fill_reason == "ENTRY"
    assert entry_fill.fill_price == Decimal("100")
    assert entry_fill.realized_pnl == Decimal("0")
    assert len(broker._pending) == 0
    assert len(broker._positions) == 1


# ---------------------------------------------------------------------------
# 5. test_limit_entry_gap_fills_at_open  (delta 2: gap-down through limit)
# ---------------------------------------------------------------------------


def test_limit_entry_gap_fills_at_open() -> None:
    """Long LIMIT at 100; bar.open=98 (gap-down past entry) — fill at 98."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("95"), tp=Decimal("106"))
    # Gap-down: open=98 < entry=100; bar must be valid: high >= max(open,close)
    bar = _candle(open_=98.0, high=99.0, low=97.0, close=98.5, idx=1)
    fills = broker.evaluate_fills(bar)
    assert len(fills) == 1
    assert fills[0].fill_reason == "ENTRY"
    assert fills[0].fill_price == Decimal("98")


# ---------------------------------------------------------------------------
# 6. test_limit_entry_untouched_carries_forward  (delta 2: no touch)
# ---------------------------------------------------------------------------


def test_limit_entry_untouched_carries_forward() -> None:
    """Long LIMIT at 100; bar [102, 105] — not filled, stays in pending."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("98"), tp=Decimal("104"))
    bar = _candle(open_=103.0, high=105.0, low=102.0, close=103.5, idx=1)
    fills = broker.evaluate_fills(bar)
    assert fills == []
    assert len(broker._pending) == 1


# ---------------------------------------------------------------------------
# 7. test_sl_wins_when_both_hit  (A22 — SL wins collision)
# ---------------------------------------------------------------------------


def test_sl_wins_when_both_hit() -> None:
    """Bar contains both SL and TP range; position must close at SL."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("103"))

    # Bar 1: entry fills normally
    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    fills1 = broker.evaluate_fills(bar1)
    assert any(f.fill_reason == "ENTRY" for f in fills1)

    # Bar 2: bar goes both below SL (97) and above TP (103) — SL wins
    # open=101, low=96 (hits SL=97), high=104 (hits TP=103)
    bar2 = _candle(open_=101.0, high=104.0, low=96.0, close=100.0, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "STOP_OUT"
    assert fills2[0].fill_price == Decimal("97")


# ---------------------------------------------------------------------------
# 8. test_tp_fills_when_sl_not_hit
# ---------------------------------------------------------------------------


def test_tp_fills_when_sl_not_hit() -> None:
    """Bar touches TP only; position closed at take_profit_price."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("103"))

    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)

    # Bar 2: low=99 (above SL=97), high=104 (hits TP=103)
    bar2 = _candle(open_=101.0, high=104.0, low=99.0, close=102.0, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "TAKE_PROFIT"
    assert fills2[0].fill_price == Decimal("103")


# ---------------------------------------------------------------------------
# 9. test_gap_open_past_sl_fills_at_open
# ---------------------------------------------------------------------------


def test_gap_open_past_sl_fills_at_open() -> None:
    """Long position; bar.open gaps below SL — fill at bar.open, not SL price."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)

    # Gap down: open=95 < SL=97
    bar2 = _candle(open_=95.0, high=96.5, low=94.0, close=95.5, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "STOP_OUT"
    assert fills2[0].fill_price == Decimal("95")


# ---------------------------------------------------------------------------
# 10. test_gap_open_past_tp_fills_at_open
# ---------------------------------------------------------------------------


def test_gap_open_past_tp_fills_at_open() -> None:
    """Long position; bar.open gaps above TP — fill at bar.open, not TP price."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)

    # Gap up: open=106 > TP=104
    bar2 = _candle(open_=106.0, high=107.0, low=105.0, close=106.5, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "TAKE_PROFIT"
    assert fills2[0].fill_price == Decimal("106")


# ---------------------------------------------------------------------------
# 11. test_entry_and_exit_same_bar
# ---------------------------------------------------------------------------


def test_entry_and_exit_same_bar() -> None:
    """Entry fills on bar N; SL also hit on same bar N — both should occur."""
    broker = _broker()
    # Long LIMIT at 100, SL at 97, TP at 104
    # Bar: open=98 (gap fills entry at 98), low=94 (SL at 97 also triggered)
    # high >= max(open=98, close) and low=94 is valid
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    bar = _candle(open_=98.0, high=99.0, low=94.0, close=95.0, idx=1)
    fills = broker.evaluate_fills(bar)
    # Expect entry fill + SL fill on same bar
    reasons = {f.fill_reason for f in fills}
    assert "ENTRY" in reasons
    assert "STOP_OUT" in reasons
    assert len(broker._positions) == 0  # position opened and immediately closed


# ---------------------------------------------------------------------------
# 12. test_no_partial_fills
# ---------------------------------------------------------------------------


def test_no_partial_fills() -> None:
    """filled_qty always equals order qty — no partial fills in MockBroker."""
    broker = _broker()
    qty = Decimal("7")
    _place(broker, qty=qty, entry=Decimal("100"), stop=Decimal("98"), tp=Decimal("104"))

    bar = _candle(open_=101.0, high=102.0, low=99.0, close=101.0, idx=1)
    fills = broker.evaluate_fills(bar)
    assert len(fills) == 1
    assert fills[0].qty == qty


# ---------------------------------------------------------------------------
# 13. test_modify_bracket_stop_updates_position
# ---------------------------------------------------------------------------


def test_modify_bracket_stop_updates_position() -> None:
    """modify_bracket_stop updates stop_price on open position."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    # Fill entry
    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)
    assert len(broker._positions) == 1

    broker.modify_bracket_stop("order-1", Decimal("98.50"))
    pos = broker._positions["order-1"]
    assert pos.stop_price == Decimal("98.50")


# ---------------------------------------------------------------------------
# 14. test_modify_bracket_stop_updates_pending
# ---------------------------------------------------------------------------


def test_modify_bracket_stop_updates_pending() -> None:
    """modify_bracket_stop updates stop_price on a pending (unfilled) order."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))
    assert len(broker._pending) == 1

    broker.modify_bracket_stop("order-1", Decimal("96.00"))
    assert broker._pending[0].stop_price == Decimal("96.00")


# ---------------------------------------------------------------------------
# 15. test_modify_bracket_stop_raises_key_error
# ---------------------------------------------------------------------------


def test_modify_bracket_stop_raises_key_error() -> None:
    """modify_bracket_stop raises KeyError for unknown order_id (no pending, no position)."""
    broker = _broker()
    with pytest.raises(KeyError):
        broker.modify_bracket_stop("nonexistent", Decimal("99"))


def test_modify_bracket_stop_raises_key_error_with_pending() -> None:
    """modify_bracket_stop raises KeyError when pending exist but none match."""
    broker = _broker()
    _place(broker, coid="order-1")  # pending order with different id
    with pytest.raises(KeyError):
        broker.modify_bracket_stop("nonexistent", Decimal("99"))


# ---------------------------------------------------------------------------
# 16. test_flatten_closes_all_positions
# ---------------------------------------------------------------------------


def test_flatten_closes_all_positions() -> None:
    """flatten() then evaluate_fills closes position at bar.close."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    # Fill entry
    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)
    assert len(broker._positions) == 1

    broker.flatten()

    bar2 = _candle(open_=101.0, high=102.0, low=100.5, close=102.0, idx=2)
    fills = broker.evaluate_fills(bar2)
    assert any(f.fill_reason == "FLATTEN" for f in fills)
    assert len(broker._positions) == 0
    # Flatten fill uses bar.close
    flatten_fill = next(f for f in fills if f.fill_reason == "FLATTEN")
    assert flatten_fill.fill_price == Decimal("102")


# ---------------------------------------------------------------------------
# 17. test_cancel_all_removes_pending
# ---------------------------------------------------------------------------


def test_cancel_all_removes_pending() -> None:
    """cancel_all() clears pending brackets and marks them CANCELED."""
    broker = _broker()
    _place(broker, coid="order-1")
    _place(broker, coid="order-2")
    assert len(broker._pending) == 2

    broker.cancel_all()
    assert len(broker._pending) == 0
    assert broker.get_order("order-1").status == "CANCELED"  # type: ignore[union-attr]
    assert broker.get_order("order-2").status == "CANCELED"  # type: ignore[union-attr]


def test_cancel_all_by_symbol() -> None:
    """cancel_all(symbol) only removes pending brackets for that symbol."""
    broker = _broker()
    _place(broker, symbol="AAPL", coid="order-1")
    _place(broker, symbol="MSFT", coid="order-2")

    broker.cancel_all("AAPL")
    assert len(broker._pending) == 1
    assert broker._pending[0].client_order_id == "order-2"
    assert broker.get_order("order-1").status == "CANCELED"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 18. test_get_positions_returns_open
# ---------------------------------------------------------------------------


def test_get_positions_returns_open() -> None:
    """get_positions() reflects current open positions."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)

    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == Decimal("10")
    assert positions[0].avg_entry_price == Decimal("100")


# ---------------------------------------------------------------------------
# 19. test_get_account_equity_matches_ledger
# ---------------------------------------------------------------------------


def test_get_account_equity_matches_ledger() -> None:
    """Equity tracks initial + realized PnL after fills."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    # Entry fill (no PnL change)
    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)
    assert broker.get_account_equity() == _INIT_EQUITY

    # TP hit: gross PnL = 10 * (104 - 100) = 40; commission = 10 * 4.50 = 45;
    # net PnL = -5 → equity = INIT - 5.
    bar2 = _candle(open_=101.0, high=105.0, low=100.5, close=104.5, idx=2)
    broker.evaluate_fills(bar2)
    expected = _INIT_EQUITY + Decimal("40") - Decimal("45")
    assert broker.get_account_equity() == expected
    assert broker.get_realtime_equity() == expected


# ---------------------------------------------------------------------------
# 20. test_get_order_returns_history
# ---------------------------------------------------------------------------


def test_get_order_returns_history() -> None:
    """get_order returns NEW after place_bracket; None for unknown id."""
    broker = _broker()
    _place(broker)
    state = broker.get_order("order-1")
    assert state is not None
    assert state.status == "NEW"
    assert broker.get_order("nonexistent") is None


# ---------------------------------------------------------------------------
# 21. test_calendar_stream_are_stubs
# ---------------------------------------------------------------------------


def test_calendar_stream_are_stubs() -> None:
    """get_trading_calendar returns is_open=True; stream_bars and get_contract_spec raise."""
    broker = _broker()
    cal = broker.get_trading_calendar(date(2024, 1, 15))
    assert cal.is_open is True
    assert cal.rth_open_utc is None
    assert cal.rth_close_utc is None

    with pytest.raises(NotImplementedError):
        broker.stream_bars(["AAPL"])

    with pytest.raises(NotImplementedError):
        broker.get_contract_spec("AAPL")

    # assert_market_open is a no-op (should not raise)
    broker.assert_market_open(datetime.now(UTC))

    # submit_market_flatten returns an OrderRef dummy
    ref = broker.submit_market_flatten("AAPL")
    assert isinstance(ref, OrderRef)
    assert "flatten" in ref.client_order_id.lower()


# ---------------------------------------------------------------------------
# 22. test_fill_events_dispatched_to_ledger
# ---------------------------------------------------------------------------


def test_fill_events_dispatched_to_ledger() -> None:
    """After a winning trade, ledger.realized_today reflects PnL."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"), qty=Decimal("5"))

    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)

    # TP hit: gross PnL = 5 * (104 - 100) = 20; commission = 5 * 4.50 = 22.50;
    # session_pnl = 20 - 22.50 = -2.50.
    bar2 = _candle(open_=101.0, high=105.0, low=100.5, close=104.5, idx=2)
    broker.evaluate_fills(bar2)

    assert broker.get_session_pnl() == Decimal("-2.50")


# ---------------------------------------------------------------------------
# 23. test_fill_timestamp_is_bar_ts
# ---------------------------------------------------------------------------


def test_fill_timestamp_is_bar_ts() -> None:
    """fill.fill_ts == bar.ts for every fill event."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    bar = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=3)
    fills = broker.evaluate_fills(bar)
    assert len(fills) == 1
    assert fills[0].fill_ts == bar.ts


# ---------------------------------------------------------------------------
# Short LIMIT entry path tests (branch coverage)
# ---------------------------------------------------------------------------


def test_short_limit_entry_fills_in_range() -> None:
    """Short LIMIT at 105; bar.high >= 105 but open < 105 — fill at 105."""
    broker = _broker()
    # Short: entry=105, stop=107 (above entry), tp=101 (below entry)
    _place(
        broker,
        side="SELL",
        entry=Decimal("105"),
        stop=Decimal("107"),
        tp=Decimal("101"),
    )
    # open=103 (below entry), high=106 (touches entry from below on up-move)
    bar = _candle(open_=103.0, high=106.0, low=102.5, close=104.0, idx=1)
    fills = broker.evaluate_fills(bar)
    assert len(fills) == 1
    assert fills[0].fill_reason == "ENTRY"
    assert fills[0].fill_price == Decimal("105")


def test_short_limit_entry_gap_fills_at_open() -> None:
    """Short LIMIT at 105; bar.open=108 (gap-up) — fill at bar.open=108."""
    broker = _broker()
    # Stop at 115 (well above bar.high=109) to avoid same-bar SL trigger
    _place(
        broker,
        side="SELL",
        entry=Decimal("105"),
        stop=Decimal("115"),
        tp=Decimal("101"),
    )
    # Gap up: open=108 >= entry=105; stop=115 above bar.high=109 — no exit this bar
    bar = _candle(open_=108.0, high=109.0, low=107.0, close=108.0, idx=1)
    fills = broker.evaluate_fills(bar)
    assert len(fills) == 1
    assert fills[0].fill_reason == "ENTRY"
    assert fills[0].fill_price == Decimal("108")


def test_short_limit_entry_untouched() -> None:
    """Short LIMIT at 105; bar [98, 103] — not filled."""
    broker = _broker()
    _place(
        broker,
        side="SELL",
        entry=Decimal("105"),
        stop=Decimal("108"),
        tp=Decimal("101"),
    )
    bar = _candle(open_=100.0, high=103.0, low=99.0, close=101.0, idx=1)
    fills = broker.evaluate_fills(bar)
    assert fills == []
    assert len(broker._pending) == 1


def test_short_sl_wins_collision() -> None:
    """Short position: bar hits both SL (above) and TP (below) — SL wins."""
    broker = _broker()
    _place(
        broker,
        side="SELL",
        entry=Decimal("105"),
        stop=Decimal("108"),
        tp=Decimal("101"),
    )
    # Fill entry
    bar1 = _candle(open_=103.0, high=106.0, low=102.5, close=104.0, idx=1)
    broker.evaluate_fills(bar1)

    # Both TP (101) and SL (108) in range — SL wins (open=104, high=109 >= 108, low=100 <= 101)
    bar2 = _candle(open_=104.0, high=109.0, low=100.0, close=104.0, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "STOP_OUT"
    assert fills2[0].fill_price == Decimal("108")


def test_short_tp_fills_when_sl_not_hit() -> None:
    """Short position: bar hits TP only."""
    broker = _broker()
    _place(
        broker,
        side="SELL",
        entry=Decimal("105"),
        stop=Decimal("108"),
        tp=Decimal("101"),
    )
    bar1 = _candle(open_=103.0, high=106.0, low=102.5, close=104.0, idx=1)
    broker.evaluate_fills(bar1)

    # TP=101 hit, SL=108 not hit (high=106 < 108)
    bar2 = _candle(open_=104.0, high=106.0, low=100.0, close=102.0, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "TAKE_PROFIT"
    assert fills2[0].fill_price == Decimal("101")


def test_short_gap_sl_fills_at_open() -> None:
    """Short position; bar.open gaps above SL — fill at bar.open."""
    broker = _broker()
    _place(
        broker,
        side="SELL",
        entry=Decimal("105"),
        stop=Decimal("108"),
        tp=Decimal("101"),
    )
    bar1 = _candle(open_=103.0, high=106.0, low=102.5, close=104.0, idx=1)
    broker.evaluate_fills(bar1)

    # Gap up past SL: open=110 > SL=108
    bar2 = _candle(open_=110.0, high=111.0, low=109.0, close=110.0, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "STOP_OUT"
    assert fills2[0].fill_price == Decimal("110")


def test_short_gap_tp_fills_at_open() -> None:
    """Short position; bar.open gaps below TP — fill at bar.open."""
    broker = _broker()
    _place(
        broker,
        side="SELL",
        entry=Decimal("105"),
        stop=Decimal("108"),
        tp=Decimal("101"),
    )
    bar1 = _candle(open_=103.0, high=106.0, low=102.5, close=104.0, idx=1)
    broker.evaluate_fills(bar1)

    # Gap down past TP: open=98 < TP=101
    bar2 = _candle(open_=98.0, high=99.5, low=97.0, close=98.5, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "TAKE_PROFIT"
    assert fills2[0].fill_price == Decimal("98")


def test_flatten_by_symbol() -> None:
    """flatten(symbol) only closes positions for that symbol."""
    broker = _broker()
    # Use same price range for both symbols so a single bar fills both entries.
    # Stops far away (below bar range) so no SL fires on the entry bar.
    _place(broker, symbol="AAPL", coid="order-1", entry=Decimal("100"), stop=Decimal("50"), tp=Decimal("200"))
    _place(broker, symbol="MSFT", coid="order-2", entry=Decimal("100"), stop=Decimal("50"), tp=Decimal("200"))

    # Both entries fill: open=101 > entry=100, low=99 <= entry=100
    bar_fill = Candle(
        ts=_ts(1),
        open=101.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=1000.0,
    )
    broker.evaluate_fills(bar_fill)
    assert len(broker._positions) == 2

    broker.flatten("AAPL")
    bar2 = _candle(open_=101.0, high=102.0, low=100.5, close=102.0, idx=2)
    fills = broker.evaluate_fills(bar2)
    flatten_fills = [f for f in fills if f.fill_reason == "FLATTEN"]
    assert len(flatten_fills) == 1
    assert flatten_fills[0].symbol == "AAPL"
    # MSFT still open
    assert any(p.symbol == "MSFT" for p in broker._positions.values())


def test_order_filled_status_after_entry() -> None:
    """get_order returns FILLED after entry fill."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    bar = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar)

    state = broker.get_order("order-1")
    assert state is not None
    assert state.status == "FILLED"
    assert state.filled_qty == Decimal("10")
    assert state.avg_fill_price == Decimal("100")


def test_long_sl_fills_at_sl_price() -> None:
    """Long position: bar.low hits SL exactly — fill at SL price (not gap)."""
    broker = _broker()
    _place(broker, entry=Decimal("100"), stop=Decimal("97"), tp=Decimal("104"))

    bar1 = _candle(open_=101.0, high=102.0, low=99.5, close=101.0, idx=1)
    broker.evaluate_fills(bar1)

    # open=100 > SL=97, low=97 == SL=97 → fill at 97
    bar2 = _candle(open_=100.0, high=100.5, low=97.0, close=99.0, idx=2)
    fills2 = broker.evaluate_fills(bar2)
    assert len(fills2) == 1
    assert fills2[0].fill_reason == "STOP_OUT"
    assert fills2[0].fill_price == Decimal("97")
    # gross PnL: 10 * (97 - 100) = -30; commission = 10 * 4.50 = 45;
    # net pnl = -75.
    assert fills2[0].realized_pnl == Decimal("-75.00")
