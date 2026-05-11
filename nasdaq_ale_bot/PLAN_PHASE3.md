# PLAN_PHASE3.md — Phase 3 Backtest Engine (v2, APPROVED-AFTER-ITERATION)

**Status:** APPROVED-AFTER-ITERATION (awaiting user "go" before Phase 3 coding)
**Basis:** PLAN.md v3 Phase 3 + ASSUMPTIONS.md A1-A36
**Budget:** 2500-4000 LOC (source + tests + fixtures)
**Phase 2 prerequisite:** complete (242 tests passing, 97% branch cov, ~3650 LOC)
**Phase 2 artifacts consumed:** `AccountLedger`, `GateList` (7 base gates), `JsonlSink` (schema v1), `BrokerProtocol` (14 methods at `broker.py:168`), `StateMachine.on_bar()`, `HTFBiasDetector`, `SMTTracker`, `CandleView`

---

## 1. Scope & Non-Goals

### In Scope (Phase 3 ships)

1. **Historical data pipeline** — `scripts/fetch_phase3_data.py` downloads QQQ + SPY 1-minute bars for 2024-01-01 through 2024-06-30 via the Alpaca historical data API, writes gzip'd Parquet to `data/historical/`, and verifies SHA-256 manifest integrity on every run.
2. **MockBroker** — `execution/mock_broker.py` implements all 14 `BrokerProtocol` methods (`BROKER_PROTOCOL_METHODS` at `broker.py:168`). Trading methods drive fill simulation with the "SL wins" fill model (A22). Read methods return internal state. Calendar/stream methods are no-ops or trivial stubs since BacktestRunner provides the bar stream.
3. **BacktestRunner** — `backtest/runner.py` replays bar-by-bar over `CandleView`, calls `StateMachine.on_bar()`, uses MockBroker for order simulation. No real broker connection.
4. **GridHarness** — `backtest/grid.py` sweeps a 3-axis parameter grid (27 combinations) on the In-Sample window, ranks by composite score, selects Top-3 parameter sets.
5. **Walk-Forward controller** — `backtest/walk_forward.py` enforces IS/OOS split with single-shot OOS evaluation. Raises `OOSAlreadyRunError` if OOS is invoked twice with the same `param_set_hash`.
6. **Metrics engine** — `backtest/metrics.py` computes WR, Avg R:R, Max DD, Profit Factor, Sharpe, trades/day, avg hold time from `AccountLedger` event streams.
7. **vectorbt crosscheck** — `scripts/metrics_crosscheck.py` runs a simple SMA-cross stand-in strategy through both the custom engine and vectorbt; asserts WR/MaxDD/Sharpe agree within 1% (absolute).
8. **Golden Ledger** — `tests/fixtures/golden_ledger_2024_01.json` pinned fixture for January 2024 IS window; Decimal-exact comparison against BacktestRunner output.
9. **OOS gate** — `results/phase3_oos_verdict.json` artifact with pass/fail flag at WR >= 55% threshold.

### Explicitly NOT in Scope

- **Phase 4 live runner** — no WebSocket, no Alpaca real-time, no `alpaca_client.py`.
- **Phase 5 Apex gates** — MockBroker does not implement Apex-specific logic. `AccountLedger` is exercised identically to how Phase 5 will consume it, but no Apex gates are attached to `GateList`.
- **Phase 6 GUI** — no PyQt6, no desktop app. `JsonlSink` continues writing schema v1 events during backtest runs.
- **HTML report** — deferred to a post-Phase-3 polish pass. Phase 3 outputs JSON artifacts and DataFrames; visualization is not a gate.
- **Modifying `src/nasdaq_ale_bot/detection/**`** — pure detection layer is frozen at Phase 1.
- **Modifying `core/state_machine.py` internals** — BacktestRunner calls `on_bar()` exactly as the live runner will.
- **Modifying `core/candle.py` or `core/candle_view.py`** — Phase 1 invariants stay.

---

## 2. RALPLAN-DR Summary

### 2.1 Principles (5)

1. **No look-ahead by construction.** BacktestRunner constructs `CandleView(bars, i)` once per bar. `CandleView.__getitem__` raises `LookAheadError` on `k > i` (A24). Fill-timestamp semantics: brackets armed on bar N's close fill against bar N+1's range at bar N+1's `close_ts`. No bar ever sees its own fill.
2. **One code path, two brokers.** `StateMachine.on_bar()` is the sole entry point for both backtest and live. MockBroker implements the same `BrokerProtocol` (14 methods) as the future `AlpacaBroker`. The state machine cannot distinguish backtest from live.
3. **Conservative fill model.** When a bar's [low, high] range contains both stop and take-profit, stop fills first ("SL wins", A22). Without tick-level data, the risk-sided assumption is the only honest choice.
4. **OOS is sacred.** The walk-forward controller enforces exactly-once evaluation on the OOS window per parameter set. No iterative tuning on OOS data. `param_set_hash` encodes both parameter values and code-version fingerprint.
5. **Decimal exactness for money.** All PnL, equity, and fill values flow through `AccountLedger` as `Decimal`. The Golden Ledger test asserts Decimal-exact equality, not floating-point tolerance. MockBroker uses `OrderFillEvent.from_floats` as the sole float-to-Decimal boundary.

### 2.2 Decision Drivers (top 3)

1. **D1: Backtest reproducibility** — identical bar stream and parameter set must produce identical trade ledger. Golden Ledger test is the CI gate. Any Decimal drift, fill-ordering ambiguity, or non-deterministic path breaks this invariant.
2. **D2: OOS integrity** — the WR >= 55% gate on OOS data is the sole go/no-go signal for Phase 4. If OOS is contaminated by iterative tuning, the gate is meaningless and live trading is exposed to overfitted parameters.
3. **D3: MockBroker fidelity** — MockBroker must behave identically to BrokerProtocol for the methods that affect trade outcomes (place_bracket, modify_bracket_stop, flatten). Fill simulation must be conservative (SL wins) so backtest WR is a floor, not a ceiling, relative to live.

### 2.3 Viable Options (>= 2, with bounded pros/cons and invalidation rationale)

**Option A — Custom bar-by-bar replay engine with MockBroker (CHOSEN).**
- Pros: full control over fill model, injectable synthetic fixtures, CandleView look-ahead enforcement works unchanged, one `on_bar` code path shared with live, explicit state transitions visible in logs, Decimal-exact accounting via existing `AccountLedger`.
- Cons: more code than wrapping vectorbt (~400 LOC runner + ~450 LOC MockBroker). Requires separate metrics cross-check against a known-good library.
- Risk: Custom metrics may have subtle bugs (mitigated by vectorbt cross-check on SMA-cross stand-in).

**Option B — vectorbt as the primary backtest engine.**
- Pros: battle-tested metrics, built-in equity curve, less custom code for the engine itself.
- Cons: forces the 6-state CISD/IFVG/sweep detection into vectorized `Strategy` classes, which obscures the stateful backward scans; look-ahead bugs become convention-only (no `CandleView` enforcement); fill model control is limited (no "SL wins" without a custom fork); `AccountLedger` integration requires an adapter layer that negates code savings.
- **Invalidation:** PLAN.md explicitly forbids `backtesting.py` as the production path and retains vectorbt only for cross-check (PLAN.md lines 42-43). The 6-state machine's stateful nature (CISD backward scan, SMT cross-instrument clock) makes vectorization actively harmful to correctness.

**Option C — Hybrid: vectorbt for metrics only, custom engine for replay.**
- Essentially the same as Option A with a tighter vectorbt integration for computing metrics. Invalidated because metric computation is ~200 LOC and the cross-check on a stand-in strategy gives equivalent confidence without coupling the production metrics path to vectorbt's API surface. The stand-in approach is strictly less coupled.

**Chosen approach:** Option A. Custom bar-by-bar replay with MockBroker, verified by a vectorbt cross-check on a separate SMA-cross strategy.

### 2.4 Pre-Mortem (3 scenarios — deliberate mode)

#### Scenario 1: Data integrity drift — Alpaca returns different bars on re-fetch

**Trigger:** Alpaca applies a corporate action adjustment or bar correction to QQQ historical data between two CI runs. The re-fetched parquet files produce different bar values, breaking Golden Ledger Decimal-exact assertions and invalidating parameter grid results.

**Blast radius:** CI goes red on `test_golden_ledger.py`. If the SHA-256 manifest check is bypassed or weakened (e.g., developer deletes manifest to "fix" CI), all backtest results become silently unreproducible. Worse: parameter grid results computed on old data are compared against OOS results on new data, corrupting the IS/OOS split semantics.

**Detection:** `scripts/fetch_phase3_data.py` writes `data/historical/manifest.json` with SHA-256 per file on first fetch. Every subsequent run re-computes the hash and compares. Mismatch raises `DataIntegrityError` with the file name, expected hash, and actual hash. CI runs the fetch script and the hash check as a prerequisite to any backtest test. The manifest file is committed to git; the parquet files are `.gitignore`d.

**Mitigation in plan:**
- Step 1 embeds SHA-256 verification as a non-bypassable first step in `fetch_phase3_data.py`. The script exits with code 2 (distinct from general errors) on hash mismatch, so CI can distinguish "data drifted" from "Alpaca API down."
- The manifest includes `fetch_timestamp_utc` and `alpaca_api_version` fields for forensics.
- Recovery procedure documented: if bars legitimately change, the operator must (a) re-run the grid harness on IS, (b) re-generate the Golden Ledger fixture, (c) update `manifest.json` hashes — a deliberate 3-step process that prevents silent acceptance.

#### Scenario 2: OOS contamination — developer tweaks code between IS grid run and OOS evaluation

**Trigger:** A developer runs the grid harness on IS, sees a promising parameter set, then modifies a detection function (e.g., adjusts CISD lookback logic) before running the OOS evaluation. The OOS results reflect code that was never validated on IS, making the IS/OOS comparison meaningless.

**Blast radius:** Phase 4 ships with parameters that passed OOS under different code than the IS grid, silently undermining the walk-forward validation. The WR >= 55% gate is met by coincidence, not by evidence.

**Detection:** The `param_set_hash` computed by `GridHarness` includes a code-version fingerprint: `sha1(strategy_version + frozen_param_json)` where `strategy_version` is `core/__init__.py::STRATEGY_VERSION` (a constant bumped on any detection/state-machine rule change). The walk-forward controller stores the `param_set_hash` used during IS. When OOS is invoked, the controller recomputes `param_set_hash` from the current code + params. If it differs from the IS hash, `OOSCodeVersionMismatchError` is raised, blocking the OOS run.

**Mitigation in plan:**
- Step 8 (walk-forward controller) enforces `param_set_hash` identity between IS and OOS runs.
- `STRATEGY_VERSION` is not a git SHA (those change on any commit, including test-only changes); it is a manually-bumped constant specifically tied to rule changes. This prevents false positives from non-functional commits.
- The OOS verdict artifact includes the `param_set_hash` for audit.

#### Scenario 3: Fill-model edge cases — stop and TP both touched in first bar after entry; gap-open past SL

