"""Directional leg primitive used by equilibrium and TP scans."""

from enum import StrEnum

from pydantic import BaseModel


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class Leg(BaseModel):
    """A directional price leg between two bar indices."""

    start_idx: int
    end_idx: int
    direction: Direction
    low: float
    high: float
