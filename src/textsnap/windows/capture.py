"""Physical-pixel capture of the monitor under the mouse using Win32 GDI."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from typing import Callable, Final, Protocol, TypeVar

from textsnap.domain import CaptureFrame


MONITOR_DEFAULT_TO_NEAREST: Final = 2
SRCCOPY: Final = 0x00CC0020
CAPTUREBLT: Final = 0x40000000
RASTER_OPERATION: Final = SRCCOPY | CAPTUREBLT
DIB_RGB_COLORS: Final = 0
BI_RGB: Final = 0
HGDI_ERROR: Final = -1
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    """A monitor rectangle expressed in physical virtual-screen pixels."""

    monitor_id: str
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if not self.monitor_id or "\r" in self.monitor_id or "\n" in self.monitor_id:
            raise ValueError("monitor_id must be a non-empty single line")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("monitor rectangle must have positive dimensions")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class CaptureApi(Protocol):
    """Mockable Win32 surface used by :func:`capture_monitor_under_cursor`."""

    def get_cursor_position(self) -> tuple[int, int] | None: ...

    def monitor_from_point(self, x: int, y: int) -> object | None: ...

    def get_monitor_info(self, monitor: object) -> MonitorInfo | None: ...

    def get_monitor_dpi(self, monitor: object) -> tuple[int, int] | None: ...

    def dwm_flush(self) -> int: ...

    def get_screen_dc(self) -> object | None: ...

    def create_compatible_dc(self, source_dc: object) -> object | None: ...

    def create_compatible_bitmap(
        self,
        source_dc: object,
        width: int,
        height: int,
    ) -> object | None: ...

    def select_object(self, dc: object, gdi_object: object) -> object | None: ...

    def bit_blt(
        self,
        destination_dc: object,
        width: int,
        height: int,
        source_dc: object,
        source_x: int,
        source_y: int,
        raster_operation: int,
    ) -> bool: ...

    def get_bgra_bits(
        self,
        dc: object,
        bitmap: object,
        width: int,
        height: int,
    ) -> bytes | None: ...

    def delete_object(self, gdi_object: object) -> bool: ...

    def delete_dc(self, dc: object) -> bool: ...

    def release_screen_dc(self, dc: object) -> bool: ...

    def get_last_error(self) -> int: ...


class CaptureError(RuntimeError):
    """A capture failure that contains neither pixels nor user paths."""

    def __init__(
        self,
        diagnostic_code: str,
        public_message: str = "无法截取当前显示器。",
        winerror: int | None = None,
    ) -> None:
        self.diagnostic_code = diagnostic_code
        self.public_message = public_message
        self.winerror = winerror
        suffix = f" (Win32 error {winerror})" if winerror else ""
        super().__init__(f"{public_message} [{diagnostic_code}]{suffix}")


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", wintypes.BYTE),
        ("rgbGreen", wintypes.BYTE),
        ("rgbRed", wintypes.BYTE),
        ("rgbReserved", wintypes.BYTE),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", _RGBQUAD * 1),
    ]


class CtypesCaptureApi:
    """ctypes-backed Win32 adapter, instantiated only on Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise CaptureError("CAPTURE-UNSUPPORTED", "当前平台不支持 Win32 截图。")

        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._shcore = ctypes.WinDLL("shcore", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._get_cursor_pos = self._user32.GetCursorPos
        self._get_cursor_pos.argtypes = [ctypes.POINTER(_POINT)]
        self._get_cursor_pos.restype = wintypes.BOOL

        self._monitor_from_point = self._user32.MonitorFromPoint
        self._monitor_from_point.argtypes = [_POINT, wintypes.DWORD]
        self._monitor_from_point.restype = wintypes.HANDLE

        self._get_monitor_info = self._user32.GetMonitorInfoW
        self._get_monitor_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_MONITORINFOEXW),
        ]
        self._get_monitor_info.restype = wintypes.BOOL

        self._get_dpi_for_monitor = self._shcore.GetDpiForMonitor
        self._get_dpi_for_monitor.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        self._get_dpi_for_monitor.restype = ctypes.c_long

        self._dwm_flush = self._dwmapi.DwmFlush
        self._dwm_flush.argtypes = []
        self._dwm_flush.restype = ctypes.c_long

        self._get_dc = self._user32.GetDC
        self._get_dc.argtypes = [wintypes.HWND]
        self._get_dc.restype = wintypes.HANDLE

        self._create_compatible_dc = self._gdi32.CreateCompatibleDC
        self._create_compatible_dc.argtypes = [wintypes.HANDLE]
        self._create_compatible_dc.restype = wintypes.HANDLE

        self._create_compatible_bitmap = self._gdi32.CreateCompatibleBitmap
        self._create_compatible_bitmap.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._create_compatible_bitmap.restype = wintypes.HANDLE

        self._select_object = self._gdi32.SelectObject
        self._select_object.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._select_object.restype = wintypes.HANDLE

        self._bit_blt = self._gdi32.BitBlt
        self._bit_blt.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        self._bit_blt.restype = wintypes.BOOL

        self._get_dibits = self._gdi32.GetDIBits
        self._get_dibits.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.POINTER(_BITMAPINFO),
            wintypes.UINT,
        ]
        self._get_dibits.restype = ctypes.c_int

        self._delete_object = self._gdi32.DeleteObject
        self._delete_object.argtypes = [wintypes.HANDLE]
        self._delete_object.restype = wintypes.BOOL

        self._delete_dc = self._gdi32.DeleteDC
        self._delete_dc.argtypes = [wintypes.HANDLE]
        self._delete_dc.restype = wintypes.BOOL

        self._release_dc = self._user32.ReleaseDC
        self._release_dc.argtypes = [wintypes.HWND, wintypes.HANDLE]
        self._release_dc.restype = ctypes.c_int

    def _clear_error(self) -> None:
        self._ctypes.set_last_error(0)

    def get_cursor_position(self) -> tuple[int, int] | None:
        point = _POINT()
        self._clear_error()
        if not self._get_cursor_pos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def monitor_from_point(self, x: int, y: int) -> object | None:
        self._clear_error()
        return self._monitor_from_point(
            _POINT(x, y),
            MONITOR_DEFAULT_TO_NEAREST,
        )

    def get_monitor_info(self, monitor: object) -> MonitorInfo | None:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        self._clear_error()
        if not self._get_monitor_info(monitor, ctypes.byref(info)):
            return None
        return MonitorInfo(
            monitor_id=str(info.szDevice),
            left=int(info.rcMonitor.left),
            top=int(info.rcMonitor.top),
            right=int(info.rcMonitor.right),
            bottom=int(info.rcMonitor.bottom),
        )

    def get_monitor_dpi(self, monitor: object) -> tuple[int, int] | None:
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        self._clear_error()
        # MDT_EFFECTIVE_DPI is zero. PerMonitorV2 was enabled before Qt startup.
        result = self._get_dpi_for_monitor(
            monitor,
            0,
            ctypes.byref(dpi_x),
            ctypes.byref(dpi_y),
        )
        if result != 0 or dpi_x.value == 0 or dpi_y.value == 0:
            return None
        return int(dpi_x.value), int(dpi_y.value)

    def dwm_flush(self) -> int:
        self._clear_error()
        return int(self._dwm_flush())

    def get_screen_dc(self) -> object | None:
        self._clear_error()
        return self._get_dc(None)

    def create_compatible_dc(self, source_dc: object) -> object | None:
        self._clear_error()
        return self._create_compatible_dc(source_dc)

    def create_compatible_bitmap(
        self,
        source_dc: object,
        width: int,
        height: int,
    ) -> object | None:
        self._clear_error()
        return self._create_compatible_bitmap(source_dc, width, height)

    def select_object(self, dc: object, gdi_object: object) -> object | None:
        self._clear_error()
        selected = self._select_object(dc, gdi_object)
        if not selected:
            return None
        selected_value = int(selected)
        if selected_value == ctypes.c_void_p(HGDI_ERROR).value:
            return None
        return selected

    def bit_blt(
        self,
        destination_dc: object,
        width: int,
        height: int,
        source_dc: object,
        source_x: int,
        source_y: int,
        raster_operation: int,
    ) -> bool:
        self._clear_error()
        return bool(
            self._bit_blt(
                destination_dc,
                0,
                0,
                width,
                height,
                source_dc,
                source_x,
                source_y,
                raster_operation,
            )
        )

    def get_bgra_bits(
        self,
        dc: object,
        bitmap: object,
        width: int,
        height: int,
    ) -> bytes | None:
        byte_count = width * height * 4
        buffer = (ctypes.c_ubyte * byte_count)()
        bitmap_info = _BITMAPINFO()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bitmap_info.bmiHeader.biWidth = width
        # Negative height requests a top-down DIB matching screen coordinates.
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = BI_RGB

        self._clear_error()
        scan_lines = self._get_dibits(
            dc,
            bitmap,
            0,
            height,
            ctypes.byref(buffer),
            ctypes.byref(bitmap_info),
            DIB_RGB_COLORS,
        )
        if scan_lines != height:
            return None
        return bytes(buffer)

    def delete_object(self, gdi_object: object) -> bool:
        self._clear_error()
        return bool(self._delete_object(gdi_object))

    def delete_dc(self, dc: object) -> bool:
        self._clear_error()
        return bool(self._delete_dc(dc))

    def release_screen_dc(self, dc: object) -> bool:
        self._clear_error()
        return bool(self._release_dc(None, dc))

    def get_last_error(self) -> int:
        return int(self._ctypes.get_last_error())


