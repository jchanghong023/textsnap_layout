"""Deterministic OCR tile generation and coordinate mapping."""

from __future__ import annotations

import math

from .domain import Quad, TileRegion
from .geometry import quad_bounds

DEFAULT_TILE_SIZE = 1216
DEFAULT_TILE_OVERLAP = 128


def _axis_origins(length: int, tile_size: int, overlap: int) -> tuple[int, ...]:
    if length <= 0:
        raise ValueError("axis length must be positive")
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    if overlap < 0 or overlap >= tile_size:
        raise ValueError("overlap must satisfy 0 <= overlap < tile_size")
    if length <= tile_size:
        return (0,)

    step = tile_size - overlap
    origins = [0]
    while origins[-1] + tile_size < length:
        origins.append(origins[-1] + step)
    return tuple(origins)


def generate_tiles(
    image_width: int,
    image_height: int,
    *,
    tile_size: int = DEFAULT_TILE_SIZE,
    overlap: int = DEFAULT_TILE_OVERLAP,
) -> tuple[TileRegion, ...]:
    """Cover an image, pinning final tiles to its right and bottom edges."""

    x_origins = _axis_origins(image_width, tile_size, overlap)
    y_origins = _axis_origins(image_height, tile_size, overlap)
    tiles: list[TileRegion] = []
    for y in y_origins:
        for x in x_origins:
            tiles.append(
                TileRegion(
                    index=len(tiles),
                    x=x,
                    y=y,
                    width=min(tile_size, image_width - x),
                    height=min(tile_size, image_height - y),
                    image_width=image_width,
                    image_height=image_height,
                )
            )
    return tuple(tiles)


def map_quad_to_global(quad: Quad, tile: TileRegion) -> Quad:
    return tuple((float(point[0]) + tile.x, float(point[1]) + tile.y) for point in quad)  # type: ignore[return-value]


def internal_edge_metrics(
    global_quad: Quad,
    tile: TileRegion,
    *,
    tolerance: float = 2.0,
) -> tuple[float, bool]:
    """Return nearest internal tile-edge distance and whether it is touched."""

    if tolerance < 0 or not math.isfinite(tolerance):
        raise ValueError("tolerance must be a finite non-negative number")
    left, top, right, bottom = quad_bounds(global_quad)
    distances: list[float] = []
    if tile.has_internal_left:
        distances.append(abs(left - tile.x))
    if tile.has_internal_top:
        distances.append(abs(top - tile.y))
    if tile.has_internal_right:
        distances.append(abs(tile.right - right))
    if tile.has_internal_bottom:
        distances.append(abs(tile.bottom - bottom))
    if not distances:
        return math.inf, False
    nearest = min(distances)
    return nearest, nearest <= tolerance
