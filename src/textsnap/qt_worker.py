"""Qt thread boundary for the resident OCR engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from time import monotonic_ns
from typing import Protocol

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from .domain import (
    Cancelled,
    Empty,
    Failure,
    ModelState,
    Success,
    TaskOutcome,
    TaskState,
)
from .runtime_diagnostics import record_runtime_event


_OUTCOME_TYPES = (Success, Empty, Cancelled, Failure)


class OcrEngineProtocol(Protocol):
    def initialize(self) -> Failure | None: ...

    def recognize(
        self, image_bgr: object, cancel_event: Event | None = None
    ) -> TaskOutcome: ...

    def close(self) -> Failure | None: ...


@dataclass(frozen=True, slots=True)
class OcrTask:
    """Immutable ownership boundary for one in-memory OCR request."""

    task_id: int
    image_bgr: object = field(repr=False, compare=False)
    cancel_event: Event = field(default_factory=Event, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.task_id <= 0:
            raise ValueError("task_id must be positive")


@dataclass(slots=True)
class _ShutdownStatus:
    """Plain-Python handoff readable after the GUI event loop has stopped."""

    completed: Event = field(default_factory=Event)
    failure: Failure | None = None


def _failure(error_type: str, public_message: str, diagnostic_code: str) -> Failure:
    return Failure(
        error_type=error_type,
        public_message=public_message,
        diagnostic_code=diagnostic_code,
    )


class _OcrWorker(QObject):
    """Runs every engine method in its owning QThread."""

    model_state_changed = Signal(object)
    model_failed = Signal(object)
    task_completed = Signal(int, object)
    task_rejected = Signal(int, object)
    close_completed = Signal(object)

    def __init__(
        self,
        engine_factory: Callable[[], OcrEngineProtocol],
        shutdown_status: _ShutdownStatus,
    ) -> None:
        super().__init__()
        self._engine_factory = engine_factory
        self._shutdown_status = shutdown_status
        self._engine: OcrEngineProtocol | None = None
        self._model_state = ModelState.LOADING
        self._busy = False
        self._shutting_down = False

    @Slot()
    def initialize(self) -> None:
        if self._shutting_down or self._engine is not None:
            return
        self._initialize_engine()

    @Slot()
    def retry_initialize(self) -> None:
        if (
            self._shutting_down
            or self._busy
            or self._model_state is not ModelState.ERROR
        ):
            return
        self._model_state = ModelState.LOADING
        self.model_state_changed.emit(ModelState.LOADING)
        record_runtime_event("ocr.model-retry")
        close_failure = self._close_engine()
        if close_failure is not None:
            record_runtime_event(
                "ocr.model-retry-close-failed",
                diagnostic_code=close_failure.diagnostic_code,
            )
            self._model_state = ModelState.ERROR
            self.model_state_changed.emit(ModelState.ERROR)
            self.model_failed.emit(close_failure)
            return
        self._initialize_engine(emit_loading=False)

    def _initialize_engine(self, *, emit_loading: bool = True) -> None:
        started_ns = monotonic_ns()
        record_runtime_event(
            "ocr.model-load-start",
            retry=not emit_loading,
        )
        self._model_state = ModelState.LOADING
        if emit_loading:
            self.model_state_changed.emit(ModelState.LOADING)
        try:
            self._engine = self._engine_factory()
            result = self._engine.initialize()
            if result is not None and not isinstance(result, Failure):
                result = _failure(
                    "OcrWorkerInitializationError",
                    "The OCR worker could not initialize the model.",
                    "ocr-worker-initialize-contract",
                )
        except Exception as exc:
            record_runtime_event(
                "ocr.model-load-exception",
                exception_type=type(exc).__name__,
            )
            result = _failure(
                "OcrWorkerInitializationError",
                "The OCR worker could not initialize the model.",
                "ocr-worker-initialize-failed",
            )

        if isinstance(result, Failure):
            record_runtime_event(
                "ocr.model-load-failed",
                diagnostic_code=result.diagnostic_code,
                duration_ms=(monotonic_ns() - started_ns) / 1_000_000,
            )
            self._model_state = ModelState.ERROR
            self.model_state_changed.emit(ModelState.ERROR)
            self.model_failed.emit(result)
            return

        self._model_state = ModelState.READY
        record_runtime_event(
            "ocr.model-ready",
            duration_ms=(monotonic_ns() - started_ns) / 1_000_000,
        )
        self.model_state_changed.emit(ModelState.READY)

    def _close_engine(self) -> Failure | None:
        engine = self._engine
        self._engine = None
        if engine is None:
            return None
        try:
            result = engine.close()
            if result is not None and not isinstance(result, Failure):
                return _failure(
                    "OcrWorkerShutdownError",
                    "The OCR worker did not shut down cleanly.",
                    "ocr-worker-close-contract",
                )
            return result
        except Exception:
            return _failure(
                "OcrWorkerShutdownError",
                "The OCR worker did not shut down cleanly.",
                "ocr-worker-close-failed",
            )

    @Slot(object)
    def run_task(self, task: object) -> None:
        if not isinstance(task, OcrTask):
            record_runtime_event("ocr.task-rejected", reason="invalid")
            self.task_rejected.emit(
                0,
                _failure(
                    "OcrTaskBoundaryError",
                    "The OCR task was invalid.",
                    "ocr-task-invalid",
                ),
            )
            return
        if self._shutting_down:
            record_runtime_event(
                "ocr.task-rejected",
                reason="shutdown",
                task_id=task.task_id,
            )
            self.task_rejected.emit(
                task.task_id,
                _failure(
                    "OcrWorkerShutdownError",
                    "The OCR worker is shutting down.",
                    "ocr-worker-shutting-down",
                ),
            )
            return
        if self._busy:
            record_runtime_event(
                "ocr.task-rejected",
                reason="busy",
                task_id=task.task_id,
            )
            self.task_rejected.emit(
                task.task_id,
                _failure(
                    "OcrTaskBusyError",
                    "Text recognition is already running.",
                    "ocr-task-busy",
                ),
            )
            return
        if self._model_state is not ModelState.READY or self._engine is None:
            record_runtime_event(
                "ocr.task-rejected",
                reason="model-not-ready",
                task_id=task.task_id,
            )
            self.task_rejected.emit(
                task.task_id,
                _failure(
                    "OcrModelNotReadyError",
                    "The OCR model is not ready.",
                    "ocr-model-not-ready",
                ),
            )
            return

        self._busy = True
        started_ns = monotonic_ns()
        record_runtime_event("ocr.task-start", task_id=task.task_id)
        try:
            outcome = self._engine.recognize(task.image_bgr, task.cancel_event)
            if not isinstance(outcome, _OUTCOME_TYPES):
                outcome = _failure(
                    "OcrWorkerContractError",
                    "Text recognition failed.",
                    "ocr-worker-outcome-contract",
                )
        except Exception as exc:
            record_runtime_event(
                "ocr.task-exception",
                exception_type=type(exc).__name__,
                task_id=task.task_id,
            )
            outcome = _failure(
                "OcrWorkerInferenceError",
                "Text recognition failed.",
                "ocr-worker-inference-failed",
            )
        finally:
            self._busy = False
        record_runtime_event(
            "ocr.task-complete",
            duration_ms=(monotonic_ns() - started_ns) / 1_000_000,
            outcome_type=type(outcome).__name__,
            task_id=task.task_id,
        )
        self.task_completed.emit(task.task_id, outcome)

    @Slot()
    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        record_runtime_event("ocr.shutdown-start")

        result = self._close_engine()
        record_runtime_event(
            "ocr.shutdown-complete",
            diagnostic_code=(result.diagnostic_code if result is not None else None),
            success=result is None,
        )
        self._shutdown_status.failure = result
        self._shutdown_status.completed.set()
        self.close_completed.emit(result)
        # QCoreApplication may already be leaving its GUI event loop (for
        # example during a Windows session shutdown). Quit the worker loop from
        # its own thread so cleanup does not depend on a queued GUI callback.
        owning_thread = self.thread()
        if owning_thread is not None:
            owning_thread.quit()


class OcrThreadController(QObject):
    """GUI-thread controller for exactly one resident OCR worker.

    ``engine_factory`` is invoked in the worker thread and must return a fresh
    engine on every call so a failed model load can be retried safely.
    """

    model_state_changed = Signal(object)
    model_failed = Signal(object)
    task_state_changed = Signal(object)
    task_finished = Signal(int, object)
    task_rejected = Signal(int, object)
    shutdown_finished = Signal(object)

    _task_requested = Signal(object)
    _retry_requested = Signal()
    _shutdown_requested = Signal()

    def __init__(
        self,
        engine_factory: Callable[[], OcrEngineProtocol],
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._thread.setObjectName("textsnap-ocr-worker")
        self._shutdown_status = _ShutdownStatus()
        self._worker = _OcrWorker(engine_factory, self._shutdown_status)
        self._worker.moveToThread(self._thread)

        self._model_state = ModelState.LOADING
        self._task_state = TaskState.IDLE
        self._active_task: OcrTask | None = None
        self._next_task_id = 1
        self._started = False
        self._shutting_down = False
        self._retry_pending = False
        self._close_result: Failure | None = None
        self._shutdown_reported = False

        self._thread.started.connect(
            self._worker.initialize, Qt.ConnectionType.QueuedConnection
        )
        self._task_requested.connect(
            self._worker.run_task, Qt.ConnectionType.QueuedConnection
        )
        self._retry_requested.connect(
            self._worker.retry_initialize,
            Qt.ConnectionType.QueuedConnection,
        )
        self._shutdown_requested.connect(
            self._worker.shutdown, Qt.ConnectionType.QueuedConnection
        )
        self._worker.model_state_changed.connect(
            self._on_model_state, Qt.ConnectionType.QueuedConnection
        )
        self._worker.model_failed.connect(
            self._on_model_failure, Qt.ConnectionType.QueuedConnection
        )
        self._worker.task_completed.connect(
            self._on_task_completed, Qt.ConnectionType.QueuedConnection
        )
        self._worker.task_rejected.connect(
            self._on_worker_rejection, Qt.ConnectionType.QueuedConnection
        )
        self._worker.close_completed.connect(
            self._on_worker_closed, Qt.ConnectionType.QueuedConnection
        )
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)

    @property
    def model_state(self) -> ModelState:
        return self._model_state

    @property
    def task_state(self) -> TaskState:
        return self._task_state

    @property
    def active_task(self) -> OcrTask | None:
        return self._active_task

    @property
    def thread(self) -> QThread:
        return self._thread

    @property
    def running(self) -> bool:
        return self._thread.isRunning()

    @property
    def shutdown_failure(self) -> Failure | None:
        if not self._shutdown_status.completed.is_set():
            return None
        return self._shutdown_status.failure

    def start(self) -> None:
        if self._shutting_down:
            raise RuntimeError("cannot start an OCR worker during shutdown")
        if self._started:
            return
        self._started = True
        record_runtime_event("ocr.thread-start")
        self._thread.start()

    def submit(self, image_bgr: object) -> OcrTask | None:
        task_id = self._allocate_task_id()
        if self._shutting_down:
            self._reject_locally(
                task_id,
                _failure(
                    "OcrWorkerShutdownError",
                    "The OCR worker is shutting down.",
                    "ocr-worker-shutting-down",
                ),
            )
            return None
        if self._active_task is not None:
            self._reject_locally(
                task_id,
                _failure(
                    "OcrTaskBusyError",
                    "Text recognition is already running.",
                    "ocr-task-busy",
                ),
            )
            return None
        if (
            not self._started
            or self._model_state is not ModelState.READY
            or not self._thread.isRunning()
        ):
            self._reject_locally(
                task_id,
                _failure(
                    "OcrModelNotReadyError",
                    "The OCR model is not ready.",
                    "ocr-model-not-ready",
                ),
            )
            return None

        task = OcrTask(task_id=task_id, image_bgr=image_bgr)
        self._active_task = task
        self._set_task_state(TaskState.RECOGNIZING)
        self._task_requested.emit(task)
        return task

    def cancel_active(self) -> bool:
        task = self._active_task
        if task is None:
            return False
        task.cancel_event.set()
        record_runtime_event("ocr.task-cancel-requested", task_id=task.task_id)
        return True

    def retry_model(self) -> bool:
        """Queue one model reload only from the Error/Idle state."""

        if (
            self._shutting_down
            or self._retry_pending
            or not self._started
            or not self._thread.isRunning()
            or self._model_state is not ModelState.ERROR
            or self._task_state is not TaskState.IDLE
            or self._active_task is not None
        ):
            return False
        self._retry_pending = True
        self._retry_requested.emit()
        return True

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        record_runtime_event("ocr.thread-shutdown-requested")
        self.cancel_active()
        if not self._started:
            self._shutdown_status.completed.set()
            self._shutdown_reported = True
            self.shutdown_finished.emit(None)
            return
        self._shutdown_requested.emit()

    def wait_for_shutdown(self) -> bool:
        """Block until shutdown and report both thread and engine-close failures."""

        if not self._started:
            self._release_stopped_task()
            return True
        if self._thread.isRunning() and not bool(self._thread.wait()):
            return False
        self._release_stopped_task()
        return (
            self._shutdown_status.completed.is_set()
            and self._shutdown_status.failure is None
        )

    def _release_stopped_task(self) -> None:
        """Drop image ownership once the worker thread can no longer use it."""

        self._active_task = None
        self._set_task_state(TaskState.IDLE)

    def _allocate_task_id(self) -> int:
        task_id = self._next_task_id
        self._next_task_id += 1
        return task_id

    def _reject_locally(self, task_id: int, failure: Failure) -> None:
        self.task_rejected.emit(task_id, failure)

    def _set_task_state(self, state: TaskState) -> None:
        if self._task_state is state:
            return
        self._task_state = state
        self.task_state_changed.emit(state)

    @Slot(object)
    def _on_model_state(self, state: object) -> None:
        if not isinstance(state, ModelState):
            state = ModelState.ERROR
        self._retry_pending = False
        self._model_state = state
        self.model_state_changed.emit(state)

    @Slot(object)
    def _on_model_failure(self, failure: object) -> None:
        if not isinstance(failure, Failure):
            failure = _failure(
                "OcrWorkerContractError",
                "The OCR worker could not initialize the model.",
                "ocr-worker-model-failure-contract",
            )
        self.model_failed.emit(failure)

    @Slot(int, object)
    def _on_task_completed(self, task_id: int, outcome: object) -> None:
        if not isinstance(outcome, _OUTCOME_TYPES):
            outcome = _failure(
                "OcrWorkerContractError",
                "Text recognition failed.",
                "ocr-worker-outcome-contract",
            )
        active = self._active_task
        if active is None and self._shutting_down:
            return
        if active is None or active.task_id != task_id:
            self.task_rejected.emit(
                task_id,
                _failure(
                    "OcrTaskBoundaryError",
                    "The OCR task result was invalid.",
                    "ocr-task-result-mismatch",
                ),
            )
            return
        self._active_task = None
        self._set_task_state(TaskState.IDLE)
        self.task_finished.emit(task_id, outcome)

    @Slot(int, object)
    def _on_worker_rejection(self, task_id: int, failure: object) -> None:
        if not isinstance(failure, Failure):
            failure = _failure(
                "OcrWorkerContractError",
                "The OCR task was rejected.",
                "ocr-worker-rejection-contract",
            )
        active = self._active_task
        if active is not None and active.task_id == task_id:
            self._active_task = None
            self._set_task_state(TaskState.IDLE)
        self.task_rejected.emit(task_id, failure)

    @Slot(object)
    def _on_worker_closed(self, result: object) -> None:
        if result is not None and not isinstance(result, Failure):
            result = _failure(
                "OcrWorkerContractError",
                "The OCR worker did not shut down cleanly.",
                "ocr-worker-close-result-contract",
            )
        self._close_result = result
        self._thread.quit()

    @Slot()
    def _on_thread_finished(self) -> None:
        if self._shutdown_reported:
            return
        if self._shutdown_status.completed.is_set():
            self._close_result = self._shutdown_status.failure
        elif self._close_result is None:
            self._close_result = _failure(
                "OcrWorkerShutdownError",
                "The OCR worker did not shut down cleanly.",
                "ocr-worker-stopped-without-close",
            )
        self._shutdown_reported = True
        self.shutdown_finished.emit(self._close_result)
