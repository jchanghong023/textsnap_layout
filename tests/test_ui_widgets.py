"""Real offscreen Qt tests without pytest-qt."""

from __future__ import annotations

from dataclasses import fields
from importlib.util import find_spec
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if find_spec("PySide6") is None:
    _PYSIDE_AVAILABLE = False
    _PYSIDE_SKIP_REASON = "PySide6 6.11.1 runtime is not installed"
else:
    from PySide6.QtCore import QPoint, QRect, Qt
    from PySide6.QtGui import QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
        QPlainTextEdit,
        QPushButton,
    )
    _PYSIDE_AVAILABLE = True
    _PYSIDE_SKIP_REASON = ""

from textsnap.domain import CaptureFrame

if _PYSIDE_AVAILABLE:
    from textsnap.ui import (
        ErrorDialog,
        HotkeyValue,
        ProgressWindow,
        ResultWindow,
        SelectionOverlay,
        SettingsDraft,
        SettingsWindow,
        TrayUi,
    )
    from textsnap.ui.selection import _screen_geometry_for_monitor_handle
else:
    # These names are only needed so unittest can collect and skip the classes
    # on hosts where Qt is deliberately absent.
    ErrorDialog = HotkeyValue = ProgressWindow = ResultWindow = object
    SelectionOverlay = object
    SettingsDraft = SettingsWindow = TrayUi = object


@unittest.skipUnless(_PYSIDE_AVAILABLE, _PYSIDE_SKIP_REASON)
class QtWidgetTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        for widget in QApplication.topLevelWidgets():
            widget.close()
        self.app.processEvents()


