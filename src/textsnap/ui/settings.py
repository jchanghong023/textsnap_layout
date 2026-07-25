"""Settings presentation widgets with no platform or persistence behavior."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from textsnap.settings import Hotkey


_MODIFIER_KEYS = frozenset(
    {
        Qt.Key.Key_Control,
        Qt.Key.Key_Alt,
        Qt.Key.Key_Shift,
        Qt.Key.Key_Meta,
    }
)
_NAMED_QT_KEYS = {
    Qt.Key.Key_Backspace: "Backspace",
    Qt.Key.Key_Delete: "Delete",
    Qt.Key.Key_Down: "Down",
    Qt.Key.Key_End: "End",
    Qt.Key.Key_Home: "Home",
    Qt.Key.Key_Insert: "Insert",
    Qt.Key.Key_Left: "Left",
    Qt.Key.Key_PageDown: "PageDown",
    Qt.Key.Key_PageUp: "PageUp",
    Qt.Key.Key_Return: "Return",
    Qt.Key.Key_Enter: "Return",
    Qt.Key.Key_Right: "Right",
    Qt.Key.Key_Space: "Space",
    Qt.Key.Key_Tab: "Tab",
    Qt.Key.Key_Up: "Up",
}


HotkeyValue = Hotkey


@dataclass(frozen=True, slots=True)
class SettingsDraft:
    """The complete editable settings boundary exposed by this window."""

    hotkey: HotkeyValue
    autostart: bool


class HotkeyRecorder(QLineEdit):
    """Read-only line edit that records a portable key combination."""

    hotkey_changed = Signal(object)

    def __init__(
        self,
        hotkey: HotkeyValue | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setClearButtonEnabled(False)
        self.setPlaceholderText("请按下快捷键")
        self._hotkey = HotkeyValue(("Ctrl", "Alt"), "O")
        self.set_hotkey(hotkey or self._hotkey)

    @property
    def hotkey(self) -> HotkeyValue:
        return self._hotkey

    def set_hotkey(self, hotkey: HotkeyValue) -> None:
        if not isinstance(hotkey, HotkeyValue):
            raise TypeError("hotkey must be HotkeyValue")
        self._hotkey = hotkey
        self.setText("+".join((*hotkey.modifiers, hotkey.key)))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in _MODIFIER_KEYS:
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            super().keyPressEvent(event)
            return

        key_name = self._portable_key_name(event)
        if not key_name:
            event.accept()
            return
        modifiers = event.modifiers()
        names = tuple(
            name
            for name, flag in (
                ("Ctrl", Qt.KeyboardModifier.ControlModifier),
                ("Alt", Qt.KeyboardModifier.AltModifier),
                ("Shift", Qt.KeyboardModifier.ShiftModifier),
                ("Win", Qt.KeyboardModifier.MetaModifier),
            )
            if modifiers & flag
        )
        try:
            hotkey = HotkeyValue(names, key_name)
        except ValueError:
            event.accept()
            return
        self.set_hotkey(hotkey)
        self.hotkey_changed.emit(hotkey)
        event.accept()

    @staticmethod
    def _portable_key_name(event: QKeyEvent) -> str:
        key = event.key()
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(ord("A") + key - Qt.Key.Key_A)
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            return chr(ord("0") + key - Qt.Key.Key_0)
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            return f"F{key - Qt.Key.Key_F1 + 1}"
        if key in _NAMED_QT_KEYS:
            return _NAMED_QT_KEYS[key]
        text = QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)
        if not text or text in {"+", ","} or "+" in text:
            return ""
        return text


class SettingsWindow(QDialog):
    """Expose only the four settings surfaces fixed by the product plan."""

    save_requested = Signal(object)
    cancelled = Signal()
    retry_model_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._close_reason: str | None = None
        self._saved_draft = SettingsDraft(
            hotkey=HotkeyValue(("Ctrl", "Alt"), "O"),
            autostart=False,
        )

        self.hotkey_recorder = HotkeyRecorder(parent=self)
        self.autostart_checkbox = QCheckBox("登录 Windows 后自动启动", self)
        self.hotkey_recorder.hotkey_changed.connect(self.clear_error)
        self.autostart_checkbox.toggled.connect(self.clear_error)

        self.model_status_label = QLabel("正在加载…", self)
        self.model_status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction
        )
        self.retry_model_button = QPushButton("重试加载", self)
        self.retry_model_button.clicked.connect(self.retry_model_requested.emit)

        model_row = QWidget(self)
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.addWidget(self.model_status_label, 1)
        model_layout.addWidget(self.retry_model_button)
        self.retry_model_button.setEnabled(False)

        form = QFormLayout()
        form.addRow("快捷键", self.hotkey_recorder)
        form.addRow("开机启动", self.autostart_checkbox)
        form.addRow("模型状态", model_row)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("settingsError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.hide()

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        save_button = self.button_box.button(QDialogButtonBox.StandardButton.Save)
        cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
        save_button.setText("保存")
        cancel_button.setText("取消")
        self.button_box.accepted.connect(self._request_save)
        self.button_box.rejected.connect(self._request_cancel)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(self.button_box)
        self.setLayout(layout)

    @property
    def draft(self) -> SettingsDraft:
        return SettingsDraft(
            hotkey=self.hotkey_recorder.hotkey,
            autostart=self.autostart_checkbox.isChecked(),
        )

    def set_settings(
        self,
        hotkey: HotkeyValue,
        autostart: bool,
    ) -> None:
        if not isinstance(autostart, bool):
            raise TypeError("autostart must be bool")
        self._saved_draft = SettingsDraft(hotkey=hotkey, autostart=autostart)
        self.restore_saved_settings()
        self.clear_error()

    def restore_saved_settings(self) -> None:
        """Restore controls to the last controller-confirmed values."""

        self.hotkey_recorder.set_hotkey(self._saved_draft.hotkey)
        old_blocked = self.autostart_checkbox.blockSignals(True)
        try:
            self.autostart_checkbox.setChecked(self._saved_draft.autostart)
        finally:
            self.autostart_checkbox.blockSignals(old_blocked)

    def set_error(self, message: str) -> None:
        """Show a controller-sanitized, single-line save or registration error."""

        if (
            not isinstance(message, str)
            or not message
            or len(message) > 240
            or any(character in message for character in ("\r", "\n"))
            or any(ord(character) < 32 for character in message)
        ):
            raise ValueError("settings error must be a sanitized single line")
        self.error_label.setText(message)
        self.error_label.show()

    def clear_error(self, *_unused: object) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def accept_saved(self, saved: SettingsDraft | None = None) -> None:
        """Close only after the controller has committed every requested change."""

        confirmed = self.draft if saved is None else saved
        if not isinstance(confirmed, SettingsDraft):
            raise TypeError("saved must be SettingsDraft")
        self._saved_draft = confirmed
        self.clear_error()
        self._close_reason = "save"
        self.close()

    close_after_save = accept_saved

    def set_model_status(self, status: object) -> None:
        """Set a sanitized display label and whether retry is currently useful."""

        raw = getattr(status, "value", status)
        normalized = str(raw).lower()
        if normalized == "loading":
            text, retry_enabled = "正在加载…", False
        elif normalized == "ready":
            text, retry_enabled = "已就绪", False
        elif normalized == "error":
            text, retry_enabled = "加载失败", True
        else:
            raise ValueError("model status must be loading, ready, or error")
        self.model_status_label.setText(text)
        self.retry_model_button.setEnabled(retry_enabled)

    def _request_save(self) -> None:
        self.clear_error()
        self.save_requested.emit(self.draft)

    def _request_cancel(self) -> None:
        self._close_reason = "cancel"
        self.restore_saved_settings()
        self.clear_error()
        self.cancelled.emit()
        self.close()

    def reject(self) -> None:
        """Route Escape through the same restoring cancel transaction."""

        self._request_cancel()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._close_reason is None:
            self.restore_saved_settings()
            self.clear_error()
            self.cancelled.emit()
        self._close_reason = None
        event.accept()
