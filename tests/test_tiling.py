from __future__ import annotations

import unittest

from textsnap.domain import TileRegion
from textsnap.tiling import generate_tiles, internal_edge_metrics, map_quad_to_global


class TilingTests(unittest.TestCase):
    def test_small_image_uses_one_exact_tile(self) -> None:
        self.assertEqual(
            generate_tiles(640, 480),
            (
                TileRegion(
                    index=0,
                    x=0,
                    y=0,
                    width=640,
                    height=480,
                    image_width=640,
                    image_height=480,
                ),
            ),
        )

    def test_large_axis_has_128_pixel_overlap_and_tail_coverage(self) -> None:
        tiles = generate_tiles(2500, 600)
        self.assertEqual([tile.x for tile in tiles], [0, 1088, 2176])
        self.assertEqual([tile.width for tile in tiles], [1216, 1216, 324])
        self.assertEqual(tiles[-1].right, 2500)
        self.assertEqual(tiles[0].right - tiles[1].x, 128)
        self.assertEqual(tiles[1].right - tiles[2].x, 128)

    def test_two_dimensional_tiles_cover_every_boundary(self) -> None:
        tiles = generate_tiles(2305, 2305)
        self.assertEqual(len(tiles), 9)
        self.assertEqual(max(tile.right for tile in tiles), 2305)
        self.assertEqual(max(tile.bottom for tile in tiles), 2305)
        self.assertTrue(any(tile.x == 2176 and tile.y == 2176 for tile in tiles))

    def test_invalid_overlap_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            generate_tiles(100, 100, tile_size=128, overlap=128)

    def test_mapping_and_internal_edge_distance(self) -> None:
        tile = generate_tiles(2000, 400)[1]
        local = ((0.0, 10.0), (50.0, 10.0), (50.0, 30.0), (0.0, 30.0))
        global_quad = map_quad_to_global(local, tile)
        self.assertEqual(global_quad[0], (1088.0, 10.0))
        distance, touching = internal_edge_metrics(global_quad, tile)
        self.assertEqual(distance, 0.0)
        self.assertTrue(touching)


if __name__ == "__main__":
    unittest.main()
