"""Package-external acceptance runner for an extracted Windows bundle.

This file is intentionally executed by the bundle's own ``pythonw.exe``.  It
imports only the standard library until the packaged offline guard is active
and writes a content-free JSON result outside the bundle.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import struct
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, NoReturn
import uuid
import weakref


_SCHEMA_VERSION = 1
_EXPECTED_PYTHON = (3, 13, 14)
_EXPECTED_WHEEL_COUNT = 68
_RUNTIME_PTH = b"python313.zip\n.\nLib/site-packages\n../app\n"
_CONTROLLED_TEXT = "TextSnap Layout OCR 2026"
_CONTROLLED_TEXT_SHA256 = (
    "6c696b511761e15c3a1c7b02a2fb1de5863ae00948685ec76422b9e7a450796f"
)
_FONT_RELATIVE = "assets/fonts/NotoSansMonoCJKsc-Regular.otf"
_FONT_SIZE = 16_393_784
_FONT_SHA256 = "ec04cc376b34887cedbdf84074e2e226ed2761eeabdcb9173fc1dd7bfd153ef7"
_EXPECTED_VERSIONS = {
    "aiohappyeyeballs": "2.7.1",
    "aiohttp": "3.14.3",
    "aiosignal": "1.4.0",
    "aistudio-sdk": "0.3.8",
    "annotated-types": "0.8.0",
    "anyio": "4.14.2",
    "attrs": "26.1.0",
    "bce-python-sdk": "0.9.76",
    "certifi": "2026.7.22",
    "chardet": "7.4.3",
    "charset-normalizer": "3.4.9",
    "click": "8.4.2",
    "colorama": "0.4.6",
    "colorlog": "6.12.0",
    "crc32c": "2.8",
    "filelock": "3.32.0",
    "frozenlist": "1.8.0",
    "fsspec": "2026.6.0",
    "future": "1.0.0",
    "h11": "0.16.0",
    "hf-xet": "1.5.2",
    "httpcore": "1.0.9",
    "httpx": "0.28.1",
    "huggingface-hub": "1.24.0",
    "idna": "3.18",
    "imagesize": "2.0.0",
    "modelscope": "1.38.1",
    "modelscope-hub": "0.1.8",
    "multidict": "6.7.1",
    "networkx": "3.6.1",
    "numpy": "2.2.6",
    "opencv-contrib-python": "4.10.0.84",
    "opt-einsum": "3.3.0",
    "packaging": "26.2",
    "paddleocr": "3.7.0",
    "paddlepaddle": "3.2.2",
    "paddlex": "3.7.2",
    "pandas": "3.0.5",
    "pillow": "12.3.0",
    "prettytable": "3.18.0",
    "propcache": "0.5.2",
    "protobuf": "7.35.1",
    "psutil": "7.2.2",
    "py-cpuinfo": "9.0.0",
    "pyclipper": "1.4.0",
    "pycryptodome": "3.23.0",
    "pydantic": "2.13.4",
    "pydantic-core": "2.46.4",
    "pypdfium2": "5.12.1",
    "pyside6-essentials": "6.11.1",
    "python-bidi": "0.6.11",
    "python-dateutil": "2.9.0.post0",
    "pyyaml": "6.0.2",
    "requests": "2.34.2",
    "ruamel-yaml": "0.19.1",
    "safetensors": "0.8.0",
    "setuptools": "83.0.0",
    "shapely": "2.1.2",
    "shiboken6": "6.11.1",
    "six": "1.17.0",
    "tqdm": "4.69.1",
    "typing-extensions": "4.16.0",
    "typing-inspection": "0.4.2",
    "tzdata": "2026.3",
    "ujson": "5.13.0",
    "urllib3": "2.7.0",
    "wcwidth": "0.8.2",
    "yarl": "1.24.5",
}
_CORE_DISTRIBUTIONS = (
    "numpy",
    "opencv-contrib-python",
    "paddleocr",
    "paddlepaddle",
    "paddlex",
    "pillow",
    "pyside6-essentials",
    "shiboken6",
)
_TRUE_CHECKS = frozenset(
    {
        "font_integrity",
        "model_integrity",
        "module_origins_in_bundle",
        "named_pipe_available",
        "network_blocks",
        "ocr_thread_cleanup",
        "offline_guard_installed_before_dependencies",
        "qt_components",
        "runtime",
    }
)
_FORBIDDEN_EARLY_IMPORTS = (
    "cv2",
    "numpy",
    "PIL",
    "paddle",
    "paddleocr",
    "paddlex",
    "PySide6",
)
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_NAME_NORMALIZER = re.compile(r"[-_.]+")
_MODEL_TIMEOUT_MS = 240_000
_OCR_TIMEOUT_MS = 120_000
_SHUTDOWN_TIMEOUT_MS = 60_000
_DIAGNOSTIC_PATH: Path | None = None
_DIAGNOSTIC_STARTED = time.perf_counter()
_DIAGNOSTIC_CREATED = False


class AcceptanceError(RuntimeError):
    """A fixed-code acceptance failure with no sensitive details."""

    def __init__(self, code: str) -> None:
        if not _IDENTIFIER.fullmatch(code):
            raise ValueError("invalid acceptance failure code")
        self.code = code
        super().__init__(code)


def _diagnostic(event: str, **details: object) -> None:
    """Append one content-free progress record outside the tested bundle."""

    global _DIAGNOSTIC_CREATED

    path = _DIAGNOSTIC_PATH
    if path is None:
        return
    record = {
        "elapsed_ms": max(0, round((time.perf_counter() - _DIAGNOSTIC_STARTED) * 1_000)),
        "event": event,
        **details,
    }
    mode = "a" if _DIAGNOSTIC_CREATED else "x"
    with path.open(mode, encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    _DIAGNOSTIC_CREATED = True


class _Resources:
    """Mutable cleanup ownership without importing Qt for annotations."""

    def __init__(self) -> None:
        self.application: Any | None = None
        self.controller: Any | None = None
        self.local_server: Any | None = None
        self.widgets: list[Any] = []
        self.qt_wait: Callable[[Callable[[], bool], int], bool] | None = None
        self.worker_stuck = False


def _canonical_name(value: str) -> str:
    return _NAME_NORMALIZER.sub("-", value).lower()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _snapshot_tree(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    """Hash every bundle file and record all paths without exposing mtimes."""

    entries: list[tuple[str, str, int, str]] = []
    for path in root.rglob("*"):
        if _is_reparse_point(path):
            raise AcceptanceError("bundle-reparse-point")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "directory", 0, ""))
        elif path.is_file():
            digest, size = _sha256_file(path)
            entries.append((relative, "file", size, digest))
        else:
            raise AcceptanceError("bundle-special-file")
    return tuple(sorted(entries, key=lambda item: (item[0].casefold(), item[0])))


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    """Publish canonical JSON without following an existing result symlink."""

    payload = (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    created = False
    try:
        with temporary.open("xb") as output:
            created = True
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        created = False
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def _assert_public_result(document: Mapping[str, Any]) -> None:
    """Enforce the complete content-free result schema and fixed value domain."""

    expected_keys = {
        "schema_version",
        "ok",
        "status",
        "cleanup_ok",
        "checks",
        "failure",
    }
    if set(document) != expected_keys:
        raise AcceptanceError("unsafe-result-document")
    ok = document["ok"]
    cleanup_ok = document["cleanup_ok"]
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != _SCHEMA_VERSION
        or type(ok) is not bool
        or type(cleanup_ok) is not bool
        or type(document["status"]) is not str
        or document["status"] != ("passed" if ok else "failed")
        or (ok and not cleanup_ok)
    ):
        raise AcceptanceError("unsafe-result-document")

    failure = document["failure"]
    if ok:
        if failure is not None:
            raise AcceptanceError("unsafe-result-document")
    elif (
        not isinstance(failure, Mapping)
        or set(failure) != {"type", "code"}
        or type(failure["type"]) is not str
        or type(failure["code"]) is not str
        or _IDENTIFIER.fullmatch(failure["type"]) is None
        or _IDENTIFIER.fullmatch(failure["code"]) is None
    ):
        raise AcceptanceError("unsafe-result-document")

    checks = document["checks"]
    if not isinstance(checks, Mapping):
        raise AcceptanceError("unsafe-result-document")
    allowed_checks = _TRUE_CHECKS | {
        "bundle_entry_count",
        "bundle_unchanged",
        "dependency_count",
        "dependency_versions",
        "ocr",
    }
    if not set(checks).issubset(allowed_checks):
        raise AcceptanceError("unsafe-result-document")
    if any(checks[key] is not True for key in set(checks) & _TRUE_CHECKS):
        raise AcceptanceError("unsafe-result-document")

    if "bundle_entry_count" in checks:
        entry_count = checks["bundle_entry_count"]
        if type(entry_count) is not int or entry_count < 0:
            raise AcceptanceError("unsafe-result-document")
    if "bundle_unchanged" in checks and type(checks["bundle_unchanged"]) is not bool:
        raise AcceptanceError("unsafe-result-document")
    if "dependency_count" in checks and checks["dependency_count"] != len(
        _EXPECTED_VERSIONS
    ):
        raise AcceptanceError("unsafe-result-document")
    if "dependency_versions" in checks:
        expected_core = {
            name: _EXPECTED_VERSIONS[name] for name in _CORE_DISTRIBUTIONS
        }
        if checks["dependency_versions"] != expected_core:
            raise AcceptanceError("unsafe-result-document")
    if "ocr" in checks:
        ocr = checks["ocr"]
        if not isinstance(ocr, Mapping) or set(ocr) != {
            "success",
            "line_count",
            "elapsed_ms",
            "output_sha256_match",
        }:
            raise AcceptanceError("unsafe-result-document")
        if ocr["success"] is not True:
            raise AcceptanceError("unsafe-result-document")
        for key in ("line_count", "elapsed_ms"):
            if type(ocr[key]) is not int or ocr[key] < 0:
                raise AcceptanceError("unsafe-result-document")
        if type(ocr["output_sha256_match"]) is not bool:
            raise AcceptanceError("unsafe-result-document")


def _safe_exception_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if _IDENTIFIER.fullmatch(name) else "RuntimeError"


def _failure_code(phase: str, error: BaseException) -> str:
    if isinstance(error, AcceptanceError):
        return error.code
    if not _IDENTIFIER.fullmatch(phase):
        phase = "acceptance"
    return f"{phase}-failed"


def _locked_versions_from_manifest(document: Mapping[str, Any]) -> dict[str, str]:
    wheels = document.get("wheels")
    if not isinstance(wheels, list) or len(wheels) != _EXPECTED_WHEEL_COUNT:
        raise AcceptanceError("manifest-wheel-count")
    versions: dict[str, str] = {}
    for wheel in wheels:
        if not isinstance(wheel, Mapping):
            raise AcceptanceError("manifest-wheel-entry")
        name = wheel.get("name")
        version = wheel.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise AcceptanceError("manifest-wheel-entry")
        canonical = _canonical_name(name)
        if canonical in versions:
            raise AcceptanceError("manifest-wheel-duplicate")
        versions[canonical] = version
    if versions != _EXPECTED_VERSIONS:
        raise AcceptanceError("manifest-dependency-versions")
    return versions


def _validate_invocation_paths(
    bundle_value: str,
    result_value: str,
    executable: Path,
) -> tuple[Path, Path]:
    bundle = Path(bundle_value).resolve(strict=True)
    if not bundle.is_dir():
        raise AcceptanceError("bundle-root-invalid")
    result = Path(os.path.abspath(result_value))
    result_parent = result.parent.resolve(strict=True)
    if not result_parent.is_dir() or result.is_dir() or result.is_symlink():
        raise AcceptanceError("result-parent-invalid")
    resolved_destination = result_parent / result.name
    if resolved_destination == bundle or resolved_destination.is_relative_to(bundle):
        raise AcceptanceError("result-inside-bundle")
    expected_executable = (bundle / "runtime" / "pythonw.exe").resolve(strict=True)
    if executable.resolve(strict=True) != expected_executable:
        raise AcceptanceError("runtime-executable")
    return bundle, result


def _verify_runtime_pth(bundle: Path) -> None:
    try:
        runtime_pth = (bundle / "runtime" / "python313._pth").read_bytes()
    except OSError:
        raise AcceptanceError("runtime-pth") from None
    if runtime_pth != _RUNTIME_PTH:
        raise AcceptanceError("runtime-pth")


def _verify_runtime_flags(flags: Any) -> None:
    if (
        flags.isolated != 1
        or flags.dont_write_bytecode != 1
        or flags.no_site != 1
        or flags.no_user_site != 1
        or flags.ignore_environment != 1
        or flags.safe_path is not True
    ):
        raise AcceptanceError("runtime-flags")


def _verify_runtime(bundle: Path) -> None:
    if os.name != "nt" or sys.platform != "win32":
        raise AcceptanceError("runtime-platform")
    if tuple(sys.version_info[:3]) != _EXPECTED_PYTHON:
        raise AcceptanceError("runtime-python-version")
    if platform.machine().upper() != "AMD64" or struct.calcsize("P") != 8:
        raise AcceptanceError("runtime-architecture")
    _verify_runtime_flags(sys.flags)
    _verify_runtime_pth(bundle)

    expected_paths = (
        bundle / "runtime" / "python313.zip",
        bundle / "runtime",
        bundle / "runtime" / "Lib" / "site-packages",
        bundle / "app",
    )
    actual = tuple(Path(value).resolve(strict=False) for value in sys.path)
    expected = tuple(path.resolve(strict=False) for path in expected_paths)
    if actual != expected:
        raise AcceptanceError("runtime-search-path")


def _verify_file(path: Path, expected_size: int, expected_sha256: str) -> None:
    digest, size = _sha256_file(path)
    if size != expected_size or digest != expected_sha256:
        raise AcceptanceError("font-integrity")


def _verify_distributions(
    bundle: Path,
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    from importlib import metadata

    expected = _locked_versions_from_manifest(manifest)
    site_packages = (bundle / "runtime" / "Lib" / "site-packages").resolve()
    actual: dict[str, str] = {}
    for distribution in metadata.distributions(path=[str(site_packages)]):
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not isinstance(name, str) or not isinstance(version, str):
            raise AcceptanceError("runtime-distribution-metadata")
        canonical = _canonical_name(name)
        if canonical in actual:
            raise AcceptanceError("runtime-distribution-duplicate")
        origin = Path(distribution.locate_file("")).resolve(strict=False)
        if origin != site_packages and not origin.is_relative_to(site_packages):
            raise AcceptanceError("runtime-distribution-origin")
        actual[canonical] = version
    if actual != expected:
        raise AcceptanceError("runtime-distribution-set")
    return {name: actual[name] for name in _CORE_DISTRIBUTIONS}


def _expect_offline(
    operation: Callable[[], object],
    offline_error: type[BaseException],
) -> None:
    try:
        operation()
    except offline_error:
        return
    raise AcceptanceError("offline-probe-not-blocked")


def _verify_network_blocks(offline_error: type[BaseException]) -> None:
    import _overlapped
    import _socket
    import socket

    class AuditSentinel(RuntimeError):
        pass

    watched = {
        "socket.bind",
        "socket.connect",
        "socket.sendto",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }

    def sentinel(event: str, arguments: tuple[object, ...]) -> None:
        del arguments
        if event in watched:
            raise AuditSentinel(event)

    sys.addaudithook(sentinel)

    def socket_operation(
        constructor: Callable[..., Any],
        family: int,
        socket_type: int,
        method: str,
    ) -> None:
        client = constructor(family, socket_type)
        try:
            address: tuple[Any, ...]
            if family == socket.AF_INET6:
                address = ("::1", 0 if method == "bind" else 9, 0, 0)
            else:
                address = ("127.0.0.1", 0 if method == "bind" else 9)
            if method == "sendto":
                client.sendto(b"blocked", address)
            else:
                getattr(client, method)(address)
        finally:
            client.close()
            if client.fileno() != -1:
                raise AcceptanceError("offline-probe-socket-close")

    for constructor in (socket.socket, socket.SocketType, _socket.socket):
        for family in (socket.AF_INET, socket.AF_INET6):
            for method, socket_type in (
                ("bind", socket.SOCK_STREAM),
                ("connect", socket.SOCK_STREAM),
                ("connect_ex", socket.SOCK_STREAM),
                ("sendto", socket.SOCK_DGRAM),
            ):
                _expect_offline(
                    lambda c=constructor, f=family, t=socket_type, m=method: (
                        socket_operation(c, f, t, m)
                    ),
                    offline_error,
                )

    for operation in (
        lambda: _socket.getaddrinfo("offline.invalid", 443),
        lambda: _socket.gethostbyname("offline.invalid"),
        lambda: _socket.gethostbyaddr("203.0.113.1"),
        lambda: _socket.getnameinfo(("203.0.113.1", 443), 0),
    ):
        _expect_offline(operation, offline_error)

    # Exercise the guarded Overlapped proxy with genuine Winsock handles.
    # The sockets remain unbound and unconnected, so even a broken guard cannot
    # send traffic; the native fallback would only report WSAENOTCONN.
    for family in (socket.AF_INET, socket.AF_INET6):
        client = socket.SocketType(family, socket.SOCK_STREAM)
        try:
            handle = client.fileno()
            if handle < 0:
                raise AcceptanceError("offline-probe-socket-handle")
            for operation in (
                lambda h=handle: _overlapped.Overlapped().WSASend(
                    h,
                    b"blocked",
                    0,
                ),
                lambda h=handle: _overlapped.Overlapped().WSARecv(h, 1, 0),
            ):
                _expect_offline(operation, offline_error)
                if (
                    client.fileno() != handle
                    or client.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                    != socket.SOCK_STREAM
                ):
                    raise AcceptanceError("offline-probe-socket-invalidated")
        finally:
            client.close()
        if client.fileno() != -1:
            raise AcceptanceError("offline-probe-socket-close")

    # Invalid handles prove interception without allowing any kernel network I/O.
    for operation in (
        lambda: _overlapped.WSAConnect(-1, ("127.0.0.1", 9)),
        lambda: _overlapped.BindLocal(-1, socket.AF_INET),
        lambda: _overlapped.Overlapped().ConnectEx(-1, ("127.0.0.1", 9)),
        lambda: _overlapped.Overlapped().WSASendTo(
            -1,
            b"blocked",
            0,
            ("127.0.0.1", 9),
        ),
    ):
        _expect_offline(operation, offline_error)


def _qt_wait_factory() -> Callable[[Callable[[], bool], int], bool]:
    from PySide6.QtCore import QEventLoop, QTimer

    def wait_until(predicate: Callable[[], bool], timeout_ms: int) -> bool:
        if predicate():
            return True
        loop = QEventLoop()
        poll = QTimer()
        deadline = QTimer()
        poll.setInterval(20)
        deadline.setSingleShot(True)

        def inspect() -> None:
            if predicate():
                loop.quit()

        poll.timeout.connect(inspect)
        deadline.timeout.connect(loop.quit)
        poll.start()
        deadline.start(timeout_ms)
        try:
            loop.exec()
        finally:
            poll.stop()
            deadline.stop()
        return bool(predicate())

    return wait_until


def _verify_named_pipe(resources: _Resources) -> None:
    from textsnap.qt_instance import InstanceCommandServer

    name = rf"\\.\pipe\LOCAL\TextSnapLayout.Acceptance.{os.getpid()}.{uuid.uuid4().hex}"
    server = InstanceCommandServer(server_name=name)
    resources.local_server = server
    received: list[str] = []
    server.command_received.connect(received.append)
    server.start()
    assert resources.qt_wait is not None
    sender_code = (
        "from textsnap.qt_instance import send_instance_command;"
        "raise SystemExit(0 if send_instance_command("
        f"'open-settings', server_name={name!r}, attempts=1, timeout_ms=3000"
        ") else 1)"
    )
    sender = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", sender_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # The server lives on this GUI thread, so its event loop must run while
        # the independent sender connects and flushes the command.
        if not resources.qt_wait(lambda: bool(received), 5_000):
            raise AcceptanceError("named-pipe-receive")
        try:
            sender_exit = sender.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise AcceptanceError("named-pipe-send") from None
        if sender_exit != 0:
            raise AcceptanceError("named-pipe-send")
    finally:
        if sender.poll() is None:
            sender.kill()
            sender.wait()
    if received != ["open-settings"]:
        raise AcceptanceError("named-pipe-payload")
    server.close()
    server.deleteLater()
    resources.local_server = None


def _construct_qt_components(
    bundle: Path,
    resources: _Resources,
) -> None:
    from PySide6.QtGui import QFontDatabase, QIcon
    from PySide6.QtWidgets import QPlainTextEdit
    from textsnap.ui import (
        ErrorDialog,
        ProgressWindow,
        ResultWindow,
        SettingsWindow,
        TrayUi,
    )

    font_id = QFontDatabase.addApplicationFont(str(bundle / _FONT_RELATIVE))
    if font_id < 0:
        raise AcceptanceError("qt-font-load")
    if "Noto Sans Mono CJK SC" not in QFontDatabase.applicationFontFamilies(font_id):
        raise AcceptanceError("qt-font-family")
    icon = QIcon(str(bundle / "assets" / "icons" / "textsnap.ico"))
    if icon.isNull():
        raise AcceptanceError("qt-icon-load")

    result = ResultWindow()
    progress = ProgressWindow()
    settings = SettingsWindow()
    error = ErrorDialog()
    tray = TrayUi(icon)
    resources.widgets.extend((result, progress, settings, error, tray))
    if any(
        widget.isVisible()
        for widget in (result, progress, settings, error, tray, tray.menu)
    ):
        raise AcceptanceError("qt-widget-visible")
    if result.text_edit.lineWrapMode() != QPlainTextEdit.LineWrapMode.NoWrap:
        raise AcceptanceError("qt-result-wrap")
    progress.set_waiting_for_model()
    progress.set_recognizing()
    settings.set_model_status("ready")
    if tray.toolTip() != "TextSnap Layout":
        raise AcceptanceError("qt-tray-tooltip")


def _controlled_image(font_path: Path) -> object:
    from PIL import Image, ImageDraw, ImageFont
    import numpy

    font = ImageFont.truetype(str(font_path), size=56)
    canvas = Image.new("RGB", (1_400, 190), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((40, 48), _CONTROLLED_TEXT, font=font, fill="black")
    image = numpy.asarray(canvas, dtype=numpy.uint8)[:, :, ::-1].copy()
    del draw
    del canvas
    del font
    return image


def _run_threaded_ocr(
    bundle: Path,
    resources: _Resources,
    checks: dict[str, Any],
) -> None:
    from textsnap.domain import Failure, ModelState, Success
    from textsnap.ocr import OcrEngine
    from textsnap.paths import BundlePaths
    from textsnap.qt_worker import OcrThreadController

    paths = BundlePaths(bundle)
    detector, recognizer = paths.model_specs()
    engine_count = 0

    def engine_factory() -> OcrEngine:
        nonlocal engine_count
        engine_count += 1
        engine = OcrEngine(detector, recognizer)
        if dict(engine.engine_config) != {
            "device_type": "cpu",
            "providers": ("CPUExecutionProvider",),
            "graph_optimization_level": 99,
            "intra_op_num_threads": 10,
            "inter_op_num_threads": 1,
            "execution_mode": "sequential",
            "log_severity_level": 3,
            "enable_mem_pattern": True,
            "enable_cpu_mem_arena": True,
        }:
            raise AcceptanceError("ocr-engine-config")
        return engine

    controller = OcrThreadController(engine_factory)
    resources.controller = controller
    model_failures: list[object] = []
    outcomes: list[object] = []
    rejections: list[object] = []
    controller.model_failed.connect(model_failures.append)
    controller.task_finished.connect(lambda _task_id, outcome: outcomes.append(outcome))
    controller.task_rejected.connect(
        lambda _task_id, failure: rejections.append(failure)
    )
    _diagnostic("ocr-model-start")
    controller.start()
    assert resources.qt_wait is not None
    if not resources.qt_wait(
        lambda: controller.model_state in {ModelState.READY, ModelState.ERROR},
        _MODEL_TIMEOUT_MS,
    ):
        raise AcceptanceError("ocr-model-timeout")
    if controller.model_state is not ModelState.READY or model_failures:
        _diagnostic(
            "ocr-model-failed",
            failure_count=len(model_failures),
            model_state=controller.model_state.value,
        )
        raise AcceptanceError("ocr-model-initialize")
    if engine_count != 1:
        raise AcceptanceError("ocr-engine-count")
    checks["model_integrity"] = True
    _diagnostic("ocr-model-ready", engine_count=engine_count)

    image: Any | None = _controlled_image(paths.font_file)
    image_reference = weakref.ref(image)
    started = time.perf_counter()
    task = controller.submit(image)
    if task is None:
        raise AcceptanceError("ocr-task-submit")
    _diagnostic("ocr-task-submitted")
    if not resources.qt_wait(lambda: bool(outcomes or rejections), _OCR_TIMEOUT_MS):
        raise AcceptanceError("ocr-task-timeout")
    elapsed_ms = max(0, round((time.perf_counter() - started) * 1_000))
    if rejections or len(outcomes) != 1:
        raise AcceptanceError("ocr-task-rejected")

    outcome = outcomes[0]
    if isinstance(outcome, Failure):
        raise AcceptanceError("ocr-task-failure")
    if not isinstance(outcome, Success) or not outcome.result.text:
        raise AcceptanceError("ocr-task-not-success")
    text_digest = hashlib.sha256(outcome.result.text.encode("utf-8")).hexdigest()
    line_count = outcome.result.stats.line_count
    matches = text_digest == _CONTROLLED_TEXT_SHA256
    checks["ocr"] = {
        "success": True,
        "line_count": line_count,
        "elapsed_ms": elapsed_ms,
        "output_sha256_match": matches,
    }
    if line_count != 1 or not matches:
        raise AcceptanceError("ocr-controlled-output")
    _diagnostic(
        "ocr-task-complete",
        duration_ms=elapsed_ms,
        line_count=line_count,
        output_sha256_match=matches,
    )

    controller.shutdown()
    if not resources.qt_wait(lambda: not controller.running, _SHUTDOWN_TIMEOUT_MS):
        raise AcceptanceError("ocr-shutdown-timeout")
    if not controller.wait_for_shutdown() or controller.shutdown_failure is not None:
        raise AcceptanceError("ocr-shutdown-failure")
    if controller.active_task is not None:
        raise AcceptanceError("ocr-task-reference")

    image = None
    task = None
    outcomes.clear()
    rejections.clear()
    resources.application.processEvents()
    gc.collect()
    if image_reference() is not None:
        raise AcceptanceError("ocr-image-reference")
    controller.deleteLater()
    resources.application.processEvents()
    resources.controller = None
    checks["ocr_thread_cleanup"] = True
    _diagnostic("ocr-thread-cleanup")


def _verify_module_origins(bundle: Path) -> None:
    bundle_root = bundle.resolve()
    runner = Path(__file__).resolve()
    shiboken = sys.modules.get("shiboken6")
    shiboken_origin_value = getattr(shiboken, "__file__", None)
    shiboken_origin = (
        Path(shiboken_origin_value).resolve(strict=False)
        if isinstance(shiboken_origin_value, str)
        else None
    )
    for name, module in tuple(sys.modules.items()):
        if name == "__main__":
            continue
        origin_value = getattr(module, "__file__", None)
        if not isinstance(origin_value, str) or origin_value.startswith("<"):
            continue
        origin = Path(origin_value).resolve(strict=False)
        if origin == runner:
            continue
        embedded_origin = PurePosixPath(origin_value)
        embedded_shiboken_name = (
            name == "signature_bootstrap"
            or name == "__feature__"
            or name == "shibokensupport"
            or name.startswith("shibokensupport.")
            or name.startswith("PySide6.support.")
        )
        embedded_shiboken_path = (
            origin_value == "signature_bootstrap.py"
            or (
                embedded_origin.parts
                and embedded_origin.parts[0] == "shibokensupport"
                and not embedded_origin.is_absolute()
                and ".." not in embedded_origin.parts
            )
        )
        if (
            embedded_shiboken_name
            and embedded_shiboken_path
            and not origin.exists()
            and shiboken_origin is not None
            and shiboken_origin.is_relative_to(bundle_root)
        ):
            # Shiboken embeds these Python helpers in its bundled DLL but gives
            # the synthetic modules relative __file__ values. Resolving those
            # markers against cwd must not be mistaken for host dependencies.
            continue
        if origin != bundle_root and not origin.is_relative_to(bundle_root):
            _diagnostic(
                "module-origin-rejected",
                module=name,
                origin=str(origin),
            )
            raise AcceptanceError("module-origin-outside-bundle")


def _cleanup_resources(resources: _Resources) -> bool:
    clean = True
    server = resources.local_server
    if server is not None:
        try:
            server.close()
            server.deleteLater()
        except BaseException:
            clean = False
        resources.local_server = None

    controller = resources.controller
    if controller is not None:
        try:
            controller.shutdown()
        except BaseException:
            clean = False
        try:
            if controller.running:
                if resources.qt_wait is None or not resources.qt_wait(
                    lambda: not controller.running,
                    _SHUTDOWN_TIMEOUT_MS,
                ):
                    clean = False
        except BaseException:
            clean = False
        try:
            controller_running = bool(controller.running)
        except BaseException:
            controller_running = True
            clean = False
        if controller_running:
            clean = False
            resources.worker_stuck = True
            # A live child QThread must retain its owning wrapper until the
            # immediate os._exit path runs; destroying it would make Qt abort
            # before the sanitized result can be published.
            resources.controller = controller
        else:
            try:
                if not controller.wait_for_shutdown():
                    clean = False
                if controller.shutdown_failure is not None:
                    clean = False
                controller.deleteLater()
            except BaseException:
                clean = False
            resources.controller = None

    for widget in reversed(resources.widgets):
        try:
            hide = getattr(widget, "hide", None)
            if callable(hide):
                hide()
            close = getattr(widget, "close", None)
            if callable(close):
                close()
            delete_later = getattr(widget, "deleteLater", None)
            if callable(delete_later):
                delete_later()
        except BaseException:
            clean = False
    resources.widgets.clear()

    application = resources.application
    if application is not None:
        try:
            from PySide6.QtCore import QCoreApplication, QEvent

            application.processEvents()
            application.closeAllWindows()
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            application.processEvents()
            application.quit()
        except BaseException:
            clean = False
    return clean


def _write_stuck_result_and_exit(
    result: Path,
    checks: Mapping[str, Any],
) -> NoReturn:
    """Publish a fixed failure if possible, then terminate with the guard active."""

    try:
        document = _result_document(
            ok=False,
            checks=checks,
            phase="cleanup",
            error=AcceptanceError("ocr-worker-stuck"),
            cleanup_ok=False,
        )
        _write_json_atomic(result, document)
    finally:
        # Never return into Python/Qt teardown with a running QThread.
        os._exit(1)


def _execute_acceptance(
    bundle: Path,
    checks: dict[str, Any],
    resources: _Resources,
) -> None:
    manifest_path = bundle / "BUILD_MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AcceptanceError("manifest-read") from None
    if not isinstance(manifest, Mapping):
        raise AcceptanceError("manifest-root")
    _diagnostic("manifest-loaded")

    for prefix in _FORBIDDEN_EARLY_IMPORTS:
        if any(
            name == prefix or name.startswith(f"{prefix}.") for name in sys.modules
        ):
            raise AcceptanceError("offline-guard-late")

    from textsnap.privacy import OfflineGuard, OfflineNetworkError

    font_path = bundle / _FONT_RELATIVE
    _verify_file(font_path, _FONT_SIZE, _FONT_SHA256)
    checks["font_integrity"] = True
    guard = OfflineGuard(
        cache_home=(bundle / "runtime" / "pdx-cache").resolve(),
        font_file=font_path.resolve(),
    )
    checks["_guard"] = guard
    guard.install()
    checks["offline_guard_installed_before_dependencies"] = True
    _diagnostic("offline-guard-installed")

    _verify_network_blocks(OfflineNetworkError)
    checks["network_blocks"] = True
    _diagnostic("network-probes-complete")

    from textsnap.bootstrap import clear_external_qt_paths

    clear_external_qt_paths()
    os.environ["QT_QPA_PLATFORM"] = "windows"

    core_versions = _verify_distributions(bundle, manifest)
    checks["dependency_versions"] = core_versions
    checks["dependency_count"] = _EXPECTED_WHEEL_COUNT
    _diagnostic("dependency-closure-complete", count=_EXPECTED_WHEEL_COUNT)

    # These imports are deliberately after OfflineGuard.install().
    import cv2
    import numpy
    import paddle
    import paddleocr
    import paddlex
    import PIL
    import PySide6
    from PySide6.QtWidgets import QApplication

    del cv2, numpy, paddle, paddleocr, paddlex, PIL, PySide6
    _diagnostic("native-dependencies-imported")

    application = QApplication.instance()
    if application is None:
        application = QApplication(["TextSnapLayout-package-acceptance"])
    application.setQuitOnLastWindowClosed(False)
    resources.application = application
    resources.qt_wait = _qt_wait_factory()
    _diagnostic("qt-application-ready")

    _verify_named_pipe(resources)
    checks["named_pipe_available"] = True
    _diagnostic("named-pipe-complete")
    _construct_qt_components(bundle, resources)
    checks["qt_components"] = True
    _diagnostic("qt-components-complete")
    _run_threaded_ocr(bundle, resources, checks)
    _verify_module_origins(bundle)
    checks["module_origins_in_bundle"] = True
    _diagnostic("module-origins-complete")


def _result_document(
    *,
    ok: bool,
    checks: Mapping[str, Any],
    phase: str,
    error: BaseException | None,
    cleanup_ok: bool,
) -> dict[str, Any]:
    public_checks = {key: value for key, value in checks.items() if not key.startswith("_")}
    document: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "ok": ok,
        "status": "passed" if ok else "failed",
        "cleanup_ok": cleanup_ok,
        "checks": public_checks,
        "failure": None,
    }
    if error is not None:
        document["failure"] = {
            "type": _safe_exception_type(error),
            "code": _failure_code(phase, error),
        }
    _assert_public_result(document)
    return document


def main(argv: list[str] | None = None) -> int:
    global _DIAGNOSTIC_PATH

    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args(argv)

    checks: dict[str, Any] = {}
    resources = _Resources()
    before: tuple[tuple[str, str, int, str], ...] | None = None
    bundle: Path | None = None
    result = Path(os.path.abspath(arguments.result))
    invocation_valid = False
    phase = "invocation"
    error: BaseException | None = None
    cleanup_ok = True

    try:
        bundle, result = _validate_invocation_paths(
            arguments.bundle,
            arguments.result,
            Path(sys.executable),
        )
        invocation_valid = True
        _DIAGNOSTIC_PATH = result.with_name(f"{result.name}.progress.jsonl")
        _diagnostic("invocation-complete", pid=os.getpid())
        phase = "runtime"
        _diagnostic("runtime-verification-start")
        _verify_runtime(bundle)
        checks["runtime"] = True
        _diagnostic("runtime-verification-complete")
        phase = "snapshot-before"
        _diagnostic("snapshot-before-start")
        before = _snapshot_tree(bundle)
        checks["bundle_entry_count"] = len(before)
        _diagnostic("snapshot-before-complete", entry_count=len(before))
        phase = "acceptance"
        _diagnostic("acceptance-start")
        _execute_acceptance(bundle, checks, resources)
    except BaseException as exc:
        error = exc
    finally:
        phase_before_cleanup = phase
        phase = "cleanup"
        _diagnostic("cleanup-start")
        cleanup_ok = _cleanup_resources(resources)
        if resources.worker_stuck:
            _write_stuck_result_and_exit(result, checks)
        guard = checks.pop("_guard", None)
        if guard is not None:
            try:
                guard.restore()
            except BaseException as exc:
                cleanup_ok = False
                if error is None:
                    error = exc
        gc.collect()

        if bundle is not None and before is not None:
            try:
                _diagnostic("snapshot-after-start")
                after = _snapshot_tree(bundle)
                checks["bundle_unchanged"] = after == before
                _diagnostic(
                    "snapshot-after-complete",
                    entry_count=len(after),
                    unchanged=after == before,
                )
                if after != before and error is None:
                    error = AcceptanceError("bundle-tree-changed")
            except BaseException as exc:
                cleanup_ok = False
                if error is None:
                    error = exc
        if not cleanup_ok and error is None:
            error = AcceptanceError("cleanup-failed")
        phase = phase_before_cleanup if error is not None else "complete"
        _diagnostic(
            "cleanup-complete",
            cleanup_ok=cleanup_ok,
            failure_code=None if error is None else _failure_code(phase, error),
        )

    if not invocation_valid:
        return 2
    document = _result_document(
        ok=error is None and cleanup_ok,
        checks=checks,
        phase=phase,
        error=error,
        cleanup_ok=cleanup_ok,
    )
    try:
        _write_json_atomic(result, document)
        _diagnostic("result-written", ok=document["ok"])
    except BaseException:
        return 2
    if resources.worker_stuck:
        os._exit(1)
    return 0 if document["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
