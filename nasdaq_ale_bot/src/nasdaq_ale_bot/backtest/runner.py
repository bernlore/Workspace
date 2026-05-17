"""BacktestRunner — bar-by-bar replay engine for Phase 3.

Step 3 limitations:
  - BE-move (BREAKEVEN_MOVE / "BE_*" reasons): deferred to Phase 4.
    The `StateEvent.reason` field is inspected but no `modify_bracket_stop`
    call is issued; the new stop value is not plumbed through StateEvent in
    Phase 2.
  - MetricsCalculator fields in `BacktestResult.metrics` are left as an empty
    dict; MetricsCalculator ships in Step 4.

API mismatch adaptations vs PLAN_PHASE3.md §3.4:
  - `sm.current_state` -> `sm.state`  (core/state_machine.py:115)
  - `sm.active_setup`  -> `sm._active_setup`  (private; accessed by runner with
    a comment on each access site)
  - `StrategyState.ENTRY_ARMED` -> `StrategyState.ENTRY_EXECUTION`
  - Setup has no .symbol/.side/.qty/.take_profit_price/.client_order_id; these
    are derived by the bridge from instrument_cfg/strategy_cfg and setup fields.
  - `event.event_name` does not exist on StateEvent; flatten/BE are detected by
    inspecting event.from_state / event.to_state / event.reason.
  - `ledger.on_fill` is NOT called by BacktestRunner; MockBroker._dispatch_to_ledger
    handles it internally; double-call would double-count.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.core.candle import Candle
from nasdaq_ale_bot.strategies.nasdaqale.state_machine import (
    StateMachine,
    StrategyState,
)
from nasdaq_ale_bot.execution.mock_broker import FillEvent, MockBroker

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# exit-reason mapping (FillEvent.fill_reason -> TradeRecord.exit_reason)
# ---------------------------------------------------------------------------

_FILL_REASON_MAP: dict[str, str] = {
    "STOP_OUT": "stop_out",
    "TAKE_PROFIT": "target_hit",
    "FLATTEN": "flatten",
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


class TradeRecord:
    """One completed round-trip trade."""

    __slots__ = (
        "entry_ts",
        "exit_ts",
        "symbol",
        "side",
        "entry_price",
        "exit_price",
        "stop_price",
        "qty",
        "realized_pnl",
        "exit_reason",
        "param_set_hash",
    )

    def __init__(
        self,
        *,
        entry_ts: datetime,
        exit_ts: datetime,
        symbol: str,
        side: str,
        entry_price: Decimal,
        exit_price: Decimal,
        qty: Decimal,
        realized_pnl: Decimal,
        exit_reason: str,
        param_set_hash: str | None,
        stop_price: Decimal | None = None,
    ) -> None:
        self.entry_ts = entry_ts
        self.exit_ts = exit_ts
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.stop_price = stop_price
        self.qty = qty
        self.realized_pnl = realized_pnl
        self.exit_reason = exit_reason
        self.param_set_hash = param_set_hash

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TradeRecord):
            return NotImplemented
        return all(
            getattr(self, s) == getattr(other, s) for s in self.__slots__
        )

    def __repr__(self) -> str:
        return (
            f"TradeRecord(symbol={self.symbol!r}, side={self.side!r}, "
            f"entry_ts={self.entry_ts!r}, exit_reason={self.exit_reason!r}, "
            f"realized_pnl={self.realized_pnl!r})"
        )


class BacktestResult:
    """Output of a single backtest run."""

    def __init__(
        self,
        *,
        trades: list[TradeRecord],
        equity_curve: list[tuple[datetime, Decimal]],
        metrics: dict[str, Any],
        params: dict[str, Any],
        param_set_hash: str,
        window_start: date,
        window_end: date,
    ) -> None:
        self.trades = trades
        self.equity_curve = equity_curve
        self.metrics = metrics
        self.params = params
        self.param_set_hash = param_set_hash
        self.window_start = window_start
        self.window_end = window_end

    def __repr__(self) -> str:
        return (
            f"BacktestResult(trades={len(self.trades)}, "
            f"param_set_hash={self.param_set_hash!r}, "
            f"window={self.window_start}..{self.window_end})"
        )


# ---------------------------------------------------------------------------
# BacktestRunner
# ---------------------------------------------------------------------------


class BacktestRunner:
    """Bar-by-bar replay engine. One code path with StateMachine.on_bar().

    The runner constructs ``StateMachine`` internally from the provided
    collaborators.  All SM -> MockBroker bridging is this class's responsibility.

    Timing invariant: brackets armed on bar N fill against bar N+1.
    """

    def __init__(
        self,
        *,
        bars_primary: list[Candle],
        bars_correlated: list[Candle] | None = None,
        mock_broker: MockBroker,
        ledger: AccountLedger,
        strategy_cfg: dict[str, Any],
        instrument_cfg: Any,
        param_set_hash: str = "",
    ) -> None:
        """Construct the runner and its internal StateMachine.

        Args:
            bars_primary: Primary instrument bars (e.g. QQQ 1m), assumed sorted.
            bars_correlated: Optional correlated bars (e.g. SPY 1m) for SMT.
            mock_broker: MockBroker instance (shares ledger reference).
            ledger: AccountLedger instance.
            strategy_cfg: Strategy configuration dict.
            instrument_cfg: Object with a `.symbol` attr (e.g. InstrumentSpec).
            param_set_hash: Identifier for this param set run.
        """
        # Sort bars by ts to enforce pre-sorted invariant.
        self._bars_primary: list[Candle] = sorted(bars_primary, key=lambda b: b.ts)
        self._bars_correlated: list[Candle] | None = bars_correlated
        # Timestamp-keyed lookup so SMT receives the ES bar that actually
        # matches the current NQ bar's UTC minute (positional indexing drifts
        # whenever the two feeds have non-identical missing-minute patterns).
        self._correlated_lookup: dict[datetime, Candle] = (
            {b.ts: b for b in bars_correlated} if bars_correlated else {}
        )
        self._mock_broker = mock_broker
        self._ledger = ledger
        self._strategy_cfg = strategy_cfg
        self._instrument_cfg = instrument_cfg
        self._param_set_hash = param_set_hash

        # Symbol: instrument_cfg.symbol preferred; strategy_cfg["symbol"] fallback.
        self._symbol: str = (
            getattr(instrument_cfg, "symbol", None)
            or strategy_cfg.get("symbol", "UNKNOWN")
        )

        # Default quantity (Phase 4 adds real position sizing).
        raw_qty = strategy_cfg.get("default_qty", Decimal("1"))
        self._default_qty: Decimal = Decimal(str(raw_qty))

        # Risk-based position sizing (futures contracts):
        #   qty = floor(risk_per_trade_usd / (stop_distance * point_value)), min 1
        # When ``risk_per_trade_usd`` is absent we fall back to ``_default_qty``
        # (legacy QQQ/SPY behaviour with point_value=1).
        rpt = strategy_cfg.get("risk_per_trade_usd")
        self._risk_per_trade_usd: Decimal | None = (
            Decimal(str(rpt)) if rpt is not None else None
        )
        pv = getattr(instrument_cfg, "point_value", None)
        self._point_value: Decimal = Decimal(str(pv)) if pv is not None else Decimal("1")

        # Build optional SMTTracker before constructing StateMachine.
        smt_tracker = self._build_smt_tracker()

        # Collaborators may be injected via strategy_cfg for test override.
        bias_detector = strategy_cfg.get("_bias_detector", None)
        gate_list = strategy_cfg.get("_gate_list", None)

        # Construct StateMachine internally per spec §3.4.
        self._state_machine = StateMachine(
            bias_detector=bias_detector,
            smt_tracker=smt_tracker,
            gate_list=gate_list,
            ledger=ledger,
            instrument=instrument_cfg,
            strategy_cfg=strategy_cfg,
        )

        # Track armed order IDs to prevent double-arming (same setup, two bars).
        self._armed_order_ids: set[str] = set()

        # Open entry fills awaiting a matching exit: client_order_id -> FillEvent.
        self._open_entries: dict[str, FillEvent] = {}

        # Stop price per armed bracket — needed to compute true R-multiple at
        # fill time. Populated in _bridge_arm_bracket, consumed in _process_fills.
        self._stop_by_order_id: dict[str, Decimal] = {}

        # Accumulated closed-trade records.
        self._trades: list[TradeRecord] = []

        # Equity curve: list of (bar.ts, ledger.current_equity).
        self._equity_curve: list[tuple[datetime, Decimal]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        """Main replay loop.

        Returns:
            BacktestResult with trades, equity curve, and empty metrics.
        """
        bars = self._bars_primary

        if not bars:
            return BacktestResult(
                trades=[],
                equity_curve=[],
                metrics={},
                params=self._strategy_cfg,
                param_set_hash=self._param_set_hash,
                window_start=date.today(),
                window_end=date.today(),
            )

        for i, bar in enumerate(bars):
            self._process_bar(bar, i)

        # Warn about open positions at end of run (unmatched entries).
        if self._open_entries:
            _log.warning(
                "backtest_run_ended_with_open_entries",
                count=len(self._open_entries),
                order_ids=list(self._open_entries.keys()),
            )

        self._assert_equity_curve_monotonic()

        return BacktestResult(
            trades=list(self._trades),
            equity_curve=list(self._equity_curve),
            metrics={},
            params=self._strategy_cfg,
            param_set_hash=self._param_set_hash,
            window_start=bars[0].ts.date(),
            window_end=bars[-1].ts.date(),
        )

    @staticmethod
    def load_bars_from_parquet(path: Path) -> list[Candle]:
        """Load a parquet file into a list of Candle objects.

        Parquet columns expected: ts_utc, open, high, low, close, volume.
        Each row is validated through Candle's pydantic validators.

        Args:
            path: Path to the parquet file.

        Returns:
            List of Candle objects in row order.
        """
        import pyarrow.parquet as pq  # lazy import — not required for core replay

        table = pq.read_table(path)
        df = table.to_pandas()
        if len(df) == 0:
            raise ValueError(f"Parquet file contains zero rows: {path}")
        candles: list[Candle] = []
        for row in df.itertuples(index=False):
            candles.append(
                Candle(
                    ts=row.ts_utc.to_pydatetime(),
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                )
            )
        return candles

    @staticmethod
    def load_bars_from_dbn(path: Path, symbol_prefix: str) -> list[Candle]:
        """Load a Databento DBN/DBN.ZST file into a list of Candle objects.

        Filters to outright front-month contracts:
          1. Keep only rows whose ``symbol`` starts with ``symbol_prefix`` AND
             does NOT contain ``-`` (excludes calendar spreads).
          2. Per timestamp, keep the row with the highest volume — proxies the
             front-month contract without an explicit roll calendar.

        The DBN DataFrame index is ``ts_event`` (UTC tz-aware).
        OHLCV values are already floats in the databento-dbn format.
        """
        import databento as db

        store = db.DBNStore.from_file(path)
        df = store.to_df()
        if df.empty:
            raise ValueError(f"DBN file contains zero rows: {path}")
        df = df.reset_index()  # ts_event becomes a column
        mask = df["symbol"].str.startswith(symbol_prefix, na=False) & ~df[
            "symbol"
        ].str.contains("-", na=False, regex=False)
        df = df[mask]
        if df.empty:
            raise ValueError(
                f"No outright contracts matched prefix={symbol_prefix!r} in {path}"
            )
        df = df.sort_values(["ts_event", "volume"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ts_event"], keep="first")
        candles: list[Candle] = []
        for row in df.itertuples(index=False):
            candles.append(
                Candle(
                    ts=row.ts_event.to_pydatetime(),
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                )
            )
        return candles

    # ------------------------------------------------------------------
    # Internal replay helpers
    # ------------------------------------------------------------------

    def _process_bar(self, bar: Candle, i: int) -> None:
        """Process a single bar through SM -> broker bridge.

        Order enforces the §3.4 timing invariant "arm on bar N, fill on bar N+1":
        evaluate_fills runs BEFORE on_bar/bridge, so a bracket armed on bar N
        physically cannot be evaluated against bar N's OHLC.

        Sequence:
          1. Feed optional correlated bar to SMTTracker (before SM sees bar).
          2. evaluate_fills(bar) — armed orders from PRIOR bars settle here.
          3. Translate FillEvents to TradeRecords.
          4. Drive StateMachine.on_bar(bar); may queue a new setup.
          5. Bridge: arm bracket if any event transitioned into ENTRY_EXECUTION
             (or SM is still in ENTRY_EXECUTION from a prior bar).
          6. Bridge: flatten if any event was TRADE_MANAGEMENT -> FLAT.
          7. Append equity snapshot (post-fill, so exits are reflected).

        Look-ahead guard (§A24) is provided by StateMachine's internal
        CandleView — the runner does not need to build a second one.
        """
        # Step 1.
        self._feed_correlated_bar(bar, i)

        # Step 2 — fills for orders armed on previous bars.
        fills = self._mock_broker.evaluate_fills(bar)

        # Step 4.
        self._process_fills(fills)

        # Feed any stateful gate (e.g. TrendRegimeGate) the current bar BEFORE
        # the SM dispatches and before its check() is called by ENTRY_EXECUTION.
        gate_list = self._strategy_cfg.get("_gate_list")
        if gate_list is not None:
            for g in getattr(gate_list, "_gates", []):
                if hasattr(g, "on_1m_bar"):
                    g.on_1m_bar(bar)

        # Step 5.
        events = self._state_machine.on_bar(bar)

        # Step 6.
        self._bridge_arm_bracket(bar, events)

        # Step 7.
        self._bridge_flatten(events)

        # Step 8.
        self._equity_curve.append((bar.ts, self._ledger.current_equity))

        _log.debug(
            "bar_processed",
            bar_ts=bar.ts.isoformat(),
            bar_index=i,
            sm_state=str(self._state_machine.state),
            fills=len(fills),
        )

    def _bridge_arm_bracket(self, bar: Candle, events: list) -> None:
        """Arm a bracket order if SM entered or is in ENTRY_EXECUTION with a setup.

        Detection strategy: the SM dispatch loop may transition ENTRY_EXECUTION ->
        TRADE_MANAGEMENT within a single on_bar call, so checking only the
        post-call state misses the window. Instead, inspect the events list for
        any transition INTO ENTRY_EXECUTION, OR check if the SM is still
        currently in ENTRY_EXECUTION (multi-bar arm-wait case).
        """
        entered_entry = any(
            e.to_state == StrategyState.ENTRY_EXECUTION for e in events
        )
        still_in_entry = self._state_machine.state == StrategyState.ENTRY_EXECUTION
        if not (entered_entry or still_in_entry):
            return

        # Accessing private attr — SM has no public accessor for active_setup;
        # documented in module docstring as an API mismatch from the plan.
        setup = self._state_machine._active_setup  # noqa: SLF001
        if setup is None or setup.entry_price is None or setup.stop_price is None:
            return

        # Build a deterministic client_order_id.
        ts_iso = (
            setup.entry_bar_ts.isoformat()
            if setup.entry_bar_ts is not None
            else bar.ts.isoformat()
        )
        client_order_id = f"{self._symbol}-{setup.bias}-{ts_iso}"

        if client_order_id in self._armed_order_ids:
            return  # Deduplication: never re-arm the same setup.

        side = "BUY" if setup.bias == "LONG" else "SELL"
        entry_d = Decimal(str(setup.entry_price))
        stop_d = Decimal(str(setup.stop_price))
        if setup.take_profit is not None:
            tp_d = Decimal(str(setup.take_profit))
        else:
            # Fallback TP: one point from entry (setup incomplete).
            tp_d = entry_d + Decimal("1") if side == "BUY" else entry_d - Decimal("1")

        qty = self._size_for_trade(entry_d, stop_d)
        order_type = str(
            self._strategy_cfg.get("entry_order_type", "LIMIT")
        ).upper()

        if order_type == "IMMEDIATE":
            # Same-bar fill at the retest bar's close — no next-bar gap risk.
            # ``entry_d`` here is the retest bar's close (set by the SM zone
            # monitor); the broker books a position right now and returns an
            # ENTRY FillEvent that we feed into the runner's fill stream.
            fill = self._mock_broker.place_immediate(
                symbol=self._symbol,
                side=side,
                qty=qty,
                fill_price=entry_d,
                stop=stop_d,
                take_profit=tp_d,
                client_order_id=client_order_id,
                fill_ts=bar.ts,
            )
            self._armed_order_ids.add(client_order_id)
            self._stop_by_order_id[client_order_id] = stop_d
            self._process_fills([fill])
            _log.info(
                "immediate_entry",
                client_order_id=client_order_id,
                side=side,
                fill=str(entry_d),
                stop=str(stop_d),
                tp=str(tp_d),
            )
            return

        slip_ticks = int(
            self._strategy_cfg.get("entry_slippage_ticks", 0) or 0
        )
        tick = Decimal(str(getattr(self._instrument_cfg, "tick", "0.01") or "0.01"))
        slippage_max = Decimal(slip_ticks) * tick
        self._mock_broker.place_bracket(
            symbol=self._symbol,
            side=side,
            qty=qty,
            entry=entry_d,
            stop=stop_d,
            take_profit=tp_d,
            client_order_id=client_order_id,
            order_type=order_type,  # type: ignore[arg-type]
            slippage_max_price=slippage_max,
        )
        self._armed_order_ids.add(client_order_id)
        self._stop_by_order_id[client_order_id] = stop_d

        _log.info(
            "bracket_armed",
            client_order_id=client_order_id,
            side=side,
            entry=str(entry_d),
            stop=str(stop_d),
            tp=str(tp_d),
        )

    def _size_for_trade(self, entry: Decimal, stop: Decimal) -> Decimal:
        """Return position size for a single trade.

        If ``risk_per_trade_usd`` is configured, we size by:
            qty = floor(risk_per_trade_usd / (|entry - stop| * point_value))
        with a minimum of 1 contract (the spec keeps min=1 even if a single
        contract exceeds the budget — accept the over-risk on outlier-wide
        stops rather than skip the setup).
        Otherwise we fall back to ``default_qty`` (legacy QQQ/SPY behaviour).
        """
        if self._risk_per_trade_usd is None:
            return self._default_qty
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            return Decimal("1")
        risk_per_contract = stop_dist * self._point_value
        if risk_per_contract <= 0:
            return Decimal("1")
        from decimal import ROUND_DOWN

        qty = (self._risk_per_trade_usd / risk_per_contract).to_integral_value(
            rounding=ROUND_DOWN
        )
        return max(qty, Decimal("1"))

    def _bridge_flatten(self, events: list) -> None:
        """On TRADE_MANAGEMENT -> FLAT: cancel pending LIMITs + flatten any
        open position. Both calls are safe no-ops when there's nothing to do.

        Pre-fill exits (``ifvg_invalidated_pre_fill`` / ``ifvg_session_end_pre_fill``)
        leave a LIMIT on the broker's pending list — ``cancel_pending`` clears it.
        Post-fill exits (``stop_out`` / ``target_hit``) typically already have
        the position closed by ``evaluate_fills`` on the same bar; ``flatten``
        is then a no-op.
        """
        for event in events:
            if (
                event.from_state == StrategyState.TRADE_MANAGEMENT
                and event.to_state == StrategyState.FLAT
            ):
                _log.info("flatten_triggered", reason=event.reason, symbol=self._symbol)
                self._mock_broker.cancel_pending(None)
                self._mock_broker.flatten(symbol=None)
                return  # One flatten per bar is sufficient.

    def _process_fills(self, fills: list[FillEvent]) -> None:
        """Pair entry/exit FillEvents into TradeRecords."""
        for fill in fills:
            if fill.fill_reason == "ENTRY":
                self._open_entries[fill.client_order_id] = fill
                # Tell the SM the LIMIT actually filled so TRADE_MANAGEMENT
                # can switch from pre-fill to post-fill checks.
                active = self._state_machine._active_setup  # noqa: SLF001
                if active is not None and not active.entry_filled:
                    active.entry_filled = True
                _log.debug(
                    "entry_fill",
                    client_order_id=fill.client_order_id,
                    fill_price=str(fill.fill_price),
                )
            else:
                entry_fill = self._open_entries.pop(fill.client_order_id, None)
                if entry_fill is None:
                    _log.warning(
                        "exit_fill_without_open_entry",
                        client_order_id=fill.client_order_id,
                        fill_reason=fill.fill_reason,
                    )
                    continue

                exit_reason = _FILL_REASON_MAP.get(fill.fill_reason, "flatten")
                stop_at_arm = self._stop_by_order_id.pop(fill.client_order_id, None)
                record = TradeRecord(
                    entry_ts=entry_fill.fill_ts,
                    exit_ts=fill.fill_ts,
                    symbol=fill.symbol,
                    side=entry_fill.side,
                    entry_price=entry_fill.fill_price,
                    exit_price=fill.fill_price,
                    stop_price=stop_at_arm,
                    qty=fill.qty,
                    realized_pnl=fill.realized_pnl,
                    exit_reason=exit_reason,
                    param_set_hash=self._param_set_hash if self._param_set_hash else None,
                )
                self._trades.append(record)
                _log.info(
                    "trade_closed",
                    client_order_id=fill.client_order_id,
                    exit_reason=exit_reason,
                    realized_pnl=str(fill.realized_pnl),
                )

    def _feed_correlated_bar(self, primary_bar: Candle, i: int) -> None:
        """Feed correlated bar to SMTTracker before SM processes primary bar.

        Joins by UTC timestamp: looks up the correlated bar whose ts matches
        the primary bar's ts. If absent, passes None — the SMT tracker treats
        a missing bar via its forward-fill / UNAVAILABLE rules (§A13).
        """
        smt = self._state_machine._smt_tracker  # noqa: SLF001
        if smt is None or self._bars_correlated is None:
            return

        corr_bar = self._correlated_lookup.get(primary_bar.ts)

        try:
            smt.on_1m_bar_pair(
                primary_bar=primary_bar,
                correlated_bar=corr_bar,
                bar_ts=primary_bar.ts,
            )
        except Exception as exc:  # noqa: BLE001 - defensive
            _log.warning("smt_tracker_error", error=str(exc), bar_index=i)

    def _build_smt_tracker(self) -> Any:
        """Build an SMTTracker when correlated bars are provided."""
        if self._bars_correlated is None:
            return None
        # Allow test injection via strategy_cfg.
        pre_built = self._strategy_cfg.get("_smt_tracker", None)
        if pre_built is not None:
            return pre_built
        # Build from instrument config.
        primary_symbol = (
            getattr(self._instrument_cfg, "symbol", None) or "QQQ"
        )
        correlated_symbol = self._strategy_cfg.get("correlated_symbol", "SPY")
        try:
            from nasdaq_ale_bot.core.smt_tracker import SMTTracker
            return SMTTracker(
                primary_symbol=primary_symbol,
                correlated_symbol=correlated_symbol,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("smt_tracker_build_failed", error=str(exc))
            return None

    def _assert_equity_curve_monotonic(self) -> None:
        """Raise ValueError if equity curve timestamps are not strictly increasing."""
        for j in range(1, len(self._equity_curve)):
            prev_ts, curr_ts = self._equity_curve[j - 1][0], self._equity_curve[j][0]
            if curr_ts <= prev_ts:
                _log.error(
                    "equity_curve_non_monotonic",
                    prev_ts=prev_ts.isoformat(),
                    curr_ts=curr_ts.isoformat(),
                    index=j,
                )
                raise ValueError(
                    f"equity curve not strictly increasing at index {j}: "
                    f"{prev_ts.isoformat()} -> {curr_ts.isoformat()}"
                )
