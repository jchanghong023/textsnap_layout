from __future__ import annotations

import unittest

from textsnap.domain import CaptureFrame
from textsnap.windows.capture import (
    CAPTUREBLT,
    RASTER_OPERATION,
    SRCCOPY,
    CaptureError,
    MonitorInfo,
    capture_monitor_under_cursor,
)


class _CaptureApi:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.info = MonitorInfo("DISPLAY-2", -2, -1, 0, 1)
        self.dpi = (144, 144)
        self.pixels = bytes(
            (
                0,
                0,
                1,
                255,
                2,
                0,
                0,
                255,
                0,
                3,
                0,
                255,
                0,
                0,
                4,
                255,
            )
        )
        self.last_error = 0
        self.bit_blt_result = True
        self.delete_object_result = True
        self.delete_dc_result = True
        self.restore_results: list[bool] = []
        self.selected = False
        self.bitmap_deleted = False
        self.monitor_handle: object | None = 0x1234

    def get_cursor_position(self) -> tuple[int, int] | None:
        self.calls.append("cursor")
        return (-1, 0)

    def monitor_from_point(self, x: int, y: int) -> object | None:
        self.calls.append(("monitor", x, y))
        return self.monitor_handle

    def get_monitor_info(self, monitor: object) -> MonitorInfo | None:
        self.calls.append(("monitor-info", monitor))
        return self.info

    def get_monitor_dpi(self, monitor: object) -> tuple[int, int] | None:
        self.calls.append(("dpi", monitor))
        return self.dpi

    def dwm_flush(self) -> int:
        self.calls.append("dwm-flush")
        return 0

    def get_screen_dc(self) -> object | None:
        self.calls.append("get-screen-dc")
        return "screen-dc"

    def create_compatible_dc(self, source_dc: object) -> object | None:
        self.calls.append(("create-dc", source_dc))
        return "memory-dc"

    def create_compatible_bitmap(
        self,
        source_dc: object,
        width: int,
        height: int,
    ) -> object | None:
        self.calls.append(("create-bitmap", source_dc, width, height))
        return "bitmap"

    def select_object(self, dc: object, gdi_object: object) -> object | None:
        self.calls.append(("select", dc, gdi_object))
        if gdi_object == "bitmap":
            self.selected = True
            return "stock-bitmap"
        if self.restore_results and not self.restore_results.pop(0):
            return None
        self.selected = False
        return "bitmap"

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
        self.calls.append(
            (
                "bitblt",
                destination_dc,
                width,
                height,
                source_dc,
                source_x,
                source_y,
                raster_operation,
            )
        )
        return self.bit_blt_result

    def get_bgra_bits(
        self,
        dc: object,
        bitmap: object,
        width: int,
        height: int,
    ) -> bytes | None:
        self.calls.append(("get-bits", dc, bitmap, width, height))
        if self.selected:
            raise AssertionError("bitmap must be restored before GetDIBits")
        return self.pixels

    def delete_object(self, gdi_object: object) -> bool:
        self.calls.append(("delete-object", gdi_object))
        if self.selected or not self.delete_object_result:
            return False
        self.bitmap_deleted = True
        return True

    def delete_dc(self, dc: object) -> bool:
        self.calls.append(("delete-dc", dc))
        if self.delete_dc_result:
            self.selected = False
        return self.delete_dc_result

    def release_screen_dc(self, dc: object) -> bool:
        self.calls.append(("release-screen-dc", dc))
        return True

    def get_last_error(self) -> int:
        return self.last_error


