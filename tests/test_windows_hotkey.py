from __future__ import annotations

import unittest

from textsnap.windows.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    PRIMARY_HOTKEY_ID,
    SECONDARY_HOTKEY_ID,
    WM_HOTKEY,
    HotkeyBinding,
    HotkeyRegistrationError,
    HotkeyService,
    decode_hotkey_message,
    virtual_key_for_name,
)


class _HotkeyApi:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.register_results: list[bool] = []
        self.unregister_results: list[bool] = []
        self.last_error = 1409

    def register_hot_key(
        self,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> bool:
        self.events.append(("register", hotkey_id, modifiers, virtual_key))
        return self.register_results.pop(0) if self.register_results else True

    def unregister_hot_key(self, hotkey_id: int) -> bool:
        self.events.append(("unregister", hotkey_id))
        return self.unregister_results.pop(0) if self.unregister_results else True

    def get_last_error(self) -> int:
        return self.last_error


class WindowsHotkeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _HotkeyApi()
        self.service = HotkeyService(self.api)
        self.default = HotkeyBinding(("Ctrl", "Alt"), "O")

    def test_registers_null_window_hotkey_flags_with_norepeat(self) -> None:
        self.assertTrue(self.service.register(self.default))

        self.assertEqual(
            self.api.events,
            [
                (
                    "register",
                    PRIMARY_HOTKEY_ID,
                    MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                    ord("O"),
                )
            ],
        )
        self.assertEqual(self.service.active_binding, self.default)
        self.assertFalse(self.service.register(self.default))
        self.assertEqual(len(self.api.events), 1)

    def test_replacement_registers_standby_before_releasing_old(self) -> None:
        self.service.register(self.default)
        replacement = HotkeyBinding(("Ctrl", "Shift"), "F12")
        self.api.events.clear()

        self.assertTrue(self.service.register(replacement))

        self.assertEqual(
            self.api.events,
            [
                (
                    "register",
                    SECONDARY_HOTKEY_ID,
                    replacement.modifier_flags,
                    virtual_key_for_name("F12"),
                ),
                ("unregister", PRIMARY_HOTKEY_ID),
            ],
        )
        self.assertEqual(self.service.active_binding, replacement)
        self.assertEqual(self.service.active_hotkey_id, SECONDARY_HOTKEY_ID)

    def test_conflicting_replacement_keeps_old_binding(self) -> None:
        self.service.register(self.default)
        replacement = HotkeyBinding(("Win",), "P")
        self.api.events.clear()
        self.api.register_results = [False]

        with self.assertRaises(HotkeyRegistrationError) as raised:
            self.service.register(replacement)

        self.assertEqual(raised.exception.diagnostic_code, "HOTKEY-CONFLICT")
        self.assertEqual(raised.exception.winerror, 1409)
        self.assertEqual(self.service.active_binding, self.default)
        self.assertFalse(any(event[0] == "unregister" for event in self.api.events))

    def test_old_unregister_failure_rolls_back_new_and_keeps_old(self) -> None:
        self.service.register(self.default)
        replacement = HotkeyBinding(("Alt",), "F2")
        self.api.events.clear()
        self.api.unregister_results = [False, True]

        with self.assertRaises(HotkeyRegistrationError) as raised:
            self.service.register(replacement)

        self.assertEqual(
            raised.exception.diagnostic_code,
            "HOTKEY-OLD-UNREGISTER",
        )
        self.assertEqual(
            self.api.events,
            [
                (
                    "register",
                    SECONDARY_HOTKEY_ID,
                    replacement.modifier_flags,
                    replacement.virtual_key,
                ),
                ("unregister", PRIMARY_HOTKEY_ID),
                ("unregister", SECONDARY_HOTKEY_ID),
            ],
        )
        self.assertEqual(self.service.active_binding, self.default)

    def test_unregister_is_idempotent_after_success(self) -> None:
        self.service.register(self.default)

        self.service.unregister()
        self.service.unregister()

        self.assertIsNone(self.service.active_binding)
        self.assertEqual(
            self.api.events[-1],
            ("unregister", PRIMARY_HOTKEY_ID),
        )

    def test_mapping_rejects_noncanonical_or_duplicate_input(self) -> None:
        invalid = (
            (("ctrl",), "O"),
            (("Ctrl", "Ctrl"), "O"),
            (("Ctrl",), "o"),
            (("Ctrl",), "F01"),
            (("Ctrl",), "Enter"),
            ((), "O"),
        )
        for modifiers, key in invalid:
            with self.subTest(modifiers=modifiers, key=key):
                with self.assertRaises(ValueError):
                    HotkeyBinding(modifiers, key)
        self.assertEqual(self.api.events, [])
        self.assertEqual(virtual_key_for_name("Return"), 0x0D)

    def test_message_decoder_accepts_only_active_id(self) -> None:
        self.assertTrue(
            decode_hotkey_message(WM_HOTKEY, PRIMARY_HOTKEY_ID, PRIMARY_HOTKEY_ID)
        )
        self.assertFalse(
            decode_hotkey_message(WM_HOTKEY, SECONDARY_HOTKEY_ID, PRIMARY_HOTKEY_ID)
        )
        self.assertFalse(
            decode_hotkey_message(0x000F, PRIMARY_HOTKEY_ID, PRIMARY_HOTKEY_ID)
        )
        self.assertFalse(decode_hotkey_message(WM_HOTKEY, PRIMARY_HOTKEY_ID, None))


if __name__ == "__main__":
    unittest.main()
