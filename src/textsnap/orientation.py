"""Pure rotation-attempt policy for cropped text recognition."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

VERTICAL_ASPECT_THRESHOLD = 1.3
LOW_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class RecognitionAttempt:
    text: str
    score: float
    rotation_degrees: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be str")
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("score must be a finite value between 0 and 1")
        if self.rotation_degrees not in {0, 90, 180, 270}:
            raise ValueError("rotation must be a right angle")


def additional_rotations(
    *,
    crop_width: float,
    crop_height: float,
    initial_score: float,
) -> tuple[int, ...]:
    """Return rotations to try after the mandatory unrotated recognition."""

    if crop_width <= 0 or crop_height <= 0:
        raise ValueError("crop dimensions must be positive")
    if not math.isfinite(initial_score) or not 0 <= initial_score <= 1:
        raise ValueError("initial_score must be between 0 and 1")
    if crop_height / crop_width > VERTICAL_ASPECT_THRESHOLD:
        return (90, 270)
    if initial_score < LOW_CONFIDENCE_THRESHOLD:
        return (180,)
    return ()


def best_attempt(attempts: Iterable[RecognitionAttempt]) -> RecognitionAttempt:
    """Select maximum confidence, preferring fewer rotations on ties."""

    items = tuple(attempts)
    if not items:
        raise ValueError("at least one recognition attempt is required")
    rotation_preference = {0: 3, 180: 2, 90: 1, 270: 0}
    return max(
        items,
        key=lambda attempt: (
            attempt.score,
            bool(attempt.text),
            rotation_preference[attempt.rotation_degrees],
        ),
    )
