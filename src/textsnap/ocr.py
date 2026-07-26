"""Local PaddleOCR det/rec orchestration with dependency-free import."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
from threading import Event, RLock
from types import MappingProxyType
from typing import Protocol

from .detection import consolidate_candidates
from .domain import (
    Cancelled,
    DetectionCandidate,
    Empty,
    Failure,
    Quad,
    RecognizedSpan,
    Success,
    TaskOutcome,
    TileRegion,
)
from .layout import build_layout
from .orientation import RecognitionAttempt, additional_rotations, best_attempt
from .privacy import require_offline_guard
from .tiling import generate_tiles, internal_edge_metrics, map_quad_to_global


DETECTION_MODEL_NAME = "PP-OCRv6_small_det"
RECOGNITION_MODEL_NAME = "PP-OCRv6_small_rec"
RECOGNITION_BATCH_SIZE = 8
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_MODEL_WORKING_DIRECTORY_LOCK = RLock()
_DEFAULT_ENGINE_CONFIG = MappingProxyType(
    {
        "device_type": "cpu",
        "run_mode": "mkldnn",
        "cpu_threads": 10,
        "mkldnn_cache_capacity": 10,
        # The locked Paddle 3.2.2 runtime does not require the new-IR path.
        # Disable it explicitly on every platform to keep inference behavior
        # identical to the validated Windows CPU configuration.
        "enable_new_ir": False,
    }
)


@dataclass(frozen=True, slots=True)
class LocalModelSpec:
    """One pinned local model directory and all release-file hashes."""

    model_name: str
    directory: Path = field(repr=False)
    files_sha256: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.model_name:
            raise ValueError("model_name must not be empty")
        object.__setattr__(self, "directory", Path(self.directory))
        hashes = dict(self.files_sha256)
        if not hashes:
            raise ValueError("model hashes must not be empty")
        for relative_name, digest in hashes.items():
            if not isinstance(relative_name, str) or not relative_name:
                raise ValueError("model file names must be non-empty strings")
            if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(
                digest.lower()
            ):
                raise ValueError("model hashes must be lowercase SHA-256 values")
        object.__setattr__(
            self,
            "files_sha256",
            MappingProxyType({name: digest.lower() for name, digest in hashes.items()}),
        )


class Predictor(Protocol):
    def predict(self, *, input: object, batch_size: int) -> object: ...

    def close(self) -> object: ...


class PredictorFactory(Protocol):
    def create_detector(self, **kwargs: object) -> Predictor: ...

    def create_recognizer(self, **kwargs: object) -> Predictor: ...


class ImageBackend(Protocol):
    def normalize_bgr(self, image: object) -> object: ...

    def tile(self, image: object, tile: TileRegion) -> object: ...

    def perspective_crop(self, image: object, quad: Quad) -> object: ...

    def rotate(self, image: object, degrees: int) -> object: ...

    def dimensions(self, image: object) -> tuple[int, int]: ...

    def warmup_image(self) -> object: ...


class _ModelValidationError(Exception):
    pass


class _PredictorOutputError(Exception):
    pass


class _PaddlePredictorFactory:
    """Import PaddleOCR only after the process offline policy is active."""

    def create_detector(self, **kwargs: object) -> Predictor:
        from paddleocr import TextDetection

        return TextDetection(**kwargs)

    def create_recognizer(self, **kwargs: object) -> Predictor:
        from paddleocr import TextRecognition

        return TextRecognition(**kwargs)


class _OpenCvImageBackend:
    """NumPy/OpenCV operations loaded lazily with the Paddle runtime."""

    def __init__(self) -> None:
        import cv2
        import numpy

        self._cv2 = cv2
        self._numpy = numpy

    def normalize_bgr(self, image: object) -> object:
        numpy = self._numpy
        if not isinstance(image, numpy.ndarray):
            raise ValueError("OCR input must be a NumPy ndarray")
        if (
            image.ndim != 3
            or image.shape[0] <= 0
            or image.shape[1] <= 0
            or image.shape[2] != 3
            or image.dtype != numpy.uint8
        ):
            raise ValueError("OCR input must be a non-empty uint8 BGR image")
        return numpy.ascontiguousarray(image)

    def tile(self, image: object, tile: TileRegion) -> object:
        view = image[tile.y : tile.bottom, tile.x : tile.right]
        return self._numpy.ascontiguousarray(view)

    def perspective_crop(self, image: object, quad: Quad) -> object:
        numpy = self._numpy
        points = numpy.asarray(quad, dtype=numpy.float32)
        if points.shape != (4, 2) or not bool(numpy.isfinite(points).all()):
            raise ValueError("invalid detection quadrilateral")

        if int(numpy.unique(points, axis=0).shape[0]) != 4:
            raise ValueError("degenerate detection quadrilateral")
        validation_points = points.astype(numpy.float64)
        edges = numpy.roll(validation_points, -1, axis=0) - validation_points
        following_edges = numpy.roll(edges, -1, axis=0)
        cross_products = (
            edges[:, 0] * following_edges[:, 1]
            - edges[:, 1] * following_edges[:, 0]
        )
        if not (
            bool(numpy.all(cross_products > 0))
            or bool(numpy.all(cross_products < 0))
        ):
            raise ValueError("invalid detection quadrilateral")
        following_points = numpy.roll(validation_points, -1, axis=0)
        twice_area = float(
            numpy.sum(
                validation_points[:, 0] * following_points[:, 1]
                - validation_points[:, 1] * following_points[:, 0]
            )
        )
        if twice_area == 0:
            raise ValueError("degenerate detection quadrilateral")

        ordered = numpy.ascontiguousarray(points)
        top_left, top_right, bottom_right, bottom_left = ordered

        width = max(
            float(numpy.linalg.norm(top_right - top_left)),
            float(numpy.linalg.norm(bottom_right - bottom_left)),
        )
        height = max(
            float(numpy.linalg.norm(bottom_left - top_left)),
            float(numpy.linalg.norm(bottom_right - top_right)),
        )
        output_width = int(round(width))
        output_height = int(round(height))
        if output_width < 2 or output_height < 2:
            raise ValueError("degenerate detection quadrilateral")

        destination = numpy.asarray(
            (
                (0, 0),
                (output_width - 1, 0),
                (output_width - 1, output_height - 1),
                (0, output_height - 1),
            ),
            dtype=numpy.float32,
        )
        transform = self._cv2.getPerspectiveTransform(ordered, destination)
        crop = self._cv2.warpPerspective(
            image,
            transform,
            (output_width, output_height),
            flags=self._cv2.INTER_CUBIC,
            borderMode=self._cv2.BORDER_REPLICATE,
        )
        return numpy.ascontiguousarray(crop)

    def rotate(self, image: object, degrees: int) -> object:
        if degrees not in {90, 180, 270}:
            raise ValueError("rotation must be 90, 180, or 270 degrees")
        return self._numpy.ascontiguousarray(self._numpy.rot90(image, k=degrees // 90))

    def dimensions(self, image: object) -> tuple[int, int]:
        return int(image.shape[1]), int(image.shape[0])

    def warmup_image(self) -> object:
        return self._numpy.full((64, 256, 3), 255, dtype=self._numpy.uint8)


@dataclass(slots=True)
class _CropRecord:
    candidate: DetectionCandidate
    crop: object | None = field(repr=False)
    width: int
    height: int
    attempts: list[RecognitionAttempt] = field(default_factory=list, repr=False)


def _failure(error_type: str, public_message: str, diagnostic_code: str) -> Failure:
    return Failure(
        error_type=error_type,
        public_message=public_message,
        diagnostic_code=diagnostic_code,
    )


def _validate_relative_model_name(relative_name: str) -> PurePosixPath:
    path = PurePosixPath(relative_name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in relative_name
    ):
        raise _ModelValidationError
    return path


def validate_local_model(spec: LocalModelSpec, *, expected_model_name: str) -> None:
    """Validate required files and every pinned SHA-256 without downloading."""

    if spec.model_name != expected_model_name:
        raise _ModelValidationError
    try:
        root = spec.directory.resolve(strict=True)
    except OSError as exc:
        raise _ModelValidationError from exc
    if not root.is_dir():
        raise _ModelValidationError

    locked_names = set(spec.files_sha256)
    required = {"inference.yml", "inference.pdiparams"}
    if not required.issubset(locked_names) or not (
        {"inference.json", "inference.pdmodel"} & locked_names
    ):
        raise _ModelValidationError

    for relative_name, expected_digest in spec.files_sha256.items():
        relative_path = _validate_relative_model_name(relative_name)
        try:
            target = root.joinpath(*relative_path.parts).resolve(strict=True)
        except OSError as exc:
            raise _ModelValidationError from exc
        if not target.is_relative_to(root) or not target.is_file():
            raise _ModelValidationError
        digest = hashlib.sha256()
        try:
            with target.open("rb") as model_file:
                for block in iter(lambda: model_file.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise _ModelValidationError from exc
        if digest.hexdigest() != expected_digest:
            raise _ModelValidationError


@contextmanager
def _paddle_model_directory_arguments(
    detection_directory: Path,
    recognition_directory: Path,
) -> Iterator[tuple[str, str]]:
    """Use ASCII relative paths for Paddle's narrow Windows file API."""

    detection = detection_directory.resolve(strict=True)
    recognition = recognition_directory.resolve(strict=True)
    absolute_arguments = (str(detection), str(recognition))
    if os.name != "nt" or all(argument.isascii() for argument in absolute_arguments):
        yield absolute_arguments
        return

    try:
        common = Path(os.path.commonpath((detection, recognition)))
        if common in {detection, recognition}:
            common = common.parent
        detection_relative = detection.relative_to(common)
        recognition_relative = recognition.relative_to(common)
    except (OSError, ValueError):
        raise _ModelValidationError from None
    relative_arguments = (
        str(detection_relative),
        str(recognition_relative),
    )
    if not all(argument and argument.isascii() for argument in relative_arguments):
        raise _ModelValidationError

    # Paddle 3.2.2 passes model paths to a narrow Windows C++ file API. Keep
    # the process-wide cwd change bounded to predictor construction and restore
    # it before warmup or any user-triggered work.
    with _MODEL_WORKING_DIRECTORY_LOCK:
        previous = Path.cwd()
        try:
            os.chdir(common)
            yield relative_arguments
        finally:
            os.chdir(previous)


