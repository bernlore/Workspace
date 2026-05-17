"""Strategy implementations.

Each subpackage is one self-contained trading strategy (state machine +
strategy-specific detection). Shared infrastructure — backtest runner,
ledger, gates, candle primitives, killzone filter — lives outside this
package and is consumed by every strategy.
"""
