"""Tests for ORB position sizing and stop/target placement (pure functions)."""

import pytest

from nasdaq_ale_bot.strategies.orb.state_machine import (
    compute_position_size,
    compute_stop_target,
)

PV = 20.0  # NQ point value
RR = 1.5   # reward:risk multiple


# --- compute_position_size --------------------------------------------------

def test_sizing_basic_floor_division():
    # floor(500 / (20 * 20)) = floor(1.25) = 1
    assert compute_position_size(
        risk_budget=500, stop_distance_points=20, point_value=PV,
        min_contracts=1, max_contracts=4,
    ) == 1


def test_sizing_tight_stop_allows_more_contracts():
    # floor(500 / (10 * 20)) = floor(2.5) = 2
    assert compute_position_size(
        risk_budget=500, stop_distance_points=10, point_value=PV,
        min_contracts=1, max_contracts=4,
    ) == 2


def test_sizing_clamped_to_max_contracts():
    # floor(500 / (5 * 20)) = 5  ->  clamped to max 4
    assert compute_position_size(
        risk_budget=500, stop_distance_points=5, point_value=PV,
        min_contracts=1, max_contracts=4,
    ) == 4


def test_sizing_skips_when_one_contract_exceeds_budget():
    # floor(500 / (30 * 20)) = floor(0.83) = 0  ->  cannot afford 1  ->  None
    assert compute_position_size(
        risk_budget=500, stop_distance_points=30, point_value=PV,
        min_contracts=1, max_contracts=4,
    ) is None


def test_sizing_skips_on_nonpositive_stop_distance():
    assert compute_position_size(
        risk_budget=500, stop_distance_points=0, point_value=PV,
        min_contracts=1, max_contracts=4,
    ) is None


def test_sizing_exact_one_contract_boundary():
    # floor(500 / (25 * 20)) = floor(1.0) = 1
    assert compute_position_size(
        risk_budget=500, stop_distance_points=25, point_value=PV,
        min_contracts=1, max_contracts=4,
    ) == 1


# --- compute_stop_target ----------------------------------------------------

def test_long_stop_target_uncapped():
    # OR midpoint = (17015 + 17000) / 2 = 17007.5
    stop, dist, target = compute_stop_target(
        direction="LONG", entry_price=17017.0,
        or_high=17015.0, or_low=17000.0,
        buffer=0.5, max_stop_points=50.0, rr_multiple=RR,
    )
    assert stop == 17007.0            # or_mid - buffer
    assert dist == 10.0               # entry - stop
    assert target == 17032.0          # entry + stop_dist * 1.5


def test_long_stop_capped_at_max():
    # OR midpoint = (17080 + 17000) / 2 = 17040; raw stop 17039.5.
    stop, dist, target = compute_stop_target(
        direction="LONG", entry_price=17100.0,
        or_high=17080.0, or_low=17000.0,
        buffer=0.5, max_stop_points=50.0, rr_multiple=RR,
    )
    # raw stop distance = 17100 - 17039.5 = 60.5 > 50  ->  capped.
    assert stop == 17050.0            # entry - max_stop_points
    assert dist == 50.0
    # Target derives from the CAPPED stop distance: 50 * 1.5 = 75.
    assert target == 17175.0          # entry + 50 * 1.5


def test_short_stop_target_uncapped():
    # OR midpoint = (17000 + 16985) / 2 = 16992.5
    stop, dist, target = compute_stop_target(
        direction="SHORT", entry_price=16983.0,
        or_high=17000.0, or_low=16985.0,
        buffer=0.5, max_stop_points=50.0, rr_multiple=RR,
    )
    assert stop == 16993.0            # or_mid + buffer
    assert dist == 10.0               # stop - entry
    assert target == 16968.0          # entry - stop_dist * 1.5


def test_short_stop_capped_at_max():
    # OR midpoint = (17000 + 16920) / 2 = 16960; raw stop 16960.5.
    stop, dist, target = compute_stop_target(
        direction="SHORT", entry_price=16900.0,
        or_high=17000.0, or_low=16920.0,
        buffer=0.5, max_stop_points=50.0, rr_multiple=RR,
    )
    # raw stop distance = 16960.5 - 16900 = 60.5 > 50  ->  capped.
    assert stop == 16950.0            # entry + max_stop_points
    assert dist == 50.0
    assert target == 16825.0          # entry - 50 * 1.5


@pytest.mark.parametrize("or_range", [14.0, 43.0, 60.0])
def test_rr_is_exactly_constant_across_range_sizes(or_range):
    """R:R must be exactly 1:rr_multiple regardless of opening-range size —
    the whole point of deriving the target from the stop distance."""
    or_low = 17000.0
    or_high = or_low + or_range
    for direction, entry in (("LONG", or_high + 5.0), ("SHORT", or_low - 5.0)):
        stop, dist, target = compute_stop_target(
            direction=direction, entry_price=entry,
            or_high=or_high, or_low=or_low,
            buffer=0.5, max_stop_points=50.0, rr_multiple=RR,
        )
        target_dist = abs(target - entry)
        assert target_dist == pytest.approx(dist * RR)
        assert dist > 0
