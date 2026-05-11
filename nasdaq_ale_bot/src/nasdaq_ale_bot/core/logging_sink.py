"""Append-only JSONL sink for the NasdaqAle ICT Trading Bot.

Mirrors every structlog event to `.omc/state/bot_events.jsonl` using the
frozen :class:`EventSchemaV1` contract.  Phase 6 GUI will tail this file;
the schema MUST remain stable.

Schema stability contract (A2):
    SCHEMA_VERSION is frozen at 1 in Phase 2.  A SHA-256 lock-file test
    ships skipped until the end of Phase 3.5, at which point Phase 3's
    emitted event types are considered fully discovered and any missing
    top-level fields must be folded in BEFORE activating the lock.

Schema-violation safety (W4):
    When an inbound event fails validation, the sink writes a synthetic
    SCHEMA_VIOLATION event assembled from a HARD-CODED TEMPLATE containing
    every mandatory field pre-populated.  The template NEVER routes back
    through the validation path, preventing a theoretical infinite loop
    where a bug in the fallback path itself triggers more schema failures.
    The original (offending) event is NOT written; its repr is truncated
    to 500 chars and stored in `fields.offending_event`.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

import structlog

_log = structlog.get_logger("nasdaq_ale_bot.core.logging_sink")

# ---------------------------------------------------------------------------
# Frozen schema v1
# ---------------------------------------------------------------------------

SCHEMA_VERSION: Final[int] = 1  # FROZEN — bump requires explicit ADR


class EventSchemaV1(TypedDict):
    """Top-level JSONL event schema — v1, frozen for Phase 2+.

    All seven keys are mandatory and appear on every line.  Optional
    per-event data lives inside ``fields``; new top-level keys require
    a SCHEMA_VERSION bump and an ADR.
    """

    schema_version: int
    ts_utc: str
    level: Literal["debug", "info", "warning", "error", "critical"]
    event: str
    state: str | None
    bar_ts: str | None
    fields: dict[str, Any]


EVENT_SCHEMA_V1: Final = EventSchemaV1  # re-export for Phase 6


_MANDATORY_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "ts_utc", "level", "event", "state", "bar_ts", "fields"}
)

_ALLOWED_LEVELS: Final[frozenset[str]] = frozenset(
    {"debug", "info", "warning", "error", "critical"}
)


# ---------------------------------------------------------------------------
# JsonlSink
# ---------------------------------------------------------------------------


class JsonlSink:
    """structlog processor that appends one JSONL line per event.

    Rotation:  at ``rotate_at_bytes``, the current file is renamed to
    ``<name>.jsonl.1``; earlier backups shift up to ``.5``; anything beyond
    is discarded.

    fsync:  every ``fsync_every_n`` writes a durable fsync is issued.

    Thread-safety:  a single ``threading.Lock`` serialises write+rotate.
    """

    def __init__(
        self,
        *,
        path: Path | str = Path(".omc/state/bot_events.jsonl"),
        rotate_at_bytes: int = 50 * 1024 * 1024,
        max_backups: int = 5,
        fsync_every_n: int = 10,
    ) -> None:
        self._path: Path = Path(path)
        self._rotate_at_bytes = rotate_at_bytes
        self._max_backups = max_backups
        self._fsync_every_n = fsync_every_n
        self._write_count = 0
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # structlog processor entry point
    # ------------------------------------------------------------------

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate + persist.  Never raises into the engine thread.

        Returns the original ``event_dict`` unchanged so downstream
        processors (console renderer, etc.) still see the full payload.
        """
        try:
            record = self._build_record(event_dict)
            self._write_line(record)
        except Exception as exc:  # noqa: BLE001 - defensive top-level
            # W4 FIX: hard-coded fallback template never routes through
            # validation; if even this fails, we swallow rather than
            # raising into the engine thread.
            try:
                fallback = self._schema_violation_template(event_dict, str(exc))
                self._write_line(fallback)
            except Exception:  # noqa: BLE001
                # Last-resort: drop silently.  The engine must not die
                # because logging has stopped working.
                pass
        return event_dict

    # ------------------------------------------------------------------
    # Validation + record assembly
    # ------------------------------------------------------------------

    def _build_record(self, event_dict: dict[str, Any]) -> dict[str, Any]:
        """Shape a raw structlog event into EventSchemaV1.

        Extracts the 7 mandatory top-level fields; every unknown key is
        collapsed into ``fields``.  Raises ``ValueError`` on any violation
        so ``__call__`` can route to the SCHEMA_VIOLATION fallback.
        """
        if not isinstance(event_dict, dict):
            raise ValueError("event_dict is not a dict")

        event_name = event_dict.get("event")
        if not isinstance(event_name, str) or not event_name:
            raise ValueError("missing or non-string 'event' key")

        ts_utc = event_dict.get("timestamp") or event_dict.get("ts_utc")
        if not isinstance(ts_utc, str) or not ts_utc:
            ts_utc = _now_iso()

        level = event_dict.get("level")
        if not isinstance(level, str):
            level = "info"
        level = level.lower()
        if level not in _ALLOWED_LEVELS:
            level = "info"

        state = event_dict.get("state")
        if state is not None and not isinstance(state, str):
            state = str(state)

        bar_ts = event_dict.get("bar_ts")
        if bar_ts is not None and not isinstance(bar_ts, str):
            bar_ts = str(bar_ts)

        reserved = {"event", "level", "timestamp", "ts_utc", "state", "bar_ts"}
        fields: dict[str, Any] = {}
        for key, value in event_dict.items():
            if key in reserved:
                continue
            fields[key] = _jsonable(value)

        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts_utc": ts_utc,
            "level": level,
            "event": event_name,
            "state": state,
            "bar_ts": bar_ts,
            "fields": fields,
        }

        if set(record.keys()) != _MANDATORY_KEYS:
            raise ValueError("record does not match EVENT_SCHEMA_V1 keys")
        return record

    @staticmethod
    def _schema_violation_template(
        offending: Any,
        error: str,
    ) -> dict[str, Any]:
        """W4 FIX: hard-coded SCHEMA_VIOLATION event.

        Bypasses ``_build_record`` entirely so a bug in validation logic
        cannot cause recursive violations.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "ts_utc": _now_iso(),
            "level": "error",
            "event": "SCHEMA_VIOLATION",
            "state": None,
            "bar_ts": None,
            "fields": {
                "offending_event": repr(offending)[:500],
                "error": error[:500],
            },
        }

    # ------------------------------------------------------------------
    # Write + rotation + fsync
    # ------------------------------------------------------------------

    def _write_line(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._rotate_if_needed()
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
                fh.flush()
                self._write_count += 1
                if self._fsync_every_n > 0 and (
                    self._write_count % self._fsync_every_n == 0
                ):
                    os.fsync(fh.fileno())

    def _rotate_if_needed(self) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size < self._rotate_at_bytes:
            return

        # Drop the oldest backup if it exists
        oldest = self._backup_path(self._max_backups)
        if oldest.exists():
            oldest.unlink()
        # Shift .(N-1) -> .N, ..., .1 -> .2
        for n in range(self._max_backups - 1, 0, -1):
            src = self._backup_path(n)
            dst = self._backup_path(n + 1)
            if src.exists():
                src.rename(dst)
        # Current file -> .1
        self._path.rename(self._backup_path(1))

    def _backup_path(self, n: int) -> Path:
        return self._path.with_suffix(self._path.suffix + f".{n}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _jsonable(value: Any) -> Any:
    """Best-effort coercion to a JSON-serialisable primitive."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


# ---------------------------------------------------------------------------
# Install hook
# ---------------------------------------------------------------------------


def install_jsonl_sink(sink: JsonlSink) -> None:
    """Insert ``sink`` into the structlog processor chain.

    The sink is placed AFTER :func:`logging_setup.drop_sensitive` so
    redacted payloads are the ones persisted to JSONL.  The JSON
    renderer follows, so console output still shows the event.
    """
    from nasdaq_ale_bot.logging_setup import drop_sensitive  # local import

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            drop_sensitive,
            sink,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        cache_logger_on_first_use=False,
    )
