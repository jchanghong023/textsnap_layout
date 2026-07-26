from __future__ import annotations

import builtins
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from textsnap.domain import Cancelled, Empty, Failure, Success
from textsnap.layout import build_layout
from textsnap.ocr import (
    DETECTION_MODEL_NAME,
    RECOGNITION_MODEL_NAME,
    SMALL_TEXT_CROP_HEIGHT,
    SMALL_TEXT_SCALE_FACTOR,
    WIDE_TEXT_HORIZONTAL_SCALE_FACTOR,
    WIDE_TEXT_MAX_HEIGHT,
    WIDE_TEXT_MIN_ASPECT_RATIO,
    LocalModelSpec,
    OcrEngine,
    _OpenCvImageBackend,
)
from textsnap.orientation import best_attempt


def _quad(left: float, top: float, right: float, bottom: float):
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def _reject_python_file_write(*args, **kwargs):
    raise AssertionError("unexpected Python file write")


@contextmanager
def _deny_python_file_writes():
    entrypoints = [
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (os, "fdopen"),
        (os, "write"),
    ]
    entrypoints.extend(
        (os, name) for name in ("pwrite", "writev") if hasattr(os, name)
    )
    with ExitStack() as stack:
        for owner, name in entrypoints:
            stack.enter_context(
                patch.object(owner, name, side_effect=_reject_python_file_write)
            )
        yield


class StagedCancelEvent:
    def __init__(self, armed: Event, cancel_on_armed_check: int) -> None:
        self._armed = armed
        self._cancel_on_armed_check = cancel_on_armed_check
        self.armed_checks = 0

    def is_set(self) -> bool:
        if not self._armed.is_set():
            return False
        self.armed_checks += 1
        return self.armed_checks >= self._cancel_on_armed_check


class ControlledNdarray:
    """Small ndarray stand-in so unit tests need neither NumPy nor Paddle."""

    def __init__(
        self,
        height: int,
        width: int,
        *,
        tag: object = "image",
        dtype: str = "uint8",
        contiguous: bool = False,
    ) -> None:
        self.shape = (height, width, 3)
        self.ndim = 3
        self.dtype = dtype
        self.tag = tag
        self.contiguous = contiguous


class FakeImageBackend:
    def __init__(self) -> None:
        self.perspective_quads = []
        self.detector_inputs = []
        self.enhanced_crops = []
        self.stretched_crops = []

    def normalize_bgr(self, image):
        if (
            not isinstance(image, ControlledNdarray)
            or image.ndim != 3
            or image.shape[2] != 3
            or image.dtype != "uint8"
            or image.shape[0] <= 0
            or image.shape[1] <= 0
        ):
            raise ValueError("bad image")
        return ControlledNdarray(
            image.shape[0],
            image.shape[1],
            tag=image.tag,
            contiguous=True,
        )

    def tile(self, image, tile):
        result = ControlledNdarray(
            tile.height,
            tile.width,
            tag=("tile", tile.x, tile.y),
            contiguous=True,
        )
        self.detector_inputs.append(result)
        return result

    def perspective_crop(self, image, quad):
        self.perspective_quads.append(quad)
        xs = [point[0] for point in quad]
        ys = [point[1] for point in quad]
        width = max(1, round(max(xs) - min(xs)))
        height = max(1, round(max(ys) - min(ys)))
        return ControlledNdarray(
            height,
            width,
            tag=("crop", quad),
            contiguous=True,
        )

    def enhance_recognition_crop(self, image):
        self.enhanced_crops.append(image)
        return image

    def stretch_recognition_crop(self, image):
        self.stretched_crops.append(image)
        return ControlledNdarray(
            image.shape[0],
            round(image.shape[1] * WIDE_TEXT_HORIZONTAL_SCALE_FACTOR),
            tag=("stretched", image.tag),
            contiguous=True,
        )

    def rotate(self, image, degrees):
        height, width, _ = image.shape
        if degrees in {90, 270}:
            height, width = width, height
        return ControlledNdarray(
            height,
            width,
            tag=("rotated", image.tag, degrees),
            contiguous=True,
        )

    def dimensions(self, image):
        return image.shape[1], image.shape[0]

    def warmup_image(self):
        return ControlledNdarray(64, 256, tag="warmup", contiguous=True)


