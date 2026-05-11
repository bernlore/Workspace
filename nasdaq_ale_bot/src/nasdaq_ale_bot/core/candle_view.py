"""Look-ahead-proof view over a candle list (A24)."""

from .candle import Candle


class LookAheadError(Exception):
    """Raised when a detection function attempts to read a bar beyond its horizon."""


class CandleView:
    """Wraps a candle list, exposing only bars at indices <= i."""

    def __init__(self, bars: list[Candle], i: int) -> None:
        self._bars = bars
        self._i = i

    def __len__(self) -> int:
        return self._i + 1

    def __getitem__(self, k: int) -> Candle:
        if isinstance(k, slice):
            raise TypeError("CandleView does not support slicing")
        if not isinstance(k, int):
            raise TypeError(f"CandleView index must be int, got {type(k).__name__}")
        if k < 0:
            k += self._i + 1
        if k < 0 or k > self._i:
            raise LookAheadError(f"index {k} > horizon {self._i}")
        return self._bars[k]
