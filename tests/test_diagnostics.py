from __future__ import annotations

import unittest

from textsnap.diagnostics import diagnostic_from_exception, diagnostic_from_failure
from textsnap.domain import Failure


class DiagnosticsTests(unittest.TestCase):
    def test_failure_diagnostic_omits_public_message_content(self) -> None:
        rendered = diagnostic_from_failure(
            Failure(
                "OcrInferenceError",
                "private OCR content /Users/alice/capture.png",
                "ocr-inference-failed",
            )
        ).render()
        self.assertIn("OcrInferenceError", rendered)
        self.assertIn("ocr-inference-failed", rendered)
        self.assertNotIn("private OCR", rendered)
        self.assertNotIn("alice", rendered)

    def test_exception_message_and_user_path_are_never_rendered(self) -> None:
        secret = "/Users/alice/private/screenshot.png OCR_SECRET"
        try:
            raise RuntimeError(secret)
        except RuntimeError as error:
            diagnostic = diagnostic_from_exception(
                error,
                diagnostic_code="ocr-inference-failed",
            )
        rendered = diagnostic.render()
        self.assertNotIn("alice", rendered)
        self.assertNotIn("OCR_SECRET", rendered)
        self.assertNotIn("screenshot", rendered)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("ocr-inference-failed", rendered)

    def test_unsafe_diagnostic_code_is_replaced(self) -> None:
        error = ValueError("not shown")
        diagnostic = diagnostic_from_exception(
            error,
            diagnostic_code="/private/path\nsecret",
        )
        self.assertEqual(diagnostic.diagnostic_code, "internal-error")

    def test_negative_frame_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            diagnostic_from_exception(
                RuntimeError(),
                diagnostic_code="test",
                maximum_frames=-1,
            )


if __name__ == "__main__":
    unittest.main()
