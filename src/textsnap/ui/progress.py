"""Minimal OCR progress window."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ProgressWindow(QDialog):
    """Show only recognition progress and a cancellable action."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("TextSnap Layout")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, False)
        self._cancel_emitted = False
        self._silent_close = False

        self.status_label = QLabel("正在识别…", self)
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.clicked.connect(self._request_cancel)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addLayout(button_row)
        self.setLayout(layout)
        self.setFixedSize(self.sizeHint())

    def set_waiting_for_model(self) -> None:
        """Show that the captured selection is waiting for model startup."""

        self.status_label.setText("等待模型就绪…")
        self.setFixedSize(self.sizeHint())

    def set_recognizing(self) -> None:
        """Show that the resident model is processing the selection."""

        self.status_label.setText("正在识别…")
        self.setFixedSize(self.sizeHint())

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self._request_cancel()
            return
        super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        self._cancel_emitted = False
        self.cancel_button.setEnabled(True)
        super().showEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._silent_close:
            self._request_cancel()
        event.accept()

    def dismiss(self) -> None:
        """Close after controller completion without requesting cancellation."""

        self._silent_close = True
        try:
            self.close()
        finally:
            self._silent_close = False

    finish_close = dismiss

    def _request_cancel(self) -> None:
        if self._cancel_emitted:
            return
        self._cancel_emitted = True
        self.cancel_button.setEnabled(False)
        self.cancel_requested.emit()
