from __future__ import annotations

import unittest

from textsnap.windows.dpi import (
    DpiAwarenessError,
    PER_MONITOR_AWARE_V2,
    enable_per_monitor_v2,
)


class _DpiApi:
    def __init__(
        self,
        *,
        result: bool = True,
        last_error: int = 0,
        failure: Exception | None = None,
        current_per_monitor_v2: bool = False,
    ) -> None:
        self.result = result
        self.last_error = last_error
        self.failure = failure
        self.current_per_monitor_v2 = current_per_monitor_v2
        self.contexts: list[int] = []
        self.context_checks = 0

    def set_process_dpi_awareness_context(self, context: int) -> bool:
        self.contexts.append(context)
        if self.failure is not None:
            raise self.failure
        return self.result

    def is_current_thread_per_monitor_v2(self) -> bool:
        self.context_checks += 1
        return self.current_per_monitor_v2

    def get_last_error(self) -> int:
        return self.last_error


class WindowsDpiTests(unittest.TestCase):
    def test_enables_per_monitor_v2_exactly_once(self) -> None:
        api = _DpiApi()

        self.assertIsNone(enable_per_monitor_v2(api))

        self.assertEqual(api.contexts, [PER_MONITOR_AWARE_V2])
        self.assertEqual(api.context_checks, 0)

    def test_already_established_per_monitor_v2_context_is_accepted(self) -> None:
        api = _DpiApi(
            result=False,
            last_error=5,
            current_per_monitor_v2=True,
        )

        self.assertIsNone(enable_per_monitor_v2(api))

        self.assertEqual(api.contexts, [PER_MONITOR_AWARE_V2])
        self.assertEqual(api.context_checks, 1)

    def test_failed_configuration_is_explicit(self) -> None:
        api = _DpiApi(result=False, last_error=5)

        with self.assertRaises(DpiAwarenessError) as raised:
            enable_per_monitor_v2(api)

        self.assertEqual(raised.exception.diagnostic_code, "DPI-SET-FAILED")
        self.assertEqual(raised.exception.winerror, 5)
        self.assertIn("QApplication was not created", str(raised.exception))

    def test_api_exception_is_sanitized(self) -> None:
        api = _DpiApi(failure=RuntimeError(r"C:\Users\alice\private.txt"))

        with self.assertRaises(DpiAwarenessError) as raised:
            enable_per_monitor_v2(api)

        self.assertEqual(raised.exception.diagnostic_code, "DPI-API-FAILED")
        self.assertNotIn("alice", str(raised.exception))
        self.assertNotIn("private.txt", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
