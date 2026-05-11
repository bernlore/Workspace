# CISD Criticality and Test Strategy

## Why CISD is the Critical Path

CISD (Change in State of Delivery) is the strategy's core confirmation signal. A subtle bug here -- e.g., triggering on `candle.high` instead of `candle.close` -- silently inflates win rate in backtests while producing false signals live. The bug is invisible without targeted tests because:

1. `high >= close` always, so a high-based trigger is a strict superset of close-based
2. Many candles have `high == close` (up-close bars), so most signals look identical
3. The difference only surfaces on specific bar shapes (wick above reference, body below)

## Test Strategy: 3 Layers

### Layer 1: Mandated Cases (Functional Correctness)

Each case tests a specific semantic of the CISD algorithm:

| Test | What it validates |
|------|-------------------|
| `test_bullish_wick_break_not_confirmed` | Wick piercing reference.high with body below does NOT confirm |
| `test_bullish_body_close_just_above_reference_confirmed` | Body close barely above reference.high DOES confirm |
| `test_bullish_multiple_up_candles_picks_run_terminator` | Reference is the run-terminator per A1 algorithm |
| `test_bullish_doji_not_selectable_as_reference` | Doji (close == open) is excluded from reference selection |
| `test_bullish_no_up_candle_in_cap_returns_unconfirmed` | No up-close in 20-bar lookback -> unconfirmed |
| `test_bullish_confirmation_timeout` | No confirm within 15 bars -> unconfirmed |
| `test_bullish_sweep_idx_out_of_range` | Boundary: sweep_idx=0 and beyond view length |

### Layer 2: Bearish Mirrors (Symmetry Verification)

Every bullish test has a bearish counterpart. This catches bugs where only one direction was implemented/fixed.

### Layer 3: Hardening Tests (Edge Cases + Mutation Sentinels)

| Test | Focus Area |
|------|------------|
| `test_cisd_trigger_is_close_not_high` | **Mutation sentinel** -- fails if trigger changed to `.high` |
| `test_bearish_trigger_is_close_not_low` | Bearish mutation sentinel |
| `test_lookback_cap_respected` | Off-by-one at 20-bar boundary |
| `test_bearish_lookback_cap_respected` | Bearish mirror of lookback cap |
| `test_bullish_all_doji_returns_unconfirmed` | All-doji series -> no reference possible |
| `test_bearish_all_doji_returns_unconfirmed` | Bearish mirror |
| `test_bearish_doji_terminator_no_down_close_anywhere` | Terminator found but not down-close, and no fallback exists |
| `test_bullish_reference_immediately_before_sweep` | Minimal lookback distance (ref at sweep_idx-1) |
| `test_bearish_reference_immediately_before_sweep` | Bearish mirror |
| `test_bullish_confirm_at_exact_window_boundary` | Bar at sweep+CONFIRM_WINDOW confirms (last valid) |
| `test_bullish_confirm_one_past_window_fails` | Bar at sweep+CONFIRM_WINDOW+1 is too late |
| `test_bearish_confirm_at_exact_window_boundary` | Bearish mirror |
| `test_bearish_confirm_one_past_window_fails` | Bearish mirror |

## Coverage Result

- **cisd.py: 100% branch coverage** (77 statements, 44 branches, 0 missing)
- 27 total CISD tests

## The Mutation Sentinel Pattern

The mutation sentinel is a test specifically designed so that if someone accidentally changes the trigger comparison from `.close` to `.high` (or `.low`), the test fails immediately:

```python
def test_cisd_trigger_is_close_not_high():
    # reference.high = 105. Later bar: high=105.5 but close=104.9
    # Close-based: NOT confirmed (104.9 < 105)
    # High-based:  WOULD confirm (105.5 > 105) -- BUG
    ...
    assert result.confirmed is False
```

This is more reliable than grep-based checks because it verifies behavior, not syntax.
