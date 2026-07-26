"""Tile detection de-duplication and seam-fragment consolidation."""

from __future__ import annotations

from collections.abc import Iterable

from .domain import DetectionCandidate
from .geometry import (
    bounding_quad,
    overlap_metrics,
    quad_bounds,
    quad_dimensions,
    vertical_overlap_ratio,
)

DEFAULT_IOU_THRESHOLD = 0.4
DEFAULT_SMALLER_INTERSECTION_THRESHOLD = 0.6
DEFAULT_VERTICAL_OVERLAP_THRESHOLD = 0.6
DEFAULT_HEIGHT_SIMILARITY = 0.65


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self._parents = list(range(size))

    def find(self, value: int) -> int:
        parent = self._parents[value]
        if parent != value:
            self._parents[value] = self.find(parent)
        return self._parents[value]

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self._parents[second_root] = first_root


def _candidate_rank(candidate: DetectionCandidate) -> tuple[bool, float, float]:
    return (
        not candidate.touches_internal_edge,
        candidate.internal_edge_distance,
        candidate.detection_score,
    )


def deduplicate_candidates(
    candidates: Iterable[DetectionCandidate],
    *,
    iou_threshold: float = DEFAULT_IOU_THRESHOLD,
    smaller_intersection_threshold: float = DEFAULT_SMALLER_INTERSECTION_THRESHOLD,
) -> tuple[DetectionCandidate, ...]:
    """Group transitive overlaps and retain the plan-defined best candidate."""

    items = tuple(candidates)
    if not 0 <= iou_threshold <= 1:
        raise ValueError("iou_threshold must be between 0 and 1")
    if not 0 <= smaller_intersection_threshold <= 1:
        raise ValueError("smaller_intersection_threshold must be between 0 and 1")
    groups = _DisjointSet(len(items))
    for first_index, first in enumerate(items):
        for second_index in range(first_index + 1, len(items)):
            second = items[second_index]
            iou, smaller_ratio = overlap_metrics(first.quad, second.quad)
            if iou >= iou_threshold or smaller_ratio >= smaller_intersection_threshold:
                groups.union(first_index, second_index)

    components: dict[int, list[DetectionCandidate]] = {}
    for index, candidate in enumerate(items):
        components.setdefault(groups.find(index), []).append(candidate)

    selected = [
        max(component, key=_candidate_rank) for component in components.values()
    ]
    return tuple(
        sorted(
            selected,
            key=lambda candidate: (
                quad_bounds(candidate.quad)[1],
                quad_bounds(candidate.quad)[0],
                candidate.source_tile.index,
            ),
        )
    )


def _touches_right_edge(candidate: DetectionCandidate, tolerance: float) -> bool:
    right = quad_bounds(candidate.quad)[2]
    tile = candidate.source_tile
    return tile.has_internal_right and abs(tile.right - right) <= tolerance


def _touches_left_edge(candidate: DetectionCandidate, tolerance: float) -> bool:
    left = quad_bounds(candidate.quad)[0]
    tile = candidate.source_tile
    return tile.has_internal_left and abs(left - tile.x) <= tolerance


def _tiles_are_horizontal_neighbors(
    first: DetectionCandidate, second: DetectionCandidate
) -> bool:
    first_tile = first.source_tile
    second_tile = second.source_tile
    tile_vertical_overlap = max(
        0,
        min(first_tile.bottom, second_tile.bottom) - max(first_tile.y, second_tile.y),
    )
    if tile_vertical_overlap <= 0 or first_tile.x == second_tile.x:
        return False
    return max(first_tile.x, second_tile.x) < min(first_tile.right, second_tile.right)