**Trigger (3a — same-bar collision):** Entry is armed on bar N's close. Bar N+1's [low, high] contains both stop and take-profit. "SL wins" applies: stop fills at `stop_price`, take-profit is cancelled. But what if bar N+1 is also the entry bar (i.e., entry fills at bar N+1's open, and SL/TP are both within bar N+1's range)? The fill sequence becomes: entry fill -> SL fill, all at bar N+1's `close_ts`. The position is opened and closed in the same bar.

**Trigger (3b — gap-open past SL):** Bar N+1 opens beyond the stop price (gap). The fill should be at `bar_open`, not at `stop_price`, because the stop is a stop-market order and the first available price is the open. MockBroker must simulate gap fills at the opening price, not at the stop level.

**Trigger (3c — partial fills):** MockBroker is designed for simple, all-or-nothing fills. No partial fill semantics. If the future ever needs partial fills, MockBroker's design must be explicitly extended, not silently broken.

**Blast radius:** Incorrect fill prices bias backtest PnL. Gap fills at `stop_price` instead of `bar_open` understate losses, inflating backtest WR. Same-bar entry + exit without correct sequencing may double-count or miss the trade entirely.

**Detection:** Unit tests pin each scenario:
- `test_sl_wins_when_both_hit` — bar contains both SL and TP; SL fills first; position closed at SL price.
- `test_gap_open_past_sl_fills_at_open` — bar opens below (long) or above (short) SL; fill price = bar open, not SL.
- `test_entry_and_exit_same_bar` — entry armed on bar N close, bar N+1 touches both entry fill zone and SL; entry fills at open, SL fills at SL price (or open if gap), trade is recorded as a complete round-trip.
- `test_no_partial_fills` — MockBroker always fills full quantity; `filled_qty == intent.qty`.

**Mitigation in plan:**
- Step 2 (MockBroker) explicitly documents fill priority order: (1) check if entry order fills at bar open, (2) check SL against bar range (gap-fill at open if needed), (3) check TP against bar range (only if SL did not fire), (4) report fill events to `AccountLedger`.
- MockBroker constructor takes a `fill_model: Literal["sl_wins"] = "sl_wins"` parameter for future extensibility without modifying existing code.

### 2.5 Expanded Test Plan (unit / integration / e2e / observability)

#### Unit Tests

| Module | Test file | Key test cases | Coverage target |
|---|---|---|---|
| `execution/mock_broker.py` | `tests/unit/test_mock_broker.py` | 14-method protocol conformance, `isinstance(MockBroker(), BrokerProtocol)`, fill ordering ("SL wins"), gap-open fill at bar open, entry+exit same bar, no partial fills, position tracking, equity computation, `get_positions` returns current state, `get_account_equity` matches ledger, calendar/stream methods are no-ops, `modify_bracket_stop` updates pending stop | **100%** |
| `backtest/runner.py` | `tests/unit/test_backtest_runner.py` | Bar iteration + CandleView construction, fill-timestamp semantics (bar N arm -> bar N+1 fill), no look-ahead (CandleView horizon == current index), session rotation propagation, ledger Decimal integrity after replay, empty bar list edge case, single-bar edge case, multi-day replay state continuity | **>= 90%** |
| `backtest/grid.py` | `tests/unit/test_grid_harness.py` | Param enumeration (27 sets from 3x3x3), composite score ranking formula (WR*0.5 + PF_norm*0.3 + MaxDD_norm*-0.2), Top-3 selection, deterministic ordering on score ties, `param_set_hash` computation includes `STRATEGY_VERSION`, grid on empty bars returns empty DataFrame | **>= 85%** |
| `backtest/walk_forward.py` | `tests/unit/test_walk_forward.py` | IS window read/write access, OOS single-shot guard (`OOSAlreadyRunError` on second invocation with same hash), `OOSCodeVersionMismatchError` when code version changes between IS and OOS, IS/OOS date boundary correctness (IS ends 2024-04-30, OOS starts 2024-05-01), param_set_hash determinism | **>= 90%** |
| `backtest/metrics.py` | `tests/unit/test_metrics.py` | WR on known trade list, Avg R:R with mixed wins/losses, Max DD computation on synthetic equity curve, Profit Factor edge case (zero losses -> infinity capped), Sharpe on flat equity (zero std -> 0.0), trades/day normalization by trading days not calendar days, avg hold time in minutes | **>= 90%** |

#### Integration Tests

| Test file | What it proves |
|---|---|
| `tests/integration/test_phase3_e2e.py` | Full QQQ+SPY 6-month replay end-to-end through BacktestRunner -> MockBroker -> AccountLedger. Asserts >= 1 completed trade lifecycle. Asserts all fills are Decimal-exact. Asserts no `LookAheadError` raised. Asserts `JsonlSink` wrote schema-valid events for every fill/skip/flatten. |
| `tests/integration/test_golden_ledger.py` | Loads `tests/fixtures/golden_ledger_2024_01.json`. Replays January 2024 IS window. Compares trade-by-trade output against fixture: `entry_price`, `exit_price`, `realized_pnl`, `fill_ts` must be Decimal-exact. Any drift fails CI. |
| `tests/integration/test_grid_harness_mini.py` | Runs GridHarness on a 1-week mini-fixture (5 trading days). Asserts 27 param sets evaluated. Asserts Top-3 returned. Asserts composite score formula applied correctly. Validates that results DataFrame has expected columns and types. |

#### E2E Tests

| Test / script | What it proves |
|---|---|
| `scripts/fetch_phase3_data.py` round-trip | Script fetches data, writes parquet, computes SHA-256, writes manifest. Re-run verifies hash match. Test asserts exit code 0 on match, exit code 2 on mismatch (using a corrupted file). |
| `scripts/metrics_crosscheck.py` | Runs SMA-cross strategy on QQQ 6-month window through both custom engine and vectorbt. Asserts WR agrees within 1% absolute. Asserts MaxDD agrees within 1% absolute. Asserts Sharpe agrees within 1% absolute. |

#### Observability

| What | How |
|---|---|
| Fill/skip/flatten events | Every MockBroker fill writes to `JsonlSink` via `structlog` with schema v1. Events include `TRADE_FILLED`, `TRADE_EXIT`, `SKIP_*` (gate rejections), `TIME_EXIT`. |
| OOS verdict artifact | `results/phase3_oos_verdict.json` is JSON-parseable with schema `{param_set_hash, is_metrics, oos_metrics, oos_wr, pass: bool, gate_threshold: 0.55, evaluated_at_utc}`. CI can parse this artifact to gate Phase 4 start. |
| Grid results | `results/phase3_grid_results.csv` — one row per parameter set with IS metrics. Machine-readable for post-hoc analysis. |
| Data manifest | `data/historical/manifest.json` — SHA-256 per parquet file, fetch timestamp, Alpaca API version. Machine-readable for CI hash verification. |

---

## 3. Architecture

### 3.1 Module layout (new files under `src/nasdaq_ale_bot/backtest/`)

```
src/nasdaq_ale_bot/
    backtest/
        __init__.py          (exists, currently empty)
        runner.py            (NEW — BacktestRunner)
        grid.py              (NEW — GridHarness)
        walk_forward.py      (NEW — WalkForwardController)
        metrics.py           (NEW — MetricsCalculator)
    execution/
        mock_broker.py       (NEW — MockBroker implements BrokerProtocol)
        broker.py            (EXISTS — BrokerProtocol, 14 methods)
        gates.py             (EXISTS — GateList, 7 base gates)
scripts/
    fetch_phase3_data.py     (NEW — historical data download)
    metrics_crosscheck.py    (NEW — vectorbt cross-check)
data/
    historical/              (NEW directory)
        QQQ_1m_2024H1.parquet   (.gitignore'd)
        SPY_1m_2024H1.parquet   (.gitignore'd)
        manifest.json           (committed — SHA-256 hashes)
results/                     (NEW directory)
    phase3_oos_verdict.json  (.gitignore'd — generated artifact)
    phase3_grid_results.csv  (.gitignore'd — generated artifact)
tests/
    unit/
        test_mock_broker.py         (NEW)
        test_backtest_runner.py     (NEW)
        test_grid_harness.py        (NEW)
        test_walk_forward.py        (NEW)
        test_metrics.py             (NEW)
    integration/
        test_phase3_e2e.py          (NEW)
        test_golden_ledger.py       (NEW)
        test_grid_harness_mini.py   (NEW)
        test_fetch_determinism.py   (NEW — parquet determinism)
    fixtures/
        golden_ledger_2024_01.json  (NEW — pinned first month of IS)
```

### 3.2 Data flow diagram

```
                         scripts/fetch_phase3_data.py
                                    |
                    +-------------------------------+
                    | Alpaca Historical Data API    |
                    | QQQ + SPY 1m bars             |
                    | 2024-01-01 .. 2024-06-30      |
                    +-------------------------------+
                                    |
                                    v
                    +-------------------------------+
                    | data/historical/              |
                    | QQQ_1m_2024H1.parquet (gzip)  |
                    | SPY_1m_2024H1.parquet (gzip)  |
                    | manifest.json (SHA-256)       |
                    +-------------------------------+
                                    |
                    +---------------+---------------+
                    |                               |
                    v                               v
        +-------------------+           +-------------------+
        | IS Window         |           | OOS Window        |
        | 2024-01..2024-04  |           | 2024-05..2024-06  |
        +-------------------+           +-------------------+
                    |                               |
                    v                               v
        +-------------------+           +-------------------+
        | GridHarness       |           | WalkForward       |
        | 27 param sets     |           | single-shot eval  |
        | composite score   |           | per Top-3 set     |
        +-------------------+           +-------------------+
                    |                               |
                    v                               v
        +-------------------+           +-------------------+
        | Top-3 param sets  |           | OOS Verdict       |
        | ranked DataFrame  |           | WR >= 55% gate    |
        +-------------------+           +-------------------+
                                                    |
                                                    v
                                    +-------------------------------+
                                    | results/phase3_oos_verdict.json |
                                    +-------------------------------+

        BacktestRunner (per param set):
        +-------+   +-------------+   +------------+   +-----------------+   +----------------+
        | Bars  |-->| CandleView  |-->| StateMachine|->| BacktestRunner  |-->| MockBroker     |
        | [i]   |   | (bars, i)   |   | .on_bar()  |   | .bridge(events) |   | .place_bracket |
        +-------+   +-------------+   +------------+   +-----------------+   | .fill_against  |
                                              |                              | (bar N+1)     |
                                              v                              +----------------+
                                         StateEvents                                 |
                                                                                     v
                                                                            +----------------+
                                                                            | AccountLedger  |
                                                                            | (Decimal PnL)  |
                                                                            +----------------+
                                                                                     |
                                                                                     v
                                                                            +----------------+
                                                                            | MetricsCalc    |
                                                                            | WR, MaxDD, PF  |
                                                                            +----------------+
```

### 3.3 MockBroker design (14 methods, "SL wins" A22)

`MockBroker` implements `BrokerProtocol` and inherits `_QuantizingBrokerMixin`. It maintains internal state for:

