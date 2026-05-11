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

**Phase 4 — Paper Live Runner.** `alpaca_client.py` wraps `TradingClient` + `StockDataStream` behind `BrokerProtocol`. WS thread enqueues to `queue.Queue`; a dedicated engine thread drains the queue and is the only caller of `StateMachine.on_bar`. `order_manager.py` uses bracket orders (atomic SL/TP); BE moves via `BrokerProtocol.modify_bracket_stop`. `risk.py` enforces position sizing from `instruments.yaml` ATR ratio. `safety.py` owns kill switches including news CSV freshness check on startup and clock-drift guard. `runner.py` is the main loop. ≥ 1 full trading-day dry run.

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
