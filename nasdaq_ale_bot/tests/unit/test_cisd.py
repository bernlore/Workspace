"""CISD body-close detection — the critical correctness gate (A1/A1b/A2)."""

from nasdaq_ale_bot.core.candle_view import CandleView
from nasdaq_ale_bot.detection.cisd import (
    CONFIRM_WINDOW,
    LOOKBACK_CAP,
    detect_bearish_cisd,
    detect_bullish_cisd,
)

from .conftest import mk_candle

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _up(idx, o, c, *, high=None, low=None):
    """Up-close bar (close > open)."""
    assert c > o
    return mk_candle(idx, open_=o, close=c, high=high, low=low)


def _down(idx, o, c, *, high=None, low=None):
    """Down-close bar (close < open)."""
    assert c < o
    return mk_candle(idx, open_=o, close=c, high=high, low=low)


def _doji(idx, price, *, high=None, low=None):
    return mk_candle(idx, open_=price, close=price, high=high, low=low)


# -----------------------------------------------------------------------------
# Bullish CISD — mandated cases
# -----------------------------------------------------------------------------


def test_bullish_wick_break_not_confirmed():
    """Case (a): later bar's wick pierces reference.high, but body close is below."""
    bars = [
        _up(0, 100, 105, high=106, low=99),   # reference up-close, high = 106
        _down(1, 105, 102),                   # first bar of down-leg
        _down(2, 102, 99),                    # sweep bar (idx=2)
        # Later bar with wick breaking 106 but close below
        mk_candle(3, open_=100, high=106.5, low=99.5, close=103),
        mk_candle(4, open_=103, high=105, low=101, close=104),
    ]
    view = CandleView(bars, 4)
    result = detect_bullish_cisd(view, sweep_idx=2)
    assert result.confirmed is False, "wick-only break must NOT confirm"


def test_bullish_body_close_just_above_reference_confirmed():
    """Case (b): body close just above reference.high confirms."""
    bars = [
        _up(0, 100, 105, high=106, low=99),
        _down(1, 105, 102),
        _down(2, 102, 99),
        # Body close at 106.01 — just above reference.high=106
        mk_candle(3, open_=102, high=106.5, low=101.5, close=106.01),
    ]
    view = CandleView(bars, 3)
    result = detect_bullish_cisd(view, sweep_idx=2)
    assert result.confirmed is True
    assert result.ref_idx == 0
    assert result.confirm_idx == 3


def test_bullish_multiple_up_candles_picks_run_terminator():
    """Case (c): three up-close bars before the down-leg; reference is the one
    immediately preceding the contiguous down-leg that terminates at the sweep.

    The algorithm walks backward while view[k].close > view[k+1].close.
    Bar 2 close (102.5) <= bar 3 close (103) breaks the chain → bar 2 is the
    run terminator and, being up-close, becomes the reference.
    """
    bars = [
        _up(0, 100, 101),                          # early up-close, NOT reference
        _up(1, 101, 102),                          # another up-close, NOT reference
        _up(2, 102, 102.5, high=105, low=101.5),   # run terminator — this is the reference
        _down(3, 104, 103),                        # down-leg starts
        _down(4, 103, 101),
        _down(5, 101, 99),                         # sweep at idx=5
        mk_candle(6, open_=100, high=106, low=99.5, close=105.5),  # body > 105 confirms
    ]
    view = CandleView(bars, 6)
    result = detect_bullish_cisd(view, sweep_idx=5)
    assert result.confirmed is True
    assert result.ref_idx == 2  # the bar immediately preceding the down-leg


def test_bullish_doji_not_selectable_as_reference():
    """Doji (close == open) can never be the CISD reference."""
    bars = [
        _up(0, 100, 103, high=104, low=99),    # real up-close, should be chosen
        _doji(1, 103, high=104, low=102),      # doji terminator — skip
        _down(2, 103, 101),
        _down(3, 101, 99),                     # sweep
        mk_candle(4, open_=100, high=105, low=99.5, close=104.5),  # > 104
    ]
    view = CandleView(bars, 4)
    result = detect_bullish_cisd(view, sweep_idx=3)
    assert result.confirmed is True
    assert result.ref_idx == 0


