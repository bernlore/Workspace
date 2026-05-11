"""Integration tests for fetch_phase3_data determinism contract.

These tests use only local synthetic data — no Alpaca credentials required.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

# Ensure scripts/ is importable from project root
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fetch_phase3_data import (  # noqa: E402
    DataIntegrityError,
    compute_sha256,
    verify_manifest,
    write_manifest,
    write_parquet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_synthetic_df(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """Build a deterministic n-row OHLCV DataFrame with UTC ts_utc column."""
    rng = pd.date_range("2024-01-02 14:30:00", periods=n, freq="1min", tz="UTC")
    import numpy as np

    rs = np.random.default_rng(seed)
    closes = 400.0 + rs.normal(0, 1, n).cumsum()
    opens = closes + rs.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + rs.uniform(0.05, 0.5, n)
    lows = np.minimum(opens, closes) - rs.uniform(0.05, 0.5, n)
    volumes = rs.integers(500_000, 2_000_000, n).astype(float)

    return pd.DataFrame(
        {
            "ts_utc": rng,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


# ---------------------------------------------------------------------------
# Test: byte-identical parquet on two writes of the same DataFrame
# ---------------------------------------------------------------------------

def test_write_parquet_is_deterministic(tmp_path: Path) -> None:
    """Same synthetic DataFrame written twice produces byte-identical parquet."""
    df = _make_synthetic_df(100)

    path_a = tmp_path / "a.parquet"
    path_b = tmp_path / "b.parquet"

    write_parquet(df, path_a)
    write_parquet(df, path_b)

    sha_a = compute_sha256(path_a)
    sha_b = compute_sha256(path_b)

    assert sha_a == sha_b, (
        f"Parquet files are not byte-identical: sha_a={sha_a[:16]}… sha_b={sha_b[:16]}…"
    )


# ---------------------------------------------------------------------------
# Test: output is sorted by ts_utc even when input is shuffled
# ---------------------------------------------------------------------------

def test_write_parquet_sorts_by_ts(tmp_path: Path) -> None:
    """Out-of-order input produces sorted output."""
    df = _make_synthetic_df(50)
    df_shuffled = df.sample(frac=1, random_state=7).reset_index(drop=True)

    # Verify input is actually out of order (sanity)
    assert not df_shuffled["ts_utc"].is_monotonic_increasing

    out_path = tmp_path / "sorted.parquet"
    write_parquet(df_shuffled, out_path)

    import pyarrow.parquet as pq

    result = pq.read_table(str(out_path)).to_pandas()
    result["ts_utc"] = pd.to_datetime(result["ts_utc"], utc=True)

    assert result["ts_utc"].is_monotonic_increasing, (
        "Parquet output is not sorted by ts_utc ascending"
    )


# ---------------------------------------------------------------------------
# Test: same file bytes produce same SHA-256 (stability)
# ---------------------------------------------------------------------------

def test_compute_sha256_stable(tmp_path: Path) -> None:
    """Same file bytes produce same SHA-256."""
    content = b"deterministic test content 12345"
    file_a = tmp_path / "file_a.bin"
    file_b = tmp_path / "file_b.bin"
    file_a.write_bytes(content)
    file_b.write_bytes(content)

    sha_a = compute_sha256(file_a)
    sha_b = compute_sha256(file_b)

    assert sha_a == sha_b
    # Also cross-check against stdlib
    expected = hashlib.sha256(content).hexdigest()
    assert sha_a == expected


# ---------------------------------------------------------------------------
# Test: verify_manifest raises DataIntegrityError on hash mismatch
# ---------------------------------------------------------------------------

def test_verify_manifest_raises_on_mismatch(tmp_path: Path) -> None:
    """Corrupt one byte in a parquet file; verify_manifest must raise DataIntegrityError."""
    df = _make_synthetic_df(30)
    filename = "QQQ_1m_2024H1.parquet"
    file_path = tmp_path / filename

    write_parquet(df, file_path)
    sha = compute_sha256(file_path)

    # Build a valid manifest
    files = {
        filename: {
            "sha256": sha,
            "rows": len(df),
            "date_range": ["2024-01-02", "2024-06-28"],
        }
    }
    write_manifest(tmp_path, files)

    # Corrupt one byte in the parquet file
    raw = bytearray(file_path.read_bytes())
    raw[-1] ^= 0xFF  # flip last byte
    file_path.write_bytes(bytes(raw))

    with pytest.raises(DataIntegrityError, match="Hash mismatch"):
        verify_manifest(tmp_path)


# ---------------------------------------------------------------------------
# Test: verify_manifest passes when all hashes match
# ---------------------------------------------------------------------------

def test_verify_manifest_passes_on_valid_files(tmp_path: Path) -> None:
    """verify_manifest returns True when all files match manifest."""
    df = _make_synthetic_df(20)
    filename = "SPY_1m_2024H1.parquet"
    file_path = tmp_path / filename

    write_parquet(df, file_path)
    sha = compute_sha256(file_path)

    files = {
        filename: {
            "sha256": sha,
            "rows": len(df),
            "date_range": ["2024-01-02", "2024-06-28"],
        }
    }
    write_manifest(tmp_path, files)

    result = verify_manifest(tmp_path)
    assert result is True


# ---------------------------------------------------------------------------
# Test: verify_manifest raises DataIntegrityError when file is missing
# ---------------------------------------------------------------------------

def test_verify_manifest_raises_on_missing_file(tmp_path: Path) -> None:
    """verify_manifest raises DataIntegrityError when a listed file is absent."""
    files = {
        "QQQ_1m_2024H1.parquet": {
            "sha256": "abc123",
            "rows": 100,
            "date_range": ["2024-01-02", "2024-06-28"],
        }
    }
    write_manifest(tmp_path, files)
    # Do NOT create the parquet file

    with pytest.raises(DataIntegrityError, match="not found"):
        verify_manifest(tmp_path)


# ---------------------------------------------------------------------------
# Optional: tests requiring Alpaca credentials (skipped in CI without keys)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("ALPACA_API_KEY"),
    reason="No ALPACA_API_KEY set — skipping live Alpaca fetch test",
)
def test_fetch_bars_live_qqq() -> None:
    """Smoke-test live fetch for a 1-day window (requires Alpaca credentials)."""
    from fetch_phase3_data import fetch_bars

    df = fetch_bars("QQQ", "2024-01-02", "2024-01-03")
    assert not df.empty
    assert list(df.columns) == ["ts_utc", "open", "high", "low", "close", "volume"]
    assert df["ts_utc"].is_monotonic_increasing
