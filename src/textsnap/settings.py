"""Versioned settings loading and recoverable atomic persistence."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
from typing import Final

SETTINGS_SCHEMA_VERSION: Final = 1
MAX_SETTINGS_BYTES: Final = 64 * 1024
_MODIFIER_ORDER: Final = ("Ctrl", "Alt", "Shift", "Win")
_NAMED_KEYS: Final = frozenset(
    {
        "Backspace",
        "Delete",
        "Down",
        "End",
        "Escape",
        "Home",
        "Insert",
        "Left",
        "PageDown",
        "PageUp",
        "Return",
        "Right",
        "Space",
        "Tab",
        "Up",
    }
    | {f"F{number}" for number in range(1, 25)}
)


class SettingsSaveError(RuntimeError):
    """A sanitized persistence failure safe to show in the UI."""


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate settings key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class Hotkey:
    modifiers: tuple[str, ...] = ("Ctrl", "Alt")
    key: str = "O"

    def __post_init__(self) -> None:
        if not isinstance(self.modifiers, tuple):
            object.__setattr__(self, "modifiers", tuple(self.modifiers))
        if not self.modifiers:
            raise ValueError("hotkey must include at least one modifier")
        if len(set(self.modifiers)) != len(self.modifiers):
            raise ValueError("hotkey modifiers must be unique")
        if any(modifier not in _MODIFIER_ORDER for modifier in self.modifiers):
            raise ValueError("unsupported hotkey modifier")
        canonical = tuple(
            modifier for modifier in _MODIFIER_ORDER if modifier in self.modifiers
        )
        object.__setattr__(self, "modifiers", canonical)
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("hotkey key must be a non-empty string")
        key = self.key
        if len(key) == 1 and key.isascii() and key.isalnum():
            key = key.upper()
        elif key not in _NAMED_KEYS:
            raise ValueError("unsupported hotkey key")
        object.__setattr__(self, "key", key)


@dataclass(frozen=True, slots=True)
class Settings:
    schema_version: int = SETTINGS_SCHEMA_VERSION
    hotkey: Hotkey = Hotkey()
    autostart: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != SETTINGS_SCHEMA_VERSION
        ):
            raise ValueError("unsupported settings schema")
        if not isinstance(self.hotkey, Hotkey):
            raise TypeError("hotkey must be Hotkey")
        if not isinstance(self.autostart, bool):
            raise TypeError("autostart must be bool")


DEFAULT_SETTINGS: Final = Settings()


@dataclass(frozen=True, slots=True)
class SettingsIssue:
    code: str
    public_message: str


@dataclass(frozen=True, slots=True)
class SettingsLoadResult:
    settings: Settings
    issue: SettingsIssue | None
    file_existed: bool


def _decode_settings(payload: object) -> Settings:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "hotkey",
        "autostart",
    }:
        raise ValueError("invalid settings object")
    hotkey_payload = payload["hotkey"]
    if not isinstance(hotkey_payload, dict) or set(hotkey_payload) != {
        "modifiers",
        "key",
    }:
        raise ValueError("invalid hotkey object")
    modifiers = hotkey_payload["modifiers"]
    if not isinstance(modifiers, list) or not all(
        isinstance(value, str) for value in modifiers
    ):
        raise ValueError("invalid hotkey modifiers")
    return Settings(
        schema_version=payload["schema_version"],
        hotkey=Hotkey(tuple(modifiers), hotkey_payload["key"]),
        autostart=payload["autostart"],
    )


def load_settings(path: Path | str) -> SettingsLoadResult:
    """Load settings without ever repairing or overwriting a damaged file."""

    settings_path = Path(path)
    try:
        with settings_path.open("rb") as source:
            raw = source.read(MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        return SettingsLoadResult(DEFAULT_SETTINGS, None, False)
    except OSError:
        return SettingsLoadResult(
            DEFAULT_SETTINGS,
            SettingsIssue(
                "settings-read-failed",
                "无法读取设置，已在内存中使用默认值。",
            ),
            settings_path.exists(),
        )

    try:
        if len(raw) > MAX_SETTINGS_BYTES:
            raise ValueError("settings file is too large")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        settings = _decode_settings(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return SettingsLoadResult(
            DEFAULT_SETTINGS,
            SettingsIssue(
                "settings-invalid",
                "设置文件已损坏，已在内存中使用默认值；保存前不会覆盖原文件。",
            ),
            True,
        )
    return SettingsLoadResult(settings, None, True)


def settings_bytes(settings: Settings) -> bytes:
    payload = {
        "schema_version": settings.schema_version,
        "hotkey": {
            "modifiers": list(settings.hotkey.modifiers),
            "key": settings.hotkey.key,
        },
        "autostart": settings.autostart,
    }
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def save_settings(path: Path | str, settings: Settings) -> None:
    """Atomically replace settings; the old file survives every pre-replace error."""

    settings_path = Path(path)
    parent = settings_path.parent
    if not parent.is_dir():
        raise SettingsSaveError("设置目录不存在。")

    temporary = parent / (
        f".{settings_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor: int | None = None
    replaced = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        payload = settings_bytes(settings)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short settings write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, settings_path)
        replaced = True
    except (OSError, ValueError):
        raise SettingsSaveError("无法保存设置；原设置文件保持不变。") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def check_data_directory_writable(data_directory: Path | str) -> bool:
    """Probe real create/write/delete access without leaving a persistent file."""

    directory = Path(data_directory)
    if not directory.is_dir():
        return False
    probe = directory / f".textsnap-write-probe-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(probe, flags, 0o600)
        os.write(descriptor, b"ok")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        probe.unlink()
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
