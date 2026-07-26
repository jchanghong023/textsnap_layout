from __future__ import annotations

from dataclasses import FrozenInstanceError
import gc
import importlib.util
import os
from threading import Event
import time
import unittest
import weakref

from textsnap.domain import (
    Cancelled,
    Empty,
    Failure,
    ModelState,
    TaskState,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PYSIDE_AVAILABLE = importlib.util.find_spec("PySide6") is not None

if _PYSIDE_AVAILABLE:
    from PySide6.QtCore import QCoreApplication, QThread
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import QApplication

if not _PYSIDE_AVAILABLE:
    _PYSIDE_SKIP_REASON = "PySide6 is not installed"
else:
    _PYSIDE_SKIP_REASON = ""
if _PYSIDE_AVAILABLE:
    from textsnap.qt_worker import OcrTask, OcrThreadController


class _SensitiveImage:
    def __repr__(self) -> str:
        return "/secret/capture.png OCR-CONTENT"


class FakeEngine:
    def __init__(
        self,
        events: list[tuple[str, str]],
        *,
        initialize_result: Failure | None = None,
        outcome: object | None = None,
        block_recognition: bool = False,
        close_result: Failure | None = None,
    ) -> None:
        self.events = events
        self.initialize_result = initialize_result
        self.outcome = Empty() if outcome is None else outcome
        self.block_recognition = block_recognition
        self.close_result = close_result
        self.recognition_started = Event()
        self.release_recognition = Event()
        self.recognize_calls = 0

    @staticmethod
    def _thread_name() -> str:
        return QThread.currentThread().objectName()

    def initialize(self):
        self.events.append(("initialize", self._thread_name()))
        return self.initialize_result

    def recognize(self, image_bgr, cancel_event=None):
        self.events.append(("recognize-start", self._thread_name()))
        self.recognize_calls += 1
        self.recognition_started.set()
        if self.block_recognition:
            while (
                not self.release_recognition.is_set()
                and cancel_event is not None
                and not cancel_event.is_set()
            ):
                self.release_recognition.wait(0.01)
        if cancel_event is not None and cancel_event.is_set():
            outcome = Cancelled()
        else:
            outcome = self.outcome
        self.events.append(("recognize-end", self._thread_name()))
        return outcome

    def close(self):
        self.events.append(("close", self._thread_name()))
        return self.close_result


@unittest.skipUnless(_PYSIDE_AVAILABLE, _PYSIDE_SKIP_REASON)
class QtWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # QApplication is a QCoreApplication and can also be reused by widget
        # tests discovered later in the same process.
        cls.application = QCoreApplication.instance() or QApplication([])
        QThread.currentThread().setObjectName("textsnap-test-gui")

    def setUp(self) -> None:
        self.controllers: list[OcrThreadController] = []

    def tearDown(self) -> None:
        for controller in reversed(self.controllers):
            if controller.running:
                controller.shutdown()
                self.assertTrue(
                    self._wait_until(lambda: not controller.running),
                    "OCR worker thread did not stop during test cleanup",
                )
                controller.thread.wait(1000)
            controller.deleteLater()
        self.application.processEvents()

    def _controller(
        self,
        engine: FakeEngine,
        *,
        factory_exception: Exception | None = None,
    ) -> OcrThreadController:
        def factory():
            engine.events.append(("factory", QThread.currentThread().objectName()))
            if factory_exception is not None:
                raise factory_exception
            return engine

        controller = OcrThreadController(factory)
        self.controllers.append(controller)
        return controller

    def _start_ready(self, controller: OcrThreadController) -> None:
        controller.start()
        self.assertTrue(
            self._wait_until(lambda: controller.model_state is ModelState.READY),
            "OCR worker did not become ready",
        )

    def _wait_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.application.processEvents()
        return bool(predicate())

    def test_all_engine_lifecycle_calls_run_in_dedicated_thread(self) -> None:
        events: list[tuple[str, str]] = []
        engine = FakeEngine(events)
        controller = self._controller(engine)
        model_spy = QSignalSpy(controller.model_state_changed)
        task_state_spy = QSignalSpy(controller.task_state_changed)
        finished_spy = QSignalSpy(controller.task_finished)
        shutdown_spy = QSignalSpy(controller.shutdown_finished)

        self._start_ready(controller)
        task = controller.submit(_SensitiveImage())
        self.assertIsNotNone(task)
        self.assertTrue(
            self._wait_until(lambda: finished_spy.count() == 1),
            "OCR task did not finish",
        )
        controller.shutdown()
        self.assertTrue(
            self._wait_until(lambda: shutdown_spy.count() == 1),
            "OCR worker did not finish shutdown",
        )

        self.assertFalse(controller.running)
        self.assertEqual(
            [model_spy.at(index)[0] for index in range(model_spy.count())],
            [ModelState.LOADING, ModelState.READY],
        )
        self.assertEqual(
            [task_state_spy.at(index)[0] for index in range(task_state_spy.count())],
            [TaskState.RECOGNIZING, TaskState.IDLE],
        )
        self.assertEqual(finished_spy.at(0)[0], task.task_id)
        self.assertIsInstance(finished_spy.at(0)[1], Empty)
        self.assertTrue(events)
        self.assertTrue(
            all(thread_name == "textsnap-ocr-worker" for _, thread_name in events)
        )

    def test_busy_submission_is_rejected_synchronously_and_not_queued(self) -> None:
        events: list[tuple[str, str]] = []
        engine = FakeEngine(events, block_recognition=True)
        controller = self._controller(engine)
        rejected_spy = QSignalSpy(controller.task_rejected)
        finished_spy = QSignalSpy(controller.task_finished)
        self._start_ready(controller)

        first = controller.submit(object())
        second = controller.submit(object())
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(rejected_spy.count(), 1)
        rejection = rejected_spy.at(0)[1]
        self.assertIsInstance(rejection, Failure)
        self.assertEqual(rejection.diagnostic_code, "ocr-task-busy")

        self.assertTrue(engine.recognition_started.wait(1.0))
        engine.release_recognition.set()
        self.assertTrue(self._wait_until(lambda: finished_spy.count() == 1))
        self.assertEqual(engine.recognize_calls, 1)

    def test_gui_can_set_task_cancel_event_without_queued_cancel_slot(self) -> None:
        events: list[tuple[str, str]] = []
        engine = FakeEngine(events, block_recognition=True)
        controller = self._controller(engine)
        finished_spy = QSignalSpy(controller.task_finished)
        self._start_ready(controller)

        task = controller.submit(object())
        self.assertIsNotNone(task)
        self.assertTrue(engine.recognition_started.wait(1.0))
        task.cancel_event.set()
        self.assertTrue(self._wait_until(lambda: finished_spy.count() == 1))
        self.assertIsInstance(finished_spy.at(0)[1], Cancelled)

    def test_shutdown_cancels_then_closes_then_quits_without_terminate(self) -> None:
        events: list[tuple[str, str]] = []
        engine = FakeEngine(events, block_recognition=True)
        controller = self._controller(engine)
        finished_spy = QSignalSpy(controller.task_finished)
        shutdown_spy = QSignalSpy(controller.shutdown_finished)
        self._start_ready(controller)

        task = controller.submit(object())
        self.assertIsNotNone(task)
        self.assertTrue(engine.recognition_started.wait(1.0))
        controller.shutdown()
        self.assertTrue(task.cancel_event.is_set())
        self.assertTrue(self._wait_until(lambda: shutdown_spy.count() == 1))

        self.assertEqual(finished_spy.count(), 1)
        self.assertIsInstance(finished_spy.at(0)[1], Cancelled)
        event_names = [name for name, _ in events]
        self.assertLess(event_names.index("recognize-end"), event_names.index("close"))
        self.assertFalse(controller.running)

    def test_shutdown_wait_does_not_depend_on_gui_signal_delivery(self) -> None:
        events: list[tuple[str, str]] = []
        engine = FakeEngine(events)
        controller = self._controller(engine)
        self._start_ready(controller)

        controller.shutdown()

        self.assertTrue(controller.wait_for_shutdown())
        self.assertFalse(controller.running)
        self.assertIn(("close", "textsnap-ocr-worker"), events)

    def test_shutdown_wait_reports_close_failure_without_gui_signal_delivery(
        self,
    ) -> None:
        events: list[tuple[str, str]] = []
        failure = Failure(
            "OcrShutdownError",
            "The OCR engine did not shut down cleanly.",
            "ocr-close-failed",
        )
        engine = FakeEngine(events, close_result=failure)
        controller = self._controller(engine)
        self._start_ready(controller)

        controller.shutdown()

        self.assertFalse(controller.wait_for_shutdown())
        self.assertFalse(controller.running)
        self.assertIs(controller.shutdown_failure, failure)
        self.assertIn(("close", "textsnap-ocr-worker"), events)

    def test_shutdown_wait_releases_active_task_without_gui_signal_delivery(
        self,
    ) -> None:
        events: list[tuple[str, str]] = []
        engine = FakeEngine(events, block_recognition=True)
        controller = self._controller(engine)
        rejected_spy = QSignalSpy(controller.task_rejected)
        self._start_ready(controller)
        image = _SensitiveImage()
        image_reference = weakref.ref(image)

        task = controller.submit(image)
        self.assertIsNotNone(task)
        self.assertTrue(engine.recognition_started.wait(1.0))
        controller.shutdown()
        del image
        del task

        self.assertTrue(controller.wait_for_shutdown())
        self.assertIsNone(controller.active_task)
        self.assertEqual(controller.task_state, TaskState.IDLE)
        gc.collect()
        self.assertIsNone(image_reference())

        self.application.processEvents()
        self.assertEqual(rejected_spy.count(), 0)

    def test_model_failure_and_factory_exception_are_sanitized(self) -> None:
        events: list[tuple[str, str]] = []
        engine = FakeEngine(events)
        controller = self._controller(
            engine,
            factory_exception=RuntimeError("/secret/model/path OCR-CONTENT"),
        )
        failure_spy = QSignalSpy(controller.model_failed)
        rejected_spy = QSignalSpy(controller.task_rejected)
        controller.start()
        self.assertTrue(
            self._wait_until(lambda: controller.model_state is ModelState.ERROR)
        )

        self.assertEqual(failure_spy.count(), 1)
        failure = failure_spy.at(0)[0]
        self.assertIsInstance(failure, Failure)
        public = " ".join(
            (failure.error_type, failure.public_message, failure.diagnostic_code)
        )
        self.assertNotIn("/secret", public)
        self.assertNotIn("OCR-CONTENT", public)
        self.assertIsNone(controller.submit(_SensitiveImage()))
        self.assertEqual(rejected_spy.at(0)[1].diagnostic_code, "ocr-model-not-ready")

    def test_model_retry_closes_failed_engine_then_creates_a_new_one(self) -> None:
        events: list[tuple[str, str]] = []
        first = FakeEngine(
            events,
            initialize_result=Failure(
                "ModelValidationError",
                "Local OCR models are missing or damaged.",
                "ocr-model-integrity",
            ),
        )
        second = FakeEngine(events)
        engines = iter((first, second))

        def factory():
            events.append(("factory", QThread.currentThread().objectName()))
            return next(engines)

        controller = OcrThreadController(factory)
        self.controllers.append(controller)
        model_spy = QSignalSpy(controller.model_state_changed)
        finished_spy = QSignalSpy(controller.task_finished)
        controller.start()
        self.assertTrue(
            self._wait_until(lambda: controller.model_state is ModelState.ERROR)
        )

        self.assertTrue(controller.retry_model())
        self.assertFalse(controller.retry_model())
        self.assertTrue(
            self._wait_until(lambda: controller.model_state is ModelState.READY)
        )
        self.assertEqual(
            [model_spy.at(index)[0] for index in range(model_spy.count())],
            [
                ModelState.LOADING,
                ModelState.ERROR,
                ModelState.LOADING,
                ModelState.READY,
            ],
        )
        self.assertEqual(
            [name for name, _ in events[:5]],
            ["factory", "initialize", "close", "factory", "initialize"],
        )
        self.assertTrue(
            all(thread_name == "textsnap-ocr-worker" for _, thread_name in events)
        )

        task = controller.submit(object())
        self.assertIsNotNone(task)
        self.assertTrue(self._wait_until(lambda: finished_spy.count() == 1))
        self.assertEqual(first.recognize_calls, 0)
        self.assertEqual(second.recognize_calls, 1)
        self.assertFalse(controller.retry_model())

    def test_task_boundary_is_frozen_and_repr_hides_pixels(self) -> None:
        task = OcrTask(1, _SensitiveImage())
        self.assertNotIn("/secret", repr(task))
        self.assertNotIn("OCR-CONTENT", repr(task))
        with self.assertRaises(FrozenInstanceError):
            task.task_id = 2
        task.cancel_event.set()
        self.assertTrue(task.cancel_event.is_set())


if __name__ == "__main__":
    unittest.main()
