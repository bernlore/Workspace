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

---

## A25. Apex — Daily loss cap (hard)

**Decision:** When `apex_mode.enabled`, the daily loss limit is `apex_mode.daily_loss_usd` (default −1000 USD) and **replaces** (not supplements) the base −1500 USD gate. Evaluation is identical to A15: realized PnL triggers immediate `flatten` + session halt; new-entry gate uses `projected_pnl = realized + unrealized - risk_per_trade`. Event: `APEX_DAILY_LOSS_BREACH` (realized) / `SKIP_APEX_DAILY_LOSS` (projected).

**Rationale:** Apex's rule is hard and account-killing. Using `projected_pnl` prevents stacking a new trade that could breach on a bad outcome while the existing trade plays out.

---

## A26. Apex — Trailing drawdown from high-water mark

**Decision:** `AccountLedger.high_watermark_equity` is updated on **every equity snapshot** (live: ≥ 1 Hz poll from `BrokerProtocol.get_realtime_equity`; backtest: on each closed bar). When `current_equity <= high_watermark_equity - apex_mode.trailing_dd_usd` (default 2500), `TrailingDDGate` fires:

- **Soft breach** (≤ 200 USD headroom): block new entries, emit `APEX_TRAILING_DD_WARN`.
- **Hard breach** (threshold crossed): `flatten` all + cancel all + write `.omc/state/apex_account_state.json` with `status: DEAD`, refuse to start runner on subsequent launches unless manually cleared.

**Rationale:** Apex treats the trailing DD as an account-death line, not a soft stop. The persisted state prevents accidental restart.

---

## A27. Apex — Profit target + 30-day clock

**Decision:** `profit_window_start_date` is persisted at first run under `apex_mode.enabled`. `cumulative_profit` is `sum(daily_realized_pnl)` over the window. When `cumulative_profit >= apex_mode.profit_target_usd` (default 3000), emit `APEX_TARGET_HIT` with `days_elapsed`; the gate does **not** block trading (Apex allows continued trading after target). If `today - profit_window_start_date > 30 calendar days` and target not met, raise `ApexWindowExpired` on next session start — runner refuses to start until state is reset. Calendar days, not trading days, per Apex spec.

---

## A28. Apex — Consistency rule

**Decision:** `ConsistencyGate` computes `best_day_ratio = best_day_profit / cumulative_profit` on each new entry intent. If `cumulative_profit >= 0.5 * profit_target_usd` **and** `best_day_ratio > 0.30`, block the entry with `SKIP_APEX_CONSISTENCY`. Below the 50% threshold the rule is **dormant** — Apex only enforces consistency near payout, and early-window trading should not be artificially capped.

**Rationale:** The dormant-until-near-target behavior prevents the rule from rejecting legitimate trades during the first half of the evaluation, when a single winning day naturally dominates the small cumulative profit.

---

## A29. Apex — Scaling plan (contract limits by account balance)

**Decision:** `apex_mode.scaling_plan` is a YAML list of `{balance_ge: <USD>, max_contracts: <int>}` pairs sorted ascending by `balance_ge`. `ScalingPlanGate` reads `current_equity`, picks the highest matching threshold, and clamps `intent.qty = min(intent.qty, max_contracts)`. Default 50k plan: `[{balance_ge: 50000, max_contracts: 2}, {balance_ge: 52500, max_contracts: 4}, {balance_ge: 55000, max_contracts: 10}]`. Clamping is silent (logged at INFO as `APEX_SCALE_CLAMP`, not a skip).

**Rationale:** Clamping preserves the signal — a blocked entry would waste a setup, while a reduced-size entry still participates. The plan is config-driven so Apex policy changes do not require code changes.

---

## A30. Apex — MNQ instrument, futures session, Tradovate/Rithmic broker

**Decision:** Primary symbol = `MNQ` (Micro E-mini Nasdaq-100 futures). `instruments.yaml::mnq` block: `tick=0.25`, `tick_value_usd=0.50`, `contract_size=2`, `rth_session=CME_GLOBEX`, `maintenance_window="17:00-18:00 ET"`. SL distance is computed in MNQ ticks directly — the Phase 1 QQQ→NQ ATR-ratio scaling is bypassed when primary symbol is a futures contract (flag on the instruments block). Correlated symbol = `ES` (Micro or full — configurable).

**Correlated symbol = `ES`** (full E-mini S&P 500), **not** MES. Rationale: SMT divergence is read-only on the correlated symbol — we never trade it, we only read its wicks and body-closes. The full ES contract has deep institutional liquidity and produces clean sweep wicks, while MES has thinner order books and occasionally prints distorted wicks that would generate false SMT signals. MES remains a valid config option for users on data-fee-constrained plans, but is **not** the default.

