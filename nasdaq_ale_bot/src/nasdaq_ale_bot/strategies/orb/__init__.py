"""ORB — 15-minute Opening Range Breakout strategy for NQ.

A mechanically simple strategy: build the opening range from the first
15 one-minute bars of the NY session, then take the first 5-minute-close
breakout of that range. Max one trade per day.

Modules:
  opening_range     — accumulates and freezes the 15-bar opening range
  breakout_detector — 5-min aggregation + breakout-signal detection
  state_machine     — 5-state ORB engine (SESSION_CLOSED .. DAY_DONE)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_orb_config(path: Path | None = None) -> dict[str, Any]:
    """Load ``config/orb_strategy.yaml`` as a plain dict."""
    cfg_path = path or Path("config/orb_strategy.yaml")
    with Path(cfg_path).open() as fh:
        return yaml.safe_load(fh)  # type: ignore[return-value]
