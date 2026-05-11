# NasdaqAle ICT Trading Bot — Implementation Plan (v2, post-consensus)

**Status:** APPROVED-AFTER-ITERATION (awaiting user "go" before Phase 1 coding)
**Instrument mapping:** QQQ primary + SPY correlated (config-driven via `instruments.yaml`)
**Stack:** Python 3.11+, alpaca-py, pandas, pydantic v2, structlog, pytest, pandas_market_calendars

---

## 1. Requirements Summary

Build a mechanical ICT trading bot replicating the NasdaqAle YouTube strategy via a deterministic 6-state engine. The bot must:

- Drive all **signal detection** from pure functions (100% unit-testable, no Alpaca dependency, look-ahead-proof at runtime via `CandleView`).
- Share **one** `StateMachine.on_bar(closed_bar)` code path across backtest and live paper.
- Be instrument-agnostic — primary and correlated symbols, ticks, point value, and ATR-ratio scaling live in `config/instruments.yaml`.
- Fail **closed** on every ambiguity in live trading (stale data, disconnect, missing news feed, missing SMT data).
- Enforce hard safety caps: 2 trades/day, -$1500 realized daily loss, killzone windows, news blackout.
- Log every state transition via `structlog`, redact secrets, assert no look-ahead, and match golden-ledger fixtures byte-for-byte on refactors.

## 2. RALPLAN-DR Summary (Short Mode)

### Principles

1. **Purity in signal detection.** Every detection function is stateless, deterministic, and reads only `CandleView(bars, i)` — a wrapper that raises on `idx > i`. No look-ahead is possible by construction.
2. **Instrument-agnostic core.** Primary/correlated symbols, tick, point value, and ATR scaling live in `config/instruments.yaml`. Strategy and detection code receive `primary_bars`/`correlated_bars`, never `qqq_bars`/`spy_bars`.
3. **Body-close semantics for signals.** CISD, FVG breach, IFVG confirmation, and HTF bias flip all key on `candle.close`, never on wicks. **Exemption:** risk-management triggers (breakeven move, time exit, SL/TP bracket fills) operate on touch and are listed explicitly in `ASSUMPTIONS.md` §A17 / §A18.
4. **Fail-closed safety.** Any missing datum (SMT UNAVAILABLE, stale news CSV, WS disconnect, missing correlated symbol) halts new orders and — where open positions exist — flattens them.
5. **Mechanical over discretionary.** Ambiguous spec points get the strictest mechanical interpretation and a dated entry in `ASSUMPTIONS.md`.

### Decision Drivers

1. **Correctness of CISD detection** — wick-vs-body bug silently destroys WR. Exhaustively unit-tested and runtime-enforced via `CandleView` before any broker code.
2. **Reproducibility** — identical bar stream → identical trade ledger (golden-ledger test), modulo live fills/slippage.
3. **Safety of live runner** — cannot exceed risk caps under disconnect, crash, replay, news freeze, or clock drift.

### Viable Options Considered

**Option A — Custom bar-by-bar replay engine + pure detection functions (CHOSEN).**
- Pros: full control, injectable synthetic fixtures, no look-ahead risk (enforced by `CandleView`), one code path for backtest + live, explicit state transitions.
- Cons: more code (mitigated by ~200-LOC `metrics.py` and a Phase 3.5 cross-check against `vectorbt` on a toy strategy to validate metric numbers).

**Option B — `backtesting.py` / `vectorbt`.** Invalidation: forces stateful 6-state logic into vectorized Strategy classes, hiding CISD backward scans and look-ahead bugs. Spec explicitly forbids `backtesting.py`. Retained as a **cross-check harness** in Phase 3.5 only (not production path).

**Option C — Vectorized pandas indicators + post-hoc trade selection.** Invalidation: CISD's "reference up-close candle immediately preceding the sweep" is a stateful backward scan that vectorization obscures; cross-instrument SMT on two clocks amplifies the problem.

### Chosen Approach
Option A. Pure-function detection with `CandleView`, feeding a single-threaded state machine. Backtest and live share `on_bar(closed_bar)`. Live uses a WebSocket-thread → queue → engine-thread actor model (see §4.1.2). BE/SL/TP evaluated **server-side** via Alpaca bracket orders; local code never evaluates stop touches.

## 3. Acceptance Criteria

### Phase 1 — Pure Detection Layer

- [ ] `pytest -q` green on `tests/unit/**`
- [ ] `pytest --cov=src/nasdaq_ale_bot/detection --cov-branch` ≥ **90 %** overall
- [ ] **`cisd.py` branch coverage ≥ 95 %** (critical file per-file gate)
- [ ] CISD unit tests cover all three mandated cases plus bearish mirrors (`tests/unit/test_cisd.py`)
- [ ] Mutation test: changing `candles[j].close` → `candles[j].high` in `detect_bullish_cisd` makes `test_wick_break_not_confirmed` fail (replaces grep check)
- [ ] Every detection function receives a `CandleView` and calls into `view[k]`; `view[k]` for `k > i` raises `LookAheadError`. Unit test `test_candle_view.py` asserts the raise.
- [ ] `Candle` pydantic validators (tz-aware UTC, `high >= max(open,close)`, `low <= min(open,close)`) covered by `tests/unit/test_candle_validation.py`
- [ ] Killzone filter passes DST transition tests (March 2024-03-10, November 2024-11-03) **and** NYSE early-close (2024-11-29 day after Thanksgiving, 13:00 ET close)
- [ ] `news.py` raises `NewsFeedStale` if `config/news_events.csv` is missing or `mtime` older than 24 h; Phase 1 stub CSV ships with `ts_utc,impact` header and at least one row
- [ ] No Alpaca imports anywhere under `src/nasdaq_ale_bot/detection/`
- [ ] `pyproject.toml` installs cleanly on Python 3.11 and 3.12
- [ ] `.env.example` present, `.gitignore` excludes `.env`, `test_logging_redaction.py` asserts no secret leak
- [ ] `ruff check` and `mypy src/nasdaq_ale_bot/detection` clean
- [ ] Threading contract documented in `src/nasdaq_ale_bot/core/state_machine.py` module docstring

