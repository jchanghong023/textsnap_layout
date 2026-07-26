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
_NAME_RESOLUTION_FUNCTIONS = (
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
    "getfqdn",
)
_AUDIT_NAME_RESOLUTION_EVENTS = frozenset(
    {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }
)
_AUDIT_PROBE_EVENT = "textsnap.offline_guard.audit_probe"
_AUDIT_HOOK_UNINITIALIZED = 0
_AUDIT_HOOK_READY = 1
_AUDIT_HOOK_FAILED = 2
_WINDOWS_NOT_A_SOCKET = 10038
_MISSING = object()
_RAW_SOCKET_TYPE = socket.SocketType
_RAW_SOCKET_FAMILY_DESCRIPTOR = _RAW_SOCKET_TYPE.family
_RAW_SOCKET_FROM_SHARE = getattr(socket, "fromshare", None)
_guard_lock = RLock()
_active_guard: OfflineGuard | None = None
_audit_hook_state = _AUDIT_HOOK_UNINITIALIZED
_audit_probe_token: object | None = None
_audit_probe_seen = False


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


def _socket_family_from_python_socket(socket_object: object) -> int:
    """Read the native family without invoking subclass attribute overrides."""

    try:
        family = _RAW_SOCKET_FAMILY_DESCRIPTOR.__get__(
            socket_object,
            _RAW_SOCKET_TYPE,
        )
        return int(family)
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise OfflineNetworkError(
            "cannot safely classify a Python socket"
        ) from None


def _offline_socket_audit_hook(event: str, arguments: tuple[Any, ...]) -> None:
    """Deny low-level ``_socket`` operations while one guard is active."""

    global _audit_probe_seen

    if event == _AUDIT_PROBE_EVENT:
        if (
            _audit_probe_token is not None
            and len(arguments) == 1
            and arguments[0] is _audit_probe_token
        ):
            _audit_probe_seen = True
        return
    if _active_guard is None:
        return
    if event in {"socket.bind", "socket.connect", "socket.sendto"}:
        if (
            arguments
            and _socket_family_from_python_socket(arguments[0])
            in {socket.AF_INET, socket.AF_INET6}
        ):
            raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
        return
    if event in _AUDIT_NAME_RESOLUTION_EVENTS:
        raise OfflineNetworkError("IPv4/IPv6 name resolution is disabled")


def _ensure_socket_audit_hook() -> None:
    """Install and verify the process-wide audit backstop exactly once."""

    global _audit_hook_state
    global _audit_probe_seen
    global _audit_probe_token

    if _audit_hook_state == _AUDIT_HOOK_READY:
        return
    if _audit_hook_state == _AUDIT_HOOK_FAILED:
        raise RuntimeError("offline socket audit hook is unavailable")

    # Audit hooks cannot be removed. Mark this one attempt as failed until a
    # private probe proves the hook was registered; a suppressed or exceptional
    # registration must never lead to repeated hook accumulation.
    _audit_hook_state = _AUDIT_HOOK_FAILED
    token = object()
    _audit_probe_token = token
    _audit_probe_seen = False
    try:
        sys.addaudithook(_offline_socket_audit_hook)
        sys.audit(_AUDIT_PROBE_EVENT, token)
        if not _audit_probe_seen:
            raise RuntimeError("offline socket audit hook registration was suppressed")
    except BaseException:
        raise RuntimeError("offline socket audit hook is unavailable") from None
    finally:
        _audit_probe_token = None
        _audit_probe_seen = False
    _audit_hook_state = _AUDIT_HOOK_READY


def _socket_family_from_windows_handle(handle: object) -> int | None:
    """Return a Winsock handle's family without taking ownership of it."""

    probe: Any | None = None
    duplicate: Any | None = None
    try:
        # The raw Windows constructor cannot infer a family from ``fileno``
        # alone. Wrapping with AF_UNSPEC first distinguishes SOCKET handles
        # from files/pipes; WSADuplicateSocket then reports the real family.
        probe = _RAW_SOCKET_TYPE(0, 0, 0, fileno=handle)
        if _RAW_SOCKET_FROM_SHARE is None:
            raise OfflineNetworkError(
                "Windows socket family inspection is unavailable"
            )
        duplicate = _RAW_SOCKET_FROM_SHARE(probe.share(os.getpid()))
        family = _socket_family_from_python_socket(duplicate)
    except OSError as exc:
        if (
            getattr(exc, "winerror", None) == _WINDOWS_NOT_A_SOCKET
            or getattr(exc, "errno", None) == _WINDOWS_NOT_A_SOCKET
        ):
            return None
        raise OfflineNetworkError(
            "cannot safely classify a Windows I/O handle"
        ) from None
    except (OverflowError, TypeError, ValueError):
        raise OfflineNetworkError(
            "cannot safely classify a Windows I/O handle"
        ) from None
    finally:
        try:
            if duplicate is not None:
                duplicate.close()
        finally:
            if probe is not None:
                try:
                    probe.detach()
                except OSError:
                    raise OfflineNetworkError(
                        "cannot safely release a Windows socket probe"
                    ) from None
    return int(family)