def test_bullish_no_up_candle_in_cap_returns_unconfirmed():
    # All down bars, no up-close anywhere in lookback
    bars = [_down(i, 110 - i * 0.5, 110 - (i + 1) * 0.5) for i in range(5)]
    bars.append(mk_candle(5, open_=107.5, high=108, low=107, close=107.2))
    view = CandleView(bars, 5)
    result = detect_bullish_cisd(view, sweep_idx=4)
    assert result.confirmed is False


def test_bullish_confirmation_timeout():
    bars = [
        _up(0, 100, 105, high=106, low=99),
        _down(1, 105, 102),
        _down(2, 102, 99),                     # sweep at idx=2
    ]
    # Fill CONFIRM_WINDOW bars that never body-close above 106
    for k in range(CONFIRM_WINDOW + 2):
        bars.append(mk_candle(3 + k, open_=100, high=105.5, low=99.5, close=103))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bullish_cisd(view, sweep_idx=2)
    assert result.confirmed is False


def test_bullish_sweep_idx_out_of_range():
    bars = [_up(0, 100, 101)]
    view = CandleView(bars, 0)
    assert detect_bullish_cisd(view, sweep_idx=0).confirmed is False
    assert detect_bullish_cisd(view, sweep_idx=5).confirmed is False


# -----------------------------------------------------------------------------
# Bearish mirrors
# -----------------------------------------------------------------------------


def test_bearish_wick_break_not_confirmed():
    bars = [
        _down(0, 105, 100, high=106, low=99),  # reference down-close, low = 99
        _up(1, 100, 103),                       # up-leg
        _up(2, 103, 106),                       # sweep at idx=2 (buy-side)
        # wick pierces 99 but close above
        mk_candle(3, open_=105, high=106, low=98.5, close=102),
    ]
    view = CandleView(bars, 3)
    result = detect_bearish_cisd(view, sweep_idx=2)
    assert result.confirmed is False


def test_bearish_body_close_just_below_reference_confirmed():
    bars = [
        _down(0, 105, 100, high=106, low=99),
        _up(1, 100, 103),
        _up(2, 103, 106),
        mk_candle(3, open_=104, high=105, low=98.5, close=98.99),  # close < 99
    ]
    view = CandleView(bars, 3)
    result = detect_bearish_cisd(view, sweep_idx=2)
    assert result.confirmed is True
    assert result.ref_idx == 0


def test_bearish_multiple_down_candles_picks_run_terminator():
    bars = [
        _down(0, 110, 109),
        _down(1, 109, 108),
        _down(2, 108, 107.5, high=109, low=104),  # run terminator (down-close, close=107.5 >= bar3 close=107)
        _up(3, 105, 107),                          # up-leg starts
        _up(4, 107, 109),
        _up(5, 109, 112),                          # sweep
        mk_candle(6, open_=110, high=111, low=103, close=103.5),  # < 104
    ]
    view = CandleView(bars, 6)
    result = detect_bearish_cisd(view, sweep_idx=5)
    assert result.confirmed is True
    assert result.ref_idx == 2


def test_bearish_doji_not_reference():
    bars = [
        _down(0, 108, 104, high=109, low=103),
        _doji(1, 104, high=105, low=103.5),
        _up(2, 104, 106),
        _up(3, 106, 108),                        # sweep
        mk_candle(4, open_=106, high=107, low=102, close=102.5),  # < 103
    ]
    view = CandleView(bars, 4)
    result = detect_bearish_cisd(view, sweep_idx=3)
    assert result.confirmed is True
    assert result.ref_idx == 0


def test_bearish_no_down_candle_in_cap_returns_unconfirmed():
    # All up bars — no down-close anywhere in lookback for bearish
    bars = [_up(i, 100 + i * 0.5, 100 + (i + 1) * 0.5) for i in range(5)]
    bars.append(mk_candle(5, open_=102.5, high=103, low=102, close=102.8))
    view = CandleView(bars, 5)
    result = detect_bearish_cisd(view, sweep_idx=4)
    assert result.confirmed is False


def test_bearish_confirmation_timeout():
    bars = [
        _down(0, 105, 100, high=106, low=99),
        _up(1, 100, 103),
        _up(2, 103, 106),  # sweep
    ]
    for k in range(CONFIRM_WINDOW + 2):
        bars.append(mk_candle(3 + k, open_=104, high=105, low=99.5, close=100))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bearish_cisd(view, sweep_idx=2)
    assert result.confirmed is False


