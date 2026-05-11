# PLAN_PHASE2.md — State Machine + HTF Bias + Phase 5/6 Pre-Wiring

**Mode:** RALPLAN-DR Deliberate
**Phase:** 2 of 6
**Author:** Planner pass (consensus loop)
**Inputs:** PLAN.md §3 Phase 2 (lines 68-82), §5.A/§5.B (lines 195-231), §9.A/§9.B (lines 342-375); ASSUMPTIONS.md §A8, §A9, §A11-A36; existing Phase 1 stubs in `src/nasdaq_ale_bot/core/state_machine.py`, `core/smt_tracker.py`, `core/candle.py`, `core/candle_view.py`.
**LOC budget:** 3000-4500 (code + tests)
**Revision:** v2 — folds Architect amendments A1-A5 (see `.omc/state/phase2_architect_review.md`)

---

## 1. Scope & Non-Goals

### In Scope (Phase 2 ships)
1. **Pre-wiring primitives** (PLAN.md §3 lines 78-82) — `AccountLedger`, `GateList`, `logging_sink`, `BrokerProtocol` 14-method extension, `instruments.yaml::futures` schema. These exist but are *passive* — Phase 2 wires them so Phases 3/4/5/6 plug in without refactoring detection or state-machine code.
2. **State Machine engine** (PLAN.md §3 lines 70-72) — full `BIAS_DETERMINATION → WAITING_FOR_SWEEP → CISD_CONFIRMATION → IFVG_FORMATION → ENTRY_EXECUTION → TRADE_MANAGEMENT → FLAT` lifecycle, single-threaded (§A21), one structlog event per transition.
3. **HTF Bias detector** with pending/active two-stage gating (§A8) — 4H unmitigated FVG, body-close break, two-bar 4H confirmation, 1H structure check, Daily agreement check.
4. **SMTTracker full implementation** — clock-anchored 1m→5m aggregation, forward-fill max 1, fail-closed UNAVAILABLE on ≥2 missing (§A12, §A13).
5. **Integration test** on `tests/fixtures/qqq_1m_sample.csv` proving ≥1 complete trade lifecycle, session rotation, AM→PM gating, SKIP_MAX_STOP.

### Explicitly NOT in Scope
- **Phase 3 backtest engine** — no `engine.py`, no parquet loaders, no metrics, no HTML report. `AccountLedger` is exercised by the integration test only.
- **Phase 4 live runner** — no Alpaca WS thread, no `runner.py` queue plumbing, no clock-drift guard. Threading contract is *asserted* in docstrings only.
- **Phase 5 Apex gates** — `GateList` exposes `base_list()` only. The Apex-specific gate classes (`TrailingDDGate`, `ConsistencyGate`, `ScalingPlanGate`, `DailyLossApexGate`) are NOT implemented in Phase 2. The `EntryGate` *protocol* and the *registration mechanism* are.
- **Phase 6 GUI** — no PyQt6, no `bot_launcher`, no `bot_gui` package, no watchdog tail. Only the JSONL sink schema v1, frozen.
- **TradovateBroker / RithmicBroker** — only the protocol surface is extended; no concrete futures broker implementation.
- **Modifying `src/nasdaq_ale_bot/detection/**`** — pure detection layer is frozen at Phase 1.
- **Modifying `core/candle.py` or `core/candle_view.py`** — Phase 1 invariants stay.

---

## 2. RALPLAN-DR Short Summary

### Principles (5)
1. **Pre-wire, don't refactor later.** Every primitive Phase 5/6 needs lands in Phase 2 with frozen interfaces, even if its body is a stub.
2. **Fail-closed by default.** Any missing data, stale snapshot, or unknown state blocks new entries; never silently passes.
3. **Single-threaded engine (§A21).** State machine never sees concurrency. Cross-thread inputs (bars, equity polls) arrive via queue; the engine drains.
4. **Schema stability over feature breadth.** The JSONL event schema is *frozen* the instant Phase 2 ships. Forward changes are additive only.
5. **Decimal for money, float for prices.** **Two** documented boundaries (A1 amendment): (a) `OrderFillEvent.from_floats` for engine-internal fill bookkeeping, (b) the **broker adapter** for `get_account_equity` / `get_realtime_equity` / `get_session_pnl` / `get_contract_spec.tick_value_usd` — adapter quantizes to cent precision (`.quantize(Decimal("0.01"))`). Anywhere else, Decimal inputs are asserted via runtime `isinstance` checks.

### Decision Drivers (top 3)
1. **D1: Phase 5 must be a config flip, not a refactor** (PLAN.md §9.A line 344). Drives the `AccountLedger` + `GateList` + 14-method `BrokerProtocol` shape.
2. **D2: Phase 6 GUI must read events without coupling to engine process** (§9.B, §A32). Drives the append-only JSONL file IPC and frozen schema v1.
3. **D3: Integration test on real 5-day fixture must produce a complete trade lifecycle** (PLAN.md §3 line 73). Drives implementation order — state machine engine must be exercised end-to-end before Phase 2 closes.

### Viable Options Considered

**Option A — Pre-wire all primitives FIRST, then state machine (CHOSEN, user-mandated).**
- Pros: Phase 5/6 unblocked from day 1; integration test naturally exercises ledger + sink; no retroactive instrumentation.
- Cons: ~600 LOC of "passive" code lands before any visible behavior; harder to demo mid-phase.

**Option B — State machine first, retrofit ledger/sink/gates later in Phase 2.**
- Pros: Earlier visible behavior; fewer "stub" classes in repo.
- Cons: Requires rewriting state-machine call sites to add ledger/gate/sink hooks; risks Phase 5 slipping because pre-wiring deferred; user explicitly rejected this ordering.
- **Invalidation:** User mandate (task brief, "Implementation order — USER-MANDATED — do not reorder"). Also violates Driver D1 because state-machine commits would land without sink instrumentation.

**Option C — Defer pre-wiring entirely to a Phase 2.5 sub-phase.**
- Pros: Phase 2 stays small (~1500 LOC).
- Cons: PLAN.md §3 Phase 2 AC explicitly requires all 14 items in one phase; defers Phase 5 unblocking; violates "no retroactive instrumentation" (§A35 rationale, line 299).
- **Invalidation:** Direct conflict with PLAN.md §3 Phase 2 acceptance criteria block.

**Chosen approach:** Option A. Five ordered sub-phases (Pre-wiring → State Machine → HTF Bias → SMTTracker → Integration test), with the pre-wiring sub-phase landing first so the state-machine implementation naturally calls into ledger/sink/gates as it's written.

---

## 3. Pre-Mortem (Deliberate-mode requirement)

### Scenario 1: Decimal/float boundary leaks, HWM regresses by 1e-9 USD on a backtest replay
- **Trigger:** Phase 3 backtest replays a fill with PnL of `0.1 + 0.2` computed in float, fed to `AccountLedger.update_realized(...)` without explicit `Decimal()` conversion. HWM stored as `Decimal("0.30000000000000004")`. Next snapshot computes `< previous HWM`, monotonicity assertion fires.
- **Blast radius:** Backtest crashes mid-replay; Phase 3 golden-ledger test goes red; potential silent corruption if the assertion is downgraded to a warning.
- **Detection:** Unit test `test_account_ledger_decimal_boundary.py` constructs synthetic fills through `OrderFillEvent.__init__` with float inputs and asserts the ledger field is `Decimal` and equal to a `Decimal("0.30")` reference. CI fails on regression.
- **Mitigation in plan:** Step 1a defines `OrderFillEvent.__init__` as the *sole* float→Decimal conversion site. All `AccountLedger` setters reject non-Decimal inputs at runtime via `isinstance(v, Decimal) or raise TypeError`. HWM setter is `@property.setter` that asserts `new >= self._hwm`. Test pinned.

