"""Trend filter: reject counter-trend setups."""

from ..core.leg import Direction


def is_with_trend(bias_direction: Direction | None, setup_direction: Direction) -> bool:
    return bias_direction is not None and bias_direction == setup_direction
