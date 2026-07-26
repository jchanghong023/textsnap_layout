"""GUI-thread orchestration for capture, OCR, settings, and shutdown."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRect, QTimer
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .diagnostics import diagnostic_from_failure
from .domain import (
    Cancelled,
    CaptureFrame,
    Empty,
    Failure,
    ModelState,
    Success,
    TaskOutcome,
    TaskState,
)
from .ocr import OcrEngine
from .paths import BundlePaths
from .qt_instance import InstanceCommandServer
from .qt_worker import OcrThreadController
from .runtime_diagnostics import record_runtime_event
from .settings import (
    Hotkey,
    Settings,
    SettingsIssue,
    save_settings,
)
from .state import ApplicationState, CaptureRequest
from .ui import (
    ErrorDialog,
    ProgressWindow,
    ResultWindow,
    SelectionOverlay,
    SettingsDraft,
    SettingsWindow,
    TrayUi,
)
from .windows.autostart import AutostartService
from .windows.capture import capture_monitor_under_cursor
from .windows.hotkey import (
    HotkeyBinding,
    HotkeyRegistrationError,
    HotkeyService,
    create_qt_native_event_filter,
)


_OUTCOME_TYPES = (Success, Empty, Cancelled, Failure)
APPLICATION_ICON_RELATIVE_PATH = Path("assets/icons/textsnap.ico")
RESULT_FONT_FAMILY = "Noto Sans Mono CJK SC"


class ControllerStartupError(RuntimeError):
    """A fixed, sanitized failure raised before the resident UI is ready."""

    def __init__(self, diagnostic_code: str) -> None:
        self.diagnostic_code = diagnostic_code
        super().__init__(f"应用控制器无法启动。 [{diagnostic_code}]")


def _failure(
    error_type: str,
    public_message: str,
    diagnostic_code: str,
) -> Failure:
    return Failure(error_type, public_message, diagnostic_code)


def frame_selection_to_bgr(
    frame: CaptureFrame,
    rectangle: QRect,
    *,
    numpy_module: Any | None = None,
) -> object:
    """Copy one physical selection into an owned contiguous uint8 BGR array."""

    if not isinstance(frame, CaptureFrame):
        raise TypeError("frame must be CaptureFrame")
    if not isinstance(rectangle, QRect):
        raise TypeError("rectangle must be QRect")
    x = int(rectangle.x())
    y = int(rectangle.y())
    width = int(rectangle.width())
    height = int(rectangle.height())
    if (
        x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > frame.width
        or y + height > frame.height
    ):
        raise ValueError("selection rectangle is outside the capture")

    try:
        pixel_view = memoryview(frame.pixels)
    except TypeError:
        raise ValueError("capture pixels are not a packed BGRA buffer") from None
    expected_bytes = frame.width * frame.height * 4
    if not pixel_view.c_contiguous or pixel_view.nbytes != expected_bytes:
        raise ValueError("capture BGRA storage is invalid")

    if numpy_module is None:
        import numpy as numpy_module

    try:
        bgra = numpy_module.frombuffer(
            pixel_view,
            dtype=numpy_module.uint8,
            count=expected_bytes,
        ).reshape((frame.height, frame.width, 4))
        # The first three GDI bytes are already B, G, R. Always copy so neither
        # the CaptureFrame nor the overlay owns storage used by the worker.
        bgr = numpy_module.array(
            bgra[y : y + height, x : x + width, :3],
            dtype=numpy_module.uint8,
            order="C",
            copy=True,
        )
    except Exception:
        raise ValueError("capture BGRA storage could not be converted") from None
    if (
        bgr.shape != (height, width, 3)
        or bgr.dtype != numpy_module.uint8
        or not bool(bgr.flags.c_contiguous)
    ):
        raise ValueError("selection BGR storage is invalid")
    return bgr


class ApplicationController(QObject):
    """Coordinate one in-memory capture/OCR task and the resident tray UI."""

    def __init__(
        self,
        *,
        application: QApplication,
        paths: BundlePaths,
        initial_settings: Settings,
        hotkey_service: HotkeyService,
        autostart_service: AutostartService,
        command_server: InstanceCommandServer,
        ocr_controller: OcrThreadController,
        tray: TrayUi,
        settings_window: SettingsWindow,
        result_window: ResultWindow,
        progress_window: ProgressWindow,
        error_window: ErrorDialog | None = None,
        startup_autostart: bool = False,
        settings_issue: SettingsIssue | None = None,
        overlay_factory: Callable[[CaptureFrame], object] = SelectionOverlay,
        capture_function: Callable[[], CaptureFrame] = capture_monitor_under_cursor,
        settings_writer: Callable[[Path | str, Settings], None] = save_settings,
        notifier: Callable[[str], None] | None = None,
        native_event_filter_factory: Callable[
            [HotkeyService, Callable[[], None]], object
        ]
        | None = None,
        shutdown_timer_factory: Callable[[QObject], object] | None = None,
        force_exit_prompt: Callable[[], bool] | None = None,
        force_exit: Callable[[int], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(initial_settings, Settings):
            raise TypeError("initial_settings must be Settings")
        self._application = application
        self._paths = paths
        self._settings = initial_settings
        self._hotkey_service = hotkey_service
        self._autostart_service = autostart_service
        self._command_server = command_server
        self._ocr_controller = ocr_controller
        self._tray = tray
        self._settings_window = settings_window
        self._result_window = result_window
        self._progress_window = progress_window
        self._error_window = error_window
        self._startup_autostart = bool(startup_autostart)
        self._settings_issue = settings_issue
        self._autostart_requires_sync = settings_issue is not None
        self._overlay_factory = overlay_factory
        self._capture_function = capture_function
        self._settings_writer = settings_writer
        self._notifier = notifier or self._tray_notice
        if native_event_filter_factory is None and os.name == "nt":
            native_event_filter_factory = create_qt_native_event_filter
        self._native_event_filter_factory = native_event_filter_factory
        self._force_exit_prompt = force_exit_prompt or self._default_force_exit_prompt
        self._force_exit = force_exit or os._exit

        timer_factory = shutdown_timer_factory or QTimer
        self._shutdown_timer = timer_factory(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.timeout.connect(self._offer_force_exit)

        initial_model_state = getattr(
            self._ocr_controller,
            "model_state",
            ModelState.LOADING,
        )
        if not isinstance(initial_model_state, ModelState):
            initial_model_state = ModelState.LOADING
        self._state = ApplicationState(model_state=initial_model_state)
        self._capture_frame: CaptureFrame | None = None
        self._selection_overlay: object | None = None
        self._pending_bgr: object | None = None
        self._active_task_id: int | None = None
        self._native_event_filter: object | None = None
        self._visible_result_target: object | None = None
        self._previous_result_target: object | None = None
        self._task_result_target: object | None = None
        self._started = False
        self._shutting_down = False
        self._shutdown_complete = False
        self._shutdown_failure: Failure | None = None
        self._settings_open_pending = False
        self._settings_open_scheduled = False

        self._connect_signals()

    @property
    def state(self) -> ApplicationState:
        return self._state

    @property
    def current_settings(self) -> Settings:
        return self._settings

    @property
    def pending_image_bgr(self) -> object | None:
        return self._pending_bgr

    @property
    def active_task_id(self) -> int | None:
        return self._active_task_id

    @property
    def shutdown_failure(self) -> Failure | None:
        return self._shutdown_failure

    def _connect_signals(self) -> None:
        self._tray.capture_requested.connect(self.request_capture)
        self._tray.settings_requested.connect(self.show_settings)
        self._tray.autostart_toggled.connect(self.set_autostart_from_tray)
        self._tray.exit_requested.connect(self.request_exit)
        self._settings_window.save_requested.connect(self.apply_settings_draft)
        self._settings_window.retry_model_requested.connect(self.retry_model)
        self._progress_window.cancel_requested.connect(self.cancel_current_task)
        self._result_window.closed.connect(self._on_result_closed)
        self._command_server.command_received.connect(self._on_instance_command)
        self._ocr_controller.model_state_changed.connect(self._on_model_state)
        self._ocr_controller.model_failed.connect(self._on_model_failure)
        self._ocr_controller.task_finished.connect(self._on_task_finished)
        self._ocr_controller.task_rejected.connect(self._on_task_rejected)
        self._ocr_controller.shutdown_finished.connect(self._on_shutdown_finished)

    def start(self) -> None:
        """Register resident services, show the tray, then start model loading."""

        if self._started:
            return
        record_runtime_event("controller.start")
        hotkey_registered = False
        server_started = False
        try:
            self._hotkey_service.register(self._binding(self._settings.hotkey))
            hotkey_registered = True
            record_runtime_event("controller.hotkey-ready")
            self._command_server.start()
            server_started = True
            record_runtime_event("controller.instance-server-ready")
            if self._native_event_filter_factory is not None:
                self._native_event_filter = self._native_event_filter_factory(
                    self._hotkey_service,
                    self.request_capture,
                )
                self._application.installNativeEventFilter(self._native_event_filter)
                record_runtime_event("controller.native-filter-ready")
        except Exception as exc:
            record_runtime_event(
                "controller.start-failed",
                exception_type=type(exc).__name__,
            )
            if server_started:
                self._safe_call(self._command_server.close)
            if hotkey_registered:
                self._safe_call(self._hotkey_service.unregister)
            raise ControllerStartupError("controller-resident-start") from None

        autostart_sync_notice = self._sync_autostart_at_start()
        self._tray.set_autostart_checked(self._settings.autostart)
        self._settings_window.set_settings(
            self._settings.hotkey,
            self._settings.autostart,
        )
        self._settings_window.set_model_status(self._state.model_state)
        self._tray.show()
        self._tray.show_startup_notification(
            self._hotkey_text(self._settings.hotkey),
            enabled=not self._startup_autostart,
        )
        record_runtime_event("controller.tray-ready")
        self._ocr_controller.start()
        record_runtime_event("controller.ocr-started")
        self._started = True
        record_runtime_event("controller.ready")
        if autostart_sync_notice is not None:
            self._notify(autostart_sync_notice)
        if self._settings_issue is not None:
            self._notify(self._settings_issue.public_message)

    def _sync_autostart_at_start(self) -> str | None:
        if self._settings_issue is not None:
            # A damaged or unreadable file supplies memory-only defaults. Do
            # not project those defaults into HKCU before the user explicitly
            # saves a replacement configuration.
            return None
        try:
            self._autostart_service.set_enabled(self._settings.autostart)
        except Exception:
            self._autostart_requires_sync = True
            return "无法同步开机启动设置。"
        return None

    def request_capture(self) -> bool:
        """Hide application windows, flush events, and freeze the mouse monitor."""

        record_runtime_event("capture.requested")
        if self._shutting_down:
            record_runtime_event("capture.rejected", reason="shutdown")
            return False
        if self._state.request_capture() is CaptureRequest.BUSY:
            record_runtime_event("capture.rejected", reason="busy")
            self._notify("正在识别，请等待当前任务完成。")
            return False

        self._previous_result_target = self._visible_result_target
        self._hide_for_capture()
        self._application.processEvents()
        if (
            self._shutting_down
            or self._state.task_state is not TaskState.CAPTURING
        ):
            return False
        # processEvents() can deliver queued model or local-instance signals
        # that reopen one of our windows. Hide them again immediately before
        # the Win32 capture; capture_monitor_under_cursor performs DwmFlush.
        self._hide_for_capture()
        try:
            frame = self._capture_function()
            if not isinstance(frame, CaptureFrame):
                raise TypeError
            overlay = self._overlay_factory(frame)
            overlay.selection_submitted.connect(self._on_selection_submitted)
            overlay.cancelled.connect(self._on_selection_cancelled)
            self._capture_frame = frame
            self._selection_overlay = overlay
            overlay.show()
            record_runtime_event("capture.overlay-visible")
            screen_getter = getattr(overlay, "screen", None)
            target_screen = screen_getter() if callable(screen_getter) else None
            if isinstance(target_screen, QRect):
                self._task_result_target = QRect(target_screen)
            else:
                geometry_getter = getattr(target_screen, "availableGeometry", None)
                self._task_result_target = (
                    QRect(geometry_getter()) if callable(geometry_getter) else None
                )
            return True
        except Exception as exc:
            record_runtime_event(
                "capture.failed",
                exception_type=type(exc).__name__,
            )
            self._capture_frame = None
            self._selection_overlay = None
            self._state.cancel_capture()
            self._restore_previous_result()
            self._show_failure(
                _failure(
                    "CaptureError",
                    "截图失败；当前桌面表面可能不受支持。",
                    "controller-capture-failed",
                )
            )
            self._schedule_pending_settings_open()
            return False

    def _hide_for_capture(self) -> None:
        self._tray.hide_context_menu()
        windows = [self._settings_window, self._result_window]
        if self._error_window is not None:
            windows.append(self._error_window)
        for window in windows:
            try:
                if window.isVisible():
                    window.hide()
            except Exception:
                window.hide()

    def _on_selection_submitted(self, rectangle: QRect) -> None:
        record_runtime_event("capture.selection-submitted")
        if (
            self._shutting_down
            or self._state.task_state is not TaskState.CAPTURING
            or self._capture_frame is None
        ):
            return
        try:
            selected = frame_selection_to_bgr(self._capture_frame, rectangle)
        except Exception:
            self._capture_frame = None
            self._selection_overlay = None
            self._state.cancel_capture()
            self._restore_previous_result()
            self._show_failure(
                _failure(
                    "CaptureSelectionError",
                    "无法处理截图选区。",
                    "controller-selection-convert",
                )
            )
            self._schedule_pending_settings_open()
            return

        self._pending_bgr = selected
        self._capture_frame = None
        self._selection_overlay = None
        self._state.submit_capture()
        self._set_progress_waiting(self._state.model_state is ModelState.LOADING)
        self._progress_window.show()
        self._submit_or_wait_for_model()
        self._schedule_pending_settings_open()

    def _on_selection_cancelled(self) -> None:
        if self._state.task_state is not TaskState.CAPTURING:
            return
        record_runtime_event("capture.cancelled")
        self._capture_frame = None
        self._selection_overlay = None
        self._task_result_target = None
        self._state.cancel_capture()
        self._restore_previous_result()
        self._schedule_pending_settings_open()

    def _submit_or_wait_for_model(self) -> None:
        if (
            self._shutting_down
            or self._state.task_state is not TaskState.RECOGNIZING
            or self._pending_bgr is None
            or self._active_task_id is not None
        ):
            return
        if self._state.model_state is ModelState.LOADING:
            return
        if self._state.model_state is ModelState.ERROR:
            self._finish_outcome(
                _failure(
                    "OcrModelNotReadyError",
                    "模型尚未就绪。",
                    "controller-model-not-ready",
                )
            )
            return

        self._set_progress_waiting(False)
        image = self._pending_bgr
        task = self._ocr_controller.submit(image)
        if task is None:
            # Real OcrThreadController emits task_rejected synchronously. Fakes
            # and future adapters still cannot leave the task stuck.
            if self._state.task_state is TaskState.RECOGNIZING:
                self._finish_outcome(
                    _failure(
                        "OcrTaskSubmissionError",
                        "识别任务无法启动。",
                        "controller-task-submit",
                    )
                )
            return
        task_id = getattr(task, "task_id", None)
        if not isinstance(task_id, int) or task_id <= 0:
            self._finish_outcome(
                _failure(
                    "OcrTaskBoundaryError",
                    "识别任务无法启动。",
                    "controller-task-id",
                )
            )
            return
        self._active_task_id = task_id
        self._pending_bgr = None
        record_runtime_event("controller.task-submitted", task_id=task_id)

    def cancel_current_task(self) -> bool:
        if self._state.task_state is TaskState.CAPTURING:
            overlay = self._selection_overlay
            if overlay is not None:
                self._safe_call(overlay.close)
            if self._state.task_state is TaskState.CAPTURING:
                self._on_selection_cancelled()
            return True
        if self._state.task_state is not TaskState.RECOGNIZING:
            return False
        self._state.request_cancel()
        if self._active_task_id is None:
            self._finish_outcome(Cancelled())
            return True
        self._ocr_controller.cancel_active()
        return True

    def _on_model_state(self, state: object) -> None:
        if not isinstance(state, ModelState):
            state = ModelState.ERROR
        if state is ModelState.LOADING:
            self._state.start_model_loading()
        elif state is ModelState.READY:
            self._state.model_ready()
        else:
            self._state.model_state = ModelState.ERROR
        self._settings_window.set_model_status(state)
        if state is ModelState.READY:
            self._submit_or_wait_for_model()

    def _on_model_failure(self, failure: object) -> None:
        if not isinstance(failure, Failure):
            failure = _failure(
                "OcrModelFailure",
                "模型加载失败。",
                "controller-model-failure",
            )
        self._state.model_failed(failure.diagnostic_code)
        self._settings_window.set_model_status(ModelState.ERROR)
        if self._state.task_state is TaskState.RECOGNIZING:
            self._finish_outcome(failure)
        elif not self._shutting_down:
            self._show_failure(
                failure,
                public_message="模型加载失败，可在设置中重试。",
            )

    def retry_model(self) -> bool:
        if self._shutting_down:
            return False
        if not self._ocr_controller.retry_model():
            self._notify("当前无法重试模型加载。")
            return False
        self._state.start_model_loading()
        self._settings_window.set_model_status(ModelState.LOADING)
        return True

    def _on_task_finished(self, task_id: int, outcome: object) -> None:
        if self._active_task_id != task_id:
            return
        self._active_task_id = None
        if not isinstance(outcome, _OUTCOME_TYPES):
            outcome = _failure(
                "OcrTaskOutcomeError",
                "识别失败。",
                "controller-task-outcome",
            )
        self._finish_outcome(outcome)

    def _on_task_rejected(self, task_id: int, failure: object) -> None:
        if self._state.task_state is not TaskState.RECOGNIZING:
            return
        if self._active_task_id is not None and self._active_task_id != task_id:
            return
        self._active_task_id = None
        if not isinstance(failure, Failure):
            failure = _failure(
                "OcrTaskRejectedError",
                "识别任务无法启动。",
                "controller-task-rejected",
            )
        self._finish_outcome(failure)

    def _finish_outcome(self, outcome: TaskOutcome) -> None:
        if self._state.task_state is not TaskState.RECOGNIZING:
            return
        record_runtime_event(
            "controller.task-finished",
            outcome_type=type(outcome).__name__,
        )
        previous_visible_result = self._state.visible_result
        previous_result_target = self._previous_result_target
        visible = self._state.finish_task(outcome)
        self._pending_bgr = None
        self._capture_frame = None
        self._selection_overlay = None
        self._active_task_id = None
        self._progress_window.dismiss()

        if isinstance(outcome, Success):
            self._visible_result_target = self._task_result_target
        else:
            self._visible_result_target = self._previous_result_target
        self._task_result_target = None
        self._previous_result_target = None

        if self._shutting_down:
            return
        if visible is not None:
            try:
                self._result_window.show_result(
                    visible.text,
                    self._visible_result_target,
                )
            except Exception:
                if isinstance(outcome, Success):
                    self._state.visible_result = previous_visible_result
                    self._visible_result_target = previous_result_target
                    if previous_visible_result is not None:
                        self._safe_call(
                            lambda: self._result_window.show_result(
                                previous_visible_result.text,
                                previous_result_target,
                            )
                        )
                self._show_failure(
                    _failure(
                        "ResultDisplayError",
                        "无法显示识别结果。",
                        "controller-result-display",
                    )
                )
        if isinstance(outcome, Empty):
            self._notify("未识别到文字。")
        elif isinstance(outcome, Failure):
            self._show_failure(outcome, public_message="识别失败。")

    def _restore_previous_result(self) -> None:
        self._visible_result_target = self._previous_result_target
        self._previous_result_target = None
        self._task_result_target = None
        if self._shutting_down or self._state.visible_result is None:
            return
        try:
            self._result_window.show_result(
                self._state.visible_result.text,
                self._visible_result_target,
            )
        except Exception:
            self._show_failure(
                _failure(
                    "ResultRestoreError",
                    "无法恢复上一次结果。",
                    "controller-result-restore",
                )
            )

    def _on_result_closed(self) -> None:
        self._state.clear_result()
        self._visible_result_target = None
        self._previous_result_target = None

    def show_settings(self) -> None:
        if (
            self._shutting_down
            or self._state.task_state is TaskState.CAPTURING
        ):
            return
        self._settings_window.set_settings(
            self._settings.hotkey,
            self._settings.autostart,
        )
        self._settings_window.set_model_status(self._state.model_state)
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _on_instance_command(self, command: str) -> None:
        if command == "open-settings":
            if self._shutting_down:
                return
            if self._state.task_state is TaskState.CAPTURING:
                self._settings_open_pending = True
                return
            self.show_settings()

    def _schedule_pending_settings_open(self) -> None:
        if (
            not self._settings_open_pending
            or self._settings_open_scheduled
            or self._shutting_down
            or self._state.task_state is TaskState.CAPTURING
        ):
            return
        self._settings_open_scheduled = True
        QTimer.singleShot(0, self._open_pending_settings)

    def _open_pending_settings(self) -> None:
        self._settings_open_scheduled = False
        if self._shutting_down:
            self._settings_open_pending = False
            return
        if self._state.task_state is TaskState.CAPTURING:
            return
        if not self._settings_open_pending:
            return
        self._settings_open_pending = False
        self.show_settings()

    def apply_settings_draft(self, draft: object) -> bool:
        if not isinstance(draft, SettingsDraft):
            self._settings_window.set_error("设置内容无效。")
            return False
        return self._commit_settings(
            draft,
            close_settings_window=True,
            error_in_settings=True,
        )

    def set_autostart_from_tray(self, enabled: bool) -> bool:
        if self._shutting_down:
            self._tray.set_autostart_checked(self._settings.autostart)
            return False
        draft = SettingsDraft(
            hotkey=self._settings.hotkey,
            autostart=bool(enabled),
        )
        committed = self._commit_settings(
            draft,
            close_settings_window=False,
            error_in_settings=False,
        )
        if not committed:
            self._tray.set_autostart_checked(self._settings.autostart)
        return committed

    def _commit_settings(
        self,
        draft: SettingsDraft,
        *,
        close_settings_window: bool,
        error_in_settings: bool,
    ) -> bool:
        old = self._settings
        try:
            new = Settings(
                hotkey=draft.hotkey,
                autostart=draft.autostart,
            )
        except (TypeError, ValueError):
            self._settings_window.set_error("设置内容无效。")
            return False

        hotkey_changed = new.hotkey != old.hotkey
        autostart_changed = (
            new.autostart != old.autostart or self._autostart_requires_sync
        )
        hotkey_applied = False
        autostart_attempted = False
        autostart_snapshot: str | None = None
        autostart_snapshot_taken = False
        failure: Exception | None = None
        try:
            if autostart_changed and self._autostart_requires_sync:
                autostart_snapshot = self._autostart_service.registered_command()
                autostart_snapshot_taken = True
            if hotkey_changed:
                self._hotkey_service.register(self._binding(new.hotkey))
                hotkey_applied = True
            if autostart_changed:
                autostart_attempted = True
                self._autostart_service.set_enabled(new.autostart)
            self._settings_writer(self._paths.settings_file, new)
        except Exception as exc:
            failure = exc

        if failure is not None:
            rollback_failed = False
            if autostart_attempted:
                if autostart_snapshot_taken:
                    rollback_failed |= not self._safe_call(
                        lambda: self._autostart_service.restore_registered_command(
                            autostart_snapshot
                        )
                    )
                else:
                    rollback_failed |= not self._safe_call(
                        lambda: self._autostart_service.set_enabled(old.autostart)
                    )
            if hotkey_applied:
                rollback_failed |= not self._safe_call(
                    lambda: self._hotkey_service.register(self._binding(old.hotkey))
                )
            self._tray.set_autostart_checked(old.autostart)
            self._settings_window.set_settings(old.hotkey, old.autostart)
            if rollback_failed:
                message = "保存失败，部分系统设置可能未恢复；配置文件保持原值。"
            elif (
                isinstance(failure, HotkeyRegistrationError)
                and failure.diagnostic_code == "HOTKEY-CONFLICT"
            ):
                message = "快捷键已被其他程序占用，请选择其他组合。"
            else:
                message = "无法保存设置；原设置保持不变。"
            if error_in_settings:
                self._settings_window.set_error(message)
            else:
                self._notify(message)
            return False

        self._settings = new
        self._settings_issue = None
        self._autostart_requires_sync = False
        self._tray.set_autostart_checked(new.autostart)
        if close_settings_window:
            self._settings_window.accept_saved(draft)
        else:
            self._settings_window.set_settings(new.hotkey, new.autostart)
        return True

    def _set_progress_waiting(self, waiting: bool) -> None:
        method_name = "set_waiting_for_model" if waiting else "set_recognizing"
        method = getattr(self._progress_window, method_name, None)
        if callable(method):
            method()
            return
        # Kept as a compatibility probe while presentation stays independent:
        # no controller code reaches into labels or other widget internals.
        phase_method = getattr(self._progress_window, "set_waiting", None)
        if callable(phase_method):
            phase_method(waiting)

    def _show_failure(
        self,
        failure: Failure,
        *,
        public_message: str | None = None,
    ) -> None:
        message = failure.public_message if public_message is None else public_message
        if self._error_window is None:
            self._notify(message)
            return
        try:
            diagnostic = diagnostic_from_failure(failure).render()
            self._error_window.show_error(message, diagnostic)
        except Exception:
            self._notify(message)

    def request_exit(self) -> None:
        """Cancel, wait for native inference, and never terminate the QThread."""

        if self._shutting_down:
            return
        record_runtime_event("controller.shutdown-requested")
        self._shutting_down = True
        self._settings_open_pending = False
        self._state.request_exit()
        if self._state.task_state is TaskState.CAPTURING:
            overlay = self._selection_overlay
            if overlay is not None:
                self._safe_call(overlay.close)
            if self._state.task_state is TaskState.CAPTURING:
                self._on_selection_cancelled()
        elif self._state.task_state is TaskState.RECOGNIZING:
            if self._active_task_id is None:
                self._finish_outcome(Cancelled())
            else:
                self._state.request_cancel()
                self._ocr_controller.cancel_active()

        self._safe_call(self._hotkey_service.unregister)
        self._safe_call(self._command_server.close)
        self._safe_call(self._tray.hide)
        self._safe_call(self._settings_window.hide)
        self._safe_call(self._result_window.hide)
        if self._error_window is not None:
            self._safe_call(self._error_window.close)
        self._shutdown_timer.start(10_000)
        self._ocr_controller.shutdown()

    def _offer_force_exit(self) -> None:
        if not self._shutting_down or self._shutdown_complete:
            return
        try:
            force = bool(self._force_exit_prompt())
        except Exception:
            force = False
        if not self._shutting_down or self._shutdown_complete:
            return
        if force:
            self._force_exit(1)
            return
        self._shutdown_timer.start(10_000)

    def _on_shutdown_finished(self, failure_result: object) -> None:
        if self._shutdown_complete:
            return
        if failure_result is not None:
            if not isinstance(failure_result, Failure):
                failure_result = _failure(
                    "OcrShutdownError",
                    "OCR 引擎未能安全退出。",
                    "controller-shutdown-result",
                )
            self._shutdown_failure = failure_result
        record_runtime_event(
            "controller.shutdown-finished",
            success=failure_result is None,
        )
        self._shutdown_complete = True
        self._shutdown_timer.stop()
        self._pending_bgr = None
        self._capture_frame = None
        self._selection_overlay = None
        self._active_task_id = None
        self._safe_call(self._progress_window.dismiss)
        if self._error_window is not None:
            self._safe_call(self._error_window.close)
        if self._native_event_filter is not None:
            self._safe_call(
                lambda: self._application.removeNativeEventFilter(
                    self._native_event_filter
                )
            )
            self._native_event_filter = None
        self._safe_call(self._hotkey_service.unregister)
        self._safe_call(self._command_server.close)
        self._application.quit()

    def wait_for_shutdown(self) -> bool:
        """Wait after Qt's event loop has already committed to exiting."""

        self.request_exit()
        waiter = getattr(self._ocr_controller, "wait_for_shutdown", None)
        if not callable(waiter):
            return False
        try:
            stopped_cleanly = bool(waiter())
        except Exception:
            return False
        try:
            worker_failure = getattr(
                self._ocr_controller,
                "shutdown_failure",
                None,
            )
        except Exception:
            worker_failure = _failure(
                "OcrShutdownError",
                "OCR 引擎未能安全退出。",
                "controller-shutdown-inspect",
            )
        if worker_failure is not None:
            if not isinstance(worker_failure, Failure):
                worker_failure = _failure(
                    "OcrShutdownError",
                    "OCR 引擎未能安全退出。",
                    "controller-shutdown-contract",
                )
            self._shutdown_failure = worker_failure
        return stopped_cleanly and self._shutdown_failure is None

    def _default_force_exit_prompt(self) -> bool:
        answer = QMessageBox.question(
            None,
            "TextSnap Layout",
            "识别引擎仍在退出。是否强制结束应用？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _tray_notice(self, message: str) -> None:
        self._tray.showMessage("TextSnap Layout", message)

    def _notify(self, message: str) -> None:
        if (
            not isinstance(message, str)
            or not message
            or len(message) > 240
            or any(character in message for character in ("\r", "\n"))
        ):
            message = "操作失败。"
        try:
            self._notifier(message)
        except Exception:
            pass

    @staticmethod
    def _binding(hotkey: Hotkey) -> HotkeyBinding:
        return HotkeyBinding(hotkey.modifiers, hotkey.key)

    @staticmethod
    def _hotkey_text(hotkey: Hotkey) -> str:
        return "+".join((*hotkey.modifiers, hotkey.key))

    @staticmethod
    def _safe_call(operation: Callable[[], object]) -> bool:
        try:
            operation()
        except Exception:
            return False
        return True


def build_application_controller(
    application: QApplication,
    paths: BundlePaths,
    settings: Settings,
    *,
    startup_autostart: bool = False,
    settings_issue: SettingsIssue | None = None,
) -> ApplicationController:
    """Construct production dependencies after the offline guard is installed."""

    model_specs = paths.model_specs()

    def engine_factory() -> OcrEngine:
        return OcrEngine(model_specs[0], model_specs[1])

    icon = load_packaged_ui_resources(paths)
    tray = TrayUi(icon)
    return ApplicationController(
        application=application,
        paths=paths,
        initial_settings=settings,
        hotkey_service=HotkeyService(),
        autostart_service=AutostartService(paths.executable),
        command_server=InstanceCommandServer(),
        ocr_controller=OcrThreadController(engine_factory),
        tray=tray,
        settings_window=SettingsWindow(),
        result_window=ResultWindow(),
        progress_window=ProgressWindow(),
        error_window=ErrorDialog(),
        startup_autostart=startup_autostart,
        settings_issue=settings_issue,
        parent=tray,
    )


def load_packaged_ui_resources(paths: BundlePaths) -> QIcon:
    """Register the bundled font and return the required non-null tray icon."""

    font_identifier = QFontDatabase.addApplicationFont(str(paths.font_file))
    if font_identifier < 0:
        raise ControllerStartupError("controller-font-load")
    families = tuple(QFontDatabase.applicationFontFamilies(font_identifier))
    if RESULT_FONT_FAMILY not in families:
        raise ControllerStartupError("controller-font-family")

    icon = QIcon(str(paths.root / APPLICATION_ICON_RELATIVE_PATH))
    if icon.isNull():
        raise ControllerStartupError("controller-icon-missing")
    return icon