### Phase 2 — State Machine + HTF Bias

- [ ] `StrategyState` enum + `StateMachine.on_bar(bar) -> list[StateEvent]` return type defined; each transition emits exactly one `StateEvent`
- [ ] Full `(BIAS → SWEEP → CISD → IFVG → ENTRY → MANAGE → FLAT)` lifecycle on synthetic fixture
- [ ] `structlog` JSON event on every transition: `from_state`, `to_state`, `reason`, `bar_ts`
- [ ] Integration test on `tests/fixtures/qqq_1m_sample.csv` (≥ 5 trading days, ≥ 1 950 bars) emits ≥ 1 completed trade lifecycle
- [ ] HTF bias yields `LONG | SHORT | NONE` for every trading day in the fixture
- [ ] **AM → PM killzone state** test: AM order placed → `pm_killzone_enabled == False` for remainder of session (§A9)
- [ ] **Session rotation** test: counters (trades, realized PnL, killzone flags) reset at 00:00 ET (§A16)
- [ ] **`SKIP_MAX_STOP`** structured log event emitted when structural SL exceeds max (§A11)
- [ ] **`AccountLedger` primitive** (`core/account_ledger.py`): `Decimal` fields `realized_today`, `unrealized`, `high_watermark_equity`, `session_start_equity`, `best_day_profit`, `cumulative_profit`, `profit_window_start_date`; update hooks on fill/close/EOD. Unit tests cover HWM monotonicity and EOD rotation (§A25-A30 pre-wiring)
- [ ] **`GateList` composition** (`execution/gates.py`): ordered list of `EntryGate` callables with `(ledger, intent) -> GateResult(allow: bool, reason: str)`. Base gates = daily loss, max trades, killzone, news, SMT, projected_pnl. Apex gates will append in Phase 5; Phase 2 AC verifies `gates.base_list()` returns exactly the v1 set so `apex_mode: false` behavior is unchanged
- [ ] **BrokerProtocol extended to 14 methods** with futures-oriented defaults. Additions (default `NotImplementedError` in Alpaca stub, exercised only when `apex_mode.enabled`): `get_contract_spec(symbol) -> ContractSpec`, `get_session_pnl() -> Decimal`, `get_realtime_equity() -> Decimal`, `assert_market_open(ts) -> None`, `submit_market_flatten(symbol) -> OrderRef`. Protocol file unit test asserts 14 methods present
- [ ] **`core/logging_sink.py`** writes every `structlog` event additionally to `.omc/state/bot_events.jsonl` (append-only, line-delimited JSON, schema v1: `{ts_utc, level, event, state, bar_ts, fields}`). Rotation at 50 MB → `bot_events.jsonl.1`. Unit test `test_logging_sink.py` asserts round-trip parse and rotation. This feeds Phase 6 GUI without coupling to the engine process (§A35)
- [ ] **`instruments.yaml` schema extended** with optional `futures:` block (`contract_size`, `tick_value_usd`, `margin_requirement_usd`, `rth_session`, `maintenance_window`) — Phase 2 adds schema, populates MNQ/ES rows; detection code untouched

### Phase 3 — Backtest Engine

- [ ] Loads **pinned** QQQ + SPY 1-minute bars for `2024-01-01` through `2024-06-30` via Alpaca, caches to parquet, records SHA-256 of each parquet in `tests/fixtures/data_hashes.json`
- [ ] **Walk-forward split:** In-Sample (IS) = `2024-01-01..2024-04-30` (4 months), Out-of-Sample (OOS) = `2024-05-01..2024-06-30` (2 months). All parameter tuning (grid harness, CISD windows, sweep penetration) happens on IS only. OOS is run **exactly once** per parameter set to produce the reported WR/DD/Sharpe numbers. OOS results must be printed in a separate section of the HTML report and must not feed back into tuning.
- [ ] Bar-by-bar replay using `CandleView`; no look-ahead possible
- [ ] **Fill-model ADR implemented:** intrabar SL+TP collision → **SL wins** (conservative). Unit test `test_fill_model.py` proves it.
- [ ] Metrics: WR, avg R, Max DD, profit factor, Sharpe, trades/day, avg hold time
- [ ] HTML report with equity curve + trade table
- [ ] **Phase 3.5 cross-check:** `scripts/metrics_crosscheck.py` runs a simple SMA-cross stand-in strategy through both the custom engine and `vectorbt`; WR, Max DD, Sharpe agree within 1 % on the pinned 6-month data
- [ ] **Golden-ledger test** `tests/integration/test_golden_ledger.py`: pinned expected-trades JSON (hash-compared); CI fails on drift
- [ ] **Parameter grid harness** `scripts/grid.py` sweeps `min_penetration_ticks ∈ {1,2,3}`, CISD `lookback_bars ∈ {10,20,30}`, `confirm_window ∈ {10,15,20}` and emits a WR matrix
- [ ] Target: WR ≥ 55 % at R:R 1:1.2 on the **Out-of-Sample** window (2024-05-01..2024-06-30). IS WR is reported but is not the gate — OOS is the pass/fail. Miss on OOS → ultraqa debug loop, first suspect CISD body-close bug or overfit on IS.

