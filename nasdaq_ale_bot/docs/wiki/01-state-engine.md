# 6-State Engine Overview

## States

The strategy operates as a deterministic finite state machine with 7 states (6 active + FLAT):

```
BIAS_DETERMINATION -> WAITING_FOR_SWEEP -> CISD_CONFIRMATION -> IFVG_FORMATION -> ENTRY_EXECUTION -> TRADE_MANAGEMENT -> FLAT
```

| State | Purpose | Transition trigger |
|-------|---------|-------------------|
| `BIAS_DETERMINATION` | Establish directional bias from HTF (4H FVG + 1H/Daily confirmation, A8) | Bias confirmed -> `WAITING_FOR_SWEEP` |
| `WAITING_FOR_SWEEP` | Monitor liquidity levels for sweep | Sweep detected (wick pierce + body reclaim, A3) -> `CISD_CONFIRMATION` |
| `CISD_CONFIRMATION` | Wait for body-close above/below reference candle (A1/A2) | Body-close confirms within 15-bar window -> `IFVG_FORMATION` |
| `IFVG_FORMATION` | Scan CISD move for inverse FVGs (A4) | 1-2 IFVGs found in discount/premium zone -> `ENTRY_EXECUTION` |
| `ENTRY_EXECUTION` | Size position, validate R:R (A7), submit bracket order | Order placed -> `TRADE_MANAGEMENT` |
| `TRADE_MANAGEMENT` | Monitor BE move (A17), time exit (A18), daily loss (A15) | Exit filled -> `FLAT` |
| `FLAT` | Post-trade cooldown; check 2-trade cap, daily loss limit | New session / conditions met -> `BIAS_DETERMINATION` |

## Location

- **Definition:** `src/nasdaq_ale_bot/core/state_machine.py`
- **Phase 1:** Stub only - `StrategyState` enum + `StateMachine` class with `on_bar()` raising `NotImplementedError`
- **Phase 2:** Full `on_bar(bar) -> list[StateEvent]` implementation with structlog JSON on every transition

## Threading Contract (A21)

The state machine is single-threaded by design. In the live runner:

1. **WS thread** receives bar events from Alpaca, pushes to `queue.Queue`
2. **Engine thread** drains queue, is the **sole caller** of `StateMachine.on_bar()` and `safety.flatten()`
3. **Main thread** handles startup/shutdown

The state machine holds no locks and never spawns threads. Correctness depends on the single-caller invariant.

## Shared Code Path

Backtest and live share the identical `StateMachine.on_bar(closed_bar)` call. The backtest engine replays bars sequentially; the live runner receives them from the WS queue. This guarantees that a passing golden-ledger test on historical data means the live runner will produce identical signals on the same bar stream.
