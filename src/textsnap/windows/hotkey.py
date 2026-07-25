"""RegisterHotKey service with atomic replacement and strict key mapping."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from typing import Callable, Final, Protocol


MOD_ALT: Final = 0x0001
MOD_CONTROL: Final = 0x0002
MOD_SHIFT: Final = 0x0004
MOD_WIN: Final = 0x0008
MOD_NOREPEAT: Final = 0x4000
WM_HOTKEY: Final = 0x0312

PRIMARY_HOTKEY_ID: Final = 0x5453
SECONDARY_HOTKEY_ID: Final = 0x5454

MODIFIER_VIRTUAL_KEYS: Final = {
    "Alt": MOD_ALT,
    "Ctrl": MOD_CONTROL,
    "Shift": MOD_SHIFT,
    "Win": MOD_WIN,
}

_NAMED_VIRTUAL_KEYS: Final = {
    "Backspace": 0x08,
    "Tab": 0x09,
    "Return": 0x0D,
    "Escape": 0x1B,
    "Space": 0x20,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "End": 0x23,
    "Home": 0x24,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "Insert": 0x2D,
    "Delete": 0x2E,
}


class HotkeyApi(Protocol):
    def register_hot_key(
        self,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> bool: ...

    def unregister_hot_key(self, hotkey_id: int) -> bool: ...

    def get_last_error(self) -> int: ...


@dataclass(frozen=True, slots=True)
class HotkeyBinding:
    modifiers: tuple[str, ...]
    key: str

    def __post_init__(self) -> None:
        if not self.modifiers:
            raise ValueError("at least one modifier is required")
        if len(set(self.modifiers)) != len(self.modifiers):
            raise ValueError("hotkey modifiers must not contain duplicates")
        unknown = [
            modifier
            for modifier in self.modifiers
            if modifier not in MODIFIER_VIRTUAL_KEYS
        ]
        if unknown:
            raise ValueError("unsupported hotkey modifier")
        virtual_key_for_name(self.key)

    @property
    def modifier_flags(self) -> int:
        flags = MOD_NOREPEAT
        for modifier in self.modifiers:
            flags |= MODIFIER_VIRTUAL_KEYS[modifier]
        return flags

    @property
    def virtual_key(self) -> int:
        return virtual_key_for_name(self.key)


class HotkeyRegistrationError(RuntimeError):
    """A sanitized registration or cleanup failure."""

    def __init__(
        self,
        diagnostic_code: str,
        winerror: int | None = None,
    ) -> None:
        self.diagnostic_code = diagnostic_code
        self.winerror = winerror
        suffix = f" (Win32 error {winerror})" if winerror else ""
        super().__init__(f"全局快捷键不可用。 [{diagnostic_code}]{suffix}")


class CtypesHotkeyApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise HotkeyRegistrationError("HOTKEY-UNSUPPORTED")

        from ctypes import wintypes

        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._register = self._user32.RegisterHotKey
        self._register.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._register.restype = wintypes.BOOL
        self._unregister = self._user32.UnregisterHotKey
        self._unregister.argtypes = [wintypes.HWND, ctypes.c_int]
        self._unregister.restype = wintypes.BOOL

    def register_hot_key(
        self,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> bool:
        self._ctypes.set_last_error(0)
        return bool(self._register(None, hotkey_id, modifiers, virtual_key))

    def unregister_hot_key(self, hotkey_id: int) -> bool:
        self._ctypes.set_last_error(0)
        return bool(self._unregister(None, hotkey_id))

    def get_last_error(self) -> int:
        return int(self._ctypes.get_last_error())


class HotkeyService:
    """Own one active global hotkey while replacing it without a gap."""

    def __init__(self, api: HotkeyApi | None = None) -> None:
        try:
            self._api = api if api is not None else CtypesHotkeyApi()
        except HotkeyRegistrationError:
            raise
        except Exception:
            raise HotkeyRegistrationError("HOTKEY-API-INIT") from None
        self._registrations: dict[int, HotkeyBinding] = {}
        self._active_id: int | None = None

    @property
    def active_binding(self) -> HotkeyBinding | None:
        if self._active_id is None:
            return None
        return self._registrations[self._active_id]

    @property
    def active_hotkey_id(self) -> int | None:
        return self._active_id

    def register(self, binding: HotkeyBinding) -> bool:
        """Register or atomically replace the active binding.

        Returns ``True`` when the OS registration changed and ``False`` for an
        already-active binding. A failed replacement leaves the old binding
        active.
        """

        if not isinstance(binding, HotkeyBinding):
            raise TypeError("binding must be HotkeyBinding")
        if self.active_binding == binding:
            return False

        new_id = self._available_id()
        if not self._register_os(new_id, binding):
            raise HotkeyRegistrationError(
                "HOTKEY-CONFLICT",
                self._safe_last_error(),
            )
        self._registrations[new_id] = binding

        old_id = self._active_id
        if old_id is not None:
            old_removed, old_error = self._attempt_unregister_os(old_id)
        else:
            old_removed, old_error = True, None
        if not old_removed:
            # Roll back the just-created registration. The old registration and
            # persisted setting remain authoritative even if cleanup also fails.
            rollback_removed, _ = self._attempt_unregister_os(new_id)
            if rollback_removed:
                del self._registrations[new_id]
                code = "HOTKEY-OLD-UNREGISTER"
            else:
                code = "HOTKEY-ROLLBACK-CLEANUP"
            raise HotkeyRegistrationError(code, old_error)

        if old_id is not None:
            del self._registrations[old_id]
        self._active_id = new_id
        return True

    def unregister(self) -> None:
        """Unregister every handle owned by this service, attempting all of them."""

        failed = False
        failure_error: int | None = None
        for hotkey_id in tuple(self._registrations):
            removed, error = self._attempt_unregister_os(hotkey_id)
            if removed:
                del self._registrations[hotkey_id]
                if self._active_id == hotkey_id:
                    self._active_id = None
            else:
                failed = True
                failure_error = failure_error or error
        if failed:
            raise HotkeyRegistrationError(
                "HOTKEY-UNREGISTER",
                failure_error,
            )

    close = unregister

    def __enter__(self) -> HotkeyService:
        return self

    def __exit__(self, *_: object) -> None:
        self.unregister()

    def _available_id(self) -> int:
        candidates = (PRIMARY_HOTKEY_ID, SECONDARY_HOTKEY_ID)
        for candidate in candidates:
            if candidate not in self._registrations:
                return candidate
        raise HotkeyRegistrationError("HOTKEY-ID-EXHAUSTED")

    def _register_os(self, hotkey_id: int, binding: HotkeyBinding) -> bool:
        try:
            return bool(
                self._api.register_hot_key(
                    hotkey_id,
                    binding.modifier_flags,
                    binding.virtual_key,
                )
            )
        except Exception:
            raise HotkeyRegistrationError("HOTKEY-REGISTER-API") from None

    def _attempt_unregister_os(
        self,
        hotkey_id: int,
    ) -> tuple[bool, int | None]:
        try:
            removed = bool(self._api.unregister_hot_key(hotkey_id))
        except Exception:
            return False, None
        return removed, None if removed else self._safe_last_error()

    def _safe_last_error(self) -> int | None:
        try:
            error = int(self._api.get_last_error())
        except Exception:
            return None
        return error or None


def virtual_key_for_name(key: str) -> int:
    """Map one canonical key name to its Win32 virtual-key code."""

    if not isinstance(key, str):
        raise TypeError("hotkey key must be str")
    if len(key) == 1 and ("A" <= key <= "Z" or "0" <= key <= "9"):
        return ord(key)
    if key.startswith("F") and key[1:].isdigit():
        function_number = int(key[1:])
        if 1 <= function_number <= 24 and key == f"F{function_number}":
            return 0x70 + function_number - 1
    try:
        return _NAMED_VIRTUAL_KEYS[key]
    except KeyError:
        raise ValueError("unsupported hotkey key") from None


def decode_hotkey_message(
    message_code: int,
    wparam: int,
    expected_hotkey_id: int | None,
) -> bool:
    """Return whether a Win32 message is the service's active WM_HOTKEY."""

    return (
        expected_hotkey_id is not None
        and int(message_code) == WM_HOTKEY
        and int(wparam) == expected_hotkey_id
    )


