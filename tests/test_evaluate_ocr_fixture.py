from __future__ import annotations

import unittest

from scripts.evaluate_ocr_fixture import _comparison, _levenshtein_distance


class OcrFixtureEvaluationTests(unittest.TestCase):
    def test_levenshtein_distance_covers_insert_delete_and_substitute(self) -> None:
        self.assertEqual(_levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(_levenshtein_distance("same", "same"), 0)
        self.assertEqual(_levenshtein_distance("", "abc"), 3)

    def test_comparison_normalizes_line_endings_and_one_terminal_newline(self) -> None:
        comparison = _comparison("first\r\nsecond\r\n", "first\nsecond")

        self.assertEqual(comparison["exact_distance"], 0)
        self.assertEqual(comparison["compact_distance"], 0)
        self.assertEqual(comparison["expected_lines"], 2)
        self.assertEqual(comparison["actual_lines"], 2)

    def test_compact_cer_ignores_layout_whitespace_but_not_symbols(self) -> None:
        comparison = _comparison("A _ B\nC", "A B C")

        self.assertEqual(comparison["compact_expected_characters"], 4)
        self.assertEqual(comparison["compact_actual_characters"], 3)
        self.assertEqual(comparison["compact_distance"], 1)
        self.assertEqual(comparison["compact_cer"], 0.25)


if __name__ == "__main__":
    unittest.main()
