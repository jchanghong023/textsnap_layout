"""Current-user HKCU Run integration scoped to TextSnap Layout's own value."""

from __future__ import annotations

import os
from typing import Final, Protocol


RUN_KEY_PATH: Final = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME: Final = "TextSnapLayout"
AUTOSTART_ARGUMENT: Final = "--autostart"


class RegistryApi(Protocol):
    def read_value(self, value_name: str) -> str | None: ...

    def write_value(self, value_name: str, value: str) -> None: ...

    def delete_value(self, value_name: str) -> None: ...


class AutostartError(RuntimeError):
    """A registry failure whose text never includes executable/user paths."""

    def __init__(
        self,
        diagnostic_code: str,
        winerror: int | None = None,
    ) -> None:
        self.diagnostic_code = diagnostic_code
        self.winerror = winerror
        suffix = f" (Win32 error {winerror})" if winerror else ""
        super().__init__(f"无法更新开机启动设置。 [{diagnostic_code}]{suffix}")


class WinregRegistryApi:
    """winreg adapter for one fixed HKCU Run key."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise AutostartError("AUTOSTART-UNSUPPORTED")
        import winreg

        self._winreg = winreg

    def read_value(self, value_name: str) -> str | None:
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                self._winreg.KEY_QUERY_VALUE,
            ) as key:
                value, value_type = self._winreg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            return None
        if value_type not in (self._winreg.REG_SZ, self._winreg.REG_EXPAND_SZ):
            return None
        return str(value)

    def write_value(self, value_name: str, value: str) -> None:
        with self._winreg.CreateKeyEx(
            self._winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            self._winreg.KEY_SET_VALUE,
        ) as key:
            self._winreg.SetValueEx(
                key,
                value_name,
                0,
                self._winreg.REG_SZ,
                value,
            )

    def delete_value(self, value_name: str) -> None:
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                self._winreg.KEY_SET_VALUE,
            ) as key:
                self._winreg.DeleteValue(key, value_name)
        except FileNotFoundError:
            return


class AutostartService:
    """Read and update only the application's own current-user Run value."""

    def __init__(
        self,
        executable: os.PathLike[str] | str,
        api: RegistryApi | None = None,
    ) -> None:
        self._command = build_autostart_command(executable)
        try:
            self._api = api if api is not None else WinregRegistryApi()
        except AutostartError:
            raise
        except Exception:
            raise AutostartError("AUTOSTART-API-INIT") from None

    @property
    def expected_command(self) -> str:
        return self._command

    def registered_command(self) -> str | None:
        try:
            return self._api.read_value(RUN_VALUE_NAME)
        except Exception as exc:
            raise AutostartError(
                "AUTOSTART-READ",
                _exception_winerror(exc),
            ) from None

    def is_enabled(self) -> bool:
        return self.registered_command() == self._command

    def enable(self) -> None:
        """Write the current executable command, replacing a moved stale path."""

        try:
            self._api.write_value(RUN_VALUE_NAME, self._command)
        except Exception as exc:
            raise AutostartError(
                "AUTOSTART-WRITE",
                _exception_winerror(exc),
            ) from None

    def disable(self) -> None:
        """Delete only TextSnap Layout's own HKCU Run value."""

        try:
            self._api.delete_value(RUN_VALUE_NAME)
        except Exception as exc:
            raise AutostartError(
                "AUTOSTART-DELETE",
                _exception_winerror(exc),
            ) from None

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        if enabled:
            self.enable()
        else:
            self.disable()

    def update_if_registered(self) -> bool:
        """Refresh a stale path after the portable directory has been moved."""

        current = self.registered_command()
        if current is None:
            return False
        if current != self._command:
            self.enable()
        return True

    def restore_registered_command(self, command: str | None) -> None:
        """Restore an exact pre-transaction snapshot of this application's value."""

        if command is not None and not isinstance(command, str):
            raise TypeError("autostart snapshot must be text or None")
        try:
            if command is None:
                self._api.delete_value(RUN_VALUE_NAME)
            else:
                self._api.write_value(RUN_VALUE_NAME, command)
        except Exception as exc:
            raise AutostartError(
                "AUTOSTART-RESTORE",
                _exception_winerror(exc),
            ) from None


def build_autostart_command(executable: os.PathLike[str] | str) -> str:
    path = os.fspath(executable)
    if not isinstance(path, str):
        raise TypeError("executable path must be text")
    if not path or any(character in path for character in ('"', "\0", "\r", "\n")):
        raise ValueError("executable path cannot be represented safely")
    return f'"{path}" {AUTOSTART_ARGUMENT}'


def _exception_winerror(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    return int(value) if isinstance(value, int) and value else None
