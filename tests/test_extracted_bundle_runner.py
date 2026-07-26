from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


RUNNER_PATH = (
    Path(__file__).resolve().parent / "windows" / "verify_extracted_bundle.py"
)
SPEC = importlib.util.spec_from_file_location(
    "textsnap_extracted_bundle_runner",
    RUNNER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ExtractedBundleRunnerHelperTests(unittest.TestCase):
    def test_snapshot_records_relative_content_and_detects_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty").mkdir()
            content = root / "中文 空格" / "file.bin"
            content.parent.mkdir()
            content.write_bytes(b"first")

            before = runner._snapshot_tree(root)
            content.write_bytes(b"second")
            after = runner._snapshot_tree(root)

            self.assertNotEqual(before, after)
            self.assertIn(("empty", "directory", 0, ""), before)
            self.assertTrue(
                any(entry[0] == "中文 空格/file.bin" for entry in before)
            )
            self.assertNotIn(str(root), repr(before))

    def test_atomic_json_replaces_hardlink_without_changing_victim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim"
            result = root / "result.json"
            victim.write_bytes(b"private")
            try:
                os.link(victim, result)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {type(exc).__name__}")

            runner._write_json_atomic(result, {"ok": True, "value": 1})

            self.assertEqual(victim.read_bytes(), b"private")
            self.assertFalse(result.is_symlink())
            self.assertFalse(os.path.samefile(victim, result))
            self.assertEqual(
                result.read_bytes(),
                b'{"ok":true,"value":1}\n',
            )

    def test_failure_document_never_includes_exception_message_or_paths(self) -> None:
        marker = r"C:\Users\private\capture.png OCR-CONTENT"
        error = RuntimeError(marker)

        document = runner._result_document(
            ok=False,
            checks={"runtime": True},
            phase="acceptance",
            error=error,
            cleanup_ok=True,
        )
        encoded = json.dumps(document, sort_keys=True)

        self.assertNotIn(marker, encoded)
        self.assertNotIn("OCR-CONTENT", encoded)
        self.assertEqual(
            document["failure"],
            {"type": "RuntimeError", "code": "acceptance-failed"},
        )

    def test_public_result_schema_rejects_every_unapproved_string_field(self) -> None:
        valid = runner._result_document(
            ok=True,
            checks={"runtime": True},
            phase="complete",
            error=None,
            cleanup_ok=True,
        )
        for unsafe in (
            runner._CONTROLLED_TEXT,
            "SECRET CUSTOMER OCR TEXT",
            r"C:\private\file.txt",
            "C:private.txt",
            "/private/file.txt",
            "capture.png",
            "line one\nline two",
        ):
            with self.subTest(unsafe=unsafe):
                document = {**valid, "checks": {"runtime": unsafe}}
                with self.assertRaises(runner.AcceptanceError):
                    runner._assert_public_result(document)

        unknown = {**valid, "checks": {"debug": "SECRETCUSTOMEROCRTEXT"}}
        with self.assertRaises(runner.AcceptanceError):
            runner._assert_public_result(unknown)

    def test_manifest_versions_require_exact_locked_closure(self) -> None:
        wheels = [
            {"name": name, "version": version}
            for name, version in runner._EXPECTED_VERSIONS.items()
        ]
        manifest = {"wheels": wheels}

        versions = runner._locked_versions_from_manifest(manifest)

        self.assertEqual(len(versions), runner._EXPECTED_WHEEL_COUNT)
        self.assertEqual(versions["numpy"], "2.2.6")
        broken_wheels = [dict(wheel) for wheel in wheels]
        broken_wheels[0]["version"] = "0.0"
        broken = {"wheels": broken_wheels}
        with self.assertRaises(runner.AcceptanceError):
            runner._locked_versions_from_manifest(broken)

    def test_runtime_configuration_requires_exact_pth_and_isolation_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "TextSnapLayout"
            runtime = bundle / "runtime"
            runtime.mkdir(parents=True)
            pth = runtime / "python313._pth"
            pth.write_bytes(runner._RUNTIME_PTH)

            runner._verify_runtime_pth(bundle)

            pth.write_bytes(runner._RUNTIME_PTH + b"import site\n")
            with self.assertRaises(runner.AcceptanceError):
                runner._verify_runtime_pth(bundle)

        isolated = SimpleNamespace(
            isolated=1,
            dont_write_bytecode=1,
            no_site=1,
            no_user_site=1,
            ignore_environment=1,
            safe_path=True,
        )
        runner._verify_runtime_flags(isolated)
        for field in vars(isolated):
            with self.subTest(field=field):
                values = vars(isolated) | {field: False}
                with self.assertRaises(runner.AcceptanceError):
                    runner._verify_runtime_flags(SimpleNamespace(**values))

    def test_invocation_requires_package_python_and_external_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "中文 空格" / "TextSnapLayout"
            runtime = bundle / "runtime"
            runtime.mkdir(parents=True)
            executable = runtime / "pythonw.exe"
            executable.write_bytes(b"fixture")
            outside = root / "result.json"

            selected_bundle, selected_result = runner._validate_invocation_paths(
                str(bundle),
                str(outside),
                executable,
            )

            self.assertEqual(selected_bundle, bundle.resolve())
            self.assertEqual(selected_result, outside.absolute())
            with self.assertRaises(runner.AcceptanceError):
                runner._validate_invocation_paths(
                    str(bundle),
                    str(bundle / "result.json"),
                    executable,
                )
            wrong = root / "pythonw.exe"
            wrong.write_bytes(b"fixture")
            with self.assertRaises(runner.AcceptanceError):
                runner._validate_invocation_paths(
                    str(bundle),
                    str(outside),
                    wrong,
                )

    def test_main_does_not_write_when_result_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "TextSnapLayout"
            runtime = bundle / "runtime"
            runtime.mkdir(parents=True)
            executable = runtime / "pythonw.exe"
            executable.write_bytes(b"fixture")
            rejected_result = bundle / "result.json"

            with mock.patch.object(runner.sys, "executable", str(executable)):
                exit_code = runner.main(
                    [
                        "--bundle",
                        str(bundle),
                        "--result",
                        str(rejected_result),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertFalse(rejected_result.exists())

    def test_main_writes_only_sanitized_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "TextSnapLayout"
            runtime = bundle / "runtime"
            runtime.mkdir(parents=True)
            executable = runtime / "pythonw.exe"
            executable.write_bytes(b"fixture")
            result = root / "result.json"
            marker = r"C:\Users\private\capture.png OCR-CONTENT"

            with (
                mock.patch.object(runner.sys, "executable", str(executable)),
                mock.patch.object(
                    runner,
                    "_verify_runtime",
                    side_effect=RuntimeError(marker),
                ),
            ):
                exit_code = runner.main(
                    [
                        "--bundle",
                        str(bundle),
                        "--result",
                        str(result),
                    ]
                )

            document = json.loads(result.read_text(encoding="utf-8"))
            encoded = json.dumps(document, sort_keys=True)
            self.assertEqual(exit_code, 1)
            self.assertNotIn(marker, encoded)
            self.assertNotIn("OCR-CONTENT", encoded)
            self.assertEqual(
                document["failure"],
                {"type": "RuntimeError", "code": "runtime-failed"},
            )

    def test_cleanup_retains_controller_while_worker_is_stuck(self) -> None:
        class StuckController:
            running = True
            shutdown_failure = None

            def shutdown(self) -> None:
                pass

            def wait_for_shutdown(self) -> bool:
                raise AssertionError("live worker must not be released")

            def deleteLater(self) -> None:
                raise AssertionError("live worker must not be deleted")

        controller = StuckController()
        resources = runner._Resources()
        resources.controller = controller
        resources.qt_wait = lambda _predicate, _timeout: False

        cleanup_ok = runner._cleanup_resources(resources)

        self.assertFalse(cleanup_ok)
        self.assertTrue(resources.worker_stuck)
        self.assertIs(resources.controller, controller)

    def test_stuck_worker_force_exit_runs_even_if_result_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.json"

            for failing_function in ("_result_document", "_write_json_atomic"):
                with (
                    self.subTest(failing_function=failing_function),
                    mock.patch.object(
                        runner,
                        failing_function,
                        side_effect=OSError("private path"),
                    ),
                    mock.patch.object(
                        runner.os,
                        "_exit",
                        side_effect=SystemExit(1),
                    ) as forced_exit,
                ):
                    with self.assertRaisesRegex(SystemExit, "1"):
                        runner._write_stuck_result_and_exit(
                            result,
                            {"runtime": True},
                        )

                forced_exit.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
