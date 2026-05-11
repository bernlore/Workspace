#!/usr/bin/env python3
"""Fetch QQQ 1-minute bars for a pinned window from Alpaca and write to
``tests/fixtures/qqq_1m_sample.csv``.  Also updates
``tests/fixtures/data_hashes.json`` with the file's SHA-256 so CI can
verify data integrity on subsequent runs.

Pinned window: 2024-01-02 09:30 ET -> 2024-01-03 16:00 ET (one full
session plus overnight) — chosen for stable historical data.

Usage::

    python scripts/fetch_phase2_fixture.py [--out-dir tests/fixtures]

Environment variables required:
    ALPACA_API_KEY   — Alpaca paper-account API key
    ALPACA_SECRET_KEY — Alpaca paper-account secret key

The script is idempotent: if the fixture already exists and its hash
matches, it exits 0 without re-fetching.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_bars(api_key: str, secret_key: str, out_path: Path) -> None:
    try:
        import alpaca.data.historical as adh
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    except ImportError:
        print(
            "ERROR: alpaca-trade-api not installed. Run: pip install alpaca-py",
            file=sys.stderr,
        )
        sys.exit(1)

    client = adh.StockHistoricalDataClient(api_key, secret_key)
    request = StockBarsRequest(
        symbol_or_symbols=["QQQ"],
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start="2024-01-02T14:30:00Z",  # 09:30 ET
        end="2024-01-03T21:00:00Z",    # 16:00 ET next day
        adjustment="split",
    )
    data = client.get_stock_bars(request)
    bars = data["QQQ"]

    fieldnames = ["ts", "open", "high", "low", "close", "volume"]
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "ts": bar.timestamp.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            )
    print(f"Wrote {len(bars)} bars to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default="tests/fixtures",
        help="Directory to write qqq_1m_sample.csv and data_hashes.json",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "qqq_1m_sample.csv"
    hash_path = out_dir / "data_hashes.json"

    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        print(
            "ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check if fixture already up-to-date
    if csv_path.exists() and hash_path.exists():
        hashes = json.loads(hash_path.read_text())
        existing = _sha256(csv_path)
        if hashes.get("qqq_1m_sample.csv") == existing:
            print(f"Fixture up-to-date (sha256={existing[:16]}…), skipping fetch.")
            return

    _fetch_bars(api_key, secret_key, csv_path)

    sha = _sha256(csv_path)
    hashes: dict[str, str] = {}
    if hash_path.exists():
        hashes = json.loads(hash_path.read_text())
    hashes["qqq_1m_sample.csv"] = sha
    hash_path.write_text(json.dumps(hashes, indent=2))
    print(f"SHA-256: {sha}")


if __name__ == "__main__":
    main()
