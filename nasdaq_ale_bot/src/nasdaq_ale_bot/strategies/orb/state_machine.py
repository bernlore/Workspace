"""ORB state machine — 5-state Opening Range Breakout engine for NQ.

States::

    SESSION_CLOSED -> OR_FORMING -> WAITING_FOR_BREAKOUT -> IN_TRADE -> DAY_DONE
           ^                                                              |
           +--------------------------- next 09:30 ET --------------------+

The machine owns a :class:`MockBroker` and an :class:`AccountLedger`: it
places the entry, lets the broker auto-manage the bracket (SL/TP), force-flats
at 15:45 ET, and records completed trades on ``self.trades``.

Look-ahead contract (spec §9.3): the opening range freezes before any
breakout check (enforced by :class:`OpeningRange` + :class:`BreakoutDetector`).
The 5-minute confirmation bar is fully closed when the signal fires; entry
executes on the OPEN of the NEXT 1-minute bar — never the confirmation bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.execution.cost_model import CostModel
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.strategies.orb.breakout_detector import (
    BreakoutDetector,
    BreakoutSignal,
)
from nasdaq_ale_bot.strategies.orb.opening_range import ORState, OpeningRange

NY = ZoneInfo("America/New_York")


class OrbState(StrEnum):
    SESSION_CLOSED = "SESSION_CLOSED"
    OR_FORMING = "OR_FORMING"
    WAITING_FOR_BREAKOUT = "WAITING_FOR_BREAKOUT"
    IN_TRADE = "IN_TRADE"
    DAY_DONE = "DAY_DONE"


@dataclass
class OrbTrade:
    """One completed ORB trade (or one in progress until exit fields fill)."""

    session_date: date
    direction: str               # "LONG" | "SHORT"
    signal_ts: datetime          # ts of the confirming 5-min bar
    entry_ts: datetime
    entry_price: float           # actual fill (slippage included)
    planned_entry_price: float   # pre-slippage entry the stop/target were sized on
    stop_price: float
    target_price: float
    qty: int
    client_order_id: str
    or_high: float
    or_low: float
    or_range: float
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    net_pnl: float = 0.0          # broker realized pnl (net of commission + slippage)
    gross_pnl: float = 0.0        # net + round-trip cost


def _parse_time(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def compute_stop_target(
    *,
    direction: str,
    entry_price: float,
    or_high: float,
    or_low: float,
    buffer: float,
    max_stop_points: float,
    rr_multiple: float,
) -> tuple[float, float, float]:
    """Pure stop/target placement.

    Returns ``(stop_price, stop_distance_points, target_price)``.

    Stop sits at the **OR midpoint** ``(or_high + or_low) / 2`` with a buffer
    (long: mid - buffer; short: mid + buffer), capped at ``max_stop_points``
    from entry.

    Target is derived from the *actual* stop distance:
    ``target_distance = stop_distance * rr_multiple``. This guarantees a
    constant reward:risk of exactly ``1:rr_multiple`` on every trade,
    independent of the opening-range size or breakout extension. The stop
    distance used is the post-cap value, so a capped stop also yields a
    proportionally capped target.
    """
    or_mid = (or_high + or_low) / 2.0
    if direction == "LONG":
        raw_stop = or_mid - buffer
        stop_dist = entry_price - raw_stop
        if stop_dist > max_stop_points:
            stop_price = entry_price - max_stop_points
            stop_dist = max_stop_points
        else:
            stop_price = raw_stop
        target_price = entry_price + stop_dist * rr_multiple
    else:  # SHORT
        raw_stop = or_mid + buffer
        stop_dist = raw_stop - entry_price
        if stop_dist > max_stop_points:
            stop_price = entry_price + max_stop_points
            stop_dist = max_stop_points
        else:
            stop_price = raw_stop
        target_price = entry_price - stop_dist * rr_multiple
    return stop_price, stop_dist, target_price


def compute_position_size(
    *,
    risk_budget: float,
    stop_distance_points: float,
    point_value: float,
    min_contracts: int,
    max_contracts: int,
) -> int | None:
    """Pure position sizing — ``floor(risk / (stop_pts * point_value))``.

    Returns the contract count, clamped to ``max_contracts``. Returns ``None``
    when even one contract would exceed the risk budget (caller skips the
    trade).
    """
    if stop_distance_points <= 0:
        return None
    raw = math.floor(risk_budget / (stop_distance_points * point_value))
    if raw < min_contracts:
        return None
    return min(raw, max_contracts)


class OrbStateMachine:
    """Drives one NQ instrument through the ORB lifecycle, bar by bar."""

    def __init__(
        self,
        *,
        config: dict,
        broker: MockBroker,
        ledger: AccountLedger,
        tick_size: float,
        point_value: float,
        cost_model: CostModel | None = None,
        symbol: str = "NQ",
    ) -> None:
        self._broker = broker
        self._ledger = ledger
        self._tick_size = tick_size
        self._point_value = point_value
        self._cost_model = cost_model
        self._symbol = symbol

        # --- parsed config ---
        orw = config["opening_range"]
        tw = config["trading_window"]
        flt = config["filters"]
        entry = config["entry"]
        sl = config["stop_loss"]
        tp = config["take_profit"]
        risk = config["risk"]
        self._or_start = _parse_time(orw["start_time_et"])
        self._or_duration = int(orw["duration_minutes"])
        self._entry_window_end = _parse_time(tw["entry_window_end_et"])
        self._force_flat_time = _parse_time(tw["force_flat_time_et"])
        self._min_or = float(flt["min_or_size_points"])
        self._max_or = float(flt["max_or_size_points"])
        self._min_break_dist = int(entry["min_breakout_distance_ticks"]) * tick_size
        self._require_solid_body = bool(entry["require_solid_body"])
        self._buffer = int(sl["buffer_ticks"]) * tick_size
        self._max_stop_points = float(sl["max_stop_points"])
        self._rr_multiple = float(tp["rr_multiple"])
        self._risk_budget = float(risk["risk_per_trade_usd"])
        self._max_contracts = int(risk["max_contracts"])
        self._min_contracts = int(risk["min_contracts"])

        # --- runtime state ---
        self.state: OrbState = OrbState.SESSION_CLOSED
        self.trades: list[OrbTrade] = []
        self._session_date: date | None = None
        self._or: OpeningRange | None = None
        self._detector: BreakoutDetector | None = None
        self._pending_signal: BreakoutSignal | None = None
        self._signal_bar_ts: datetime | None = None
        self._active_trade: OrbTrade | None = None

        # --- diagnostics counters ---
        self.days_or_valid = 0       # OR passed the size filter
        self.days_with_signal = 0    # a breakout signal fired
        self.days_with_trade = 0     # an entry was actually placed
        self.days_skipped_size = 0   # OR rejected by size filter
        self.days_skipped_invalid = 0  # OR incomplete (bad data)
        self.days_skipped_sizing = 0  # signal fired but 1 contract over budget

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> None:
        """Process one 1-minute NQ bar."""
        ny = bar.ts.astimezone(NY)
        if self._session_date != ny.date():
            self._rotate_session(ny.date())

        # Force-flat: schedule the close BEFORE evaluate_fills so the position
        # is flattened at THIS bar's close (spec: "close at 15:45 bar close").
        if (
            self.state == OrbState.IN_TRADE
            and self._active_trade is not None
            and self._active_trade.exit_reason is None
            and ny.time() >= self._force_flat_time
        ):
            self._broker.flatten()

        # Settle prior-bar orders and check SL/TP on any open position.
        for fill in self._broker.evaluate_fills(bar):
            if fill.fill_reason != "ENTRY":
                self._on_exit_fill(fill)

        # Dispatch — a short loop lets OR_FORMING chain into
        # WAITING_FOR_BREAKOUT within the single freezing bar.
        for _ in range(4):
            prev = self.state
            self._dispatch(bar, ny)
            if self.state == prev:
                break

    # ------------------------------------------------------------------
    # Session rotation
    # ------------------------------------------------------------------

    def _rotate_session(self, new_date: date) -> None:
        self._session_date = new_date
        self.state = OrbState.SESSION_CLOSED
        self._or = None
        self._detector = None
        self._pending_signal = None
        self._signal_bar_ts = None
        self._active_trade = None  # clean data force-flats before EOD

    # ------------------------------------------------------------------
    # State dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, bar: Candle, ny: datetime) -> None:
        if self.state == OrbState.SESSION_CLOSED:
            self._do_session_closed(bar, ny)
        elif self.state == OrbState.OR_FORMING:
            self._do_or_forming(bar, ny)
        elif self.state == OrbState.WAITING_FOR_BREAKOUT:
            self._do_waiting(bar, ny)
        elif self.state == OrbState.IN_TRADE:
            self._do_in_trade(bar, ny)
        # DAY_DONE: nothing until next session.

    def _do_session_closed(self, bar: Candle, ny: datetime) -> None:
        if ny.time() >= self._or_start:
            self._or = OpeningRange(
                start_et=self._or_start, duration_minutes=self._or_duration
            )
            self._detector = BreakoutDetector(
                min_breakout_distance=self._min_break_dist,
                require_solid_body=self._require_solid_body,
            )
            self.state = OrbState.OR_FORMING
        # else: pre-09:30 bar — stay closed.

    def _do_or_forming(self, bar: Candle, ny: datetime) -> None:
        assert self._or is not None
        self._or.offer(bar)
        if self._or.state == ORState.FORMING:
            return  # still accumulating
        if self._or.state == ORState.INVALID:
            self.days_skipped_invalid += 1
            self.state = OrbState.DAY_DONE
            return
        # FROZEN — apply OR-size filter.
        assert self._or.range is not None
        if not (self._min_or <= self._or.range <= self._max_or):
            self.days_skipped_size += 1
            self.state = OrbState.DAY_DONE
            return
        self.days_or_valid += 1
        self.state = OrbState.WAITING_FOR_BREAKOUT
        # The freezing bar is the first bar of the trading window — the
        # dispatch loop re-runs and feeds it to the breakout detector.

    def _do_waiting(self, bar: Candle, ny: datetime) -> None:
        assert self._detector is not None and self._or is not None
        if ny.time() >= self._entry_window_end:
            self.state = OrbState.DAY_DONE  # 12:00 reached, no signal
            return
        signal = self._detector.on_bar(bar, self._or)
        if signal is not None:
            self._pending_signal = signal
            self._signal_bar_ts = bar.ts
            self.days_with_signal += 1
            self.state = OrbState.IN_TRADE

    def _do_in_trade(self, bar: Candle, ny: datetime) -> None:
        # Execute the pending entry on the first bar AFTER the signal bar.
        if (
            self._pending_signal is not None
            and self._active_trade is None
            and self._signal_bar_ts is not None
            and bar.ts > self._signal_bar_ts
        ):
            self._execute_entry(bar)
        # Otherwise: position open — exits are handled by evaluate_fills.

    # ------------------------------------------------------------------
    # Entry / exit
    # ------------------------------------------------------------------

    def _execute_entry(self, bar: Candle) -> None:
        assert self._pending_signal is not None and self._or is not None
        assert self._or.high is not None and self._or.low is not None
        assert self._or.range is not None and self._session_date is not None
        sig = self._pending_signal
        entry_price = float(bar.open)
        side = "BUY" if sig.direction.value == "LONG" else "SELL"

        stop_price, stop_dist, target_price = compute_stop_target(
            direction=sig.direction.value,
            entry_price=entry_price,
            or_high=self._or.high,
            or_low=self._or.low,
            buffer=self._buffer,
            max_stop_points=self._max_stop_points,
            rr_multiple=self._rr_multiple,
        )
        qty = compute_position_size(
            risk_budget=self._risk_budget,
            stop_distance_points=stop_dist,
            point_value=self._point_value,
            min_contracts=self._min_contracts,
            max_contracts=self._max_contracts,
        )
        if qty is None:
            # Cannot afford even one contract within the risk budget — skip.
            self.days_skipped_sizing += 1
            self._pending_signal = None
            self.state = OrbState.DAY_DONE
            return

        coid = f"ORB-{self._session_date.isoformat()}-{sig.direction.value}"
        entry_fill = self._broker.place_immediate(
            symbol=self._symbol,
            side=side,
            qty=Decimal(qty),
            fill_price=Decimal(str(entry_price)),
            stop=Decimal(str(stop_price)),
            take_profit=Decimal(str(target_price)),
            client_order_id=coid,
            fill_ts=bar.ts,
        )
        self._active_trade = OrbTrade(
            session_date=self._session_date,
            direction=sig.direction.value,
            signal_ts=sig.confirmation_ts,
            entry_ts=bar.ts,
            entry_price=float(entry_fill.fill_price),
            planned_entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            qty=qty,
            client_order_id=coid,
            or_high=self._or.high,
            or_low=self._or.low,
            or_range=self._or.range,
        )
        self._pending_signal = None
        self.days_with_trade += 1
        if hasattr(self._ledger, "increment_trades_today"):
            self._ledger.increment_trades_today()

    def _on_exit_fill(self, fill) -> None:  # noqa: ANN001 — FillEvent
        if (
            self._active_trade is None
            or fill.client_order_id != self._active_trade.client_order_id
        ):
            return
        t = self._active_trade
        t.exit_ts = fill.fill_ts
        t.exit_price = float(fill.fill_price)
        t.exit_reason = fill.fill_reason
        t.net_pnl = float(fill.realized_pnl)
        t.gross_pnl = t.net_pnl + self._round_trip_cost(t.qty)
        self.trades.append(t)
        self._active_trade = None
        self.state = OrbState.DAY_DONE

    def _round_trip_cost(self, qty: int) -> float:
        """Total commission + slippage cost for the round trip (for gross calc)."""
        if self._cost_model is None:
            return 4.50 * qty
        commission = float(self._cost_model.commission_round_trip)
        slippage = (
            2
            * self._cost_model.slippage_ticks_per_side
            * float(self._cost_model.tick_value_usd)
        )
        return (commission + slippage) * qty
