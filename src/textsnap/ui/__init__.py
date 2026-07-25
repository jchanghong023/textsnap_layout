"""Qt Widgets used by the TextSnap Layout application.

This package intentionally contains presentation code only.  Registering hotkeys,
capturing screens, changing autostart state, and running OCR belong to the
controller and platform layers.
"""

from .error import ErrorDialog
from .progress import ProgressWindow
from .result import ResultWindow
from .selection import SelectionOverlay
from .settings import HotkeyRecorder, HotkeyValue, SettingsDraft, SettingsWindow
from .tray import TrayIcon, TrayUi

__all__ = [
    "ErrorDialog",
    "HotkeyRecorder",
    "HotkeyValue",
    "ProgressWindow",
    "ResultWindow",
    "SelectionOverlay",
    "SettingsDraft",
    "SettingsWindow",
    "TrayIcon",
    "TrayUi",
]