def create_qt_native_event_filter(
    service: HotkeyService,
    callback: Callable[[], None],
):
    """Create the optional PySide6 native-event adapter on Windows.

    Importing this module does not import PySide6. The adapter is created only
    by the GUI layer after QApplication exists.
    """

    if os.name != "nt":
        raise HotkeyRegistrationError("HOTKEY-NATIVE-EVENT-UNSUPPORTED")
    try:
        from ctypes import wintypes
        from PySide6.QtCore import QAbstractNativeEventFilter
    except Exception:
        raise HotkeyRegistrationError("HOTKEY-QT-UNAVAILABLE") from None

    class _HotkeyNativeEventFilter(QAbstractNativeEventFilter):
        def nativeEventFilter(self, event_type, message):
            event_name = bytes(event_type)
            if event_name not in (
                b"windows_generic_MSG",
                b"windows_dispatcher_MSG",
            ):
                return False, 0
            try:
                native_message = ctypes.cast(
                    int(message),
                    ctypes.POINTER(wintypes.MSG),
                ).contents
            except Exception:
                return False, 0
            if decode_hotkey_message(
                int(native_message.message),
                int(native_message.wParam),
                service.active_hotkey_id,
            ):
                callback()
                return True, 0
            return False, 0

    return _HotkeyNativeEventFilter()
