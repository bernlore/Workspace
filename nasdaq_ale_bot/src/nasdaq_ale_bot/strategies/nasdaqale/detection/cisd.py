"""Change-in-State-of-Delivery (CISD) detection.

Body-close semantics only — wicks are ignored. See ASSUMPTIONS.md A1, A1b, A2.
The trigger for confirmation is ALWAYS view[j].close, never view[j].high / low.
"""

from dataclasses import dataclass

from nasdaq_ale_bot.core.candle_view import CandleView

LOOKBACK_CAP = 20
CONFIRM_WINDOW = 15


@dataclass
class CISDResult:
    confirmed: bool
    ref_idx: int | None = None
    confirm_idx: int | None = None


def _is_up_close(close: float, open_: float) -> bool:
    # strict: doji (close == open) is NOT up-close
    return close > open_


def _is_down_close(close: float, open_: float) -> bool:
    return close < open_


def detect_bullish_cisd(view: CandleView, sweep_idx: int) -> CISDResult:
    """Bullish CISD after a sell-side sweep.

    A1: find the run-terminator of the down-leg feeding the sweep. Walk backward
    from sweep_idx-1 while forward-in-time closes are descending (view[k].close >
    view[k+1].close). The first bar that breaks this (view[k].close <= view[k+1].close)
    is the run terminator. If the terminator is strictly up-close, it is the
    reference. Otherwise scan further backward for the nearest strictly up-close
    bar within the 20-bar cap.

    A2: forward-scan up to 15 bars after the sweep. First bar j where
    view[j].close > reference.high confirms. Trigger is close, never high.
    """
    if sweep_idx < 1 or sweep_idx >= len(view):
        return CISDResult(False)

    cap = max(0, sweep_idx - LOOKBACK_CAP)

    # Find run terminator — walk backward while still inside the down-leg.
    terminator: int | None = None
    k = sweep_idx - 1
    while k >= cap:
        if view[k].close > view[k + 1].close:
            # still in the down-leg (forward-in-time close descending)
            k -= 1
            continue
        terminator = k
        break

    reference_idx: int | None = None
    if terminator is not None:
        bar = view[terminator]
        if _is_up_close(bar.close, bar.open):
            reference_idx = terminator

    # Fall back: scan backward for nearest strictly up-close within cap.
    if reference_idx is None:
        start = (terminator - 1) if terminator is not None else (sweep_idx - 1)
        for j in range(start, cap - 1, -1):
            bar = view[j]
            if _is_up_close(bar.close, bar.open):
                reference_idx = j
                break

    if reference_idx is None:
        return CISDResult(False)

    reference = view[reference_idx]
    end = min(len(view), sweep_idx + 1 + CONFIRM_WINDOW)
    for j in range(sweep_idx + 1, end):
        # CRITICAL: close, not high.
        if view[j].close > reference.high:
            return CISDResult(True, reference_idx, j)
    return CISDResult(False)


def detect_bearish_cisd(view: CandleView, sweep_idx: int) -> CISDResult:
    """Bearish CISD after a buy-side sweep — mirror of A1b."""
    if sweep_idx < 1 or sweep_idx >= len(view):
        return CISDResult(False)

    cap = max(0, sweep_idx - LOOKBACK_CAP)

    terminator: int | None = None
    k = sweep_idx - 1
    while k >= cap:
        if view[k].close < view[k + 1].close:
            # still inside the up-leg feeding the sweep
            k -= 1
            continue
        terminator = k
        break

    reference_idx: int | None = None
    if terminator is not None:
        bar = view[terminator]
        if _is_down_close(bar.close, bar.open):
            reference_idx = terminator

    if reference_idx is None:
        start = (terminator - 1) if terminator is not None else (sweep_idx - 1)
        for j in range(start, cap - 1, -1):
            bar = view[j]
            if _is_down_close(bar.close, bar.open):
                reference_idx = j
                break

    if reference_idx is None:
        return CISDResult(False)

    reference = view[reference_idx]
    end = min(len(view), sweep_idx + 1 + CONFIRM_WINDOW)
    for j in range(sweep_idx + 1, end):
        if view[j].close < reference.low:
            return CISDResult(True, reference_idx, j)
    return CISDResult(False)
