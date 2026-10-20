#!/usr/bin/env python3
"""Unit tests for toolkit/queries.py, against fixtures/sample.kicad_pcb.

Expected numeric values in this file were hand-derived from the fixture's
coordinates (see comments) and cross-checked by running the module once —
they are not just "whatever the code currently returns".

Run with:
    python3 skills/kicad/toolkit/test_queries.py
    python3 -m unittest skills/kicad/toolkit/test_queries.py
"""

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from toolkit import deps, model, queries as q  # noqa: E402
from toolkit.model import parse_pcb  # noqa: E402

FIXTURE = os.path.join(SCRIPT_DIR, "fixtures", "sample.kicad_pcb")


class TestQueries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = parse_pcb(FIXTURE)

    def test_stackup_summary(self):
        s = q.stackup_summary(self.board)
        self.assertEqual(s["board_thickness_mm"], 1.6)
        self.assertEqual(s["copper_layers"], ["F.Cu", "B.Cu"])
        self.assertEqual(s["outline_bbox_mm"], [0.0, 0.0, 20.0, 15.0])
        self.assertTrue(s["outline_exact"])
        self.assertEqual(s["edge_cuts_segment_count"], 4)

    def test_component_inventory(self):
        rows = q.component_inventory(self.board)
        self.assertEqual([r["reference"] for r in rows], ["R1", "R2"])
        self.assertEqual(rows[0]["nets"], ["GND", "NET1"])
        self.assertEqual(rows[0]["pad_count"], 2)

    def test_component_inventory_ref_glob(self):
        rows = q.component_inventory(self.board, ref_glob="R2")
        self.assertEqual([r["reference"] for r in rows], ["R2"])

    def test_net_routing_net1(self):
        # track segment (4.5,7)->(14.5,7) = 10.0 mm + gr_line (5,8)->(10,8) = 5.0 mm
        r = q.net_routing(self.board, "NET1")
        self.assertTrue(r["found"])
        self.assertAlmostEqual(r["length_mm"], 15.0, places=6)
        self.assertAlmostEqual(r["by_layer_mm"]["F.Cu"], 15.0, places=6)
        self.assertAlmostEqual(r["graphic_copper_by_layer_mm"]["F.Cu"], 5.0, places=6)
        self.assertEqual(r["via_count"], 0)
        self.assertEqual(r["pads"], ["R1.1", "R2.1"])

    def test_net_routing_gnd_has_via_and_zone(self):
        r = q.net_routing(self.board, "GND")
        self.assertTrue(r["found"])
        self.assertEqual(r["via_count"], 1)
        self.assertEqual(r["pads"], ["R1.2", "R2.2"])
        self.assertEqual(r["zone_layers"], ["F.Cu"])

    def test_net_routing_unknown_net(self):
        r = q.net_routing(self.board, "NOPE")
        self.assertFalse(r["found"])
        self.assertEqual(r["length_mm"], 0)

    def test_keepout_summary_exact_polygon(self):
        rows = q.keepout_summary(self.board)
        self.assertEqual(len(rows), 1)
        kp = rows[0]
        self.assertEqual(kp["name"], "KEEPOUT1")
        self.assertTrue(kp["outline_supported"])
        self.assertAlmostEqual(kp["outline_area_mm2"], 8.0, places=6)
        self.assertEqual(kp["outline_bbox_mm"], [2.0, 2.0, 6.0, 4.0])
        self.assertEqual(kp["restrictions"]["tracks"], "not_allowed")
        # R1 (at x=5,y=5) is within the 5mm margin of bbox [2,2,6,4]; R2 (x=15) is not.
        self.assertEqual(kp["nearby_components"], ["R1"])

    def test_edge_distance_net1(self):
        # Closest feature is pad R1.1's left edge (x=4.5-0.45=4.05) to the x=0 wall.
        r = q.edge_distance(self.board, "NET1")
        self.assertAlmostEqual(r["min_mm"], 4.05, places=6)
        self.assertEqual(r["layer"], "F.Cu")
        self.assertTrue(r["exact"])

    def test_edge_distance_no_copper(self):
        r = q.edge_distance(self.board, "NOPE")
        self.assertIsNone(r["min_mm"])
        self.assertIn("no copper", r["reason"])

    def test_net_clearance_stdlib_exact(self):
        # R1's own two adjacent pads give a centerline/outline gap of 0.10 mm
        # (NET1 pad right edge 4.95 to GND pad left edge 5.05), but the real
        # copper-to-copper minimum on this fixture is tighter: the GND via at
        # (10, 6.5) r=0.3 sits only 0.5 mm (center-to-centerline) from the
        # NET1 track at y=7 width=0.25 — once the track's own half-width
        # (0.125) is subtracted from that 0.2 mm via-boundary-to-centerline
        # gap, the true clearance is 0.075 mm, smaller than the pad/pad gap.
        r = q.net_clearance(self.board, "NET1", "GND", layer="F.Cu")
        self.assertEqual(r["engine"], "stdlib_exact")
        self.assertAlmostEqual(r["copper_gap_mm"], 0.075, places=6)
        self.assertAlmostEqual(r["min_mm"], 0.075, places=6)
        self.assertAlmostEqual(r["centerline_min_mm"], 0.2, places=6)

    def test_net_clearance_all_layers_matches_single_layer(self):
        r = q.net_clearance(self.board, "NET1", "GND")
        self.assertAlmostEqual(r["min_mm"], 0.075, places=6)
        self.assertEqual(r["shared_layers"], ["F.Cu"])

    @unittest.skipUnless(deps.has_shapely(), "shapely not installed - optional dependency")
    def test_net_clearance_shapely_matches_stdlib(self):
        exact = q.net_clearance(self.board, "NET1", "GND", layer="F.Cu")
        fast = q.net_clearance(self.board, "NET1", "GND", layer="F.Cu", prefer_shapely=True)
        self.assertEqual(fast["engine"], "shapely")
        self.assertAlmostEqual(fast["min_mm"], exact["min_mm"], places=3)

    def test_net_clearance_shapely_requested_but_absent_falls_back(self):
        if deps.has_shapely():
            self.skipTest("shapely is installed in this environment")
        r = q.net_clearance(self.board, "NET1", "GND", layer="F.Cu", prefer_shapely=True)
        self.assertEqual(r["engine"], "stdlib_exact")

    def test_net_clearance_flags_approximate_geometry_from_via(self):
        # The GND via (always rendered as an N-gon standing in for a true
        # circle) is the piece driving this fixture's minimum clearance, so
        # the result must say so rather than silently claiming exactness.
        r = q.net_clearance(self.board, "NET1", "GND", layer="F.Cu")
        self.assertFalse(r["exact"])
        self.assertIn("via", r["note"])


