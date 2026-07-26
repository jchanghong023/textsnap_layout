"""System tray presentation and action signals."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayUi(QSystemTrayIcon):
    """Own tray actions while leaving all effects to the controller."""

    capture_requested = Signal()
    settings_requested = Signal()
    autostart_toggled = Signal(bool)
    exit_requested = Signal()

    def __init__(
        self,
        icon: QIcon | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(icon or QIcon(), parent)
        self.setToolTip("TextSnap Layout")
        self._startup_notification_shown = False

        self.menu = QMenu()
        self.capture_action = QAction("截图识别", self)
        self.settings_action = QAction("设置", self)
        self.autostart_action = QAction("开机启动", self)
        self.autostart_action.setCheckable(True)
        self.exit_action = QAction("退出", self)

        self.menu.addAction(self.capture_action)
        self.menu.addAction(self.settings_action)
        self.menu.addSeparator()
        self.menu.addAction(self.autostart_action)
        self.menu.addSeparator()
        self.menu.addAction(self.exit_action)
        self.setContextMenu(self.menu)

        self.capture_action.triggered.connect(self.capture_requested.emit)
        self.settings_action.triggered.connect(self.settings_requested.emit)
        self.autostart_action.toggled.connect(self.autostart_toggled.emit)
        self.exit_action.triggered.connect(self.exit_requested.emit)

    @property
    def startup_notification_shown(self) -> bool:
        return self._startup_notification_shown

    def set_autostart_checked(self, checked: bool) -> None:
        """Reflect controller state without requesting a registry change."""

        old_blocked = self.autostart_action.blockSignals(True)
        try:
            self.autostart_action.setChecked(bool(checked))
        finally:
            self.autostart_action.blockSignals(old_blocked)

    def hide_context_menu(self) -> None:
        """Synchronously dismiss the application-owned popup before capture."""

        self.menu.hide()

    def show_startup_notification(
        self,
        hotkey_text: str = "Ctrl+Alt+O",
        *,
        enabled: bool = True,
    ) -> bool:
        """Show the startup message at most once for this application process."""

        if not enabled or self._startup_notification_shown:
            return False
        if not hotkey_text or "\n" in hotkey_text or "\r" in hotkey_text:
            raise ValueError("hotkey text must be a non-empty single line")
        self._startup_notification_shown = True
        self.showMessage(
            "TextSnap Layout",
            f"已启动，按 {hotkey_text} 截图",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )
        return True

    notify_started = show_startup_notification


TrayIcon = TrayUi
