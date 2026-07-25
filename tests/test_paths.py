from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from textsnap.ocr import DETECTION_MODEL_NAME, RECOGNITION_MODEL_NAME
from textsnap.paths import (
    BundlePaths,
    DETECTION_MODEL_HASHES,
    RECOGNITION_MODEL_HASHES,
)


class BundlePathTests(unittest.TestCase):
    def test_entry_script_resolves_bundle_without_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = BundlePaths.from_entry_script(root / "app" / "main.py")
            self.assertEqual(paths.root, root)
            self.assertEqual(paths.settings_file, root / "data" / "settings.json")
            self.assertEqual(
                paths.font_file,
                root / "assets" / "fonts" / "NotoSansMonoCJKsc-Regular.otf",
            )

    def test_non_bundle_entry_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BundlePaths.from_entry_script("/tmp/main.py")

    def test_relative_root_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BundlePaths(Path("relative"))

    def test_model_specs_use_exact_names_and_complete_hashes(self) -> None:
        root = Path(tempfile.gettempdir()).resolve() / "bundle"
        detection, recognition = BundlePaths(root).model_specs()
        self.assertEqual(detection.model_name, DETECTION_MODEL_NAME)
        self.assertEqual(recognition.model_name, RECOGNITION_MODEL_NAME)
        self.assertEqual(dict(detection.files_sha256), DETECTION_MODEL_HASHES)
        self.assertEqual(dict(recognition.files_sha256), RECOGNITION_MODEL_HASHES)
        self.assertEqual(
            detection.directory,
            root / "models" / "PP-OCRv6_small_det",
        )


if __name__ == "__main__":
    unittest.main()
