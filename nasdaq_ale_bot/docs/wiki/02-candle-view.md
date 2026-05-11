# CandleView Look-Ahead Enforcement (A24)

## Problem

In bar-by-bar replay systems, look-ahead bias is the #1 cause of inflated backtest results. A detection function that accidentally reads `bars[i+1]` produces signals that could never exist in live trading.

## Solution: Runtime Enforcement over Convention

**Decision:** Enforce at runtime, not by convention (code review + asserts).

`CandleView(bars, i)` wraps a candle list and raises `LookAheadError` on any access `k > i`:

```python
class CandleView:
    def __init__(self, bars: list[Candle], i: int) -> None:
        self._bars = bars
        self._i = i

    def __len__(self) -> int:
        return self._i + 1

    def __getitem__(self, k: int) -> Candle:
        if k > self._i:
            raise LookAheadError(f"index {k} > horizon {self._i}")
        return self._bars[k]
```

## Why Runtime, Not Convention

| Approach | Catches bugs at | Failure mode |
|----------|----------------|--------------|
| Convention (code review) | PR review time | Silent if reviewer misses it |
| Asserts (`assert i < len`) | Test time only (stripped in `-O`) | Silent in production |
| `CandleView` wrapper | Runtime, always | Loud crash, impossible to ignore |

The runtime approach means:
- A detection function **cannot** read future bars, period
- Every test documents its allowed horizon via `CandleView(bars, i)`
- Slicing is disabled (raises `TypeError`) to prevent accidental bulk copies that bypass the guard
- Negative indexing is supported but mapped to the visible window only

## Location

- `src/nasdaq_ale_bot/core/candle_view.py`
- Tests: `tests/unit/test_candle_view.py`

## Usage Pattern

Every detection function receives `view: CandleView` (never `bars: list[Candle]`):

```python
def detect_bullish_cisd(view: CandleView, sweep_idx: int) -> CISDResult:
    # view[sweep_idx + 1] works only if caller passed i >= sweep_idx + 1
    # view[len(bars) - 1] raises LookAheadError if we're mid-replay
    ...
```

The caller (state machine or backtest engine) constructs the view:

```python
for i in range(len(bars)):
    view = CandleView(bars, i)
    result = detect_bullish_cisd(view, sweep_idx)
```