def capture_monitor_under_cursor(api: CaptureApi | None = None) -> CaptureFrame:
    """Capture the current mouse monitor as tightly packed top-down BGRA bytes.

    GDI resources are always released in reverse ownership order. BitBlt does not
    draw the cursor, so the returned frame intentionally excludes the pointer.
    """

    selected_api: CaptureApi
    try:
        selected_api = api if api is not None else CtypesCaptureApi()
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("CAPTURE-API-INIT") from None

    cursor = _checked_call(
        selected_api,
        "CAPTURE-CURSOR",
        selected_api.get_cursor_position,
    )
    if cursor is None:
        raise _win32_error(selected_api, "CAPTURE-CURSOR")
    try:
        cursor_x, cursor_y = cursor
        cursor_x = int(cursor_x)
        cursor_y = int(cursor_y)
    except (TypeError, ValueError):
        raise CaptureError("CAPTURE-CURSOR-INVALID") from None

    monitor = _checked_call(
        selected_api,
        "CAPTURE-MONITOR",
        lambda: selected_api.monitor_from_point(cursor_x, cursor_y),
    )
    if monitor is None:
        raise _win32_error(selected_api, "CAPTURE-MONITOR")

    info = _checked_call(
        selected_api,
        "CAPTURE-MONITOR-INFO",
        lambda: selected_api.get_monitor_info(monitor),
    )
    if info is None:
        raise _win32_error(selected_api, "CAPTURE-MONITOR-INFO")
    if not isinstance(info, MonitorInfo):
        raise CaptureError("CAPTURE-MONITOR-INFO-INVALID")

    dpi = _checked_call(
        selected_api,
        "CAPTURE-DPI",
        lambda: selected_api.get_monitor_dpi(monitor),
    )
    if dpi is None:
        raise _win32_error(selected_api, "CAPTURE-DPI")
    try:
        dpi_x, dpi_y = dpi
        dpi_x = int(dpi_x)
        dpi_y = int(dpi_y)
    except (TypeError, ValueError):
        raise CaptureError("CAPTURE-DPI-INVALID") from None
    if dpi_x <= 0 or dpi_y <= 0:
        raise CaptureError("CAPTURE-DPI-INVALID")

    flush_result = _checked_call(
        selected_api,
        "CAPTURE-DWM-FLUSH",
        selected_api.dwm_flush,
    )
    if flush_result != 0:
        raise CaptureError("CAPTURE-DWM-FLUSH")

    pixels = _capture_bgra(selected_api, info)
    if _is_all_black_bgra(pixels):
        raise CaptureError(
            "CAPTURE-BLACK-SURFACE",
            "当前表面无法截图；安全桌面或受保护内容不受支持。",
        )

    return CaptureFrame(
        pixels=pixels,
        width=info.width,
        height=info.height,
        monitor_id=info.monitor_id,
        origin_x=info.left,
        origin_y=info.top,
        dpi_x=dpi_x,
        dpi_y=dpi_y,
    )


