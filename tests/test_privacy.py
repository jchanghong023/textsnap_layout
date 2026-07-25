from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from textsnap.privacy import (
    OfflineGuard,
    OfflineNetworkError,
    offline_guard_active,
)


_EXPECTED_FLAGS = {
    "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    "PADDLEOCR_DISABLE_AUTO_LOGGING_CONFIG": "1",
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}


class PrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.cache_home = root / "cache"
        (self.cache_home / "temp").mkdir(parents=True)
        self.font_file = root / "font.otf"
        self.font_file.write_bytes(b"controlled test font")
        self.guard = OfflineGuard(
            cache_home=self.cache_home.resolve(),
            font_file=self.font_file.resolve(),
        )
        self.addCleanup(self.guard.restore)

    def test_environment_is_set_before_use_and_fully_restored(self) -> None:
        keys = set(_EXPECTED_FLAGS) | {
            "PADDLE_PDX_CACHE_HOME",
            "PADDLE_PDX_LOCAL_FONT_FILE_PATH",
        }
        before = {key: os.environ.get(key) for key in keys}
        self.guard.install()
        self.assertTrue(offline_guard_active())
        for key, value in _EXPECTED_FLAGS.items():
            self.assertEqual(os.environ[key], value)
        self.assertEqual(
            os.environ["PADDLE_PDX_CACHE_HOME"], str(self.cache_home.resolve())
        )
        self.assertEqual(
            os.environ["PADDLE_PDX_LOCAL_FONT_FILE_PATH"],
            str(self.font_file.resolve()),
        )
        self.guard.restore()
        self.assertFalse(offline_guard_active())
        self.assertEqual({key: os.environ.get(key) for key in keys}, before)

    def test_ipv4_and_create_connection_are_denied_immediately(self) -> None:
        original_connect = socket.socket.connect
        original_create_connection = socket.create_connection
        with self.guard:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                with self.assertRaises(OfflineNetworkError):
                    client.connect(("127.0.0.1", 9))
                with self.assertRaises(OfflineNetworkError):
                    client.connect_ex(("127.0.0.1", 9))
            with self.assertRaises(OfflineNetworkError):
                socket.create_connection(("example.invalid", 443))
        self.assertIs(socket.socket.connect, original_connect)
        self.assertIs(socket.create_connection, original_create_connection)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX unavailable")
    def test_af_unix_remains_available(self) -> None:
        socket_path = str(Path(self.temporary_directory.name) / "local.sock")
        with self.guard:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(socket_path)
                server.listen(1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(socket_path)
                    connection, _ = server.accept()
                    with connection:
                        client.sendall(b"local")
                        self.assertEqual(connection.recv(5), b"local")

    def test_guard_never_creates_cache_or_log_files(self) -> None:
        with patch("os.mkdir", side_effect=AssertionError("unexpected mkdir")):
            self.guard.install()
            self.guard.restore()

    def test_missing_precreated_temp_directory_fails_without_creating_it(self) -> None:
        missing_cache = Path(self.temporary_directory.name) / "missing-cache"
        missing_cache.mkdir()
        guard = OfflineGuard(
            cache_home=missing_cache.resolve(),
            font_file=self.font_file.resolve(),
        )
        with self.assertRaises(ValueError):
            guard.install()
        self.assertFalse((missing_cache / "temp").exists())

    def test_guard_refuses_late_install_after_paddlex_import(self) -> None:
        marker = object()
        self.assertNotIn("paddlex", sys.modules)
        sys.modules["paddlex"] = marker
        try:
            with self.assertRaises(RuntimeError):
                self.guard.install()
        finally:
            del sys.modules["paddlex"]
        self.assertFalse(self.guard.installed)


if __name__ == "__main__":
    unittest.main()
