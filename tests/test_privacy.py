from __future__ import annotations

import _socket
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch


_EXPECTED_FLAGS = {
    "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    "PADDLEOCR_DISABLE_AUTO_LOGGING_CONFIG": "1",
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}


def _run_guard_child(case_name: str) -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source_root))

    from textsnap.privacy import (
        OfflineGuard,
        OfflineNetworkError,
        offline_guard_active,
    )

    assertion = unittest.TestCase()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        cache_home = root / "cache"
        (cache_home / "temp").mkdir(parents=True)
        font_file = root / "font.otf"
        font_file.write_bytes(b"controlled test font")
        guard = OfflineGuard(
            cache_home=cache_home.resolve(),
            font_file=font_file.resolve(),
        )

        try:
            if case_name == "environment":
                keys = set(_EXPECTED_FLAGS) | {
                    "PADDLE_PDX_CACHE_HOME",
                    "PADDLE_PDX_LOCAL_FONT_FILE_PATH",
                }
                before = {key: os.environ.get(key) for key in keys}
                guard.install()
                assertion.assertTrue(offline_guard_active())
                for key, value in _EXPECTED_FLAGS.items():
                    assertion.assertEqual(os.environ[key], value)
                assertion.assertEqual(
                    os.environ["PADDLE_PDX_CACHE_HOME"],
                    str(cache_home.resolve()),
                )
                assertion.assertEqual(
                    os.environ["PADDLE_PDX_LOCAL_FONT_FILE_PATH"],
                    str(font_file.resolve()),
                )
                guard.restore()
                assertion.assertFalse(offline_guard_active())
                assertion.assertEqual(
                    {key: os.environ.get(key) for key in keys},
                    before,
                )
            elif case_name == "inet":
                families = [(socket.AF_INET, ("127.0.0.1", 9))]
                if hasattr(socket, "AF_INET6") and socket.has_ipv6:
                    try:
                        ipv6_probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    except OSError:
                        pass
                    else:
                        ipv6_probe.close()
                        families.append((socket.AF_INET6, ("::1", 9)))

                function_owners = {
                    "bind": (socket.socket, "bind"),
                    "connect": (socket.socket, "connect"),
                    "connect_ex": (socket.socket, "connect_ex"),
                    "sendto": (socket.socket, "sendto"),
                    "create_connection": (socket, "create_connection"),
                }
                real_functions = {
                    name: getattr(owner, attribute)
                    for name, (owner, attribute) in function_owners.items()
                }
                network_calls: list[str] = []

                def fake_network_function(name: str):
                    def fake(*args, **kwargs):
                        network_calls.append(name)
                        raise AssertionError(f"real network function reached: {name}")

                    return fake

                fake_functions = {
                    name: fake_network_function(name) for name in function_owners
                }
                for name, (owner, attribute) in function_owners.items():
                    setattr(owner, attribute, fake_functions[name])

                try:
                    with guard:
                        for family, address in families:
                            with socket.socket(
                                family,
                                socket.SOCK_STREAM,
                            ) as client:
                                with assertion.assertRaises(OfflineNetworkError):
                                    client.bind(address)
                                with assertion.assertRaises(OfflineNetworkError):
                                    client.connect(address)
                                with assertion.assertRaises(OfflineNetworkError):
                                    client.connect_ex(address)
                            with socket.socket(
                                family,
                                socket.SOCK_DGRAM,
                            ) as datagram:
                                with assertion.assertRaises(OfflineNetworkError):
                                    datagram.sendto(b"blocked", address)
                        with assertion.assertRaises(OfflineNetworkError):
                            socket.create_connection(
                                ("network.example.invalid", 443)
                            )

                    assertion.assertEqual(network_calls, [])
                    for name, (owner, attribute) in function_owners.items():
                        assertion.assertIs(
                            getattr(owner, attribute),
                            fake_functions[name],
                        )

                    # Reinstallation must restore and reapply the bind guard
                    # without accumulating wrappers.
                    with guard:
                        for family, address in families:
                            with socket.socket(
                                family,
                                socket.SOCK_STREAM,
                            ) as listener:
                                with assertion.assertRaises(OfflineNetworkError):
                                    listener.bind(address)
                    for name, (owner, attribute) in function_owners.items():
                        assertion.assertIs(
                            getattr(owner, attribute),
                            fake_functions[name],
                        )
                    assertion.assertEqual(network_calls, [])
                finally:
                    for name, (owner, attribute) in function_owners.items():
                        setattr(owner, attribute, real_functions[name])
            elif case_name == "dns":
                resolver_arguments = {
                    "getaddrinfo": ("dns.example.invalid", 443),
                    "gethostbyname": ("dns.example.invalid",),
                    "gethostbyname_ex": ("dns.example.invalid",),
                    "gethostbyaddr": ("203.0.113.1",),
                    "getnameinfo": (("203.0.113.1", 443), 0),
                    "getfqdn": ("dns.example.invalid",),
                }
                real_resolvers = {
                    name: getattr(socket, name) for name in resolver_arguments
                }
                resolver_calls: list[str] = []

                def fake_resolver(name: str):
                    def fake(*args, **kwargs):
                        resolver_calls.append(name)
                        raise AssertionError(f"real resolver reached: {name}")

                    return fake

                fake_resolvers = {
                    name: fake_resolver(name) for name in resolver_arguments
                }
                for name, fake in fake_resolvers.items():
                    setattr(socket, name, fake)

                try:
                    with guard:
                        for name, arguments in resolver_arguments.items():
                            with assertion.assertRaises(OfflineNetworkError):
                                getattr(socket, name)(*arguments)

                    assertion.assertEqual(resolver_calls, [])
                    for name, fake in fake_resolvers.items():
                        assertion.assertIs(getattr(socket, name), fake)
                finally:
                    for name, real in real_resolvers.items():
                        setattr(socket, name, real)
            elif case_name == "low_level_inet":
                families = [
                    (_socket.AF_INET, ("127.0.0.1", 9)),
                ]
                if hasattr(_socket, "AF_INET6") and socket.has_ipv6:
                    try:
                        ipv6_probe = _socket.socket(
                            _socket.AF_INET6,
                            _socket.SOCK_STREAM,
                        )
                    except OSError:
                        pass
                    else:
                        ipv6_probe.close()
                        families.append((_socket.AF_INET6, ("::1", 9)))

                class AuditSentinel(RuntimeError):
                    pass

                blocked_events = {
                    "socket.bind",
                    "socket.connect",
                    "socket.sendto",
                }
                sentinel_events: list[str] = []

                def sentinel_hook(event: str, arguments: tuple[object, ...]) -> None:
                    del arguments
                    if event in blocked_events:
                        sentinel_events.append(event)
                        raise AuditSentinel(event)

                def invoke_socket(
                    constructor,
                    family: int,
                    method_name: str,
                    address: tuple[str, int],
                ) -> None:
                    socket_type = (
                        _socket.SOCK_DGRAM
                        if method_name == "sendto"
                        else _socket.SOCK_STREAM
                    )
                    sock = constructor(family, socket_type)
                    try:
                        if method_name == "sendto":
                            sock.sendto(b"blocked", address)
                        else:
                            getattr(sock, method_name)(address)
                    finally:
                        sock.close()

                guard.install()
                # Registered after the production hook: if the guard works,
                # its OfflineNetworkError wins before this no-network sentinel.
                sys.addaudithook(sentinel_hook)
                probes = (
                    (
                        "SocketType.bind",
                        socket.SocketType,
                        "bind",
                        "socket.bind",
                    ),
                    (
                        "_socket.bind",
                        _socket.socket,
                        "bind",
                        "socket.bind",
                    ),
                    (
                        "SocketType.connect",
                        socket.SocketType,
                        "connect",
                        "socket.connect",
                    ),
                    (
                        "_socket.connect",
                        _socket.socket,
                        "connect",
                        "socket.connect",
                    ),
                    (
                        "_socket.connect_ex",
                        _socket.socket,
                        "connect_ex",
                        "socket.connect",
                    ),
                    ("_socket.sendto", _socket.socket, "sendto", "socket.sendto"),
                )
                for family, address in families:
                    for label, constructor, method_name, expected_event in probes:
                        with assertion.subTest(
                            phase="active",
                            family=family,
                            probe=label,
                        ):
                            before = len(sentinel_events)
                            with assertion.assertRaises(OfflineNetworkError):
                                invoke_socket(
                                    constructor,
                                    family,
                                    method_name,
                                    address,
                                )
                            assertion.assertEqual(len(sentinel_events), before)

                guard.restore()
                for family, address in families:
                    for label, constructor, method_name, expected_event in probes:
                        with assertion.subTest(
                            phase="restored",
                            family=family,
                            probe=label,
                        ):
                            before = len(sentinel_events)
                            with assertion.assertRaises(AuditSentinel):
                                invoke_socket(
                                    constructor,
                                    family,
                                    method_name,
                                    address,
                                )
                            assertion.assertEqual(
                                sentinel_events[before:],
                                [expected_event],
                            )

                guard.install()
                for family, address in families:
                    for label, constructor, method_name, _expected_event in probes[:2]:
                        with assertion.subTest(
                            phase="reinstalled",
                            family=family,
                            probe=label,
                        ):
                            before = len(sentinel_events)
                            with assertion.assertRaises(OfflineNetworkError):
                                invoke_socket(
                                    constructor,
                                    family,
                                    method_name,
                                    address,
                                )
                            assertion.assertEqual(len(sentinel_events), before)
                guard.restore()
            elif case_name == "masked_socket_family":
                families = [(socket.AF_INET, ("127.0.0.1", 9))]
                if hasattr(socket, "AF_INET6") and socket.has_ipv6:
                    try:
                        ipv6_probe = socket.SocketType(
                            socket.AF_INET6,
                            socket.SOCK_STREAM,
                        )
                    except OSError:
                        pass
                    else:
                        ipv6_probe.close()
                        families.append((socket.AF_INET6, ("::1", 9)))

                disguised_family = getattr(socket, "AF_UNIX", 0)

                class MaskedFamilySocket(socket.socket):
                    @property
                    def family(self) -> object:
                        return disguised_family

                class AuditSentinel(RuntimeError):
                    pass

                sentinel_events: list[str] = []

                def sentinel_hook(event: str, arguments: tuple[object, ...]) -> None:
                    del arguments
                    if event in {
                        "socket.bind",
                        "socket.connect",
                        "socket.sendto",
                    }:
                        sentinel_events.append(event)
                        raise AuditSentinel(event)

                expected_events = {
                    "bind": "socket.bind",
                    "connect": "socket.connect",
                    "connect_ex": "socket.connect",
                    "sendto": "socket.sendto",
                }

                def invoke(
                    family: int,
                    address: tuple[str, int],
                    method_name: str,
                    *,
                    raw_method: bool,
                ) -> None:
                    socket_type = (
                        socket.SOCK_DGRAM
                        if method_name == "sendto"
                        else socket.SOCK_STREAM
                    )
                    sock = MaskedFamilySocket(family, socket_type)
                    try:
                        assertion.assertEqual(sock.family, disguised_family)
                        assertion.assertEqual(
                            socket.SocketType.family.__get__(
                                sock,
                                socket.SocketType,
                            ),
                            family,
                        )
                        operation = (
                            getattr(socket.SocketType, method_name).__get__(
                                sock,
                                socket.SocketType,
                            )
                            if raw_method
                            else getattr(sock, method_name)
                        )
                        if method_name == "sendto":
                            operation(b"blocked", address)
                        else:
                            operation(address)
                    finally:
                        sock.close()

                guard.install()
                sys.addaudithook(sentinel_hook)
                for family, address in families:
                    for method_name in expected_events:
                        for raw_method in (False, True):
                            with assertion.subTest(
                                phase="active",
                                family=family,
                                method=method_name,
                                raw=raw_method,
                            ):
                                before = len(sentinel_events)
                                with assertion.assertRaises(OfflineNetworkError):
                                    invoke(
                                        family,
                                        address,
                                        method_name,
                                        raw_method=raw_method,
                                    )
                                assertion.assertEqual(
                                    len(sentinel_events),
                                    before,
                                )

                guard.restore()
                for family, address in families:
                    for method_name, expected_event in expected_events.items():
                        for raw_method in (False, True):
                            with assertion.subTest(
                                phase="restored",
                                family=family,
                                method=method_name,
                                raw=raw_method,
                            ):
                                before = len(sentinel_events)
                                with assertion.assertRaises(AuditSentinel):
                                    invoke(
                                        family,
                                        address,
                                        method_name,
                                        raw_method=raw_method,
                                    )
                                assertion.assertEqual(
                                    sentinel_events[before:],
                                    [expected_event],
                                )
            elif case_name == "low_level_dns":
                resolver_probes = (
                    (
                        "_socket.getaddrinfo",
                        lambda: _socket.getaddrinfo(
                            "dns.example.invalid",
                            443,
                        ),
                        "socket.getaddrinfo",
                    ),
                    (
                        "_socket.gethostbyname",
                        lambda: _socket.gethostbyname("dns.example.invalid"),
                        "socket.gethostbyname",
                    ),
                    (
                        "_socket.gethostbyname_ex",
                        lambda: _socket.gethostbyname_ex("dns.example.invalid"),
                        "socket.gethostbyname",
                    ),
                    (
                        "_socket.gethostbyaddr",
                        lambda: _socket.gethostbyaddr("203.0.113.1"),
                        "socket.gethostbyaddr",
                    ),
                    (
                        "_socket.getnameinfo",
                        lambda: _socket.getnameinfo(("203.0.113.1", 443), 0),
                        "socket.getnameinfo",
                    ),
                )

                class AuditSentinel(RuntimeError):
                    pass

                blocked_events = {probe[2] for probe in resolver_probes}
                sentinel_events: list[str] = []

                def sentinel_hook(event: str, arguments: tuple[object, ...]) -> None:
                    del arguments
                    if event in blocked_events:
                        sentinel_events.append(event)
                        raise AuditSentinel(event)

                guard.install()
                sys.addaudithook(sentinel_hook)
                for label, operation, expected_event in resolver_probes:
                    with assertion.subTest(phase="active", probe=label):
                        before = len(sentinel_events)
                        with assertion.assertRaises(OfflineNetworkError):
                            operation()
                        assertion.assertEqual(len(sentinel_events), before)

                guard.restore()
                for label, operation, expected_event in resolver_probes:
                    with assertion.subTest(phase="restored", probe=label):
                        before = len(sentinel_events)
                        with assertion.assertRaises(AuditSentinel):
                            operation()
                        assertion.assertEqual(
                            sentinel_events[before:],
                            [expected_event],
                        )
            elif case_name == "overlapped_inet":
                if sys.platform != "win32":
                    raise unittest.SkipTest("Windows overlapped I/O unavailable")
                import _overlapped

                original_attributes = {
                    name: getattr(_overlapped, name)
                    for name in ("WSAConnect", "BindLocal", "Overlapped")
                }
                guard.install()
                for name, original in original_attributes.items():
                    assertion.assertIsNot(getattr(_overlapped, name), original)

                probes = (
                    (
                        "AcceptEx",
                        lambda operation, handle, address: operation(handle, handle),
                    ),
                    (
                        "ConnectEx",
                        lambda operation, handle, address: operation(handle, address),
                    ),
                    (
                        "ReadFile",
                        lambda operation, handle, address: operation(handle, 1),
                    ),
                    (
                        "ReadFileInto",
                        lambda operation, handle, address: operation(
                            handle,
                            bytearray(1),
                        ),
                    ),
                    (
                        "TransmitFile",
                        lambda operation, handle, address: operation(
                            handle,
                            0,
                            0,
                            0,
                            0,
                            0,
                            0,
                        ),
                    ),
                    (
                        "WSARecv",
                        lambda operation, handle, address: operation(handle, 1, 0),
                    ),
                    (
                        "WSARecvFrom",
                        lambda operation, handle, address: operation(handle, 1, 0),
                    ),
                    (
                        "WSARecvFromInto",
                        lambda operation, handle, address: operation(
                            handle,
                            bytearray(1),
                            1,
                            0,
                        ),
                    ),
                    (
                        "WSARecvInto",
                        lambda operation, handle, address: operation(
                            handle,
                            bytearray(1),
                            0,
                        ),
                    ),
                    (
                        "WSASend",
                        lambda operation, handle, address: operation(
                            handle,
                            b"blocked",
                            0,
                        ),
                    ),
                    (
                        "WSASendTo",
                        lambda operation, handle, address: operation(
                            handle,
                            b"blocked",
                            0,
                            address,
                        ),
                    ),
                    (
                        "WriteFile",
                        lambda operation, handle, address: operation(
                            handle,
                            b"blocked",
                        ),
                    ),
                )
                families = [
                    (socket.AF_INET, ("127.0.0.1", 9)),
                ]
                if hasattr(socket, "AF_INET6") and socket.has_ipv6:
                    families.append((socket.AF_INET6, ("::1", 9)))

                for family, address in families:
                    raw_socket = socket.SocketType(
                        family,
                        socket.SOCK_DGRAM,
                    )
                    try:
                        handle = raw_socket.fileno()
                        with assertion.assertRaises(OfflineNetworkError):
                            _overlapped.WSAConnect(handle, address)
                        with assertion.assertRaises(OfflineNetworkError):
                            _overlapped.BindLocal(handle, family)
                        with assertion.assertRaises(OfflineNetworkError):
                            _overlapped.BindLocal(
                                handle,
                                getattr(socket, "AF_UNIX", 0),
                            )

                        operation_holder = _overlapped.Overlapped()
                        for method_name, invoke in probes:
                            with assertion.subTest(
                                family=family,
                                method=method_name,
                            ):
                                with assertion.assertRaises(OfflineNetworkError):
                                    invoke(
                                        getattr(operation_holder, method_name),
                                        handle,
                                        address,
                                    )
                                assertion.assertEqual(raw_socket.fileno(), handle)
                                assertion.assertEqual(raw_socket.family, family)
                    finally:
                        raw_socket.close()

                guard.restore()
                for name, original in original_attributes.items():
                    assertion.assertIs(getattr(_overlapped, name), original)

                # The same guard may be installed again without accumulating
                # wrappers, and restore returns every module attribute exactly.
                guard.install()
                guard.restore()
                for name, original in original_attributes.items():
                    assertion.assertIs(getattr(_overlapped, name), original)
            elif case_name == "overlapped_delegation":
                if sys.platform != "win32":
                    raise unittest.SkipTest("Windows overlapped I/O unavailable")
                import msvcrt

                calls: list[tuple[str, tuple[object, ...]]] = []

                def record(name: str):
                    def operation(*args: object) -> str:
                        calls.append((name, args))
                        return name

                    return operation

                class FakeOverlapped:
                    address = 1
                    pending = False

                method_names = (
                    "AcceptEx",
                    "ConnectEx",
                    "ConnectNamedPipe",
                    "DisconnectEx",
                    "ReadFile",
                    "ReadFileInto",
                    "TransmitFile",
                    "WSARecv",
                    "WSARecvFrom",
                    "WSARecvFromInto",
                    "WSARecvInto",
                    "WSASend",
                    "WSASendTo",
                    "WriteFile",
                )
                for method_name in method_names:
                    setattr(FakeOverlapped, method_name, record(method_name))

                fake_module = ModuleType("_overlapped")
                fake_module.WSAConnect = record("WSAConnect")
                fake_module.BindLocal = record("BindLocal")
                fake_module.Overlapped = FakeOverlapped
                fake_module.ConnectPipe = record("ConnectPipe")
                original_attributes = {
                    name: getattr(fake_module, name)
                    for name in (
                        "WSAConnect",
                        "BindLocal",
                        "Overlapped",
                        "ConnectPipe",
                    )
                }

                with (
                    patch.dict(sys.modules, {"_overlapped": fake_module}),
                    tempfile.TemporaryFile() as local_file,
                ):
                    guard.install()
                    assertion.assertIs(
                        fake_module.ConnectPipe,
                        original_attributes["ConnectPipe"],
                    )
                    file_handle = msvcrt.get_osfhandle(local_file.fileno())
                    operation_holder = fake_module.Overlapped()

                    fake_module.WSAConnect(file_handle, ("local", 0))
                    fake_module.BindLocal(file_handle, 0)
                    operation_holder.AcceptEx(file_handle, file_handle)
                    operation_holder.ConnectEx(file_handle, ("local", 0))
                    operation_holder.ConnectNamedPipe(file_handle)
                    operation_holder.ReadFile(file_handle, 1)
                    operation_holder.ReadFileInto(file_handle, bytearray(1))
                    operation_holder.TransmitFile(
                        file_handle,
                        file_handle,
                        0,
                        0,
                        0,
                        0,
                        0,
                    )
                    operation_holder.WSARecv(file_handle, 1, 0)
                    operation_holder.WSARecvFrom(file_handle, 1, 0)
                    operation_holder.WSARecvFromInto(
                        file_handle,
                        bytearray(1),
                        1,
                        0,
                    )
                    operation_holder.WSARecvInto(
                        file_handle,
                        bytearray(1),
                        0,
                    )
                    operation_holder.WSASend(file_handle, b"local", 0)
                    operation_holder.WSASendTo(
                        file_handle,
                        b"local",
                        0,
                        ("local", 0),
                    )
                    operation_holder.WriteFile(file_handle, b"local")
                    fake_module.ConnectPipe("local-pipe")

                    inet_socket = socket.SocketType(
                        socket.AF_INET,
                        socket.SOCK_DGRAM,
                    )
                    try:
                        operation_holder.DisconnectEx(inet_socket.fileno(), 0)
                    finally:
                        inet_socket.close()

                    assertion.assertEqual(
                        {name for name, _arguments in calls},
                        {*method_names, "WSAConnect", "BindLocal", "ConnectPipe"},
                    )
                    guard.restore()
                    for name, original in original_attributes.items():
                        assertion.assertIs(getattr(fake_module, name), original)

                    # A proxy retained across restore delegates normally. If a
                    # later guard becomes active, it observes that state
                    # dynamically and blocks the same retained proxy.
                    calls.clear()
                    inet_socket = socket.SocketType(
                        socket.AF_INET,
                        socket.SOCK_DGRAM,
                    )
                    try:
                        operation_holder.WSASendTo(
                            inet_socket.fileno(),
                            b"restored",
                            0,
                            ("local", 0),
                        )
                        assertion.assertEqual(calls[0][0], "WSASendTo")
                        guard.install()
                        with assertion.assertRaises(OfflineNetworkError):
                            operation_holder.WSASendTo(
                                inet_socket.fileno(),
                                b"blocked",
                                0,
                                ("local", 0),
                            )
                        guard.restore()
                    finally:
                        inet_socket.close()
                for name, original in original_attributes.items():
                    assertion.assertIs(getattr(fake_module, name), original)
            elif case_name == "overlapped_install_rollback":
                if sys.platform != "win32":
                    raise unittest.SkipTest("Windows overlapped I/O unavailable")

                def no_op(*args: object) -> None:
                    del args

                class FakeOverlapped:
                    pass

                for method_name in (
                    "AcceptEx",
                    "ConnectEx",
                    "ConnectNamedPipe",
                    "DisconnectEx",
                    "ReadFile",
                    "ReadFileInto",
                    "TransmitFile",
                    "WSARecv",
                    "WSARecvFrom",
                    "WSARecvFromInto",
                    "WSARecvInto",
                    "WSASend",
                    "WSASendTo",
                    "WriteFile",
                ):
                    setattr(FakeOverlapped, method_name, no_op)

                class FailingModule(ModuleType):
                    fail_overlapped_assignment = False
                    original_overlapped: object | None = None

                    def __setattr__(self, name: str, value: object) -> None:
                        if (
                            self.fail_overlapped_assignment
                            and name == "Overlapped"
                            and value is not self.original_overlapped
                        ):
                            raise RuntimeError("controlled assignment failure")
                        super().__setattr__(name, value)

                fake_module = FailingModule("_overlapped")
                fake_module.WSAConnect = no_op
                fake_module.BindLocal = no_op
                fake_module.Overlapped = FakeOverlapped
                fake_module.original_overlapped = FakeOverlapped
                originals = {
                    name: getattr(fake_module, name)
                    for name in ("WSAConnect", "BindLocal", "Overlapped")
                }
                fake_module.fail_overlapped_assignment = True
                before_environment = dict(os.environ)
                before_socket_functions = {
                    "bind": socket.socket.bind,
                    "connect": socket.socket.connect,
                    "connect_ex": socket.socket.connect_ex,
                    "sendto": socket.socket.sendto,
                    "create_connection": socket.create_connection,
                }

                with patch.dict(sys.modules, {"_overlapped": fake_module}):
                    with assertion.assertRaises(RuntimeError):
                        guard.install()

                assertion.assertFalse(guard.installed)
                assertion.assertFalse(offline_guard_active())
                assertion.assertEqual(dict(os.environ), before_environment)
                for name, original in originals.items():
                    assertion.assertIs(getattr(fake_module, name), original)
                for name, original in before_socket_functions.items():
                    owner = (
                        socket.socket
                        if name in {"bind", "connect", "connect_ex", "sendto"}
                        else socket
                    )
                    assertion.assertIs(getattr(owner, name), original)
            elif case_name == "audit_hook_once":
                guard.install()
                guard.restore()
                with patch.object(
                    sys,
                    "addaudithook",
                    side_effect=AssertionError("audit hook registered twice"),
                ):
                    guard.install()
                    guard.restore()
            elif case_name in {
                "audit_registration_raised",
                "audit_registration_suppressed",
            }:
                keys = set(_EXPECTED_FLAGS) | {
                    "PADDLE_PDX_CACHE_HOME",
                    "PADDLE_PDX_LOCAL_FONT_FILE_PATH",
                }
                before_environment = {
                    key: os.environ.get(key) for key in keys
                }
                before_functions = {
                    "bind": socket.socket.bind,
                    "connect": socket.socket.connect,
                    "connect_ex": socket.socket.connect_ex,
                    "sendto": socket.socket.sendto,
                    "create_connection": socket.create_connection,
                    **{
                        name: getattr(socket, name)
                        for name in (
                            "getaddrinfo",
                            "gethostbyname",
                            "gethostbyname_ex",
                            "gethostbyaddr",
                            "getnameinfo",
                            "getfqdn",
                        )
                    },
                }
                registration_effect = (
                    {"side_effect": RuntimeError("audit unavailable")}
                    if case_name == "audit_registration_raised"
                    else {"return_value": None}
                )
                with patch.object(
                    sys,
                    "addaudithook",
                    **registration_effect,
                ):
                    with assertion.assertRaises(RuntimeError):
                        guard.install()

                assertion.assertFalse(guard.installed)
                assertion.assertFalse(offline_guard_active())
                assertion.assertEqual(
                    {key: os.environ.get(key) for key in keys},
                    before_environment,
                )
                for name, original in before_functions.items():
                    owner = socket.socket if name in {
                        "bind",
                        "connect",
                        "connect_ex",
                        "sendto",
                    } else socket
                    assertion.assertIs(getattr(owner, name), original)
            elif case_name == "qt_local_server":
                if sys.platform != "win32":
                    raise unittest.SkipTest("Windows QLocalServer unavailable")

                guard.install()
                from PySide6.QtCore import QCoreApplication
                from PySide6.QtNetwork import QLocalServer, QLocalSocket

                application = QCoreApplication.instance()
                if application is None:
                    application = QCoreApplication(["privacy-local-server"])
                server_name = f"TextSnapLayout-privacy-{os.getpid()}"
                QLocalServer.removeServer(server_name)
                server = QLocalServer()
                client = QLocalSocket()
                peer = None
                try:
                    assertion.assertTrue(server.listen(server_name))
                    client.connectToServer(server_name)
                    assertion.assertTrue(client.waitForConnected(2_000))
                    assertion.assertTrue(server.waitForNewConnection(2_000))
                    peer = server.nextPendingConnection()
                    assertion.assertIsNotNone(peer)

                    payload = b"local"
                    assertion.assertEqual(client.write(payload), len(payload))
                    assertion.assertTrue(client.flush())
                    if client.bytesToWrite():
                        assertion.assertTrue(client.waitForBytesWritten(2_000))
                    if not peer.bytesAvailable():
                        assertion.assertTrue(peer.waitForReadyRead(2_000))
                    assertion.assertEqual(bytes(peer.readAll()), payload)
                finally:
                    if peer is not None:
                        peer.close()
                    client.close()
                    server.close()
                    QLocalServer.removeServer(server_name)
            elif case_name == "af_unix":
                assertion.assertTrue(hasattr(socket, "AF_UNIX"))
                socket_path = str(root / "local.sock")
                with guard:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                        server.bind(socket_path)
                        server.listen(1)
                        with socket.socket(
                            socket.AF_UNIX,
                            socket.SOCK_STREAM,
                        ) as client:
                            client.connect(socket_path)
                            connection, _ = server.accept()
                            with connection:
                                client.sendall(b"local")
                                assertion.assertEqual(connection.recv(5), b"local")
            elif case_name == "no_files":
                with patch(
                    "os.mkdir",
                    side_effect=AssertionError("unexpected mkdir"),
                ):
                    guard.install()
                    guard.restore()
            elif case_name == "missing_temp":
                missing_cache = root / "missing-cache"
                missing_cache.mkdir()
                missing_guard = OfflineGuard(
                    cache_home=missing_cache.resolve(),
                    font_file=font_file.resolve(),
                )
                try:
                    with assertion.assertRaises(ValueError):
                        missing_guard.install()
                    assertion.assertFalse((missing_cache / "temp").exists())
                finally:
                    missing_guard.restore()
            elif case_name == "late_import":
                assertion.assertNotIn("paddlex", sys.modules)
                sys.modules["paddlex"] = object()
                with assertion.assertRaises(RuntimeError):
                    guard.install()
                assertion.assertFalse(guard.installed)
            else:
                raise ValueError(f"unknown privacy test case: {case_name}")
        finally:
            guard.restore()


