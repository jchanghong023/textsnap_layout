"""Explicit model/task state machine and old-result restoration policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .domain import (
    Cancelled,
    Empty,
    Failure,
    LayoutResult,
    ModelState,
    Success,
    TaskOutcome,
    TaskState,
)


class CaptureRequest(str, Enum):
    STARTED = "started"
    BUSY = "busy"


@dataclass(slots=True)
class ApplicationState:
    model_state: ModelState = ModelState.LOADING
    task_state: TaskState = TaskState.IDLE
    visible_result: LayoutResult | None = None
    model_error_code: str | None = None
    cancel_requested: bool = False
    exit_requested: bool = False

    def start_model_loading(self) -> None:
        if self.model_state is ModelState.LOADING:
            return
        self.model_state = ModelState.LOADING
        self.model_error_code = None

    def model_ready(self) -> None:
        self.model_state = ModelState.READY
        self.model_error_code = None

    def model_failed(self, diagnostic_code: str) -> None:
        if not diagnostic_code or "\n" in diagnostic_code:
            raise ValueError("diagnostic_code must be a sanitized single line")
        self.model_state = ModelState.ERROR
        self.model_error_code = diagnostic_code

    def request_capture(self) -> CaptureRequest:
        if self.task_state is not TaskState.IDLE:
            return CaptureRequest.BUSY
        self.task_state = TaskState.CAPTURING
        self.cancel_requested = False
        return CaptureRequest.STARTED

    def cancel_capture(self) -> None:
        self._require_task(TaskState.CAPTURING)
        self.task_state = TaskState.IDLE

    def submit_capture(self) -> None:
        self._require_task(TaskState.CAPTURING)
        self.task_state = TaskState.RECOGNIZING
        self.cancel_requested = False

    def request_cancel(self) -> bool:
        if self.task_state is not TaskState.RECOGNIZING:
            return False
        self.cancel_requested = True
        return True

    def finish_task(self, outcome: TaskOutcome) -> LayoutResult | None:
        self._require_task(TaskState.RECOGNIZING)
        if isinstance(outcome, Success):
            self.visible_result = outcome.result
        elif not isinstance(outcome, (Empty, Cancelled, Failure)):
            raise TypeError("unsupported task outcome")
        self.task_state = TaskState.IDLE
        self.cancel_requested = False
        return self.visible_result

    def request_exit(self) -> bool:
        """Return whether the application can exit immediately."""

        self.exit_requested = True
        if self.task_state is TaskState.RECOGNIZING:
            self.cancel_requested = True
            return False
        return True

    def clear_result(self) -> None:
        self.visible_result = None

    def _require_task(self, expected: TaskState) -> None:
        if self.task_state is not expected:
            raise RuntimeError(
                f"invalid transition from task state {self.task_state.value}; "
                f"expected {expected.value}"
            )
