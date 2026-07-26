from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from textsnap.bootstrap import (
    StartupOptions,
    clear_external_qt_paths,
    parse_startup_arguments,
    run_application,
)
from textsnap.paths import BundlePaths
from textsnap.runtime_diagnostics import DIAGNOSTIC_LOG_ENVIRONMENT
from textsnap.settings import DEFAULT_SETTINGS, SettingsLoadResult


class _Guard:
    def __init__(self, events: list[object], **resources: object) -> None:
        self.events = events
        self.events.append(("guard-init", resources))

    def install(self) -> None:
        self.events.append("guard-install")

    def restore(self) -> None:
        self.events.append("guard-restore")


class _Mutex:
    def __init__(self, events: list[object], primary: bool) -> None:
        self.events = events
        self.primary = primary
        self.events.append("mutex-init")

    def acquire(self) -> bool:
        self.events.append("mutex-acquire")
        return self.primary

    def close(self) -> None:
        self.events.append("mutex-close")


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.paths = BundlePaths(self.root)
        self.loaded = SettingsLoadResult(DEFAULT_SETTINGS, None, False)

    def test_argument_parser_is_exact(self) -> None:
        self.assertEqual(parse_startup_arguments(()), StartupOptions(False))
        self.assertEqual(
            parse_startup_arguments(("--autostart",)),
            StartupOptions(True),
        )
        for arguments in (
            ("--autostart", "--autostart"),
            ("--unknown",),
            (" --autostart",),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    parse_startup_arguments(arguments)

    def test_external_qt_paths_are_removed_without_changing_platform_choice(
        self,
    ) -> None:
        environment = {
            "QT_PLUGIN_PATH": "/external/plugins",
            "QML2_IMPORT_PATH": "/external/qml",
            "QT_QPA_PLATFORM": "offscreen",
            "PATH": "/usr/bin",
        }

        removed = clear_external_qt_paths(environment)

        self.assertEqual(
            set(removed),
            {"QT_PLUGIN_PATH", "QML2_IMPORT_PATH"},
        )
        self.assertEqual(
            environment,
            {
                "QT_QPA_PLATFORM": "offscreen",
                "PATH": "/usr/bin",
            },
        )

    def test_primary_order_installs_guard_before_dpi_and_runner(self) -> None:
        events: list[object] = []
        environment = {
            "QT_PLUGIN_PATH": "/unsafe",
            "QML_IMPORT_PATH": "/unsafe",
        }

        def guard_factory(**resources: object) -> _Guard:
            return _Guard(events, **resources)

        def mutex_factory() -> _Mutex:
            return _Mutex(events, True)

        def writable(path: object) -> bool:
            events.append(("writable", path))
            return True

        def dpi() -> None:
            events.append("dpi")

        def loader(path: object) -> SettingsLoadResult:
            events.append(("settings-load", path))
            return self.loaded

        def runner(
            paths: BundlePaths,
            loaded: SettingsLoadResult,
            autostart: bool,
            arguments: tuple[str, ...],
        ) -> int:
            events.append(("runner", paths, loaded, autostart, arguments))
            return 7

        result = run_application(
            self.paths,
            (),
            guard_factory=guard_factory,
            mutex_factory=mutex_factory,
            dpi_enabler=dpi,
            data_directory_writable=writable,
            settings_loader=loader,
            primary_runner=runner,
            secondary_sender=lambda: self.fail("secondary sender called"),
            fatal_notifier=lambda message: self.fail(message),
            environment=environment,
        )

        self.assertEqual(result, 7)
        names = [event[0] if isinstance(event, tuple) else event for event in events]
        self.assertEqual(
            names,
            [
                "mutex-init",
                "mutex-acquire",
                "guard-init",
                "guard-install",
                "writable",
                "dpi",
                "settings-load",
                "runner",
                "mutex-close",
                "guard-restore",
            ],
        )
        self.assertLess(names.index("guard-install"), names.index("dpi"))
        self.assertLess(names.index("dpi"), names.index("runner"))
        self.assertEqual(environment, {})

    def test_autostart_secondary_exits_without_qt_or_notification(self) -> None:
        events: list[object] = []

        result = run_application(
            self.paths,
            ("--autostart",),
            guard_factory=lambda **resources: _Guard(events, **resources),
            mutex_factory=lambda: _Mutex(events, False),
            dpi_enabler=lambda: self.fail("DPI called"),
            data_directory_writable=lambda path: self.fail("writability checked"),
            settings_loader=lambda path: self.fail("settings loaded"),
            primary_runner=lambda *args: self.fail("primary runner called"),
            secondary_sender=lambda: self.fail("secondary sender called"),
            fatal_notifier=lambda message: self.fail(message),
            environment={},
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [event[0] if isinstance(event, tuple) else event for event in events],
            [
                "mutex-init",
                "mutex-acquire",
                "mutex-close",
            ],
        )

    def test_interactive_secondary_enables_dpi_then_sends_once(self) -> None:
        events: list[object] = []

        result = run_application(
            self.paths,
            (),
            guard_factory=lambda **resources: _Guard(events, **resources),
            mutex_factory=lambda: _Mutex(events, False),
            dpi_enabler=lambda: events.append("dpi"),
            data_directory_writable=lambda path: self.fail("writability checked"),
            settings_loader=lambda path: self.fail("settings loaded"),
            primary_runner=lambda *args: self.fail("primary runner called"),
            secondary_sender=lambda: events.append("send") or True,
            fatal_notifier=lambda message: self.fail(message),
            environment={},
        )

        self.assertEqual(result, 0)
        self.assertLess(events.index("guard-install"), events.index("dpi"))
        self.assertLess(events.index("dpi"), events.index("send"))

    def test_unwritable_primary_reports_fixed_error_before_qt(self) -> None:
        events: list[object] = []
        messages: list[str] = []

        result = run_application(
            self.paths,
            (),
            guard_factory=lambda **resources: _Guard(events, **resources),
            mutex_factory=lambda: _Mutex(events, True),
            dpi_enabler=lambda: self.fail("DPI called"),
            data_directory_writable=lambda path: False,
            settings_loader=lambda path: self.fail("settings loaded"),
            primary_runner=lambda *args: self.fail("primary runner called"),
            fatal_notifier=messages.append,
            environment={},
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(messages), 1)
        self.assertIn("不可写", messages[0])
        self.assertNotIn(str(self.root), messages[0])
        self.assertIn("mutex-close", events)
        self.assertIn("guard-restore", events)

    def test_invalid_arguments_do_not_install_guard(self) -> None:
        messages: list[str] = []

        result = run_application(
            self.paths,
            ("--bad",),
            guard_factory=lambda **resources: self.fail("guard created"),
            fatal_notifier=messages.append,
            environment={},
        )

        self.assertEqual(result, 2)
        self.assertEqual(messages, ["启动参数无效。"])

    def test_explicit_external_diagnostic_log_records_startup_lifecycle(
        self,
    ) -> None:
        log_directory = tempfile.TemporaryDirectory()
        self.addCleanup(log_directory.cleanup)
        destination = Path(log_directory.name) / "runtime.jsonl"
        events: list[object] = []

        result = run_application(
            self.paths,
            (),
            guard_factory=lambda **resources: _Guard(events, **resources),
            mutex_factory=lambda: _Mutex(events, True),
            dpi_enabler=lambda: None,
            data_directory_writable=lambda path: True,
            settings_loader=lambda path: self.loaded,
            primary_runner=lambda *args: 0,
            secondary_sender=lambda: self.fail("secondary sender called"),
            fatal_notifier=lambda message: self.fail(message),
            environment={DIAGNOSTIC_LOG_ENVIRONMENT: str(destination)},
        )

        self.assertEqual(result, 0)
        documents = [
            json.loads(line)
            for line in destination.read_text(encoding="utf-8").splitlines()
        ]
        logged_events = [document["event"] for document in documents]
        self.assertEqual(logged_events[0], "process.start")
        self.assertIn("startup.mutex-acquired", logged_events)
        self.assertIn("startup.offline-guard-ready", logged_events)
        self.assertIn("startup.primary-runner-enter", logged_events)
        self.assertIn("startup.primary-runner-exit", logged_events)
        self.assertEqual(logged_events[-1], "process.exit")
        self.assertEqual(documents[-1]["exit_code"], 0)

    def test_isolated_bootstrap_sets_no_bytecode_before_third_party_imports(
        self,
    ) -> None:
        script = (
            "import sys;"
            "sys.dont_write_bytecode=False;"
            "import textsnap.bootstrap;"
            "assert sys.dont_write_bytecode;"
            "assert 'PySide6' not in sys.modules;"
            "assert 'numpy' not in sys.modules;"
            "assert 'paddle' not in sys.modules"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_packaged_main_passes_explicit_bundle_and_arguments(self) -> None:
        import textsnap.main as main_module

        with mock.patch.object(
            main_module,
            "run_application",
            return_value=9,
        ) as runner:
            result = main_module.main(
                self.paths,
                ("--autostart",),
            )

        self.assertEqual(result, 9)
        runner.assert_called_once_with(self.paths, ("--autostart",))

    def test_app_shim_locates_paths_from_its_own_file(self) -> None:
        import importlib.util

        entry = Path(__file__).resolve().parents[1] / "app" / "main.py"
        specification = importlib.util.spec_from_file_location(
            "textsnap_test_app_main",
            entry,
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)

        with (
            mock.patch.object(
                module.BundlePaths,
                "from_entry_script",
                return_value=self.paths,
            ) as locator,
            mock.patch.object(module, "run_main", return_value=11) as runner,
            mock.patch.object(
                sys,
                "argv",
                [str(entry), "--autostart"],
            ),
        ):
            result = module.main()

        self.assertEqual(result, 11)
        locator.assert_called_once_with(str(entry))
        runner.assert_called_once_with(self.paths, ("--autostart",))

    def test_staged_source_main_executes_entry_block(self) -> None:
        import runpy

        source = Path(__file__).resolve().parents[1] / "src" / "textsnap" / "main.py"
        staged_entry = self.root / "app" / "main.py"
        staged_entry.parent.mkdir()
        shutil.copyfile(source, staged_entry)
        with (
            mock.patch(
                "textsnap.bootstrap.run_application",
                return_value=13,
            ) as runner,
            mock.patch(
                "textsnap.paths.BundlePaths.from_entry_script",
                return_value=self.paths,
            ) as locator,
            mock.patch.object(
                sys,
                "argv",
                [str(staged_entry), "--autostart"],
            ),
        ):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(staged_entry), run_name="__main__")

        self.assertEqual(raised.exception.code, 13)
        locator.assert_called_once_with(str(staged_entry))
        runner.assert_called_once_with(self.paths, ("--autostart",))


if __name__ == "__main__":
    unittest.main()
