"""Process-wide offline policy without importing third-party packages.

The guard is intentionally installed before PaddleOCR/PaddleX are imported:
those packages snapshot several environment flags at import time.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import socket
import sys
from threading import RLock
from types import TracebackType
from typing import Any


_OFFLINE_ENVIRONMENT = {
    "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    "PADDLEOCR_DISABLE_AUTO_LOGGING_CONFIG": "1",
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}
_THIRD_PARTY_IMPORT_PREFIXES = (
    "aistudio_sdk",
    "cv2",
    "huggingface_hub",
    "modelscope",
    "numpy",
    "paddle",
    "paddleocr",
    "paddlex",
)
_MISSING = object()
_guard_lock = RLock()
_active_guard: OfflineGuard | None = None


class OfflineNetworkError(PermissionError):
    """Raised before an IPv4 or IPv6 connection can be attempted."""


def _third_party_already_imported() -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for module_name in sys.modules
        for prefix in _THIRD_PARTY_IMPORT_PREFIXES
    )


def offline_guard_active() -> bool:
    with _guard_lock:
        return _active_guard is not None


def require_offline_guard() -> None:
    """Fail closed if production OCR imports are attempted without the guard."""

    if not offline_guard_active():
        raise RuntimeError("offline guard must be installed before OCR imports")


class OfflineGuard:
    """Set import-time offline flags and deny Python IPv4/IPv6 sockets.

    No directory is created here. ``cache_home`` and its ``temp`` child must
    already exist in the packaged application so importing PaddleX cannot
    create a runtime cache directory.
    """

    def __init__(self, *, cache_home: Path | str, font_file: Path | str) -> None:
        self._cache_home = Path(cache_home)
        self._font_file = Path(font_file)
        self._previous_environment: dict[str, str | object] = {}
        self._originals: dict[str, Callable[..., Any]] = {}
        self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> OfflineGuard:
        global _active_guard

        with _guard_lock:
            if self._installed:
                return self
            if _active_guard is not None:
                raise RuntimeError("another offline guard is already installed")
            if _third_party_already_imported():
                raise RuntimeError(
                    "offline guard must be installed before third-party imports"
                )
            self._validate_packaged_resources()

            environment = dict(_OFFLINE_ENVIRONMENT)
            environment["PADDLE_PDX_CACHE_HOME"] = str(
                self._cache_home.resolve(strict=True)
            )
            environment["PADDLE_PDX_LOCAL_FONT_FILE_PATH"] = str(
                self._font_file.resolve(strict=True)
            )
            self._previous_environment = {
                key: os.environ.get(key, _MISSING) for key in environment
            }

            try:
                os.environ.update(environment)
                self._install_socket_blocks()
            except BaseException:
                self._restore_socket_functions()
                self._restore_environment()
                raise

            self._installed = True
            _active_guard = self
            return self

    def restore(self) -> None:
        global _active_guard

        with _guard_lock:
            if not self._installed:
                return
            self._restore_socket_functions()
            self._restore_environment()
            self._installed = False
            if _active_guard is self:
                _active_guard = None

    def __enter__(self) -> OfflineGuard:
        return self.install()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.restore()

    def _validate_packaged_resources(self) -> None:
        if not self._cache_home.is_absolute() or not self._font_file.is_absolute():
            raise ValueError("offline resource paths must be absolute")
        if not self._cache_home.is_dir():
            raise ValueError("packaged cache directory is missing")
        if not (self._cache_home / "temp").is_dir():
            raise ValueError("packaged cache temp directory is missing")
        if not self._font_file.is_file():
            raise ValueError("packaged local font is missing")

    def _install_socket_blocks(self) -> None:
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_sendto = socket.socket.sendto
        original_create_connection = socket.create_connection
        self._originals = {
            "connect": original_connect,
            "connect_ex": original_connect_ex,
            "sendto": original_sendto,
            "create_connection": original_create_connection,
        }

        def blocked_connect(sock: socket.socket, address: Any) -> Any:
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
            return original_connect(sock, address)

        def blocked_connect_ex(sock: socket.socket, address: Any) -> int:
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
            return original_connect_ex(sock, address)

        def blocked_sendto(sock: socket.socket, *args: Any, **kwargs: Any) -> int:
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
            return original_sendto(sock, *args, **kwargs)

        def blocked_create_connection(*args: Any, **kwargs: Any) -> socket.socket:
            raise OfflineNetworkError("IPv4/IPv6 network access is disabled")

        socket.socket.connect = blocked_connect
        socket.socket.connect_ex = blocked_connect_ex
        socket.socket.sendto = blocked_sendto
        socket.create_connection = blocked_create_connection

    def _restore_socket_functions(self) -> None:
        if not self._originals:
            return
        socket.socket.connect = self._originals["connect"]
        socket.socket.connect_ex = self._originals["connect_ex"]
        socket.socket.sendto = self._originals["sendto"]
        socket.create_connection = self._originals["create_connection"]
        self._originals.clear()

    def _restore_environment(self) -> None:
        for key, previous in self._previous_environment.items():
            if previous is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(previous)
        self._previous_environment.clear()
