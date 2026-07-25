from __future__ import annotations

import unittest

from textsnap.domain import (
    Cancelled,
    Empty,
    Failure,
    LayoutResult,
    LayoutStats,
    ModelState,
    Success,
    TaskState,
)
from textsnap.state import ApplicationState, CaptureRequest


def _result(text: str) -> LayoutResult:
    return LayoutResult(text, LayoutStats(1, 1, 1, 10.0, 20.0))


class StateTests(unittest.TestCase):
    def test_model_state_is_independent_from_task_state(self) -> None:
        state = ApplicationState()
        self.assertEqual(state.request_capture(), CaptureRequest.STARTED)
        state.submit_capture()
        self.assertEqual(state.model_state, ModelState.LOADING)
        self.assertEqual(state.task_state, TaskState.RECOGNIZING)
        state.model_ready()
        self.assertEqual(state.task_state, TaskState.RECOGNIZING)

    def test_repeated_hotkey_is_busy_and_does_not_queue(self) -> None:
        state = ApplicationState()
        self.assertEqual(state.request_capture(), CaptureRequest.STARTED)
        self.assertEqual(state.request_capture(), CaptureRequest.BUSY)
        state.submit_capture()
        self.assertEqual(state.request_capture(), CaptureRequest.BUSY)

    def test_cancelled_capture_returns_to_idle(self) -> None:
        state = ApplicationState()
        state.request_capture()
        state.cancel_capture()
        self.assertEqual(state.task_state, TaskState.IDLE)

    def test_success_replaces_old_result(self) -> None:
        old = _result("old")
        new = _result("new")
        state = ApplicationState(visible_result=old)
        state.request_capture()
        state.submit_capture()
        self.assertEqual(state.finish_task(Success(new)), new)
        self.assertEqual(state.visible_result, new)

    def test_non_success_outcomes_restore_old_result(self) -> None:
        outcomes = (
            Empty(),
            Cancelled(),
            Failure("RuntimeError", "识别失败", "OCR-001"),
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                old = _result("old")
                state = ApplicationState(visible_result=old)
                state.request_capture()
                state.submit_capture()
                self.assertEqual(state.finish_task(outcome), old)
                self.assertEqual(state.task_state, TaskState.IDLE)

    def test_cancel_checkpoint_and_exit(self) -> None:
        state = ApplicationState()
        state.request_capture()
        state.submit_capture()
        self.assertTrue(state.request_cancel())
        self.assertTrue(state.cancel_requested)
        self.assertFalse(state.request_exit())
        self.assertTrue(state.exit_requested)
        self.assertTrue(state.cancel_requested)

    def test_idle_exit_is_immediate(self) -> None:
        state = ApplicationState()
        self.assertTrue(state.request_exit())

    def test_invalid_transition_is_rejected(self) -> None:
        state = ApplicationState()
        with self.assertRaises(RuntimeError):
            state.submit_capture()


if __name__ == "__main__":
    unittest.main()
