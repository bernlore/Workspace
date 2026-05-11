"""News blackout filter backed by a CSV stub (Phase 1) or a live feed (Phase 4)."""

import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


class NewsFeedStale(RuntimeError):
    """Raised when the news CSV is missing or older than the allowed age."""


_cache: dict[tuple[str, float], pd.DataFrame] = {}


def assert_fresh(csv_path: Path, max_age_hours: float = 24.0) -> None:
    if not csv_path.exists():
        raise NewsFeedStale(f"news CSV missing: {csv_path}")
    age_s = time.time() - csv_path.stat().st_mtime
    if age_s > max_age_hours * 3600:
        raise NewsFeedStale(f"news CSV stale: age {age_s / 3600:.1f}h > {max_age_hours}h")


def _load(csv_path: Path) -> pd.DataFrame:
    mtime = csv_path.stat().st_mtime
    key = (str(csv_path), mtime)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    df = pd.read_csv(csv_path, parse_dates=["ts_utc"])
    # Ensure tz-aware UTC
    if df["ts_utc"].dt.tz is None:
        df["ts_utc"] = df["ts_utc"].dt.tz_localize("UTC")
    _cache.clear()
    _cache[key] = df
    return df


def is_news_blackout(
    ts_utc: datetime,
    csv_path: Path,
    window_seconds: int = 900,
) -> bool:
    """Return True iff ts_utc is within +/- window_seconds of any HIGH-impact event."""
    assert_fresh(csv_path)
    if ts_utc.tzinfo is None:
        raise ValueError("ts_utc must be timezone-aware")
    df = _load(csv_path)
    if df.empty:
        return False
    window = timedelta(seconds=window_seconds)
    high = df[df["impact"].str.upper() == "HIGH"]
    for event_ts in high["ts_utc"]:
        delta = event_ts.to_pydatetime() - ts_utc
        if abs(delta) <= window:
            return True
    return False
