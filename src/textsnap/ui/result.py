"""Layout-preserving plain-text result window."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QCloseEvent, QCursor, QFont, QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ResultWindow(QWidget):
    """Present one unmodified layout result and release it when closed."""

    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("TextSnap Layout")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)

        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setUndoRedoEnabled(False)
        self.text_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text_edit.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        font = QFont("Noto Sans Mono CJK SC")
        font.setPointSize(12)
        self.text_edit.setFont(font)

        self.copy_all_button = QPushButton("复制全部", self)
        self.copy_all_button.clicked.connect(self.copy_all)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.copy_all_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.text_edit, 1)
        layout.addLayout(button_row)
        self.setLayout(layout)

        self._result_text: str | None = None
        self._target_work_area: QRect | None = None

    @property
    def result_text(self) -> str | None:
        return self._result_text

    def show_result(
        self,
        text: str,
        target_screen=None,  # QScreen is not available from QtCore for annotation.
    ) -> None:
        """Set the exact source text, size to 80% of the work area, and activate."""

        if not isinstance(text, str):
            raise TypeError("result text must be str")
        work_area = self._available_geometry(target_screen)
        self._target_work_area = QRect(work_area)
        self._result_text = text
        self.text_edit.setPlainText(text)
        self._place_in_work_area(work_area)
        self.show()
        self.raise_()
        self.activateWindow()

    def copy_all(self) -> None:
        """Copy the original layout string and close the result window."""

        if self._result_text is None:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._result_text)
            self.close()

    @staticmethod
    def _available_geometry(target_screen) -> QRect:
        if isinstance(target_screen, QRect):
            work_area = QRect(target_screen)
        elif target_screen is not None and hasattr(target_screen, "availableGeometry"):
            work_area = QRect(target_screen.availableGeometry())
        else:
            screen = QGuiApplication.screenAt(QCursor.pos())
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            if screen is None:
                raise RuntimeError("no target screen is available")
            work_area = QRect(screen.availableGeometry())
        if work_area.width() <= 0 or work_area.height() <= 0:
            raise ValueError("target screen work area must be non-empty")
        return work_area

    def _place_in_work_area(self, work_area: QRect) -> None:
        width = max(1, round(work_area.width() * 0.8))
        height = max(1, round(work_area.height() * 0.8))
        x = work_area.x() + (work_area.width() - width) // 2
        y = work_area.y() + (work_area.height() - height) // 2
        self.setGeometry(x, y, width, height)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.text_edit.clear()
        self._result_text = None
        self._target_work_area = None
        event.accept()
        self.closed.emit()
