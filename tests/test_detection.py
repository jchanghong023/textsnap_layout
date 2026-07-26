from __future__ import annotations

import unittest

from textsnap.detection import (
    consolidate_candidates,
    deduplicate_candidates,
    merge_seam_fragments,
)
from textsnap.domain import DetectionCandidate, TileRegion


def _quad(left: float, top: float, right: float, bottom: float):
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def _candidate(
    quad,
    *,
    score: float,
    tile: TileRegion,
    distance: float,
    touching: bool,
) -> DetectionCandidate:
    return DetectionCandidate(
        quad=quad,
        detection_score=score,
        source_tile=tile,
        internal_edge_distance=distance,
        touches_internal_edge=touching,
    )


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tile0 = TileRegion(0, 0, 0, 1216, 400, 2500, 400)
        self.tile1 = TileRegion(1, 1088, 0, 1216, 400, 2500, 400)
        self.tile2 = TileRegion(2, 2176, 0, 324, 400, 2500, 400)

    def test_duplicate_prefers_candidate_away_from_internal_edge(self) -> None:
        edge = _candidate(
            _quad(100, 20, 300, 50),
            score=0.99,
            tile=self.tile0,
            distance=0,
            touching=True,
        )
        interior = _candidate(
            _quad(102, 20, 302, 50),
            score=0.55,
            tile=self.tile1,
            distance=30,
            touching=False,
        )
        self.assertEqual(deduplicate_candidates((edge, interior)), (interior,))

    def test_duplicate_then_prefers_distance_then_score(self) -> None:
        near = _candidate(
            _quad(100, 20, 300, 50),
            score=0.99,
            tile=self.tile0,
            distance=10,
            touching=False,
        )
        far = _candidate(
            _quad(100, 20, 300, 50),
            score=0.50,
            tile=self.tile1,
            distance=20,
            touching=False,
        )
        self.assertEqual(deduplicate_candidates((near, far)), (far,))

    def test_transitive_duplicate_group_is_single_candidate(self) -> None:
        first = _candidate(
            _quad(0, 0, 100, 20),
            score=0.6,
            tile=self.tile0,
            distance=5,
            touching=False,
        )
        middle = _candidate(
            _quad(30, 0, 130, 20),
            score=0.7,
            tile=self.tile1,
            distance=6,
            touching=False,
        )
        last = _candidate(
            _quad(60, 0, 160, 20),
            score=0.8,
            tile=self.tile2,
            distance=7,
            touching=False,
        )
        self.assertEqual(deduplicate_candidates((first, middle, last)), (last,))

    def test_seam_fragments_merge_across_overlapping_tiles(self) -> None:
        left = _candidate(
            _quad(1000, 100, 1216, 120),
            score=0.8,
            tile=self.tile0,
            distance=0,
            touching=True,
        )
        right = _candidate(
            _quad(1088, 101, 1350, 121),
            score=0.9,
            tile=self.tile1,
            distance=0,
            touching=True,
        )
        merged = merge_seam_fragments((left, right))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].quad, _quad(1000, 100, 1350, 121))
        self.assertEqual(merged[0].source_tile_indices, (0, 1))

    def _four_tile_seam_fragments(self) -> tuple[DetectionCandidate, ...]:
        tiles = (
            TileRegion(0, 0, 0, 1216, 400, 3840, 400),
            TileRegion(1, 1088, 0, 1216, 400, 3840, 400),
            TileRegion(2, 2176, 0, 1216, 400, 3840, 400),
            TileRegion(3, 3264, 0, 576, 400, 3840, 400),
        )
        quads = (
            _quad(1000, 100, 1216, 120),
            _quad(1088, 100, 2304, 120),
            _quad(2176, 100, 3392, 120),
            _quad(3264, 100, 3500, 120),
        )
        return tuple(
            _candidate(
                quad,
                score=0.8 + index * 0.01,
                tile=tile,
                distance=0,
                touching=True,
            )
            for index, (tile, quad) in enumerate(zip(tiles, quads))
        )

    def _assert_four_tile_seam_chain(
        self, fragments: tuple[DetectionCandidate, ...]
    ) -> None:
        consolidated = consolidate_candidates(fragments)
        self.assertEqual(len(consolidated), 1)
        self.assertEqual(consolidated[0].quad, _quad(1000, 100, 3500, 120))
        self.assertEqual(consolidated[0].source_tile_indices, (0, 1, 2, 3))

    def test_four_tile_seam_chain_merges_in_forward_order(self) -> None:
        self._assert_four_tile_seam_chain(self._four_tile_seam_fragments())

    def test_four_tile_seam_chain_merges_in_reverse_order(self) -> None:
        fragments = self._four_tile_seam_fragments()
        self._assert_four_tile_seam_chain(tuple(reversed(fragments)))

    def test_overlap_threshold_does_not_discard_one_seam_outer_edge(self) -> None:
        left = _candidate(
            _quad(1016, 100, 1216, 120),
            score=0.8,
            tile=self.tile0,
            distance=0,
            touching=True,
        )
        right = _candidate(
            _quad(1088, 100, 1300, 120),
            score=0.9,
            tile=self.tile1,
            distance=0,
            touching=True,
        )

        consolidated = consolidate_candidates((left, right))

        self.assertEqual(len(consolidated), 1)
        self.assertEqual(consolidated[0].quad, _quad(1016, 100, 1300, 120))
        self.assertEqual(consolidated[0].source_tile_indices, (0, 1))

    def test_different_rows_do_not_merge_at_seam(self) -> None:
        first = _candidate(
            _quad(1000, 10, 1216, 30),
            score=0.8,
            tile=self.tile0,
            distance=0,
            touching=True,
        )
        second = _candidate(
            _quad(1088, 40, 1350, 60),
            score=0.8,
            tile=self.tile1,
            distance=0,
            touching=True,
        )
        self.assertEqual(len(merge_seam_fragments((first, second))), 2)

    def test_full_consolidation_keeps_unrelated_candidate(self) -> None:
        duplicate1 = _candidate(
            _quad(20, 20, 100, 40),
            score=0.5,
            tile=self.tile0,
            distance=10,
            touching=False,
        )
        duplicate2 = _candidate(
            _quad(21, 20, 101, 40),
            score=0.6,
            tile=self.tile1,
            distance=20,
            touching=False,
        )
        unrelated = _candidate(
            _quad(500, 80, 600, 100),
            score=0.7,
            tile=self.tile0,
            distance=50,
            touching=False,
        )
        result = consolidate_candidates((duplicate1, duplicate2, unrelated))
        self.assertEqual(len(result), 2)
        self.assertIn(duplicate2, result)
        self.assertIn(unrelated, result)


if __name__ == "__main__":
    unittest.main()
