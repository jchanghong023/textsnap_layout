"""Run the real packaged OCR pipeline against one image/text fixture.

This diagnostic intentionally writes only the explicitly requested JSON report.
The OCR engine itself remains under the normal offline guard and uses the
bundle's locked models, font, cache directories, and native dependencies.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from types import MethodType
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from textsnap.domain import DetectionCandidate, RecognizedSpan, Success  # noqa: E402
from textsnap.geometry import quad_bounds  # noqa: E402
from textsnap import ocr as ocr_module  # noqa: E402
from textsnap.ocr import OcrEngine  # noqa: E402
from textsnap.paths import BundlePaths  # noqa: E402
from textsnap.privacy import OfflineGuard  # noqa: E402


def _normalized_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized[:-1] if normalized.endswith("\n") else normalized


def _without_whitespace(text: str) -> str:
    return "".join(character for character in text if not character.isspace())


def _levenshtein_distance(expected: str, actual: str) -> int:
    if len(expected) < len(actual):
        expected, actual = actual, expected
    previous = list(range(len(actual) + 1))
    for expected_index, expected_character in enumerate(expected, start=1):
        current = [expected_index]
        for actual_index, actual_character in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[actual_index] + 1,
                    previous[actual_index - 1]
                    + (expected_character != actual_character),
                )
            )
        previous = current
    return previous[-1]


def _comparison(expected: str, actual: str) -> dict[str, Any]:
    normalized_expected = _normalized_text(expected)
    normalized_actual = _normalized_text(actual)
    expected_compact = _without_whitespace(normalized_expected)
    actual_compact = _without_whitespace(normalized_actual)
    exact_distance = _levenshtein_distance(
        normalized_expected,
        normalized_actual,
    )
    compact_distance = _levenshtein_distance(
        expected_compact,
        actual_compact,
    )
    return {
        "expected_characters": len(normalized_expected),
        "actual_characters": len(normalized_actual),
        "expected_lines": len(normalized_expected.splitlines()),
        "actual_lines": len(normalized_actual.splitlines()),
        "exact_distance": exact_distance,
        "exact_cer": exact_distance / max(1, len(normalized_expected)),
        "compact_expected_characters": len(expected_compact),
        "compact_actual_characters": len(actual_compact),
        "compact_distance": compact_distance,
        "compact_cer": compact_distance / max(1, len(expected_compact)),
    }


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-details", action="store_true")
    parser.add_argument("--max-compact-cer", type=float)
    return parser.parse_args()


def _candidate_record(candidate: DetectionCandidate) -> dict[str, Any]:
    return {
        "bounds": list(quad_bounds(candidate.quad)),
        "detection_score": candidate.detection_score,
        "source_tile": candidate.source_tile.index,
        "source_tile_indices": list(candidate.source_tile_indices),
        "touches_internal_edge": candidate.touches_internal_edge,
    }


def _span_record(span: RecognizedSpan) -> dict[str, Any]:
    return {
        "bounds": list(quad_bounds(span.quad)),
        "text": span.text,
        "detection_score": span.detection_score,
        "recognition_score": span.recognition_score,
        "rotation_degrees": span.rotation_degrees,
    }


def main() -> int:
    arguments = _parse_arguments()
    if (
        arguments.max_compact_cer is not None
        and not 0 <= arguments.max_compact_cer <= 1
    ):
        raise ValueError("max compact CER must be between zero and one")
    bundle_root = arguments.bundle_root.resolve(strict=True)
    image_path = arguments.image.resolve(strict=True)
    expected_path = arguments.expected.resolve(strict=True)
    output_path = arguments.output.resolve(strict=False)
    if not bundle_root.is_dir():
        raise ValueError("bundle root must be a directory")
    if not image_path.is_file() or not expected_path.is_file():
        raise ValueError("image and expected text must be files")
    if output_path.exists() and not output_path.is_file():
        raise ValueError("output must be a file path")
    if not output_path.parent.is_dir():
        raise ValueError("output parent directory must already exist")

    expected = expected_path.read_text(encoding="utf-8")
    encoded_image = image_path.read_bytes()
    paths = BundlePaths(bundle_root)
    detection_spec, recognition_spec = paths.model_specs()
    engine = OcrEngine(detection_spec, recognition_spec)
    initialization_started = time.perf_counter()
    recognition_seconds: float | None = None
    raw_candidates: list[DetectionCandidate] = []
    consolidated_candidates: list[DetectionCandidate] = []
    recognized_spans: list[RecognizedSpan] = []
    recognition_batches: list[dict[str, Any]] = []

    if arguments.include_details:
        original_consolidate = ocr_module.consolidate_candidates
        original_build_layout = ocr_module.build_layout

        def capture_consolidation(
            candidates: object,
        ) -> tuple[DetectionCandidate, ...]:
            materialized = tuple(candidates)  # type: ignore[arg-type]
            raw_candidates.extend(materialized)
            consolidated = original_consolidate(materialized)
            consolidated_candidates.extend(consolidated)
            return consolidated

        def capture_layout(spans: object) -> object:
            materialized = tuple(spans)  # type: ignore[arg-type]
            recognized_spans.extend(materialized)
            return original_build_layout(materialized)

        ocr_module.consolidate_candidates = capture_consolidation
        ocr_module.build_layout = capture_layout

    with OfflineGuard(
        cache_home=paths.paddlex_cache,
        font_file=paths.font_file,
    ):
        import cv2
        import numpy

        image_bgr = cv2.imdecode(
            numpy.frombuffer(encoded_image, dtype=numpy.uint8),
            cv2.IMREAD_COLOR,
        )
        if image_bgr is None:
            raise ValueError("image cannot be decoded")

        initialization_failure = engine.initialize()
        initialization_seconds = time.perf_counter() - initialization_started
        if initialization_failure is not None:
            report = {
                "status": "initialization_failure",
                "error_type": initialization_failure.error_type,
                "diagnostic_code": initialization_failure.diagnostic_code,
                "initialization_seconds": initialization_seconds,
            }
        else:
            if arguments.include_details:
                original_predict_recognition = engine._predict_recognition

                def capture_recognition(
                    self: OcrEngine,
                    images: object,
                ) -> list[tuple[str, float]]:
                    materialized = tuple(images)  # type: ignore[arg-type]
                    results = original_predict_recognition(materialized)
                    assert self._backend is not None
                    recognition_batches.append(
                        {
                            "dimensions": [
                                list(self._backend.dimensions(image))
                                for image in materialized
                            ],
                            "results": [
                                {"text": text, "score": score}
                                for text, score in results
                            ],
                        }
                    )
                    return results

                engine._predict_recognition = MethodType(
                    capture_recognition,
                    engine,
                )
            recognition_started = time.perf_counter()
            try:
                outcome = engine.recognize(image_bgr)
                recognition_seconds = time.perf_counter() - recognition_started
            finally:
                close_failure = engine.close()
            if not isinstance(outcome, Success):
                report = {
                    "status": type(outcome).__name__,
                    "initialization_seconds": initialization_seconds,
                    "recognition_seconds": recognition_seconds,
                    "close_failure": (
                        None
                        if close_failure is None
                        else close_failure.diagnostic_code
                    ),
                }
            else:
                actual = outcome.result.text
                report = {
                    "status": "success",
                    "initialization_seconds": initialization_seconds,
                    "recognition_seconds": recognition_seconds,
                    "comparison": _comparison(expected, actual),
                    "layout_stats": {
                        "input_spans": outcome.result.stats.input_spans,
                        "output_spans": outcome.result.stats.output_spans,
                        "line_count": outcome.result.stats.line_count,
                        "grid_cell_width": outcome.result.stats.grid_cell_width,
                        "row_step": outcome.result.stats.row_step,
                    },
                    "actual_text": actual,
                    "close_failure": (
                        None
                        if close_failure is None
                        else close_failure.diagnostic_code
                    ),
                }
            if arguments.include_details:
                report["details"] = {
                    "raw_candidates": [
                        _candidate_record(candidate)
                        for candidate in raw_candidates
                    ],
                    "consolidated_candidates": [
                        _candidate_record(candidate)
                        for candidate in consolidated_candidates
                    ],
                    "recognized_spans": [
                        _span_record(span) for span in recognized_spans
                    ],
                    "recognition_batches": recognition_batches,
                }

    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    if report["status"] != "success":
        return 1
    if (
        arguments.max_compact_cer is not None
        and report["comparison"]["compact_cer"] > arguments.max_compact_cer
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
