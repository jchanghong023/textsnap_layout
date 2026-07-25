"""Controller tests use fake services while exercising the real Qt boundary."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication
except (ImportError, OSError) as exc:
    _PYSIDE_AVAILABLE = False
    _PYSIDE_SKIP_REASON = f"PySide6 runtime unavailable: {exc}"
else:
    _PYSIDE_AVAILABLE = importlib.util.find_spec("numpy") is not None
    _PYSIDE_SKIP_REASON = "NumPy is unavailable" if not _PYSIDE_AVAILABLE else ""

from textsnap.domain import (
    CaptureFrame,
    Failure,
    LayoutResult,
    LayoutStats,
    ModelState,
    Success,
    TaskState,
)
from textsnap.paths import BundlePaths
from textsnap.settings import Hotkey, Settings, SettingsIssue

if _PYSIDE_AVAILABLE:
    from textsnap.controller import (
        APPLICATION_ICON_RELATIVE_PATH,
        ApplicationController,
        frame_selection_to_bgr,
        load_packaged_ui_resources,
    )
    from textsnap.ui import SettingsDraft
    from textsnap.windows.hotkey import HotkeyRegistrationError
else:
    ApplicationController = object
    SettingsDraft = object


class _Signal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *arguments: object) -> None:
        for callback in tuple(self.callbacks):
            callback(*arguments)


class _Application:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.quit_called = False
        self.filters: list[object] = []

    def processEvents(self) -> None:  # noqa: N802
        self.events.append("process-events")

    def installNativeEventFilter(self, event_filter: object) -> None:  # noqa: N802
        self.filters.append(event_filter)

    def removeNativeEventFilter(self, event_filter: object) -> None:  # noqa: N802
        self.filters.remove(event_filter)

    def quit(self) -> None:
        self.quit_called = True
        self.events.append("application-quit")


class _Timer:
    def __init__(self, _parent: object) -> None:
        self.timeout = _Signal()
        self.single_shot = False
        self.starts: list[int] = []
        self.stopped = False

    def setSingleShot(self, enabled: bool) -> None:  # noqa: N802
        self.single_shot = enabled

    def start(self, milliseconds: int) -> None:
        self.starts.append(milliseconds)

    def stop(self) -> None:
        self.stopped = True


class _Tray:
    def __init__(self, events: list[object]) -> None:
        self.capture_requested = _Signal()
        self.settings_requested = _Signal()
        self.autostart_toggled = _Signal()
        self.exit_requested = _Signal()
        self.events = events
        self.visible = False
        self.autostart_checked = False
        self.notifications: list[tuple[str, bool]] = []
        self.messages: list[tuple[str, str]] = []

    def show(self) -> None:
        self.visible = True
        self.events.append("tray-show")

    def hide(self) -> None:
        self.visible = False
        self.events.append("tray-hide")

    def set_autostart_checked(self, checked: bool) -> None:
        self.autostart_checked = checked

    def show_startup_notification(
        self,
        hotkey_text: str,
        *,
        enabled: bool,
    ) -> None:
        self.notifications.append((hotkey_text, enabled))

    def showMessage(self, title: str, message: str) -> None:  # noqa: N802
        self.messages.append((title, message))


class _SettingsWindow:
    def __init__(self) -> None:
        self.save_requested = _Signal()
        self.retry_model_requested = _Signal()
        self.visible = False
        self.saved = None
        self.model_status = None
        self.error: str | None = None
        self.accepted: list[object] = []

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:  # noqa: N802
        pass

    def set_settings(self, hotkey: Hotkey, autostart: bool) -> None:
        self.saved = SettingsDraft(hotkey, autostart)
        self.error = None

    def set_model_status(self, status: object) -> None:
        self.model_status = status

    def set_error(self, message: str) -> None:
        self.error = message

    def accept_saved(self, draft: object) -> None:
        self.saved = draft
        self.accepted.append(draft)
        self.visible = False


class _ResultWindow:
    def __init__(self) -> None:
        self.closed = _Signal()
        self.visible = False
        self.shown: list[tuple[str, object]] = []
        self.fail_next = False

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible

    def show_result(self, text: str, target: object) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("private display failure")
        self.visible = True
        self.shown.append((text, target))

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


class _ProgressWindow:
    def __init__(self) -> None:
        self.cancel_requested = _Signal()
        self.visible = False
        self.phases: list[str] = []

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def dismiss(self) -> None:
        self.visible = False

    def set_waiting_for_model(self) -> None:
        self.phases.append("waiting")

    def set_recognizing(self) -> None:
        self.phases.append("recognizing")


class _ErrorWindow:
    def __init__(self) -> None:
        self.visible = False
        self.errors: list[tuple[str, str]] = []

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible

    def show_error(self, message: str, diagnostic: str) -> None:
        self.visible = True
        self.errors.append((message, diagnostic))

    def hide(self) -> None:
        self.visible = False

    def close(self) -> None:
        self.visible = False


class _Overlay:
    def __init__(self, frame: CaptureFrame) -> None:
        self.frame = frame
        self.selection_submitted = _Signal()
        self.cancelled = _Signal()
        self.visible = False
        self.closed = False
        self.target_screen = _Screen()

    def show(self) -> None:
        self.visible = True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.visible = False
        self.cancelled.emit()

    def screen(self) -> object:
        return self.target_screen


class _Screen:
    def availableGeometry(self) -> QRect:  # noqa: N802
        return QRect(100, 200, 1000, 500)


class _Hotkeys:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.active = None
        self.fail_next = False
        self.fail_rollback = False

    def register(self, binding) -> bool:
        self.events.append(("hotkey", "+".join((*binding.modifiers, binding.key))))
        if self.fail_next:
            self.fail_next = False
            raise HotkeyRegistrationError("HOTKEY-CONFLICT", 1409)
        if self.fail_rollback and binding.key == "O":
            raise HotkeyRegistrationError("HOTKEY-ROLLBACK", 5)
        self.active = binding
        return True

    def unregister(self) -> None:
        self.events.append("hotkey-unregister")
        self.active = None


class _Autostart:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.enabled = False
        self.command: str | None = None
        self.fail_rollback = False

    def set_enabled(self, enabled: bool) -> None:
        self.events.append(("autostart", enabled))
        if self.fail_rollback and not enabled:
            raise RuntimeError("private path must not escape")
        self.enabled = enabled
        self.command = "current-command" if enabled else None

    def registered_command(self) -> str | None:
        self.events.append(("autostart-read",))
        return self.command

    def restore_registered_command(self, command: str | None) -> None:
        self.events.append(("autostart-restore", command))
        if self.fail_rollback:
            raise RuntimeError("private path must not escape")
        self.command = command
        self.enabled = command is not None


class _Server:
    def __init__(self, events: list[object]) -> None:
        self.command_received = _Signal()
        self.events = events

    def start(self) -> None:
        self.events.append("server-start")

    def close(self) -> None:
        self.events.append("server-close")


class _Worker:
    def __init__(self, events: list[object], state: ModelState) -> None:
        self.model_state_changed = _Signal()
        self.model_failed = _Signal()
        self.task_finished = _Signal()
        self.task_rejected = _Signal()
        self.shutdown_finished = _Signal()
        self.events = events
        self.model_state = state
        self.submitted: list[object] = []
        self.next_task_id = 1
        self.cancel_calls = 0
        self.retry_result = True

    def start(self) -> None:
        self.events.append("worker-start")

    def submit(self, image: object) -> object:
        self.submitted.append(image)
        task = SimpleNamespace(task_id=self.next_task_id)
        self.next_task_id += 1
        return task

    def cancel_active(self) -> bool:
        self.cancel_calls += 1
        return True

    def retry_model(self) -> bool:
        return self.retry_result

    def shutdown(self) -> None:
        self.events.append("worker-shutdown")

    def emit_model_state(self, state: ModelState) -> None:
        self.model_state = state
        self.model_state_changed.emit(state)


@dataclass
class _Harness:
    controller: object
    events: list[object]
    application: _Application
    timer: _Timer
    tray: _Tray
    settings: _SettingsWindow
    result: _ResultWindow
    progress: _ProgressWindow
    error: _ErrorWindow
    hotkeys: _Hotkeys
    autostart: _Autostart
    server: _Server
    worker: _Worker
    overlays: list[_Overlay]
    writes: list[object]
    notices: list[str]
    capture_calls: list[bool]


@unittest.skipUnless(_PYSIDE_AVAILABLE, _PYSIDE_SKIP_REASON)
class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

    @staticmethod
    def _frame() -> CaptureFrame:
        pixels = bytes(
            (
                1,
                2,
                3,
                0,
                4,
                5,
                6,
                0,
                7,
                8,
                9,
                0,
                10,
                11,
                12,
                0,
                13,
                14,
                15,
                0,
                16,
                17,
                18,
                0,
            )
        )
        return CaptureFrame(pixels, 3, 2, "DISPLAY-1", 0, 0, 96, 96)

    def _harness(
        self,
        *,
        model_state: ModelState = ModelState.LOADING,
        writer=None,
        settings_issue: SettingsIssue | None = None,
    ) -> _Harness:
        events: list[object] = []
        application = _Application(events)
        timer = _Timer(None)
        tray = _Tray(events)
        settings = _SettingsWindow()
        result = _ResultWindow()
        progress = _ProgressWindow()
        error = _ErrorWindow()
        hotkeys = _Hotkeys(events)
        autostart = _Autostart(events)
        server = _Server(events)
        worker = _Worker(events, model_state)
        overlays: list[_Overlay] = []
        writes: list[object] = []
        notices: list[str] = []
        capture_calls: list[bool] = []
        paths = BundlePaths(Path(self.temporary_directory.name).resolve())

        def overlay_factory(frame: CaptureFrame) -> _Overlay:
            overlay = _Overlay(frame)
            overlays.append(overlay)
            return overlay

        def capture() -> CaptureFrame:
            capture_calls.append(True)
            return self._frame()

        def settings_writer(path: object, value: Settings) -> None:
            writes.append((path, value))
            if writer is not None:
                writer(path, value)

        controller = ApplicationController(
            application=application,
            paths=paths,
            initial_settings=Settings(),
            hotkey_service=hotkeys,
            autostart_service=autostart,
            command_server=server,
            ocr_controller=worker,
            tray=tray,
            settings_window=settings,
            result_window=result,
            progress_window=progress,
            error_window=error,
            settings_issue=settings_issue,
            overlay_factory=overlay_factory,
            capture_function=capture,
            settings_writer=settings_writer,
            notifier=notices.append,
            native_event_filter_factory=None,
            shutdown_timer_factory=lambda parent: timer,
            force_exit_prompt=lambda: False,
            force_exit=lambda code: events.append(("force-exit", code)),
        )
        return _Harness(
            controller,
            events,
            application,
            timer,
            tray,
            settings,
            result,
            progress,
            error,
            hotkeys,
            autostart,
            server,
            worker,
            overlays,
            writes,
            notices,
            capture_calls,
        )

    def test_start_shows_tray_before_starting_worker(self) -> None:
        harness = self._harness()

        harness.controller.start()

        self.assertLess(
            harness.events.index("tray-show"),
            harness.events.index("worker-start"),
        )
        self.assertIn(("hotkey", "Ctrl+Alt+O"), harness.events)
        self.assertIn("server-start", harness.events)
        self.assertEqual(harness.tray.notifications, [("Ctrl+Alt+O", True)])
        self.assertFalse(harness.tray.autostart_checked)

    def test_invalid_settings_defaults_do_not_change_autostart_before_save(
        self,
    ) -> None:
        issue = SettingsIssue(
            "settings-invalid",
            "设置文件已损坏，已在内存中使用默认值；保存前不会覆盖原文件。",
        )
        harness = self._harness(settings_issue=issue)

        harness.controller.start()

        self.assertNotIn(("autostart", False), harness.events)
        self.assertEqual(harness.notices, [issue.public_message])

    def test_invalid_settings_defaults_sync_only_after_explicit_save(self) -> None:
        issue = SettingsIssue(
            "settings-invalid",
            "设置文件已损坏，已在内存中使用默认值；保存前不会覆盖原文件。",
        )
        harness = self._harness(settings_issue=issue)
        harness.autostart.command = "stale-command"
        harness.autostart.enabled = True
        harness.controller.start()
        harness.events.clear()

        self.assertTrue(
            harness.controller.apply_settings_draft(SettingsDraft(Hotkey(), False))
        )

        self.assertEqual(
            harness.events,
            [
                ("autostart-read",),
                ("autostart", False),
            ],
        )
        self.assertIsNone(harness.autostart.command)
        self.assertEqual(len(harness.writes), 1)

    def test_invalid_settings_save_failure_restores_exact_autostart_snapshot(
        self,
    ) -> None:
        def fail_write(path: object, value: Settings) -> None:
            raise RuntimeError

        issue = SettingsIssue(
            "settings-invalid",
            "设置文件已损坏，已在内存中使用默认值；保存前不会覆盖原文件。",
        )
        harness = self._harness(writer=fail_write, settings_issue=issue)
        harness.autostart.command = "stale-command"
        harness.autostart.enabled = True
        harness.controller.start()
        harness.events.clear()

        self.assertFalse(
            harness.controller.apply_settings_draft(SettingsDraft(Hotkey(), False))
        )

        self.assertEqual(
            harness.events,
            [
                ("autostart-read",),
                ("autostart", False),
                ("autostart-restore", "stale-command"),
            ],
        )
        self.assertEqual(harness.autostart.command, "stale-command")

    def test_loading_capture_waits_once_then_submits_contiguous_bgr(self) -> None:
        harness = self._harness(model_state=ModelState.LOADING)
        harness.controller.start()

        self.assertTrue(harness.controller.request_capture())
        overlay = harness.overlays[-1]
        overlay.selection_submitted.emit(QRect(1, 0, 2, 2))

        self.assertEqual(harness.controller.state.task_state, TaskState.RECOGNIZING)
        self.assertIsNotNone(harness.controller.pending_image_bgr)
        self.assertEqual(harness.worker.submitted, [])
        self.assertEqual(harness.progress.phases, ["waiting"])
        self.assertFalse(harness.controller.request_capture())
        self.assertEqual(len(harness.capture_calls), 1)
        self.assertIn("正在识别", harness.notices[-1])

        harness.worker.emit_model_state(ModelState.READY)

        self.assertEqual(harness.progress.phases, ["waiting", "recognizing"])
        self.assertEqual(len(harness.worker.submitted), 1)
        image = harness.worker.submitted[0]
        self.assertEqual(image.shape, (2, 2, 3))
        self.assertTrue(image.flags.c_contiguous)
        self.assertEqual(
            image.tolist(),
            [
                [[4, 5, 6], [7, 8, 9]],
                [[13, 14, 15], [16, 17, 18]],
            ],
        )
        self.assertIsNone(harness.controller.pending_image_bgr)

    def test_success_replaces_and_failure_restores_old_result(self) -> None:
        harness = self._harness(model_state=ModelState.READY)
        harness.controller.start()
        old = LayoutResult("old", LayoutStats(1, 1, 1, 1.0, 1.0))
        harness.controller.state.visible_result = old
        harness.result.show_result("old", "old-screen")

        harness.controller.request_capture()
        harness.overlays[-1].selection_submitted.emit(QRect(0, 0, 3, 2))
        task_id = harness.controller.active_task_id
        failure = Failure("OcrError", "private text", "ocr-failed")
        harness.worker.task_finished.emit(task_id, failure)

        self.assertEqual(harness.controller.state.visible_result, old)
        self.assertEqual(harness.result.shown[-1][0], "old")
        message, diagnostic = harness.error.errors[-1]
        self.assertEqual(message, "识别失败。")
        self.assertIn("OcrError", diagnostic)
        self.assertIn("ocr-failed", diagnostic)
        self.assertNotIn("private text", diagnostic)

        harness.controller.request_capture()
        harness.overlays[-1].selection_submitted.emit(QRect(0, 0, 3, 2))
        task_id = harness.controller.active_task_id
        new = LayoutResult("new", LayoutStats(1, 1, 1, 1.0, 1.0))
        harness.worker.task_finished.emit(task_id, Success(new))

        self.assertEqual(harness.controller.state.visible_result, new)
        self.assertEqual(harness.result.shown[-1][0], "new")
        harness.result.closed.emit()
        self.assertIsNone(harness.controller.state.visible_result)

    def test_result_display_failure_rolls_back_success_to_old_result(self) -> None:
        harness = self._harness(model_state=ModelState.READY)
        harness.controller.start()
        old = LayoutResult("old", LayoutStats(1, 1, 1, 1.0, 1.0))

        harness.controller.request_capture()
        harness.overlays[-1].selection_submitted.emit(QRect(0, 0, 3, 2))
        task_id = harness.controller.active_task_id
        old_target = harness.overlays[-1].target_screen.availableGeometry()
        harness.worker.task_finished.emit(task_id, Success(old))

        harness.controller.request_capture()
        harness.overlays[-1].selection_submitted.emit(QRect(0, 0, 3, 2))
        task_id = harness.controller.active_task_id
        new = LayoutResult("new", LayoutStats(1, 1, 1, 1.0, 1.0))
        harness.result.fail_next = True
        harness.worker.task_finished.emit(task_id, Success(new))

        self.assertEqual(harness.controller.state.visible_result, old)
        self.assertEqual(harness.result.shown[-1], ("old", old_target))
        message, diagnostic = harness.error.errors[-1]
        self.assertEqual(message, "无法显示识别结果。")
        self.assertIn("ResultDisplayError", diagnostic)
        self.assertIn("controller-result-display", diagnostic)
        self.assertNotIn("private display failure", diagnostic)

    def test_model_error_finishes_waiting_task_and_restores_old_result(self) -> None:
        harness = self._harness(model_state=ModelState.LOADING)
        harness.controller.start()
        old = LayoutResult("old", LayoutStats(1, 1, 1, 1.0, 1.0))
        harness.controller.state.visible_result = old
        harness.result.show_result("old", None)

        harness.controller.request_capture()
        harness.overlays[-1].selection_submitted.emit(QRect(0, 0, 3, 2))
        harness.worker.emit_model_state(ModelState.ERROR)
        harness.worker.model_failed.emit(Failure("ModelError", "secret", "model-error"))

        self.assertEqual(harness.controller.state.task_state, TaskState.IDLE)
        self.assertIsNone(harness.controller.pending_image_bgr)
        self.assertEqual(harness.controller.state.visible_result, old)
        self.assertEqual(harness.result.shown[-1][0], "old")

    def test_settings_failure_rolls_back_in_reverse_order(self) -> None:
        def fail_write(path: object, value: Settings) -> None:
            raise RuntimeError("/Users/alice/private/settings.json")

        harness = self._harness(writer=fail_write)
        harness.controller.start()
        harness.events.clear()
        draft = SettingsDraft(Hotkey(("Ctrl", "Shift"), "P"), True)

        self.assertFalse(harness.controller.apply_settings_draft(draft))

        self.assertEqual(
            harness.events,
            [
                ("hotkey", "Ctrl+Shift+P"),
                ("autostart", True),
                ("autostart", False),
                ("hotkey", "Ctrl+Alt+O"),
            ],
        )
        self.assertEqual(harness.controller.current_settings, Settings())
        self.assertEqual(harness.settings.saved, SettingsDraft(Hotkey(), False))
        self.assertEqual(
            harness.settings.error,
            "无法保存设置；原设置保持不变。",
        )
        self.assertNotIn("alice", harness.settings.error)

    def test_rollback_failure_reports_possible_external_divergence(self) -> None:
        def fail_write(path: object, value: Settings) -> None:
            raise RuntimeError

        harness = self._harness(writer=fail_write)
        harness.controller.start()
        harness.hotkeys.fail_rollback = True
        harness.autostart.fail_rollback = True

        draft = SettingsDraft(Hotkey(("Ctrl", "Shift"), "P"), True)
        self.assertFalse(harness.controller.apply_settings_draft(draft))

        self.assertEqual(harness.controller.current_settings, Settings())
        self.assertEqual(harness.settings.saved, SettingsDraft(Hotkey(), False))
        self.assertEqual(
            harness.settings.error,
            "保存失败，部分系统设置可能未恢复；配置文件保持原值。",
        )
        self.assertFalse(harness.tray.autostart_checked)

    def test_hotkey_conflict_keeps_window_open_with_fixed_error(self) -> None:
        harness = self._harness()
        harness.controller.start()
        harness.settings.visible = True
        harness.hotkeys.fail_next = True
        draft = SettingsDraft(Hotkey(("Ctrl", "Shift"), "P"), False)

        self.assertFalse(harness.controller.apply_settings_draft(draft))

        self.assertTrue(harness.settings.visible)
        self.assertEqual(harness.controller.current_settings, Settings())
        self.assertEqual(
            harness.settings.error,
            "快捷键已被其他程序占用，请选择其他组合。",
        )

    def test_shutdown_waits_ten_seconds_before_force_choice(self) -> None:
        harness = self._harness(model_state=ModelState.READY)
        harness.controller.start()
        harness.controller.request_capture()
        harness.overlays[-1].selection_submitted.emit(QRect(0, 0, 3, 2))

        harness.controller.request_exit()

        self.assertEqual(harness.worker.cancel_calls, 1)
        self.assertEqual(harness.timer.starts, [10_000])
        self.assertIn("worker-shutdown", harness.events)
        self.assertFalse(harness.application.quit_called)

        harness.timer.timeout.emit()
        self.assertEqual(harness.timer.starts, [10_000, 10_000])
        harness.worker.shutdown_finished.emit(None)

        self.assertTrue(harness.timer.stopped)
        self.assertTrue(harness.application.quit_called)
        self.assertNotIn(("force-exit", 1), harness.events)

    def test_packaged_icon_path_matches_staging_contract(self) -> None:
        self.assertEqual(
            APPLICATION_ICON_RELATIVE_PATH,
            Path("assets/icons/textsnap.ico"),
        )

    def test_packaged_font_is_registered_before_non_null_icon_is_used(self) -> None:
        paths = BundlePaths(Path(self.temporary_directory.name).resolve())
        icon = SimpleNamespace(isNull=lambda: False)

        with (
            mock.patch(
                "textsnap.controller.QFontDatabase.addApplicationFont",
                return_value=4,
            ) as add_font,
            mock.patch(
                "textsnap.controller.QFontDatabase.applicationFontFamilies",
                return_value=["Noto Sans Mono CJK SC"],
            ) as font_families,
            mock.patch(
                "textsnap.controller.QIcon",
                return_value=icon,
            ) as icon_factory,
        ):
            loaded = load_packaged_ui_resources(paths)

        self.assertIs(loaded, icon)
        add_font.assert_called_once_with(str(paths.font_file))
        font_families.assert_called_once_with(4)
        icon_factory.assert_called_once_with(
            str(paths.root / "assets" / "icons" / "textsnap.ico")
        )

    def test_direct_selection_converter_rejects_out_of_bounds(self) -> None:
        with self.assertRaises(ValueError):
            frame_selection_to_bgr(self._frame(), QRect(2, 0, 2, 2))


if __name__ == "__main__":
    unittest.main()
