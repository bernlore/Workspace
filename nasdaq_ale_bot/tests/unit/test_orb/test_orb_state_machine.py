"""Tests for the ORB 5-state machine — transitions, first-signal-only,
force-flat, session rotation, full long/short trade lifecycles."""

from datetime import datetime

import pytest

from nasdaq_ale_bot.strategies.orb.state_machine import OrbState

DAY = datetime(2024, 1, 3)


def _drive(sm, bars):
    for b in bars:
        sm.on_bar(b)


def _long_breakout_day(or_bars, bar, day=DAY):
    """OR (range 15) + a clean LONG 5-min breakout + the 09:50 entry bar.

    Bars 09:30..09:50. The caller appends management bars afterwards.
    """
    bars = or_bars(day, high=17015, low=17000)        # range 15
    # 5-min breakout window 09:45..09:49 — rising, body up, close 17021.
    window = [
        (45, 17016, 17018, 17015, 17017),
        (46, 17017, 17019, 17016, 17018),
        (47, 17018, 17020, 17017, 17019),
        (48, 17019, 17021, 17018, 17020),
        (49, 17020, 17022, 17019, 17021),
    ]
    for m, o, h, lo, c in window:
        bars.append(bar(datetime(day.year, day.month, day.day, 9, m), o, h, lo, c))
    # Entry bar — open 17018.
    bars.append(bar(datetime(day.year, day.month, day.day, 9, 50),
                    17018, 17019, 17017, 17018))
    return bars


def _short_breakout_day(or_bars, bar, day=DAY):
    """OR (range 15) + a clean SHORT 5-min breakdown + the 09:50 entry bar."""
    bars = or_bars(day, high=17015, low=17000)
    window = [
        (45, 16999, 17000, 16997, 16998),
        (46, 16998, 16999, 16996, 16997),
        (47, 16997, 16998, 16995, 16996),
        (48, 16996, 16997, 16993, 16994),
        (49, 16994, 16995, 16989, 16990),
    ]
    for m, o, h, lo, c in window:
        bars.append(bar(datetime(day.year, day.month, day.day, 9, m), o, h, lo, c))
    bars.append(bar(datetime(day.year, day.month, day.day, 9, 50),
                    16995, 16996, 16994, 16995))
    return bars