- `_pending_brackets: list[BracketOrder]` — armed orders awaiting fill against the next bar.
- `_positions: dict[str, MockPosition]` — open positions with entry price, qty, side, SL, TP.
- `_order_history: dict[str, OrderState]` — keyed by `client_order_id` for `get_order()`.
- `_equity: Decimal` — current account equity, initialized from config.
- `_ledger: AccountLedger` — reference to the shared ledger for fill event dispatch.

**Method categories:**

| Category | Methods | Phase 3 behavior |
|---|---|---|
| **Trading** | `place_bracket`, `modify_bracket_stop`, `flatten` | Drive fill simulation. `place_bracket` queues a `BracketOrder`. `modify_bracket_stop` updates the pending SL price. `flatten` closes all open positions at the current bar's close. |
| **Read** | `get_positions`, `get_account_equity`, `get_order`, `get_session_pnl`, `get_realtime_equity` | Return internal state. `get_account_equity` and `get_realtime_equity` return `self._equity` (same value in backtest). `get_session_pnl` returns `self._ledger.realized_today`. |
| **Calendar/Stream** | `get_trading_calendar`, `stream_bars`, `assert_market_open` | No-ops or trivial stubs. `get_trading_calendar` returns a `TradingDay(is_open=True)`. `stream_bars` raises `NotImplementedError` (BacktestRunner provides bars). `assert_market_open` is a no-op (always open in backtest). |
| **Futures** | `get_contract_spec`, `submit_market_flatten` | `get_contract_spec` raises `NotImplementedError` (equities in Phase 3). `submit_market_flatten` delegates to `flatten` and returns an `OrderRef`. |
| **Lifecycle** | `cancel_all` | Removes all pending brackets for the given symbol (or all symbols if `None`). |

**Fill simulation lifecycle (called by BacktestRunner once per bar):**

```python
def evaluate_fills(self, bar: Candle) -> list[FillEvent]:
    """Called by BacktestRunner with bar N+1 AFTER StateMachine.on_bar(bar_N).
    
    Fill priority order:
    1. Pending entry orders (LIMIT semantics by default):
       a. Long: if bar.low <= order.entry_price <= bar.high -> fill at order.entry_price.
       b. Short: if bar.low <= order.entry_price <= bar.high -> fill at order.entry_price.
       c. Gap entry: if long order.entry_price > bar.high (gap up past entry) -> fill at bar.open.
          Short mirror: entry_price < bar.low (gap down past entry) -> fill at bar.open.
       d. Un-touched limit: if bar range does not contain entry_price and no gap, the
          order REMAINS pending for future bars up to a configurable TTL
          (default: carry forward indefinitely; Phase 3 does not expire pending entries).
          Justification: ICT setups expect retracement to the IFVG; waiting is correct.
    2. For each open position, check SL:
       a. Gap past SL: if bar.open past SL (long: bar.open <= SL), fill at bar.open.
       b. SL in range: if bar.low <= SL <= bar.high (long), fill at SL price.
    3. For each open position where SL did NOT fire, check TP:
       a. Gap past TP: if bar.open past TP (long: bar.open >= TP), fill at bar.open.
       b. TP in range: if bar.low <= TP <= bar.high (long), fill at TP price.
    4. "SL wins" (A22): if bar range contains BOTH SL and TP, step 2 fires before
       step 3 — SL fills, TP is never checked in the same bar.
    
    Fill timestamp: all fills for bar N+1 use bar N+1's close_ts. The fill is
    reported AFTER the bar closes, preventing look-ahead.
    """
```

**Additional entry-fill tests (LIMIT semantics):**

- `test_limit_entry_fills_in_range` -- entry=100, bar [99, 101], fill at 100.
- `test_limit_entry_gap_fills_at_open` -- long entry=100, bar.open=102 (gap up past entry), fill at bar.open=102.
- `test_limit_entry_untouched_carries_forward` -- entry=100, bar [90, 95], no fill, order still pending on next bar.

### 3.4 BacktestRunner lifecycle

```python
class BacktestRunner:
    """Bar-by-bar replay engine. One code path with StateMachine.on_bar()."""
    
    def __init__(
        self,
        *,
        bars_primary: list[Candle],      # QQQ 1m bars
        bars_correlated: list[Candle],   # SPY 1m bars (for SMT)
        state_machine: StateMachine,
        mock_broker: MockBroker,
        ledger: AccountLedger,
    ) -> None: ...
    
    def run(self) -> BacktestResult:
        """Main replay loop.
        
        For each bar index i in range(len(bars_primary)):
            1. Construct CandleView(bars_primary, i)
            2. Call state_machine.on_bar(bars_primary[i])
               — StateMachine may call mock_broker.place_bracket()
            3. Call mock_broker.evaluate_fills(bars_primary[i])
               — This evaluates fills for orders armed on PREVIOUS bars
               — Brackets armed on bar N's close fill against bar N+1's range
               — Fill timestamp = bar N+1's close_ts
            4. Report fill events to AccountLedger via OrderFillEvent.from_floats
            5. Log events via structlog (-> JsonlSink schema v1)
        
        Returns BacktestResult with trade list, equity curve, and raw metrics.
        """
    
    def _load_correlated_bars(self) -> None:
        """Feed correlated bars to SMTTracker for divergence detection."""
```

**Critical timing invariant:** The BacktestRunner MUST NOT evaluate fills against the bar that armed the bracket. This is the core look-ahead prevention for fills:

- Bar N: `StateMachine.on_bar(bar_N)` may produce a `TradeIntent` -> `MockBroker.place_bracket()` queues the order.
- Bar N+1: `MockBroker.evaluate_fills(bar_N_plus_1)` checks the queued bracket against bar N+1's OHLC. If SL or TP is hit, the fill is recorded with `close_ts = bar_N_plus_1.ts`.

This matches the live behavior where bracket orders submitted at bar close are evaluated by the exchange against subsequent price action.

**StateMachine -> MockBroker bridge (BacktestRunner responsibility):**

The `StateMachine.on_bar()` method does NOT directly invoke broker calls. It
mutates internal state (current state, active setup) and emits `StateEvent`s.
BacktestRunner is responsible for translating state transitions into
MockBroker actions:

```python
class BacktestRunner:
    def _process_bar(self, bar: Candle, i: int) -> None:
        view = CandleView(self._bars_primary, i)
        
        # 1. Drive state machine forward
        events = self._state_machine.on_bar(bar)  # may transition to ENTRY_ARMED
        
        # 2. Bridge: inspect new state/setup and translate to broker calls
        if self._state_machine.current_state == StrategyState.ENTRY_ARMED:
            setup = self._state_machine.active_setup
            assert setup is not None, "ENTRY_ARMED state must have active setup"
            if setup.client_order_id not in self._armed_order_ids:
                self._mock_broker.place_bracket(
                    symbol=setup.symbol,
                    side=setup.side,
                    qty=setup.qty,
                    entry=setup.entry_price,
                    stop=setup.stop_price,
                    take_profit=setup.take_profit_price,
                    client_order_id=setup.client_order_id,
                )
                self._armed_order_ids.add(setup.client_order_id)
        
        # 3. Bridge: handle flatten intent (time exit, safety trigger, etc.)
        if any(e.event_name == "FLATTEN" for e in events):
            self._mock_broker.flatten(symbol=None)
        
        # 4. Bridge: handle BE-move intent
        for e in events:
            if e.event_name == "BREAKEVEN_MOVE":
                self._mock_broker.modify_bracket_stop(
                    e.fields["order_id"], e.fields["new_stop_price"]
                )
        
        # 5. Evaluate fills against THIS bar (bar N+1 relative to arm bar)
        fills = self._mock_broker.evaluate_fills(bar)
        for fill in fills:
            self._ledger.on_fill(fill)  # Decimal boundary enforced by OrderFillEvent
```

The bridge logic is the sole coupling between detection/state-machine and
broker layers. Phase 4's live runner implements the same bridge against
`AlpacaBroker`, ensuring behavior parity.

Test: `test_state_machine_to_broker_bridge` in `test_backtest_runner.py`
asserts that a synthetic fixture reaching `ENTRY_ARMED` produces exactly
one `mock_broker.place_bracket()` call with the setup's entry/SL/TP values.

### 3.5 GridHarness parameter grid shape

Three axes with the following values (total 27 = 3 x 3 x 3):

| Axis | Config key | Values | Source |
|---|---|---|---|
| IFVG tolerance | `ifvg_tolerance_ticks` | {0, 1, 2} | New Phase 3 param — ticks of tolerance for IFVG body-close-through |
| R:R cap | `rr_cap` | {Decimal("1.2"), Decimal("1.3"), Decimal("1.5")} | A7 — currently fixed at 1.3 |
| CISD lookback | `cisd_lookback_bars` | {10, 15, 20} | A1 — currently capped at 20 |

**Composite score formula (module constants in `grid.py`):**

```python
# grid.py — top of module

# Composite score weights (DISCRETIONARY — see ADR §9 Consequences)
# Rationale: WR is the primary signal per PLAN.md §3 line 95 (55% OOS gate).
# PF provides tail-risk sensitivity. MaxDD penalty discourages overfit to
# low-drawdown noise. Weights sum to 1.0 with a penalty term.
COMPOSITE_WR_WEIGHT = 0.5      # win rate is primary
COMPOSITE_PF_WEIGHT = 0.3      # profit factor secondary
COMPOSITE_DD_PENALTY = 0.2     # max drawdown subtracted

PF_NORMALIZATION_CAP = Decimal("3.0")       # PF > 3 clamped to 1.0
MAXDD_NORMALIZATION_USD = Decimal("5000")   # MaxDD normalized against this


def composite_score(wr: float, pf: float, max_dd_usd: float) -> float:
    pf_norm = min(pf / float(PF_NORMALIZATION_CAP), 1.0)
    dd_norm = min(max_dd_usd / float(MAXDD_NORMALIZATION_USD), 1.0)
    return (
        wr * COMPOSITE_WR_WEIGHT
        + pf_norm * COMPOSITE_PF_WEIGHT
        - dd_norm * COMPOSITE_DD_PENALTY
    )
```

Principle 5 (mechanical over discretionary) tension: these weights are a
deliberate design choice, not derived from spec. Acknowledged in §9 ADR
Consequences. Changes to weights require a documented rationale and
golden-ledger regeneration.

Top-3 parameter sets are selected by descending composite score. Ties are broken by ascending `param_set_hash` (deterministic).

**Output:** `results/phase3_grid_results.csv` with columns:
`param_set_hash, ifvg_tolerance_ticks, rr_cap, cisd_lookback_bars, wr, avg_rr, max_dd, profit_factor, sharpe, trades_count, composite_score, rank`

### 3.6 Walk-Forward controller (OOS single-shot enforcement)