### Phase 4 — Paper Live Runner

- [ ] Alpaca WebSocket consumes QQQ + SPY 1m bars joined on identical timestamps via `SMTTracker`
- [ ] WS-thread → `queue.Queue` → engine-thread; `StateMachine.on_bar` called **only** from the engine thread
- [ ] Hard safety stops: 2-trade cap, −$1500 realized daily stop, WS disconnect → cancel all + flatten, outside 09:30–16:00 ET → no new orders
- [ ] **News CSV freshness check blocks runner start** (§C2). `news.py` verified < 24 h old before first bar accepted
- [ ] **Clock drift guard:** on startup, assert `abs(local_clock - alpaca_clock) < 2 s`; re-check hourly; drift >2 s → halt new entries
- [ ] ≥ 1 full trading-day dry run against paper account without any safety-stop violation
- [ ] Reconnection reconciliation: `BrokerProtocol.get_positions()` + `get_order(client_order_id)` run on reconnect before resuming entries

## 4. Phase 1 — Detailed Implementation Steps

### 1.1 Repo skeleton + tooling

- Layout per spec: `src/nasdaq_ale_bot/{core,detection,bias,filters,execution,backtest,live}/__init__.py`, `config/`, `tests/{unit,integration,fixtures}/`, `scripts/`
- `pyproject.toml` (PEP 621) — see §6
- `.env.example` with Alpaca placeholders pointing to https://app.alpaca.markets/paper/dashboard/overview
- `.gitignore`: `.env`, `*.env`, `.env.local`, `__pycache__/`, `.pytest_cache/`, `.omc/`, `*.parquet`, `htmlcov/`
- `README.md` stub: setup, `pytest`, backtest entrypoint
- `config/strategy.yaml` with all spec values (risk, killzone, R:R target 1:1.2 / cap 1:1.3 / floor 1:1.1, max stop, daily loss limit)
- `config/instruments.yaml` with `primary` (QQQ) and `correlated` (SPY) blocks — tick, point_value, atr_ratio_vs_nq, session calendar id
- `config/news_events.csv` stub (header + ≥ 1 row so Phase 1 tests run)

### 1.2 Core primitives (`src/nasdaq_ale_bot/core/`)

- **`candle.py`** — `Candle` pydantic model. Fields: `ts: datetime` (tz-aware UTC required, validator rejects naive), `open`, `high`, `low`, `close`, `volume`. Validators: `high >= max(open,close)`, `low <= min(open,close)`.
- **`candle_view.py`** — `CandleView(bars: list[Candle], i: int)`. `__getitem__(k)` raises `LookAheadError` if `k > i`. `__len__` returns `i + 1`. Constructed once per detection call; no slice copies.
- **`liquidity.py`** — `LiquidityLevel(kind, price, ts)`; kinds: PDH, PDL, ASIA_HIGH, ASIA_LOW, LONDON_HIGH, LONDON_LOW, SWING_HIGH, SWING_LOW, MIDNIGHT_OPEN, UNFILLED_4H_FVG.
- **`leg.py`** — `Leg(start_idx: int, end_idx: int, direction: Literal["UP","DOWN"], low: float, high: float)`; used by `equilibrium` and TP scan.
- **`state_machine.py`** — `StrategyState` enum + `StateMachine` class (Phase 1 stub). Module docstring: "state machine is single-threaded; the live runner enqueues bars from the WS thread and a dedicated engine thread is the sole caller of `on_bar` and `safety.flatten`."
- **`smt_tracker.py`** — Phase 1 stub of stateful 1m→5m aggregator; full impl in Phase 2.

### 1.3 Detection layer (`src/nasdaq_ale_bot/detection/`) — pure functions

Every function takes `view: CandleView, i: int` (and any extra config/levels) and returns a typed result. `view` is constructed by the caller as `CandleView(bars, i)`; look-ahead is impossible by construction.

- **`fvg.py`** — `detect_fvg(view, i) -> list[FVG]`. Bullish FVG: `view[i-2].high < view[i].low`. Bearish mirror. Returns all open FVGs touching bar `i`.
- **`sweep.py`** — `detect_sweep(view, i, levels) -> SweepResult`. Wick pierces a level by ≥ `min_penetration_ticks * tick_size`, body closes back inside.
- **`cisd.py`** — **critical.** `detect_bullish_cisd(view, sweep_idx) -> CISDResult` / `detect_bearish_cisd` mirror.
  - **Reference candle selection (§A1):** walk backward from `sweep_idx` as long as bars form a contiguous down-leg (each `candles[k].close < candles[k+1].close`); stop at the first bar that breaks the down-leg. The bar **just before** that break is `reference_candle` (the last up-close before the sweep move). Capped at 20-bar safety bound.
  - **Up-close definition:** strictly `close > open` (doji `close == open` is **not** up-close).
  - **Confirmation scan:** forward from `sweep_idx + 1` up to 15 bars; first `view[j].close > reference_candle.high` → `CISDResult(confirmed=True, ref_idx, confirm_idx=j)`. **Trigger is `close[j]`, never `high[j]`.**
  - Returns `CISDResult(confirmed=False)` otherwise.
