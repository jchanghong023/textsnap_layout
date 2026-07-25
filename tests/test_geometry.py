from __future__ import annotations

import unittest

from textsnap.geometry import (
    bounding_quad,
    overlap_metrics,
    quad_area,
    vertical_overlap_ratio,
)


class GeometryTests(unittest.TestCase):
    def test_rotated_quad_area_and_self_overlap(self) -> None:
        quad = ((0.0, 1.0), (1.0, 0.0), (2.0, 1.0), (1.0, 2.0))
        self.assertAlmostEqual(quad_area(quad), 2.0)
        iou, smaller = overlap_metrics(quad, quad)
        self.assertAlmostEqual(iou, 1.0)
        self.assertAlmostEqual(smaller, 1.0)

    def test_partial_axis_aligned_overlap(self) -> None:
        first = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        second = ((5.0, 0.0), (15.0, 0.0), (15.0, 10.0), (5.0, 10.0))
        iou, smaller = overlap_metrics(first, second)
        self.assertAlmostEqual(iou, 1.0 / 3.0)
        self.assertAlmostEqual(smaller, 0.5)

    def test_vertical_overlap_uses_smaller_height(self) -> None:
        first = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        second = ((20.0, 5.0), (30.0, 5.0), (30.0, 25.0), (20.0, 25.0))
        self.assertAlmostEqual(vertical_overlap_ratio(first, second), 0.5)

    def test_bounding_quad(self) -> None:
        first = ((2.0, 3.0), (4.0, 3.0), (4.0, 5.0), (2.0, 5.0))
        second = ((-1.0, 6.0), (8.0, 6.0), (8.0, 9.0), (-1.0, 9.0))
        self.assertEqual(
            bounding_quad((first, second)),
            ((-1.0, 3.0), (8.0, 3.0), (8.0, 9.0), (-1.0, 9.0)),
        )


if __name__ == "__main__":
    unittest.main()
