"""In-memory diagnostics that deliberately omit content and user paths."""

from __future__ import annotations

from dataclasses import dataclass
import platform
import re
import traceback

from . import __version__
from .domain import Failure

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


@dataclass(frozen=True, slots=True)
class DiagnosticInfo:
    application_version: str
    system_version: str
    error_type: str
    diagnostic_code: str
    internal_frames: tuple[str, ...]

    def render(self) -> str:
        lines = [
            f"TextSnap Layout {self.application_version}",
            f"System: {self.system_version}",
            f"Error: {self.error_type}",
            f"Code: {self.diagnostic_code}",
        ]
        if self.internal_frames:
            lines.append("Internal stack:")
            lines.extend(f"  {frame}" for frame in self.internal_frames)
        return "\n".join(lines)


def _safe_token(value: str, fallback: str) -> str:
    return value if _SAFE_TOKEN.fullmatch(value) else fallback


def _system_version() -> str:
    value = " ".join(
        part
        for part in (platform.system(), platform.release(), platform.version())
        if part
    )
    return value.replace("\r", " ").replace("\n", " ") or "Unknown"


def diagnostic_from_failure(failure: Failure) -> DiagnosticInfo:
    """Create content-free diagnostics from an already sanitized failure."""

    if not isinstance(failure, Failure):
        raise TypeError("failure must be Failure")
    return DiagnosticInfo(
        application_version=__version__,
        system_version=_system_version(),
        error_type=_safe_token(failure.error_type, "InternalError"),
        diagnostic_code=_safe_token(failure.diagnostic_code, "internal-error"),
        internal_frames=(),
    )


def diagnostic_from_exception(
    exception: BaseException,
    *,
    diagnostic_code: str,
    maximum_frames: int = 12,
) -> DiagnosticInfo:
    """Extract only module/function/line for frames owned by this application."""

    frames: list[str] = []
    traceback_object = exception.__traceback__
    if traceback_object is not None:
        for frame, line_number in traceback.walk_tb(traceback_object):
            module_name = str(frame.f_globals.get("__name__", ""))
            if not (module_name == "textsnap" or module_name.startswith("textsnap.")):
                continue
            function_name = frame.f_code.co_name
            module = _safe_token(module_name, "textsnap")
            function = _safe_token(function_name, "internal")
            frames.append(f"{module}:{function}:{int(line_number)}")
    if maximum_frames < 0:
        raise ValueError("maximum_frames must be non-negative")
    frames = frames[-maximum_frames:] if maximum_frames else []

    return DiagnosticInfo(
        application_version=__version__,
        system_version=_system_version(),
        error_type=_safe_token(type(exception).__name__, "InternalError"),
        diagnostic_code=_safe_token(diagnostic_code, "internal-error"),
        internal_frames=tuple(frames),
    )