class WindowsCaptureTests(unittest.TestCase):
    def test_captures_mouse_monitor_as_top_down_bgra(self) -> None:
        api = _CaptureApi()

        frame = capture_monitor_under_cursor(api)

        self.assertEqual((frame.width, frame.height), (2, 2))
        self.assertEqual((frame.origin_x, frame.origin_y), (-2, -1))
        self.assertEqual((frame.dpi_x, frame.dpi_y), (144, 144))
        self.assertEqual(frame.monitor_id, "DISPLAY-2")
        self.assertEqual(frame.monitor_handle, 0x1234)
        self.assertEqual(frame.pixels, api.pixels)
        self.assertIn(("monitor-info", 0x1234), api.calls)
        self.assertIn(("dpi", 0x1234), api.calls)
        self.assertIn(
            (
                "bitblt",
                "memory-dc",
                2,
                2,
                "screen-dc",
                -2,
                -1,
                SRCCOPY | CAPTUREBLT,
            ),
            api.calls,
        )
        self.assertEqual(RASTER_OPERATION, SRCCOPY | CAPTUREBLT)
        self.assertLess(api.calls.index("dwm-flush"), api.calls.index("get-screen-dc"))
        self.assertEqual(
            api.calls[-3:],
            [
                ("delete-object", "bitmap"),
                ("delete-dc", "memory-dc"),
                ("release-screen-dc", "screen-dc"),
            ],
        )

    def test_capture_frame_monitor_handle_accepts_only_positive_non_bool_int(
        self,
    ) -> None:
        fields = {
            "pixels": object(),
            "width": 1,
            "height": 1,
            "monitor_id": "DISPLAY-1",
            "origin_x": 0,
            "origin_y": 0,
            "dpi_x": 96,
            "dpi_y": 96,
        }
        self.assertIsNone(CaptureFrame(**fields).monitor_handle)
        self.assertEqual(CaptureFrame(**fields, monitor_handle=1).monitor_handle, 1)
        for invalid in (True, 0, -1, 1.5, "monitor-handle"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    CaptureFrame(**fields, monitor_handle=invalid)

    def test_invalid_monitor_handle_fails_before_monitor_queries(self) -> None:
        for invalid in (True, 0, -1, "monitor-handle"):
            with self.subTest(invalid=invalid):
                api = _CaptureApi()
                api.monitor_handle = invalid

                with self.assertRaises(CaptureError) as raised:
                    capture_monitor_under_cursor(api)

                self.assertEqual(
                    raised.exception.diagnostic_code,
                    "CAPTURE-MONITOR-HANDLE-INVALID",
                )
                self.assertEqual(
                    api.calls,
                    ["cursor", ("monitor", -1, 0)],
                )

    def test_black_surface_ignores_alpha_and_is_rejected_after_cleanup(self) -> None:
        api = _CaptureApi()
        api.pixels = bytes((0, 0, 0, 255)) * 4

        with self.assertRaises(CaptureError) as raised:
            capture_monitor_under_cursor(api)

        self.assertEqual(
            raised.exception.diagnostic_code,
            "CAPTURE-BLACK-SURFACE",
        )
        self.assertIn("不受支持", raised.exception.public_message)
        self.assertEqual(
            api.calls[-1],
            ("release-screen-dc", "screen-dc"),
        )

    def test_bitblt_failure_restores_and_releases_every_owned_resource(self) -> None:
        api = _CaptureApi()
        api.bit_blt_result = False
        api.last_error = 5

        with self.assertRaises(CaptureError) as raised:
            capture_monitor_under_cursor(api)

        self.assertEqual(raised.exception.diagnostic_code, "CAPTURE-BITBLT")
        self.assertEqual(raised.exception.winerror, 5)
        self.assertEqual(
            api.calls[-4:],
            [
                ("select", "memory-dc", "stock-bitmap"),
                ("delete-object", "bitmap"),
                ("delete-dc", "memory-dc"),
                ("release-screen-dc", "screen-dc"),
            ],
        )

    def test_cleanup_failure_is_not_silently_ignored(self) -> None:
        api = _CaptureApi()
        api.delete_object_result = False

        with self.assertRaises(CaptureError) as raised:
            capture_monitor_under_cursor(api)

        self.assertEqual(raised.exception.diagnostic_code, "CAPTURE-GDI-CLEANUP")
        self.assertIn(("delete-dc", "memory-dc"), api.calls)
        self.assertIn(("release-screen-dc", "screen-dc"), api.calls)

    def test_restore_failure_deletes_dc_before_selected_bitmap(self) -> None:
        api = _CaptureApi()
        api.restore_results = [False, False]

        with self.assertRaises(CaptureError) as raised:
            capture_monitor_under_cursor(api)

        self.assertEqual(
            raised.exception.diagnostic_code,
            "CAPTURE-RESTORE-BITMAP",
        )
        self.assertEqual(
            api.calls[-4:],
            [
                ("select", "memory-dc", "stock-bitmap"),
                ("delete-dc", "memory-dc"),
                ("delete-object", "bitmap"),
                ("release-screen-dc", "screen-dc"),
            ],
        )
        self.assertTrue(api.bitmap_deleted)

    def test_operation_and_cleanup_failure_has_fixed_sanitized_diagnostic(
        self,
    ) -> None:
        class CleanupExceptionApi(_CaptureApi):
            def delete_object(self, gdi_object: object) -> bool:
                self.calls.append(("delete-object", gdi_object))
                raise RuntimeError(r"C:\Users\alice\private-capture")

        api = CleanupExceptionApi()
        api.bit_blt_result = False

        with self.assertRaises(CaptureError) as raised:
            capture_monitor_under_cursor(api)

        self.assertEqual(
            raised.exception.diagnostic_code,
            "CAPTURE-GDI-OPERATION-CLEANUP",
        )
        self.assertNotIn("alice", str(raised.exception))
        self.assertNotIn("private-capture", str(raised.exception))
        self.assertIn(("delete-dc", "memory-dc"), api.calls)
        self.assertIn(("release-screen-dc", "screen-dc"), api.calls)

    def test_invalid_pixel_buffer_is_sanitized_and_released(self) -> None:
        api = _CaptureApi()
        api.pixels = b"too short"

        with self.assertRaises(CaptureError) as raised:
            capture_monitor_under_cursor(api)

        self.assertEqual(raised.exception.diagnostic_code, "CAPTURE-BGRA-INVALID")
        self.assertNotIn("too short", str(raised.exception))
        self.assertEqual(
            api.calls[-1],
            ("release-screen-dc", "screen-dc"),
        )

    def test_dwm_failure_stops_before_allocating_gdi_resources(self) -> None:
        api = _CaptureApi()
        api.dwm_flush = lambda: 1

        with self.assertRaises(CaptureError) as raised:
            capture_monitor_under_cursor(api)

        self.assertEqual(raised.exception.diagnostic_code, "CAPTURE-DWM-FLUSH")
        self.assertNotIn("get-screen-dc", api.calls)


if __name__ == "__main__":
    unittest.main()
