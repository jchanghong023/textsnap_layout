from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest

from textsnap.runtime_diagnostics import (
    DIAGNOSTIC_LOG_ENVIRONMENT,
    record_runtime_event,
    runtime_diagnostics_active,
    start_runtime_diagnostics,
    stop_runtime_diagnostics,
)


class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        stop_runtime_diagnostics()
        self.addCleanup(stop_runtime_diagnostics)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()

    def test_logging_is_disabled_without_explicit_environment_value(self) -> None:
        self.assertFalse(start_runtime_diagnostics(self.bundle, {}))
        self.assertFalse(runtime_diagnostics_active())

    def test_destination_must_be_absolute_and_outside_bundle(self) -> None:
        for destination in (
            "relative.jsonl",
            str(self.bundle / "diagnostic.jsonl"),
        ):
            with self.subTest(destination=destination):
                self.assertFalse(
                    start_runtime_diagnostics(
                        self.bundle,
                        {DIAGNOSTIC_LOG_ENVIRONMENT: destination},
                    )
                )
                self.assertFalse(runtime_diagnostics_active())

    def test_json_lines_are_detailed_but_redact_content_and_paths(self) -> None:
        destination = self.root / "diagnostic.jsonl"
        self.assertTrue(
            start_runtime_diagnostics(
                self.bundle,
                {DIAGNOSTIC_LOG_ENVIRONMENT: str(destination)},
            )
        )

        record_runtime_event(
            "ocr.model-ready",
            duration_ms=1825.4321,
            success=True,
            diagnostic_code="ocr-model-ready",
            unsafe=r"C:\Users\someone\captured secret text",
        )
        stop_runtime_diagnostics()

        lines = [
            json.loads(line)
            for line in destination.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [line["event"] for line in lines],
            ["process.start", "ocr.model-ready"],
        )
        for line in lines:
            self.assertEqual(line["pid"], lines[0]["pid"])
            self.assertIsInstance(line["thread_id"], int)
            self.assertIsInstance(line["elapsed_ms"], float)
            self.assertTrue(line["timestamp_utc"].endswith("Z"))
        self.assertEqual(lines[1]["duration_ms"], 1825.432)
        self.assertTrue(lines[1]["success"])
        self.assertEqual(lines[1]["diagnostic_code"], "ocr-model-ready")
        self.assertEqual(lines[1]["unsafe"], "redacted")
        serialized = destination.read_text(encoding="utf-8")
        self.assertNotIn("someone", serialized)
        self.assertNotIn("captured secret text", serialized)
        self.assertNotIn(str(self.bundle), serialized)

    def test_worker_thread_events_are_complete_json_objects(self) -> None:
        destination = self.root / "threaded.jsonl"
        self.assertTrue(
            start_runtime_diagnostics(
                self.bundle,
                {DIAGNOSTIC_LOG_ENVIRONMENT: str(destination)},
            )
        )

        threads = [
            threading.Thread(
                target=record_runtime_event,
                args=("ocr.task-complete",),
                kwargs={"task_id": index, "outcome_type": "Success"},
            )
            for index in range(1, 17)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stop_runtime_diagnostics()

        lines = destination.read_text(encoding="utf-8").splitlines()
        documents = [json.loads(line) for line in lines]
        task_ids = sorted(
            document["task_id"]
            for document in documents
            if document["event"] == "ocr.task-complete"
        )
        self.assertEqual(task_ids, list(range(1, 17)))


if __name__ == "__main__":
    unittest.main()
