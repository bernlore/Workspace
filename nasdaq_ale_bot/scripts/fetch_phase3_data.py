#!/usr/bin/env python3
"""Fetch QQQ + SPY 1m bars for 2024H1 from Alpaca. Gzip'd Parquet output.

Usage:
    python scripts/fetch_phase3_data.py [--out-dir data/historical] [--verify-only]

Environment variables:
    ALPACA_API_KEY   -- Alpaca paper-account API key
    ALPACA_SECRET_KEY -- Alpaca paper-account secret key

Idempotent: if parquet files exist and SHA-256 matches manifest, exits 0.
Exit codes: 0 = success, 1 = error, 2 = data integrity mismatch.

Credentials help: https://app.alpaca.markets/paper/dashboard/overview
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_SYMBOLS = ["QQQ", "SPY"]
_START = "2024-01-01"
_END = "2025-12-31"
_ALPACA_API_VERSION = "v2"
# ~500 trading days * 375 minutes/day extended ETH = many rows; keep a single
# row group so SHA-256 stays stable across environments.
_COLUMNS = ["ts_utc", "open", "high", "low", "close", "volume"]
_FILENAME_TAG = "2024_2025"


class DataIntegrityError(Exception):
    """Raised when a parquet file's SHA-256 does not match the manifest."""


def fetch_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch 1m bars from Alpaca StockHistoricalDataClient.

    Returns a DataFrame with columns [ts_utc, open, high, low, close, volume]
    sorted by ts_utc ascending.
    """
    try:
        import alpaca.data.historical as adh
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    except ImportError:
        log.error("alpaca-py not installed. Run: pip install alpaca-py")
        sys.exit(1)

    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        log.error(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. "
            "See https://app.alpaca.markets/paper/dashboard/overview"
        )
        sys.exit(1)

    client = adh.StockHistoricalDataClient(api_key, secret_key)
    request = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        end=end,
        adjustment="split",
    )
    log.info("Fetching %s bars %s -> %s …", symbol, start, end)
    data = client.get_stock_bars(request)
    bars = data[symbol]

    rows = [
        {
            "ts_utc": bar.timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        }
        for bar in bars
    ]
    df = pd.DataFrame(rows, columns=_COLUMNS)
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.sort_values("ts_utc").reset_index(drop=True)
    log.info("  %s: %d bars fetched", symbol, len(df))
    return df


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write DataFrame to gzip-compressed Parquet with determinism guarantees.

    Determinism contract:
    - Sorted by ts_utc ascending.
    - Fixed column order: [ts_utc, open, high, low, close, volume].
    - compression='gzip', compression_level=6.
    - store_schema=False, write_statistics=False (no embedded metadata).
    - Single row group (row_group_size = full dataset length).
    - Pandas metadata stripped from schema via replace_schema_metadata({}).
    """
    df = df[_COLUMNS].sort_values("ts_utc").reset_index(drop=True)
    table = pa.Table.from_pandas(df, preserve_index=False)

    # Strip pandas/pyarrow metadata so output is byte-identical across environments
    table = table.replace_schema_metadata({})

    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        str(path),
        compression="gzip",
        compression_level=6,
        store_schema=False,
        write_statistics=False,
        row_group_size=max(len(df), 1),
    )


def compute_sha256(path: Path) -> str:
    """Return hex SHA-256 digest of file at path."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(out_dir: Path, files: dict) -> None:
    """Write manifest.json with SHA-256 per file and fetch metadata.

    Args:
        out_dir: Directory containing the parquet files.
        files: Dict mapping filename -> {"sha256": ..., "rows": ..., "date_range": ...}
    """
    manifest = {
        "fetch_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "alpaca_api_version": _ALPACA_API_VERSION,
        "files": files,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log.info("Manifest written to %s", manifest_path)


def verify_manifest(out_dir: Path) -> bool:
    """Re-compute SHA-256 for each file listed in manifest.json.

    Returns True if all hashes match.
    Raises DataIntegrityError with details on any mismatch.
    """
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise DataIntegrityError(f"Manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text())
    for filename, meta in manifest.get("files", {}).items():
        file_path = out_dir / filename
        if not file_path.exists():
            raise DataIntegrityError(f"File listed in manifest not found: {file_path}")
        actual = compute_sha256(file_path)
        expected = meta["sha256"]
        if actual != expected:
            raise DataIntegrityError(
                f"Hash mismatch for {filename}: "
                f"expected={expected[:16]}… actual={actual[:16]}…"
            )
    log.info("Manifest verification passed (%d files OK)", len(manifest.get("files", {})))
    return True


def _parquet_filename(symbol: str) -> str:
    return f"{symbol}_1m_{_FILENAME_TAG}.parquet"


def _is_up_to_date(out_dir: Path) -> bool:
    """Return True if all parquet files exist and hashes match manifest."""
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text())
    for symbol in _SYMBOLS:
        filename = _parquet_filename(symbol)
        if filename not in manifest.get("files", {}):
            return False
        file_path = out_dir / filename
        if not file_path.exists():
            return False
        actual = compute_sha256(file_path)
        if actual != manifest["files"][filename]["sha256"]:
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default="data/historical",
        help="Directory to write parquet files and manifest.json",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip fetch; only verify SHA-256 hashes against manifest (for CI).",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.verify_only:
        try:
            verify_manifest(out_dir)
            log.info("All files verified OK.")
            sys.exit(0)
        except DataIntegrityError as exc:
            log.error("Data integrity check failed: %s", exc)
            sys.exit(2)

    # Idempotency check — skip fetch if hashes already match
    if _is_up_to_date(out_dir):
        log.info("All files up-to-date; skipping fetch.")
        sys.exit(0)

    # Check credentials before fetching
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        log.error(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. "
            "See https://app.alpaca.markets/paper/dashboard/overview"
        )
        sys.exit(1)

    files: dict = {}
    for symbol in _SYMBOLS:
        try:
            df = fetch_bars(symbol, _START, _END)
        except Exception as exc:
            log.error("Failed to fetch %s: %s", symbol, type(exc).__name__)
            sys.exit(1)

        filename = _parquet_filename(symbol)
        file_path = out_dir / filename
        write_parquet(df, file_path)

        sha = compute_sha256(file_path)
        ts_col = df["ts_utc"]
        date_range = [
            str(ts_col.min().date()),
            str(ts_col.max().date()),
        ]
        files[filename] = {
            "sha256": sha,
            "rows": len(df),
            "date_range": date_range,
        }
        log.info("%s: sha256=%s… rows=%d", filename, sha[:16], len(df))

    write_manifest(out_dir, files)

    # Final integrity verification
    try:
        verify_manifest(out_dir)
    except DataIntegrityError as exc:
        log.error("Post-write integrity check failed: %s", exc)
        sys.exit(2)

    log.info("Phase 3 data fetch complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
