from __future__ import annotations

import builtins
import hashlib
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from textsnap.domain import Cancelled, Failure, Success
from textsnap.ocr import (
    DETECTION_MODEL_NAME,
    RECOGNITION_MODEL_NAME,
    LocalModelSpec,
    OcrEngine,
)


def _quad(left: float, top: float, right: float, bottom: float):
    return ((left, top), (right, top), (right, bottom), (left, bottom))


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

    def test_only_exact_empty_string_is_dropped(self) -> None:
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
        outcome = engine.recognize(ControlledNdarray(40, 100))
        self.assertIsInstance(outcome, Success)
        self.assertEqual(outcome.result.stats.output_spans, 1)

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

        def reject_write(*args, **kwargs):
            raise AssertionError("unexpected file access")

        with patch.object(builtins, "open", reject_write):
            outcome = engine.recognize(ControlledNdarray(40, 100))
        self.assertNotIsInstance(outcome, Failure)


if __name__ == "__main__":
    unittest.main()
