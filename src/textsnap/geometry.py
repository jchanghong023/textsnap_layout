"""Small dependency-free geometry helpers for OCR quadrilaterals."""

from __future__ import annotations

import math
from statistics import median
from typing import Iterable, Sequence

from .domain import Point, Quad

_EPSILON = 1e-9


def ordered_polygon(points: Sequence[Point]) -> tuple[Point, ...]:
    """Return points in counter-clockwise order around their centroid."""

    if len(points) < 3:
        return tuple(points)
    center_x = sum(point[0] for point in points) / len(points)
    center_y = sum(point[1] for point in points) / len(points)
    return tuple(
        sorted(
            ((float(x), float(y)) for x, y in points),
            key=lambda point: math.atan2(point[1] - center_y, point[0] - center_x),
        )
    )


def polygon_area(points: Sequence[Point]) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        total += point[0] * next_point[1] - next_point[0] * point[1]
    return abs(total) * 0.5


def quad_area(quad: Quad) -> float:
    return polygon_area(ordered_polygon(quad))


def quad_bounds(quad: Quad) -> tuple[float, float, float, float]:
    xs = [point[0] for point in quad]
    ys = [point[1] for point in quad]
    return min(xs), min(ys), max(xs), max(ys)


def quad_dimensions(quad: Quad) -> tuple[float, float]:
    left, top, right, bottom = quad_bounds(quad)
    return max(0.0, right - left), max(0.0, bottom - top)


def quad_center(quad: Quad) -> Point:
    return (
        sum(point[0] for point in quad) / 4.0,
        sum(point[1] for point in quad) / 4.0,
    )


def quad_baseline(quad: Quad) -> float:
    """Approximate a baseline using the median of the two lowest vertices."""

    ys = sorted((point[1] for point in quad), reverse=True)
    return median(ys[:2])


def vertical_overlap_ratio(first: Quad, second: Quad) -> float:
    _, first_top, _, first_bottom = quad_bounds(first)
    _, second_top, _, second_bottom = quad_bounds(second)
    overlap = max(0.0, min(first_bottom, second_bottom) - max(first_top, second_top))
    smaller_height = min(first_bottom - first_top, second_bottom - second_top)
    if smaller_height <= _EPSILON:
        return 0.0
    return overlap / smaller_height


def horizontal_overlap_ratio(first: Quad, second: Quad) -> float:
    first_left, _, first_right, _ = quad_bounds(first)
    second_left, _, second_right, _ = quad_bounds(second)
    overlap = max(0.0, min(first_right, second_right) - max(first_left, second_left))
    smaller_width = min(first_right - first_left, second_right - second_left)
    if smaller_width <= _EPSILON:
        return 0.0
    return overlap / smaller_width


def _cross(first: Point, second: Point, third: Point) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
        third[0] - first[0]
    )


def _line_intersection(
    segment_start: Point,
    segment_end: Point,
    clip_start: Point,
    clip_end: Point,
) -> Point:
    segment_dx = segment_end[0] - segment_start[0]
    segment_dy = segment_end[1] - segment_start[1]
    clip_dx = clip_end[0] - clip_start[0]
    clip_dy = clip_end[1] - clip_start[1]
    denominator = segment_dx * clip_dy - segment_dy * clip_dx
    if abs(denominator) <= _EPSILON:
        return segment_end
    delta_x = clip_start[0] - segment_start[0]
    delta_y = clip_start[1] - segment_start[1]
    factor = (delta_x * clip_dy - delta_y * clip_dx) / denominator
    return (
        segment_start[0] + factor * segment_dx,
        segment_start[1] + factor * segment_dy,
    )


def convex_intersection(
    subject_points: Sequence[Point],
    clip_points: Sequence[Point],
) -> tuple[Point, ...]:
    """Clip one convex polygon by another using Sutherland-Hodgman."""

    output = list(ordered_polygon(subject_points))
    clip = ordered_polygon(clip_points)
    if len(output) < 3 or len(clip) < 3:
        return ()

    for index, clip_start in enumerate(clip):
        clip_end = clip[(index + 1) % len(clip)]
        input_points = output
        output = []
        if not input_points:
            break
        previous = input_points[-1]
        previous_inside = _cross(clip_start, clip_end, previous) >= -_EPSILON
        for current in input_points:
            current_inside = _cross(clip_start, clip_end, current) >= -_EPSILON
            if current_inside:
                if not previous_inside:
                    output.append(
                        _line_intersection(previous, current, clip_start, clip_end)
                    )
                output.append(current)
            elif previous_inside:
                output.append(
                    _line_intersection(previous, current, clip_start, clip_end)
                )
            previous = current
            previous_inside = current_inside
    return tuple(output)


def intersection_area(first: Quad, second: Quad) -> float:
    return polygon_area(convex_intersection(first, second))


def overlap_metrics(first: Quad, second: Quad) -> tuple[float, float]:
    """Return (IoU, intersection/smaller-area), handling degenerate boxes."""

    first_area = quad_area(first)
    second_area = quad_area(second)
    if first_area <= _EPSILON or second_area <= _EPSILON:
        return 0.0, 0.0
    intersection = intersection_area(first, second)
    union = first_area + second_area - intersection
    iou = intersection / union if union > _EPSILON else 0.0
    smaller_ratio = intersection / min(first_area, second_area)
    return iou, smaller_ratio


def bounding_quad(quads: Iterable[Quad]) -> Quad:
    bounds = [quad_bounds(quad) for quad in quads]
    if not bounds:
        raise ValueError("at least one quad is required")
    left = min(bound[0] for bound in bounds)
    top = min(bound[1] for bound in bounds)
    right = max(bound[2] for bound in bounds)
    bottom = max(bound[3] for bound in bounds)
    return ((left, top), (right, top), (right, bottom), (left, bottom))
