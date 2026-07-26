# ruff: noqa: E402
import sys

sys.dont_write_bytecode = True

"""Import-safe application bootstrap; Qt is loaded only after offline setup."""

from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
import os
from typing import Any

from .paths import BundlePaths
from .privacy import OfflineGuard
from .runtime_diagnostics import (
    record_runtime_event,
    start_runtime_diagnostics,
    stop_runtime_diagnostics,
)
from .settings import (
    SettingsLoadResult,
    check_data_directory_writable,
    load_settings,
)
from .windows.dpi import enable_per_monitor_v2
from .windows.instance import SingleInstanceMutex


_QT_PATH_VARIABLES = (
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QML_IMPORT_PATH",
    "QML2_IMPORT_PATH",
    "QT_QML_IMPORT_PATH",
    "QT_QML2_IMPORT_PATH",
)


@dataclass(frozen=True, slots=True)
class StartupOptions:
    autostart: bool = False


def parse_startup_arguments(arguments: Sequence[str]) -> StartupOptions:
    values = tuple(arguments)
    if not all(isinstance(value, str) for value in values):
        raise TypeError("startup arguments must be strings")
    if not values:
        return StartupOptions()
    if values == ("--autostart",):
        return StartupOptions(autostart=True)
    raise ValueError("unsupported startup arguments")


