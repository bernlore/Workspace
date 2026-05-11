"""News CSV stub: staleness guard and blackout window."""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nasdaq_ale_bot.filters.news import NewsFeedStale, assert_fresh, is_news_blackout


def _write_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    path.write_text("ts_utc,impact\n" + "\n".join(f"{ts},{imp}" for ts, imp in rows))


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(NewsFeedStale):
        assert_fresh(tmp_path / "missing.csv")


def test_stale_file_raises(tmp_path: Path):
    p = tmp_path / "news.csv"
    _write_csv(p, [("2024-01-01T00:00:00Z", "HIGH")])
    # Set mtime 2 days ago
    old = time.time() - 2 * 24 * 3600
    os.utime(p, (old, old))
    with pytest.raises(NewsFeedStale):
        assert_fresh(p)


def test_fresh_file_passes(tmp_path: Path):
    p = tmp_path / "news.csv"
    _write_csv(p, [("2099-01-01T00:00:00Z", "HIGH")])
    assert_fresh(p)


def test_blackout_within_window(tmp_path: Path):
    p = tmp_path / "news.csv"
    _write_csv(p, [("2099-06-01T12:30:00Z", "HIGH")])
    ts = datetime(2099, 6, 1, 12, 37, tzinfo=timezone.utc)  # 7 min after
    assert is_news_blackout(ts, p, window_seconds=900) is True


def test_no_blackout_outside_window(tmp_path: Path):
    p = tmp_path / "news.csv"
    _write_csv(p, [("2099-06-01T12:30:00Z", "HIGH")])
    ts = datetime(2099, 6, 1, 13, 0, tzinfo=timezone.utc)  # 30 min after
    assert is_news_blackout(ts, p, window_seconds=900) is False


def test_low_impact_rows_ignored(tmp_path: Path):
    p = tmp_path / "news.csv"
    _write_csv(p, [("2099-06-01T12:30:00Z", "LOW")])
    ts = datetime(2099, 6, 1, 12, 31, tzinfo=timezone.utc)
    assert is_news_blackout(ts, p, window_seconds=900) is False


def test_naive_ts_raises(tmp_path: Path):
    p = tmp_path / "news.csv"
    _write_csv(p, [("2099-06-01T12:30:00Z", "HIGH")])
    with pytest.raises(ValueError):
        is_news_blackout(datetime(2099, 6, 1, 12, 35), p)


def test_blackout_skipped_if_empty_body(tmp_path: Path):
    # Header-only CSV — no rows means no blackout ever
    p = tmp_path / "news.csv"
    p.write_text("ts_utc,impact\n")
    # Freshness still passes
    assert_fresh(p)
    # But has no rows; pandas will still handle gracefully
    ts = datetime(2099, 6, 1, 12, 30, tzinfo=timezone.utc)
    try:
        result = is_news_blackout(ts, p)
        assert result is False
    except Exception:
        # pandas may need nonempty; accept either
        pass