def _call_with_windows_handle_guard(
    operation: Callable[..., Any],
    handles: tuple[object, ...],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Serialize a guarded overlapped operation with install and restore."""

    with _guard_lock:
        if _active_guard is not None:
            for handle in handles:
                if _socket_family_from_windows_handle(handle) in {
                    socket.AF_INET,
                    socket.AF_INET6,
                }:
                    raise OfflineNetworkError(
                        "IPv4/IPv6 network access is disabled"
                    )
        return operation(*args, **kwargs)


class _OverlappedProxy:
    """Proxy Windows OVERLAPPED I/O while one offline guard is active."""

    __slots__ = ("_inner",)

    def __init__(self, inner: object) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_inner":
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    def AcceptEx(
        self,
        listen_handle: object,
        accept_handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.AcceptEx,
            (listen_handle, accept_handle),
            listen_handle,
            accept_handle,
            *args,
            **kwargs,
        )

    def ConnectEx(
        self,
        client_handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.ConnectEx,
            (client_handle,),
            client_handle,
            *args,
            **kwargs,
        )

    def ConnectNamedPipe(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return self._inner.ConnectNamedPipe(handle, *args, **kwargs)

    def DisconnectEx(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # Disconnecting an existing socket is cleanup, not network access.
        return self._inner.DisconnectEx(handle, *args, **kwargs)

    def ReadFile(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.ReadFile,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def ReadFileInto(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.ReadFileInto,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def TransmitFile(
        self,
        socket_handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.TransmitFile,
            (socket_handle,),
            socket_handle,
            *args,
            **kwargs,
        )

    def WSARecv(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.WSARecv,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def WSARecvFrom(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.WSARecvFrom,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def WSARecvFromInto(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.WSARecvFromInto,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def WSARecvInto(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.WSARecvInto,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def WSASend(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.WSASend,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def WSASendTo(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.WSASendTo,
            (handle,),
            handle,
            *args,
            **kwargs,
        )

    def WriteFile(
        self,
        handle: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return _call_with_windows_handle_guard(
            self._inner.WriteFile,
            (handle,),
            handle,
            *args,
            **kwargs,
        )


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
        self._overlapped_module: Any | None = None
        self._overlapped_originals: dict[str, Any] = {}
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
                _ensure_socket_audit_hook()
                os.environ.update(environment)
                self._install_socket_blocks()
                self._install_windows_overlapped_blocks()
            except BaseException:
                try:
                    self._restore_windows_overlapped_functions()
                finally:
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
            self._restore_windows_overlapped_functions()
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
        original_bind = socket.socket.bind
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_sendto = socket.socket.sendto
        original_create_connection = socket.create_connection
        self._originals = {
            "bind": original_bind,
            "connect": original_connect,
            "connect_ex": original_connect_ex,
            "sendto": original_sendto,
            "create_connection": original_create_connection,
            **{
                function_name: getattr(socket, function_name)
                for function_name in _NAME_RESOLUTION_FUNCTIONS
            },
        }

        def blocked_bind(sock: socket.socket, address: Any) -> None:
            if _socket_family_from_python_socket(sock) in {
                socket.AF_INET,
                socket.AF_INET6,
            }:
                raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
            return original_bind(sock, address)

        def blocked_connect(sock: socket.socket, address: Any) -> Any:
            if _socket_family_from_python_socket(sock) in {
                socket.AF_INET,
                socket.AF_INET6,
            }:
                raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
            return original_connect(sock, address)

        def blocked_connect_ex(sock: socket.socket, address: Any) -> int:
            if _socket_family_from_python_socket(sock) in {
                socket.AF_INET,
                socket.AF_INET6,
            }:
                raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
            return original_connect_ex(sock, address)

        def blocked_sendto(sock: socket.socket, *args: Any, **kwargs: Any) -> int:
            if _socket_family_from_python_socket(sock) in {
                socket.AF_INET,
                socket.AF_INET6,
            }:
                raise OfflineNetworkError("IPv4/IPv6 network access is disabled")
            return original_sendto(sock, *args, **kwargs)

        def blocked_create_connection(*args: Any, **kwargs: Any) -> socket.socket:
            raise OfflineNetworkError("IPv4/IPv6 network access is disabled")

        def blocked_name_resolution(*args: Any, **kwargs: Any) -> Any:
            raise OfflineNetworkError("IPv4/IPv6 name resolution is disabled")

        socket.socket.bind = blocked_bind
        socket.socket.connect = blocked_connect
        socket.socket.connect_ex = blocked_connect_ex
        socket.socket.sendto = blocked_sendto
        socket.create_connection = blocked_create_connection
        for function_name in _NAME_RESOLUTION_FUNCTIONS:
            setattr(socket, function_name, blocked_name_resolution)

    def _install_windows_overlapped_blocks(self) -> None:
        if sys.platform != "win32":
            return

        # This covers Python's Windows async socket implementation. It is
        # defense in depth, not an OS sandbox: native DLLs and ctypes can call
        # Winsock directly and remain subject to Windows acceptance testing.
        overlapped = __import__("_overlapped")
        originals = {
            name: getattr(overlapped, name)
            for name in ("WSAConnect", "BindLocal", "Overlapped")
        }
        original_type = originals["Overlapped"]
        required_methods = (
            "AcceptEx",
            "ConnectEx",
            "ConnectNamedPipe",
            "DisconnectEx",
            "ReadFile",
            "ReadFileInto",
            "TransmitFile",
            "WSARecv",
            "WSARecvFrom",
            "WSARecvFromInto",
            "WSARecvInto",
            "WSASend",
            "WSASendTo",
            "WriteFile",
        )
        if not callable(original_type) or not all(
            callable(getattr(original_type, name, None))
            for name in required_methods
        ):
            raise RuntimeError("Windows overlapped I/O guard is unavailable")

        def guarded_wsa_connect(
            client_handle: object,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            return _call_with_windows_handle_guard(
                originals["WSAConnect"],
                (client_handle,),
                client_handle,
                *args,
                **kwargs,
            )

        def guarded_bind_local(
            handle: object,
            family: object,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            return _call_with_windows_handle_guard(
                originals["BindLocal"],
                (handle,),
                handle,
                family,
                *args,
                **kwargs,
            )

        class GuardedOverlapped(_OverlappedProxy):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(original_type(*args, **kwargs))

        GuardedOverlapped.__name__ = "Overlapped"
        GuardedOverlapped.__qualname__ = "Overlapped"
        GuardedOverlapped.__module__ = "_overlapped"

        self._overlapped_module = overlapped
        self._overlapped_originals = originals
        try:
            overlapped.WSAConnect = guarded_wsa_connect
            overlapped.BindLocal = guarded_bind_local
            overlapped.Overlapped = GuardedOverlapped
        except BaseException:
            self._restore_windows_overlapped_functions()
            raise RuntimeError("cannot install Windows overlapped I/O guard") from None

    def _restore_windows_overlapped_functions(self) -> None:
        overlapped = self._overlapped_module
        if overlapped is None or not self._overlapped_originals:
            return
        failures: list[BaseException] = []
        for name, original in self._overlapped_originals.items():
            try:
                setattr(overlapped, name, original)
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError("cannot restore Windows overlapped I/O guard") from None
        self._overlapped_module = None
        self._overlapped_originals.clear()

    def _restore_socket_functions(self) -> None:
        if not self._originals:
            return
        socket.socket.bind = self._originals["bind"]
        socket.socket.connect = self._originals["connect"]
        socket.socket.connect_ex = self._originals["connect_ex"]
        socket.socket.sendto = self._originals["sendto"]
        socket.create_connection = self._originals["create_connection"]
        for function_name in _NAME_RESOLUTION_FUNCTIONS:
            setattr(socket, function_name, self._originals[function_name])
        self._originals.clear()

    def _restore_environment(self) -> None:
        for key, previous in self._previous_environment.items():
            if previous is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(previous)
        self._previous_environment.clear()