class OcrEngine:
    """Own two resident predictors and run the fixed local OCR pipeline."""

    def __init__(
        self,
        detection_model: LocalModelSpec,
        recognition_model: LocalModelSpec,
        *,
        predictor_factory: PredictorFactory | None = None,
        image_backend: ImageBackend | None = None,
        engine_config: Mapping[str, object] | None = None,
    ) -> None:
        self._detection_model = detection_model
        self._recognition_model = recognition_model
        self._factory = predictor_factory
        self._backend = image_backend
        config = dict(_DEFAULT_ENGINE_CONFIG)
        if engine_config is not None:
            config.update(engine_config)
        if config.get("device_type") != "cpu":
            raise ValueError("only the CPU inference device is supported")
        if not isinstance(config.get("cpu_threads"), int) or config["cpu_threads"] <= 0:
            raise ValueError("cpu_threads must be a positive integer")
        self._engine_config = MappingProxyType(config)
        self._detector: Predictor | None = None
        self._recognizer: Predictor | None = None
        self._closed = False

    @property
    def ready(self) -> bool:
        return self._detector is not None and self._recognizer is not None

    @property
    def engine_config(self) -> Mapping[str, object]:
        return self._engine_config

    def initialize(self) -> Failure | None:
        if self._closed:
            return _failure(
                "OcrLifecycleError",
                "OCR engine is already closed.",
                "ocr-engine-closed",
            )
        if self.ready:
            return None

        try:
            validate_local_model(
                self._detection_model, expected_model_name=DETECTION_MODEL_NAME
            )
            validate_local_model(
                self._recognition_model, expected_model_name=RECOGNITION_MODEL_NAME
            )
        except _ModelValidationError:
            return _failure(
                "ModelValidationError",
                "Local OCR models are missing or damaged.",
                "ocr-model-integrity",
            )

        detector: Predictor | None = None
        recognizer: Predictor | None = None
        try:
            if self._factory is None:
                require_offline_guard()
                self._factory = _PaddlePredictorFactory()
            if self._backend is None:
                require_offline_guard()
                self._backend = _OpenCvImageBackend()

            with _paddle_model_directory_arguments(
                self._detection_model.directory,
                self._recognition_model.directory,
            ) as (detection_model_dir, recognition_model_dir):
                detector = self._factory.create_detector(
                    model_name=DETECTION_MODEL_NAME,
                    model_dir=detection_model_dir,
                    device="cpu",
                    engine="paddle_static",
                    engine_config=dict(self._engine_config),
                    limit_type="max",
                    limit_side_len=1216,
                    thresh=0.3,
                    box_thresh=0.5,
                    unclip_ratio=1.5,
                )
                recognizer = self._factory.create_recognizer(
                    model_name=RECOGNITION_MODEL_NAME,
                    model_dir=recognition_model_dir,
                    device="cpu",
                    engine="paddle_static",
                    engine_config=dict(self._engine_config),
                )
            warmup = self._backend.warmup_image()
            detector_results = list(
                detector.predict(input=warmup, batch_size=1)  # type: ignore[arg-type]
            )
            detector_results.clear()
            recognition_results = list(
                recognizer.predict(input=[warmup], batch_size=1)  # type: ignore[arg-type]
            )
            recognition_results.clear()
            warmup = None
        except Exception:
            recognizer_closed = self._close_predictor(recognizer)
            detector_closed = self._close_predictor(detector)
            cleanup_failed = not (recognizer_closed and detector_closed)
            return _failure(
                (
                    "ModelInitializationCleanupError"
                    if cleanup_failed
                    else "ModelInitializationError"
                ),
                "The local OCR engine could not be initialized.",
                (
                    "ocr-model-initialize-cleanup"
                    if cleanup_failed
                    else "ocr-model-initialize"
                ),
            )

        self._detector = detector
        self._recognizer = recognizer
        return None

    def recognize(
        self, image_bgr: object, cancel_event: Event | None = None
    ) -> TaskOutcome:
        if not self.ready or self._backend is None:
            return _failure(
                "OcrLifecycleError",
                "The OCR engine is not ready.",
                "ocr-engine-not-ready",
            )
        if cancel_event is not None and cancel_event.is_set():
            return Cancelled()

        image: object | None = None
        records: list[_CropRecord] = []
        try:
            image = self._backend.normalize_bgr(image_bgr)
            width, height = self._backend.dimensions(image)
        except Exception:
            return _failure(
                "OcrInputError",
                "The captured image is not a valid BGR image.",
                "ocr-invalid-input",
            )

        try:
            candidates = self._detect(image, width, height, cancel_event)
            if candidates is None:
                return Cancelled()
            if not candidates:
                return Empty()

            for candidate in candidates:
                if cancel_event is not None and cancel_event.is_set():
                    return Cancelled()
                try:
                    crop = self._backend.perspective_crop(image, candidate.quad)
                    crop_width, crop_height = self._backend.dimensions(crop)
                except ValueError:
                    continue
                if crop_width <= 0 or crop_height <= 0:
                    continue
                records.append(
                    _CropRecord(
                        candidate=candidate,
                        crop=crop,
                        width=crop_width,
                        height=crop_height,
                    )
                )
            if not records:
                return Empty()

            if not self._recognize_initial(records, cancel_event):
                return Cancelled()
            if not self._recognize_rotations(records, cancel_event):
                return Cancelled()

            spans: list[RecognizedSpan] = []
            for record in records:
                attempt = best_attempt(record.attempts)
                if attempt.text == "":
                    continue
                spans.append(
                    RecognizedSpan(
                        quad=record.candidate.quad,
                        text=attempt.text,
                        detection_score=record.candidate.detection_score,
                        recognition_score=attempt.score,
                        rotation_degrees=attempt.rotation_degrees,
                    )
                )
            if not spans:
                if cancel_event is not None and cancel_event.is_set():
                    return Cancelled()
                return Empty()
            layout = build_layout(spans)
            if cancel_event is not None and cancel_event.is_set():
                return Cancelled()
            if not layout.text.strip():
                return Empty()
            return Success(layout)
        except Exception:
            return _failure(
                "OcrInferenceError",
                "Text recognition failed.",
                "ocr-inference-failed",
            )
        finally:
            image = None
            for record in records:
                record.crop = None
                record.attempts.clear()
            records.clear()

    def close(self) -> Failure | None:
        close_failed = False
        for predictor in (self._recognizer, self._detector):
            if predictor is not None:
                try:
                    predictor.close()
                except Exception:
                    close_failed = True
        self._recognizer = None
        self._detector = None
        self._closed = True
        if close_failed:
            return _failure(
                "OcrShutdownError",
                "The OCR engine did not shut down cleanly.",
                "ocr-close-failed",
            )
        return None

    def _detect(
        self,
        image: object,
        width: int,
        height: int,
        cancel_event: Event | None,
    ) -> tuple[DetectionCandidate, ...] | None:
        assert self._detector is not None
        assert self._backend is not None
        candidates: list[DetectionCandidate] = []
        for tile in generate_tiles(width, height):
            if cancel_event is not None and cancel_event.is_set():
                return None
            tile_image: object | None = self._backend.tile(image, tile)
            try:
                detections = self._predict_detection(tile_image, tile)
            finally:
                tile_image = None
            candidates.extend(detections)
            if cancel_event is not None and cancel_event.is_set():
                return None
        return consolidate_candidates(candidates)

    def _predict_detection(
        self, tile_image: object, tile: TileRegion
    ) -> tuple[DetectionCandidate, ...]:
        assert self._detector is not None
        outputs: list[object] = []
        result: object | None = None
        try:
            outputs = list(self._detector.predict(input=tile_image, batch_size=1))
            if len(outputs) != 1:
                raise _PredictorOutputError
            result = outputs[0]
            raw_polys = result["dt_polys"]  # type: ignore[index]
            raw_scores = result["dt_scores"]  # type: ignore[index]
            if len(raw_polys) != len(raw_scores):
                raise _PredictorOutputError

            candidates: list[DetectionCandidate] = []
            for raw_poly, raw_score in zip(raw_polys, raw_scores):
                points = tuple(raw_poly)
                if len(points) != 4:
                    raise _PredictorOutputError
                local_points: list[tuple[float, float]] = []
                for raw_point in points:
                    if len(raw_point) != 2:
                        raise _PredictorOutputError
                    x = float(raw_point[0])
                    y = float(raw_point[1])
                    if not math.isfinite(x) or not math.isfinite(y):
                        raise _PredictorOutputError
                    local_points.append(
                        (
                            min(max(x, 0.0), float(tile.width)),
                            min(max(y, 0.0), float(tile.height)),
                        )
                    )
                score = float(raw_score)
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise _PredictorOutputError
                local_quad = tuple(local_points)
                global_quad = map_quad_to_global(local_quad, tile)  # type: ignore[arg-type]
                edge_distance, touches_edge = internal_edge_metrics(global_quad, tile)
                candidates.append(
                    DetectionCandidate(
                        quad=global_quad,
                        detection_score=score,
                        source_tile=tile,
                        internal_edge_distance=edge_distance,
                        touches_internal_edge=touches_edge,
                    )
                )
            return tuple(candidates)
        finally:
            result = None
            outputs.clear()

    def _recognize_initial(
        self, records: list[_CropRecord], cancel_event: Event | None
    ) -> bool:
        for start in range(0, len(records), RECOGNITION_BATCH_SIZE):
            if cancel_event is not None and cancel_event.is_set():
                return False
            batch = records[start : start + RECOGNITION_BATCH_SIZE]
            attempts = self._predict_recognition(
                [record.crop for record in batch if record.crop is not None]
            )
            if len(attempts) != len(batch):
                raise _PredictorOutputError
            for record, (text, score) in zip(batch, attempts):
                record.attempts.append(RecognitionAttempt(text, score, 0))
            if cancel_event is not None and cancel_event.is_set():
                return False
        return True

    def _recognize_rotations(
        self, records: list[_CropRecord], cancel_event: Event | None
    ) -> bool:
        pending: list[tuple[_CropRecord, int, object]] = []

        def flush() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return False
            if not pending:
                return True
            attempts = self._predict_recognition([job[2] for job in pending])
            if len(attempts) != len(pending):
                raise _PredictorOutputError
            for (record, rotation, _), (text, score) in zip(pending, attempts):
                record.attempts.append(RecognitionAttempt(text, score, rotation))
            pending.clear()
            return cancel_event is None or not cancel_event.is_set()

        for record in records:
            initial = record.attempts[0]
            for rotation in additional_rotations(
                crop_width=record.width,
                crop_height=record.height,
                initial_score=initial.score,
            ):
                assert record.crop is not None
                pending.append(
                    (record, rotation, self._backend.rotate(record.crop, rotation))
                )
                if len(pending) == RECOGNITION_BATCH_SIZE and not flush():
                    pending.clear()
                    return False
        completed = flush()
        pending.clear()
        return completed

    def _predict_recognition(self, images: Sequence[object]) -> list[tuple[str, float]]:
        assert self._recognizer is not None
        outputs: list[object] = []
        try:
            outputs = list(
                self._recognizer.predict(
                    input=list(images), batch_size=RECOGNITION_BATCH_SIZE
                )
            )
            if len(outputs) != len(images):
                raise _PredictorOutputError
            parsed: list[tuple[str, float]] = []
            for result in outputs:
                text = result["rec_text"]  # type: ignore[index]
                score = float(result["rec_score"])  # type: ignore[index]
                if (
                    not isinstance(text, str)
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise _PredictorOutputError
                parsed.append((text, score))
            return parsed
        finally:
            outputs.clear()

    @staticmethod
    def _close_predictor(predictor: Predictor | None) -> bool:
        if predictor is None:
            return True
        try:
            predictor.close()
        except Exception:
            return False
        return True