`TradovateBroker` is the Phase 5 reference implementation of `BrokerProtocol` (14 methods). Rithmic is a later alternative. Both use the same protocol — no strategy-layer change between them.

**Rationale:** Apex requires futures, and MNQ is the retail-sized NQ proxy that the original NasdaqAle strategy was designed on. No QQQ proxy means tick math is cleaner and matches the ICT literature directly.

---

## A31. GUI — Two-process supervised architecture

**Decision:** `bot_launcher` is a minimal supervisor (≤ 200 LOC) that spawns two children: `bot` (trading engine) and `bot_gui` (PyQt6 display). Separate OS processes, separate PIDs, no shared runtime. Supervisor restarts `bot_gui` on crash (up to 3×/minute then cool down 5 min). Supervisor **never auto-restarts `bot`** — a trading crash halts trading and requires human acknowledgment via `bot_launcher --ack` to resume.

**Rationale:** GUI bugs must not affect trading, and trading crashes must not silently resurrect and re-enter the market without a human in the loop.

---

## A32. GUI — IPC via append-only JSONL file tail

**Decision:** GUI reads `.omc/state/bot_events.jsonl` via `watchdog` filesystem events + incremental `seek` from the last known offset. No sockets, no pipes, no shared memory. The bot process is the **sole writer**; the GUI is the **sole reader**. GUI cannot send commands back — commands (pause, manual flatten) are implemented in Phase 7+ via a separate command file the bot polls.

**Rationale:** One-way file IPC is the strongest isolation guarantee — a GUI crash physically cannot corrupt bot state. `watchdog` gives ~100 ms latency, well below the 1 Hz refresh rate.

---

## A33. GUI — Windows auto-start via Startup folder shortcut

**Decision:** `scripts/install_autostart.py` writes a `.lnk` to `bot_launcher.exe` into `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`. No registry edits. No Windows Service. No admin rights required. `scripts/uninstall_autostart.py` removes the shortcut.

**Rationale:** Startup folder is the least-privileged mechanism. Services complicate debugging and prevent the GUI from rendering in the user session. Registry `Run` keys are hidden and easier to forget.

---

## A34. GUI — Crash isolation contract

**Decision:** Integration test in `tests/integration/test_gui_isolation.py` must prove:

- Kill `bot_gui` PID mid-session → `bot` continues for ≥ 60 s without state corruption, next bar processed normally.
- Kill `bot` PID mid-session → `bot_gui` displays red `BOT_OFFLINE` banner within 5 s, freezes last state, does not crash.
- Delete `bot_events.jsonl` while both running → `bot` recreates file on next event, `bot_gui` reopens tail.

**Rationale:** Makes the isolation guarantee testable rather than aspirational.

---

## A35. GUI — Event sink schema v1 (Phase 2 prerequisite)

**Decision:** `core/logging_sink.py` (shipped in **Phase 2**, not Phase 6) writes every `structlog` event additionally to `.omc/state/bot_events.jsonl`. Line format:

```json
{"schema_version": 1, "ts_utc": "2024-...", "level": "info", "event": "STATE_TRANSITION", "state": "CISD", "bar_ts": "2024-...", "fields": {...}}
```

Append-only. Rotation at 50 MB → `bot_events.jsonl.1` through `.jsonl.5` (oldest dropped). fsync every N events (N configurable, default 10) to bound loss on power cut. Schema is **frozen** at v1; Phase 6 GUI reads this schema. Any future v2 must keep `schema_version` field first-class so GUIs can refuse unknown versions.

**Rationale:** Shipping the sink in Phase 2 means every downstream phase (3 backtest, 4 live, 5 Apex) naturally writes the events the GUI will later consume. Delaying the sink would force a retroactive instrumentation pass.

---

## A36. GUI — Panels and refresh cadence

**Decision:** Fixed panel set for v1: (1) engine state + current bar ts, (2) equity curve (from `AccountLedger` snapshots in the event stream), (3) open positions table, (4) last 20 closed trades, (5) Apex compliance (HWM, trailing DD headroom, consistency ratio, profit target progress, days remaining in window — only visible when `apex_mode.enabled` in the observed events). Manual refresh button + 1 Hz auto-refresh (reads delta from last file offset only, never re-parses full file).

**Rationale:** Fixed panels keep Phase 6 scope bounded. 1 Hz is generous for a strategy whose signal cadence is 1-minute bars; tick-level refresh would waste CPU with no information gain.