```python
class WalkForwardController:
    """Enforces IS/OOS split with single-shot OOS evaluation."""
    
    # Date boundaries
    IS_START = date(2024, 1, 1)
    IS_END = date(2024, 4, 30)    # inclusive
    OOS_START = date(2024, 5, 1)
    OOS_END = date(2024, 6, 30)   # inclusive
    
    def __init__(self) -> None:
        self._oos_runs: dict[str, OOSResult] = {}  # param_set_hash -> result
    
    def run_is(
        self,
        *,
        bars_primary: list[Candle],
        bars_correlated: list[Candle],
        params: GridParams,
    ) -> ISResult:
        """Run backtest on IS window. Can be called multiple times per param set.
        Returns full metrics + trade list for the IS window.
        """
    
    def run_oos(
        self,
        *,
        bars_primary: list[Candle],
        bars_correlated: list[Candle],
        params: GridParams,
        param_set_hash: str,
    ) -> OOSResult:
        """Run backtest on OOS window. EXACTLY ONCE per param_set_hash.
        
        Raises:
            OOSAlreadyRunError: if this param_set_hash has been evaluated before.
            OOSCodeVersionMismatchError: if STRATEGY_VERSION changed since IS run.
        """
        if param_set_hash in self._oos_runs:
            raise OOSAlreadyRunError(
                f"OOS already evaluated for param_set_hash={param_set_hash}. "
                f"OOS is write-once per parameter set."
            )
        # ... run backtest on OOS window ...
        result = OOSResult(...)
        self._oos_runs[param_set_hash] = result
        return result
    
    def generate_verdict(
        self,
        param_set_hash: str,
        gate_threshold: float = 0.55,
    ) -> dict:
        """Generate OOS verdict artifact for results/phase3_oos_verdict.json.
        
        Schema:
        {
            "param_set_hash": str,
            "is_metrics": {WR, avg_rr, max_dd, profit_factor, sharpe, trades},
            "oos_metrics": {WR, avg_rr, max_dd, profit_factor, sharpe, trades},
            "oos_wr": float,
            "pass": bool,              # oos_wr >= gate_threshold
            "gate_threshold": 0.55,
            "evaluated_at_utc": str    # ISO-8601
        }
        """
```

**`param_set_hash` computation:**

```python
def compute_param_set_hash(params: GridParams, strategy_version: str) -> str:
    """Deterministic hash of parameter values + code version.
    
    hash = sha1(json.dumps(sorted(params.items()), sort_keys=True) + "|" + strategy_version)
    
    strategy_version comes from core/__init__.py::STRATEGY_VERSION, a constant
    bumped on any detection/state-machine rule change.
    """
```

**OOS stateful-component reset contract:**

At the start of every OOS run, `WalkForwardController._run_oos_single_shot()` MUST
construct FRESH instances of all stateful engine components. No IS state may leak
into OOS evaluation. Concretely:

- `AccountLedger` -- new instance with `session_start_equity = OOS_START_EQUITY`
  (default: same as IS start equity, not IS-end equity, so OOS PnL starts at 0).
- `StateMachine` -- new instance in `FLAT` state with no active setup.
- `HTFBiasDetector` -- new instance with no prior bias (enters `PENDING` until HTF
  confirms from within the OOS window).
- `SMTTracker` -- new instance with empty 5m anchors.
- `MockBroker` -- new instance with no pending brackets, no open positions,
  initial equity = OOS_START_EQUITY.
- `JsonlSink` -- rotated to a new file path (e.g., `bot_events_oos_{param_hash}.jsonl`)
  so IS and OOS event streams are independently auditable.

Rationale: IS-end state (e.g., HTF bias = LONG after 4 months of IS data) would
constitute a look-ahead leak into OOS. Walk-forward methodology requires OOS
to be evaluated AS IF the strategy had never seen IS data.

Test: `test_oos_fresh_instantiation` asserts that modifying IS-end ledger state
(via monkeypatch) does not affect OOS results for the same param_set_hash.

---

## 4. Implementation Steps (strict order)

### Step 1 — Data fetch + manifest (`scripts/fetch_phase3_data.py`)

**LOC estimate:** 150-200

**Purpose:** Download pinned QQQ + SPY 1-minute bars for 2024-01-01 through 2024-06-30 from Alpaca historical data API. Write gzip'd Parquet to `data/historical/`. Create SHA-256 manifest.

**Concrete deliverables:**
- NEW: `scripts/fetch_phase3_data.py`
- NEW: `data/historical/` directory
- NEW: `data/historical/manifest.json` (committed to git)
- MODIFIED: `.gitignore` — add `data/historical/*.parquet`

**Implementation details:**

```python
#!/usr/bin/env python3
"""Fetch QQQ + SPY 1m bars for 2024H1 from Alpaca. Gzip'd Parquet output.

Usage:
    python scripts/fetch_phase3_data.py [--out-dir data/historical]

Environment variables:
    ALPACA_API_KEY, ALPACA_SECRET_KEY

Idempotent: if parquet files exist and SHA-256 matches manifest, exits 0.
Exit codes: 0 = success, 1 = error, 2 = data integrity mismatch.
"""

# Key functions:
def fetch_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch via alpaca-py StockHistoricalDataClient.get_stock_bars()."""

def write_parquet(df: pd.DataFrame, path: Path) -> None:
    """pyarrow.parquet.write_table(compression='gzip')."""

**Determinism contract for write_parquet:**

To guarantee byte-identical parquet output across fetches with identical input
data, `write_parquet` MUST:

1. Sort the DataFrame by `ts_utc` ascending before writing.
2. Use a fixed column order: `[ts_utc, open, high, low, close, volume]`.
3. Pass `compression='gzip'` with `compression_level=6` (pyarrow default is 9;
   pin to 6 for reproducibility across pyarrow versions).
4. Strip pyarrow's `pandas_metadata` and `created_by` fields via
   `pa.parquet.write_table(..., use_pandas_metadata=False, write_statistics=False)`.
5. Assert `row_group_size=48750` (full dataset as single row group) so row-group
   boundaries don't contribute to hash variance.

This ensures that `compute_sha256(parquet_path)` is a function of input bar
values only, not of pyarrow version, write timestamp, or row-group layout.

Test (`tests/integration/test_fetch_determinism.py`):
- Write the same synthetic DataFrame to parquet twice with different pyarrow
  cache states. Assert SHA-256 of both files is identical.

def compute_sha256(path: Path) -> str: ...

def write_manifest(out_dir: Path, files: dict[str, str]) -> None:
    """Write manifest.json with SHA-256 per file, fetch_timestamp_utc,
    alpaca_api_version."""

def verify_manifest(out_dir: Path) -> bool:
    """Re-compute SHA-256 for each file in manifest. Return True if all match.
    Raise DataIntegrityError with details on mismatch."""
```

**Manifest schema (`data/historical/manifest.json`):**

```json
{
  "fetch_timestamp_utc": "2024-...",
  "alpaca_api_version": "v2",
  "files": {
    "QQQ_1m_2024H1.parquet": {
      "sha256": "abc123...",
      "rows": 48750,
      "date_range": ["2024-01-02", "2024-06-28"]
    },
    "SPY_1m_2024H1.parquet": {
      "sha256": "def456...",
      "rows": 48750,
      "date_range": ["2024-01-02", "2024-06-28"]
    }
  }
}
```

**Acceptance criteria (Step 1):**
- [ ] `python scripts/fetch_phase3_data.py` produces two parquet files in `data/historical/`.
- [ ] Each parquet file is gzip-compressed (verified by `pyarrow.parquet.read_metadata(path).row_group(0).column(0).compression`).
- [ ] `data/historical/manifest.json` contains SHA-256 per file.
- [ ] Re-running the script with existing files and matching hashes exits 0 without re-fetching.
- [ ] Corrupting one byte in a parquet file and re-running exits with code 2.
- [ ] Parquet files are in `.gitignore`; `manifest.json` is committed.

---

### Step 2 — MockBroker (`execution/mock_broker.py`)

**LOC estimate:** 350-500 (source) + 300-400 (tests)

**Purpose:** Implement all 14 `BrokerProtocol` methods for backtest fill simulation. This is the largest single deliverable in Phase 3.

**Concrete deliverables:**
- NEW: `src/nasdaq_ale_bot/execution/mock_broker.py`
- NEW: `tests/unit/test_mock_broker.py`

**Key design decisions:**

1. **`fill_model` parameter:** Constructor takes `fill_model: Literal["sl_wins"] = "sl_wins"`. Only "sl_wins" is implemented in Phase 3. Future fill models (e.g., "random_first") can be added without modifying existing logic.

2. **Fill evaluation is pull-based:** BacktestRunner calls `mock_broker.evaluate_fills(bar)` explicitly after `StateMachine.on_bar()`. MockBroker never auto-evaluates. This keeps the timing explicit and testable.

3. **Internal position model:**

```python
@dataclass
class MockPosition:
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    client_order_id: str
    entry_bar_ts: datetime

@dataclass
class BracketOrder:
    """Pending bracket awaiting fill on the next bar."""
    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal  # market order -> fills at bar open
    stop: Decimal
    take_profit: Decimal
    client_order_id: str
    armed_at_ts: datetime
```

4. **Fill event dispatch:** Every fill calls `AccountLedger.on_fill(OrderFillEvent.from_floats(...))`, maintaining the Decimal boundary contract from Phase 2.

5. **"SL wins" implementation:**

```python
def _evaluate_position_exits(self, pos: MockPosition, bar: Candle) -> FillEvent | None:
    """Check SL first, then TP. SL wins on collision (A22).
    
    Gap-open handling:
    - Long: if bar.open <= pos.stop_price -> fill at bar.open (not SL)
    - Short: if bar.open >= pos.stop_price -> fill at bar.open (not SL)
    
    Normal SL:
    - Long: if bar.low <= pos.stop_price -> fill at pos.stop_price
    - Short: if bar.high >= pos.stop_price -> fill at pos.stop_price
    
    TP (only if SL did not fire):
    - Long: if bar.high >= pos.take_profit_price -> fill at pos.take_profit_price
    - Short: if bar.low <= pos.take_profit_price -> fill at pos.take_profit_price
    """
```

**Tests (`tests/unit/test_mock_broker.py`):**

- `test_mock_broker_implements_protocol` — `isinstance(MockBroker(...), BrokerProtocol)` is True.
- `test_all_14_methods_callable` — every method in `BROKER_PROTOCOL_METHODS` exists and is callable.
- `test_place_bracket_queues_order` — after `place_bracket`, `_pending_brackets` has one entry.
- `test_entry_fills_at_bar_open` — pending bracket fills at bar N+1's open price.
- `test_sl_wins_when_both_hit` — bar contains both SL and TP; position closed at SL price.
- `test_tp_fills_when_sl_not_hit` — bar touches TP but not SL; position closed at TP price.
- `test_gap_open_past_sl_fills_at_open` — bar opens past SL; fill at open, not SL.
- `test_gap_open_past_tp_fills_at_open` — bar opens past TP (no SL hit); fill at open.
- `test_entry_and_exit_same_bar` — bracket armed, next bar triggers entry fill + SL in same evaluation.
- `test_no_partial_fills` — fill quantity always equals order quantity.
- `test_modify_bracket_stop_updates_pending` — `modify_bracket_stop` changes SL price on open position.
- `test_flatten_closes_all_positions` — `flatten()` closes everything at current bar's close.
- `test_cancel_all_removes_pending` — `cancel_all()` clears pending brackets.
- `test_get_positions_returns_open` — reflects current open positions.
- `test_get_account_equity_matches_ledger` — equity tracks with AccountLedger.
- `test_get_order_returns_history` — `get_order(client_order_id)` returns correct state.
- `test_calendar_stream_are_stubs` — `get_trading_calendar` returns `TradingDay(is_open=True)`, `stream_bars` raises, `assert_market_open` is no-op.
- `test_fill_events_dispatched_to_ledger` — after a fill, `AccountLedger.realized_today` reflects the PnL.
- `test_fill_timestamp_is_bar_close_ts` — fill event's `fill_ts` equals the evaluating bar's `ts`.

