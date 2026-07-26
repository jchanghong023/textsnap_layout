"""Stable, dependency-free data boundaries used across application layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Final, TypeAlias

Point: TypeAlias = tuple[float, float]
Quad: TypeAlias = tuple[Point, Point, Point, Point]

_RIGHT_ANGLE_ROTATIONS: Final = frozenset({0, 90, 180, 270})


def _require_finite(value: float, field_name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")


def _validate_quad(quad: Quad) -> None:
    if len(quad) != 4:
        raise ValueError("quad must contain exactly four points")
    for point in quad:
        if len(point) != 2:
            raise ValueError("each quad point must contain x and y")
        _require_finite(float(point[0]), "quad x")
        _require_finite(float(point[1]), "quad y")


@dataclass(frozen=True, slots=True)
class TileRegion:
    """A physical-pixel tile within a captured selection."""

    index: int
    x: int
    y: int
    width: int
    height: int
    image_width: int
    image_height: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("tile index must be non-negative")
        if self.x < 0 or self.y < 0:
            raise ValueError("tile origin must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("tile dimensions must be positive")
        if self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("image dimensions must be positive")
        if self.x + self.width > self.image_width:
            raise ValueError("tile exceeds image width")
        if self.y + self.height > self.image_height:
            raise ValueError("tile exceeds image height")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def has_internal_left(self) -> bool:
        return self.x > 0

    @property
    def has_internal_top(self) -> bool:
        return self.y > 0

    @property
    def has_internal_right(self) -> bool:
        return self.right < self.image_width

    @property
    def has_internal_bottom(self) -> bool:
        return self.bottom < self.image_height


@dataclass(frozen=True, slots=True)
class CaptureFrame:
    """An in-memory monitor capture in Windows physical-pixel coordinates.

    ``monitor_handle`` is an opaque, process-local HMONITOR value. It is only
    used synchronously to associate the capture with Qt's native screen and
    must not be persisted or closed by the application.
    """

    pixels: object = field(repr=False, compare=False)
    width: int
    height: int
    monitor_id: str
    origin_x: int
    origin_y: int
    dpi_x: int
    dpi_y: int
    monitor_handle: int | None = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("capture dimensions must be positive")
        if not self.monitor_id:
            raise ValueError("monitor_id must not be empty")
        if self.dpi_x <= 0 or self.dpi_y <= 0:
            raise ValueError("DPI must be positive")
        if self.monitor_handle is not None and (
            isinstance(self.monitor_handle, bool)
            or not isinstance(self.monitor_handle, int)
            or self.monitor_handle <= 0
        ):
            raise ValueError("monitor_handle must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class DetectionCandidate:
    """A detection mapped from one tile into selection-global coordinates."""

    quad: Quad
    detection_score: float
    source_tile: TileRegion
    internal_edge_distance: float
    touches_internal_edge: bool
    source_tile_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _validate_quad(self.quad)
        _require_finite(self.detection_score, "detection_score")
        if not 0.0 <= self.detection_score <= 1.0:
            raise ValueError("detection_score must be between 0 and 1")
        if math.isnan(self.internal_edge_distance):
            raise ValueError("internal_edge_distance must not be NaN")
        if self.internal_edge_distance < 0:
            raise ValueError("internal_edge_distance must be non-negative")
        indices = self.source_tile_indices or (self.source_tile.index,)
        if any(index < 0 for index in indices):
            raise ValueError("source tile indices must be non-negative")
        object.__setattr__(self, "source_tile_indices", tuple(sorted(set(indices))))


@dataclass(frozen=True, slots=True)
class RecognizedSpan:
    """One recognized text span with its final selection-global geometry."""

    quad: Quad
    text: str
    detection_score: float
    recognition_score: float
    rotation_degrees: int

    def __post_init__(self) -> None:
        _validate_quad(self.quad)
        if not isinstance(self.text, str):
            raise TypeError("text must be str")
        for field_name, value in (
            ("detection_score", self.detection_score),
            ("recognition_score", self.recognition_score),
        ):
            _require_finite(value, field_name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.rotation_degrees not in _RIGHT_ANGLE_ROTATIONS:
            raise ValueError("rotation_degrees must be 0, 90, 180, or 270")


@dataclass(frozen=True, slots=True)
class LayoutStats:
    """Non-content measurements useful for an in-memory diagnostic view."""

    input_spans: int
    output_spans: int
    line_count: int
    grid_cell_width: float
    row_step: float

    def __post_init__(self) -> None:
        if min(self.input_spans, self.output_spans, self.line_count) < 0:
            raise ValueError("layout counts must be non-negative")
        if self.grid_cell_width < 0 or self.row_step < 0:
            raise ValueError("layout measurements must be non-negative")


@dataclass(frozen=True, slots=True)
class LayoutResult:
    """The sole layout-preserving plain-text output and non-content statistics."""

    text: str
    stats: LayoutStats


@dataclass(frozen=True, slots=True)
class Success:
    result: LayoutResult


@dataclass(frozen=True, slots=True)
class Empty:
    reason: str = "no-text"


@dataclass(frozen=True, slots=True)
class Cancelled:
    reason: str = "user-cancelled"


@dataclass(frozen=True, slots=True)
class Failure:
    """A sanitized failure that must never contain pixels, OCR text, or paths."""

    error_type: str
    public_message: str
    diagnostic_code: str

    def __post_init__(self) -> None:
        if not self.error_type or not self.public_message or not self.diagnostic_code:
            raise ValueError("failure fields must not be empty")
        for value in (self.error_type, self.public_message, self.diagnostic_code):
            if "\n" in value or "\r" in value:
                raise ValueError("failure fields must be single-line and sanitized")


TaskOutcome: TypeAlias = Success | Empty | Cancelled | Failure


class ModelState(str, Enum):
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


class TaskState(str, Enum):
    IDLE = "idle"
    CAPTURING = "capturing"
    RECOGNIZING = "recognizing"
