# ASSUMPTIONS.md — Mechanical Interpretations (v2, post-consensus)

Every entry resolves a spec ambiguity with the **strictest mechanical interpretation** so detection and state-machine logic remain deterministic. Updated after Architect + Critic review.

---

## A1. CISD reference-candle selection (bullish)

**Ambiguity:** "last up-close candle immediately before the sweep move" allows multiple readings.

**Decision:** Walk backward from `sweep_idx`. As long as bars form a **contiguous down-leg** (each bar's `close < next bar's close`), continue. Stop at the first bar that breaks the down-leg. That breaking bar is the **run terminator**; if it is itself up-close (`close > open`, doji excluded), it is `reference_candle`. If the run terminator is not up-close, scan one further bar backward for the nearest up-close within a 20-bar safety cap. If none found in 20 bars, CISD is not confirmable.

**Rationale:** Matches ICT literature ("reference bar = start of the directional move that swept liquidity"). Fully mechanical — no visual interpretation required.

---

## A1b. CISD reference-candle selection (bearish mirror)

**Decision:** Mirror of A1. Walk backward from `sweep_idx` while bars form a contiguous up-leg (`close > next close`). Reference candle is the first **down-close** bar (`close < open`, doji excluded) terminating the run, within a 20-bar cap.

---

## A2. CISD confirmation window

**Decision:** Scan forward from `sweep_idx + 1` up to **15 bars**. First bar `j` where `view[j].close > reference_candle.high` (bullish) / `view[j].close < reference_candle.low` (bearish) confirms. Otherwise CISD times out. Both the 20-bar lookback (A1) and the 15-bar forward window (A2) are swept by the Phase 3 grid harness (`scripts/grid.py`).

---

## A3. Sweep minimum penetration

**Decision:** Tick size is a **per-instrument** value in `config/instruments.yaml` under the `tick` key. Example default for QQQ: `0.01` USD. `min_penetration_ticks = 2` (configurable) means a sweep requires the wick to extend ≥ 2 × tick beyond the level. No instrument is hard-coded in any assumption or source file.

---

## A4. IFVG count filter and tie-break

**Decision:** 0 IFVGs in the CISD move → **no entry** (invalid setup). 1 IFVG → ideal. 2 IFVGs → accept and pick the one **closer to the sweep level** (the deeper IFVG). Ties → pick the one with larger gap size. ≥ 3 IFVGs → reject (noise).

---

## A5. Equilibrium check leg

**Decision:** Use the **CISD-move leg**: `leg.low = min(view[sweep_idx..confirm_idx].low)`, `leg.high = max(view[sweep_idx..confirm_idx].high)`. Entry must be in discount (long) or premium (short) per Fib 0.5 of that leg.

---

## A6. SL construction

**Decision (long):** `swing_low` = most recent **1-minute** 5-bar pivot low before the sweep where `pivot_low[k] = min(view[k-2..k+2].low)` on the execution-timeframe (1m) bars. All three inputs (`IFVG.bottom`, `sweep_low`, `swing_low`) are read from the 1m series to keep SL construction on a single timeframe. SL = `min(IFVG.bottom, sweep_low, swing_low) - 2 * tick`. Bearish mirror. SL distance > `max_stop` (from `instruments.yaml`, scaled from NQ 25 pts via ATR ratio) → **SKIP** the trade (see A11).

---

## A7. TP selection and R:R gating

**Decision:** TP = first 3-bar pivot (high for long, low for short) encountered in the HTF leg spanning from the prior opposing swing to the current DOL. R:R gating:

- `R:R < 1:1.1` → SKIP
- `1:1.1 ≤ R:R ≤ 1:1.3` → take trade at natural TP
- `R:R > 1:1.3` → **cap TP** to yield exactly 1:1.3

Target average (backtest WR benchmark in PLAN §3 Phase 3) is R:R 1:1.2 — an **average**, not a cap. The 1:1.3 cap and 1:1.1 floor are the hard bounds. The floor clause `R:R < 1:1` is implied by 1:1.1 and is dropped from code to avoid dead branches.

**Logging:** Every `TradeIntent` (accepted or skipped for R:R) emits a `structlog` event carrying `natural_rr_at_signal` — the **uncapped** R:R computed from the natural TP pivot before any 1:1.3 cap is applied. Accepted trades additionally log `executed_rr` (post-cap). This lets Phase 3 analytics separate "capped winners" from "natural winners" without reconstructing state.

---

## A8. HTF bias break

**Decision:** Most recent **unmitigated 4H FVG** in the opposing direction. Bias flips when a 4H bar's **body** (`min(open,close)` / `max(open,close)`) closes beyond the FVG **and** the immediately following 4H bar also body-closes on the same side (anti-noise two-bar confirmation on the 4H timeframe).

**1H + Daily confirmation rule:** The flip is only **acted upon by the state machine** once both higher timeframes agree with the new bias:

- **Daily:** the current in-progress daily bar has printed at least one body-close in the direction of the new bias since the 4H flip (i.e., at least one completed daily session, or the live daily bar's latest close, confirms direction). If the Daily still closes against the new bias, the 4H flip is **pending**, not active.
- **1H:** on the 1H timeframe, structure must match — bullish bias requires the most recent completed 1H bar to print HH+HL relative to the prior 1H swing; bearish mirror. Failure → flip stays **pending**.

A pending flip becomes **active** the moment both confirmations are satisfied. Until active, the state machine keeps the prior bias (or `NONE` on first session) and does not arm entries. Logged as `BIAS_FLIP_PENDING` / `BIAS_FLIP_ACTIVE`.

---

## A9. Secondary killzone activation

**Decision:** Any **order placed** (entry submitted to the broker, regardless of subsequent fill or cancel) in the AM killzone [09:30, 11:00) ET disables the PM killzone [13:30, 16:00) ET for that session. Tracked as a session-scoped boolean in `runner.py` / state machine, not in `killzone.py` (filter stays pure). Killzones are **half-open intervals**: 09:30 included, 11:00 excluded.

---

## A10. News filter source

**Decision:** Phase 1 ships a CSV-backed stub at `config/news_events.csv` (`ts_utc,impact` header, ≥ 1 seed row). `news.py` raises `NewsFeedStale` if the file is missing **or** `mtime` is older than 24 h. Phase 4 runner calls `news.assert_fresh()` on startup before accepting the first WS bar; a stale feed refuses to start the runner. Live feed integration (ForexFactory scrape / TE API) deferred to Phase 4 behind the same interface.

---

## A11. Position sizing when stop > max allowed

**Decision:** **Skip** the trade — never widen to fit risk, never shrink position. Emit `structlog` event `SKIP_MAX_STOP` with fields `setup_id`, `sl_distance`, `max_stop`, `bar_ts`. Phase 2 acceptance criterion requires this log event to be produced by a unit test with an oversized synthetic setup.

---

## A12. SMT aggregation window

**Decision:** **Clock-anchored** 5-minute bars (09:30, 09:35, 09:40 ET…). The `SMTTracker` in `core/smt_tracker.py` owns the 1m→5m aggregation state; it emits a new 5m bar when the bar closes. `detection/smt_pure.py` is stateless and receives pre-aggregated 5m series — keeps the pure detection layer free of multi-clock coupling.

---

## A13. Missing bar handling (primary/correlated join) — **FAIL-CLOSED**

**Decision:** Forward-fill **max 1** missing bar on either symbol. **≥ 2 consecutive missing bars** on either symbol → SMT verdict is `UNAVAILABLE` for that timestamp. `UNAVAILABLE` **blocks any new entry** (fail-closed) and, for open positions, is **not** a forced exit by itself — but combined with stale data for > 2 bars, `safety.py` flattens. Matches Principle 4 (fail-closed safety) and eliminates the v1 fail-open gap.

---

## A14. Risk parameter fixed vs percent

**Decision:** `config/strategy.yaml`: `risk_mode: fixed | percent`, `risk_fixed_usd: 750`, `risk_percent: 0.005`. Default `fixed`. Percent mode queries `BrokerProtocol.get_account_equity()` at **session start only** (not intraday) and uses that value for all trades in the session.

---

## A15. Daily loss limit evaluation

**Decision (stop trigger):** **Realized PnL only**, computed at each closed trade. Unrealized drawdown does **not** trigger the −$1500 flatten-and-halt stop — a trade in progress is allowed to play out against its server-side bracket.

**Decision (new-entry gate):** **Unrealized PnL is considered for the new-entry decision.** Before arming a new entry, the runner computes `projected_pnl = realized_pnl_today + unrealized_pnl_open_positions + (-risk_per_trade_usd)` — i.e., "if this next trade hits full stop, where would I end the session?" If `projected_pnl < -$1500`, the entry is **skipped** with `structlog` event `SKIP_PROJECTED_LOSS_LIMIT`. This prevents a trader from stacking a losing open position with a fresh entry that would exceed the cap on a bad outcome, while still letting the existing trade run.

On actual breach (realized side): flatten any open position, cancel all working orders, halt new entries until session rotation at 00:00 ET.

---

## A16. Session boundary

**Decision:** Session = America/New_York calendar day. All counters (trades placed, realized PnL, killzone AM/PM flag, `SMTTracker` state, `NewsFeedStale` check) reset at 00:00 ET. Phase 2 acceptance test asserts counter reset.

---

## A17. Breakeven move trigger — **EXEMPT from body-close principle**

**Decision:** Breakeven move triggers on **touch** (any bar high/low reaches the 50 %-to-TP price). SL moved to entry + 1 tick (long) / entry − 1 tick (short) so fees/slippage can't turn BE into a loss.

**Exemption rationale:** PLAN §2 Principle 3 ("body-close semantics") applies to **signal detection** (CISD, FVG breach, IFVG confirmation, HTF bias). Risk-management triggers (BE, time exit, SL/TP bracket fills) are explicitly exempt and operate on touch. BE execution uses `BrokerProtocol.modify_bracket_stop(order_id, new_stop_price)` — no local stop-touch evaluation.

---

## A18. Hard time exit

**Decision:** At 11:00 ET (AM session) or 15:45 ET (PM session), send a **market order immediately** to flatten. On NYSE early-close days (from `pandas_market_calendars`), 15:45 ET shifts to `close - 15 minutes`. Logged as `TIME_EXIT`. Exempt from body-close principle (risk-management, like A17).

---

## A19. Trade idempotency

**Decision:** Every `TradeIntent` carries `client_order_id = sha1(f"{bar_ts_iso}|{direction}|{strategy_version}")` where `bar_ts_iso` is the confirming bar's ISO-8601 UTC timestamp and `strategy_version` is a constant in `core/__init__.py` bumped on any rule change. Alpaca bracket submission uses this as `client_order_id`, making order placement idempotent across reconnects and preventing duplicate entries after a crash-recovery replay.

---

## A20. Broker abstraction for instrument swap

**Decision:** `src/nasdaq_ale_bot/execution/broker.py::BrokerProtocol` (`typing.Protocol`) with **nine** methods:

1. `place_bracket(symbol, side, qty, entry, stop, take_profit, client_order_id) -> OrderRef`
2. `modify_bracket_stop(order_id, new_stop_price) -> None` — for A17 BE moves
3. `cancel_all(symbol: str | None = None) -> None`
4. `flatten(symbol: str | None = None) -> None`
5. `get_positions() -> list[Position]` — reconnection reconciliation
6. `get_account_equity() -> Decimal` — A14 percent-mode session start
7. `get_order(client_order_id: str) -> OrderState | None` — A19 idempotency check on reconnect
8. `get_trading_calendar(date: date) -> TradingDay` — early-close handling for A18 and killzone
9. `stream_bars(symbols: list[str]) -> AsyncIterator[Bar]`

Alpaca implementation lives in `alpaca_client.py`. Future Tradovate / IBKR adapters implement the same Protocol; strategy and detection layers never touch broker code.

---

## A21. Threading contract (new)

**Decision:** The state machine is **single-threaded**. Live runner uses an actor model:

- **WS thread** (owned by `alpaca-py StockDataStream`): receives bar events, appends to `queue.Queue`, never touches strategy state.
- **Engine thread** (owned by `runner.py`): the **only** caller of `StateMachine.on_bar` and `safety.flatten`. Drains the queue synchronously.
- **Main thread**: startup, shutdown signals, log flushing.

`safety.py` interrupts come via a thread-safe flag the engine thread checks between bars, not via cross-thread method calls. Documented in `core/state_machine.py` module docstring.

---

## A22. Fill model — intrabar SL+TP collision (new)

**Decision:** When a single backtest bar's range contains both SL and TP prices, **SL wins** (conservative). Rationale: without tick data we cannot know which was touched first, so the risk-sided assumption is chosen. Documented in PLAN §9 ADR and enforced by `tests/unit/test_fill_model.py`.

---

## A23. Clock drift guard (new)

**Decision:** On runner startup, compare local UTC clock to Alpaca's server clock (via a trading-calendar or account-status request). If `|delta| > 2 s`, abort startup. Re-check hourly during the session; drift > 2 s → halt new entries (open positions continue under their server-side brackets).

---

## A24. Look-ahead enforcement (new)

**Decision:** Detection functions never receive raw `list[Candle]`. They receive `CandleView(bars, i)` from `core/candle_view.py`. `CandleView.__getitem__(k)` raises `LookAheadError` if `k > i`. This replaces convention-based enforcement (code review + asserts) with runtime enforcement. Every detection function's unit tests construct `CandleView(bars, i)` explicitly, documenting the allowed horizon.