---

### Step 3 — BacktestRunner (`backtest/runner.py`)

**LOC estimate:** 400-600 (source) + 400-500 (tests)

**Purpose:** Replay loop that feeds bars to StateMachine and coordinates MockBroker fill evaluation.

**Concrete deliverables:**
- NEW: `src/nasdaq_ale_bot/backtest/runner.py`
- NEW: `tests/unit/test_backtest_runner.py`

**Key signatures:**

```python
@dataclass(frozen=True)
class TradeRecord:
    """One completed round-trip trade."""
    entry_ts: datetime
    exit_ts: datetime
    symbol: str
    side: str
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    realized_pnl: Decimal
    exit_reason: str  # "stop_out" | "target_hit" | "flatten" | "time_exit"
    param_set_hash: str | None

@dataclass
class BacktestResult:
    """Output of a single backtest run."""
    trades: list[TradeRecord]
    equity_curve: list[tuple[datetime, Decimal]]  # (ts, equity) snapshots
    metrics: dict[str, Any]  # computed by MetricsCalculator
    params: dict[str, Any]
    param_set_hash: str
    window_start: date
    window_end: date

class BacktestRunner:
    def __init__(
        self,
        *,
        bars_primary: list[Candle],
        bars_correlated: list[Candle] | None = None,
        mock_broker: MockBroker,
        ledger: AccountLedger,
        strategy_cfg: dict[str, Any],
        instrument_cfg: Any,  # InstrumentSpec from settings.py
    ) -> None:
        """Constructs StateMachine internally with the provided collaborators."""
    
    def run(self) -> BacktestResult:
        """Main replay loop. See §3.4 for lifecycle details."""
    
    @staticmethod
    def load_bars_from_parquet(path: Path) -> list[Candle]:
        """Load a parquet file into a list of Candle objects.
        Validates each row through Candle's pydantic validators.
        """
```

**Tests (`tests/unit/test_backtest_runner.py`):**

- `test_bar_iteration_constructs_candle_view` — monkey-patch CandleView to track construction; assert view.horizon == current index for each bar.
- `test_fill_timestamp_semantics` — arm bracket on bar 5, assert fill evaluates against bar 6, fill_ts == bar 6's ts.
- `test_no_look_ahead_during_replay` — wrap CandleView.__getitem__ to record max k accessed; assert max k <= current i.
- `test_session_rotation_propagated` — bars spanning two calendar days; assert ledger.on_session_rotation called at the boundary.
- `test_ledger_decimal_integrity` — after full replay, all ledger fields are Decimal instances.
- `test_empty_bars_returns_empty_result` — zero bars produces BacktestResult with empty trades list.
- `test_single_bar_no_crash` — one bar produces BacktestResult without exception.
- `test_multi_day_state_continuity` — state machine state persists across day boundaries (no reset).
- `test_trades_list_matches_fill_count` — number of TradeRecords equals number of round-trip fills.
- `test_equity_curve_monotonic_timestamps` — equity curve timestamps are strictly increasing.
- `test_correlated_bars_fed_to_smt` — when `bars_correlated` is provided, SMTTracker receives paired bar updates.
- `test_result_includes_param_set_hash` — BacktestResult.param_set_hash matches input.
- `test_load_bars_from_parquet` — round-trip: write synthetic Candle list to parquet, load back, assert equality.

---

### Step 4 — MetricsCalculator (`backtest/metrics.py`)

**LOC estimate:** 150-250 (source) + 100-150 (tests)

**Purpose:** Compute all strategy metrics from the trade list and equity curve produced by `BacktestRunner`. Consumed by `GridHarness` (composite score inputs) and `WalkForwardController` (OOS verdict).

**Concrete deliverables:**
- NEW: `src/nasdaq_ale_bot/backtest/metrics.py`
- NEW: `tests/unit/test_metrics.py`

**Key signatures:**

```python
@dataclass(frozen=True)
class StrategyMetrics:
    wr: float                    # win rate [0.0, 1.0]
    avg_rr: float                # average R:R across all closed trades
    max_dd_usd: Decimal          # max drawdown in USD (peak-to-trough)
    max_dd_pct: float            # max drawdown as fraction of HWM
    profit_factor: float         # sum(wins) / abs(sum(losses)); inf capped at 999.0
    sharpe: float                # annualized (252 * sqrt(trading_days) scaling)
    trades_count: int
    trades_per_day: float        # trades / distinct trading days observed
    avg_hold_minutes: float      # mean (exit_ts - entry_ts) in minutes
    total_pnl_usd: Decimal

class MetricsCalculator:
    def __init__(self, *, annualization_factor: int = 252) -> None: ...
    
    def compute(
        self,
        *,
        trades: list[TradeRecord],
        equity_curve: list[tuple[datetime, Decimal]],
    ) -> StrategyMetrics: ...
    
    @staticmethod
    def _compute_wr(trades: list[TradeRecord]) -> float: ...
    @staticmethod
    def _compute_max_dd(equity_curve: list[tuple[datetime, Decimal]]) -> tuple[Decimal, float]: ...
    @staticmethod
    def _compute_profit_factor(trades: list[TradeRecord]) -> float: ...
    @staticmethod
    def _compute_sharpe(equity_curve: list[tuple[datetime, Decimal]], af: int) -> float: ...
```

**Edge cases:**
- Zero trades -> WR=0.0, avg_rr=0.0, PF=0.0, Sharpe=0.0, max_dd=0.
- All losses -> PF=0.0 (0 / abs(sum_losses)).
- All wins -> PF=999.0 (capped).
- Flat equity (zero std) -> Sharpe=0.0.
- Single trading day -> trades_per_day = trades_count.

**Tests (`tests/unit/test_metrics.py`):**
- `test_wr_on_known_trade_list` -- 6 wins + 4 losses -> WR=0.6.
- `test_avg_rr_mixed` -- R:R {+1.2, -1.0, +1.5, -1.0} -> avg_rr=0.175.
- `test_max_dd_synthetic_equity_curve` -- known curve with HWM=100k, trough=97k -> MaxDD=3000.
- `test_profit_factor_all_losses_returns_zero` -- edge case.
- `test_profit_factor_all_wins_caps_at_999` -- edge case.
- `test_sharpe_flat_equity_returns_zero` -- zero std edge case.
- `test_trades_per_day_normalization` -- 10 trades over 5 distinct days = 2.0.
- `test_avg_hold_minutes` -- 3 trades with holds [10, 20, 30] -> 20.0.
- `test_compute_returns_strategy_metrics_instance` -- type check.
- `test_decimal_preserved_in_pnl_fields` -- max_dd_usd and total_pnl_usd are Decimal.

**Coverage gate:** `pytest --cov=src/nasdaq_ale_bot/backtest/metrics --cov-branch --cov-fail-under=90`.

---

### Step 5 — GridHarness (`backtest/grid.py`)

**LOC estimate:** 250-350 (source) + 150-250 (tests)

**Purpose:** Sweep parameter grid on IS window, rank by composite score, return Top-3.

**Concrete deliverables:**
- NEW: `src/nasdaq_ale_bot/backtest/grid.py`
- NEW: `tests/unit/test_grid_harness.py`

**Key signatures:**

```python
@dataclass(frozen=True)
class GridParams:
    ifvg_tolerance_ticks: int
    rr_cap: Decimal
    cisd_lookback_bars: int
    
    def to_dict(self) -> dict[str, Any]: ...

class GridHarness:
    # Default grid axes
    DEFAULT_IFVG_TOLERANCE = [0, 1, 2]
    DEFAULT_RR_CAP = [Decimal("1.2"), Decimal("1.3"), Decimal("1.5")]
    DEFAULT_CISD_LOOKBACK = [10, 15, 20]
    
    def __init__(
        self,
        *,
        bars_primary: list[Candle],
        bars_correlated: list[Candle] | None = None,
        base_strategy_cfg: dict[str, Any],
        instrument_cfg: Any,
        ifvg_tolerance_values: list[int] | None = None,
        rr_cap_values: list[Decimal] | None = None,
        cisd_lookback_values: list[int] | None = None,
    ) -> None: ...
    
    def run(self) -> GridResult:
        """Enumerate all param combinations, run BacktestRunner for each
        on the IS window, compute composite scores, rank, return Top-3.
        """
    
    @staticmethod
    def composite_score(wr: float, pf: float, max_dd_usd: float) -> float:
        """Uses module constants COMPOSITE_WR_WEIGHT, COMPOSITE_PF_WEIGHT,
        COMPOSITE_DD_PENALTY, PF_NORMALIZATION_CAP, MAXDD_NORMALIZATION_USD.
        See §3.5 for formula."""
    
    def to_dataframe(self, result: GridResult) -> pd.DataFrame:
        """Convert to ranked DataFrame for results/phase3_grid_results.csv."""

@dataclass
class GridResult:
    all_results: list[ParamResult]  # all 27
    top_3: list[ParamResult]        # sorted by composite score descending
    
@dataclass
class ParamResult:
    params: GridParams
    param_set_hash: str
    metrics: dict[str, Any]
    composite_score: float
    rank: int
```

**Tests (`tests/unit/test_grid_harness.py`):**

- `test_default_grid_produces_27_sets` — 3 x 3 x 3 = 27.
- `test_custom_grid_respects_overrides` — passing `ifvg_tolerance_values=[0, 1]` produces 2 x 3 x 3 = 18.
- `test_composite_score_formula` — known inputs produce known output. WR=0.6, PF=2.0, MaxDD=2000 -> score = 0.6*0.5 + (2.0/3.0)*0.3 + (2000/5000)*(-0.2) = 0.3 + 0.2 + (-0.08) = 0.42.
- `test_top_3_selection_by_score` — 5 results with known scores; Top-3 are the three highest.
- `test_tie_break_by_hash` — two results with identical scores; deterministic ordering by `param_set_hash`.
- `test_param_set_hash_includes_strategy_version` — changing `STRATEGY_VERSION` changes the hash.
- `test_param_set_hash_deterministic` — same params + version produce same hash across runs.
- `test_grid_on_empty_bars` — returns GridResult with all_results empty, top_3 empty.
- `test_to_dataframe_columns` — DataFrame has all expected columns with correct dtypes.

---

### Step 6 — vectorbt crosscheck (`scripts/metrics_crosscheck.py`)

**LOC estimate:** 100-150

