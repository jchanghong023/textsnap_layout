"""Named-mutex single-instance authority and local command protocol."""

from __future__ import annotations

import ctypes
import os
from typing import Final, Protocol


ERROR_ALREADY_EXISTS: Final = 183
MUTEX_NAME: Final = r"Local\TextSnapLayout.SingleInstance.v1"
OPEN_SETTINGS_COMMAND: Final = "open-settings"
INSTANCE_COMMANDS: Final = frozenset({OPEN_SETTINGS_COMMAND})
INSTANCE_COMMAND_ENCODING: Final = "utf-8"
MAX_INSTANCE_COMMAND_BYTES: Final = 64


class MutexApi(Protocol):
    def create_mutex(self, name: str) -> tuple[object | None, bool, int]: ...

    def close_handle(self, handle: object) -> bool: ...

    def get_last_error(self) -> int: ...


class SingleInstanceError(RuntimeError):
    """A sanitized named-mutex failure."""

    def __init__(
        self,
        diagnostic_code: str,
        winerror: int | None = None,
    ) -> None:
        self.diagnostic_code = diagnostic_code
        self.winerror = winerror
        suffix = f" (Win32 error {winerror})" if winerror else ""
        super().__init__(f"无法确认应用实例状态。 [{diagnostic_code}]{suffix}")


class CtypesMutexApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise SingleInstanceError("INSTANCE-UNSUPPORTED")

        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create_mutex = self._kernel32.CreateMutexW
        self._create_mutex.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._create_mutex.restype = wintypes.HANDLE
        self._close_handle = self._kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

    def create_mutex(self, name: str) -> tuple[object | None, bool, int]:
        self._ctypes.set_last_error(0)
        handle = self._create_mutex(None, False, name)
        error = int(self._ctypes.get_last_error())
        return handle, error == ERROR_ALREADY_EXISTS, error

    def close_handle(self, handle: object) -> bool:
        self._ctypes.set_last_error(0)
        return bool(self._close_handle(handle))

    def get_last_error(self) -> int:
        return int(self._ctypes.get_last_error())


class SingleInstanceMutex:
    """Use a Local\\ named mutex as the sole authority for primary ownership."""

    def __init__(
        self,
        api: MutexApi | None = None,
        name: str = MUTEX_NAME,
    ) -> None:
        if (
            not name.startswith("Local\\")
            or "\0" in name
            or len(name) <= len("Local\\")
        ):
            raise ValueError("mutex name must use the Local namespace")
        self._name = name
        try:
            self._api = api if api is not None else CtypesMutexApi()
        except SingleInstanceError:
            raise
        except Exception:
            raise SingleInstanceError("INSTANCE-API-INIT") from None
        self._handle: object | None = None
        self._primary = False

    @property
    def is_primary(self) -> bool:
        return self._primary and self._handle is not None

    def acquire(self) -> bool:
        """Return True only when this process created the mutex first."""

        if self._handle is not None:
            return self.is_primary
        try:
            handle, already_exists, error = self._api.create_mutex(self._name)
        except Exception:
            raise SingleInstanceError("INSTANCE-CREATE-API") from None
        if handle is None:
            raise SingleInstanceError(
                "INSTANCE-CREATE",
                error or self._safe_last_error(),
            )

        self._handle = handle
        self._primary = not already_exists
        if already_exists:
            try:
                self.close()
            except SingleInstanceError as exc:
                raise SingleInstanceError(
                    "INSTANCE-SECONDARY-CLOSE",
                    exc.winerror,
                ) from None
            return False
        return True

    def close(self) -> None:
        """Close the mutex handle, retaining it when CloseHandle reports failure."""

        if self._handle is None:
            self._primary = False
            return
        try:
            closed = bool(self._api.close_handle(self._handle))
        except Exception:
            raise SingleInstanceError("INSTANCE-CLOSE-API") from None
        if not closed:
            raise SingleInstanceError(
                "INSTANCE-CLOSE",
                self._safe_last_error(),
            )
        self._handle = None
        self._primary = False

    def __enter__(self) -> SingleInstanceMutex:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _safe_last_error(self) -> int | None:
        try:
            error = int(self._api.get_last_error())
        except Exception:
            return None
        return error or None


def validate_instance_command(command: str) -> str:
    if not isinstance(command, str):
        raise TypeError("instance command must be str")
    if command not in INSTANCE_COMMANDS:
        raise ValueError("unsupported instance command")
    return command


def encode_instance_command(command: str) -> bytes:
    encoded = validate_instance_command(command).encode(INSTANCE_COMMAND_ENCODING)
    if len(encoded) > MAX_INSTANCE_COMMAND_BYTES:
        raise ValueError("instance command is too large")
    return encoded


def decode_instance_command(payload: bytes) -> str:
    if not isinstance(payload, bytes):
        raise TypeError("instance command payload must be bytes")
    if not payload or len(payload) > MAX_INSTANCE_COMMAND_BYTES:
        raise ValueError("invalid instance command payload size")
    try:
        command = payload.decode(INSTANCE_COMMAND_ENCODING, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("instance command payload is not UTF-8") from None
    return validate_instance_command(command)
