from __future__ import annotations

import unittest

from textsnap.windows.autostart import (
    RUN_VALUE_NAME,
    AutostartError,
    AutostartService,
    build_autostart_command,
)


class _RegistryApi:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.events: list[tuple[object, ...]] = []
        self.failure: Exception | None = None

    def read_value(self, value_name: str) -> str | None:
        self.events.append(("read", value_name))
        if self.failure is not None:
            raise self.failure
        return self.values.get(value_name)

    def write_value(self, value_name: str, value: str) -> None:
        self.events.append(("write", value_name, value))
        if self.failure is not None:
            raise self.failure
        self.values[value_name] = value

    def delete_value(self, value_name: str) -> None:
        self.events.append(("delete", value_name))
        if self.failure is not None:
            raise self.failure
        self.values.pop(value_name, None)


class _RegistryFailure(OSError):
    def __init__(self) -> None:
        super().__init__(r"C:\Users\alice\private-location")
        self.winerror = 5


class WindowsAutostartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _RegistryApi()
        self.executable = r"C:\便携 软件\TextSnapLayout.exe"
        self.service = AutostartService(self.executable, self.api)

    def test_command_quotes_current_executable_and_uses_fixed_argument(self) -> None:
        self.assertEqual(
            build_autostart_command(self.executable),
            r'"C:\便携 软件\TextSnapLayout.exe" --autostart',
        )
        with self.assertRaises(ValueError):
            build_autostart_command('C:\\bad"name.exe')

    def test_enable_overwrites_stale_path_after_move(self) -> None:
        self.api.values[RUN_VALUE_NAME] = r'"C:\old\TextSnapLayout.exe" --autostart'

        self.service.enable()

        self.assertEqual(
            self.api.values[RUN_VALUE_NAME],
            self.service.expected_command,
        )
        self.assertTrue(self.service.is_enabled())

    def test_update_if_registered_refreshes_but_does_not_create(self) -> None:
        self.assertFalse(self.service.update_if_registered())
        self.assertNotIn(RUN_VALUE_NAME, self.api.values)

        self.api.values[RUN_VALUE_NAME] = '"D:\\moved.exe" --autostart'
        self.assertTrue(self.service.update_if_registered())
        self.assertEqual(
            self.api.values[RUN_VALUE_NAME],
            self.service.expected_command,
        )

    def test_transaction_restore_reinstates_exact_stale_command(self) -> None:
        stale = r'"C:\old\TextSnapLayout.exe" --autostart'
        self.api.values[RUN_VALUE_NAME] = stale
        snapshot = self.service.registered_command()
        self.service.disable()

        self.service.restore_registered_command(snapshot)

        self.assertEqual(self.api.values[RUN_VALUE_NAME], stale)
        self.service.restore_registered_command(None)
        self.assertNotIn(RUN_VALUE_NAME, self.api.values)

    def test_disable_deletes_only_own_value_and_is_idempotent(self) -> None:
        self.api.values.update(
            {
                RUN_VALUE_NAME: self.service.expected_command,
                "AnotherApplication": '"C:\\other.exe"',
            }
        )

        self.service.disable()
        self.service.disable()

        self.assertNotIn(RUN_VALUE_NAME, self.api.values)
        self.assertEqual(
            self.api.values["AnotherApplication"],
            '"C:\\other.exe"',
        )
        self.assertEqual(
            [event for event in self.api.events if event[0] == "delete"],
            [
                ("delete", RUN_VALUE_NAME),
                ("delete", RUN_VALUE_NAME),
            ],
        )

    def test_registry_error_does_not_expose_user_path(self) -> None:
        self.api.failure = _RegistryFailure()

        with self.assertRaises(AutostartError) as raised:
            self.service.enable()

        self.assertEqual(raised.exception.diagnostic_code, "AUTOSTART-WRITE")
        self.assertEqual(raised.exception.winerror, 5)
        self.assertNotIn("alice", str(raised.exception))
        self.assertNotIn("private-location", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
