"""Pydantic Candle model with tz-aware UTC timestamp and OHLCV invariants."""

from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator


class Candle(BaseModel):
    """A closed OHLCV bar on the execution timeframe."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @field_validator("ts")
    @classmethod
    def _ts_must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("Candle.ts must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _check_ohlcv_invariants(self) -> "Candle":
        if self.high < max(self.open, self.close):
            raise ValueError("Candle.high must be >= max(open, close)")
        if self.low > min(self.open, self.close):
            raise ValueError("Candle.low must be <= min(open, close)")
        if self.volume < 0:
            raise ValueError("Candle.volume must be >= 0")
        return self