class SelectionOverlayTests(QtWidgetTestCase):
    @staticmethod
    def _frame(
        width: int = 200,
        height: int = 100,
        monitor_handle: int | None = None,
    ) -> CaptureFrame:
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        return CaptureFrame(
            pixels=image,
            width=width,
            height=height,
            monitor_id=r"\\.\DISPLAY2",
            origin_x=100,
            origin_y=-50,
            dpi_x=192,
            dpi_y=192,
            monitor_handle=monitor_handle,
        )

    def test_native_handle_selects_exact_mixed_dpi_geometry_ignoring_names(
        self,
    ) -> None:
        class FakeScreen:
            def __init__(self, name: str, geometry: QRect) -> None:
                self.name = name
                self._geometry = QRect(geometry)

            def geometry(self) -> QRect:
                return QRect(self._geometry)

        primary = FakeScreen(
            "Qt panel name, not a Win32 device",
            QRect(0, 0, 1920, 1080),
        )
        high_dpi = FakeScreen(
            "Another unrelated Qt panel name",
            QRect(1920, -240, 1280, 720),
        )
        handles = {primary: 0x1111, high_dpi: 0x2222}

        geometry = _screen_geometry_for_monitor_handle(
            0x2222,
            (primary, high_dpi),
            handles.get,
        )

        self.assertEqual(geometry, QRect(1920, -240, 1280, 720))

    def test_stale_native_monitor_handle_fails_closed(self) -> None:
        with patch.object(
            SelectionOverlay,
            "_matching_qt_screen_geometry",
            return_value=None,
        ) as resolver:
            with self.assertRaisesRegex(
                RuntimeError,
                "captured monitor is no longer available",
            ):
                SelectionOverlay(self._frame(monitor_handle=0x9999))

        resolver.assert_called_once_with(0x9999)

    def test_reverse_drag_maps_logical_to_local_and_global_physical_pixels(
        self,
    ) -> None:
        overlay = SelectionOverlay(
            self._frame(),
            logical_geometry=QRect(0, 0, 100, 50),
        )
        local: list[QRect] = []
        global_: list[QRect] = []
        overlay.selection_submitted.connect(lambda rect: local.append(QRect(rect)))
        overlay.global_selection_submitted.connect(
            lambda rect: global_.append(QRect(rect))
        )
        overlay.show()
        self.app.processEvents()
        self.assertEqual(
            overlay.map_widget_point_to_physical(QPoint(99, 49)),
            QPoint(200, 100),
        )

        QTest.mousePress(
            overlay,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(50, 30),
        )
        QTest.mouseRelease(
            overlay,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(10, 10),
        )
        self.app.processEvents()

        self.assertEqual(local, [QRect(20, 20, 80, 40)])
        self.assertEqual(global_, [QRect(120, -30, 80, 40)])
        self.assertEqual(overlay.last_local_selection, local[0])
        self.assertEqual(overlay.last_global_selection, global_[0])
        self.assertFalse(overlay.has_frozen_image)

    def test_drag_to_final_widget_pixel_reaches_physical_boundary(self) -> None:
        overlay = SelectionOverlay(
            self._frame(),
            logical_geometry=QRect(0, 0, 100, 50),
        )
        submitted: list[QRect] = []
        overlay.selection_submitted.connect(lambda rect: submitted.append(QRect(rect)))
        overlay.show()
        self.app.processEvents()

        QTest.mousePress(
            overlay,
            Qt.MouseButton.LeftButton,
            pos=QPoint(1, 1),
        )
        QTest.mouseRelease(
            overlay,
            Qt.MouseButton.LeftButton,
            pos=QPoint(99, 49),
        )
        self.app.processEvents()

        self.assertEqual(submitted, [QRect(2, 2, 198, 98)])

    def test_packed_gdi_bgra_frame_is_owned_as_a_frozen_qimage(self) -> None:
        pixels = bytearray([10, 20, 30, 0] * 4)
        frame = CaptureFrame(
            pixels=pixels,
            width=2,
            height=2,
            monitor_id=r"\\.\DISPLAY1",
            origin_x=0,
            origin_y=0,
            dpi_x=96,
            dpi_y=96,
        )
        overlay = SelectionOverlay(frame)
        pixels[:] = b"\0" * len(pixels)

        frozen = overlay.frozen_image_copy()
        self.assertEqual(frozen.pixelColor(0, 0).getRgb(), (30, 20, 10, 255))
        overlay.close()

    def test_exactly_eight_by_eight_is_submitted(self) -> None:
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        overlay = SelectionOverlay(
            image,
            logical_geometry=QRect(0, 0, 32, 32),
        )
        submitted: list[QRect] = []
        cancelled: list[bool] = []
        overlay.selection_submitted.connect(lambda rect: submitted.append(QRect(rect)))
        overlay.cancelled.connect(lambda: cancelled.append(True))
        overlay.show()
        self.app.processEvents()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(3, 4))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(11, 12))
        self.app.processEvents()

        self.assertEqual(submitted, [QRect(3, 4, 8, 8)])
        self.assertEqual(cancelled, [])

    def test_any_dimension_below_eight_cancels_and_releases_image(self) -> None:
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        overlay = SelectionOverlay(
            image,
            logical_geometry=QRect(0, 0, 32, 32),
        )
        submitted: list[QRect] = []
        cancelled: list[bool] = []
        overlay.selection_submitted.connect(lambda rect: submitted.append(QRect(rect)))
        overlay.cancelled.connect(lambda: cancelled.append(True))
        overlay.show()
        self.app.processEvents()

        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(9, 20))
        self.app.processEvents()

        self.assertEqual(submitted, [])
        self.assertEqual(cancelled, [True])
        self.assertFalse(overlay.has_frozen_image)

    def test_escape_and_right_click_cancel(self) -> None:
        for use_escape in (True, False):
            with self.subTest(use_escape=use_escape):
                image = QImage(32, 32, QImage.Format.Format_RGB32)
                overlay = SelectionOverlay(
                    image,
                    logical_geometry=QRect(0, 0, 32, 32),
                )
                cancelled: list[bool] = []
                overlay.cancelled.connect(lambda: cancelled.append(True))
                overlay.show()
                self.app.processEvents()
                if use_escape:
                    QTest.keyClick(overlay, Qt.Key.Key_Escape)
                else:
                    QTest.mouseClick(
                        overlay,
                        Qt.MouseButton.RightButton,
                        pos=QPoint(5, 5),
                    )
                self.app.processEvents()
                self.assertEqual(cancelled, [True])


