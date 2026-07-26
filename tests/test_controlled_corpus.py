from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import unittest

from textsnap.layout import display_cell_width
from textsnap.tiling import generate_tiles


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "html"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _BodyTextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.chunks: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.casefold() == "body":
            self.in_body = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "body":
            self.in_body = False

    def handle_data(self, data: str) -> None:
        if self.in_body:
            self.chunks.append(data)


def _canonical_distribution_name(value: str) -> str:
    return value.casefold().replace("_", "-").replace(".", "-")


def _visible_html_lines(html: str) -> tuple[str, ...]:
    parser = _BodyTextCollector()
    parser.feed(html)
    parser.close()
    return tuple(
        stripped
        for chunk in parser.chunks
        for line in chunk.splitlines()
        if (stripped := line.strip())
    )


class ControlledCorpusTests(unittest.TestCase):
    def _fixture(self) -> tuple[dict[str, object], str, str]:
        manifest = json.loads(
            (FIXTURE_ROOT / "corpus-manifest.json").read_text(encoding="utf-8")
        )
        page = manifest["pages"][0]
        html = (FIXTURE_ROOT / page["html"]).read_text(encoding="utf-8")
        expected = (FIXTURE_ROOT / page["expected"]).read_text(
            encoding="utf-8"
        ).removesuffix("\n")
        return manifest, html, expected

    def test_manifest_references_a_complete_offline_4k_fixture(self) -> None:
        manifest, html, expected = self._fixture()
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(len(manifest["pages"]), 1)
        page = manifest["pages"][0]
        self.assertEqual(
            page["recommended_viewport"],
            {"width": 3840, "height": 2160, "device_scale_factor": 1},
        )

        raw_expected = (FIXTURE_ROOT / page["expected"]).read_text(encoding="utf-8")
        self.assertTrue(raw_expected.endswith("\n"))
        self.assertFalse(raw_expected.endswith("\n\n"))
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

    def test_html_expected_and_render_manifest_are_bidirectionally_linked(
        self,
    ) -> None:
        manifest, html, expected = self._fixture()
        rendering = manifest["rendering"]
        expected_lines = expected.splitlines()
        styles = rendering["line_styles"]
        line_anchors = rendering["line_anchors"]
        self.assertEqual(len(styles), len(expected_lines))
        self.assertEqual(len(line_anchors), len(expected_lines))
        self.assertTrue(
            set(styles)
            <= {"title", "body", "heading", "code", "inverse", "small"}
        )
        for expected_line, anchor in zip(expected_lines, line_anchors):
            with self.subTest(mapped_line_anchor=anchor):
                self.assertIn(anchor, expected_line)

        visible_html_lines = _visible_html_lines(html)
        visible_html_text = "\n".join(visible_html_lines)
        for visible_line in visible_html_lines:
            with self.subTest(visible_html_line=visible_line):
                self.assertIn(visible_line, expected)

        for expected_line in expected_lines:
            if "RIGHT-COLUMN" in expected_line:
                right_offset = expected_line.index("RIGHT-COLUMN")
                pieces = (
                    expected_line[:right_offset].strip(),
                    expected_line[right_offset:].strip(),
                )
            else:
                pieces = (expected_line.strip(),)
            for piece in pieces:
                if piece:
                    with self.subTest(expected_piece=piece):
                        self.assertIn(piece, visible_html_text)

        code_lines = [
            line
            for line, style in zip(expected_lines, styles)
            if style == "code"
        ]
        self.assertIn("\n".join(code_lines), html)

        for anchor in (
            *line_anchors,
            *rendering["geometry_anchors"],
            *rendering["seam_anchors"],
        ):
            with self.subTest(render_anchor=anchor):
                self.assertEqual(expected.count(anchor), 1)
                self.assertEqual(visible_html_text.count(anchor), 1)

    def test_local_renderer_uses_only_locked_dependencies_and_font(self) -> None:
        manifest, _html, _expected = self._fixture()
        rendering = manifest["rendering"]
        self.assertEqual(rendering["backend"], "Pillow")
        self.assertNotIn("browser", rendering)

        wheels = json.loads(
            (REPOSITORY_ROOT / "vendor-lock" / "wheels.json").read_text(
                encoding="utf-8"
            )
        )
        locked_versions = {
            _canonical_distribution_name(artifact["name"]): artifact["version"]
            for artifact in wheels["artifacts"]
        }
        self.assertEqual(
            rendering["dependency_versions"],
            {
                name: locked_versions[_canonical_distribution_name(name)]
                for name in rendering["dependency_versions"]
            },
        )

        resources = json.loads(
            (REPOSITORY_ROOT / "vendor-lock" / "resources.json").read_text(
                encoding="utf-8"
            )
        )
        font_resources = [
            artifact
            for artifact in resources["artifacts"]
            if artifact["kind"] == "font"
        ]
        self.assertEqual(len(font_resources), 1)
        selected_fonts = font_resources[0]["unpack"]["selected_files"]
        self.assertEqual(len(selected_fonts), 1)
        locked_font = selected_fonts[0]
        self.assertEqual(
            rendering["font"],
            {
                "filename": Path(locked_font["destination_path"]).name,
                "size": locked_font["size"],
                "sha256": locked_font["sha256"],
            },
        )

    def test_render_cases_cover_normal_and_4k_and_cross_tile_edges(self) -> None:
        manifest, _html, expected = self._fixture()
        rendering = manifest["rendering"]
        cases = {case["id"]: case for case in rendering["cases"]}
        self.assertEqual(
            set(cases),
            {"normal-1920x1080", "4k-3840x2160"},
        )
        self.assertEqual(
            (cases["normal-1920x1080"]["width"], cases["normal-1920x1080"]["height"]),
            (1920, 1080),
        )
        four_k = cases["4k-3840x2160"]
        self.assertEqual((four_k["width"], four_k["height"]), (3840, 2160))

        line_count = len(expected.splitlines())
        maximum_font_size = max(rendering["font_sizes"].values())
        for case_id, case in cases.items():
            with self.subTest(case=case_id):
                last_baseline_area = (
                    case["origin_y"]
                    + (line_count - 1) * case["row_step"]
                    + maximum_font_size
                )
                self.assertLess(last_baseline_area, case["height"])

        long_line = next(
            line for line in expected.splitlines() if "END-LONG-LINE" in line
        )
        long_style = rendering["line_styles"][
            expected.splitlines().index(long_line)
        ]
        nominal_cell_advance = rendering["font_sizes"][long_style] / 2
        nominal_left = four_k["origin_x"]
        nominal_right = (
            nominal_left
            + display_cell_width(long_line) * nominal_cell_advance
        )
        internal_edges = {
            edge
            for tile in generate_tiles(four_k["width"], four_k["height"])
            for edge in (
                tile.x if tile.has_internal_left else None,
                tile.right if tile.has_internal_right else None,
            )
            if edge is not None
        }
        self.assertGreaterEqual(
            sum(nominal_left < edge < nominal_right for edge in internal_edges),
            2,
        )

    def test_expected_text_preserves_two_column_geometry(self) -> None:
        expected = (
            FIXTURE_ROOT / "controlled-corpus.expected.txt"
        ).read_text(encoding="utf-8").removesuffix("\n")
        lines = expected.splitlines()

        right_column: int | None = None
        for left, right in (
            ("LEFT-COLUMN-A 左栏第一行", "RIGHT-COLUMN-A 右栏第一行"),
            ("LEFT-COLUMN-B 左栏第二行", "RIGHT-COLUMN-B 右栏第二行"),
        ):
            matching = [line for line in lines if left in line]
            self.assertEqual(len(matching), 1)
            line = matching[0]
            self.assertIn(right, line)
            gap_start = line.index(left) + len(left)
            right_start = line.index(right)
            gap = line[gap_start:right_start]
            self.assertEqual(gap, " " * len(gap))
            self.assertGreaterEqual(len(gap), 16)
            current_column = display_cell_width(line[:right_start])
            if right_column is None:
                right_column = current_column
            self.assertEqual(current_column, right_column)

        right_c = "RIGHT-COLUMN-C 右栏第三行"
        left_c = "LEFT-COLUMN-C 左栏留白后"
        right_c_index = next(
            index for index, line in enumerate(lines) if right_c in line
        )
        left_c_index = next(
            index for index, line in enumerate(lines) if left_c in line
        )
        right_c_line = lines[right_c_index]
        self.assertLess(right_c_index, left_c_index)
        self.assertEqual(right_c_line.strip(), right_c)
        self.assertEqual(lines[left_c_index], left_c)
        self.assertIsNotNone(right_column)
        self.assertEqual(
            display_cell_width(right_c_line[: right_c_line.index(right_c)]),
            right_column,
        )


if __name__ == "__main__":
    unittest.main()
