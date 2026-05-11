"""Unit tests for core/logging_sink.py (EVENT_SCHEMA_V1 + JsonlSink)."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Any

import pytest
import structlog

from nasdaq_ale_bot.core import logging_sink as sink_mod
from nasdaq_ale_bot.core.logging_sink import (
    EVENT_SCHEMA_V1,
    SCHEMA_VERSION,
    EventSchemaV1,
    JsonlSink,
    install_jsonl_sink,
)

# ---------------------------------------------------------------------------
# Sample events — one per documented event type
# ---------------------------------------------------------------------------

_EVENT_TYPES = [
    "STATE_TRANSITION",
    "BIAS_FLIP_PENDING",
    "BIAS_FLIP_ACTIVE",
    "GATE_EVAL",
    "SKIP_MAX_STOP",
    "SKIP_PROJECTED_LOSS_LIMIT",
    "SKIP_NEWS_BLACKOUT",
    "TRADE_INTENT",
    "TRADE_FILLED",
    "TRADE_EXIT",
    "TIME_EXIT",
    "BE_MOVED",
    "SESSION_ROTATION",
    "SCHEMA_VIOLATION",
]


def _make_event(name: str) -> dict[str, Any]:
    return {
        "event": name,
        "level": "info",
        "state": "BIAS",
        "bar_ts": "2024-01-15T09:30:00+00:00",
        "timestamp": "2024-01-15T09:30:00.123456+00:00",
        "extra_field": "value",
        "symbol": "MNQ",
    }


# ---------------------------------------------------------------------------
# test_schema_version_constant_is_one
# ---------------------------------------------------------------------------


def test_schema_version_constant_is_one() -> None:
    assert SCHEMA_VERSION == 1
    assert EVENT_SCHEMA_V1 is EventSchemaV1


# ---------------------------------------------------------------------------
# test_round_trip_every_event_type
# ---------------------------------------------------------------------------


def test_round_trip_every_event_type(tmp_path: Path) -> None:
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    for name in _EVENT_TYPES:
        sink(None, "info", _make_event(name))

    lines = (tmp_path / "events.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == len(_EVENT_TYPES)

    for name, raw in zip(_EVENT_TYPES, lines):
        record = json.loads(raw)
        assert record["schema_version"] == 1
        assert record["event"] == name
        assert record["state"] == "BIAS"
        assert record["bar_ts"] == "2024-01-15T09:30:00+00:00"
        assert record["fields"]["symbol"] == "MNQ"
        assert record["fields"]["extra_field"] == "value"
        assert record["ts_utc"] == "2024-01-15T09:30:00.123456+00:00"
        assert record["level"] == "info"


# ---------------------------------------------------------------------------
# test_mandatory_fields_enforced
# ---------------------------------------------------------------------------


def test_missing_event_triggers_schema_violation(tmp_path: Path) -> None:
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    # No "event" key
    sink(None, "info", {"state": "BIAS", "level": "info"})
    lines = (tmp_path / "events.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "SCHEMA_VIOLATION"
    assert record["level"] == "error"
    assert "offending_event" in record["fields"]


def test_non_string_event_triggers_schema_violation(tmp_path: Path) -> None:
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    sink(None, "info", {"event": 123, "state": "BIAS"})
    lines = (tmp_path / "events.jsonl").read_text("utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["event"] == "SCHEMA_VIOLATION"


def test_missing_timestamp_auto_fills(tmp_path: Path) -> None:
    """When structlog hasn't added a timestamp yet, sink injects one."""
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    sink(None, "info", {"event": "TEST", "state": None})
    record = json.loads((tmp_path / "events.jsonl").read_text("utf-8").splitlines()[0])
    assert record["event"] == "TEST"
    assert record["ts_utc"]  # non-empty string


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotation_at_threshold(tmp_path: Path) -> None:
    sink = JsonlSink(
        path=tmp_path / "events.jsonl",
        rotate_at_bytes=256,
        max_backups=5,
        fsync_every_n=0,
    )
    for i in range(40):
        sink(None, "info", {"event": f"E{i}", "padding": "x" * 50})

    assert (tmp_path / "events.jsonl.1").exists()


def test_max_5_backups_drops_oldest(tmp_path: Path) -> None:
    sink = JsonlSink(
        path=tmp_path / "events.jsonl",
        rotate_at_bytes=128,
        max_backups=5,
        fsync_every_n=0,
    )
    # Force many rotations by writing large entries
    for i in range(120):
        sink(None, "info", {"event": f"E{i}", "padding": "x" * 100})

    # .5 should exist, .6 should not
    assert (tmp_path / "events.jsonl.5").exists()
    assert not (tmp_path / "events.jsonl.6").exists()


# ---------------------------------------------------------------------------
# fsync cadence
# ---------------------------------------------------------------------------


def test_fsync_cadence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def spy_fsync(fd: int) -> None:
        calls.append(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=5)
    for i in range(15):
        sink(None, "info", {"event": f"E{i}"})

    # 15 writes / 5 = 3 fsyncs
    assert len(calls) == 3


