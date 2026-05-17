"""Pure stateless SMT divergence calculation on pre-aggregated 5m series.

The 1m->5m aggregation is stateful and lives in core/smt_tracker.py (A12).
Parameter names are intentionally primary_*/correlated_* to keep the detection
layer instrument-agnostic (Principle 2).
"""

from dataclasses import dataclass

from nasdaq_ale_bot.core.candle import Candle


class SMTConfigError(ValueError):
    """Raised when the correlated symbol is missing or unconfigured."""


@dataclass
class SMTResult:
    bearish_divergence: bool
    bullish_divergence: bool
    reason: str


def detect_smt_divergence(
    primary_5m: list[Candle] | None,
    correlated_5m: list[Candle] | None,
    i: int,
) -> SMTResult:
    """Return SMT verdict at 5-minute index i.

    Bearish divergence: primary makes higher high while correlated makes lower high.
    Bullish divergence: primary makes lower low while correlated makes higher low.
    """
    if primary_5m is None or correlated_5m is None or not primary_5m or not correlated_5m:
        raise SMTConfigError("SMT requires both primary and correlated 5m series")
    if i < 1 or i >= len(primary_5m) or i >= len(correlated_5m):
        return SMTResult(False, False, "insufficient_bars")

    p_hh = primary_5m[i].high > primary_5m[i - 1].high
    c_lh = correlated_5m[i].high < correlated_5m[i - 1].high
    p_ll = primary_5m[i].low < primary_5m[i - 1].low
    c_hl = correlated_5m[i].low > correlated_5m[i - 1].low

    bearish = p_hh and c_lh
    bullish = p_ll and c_hl
    reason = "ok" if (bearish or bullish) else "no_divergence"
    return SMTResult(bearish_divergence=bearish, bullish_divergence=bullish, reason=reason)
