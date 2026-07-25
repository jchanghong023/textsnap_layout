from __future__ import annotations

import os
from pathlib import Path
import platform
import tempfile
import unittest

from textsnap.domain import Success
from textsnap.ocr import OcrEngine
from textsnap.paths import BundlePaths
from textsnap.privacy import OfflineGuard


def _snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
            if path.is_file()
        )
    )


@unittest.skipUnless(
    os.environ.get("TEXTSNAP_RUN_REAL_OCR") == "1",
    "set TEXTSNAP_RUN_REAL_OCR=1 with the pinned native runtime and models",
)
class RealOcrRegressionTests(unittest.TestCase):
    def test_fixed_high_contrast_sample_offline(self) -> None:
        model_root_value = os.environ.get("TEXTSNAP_TEST_MODEL_ROOT")
        font_value = os.environ.get("TEXTSNAP_TEST_FONT")
        if not model_root_value or not font_value:
            self.fail("TEXTSNAP_TEST_MODEL_ROOT and TEXTSNAP_TEST_FONT are required")

        model_root = Path(model_root_value).resolve(strict=True)
        font = Path(font_value).resolve(strict=True)
        bundle = BundlePaths(
            Path(tempfile.gettempdir()).resolve() / "integration-bundle"
        )
        detection_spec, recognition_spec = bundle.model_specs()
        detection_spec = type(detection_spec)(
            detection_spec.model_name,
            model_root / detection_spec.model_name,
            detection_spec.files_sha256,
        )
        recognition_spec = type(recognition_spec)(
            recognition_spec.model_name,
            model_root / recognition_spec.model_name,
            recognition_spec.files_sha256,
        )

        engine_config: dict[str, object] = {}
        if platform.machine().lower() in {"aarch64", "arm64"}:
            if os.environ.get("TEXTSNAP_ALLOW_ARM_COMPAT") != "1":
                self.fail(
                    "ARM cannot provide target MKL-DNN/10-thread equivalence; "
                    "set TEXTSNAP_ALLOW_ARM_COMPAT=1 for the explicit paddle/1 smoke test"
                )
            engine_config = {"run_mode": "paddle", "cpu_threads": 1}

        before_models = _snapshot(model_root)
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "pdx-cache"
            (cache / "temp").mkdir(parents=True)
            before_cache = _snapshot(cache)
            with OfflineGuard(cache_home=cache, font_file=font):
                engine = OcrEngine(
                    detection_spec,
                    recognition_spec,
                    engine_config=engine_config,
                )
                self.assertIsNone(engine.initialize())
                try:
                    import cv2
                    import numpy

                    image = numpy.full((240, 1000, 3), 255, numpy.uint8)
                    cv2.putText(
                        image,
                        "TextSnap Layout OCR 2026",
                        (30, 90),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.6,
                        (0, 0, 0),
                        3,
                        cv2.LINE_AA,
                    )
                    cv2.putText(
                        image,
                        "offline 123 ABC xyz",
                        (30, 180),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.4,
                        (0, 0, 0),
                        3,
                        cv2.LINE_AA,
                    )
                    outcome = engine.recognize(image)
                    self.assertIsInstance(outcome, Success)
                    self.assertEqual(
                        outcome.result.text,
                        "TextSnap Layout OCR 2026\noffline 123 ABC xyz",
                    )
                finally:
                    self.assertIsNone(engine.close())
            self.assertEqual(_snapshot(cache), before_cache)

        self.assertEqual(_snapshot(model_root), before_models)


if __name__ == "__main__":
    unittest.main()
