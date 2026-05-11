"""Stateful 1m -> 5m SMT tracker (§A12, §A13).

Owns the clock-anchored aggregation and the fail-closed logic so the
pure divergence math in :mod:`nasdaq_ale_bot.detection.smt_pure` stays
stateless.

Rules (pinned by PLAN.md §A12/§A13):

* Clock anchors at :00, :05, :10, ..., :55 on the wall clock -- the
  session-relative 09:30 ET anchor aligns to these naturally.
* If one side's 1m bar is missing and that side's missing streak is 0,
  forward-fill with a flat OHLC from the previous 1m close; streak -> 1.
* On the *second* consecutive miss from either side (streak >= 1),
  latch the verdict to :attr:`SMTVerdict.UNAVAILABLE` for the rest of
  the current 5m window.  UNAVAILABLE persists until the next clean
  5m window closes (no new misses), at which point the verdict
  re-evaluates normally.
* The verdict is computed only at 5m close and held constant until the
  next 5m close (`_latched_verdict`).

This tracker never emits structlog itself; the caller decides how to
log verdicts so the tracker stays a pure state machine.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from ..detection.smt_pure import SMTConfigError, detect_smt_divergence
from .candle import Candle


class SMTVerdict(StrEnum):
    BULLISH_DIVERGENCE = "BULLISH_DIVERGENCE"
    BEARISH_DIVERGENCE = "BEARISH_DIVERGENCE"
    NONE = "NONE"
    UNAVAILABLE = "UNAVAILABLE"


_Side = Literal["primary", "correlated"]


class SMTTracker:
    """Per-pair 1m -> 5m aggregator with fail-closed missing-bar handling."""

    def __init__(self, *, primary_symbol: str, correlated_symbol: str) -> None:
        self._primary_symbol = primary_symbol
        self._correlated_symbol = correlated_symbol

        self._primary_5m_buffer: list[Candle] = []
        self._correlated_5m_buffer: list[Candle] = []
        self._current_5m_anchor: datetime | None = None

        self._in_flight_primary_1m: list[Candle] = []
        self._in_flight_correlated_1m: list[Candle] = []

        self._latched_verdict: SMTVerdict = SMTVerdict.NONE
        self._missing_streak_primary: int = 0
        self._missing_streak_correlated: int = 0

        self._last_primary_1m: Candle | None = None
        self._last_correlated_1m: Candle | None = None

        self._unavailable_latched_in_window: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def primary_symbol(self) -> str:
        return self._primary_symbol

    @property
    def correlated_symbol(self) -> str:
        return self._correlated_symbol

    @property
    def verdict(self) -> SMTVerdict:
        return self._latched_verdict

    def on_1m_bar_pair(
        self,
        primary_bar: Candle | None,
        correlated_bar: Candle | None,
        bar_ts: datetime,
    ) -> SMTVerdict:
        """Feed one 1m tick for each symbol.

        Either argument may be ``None`` to signal a missing bar.  Returns
        the currently latched :class:`SMTVerdict` after this update.
        """
        anchor = self._anchor_for(bar_ts)
        if self._current_5m_anchor is None:
            self._current_5m_anchor = anchor
        elif anchor != self._current_5m_anchor:
            # Roll: close the previous 5m window before accepting the new bar
            self._close_5m_anchor(self._current_5m_anchor)
            self._current_5m_anchor = anchor
            self._in_flight_primary_1m = []
            self._in_flight_correlated_1m = []
            self._unavailable_latched_in_window = False

        p = self._consume_side(primary_bar, "primary", bar_ts)
        c = self._consume_side(correlated_bar, "correlated", bar_ts)

        if p is not None:
            self._in_flight_primary_1m.append(p)
            self._last_primary_1m = p
        if c is not None:
            self._in_flight_correlated_1m.append(c)
            self._last_correlated_1m = c

        return self._latched_verdict

    def force_close(self) -> SMTVerdict:
        """Close the in-flight 5m window and return the updated verdict."""
        if self._current_5m_anchor is None:
            return self._latched_verdict
        self._close_5m_anchor(self._current_5m_anchor)
        self._current_5m_anchor = None
        self._in_flight_primary_1m = []
        self._in_flight_correlated_1m = []
        self._unavailable_latched_in_window = False
        return self._latched_verdict

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _anchor_for(self, bar_ts: datetime) -> datetime:
        aligned = (bar_ts.minute // 5) * 5
        return bar_ts.replace(minute=aligned, second=0, microsecond=0)

    def _is_anchor_boundary(self, bar_ts: datetime) -> bool:
        return bar_ts.minute % 5 == 0

    def _consume_side(
        self,
        bar: Candle | None,
        side: _Side,
        bar_ts: datetime,
    ) -> Candle | None:
        if bar is not None:
            self._set_streak(side, 0)
            return bar

        # Missing bar -- apply forward-fill / UNAVAILABLE rule.
        streak = self._streak(side)
        last = self._last(side)
        self._set_streak(side, streak + 1)

        if streak >= 1 or last is None:
            # Second consecutive miss (or missing with no history to fill)
            self._latched_verdict = SMTVerdict.UNAVAILABLE
            self._unavailable_latched_in_window = True
            return None

        # First miss: carry forward a flat OHLC at the previous close
        flat = last.close
        return Candle(
            ts=bar_ts,
            open=flat,
            high=flat,
            low=flat,
            close=flat,
            volume=0.0,
        )

    def _streak(self, side: _Side) -> int:
        if side == "primary":
            return self._missing_streak_primary
        return self._missing_streak_correlated

    def _set_streak(self, side: _Side, value: int) -> None:
        if side == "primary":
            self._missing_streak_primary = value
        else:
            self._missing_streak_correlated = value

    def _last(self, side: _Side) -> Candle | None:
        if side == "primary":
            return self._last_primary_1m
        return self._last_correlated_1m

    def _close_5m_anchor(self, anchor_ts: datetime) -> None:
        if self._in_flight_primary_1m:
            self._primary_5m_buffer.append(
                _aggregate_1m_to_5m(self._in_flight_primary_1m, anchor_ts)
            )
        if self._in_flight_correlated_1m:
            self._correlated_5m_buffer.append(
                _aggregate_1m_to_5m(self._in_flight_correlated_1m, anchor_ts)
            )

        # UNAVAILABLE for this window wins over any divergence compute
        if self._unavailable_latched_in_window:
            return

        if (
            len(self._primary_5m_buffer) < 2
            or len(self._correlated_5m_buffer) < 2
        ):
            self._latched_verdict = SMTVerdict.NONE
            return

        i = min(len(self._primary_5m_buffer), len(self._correlated_5m_buffer)) - 1
        try:
            result = detect_smt_divergence(
                self._primary_5m_buffer,
                self._correlated_5m_buffer,
                i,
            )
        except SMTConfigError:
            self._latched_verdict = SMTVerdict.UNAVAILABLE
            return

        if result.bullish_divergence:
            self._latched_verdict = SMTVerdict.BULLISH_DIVERGENCE
        elif result.bearish_divergence:
            self._latched_verdict = SMTVerdict.BEARISH_DIVERGENCE
        else:
            self._latched_verdict = SMTVerdict.NONE


def _aggregate_1m_to_5m(bars: list[Candle], anchor_ts: datetime) -> Candle:
    assert bars, "cannot aggregate empty 1m buffer"
    return Candle(
        ts=anchor_ts,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
    )
