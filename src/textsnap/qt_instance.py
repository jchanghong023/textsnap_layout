"""QLocalServer command transport; the Win32 mutex remains the authority."""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .windows.instance import (
    MAX_INSTANCE_COMMAND_BYTES,
    OPEN_SETTINGS_COMMAND,
    decode_instance_command,
    encode_instance_command,
)

_SERVER_BASENAME = "TextSnapLayout.Command.v1"
LOCAL_SERVER_NAME = (
    rf"\\.\pipe\LOCAL\{_SERVER_BASENAME}"
    if sys.platform == "win32"
    else _SERVER_BASENAME
)


class LocalCommandError(RuntimeError):
    """A sanitized local IPC setup or delivery failure."""


class InstanceCommandServer(QObject):
    command_received = Signal(str)

    def __init__(
        self,
        *,
        server_name: str = LOCAL_SERVER_NAME,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not server_name or "\0" in server_name:
            raise ValueError("invalid local server name")
        self._server_name = server_name
        self._server = QLocalServer(self)
        if sys.platform == "win32":
            self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept_connections)
        self._buffers: dict[QLocalSocket, bytearray] = {}

    @property
    def listening(self) -> bool:
        return self._server.isListening()

    def start(self) -> None:
        if self.listening:
            return
        # Never call removeServer here: the named mutex, not the pipe, elects
        # the primary process on Windows.
        if not self._server.listen(self._server_name):
            raise LocalCommandError("无法启动本地实例通信。")

    def close(self) -> None:
        for socket in tuple(self._buffers):
            self._disconnect_socket_signals(socket)
            socket.abort()
            socket.deleteLater()
        self._buffers.clear()
        self._server.close()

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(
                lambda connection=socket: self._read_connection(connection)
            )
            socket.disconnected.connect(
                lambda connection=socket: self._finish_connection(connection)
            )
            self._read_connection(socket)

    def _read_connection(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        chunk = bytes(socket.readAll())
        if len(buffer) + len(chunk) > MAX_INSTANCE_COMMAND_BYTES:
            socket.abort()
            self._drop_connection(socket)
            return
        buffer.extend(chunk)

    def _finish_connection(self, socket: QLocalSocket) -> None:
        self._read_connection(socket)
        payload = bytes(self._buffers.get(socket, b""))
        self._drop_connection(socket)
        try:
            command = decode_instance_command(payload)
        except (TypeError, ValueError):
            return
        self.command_received.emit(command)

    def _drop_connection(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        self._disconnect_socket_signals(socket)
        socket.deleteLater()

    @staticmethod
    def _disconnect_socket_signals(socket: QLocalSocket) -> None:
        """Remove Python callbacks before the native socket is deferred-deleted."""

        for signal in (socket.readyRead, socket.disconnected):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass


def send_instance_command(
    command: str = OPEN_SETTINGS_COMMAND,
    *,
    server_name: str = LOCAL_SERVER_NAME,
    attempts: int = 20,
    timeout_ms: int = 100,
) -> bool:
    """Deliver a bounded command, retrying the mutex/server startup window."""

    if attempts <= 0 or timeout_ms <= 0:
        raise ValueError("attempts and timeout_ms must be positive")
    payload = encode_instance_command(command)
    for attempt in range(attempts):
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if socket.waitForConnected(timeout_ms):
            if socket.write(payload) != len(payload):
                socket.abort()
                return False
            socket.flush()
            if socket.bytesToWrite() > 0 and not socket.waitForBytesWritten(timeout_ms):
                socket.abort()
                return False
            socket.disconnectFromServer()
            if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                socket.waitForDisconnected(timeout_ms)
            return True
        socket.abort()
        if attempt + 1 < attempts:
            # Server-not-found can return immediately, so impose the bounded
            # delay explicitly instead of exhausting every startup retry in a
            # tight loop. This process has no interactive UI to service.
            time.sleep(timeout_ms / 1000)
    return False
