"""Enforce: detection/ and core/ and filters/ contain no alpaca imports."""

from pathlib import Path

FORBIDDEN_PREFIXES = ("alpaca",)
DIRS = ["detection", "core", "filters"]


def test_no_alpaca_imports_in_pure_layers():
    root = Path(__file__).resolve().parents[2] / "src" / "nasdaq_ale_bot"
    violations: list[str] = []
    for d in DIRS:
        for py in (root / d).rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for prefix in FORBIDDEN_PREFIXES:
                    if f"import {prefix}" in stripped or f"from {prefix}" in stripped:
                        violations.append(f"{py}: {stripped}")
    assert violations == [], f"Alpaca imports found in pure layers: {violations}"
