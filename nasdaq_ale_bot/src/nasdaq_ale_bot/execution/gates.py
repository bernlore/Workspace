"""Entry-gate composition layer for the NasdaqAle ICT Trading Bot.

Provides an ordered list of named guards that must all pass before an order is
placed. Phase 5 will append Apex-specific gates to the base list without
modifying the classes defined here.

Logger name: nasdaq_ale_bot.execution.gates
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
import yaml

from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.filters.killzone import in_primary_killzone, in_secondary_killzone
from nasdaq_ale_bot.filters.news import NewsFeedStale, is_news_blackout

_log = structlog.get_logger("nasdaq_ale_bot.execution.gates")

# ---------------------------------------------------------------------------
# Public data-transfer types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeIntent:
    """Describes a proposed trade before it is sent to any gate."""

    symbol: str
    side: Literal["BUY", "SELL"]
    entry_price: Decimal
    stop_price: Decimal
    projected_risk_usd: Decimal  # absolute value, positive
    ts_utc: datetime  # tz-aware UTC bar timestamp
    smt_verdict: str | None = None  # SMTVerdict string; None -> gate blocks


@dataclass(frozen=True)
class GateResult:
    """Result returned by a single gate or by GateList.evaluate()."""

    allowed: bool
    gate_name: str
    reason: str | None  # None when allowed=True


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EntryGate(Protocol):
    """Protocol that every gate must satisfy."""

    name: str  # unique identifier used in logs and GateResult

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        """Return GateResult(allowed=True, ...) or GateResult(allowed=False, ...)."""
        ...


# ---------------------------------------------------------------------------
# GateList
# ---------------------------------------------------------------------------


class GateList:
    """An ordered list of :class:`EntryGate` objects evaluated at entry time."""

    def __init__(self, gates: list[EntryGate]) -> None:
        self._gates = list(gates)

    # ------------------------------------------------------------------
    # Hot-path evaluation (first-failing-wins)
    # ------------------------------------------------------------------

    def evaluate(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        """First-failing-wins evaluation.

        Iterates gates in order, stopping at the first that blocks.
        Emits exactly one ``GATE_EVAL`` structlog event — either for the
        failing gate, or for the synthetic "all passed" result.

        This is the engine hot path; it must not allocate more than
        necessary and must not call evaluate_all().
        """
        for gate in self._gates:
            result = gate.check(ledger, intent)
            if not result.allowed:
                _log.info(
                    "GATE_EVAL",
                    gate_name=result.gate_name,
                    allowed=False,
                    reason=result.reason,
                    symbol=intent.symbol,
                )
                return result
        # All gates passed — emit single pass event
        pass_result = GateResult(allowed=True, gate_name="ALL_PASS", reason=None)
        _log.info(
            "GATE_EVAL",
            gate_name="ALL_PASS",
            allowed=True,
            reason=None,
            symbol=intent.symbol,
        )
        return pass_result

    # ------------------------------------------------------------------
    # Diagnostic evaluation (run every gate regardless of failure)
    # ------------------------------------------------------------------

    def evaluate_all(
        self,
        ledger: AccountLedger,
        intent: TradeIntent,
        *,
        log_individual_evals: bool = False,
    ) -> list[GateResult]:
        """Evaluate every gate regardless of failures.

        NOT on engine hot path; diagnostic use only.  Used by the
        integration test (Step 5) and Phase 3 backtest analytics to
        report multi-gate rejection reasons.

        ``log_individual_evals`` defaults to ``False`` so that Phase 3
        backtest replays do not flood JSONL output with per-gate
        ``GATE_EVAL`` events on every bar.  Set it to ``True`` in the
        integration test or in targeted per-sample analytics only.
        """
        results: list[GateResult] = []
        for gate in self._gates:
            result = gate.check(ledger, intent)
            if log_individual_evals:
                _log.info(
                    "GATE_EVAL",
                    gate_name=result.gate_name,
                    allowed=result.allowed,
                    reason=result.reason,
                    symbol=intent.symbol,
                )
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def base_list(cls, config: dict[str, Any]) -> "GateList":
        """Return the Phase 2 base gate set in the mandated evaluation order.

        Gates (in order):
            1. DailyLossGate         — §A15 realized-side daily loss cap
            2. ProjectedLossGate     — §A15 unrealized-side projected loss cap
            3. KillzoneGate          — §A9 AM/PM killzone window check
            4. NewsBlackoutGate      — §A10 news blackout window check
            5. SMTAvailabilityGate   — §A13 fail-closed on UNAVAILABLE
            6. MaxTradesGate         — 2-trade-per-day cap
            7. MaxStopGate           — §A11 max stop distance in NQ points
            8. TrendRegimeGate       — straight-line-trend regime filter

        Returns exactly these 8 gates in this order.
        """
        gates: list[EntryGate] = [
            DailyLossGate(
                daily_loss_limit=Decimal(str(config.get("daily_loss_limit", -1500)))
            ),
            ProjectedLossGate(
                daily_loss_limit=Decimal(str(config.get("daily_loss_limit", -1500)))
            ),
            KillzoneGate(),
            NewsBlackoutGate(
                window_seconds=int(config.get("news", {}).get("window_seconds", 900))
            ),
            SMTAvailabilityGate(),
            MaxTradesGate(
                max_trades=int(config.get("max_trades_per_day", 2))
            ),
            MaxStopGate(
                max_stop_nq_pts=Decimal(str(config.get("max_stop_nq_pts", 25)))
            ),
            TrendRegimeGate(
                efficiency_ratio=float(
                    config.get("trend_filter_efficiency_ratio", 3.0)
                ),
            ),
        ]
        return cls(gates)


# ---------------------------------------------------------------------------
# Concrete gate implementations
# ---------------------------------------------------------------------------


class DailyLossGate:
    """Block if realized_today has hit or exceeded the daily loss limit.

    §A15 realized side: if ledger.realized_today <= daily_loss_limit, block.
    Uses Decimal for all comparisons.
    """

    name = "DailyLossGate"

    def __init__(self, *, daily_loss_limit: Decimal) -> None:
        self._limit = daily_loss_limit  # expected to be negative, e.g. Decimal("-1500")

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        if ledger.realized_today <= self._limit:
            return GateResult(
                allowed=False,
                gate_name=self.name,
                reason="SKIP_DAILY_LOSS",
            )
        return GateResult(allowed=True, gate_name=self.name, reason=None)


class ProjectedLossGate:
    """Block if adding projected_risk_usd would breach the daily loss limit.

    §A15 unrealized side: projects the worst-case outcome as
    ``realized_today + unrealized - projected_risk_usd`` and blocks if
    that value would breach the limit.

    Example from spec: realized -800, unrealized -500, risk 300 →
    projected = -800 + (-500) + (-300) = -1600 < -1500 → block.
    """

    name = "ProjectedLossGate"

    def __init__(self, *, daily_loss_limit: Decimal) -> None:
        self._limit = daily_loss_limit

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        projected = (
            ledger.realized_today
            + ledger.unrealized
            - intent.projected_risk_usd
        )
        if projected <= self._limit:
            return GateResult(
                allowed=False,
                gate_name=self.name,
                reason="SKIP_PROJECTED_LOSS_LIMIT",
            )
        return GateResult(allowed=True, gate_name=self.name, reason=None)


class KillzoneGate:
    """Block if the bar timestamp is outside an active killzone window.

    §A9: accepts trades only during the AM (09:30-11:00 ET) or
    PM (13:30-15:45 ET) killzone windows.
    """

    name = "KillzoneGate"

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        ts = intent.ts_utc
        if in_primary_killzone(ts) or in_secondary_killzone(ts):
            return GateResult(allowed=True, gate_name=self.name, reason=None)
        return GateResult(
            allowed=False,
            gate_name=self.name,
            reason="SKIP_OUTSIDE_KILLZONE",
        )


class NewsBlackoutGate:
    """Block if the trade timestamp falls within a news blackout window.

    §A10: fail-closed on a stale or missing news feed — if
    ``NewsFeedStale`` is raised, the gate blocks and logs a warning.
    """

    name = "NewsBlackoutGate"

    def __init__(
        self,
        *,
        window_seconds: int = 900,
        csv_path: Path | None = None,
    ) -> None:
        self._window_seconds = window_seconds
        self._csv_path = csv_path or Path("data/news_events.csv")

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        try:
            if is_news_blackout(
                intent.ts_utc,
                self._csv_path,
                self._window_seconds,
            ):
                return GateResult(
                    allowed=False,
                    gate_name=self.name,
                    reason="SKIP_NEWS_BLACKOUT",
                )
        except NewsFeedStale as exc:
            _log.warning(
                "news_feed_stale_gate_blocked",
                gate=self.name,
                error=str(exc),
            )
            return GateResult(
                allowed=False,
                gate_name=self.name,
                reason="SKIP_NEWS_FEED_STALE",
            )
        return GateResult(allowed=True, gate_name=self.name, reason=None)


class SMTAvailabilityGate:
    """Block if the SMT verdict is UNAVAILABLE or not provided.

    §A13 fail-closed: any state other than an explicit non-UNAVAILABLE
    verdict blocks the trade.  Callers pass the verdict string (or None)
    via ``intent``; this gate reads it from ``intent.symbol``'s associated
    SMT context via the ledger extension point.

    Because Phase 2 does not yet wire the live SMT tracker into the
    intent, the gate checks for the presence of a ``smt_verdict`` attribute
    on the intent.  If it is absent, UNAVAILABLE, or None, the gate blocks.
    """

    name = "SMTAvailabilityGate"
    _UNAVAILABLE = "UNAVAILABLE"

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        verdict: str | None = getattr(intent, "smt_verdict", None)
        if verdict is None or verdict == self._UNAVAILABLE:
            return GateResult(
                allowed=False,
                gate_name=self.name,
                reason="SKIP_SMT_UNAVAILABLE",
            )
        return GateResult(allowed=True, gate_name=self.name, reason=None)


class MaxTradesGate:
    """Block if the maximum number of trades for the session has been reached.

    Reads trade count from ``ledger.trades_today`` if present, otherwise
    falls back to zero (fail-open on missing count — the engine must set
    this attribute for the gate to be effective).
    """

    name = "MaxTradesGate"

    def __init__(self, *, max_trades: int = 2) -> None:
        self._max = max_trades

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        trades_today: int = getattr(ledger, "trades_today", 0)
        if trades_today >= self._max:
            return GateResult(
                allowed=False,
                gate_name=self.name,
                reason="SKIP_MAX_TRADES",
            )
        return GateResult(allowed=True, gate_name=self.name, reason=None)


class MaxStopGate:
    """Block if the stop distance exceeds the maximum allowed NQ points.

    §A11: computes |entry_price - stop_price| and compares to the config
    limit.  Uses Decimal for all comparisons.  Emits a ``SKIP_MAX_STOP``
    structlog event when blocking.
    """

    name = "MaxStopGate"

    def __init__(self, *, max_stop_nq_pts: Decimal) -> None:
        self._max = max_stop_nq_pts

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        stop_dist = abs(intent.entry_price - intent.stop_price)
        if stop_dist > self._max:
            _log.info(
                "SKIP_MAX_STOP",
                gate_name=self.name,
                stop_dist=str(stop_dist),
                max_stop_nq_pts=str(self._max),
                symbol=intent.symbol,
            )
            return GateResult(
                allowed=False,
                gate_name=self.name,
                reason="SKIP_MAX_STOP",
            )
        return GateResult(allowed=True, gate_name=self.name, reason=None)


class TrendRegimeGate:
    """Block trading during straight-line trend regimes via directional efficiency.

    Maintains a rolling NY-day daily aggregate of the primary instrument's
    1m bars (fed via :meth:`on_1m_bar`). When ``check()`` is asked to admit
    an entry, it computes:

      net_move = (close[-1] - close[-10]) / close[-10]
      adr      = mean( (high - low) / close ) over the last 10 closed daily bars
      efficiency = |net_move| / adr

    and rejects with reason ``SKIP_TREND_REGIME`` iff
    ``efficiency > efficiency_ratio``. The ratio is volatility-normalised, so
    it works across instruments with different ADR profiles (NQ ≈ 1.0-2.0%,
    QQQ ≈ 0.5-1.0%) without needing per-instrument absolute thresholds.

    Interpretation: if the 10-day net move is more than ``efficiency_ratio``
    times the average daily range, the market is moving in a straight line —
    pullbacks the strategy needs aren't there.

    Fail-open until 10 closed daily bars have accumulated, or when adr is 0.
    """

    name = "TrendRegimeGate"

    def __init__(
        self,
        *,
        efficiency_ratio: float = 3.0,
    ) -> None:
        self._efficiency_ratio = float(efficiency_ratio)
        # Lazy import — DailyAggregator lives in the bias package and we
        # don't want to take a hard dependency at module import time.
        from nasdaq_ale_bot.bias.timeframe import DailyAggregator

        self._daily_agg = DailyAggregator()
        # Hold up to 30 closed daily bars; we only need the last 10 for the rule.
        self._daily_bars: list = []

    def on_1m_bar(self, bar: Any) -> None:
        """Feed a 1m bar — the runner calls this once per processed bar."""
        closed = self._daily_agg.on_1m_bar(bar)
        if closed is not None:
            self._daily_bars.append(closed)
            if len(self._daily_bars) > 30:
                self._daily_bars = self._daily_bars[-30:]

    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        if len(self._daily_bars) < 10:
            return GateResult(allowed=True, gate_name=self.name, reason=None)
        recent = self._daily_bars[-10:]
        close_first = float(recent[0].close)
        close_last = float(recent[-1].close)
        if close_first <= 0:
            return GateResult(allowed=True, gate_name=self.name, reason=None)
        net_move = (close_last - close_first) / close_first
        ranges = [
            (float(b.high) - float(b.low)) / float(b.close)
            for b in recent
            if float(b.close) > 0
        ]
        if not ranges:
            return GateResult(allowed=True, gate_name=self.name, reason=None)
        adr = sum(ranges) / len(ranges)
        if adr <= 0:
            return GateResult(allowed=True, gate_name=self.name, reason=None)
        efficiency = abs(net_move) / adr
        if efficiency > self._efficiency_ratio:
            _log.info(
                "SKIP_TREND_REGIME",
                gate_name=self.name,
                net_move=f"{net_move:+.4f}",
                adr_pct=f"{adr:.4f}",
                efficiency=f"{efficiency:.2f}",
                threshold=f"{self._efficiency_ratio:.2f}",
                symbol=intent.symbol,
            )
            return GateResult(
                allowed=False,
                gate_name=self.name,
                reason="SKIP_TREND_REGIME",
            )
        return GateResult(allowed=True, gate_name=self.name, reason=None)


# ---------------------------------------------------------------------------
# Config loader convenience (used by engine bootstrap)
# ---------------------------------------------------------------------------


def load_strategy_config(path: Path | None = None) -> dict[str, Any]:
    """Load ``config/strategy.yaml`` and return as a plain dict."""
    cfg_path = path or Path("config/strategy.yaml")
    with cfg_path.open() as fh:
        return yaml.safe_load(fh)  # type: ignore[return-value]