def _capture_bgra(api: CaptureApi, info: MonitorInfo) -> bytes:
    source_dc: object | None = None
    memory_dc: object | None = None
    bitmap: object | None = None
    previous_object: object | None = None
    bitmap_selected = False
    pixels: bytes | None = None
    pending_error: CaptureError | None = None
    cleanup_failed = False

    try:
        source_dc = _checked_call(api, "CAPTURE-GET-DC", api.get_screen_dc)
        if source_dc is None:
            raise _win32_error(api, "CAPTURE-GET-DC")

        memory_dc = _checked_call(
            api,
            "CAPTURE-CREATE-DC",
            lambda: api.create_compatible_dc(source_dc),
        )
        if memory_dc is None:
            raise _win32_error(api, "CAPTURE-CREATE-DC")

        bitmap = _checked_call(
            api,
            "CAPTURE-CREATE-BITMAP",
            lambda: api.create_compatible_bitmap(
                source_dc,
                info.width,
                info.height,
            ),
        )
        if bitmap is None:
            raise _win32_error(api, "CAPTURE-CREATE-BITMAP")

        previous_object = _checked_call(
            api,
            "CAPTURE-SELECT-BITMAP",
            lambda: api.select_object(memory_dc, bitmap),
        )
        if previous_object is None:
            raise _win32_error(api, "CAPTURE-SELECT-BITMAP")
        bitmap_selected = True

        copied = _checked_call(
            api,
            "CAPTURE-BITBLT",
            lambda: api.bit_blt(
                memory_dc,
                info.width,
                info.height,
                source_dc,
                info.left,
                info.top,
                RASTER_OPERATION,
            ),
        )
        if not copied:
            raise _win32_error(api, "CAPTURE-BITBLT")

        restored = _checked_call(
            api,
            "CAPTURE-RESTORE-BITMAP",
            lambda: api.select_object(memory_dc, previous_object),
        )
        if restored is None:
            raise _win32_error(api, "CAPTURE-RESTORE-BITMAP")
        bitmap_selected = False

        pixels = _checked_call(
            api,
            "CAPTURE-GET-BITS",
            lambda: api.get_bgra_bits(
                memory_dc,
                bitmap,
                info.width,
                info.height,
            ),
        )
        expected_bytes = info.width * info.height * 4
        if pixels is None:
            raise _win32_error(api, "CAPTURE-GET-BITS")
        if not isinstance(pixels, bytes) or len(pixels) != expected_bytes:
            raise CaptureError("CAPTURE-BGRA-INVALID")
    except CaptureError as exc:
        pending_error = exc
    except Exception:
        pending_error = CaptureError("CAPTURE-GDI-API")
    finally:
        if bitmap_selected and memory_dc is not None and previous_object is not None:
            cleanup_failed |= not _cleanup_call(
                lambda: api.select_object(memory_dc, previous_object)
            )
        if bitmap is not None:
            cleanup_failed |= not _cleanup_call(lambda: api.delete_object(bitmap))
        if memory_dc is not None:
            cleanup_failed |= not _cleanup_call(lambda: api.delete_dc(memory_dc))
        if source_dc is not None:
            cleanup_failed |= not _cleanup_call(
                lambda: api.release_screen_dc(source_dc)
            )

    if pending_error is not None:
        raise pending_error
    if cleanup_failed:
        raise CaptureError("CAPTURE-GDI-CLEANUP")
    assert pixels is not None
    return pixels


def _checked_call(
    api: CaptureApi,
    code: str,
    operation: Callable[[], _T],
) -> _T:
    try:
        return operation()
    except CaptureError:
        raise
    except Exception:
        raise CaptureError(f"{code}-API") from None


def _cleanup_call(operation: Callable[[], object]) -> bool:
    try:
        return bool(operation())
    except Exception:
        return False


def _win32_error(api: CaptureApi, code: str) -> CaptureError:
    winerror: int | None = None
    try:
        candidate = int(api.get_last_error())
        if candidate:
            winerror = candidate
    except Exception:
        pass
    return CaptureError(code, winerror=winerror)


def _is_all_black_bgra(pixels: bytes) -> bool:
    view = memoryview(pixels)
    return not any(
        view[index] or view[index + 1] or view[index + 2]
        for index in range(0, len(view), 4)
    )
