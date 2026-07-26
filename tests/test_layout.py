from __future__ import annotations

import unittest

from textsnap.domain import RecognizedSpan
from textsnap.layout import build_layout, display_cell_width, sanitize_ocr_text


def _span(
    text: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> RecognizedSpan:
    return RecognizedSpan(
        quad=((left, top), (right, top), (right, bottom), (left, bottom)),
        text=text,
        detection_score=0.9,
        recognition_score=0.9,
        rotation_degrees=0,
    )


class LayoutTests(unittest.TestCase):
    def test_scrambled_input_is_sorted_by_coordinates(self) -> None:
        spans = [
            _span("World", 70, 0, 120, 20),
            _span("second", 0, 30, 60, 50),
            _span("Hello", 0, 0, 50, 20),
        ]
        self.assertEqual(build_layout(spans).text, "Hello  World\nsecond")

    def test_cjk_and_ascii_use_half_width_grid(self) -> None:
        spans = [
            _span("中文", 0, 0, 40, 20),
            _span("ABCD", 60, 0, 100, 20),
        ]
        result = build_layout(spans)
        self.assertEqual(display_cell_width("中文"), 4)
        self.assertEqual(result.text, "中文  ABCD")

    def test_code_indentation_and_internal_spaces_are_preserved(self) -> None:
        spans = [
            _span("root", 0, 0, 40, 10),
            _span("if  value:", 20, 20, 120, 30),
            _span("return", 40, 40, 100, 50),
        ]
        self.assertEqual(
            build_layout(spans).text,
            "root\n  if  value:\n    return",
        )

    def test_same_y_columns_stay_on_one_output_line(self) -> None:
        spans = [
            _span("left", 0, 0, 40, 10),
            _span("right", 300, 0, 350, 10),
            _span("below", 0, 20, 50, 30),
        ]
        lines = build_layout(spans).text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("left"))
        self.assertTrue(lines[0].endswith("right"))
        self.assertGreater(lines[0].index("right"), 20)

    def test_shared_margin_is_cropped_but_relative_indent_remains(self) -> None:
        spans = [
            _span("first", 100, 50, 150, 60),
            _span("indented", 120, 70, 200, 80),
        ]
        self.assertEqual(build_layout(spans).text, "first\n  indented")

    def test_large_vertical_gap_becomes_blank_rows(self) -> None:
        spans = [
            _span("top", 0, 0, 30, 10),
            _span("bottom", 0, 60, 60, 70),
        ]
        lines = build_layout(spans).text.split("\n")
        self.assertEqual(lines[0], "top")
        self.assertEqual(lines[-1], "bottom")
        self.assertGreaterEqual(lines.count(""), 2)

    def test_collision_pushes_later_text_right(self) -> None:
        spans = [
            _span("abcdef", 0, 0, 60, 10),
            _span("X", 40, 0, 50, 10),
        ]
        self.assertEqual(build_layout(spans).text, "abcdefX")

    def test_half_cell_spacing_rounds_up_instead_of_to_even(self) -> None:
        spans = [
            _span("A", 0, 0, 10, 10),
            _span("B", 25, 0, 35, 10),
        ]
        self.assertEqual(build_layout(spans).text, "A  B")

    def test_physical_gap_survives_mixed_box_cell_widths(self) -> None:
        spans = [
            _span("AB", 0, 0, 15, 10),
            _span("X", 20, 0, 30, 10),
        ]
        self.assertEqual(build_layout(spans).text, "AB X")

    def test_trailing_spaces_and_final_newline_are_not_added(self) -> None:
        spans = [_span("value   ", 0, 0, 80, 10)]
        result = build_layout(spans).text
        self.assertEqual(result, "value")
        self.assertFalse(result.endswith("\n"))

    def test_controls_are_safely_replaced(self) -> None:
        self.assertEqual(sanitize_ocr_text("a\tb\nc\x00d"), "a b c d")

    def test_empty_input_has_zero_stats(self) -> None:
        result = build_layout([])
        self.assertEqual(result.text, "")
        self.assertEqual(result.stats.output_spans, 0)
        self.assertEqual(result.stats.line_count, 0)


if __name__ == "__main__":
    unittest.main()
