"""Opt-in, content-free runtime diagnostics for GUI startup investigation.

The portable application normally creates no log files.  When
``TEXTSNAP_DIAGNOSTIC_LOG`` names an absolute path outside the bundle, this
module appends one JSON object per phase to that file.  Callers only provide
bounded tokens and numeric measurements; screenshots, OCR text, exception
messages, and user paths are deliberately excluded.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from threading import RLock, get_ident
from time import monotonic_ns
from typing import Any


DIAGNOSTIC_LOG_ENVIRONMENT = "TEXTSNAP_DIAGNOSTIC_LOG"
_EVENT_PATTERN = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
_FIELD_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.:+-]{1,160}\Z")
_state_lock = RLock()
_active_log: _RuntimeDiagnosticLog | None = None


class _RuntimeDiagnosticLog:
    """A fail-safe append-only JSONL writer shared by GUI and OCR threads."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._started_ns = monotonic_ns()
        self._write_lock = RLock()
        self._failed = False

    def record(self, event: str, details: Mapping[str, object]) -> None:
        if self._failed or not _EVENT_PATTERN.fullmatch(event):
            return
        safe_details: dict[str, object] = {}
        for key, value in details.items():
            if not _FIELD_PATTERN.fullmatch(key):
                continue
            safe_details[key] = _safe_detail(value)
        document = {
            "timestamp_utc": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "elapsed_ms": round((monotonic_ns() - self._started_ns) / 1_000_000, 3),
            "pid": os.getpid(),
            "thread_id": get_ident(),
            "event": event,
            **safe_details,
        }
        payload = (
            json.dumps(
                document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with self._write_lock:
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(self._descriptor, view)
                    if written <= 0:
                        raise OSError("diagnostic write made no progress")
                    view = view[written:]
            except (OSError, ValueError):
                self._failed = True

    def close(self) -> None:
        with self._write_lock:
            descriptor = self._descriptor
            self._descriptor = -1
            if descriptor < 0:
                return
            try:
                os.close(descriptor)
            except OSError:
                pass


def start_runtime_diagnostics(
    bundle_root: Path | str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Start an optional diagnostic session without making startup depend on it."""

    selected = os.environ if environment is None else environment
    requested = selected.get(DIAGNOSTIC_LOG_ENVIRONMENT)
    if not requested:
        return False
    descriptor = -1
    try:
        root = Path(bundle_root).resolve(strict=False)
        destination = Path(requested)
        if not destination.is_absolute():
            return False
        destination = destination.resolve(strict=False)
        if destination == root or root in destination.parents:
            return False
        if not destination.parent.is_dir():
            return False
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        descriptor = os.open(destination, flags, 0o600)
        active = _RuntimeDiagnosticLog(descriptor)
        descriptor = -1
    except (OSError, RuntimeError, TypeError, ValueError):
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return False

    global _active_log
    with _state_lock:
        previous = _active_log
        _active_log = active
    if previous is not None:
        previous.close()
    active.record(
        "process.start",
        {
            "python": (
                f"{os.sys.version_info.major}."
                f"{os.sys.version_info.minor}."
                f"{os.sys.version_info.micro}"
            ),
            "platform": os.sys.platform,
        },
    )
    return True


def record_runtime_event(event: str, **details: object) -> None:
    """Append one sanitized event; logging failures never affect the application."""

    with _state_lock:
        active = _active_log
    if active is not None:
        active.record(event, details)


def stop_runtime_diagnostics() -> None:
    """Close the current diagnostic destination, if one was enabled."""

    global _active_log
    with _state_lock:
        active = _active_log
        _active_log = None
    if active is not None:
        active.close()


def runtime_diagnostics_active() -> bool:
    with _state_lock:
        return _active_log is not None


def _safe_detail(value: Any) -> object:
    if value is None or isinstance(value, bool) or (
        isinstance(value, int) and not isinstance(value, bool)
    ):
        return value
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) else "invalid"
    if isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value):
        return value
    return "redacted"
