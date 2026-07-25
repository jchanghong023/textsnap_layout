from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textsnap.settings import (
    DEFAULT_SETTINGS,
    Hotkey,
    Settings,
    SettingsSaveError,
    check_data_directory_writable,
    load_settings,
    save_settings,
)


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.path = self.root / "settings.json"

    def test_missing_file_uses_defaults_without_creating_file(self) -> None:
        result = load_settings(self.path)
        self.assertEqual(result.settings, DEFAULT_SETTINGS)
        self.assertIsNone(result.issue)
        self.assertFalse(result.file_existed)
        self.assertFalse(self.path.exists())

    def test_valid_versioned_settings_are_loaded_and_canonicalized(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "hotkey": {
                        "modifiers": ["Alt", "Ctrl"],
                        "key": "q",
                    },
                    "autostart": True,
                }
            ),
            encoding="utf-8",
        )
        result = load_settings(self.path)
        self.assertIsNone(result.issue)
        self.assertEqual(result.settings.hotkey, Hotkey(("Ctrl", "Alt"), "Q"))
        self.assertTrue(result.settings.autostart)

    def test_corrupt_file_is_preserved_until_explicit_save(self) -> None:
        original = b"{not-json\xff"
        self.path.write_bytes(original)
        result = load_settings(self.path)
        self.assertEqual(result.settings, DEFAULT_SETTINGS)
        self.assertEqual(result.issue.code, "settings-invalid")
        self.assertEqual(self.path.read_bytes(), original)

    def test_unknown_fields_and_schema_are_rejected(self) -> None:
        invalid_payloads = (
            {
                "schema_version": 2,
                "hotkey": {"modifiers": ["Ctrl"], "key": "A"},
                "autostart": False,
            },
            {
                "schema_version": 1,
                "hotkey": {"modifiers": ["Ctrl"], "key": "A"},
                "autostart": False,
                "extra": True,
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(
                    load_settings(self.path).issue.code, "settings-invalid"
                )

    def test_duplicate_keys_are_treated_as_corrupt_without_rewrite(self) -> None:
        original = (
            b'{"schema_version":1,'
            b'"hotkey":{"modifiers":["Ctrl","Alt"],"key":"O"},'
            b'"autostart":false,"autostart":true}'
        )
        self.path.write_bytes(original)

        result = load_settings(self.path)

        self.assertEqual(result.settings, DEFAULT_SETTINGS)
        self.assertEqual(result.issue.code, "settings-invalid")
        self.assertEqual(self.path.read_bytes(), original)

    def test_save_is_atomic_and_matches_stable_structure(self) -> None:
        settings = Settings(hotkey=Hotkey(("Ctrl", "Shift"), "F8"), autostart=True)
        save_settings(self.path, settings)
        self.assertEqual(load_settings(self.path).settings, settings)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "hotkey": {
                    "modifiers": ["Ctrl", "Shift"],
                    "key": "F8",
                },
                "autostart": True,
            },
        )
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_failed_replace_keeps_old_file_and_removes_temporary(self) -> None:
        self.path.write_bytes(b"old")
        with mock.patch("textsnap.settings.os.replace", side_effect=OSError):
            with self.assertRaises(SettingsSaveError):
                save_settings(self.path, Settings(autostart=True))
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual(
            [entry for entry in self.root.iterdir() if entry != self.path],
            [],
        )

    def test_hotkey_validation(self) -> None:
        with self.assertRaises(ValueError):
            Hotkey((), "A")
        with self.assertRaises(ValueError):
            Hotkey(("Ctrl", "Ctrl"), "A")
        with self.assertRaises(ValueError):
            Hotkey(("Meta",), "A")
        with self.assertRaises(ValueError):
            Hotkey(("Ctrl",), "?")

    def test_writability_probe_leaves_no_file(self) -> None:
        self.assertTrue(check_data_directory_writable(self.root))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_data_directory_is_not_writable(self) -> None:
        self.assertFalse(check_data_directory_writable(self.root / "missing"))

    def test_writability_probe_failure_is_sanitized_and_clean(self) -> None:
        real_open = os.open

        def fail_probe(path, flags, mode=0o777):
            if ".textsnap-write-probe-" in os.fspath(path):
                raise PermissionError
            return real_open(path, flags, mode)

        with mock.patch("textsnap.settings.os.open", side_effect=fail_probe):
            self.assertFalse(check_data_directory_writable(self.root))
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
