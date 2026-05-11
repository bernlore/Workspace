"""Unit tests for FuturesSpec / InstrumentSpec validation (Step 1e)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from nasdaq_ale_bot.settings import (
    FuturesSpec,
    InstrumentSpec,
    InstrumentsConfig,
    load_instruments_config,
)


def test_load_live_instruments_yaml() -> None:
    """The committed instruments.yaml loads and parses cleanly."""
    cfg = load_instruments_config(Path("config/instruments.yaml"))
    assert cfg.primary.symbol == "NQ"
    assert cfg.correlated.symbol == "ES"
    assert cfg.mnq is not None
    assert cfg.es is not None


def test_primary_correlated_have_futures_blocks() -> None:
    """Primary (NQ) and correlated (ES) are futures contracts."""
    cfg = load_instruments_config(Path("config/instruments.yaml"))
    assert cfg.primary.futures is not None
    assert cfg.correlated.futures is not None


def test_mnq_futures_parsed() -> None:
    cfg = load_instruments_config(Path("config/instruments.yaml"))
    assert cfg.mnq is not None
    assert cfg.mnq.futures is not None
    fs = cfg.mnq.futures
    assert fs.tick_value_usd == Decimal("0.50")
    assert fs.contract_size == 2
    assert fs.margin_requirement_usd == Decimal("1500")
    assert fs.rth_session == "CME_GLOBEX"


def test_es_futures_parsed() -> None:
    cfg = load_instruments_config(Path("config/instruments.yaml"))
    assert cfg.es is not None
    assert cfg.es.futures is not None
    fs = cfg.es.futures
    assert fs.tick_value_usd == Decimal("12.50")
    assert fs.contract_size == 1
    assert fs.margin_requirement_usd == Decimal("13200")


def test_futures_block_optional(tmp_path: Path) -> None:
    """An instrument with no `futures:` key parses cleanly."""
    (tmp_path / "instruments.yaml").write_text(
        yaml.safe_dump(
            {
                "primary": {
                    "symbol": "QQQ",
                    "tick": 0.01,
                    "point_value": 1.0,
                    "atr_ratio_vs_nq": 0.05,
                    "calendar_id": "NYSE",
                },
                "correlated": {
                    "symbol": "SPY",
                    "tick": 0.01,
                    "point_value": 1.0,
                    "atr_ratio_vs_nq": 0.05,
                    "calendar_id": "NYSE",
                },
            }
        )
    )
    cfg = load_instruments_config(tmp_path / "instruments.yaml")
    assert cfg.mnq is None
    assert cfg.es is None
    assert cfg.primary.futures is None


def test_futures_tick_value_must_be_positive() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        FuturesSpec(
            contract_size=1,
            tick_value_usd=Decimal("0"),
            margin_requirement_usd=Decimal("100"),
            rth_session="CME_GLOBEX",
        )


def test_instrument_spec_forbids_unknown_keys() -> None:
    with pytest.raises(Exception):
        InstrumentSpec.model_validate(
            {
                "symbol": "QQQ",
                "tick": 0.01,
                "point_value": 1.0,
                "atr_ratio_vs_nq": 0.05,
                "calendar_id": "NYSE",
                "bogus_field": 42,  # must trigger extra=forbid
            }
        )


def test_instruments_config_forbids_unknown_keys() -> None:
    with pytest.raises(Exception):
        InstrumentsConfig.model_validate(
            {
                "primary": {
                    "symbol": "QQQ",
                    "tick": 0.01,
                    "point_value": 1.0,
                    "atr_ratio_vs_nq": 0.05,
                    "calendar_id": "NYSE",
                },
                "correlated": {
                    "symbol": "SPY",
                    "tick": 0.01,
                    "point_value": 1.0,
                    "atr_ratio_vs_nq": 0.05,
                    "calendar_id": "NYSE",
                },
                "random_extra": {},
            }
        )


def test_futures_spec_is_frozen() -> None:
    spec = FuturesSpec(
        contract_size=2,
        tick_value_usd=Decimal("0.50"),
        margin_requirement_usd=Decimal("1500"),
        rth_session="CME_GLOBEX",
    )
    with pytest.raises(Exception):
        spec.contract_size = 4  # type: ignore[misc]
