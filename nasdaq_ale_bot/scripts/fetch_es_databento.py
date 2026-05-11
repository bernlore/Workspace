#!/usr/bin/env python3
"""Fetch ES.c.0 (continuous front-month) 1-minute OHLCV from Databento.

Dataset:   GLBX.MDP3
Schema:    ohlcv-1m
Symbol:    ES.c.0  (continuous front-month, no back-adjust)
Range:     2023-01-01 → 2025-04-25
Output:    data/historical/ES_1m_databento.parquet

Determinism contract (mirrors fetch_phase3_data.py):
- Sorted by ts_utc ascending, deduplicated
- Fixed column order: [ts_utc, open, high, low, close, volume]
- gzip compression, level 6, store_schema=False, single row group
- Pandas/Arrow metadata stripped → byte-identical across environments

Usage:
    python scripts/fetch_es_databento.py [--out-dir data/historical] [--verify-only]

Env:
    DATABENTO_API_KEY    -- required for fetch
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

_DATASET = "GLBX.MDP3"
_SYMBOL = "ES.c.0"
_SCHEMA = "ohlcv-1m"
_STYPE_IN = "continuous"
_START = "2023-01-01"
_END = "2025-04-25"

_FILENAME = "ES_1m_databento.parquet"
_COLUMNS = ["ts_utc", "open", "high", "low", "close", "volume"]


class DataIntegrityError(Exception):
    """SHA-256 mismatch."""


def fetch_es_dataframe(start: str, end: str, api_key: str) -> pd.DataFrame:
    """Pull ES.c.0 ohlcv-1m via Databento, return DataFrame with our schema."""
    try:
        import databento as db
    except ImportError:
        log.error("databento not installed. Run: pip install databento")
        sys.exit(1)

    client = db.Historical(api_key)
    log.info(
        "Fetching %s %s %s %s → %s …", _DATASET, _SYMBOL, _SCHEMA, start, end
    )
    store = client.timeseries.get_range(
        dataset=_DATASET,
        symbols=_SYMBOL,
        schema=_SCHEMA,
        stype_in=_STYPE_IN,
        start=start,
        end=end,
    )
    df = store.to_df()
    if df.empty:
        log.error("Databento returned 0 rows for %s %s..%s", _SYMBOL, start, end)
        sys.exit(1)

    # Databento OHLCV-1m: timestamps live in the index ('ts_event' = bar start),
    # already UTC-aware. Prices are float; volume is integer.
    if "ts_event" in df.columns:
        ts = df["ts_event"]
    else:
        ts = df.index
    out = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(ts, utc=True),
            "open": df["open"].astype(float),
            "high": df["high"].astype(float),
            "low": df["low"].astype(float),
            "close": df["close"].astype(float),
            "volume": df["volume"].astype(float),
        }
    )
    out = (
        out.drop_duplicates(subset=["ts_utc"], keep="first")
        .sort_values("ts_utc")
        .reset_index(drop=True)
    )
    log.info("  fetched %d 1m bars", len(out))
    return out


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    df = df[_COLUMNS].sort_values("ts_utc").reset_index(drop=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
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
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def update_manifest(out_dir: Path, filename: str, sha: str, rows: int, date_range: list[str]) -> None:
    """Merge a new file entry into the existing manifest.json (preserve other entries)."""
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {}
    manifest.setdefault("files", {})
    manifest["files"][filename] = {
        "sha256": sha,
        "rows": rows,
        "date_range": date_range,
    }
    manifest["fetch_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log.info("Manifest updated: %s", manifest_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default="data/historical",
        help="Output directory.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip fetch; just re-hash the parquet against the manifest.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / _FILENAME

    if args.verify_only:
        manifest_path = out_dir / "manifest.json"
        if not manifest_path.exists() or not file_path.exists():
            log.error("Verify-only: manifest or file missing")
            sys.exit(2)
        manifest = json.loads(manifest_path.read_text())
        expected = manifest.get("files", {}).get(_FILENAME, {}).get("sha256")
        actual = compute_sha256(file_path)
        if expected != actual:
            log.error("SHA mismatch: expected %s actual %s", expected[:16], actual[:16])
            sys.exit(2)
        log.info("Verified %s OK", _FILENAME)
        sys.exit(0)

    api_key = os.environ.get("DATABENTO_API_KEY", "")
    if not api_key:
        log.error("DATABENTO_API_KEY missing in environment")
        sys.exit(1)

    df = fetch_es_dataframe(_START, _END, api_key)
    write_parquet(df, file_path)
    sha = compute_sha256(file_path)
    date_range = [str(df["ts_utc"].min().date()), str(df["ts_utc"].max().date())]
    update_manifest(out_dir, _FILENAME, sha, len(df), date_range)
    log.info("%s: sha256=%s… rows=%d range=%s..%s",
             _FILENAME, sha[:16], len(df), date_range[0], date_range[1])
    sys.exit(0)


if __name__ == "__main__":
    main()
