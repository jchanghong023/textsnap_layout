"""Frozen-image selection overlay with physical-pixel coordinate mapping."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from math import floor
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QNativeInterface,
    QPaintEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from textsnap.domain import CaptureFrame


_BASE_DPI = 96
_MIN_PHYSICAL_SELECTION = 8


def _screen_geometry_for_monitor_handle(
    monitor_handle: int,
    screens: Iterable[object],
    handle_getter: Callable[[object], int | None],
) -> QRect | None:
    """Return the sole Qt screen geometry matching an opaque HMONITOR."""

    if (
        isinstance(monitor_handle, bool)
        or not isinstance(monitor_handle, int)
        or monitor_handle <= 0
    ):
        raise ValueError("monitor_handle must be a positive integer")

    matched_geometry: QRect | None = None
    match_count = 0
    for screen in screens:
        candidate = handle_getter(screen)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate <= 0
            or candidate != monitor_handle
        ):
            continue
        match_count += 1
        if match_count > 1:
            return None
        geometry_getter = getattr(screen, "geometry", None)
        if not callable(geometry_getter):
            return None
        try:
            geometry = QRect(geometry_getter())
        except (TypeError, RuntimeError):
            return None
        if geometry.width() <= 0 or geometry.height() <= 0:
            return None
        matched_geometry = geometry
    return matched_geometry if match_count == 1 else None


def _qt_native_monitor_handle(screen: object) -> int | None:
    """Read a QScreen's Windows HMONITOR without taking ownership of it."""

    windows_screen_type = getattr(QNativeInterface, "QWindowsScreen", None)
    native_interface_getter = getattr(screen, "nativeInterface", None)
    if windows_screen_type is None or not callable(native_interface_getter):
        return None
    try:
        native_interface = native_interface_getter()
    except (TypeError, RuntimeError):
        return None
    if not isinstance(native_interface, windows_screen_type):
        return None
    try:
        monitor_handle = native_interface.handle()
    except (AttributeError, TypeError, RuntimeError):
        return None
    if (
        isinstance(monitor_handle, bool)
        or not isinstance(monitor_handle, int)
        or monitor_handle <= 0
    ):
        return None
    return monitor_handle


