"""Equilibrium (Fib 0.5) checks."""

from nasdaq_ale_bot.core.leg import Direction, Leg
from nasdaq_ale_bot.strategies.nasdaqale.detection.equilibrium import is_in_discount, is_in_premium


def _leg() -> Leg:
    return Leg(start_idx=0, end_idx=10, direction=Direction.UP, low=100.0, high=110.0)


def test_midpoint_is_both_discount_and_premium():
    leg = _leg()
    assert is_in_discount(105.0, leg)
    assert is_in_premium(105.0, leg)


def test_below_midpoint_is_discount():
    leg = _leg()
    assert is_in_discount(102.0, leg)
    assert not is_in_premium(102.0, leg)


def test_above_midpoint_is_premium():
    leg = _leg()
    assert is_in_premium(108.0, leg)
    assert not is_in_discount(108.0, leg)
