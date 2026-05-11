"""Alpaca credential loading with pydantic-settings. Never leaks secrets.

Also provides strict pydantic models for ``config/instruments.yaml`` so the
engine loads contracts with full type validation.  Phase 2 adds
:class:`FuturesSpec` for the MNQ / ES futures blocks required by Phase 5.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class AlpacaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ALPACA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str
    secret_key: str
    paper: bool = True
    base_url: str = "https://paper-api.alpaca.markets"


def load_alpaca_settings() -> AlpacaSettings:
    try:
        return AlpacaSettings()  # type: ignore[call-arg]
    except ValidationError:  # pragma: no cover - trivial
        # Never echo the raw exception (may contain partial secret values).
        raise RuntimeError(
            "Alpaca credentials missing. Copy .env.example to .env and fill in "
            "your paper keys from https://app.alpaca.markets/paper/dashboard/overview"
        ) from None


# ---------------------------------------------------------------------------
# Instrument configuration (config/instruments.yaml)
# ---------------------------------------------------------------------------


class FuturesSpec(BaseModel):
    """Futures-specific contract metadata (Phase 2 pre-wiring for Phase 5).

    Missing on equity instruments (QQQ, SPY).  Present on MNQ and ES.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_size: int = Field(gt=0)
    tick_value_usd: Decimal = Field(gt=Decimal("0"))
    margin_requirement_usd: Decimal = Field(ge=Decimal("0"))
    rth_session: str
    maintenance_window: str | None = None


class InstrumentSpec(BaseModel):
    """One entry from ``config/instruments.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    tick: Decimal = Field(gt=Decimal("0"))
    point_value: Decimal = Field(gt=Decimal("0"))
    atr_ratio_vs_nq: Decimal = Field(ge=Decimal("0"))
    calendar_id: str
    futures: FuturesSpec | None = None


class InstrumentsConfig(BaseModel):
    """Typed view over ``config/instruments.yaml``.

    The YAML keys ``primary``, ``correlated`` are the Phase 1 equity pair.
    ``mnq`` and ``es`` are the Phase 2 futures additions.  Unknown keys
    forbidden to catch typos early.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: InstrumentSpec
    correlated: InstrumentSpec
    mnq: InstrumentSpec | None = None
    es: InstrumentSpec | None = None


def load_instruments_config(
    path: Path | str = Path("config/instruments.yaml"),
) -> InstrumentsConfig:
    """Load ``instruments.yaml`` into a strict :class:`InstrumentsConfig`."""
    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}
    return InstrumentsConfig.model_validate(raw)
