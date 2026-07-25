from __future__ import annotations

import unittest

from textsnap.orientation import (
    RecognitionAttempt,
    additional_rotations,
    best_attempt,
)


class OrientationTests(unittest.TestCase):
    def test_vertical_crop_tries_both_quarter_turns(self) -> None:
        self.assertEqual(
            additional_rotations(crop_width=20, crop_height=30, initial_score=0.9),
            (90, 270),
        )

    def test_low_confidence_horizontal_crop_tries_180(self) -> None:
        self.assertEqual(
            additional_rotations(crop_width=100, crop_height=20, initial_score=0.49),
            (180,),
        )

    def test_confident_horizontal_crop_stops(self) -> None:
        self.assertEqual(
            additional_rotations(crop_width=100, crop_height=20, initial_score=0.5),
            (),
        )

    def test_best_score_wins(self) -> None:
        attempts = (
            RecognitionAttempt("upside", 0.3, 0),
            RecognitionAttempt("correct", 0.95, 180),
        )
        self.assertEqual(best_attempt(attempts), attempts[1])

    def test_unrotated_wins_score_tie(self) -> None:
        attempts = (
            RecognitionAttempt("same", 0.8, 180),
            RecognitionAttempt("same", 0.8, 0),
        )
        self.assertEqual(best_attempt(attempts), attempts[1])


if __name__ == "__main__":
    unittest.main()