def test_bearish_sweep_idx_out_of_range():
    bars = [_down(0, 105, 100)]
    view = CandleView(bars, 0)
    assert detect_bearish_cisd(view, sweep_idx=0).confirmed is False
    assert detect_bearish_cisd(view, sweep_idx=5).confirmed is False


# -----------------------------------------------------------------------------
# Focus (3): all-doji edge case
# -----------------------------------------------------------------------------


def test_bullish_all_doji_returns_unconfirmed():
    """No doji can serve as reference → unconfirmed."""
    bars = [_doji(i, 100 + i * 0.1) for i in range(5)]
    view = CandleView(bars, 4)
    result = detect_bullish_cisd(view, sweep_idx=3)
    assert result.confirmed is False


def test_bearish_all_doji_returns_unconfirmed():
    bars = [_doji(i, 100 + i * 0.1) for i in range(5)]
    view = CandleView(bars, 4)
    result = detect_bearish_cisd(view, sweep_idx=3)
    assert result.confirmed is False


def test_bearish_doji_terminator_no_down_close_anywhere():
    """Terminator found (doji breaks ascending chain) but no down-close exists."""
    bars = [
        _doji(0, 104),          # terminates chain (104 > 103) but not down-close
        _up(1, 101, 103),       # up-leg
        _up(2, 103, 105),       # up-leg
        _up(3, 105, 108),       # sweep at idx=3
        mk_candle(4, open_=106, high=107, low=100, close=100.5),
    ]
    view = CandleView(bars, 4)
    result = detect_bearish_cisd(view, sweep_idx=3)
    assert result.confirmed is False


# -----------------------------------------------------------------------------
# Focus (4): reference adjacent to sweep (minimal distance)
# -----------------------------------------------------------------------------


def test_bullish_reference_immediately_before_sweep():
    """Reference at sweep_idx-1 — minimal lookback distance."""
    bars = [
        _up(0, 100, 105, high=106, low=99),   # reference at idx=0, sweep at idx=1
        _down(1, 105, 99),                     # sweep
        mk_candle(2, open_=100, high=107, low=99.5, close=106.5),  # confirms
    ]
    view = CandleView(bars, 2)
    result = detect_bullish_cisd(view, sweep_idx=1)
    assert result.confirmed is True
    assert result.ref_idx == 0
    assert result.confirm_idx == 2


def test_bearish_reference_immediately_before_sweep():
    bars = [
        _down(0, 105, 100, high=106, low=99),  # reference at idx=0, sweep at idx=1
        _up(1, 100, 106),                       # sweep
        mk_candle(2, open_=104, high=105, low=98, close=98.5),  # < 99 confirms
    ]
    view = CandleView(bars, 2)
    result = detect_bearish_cisd(view, sweep_idx=1)
    assert result.confirmed is True
    assert result.ref_idx == 0
    assert result.confirm_idx == 2


# -----------------------------------------------------------------------------
# Focus (1): off-by-one on CONFIRM_WINDOW boundary
# -----------------------------------------------------------------------------


def test_bullish_confirm_at_exact_window_boundary():
    """Bar at sweep_idx + CONFIRM_WINDOW is the LAST bar within the window."""
    bars = [
        _up(0, 100, 105, high=106, low=99),
        _down(1, 105, 102),
        _down(2, 102, 99),  # sweep at idx=2
    ]
    # Fill CONFIRM_WINDOW-1 bars that don't confirm
    for k in range(CONFIRM_WINDOW - 1):
        bars.append(mk_candle(3 + k, open_=100, high=105.5, low=99.5, close=103))
    # The last bar within window (idx = 2 + CONFIRM_WINDOW) confirms
    bars.append(mk_candle(3 + CONFIRM_WINDOW - 1, open_=102, high=107, low=101, close=106.5))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bullish_cisd(view, sweep_idx=2)
    assert result.confirmed is True
    assert result.confirm_idx == 2 + CONFIRM_WINDOW


def test_bullish_confirm_one_past_window_fails():
    """Bar at sweep_idx + CONFIRM_WINDOW + 1 is outside the window."""
    bars = [
        _up(0, 100, 105, high=106, low=99),
        _down(1, 105, 102),
        _down(2, 102, 99),  # sweep at idx=2
    ]
    # Fill CONFIRM_WINDOW bars that don't confirm
    for k in range(CONFIRM_WINDOW):
        bars.append(mk_candle(3 + k, open_=100, high=105.5, low=99.5, close=103))
    # One more bar AFTER the window — would confirm but is too late
    bars.append(mk_candle(3 + CONFIRM_WINDOW, open_=102, high=107, low=101, close=106.5))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bullish_cisd(view, sweep_idx=2)
    assert result.confirmed is False


