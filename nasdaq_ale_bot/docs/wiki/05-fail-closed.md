# Fail-Closed Safety Philosophy

## Principle

> Any missing datum halts new orders. When in doubt, do nothing.

This is PLAN Principle 4 and the single most important safety property of the system.

## Where Fail-Closed Applies

| Scenario | What happens | Reference |
|----------|-------------|-----------|
| SMT data unavailable (>=2 missing bars) | New entries blocked | A13 |
| News CSV missing or stale (>24h) | Runner refuses to start | A10 |
| WS disconnect | Cancel all orders + flatten positions | A21, Phase 4 |
| Clock drift >2s vs Alpaca | Halt new entries; open positions keep server-side brackets | A23 |
| No up-close/down-close reference in CISD lookback | CISD returns `confirmed=False` | A1 |
| 0 or >=3 IFVGs in CISD move | No entry (invalid setup) | A4 |
| SL distance > max_stop | Skip trade entirely | A11 |
| Projected PnL would breach -$1500 | Skip entry (`SKIP_PROJECTED_LOSS_LIMIT`) | A15 |
| Outside killzone hours | No new orders | Filters |
| Weekend | No new orders | Killzone filter |

## Why Not Fail-Open?

The v1 design had a fail-open gap in A13: missing SMT data defaulted to "no divergence detected" and allowed entries to proceed. This is dangerous because:

1. Missing correlated data means we have no information about institutional flow
2. "No divergence" is a positive signal (safe to enter), not a neutral one
3. A data feed outage could trigger entries that institutional flow analysis would have blocked

The fix: SMT returns `UNAVAILABLE` on missing data, and `UNAVAILABLE` is treated as a **hard block** on new entries.

## Fail-Closed vs Risk Management

Fail-closed applies to **signal detection** (should we enter?). Risk management for **open positions** operates differently:

- BE moves trigger on touch (A17) -- this is a risk-management action, not a signal
- Time exits fire at hard deadlines (A18) -- non-discretionary
- SL/TP brackets are server-side -- survive local crashes
- Daily loss limit flattens and halts on breach (A15) -- protective, not predictive

The distinction is: signals require full information to fire (fail-closed). Risk management for existing positions must always be able to act (fail-safe).
