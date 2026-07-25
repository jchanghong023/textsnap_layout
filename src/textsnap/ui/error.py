"""Content-free error dialog with an explicit diagnostic-copy action."""

from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ErrorDialog(QDialog):
    """Display a short error while retaining only sanitized diagnostics."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("TextSnap Layout")
        self.setModal(False)

        self.message_label = QLabel("", self)
        self.message_label.setWordWrap(True)
        self.copy_diagnostic_button = QPushButton("复制诊断信息", self)
        self.copy_diagnostic_button.setEnabled(False)
        self.copy_diagnostic_button.clicked.connect(self.copy_diagnostic)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        self.button_box.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.message_label)
        layout.addWidget(self.copy_diagnostic_button)
        layout.addWidget(self.button_box)
        self.setLayout(layout)

        self._diagnostic_text: str | None = None

    @property
    def diagnostic_text(self) -> str | None:
        return self._diagnostic_text

    def show_error(self, public_message: str, diagnostic_text: str) -> None:
        if (
            not isinstance(public_message, str)
            or not public_message
            or len(public_message) > 240
            or any(ord(character) < 32 for character in public_message)
        ):
            raise ValueError("public error message must be a sanitized single line")
        if (
            not isinstance(diagnostic_text, str)
            or not diagnostic_text
            or len(diagnostic_text) > 8192
            or "\0" in diagnostic_text
        ):
            raise ValueError("diagnostic text is invalid")

        self.message_label.setText(public_message)
        self._diagnostic_text = diagnostic_text
        self.copy_diagnostic_button.setEnabled(True)
        self.adjustSize()
        self.show()
        self.raise_()
        self.activateWindow()

    def copy_diagnostic(self) -> None:
        if self._diagnostic_text is None:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._diagnostic_text)

    def reject(self) -> None:
        """Make Escape follow the close path that releases diagnostics."""

        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.message_label.clear()
        self._diagnostic_text = None
        self.copy_diagnostic_button.setEnabled(False)
        event.accept()
