"""Pure layout-preserving text reconstruction, independent of Qt and Paddle."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from statistics import median
import unicodedata

from .domain import LayoutResult, LayoutStats, RecognizedSpan
from .geometry import quad_baseline, quad_bounds, quad_dimensions

LINE_VERTICAL_OVERLAP = 0.45
BASELINE_HEIGHT_FACTOR = 0.5


def _round_nonnegative_half_up(value: float) -> int:
    if value < 0:
        raise ValueError("layout rounding input must be non-negative")
    return floor(value + 0.5)


def sanitize_ocr_text(text: str) -> str:
    """Replace control characters with spaces without rewriting normal Unicode."""

    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)


def display_cell_width(text: str) -> int:
    """Count half-width grid cells (ASCII/Latin 1, CJK/full-width 2)."""

    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _trimmed_median(values: list[float]) -> float:
    positive = sorted(value for value in values if value > 0)
    if not positive:
        return 0.0
    if len(positive) >= 5:
        trim = max(1, int(len(positive) * 0.1))
        if trim * 2 < len(positive):
            positive = positive[trim:-trim]
    return float(median(positive))


@dataclass(slots=True)
class _LayoutItem:
    span: RecognizedSpan
    text: str
    left: float
    top: float
    right: float
    bottom: float
    baseline: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass(slots=True)
class _TextLine:
    items: list[_LayoutItem]

    @property
    def top(self) -> float:
        return float(median(item.top for item in self.items))

    @property
    def bottom(self) -> float:
        return float(median(item.bottom for item in self.items))

    @property
    def baseline(self) -> float:
        return float(median(item.baseline for item in self.items))

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) * 0.5


def _line_overlap(item: _LayoutItem, line: _TextLine) -> float:
    overlap = max(0.0, min(item.bottom, line.bottom) - max(item.top, line.top))
    smaller_height = min(item.height, line.bottom - line.top)
    if smaller_height <= 0:
        return 0.0
    return overlap / smaller_height


def _cluster_lines(items: list[_LayoutItem], body_height: float) -> list[_TextLine]:
    lines: list[_TextLine] = []
    for item in sorted(
        items, key=lambda value: (value.top, value.baseline, value.left)
    ):
        matches: list[tuple[float, float, _TextLine]] = []
        for line in lines:
            overlap = _line_overlap(item, line)
            baseline_difference = abs(item.baseline - line.baseline)
            if (
                overlap >= LINE_VERTICAL_OVERLAP
                or baseline_difference <= body_height * BASELINE_HEIGHT_FACTOR
            ):
                matches.append((overlap, -baseline_difference, line))
        if matches:
            matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
            matches[0][2].items.append(item)
        else:
            lines.append(_TextLine(items=[item]))
    lines.sort(key=lambda line: (line.center_y, min(item.left for item in line.items)))
    for line in lines:
        line.items.sort(key=lambda item: (item.left, item.right))
    return lines


def _estimate_grid_width(items: list[_LayoutItem]) -> float:
    estimates: list[float] = []
    for item in items:
        cell_count = display_cell_width(item.text)
        width = item.right - item.left
        if cell_count > 0 and width > 0:
            estimates.append(width / cell_count)
    return _trimmed_median(estimates) or 1.0


def _estimate_row_step(lines: list[_TextLine], body_height: float) -> float:
    if len(lines) < 2:
        return body_height * 1.4 if body_height > 0 else 1.0
    differences = [
        current.baseline - previous.baseline
        for previous, current in zip(lines, lines[1:])
        if current.baseline > previous.baseline
    ]
    typical = [
        difference
        for difference in differences
        if body_height <= 0 or difference <= body_height * 3.0
    ]
    return _trimmed_median(typical) or (body_height * 1.4 if body_height > 0 else 1.0)


def _render_line(line: _TextLine, left_origin: float, grid_width: float) -> str:
    pieces: list[str] = []
    cursor = 0
    for item in line.items:
        target_column = _round_nonnegative_half_up(
            max(0.0, (item.left - left_origin) / grid_width)
        )
        target_column = max(target_column, cursor)
        if target_column > cursor:
            pieces.append(" " * (target_column - cursor))
            cursor = target_column
        pieces.append(item.text)
        cursor += display_cell_width(item.text)
    return "".join(pieces).rstrip(" ")


def build_layout(
    spans: list[RecognizedSpan] | tuple[RecognizedSpan, ...],
) -> LayoutResult:
    """Build the single layout-faithful plain-text representation."""

    items: list[_LayoutItem] = []
    for span in spans:
        if span.text == "":
            continue
        text = sanitize_ocr_text(span.text)
        left, top, right, bottom = quad_bounds(span.quad)
        if right <= left or bottom <= top:
            continue
        items.append(
            _LayoutItem(
                span=span,
                text=text,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                baseline=quad_baseline(span.quad),
            )
        )

    if not items:
        return LayoutResult(
            text="",
            stats=LayoutStats(
                input_spans=len(spans),
                output_spans=0,
                line_count=0,
                grid_cell_width=0.0,
                row_step=0.0,
            ),
        )

    heights = [quad_dimensions(item.span.quad)[1] for item in items]
    body_height = _trimmed_median(heights) or 1.0
    lines = _cluster_lines(items, body_height)
    grid_width = _estimate_grid_width(items)
    row_step = _estimate_row_step(lines, body_height)
    left_origin = min(item.left for item in items)

    output_rows: list[str] = []
    previous_row = -1
    first_baseline = lines[0].baseline
    for line in lines:
        estimated_row = _round_nonnegative_half_up(
            max(0.0, (line.baseline - first_baseline) / row_step)
        )
        row = max(previous_row + 1, estimated_row)
        output_rows.extend("" for _ in range(row - previous_row - 1))
        output_rows.append(_render_line(line, left_origin, grid_width))
        previous_row = row

    return LayoutResult(
        text="\n".join(output_rows),
        stats=LayoutStats(
            input_spans=len(spans),
            output_spans=len(items),
            line_count=len(lines),
            grid_cell_width=grid_width,
            row_step=row_step,
        ),
    )