### Scenario 2: JSONL schema field rename in Phase 4 silently breaks Phase 6 GUI
- **Trigger:** Phase 4 developer renames `bar_ts` → `bar_timestamp` in a structlog call. Sink writes the new field. Phase 6 GUI (still reading schema v1) sees `null` for bar timestamp on every event after the change.
- **Blast radius:** GUI displays stale "last bar" forever; no crash, no test failure, silent data loss for the user. This is the worst class of bug (silent + visible to the user who can't tell why).
- **Detection:** Unit test `test_logging_sink_schema_stability.py` round-trips every known event type through `EVENT_SCHEMA_V1` (TypedDict + pydantic `RootModel`). A second test, `test_schema_constant_locked.py`, computes a SHA-256 of the `EVENT_SCHEMA_V1` source lines and asserts equality to a pinned hash in `tests/unit/_schema_v1_lock.txt`. Any edit to the schema source forces a hash bump, which is a code-review tripwire.
- **Mitigation in plan:** Step 1c freezes `SCHEMA_VERSION = 1` and `EVENT_SCHEMA_V1` in `core/logging_sink.py`. Sink validates every event against the schema before writing; unknown fields are dropped with a WARN log; missing mandatory fields raise. Future additions: only optional fields under `fields: dict[str, Any]`, never top-level.

### Scenario 3: HTF bias two-bar 4H confirmation off-by-one — flip activates on the *breaking* 4H bar instead of the bar after
- **Trigger:** §A8 says "4H bar's body closes beyond the FVG **and** the immediately following 4H bar also body-closes on the same side." A naive implementation treats the breaking bar as bar 1 of 2 and confirms on the same bar, arming entries one 4H period (= 240 1m bars) too early.
- **Blast radius:** Integration test passes (lifecycle still completes), but Phase 3 backtest WR drops 5-10% because trades are armed on premature bias flips that get reversed. Hard to diagnose without the explicit test.
- **Detection:** Unit test `test_htf_bias_confirmation.py` constructs a synthetic 4H series with: bar N breaks FVG, bar N+1 closes opposite (rejection), bar N+2 closes same side. Asserts bias is `BIAS_FLIP_PENDING` after bar N, `NONE` (or prior bias) after bar N+1, and `BIAS_FLIP_PENDING` re-armed after bar N+2 (because bar N+1 broke the run). A separate test asserts that two consecutive same-side body closes after the FVG breach are required for `BIAS_FLIP_ACTIVE` (after Daily/1H also agree).
- **Mitigation in plan:** Step 3 specifies the `HTFBiasDetector.on_4h_bar(bar)` method with an explicit `pending_breach_bar_idx: int | None` field; confirmation requires `current_bar_idx == pending_breach_bar_idx + 1` AND same-side body close. Pinned synthetic fixture in `tests/fixtures/htf_bias_synthetic.json`.

---

## 4. Implementation Steps

LOC budget breakdown (target 3000-4500 total):
- Step 1 (Pre-wiring): ~1200 LOC (code 700 + tests 500)
- Step 2 (State Machine): ~1100 LOC (code 600 + tests 500)
- Step 3 (HTF Bias): ~700 LOC (code 350 + tests 350)
- Step 4 (SMTTracker): ~500 LOC (code 250 + tests 250)
- Step 5 (Integration test): ~300 LOC (test code only)
- **Total estimate: ~3800 LOC** (mid-budget)

---

### Step 1 — Pre-Wiring (BLOCKS everything else)

#### 1a. `core/account_ledger.py` (NEW) — ~250 LOC + 200 LOC tests

**Purpose:** Hold all monetary state for the engine. Single source of truth for daily PnL, equity, HWM. Consumed by `GateList` (Step 1b) and `logging_sink` (Step 1c).

**Concrete signatures:**
```python
from decimal import Decimal
from datetime import date, datetime
from dataclasses import dataclass, field

@dataclass(frozen=True)
class OrderFillEvent:
    """Sole float -> Decimal conversion site.

    Market data prices remain `float` upstream; this constructor performs
    the one-shot conversion to `Decimal` so AccountLedger never sees floats.
    """
    fill_ts: datetime           # tz-aware UTC required
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal                # contracts/shares
    fill_price: Decimal         # NOT float
    fees: Decimal
    realized_pnl_delta: Decimal # signed; computed by caller from entry/exit prices

    @classmethod
    def from_floats(
        cls,
        *, fill_ts: datetime, symbol: str, side: str,
        qty: float, fill_price: float, fees: float, realized_pnl_delta: float,
    ) -> "OrderFillEvent":
        """The ONLY allowed float -> Decimal boundary in the codebase.

        Quantizes to 8 decimal places using ROUND_HALF_EVEN to defeat
        accumulated float drift before storing as Decimal.
        """
        ...

class AccountLedger:
    def __init__(self, *, session_start_equity: Decimal, today: date) -> None: ...

    # Read-only fields (Decimal everywhere)
    @property
    def realized_today(self) -> Decimal: ...
    @property
    def unrealized(self) -> Decimal: ...
    @property
    def high_watermark_equity(self) -> Decimal: ...
    @property
    def session_start_equity(self) -> Decimal: ...
    @property
    def best_day_profit(self) -> Decimal: ...
    @property
    def cumulative_profit(self) -> Decimal: ...
    @property
    def profit_window_start_date(self) -> date: ...
    @property
    def current_equity(self) -> Decimal:
        """session_start_equity + realized_today + unrealized."""

    # Mutators (engine thread only — A21)
    def on_fill(self, event: OrderFillEvent) -> None:
        """Updates realized_today, recomputes equity, advances HWM if higher.
        Raises TypeError if event.realized_pnl_delta is not Decimal.
        """

    def on_unrealized_snapshot(self, snapshot_ts: datetime, unrealized: Decimal) -> None:
        """Equity-poll path. snapshot_ts must be >= last snapshot_ts (monotonic).
        Out-of-order snapshots are dropped with a WARN log, not applied.
        Calls _maybe_advance_hwm() which only INCREASES the HWM, never lowers.
        """

    def on_session_rotation(self, new_today: date, new_session_start_equity: Decimal) -> None:
        """A16 — called at 00:00 ET. Locks best_day_profit if realized_today > best_day_profit.
        Resets realized_today to Decimal('0'). Does NOT reset HWM, cumulative_profit,
        profit_window_start_date.
        """

    def _maybe_advance_hwm(self) -> None:
        """Internal. asserts new_hwm >= old_hwm; raises LedgerInvariantError on regression."""
```

**Files:**
- NEW: `src/nasdaq_ale_bot/core/account_ledger.py`
- NEW: `tests/unit/test_account_ledger.py`

**Tests (mandatory):**
- `test_decimal_only_inputs_rejected` — passing `float` to `on_fill` raises `TypeError`.
- `test_from_floats_quantizes` — `0.1 + 0.2` via `from_floats` becomes `Decimal("0.30000000")`.
- `test_hwm_monotonicity_under_replay` — feeds 1000 random snapshots with shuffled timestamps; asserts HWM only ever increases, asserts out-of-order snapshots are dropped.
- `test_eod_rotation` — calls `on_session_rotation`; asserts `realized_today == 0`, `best_day_profit` advanced if applicable, HWM preserved.
- `test_hwm_never_regresses_due_to_rounding` — feeds Decimal pairs that would float-compare as a regression but Decimal-compare as equal; asserts no error.
- `test_session_start_snapshot_immutable_within_session` — asserts `session_start_equity` cannot change between rotations.

#### 1b. `execution/gates.py` (NEW) — ~200 LOC + 150 LOC tests

**Purpose:** Compose entry-time checks into an ordered, named, observable list. Phase 5 will append Apex gates without modifying the base list.

**Concrete signatures:**
```python
from typing import Protocol, runtime_checkable
from dataclasses import dataclass

@dataclass(frozen=True)
class TradeIntent:
    setup_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    risk_usd: Decimal
    bar_ts: datetime
    natural_rr: float          # §A7

@dataclass(frozen=True)
class GateResult:
    allow: bool
    reason: str               # e.g., "SKIP_DAILY_LOSS", "SKIP_NEWS_BLACKOUT"
    gate_name: str            # for logging/debugging

@runtime_checkable
class EntryGate(Protocol):
    name: str
    def check(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult: ...

class GateList:
    def __init__(self, gates: list[EntryGate]) -> None: ...
    def evaluate(self, ledger: AccountLedger, intent: TradeIntent) -> GateResult:
        """First failing gate wins (production path). Logs every gate
        evaluation as structlog event 'GATE_EVAL' with gate_name and result.
        """
    def evaluate_all(
        self,
        ledger: AccountLedger,
        intent: TradeIntent,
        *,
        log_individual_evals: bool = False,
    ) -> list[GateResult]:
        """A5 amendment + W2 fix — diagnostic method. Evaluates every gate
        regardless of short-circuit. NOT used on the engine hot path — only
        by the integration test (Step 5) and by Phase 3 backtest analytics
        to report multi-gate rejection reasons.

        W2: `log_individual_evals` defaults to False (no GATE_EVAL events
        emitted). Integration test sets it to True for full observability.
        Phase 3 backtest analytics activates it per-sample, never per-bar,
        to prevent JSONL volume explosion over 6-month replays.
        ~20 LOC.
        """
    @classmethod
    def base_list(cls, *, strategy_cfg: StrategyConfig, news_provider: NewsProvider) -> "GateList":
        """The Phase 2 base set, in evaluation order:
            1. DailyLossGate         (§A15 realized side)
            2. ProjectedLossGate     (§A15 unrealized side)
            3. KillzoneGate          (§A9 — checks AM→PM session flag)
            4. NewsBlackoutGate      (§A10)
            5. SMTAvailabilityGate   (§A13 fail-closed UNAVAILABLE check)
            6. MaxTradesGate         (2-trade cap)
            7. MaxStopGate           (§A11 — emits SKIP_MAX_STOP)
        Returns exactly these 7 gates in this order.
        """

# Concrete gate classes (each ~30 LOC):
class DailyLossGate: ...
class ProjectedLossGate: ...
class KillzoneGate: ...
class NewsBlackoutGate: ...
class SMTAvailabilityGate: ...
class MaxTradesGate: ...
class MaxStopGate: ...
```

**Files:**
- NEW: `src/nasdaq_ale_bot/execution/gates.py`
- NEW: `tests/unit/test_gates.py`

**Tests:**
- `test_base_list_exact_set_and_order` — asserts `GateList.base_list(...)` returns exactly 7 gates in the documented order. Pinned to catch silent additions/reorderings (Phase 5 invariant).
- `test_first_failing_gate_short_circuits` — synthetic gate list with gates 1+2 failing; asserts only gate 1's reason is returned.
- `test_max_stop_gate_emits_structlog_event` — uses `structlog.testing.capture_logs`, asserts `SKIP_MAX_STOP` event with required fields.
- `test_projected_loss_gate_uses_unrealized` — ledger with realized -800, unrealized -500, risk 300 → projected -1600 < -1500 → block.
- `test_smt_availability_gate_fail_closed` — passing `SMTVerdict.UNAVAILABLE` blocks; passing `None` (no SMT data at all) also blocks.
- `test_each_gate_has_unique_name` — invariant for log routing.
- `test_evaluate_all_runs_every_gate` (A5) — stub 7 gates where gates 1 and 3 fail; `evaluate_all` returns exactly 7 `GateResult`s preserving order; `evaluate` returns only the first failing one.

#### 1c. `core/logging_sink.py` (NEW) — ~200 LOC + 200 LOC tests — **SCHEMA FROZEN**

**Purpose:** Mirror every structlog event to `.omc/state/bot_events.jsonl` with stable schema v1. Phase 6 GUI consumes this file; the schema is immutable after Phase 2 ships.

**Concrete signatures:**
```python
from typing import TypedDict, Literal, Any
import structlog

SCHEMA_VERSION: Final[int] = 1  # FROZEN — bump only with explicit ADR

class EventSchemaV1(TypedDict):
    schema_version: int                  # MANDATORY — always 1 in this file
    ts_utc: str                          # MANDATORY — ISO-8601 UTC, microsecond precision
    level: Literal["debug","info","warning","error","critical"]  # MANDATORY
    event: str                           # MANDATORY — e.g., "STATE_TRANSITION"
    state: str | None                    # MANDATORY (may be null) — current StrategyState
    bar_ts: str | None                   # MANDATORY (may be null) — ISO-8601 UTC
    fields: dict[str, Any]               # MANDATORY (may be empty) — additive bag

EVENT_SCHEMA_V1: Final = EventSchemaV1   # exported for Phase 6 import

class JsonlSink:
    def __init__(
        self,
        *,
        path: Path = Path(".omc/state/bot_events.jsonl"),
        rotate_at_bytes: int = 50 * 1024 * 1024,
        max_backups: int = 5,
        fsync_every_n: int = 10,
    ) -> None: ...

    def __call__(self, logger, method_name, event_dict) -> dict:
        """structlog processor signature. Validates against EVENT_SCHEMA_V1,
        writes one line, returns event_dict unchanged (so downstream processors
        — console renderer, etc — still see it).

        On schema validation failure: writes a SCHEMA_VIOLATION event
        constructed from a HARD-CODED TEMPLATE with all 7 mandatory fields
        pre-populated (schema_version=1, ts_utc=now_iso, level='error',
        event='SCHEMA_VIOLATION', state=None, bar_ts=None, fields={
        'offending_event': str(original_event_repr)[:500]}). The template
        bypasses the validation path entirely — W4 fix — to prevent a
        theoretical infinite loop where a bug in the fallback path itself
        triggers further schema failures. DOES NOT write the original event.
        Never raises into the engine thread.
        """

    def _rotate_if_needed(self) -> None:
        """When current file size >= rotate_at_bytes, renames
        bot_events.jsonl -> .jsonl.1 (and .1->.2, ..., .4->.5, dropping .5).
        """

    def _fsync_if_needed(self) -> None: ...

def install_jsonl_sink(sink: JsonlSink) -> None:
    """Adds sink to structlog's processor chain after the redaction processor
    (so api_key/secret_key are scrubbed before hitting JSONL).
    """
```

**Files:**
- NEW: `src/nasdaq_ale_bot/core/logging_sink.py`
- NEW: `tests/unit/test_logging_sink.py`
- NEW: `tests/unit/_schema_v1_lock.txt` (pinned SHA-256 of `EventSchemaV1` source lines)

**Tests:**
- `test_schema_version_constant_is_one` — guard against accidental bumps.
- `test_round_trip_every_event_type` — for each event in {`STATE_TRANSITION`, `BIAS_FLIP_PENDING`, `BIAS_FLIP_ACTIVE`, `GATE_EVAL`, `SKIP_MAX_STOP`, `SKIP_PROJECTED_LOSS_LIMIT`, `SKIP_NEWS_BLACKOUT`, `TRADE_INTENT`, `TRADE_FILLED`, `TRADE_EXIT`, `TIME_EXIT`, `BE_MOVED`, `SESSION_ROTATION`, `SCHEMA_VIOLATION`}, build a sample event, push through sink, parse back from file, assert equality.
- `test_mandatory_fields_enforced` — missing `bar_ts` raises validation; missing `event` triggers `SCHEMA_VIOLATION` write.
- `test_rotation_at_50mb` — uses small `rotate_at_bytes=1024`, writes 100 events, asserts `.jsonl.1` exists.
- `test_max_5_backups_drops_oldest` — same with 6 rotations, asserts `.jsonl.5` is the oldest and `.jsonl.6` does not exist.
- `test_fsync_cadence` — monkey-patches `os.fsync`, asserts called every N events.
- `test_redaction_runs_before_sink` — logs an event with `api_key="sk-..."`, asserts JSONL line contains `***` not the key.
- `test_schema_constant_locked` — **A2 amendment: marked `@pytest.mark.skip(reason="Lock activated at end of Phase 3.5 — see PLAN.md §9.B follow-ups")` in Phase 2.** Test body ships fully implemented (reads source, computes SHA-256, compares to `_schema_v1_lock.txt`). `_schema_v1_lock.txt` ships with placeholder `PENDING_PHASE_3_5`. Lock activation is a Phase 3.5 AC: once backtest has emitted all event types in a real run, freeze the schema source, compute the real SHA-256, commit it to `_schema_v1_lock.txt`, and remove the `@skip` marker. Rationale: Phase 3 may discover fields (`fill_source`, `is_sample`) that should be top-level in v1 rather than absorbed into `fields: dict`.
- `test_sink_never_raises_into_engine` — feed a deliberately broken event, assert sink swallows + writes `SCHEMA_VIOLATION`, never raises.

#### 1d. `execution/broker.py` (NEW — Phase 1 had `__init__.py` only) — ~150 LOC + 50 LOC tests

**Purpose:** Define the 14-method `BrokerProtocol` (extension of A20's 9 methods). Alpaca stub raises `NotImplementedError` on the 5 new methods (only used when `apex_mode.enabled`).

**Concrete signatures:**
```python
from typing import Protocol, runtime_checkable, AsyncIterator
from decimal import Decimal
from datetime import date, datetime

@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    tick_size: Decimal
    tick_value_usd: Decimal
    contract_size: int
    rth_session: str         # e.g., "CME_GLOBEX"
    maintenance_window: str | None

@runtime_checkable
class BrokerProtocol(Protocol):
    # --- Existing 9 from §A20 ---
    def place_bracket(self, *, symbol, side, qty, entry, stop, take_profit, client_order_id) -> "OrderRef": ...
    def modify_bracket_stop(self, order_id, new_stop_price) -> None: ...
    def cancel_all(self, symbol: str | None = None) -> None: ...
    def flatten(self, symbol: str | None = None) -> None: ...
    def get_positions(self) -> list["Position"]: ...
    def get_account_equity(self) -> Decimal: ...
    def get_order(self, client_order_id: str) -> "OrderState | None": ...
    def get_trading_calendar(self, day: date) -> "TradingDay": ...
    def stream_bars(self, symbols: list[str]) -> AsyncIterator["Bar"]: ...

    # --- NEW in Phase 2 (PLAN.md §3 line 80) ---
    def get_contract_spec(self, symbol: str) -> ContractSpec:
        """Phase 5 use; Alpaca raises NotImplementedError."""
    def get_session_pnl(self) -> Decimal:
        """Broker-reported realized PnL since session start. Used for
        cross-check against AccountLedger.realized_today on reconnect."""
    def get_realtime_equity(self) -> Decimal:
        """A26 — feeds AccountLedger.on_unrealized_snapshot via the engine
        thread. Live runner polls at ≥1 Hz."""
    def assert_market_open(self, ts: datetime) -> None:
        """Raises MarketClosedError if ts is outside RTH for the broker's
        primary symbol. Phase 5 futures session check."""
    def submit_market_flatten(self, symbol: str) -> "OrderRef":
        """A26 hard-breach path. Distinct from flatten() because it returns
        an order ref the engine can confirm via get_order()."""

class AlpacaBrokerStub:
    """Minimal stub — Phase 4 ships the real implementation.
    All NEW methods raise NotImplementedError. The 9 existing methods raise
    NotImplementedError too (Phase 1 ships them as protocol-only).

    A1 amendment — SECOND Decimal boundary:
    `get_account_equity`, `get_realtime_equity`, `get_session_pnl`, and
    `ContractSpec.tick_value_usd` are the broker-adapter's float->Decimal
    conversion site. Every concrete adapter (AlpacaBroker, TradovateBroker,
    RithmicBroker) is REQUIRED to quantize to cent precision:
        return Decimal(str(raw_float)).quantize(Decimal('0.01'), ROUND_HALF_EVEN)
    for USD; or to the instrument's tick precision for contract-native values.
    The abstract base `_QuantizingBrokerMixin` provides helpers
    `_q_usd(raw: float) -> Decimal` and `_q_tick(raw: float, tick: Decimal) -> Decimal`
    which adapters MUST use. Any adapter method returning Decimal without
    going through these helpers is a bug caught by `test_broker_adapter_returns_quantized_decimal`.
    """
```

**Files:**
- NEW: `src/nasdaq_ale_bot/execution/broker.py`
- NEW: `tests/unit/test_broker_protocol.py`

**Tests:**
- `test_broker_protocol_has_14_methods` — uses `inspect.getmembers(BrokerProtocol, predicate=inspect.isfunction)`, asserts count == 14, asserts the exact set of names is pinned.
- `test_alpaca_stub_implements_protocol` — `isinstance(AlpacaBrokerStub(), BrokerProtocol)` is `True`.
- `test_apex_methods_raise_not_implemented` — calling each of the 5 new methods on the stub raises `NotImplementedError`.
- `test_broker_adapter_returns_quantized_decimal` (A1) — instantiates a `_QuantizingBrokerMixin` subclass fed raw floats (e.g., `0.1 + 0.2`, `12345.6789`); asserts every Decimal-returning method output equals `.quantize(Decimal('0.01'), ROUND_HALF_EVEN)`. Also asserts `isinstance(result, Decimal)`.
- `test_broker_protocol_method_name_set_pinned` (O4) — pins the **set** of method names (not just the count) to `{'place_bracket', 'modify_bracket_stop', ..., 'submit_market_flatten'}`. Adding a legitimate 15th method requires an explicit set update, documented in the ADR.

#### 1e. `config/instruments.yaml` futures schema (MODIFIED) — ~30 LOC + 50 LOC tests

**Modify** the existing `config/instruments.yaml` (Phase 1 has `primary` and `correlated` blocks). Add an optional `futures` sub-block under each instrument, populated for MNQ and ES.

```yaml
mnq:
  symbol: MNQ
  tick: 0.25
  point_value: 2.0
  atr_ratio_vs_nq: 1.0
  futures:
    contract_size: 2
    tick_value_usd: 0.50
    margin_requirement_usd: 1500
    rth_session: CME_GLOBEX
    maintenance_window: "17:00-18:00 ET"
es:
  symbol: ES
  tick: 0.25
  point_value: 50.0
  atr_ratio_vs_nq: 1.0
  futures:
    contract_size: 1
    tick_value_usd: 12.50
    margin_requirement_usd: 13200
    rth_session: CME_GLOBEX
    maintenance_window: "17:00-18:00 ET"
```

**Files:**
- MODIFIED: `config/instruments.yaml`
- MODIFIED: `src/nasdaq_ale_bot/settings.py` (add `FuturesSpec` pydantic model + optional field on `InstrumentSpec`)
- NEW: `tests/unit/test_instruments_futures_schema.py`

**Tests:**
- `test_qqq_has_no_futures_block` — backward compat; equities don't break.
- `test_mnq_futures_parsed` — `instruments.mnq.futures.tick_value_usd == Decimal('0.50')`.
- `test_futures_block_optional` — instrument without `futures:` parses cleanly.

---

### Step 2 — State Machine Engine (~600 LOC code + 500 LOC tests)

**Purpose:** Replace the `NotImplementedError` stub with the full `BIAS_DETERMINATION → WAITING_FOR_SWEEP → CISD_CONFIRMATION → IFVG_FORMATION → ENTRY_EXECUTION → TRADE_MANAGEMENT → FLAT` lifecycle. Single-threaded contract per §A21. Every transition emits exactly one structlog event.

**Files:**
- MODIFIED: `src/nasdaq_ale_bot/core/state_machine.py`
- NEW: `tests/unit/test_state_machine_transitions.py`
- NEW: `tests/unit/test_state_machine_session_rotation.py`
- NEW: `tests/unit/test_state_machine_threading_doc.py`

**Concrete signatures:**
```python
class ThreadingContractViolation(RuntimeError):
    """A4 amendment — raised if on_bar is called from a thread other than
    the one that constructed the StateMachine. Enforces §A21 at runtime."""

class StateMachine:
    def __init__(
        self,
        *,
        bias_detector: HTFBiasDetector,
        smt_tracker: SMTTracker,
        gate_list: GateList,
        ledger: AccountLedger,
        instrument: InstrumentSpec,
        strategy_cfg: StrategyConfig,
    ) -> None:
        self.state = StrategyState.BIAS_DETERMINATION
        self._session_date: date | None = None
        self._am_order_placed: bool = False        # §A9
        self._trades_today: int = 0                # 2-trade cap
        self._active_setup: Setup | None = None
        self._bars: list[Candle] = []              # rolling window for CandleView
        self._last_bar_ts: datetime | None = None  # R6 re-entry dedup
        self._owner_thread_id: int = threading.get_ident()  # A4 amendment
        self._log = structlog.get_logger(__name__)

    def on_bar(self, bar: Candle) -> list[StateEvent]:
        """SOLE entry point. Single-threaded — enforced at runtime (A4).
        Returns the list of state transitions triggered by this bar (usually 0 or 1,
        rarely 2 if a setup completes and immediately re-arms).
        Side effects: appends to self._bars; may submit orders via gate_list +
        broker; emits structlog events.
        """
        # A4 amendment — runtime threading contract enforcement
        if threading.get_ident() != self._owner_thread_id:
            raise ThreadingContractViolation(
                f"StateMachine.on_bar called from thread "
                f"{threading.get_ident()} but owner is {self._owner_thread_id}"
            )
        # R6 re-entry dedup — same bar twice is a no-op
        if self._last_bar_ts is not None and bar.ts <= self._last_bar_ts:
            return []
        self._last_bar_ts = bar.ts
        events: list[StateEvent] = []
        self._bars.append(bar)
        self._maybe_rotate_session(bar.ts)         # §A16
        view = CandleView(self._bars, len(self._bars) - 1)

        # Dispatch table on self.state
        handler = self._handlers[self.state]
        new_state, reason = handler(view)
        if new_state != self.state:
            events.append(self._transition(new_state, reason, bar.ts))
        return events

    def _transition(self, new_state, reason, bar_ts) -> StateEvent: ...
    def _maybe_rotate_session(self, bar_ts: datetime) -> None: ...

    # State handlers — one per StrategyState
    def _handle_bias_determination(self, view) -> tuple[StrategyState, str]: ...
    def _handle_waiting_for_sweep(self, view) -> tuple[StrategyState, str]: ...
    def _handle_cisd_confirmation(self, view) -> tuple[StrategyState, str]: ...
    def _handle_ifvg_formation(self, view) -> tuple[StrategyState, str]: ...
    def _handle_entry_execution(self, view) -> tuple[StrategyState, str]: ...
    def _handle_trade_management(self, view) -> tuple[StrategyState, str]: ...
    def _handle_flat(self, view) -> tuple[StrategyState, str]: ...
```

**Tests:**
- `test_full_lifecycle_synthetic` — hand-crafted 50-bar fixture that walks all 7 states; asserts exact transition sequence + reasons.
- `test_one_event_per_transition` — captures structlog events; asserts transition count == event count.
- `test_session_rotation_resets_counters` — feed bars across 23:59 ET → 00:01 ET; assert `_trades_today == 0`, `_am_order_placed == False`, `ledger.realized_today == Decimal("0")`.
- `test_am_pm_gating` — submit AM order, assert `_am_order_placed == True`, assert `KillzoneGate` blocks PM entries for that session.
- `test_skip_max_stop_logged` — synthetic setup with SL distance > max_stop, assert state stays `WAITING_FOR_SWEEP`, assert `SKIP_MAX_STOP` event emitted.
- `test_threading_contract_documented` — reads module docstring of `state_machine.py`, asserts it contains substring "single-threaded" and "A21".
- `test_re_entry_during_replay_safe` — same `Bar` instance fed twice; second call is a no-op (idempotent — the `bar.ts` deduplication prevents double-processing).
- `test_cross_thread_call_raises` (A4) — construct `StateMachine` in main thread; spawn a `threading.Thread` that calls `sm.on_bar(bar)`; assert `ThreadingContractViolation` raised, state unchanged.

---

### Step 3 — HTF Bias Detector (~350 LOC code + 350 LOC tests)

**Purpose:** Implement §A8 — most-recent-unmitigated 4H FVG, body-close break with two-bar confirmation, gated by 1H structure and Daily agreement.

**Files:**
- NEW: `src/nasdaq_ale_bot/bias/htf_bias.py`
- NEW: `src/nasdaq_ale_bot/bias/timeframe.py` (1m → 1h, 1m → 4h, 1m → 1d aggregation)
- NEW: `tests/unit/test_htf_bias.py`
- NEW: `tests/fixtures/htf_bias_synthetic.json`

**Concrete signatures:**
```python
class HTFBias(StrEnum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"

class FlipState(StrEnum):
    INACTIVE = "INACTIVE"
    PENDING = "PENDING"     # 4H confirmed, waiting for 1H + Daily
    ACTIVE = "ACTIVE"       # all three timeframes agree

@dataclass
class HTFBiasState:
    bias: HTFBias
    flip_state: FlipState
    pending_breach_4h_idx: int | None
    last_unmitigated_4h_fvg: FVG | None

class HTFBiasDetector:
    def __init__(self, instrument: InstrumentSpec) -> None: ...

    def on_1m_bar(self, bar: Candle) -> HTFBiasState:
        """Aggregates into 1H/4H/Daily internally via timeframe.py.
        Returns the current state. Emits structlog BIAS_FLIP_PENDING /
        BIAS_FLIP_ACTIVE events on transitions only, never on no-op bars.
        """

    def _on_4h_close(self, bar_4h: Candle, idx_4h: int) -> None: ...
    def _on_1h_close(self, bar_1h: Candle) -> None: ...
    def _on_daily_close(self, bar_d: Candle) -> None: ...
    def _check_flip_promotion(self) -> None:
        """Promotes PENDING -> ACTIVE iff 4H confirmed (two-bar) AND
        1H structure agrees AND Daily body-close agrees.
        """
```

**Tests:**
- `test_4h_unmitigated_fvg_detected` — synthetic 10-bar 4H series with one bullish FVG; assert detector tracks it as `last_unmitigated_4h_fvg`.
- `test_4h_fvg_mitigated_by_body_clears` — body-close back through the FVG removes it from "unmitigated" set.
- `test_two_bar_confirmation_required` — bar N breaks, bar N+1 closes opposite → `flip_state == INACTIVE`. Bar N+1 closes same side → `flip_state == PENDING`.
- `test_off_by_one_pre_mortem_scenario_3` — exact scenario from §3.3 above.
- `test_pending_blocks_until_daily_agrees` — 4H confirmed but Daily still closes against → `flip_state == PENDING`, no `BIAS_FLIP_ACTIVE` event.
- `test_pending_blocks_until_1h_structure_agrees` — 4H + Daily agree, 1H structure says LL/LH for a long flip → still `PENDING`.
- `test_active_promotion_emits_event` — capture structlog, assert `BIAS_FLIP_ACTIVE` emitted exactly once on promotion.
- `test_first_session_starts_none` — fresh detector, no history → `bias == NONE`.

---

### Step 4 — SMTTracker Full Implementation (~250 LOC code + 250 LOC tests)

**Purpose:** Replace the Phase 1 stub in `core/smt_tracker.py`. Clock-anchored 1m → 5m aggregation per §A12, forward-fill max 1, fail-closed UNAVAILABLE on ≥2 missing bars per §A13. Latches 5m verdicts so intra-5m updates don't flip mid-bar.

**Files:**
- MODIFIED: `src/nasdaq_ale_bot/core/smt_tracker.py`
- NEW: `tests/unit/test_smt_tracker_aggregation.py`
- NEW: `tests/unit/test_smt_tracker_fail_closed.py`

**Concrete signatures:**
```python
class SMTVerdict(StrEnum):
    BULLISH_DIVERGENCE = "BULLISH_DIVERGENCE"
    BEARISH_DIVERGENCE = "BEARISH_DIVERGENCE"
    NONE = "NONE"
    UNAVAILABLE = "UNAVAILABLE"        # §A13 fail-closed

class SMTTracker:
    def __init__(self, *, primary_symbol: str, correlated_symbol: str) -> None:
        self._primary_5m_buffer: list[Candle] = []
        self._correlated_5m_buffer: list[Candle] = []
        self._current_5m_anchor: datetime | None = None  # 09:30, 09:35, ...
        self._latched_verdict: SMTVerdict = SMTVerdict.NONE
        self._missing_streak_primary: int = 0
        self._missing_streak_correlated: int = 0

    def on_1m_bar_pair(
        self,
        primary_bar: Candle | None,
        correlated_bar: Candle | None,
        bar_ts: datetime,
    ) -> SMTVerdict:
        """Both args may be None (missing bar). Updates internal state,
        emits a 5m bar to the buffer when the 5m anchor closes, and
        re-evaluates the latched verdict by calling detection/smt_pure.py.
        Returns the current latched verdict.

        Forward-fill rule: if one symbol is None and missing_streak == 0,
        carry forward the previous bar's close as a synthetic OHLC.
        If missing_streak >= 1 (i.e., this is the 2nd consecutive miss),
        latch UNAVAILABLE.
        """

    def _close_5m_anchor(self, anchor_ts: datetime) -> None: ...
    def _is_anchor_boundary(self, bar_ts: datetime) -> bool:
        """09:30, 09:35, 09:40, ... ET (clock-anchored, not session-relative)."""
```

**Tests:**
- `test_clock_anchored_5m_boundaries` — 1m bars at 09:30..09:34 produce one 5m bar at 09:30.
- `test_forward_fill_one_missing` — missing primary bar at 09:32 → synthetic flat bar; verdict still computable.
- `test_two_missing_blocks` — missing bars at 09:32, 09:33 → `UNAVAILABLE` latched, persists until next clean 5m anchor.
- `test_unavailable_persists_within_5m` — once latched within a 5m window, doesn't flip mid-window.
- `test_verdict_latches_at_5m_close` — verdict computed once at 5m close, returned for all intra-5m queries until next close.
- `test_unavailable_blocks_smt_gate` — feeds tracker into `SMTAvailabilityGate`, asserts blocks entry.

---

### Step 5 — Integration Test (~300 LOC, test code only)

**Purpose:** Prove the end-to-end Phase 2 system on real data.

**Files:**
- NEW: `tests/integration/test_phase2_lifecycle.py`
- NEW (A3 amendment — **MANDATORY deliverable**, not fallback): `scripts/fetch_phase2_fixture.py` — fetches QQQ 1m bars for a pinned window from Alpaca, writes `tests/fixtures/qqq_1m_sample.csv`, and updates `tests/fixtures/data_hashes.json` with the SHA-256. **`qqq_1m_sample.csv` is NOT committed to git** (precedent for Phase 3 large parquet fixtures). `.gitignore` excludes it. CI runs the fetch script once per job, verifies the resulting SHA-256 matches the pinned value, and then runs the integration test.
- NEW: `tests/fixtures/data_hashes.json` with the pinned SHA-256 for `qqq_1m_sample.csv`.

**Test cases (all in one file, sharing a fixture):**
- `test_at_least_one_completed_lifecycle` — replay full fixture through `StateMachine.on_bar`, assert ≥1 trade reaches `TRADE_MANAGEMENT → FLAT`.
- `test_session_counter_reset_at_midnight_et` — assert `ledger.realized_today` resets between session boundaries in the fixture.
- `test_am_pm_killzone_gating_observed` — find a session in the fixture where an AM trade is placed, assert no PM trade is placed in the same session.
- `test_skip_max_stop_event_emitted_at_least_once` — assert at least one `SKIP_MAX_STOP` JSONL line in the test's sink output (use a synthetic large-stop bar injected if natural fixture doesn't trigger).
- `test_jsonl_sink_round_trip_after_replay` — after replay, parse `bot_events.jsonl` line by line, assert every line validates against `EVENT_SCHEMA_V1`.
- `test_no_lookahead_during_integration` — wrap `CandleView.__getitem__` to record max-k accessed; assert max-k ≤ current-i for every detection call.
- `test_decimal_only_in_ledger_after_replay` — introspect ledger fields, assert all are `Decimal` instances.
- `test_hwm_monotonic_after_full_replay` — record HWM after every snapshot during replay, assert non-decreasing sequence.
- `test_evaluate_all_on_representative_setup` (A5) — pick one setup from the fixture replay, call `gate_list.evaluate_all(ledger, intent)`, assert the result length == 7 (all gates executed) and that the first failing result matches what `evaluate()` returned.
- `test_fetch_script_reproduces_pinned_sha256` (A3) — runs `scripts/fetch_phase2_fixture.py` in a tmpdir, computes SHA-256 of output, asserts equality to `data_hashes.json`. Guards against Alpaca data drift going silent.

---

## 5. Expanded Test Plan (Deliberate-mode requirement)

### Unit tests (per module, coverage targets)
| Module | Coverage target | Test file |
|---|---|---|
| `core/account_ledger.py` | ≥95% branch | `test_account_ledger.py` |
| `execution/gates.py` | ≥95% branch | `test_gates.py` |
| `core/logging_sink.py` | ≥95% branch (incl. rotation/fsync paths) | `test_logging_sink.py` |
| `execution/broker.py` | 100% line (small file) | `test_broker_protocol.py` |
| `bias/htf_bias.py` | ≥90% branch | `test_htf_bias.py` |
| `bias/timeframe.py` | ≥95% branch | `test_timeframe_aggregation.py` |
| `core/smt_tracker.py` | ≥95% branch | `test_smt_tracker_*.py` |
| `core/state_machine.py` | ≥85% branch | `test_state_machine_*.py` |

### Integration tests
- `test_phase2_lifecycle.py` — see Step 5 above.
- `test_session_rotation_across_dst.py` — synthetic bars crossing 2024-03-10 DST → assert no double-rotation, no missed rotation.
- `test_gate_list_full_eval_on_real_setup.py` — feed a real setup from the fixture through all 7 gates, assert decisions are reproducible.

### E2E tests
- `test_phase2_full_replay.py` — full `qqq_1m_sample.csv` replay; gates run, ledger updates, sink writes; final invariants checked (HWM monotonic, all events schema-valid, session counters reset N times where N = trading days in fixture).

### Observability tests
- `test_one_structlog_event_per_state_transition.py` — for every test in `test_state_machine_transitions.py`, assert `len(captured_events) == len(expected_transitions)`.
- `test_schema_round_trip_all_event_types.py` — covered in `test_logging_sink.py::test_round_trip_every_event_type`.
- `test_no_unstructured_print_calls.py` — `grep -r 'print(' src/nasdaq_ale_bot/ | grep -v test_` returns zero (engine writes structlog only).

---

## 6. Acceptance Criteria

### Copied from PLAN.md §3 Phase 2 (verbatim, all 14 items)
1. `StrategyState` enum + `StateMachine.on_bar(bar) -> list[StateEvent]` return type defined; each transition emits exactly one `StateEvent`.
2. Full `(BIAS → SWEEP → CISD → IFVG → ENTRY → MANAGE → FLAT)` lifecycle on synthetic fixture.
3. `structlog` JSON event on every transition: `from_state`, `to_state`, `reason`, `bar_ts`.
4. Integration test on `tests/fixtures/qqq_1m_sample.csv` (≥5 trading days, ≥1950 bars) emits ≥1 completed trade lifecycle.
5. HTF bias yields `LONG | SHORT | NONE` for every trading day in the fixture.
6. AM → PM killzone state test (§A9).
7. Session rotation test — counters reset at 00:00 ET (§A16).
8. `SKIP_MAX_STOP` structured log event (§A11).
9. `AccountLedger` primitive with the 7 Decimal fields, HWM monotonicity + EOD rotation tests (§A25-A30 pre-wiring).
10. `GateList` composition with `base_list()` returning exactly the v1 set.
11. `BrokerProtocol` extended to 14 methods, protocol unit test asserts count.
12. `core/logging_sink.py` writes schema v1 JSONL; round-trip + rotation tested.
13. `instruments.yaml` futures schema added; MNQ/ES populated.
14. `apex_mode: false` behavior unchanged (asserted by `test_base_list_exact_set_and_order`).

### Additional ACs for the 3 risks flagged in this plan
15. **Decimal consistency:** `test_decimal_only_inputs_rejected` and `test_from_floats_quantizes` pass; no float ever reaches an `AccountLedger` setter.
16. **HWM monotonicity:** `test_hwm_monotonicity_under_replay` passes 1000 shuffled snapshots without regression; `LedgerInvariantError` raised on contrived regression.
17. **Schema stability:** `test_round_trip_every_event_type` covers all 14 listed event types. `test_schema_constant_locked` ships with body implemented but `@pytest.mark.skip`-ped; activation deferred to end of Phase 3.5 (A2).
18. **Broker Decimal boundary:** `test_broker_adapter_returns_quantized_decimal` passes; every concrete adapter routes raw-float equity/pnl returns through `_QuantizingBrokerMixin._q_usd` (A1).
19. **Threading contract:** `test_cross_thread_call_raises` passes; `StateMachine.__init__` captures `_owner_thread_id`; `on_bar` first line raises `ThreadingContractViolation` on mismatch (A4).
20. **Fixture provenance:** `scripts/fetch_phase2_fixture.py` exists; `tests/fixtures/qqq_1m_sample.csv` is `.gitignore`d; `test_fetch_script_reproduces_pinned_sha256` passes (A3).
21. **Gate diagnostic:** `GateList.evaluate_all()` exists; `test_evaluate_all_runs_every_gate` + `test_evaluate_all_on_representative_setup` pass (A5).

---

## 7. Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Decimal/float boundary leaks → HWM regresses on rounding | Medium | High (silent corruption of equity curve) | `OrderFillEvent.from_floats` is the *only* float→Decimal site; runtime `isinstance` checks on all setters; `test_hwm_monotonicity_under_replay` (Step 1a) |
| R2 | HWM race under future broker equity polling thread | Medium | High (Phase 5 trailing DD wrong) | `on_unrealized_snapshot(snapshot_ts, ...)` rejects out-of-order ts; documented "engine thread only"; equity poll is queued like bars in Phase 4; `test_hwm_monotonicity_under_replay` covers shuffled order |
| R3 | JSONL schema field rename in Phase 4+ silently breaks Phase 6 GUI | Medium | Critical (silent user-visible data loss) | `EVENT_SCHEMA_V1` TypedDict; `_schema_v1_lock.txt` SHA-256 tripwire; `SCHEMA_VIOLATION` events; round-trip test covers all known event types |
| R4 | HTF bias 4H two-bar confirmation off-by-one | Medium | High (5-10% WR drop, hard to diagnose) | Pre-mortem scenario 3; explicit `pending_breach_4h_idx` field; `test_off_by_one_pre_mortem_scenario_3` pins the contract |
| R5 | SMTTracker clock drift across DST | Low | Medium (5m anchors misaligned for 1 day/year) | `_is_anchor_boundary` uses America/New_York `zoneinfo`, not naive UTC offset; DST-crossing test in `test_smt_tracker_aggregation.py` |
| R6 | State machine re-entry on replay (same bar fed twice) | Low | Medium (double-counted trades) | `bar.ts` dedup at top of `on_bar`; `test_re_entry_during_replay_safe` |
| R7 | `GateList.base_list()` silently drifts when a developer adds a Phase 5 gate without flag-gating | Medium | High (Phase 5 invariant violated) | `test_base_list_exact_set_and_order` pinned to exactly 7 gates with names; CI fails on drift |
| R8 | `qqq_1m_sample.csv` fixture doesn't exist or has < 1950 bars | Medium | High (Step 5 blocked) | Step 5 includes `scripts/fetch_phase2_fixture.py` fallback; SHA-256 pinned in `tests/fixtures/data_hashes.json` |
| R9 | Phase 1 stub `core/smt_tracker.py` API differs from full impl, breaks call sites | Low | Medium | Step 4 keeps the existing function names; only `__init__` signature and internal state change; existing unit tests must keep passing |
| R10 | Schema v1 frozen prematurely; Phase 3 discovers fields that should be top-level but are absorbed into `fields: dict` | Medium | Medium | A2 — ship schema + round-trip test in Phase 2; `_schema_v1_lock.txt` with `PENDING_PHASE_3_5` placeholder; lock-activation test `@skip`-ped until end of Phase 3.5 |
| R11 | Broker adapter float→Decimal boundary undocumented; silent precision drift on equity polls | Medium | High | A1 — `_QuantizingBrokerMixin._q_usd` + `_q_tick` helpers; every adapter MUST route through them; `test_broker_adapter_returns_quantized_decimal` pinned |
| R12 | Docstring-only threading contract rots; Phase 4 WS thread corrupts state silently | Low (Phase 2) / High (Phase 4) | High | A4 — `_owner_thread_id` capture + `on_bar` runtime check raising `ThreadingContractViolation`; `test_cross_thread_call_raises` pinned |
| R13 | Phase 3 backtest developers reach into `GateList._gates` for multi-rejection reporting, hard-coding order knowledge | Medium | Medium | A5 — public `GateList.evaluate_all()` diagnostic method; Phase 3 reporting code MUST use it |
| R14 | 14-method protocol count test brittle — legitimate 15th method forces re-pin without review | Low | Low | O4 — `test_broker_protocol_method_name_set_pinned` pins method **names**, not count; method-addition policy in ADR |

---

## 8. Verification Steps

Exact shell commands a reviewer runs from project root to confirm Phase 2 done-ness:

```bash
# 1. Clean install
pip install -e ".[dev]"

# 2. Lint
ruff check src/nasdaq_ale_bot tests

# 3. Type check (Phase 2 expands the strict-typed surface)
mypy src/nasdaq_ale_bot/core src/nasdaq_ale_bot/bias src/nasdaq_ale_bot/execution

# 4. Unit tests with coverage gates
pytest tests/unit -v --cov=src/nasdaq_ale_bot --cov-branch \
    --cov-fail-under=90 \
    --cov-report=term-missing

# 5. Targeted coverage gate for cisd.py (Phase 1) still ≥95% branch — regression guard
pytest tests/unit/test_cisd.py --cov=src/nasdaq_ale_bot/detection/cisd --cov-branch --cov-fail-under=95

# 6. Schema round-trip (lock test is @skip until Phase 3.5)
pytest tests/unit/test_logging_sink.py::test_round_trip_every_event_type -v

# 7. Pre-wiring contract tests
pytest tests/unit/test_broker_protocol.py::test_broker_protocol_has_14_methods -v
pytest tests/unit/test_gates.py::test_base_list_exact_set_and_order -v

# 8. Decimal & HWM invariants
pytest tests/unit/test_account_ledger.py -v -k "hwm or decimal"

# 9. HTF bias off-by-one tripwire
pytest tests/unit/test_htf_bias.py::test_off_by_one_pre_mortem_scenario_3 -v

# 9a. Threading contract (A4)
pytest tests/unit/test_state_machine_transitions.py::test_cross_thread_call_raises -v

# 9b. Broker Decimal boundary (A1)
pytest tests/unit/test_broker_protocol.py::test_broker_adapter_returns_quantized_decimal -v

# 9c. Fetch fixture + pinned SHA-256 (A3) — prerequisite for integration
python scripts/fetch_phase2_fixture.py
pytest tests/integration/test_phase2_lifecycle.py::test_fetch_script_reproduces_pinned_sha256 -v

# 10. Integration (the big one)
pytest tests/integration/test_phase2_lifecycle.py -v

# 11. JSONL round-trip after integration
pytest tests/integration/test_phase2_lifecycle.py::test_jsonl_sink_round_trip_after_replay -v

# 12. Pure detection layer untouched (CI guard)
git diff --name-only main..HEAD -- 'src/nasdaq_ale_bot/detection/**' | wc -l   # must be 0

# 13. Total LOC budget check
git diff --shortstat main..HEAD -- 'src/' 'tests/' 'config/'   # additions in 3000-4500 range
```

All commands must exit 0. Command 12 must print `0`. Command 13 must show additions in `[3000, 4500]`.

---

## 9. ADR — Phase 2 Pre-Wiring + State Machine

**Decision.** Implement Phase 2 in five strictly ordered sub-steps: (1) Pre-wiring primitives — `AccountLedger`, `GateList`, `JsonlSink`, `BrokerProtocol` 14-method extension, `instruments.yaml::futures`. (2) State Machine engine. (3) HTF Bias detector with two-stage pending/active gating. (4) Full `SMTTracker`. (5) Integration test on `qqq_1m_sample.csv`. The pre-wiring sub-step lands first so the state machine implementation naturally calls into ledger/sink/gates as it's written, eliminating a retroactive instrumentation pass.

**Drivers.**
- **D1:** Phase 5 (Apex) must be a config-flip and broker swap, not a refactor of detection or state-machine code. PLAN.md §9.A.
- **D2:** Phase 6 (GUI) must consume engine events via append-only file IPC with a frozen schema, with zero coupling to engine process. PLAN.md §9.B, §A32.
- **D3:** Integration test on a real 5-day fixture must produce ≥1 complete trade lifecycle to prove the Phase 2 system is wired correctly end-to-end. PLAN.md §3 line 73.

**Alternatives considered.**
- **Option B — State machine first, retrofit primitives later.** Rejected: violates user-mandated order; requires retroactive instrumentation (§A35 rationale); risks Phase 5 slipping.
- **Option C — Defer pre-wiring to Phase 2.5.** Rejected: directly contradicts PLAN.md §3 Phase 2 AC block; forces a second instrumentation pass; delays Phase 5 unblocking.

**Why chosen.** Option A (chosen) is the only ordering that satisfies all three drivers simultaneously and matches PLAN.md §3 Phase 2 verbatim. The cost is ~1200 LOC of "passive" code landing before any visible state-machine behavior, which is acceptable because the integration test (Step 5) exercises every primitive end-to-end before Phase 2 closes.

**Consequences.**
- Phase 2 LOC budget rises to ~3800 (mid 3000-4500 target).
- The `AlpacaBrokerStub` ships with 5 methods raising `NotImplementedError` — Phase 4 must implement them before live runner ships.
- The JSONL schema is *frozen* the moment Phase 2 merges. Future schema changes require an ADR + version bump + GUI compatibility plan.
- Phase 3 backtest engine (next phase) inherits a working `AccountLedger` and gate framework — its own scope shrinks by ~300 LOC.
- Phase 5 (Apex) becomes: 6 gate classes (~200 LOC) + `TradovateBroker` (~600 LOC) + 1 config block. No state-machine changes.
- Phase 6 (GUI) becomes: PyQt6 panels reading `EVENT_SCHEMA_V1` from `core/logging_sink`. No engine changes.

**Follow-ups (added in v2 after Architect amendments A1-A5).**
- **FU1:** End of **Phase 3.5** — freeze schema v1 lock file. Commit real SHA-256 to `_schema_v1_lock.txt`, remove `@pytest.mark.skip` from `test_schema_constant_locked`. Any new top-level fields discovered during Phase 3 backtest (e.g., `fill_source`, `is_sample`) MUST be folded into `EVENT_SCHEMA_V1` BEFORE the lock is activated. (A2)
- **FU2:** **Phase 4** — equity-poll reorder buffer decision. Phase 2 drops out-of-order snapshots; Phase 4 must empirically measure broker poll reorder window and decide between keep-drop and introduce a ~3-slot reorder buffer. (Q1 ruling)
- **FU3:** **Phase 5** — Apex gate classes (`DailyLossApexGate`, `TrailingDDGate`, `ProfitTargetGate`, `ConsistencyGate`, `ScalingPlanGate`) compose with `GateList.base_list()` via `base_list() + apex_gates()`. Phase 5 must NOT modify `base_list()` directly — it appends. Invariant enforced by `test_base_list_exact_set_and_order` which Phase 5 leaves untouched. (A5 rationale)

**Follow-ups.**
- Confirm `tests/fixtures/qqq_1m_sample.csv` exists from Phase 1, or run `scripts/fetch_phase2_fixture.py` early in Phase 2.
- Decide fsync cadence default — currently `fsync_every_n=10`; revisit after Phase 4 measures actual event volume.
- Phase 4 must add an `equity_poll_thread → queue → engine_thread` plumbing path that calls `AccountLedger.on_unrealized_snapshot`; the contract is already expressed in §A26 + Step 1a.
- Schema v2 upgrade plan deferred to Phase 6+. v1 must remain forward-readable (additive `fields:` only).