class PrivacyTests(unittest.TestCase):
    def _run_clean_guard_case(self, case_name: str) -> None:
        test_file = Path(__file__).resolve()
        command = (
            sys.executable,
            "-I",
            "-B",
            str(test_file),
            "--guard-child",
            case_name,
        )
        try:
            result = subprocess.run(
                command,
                cwd=test_file.parents[1],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"clean privacy subprocess {case_name!r} timed out "
                f"after {exc.timeout} seconds"
            )
        if result.returncode != 0:
            stdout = result.stdout.strip() or "<empty>"
            stderr = result.stderr.strip() or "<empty>"
            self.fail(
                f"clean privacy subprocess {case_name!r} exited with "
                f"{result.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )

    def test_environment_is_set_before_use_and_fully_restored(self) -> None:
        self._run_clean_guard_case("environment")

    def test_inet_connections_and_datagrams_are_denied_and_restored(self) -> None:
        self._run_clean_guard_case("inet")

    def test_name_resolution_is_denied_without_reaching_resolvers(self) -> None:
        self._run_clean_guard_case("dns")

    def test_low_level_inet_operations_are_denied_and_logically_restored(
        self,
    ) -> None:
        self._run_clean_guard_case("low_level_inet")

    def test_low_level_name_resolution_is_denied_and_logically_restored(
        self,
    ) -> None:
        self._run_clean_guard_case("low_level_dns")

    def test_socket_subclass_cannot_mask_its_native_inet_family(self) -> None:
        self._run_clean_guard_case("masked_socket_family")

    @unittest.skipUnless(sys.platform == "win32", "Windows-only overlapped I/O")
    def test_windows_overlapped_inet_operations_are_denied_and_restored(
        self,
    ) -> None:
        self._run_clean_guard_case("overlapped_inet")

    @unittest.skipUnless(sys.platform == "win32", "Windows-only overlapped I/O")
    def test_windows_overlapped_preserves_pipe_file_io_and_dynamic_restore(
        self,
    ) -> None:
        self._run_clean_guard_case("overlapped_delegation")

    @unittest.skipUnless(sys.platform == "win32", "Windows-only overlapped I/O")
    def test_windows_overlapped_partial_install_rolls_back_atomically(
        self,
    ) -> None:
        self._run_clean_guard_case("overlapped_install_rollback")

    def test_socket_audit_hook_is_registered_only_once(self) -> None:
        self._run_clean_guard_case("audit_hook_once")

    def test_audit_registration_exception_rolls_back_atomically(self) -> None:
        self._run_clean_guard_case("audit_registration_raised")

    def test_suppressed_audit_registration_rolls_back_atomically(self) -> None:
        self._run_clean_guard_case("audit_registration_suppressed")

    @unittest.skipUnless(
        sys.platform == "win32" and importlib.util.find_spec("PySide6") is not None,
        "Windows PySide6 unavailable",
    )
    def test_qt_local_server_remains_available(self) -> None:
        self._run_clean_guard_case("qt_local_server")

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX unavailable")
    def test_af_unix_remains_available(self) -> None:
        self._run_clean_guard_case("af_unix")

    def test_guard_never_creates_cache_or_log_files(self) -> None:
        self._run_clean_guard_case("no_files")

    def test_missing_precreated_temp_directory_fails_without_creating_it(self) -> None:
        self._run_clean_guard_case("missing_temp")

    def test_guard_refuses_late_install_after_paddlex_import(self) -> None:
        self._run_clean_guard_case("late_import")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--guard-child":
        _run_guard_child(sys.argv[2])
    else:
        unittest.main()