class ProgressWindowTests(QtWidgetTestCase):
    def test_waiting_and_recognizing_status_are_explicit(self) -> None:
        window = ProgressWindow()
        window.set_waiting_for_model()
        self.assertEqual(window.status_label.text(), "等待模型就绪…")

        window.show()
        self.app.processEvents()
        self.assertEqual(window.status_label.text(), "等待模型就绪…")

        window.set_recognizing()
        self.assertEqual(window.status_label.text(), "正在识别…")

    def test_window_has_only_status_and_cancel_and_requests_once(self) -> None:
        window = ProgressWindow()
        labels = window.findChildren(QLabel)
        buttons = window.findChildren(QPushButton)
        self.assertEqual([label.text() for label in labels], ["正在识别…"])
        self.assertEqual([button.text() for button in buttons], ["取消"])

        requests: list[bool] = []
        window.cancel_requested.connect(lambda: requests.append(True))
        window.show()
        self.app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        QTest.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
        window.close()
        self.assertEqual(requests, [True])
        self.assertFalse(window.cancel_button.isEnabled())

    def test_title_close_requests_cancel_but_controller_dismiss_is_silent(self) -> None:
        user_closed = ProgressWindow()
        user_requests: list[bool] = []
        user_closed.cancel_requested.connect(lambda: user_requests.append(True))
        user_closed.show()
        self.app.processEvents()
        user_closed.close()
        self.app.processEvents()
        self.assertEqual(user_requests, [True])

        completed = ProgressWindow()
        completed_requests: list[bool] = []
        completed.cancel_requested.connect(lambda: completed_requests.append(True))
        completed.show()
        self.app.processEvents()
        completed.finish_close()
        self.app.processEvents()
        self.assertEqual(completed_requests, [])
        self.assertFalse(completed.isVisible())


class ErrorDialogTests(QtWidgetTestCase):
    def test_diagnostic_copy_is_exact_and_close_releases_it(self) -> None:
        window = ErrorDialog()
        diagnostic = (
            "TextSnap Layout 0.1.0\n"
            "System: Windows 11\n"
            "Error: OcrInferenceError\n"
            "Code: ocr-inference-failed"
        )
        window.show_error("识别失败。", diagnostic)
        self.app.processEvents()
        window.copy_diagnostic()
        self.assertEqual(QApplication.clipboard().text(), diagnostic)
        self.assertEqual(window.diagnostic_text, diagnostic)

        window.close()
        self.assertIsNone(window.diagnostic_text)
        self.assertEqual(window.message_label.text(), "")
        self.assertFalse(window.copy_diagnostic_button.isEnabled())

    def test_escape_releases_sanitized_diagnostic_reference(self) -> None:
        window = ErrorDialog()
        window.show_error("识别失败。", "Error: OcrInferenceError")
        self.app.processEvents()

        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.app.processEvents()

        self.assertFalse(window.isVisible())
        self.assertIsNone(window.diagnostic_text)
        self.assertEqual(window.message_label.text(), "")
        self.assertFalse(window.copy_diagnostic_button.isEnabled())

    def test_public_message_must_be_sanitized(self) -> None:
        window = ErrorDialog()
        with self.assertRaises(ValueError):
            window.show_error("bad\nmessage", "safe diagnostic")


