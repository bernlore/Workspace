"""Strategy state machine — Phase 2 engine core.

Threading contract (§A21):
    The state machine is single-threaded.  The live runner's WS thread
    enqueues bars onto a ``queue.Queue``; a dedicated engine thread is
    the sole caller of :meth:`on_bar`.  The state machine itself never
    spawns threads, never holds locks.

    A4 amendment: the single-threaded contract is ENFORCED AT RUNTIME.
    ``__init__`` captures ``threading.get_ident()``; ``on_bar`` verifies
    on every call and raises :class:`ThreadingContractViolation` on
    mismatch.  Docstring-only contracts rot; runtime checks don't.

Session rotation (§A16):
    Daily counters (``_am_order_placed``, ``_trades_today``, ledger
    ``realized_today``) reset at 00:00 America/New_York.  Rotation is
    idempotent: feeding a bar whose date equals ``_session_date`` is a
    no-op.

Re-entry deduplication (R6):
    A bar whose ``ts`` is <= ``_last_bar_ts`` is dropped — replay safety.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog

from nasdaq_ale_bot.detection.cisd import (
    detect_bearish_cisd,
    detect_bullish_cisd,
)
from nasdaq_ale_bot.detection.equilibrium import is_in_discount, is_in_premium
from nasdaq_ale_bot.detection.ifvg import CISDRange, detect_ifvg
from nasdaq_ale_bot.detection.sweep import detect_sweep
from nasdaq_ale_bot.filters.killzone import (
    in_primary_killzone,
    in_secondary_killzone,
)

from .candle import Candle
from .candle_view import CandleView
from .leg import Direction, Leg
from .liquidity_tracker import LiquidityTracker

_ET = ZoneInfo("America/New_York")


class ThreadingContractViolation(RuntimeError):
    """A4 amendment — raised when ``on_bar`` is invoked from a thread
    other than the one that constructed the :class:`StateMachine`."""


# ---------------------------------------------------------------------------
# State + Event types
# ---------------------------------------------------------------------------

from enum import StrEnum  # noqa: E402


class StrategyState(StrEnum):
    BIAS_DETERMINATION = "BIAS_DETERMINATION"
    WAITING_FOR_SWEEP = "WAITING_FOR_SWEEP"
    CISD_CONFIRMATION = "CISD_CONFIRMATION"
    IFVG_FORMATION = "IFVG_FORMATION"
    ENTRY_EXECUTION = "ENTRY_EXECUTION"
    TRADE_MANAGEMENT = "TRADE_MANAGEMENT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class StateEvent:
    from_state: StrategyState
    to_state: StrategyState
    reason: str
    bar_ts: datetime


@dataclass
class Setup:
    """Container for an in-flight setup (bias/sweep/cisd/ifvg/entry)."""

    bias: str  # "LONG" | "SHORT"
    sweep_idx: int | None = None
    sweep_direction: Direction | None = None
    sweep_price: float | None = None
    cisd_confirmed: bool = False
    cisd_ref_idx: int | None = None
    cisd_confirm_idx: int | None = None
    # Carry-forward IFVG state — once detected, the zone stays armed until
    # price retests it inside a killzone (entry) or body-closes past the far
    # edge (invalidation) or the NY session rolls over (session_end).
    ifvg_zone_top: float | None = None
    ifvg_zone_bottom: float | None = None
    ifvg_formed_ts: datetime | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    entry_bar_ts: datetime | None = None
    # LIMIT-at-zone-edge: True after broker confirms ENTRY fill. Until then,
    # TRADE_MANAGEMENT only checks zone invalidation and session-end (no
    # stop/tp arithmetic) so the SM does not flatten before the limit fills.
    entry_filled: bool = False


# ---------------------------------------------------------------------------
# Handler protocol — assignable for test injection
# ---------------------------------------------------------------------------

HandlerResult = tuple[StrategyState, str]
Handler = Callable[["StateMachine", CandleView], HandlerResult]


# ---------------------------------------------------------------------------
# StateMachine
# ---------------------------------------------------------------------------


class StateMachine:
    """Single-threaded ICT strategy engine.

    Consumers feed 1m bars via :meth:`on_bar`.  Each bar may produce zero
    or one :class:`StateEvent` (rarely two if a setup completes and
    immediately re-arms in the same bar).
    """

    def __init__(
        self,
        *,
        bias_detector: Any = None,
        smt_tracker: Any = None,
        gate_list: Any = None,
        ledger: Any = None,
        instrument: Any = None,
        strategy_cfg: dict[str, Any] | None = None,
    ) -> None:
        self.state: StrategyState = StrategyState.BIAS_DETERMINATION

        # Collaborators (may be ``None`` in unit tests that stub handlers)
        self._bias_detector = bias_detector
        self._smt_tracker = smt_tracker
        self._gate_list = gate_list
        self._ledger = ledger
        self._instrument = instrument
        self._strategy_cfg: dict[str, Any] = strategy_cfg or {}

        # Session state
        self._session_date: date | None = None
        self._am_order_placed: bool = False
        self._trades_today: int = 0
        self._active_setup: Setup | None = None
        self._bars: list[Candle] = []
        self._last_bar_ts: datetime | None = None

        # Liquidity levels for detect_sweep — PDH/PDL + 3-bar swings.
        self._liq_tracker = LiquidityTracker()

        # Carry-forward IFVG zone book — armed zones not yet entered.
        # Cap: at most one zone per side (LONG / SHORT).
        self._zone_book: list[Setup] = []

        # A4 runtime threading contract
        self._owner_thread_id: int = threading.get_ident()

        # Structured logger
        self._log = structlog.get_logger(__name__)

        # Handlers dispatch table — overridable per instance for tests
        self._handlers: dict[StrategyState, Handler] = {
            StrategyState.BIAS_DETERMINATION: _handle_bias_determination,
            StrategyState.WAITING_FOR_SWEEP: _handle_waiting_for_sweep,
            StrategyState.CISD_CONFIRMATION: _handle_cisd_confirmation,
            StrategyState.IFVG_FORMATION: _handle_ifvg_formation,
            StrategyState.ENTRY_EXECUTION: _handle_entry_execution,
            StrategyState.TRADE_MANAGEMENT: _handle_trade_management,
            StrategyState.FLAT: _handle_flat,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[StateEvent]:
        """Sole engine entry point.  Single-threaded — enforced at runtime."""
        if threading.get_ident() != self._owner_thread_id:
            raise ThreadingContractViolation(
                f"StateMachine.on_bar called from thread "
                f"{threading.get_ident()} but owner is {self._owner_thread_id}"
            )

        # R6 — same or earlier bar is a no-op (replay dedup)
        if self._last_bar_ts is not None and bar.ts <= self._last_bar_ts:
            return []
        self._last_bar_ts = bar.ts

        events: list[StateEvent] = []
        self._bars.append(bar)
        self._liq_tracker.on_bar(bar)
        self._maybe_rotate_session(bar.ts)

        # Carry-forward IFVG zone book — runs BEFORE dispatch so a triggered
        # zone can install the active setup and force ENTRY_EXECUTION.
        self._monitor_zone_book(bar, events)

        view = CandleView(self._bars, len(self._bars) - 1)

        # Dispatch loop — a state may collapse through multiple transitions
        # in a single bar. A sweep bar can chain BIAS→WAIT→CISD→IFVG→ENTRY,
        # and the ENTRY handler must still run in that same bar so the
        # killzone and gate checks fire before the runner arms the bracket.
        # Six hops covers BIAS→WAIT→CISD→IFVG→ENTRY→TRADE_MANAGEMENT/FLAT
        # while stopping before an immediate rearm into a new setup.
        for _ in range(6):
            handler = self._handlers[self.state]
            new_state, reason = handler(self, view)
            if new_state == self.state:
                break
            events.append(self._transition(new_state, reason, bar.ts))
        return events

    # ------------------------------------------------------------------
    # Transition bookkeeping
    # ------------------------------------------------------------------

    def _transition(
        self,
        new_state: StrategyState,
        reason: str,
        bar_ts: datetime,
    ) -> StateEvent:
        event = StateEvent(
            from_state=self.state,
            to_state=new_state,
            reason=reason,
            bar_ts=bar_ts,
        )
        self._log.info(
            "STATE_TRANSITION",
            from_state=str(event.from_state),
            to_state=str(event.to_state),
            reason=reason,
            bar_ts=bar_ts.isoformat(),
        )
        self.state = new_state
        return event

    # ------------------------------------------------------------------
    # Session rotation
    # ------------------------------------------------------------------

    def _maybe_rotate_session(self, bar_ts: datetime) -> None:
        bar_et_date = bar_ts.astimezone(_ET).date()
        if self._session_date == bar_et_date:
            return
        # First bar or new session — rotate
        prev = self._session_date
        self._session_date = bar_et_date
        self._am_order_placed = False
        self._trades_today = 0
        if self._ledger is not None and hasattr(self._ledger, "on_session_rotation"):
            try:
                equity = getattr(self._ledger, "current_equity", Decimal("0"))
                self._ledger.on_session_rotation(bar_et_date, equity)
            except Exception as exc:  # noqa: BLE001 - defensive
                self._log.warning(
                    "session_rotation_ledger_error",
                    error=str(exc),
                    date=str(bar_et_date),
                )
        if prev is not None:
            self._log.info(
                "SESSION_ROTATION",
                prev_date=str(prev),
                new_date=str(bar_et_date),
                bar_ts=bar_ts.isoformat(),
            )

    # ------------------------------------------------------------------
    # IFVG zone book (carry-forward, concurrent)
    # ------------------------------------------------------------------

    def _add_zone_to_book(self, zone: Setup) -> None:
        """Add an armed IFVG zone to the book; one zone per side, newer wins."""
        # Drop any existing zone of the same side; cap at 2 (one LONG, one SHORT).
        self._zone_book = [z for z in self._zone_book if z.bias != zone.bias]
        self._zone_book.append(zone)

    def _monitor_zone_book(
        self, bar: Candle, events: list[StateEvent]
    ) -> None:
        """Walk the zone book on this bar.

        Outcomes per zone:
          - body-close past far edge → drop, log ``ifvg_invalidated``
          - NY-date rolled since formation → drop, log ``ifvg_session_end``
          - price wick re-enters the zone:
              * outside killzone → keep, log ``retest_outside_killzone``
              * inside  killzone:
                  - SM is in TRADE_MANAGEMENT/ENTRY_EXECUTION → drop (skip per spec)
                  - SM idle and no other zone has triggered yet → install as
                    active setup, compute entry/stop/tp, emit synthetic
                    BIAS_DETERMINATION → ENTRY_EXECUTION transition with
                    reason ``ifvg_ready``.
        """
        if not self._zone_book:
            return

        tol_ticks = int(self._strategy_cfg.get("ifvg_tolerance_ticks", 0))
        tick = float(getattr(self._instrument, "tick", 0.0) or 0.0)
        tol_offset = tol_ticks * tick

        surviving: list[Setup] = []
        triggered: Setup | None = None

        for zone in self._zone_book:
            # 1) invalidation
            if zone.bias == "LONG":
                if zone.ifvg_zone_bottom is not None and bar.close < zone.ifvg_zone_bottom - tol_offset:
                    self._log.info(
                        "ifvg_invalidated",
                        bias=zone.bias,
                        bar_ts=bar.ts.isoformat(),
                    )
                    continue
            else:
                if zone.ifvg_zone_top is not None and bar.close > zone.ifvg_zone_top + tol_offset:
                    self._log.info(
                        "ifvg_invalidated",
                        bias=zone.bias,
                        bar_ts=bar.ts.isoformat(),
                    )
                    continue

            # 2) session-end (NY-date rolled)
            if (
                zone.ifvg_formed_ts is not None
                and bar.ts.astimezone(_ET).date()
                != zone.ifvg_formed_ts.astimezone(_ET).date()
            ):
                self._log.info(
                    "ifvg_session_end",
                    bias=zone.bias,
                    bar_ts=bar.ts.isoformat(),
                )
                continue

            # 3) retest?
            if zone.bias == "LONG":
                retested = (
                    zone.ifvg_zone_top is not None
                    and bar.low <= zone.ifvg_zone_top
                )
            else:
                retested = (
                    zone.ifvg_zone_bottom is not None
                    and bar.high >= zone.ifvg_zone_bottom
                )
            if not retested:
                surviving.append(zone)
                continue

            # 4) killzone gate at retest time
            in_am = in_primary_killzone(bar.ts)
            in_pm = in_secondary_killzone(bar.ts)
            in_kz = in_am or (in_pm and not self._am_order_placed)
            if not in_kz:
                self._log.info(
                    "retest_outside_killzone",
                    bias=zone.bias,
                    bar_ts=bar.ts.isoformat(),
                )
                surviving.append(zone)
                continue

            # 5) trade-active gate — drop the zone per spec
            if self.state in (
                StrategyState.ENTRY_EXECUTION,
                StrategyState.TRADE_MANAGEMENT,
            ):
                self._log.info(
                    "zone_skipped_trade_active",
                    bias=zone.bias,
                    bar_ts=bar.ts.isoformat(),
                )
                continue

            # 6) only one trigger per bar — rest of the matching zones drop
            if triggered is not None:
                continue
            triggered = zone

        self._zone_book = surviving

        if triggered is None:
            return

        # Compute entry/stop/tp from the triggered zone. Entry sits at the
        # zone EDGE (LONG: top, SHORT: bottom) — the runner places a LIMIT
        # there and the broker carries it forward until price wicks back to
        # the level (true ICT retest). Stop is anchored to the IFVG's far
        # edge (structural).
        if triggered.bias == "LONG":
            triggered.entry_price = triggered.ifvg_zone_top
            triggered.stop_price = triggered.ifvg_zone_bottom - tol_offset
        else:
            triggered.entry_price = triggered.ifvg_zone_bottom
            triggered.stop_price = triggered.ifvg_zone_top + tol_offset
        rr_cfg = self._strategy_cfg.get("rr_cap")
        if rr_cfg is None:
            rr_cfg = self._strategy_cfg.get("rr", {}).get("cap", 1.3)
        rr_mult = float(rr_cfg)
        risk = abs(triggered.entry_price - triggered.stop_price)
        if risk <= 0:
            return  # skip — degenerate zone
        if triggered.bias == "LONG":
            triggered.take_profit = triggered.entry_price + risk * rr_mult
        else:
            triggered.take_profit = triggered.entry_price - risk * rr_mult

        # Install as active setup; force a synthetic transition into ENTRY.
        self._active_setup = triggered
        events.append(
            self._transition(
                StrategyState.ENTRY_EXECUTION, "ifvg_ready", bar.ts
            )
        )


# ---------------------------------------------------------------------------
# Default handlers (module-level for serialisability + test override)
# ---------------------------------------------------------------------------


def _handle_bias_determination(
    sm: StateMachine, view: CandleView
) -> HandlerResult:
    """Ask the bias detector; advance to WAITING_FOR_SWEEP on LONG/SHORT."""
    if sm._bias_detector is None:
        return (sm.state, "no_bias_detector")
    bar = view[-1]
    try:
        bias_state = sm._bias_detector.on_1m_bar(bar)
        bias = getattr(bias_state, "bias", None)
        bias_name = str(bias) if bias is not None else "NONE"
    except Exception as exc:  # noqa: BLE001 - defensive; bias is best-effort
        sm._log.warning("bias_detector_error", error=str(exc))
        return (sm.state, "bias_detector_error")

    if bias_name in ("LONG", "SHORT") or bias_name.endswith("LONG") or bias_name.endswith("SHORT"):
        direction = "LONG" if "LONG" in bias_name else "SHORT"
        sm._active_setup = Setup(bias=direction)
        return (StrategyState.WAITING_FOR_SWEEP, f"bias_{direction}")
    return (sm.state, "no_bias")


def _handle_waiting_for_sweep(
    sm: StateMachine, view: CandleView
) -> HandlerResult:
    """Call detect_sweep against liquidity-tracker levels; require bias match."""
    setup = sm._active_setup
    if setup is None:
        return (StrategyState.BIAS_DETERMINATION, "no_setup")
    i = len(view) - 1
    if i < 1:
        return (sm.state, "awaiting_sweep")
    want_direction = Direction.UP if setup.bias == "LONG" else Direction.DOWN
    levels = sm._liq_tracker.current_levels()
    if not levels:
        return (sm.state, "awaiting_sweep")
    tick = float(getattr(sm._instrument, "tick", 0.0) or 0.0) or 0.01
    result = detect_sweep(
        view, i, levels, tick_size=tick, min_penetration_ticks=2
    )
    if not result.swept or result.direction != want_direction:
        return (sm.state, "awaiting_sweep")
    setup.sweep_idx = i
    setup.sweep_direction = result.direction
    setup.sweep_price = result.level.price if result.level is not None else None
    return (StrategyState.CISD_CONFIRMATION, "sweep_detected")


def _handle_cisd_confirmation(
    sm: StateMachine, view: CandleView
) -> HandlerResult:
    setup = sm._active_setup
    if setup is None or setup.sweep_idx is None:
        return (StrategyState.BIAS_DETERMINATION, "no_setup")
    lookback = int(sm._strategy_cfg.get("cisd_lookback_bars", 15))
    i = len(view) - 1
    # Same-bar guard — CISD must confirm strictly after the sweep bar.
    if i <= setup.sweep_idx:
        return (sm.state, "awaiting_cisd")
    if (i - setup.sweep_idx) > lookback:
        return (StrategyState.BIAS_DETERMINATION, "cisd_timeout")
    if setup.bias == "LONG":
        result = detect_bullish_cisd(view, setup.sweep_idx)
        reason_on_confirm = "cisd_bullish"
    else:
        result = detect_bearish_cisd(view, setup.sweep_idx)
        reason_on_confirm = "cisd_bearish"
    if not result.confirmed:
        return (sm.state, "awaiting_cisd")
    setup.cisd_confirmed = True
    setup.cisd_ref_idx = result.ref_idx
    setup.cisd_confirm_idx = result.confirm_idx
    return (StrategyState.IFVG_FORMATION, reason_on_confirm)


def _handle_ifvg_formation(
    sm: StateMachine, view: CandleView
) -> HandlerResult:
    """Detect the IFVG zone, register it on the SM zone book, release the SM.

    The actual retest / invalidation / session-end / entry trigger lives in
    ``StateMachine._monitor_zone_book`` so the SM can immediately go back to
    BIAS_DETERMINATION and process the next setup. Multiple zones can live
    in the book simultaneously (one per side, LONG/SHORT).
    """
    setup = sm._active_setup
    if (
        setup is None
        or setup.sweep_idx is None
        or setup.cisd_confirm_idx is None
    ):
        return (StrategyState.BIAS_DETERMINATION, "no_setup")

    bar = view[-1]
    tol_ticks = int(sm._strategy_cfg.get("ifvg_tolerance_ticks", 0))
    tick = float(getattr(sm._instrument, "tick", 0.0) or 0.0)
    tol_offset = tol_ticks * tick

    direction = Direction.UP if setup.bias == "LONG" else Direction.DOWN
    cisd_range = CISDRange(start=setup.sweep_idx, end=setup.cisd_confirm_idx)
    sweep_bar = view[setup.sweep_idx]
    sweep_price = sweep_bar.low if setup.bias == "LONG" else sweep_bar.high
    try:
        ifvgs = detect_ifvg(
            view,
            setup.cisd_confirm_idx,
            cisd_range,
            sweep_price=sweep_price,
            direction=direction,
            tol_offset=tol_offset,
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        sm._log.warning("ifvg_detect_error", error=str(exc))
        return (StrategyState.BIAS_DETERMINATION, "ifvg_error")
    if not ifvgs:
        return (StrategyState.BIAS_DETERMINATION, "no_ifvg")

    nearest = ifvgs[0]
    setup.ifvg_zone_top = float(nearest.fvg.top)
    setup.ifvg_zone_bottom = float(nearest.fvg.bottom)
    setup.ifvg_formed_ts = bar.ts

    # Register the zone in the book (one per side; newer replaces older).
    sm._add_zone_to_book(setup)
    # Release the SM so new biases / sweeps / CISDs can run on subsequent bars.
    sm._active_setup = None
    return (StrategyState.BIAS_DETERMINATION, "ifvg_zone_armed")


def _handle_entry_execution(
    sm: StateMachine, view: CandleView
) -> HandlerResult:
    setup = sm._active_setup
    if setup is None:
        return (StrategyState.BIAS_DETERMINATION, "no_setup")
    bar = view[-1]
    setup.entry_bar_ts = bar.ts

    # Killzone pre-check (§A9): primary AM window, or PM window if AM trade
    # has not already fired in this session. Runs before gate evaluation.
    in_am = in_primary_killzone(bar.ts)
    in_pm = in_secondary_killzone(bar.ts)
    if not (in_am or (in_pm and not sm._am_order_placed)):
        return (StrategyState.FLAT, "outside_killzone")

    # Fib 0.5 zone filter — long must enter in discount, short in premium.
    # "Swept range" is the pre-sweep dealing range: from the swept extreme
    # back to the last opposite structural pivot over a lookback window.
    if (
        setup.sweep_idx is not None
        and setup.entry_price is not None
    ):
        look = 30
        start = max(0, setup.sweep_idx - look)
        if setup.bias == "LONG":
            lo = float(view[setup.sweep_idx].low)
            hi = max(view[k].high for k in range(start, setup.sweep_idx + 1))
        else:
            hi = float(view[setup.sweep_idx].high)
            lo = min(view[k].low for k in range(start, setup.sweep_idx + 1))
        leg = Leg(
            start_idx=start,
            end_idx=setup.sweep_idx,
            direction=Direction.UP if setup.bias == "LONG" else Direction.DOWN,
            low=lo,
            high=hi,
        )
        entry_px = float(setup.entry_price)
        if setup.bias == "LONG" and not is_in_discount(entry_px, leg):
            return (StrategyState.FLAT, "zone_filter_rejected")
        if setup.bias == "SHORT" and not is_in_premium(entry_px, leg):
            return (StrategyState.FLAT, "zone_filter_rejected")

    # SMT direction filter — reject divergence opposite to bias (A13 fail-open on NONE).
    smt_verdict_raw = "NONE"
    tracker = sm._smt_tracker
    if tracker is not None and hasattr(tracker, "verdict"):
        try:
            smt_verdict_raw = str(tracker.verdict)
        except Exception:  # noqa: BLE001 - defensive
            smt_verdict_raw = "NONE"
    smt_label = smt_verdict_raw.split(".")[-1]
    if setup.bias == "LONG" and smt_label == "BEARISH_DIVERGENCE":
        return (StrategyState.FLAT, "smt_direction_rejected")
    if setup.bias == "SHORT" and smt_label == "BULLISH_DIVERGENCE":
        return (StrategyState.FLAT, "smt_direction_rejected")

    # Build TradeIntent + evaluate gates if wired.
    if sm._gate_list is not None and sm._ledger is not None:
        from nasdaq_ale_bot.execution.gates import TradeIntent

        qty_raw = sm._strategy_cfg.get("default_qty", Decimal("1"))
        qty = qty_raw if isinstance(qty_raw, Decimal) else Decimal(str(qty_raw))
        entry_d = Decimal(str(setup.entry_price))
        stop_d = Decimal(str(setup.stop_price))
        projected_risk = abs(entry_d - stop_d) * qty
        intent = TradeIntent(
            symbol=getattr(sm._instrument, "symbol", "UNKNOWN"),
            side="BUY" if setup.bias == "LONG" else "SELL",
            entry_price=entry_d,
            stop_price=stop_d,
            projected_risk_usd=projected_risk,
            ts_utc=bar.ts,
            smt_verdict=smt_verdict_raw,
        )
        result = sm._gate_list.evaluate(sm._ledger, intent)
        if not result.allowed:
            return (StrategyState.FLAT, f"gate_rejected:{result.gate_name}")

    # All gates passed — commit the trade counter (local + ledger mirror).
    sm._trades_today += 1
    if sm._ledger is not None and hasattr(sm._ledger, "increment_trades_today"):
        sm._ledger.increment_trades_today()
    if in_am:
        sm._am_order_placed = True
    return (StrategyState.TRADE_MANAGEMENT, "order_submitted")


def _handle_trade_management(
    sm: StateMachine, view: CandleView
) -> HandlerResult:
    """Two phases:

    1. Pre-fill (``setup.entry_filled is False``) — the LIMIT is alive at the
       zone edge. We only watch for:
         * zone invalidation (body-close past far edge) → cancel LIMIT,
           transition FLAT with reason ``ifvg_invalidated_pre_fill``.
         * session-end (NY-date rolled) → cancel LIMIT, transition FLAT
           with reason ``ifvg_session_end_pre_fill``.
         * else → keep waiting (``awaiting_fill``).
       No stop/tp arithmetic here so the SM doesn't flatten before the
       broker fills.
    2. Post-fill (``entry_filled is True``) — normal stop/tp logic.
    """
    setup = sm._active_setup
    if setup is None or setup.entry_price is None or setup.stop_price is None:
        return (StrategyState.FLAT, "no_setup")
    bar = view[-1]

    if not setup.entry_filled:
        tol_ticks = int(sm._strategy_cfg.get("ifvg_tolerance_ticks", 0))
        tick = float(getattr(sm._instrument, "tick", 0.0) or 0.0)
        tol_offset = tol_ticks * tick
        if (
            setup.bias == "LONG"
            and setup.ifvg_zone_bottom is not None
            and bar.close < setup.ifvg_zone_bottom - tol_offset
        ):
            return (StrategyState.FLAT, "ifvg_invalidated_pre_fill")
        if (
            setup.bias == "SHORT"
            and setup.ifvg_zone_top is not None
            and bar.close > setup.ifvg_zone_top + tol_offset
        ):
            return (StrategyState.FLAT, "ifvg_invalidated_pre_fill")
        if (
            setup.ifvg_formed_ts is not None
            and bar.ts.astimezone(_ET).date()
            != setup.ifvg_formed_ts.astimezone(_ET).date()
        ):
            return (StrategyState.FLAT, "ifvg_session_end_pre_fill")
        return (sm.state, "awaiting_fill")

    # Post-fill — stop/tp arithmetic.
    if setup.bias == "LONG":
        if bar.low <= setup.stop_price:
            return (StrategyState.FLAT, "stop_out")
        if setup.take_profit is not None and bar.high >= setup.take_profit:
            return (StrategyState.FLAT, "target_hit")
    else:
        if bar.high >= setup.stop_price:
            return (StrategyState.FLAT, "stop_out")
        if setup.take_profit is not None and bar.low <= setup.take_profit:
            return (StrategyState.FLAT, "target_hit")
    return (sm.state, "managing")


def _handle_flat(sm: StateMachine, view: CandleView) -> HandlerResult:
    sm._active_setup = None
    return (StrategyState.BIAS_DETERMINATION, "rearm")