- **`ifvg.py`** — `detect_ifvg(view, i, cisd_range: CISDRange) -> list[IFVG]`. Asserts `cisd_range.end <= i`. Bullish setup: bearish FVGs inside `cisd_range` whose gap is body-closed through. 0 IFVGs → reject; 1 → ideal; 2 → pick the one **closer to the sweep level** (tie-break); ≥ 3 → reject.
- **`equilibrium.py`** — `is_in_discount(price: float, leg: Leg) -> bool`, `is_in_premium(price: float, leg: Leg) -> bool` via Fib 0.5 of `leg.low..leg.high`.
- **`smt_pure.py`** — `detect_smt_divergence(primary_5m: list[Candle], correlated_5m: list[Candle], i: int) -> SMTResult`. Pure, stateless, takes **pre-aggregated 5m** series. Parameter names are `primary_*`/`correlated_*` (not `qqq`/`spy`). Missing correlated symbol in config → raises `SMTConfigError` (fail-closed).

### 1.4 Filters (`src/nasdaq_ale_bot/filters/`)

- **`killzone.py`** — `in_primary_killzone(ts_utc)`, `in_secondary_killzone(ts_utc)`. America/New_York via `zoneinfo` with `tzdata` dep. Half-open intervals: [09:30, 11:00) and [13:30, 16:00). Uses `pandas_market_calendars` for early-close days — intervals shrink to the exchange's actual close on those dates. Does **not** depend on `bias/` (no circular import).
- **`news.py`** — `is_news_blackout(ts_utc) -> bool` with ±15 min window. CSV-backed stub reading `config/news_events.csv`. Raises `NewsFeedStale` if file missing or `mtime > 24h`. Phase 4 swaps in a live feed behind the same interface.
- **`trend.py`** — `is_with_trend(bias_direction, setup_direction) -> bool`. Takes the bias as a parameter; does not import `bias/`.

### 1.5 Secrets handling

- `pydantic_settings.BaseSettings` subclass `AlpacaSettings` (`env_prefix="ALPACA_"`, fields `api_key`, `secret_key`, `paper=True`, `base_url`)
- `.env` loading via `python-dotenv`; helpful error if `.env` missing, with URL to paper dashboard
- `structlog` processor chain includes `drop_sensitive` that redacts any key matching `api_key|secret_key|authorization|bearer`
- `test_logging_redaction.py` asserts an `api_key` kwarg to a log call renders as `***`

### 1.6 Tests (`tests/unit/`) — coverage target ≥ 90 % for `detection/`, ≥ 95 % branch for `cisd.py`

- `test_candle_validation.py` — tz-naive rejected, tz-aware accepted, high/low invariants, volume ≥ 0
- `test_candle_view.py` — `view[i]` works, `view[i+1]` raises `LookAheadError`, `len(view) == i+1`
- `test_fvg.py` — bullish, bearish, no-FVG, FVG mitigated by body vs wick
- `test_sweep.py` — 2-tick penetration passes, 1-tick rejected, wrong-side rejected, no-level rejected
- **`test_cisd.py`** — mandated cases:
  - `test_wick_break_not_confirmed` — later candle wick pierces `reference_candle.high`, body `close` below → `confirmed=False`
  - `test_body_close_just_above_confirmed` — `close == reference.high + 1 tick` → `confirmed=True`
  - `test_multiple_up_candles_picks_run_terminator` — three up-closes before sweep; reference = the last up-close before the contiguous down-leg that terminates at the sweep
  - `test_doji_not_up_close` — doji (`close == open`) is not selectable as reference
  - `test_no_up_candle_in_20_bars_returns_unconfirmed`
  - `test_confirmation_timeout_after_15_bars`
  - Bearish mirrors for all of the above
  - `test_cisd_mutation_sentinel` — importlib-reloads `cisd` with `close` monkey-patched to return `high`; asserts `test_wick_break_not_confirmed` now fails (catches the classic bug rewrite)
- `test_ifvg.py` — 0 rejected, 1 accepted, 2 accepted (tie-break: closer to sweep), ≥ 3 rejected, `cisd_range.end > i` raises
- `test_equilibrium.py` — boundary 0.5, inside discount, inside premium
- `test_smt_pure.py` — synthetic primary/correlated 5m arrays with divergence, without, missing correlated config raises `SMTConfigError`
- `test_killzone.py` — 09:29 excluded, 09:30 included, 10:59:59 included, 11:00 excluded, DST March 2024-03-10, DST November 2024-11-03, NYSE early-close 2024-11-29 (13:00 ET close → primary killzone still [09:30, 11:00), secondary collapses)
- `test_news_stub.py` — missing file raises, mtime > 24h raises, valid CSV returns blackout within ±15 min
- `test_logging_redaction.py` — `api_key` kwarg renders as `***`

### 1.7 Optional tooling hook

- `scripts/check.sh`: `ruff check`, `mypy src/nasdaq_ale_bot/detection src/nasdaq_ale_bot/core`, `pytest --cov`

## 5. Phases 2–4 — Outline

**Phase 2 — State Machine + HTF Bias.** Implement `StateMachine.on_bar(bar) -> list[StateEvent]`, HTF bias detector (4H unmitigated FVG body-close breach + subsequent 4H bar confirmation, §A8), swing-point detector (5-bar pivots), DOL builder, `SMTTracker` full impl (1m → clock-anchored 5m, latches 5m verdicts, per §A12). `structlog` JSON on every transition. Integration test on ≥ 5-day `qqq_1m_sample.csv` fixture. Session rotation and AM→PM gating live here.

