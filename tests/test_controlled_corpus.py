from __future__ import annotations

import json
from pathlib import Path
import unittest


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "html"


class ControlledCorpusTests(unittest.TestCase):
    def test_manifest_references_a_complete_offline_4k_fixture(self) -> None:
        manifest = json.loads(
            (FIXTURE_ROOT / "corpus-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(len(manifest["pages"]), 1)
        page = manifest["pages"][0]
        self.assertEqual(
            page["recommended_viewport"],
            {"width": 3840, "height": 2160, "device_scale_factor": 1},
        )

        html = (FIXTURE_ROOT / page["html"]).read_text(encoding="utf-8")
        raw_expected = (FIXTURE_ROOT / page["expected"]).read_text(encoding="utf-8")
        self.assertFalse(raw_expected.endswith("\n\n"))
        expected = raw_expected.removesuffix("\n")
        self.assertFalse(expected.endswith("\n"))
        for anchor in page["required_anchors"]:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, html)
                self.assertIn(anchor, expected)

        for required_feature in (
            "<pre>",
            "grid-template-columns: 1fr 1fr",
            "INVERSE-TEXT",
            "4K-SMALL-TEXT",
            "中文文档",
            "English web sample",
        ):
            self.assertIn(required_feature, html)
        self.assertNotIn("<script", html.casefold())
        self.assertNotIn("src=", html.casefold())
        self.assertNotIn("href=", html.casefold())


if __name__ == "__main__":
    unittest.main()
