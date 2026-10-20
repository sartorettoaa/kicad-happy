#!/usr/bin/env python3
"""Unit tests for toolkit/geometry.py (pure stdlib, no fixtures needed).

Run with:
    python3 skills/kicad/toolkit/test_geometry.py
    python3 -m unittest skills/kicad/toolkit/test_geometry.py
"""

import math
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from toolkit import geometry as geo  # noqa: E402


class TestArcPoints(unittest.TestCase):
    def test_quarter_circle_endpoints_exact(self):
        # Quarter circle, radius 1, centered at origin: (1,0) -> (0.7071,0.7071) -> (0,1)
        pts = geo.arc_points((1.0, 0.0), (0.70710678, 0.70710678), (0.0, 1.0))
        self.assertEqual(pts[0], (1.0, 0.0))
        self.assertEqual(pts[-1], (0.0, 1.0))
        self.assertGreater(len(pts), 2)

    def test_collinear_degenerates_to_line(self):
        pts = geo.arc_points((0.0, 0.0), (5.0, 0.0), (10.0, 0.0))
        self.assertEqual(pts, [(0.0, 0.0), (10.0, 0.0)])

    def test_missing_mid_degenerates_to_line(self):
        pts = geo.arc_points((0.0, 0.0), None, (10.0, 0.0))
        self.assertEqual(pts, [(0.0, 0.0), (10.0, 0.0)])


class TestDistances(unittest.TestCase):
    def test_point_to_segment_perpendicular(self):
        d = geo.dist_point_to_segment(5.0, 5.0, 0.0, 0.0, 10.0, 0.0)
        self.assertAlmostEqual(d, 5.0, places=9)

    def test_point_to_segment_clamped(self):
        d = geo.dist_point_to_segment(-3.0, 4.0, 0.0, 0.0, 10.0, 0.0)
        self.assertAlmostEqual(d, 5.0, places=9)  # distance to the (0,0) endpoint

    def test_segment_to_segment_parallel_gap(self):
        d = geo.dist_segment_to_segment(0.0, 0.0, 0.0, 1.0, 0.1, 0.0, 0.1, 1.0)
        self.assertAlmostEqual(d, 0.1, places=9)

    def test_segment_to_segment_crossing_is_zero(self):
        d = geo.dist_segment_to_segment(0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 0.0)
        self.assertEqual(d, 0.0)

    def test_dist_polyline_to_polyline_rectangles(self):
        rect_a = geo.rect_corners(0.0, 0.0, 1.0, 1.0)   # spans [-0.5,0.5]
        rect_b = geo.rect_corners(1.0, 0.0, 1.0, 1.0)   # spans [0.5,1.5]
        d = geo.dist_polyline_to_polyline(rect_a, rect_b, closed_a=True, closed_b=True)
        self.assertAlmostEqual(d, 0.0, places=9)        # touching edges

        rect_c = geo.rect_corners(2.0, 0.0, 1.0, 1.0)   # spans [1.5,2.5] -> 1.0 mm gap from rect_a
        d2 = geo.dist_polyline_to_polyline(rect_a, rect_c, closed_a=True, closed_b=True)
        self.assertAlmostEqual(d2, 1.0, places=9)


class TestAreaAndBbox(unittest.TestCase):
    def test_shoelace_area_square(self):
        square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
        self.assertAlmostEqual(geo.shoelace_area(square), 16.0, places=9)

    def test_shoelace_area_too_few_points(self):
        self.assertEqual(geo.shoelace_area([(0.0, 0.0), (1.0, 1.0)]), 0.0)

    def test_bbox(self):
        pts = [(1.0, -2.0), (5.0, 3.0), (-1.0, 0.0)]
        self.assertEqual(geo.bbox(pts), (-1.0, -2.0, 5.0, 3.0))

    def test_bbox_gap_overlapping_is_zero(self):
        self.assertEqual(geo.bbox_gap((0.0, 0.0, 2.0, 2.0), (1.0, 1.0, 3.0, 3.0)), 0.0)

    def test_bbox_gap_separated(self):
        d = geo.bbox_gap((0.0, 0.0, 1.0, 1.0), (4.0, 0.0, 5.0, 1.0))
        self.assertAlmostEqual(d, 3.0, places=9)


class TestPointInPolygon(unittest.TestCase):
    def test_inside_and_outside(self):
        square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
        self.assertTrue(geo.point_in_polygon(2.0, 2.0, square))
        self.assertFalse(geo.point_in_polygon(5.0, 5.0, square))


class TestPadShapes(unittest.TestCase):
    def test_rect_corners_count_and_bbox(self):
        corners = geo.rect_corners(10.0, 10.0, 2.0, 1.0)
        self.assertEqual(len(corners), 4)
        self.assertEqual(geo.bbox(corners), (9.0, 9.5, 11.0, 10.5))

    def test_circle_polygon_radius(self):
        pts = geo.circle_polygon((0.0, 0.0), 2.0, sides=8)
        for x, y in pts:
            self.assertAlmostEqual(math.hypot(x, y), 2.0, places=9)

    def test_stadium_degenerates_to_circle_when_square(self):
        pts = geo.stadium_polygon(0.0, 0.0, 2.0, 2.0)
        for x, y in pts:
            self.assertAlmostEqual(math.hypot(x, y), 1.0, places=6)

    def test_rotate_90_degrees(self):
        # KiCad convention: positive angle rotates (1,0) toward (0,-1) in board Y-down coords.
        out = geo.rotate([(1.0, 0.0)], (0.0, 0.0), 90.0)
        self.assertAlmostEqual(out[0][0], 0.0, places=9)
        self.assertAlmostEqual(out[0][1], -1.0, places=9)

    def test_rotate_zero_is_noop(self):
        pts = [(1.0, 2.0), (3.0, 4.0)]
        self.assertEqual(geo.rotate(pts, (0.0, 0.0), 0.0), pts)


if __name__ == "__main__":
    unittest.main()