**Phase 3 — Backtest Engine.** `data_loader.py` fetches pinned QQQ + SPY 1m bars `2024-01-01..2024-06-30` and caches parquet with SHA-256 recorded in `tests/fixtures/data_hashes.json`. **Walk-forward split:** IS `2024-01-01..2024-04-30` (4 months, for tuning), OOS `2024-05-01..2024-06-30` (2 months, reported once per parameter set, no feedback into tuning). `engine.py` replays bar-by-bar, injecting `CandleView`s into `StateMachine`. `metrics.py` computes WR/R/MaxDD/PF/Sharpe/trades-per-day/avg-hold, separately for IS and OOS. Fill-model ADR implemented (SL wins on intrabar collision). HTML report with IS and OOS sections clearly separated. **Phase 3.5** cross-check against `vectorbt` on SMA-cross stand-in. **Golden-ledger test** freezes expected trades JSON on the full 6-month window. **Grid harness** sweeps `(min_penetration_ticks, cisd_lookback, cisd_confirm_window)` on **IS only**. WR < 55 % on OOS → ultraqa loop, first suspect CISD body-close bug, window lengths, or IS overfit.

**Phase 3.x pre-wiring for Phase 5.** Backtest engine consumes `AccountLedger` updates exactly like live (§A25-A30). Add `scripts/backtest_apex.py` that replays any pinned window with `apex_mode.enabled=true` and reports Apex-rule breaches (daily loss, trailing DD, consistency violation, profit target hit date). No changes to core engine — pure wrapper around existing replay.

**Phase 4 — Paper Live Runner.** `alpaca_client.py` wraps `TradingClient` + `StockDataStream` behind `BrokerProtocol`. WS thread enqueues to `queue.Queue`; a dedicated engine thread drains the queue and is the only caller of `StateMachine.on_bar`. `order_manager.py` uses bracket orders (atomic SL/TP); BE moves via `BrokerProtocol.modify_bracket_stop`. `risk.py` enforces position sizing from `instruments.yaml` ATR ratio. `safety.py` owns kill switches including news CSV freshness check on startup and clock-drift guard. `runner.py` is the main loop. ≥ 1 full trading-day dry run.

---

## 5.A Phase 5 — Apex Trader Funded Compliance Layer

**Goal:** Run the bot on an Apex 50k Evaluation Account on **MNQ futures** (Tradovate or Rithmic broker) without code changes to detection or state machine — only activation of the `apex_mode` config block, addition of Apex-specific gates to `GateList`, and a `TradovateBroker` implementation of `BrokerProtocol`.

**Chosen approach (see ADR §9.A):** `AccountLedger` + `GateList` composition pattern with futures-capable `BrokerProtocol`. Apex rules live behind a config flag; all pre-wiring happens in Phase 2. No refactor required at Phase 5.

### Acceptance Criteria — Phase 5

- [ ] `config/strategy.yaml::apex_mode.enabled=true` activates all six Apex gates without touching Python source
- [ ] **A25 Daily Loss Cap:** `DailyLossGate` blocks new entries when `projected_pnl <= apex_mode.daily_loss_usd` (default −1000). Realized breach → immediate `flatten` + session halt. Replaces (does not supplement) the base −$1500 gate when `apex_mode.enabled`
- [ ] **A26 Trailing Drawdown:** `TrailingDDGate` continuously tracks `high_watermark_equity` (updated on every equity snapshot, not just EOD) and blocks entries when `current_equity <= high_watermark_equity - apex_mode.trailing_dd_usd` (default 2500). Hard breach → flatten + permanent account-dead state persisted to `.omc/state/apex_account_state.json`
- [ ] **A27 Profit Target + 30-Day Clock:** `ProfitTargetGate` logs (not blocks) when `cumulative_profit >= apex_mode.profit_target_usd` (default 3000). Emits `APEX_TARGET_HIT` with days-elapsed-since-start. `profit_window_start_date` persisted; gate raises `ApexWindowExpired` when `today - start > 30 calendar days` and target not met
- [ ] **A28 Consistency Rule:** `ConsistencyGate` blocks new entries on day N if `best_day_profit / cumulative_profit > 0.30` AND `cumulative_profit >= 0.5 * profit_target_usd`. Below the 50% threshold the rule is dormant (per Apex spec — consistency enforced only near payout)
- [ ] **A29 Scaling Plan:** `ScalingPlanGate` reads `apex_mode.scaling_plan` (map of `account_balance_threshold -> max_contracts`) and clamps `intent.qty` before submission. Unit tests cover the 50k starting contract limit (2 MNQ) and automatic step-up on balance growth
- [ ] **A30 MNQ Instrument:** `instruments.yaml::mnq` block with `tick=0.25`, `tick_value_usd=0.50`, `contract_size=2`, `rth_session=CME_GLOBEX`. `TradovateBroker` implements `BrokerProtocol` (all 14 methods). SL sizing uses MNQ ticks directly; no QQQ→NQ ATR scaling applied when primary symbol is MNQ
- [ ] `tests/integration/test_apex_compliance.py` replays a synthetic 35-day fixture: hits each gate type at least once, asserts correct log events, asserts no gate fires when `apex_mode.enabled=false`
- [ ] `scripts/backtest_apex.py` runs Phase 3 pinned data under `apex_mode` and produces a compliance report (breach timeline, HWM curve, best-day distribution)
- [ ] **Fail-closed:** any `AccountLedger` field transitioning to `None` or a stale equity snapshot (> 60 s old in live mode) blocks new entries (`SKIP_APEX_STATE_UNAVAILABLE`)
- [ ] Zero changes required to `src/nasdaq_ale_bot/detection/**` and `core/state_machine.py` between Phase 4 and Phase 5 (enforced by `git diff` check in CI)

---

