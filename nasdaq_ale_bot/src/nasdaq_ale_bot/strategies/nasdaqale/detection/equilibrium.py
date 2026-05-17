"""Discount / premium equilibrium checks using Fib 0.5 of a leg."""

from nasdaq_ale_bot.core.leg import Leg


def _midpoint(leg: Leg) -> float:
    return (leg.low + leg.high) / 2.0


def is_in_discount(price: float, leg: Leg) -> bool:
    return price <= _midpoint(leg)


def is_in_premium(price: float, leg: Leg) -> bool:
    return price >= _midpoint(leg)