**Purpose:** Validate custom metrics engine against vectorbt on a simple SMA-cross strategy (NOT the ICT strategy). This is the Phase 3.5 cross-check mandated by PLAN.md.

**Concrete deliverables:**
- NEW: `scripts/metrics_crosscheck.py`

**Implementation details:**

The script runs a **simple SMA-cross strategy** (20-period SMA crosses 50-period SMA) on the QQQ 6-month data through both:

1. **Custom engine:** A minimal `SMABacktestRunner` that uses the same `MetricsCalculator` as the real backtest. This runner does NOT use `StateMachine` — it is a standalone SMA strategy implemented in ~50 LOC.
2. **vectorbt:** `vectorbt.Portfolio.from_signals()` with the same SMA crossover signals.

Both run on identical input data (same QQQ parquet file). The script compares:
- Win Rate: `|WR_custom - WR_vbt| < 0.01` (1% absolute)
- Max Drawdown: `|MaxDD_custom - MaxDD_vbt| < 0.01` (1% absolute, as fraction of equity)
- Sharpe Ratio: `|Sharpe_custom - Sharpe_vbt| < 0.01` (1% absolute)

Exit code 0 on pass, 1 on failure. Prints a comparison table.

**Why SMA-cross, not the ICT strategy:** vectorbt cannot replicate the 6-state CISD/IFVG/sweep detection. SMA-cross is a lowest-common-denominator strategy that both engines can implement identically, validating the metrics computation layer in isolation.

**Acceptance criteria (Step 6):**
- [ ] `pip install -e ".[crosscheck]"` installs vectorbt.
- [ ] `python scripts/metrics_crosscheck.py` exits 0.
- [ ] Output table shows WR, MaxDD, Sharpe for both engines with delta column.
- [ ] All deltas < 0.01 (absolute).

---

### Step 7 — Golden Ledger fixture + test

**LOC estimate:** 100-150 (test) + ~200 JSON (fixture)

**Purpose:** Pin the exact trade output of the first month of IS data (January 2024) as an immutable fixture. Any code change that alters fill prices, fill counts, or PnL values will break this test in CI.

**Concrete deliverables:**
- NEW: `tests/fixtures/golden_ledger_2024_01.json`
- NEW: `tests/integration/test_golden_ledger.py`

**Fixture schema (`golden_ledger_2024_01.json`):**

```json
{
  "window": {"start": "2024-01-02", "end": "2024-01-31"},
  "param_set_hash": "...",
  "strategy_version": "...",
  "params": {
    "ifvg_tolerance_ticks": 1,
    "rr_cap": "1.3",
    "cisd_lookback_bars": 20
  },
  "trades": [
    {
      "entry_ts": "2024-01-03T14:32:00+00:00",
      "exit_ts": "2024-01-03T15:45:00+00:00",
      "symbol": "QQQ",
      "side": "BUY",
      "entry_price": "412.35",
      "exit_price": "413.96",
      "qty": "1",
      "realized_pnl": "1.61",
      "exit_reason": "target_hit"
    }
  ],
  "summary": {
    "total_trades": 12,
    "wins": 7,
    "losses": 5,
    "total_pnl": "8.42",
    "max_dd": "3.21"
  }
}
```

All price/PnL fields are strings representing exact Decimal values. The test parses them as `Decimal(value)` and compares field-by-field.

**Test (`tests/integration/test_golden_ledger.py`):**

```python
def test_golden_ledger_exact_match():
    """Replay January 2024 IS data and compare against pinned fixture.
    
    Every field is Decimal-exact. Any drift fails CI.
    This is the primary regression guard for the backtest engine.
    """
    fixture = load_golden_ledger("tests/fixtures/golden_ledger_2024_01.json")
    result = run_backtest_on_window(
        start=date(2024, 1, 2),
        end=date(2024, 1, 31),
        params=fixture["param_set_hash"],
    )
    for i, (expected, actual) in enumerate(zip(fixture["trades"], result.trades)):
        assert actual.entry_price == Decimal(expected["entry_price"]), \
            f"Trade {i}: entry_price {actual.entry_price} != {expected['entry_price']}"
        assert actual.exit_price == Decimal(expected["exit_price"]), \
            f"Trade {i}: exit_price {actual.exit_price} != {expected['exit_price']}"
        assert actual.realized_pnl == Decimal(expected["realized_pnl"]), \
            f"Trade {i}: realized_pnl {actual.realized_pnl} != {expected['realized_pnl']}"
        # ... all fields ...
```

**Fixture generation:** The golden ledger fixture is generated ONCE by running the BacktestRunner on
January 2024 with the PINNED default parameter set:

    GridParams(
        ifvg_tolerance_ticks=1,
        rr_cap=Decimal("1.3"),
        cisd_lookback_bars=20,
    )

These are the current live-default values (midpoint of the grid for ifvg_tolerance
and rr_cap; max of the grid for cisd_lookback to match A1 safety cap). The
fixture header records these values verbatim; test_golden_ledger.py asserts the
BacktestRunner is invoked with exactly these params before comparing trades.

The fixture is committed to git. Subsequent runs compare against it. To regenerate (e.g., after a legitimate rule change):

```bash
python -m nasdaq_ale_bot.backtest.generate_golden_ledger \
    --start 2024-01-02 --end 2024-01-31 \
    --out tests/fixtures/golden_ledger_2024_01.json
```

This is a deliberate, manually-triggered process. CI never auto-regenerates the fixture.

---

### Step 8 — Walk-Forward + OOS gate (`backtest/walk_forward.py`)

**LOC estimate:** 200-300 (source) + 150-200 (tests)

**Purpose:** Orchestrate the full walk-forward workflow: grid on IS, Top-3 selection, single-shot OOS evaluation per Top-3 set, verdict artifact generation.