def clear_external_qt_paths(
    environment: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Remove environment-controlled Qt plugin and QML search paths."""

    selected = os.environ if environment is None else environment
    removed: list[str] = []
    for name in _QT_PATH_VARIABLES:
        if name in selected:
            selected.pop(name, None)
            removed.append(name)
    return tuple(removed)


def run_application(
    paths: BundlePaths,
    arguments: Sequence[str],
    *,
    guard_factory: Callable[..., object] = OfflineGuard,
    mutex_factory: Callable[[], object] = SingleInstanceMutex,
    dpi_enabler: Callable[[], None] = enable_per_monitor_v2,
    data_directory_writable: Callable[[object], bool] = (check_data_directory_writable),
    settings_loader: Callable[[object], SettingsLoadResult] = load_settings,
    primary_runner: Callable[
        [BundlePaths, SettingsLoadResult, bool, tuple[str, ...]], int
    ]
    | None = None,
    secondary_sender: Callable[[], bool] | None = None,
    fatal_notifier: Callable[[str], None] | None = None,
    environment: MutableMapping[str, str] | None = None,
) -> int:
    """Run one primary instance or notify it from a bounded secondary."""

    sys.dont_write_bytecode = True
    notify_fatal = fatal_notifier or _show_fatal_error
    diagnostics_enabled = start_runtime_diagnostics(paths.root, environment)
    record_runtime_event(
        "startup.begin",
        arguments_count=len(tuple(arguments)),
        diagnostics_enabled=diagnostics_enabled,
    )
    try:
        removed_qt_paths = clear_external_qt_paths(environment)
    except Exception as exc:
        record_runtime_event(
            "startup.qt-path-cleanup-failed",
            exception_type=type(exc).__name__,
        )
        stop_runtime_diagnostics()
        raise
    record_runtime_event(
        "startup.qt-paths-cleared",
        count=len(removed_qt_paths),
    )
    try:
        options = parse_startup_arguments(arguments)
    except (TypeError, ValueError) as exc:
        record_runtime_event(
            "startup.arguments-invalid",
            exception_type=type(exc).__name__,
        )
        _safe_notify(notify_fatal, "启动参数无效。")
        record_runtime_event("process.exit", exit_code=2)
        stop_runtime_diagnostics()
        return 2
    record_runtime_event("startup.arguments-ready", autostart=options.autostart)

    selected_runner = primary_runner or _run_primary_qt
    selected_sender = secondary_sender or _send_open_settings
    guard: object | None = None
    mutex: object | None = None
    exit_code = 1
    cleanup_failed = False
    try:
        record_runtime_event("startup.mutex-create")
        mutex = mutex_factory()
        primary = bool(mutex.acquire())
        record_runtime_event("startup.mutex-acquired", primary=primary)

        if not primary and options.autostart:
            # Mutex ownership is authoritative. A logon duplicate exits without
            # importing Qt or touching the OCR/offline runtime.
            record_runtime_event("startup.duplicate-autostart-exit")
            exit_code = 0
        else:
            record_runtime_event("startup.offline-guard-install")
            guard = guard_factory(
                cache_home=paths.paddlex_cache,
                font_file=paths.font_file,
            )
            guard.install()
            record_runtime_event("startup.offline-guard-ready")

        if not primary and not options.autostart:
            # The sender imports QtCore, so DPI configuration still happens
            # before any possible QApplication in the secondary process.
            record_runtime_event("startup.secondary-dpi")
            dpi_enabler()
            record_runtime_event("startup.secondary-send")
            delivered = bool(selected_sender())
            record_runtime_event(
                "startup.secondary-complete",
                delivered=delivered,
            )
            if not delivered:
                _safe_notify(
                    notify_fatal,
                    "主实例正在启动，无法打开设置窗口。",
                )
            exit_code = 0 if delivered else 1
        elif primary and not data_directory_writable(paths.data_directory):
            record_runtime_event("startup.data-directory-unwritable")
            _safe_notify(
                notify_fatal,
                "程序数据目录不可写，请将完整程序目录移动到当前用户可写位置。",
            )
            exit_code = 1
        elif primary:
            record_runtime_event("startup.primary-dpi")
            dpi_enabler()
            record_runtime_event("startup.settings-load")
            loaded = settings_loader(paths.settings_file)
            record_runtime_event(
                "startup.settings-ready",
                issue=loaded.issue is not None,
            )
            record_runtime_event("startup.primary-runner-enter")
            result = selected_runner(
                paths,
                loaded,
                options.autostart,
                tuple(arguments),
            )
            if not isinstance(result, int) or isinstance(result, bool):
                raise TypeError("primary runner must return an integer")
            exit_code = result
            record_runtime_event(
                "startup.primary-runner-exit",
                exit_code=exit_code,
            )
    except Exception as exc:
        record_runtime_event(
            "startup.failed",
            exception_type=type(exc).__name__,
        )
        _safe_notify(notify_fatal, "应用启动失败。")
        exit_code = 1
    finally:
        if mutex is not None:
            try:
                mutex.close()
                record_runtime_event("cleanup.mutex-closed")
            except Exception as exc:
                record_runtime_event(
                    "cleanup.mutex-close-failed",
                    exception_type=type(exc).__name__,
                )
                cleanup_failed = True
        if guard is not None:
            try:
                guard.restore()
                record_runtime_event("cleanup.offline-guard-restored")
            except Exception as exc:
                record_runtime_event(
                    "cleanup.offline-guard-restore-failed",
                    exception_type=type(exc).__name__,
                )
                cleanup_failed = True

    if cleanup_failed:
        _safe_notify(notify_fatal, "应用资源未能正常释放。")
        if exit_code == 0:
            exit_code = 1
    record_runtime_event(
        "process.exit",
        exit_code=exit_code,
        cleanup_failed=cleanup_failed,
    )
    stop_runtime_diagnostics()
    return exit_code


def _run_primary_qt(
    paths: BundlePaths,
    loaded: SettingsLoadResult,
    startup_autostart: bool,
    arguments: tuple[str, ...],
) -> int:
    """Import and run Qt only after :class:`OfflineGuard` is active."""

    record_runtime_event("qt.import-start")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .controller import build_application_controller

    record_runtime_event("qt.import-complete")
    application = QApplication.instance()
    if application is None:
        application = QApplication([str(paths.executable), *arguments])
        record_runtime_event("qt.application-created")
    else:
        record_runtime_event("qt.application-reused")
    application.setQuitOnLastWindowClosed(False)
    try:
        record_runtime_event("qt.controller-build-start")
        controller = build_application_controller(
            application,
            paths,
            loaded.settings,
            startup_autostart=startup_autostart,
            settings_issue=loaded.issue,
        )
        record_runtime_event("qt.controller-build-complete")
        controller.start()
        record_runtime_event("qt.controller-start-complete")
    except Exception as exc:
        record_runtime_event(
            "qt.controller-start-failed",
            exception_type=type(exc).__name__,
        )
        QMessageBox.critical(
            None,
            "TextSnap Layout",
            "应用常驻服务启动失败。",
        )
        return 1
    application.aboutToQuit.connect(controller.request_exit)
    record_runtime_event("qt.event-loop-enter")
    exit_code = int(application.exec())
    record_runtime_event("qt.event-loop-exit", exit_code=exit_code)
    shutdown_clean = controller.wait_for_shutdown()
    record_runtime_event("qt.shutdown-complete", clean=shutdown_clean)
    if not shutdown_clean:
        QMessageBox.critical(
            None,
            "TextSnap Layout",
            "OCR 引擎未能安全退出。",
        )
        return 1
    return exit_code


def _send_open_settings() -> bool:
    """Use a short-lived QtCore application for bounded local delivery."""

    record_runtime_event("qt.secondary-import-start")
    from PySide6.QtCore import QCoreApplication

    from .qt_instance import send_instance_command

    record_runtime_event("qt.secondary-import-complete")
    application = QCoreApplication.instance()
    if application is None:
        application = QCoreApplication(["TextSnapLayout-secondary"])
    delivered = bool(send_instance_command(attempts=20, timeout_ms=100))
    record_runtime_event("qt.secondary-delivery", delivered=delivered)
    return delivered


def _show_fatal_error(message: str) -> None:
    safe_message = _safe_message(message)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            message_box = user32.MessageBoxW
            message_box.argtypes = [
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.UINT,
            ]
            message_box.restype = ctypes.c_int
            # MB_OK | MB_ICONERROR
            message_box(None, safe_message, "TextSnap Layout", 0x00000010)
            return
        except Exception:
            pass
    sys.stderr.write(f"TextSnap Layout: {safe_message}\n")


def _safe_notify(notifier: Callable[[str], None], message: str) -> None:
    try:
        notifier(_safe_message(message))
    except Exception:
        pass


def _safe_message(message: Any) -> str:
    if (
        not isinstance(message, str)
        or not message
        or len(message) > 240
        or any(character in message for character in ("\r", "\n"))
    ):
        return "应用启动失败。"
    return message
