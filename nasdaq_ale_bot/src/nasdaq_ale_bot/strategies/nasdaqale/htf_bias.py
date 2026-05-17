"""HTF bias detector (§A8).

Two-stage gating pipeline:

``INACTIVE`` -> body-close break of an unmitigated 4H FVG -> ``PENDING``
-> next 4H bar body-closes same side (two-bar confirmation) plus Daily
body-close agreement plus 1H HH/HL structure agreement -> ``ACTIVE``.

Structlog ``BIAS_FLIP_PENDING`` / ``BIAS_FLIP_ACTIVE`` events are emitted
only on genuine transitions -- no-op bars produce no log noise.

The detector is strictly single-timeframe internally (1m in, HTF out);
aggregation uses :mod:`nasdaq_ale_bot.bias.timeframe`.  FVG detection on
the aggregated 4H bars reuses :func:`nasdaq_ale_bot.strategies.nasdaqale.detection.fvg.detect_fvg`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.core.candle_view import CandleView
from nasdaq_ale_bot.core.leg import Direction
from nasdaq_ale_bot.strategies.nasdaqale.detection.fvg import FVG, detect_fvg
from nasdaq_ale_bot.settings import InstrumentSpec
from nasdaq_ale_bot.bias.timeframe import DailyAggregator, TimeframeAggregator


class HTFBias(StrEnum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"


class FlipState(StrEnum):
    INACTIVE = "INACTIVE"
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class HTFBiasState:
    """Immutable snapshot returned by :meth:`HTFBiasDetector.state`."""

    bias: HTFBias
    flip_state: FlipState
    pending_breach_4h_idx: int | None
    last_unmitigated_4h_fvg: FVG | None


class HTFBiasDetector:
    """Scenario-3-proof two-bar 4H confirmation with Daily + 1H gating."""

    def __init__(self, instrument: InstrumentSpec) -> None:
        self._instrument = instrument
        self._agg_4h = TimeframeAggregator(minutes=240)
        self._agg_1h = TimeframeAggregator(minutes=60)
        self._agg_daily = DailyAggregator()

        self._bars_4h: list[Candle] = []
        self._bars_1h: list[Candle] = []
        self._fvgs: list[FVG] = []

        self._bias: HTFBias = HTFBias.NONE
        self._flip_state: FlipState = FlipState.INACTIVE
        self._pending_breach_4h_idx: int | None = None
        self._pending_direction: Direction | None = None
        self._pending_4h_confirmed: bool = False

        # Replaced 1H-HH/HL + Daily-direction gating with PDH/PDL price cross.
        self._pdh: float | None = None
        self._pdl: float | None = None

        self._log = structlog.get_logger().bind(
            component="htf_bias",
            symbol=instrument.symbol,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_1m_bar(self, bar: Candle) -> HTFBiasState:
        """Feed a 1m bar. Aggregates internally; returns current state."""
        closed_1h = self._agg_1h.on_1m_bar(bar)
        closed_4h = self._agg_4h.on_1m_bar(bar)
        closed_d = self._agg_daily.on_1m_bar(bar)
        if closed_1h is not None:
            self._on_1h_close(closed_1h)
        if closed_4h is not None:
            self._on_4h_close(closed_4h, len(self._bars_4h))
        if closed_d is not None:
            self._on_daily_close(closed_d)
        # Promotion now keys on the live 1m close vs PDH/PDL — no longer
        # gated by Daily-direction or 1H-HH/HL structure (per simplification).
        if (
            self._flip_state == FlipState.PENDING
            and self._pending_4h_confirmed
        ):
            self._check_flip_promotion(price=bar.close)
        return self.state

    @property
    def state(self) -> HTFBiasState:
        last = self._fvgs[-1] if self._fvgs else None
        return HTFBiasState(
            bias=self._bias,
            flip_state=self._flip_state,
            pending_breach_4h_idx=self._pending_breach_4h_idx,
            last_unmitigated_4h_fvg=last,
        )

    # ------------------------------------------------------------------
    # Per-timeframe close handlers
    # ------------------------------------------------------------------

    def _on_4h_close(self, bar_4h: Candle, idx_4h: int) -> None:
        """Drive PENDING directly off the 4H body direction (simplified per FIX 1).

        FVG aggregation/mitigation is preserved so ``state.last_unmitigated_4h_fvg``
        keeps reporting correctly, but the FVG-breach + 2-bar confirmation
        gating is removed in favour of: ``body_dir → PENDING(direction)``.
        Promotion to ACTIVE happens in :meth:`on_1m_bar` on price-vs-PDH/PDL cross.
        """
        self._mitigate_fvgs(bar_4h)
        body_dir = _body_direction(bar_4h)

        if body_dir is not None:
            already_active_same = self._flip_state == FlipState.ACTIVE and (
                (self._bias == HTFBias.LONG and body_dir == Direction.UP)
                or (self._bias == HTFBias.SHORT and body_dir == Direction.DOWN)
            )
            if not already_active_same:
                # Direction reversal demotes any opposing ACTIVE; same-direction
                # PENDING is just refreshed with the latest breach_idx.
                if (
                    self._flip_state != FlipState.PENDING
                    or self._pending_direction != body_dir
                ):
                    self._log.info(
                        "BIAS_FLIP_PENDING",
                        direction=body_dir.value,
                        breach_idx=idx_4h,
                        bar_ts=bar_4h.ts.isoformat(),
                    )
                self._flip_state = FlipState.PENDING
                self._pending_direction = body_dir
                self._pending_breach_4h_idx = idx_4h
                self._pending_4h_confirmed = True
                if not already_active_same:
                    self._bias = HTFBias.NONE

        self._bars_4h.append(bar_4h)
        if len(self._bars_4h) >= 3:
            view = CandleView(self._bars_4h, len(self._bars_4h) - 1)
            new_fvgs = detect_fvg(view, len(self._bars_4h) - 1)
            self._fvgs.extend(new_fvgs)

    def _on_1h_close(self, bar_1h: Candle) -> None:
        # 1H structure no longer participates in promotion. Aggregator stays
        # for backward-compat with any 1H consumers; bars_1h kept in sync.
        self._bars_1h.append(bar_1h)

    def _on_daily_close(self, bar_d: Candle) -> None:
        # Capture prior-day high/low. The aggregator emits today's closed daily
        # bar on the first 1m of the next session, so from then on this bar's
        # high/low ARE the prior-day extremes for promotion checks.
        self._pdh = bar_d.high
        self._pdl = bar_d.low

    # ------------------------------------------------------------------
    # Promotion / helpers
    # ------------------------------------------------------------------

    def _check_flip_promotion(self, price: float | None = None) -> None:
        """Promote PENDING -> ACTIVE on price-vs-PDH/PDL cross in pending direction.

        Conditions (replaces Daily-direction + 1H-HH/HL gating):
          * LONG: price > PDH (and pending direction is UP)
          * SHORT: price < PDL (and pending direction is DOWN)
        4H direction is already encoded in the PENDING state's _pending_direction
        plus the 2-bar same-side _pending_4h_confirmed flag.
        """
        if self._flip_state == FlipState.ACTIVE:
            return
        if not self._pending_4h_confirmed:
            return
        if self._pending_direction is None or price is None:
            return
        if self._pending_direction == Direction.UP:
            if self._pdh is None or price <= self._pdh:
                return
        else:  # DOWN
            if self._pdl is None or price >= self._pdl:
                return
        self._flip_state = FlipState.ACTIVE
        self._bias = (
            HTFBias.LONG
            if self._pending_direction == Direction.UP
            else HTFBias.SHORT
        )
        self._log.info(
            "BIAS_FLIP_ACTIVE",
            direction=self._pending_direction.value,
            bias=self._bias.value,
        )

    def _reset_pending(self) -> None:
        self._flip_state = FlipState.INACTIVE
        self._pending_breach_4h_idx = None
        self._pending_direction = None
        self._pending_4h_confirmed = False

    def _mitigate_fvgs(self, bar: Candle) -> None:
        body_lo = min(bar.open, bar.close)
        body_hi = max(bar.open, bar.close)
        surviving: list[FVG] = []
        for fvg in self._fvgs:
            if body_hi >= fvg.bottom and body_lo <= fvg.top:
                # body-close intersects the gap -> mitigated
                continue
            surviving.append(fvg)
        self._fvgs = surviving

    def _detect_break(self, bar: Candle) -> Direction | None:
        """Body-close break of the most-recent unmitigated 4H FVG.

        * UP FVG (bullish gap): ``close < bottom`` is a *bearish* break.
        * DOWN FVG (bearish gap): ``close > top`` is a *bullish* break.
        """
        for fvg in reversed(self._fvgs):
            if fvg.direction == Direction.UP and bar.close < fvg.bottom:
                return Direction.DOWN
            if fvg.direction == Direction.DOWN and bar.close > fvg.top:
                return Direction.UP
        return None

    def _one_h_structure(self) -> Direction | None:
        """Classic ICT structure on the last two 1H bars.

        HH + HL -> :attr:`Direction.UP`; LH + LL -> :attr:`Direction.DOWN`;
        mixed -> ``None``.
        """
        if len(self._bars_1h) < 2:
            return None
        prev, cur = self._bars_1h[-2], self._bars_1h[-1]
        if cur.high > prev.high and cur.low > prev.low:
            return Direction.UP
        if cur.high < prev.high and cur.low < prev.low:
            return Direction.DOWN
        return None


def _body_direction(bar: Candle) -> Direction | None:
    if bar.close > bar.open:
        return Direction.UP
    if bar.close < bar.open:
        return Direction.DOWN
    return None
