"""Liquidity level kinds and pydantic model."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class LiquidityKind(StrEnum):
    PDH = "PDH"
    PDL = "PDL"
    ASIA_HIGH = "ASIA_HIGH"
    ASIA_LOW = "ASIA_LOW"
    LONDON_HIGH = "LONDON_HIGH"
    LONDON_LOW = "LONDON_LOW"
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"
    MIDNIGHT_OPEN = "MIDNIGHT_OPEN"
    UNFILLED_4H_FVG = "UNFILLED_4H_FVG"


class LiquidityLevel(BaseModel):
    """A draw-on-liquidity price level with provenance."""

    kind: LiquidityKind
    price: float
    ts: datetime
