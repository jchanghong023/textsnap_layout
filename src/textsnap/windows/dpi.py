"""Process DPI-awareness setup that must run before QApplication is created."""

from __future__ import annotations

import os
from typing import Final, Protocol


PER_MONITOR_AWARE_V2: Final = -4


class DpiApi(Protocol):
    """Minimal User32 surface required to configure process DPI awareness."""

    def set_process_dpi_awareness_context(self, context: int) -> bool: ...

    def is_current_thread_per_monitor_v2(self) -> bool: ...

    def get_last_error(self) -> int: ...


class DpiAwarenessError(RuntimeError):
    """A sanitized, actionable DPI initialization failure."""

    def __init__(self, diagnostic_code: str, winerror: int | None = None) -> None:
        self.diagnostic_code = diagnostic_code
        self.winerror = winerror
        suffix = f" (Win32 error {winerror})" if winerror else ""
        super().__init__(
            f"Per-monitor V2 DPI awareness could not be enabled "
            f"[{diagnostic_code}]{suffix}; "
            "QApplication was not created."
        )


class CtypesDpiApi:
    """ctypes-backed User32 adapter, instantiated only on Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise DpiAwarenessError("DPI-UNSUPPORTED")

        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._set_context = self._user32.SetProcessDpiAwarenessContext
        self._set_context.argtypes = [ctypes.c_void_p]
        self._set_context.restype = wintypes.BOOL
        self._get_thread_context = self._user32.GetThreadDpiAwarenessContext
        self._get_thread_context.argtypes = []
        self._get_thread_context.restype = ctypes.c_void_p
        self._contexts_equal = self._user32.AreDpiAwarenessContextsEqual
        self._contexts_equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._contexts_equal.restype = wintypes.BOOL

    def set_process_dpi_awareness_context(self, context: int) -> bool:
        self._ctypes.set_last_error(0)
        return bool(self._set_context(self._ctypes.c_void_p(context)))

    def is_current_thread_per_monitor_v2(self) -> bool:
        current = self._get_thread_context()
        if not current:
            return False
        return bool(
            self._contexts_equal(
                current,
                self._ctypes.c_void_p(PER_MONITOR_AWARE_V2),
            )
        )

    def get_last_error(self) -> int:
        return int(self._ctypes.get_last_error())


def enable_per_monitor_v2(api: DpiApi | None = None) -> None:
    """Enable PerMonitorV2, raising explicitly instead of silently degrading.

    The application entry point must call this function before constructing any
    QApplication or other HWND-owning Qt object.
    """

    selected_api = api
    try:
        if selected_api is None:
            selected_api = CtypesDpiApi()
        if selected_api.set_process_dpi_awareness_context(PER_MONITOR_AWARE_V2):
            return
        # An executable or an embedding host may already have established the
        # exact required context. SetProcessDpiAwarenessContext then reports
        # failure, but this is not a downgrade and must not prevent startup.
        if selected_api.is_current_thread_per_monitor_v2():
            return
        raise DpiAwarenessError(
            "DPI-SET-FAILED",
            _safe_last_error(selected_api),
        )
    except DpiAwarenessError:
        raise
    except Exception:
        raise DpiAwarenessError("DPI-API-FAILED") from None


def _safe_last_error(api: DpiApi) -> int | None:
    try:
        error = int(api.get_last_error())
    except Exception:
        return None
    return error or None
