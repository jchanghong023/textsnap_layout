from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
import unittest
from unittest import mock


PYSIDE_AVAILABLE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is not installed")
class QtInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        # Keep one QApplication for the complete discovery process. Creating a
        # QCoreApplication here would prevent later widget tests from safely
        # upgrading the process-wide Qt application object.
        cls.application = QApplication.instance() or QApplication([])

    def test_primary_server_receives_exact_valid_command(self) -> None:
        from PySide6.QtCore import QEventLoop, QTimer

        from textsnap.qt_instance import InstanceCommandServer

        name = f"TextSnapLayout-test-{uuid.uuid4().hex}"
        if sys.platform == "win32":
            name = rf"\\.\pipe\LOCAL\{name}"
        server = InstanceCommandServer(server_name=name)
        received: list[str] = []
        server.command_received.connect(received.append)
        server.start()
        self.addCleanup(server.close)

        sender = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "from textsnap.qt_instance import send_instance_command;"
                    "raise SystemExit(0 if send_instance_command("
                    f"'open-settings', server_name={name!r}) else 1)"
                ),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(
            lambda: sender.kill() if sender.poll() is None else None
        )
        loop = QEventLoop()
        QTimer.singleShot(1000, loop.quit)
        server.command_received.connect(loop.quit)
        if not received:
            loop.exec()
        self.assertEqual(sender.wait(timeout=1), 0)
        self.assertEqual(received, ["open-settings"])

    @unittest.skipUnless(sys.platform == "win32", "Windows named pipe only")
    def test_default_server_name_uses_logon_session_pipe_namespace(self) -> None:
        from textsnap.qt_instance import LOCAL_SERVER_NAME

        self.assertEqual(
            LOCAL_SERVER_NAME,
            r"\\.\pipe\LOCAL\TextSnapLayout.Command.v1",
        )

    def test_invalid_send_arguments_are_rejected(self) -> None:
        from textsnap.qt_instance import send_instance_command

        with self.assertRaises(ValueError):
            send_instance_command(attempts=0)

    def test_missing_server_retries_with_a_real_bounded_delay(self) -> None:
        import textsnap.qt_instance as module

        class MissingSocket:
            def connectToServer(self, _name: str) -> None:  # noqa: N802
                return None

            def waitForConnected(self, _timeout_ms: int) -> bool:  # noqa: N802
                return False

            def abort(self) -> None:
                return None

        with (
            mock.patch.object(module, "QLocalSocket", MissingSocket),
            mock.patch.object(module.time, "sleep") as sleep,
        ):
            self.assertFalse(
                module.send_instance_command(
                    server_name="missing",
                    attempts=3,
                    timeout_ms=25,
                )
            )

        self.assertEqual(sleep.call_args_list, [mock.call(0.025), mock.call(0.025)])


if __name__ == "__main__":
    unittest.main()