class SelectionOverlay(QWidget):
    """Display a frozen monitor image and emit a physical-pixel selection.

    ``selection_submitted`` uses coordinates local to the supplied image so the
    controller can pass it directly to :meth:`QImage.copy`.
    ``global_selection_submitted`` emits the same rectangle translated by the
    physical monitor origin.
    """

    selection_submitted = Signal(QRect)
    global_selection_submitted = Signal(QRect)
    cancelled = Signal()

    def __init__(
        self,
        image_or_frame: QImage | CaptureFrame,
        *,
        physical_origin: QPoint | tuple[int, int] | None = None,
        dpi_x: int | None = None,
        dpi_y: int | None = None,
        logical_geometry: QRect | None = None,
        screen_geometry: QRect | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        if logical_geometry is not None and screen_geometry is not None:
            raise ValueError("provide only one of logical_geometry and screen_geometry")

        image, frame_origin, frame_dpi = self._unpack_image(image_or_frame)
        if image.isNull():
            raise ValueError("selection image must not be null")

        self._image = image.copy()
        # The captured bitmap is expressed in physical pixels.  A device pixel
        # ratio on the incoming QImage must not make QPainter reinterpret it.
        self._image.setDevicePixelRatio(1.0)
        self._physical_origin = self._coerce_origin(
            physical_origin if physical_origin is not None else frame_origin
        )
        self._dpi_x = self._coerce_dpi(dpi_x if dpi_x is not None else frame_dpi[0])
        self._dpi_y = self._coerce_dpi(dpi_y if dpi_y is not None else frame_dpi[1])
        self._press_position: QPointF | None = None
        self._drag_position: QPointF | None = None
        self._finished = False
        self._last_local_selection: QRect | None = None
        self._last_global_selection: QRect | None = None

        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        geometry = logical_geometry if logical_geometry is not None else screen_geometry
        monitor_handle = getattr(image_or_frame, "monitor_handle", None)
        if geometry is None and monitor_handle is not None:
            geometry = self._matching_qt_screen_geometry(monitor_handle)
            if geometry is None:
                raise RuntimeError("captured monitor is no longer available")
        if geometry is not None:
            if geometry.width() <= 0 or geometry.height() <= 0:
                raise ValueError("overlay geometry must be non-empty")
            self.setGeometry(geometry)
        else:
            logical_width = max(
                1, self._round_pixel(self._image.width() * _BASE_DPI / self._dpi_x)
            )
            logical_height = max(
                1, self._round_pixel(self._image.height() * _BASE_DPI / self._dpi_y)
            )
            logical_x = self._round_signed(
                self._physical_origin.x() * _BASE_DPI / self._dpi_x
            )
            logical_y = self._round_signed(
                self._physical_origin.y() * _BASE_DPI / self._dpi_y
            )
            self.setGeometry(logical_x, logical_y, logical_width, logical_height)

    @staticmethod
    def _matching_qt_screen_geometry(monitor_handle: int) -> QRect | None:
        """Resolve an HMONITOR to Qt's mixed-DPI virtual geometry."""

        application = QGuiApplication.instance()
        if application is None:
            return None
        return _screen_geometry_for_monitor_handle(
            monitor_handle,
            application.screens(),
            _qt_native_monitor_handle,
        )

    @staticmethod
    def _unpack_image(
        image_or_frame: QImage | CaptureFrame,
    ) -> tuple[QImage, tuple[int, int], tuple[int, int]]:
        if isinstance(image_or_frame, QImage):
            return image_or_frame, (0, 0), (_BASE_DPI, _BASE_DPI)

        # Avoid importing the dependency-free domain module whenever the UI is
        # not used.  Structural checks also keep test doubles straightforward.
        required = (
            "pixels",
            "width",
            "height",
            "origin_x",
            "origin_y",
            "dpi_x",
            "dpi_y",
        )
        if not all(hasattr(image_or_frame, name) for name in required):
            raise TypeError("expected a QImage or CaptureFrame")
        image = image_or_frame.pixels
        if isinstance(image, QImage):
            if (
                image.width() != image_or_frame.width
                or image.height() != image_or_frame.height
            ):
                raise ValueError("CaptureFrame dimensions do not match its QImage")
        else:
            try:
                pixel_view = memoryview(image)
            except TypeError:
                raise TypeError(
                    "CaptureFrame.pixels must be a QImage or packed BGRA buffer"
                ) from None
            expected_bytes = image_or_frame.width * image_or_frame.height * 4
            if not pixel_view.c_contiguous or pixel_view.nbytes != expected_bytes:
                raise ValueError("CaptureFrame BGRA buffer has invalid storage")
            # GDI BI_RGB uses little-endian B,G,R,reserved byte order. RGB32
            # ignores that reserved byte instead of treating its usual zero as
            # transparency. __init__ immediately takes a deep QImage copy.
            image = QImage(
                pixel_view,
                image_or_frame.width,
                image_or_frame.height,
                image_or_frame.width * 4,
                QImage.Format.Format_RGB32,
            )
            if image.isNull():
                raise ValueError("CaptureFrame BGRA buffer could not form a QImage")
        return (
            image,
            (int(image_or_frame.origin_x), int(image_or_frame.origin_y)),
            (int(image_or_frame.dpi_x), int(image_or_frame.dpi_y)),
        )

    @staticmethod
    def _coerce_origin(value: QPoint | tuple[int, int]) -> QPoint:
        if isinstance(value, QPoint):
            return QPoint(value)
        if len(value) != 2:
            raise ValueError("physical_origin must contain x and y")
        return QPoint(int(value[0]), int(value[1]))

    @staticmethod
    def _coerce_dpi(value: int) -> int:
        result = int(value)
        if result <= 0:
            raise ValueError("DPI must be positive")
        return result

    @staticmethod
    def _round_pixel(value: float) -> int:
        return floor(value + 0.5)

    @staticmethod
    def _round_signed(value: float) -> int:
        if value >= 0:
            return floor(value + 0.5)
        return -floor(-value + 0.5)

    @property
    def physical_origin(self) -> QPoint:
        return QPoint(self._physical_origin)

    @property
    def last_local_selection(self) -> QRect | None:
        return (
            None
            if self._last_local_selection is None
            else QRect(self._last_local_selection)
        )

    @property
    def last_global_selection(self) -> QRect | None:
        return (
            None
            if self._last_global_selection is None
            else QRect(self._last_global_selection)
        )

    @property
    def has_frozen_image(self) -> bool:
        return not self._image.isNull()

    def frozen_image_copy(self) -> QImage:
        """Return a detached copy for diagnostics or controller-side cropping."""

        return self._image.copy()

    def map_widget_point_to_physical(self, point: QPoint | QPointF) -> QPoint:
        """Map a widget-local point to a clamped image pixel boundary."""

        if self.width() <= 0 or self.height() <= 0 or self._image.isNull():
            raise RuntimeError("overlay has no mappable image")
        logical_x = min(max(float(point.x()), 0.0), float(self.width()))
        logical_y = min(max(float(point.y()), 0.0), float(self.height()))
        # Mouse events inside a widget top out at width-1/height-1. Treat that
        # final logical pixel as the far image boundary so a corner-to-corner
        # drag can include the final physical row and column.
        physical_x = (
            self._image.width()
            if logical_x >= self.width() - 1
            else self._round_pixel(logical_x * self._image.width() / self.width())
        )
        physical_y = (
            self._image.height()
            if logical_y >= self.height() - 1
            else self._round_pixel(logical_y * self._image.height() / self.height())
        )
        return QPoint(
            min(max(physical_x, 0), self._image.width()),
            min(max(physical_y, 0), self._image.height()),
        )

    def map_widget_rect_to_physical(self, rect: QRect | QRectF) -> QRect:
        """Map a logical rectangle to an image-local physical pixel rectangle."""

        rectf = QRectF(rect).normalized()
        first = self.map_widget_point_to_physical(rectf.topLeft())
        second = self.map_widget_point_to_physical(rectf.bottomRight())
        left, right = sorted((first.x(), second.x()))
        top, bottom = sorted((first.y(), second.y()))
        return QRect(left, top, right - left, bottom - top)

    def _selection_from_drag(self) -> QRect:
        assert self._press_position is not None
        assert self._drag_position is not None
        first = self.map_widget_point_to_physical(self._press_position)
        second = self.map_widget_point_to_physical(self._drag_position)
        left, right = sorted((first.x(), second.x()))
        top, bottom = sorted((first.y(), second.y()))
        return QRect(left, top, right - left, bottom - top)

    def _widget_drag_rect(self) -> QRectF | None:
        if self._press_position is None or self._drag_position is None:
            return None
        return QRectF(self._press_position, self._drag_position).normalized()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        if self._image.isNull():
            painter.fillRect(self.rect(), Qt.GlobalColor.black)
            return

        target = QRectF(self.rect())
        source = QRectF(0, 0, self._image.width(), self._image.height())
        painter.drawImage(target, self._image, source)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 105))

        drag_rect = self._widget_drag_rect()
        if drag_rect is None or drag_rect.isEmpty():
            return
        physical_rect = self.map_widget_rect_to_physical(drag_rect)
        painter.drawImage(drag_rect, self._image, QRectF(physical_rect))
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawRect(drag_rect)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.RightButton:
            event.accept()
            self._cancel()
            return
        if event.button() is Qt.MouseButton.LeftButton and not self._finished:
            self._press_position = event.position()
            self._drag_position = event.position()
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._press_position is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and not self._finished
        ):
            self._drag_position = event.position()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            event.button() is Qt.MouseButton.LeftButton
            and self._press_position is not None
            and not self._finished
        ):
            self._drag_position = event.position()
            local_rect = self._selection_from_drag()
            event.accept()
            if (
                local_rect.width() < _MIN_PHYSICAL_SELECTION
                or local_rect.height() < _MIN_PHYSICAL_SELECTION
            ):
                self._cancel()
            else:
                self._submit(local_rect)
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self._cancel()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().showEvent(event)
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._finished:
            self._finished = True
            self.cancelled.emit()
        self._release_image()
        event.accept()

    def _submit(self, local_rect: QRect) -> None:
        if self._finished:
            return
        self._finished = True
        global_rect = QRect(local_rect)
        global_rect.translate(self._physical_origin)
        self._last_local_selection = QRect(local_rect)
        self._last_global_selection = QRect(global_rect)
        # Direct Qt connections run synchronously, allowing the controller to
        # crop its own CaptureFrame before this overlay releases its image.
        self.selection_submitted.emit(QRect(local_rect))
        self.global_selection_submitted.emit(QRect(global_rect))
        self._release_image()
        self.close()

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.cancelled.emit()
        self._release_image()
        self.close()

    def _release_image(self) -> None:
        self._image = QImage()
        self._press_position = None
        self._drag_position = None