def test_full_long_trade_take_profit(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = _long_breakout_day(or_bars, bar)
    # Mid-stop: OR mid 17007.5, stop 17007.0, stop_dist 11.0 from entry 17018.
    # Target = entry + stop_dist * 1.5 = 17018 + 16.5 = 17034.5.
    bars.append(bar(datetime(2024, 1, 3, 9, 51), 17020, 17040, 17019, 17036))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert len(sm.trades) == 1
    t = sm.trades[0]
    assert t.direction == "LONG"
    assert t.exit_reason == "TAKE_PROFIT"
    # qty = floor(1000 / (11 * 20)) = 4 -> clamped to max_contracts 4.
    assert t.qty == 4
    # R:R is exactly 1.5 — target distance == stop distance * 1.5, measured
    # against the planned (pre-slippage) entry the levels were sized on.
    target_dist = abs(t.target_price - t.planned_entry_price)
    stop_dist = abs(t.planned_entry_price - t.stop_price)
    assert target_dist == pytest.approx(stop_dist * 1.5)
    assert t.net_pnl > 0


def test_full_long_trade_stop_out(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = _long_breakout_day(or_bars, bar)
    # Management bar — low pierces the stop (or_low - 0.5 = 16999.5).
    bars.append(bar(datetime(2024, 1, 3, 9, 51), 17010, 17012, 16998, 17000))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert len(sm.trades) == 1
    assert sm.trades[0].exit_reason == "STOP_OUT"
    assert sm.trades[0].net_pnl < 0


def test_full_short_trade_take_profit(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = _short_breakout_day(or_bars, bar)
    # SHORT: entry 16995, stop 17008.0, stop_dist 13.0
    # -> target = 16995 - 13 * 1.5 = 16975.5; the low reaches it.
    bars.append(bar(datetime(2024, 1, 3, 9, 51), 16980, 16982, 16974, 16976))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert len(sm.trades) == 1
    assert sm.trades[0].direction == "SHORT"
    assert sm.trades[0].exit_reason == "TAKE_PROFIT"


def test_first_signal_only_no_second_trade(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    bars = _long_breakout_day(or_bars, bar)
    bars.append(bar(datetime(2024, 1, 3, 9, 51), 17020, 17040, 17019, 17036))  # TP
    # A second breakout window later the same day must NOT produce a trade.
    for m in range(55, 60):
        bars.append(bar(datetime(2024, 1, 3, 9, m), 17040, 17045, 17039, 17044))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert len(sm.trades) == 1   # exactly one trade for the day


def test_force_flat_at_1545(make_orb_sm, or_bars, bar, flat_run):
    sm = make_orb_sm()
    bars = _long_breakout_day(or_bars, bar)
    # Flat management 09:51..15:44 — price stays between stop and target.
    bars += flat_run(datetime(2024, 1, 3, 9, 51),
                     datetime(2024, 1, 3, 15, 45), price=17018.0)
    # The 15:45 bar triggers the force-flat.
    bars.append(bar(datetime(2024, 1, 3, 15, 45), 17018, 17018, 17018, 17018))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert len(sm.trades) == 1
    assert sm.trades[0].exit_reason == "FLATTEN"


def test_no_breakout_by_noon_day_done(make_orb_sm, or_bars, bar, flat_run):
    sm = make_orb_sm()
    bars = or_bars(DAY, high=17020, low=17000)        # valid OR
    # Flat 09:45..12:00 inside the range — no breakout ever fires.
    bars += flat_run(datetime(2024, 1, 3, 9, 45),
                     datetime(2024, 1, 3, 12, 1), price=17010.0)
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert sm.days_or_valid == 1
    assert sm.days_with_signal == 0
    assert sm.trades == []


def test_session_rotation_starts_new_or(make_orb_sm, or_bars, bar):
    sm = make_orb_sm()
    # Day 1 — drive to a completed trade.
    bars = _long_breakout_day(or_bars, bar, day=datetime(2024, 1, 3))
    bars.append(bar(datetime(2024, 1, 3, 9, 51), 17020, 17040, 17019, 17036))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    # Day 2 — the first 09:30 bar rotates the session and starts a new OR.
    sm.on_bar(bar(datetime(2024, 1, 4, 9, 30), 17000, 17001, 16999, 17000))
    assert sm.state == OrbState.OR_FORMING
    assert sm._session_date == datetime(2024, 1, 4).date()  # noqa: SLF001


def test_pre_open_bars_keep_session_closed(make_orb_sm, bar):
    sm = make_orb_sm()
    sm.on_bar(bar(datetime(2024, 1, 3, 4, 0), 17000, 17001, 16999, 17000))
    assert sm.state == OrbState.SESSION_CLOSED


def test_signal_skipped_when_budget_cannot_size_one_contract(make_orb_sm, or_bars, bar):
    # With a deliberately tiny risk budget ($100), even a normal mid-stop
    # (~11 pt) costs more than the budget for one contract -> the signal is
    # skipped by the sizing rule. (At the production $1000 budget the 50-pt
    # stop cap guarantees >= 1 contract, so this path needs a low budget.)
    sm = make_orb_sm(risk_per_trade=100)
    bars = _long_breakout_day(or_bars, bar)
    bars.append(bar(datetime(2024, 1, 3, 9, 51), 17020, 17040, 17019, 17036))
    _drive(sm, bars)
    assert sm.state == OrbState.DAY_DONE
    assert sm.days_with_signal == 1
    assert sm.days_skipped_sizing == 1
    assert sm.trades == []