def test_bearish_confirm_at_exact_window_boundary():
    bars = [
        _down(0, 105, 100, high=106, low=99),
        _up(1, 100, 103),
        _up(2, 103, 106),  # sweep at idx=2
    ]
    for k in range(CONFIRM_WINDOW - 1):
        bars.append(mk_candle(3 + k, open_=104, high=105, low=99.5, close=100))
    # Last bar in window confirms
    bars.append(mk_candle(3 + CONFIRM_WINDOW - 1, open_=102, high=103, low=98, close=98.5))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bearish_cisd(view, sweep_idx=2)
    assert result.confirmed is True
    assert result.confirm_idx == 2 + CONFIRM_WINDOW


def test_bearish_confirm_one_past_window_fails():
    bars = [
        _down(0, 105, 100, high=106, low=99),
        _up(1, 100, 103),
        _up(2, 103, 106),  # sweep at idx=2
    ]
    for k in range(CONFIRM_WINDOW):
        bars.append(mk_candle(3 + k, open_=104, high=105, low=99.5, close=100))
    bars.append(mk_candle(3 + CONFIRM_WINDOW, open_=102, high=103, low=98, close=98.5))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bearish_cisd(view, sweep_idx=2)
    assert result.confirmed is False


# -----------------------------------------------------------------------------
# Mutation sentinel — if someone rewrites the trigger to use .high, this fails
# -----------------------------------------------------------------------------


def test_cisd_trigger_is_close_not_high():
    """Mutation sentinel. Engineering a case where .high would trigger but .close
    does not, and asserting that .close semantics hold.
    """
    bars = [
        _up(0, 100, 104, high=105, low=99),
        _down(1, 104, 102),
        _down(2, 102, 99),                # sweep
        mk_candle(3, open_=101, high=105.5, low=100.5, close=104.9),
    ]
    view = CandleView(bars, 3)
    result = detect_bullish_cisd(view, sweep_idx=2)
    assert result.confirmed is False, "trigger must be close, not high"


def test_bearish_trigger_is_close_not_low():
    """Bearish mutation sentinel — mirror of bullish."""
    bars = [
        _down(0, 105, 100, high=106, low=99),  # ref low=99
        _up(1, 100, 103),
        _up(2, 103, 106),                       # sweep
        mk_candle(3, open_=102, high=103, low=98.5, close=99.1),  # low < 99 but close > 99
    ]
    view = CandleView(bars, 3)
    result = detect_bearish_cisd(view, sweep_idx=2)
    assert result.confirmed is False, "trigger must be close, not low"


# -----------------------------------------------------------------------------
# Focus (1): lookback cap — both directions
# -----------------------------------------------------------------------------


def test_lookback_cap_respected():
    bars = [_up(0, 100, 105, high=106, low=99)]
    price = 104.0
    for k in range(1, LOOKBACK_CAP + 5):
        nxt = price - 0.3
        bars.append(_down(k, price, nxt, high=price + 0.1, low=nxt - 0.1))
        price = nxt
    sweep_idx = len(bars) - 1
    bars.append(mk_candle(sweep_idx + 1, open_=price, high=price + 0.1, low=price - 0.1, close=price + 0.05))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bullish_cisd(view, sweep_idx=sweep_idx)
    assert result.confirmed is False


def test_bearish_lookback_cap_respected():
    bars = [_down(0, 105, 100, high=106, low=99)]
    price = 101.0
    for k in range(1, LOOKBACK_CAP + 5):
        nxt = price + 0.3
        bars.append(_up(k, price, nxt, high=nxt + 0.1, low=price - 0.1))
        price = nxt
    sweep_idx = len(bars) - 1
    bars.append(mk_candle(sweep_idx + 1, open_=price, high=price + 0.1, low=price - 0.1, close=price - 0.05))
    view = CandleView(bars, len(bars) - 1)
    result = detect_bearish_cisd(view, sweep_idx=sweep_idx)
    assert result.confirmed is False