## 5.B Phase 6 — Desktop GUI + Windows Auto-Start

**Goal:** A Windows desktop application that shows bot state, equity, open positions, recent trades, and Apex compliance status without touching the running bot process. Auto-starts on Windows boot via a supervisor.

**Chosen approach (see ADR §9.B):** PyQt6 GUI + JSONL append-only event tail (IPC via file, not socket). Two-process architecture — `bot_launcher.exe` supervises both `bot.exe` (trading engine) and `bot_gui.exe` (display). GUI is strictly **read-only** on `.omc/state/bot_events.jsonl`; a bot crash does not take the GUI down, and a GUI crash does not interrupt trading.

### Acceptance Criteria — Phase 6

- [ ] **A31 Process Architecture:** `bot_launcher` (supervisor) spawns `bot` and `bot_gui` as independent OS processes with separate PIDs. Supervisor restarts `bot_gui` on crash (up to 3×/minute, then cool down) but **never** restarts `bot` automatically — trading crashes require human acknowledgment
- [ ] **A32 IPC Mechanism:** GUI tails `.omc/state/bot_events.jsonl` via `watchdog` filesystem events + incremental `seek`. No shared memory, no sockets, no pipes. Bot process never reads back from the file; GUI never writes
- [ ] **A33 Windows Auto-Start:** `scripts/install_autostart.py` creates a `.lnk` shortcut to `bot_launcher.exe` in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`. Uninstall script removes it. No registry writes, no admin rights needed
- [ ] **A34 Crash Isolation:** integration test kills `bot_gui` mid-session → `bot` continues trading uninterrupted; kills `bot` mid-session → `bot_gui` displays `BOT_OFFLINE` banner and freezes last state, does not crash
- [ ] **A35 Event Sink Schema (Phase 2 prerequisite):** `core/logging_sink.py` writes v1 schema `{ts_utc, level, event, state, bar_ts, fields}` to `.omc/state/bot_events.jsonl`. Rotation at 50 MB, max 5 backups. Schema version field allows forward-compatible GUI upgrades
- [ ] **A36 GUI Panels:** state/engine, equity curve (from `AccountLedger` snapshots), open positions, last 20 trades, Apex compliance status (HWM, trailing DD headroom, consistency ratio, profit target progress, days remaining). Manual refresh button + 1 Hz auto-refresh
- [ ] GUI package lives in `src/nasdaq_ale_bot_gui/` (separate top-level package — no imports from `nasdaq_ale_bot` runtime code; only `from nasdaq_ale_bot.core.logging_sink import EVENT_SCHEMA_V1` for the schema constant)
- [ ] `pyinstaller` produces three standalone `.exe`s: `bot.exe`, `bot_gui.exe`, `bot_launcher.exe`. No Python install required on target machine
- [ ] ≥ 1 full trading-day dry run with GUI open, bot in paper mode, zero GUI→bot interference

---

## 6. `pyproject.toml` Dependency Spec

```toml
[project]
name = "nasdaq-ale-bot"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "alpaca-py>=0.33,<1.0",
  "pandas>=2.2,<3.0",
  "numpy>=1.26,<3.0",
  "pydantic>=2.6,<3.0",
  "pydantic-settings>=2.2,<3.0",
  "structlog>=24.1,<25.0",
  "pyyaml>=6.0,<7.0",
  "python-dotenv>=1.0,<2.0",
  "pyarrow>=15.0,<20.0",                # parquet caching
  "pandas_market_calendars>=4.4,<5.0",  # NYSE early-close handling
  "tzdata>=2024.1",                      # zoneinfo on Windows
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0,<9.0",
  "pytest-cov>=5.0,<6.0",
  "pytest-asyncio>=0.23,<1.0",
  "hypothesis>=6.100,<7.0",   # property-based CISD fuzz (optional)
  "ruff>=0.4,<1.0",
  "mypy>=1.10,<2.0",
  "types-pyyaml>=6.0",
]
crosscheck = [
  "vectorbt>=0.26,<1.0",      # Phase 3.5 metrics cross-check only
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.coverage.run]
source = ["src/nasdaq_ale_bot"]
branch = true

[tool.coverage.report]
fail_under = 90