def _bare_board(segments=None, arcs=None, zones=None, footprints=None, vias=None) -> model.Board:
    """Minimal in-memory Board for geometry-only net_clearance tests —
    bypasses .kicad_pcb text parsing entirely."""
    return model.Board(
        path="<test>", board_thickness_mm=1.6, layers=[], copper_layers=["F.Cu", "B.Cu"],
        stackup=[], footprints=footprints or [], segments=segments or [], arcs=arcs or [],
        vias=vias or [], zones=zones or [], edge_cuts=[],
    )


class TestNetClearanceCopperWidth(unittest.TestCase):
    """Correctness tests for the trace-width (half-width) deduction and the
    containment fix in net_clearance — see queries._net_copper_pieces /
    _piece_boundary_distance."""

    def test_parallel_tracks_copper_gap_subtracts_both_half_widths(self):
        # Two 0.2 mm tracks on parallel centerlines 0.3 mm apart: the real
        # copper-to-copper gap is 0.3 - 0.1 - 0.1 = 0.1 mm, not the 0.3 mm a
        # naive centerline-to-centerline distance would report.
        seg_a = model.Segment(0.0, 0.0, 10.0, 0.0, 0.2, "F.Cu", "A", "track")
        seg_b = model.Segment(0.0, 0.3, 10.0, 0.3, 0.2, "F.Cu", "B", "track")
        board = _bare_board(segments=[seg_a, seg_b])
        r = q.net_clearance(board, "A", "B", layer="F.Cu")
        self.assertAlmostEqual(r["centerline_min_mm"], 0.3, places=6)
        self.assertAlmostEqual(r["copper_gap_mm"], 0.1, places=6)
        self.assertAlmostEqual(r["min_mm"], 0.1, places=6)
        self.assertTrue(r["exact"])
        self.assertIsNone(r["note"])

    def test_overlapping_tracks_report_non_positive_gap_not_false_positive(self):
        # Centerlines cross (raw distance 0) — widening by half-widths must
        # not somehow produce a positive "gap".
        seg_a = model.Segment(0.0, 0.0, 10.0, 10.0, 0.2, "F.Cu", "A", "track")
        seg_b = model.Segment(0.0, 10.0, 10.0, 0.0, 0.2, "F.Cu", "B", "track")
        board = _bare_board(segments=[seg_a, seg_b])
        r = q.net_clearance(board, "A", "B", layer="F.Cu")
        self.assertLessEqual(r["copper_gap_mm"], 0.0)

    def test_contained_zone_fill_reports_overlap_not_positive_gap(self):
        # Net B's fill polygon sits entirely inside net A's — no edges cross,
        # so a naive edge-to-edge distance would (wrongly) report the margin
        # between the two boundaries as a positive "gap". This is copper
        # overlap and must come back <= 0.
        outer = model.Zone(
            name="A_POUR", net_name="A", layers=("F.Cu",), is_keepout=False,
            keepout_restrictions={}, outline=[], outline_supported=False,
            fill_polygons={"F.Cu": [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]},
        )
        inner = model.Zone(
            name="B_POUR", net_name="B", layers=("F.Cu",), is_keepout=False,
            keepout_restrictions={}, outline=[], outline_supported=False,
            fill_polygons={"F.Cu": [[(2.0, 2.0), (4.0, 2.0), (4.0, 4.0), (2.0, 4.0)]]},
        )
        board = _bare_board(zones=[outer, inner])
        r = q.net_clearance(board, "A", "B", layer="F.Cu")
        self.assertLessEqual(r["copper_gap_mm"], 0.0)
        self.assertEqual(r["copper_gap_mm"], 0.0)
        self.assertTrue(r["exact"])  # zone fills are exact polygons, no approximation involved

    def test_non_overlapping_zone_fills_report_true_positive_gap(self):
        a = model.Zone(
            name="A_POUR", net_name="A", layers=("F.Cu",), is_keepout=False,
            keepout_restrictions={}, outline=[], outline_supported=False,
            fill_polygons={"F.Cu": [[(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]]},
        )
        b = model.Zone(
            name="B_POUR", net_name="B", layers=("F.Cu",), is_keepout=False,
            keepout_restrictions={}, outline=[], outline_supported=False,
            fill_polygons={"F.Cu": [[(6.0, 0.0), (11.0, 0.0), (11.0, 5.0), (6.0, 5.0)]]},
        )
        board = _bare_board(zones=[a, b])
        r = q.net_clearance(board, "A", "B", layer="F.Cu")
        self.assertAlmostEqual(r["copper_gap_mm"], 1.0, places=6)


class TestAnalyzerJsonOverlap(unittest.TestCase):
    def test_bbox_gap_from_courtyard(self):
        data = {
            "footprints": [
                {"reference": "U1", "x": 0.0, "y": 0.0,
                 "courtyard": {"min_x": -1.0, "min_y": -1.0, "max_x": 1.0, "max_y": 1.0}},
                {"reference": "U2", "x": 5.0, "y": 0.0,
                 "courtyard": {"min_x": 4.0, "min_y": -1.0, "max_x": 6.0, "max_y": 1.0}},
            ]
        }
        r = q.analyzer_json_overlap(data, "U1", "U2")
        self.assertAlmostEqual(r["gap_mm"], 3.0, places=6)
        self.assertIn("approximate", r["note"])

    def test_missing_reference(self):
        data = {"footprints": [{"reference": "U1", "x": 0.0, "y": 0.0}]}
        r = q.analyzer_json_overlap(data, "U1", "U99")
        self.assertIn("error", r)

    def test_missing_footprints_key(self):
        r = q.analyzer_json_overlap({}, "U1", "U2")
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