**Concrete deliverables:**
- NEW: `src/nasdaq_ale_bot/backtest/walk_forward.py`
- NEW: `tests/unit/test_walk_forward.py`
- NEW: `results/` directory (`.gitignore`'d contents)

**Implementation details:**

```python
class WalkForwardController:
    IS_START = date(2024, 1, 1)
    IS_END = date(2024, 4, 30)
    OOS_START = date(2024, 5, 1)
    OOS_END = date(2024, 6, 30)
    
    def __init__(self) -> None:
        self._oos_runs: dict[str, OOSResult] = {}
    
    def run_full_pipeline(
        self,
        *,
        bars_primary: list[Candle],
        bars_correlated: list[Candle] | None = None,
        base_strategy_cfg: dict[str, Any],
        instrument_cfg: Any,
        gate_threshold: float = 0.55,
    ) -> WalkForwardResult:
        """Full pipeline:
        1. Split bars into IS and OOS by date
        2. Run GridHarness on IS -> Top-3
        3. For each Top-3 param set, run_oos() -> OOS metrics
        4. Generate verdict: best OOS WR >= gate_threshold?
        5. Write results/phase3_oos_verdict.json
        """
    
    def _split_bars(
        self, bars: list[Candle]
    ) -> tuple[list[Candle], list[Candle]]:
        """Split into IS and OOS by date boundary."""
    
    def _run_oos_single_shot(
        self,
        *,
        oos_bars_primary: list[Candle],
        oos_bars_correlated: list[Candle] | None,
        params: GridParams,
        param_set_hash: str,
        strategy_cfg: dict[str, Any],
        instrument_cfg: Any,
    ) -> OOSResult:
        """Single-shot OOS evaluation. Raises OOSAlreadyRunError on repeat."""

class OOSAlreadyRunError(RuntimeError):
    """OOS window has already been evaluated for this param_set_hash."""

class OOSCodeVersionMismatchError(RuntimeError):
    """STRATEGY_VERSION changed between IS grid run and OOS evaluation."""
```

**OOS verdict artifact (`results/phase3_oos_verdict.json`):**

```json
{
  "param_set_hash": "a1b2c3...",
  "is_metrics": {
    "wr": 0.62,
    "avg_rr": 1.18,
    "max_dd": 1250.00,
    "profit_factor": 1.85,
    "sharpe": 1.42,
    "trades_count": 87
  },
  "oos_metrics": {
    "wr": 0.57,
    "avg_rr": 1.15,
    "max_dd": 980.00,
    "profit_factor": 1.65,
    "sharpe": 1.20,
    "trades_count": 42
  },
  "oos_wr": 0.57,
  "pass": true,
  "gate_threshold": 0.55,
  "evaluated_at_utc": "2024-..."
}
```

**Tests (`tests/unit/test_walk_forward.py`):**

- `test_oos_single_shot_raises_on_repeat` — call `run_oos()` twice with same hash; second raises `OOSAlreadyRunError`.
- `test_oos_code_version_mismatch_raises` — change `STRATEGY_VERSION` between IS and OOS; raises `OOSCodeVersionMismatchError`.
- `test_is_oos_date_split_correct` — IS bars end at 2024-04-30, OOS bars start at 2024-05-01.
- `test_verdict_pass_at_threshold` — WR=0.55 -> pass=True.
- `test_verdict_fail_below_threshold` — WR=0.54 -> pass=False.
- `test_verdict_artifact_schema` — output JSON has all required keys with correct types.
- `test_full_pipeline_smoke` — 10-bar mini-fixture; runs without error; produces verdict.
- `test_param_set_hash_determinism` — same params produce same hash across calls.

---

## 5. Acceptance Criteria (per step, testable)

### Step 1 — Data fetch + manifest

- [ ] `scripts/fetch_phase3_data.py` exists and is executable.
- [ ] Running the script produces `data/historical/QQQ_1m_2024H1.parquet` and `data/historical/SPY_1m_2024H1.parquet`.
- [ ] Both parquet files are gzip-compressed.
- [ ] `data/historical/manifest.json` exists with SHA-256 per file, `fetch_timestamp_utc`, `alpaca_api_version`.
- [ ] Idempotent: re-run with matching hashes exits 0 without re-fetching.
- [ ] Integrity check: corrupted file triggers exit code 2 with descriptive error.
- [ ] `.gitignore` excludes `data/historical/*.parquet`.
- [ ] `manifest.json` is committed to git.

### Step 2 — MockBroker

- [ ] `isinstance(MockBroker(...), BrokerProtocol)` is `True`.
- [ ] All 14 methods in `BROKER_PROTOCOL_METHODS` are implemented.
- [ ] `test_sl_wins_when_both_hit` passes: bar contains both SL and TP, position closed at SL.
- [ ] `test_gap_open_past_sl_fills_at_open` passes: gap fill at bar open, not SL price.
- [ ] `test_entry_and_exit_same_bar` passes: bracket armed, next bar triggers entry + SL.
- [ ] `test_no_partial_fills` passes: fill quantity always equals order quantity.
- [ ] `test_modify_bracket_stop_updates_pending` passes: SL price updated on open position.
- [ ] `test_fill_events_dispatched_to_ledger` passes: fills update `AccountLedger.realized_today`.
- [ ] `test_fill_timestamp_is_bar_close_ts` passes: fill ts equals evaluating bar's ts.
- [ ] Coverage: `pytest --cov=src/nasdaq_ale_bot/execution/mock_broker --cov-branch` = **100%**.

### Step 3 — BacktestRunner

- [ ] BacktestRunner replays a 50-bar synthetic fixture without error.
- [ ] `CandleView` is constructed with `horizon == current_index` for each bar.
- [ ] Fill-timestamp semantics enforced: brackets armed on bar N fill against bar N+1.
- [ ] No `LookAheadError` raised during replay.
- [ ] `AccountLedger` fields are all `Decimal` after replay.
- [ ] Session rotation propagated at day boundaries.
- [ ] Empty bars list returns empty `BacktestResult` without crash.
- [ ] `BacktestResult.trades` count matches fill events.
- [ ] Coverage: `pytest --cov=src/nasdaq_ale_bot/backtest/runner --cov-branch --cov-fail-under=90`.

### Step 4 — MetricsCalculator

- [ ] `StrategyMetrics` dataclass is frozen and has all 10 fields.
- [ ] `MetricsCalculator.compute()` returns a `StrategyMetrics` instance.
- [ ] Zero trades produces WR=0.0, PF=0.0, Sharpe=0.0.
- [ ] All wins caps PF at 999.0.
- [ ] Flat equity curve produces Sharpe=0.0.
- [ ] `max_dd_usd` and `total_pnl_usd` are `Decimal` instances.
- [ ] `trades_per_day` normalizes by distinct trading days, not calendar days.
- [ ] Coverage: `pytest --cov=src/nasdaq_ale_bot/backtest/metrics --cov-branch --cov-fail-under=90`.

### Step 5 — GridHarness

- [ ] Default grid produces exactly 27 parameter sets.
- [ ] Composite score formula verified: known inputs produce known outputs.
- [ ] Top-3 selection correct on a 5-result test case.
- [ ] Tie-breaking by `param_set_hash` is deterministic.
- [ ] `param_set_hash` includes `STRATEGY_VERSION`.
- [ ] Output DataFrame has correct columns and dtypes.
- [ ] Coverage: `pytest --cov=src/nasdaq_ale_bot/backtest/grid --cov-branch --cov-fail-under=85`.

### Step 6 — vectorbt crosscheck

- [ ] `pip install -e ".[crosscheck]"` installs vectorbt.
- [ ] `python scripts/metrics_crosscheck.py` exits 0.
- [ ] WR, MaxDD, Sharpe agree within 1% absolute between custom and vectorbt engines.
- [ ] The script uses an SMA-cross strategy, NOT the ICT strategy.

### Step 7 — Golden Ledger

- [ ] `tests/fixtures/golden_ledger_2024_01.json` exists with pinned trade data.
- [ ] Fixture includes `params` block with pinned `GridParams(1, Decimal("1.3"), 20)`.
- [ ] `test_golden_ledger_exact_match` passes: every field is Decimal-exact.
- [ ] Test asserts BacktestRunner is invoked with the pinned params before comparing trades.
- [ ] Modifying a detection rule breaks this test (verified by intentionally changing a constant and running the test).
- [ ] Fixture includes `strategy_version` for provenance.

### Step 8 — Walk-Forward + OOS gate

- [ ] `OOSAlreadyRunError` raised on second invocation with same `param_set_hash`.
- [ ] `OOSCodeVersionMismatchError` raised when `STRATEGY_VERSION` changes between IS and OOS.
- [ ] IS/OOS date split: IS bars end 2024-04-30, OOS bars start 2024-05-01.
- [ ] OOS runs construct FRESH instances of all stateful components (no IS state leaks).
- [ ] `test_oos_fresh_instantiation` passes: monkeypatched IS-end state does not affect OOS results.
- [ ] Verdict artifact `results/phase3_oos_verdict.json` has correct schema.
- [ ] `pass` flag is `true` when OOS WR >= 0.55, `false` otherwise.
- [ ] Full pipeline smoke test passes on mini-fixture.
- [ ] Coverage: `pytest --cov=src/nasdaq_ale_bot/backtest/walk_forward --cov-branch --cov-fail-under=90`.

---

## 6. Coverage Gates

| Module | Metric | Target | Rationale |
|---|---|---|---|
| `execution/mock_broker.py` | Branch coverage | **100%** | MockBroker is the fill simulation engine. Every branch represents a fill scenario (SL, TP, gap, collision). Missing a branch means missing a fill edge case. |
| `backtest/runner.py` | Branch coverage | **>= 90%** | BacktestRunner is the replay loop. Most branches are error handling and edge cases. 90% ensures the main loop and fill-timing logic are fully covered. |
| `backtest/grid.py` | Branch coverage | **>= 85%** | GridHarness is mostly iteration and scoring. 85% ensures the composite score formula and ranking logic are covered. |
| `backtest/walk_forward.py` | Branch coverage | **>= 90%** | Walk-forward controller guards OOS integrity. The `OOSAlreadyRunError` and `OOSCodeVersionMismatchError` paths must be covered. |
| `backtest/metrics.py` | Branch coverage | **>= 90%** | Metrics are validated by the vectorbt crosscheck, but branch coverage ensures edge cases (zero trades, single trade, zero std for Sharpe) are handled. |

**Aggregate Phase 3 gate:** `pytest --cov=src/nasdaq_ale_bot/backtest --cov=src/nasdaq_ale_bot/execution/mock_broker --cov-branch --cov-fail-under=90` must pass.

**Phase 1+2 regression guard:** existing 242 tests must remain green. `pytest tests/unit tests/integration -q` must exit 0 with no regressions.

---

## 7. Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Alpaca historical data changes on re-fetch (corp action, bar correction) | Medium | High — Golden Ledger and grid results invalidated | SHA-256 manifest with exit code 2 on mismatch; deliberate 3-step recovery procedure documented in Step 1 |
| R2 | OOS contamination via code changes between IS and OOS | Medium | Critical — walk-forward validation meaningless | `param_set_hash` includes `STRATEGY_VERSION`; `OOSCodeVersionMismatchError` blocks contaminated OOS runs |
| R3 | Fill-model ambiguity on SL+TP same-bar collision | High | High — biased backtest WR | "SL wins" (A22) with 4 pinned unit tests (SL wins, gap fill, entry+exit same bar, no partial fills) |
| R4 | Look-ahead leak in fill timing | High | Critical — inflated backtest results | Fill-timestamp invariant: bar N arm -> bar N+1 fill. Enforced by `CandleView` + BacktestRunner loop structure + dedicated test |
| R5 | Decimal drift in fill price calculation | Medium | High — Golden Ledger test breaks, silent PnL corruption | `OrderFillEvent.from_floats` is sole float->Decimal boundary; MockBroker uses it for all fills; Golden Ledger is Decimal-exact |
| R6 | Custom metrics disagree with vectorbt | Medium | Medium — incorrect WR/DD/Sharpe reported | vectorbt crosscheck on SMA-cross stand-in; 1% absolute tolerance |
| R7 | Grid harness overfits on IS (all 27 sets happen to perform well) | Low | Medium — OOS gate passes but live performance degrades | Composite score penalizes high MaxDD; OOS is the sole go/no-go gate; IS results are reported but do not gate Phase 4 |
| R8 | Walk-forward controller `_oos_runs` dict lost on process restart | Low | Medium — OOS re-run without detection | Walk-forward runs as a single process. For CI, the verdict artifact serves as the audit trail. If needed, persist `_oos_runs` to a JSON file. Deferred until Phase 4 CI integration. |
| R9 | MockBroker diverges from AlpacaBroker behavior in Phase 4 | Medium | Medium — backtest/live results differ unexpectedly | MockBroker implements the exact `BrokerProtocol`. Phase 4's `AlpacaBroker` must pass the same protocol conformance tests. Differences are in server-side execution (latency, partial fills) which MockBroker deliberately simplifies. |
| R10 | Parquet file size too large for CI (6 months x 2 symbols x 1m bars) | Low | Low — slow CI | Estimated ~50 MB per file (gzip). CI fetches once per job and caches. If too large, split into monthly files with same manifest structure. |
| R11 | vectorbt version incompatibility | Medium | Low — crosscheck fails, not production | Pinned in `pyproject.toml` extras: `vectorbt>=0.26,<1.0`. Script handles import errors gracefully with a skip message. |

---

## 8. LOC Budget Breakdown

| File | LOC estimate | Category |
|---|---|---|
| `scripts/fetch_phase3_data.py` | 150-200 | Source |
| `src/nasdaq_ale_bot/execution/mock_broker.py` | 350-500 | Source |
| `src/nasdaq_ale_bot/backtest/runner.py` | 400-600 | Source |
| `src/nasdaq_ale_bot/backtest/grid.py` | 250-350 | Source |
| `src/nasdaq_ale_bot/backtest/walk_forward.py` | 200-300 | Source |
| `src/nasdaq_ale_bot/backtest/metrics.py` | 150-250 | Source |
| `scripts/metrics_crosscheck.py` | 100-150 | Source |
| **Source subtotal** | **1600-2350** | |
| `tests/unit/test_mock_broker.py` | 300-400 | Tests |
| `tests/unit/test_backtest_runner.py` | 400-500 | Tests |
| `tests/unit/test_grid_harness.py` | 150-250 | Tests |
| `tests/unit/test_walk_forward.py` | 150-200 | Tests |
| `tests/unit/test_metrics.py` | 100-150 | Tests |
| `tests/integration/test_phase3_e2e.py` | 300-400 | Tests |
| `tests/integration/test_golden_ledger.py` | 100-150 | Tests |
| `tests/integration/test_grid_harness_mini.py` | 50-100 | Tests |
| **Tests subtotal** | **1550-2150** | |
| `tests/fixtures/golden_ledger_2024_01.json` | ~200 | Fixture (JSON) |
| `data/historical/manifest.json` | ~20 | Fixture (JSON) |
| **Fixtures subtotal** | **~220** | |
| **TOTAL** | **3370-4720** | |

**Budget compliance note:** The high end of the estimate (4720) exceeds the 4000 LOC budget. To stay within budget, the following levers are available:
- Reduce test verbosity: combine similar test cases with parametrize (saves ~200 LOC in tests).
- Simplify metrics_crosscheck.py by using a more minimal SMA strategy (saves ~50 LOC).
- Move grid harness mini integration test into the unit test file (saves ~50 LOC).

**Target:** 3000-3950 LOC (mid-budget) by exercising the above levers.

---

## 9. ADR — Consensus Decisions

### Decision

Implement a custom bar-by-bar backtest engine (`BacktestRunner`) with a `MockBroker` that implements all 14 `BrokerProtocol` methods. Fill simulation uses the "SL wins" conservative fill model (A22). Walk-forward validation enforces single-shot OOS evaluation with code-version fingerprinting. Grid harness sweeps 27 parameter combinations on IS data and ranks by composite score. Metrics are cross-checked against vectorbt on a stand-in SMA strategy. A Golden Ledger fixture pins Decimal-exact trade output for regression detection.

### Drivers

1. **Backtest reproducibility:** Identical inputs must produce identical outputs, Decimal-exact. The Golden Ledger test is the CI gate.
2. **OOS integrity:** The WR >= 55% gate on OOS data is the sole go/no-go for Phase 4. Walk-forward controller prevents contamination via `param_set_hash` with code-version fingerprint.
3. **MockBroker fidelity:** Conservative fill model (SL wins) ensures backtest WR is a floor relative to live. Gap fills at bar open prevent understating losses.

### Alternatives considered

- **Option B — vectorbt as primary engine:** Rejected. Forces stateful 6-state detection into vectorized Strategy classes. Look-ahead enforcement is convention-only (no CandleView). Fill model control insufficient for "SL wins." PLAN.md explicitly retains vectorbt for cross-check only (line 42-43).
- **Option C — Hybrid (vectorbt for metrics, custom for replay):** Rejected. Metric computation is ~200 LOC. The SMA-cross cross-check provides equivalent confidence without coupling the production metrics path to vectorbt's API surface.

### Why chosen

Option A (custom engine) is the only approach that:
- Preserves the `CandleView` look-ahead enforcement from Phase 1.
- Shares the exact `StateMachine.on_bar()` code path with the future live runner.
- Allows explicit control of fill timing (bar N arm -> bar N+1 fill) and fill ordering ("SL wins").
- Maintains Decimal-exact accounting through the existing `AccountLedger`.
- Keeps vectorbt as a validation tool rather than a production dependency.

### Consequences

- **More code:** ~1600-2350 LOC of source (engine + broker + grid + walk-forward + metrics). Offset by: zero look-ahead risk, deterministic reproduction, Phase 4 ready.
- **vectorbt as dev dependency only:** Required for `[crosscheck]` extras but not imported in production code.
- **Golden Ledger maintenance:** Any legitimate detection rule change requires regenerating the fixture. This is a feature (forces explicit acknowledgment of behavioral changes) but adds maintenance cost.
- **MockBroker simplification:** No partial fills, no slippage model, no latency simulation. These are deliberate simplifications for Phase 3. Phase 4's live broker introduces these naturally.
- **Discretionary composite score weights (Principle 5 tension):** The 0.5/0.3/0.2
  split in `composite_score` is a design choice, not spec-derived. Lifted to
  module constants (`grid.py`) so the discretionary nature is explicit at the
  code site. Any weight change is a deliberate act requiring sign-off.

### Follow-ups

1. **Schema v1 lock activation (end of Phase 3.5):** After the backtest has emitted all event types in a real run, freeze the schema source: compute SHA-256, commit to `_schema_v1_lock.txt`, remove `@pytest.mark.skip` from `test_schema_constant_locked`.
2. **Phase 4 AlpacaBroker conformance:** Phase 4's `AlpacaBroker` must pass the same `isinstance(broker, BrokerProtocol)` and method-name-set tests as MockBroker.
3. **Grid harness expansion:** If the 3-axis grid proves too coarse, Phase 3.1 can add CISD confirm window (A2) as a 4th axis. Budget: additional ~100 LOC.
4. **OOS persistence:** If CI requires cross-job OOS single-shot enforcement, persist `_oos_runs` to `results/oos_run_log.json`. Deferred until Phase 4 CI integration.
5. **Phase 3.x Apex backtest wrapper:** `scripts/backtest_apex.py` replays pinned data with `apex_mode.enabled=true` and reports Apex-rule breaches. Pure wrapper around existing replay. ~200 LOC, deferred to post-Phase-3 polish.

---

## 10. Verification Steps

Exact shell commands a reviewer runs from project root to confirm Phase 3 done-ness:

```bash
# 0. Prerequisites — Phase 2 still green
pytest tests/unit tests/integration -q  # all 242+ tests pass

# 1. Clean install with crosscheck extras
pip install -e ".[dev,crosscheck]"

# 2. Fetch historical data (requires ALPACA_API_KEY, ALPACA_SECRET_KEY)
python scripts/fetch_phase3_data.py
# Verify parquet files exist
ls data/historical/QQQ_1m_2024H1.parquet data/historical/SPY_1m_2024H1.parquet
# Verify manifest
python -c "import json; m=json.load(open('data/historical/manifest.json')); assert len(m['files'])==2"

# 3. Lint
ruff check src/nasdaq_ale_bot/backtest src/nasdaq_ale_bot/execution/mock_broker.py scripts/fetch_phase3_data.py scripts/metrics_crosscheck.py tests

# 4. Type check (Phase 3 modules)
mypy src/nasdaq_ale_bot/backtest src/nasdaq_ale_bot/execution/mock_broker.py

# 5. Unit tests — MockBroker 100% coverage
pytest tests/unit/test_mock_broker.py -v \
    --cov=src/nasdaq_ale_bot/execution/mock_broker \
    --cov-branch --cov-fail-under=100

# 6. Unit tests — BacktestRunner >= 90% coverage
pytest tests/unit/test_backtest_runner.py -v \
    --cov=src/nasdaq_ale_bot/backtest/runner \
    --cov-branch --cov-fail-under=90

# 7. Unit tests — MetricsCalculator >= 90% coverage
pytest tests/unit/test_metrics.py -v \
    --cov=src/nasdaq_ale_bot/backtest/metrics \
    --cov-branch --cov-fail-under=90

# 8. Unit tests — GridHarness >= 85% coverage
pytest tests/unit/test_grid_harness.py -v \
    --cov=src/nasdaq_ale_bot/backtest/grid \
    --cov-branch --cov-fail-under=85

# 9. Unit tests — WalkForward >= 90% coverage
pytest tests/unit/test_walk_forward.py -v \
    --cov=src/nasdaq_ale_bot/backtest/walk_forward \
    --cov-branch --cov-fail-under=90

# 10. "SL wins" fill model pinned tests
pytest tests/unit/test_mock_broker.py::test_sl_wins_when_both_hit -v
pytest tests/unit/test_mock_broker.py::test_gap_open_past_sl_fills_at_open -v
pytest tests/unit/test_mock_broker.py::test_entry_and_exit_same_bar -v

# 10a. LIMIT entry semantics tests
pytest tests/unit/test_mock_broker.py::test_limit_entry_fills_in_range -v
pytest tests/unit/test_mock_broker.py::test_limit_entry_gap_fills_at_open -v
pytest tests/unit/test_mock_broker.py::test_limit_entry_untouched_carries_forward -v

# 11. Golden Ledger Decimal-exact test
pytest tests/integration/test_golden_ledger.py -v

# 12. Integration — full 6-month E2E replay
pytest tests/integration/test_phase3_e2e.py -v

# 13. Grid harness on 1-week mini-fixture
pytest tests/integration/test_grid_harness_mini.py -v

# 14. vectorbt metrics crosscheck
python scripts/metrics_crosscheck.py

# 14a. Parquet determinism test
pytest tests/integration/test_fetch_determinism.py -v

# 15. Walk-forward OOS single-shot guard
pytest tests/unit/test_walk_forward.py::test_oos_single_shot_raises_on_repeat -v
pytest tests/unit/test_walk_forward.py::test_oos_code_version_mismatch_raises -v

# 15a. OOS fresh instantiation
pytest tests/unit/test_walk_forward.py::test_oos_fresh_instantiation -v

# 15b. StateMachine->MockBroker bridge
pytest tests/unit/test_backtest_runner.py::test_state_machine_to_broker_bridge -v

# 16. OOS verdict artifact validation (after running full pipeline)
python -c "
import json
v = json.load(open('results/phase3_oos_verdict.json'))
assert 'param_set_hash' in v
assert 'oos_wr' in v
assert isinstance(v['pass'], bool)
assert v['gate_threshold'] == 0.55
print(f'OOS WR: {v[\"oos_wr\"]:.2%} — {\"PASS\" if v[\"pass\"] else \"FAIL\"}')"

# 17. Data manifest integrity re-check
python scripts/fetch_phase3_data.py --verify-only  # exits 0 if hashes match

# 18. Pure detection layer untouched (CI guard)
git diff --name-only main..HEAD -- 'src/nasdaq_ale_bot/detection/**' | wc -l  # must be 0

# 19. Phase 1+2 CandleView + core unchanged (CI guard)
git diff --name-only main..HEAD -- 'src/nasdaq_ale_bot/core/candle.py' \
    'src/nasdaq_ale_bot/core/candle_view.py' | wc -l  # must be 0

# 20. Total LOC budget check
git diff --shortstat main..HEAD -- 'src/' 'tests/' 'scripts/' 'data/'  # additions in [2500, 4000]

# 21. Aggregate coverage gate
pytest tests/unit tests/integration -v \
    --cov=src/nasdaq_ale_bot/backtest \
    --cov=src/nasdaq_ale_bot/execution/mock_broker \
    --cov-branch --cov-fail-under=90
```

All commands must exit 0. Commands 18-19 must print `0`. Command 20 must show additions in `[2500, 4000]`.

---

## 11. Deviations from PLAN.md §3

The grid axes in §3.5 differ from PLAN.md §3 line 94 (Phase 3 AC). Per user instruction in the ralplan invocation:

| Axis | PLAN.md §3 line 94 | This plan §3.5 |
|---|---|---|
| Axis 1 | `min_penetration_ticks ∈ {1,2,3}` | `ifvg_tolerance_ticks ∈ {0,1,2}` |
| Axis 2 | `lookback_bars ∈ {10,20,30}` | `cisd_lookback_bars ∈ {10,15,20}` |
| Axis 3 | `confirm_window ∈ {10,15,20}` | `rr_cap ∈ {Decimal("1.2"), Decimal("1.3"), Decimal("1.5")}` |

**Rationale:** The user's Phase 3 brief (verbatim): "Parameter-Grid über IFVG-Toleranz, R:R-Cap (A7), CISD look-back cap (A1)". The user deliberately prioritized R:R cap tuning (§A7) and IFVG tolerance over penetration ticks and CISD confirm window. Both axis sets are 3x3x3 = 27 combinations. PLAN.md §3 is a living document; this deviation will be back-ported to PLAN.md v3 during Phase 4 kickoff.

---

## 12. Changelog

- **v1** -- initial Planner draft.
- **v2 (post-consensus, APPROVED-AFTER-ITERATION)** -- folded in Architect + Critic deltas:
  - Added §11 documenting grid axis deviations from PLAN.md §3 line 94 (user override).
  - Clarified entry-fill model as LIMIT with gap-open fallback and indefinite carry-forward for untouched limits (§3.3).
  - Documented OOS fresh instantiation contract for all stateful components (§3.6).
  - Pinned Golden Ledger params to explicit `GridParams(1, Decimal("1.3"), 20)` (Step 7, formerly Step 6).
  - Specified parquet determinism contract: sorted rows, fixed column order, `compression_level=6`, stripped pandas metadata, fixed row-group size (Step 1).
  - Lifted composite score weights to module constants in `grid.py` (§3.5); acknowledged Principle 5 tension in §9 ADR Consequences.
  - Inserted new Step 4 `MetricsCalculator` implementation spec (`backtest/metrics.py`); renumbered subsequent steps.
  - Documented StateMachine->MockBroker bridge logic in BacktestRunner (§3.4); updated data flow diagram accordingly.