class FakePredictor:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.calls = []
        self.closed = False

    def predict(self, *, input, batch_size):
        self.calls.append((input, batch_size))
        return self.callback(input, batch_size)

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self, detector_callback, recognizer_callback) -> None:
        self.detector = FakePredictor(detector_callback)
        self.recognizer = FakePredictor(recognizer_callback)
        self.detector_kwargs = None
        self.recognizer_kwargs = None

    def create_detector(self, **kwargs):
        self.detector_kwargs = kwargs
        return self.detector

    def create_recognizer(self, **kwargs):
        self.recognizer_kwargs = kwargs
        return self.recognizer


def _default_detector(image, batch_size):
    if image.tag == "warmup":
        return [{}]
    return [{"dt_polys": [], "dt_scores": []}]


def _default_recognizer(images, batch_size):
    if images and images[0].tag == "warmup":
        return [{}]
    return [{"rec_text": "text", "rec_score": 0.9} for _ in images]


class OcrEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.detection_spec = self._make_model(root / "det", DETECTION_MODEL_NAME)
        self.recognition_spec = self._make_model(root / "rec", RECOGNITION_MODEL_NAME)

    @staticmethod
    def _make_model(directory: Path, model_name: str) -> LocalModelSpec:
        directory.mkdir()
        contents = {
            "inference.yml": f"Global:\n  model_name: {model_name}\n".encode(),
            "inference.json": b"model",
            "inference.pdiparams": b"parameters",
        }
        hashes = {}
        for name, content in contents.items():
            (directory / name).write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        return LocalModelSpec(model_name, directory, hashes)

    def _engine(
        self,
        detector_callback=_default_detector,
        recognizer_callback=_default_recognizer,
        *,
        engine_config=None,
    ):
        factory = FakeFactory(detector_callback, recognizer_callback)
        backend = FakeImageBackend()
        engine = OcrEngine(
            self.detection_spec,
            self.recognition_spec,
            predictor_factory=factory,
            image_backend=backend,
            engine_config=engine_config,
        )
        self.addCleanup(engine.close)
        return engine, factory, backend

    def test_initialization_uses_fixed_parameters_and_prewarms(self) -> None:
        engine, factory, _ = self._engine()
        self.assertIsNone(engine.initialize())
        self.assertTrue(engine.ready)
        self.assertEqual(factory.detector_kwargs["limit_side_len"], 1216)
        self.assertEqual(factory.detector_kwargs["limit_type"], "max")
        self.assertEqual(factory.detector_kwargs["thresh"], 0.3)
        self.assertEqual(factory.detector_kwargs["box_thresh"], 0.5)
        self.assertEqual(factory.detector_kwargs["unclip_ratio"], 1.5)
        config = factory.detector_kwargs["engine_config"]
        self.assertEqual(config["run_mode"], "mkldnn")
        self.assertEqual(config["cpu_threads"], 10)
        self.assertFalse(config["enable_new_ir"])
        self.assertEqual(factory.detector.calls[0][1], 1)
        self.assertEqual(factory.recognizer.calls[0][1], 1)
        self.assertIsNone(engine.close())
        self.assertTrue(factory.detector.closed)
        self.assertTrue(factory.recognizer.closed)

    @unittest.skipUnless(os.name == "nt", "Windows model path behavior")
    def test_unicode_model_root_uses_ascii_relative_predictor_paths(self) -> None:
        model_root = Path(self.temporary_directory.name) / "中文 path" / "models"
        model_root.mkdir(parents=True)
        detection = self._make_model(model_root / DETECTION_MODEL_NAME, DETECTION_MODEL_NAME)
        recognition = self._make_model(
            model_root / RECOGNITION_MODEL_NAME,
            RECOGNITION_MODEL_NAME,
        )

        class RecordingFactory(FakeFactory):
            def __init__(self) -> None:
                super().__init__(_default_detector, _default_recognizer)
                self.working_directories: list[Path] = []

            def create_detector(self, **kwargs):
                self.working_directories.append(Path.cwd())
                return super().create_detector(**kwargs)

            def create_recognizer(self, **kwargs):
                self.working_directories.append(Path.cwd())
                return super().create_recognizer(**kwargs)

        factory = RecordingFactory()
        engine = OcrEngine(
            detection,
            recognition,
            predictor_factory=factory,
            image_backend=FakeImageBackend(),
        )
        self.addCleanup(engine.close)
        original_working_directory = Path.cwd()

        self.assertIsNone(engine.initialize())

        self.assertEqual(factory.working_directories, [model_root.resolve()] * 2)
        self.assertEqual(
            factory.detector_kwargs["model_dir"],
            DETECTION_MODEL_NAME,
        )
        self.assertEqual(
            factory.recognizer_kwargs["model_dir"],
            RECOGNITION_MODEL_NAME,
        )
        self.assertEqual(Path.cwd(), original_working_directory)

    def test_explicit_arm_integration_override_has_no_silent_fallback(self) -> None:
        engine, factory, _ = self._engine(
            engine_config={"run_mode": "paddle", "cpu_threads": 1}
        )
        self.assertIsNone(engine.initialize())
        config = factory.detector_kwargs["engine_config"]
        self.assertEqual(config["run_mode"], "paddle")
        self.assertEqual(config["cpu_threads"], 1)
        self.assertFalse(config["enable_new_ir"])

    def test_hash_mismatch_is_sanitized_and_prevents_model_creation(self) -> None:
        bad_hashes = dict(self.detection_spec.files_sha256)
        bad_hashes["inference.json"] = "0" * 64
        self.detection_spec = LocalModelSpec(
            DETECTION_MODEL_NAME,
            self.detection_spec.directory,
            bad_hashes,
        )
        engine, factory, _ = self._engine()
        outcome = engine.initialize()
        self.assertIsInstance(outcome, Failure)
        self.assertEqual(outcome.diagnostic_code, "ocr-model-integrity")
        self.assertIsNone(factory.detector_kwargs)
        self.assertNotIn(
            self.temporary_directory.name,
            " ".join(
                (
                    outcome.error_type,
                    outcome.public_message,
                    outcome.diagnostic_code,
                )
            ),
        )

    def test_tiles_map_to_global_and_seam_fragments_are_recropped(self) -> None:
        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            tile_x = image.tag[1]
            if tile_x == 0:
                polys = [_quad(1000, 100, 1216, 120)]
            elif tile_x == 1088:
                polys = [_quad(0, 101, 262, 121)]
            else:
                polys = []
            return [{"dt_polys": polys, "dt_scores": [0.9] * len(polys)}]

        engine, _, backend = self._engine(detector_callback=detector)
        self.assertIsNone(engine.initialize())
        outcome = engine.recognize(ControlledNdarray(400, 2500))
        self.assertIsInstance(outcome, Success)
        self.assertEqual(outcome.result.text, "text")
        self.assertEqual(
            backend.perspective_quads,
            [_quad(1000, 100, 1350, 121)],
        )
        self.assertEqual(len(backend.enhanced_crops), 1)
        self.assertEqual(
            [image.tag[1] for image in backend.detector_inputs],
            [0, 1088, 2176],
        )
        self.assertTrue(all(image.contiguous for image in backend.detector_inputs))

    def test_orientation_policy_and_recognition_batch_size(self) -> None:
        boxes = [
            _quad(0, 0, 20, 40),
            _quad(40, 0, 140, 20),
            _quad(0, 60, 100, 80),
        ]

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": boxes, "dt_scores": [0.9, 0.8, 0.7]}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            results = []
            for image in images:
                if image.tag[0] == "crop":
                    left = image.tag[1][0][0]
                    if left == 0 and image.shape[0] == 40:
                        results.append({"rec_text": "vertical-bad", "rec_score": 0.4})
                    elif left == 40:
                        results.append({"rec_text": "low-bad", "rec_score": 0.2})
                    else:
                        results.append({"rec_text": "confident", "rec_score": 0.8})
                else:
                    original = image.tag[1]
                    rotation = image.tag[2]
                    left = original[1][0][0]
                    if left == 0 and rotation == 90:
                        results.append({"rec_text": "vertical", "rec_score": 0.95})
                    elif left == 40 and rotation == 180:
                        results.append({"rec_text": "upright", "rec_score": 0.9})
                    else:
                        results.append({"rec_text": "alternative", "rec_score": 0.5})
            return results

        engine, factory, _ = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())
        outcome = engine.recognize(ControlledNdarray(100, 200))
        self.assertIsInstance(outcome, Success)
        self.assertIn("vertical", outcome.result.text)
        self.assertIn("upright", outcome.result.text)
        self.assertIn("confident", outcome.result.text)
        runtime_calls = factory.recognizer.calls[1:]
        self.assertTrue(runtime_calls)
        self.assertTrue(all(batch_size == 8 for _, batch_size in runtime_calls))
        rotations = {
            image.tag[2]
            for images, _ in runtime_calls
            for image in images
            if image.tag[0] == "rotated"
        }
        self.assertEqual(rotations, {90, 180, 270})

    def test_dense_code_outline_uses_targeted_horizontal_retry(self) -> None:
        box = _quad(0, 0, 500, 20)
        initial_text = (
            "@0|_init_.py|TARGET_AST_UNAVAILABLE;"
            "M1|_init_.py::module|UNAVAILABLE|"
        )
        improved_text = (
            "@0|__init__.py|TARGET_AST_UNAVAILABLE;"
            "M1|__init__.py::module|UNAVAILABLE|"
        )

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": [box], "dt_scores": [0.9]}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            return [
                {
                    "rec_text": (
                        improved_text
                        if image.tag[0] == "stretched"
                        else initial_text
                    ),
                    "rec_score": 0.98 if image.tag[0] == "stretched" else 0.99,
                }
                for image in images
            ]

        engine, factory, backend = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())

        outcome = engine.recognize(ControlledNdarray(40, 500))

        self.assertIsInstance(outcome, Success)
        self.assertEqual(outcome.result.text, improved_text)
        self.assertEqual(len(backend.stretched_crops), 1)
        self.assertEqual(len(factory.recognizer.calls[1:]), 2)

    def test_only_exact_empty_string_is_dropped_before_blank_layout(self) -> None:
        boxes = [_quad(0, 0, 20, 20), _quad(40, 0, 60, 20)]

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": boxes, "dt_scores": [0.9, 0.9]}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            return [
                {"rec_text": "", "rec_score": 0.9},
                {"rec_text": " ", "rec_score": 0.9},
            ]

        engine, _, _ = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())
        captured_texts = []

        def capture_layout(spans):
            captured_texts.extend(span.text for span in spans)
            return build_layout(spans)

        with patch("textsnap.ocr.build_layout", side_effect=capture_layout):
            outcome = engine.recognize(ControlledNdarray(40, 100))
        self.assertIsInstance(outcome, Empty)
        self.assertEqual(captured_texts, [" "])

    def test_visible_text_keeps_internal_whitespace(self) -> None:
        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": [_quad(0, 0, 80, 20)], "dt_scores": [0.9]}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            return [{"rec_text": "left  right", "rec_score": 0.9}]

        engine, _, _ = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())
        outcome = engine.recognize(ControlledNdarray(40, 100))
        self.assertIsInstance(outcome, Success)
        self.assertEqual(outcome.result.text, "left  right")

    def test_cancellation_is_observed_after_uninterruptible_tile_call(self) -> None:
        cancel_event = Event()

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            cancel_event.set()
            return [{"dt_polys": [], "dt_scores": []}]

        engine, factory, _ = self._engine(detector_callback=detector)
        self.assertIsNone(engine.initialize())
        outcome = engine.recognize(ControlledNdarray(400, 2500), cancel_event)
        self.assertIsInstance(outcome, Cancelled)
        self.assertEqual(len(factory.detector.calls), 2)
        self.assertEqual(len(factory.recognizer.calls), 1)

    def test_cancellation_is_observed_between_recognition_batches(self) -> None:
        cancel_event = Event()
        boxes = [_quad(index * 30, 0, index * 30 + 20, 20) for index in range(9)]

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": boxes, "dt_scores": [0.9] * len(boxes)}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            cancel_event.set()
            return [{"rec_text": "text", "rec_score": 0.9} for _ in images]

        engine, factory, _ = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())
        outcome = engine.recognize(ControlledNdarray(40, 300), cancel_event)
        self.assertIsInstance(outcome, Cancelled)
        runtime_calls = factory.recognizer.calls[1:]
        self.assertEqual(len(runtime_calls), 1)
        self.assertEqual(len(runtime_calls[0][0]), 8)
        self.assertEqual(runtime_calls[0][1], 8)

    def test_cancellation_is_observed_before_empty_rotation_flush(self) -> None:
        armed = Event()
        cancel_event = StagedCancelEvent(armed, cancel_on_armed_check=2)

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": [_quad(0, 0, 20, 20)], "dt_scores": [0.9]}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            armed.set()
            return [{"rec_text": "text", "rec_score": 0.9}]

        engine, _, _ = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())
        with patch("textsnap.ocr.build_layout") as layout_builder:
            outcome = engine.recognize(ControlledNdarray(40, 100), cancel_event)
        self.assertIsInstance(outcome, Cancelled)
        self.assertEqual(cancel_event.armed_checks, 2)
        layout_builder.assert_not_called()

    def test_cancellation_is_observed_before_success_is_returned(self) -> None:
        armed = Event()
        cancel_event = StagedCancelEvent(armed, cancel_on_armed_check=3)

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": [_quad(0, 0, 20, 20)], "dt_scores": [0.9]}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            armed.set()
            return [{"rec_text": "text", "rec_score": 0.9}]

        engine, _, _ = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())
        with patch("textsnap.ocr.build_layout", wraps=build_layout) as layout_builder:
            outcome = engine.recognize(ControlledNdarray(40, 100), cancel_event)
        self.assertIsInstance(outcome, Cancelled)
        self.assertEqual(cancel_event.armed_checks, 3)
        layout_builder.assert_called_once()

    def test_cancellation_during_final_empty_span_scan_wins_over_empty(self) -> None:
        cancel_event = Event()

        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            return [{"dt_polys": [_quad(0, 0, 20, 20)], "dt_scores": [0.9]}]

        def recognizer(images, batch_size):
            if images and images[0].tag == "warmup":
                return [{}]
            return [{"rec_text": "", "rec_score": 0.9}]

        def cancel_then_select(attempts):
            cancel_event.set()
            return best_attempt(attempts)

        engine, _, _ = self._engine(detector, recognizer)
        self.assertIsNone(engine.initialize())
        with patch("textsnap.ocr.best_attempt", side_effect=cancel_then_select):
            outcome = engine.recognize(ControlledNdarray(40, 100), cancel_event)
        self.assertIsInstance(outcome, Cancelled)

    def test_predictor_exception_cannot_leak_path_or_ocr_text(self) -> None:
        def detector(image, batch_size):
            if image.tag == "warmup":
                return [{}]
            # Paddle uses ValueError for unsupported runner configurations;
            # this is an inference failure, not an invalid screenshot.
            raise ValueError("/secret/capture.png TOP SECRET")

        engine, _, _ = self._engine(detector_callback=detector)
        self.assertIsNone(engine.initialize())
        outcome = engine.recognize(ControlledNdarray(40, 100))
        self.assertIsInstance(outcome, Failure)
        public = " ".join(
            (outcome.error_type, outcome.public_message, outcome.diagnostic_code)
        )
        self.assertNotIn("/secret", public)
        self.assertNotIn("TOP SECRET", public)
        self.assertEqual(outcome.diagnostic_code, "ocr-inference-failed")

    def test_normal_ocr_path_performs_no_file_writes(self) -> None:
        engine, _, _ = self._engine()
        self.assertIsNone(engine.initialize())

        with _deny_python_file_writes():
            outcome = engine.recognize(ControlledNdarray(40, 100))
        self.assertNotIsInstance(outcome, Failure)

    def test_file_write_guard_catches_pathlib_tempfile_and_os_writes(self) -> None:
        root = Path(self.temporary_directory.name)
        path_target = root / "pathlib-write.txt"
        tempfile_target_prefix = "tempfile-write-"
        os_target = root / "os-write.bin"

        with self.subTest(entrypoint="pathlib"):
            with self.assertRaisesRegex(AssertionError, "unexpected Python file write"):
                with _deny_python_file_writes():
                    path_target.write_text("blocked", encoding="utf-8")
        with self.subTest(entrypoint="tempfile"):
            with self.assertRaisesRegex(AssertionError, "unexpected Python file write"):
                with _deny_python_file_writes():
                    tempfile.NamedTemporaryFile(
                        dir=root,
                        prefix=tempfile_target_prefix,
                    )
        with self.subTest(entrypoint="os"):
            with self.assertRaisesRegex(AssertionError, "unexpected Python file write"):
                with _deny_python_file_writes():
                    os.open(os_target, os.O_CREAT | os.O_WRONLY)

        self.assertFalse(path_target.exists())
        self.assertFalse(any(root.glob(f"{tempfile_target_prefix}*")))
        self.assertFalse(os_target.exists())


class OpenCvImageBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        missing = [
            module_name
            for module_name in ("numpy", "cv2")
            if importlib.util.find_spec(module_name) is None
        ]
        if missing:
            self.skipTest(f"{', '.join(missing)} required")

    def test_perspective_crop_preserves_convex_ring_order_for_diamond(self) -> None:
        import numpy

        backend = _OpenCvImageBackend()
        image = numpy.arange(12 * 12 * 3, dtype=numpy.uint8).reshape((12, 12, 3))
        quad = ((5.0, 0.0), (10.0, 5.0), (5.0, 10.0), (0.0, 5.0))
        sources = []
        real_get_transform = backend._cv2.getPerspectiveTransform

        def capture_source(source, destination):
            sources.append(source.copy())
            return real_get_transform(source, destination)

        with patch.object(
            backend._cv2,
            "getPerspectiveTransform",
            side_effect=capture_source,
        ):
            crop = backend.perspective_crop(image, quad)

        self.assertEqual(crop.shape, (7, 7, 3))
        self.assertEqual(len(sources), 1)
        numpy.testing.assert_array_equal(
            sources[0],
            numpy.asarray(quad, dtype=numpy.float32),
        )

    def test_perspective_crop_rejects_non_convex_and_degenerate_quads(self) -> None:
        import numpy

        backend = _OpenCvImageBackend()
        image = numpy.zeros((12, 12, 3), dtype=numpy.uint8)
        invalid_quads = (
            ((1.0, 1.0), (10.0, 1.0), (10.0, 10.0), (10.0, 10.0)),
            ((1.0, 1.0), (10.0, 1.0), (5.0, 5.0), (1.0, 10.0)),
            ((1.0, 1.0), (5.0, 1.0), (10.0, 1.0), (1.0, 10.0)),
            ((1.0, 1.0), (10.0, 10.0), (1.0, 10.0), (10.0, 1.0)),
            ((1.0, 1.0), (1.6, 1.0), (1.6, 10.0), (1.0, 10.0)),
        )

        for quad in invalid_quads:
            with self.subTest(quad=quad):
                with self.assertRaises(ValueError):
                    backend.perspective_crop(image, quad)

    def test_tiny_recognition_crop_is_cubically_upscaled_and_contiguous(self) -> None:
        import numpy

        backend = _OpenCvImageBackend()
        image = numpy.zeros(
            (SMALL_TEXT_CROP_HEIGHT - 1, 37, 3),
            dtype=numpy.uint8,
        )

        enhanced = backend.enhance_recognition_crop(image)

        self.assertEqual(
            enhanced.shape,
            (
                (SMALL_TEXT_CROP_HEIGHT - 1) * SMALL_TEXT_SCALE_FACTOR,
                37 * SMALL_TEXT_SCALE_FACTOR,
                3,
            ),
        )
        self.assertTrue(enhanced.flags.c_contiguous)
        self.assertIsNot(enhanced, image)

    def test_non_tiny_recognition_crop_is_not_resampled(self) -> None:
        import numpy

        backend = _OpenCvImageBackend()
        image = numpy.zeros(
            (SMALL_TEXT_CROP_HEIGHT, 1000, 3),
            dtype=numpy.uint8,
        )

        self.assertIs(backend.enhance_recognition_crop(image), image)

    def test_explicit_code_retry_crop_is_only_stretched_horizontally(self) -> None:
        import numpy

        backend = _OpenCvImageBackend()
        height = WIDE_TEXT_MAX_HEIGHT
        width = int(height * WIDE_TEXT_MIN_ASPECT_RATIO)
        image = numpy.zeros((height, width, 3), dtype=numpy.uint8)

        enhanced = backend.stretch_recognition_crop(image)

        self.assertEqual(
            enhanced.shape,
            (
                height,
                int(round(width * WIDE_TEXT_HORIZONTAL_SCALE_FACTOR)),
                3,
            ),
        )
        self.assertTrue(enhanced.flags.c_contiguous)


if __name__ == "__main__":
    unittest.main()