def test_fsync_disabled_when_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))

    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    for i in range(10):
        sink(None, "info", {"event": f"E{i}"})

    assert calls == []


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_runs_before_sink(tmp_path: Path) -> None:
    """install_jsonl_sink places sink AFTER drop_sensitive."""
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    install_jsonl_sink(sink)
    try:
        log = structlog.get_logger("redaction-test")
        log.info("TRADE_FILLED", api_key="sk-supersecret", symbol="MNQ")

        lines = (tmp_path / "events.jsonl").read_text("utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["fields"]["api_key"] == "***"
        assert "sk-supersecret" not in lines[0]
    finally:
        # Reset structlog config so other tests are unaffected
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------


class _Unserialisable:
    def __repr__(self) -> str:
        return "<Unserialisable instance>"


def test_sink_never_raises_into_engine(tmp_path: Path) -> None:
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    # Deliberately broken event — non-dict
    out = sink(None, "info", "not-a-dict")  # type: ignore[arg-type]
    assert out == "not-a-dict"

    # Object that defeats repr? use a plain object
    sink(None, "info", {"event": "OK", "obj": _Unserialisable()})
    lines = (tmp_path / "events.jsonl").read_text("utf-8").splitlines()
    # At least one SCHEMA_VIOLATION from the string event, plus OK record
    assert any(json.loads(ln)["event"] == "SCHEMA_VIOLATION" for ln in lines)
    assert any(json.loads(ln)["event"] == "OK" for ln in lines)


def test_sink_returns_event_dict_unchanged(tmp_path: Path) -> None:
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    ev = {"event": "TEST", "state": None, "x": 1}
    out = sink(None, "info", ev)
    assert out is ev  # identity preserved


# ---------------------------------------------------------------------------
# test_schema_constant_locked  (A2 — skipped until end of Phase 3.5)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Lock activated at end of Phase 3.5 — see PLAN.md §9.B follow-ups"
)
def test_schema_constant_locked() -> None:
    """Reads EventSchemaV1 source lines, SHA-256, compares to pinned hash.

    Lock file ``tests/unit/_schema_v1_lock.txt`` currently contains
    ``PENDING_PHASE_3_5``.  Activation steps (end of Phase 3.5):
        1. Verify Phase 3 backtest has emitted all documented event types.
        2. Fold any required top-level fields into EventSchemaV1.
        3. Compute SHA-256 of the source lines for EventSchemaV1.
        4. Commit the hex digest to _schema_v1_lock.txt.
        5. Remove this @skip marker.
    """
    src = inspect.getsource(EventSchemaV1)
    digest = hashlib.sha256(src.encode("utf-8")).hexdigest()
    lock_path = Path(__file__).parent / "_schema_v1_lock.txt"
    expected = lock_path.read_text("utf-8").strip()
    assert digest == expected, (
        f"EventSchemaV1 source changed; expected={expected} actual={digest}"
    )


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------


def test_jsonable_handles_various_types() -> None:
    j = sink_mod._jsonable
    assert j(None) is None
    assert j(1) == 1
    assert j("s") == "s"
    assert j(True) is True
    assert j([1, "a", None]) == [1, "a", None]
    assert j({"k": [1, 2]}) == {"k": [1, 2]}
    # Non-primitive falls through to str()
    class X:
        def __str__(self) -> str:
            return "X-str"

    assert j(X()) == "X-str"


def test_rotation_skips_when_file_missing(tmp_path: Path) -> None:
    """_rotate_if_needed silently returns when the file does not yet exist."""
    sink = JsonlSink(
        path=tmp_path / "nonexistent.jsonl",
        rotate_at_bytes=1,
        fsync_every_n=0,
    )
    # Directly invoking with no file yet should not raise
    sink._rotate_if_needed()


def test_now_iso_returns_microsecond_precision() -> None:
    val = sink_mod._now_iso()
    # e.g. 2024-01-15T09:30:00.123456+00:00  -> contains '.' for microseconds
    assert "." in val
    assert val.endswith("+00:00")


def test_level_coerced_to_valid(tmp_path: Path) -> None:
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    sink(None, "info", {"event": "E", "level": "BOGUS"})
    sink(None, "info", {"event": "E2", "level": 42})
    lines = (tmp_path / "events.jsonl").read_text("utf-8").splitlines()
    assert all(json.loads(ln)["level"] == "info" for ln in lines)


def test_state_and_bar_ts_coerced_to_string(tmp_path: Path) -> None:
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)
    sink(None, "info", {"event": "E", "state": 42, "bar_ts": 99})
    rec = json.loads((tmp_path / "events.jsonl").read_text("utf-8").splitlines()[0])
    assert rec["state"] == "42"
    assert rec["bar_ts"] == "99"


def test_fallback_suppresses_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If _write_line itself raises on the fallback path, sink stays silent."""
    sink = JsonlSink(path=tmp_path / "events.jsonl", fsync_every_n=0)

    # Trigger build_record to fail, then make _write_line raise
    def boom(self: JsonlSink, record: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(JsonlSink, "_write_line", boom)
    # Must NOT raise
    out = sink(None, "info", "broken")  # type: ignore[arg-type]
    assert out == "broken"
