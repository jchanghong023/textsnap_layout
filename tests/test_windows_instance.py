from __future__ import annotations

import unittest

from textsnap.windows.instance import (
    ERROR_ALREADY_EXISTS,
    MUTEX_NAME,
    OPEN_SETTINGS_COMMAND,
    SingleInstanceError,
    SingleInstanceMutex,
    decode_instance_command,
    encode_instance_command,
    validate_instance_command,
)


class _MutexApi:
    def __init__(self) -> None:
        self.create_result: tuple[object | None, bool, int] = (
            "mutex-handle",
            False,
            0,
        )
        self.close_results: list[bool] = []
        self.events: list[tuple[object, ...]] = []
        self.last_error = 0
        self.failure: Exception | None = None

    def create_mutex(self, name: str) -> tuple[object | None, bool, int]:
        self.events.append(("create", name))
        if self.failure is not None:
            raise self.failure
        return self.create_result

    def close_handle(self, handle: object) -> bool:
        self.events.append(("close", handle))
        return self.close_results.pop(0) if self.close_results else True

    def get_last_error(self) -> int:
        return self.last_error


class WindowsInstanceTests(unittest.TestCase):
    def test_first_creator_is_primary_until_handle_is_closed(self) -> None:
        api = _MutexApi()
        mutex = SingleInstanceMutex(api)

        self.assertTrue(mutex.acquire())
        self.assertTrue(mutex.is_primary)
        self.assertEqual(api.events, [("create", MUTEX_NAME)])

        mutex.close()

        self.assertFalse(mutex.is_primary)
        self.assertEqual(api.events[-1], ("close", "mutex-handle"))

    def test_existing_mutex_is_authoritative_and_temporary_handle_closes(self) -> None:
        api = _MutexApi()
        api.create_result = (
            "secondary-handle",
            True,
            ERROR_ALREADY_EXISTS,
        )
        mutex = SingleInstanceMutex(api)

        self.assertFalse(mutex.acquire())

        self.assertFalse(mutex.is_primary)
        self.assertEqual(
            api.events,
            [
                ("create", MUTEX_NAME),
                ("close", "secondary-handle"),
            ],
        )

    def test_close_failure_retains_handle_for_retry(self) -> None:
        api = _MutexApi()
        api.close_results = [False, True]
        api.last_error = 6
        mutex = SingleInstanceMutex(api)
        mutex.acquire()

        with self.assertRaises(SingleInstanceError) as raised:
            mutex.close()

        self.assertEqual(raised.exception.diagnostic_code, "INSTANCE-CLOSE")
        self.assertEqual(raised.exception.winerror, 6)
        self.assertTrue(mutex.is_primary)

        mutex.close()
        self.assertFalse(mutex.is_primary)
        self.assertEqual(
            api.events.count(("close", "mutex-handle")),
            2,
        )

    def test_create_failure_is_sanitized(self) -> None:
        api = _MutexApi()
        api.failure = RuntimeError(r"C:\Users\alice\private.txt")
        mutex = SingleInstanceMutex(api)

        with self.assertRaises(SingleInstanceError) as raised:
            mutex.acquire()

        self.assertEqual(
            raised.exception.diagnostic_code,
            "INSTANCE-CREATE-API",
        )
        self.assertNotIn("alice", str(raised.exception))

    def test_protocol_is_strict_and_independent_of_transport(self) -> None:
        self.assertEqual(
            validate_instance_command(OPEN_SETTINGS_COMMAND),
            OPEN_SETTINGS_COMMAND,
        )
        payload = encode_instance_command(OPEN_SETTINGS_COMMAND)
        self.assertEqual(payload, b"open-settings")
        self.assertEqual(decode_instance_command(payload), OPEN_SETTINGS_COMMAND)

        invalid_payloads = (
            b"",
            b"open-settings\n",
            b" open-settings",
            b"quit",
            b"\xff",
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    decode_instance_command(payload)


if __name__ == "__main__":
    unittest.main()
