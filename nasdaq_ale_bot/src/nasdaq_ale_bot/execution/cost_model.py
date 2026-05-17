"""Unified trading-cost model — single source of truth for commission and
slippage across every backtest path.

Loaded from ``config/cost_model.yaml``. Consumed by :class:`MockBroker`,
which applies the costs directly to ``realized_pnl``; downstream scripts must
not add costs separately (no double-counting). See AI_INSIGHTS #3.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CostModel:
    """Per-instrument execution-cost parameters.

    commission_per_side_per_contract — broker commission charged on each side
        (entry and exit) of one contract.
    slippage_ticks_per_side — adverse price movement modelled on each fill,
        in ticks (a buy fills higher, a sell fills lower, by this many ticks).
    tick_value_usd — dollar value of one tick for one contract.
    """

    instrument: str
    commission_per_side_per_contract: Decimal
    slippage_ticks_per_side: int
    tick_value_usd: Decimal

    @property
    def commission_round_trip(self) -> Decimal:
        """Round-trip commission per contract (entry side + exit side)."""
        return self.commission_per_side_per_contract * 2

    def slippage_price(self, tick_size: Decimal) -> Decimal:
        """Adverse price shift per fill, in price units, for the given tick size."""
        return Decimal(self.slippage_ticks_per_side) * tick_size


def load_cost_model(path: Path, instrument: str) -> CostModel:
    """Load the cost model for ``instrument`` (e.g. ``"nq"``, ``"mnq"``)."""
    with Path(path).open() as fh:
        data = yaml.safe_load(fh)
    key = instrument.lower()
    if not isinstance(data, dict) or key not in data:
        raise KeyError(
            f"cost_model.yaml has no block for instrument={instrument!r}"
        )
    block = data[key]
    return CostModel(
        instrument=key,
        commission_per_side_per_contract=Decimal(
            str(block["commission_per_side_per_contract_usd"])
        ),
        slippage_ticks_per_side=int(block["slippage_ticks_per_side"]),
        tick_value_usd=Decimal(str(block["tick_value_usd"])),
    )