def _is_seam_pair(
    first: DetectionCandidate,
    second: DetectionCandidate,
    *,
    vertical_overlap_threshold: float,
    height_similarity: float,
    edge_tolerance: float,
) -> bool:
    if not _tiles_are_horizontal_neighbors(first, second):
        return False
    left_tile_candidate, right_tile_candidate = sorted(
        (first, second), key=lambda candidate: candidate.source_tile.x
    )
    if not _touches_right_edge(left_tile_candidate, edge_tolerance):
        return False
    if not _touches_left_edge(right_tile_candidate, edge_tolerance):
        return False
    if vertical_overlap_ratio(first.quad, second.quad) < vertical_overlap_threshold:
        return False

    _, first_height = quad_dimensions(first.quad)
    _, second_height = quad_dimensions(second.quad)
    larger_height = max(first_height, second_height)
    if (
        larger_height <= 0
        or min(first_height, second_height) / larger_height < height_similarity
    ):
        return False

    left_bounds = quad_bounds(left_tile_candidate.quad)
    right_bounds = quad_bounds(right_tile_candidate.quad)
    tile_overlap = max(
        0,
        left_tile_candidate.source_tile.right - right_tile_candidate.source_tile.x,
    )
    horizontal_gap = right_bounds[0] - left_bounds[2]
    return horizontal_gap <= tile_overlap + larger_height * 2.0


def _merge_component(
    candidates: list[DetectionCandidate],
) -> DetectionCandidate:
    source = max(
        (candidate.source_tile for candidate in candidates),
        key=lambda tile: (tile.x, tile.y, tile.index),
    )
    return DetectionCandidate(
        quad=bounding_quad(candidate.quad for candidate in candidates),
        detection_score=max(candidate.detection_score for candidate in candidates),
        source_tile=source,
        internal_edge_distance=max(
            candidate.internal_edge_distance for candidate in candidates
        ),
        touches_internal_edge=False,
        source_tile_indices=tuple(
            index
            for candidate in candidates
            for index in candidate.source_tile_indices
        ),
    )


def merge_seam_fragments(
    candidates: Iterable[DetectionCandidate],
    *,
    vertical_overlap_threshold: float = DEFAULT_VERTICAL_OVERLAP_THRESHOLD,
    height_similarity: float = DEFAULT_HEIGHT_SIMILARITY,
    edge_tolerance: float = 4.0,
) -> tuple[DetectionCandidate, ...]:
    """Merge horizontal text fragments cut at overlapping vertical tile seams."""

    if not 0 <= vertical_overlap_threshold <= 1:
        raise ValueError("vertical_overlap_threshold must be between 0 and 1")
    if not 0 <= height_similarity <= 1:
        raise ValueError("height_similarity must be between 0 and 1")
    if edge_tolerance < 0:
        raise ValueError("edge_tolerance must be non-negative")

    items = tuple(candidates)
    groups = _DisjointSet(len(items))
    for first_index, first in enumerate(items):
        for second_index in range(first_index + 1, len(items)):
            second = items[second_index]
            if _is_seam_pair(
                first,
                second,
                vertical_overlap_threshold=vertical_overlap_threshold,
                height_similarity=height_similarity,
                edge_tolerance=edge_tolerance,
            ):
                groups.union(first_index, second_index)

    components: dict[int, list[DetectionCandidate]] = {}
    for index, candidate in enumerate(items):
        components.setdefault(groups.find(index), []).append(candidate)
    merged = [
        component[0] if len(component) == 1 else _merge_component(component)
        for component in components.values()
    ]
    return tuple(
        sorted(
            merged,
            key=lambda candidate: (
                quad_bounds(candidate.quad)[1],
                quad_bounds(candidate.quad)[0],
            ),
        )
    )


def consolidate_candidates(
    candidates: Iterable[DetectionCandidate],
) -> tuple[DetectionCandidate, ...]:
    """Merge seam fragments before overlap de-duplication.

    A long line detected on both sides of an overlapping tile seam can itself
    satisfy the duplicate thresholds. Merging first preserves both outer
    edges; de-duplication can then remove any remaining full-box duplicates.
    """

    return deduplicate_candidates(merge_seam_fragments(candidates))