[tool.ruff]
line-length = 100
target-version = "py311"
```

## 7. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| CISD wick/body bug | Silent WR collapse | 3 mandated tests + bearish mirrors + **mutation sentinel test** + `CandleView`-enforced reads |
| CISD window magic numbers (A1 20, A2 15) wrong | WR degrades without obvious bug | Phase 3 **grid harness** `scripts/grid.py` sweeps both |
| Look-ahead leak | Inflated backtest | `CandleView` raises `LookAheadError` at runtime; no convention-only checks |
| DST transition / early-close off-by-one | Killzone drift | `zoneinfo` + `pandas_market_calendars` + explicit DST + Thanksgiving tests |
| QQQ↔NQ point-value mis-scaling | Wrong position size | `instruments.yaml` only; `position_size` unit test for both instruments |
| QQQ/SPY bar mis-alignment | Spurious SMT signals | `SMTTracker` inner-joins on 1m ts, forward-fills max 1 bar, emits UNAVAILABLE verdict on ≥ 2 missing; UNAVAILABLE **fail-closes** entries (§A13) |
| Alpaca WS disconnect | Unmanaged position | `safety.py` cancels all + flattens; reconnection runs `get_positions` + `get_order` reconciliation |
| Stale news CSV | FOMC trades slip through | `NewsFeedStale` raises on missing or > 24 h CSV; Phase 4 runner refuses to start |
| Clock drift local↔Alpaca | Killzone boundary errors | Startup + hourly drift check, > 2 s halts new entries |
| Intrabar SL+TP collision | Ambiguous backtest fills | **Fill-model ADR: SL wins.** Test pinned. |
| Metrics library bugs | Wrong WR/DD numbers | **Phase 3.5** cross-check against `vectorbt` on SMA-cross toy strategy |
| Secret leak via logs | Credential exposure | `structlog drop_sensitive` processor + unit test + `.gitignore` |
| Over-trading >2/day | Risk-cap breach | Hard counter in `runner.py`, independent of strategy signals |
| Refactor silently drifts trades | Regressions in WR | **Golden-ledger** hash-compared JSON in CI |

## 8. Verification Steps

1. `pip install -e .[dev]` succeeds on Python 3.11 and 3.12 (Windows + Linux).
2. `pytest -q` — all unit tests pass.
3. `pytest --cov=src/nasdaq_ale_bot/detection --cov-branch --cov-report=term-missing` ≥ 90 %; `cisd.py` branch ≥ 95 %.
4. `ruff check src tests` clean.
5. `mypy src/nasdaq_ale_bot/core src/nasdaq_ale_bot/detection` clean.
6. **Mutation sentinel:** `pytest tests/unit/test_cisd.py::test_cisd_mutation_sentinel` passes (human-reviewed gate, documented as such).
7. `rg "api_key|secret_key|authorization|bearer|ALPACA_" src tests` → only references in `settings.py` and redaction test.
8. `python -c "from nasdaq_ale_bot.core.candle_view import CandleView, LookAheadError; v=CandleView([1,2,3], 1); v[2]"` → raises `LookAheadError` (one-liner spot check).
9. Phase 3: `sha256sum tests/fixtures/*.parquet` matches `tests/fixtures/data_hashes.json`.
10. Phase 3.5: `python scripts/metrics_crosscheck.py` reports WR/DD/Sharpe agreement within 1 % against `vectorbt`.

## 9. ADR — Consensus Decisions

### Decision
Implement a custom, pure-function detection layer with **runtime-enforced look-ahead ban** (`CandleView`), feeding a single-threaded stateful 6-state strategy machine shared between a custom bar-by-bar backtest engine and an Alpaca paper live runner. Primary/correlated symbols are config-driven (QQQ + SPY today, MNQ + ES tomorrow). BE/SL/TP evaluation is **server-side** via Alpaca bracket orders; local code receives closed 1m bars only. Intrabar SL+TP collision resolves **SL-wins**. Phase 3.5 cross-checks `metrics.py` against `vectorbt` on a toy strategy.

### Drivers
Correctness of CISD (body-close semantics, runtime-enforced purity), reproducibility across backtest/live (golden-ledger test), safety of the live runner under disconnect / stale feeds / clock drift.

### Alternatives considered
**(B)** `backtesting.py` / `vectorbt` — rejected as production path: forces stateful logic into vectorized idioms and hides look-ahead bugs. Retained as Phase 3.5 **cross-check** only. **(C)** Vectorized pandas indicators — rejected: CISD's backward scan for the "contiguous-run terminator" is not cleanly vectorizable.

### Why chosen
Pure functions with `CandleView` make look-ahead impossible by construction rather than by convention. One `on_bar` code path serves backtest and live. The mandated CISD test cases can be written before any broker code exists. `vectorbt` cross-check buys correctness assurance on metrics without adopting the library for the strategy itself.

### Consequences
More code upfront (~200 LOC `metrics.py`, `CandleView`, `SMTTracker`). Broker protocol (§A20) grows to 9 methods to enable reconciliation, BE moves, and calendar queries. Offset: zero look-ahead risk, deterministic reproduction, trivial MNQ swap.

### Follow-ups
Live news feed source (Phase 4). Real-time bar latency characterization on Alpaca paper. Optional `hypothesis` property fuzz for CISD once baseline is green.

---

### 9.A ADR — Phase 5 Apex Compliance

**Decision.** Add Apex-specific constraints via an **`AccountLedger` primitive + `GateList` composition pattern** behind a `config/strategy.yaml::apex_mode` flag (default `enabled: false`). Pre-wire all primitives in Phase 2 (ledger, gate interface, extended BrokerProtocol, instruments.yaml futures schema) so that Phase 5 is a configuration activation + `TradovateBroker` implementation — not a refactor.

**Drivers.** (1) Apex rules are **entry gates**, not signal detection — they belong in the execution layer alongside existing daily-loss and killzone checks. (2) MNQ vs QQQ is an instrument swap, which the existing `instruments.yaml` + 9-method BrokerProtocol already anticipates — needs only 5 futures-specific methods added. (3) Trailing drawdown needs continuous equity tracking, which the state machine does not currently do — `AccountLedger` provides it in one place and backtest and live both feed it.

**Alternatives considered.**
- **B — Fork a `strategy_apex.py` state machine.** Rejected: duplicates detection/state logic, guarantees drift, violates Principle 5 (mechanical over ad-hoc).
- **C — Inline Apex checks into `runner.py`.** Rejected: not testable in isolation, couples live runner to rule specifics, no clean backtest path.

**Why chosen.** `GateList` is already the shape of the existing entry gates (daily loss, news, killzone). Adding Apex as `list += apex_gates` is the minimum delta. `AccountLedger` lets the backtest engine produce the same equity telemetry as live, so Phase 3 backtests can validate Apex rules before any capital is at risk.

**Consequences.** Phase 2 gains ~400 LOC upfront (ledger, gates, broker protocol extension, sink). Phase 5 becomes a thin layer: 6 gate classes (~200 LOC), 1 broker adapter (~600 LOC for Tradovate), 1 config block. Backtest-driven Apex rule validation becomes possible without live execution.

**Follow-ups.** Tradovate vs Rithmic choice deferred to start of Phase 5 (prototype both `BrokerProtocol` implementations on a reduced method surface first). Real-time equity polling cadence (currently spec'd at ≥ 1 Hz) to be confirmed against broker API rate limits.

---

### 9.B ADR — Phase 6 Desktop GUI

**Decision.** **PyQt6** GUI reading from an **append-only JSONL event file** at `.omc/state/bot_events.jsonl`, supervised by a minimal `bot_launcher` process that spawns `bot` and `bot_gui` as independent children. **Windows Startup folder `.lnk`** for auto-start (no registry, no service).

**Drivers.** (1) **Crash isolation is non-negotiable** — a GUI hang or crash must not affect live trading. File-based IPC guarantees this: bot writes, GUI reads, no shared runtime. (2) **Observability without coupling** — the GUI should never be able to send commands back to the bot (read-only by construction). (3) **Windows-first deployment** — Startup folder is the simplest mechanism that survives reboots without admin rights.

**Alternatives considered.**
- **B — Embedded GUI (same process, Qt event loop alongside engine thread).** Rejected: violates A21 single-threaded state machine contract, GUI hangs block trading.
- **C — HTTP/WebSocket server inside bot, browser GUI.** Rejected: network surface area, auth complexity, and couples bot uptime to HTTP stack. Over-engineered for single-user local app.
- **D — Windows Service.** Rejected: requires admin install, harder to debug, GUI cannot easily render in the user session.

**Why chosen.** JSONL tail is the simplest IPC with the strongest isolation property. `watchdog` filesystem events give ~100 ms latency, well below the 1 Hz GUI refresh rate. PyQt6 is mature, ships good native Windows widgets, and packages cleanly with PyInstaller. Startup folder matches the "single-user local tool" deployment model.

**Consequences.** Phase 2 must ship the `logging_sink.py` JSONL writer with a frozen schema v1 — a change later would break the GUI. Event volume is bounded by trading activity (~100 events/day expected), so rotation at 50 MB gives months of history. GUI package ships as a separate top-level directory (`src/nasdaq_ale_bot_gui/`) to enforce the one-way dependency.

**Follow-ups.** Schema v2 upgrade path (GUI reads `schema_version` field and refuses unknown versions). Optional: system tray icon and Windows toast notifications on critical events (Apex breach, flatten). Linux/Mac ports out of scope for Phase 6.

## 10. Changelog

- **v1** — initial draft.
- **v2.1** — pre-Phase-1 user clarifications:
  - A6 SL inputs explicitly sourced from 1m execution timeframe (no multi-TF mixing).
  - A8 bias flip now requires explicit 1H (HH+HL structure) + Daily (body-close direction) confirmation before activation; pending vs active states logged.
  - A15 new-entry gate considers unrealized PnL via `projected_pnl = realized + unrealized - risk_per_trade`; skip logged as `SKIP_PROJECTED_LOSS_LIMIT`.
  - A7 logs `natural_rr_at_signal` (uncapped) and `executed_rr` (post-cap) on every intent.
  - Phase 3 walk-forward split added: IS `2024-01-01..2024-04-30`, OOS `2024-05-01..2024-06-30`; tuning on IS only, OOS run once per parameter set, WR target is OOS-gated.
- **v2 (post-consensus)** — merged Architect + Critic feedback:
  - Rewrote A1 CISD reference-candle selection to "contiguous directional run terminator"; added A1b bearish mirror; explicit doji rule.
  - Introduced `CandleView` runtime look-ahead enforcement; Phase 1 AC updated.
  - Renamed SMT params to `primary_*` / `correlated_*`; added `correlated_symbol` to `instruments.yaml`.
  - Split SMT into `detection/smt_pure.py` (stateless) + `core/smt_tracker.py` (stateful 1m→5m).
  - A13 SMT UNAVAILABLE now **fail-closes** entries.
  - A17 BE-on-touch flagged as **explicit exemption** from Principle 3 body-close semantics.
  - `ifvg.detect_ifvg` signature now takes `view, i, cisd_range` with `cisd_range.end <= i` assertion.
  - NYSE early-close handling via `pandas_market_calendars`; Thanksgiving 2024-11-29 test added.
  - BrokerProtocol extended to 9 methods (§A20).
  - Single-threaded state-machine contract documented (`state_machine.py` docstring + Phase 4 WS → queue → engine-thread actor).
  - A19 idempotency hash now `sha1(bar_ts_iso | direction | strategy_version)`.
  - Fill-model ADR: SL wins on intrabar collision.
  - Golden-ledger integration test.
  - Parameter grid harness `scripts/grid.py`.
  - Phase 3.5 `vectorbt` metrics cross-check.
  - `cisd.py` branch coverage ≥ 95 % per-file gate; **mutation sentinel test** replaces grep check.
  - `Candle` validator tests added.
  - News CSV freshness check (`NewsFeedStale`) added; Phase 4 runner refuses to start on stale CSV.
  - AM → PM killzone state, session rotation, `SKIP_MAX_STOP` ACs added to Phase 2.
  - Backtest data pinned to `2024-01-01..2024-06-30`, SHA-256 recorded in fixtures.
  - Clock-drift guard (±2 s) added as Phase 4 AC and §7 risk.
  - Added `pandas_market_calendars` and `hypothesis` deps.