class ResultWindowTests(QtWidgetTestCase):
    def test_no_wrap_exact_copy_geometry_and_close_cleanup(self) -> None:
        source = "列 A  列 B\r\n  indented\tvalue"
        window = ResultWindow()
        closed: list[bool] = []
        window.closed.connect(lambda: closed.append(True))
        work_area = QRect(100, 200, 1000, 500)
        window.show_result(source, work_area)
        self.app.processEvents()

        self.assertTrue(window.text_edit.isReadOnly())
        self.assertFalse(window.text_edit.isUndoRedoEnabled())
        self.assertEqual(
            window.text_edit.lineWrapMode(),
            QPlainTextEdit.LineWrapMode.NoWrap,
        )
        self.assertEqual(
            window.text_edit.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertEqual(
            window.text_edit.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertEqual(window.text_edit.font().family(), "Noto Sans Mono CJK SC")
        self.assertEqual(window.text_edit.font().pointSize(), 12)
        self.assertEqual(window.geometry(), QRect(200, 250, 800, 400))
        self.assertFalse(
            bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        )

        QTest.mouseClick(window.copy_all_button, Qt.MouseButton.LeftButton)
        self.assertEqual(QApplication.clipboard().text(), source)
        self.assertTrue(window.isVisible())
        self.assertEqual(window.result_text, source)

        window.close()
        self.app.processEvents()
        self.assertEqual(closed, [True])
        self.assertIsNone(window.result_text)
        self.assertEqual(window.text_edit.toPlainText(), "")

    def test_standard_select_all_and_copy_remain_available(self) -> None:
        window = ResultWindow()
        window.show_result("alpha  beta", QRect(0, 0, 800, 600))
        window.text_edit.setFocus()
        self.app.processEvents()
        QTest.keyClick(
            window.text_edit,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.ControlModifier,
        )
        QTest.keyClick(
            window.text_edit,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.assertEqual(QApplication.clipboard().text(), "alpha  beta")


class SettingsWindowTests(QtWidgetTestCase):
    def test_draft_boundary_contains_no_ocr_parameters(self) -> None:
        self.assertEqual(
            {field.name for field in fields(SettingsDraft)},
            {"hotkey", "autostart"},
        )
        window = SettingsWindow()
        self.assertEqual(
            window.draft,
            SettingsDraft(HotkeyValue(("Ctrl", "Alt"), "O"), False),
        )
        visible_text_widgets = [
            *window.findChildren(QLabel),
            *window.findChildren(QPushButton),
        ]
        all_text = " ".join(widget.text() for widget in visible_text_widgets).lower()
        for forbidden in ("threshold", "thresh", "unclip", "batch", "ocr 参数"):
            self.assertNotIn(forbidden, all_text)

    def test_records_hotkey_and_emits_only_settings_draft_on_save(self) -> None:
        window = SettingsWindow()
        window.show()
        window.hotkey_recorder.setFocus()
        self.app.processEvents()
        QTest.keyClick(
            window.hotkey_recorder,
            Qt.Key.Key_P,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        window.autostart_checkbox.setChecked(True)
        saved: list[SettingsDraft] = []
        window.save_requested.connect(saved.append)
        save_button = window.button_box.button(window.button_box.StandardButton.Save)
        QTest.mouseClick(save_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()

        self.assertEqual(
            saved,
            [SettingsDraft(HotkeyValue(("Ctrl", "Shift"), "P"), True)],
        )
        self.assertTrue(window.isVisible())

        window.set_error("快捷键已被其他程序占用。")
        self.assertTrue(window.error_label.isVisible())
        self.assertEqual(window.error_label.text(), "快捷键已被其他程序占用。")
        window.restore_saved_settings()
        self.assertEqual(
            window.draft,
            SettingsDraft(HotkeyValue(("Ctrl", "Alt"), "O"), False),
        )
        with self.assertRaises(ValueError):
            window.set_error("unsafe\npath")

        window.close_after_save(saved[0])
        self.app.processEvents()
        self.assertFalse(window.isVisible())

    def test_cancel_closes_and_restores_last_confirmed_values(self) -> None:
        window = SettingsWindow()
        window.set_settings(HotkeyValue(("Alt",), "F8"), True)
        window.hotkey_recorder.set_hotkey(HotkeyValue(("Ctrl",), "Q"))
        window.autostart_checkbox.setChecked(False)
        cancelled: list[bool] = []
        window.cancelled.connect(lambda: cancelled.append(True))
        window.show()
        self.app.processEvents()

        cancel_button = window.button_box.button(
            window.button_box.StandardButton.Cancel
        )
        QTest.mouseClick(cancel_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(cancelled, [True])
        self.assertFalse(window.isVisible())
        self.assertEqual(
            window.draft,
            SettingsDraft(HotkeyValue(("Alt",), "F8"), True),
        )

    def test_escape_restores_values_clears_error_and_emits_cancel(self) -> None:
        window = SettingsWindow()
        window.set_settings(HotkeyValue(("Alt",), "F8"), True)
        window.hotkey_recorder.set_hotkey(HotkeyValue(("Ctrl",), "Q"))
        window.autostart_checkbox.setChecked(False)
        window.set_error("快捷键已被占用。")
        cancelled: list[bool] = []
        window.cancelled.connect(lambda: cancelled.append(True))
        window.show()
        window.hotkey_recorder.setFocus()
        self.app.processEvents()

        QTest.keyClick(window.hotkey_recorder, Qt.Key.Key_Escape)
        self.app.processEvents()

        self.assertEqual(cancelled, [True])
        self.assertFalse(window.isVisible())
        self.assertFalse(window.error_label.isVisible())
        self.assertEqual(
            window.draft,
            SettingsDraft(HotkeyValue(("Alt",), "F8"), True),
        )

    def test_model_status_and_retry_are_signal_only(self) -> None:
        window = SettingsWindow()
        retries: list[bool] = []
        window.retry_model_requested.connect(lambda: retries.append(True))

        window.set_model_status("ready")
        self.assertEqual(window.model_status_label.text(), "已就绪")
        self.assertFalse(window.retry_model_button.isEnabled())
        window.set_model_status("error")
        self.assertEqual(window.model_status_label.text(), "加载失败")
        self.assertTrue(window.retry_model_button.isEnabled())
        QTest.mouseClick(window.retry_model_button, Qt.MouseButton.LeftButton)
        self.assertEqual(retries, [True])


class _RecordingTray(TrayUi):
    def __init__(self) -> None:
        self.messages: list[tuple] = []
        super().__init__()

    def showMessage(self, *args) -> None:  # noqa: N802
        self.messages.append(args)


class TrayUiTests(QtWidgetTestCase):
    def test_actions_emit_without_platform_side_effects(self) -> None:
        tray = _RecordingTray()
        self.assertEqual(
            [
                tray.capture_action.text(),
                tray.settings_action.text(),
                tray.autostart_action.text(),
                tray.exit_action.text(),
            ],
            ["截图识别", "设置", "开机启动", "退出"],
        )
        events: list[object] = []
        tray.capture_requested.connect(lambda: events.append("capture"))
        tray.settings_requested.connect(lambda: events.append("settings"))
        tray.autostart_toggled.connect(
            lambda checked: events.append(("autostart", checked))
        )
        tray.exit_requested.connect(lambda: events.append("exit"))

        tray.capture_action.trigger()
        tray.settings_action.trigger()
        tray.autostart_action.trigger()
        tray.exit_action.trigger()
        self.assertEqual(
            events,
            ["capture", "settings", ("autostart", True), "exit"],
        )

        tray.set_autostart_checked(False)
        self.assertEqual(len(events), 4)

    def test_context_menu_can_be_hidden_synchronously_before_capture(self) -> None:
        tray = _RecordingTray()
        tray.menu.show()
        self.app.processEvents()
        self.assertTrue(tray.menu.isVisible())

        tray.hide_context_menu()

        self.assertFalse(tray.menu.isVisible())

    def test_startup_notification_interface_is_once_only_and_suppressible(self) -> None:
        tray = _RecordingTray()
        self.assertFalse(tray.show_startup_notification(enabled=False))
        self.assertFalse(tray.startup_notification_shown)
        self.assertTrue(tray.show_startup_notification("Ctrl+Shift+P"))
        self.assertFalse(tray.show_startup_notification("Ctrl+Shift+P"))
        self.assertTrue(tray.startup_notification_shown)
        self.assertEqual(len(tray.messages), 1)
        self.assertEqual(tray.messages[0][0], "TextSnap Layout")
        self.assertEqual(tray.messages[0][1], "已启动，按 Ctrl+Shift+P 截图")


if __name__ == "__main__":
    unittest.main()
