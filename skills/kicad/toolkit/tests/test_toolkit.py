#!/usr/bin/env python3
"""Focused stdlib unittest suite for ``skills/kicad/toolkit/{geometry,model,
queries,cli}.py``.

Everything here is pure ``unittest`` + a hand-built synthetic ``.kicad_pcb``
written to a temp file — no third-party test libs (no pytest), no network,
no fixture files beyond what this module writes to ``tempfile``.

Run from the repo root (``skills/kicad/toolkit/...`` must be resolvable from
cwd — ``python -m unittest`` adds the cwd to ``sys.path`` automatically):

    python -m unittest discover -s skills/kicad/toolkit/tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable even if invoked some other way than
# `python -m unittest discover` from the repo root (which already adds cwd).
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.kicad.toolkit import geometry as geo  # noqa: E402
from skills.kicad.toolkit import model  # noqa: E402
from skills.kicad.toolkit import queries as q  # noqa: E402
from skills.kicad.toolkit import cli  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic minimal KiCad board
# ---------------------------------------------------------------------------
#
# Deliberately exercises:
#   * a multi-layer stackup with a bare `addsublayer` marker (KiCad 10 style
#     multi-ply dielectric) — must parse without raising, even though the
#     toolkit's stackup parser does not keep both plies as separate dict
#     entries (last ply's keys win; see ParseStackupTests below).
#   * two footprints with rect pads at known absolute positions (angle 0).
#   * a routed `segment` (track) AND a net-assigned `gr_line` ("graphic
#     copper") on the same net, on F.Cu.
#   * a via on a different net.
#   * a keepout/rule-area zone with explicit restrictions and an exact
#     drawn polygon.
#   * a filled copper-pour zone (net GND) with its own exact polygon.
#   * an Edge.Cuts rectangle outline (4 gr_line segments).
SYNTH_BOARD = """
(kicad_pcb (version 20240108) (generator pcbnew) (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core")
        (thickness 0.1) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)
        addsublayer
        (thickness 0.1) (material "PP") (epsilon_r 1.0) (loss_tangent 0.0)
      )
      (layer "In1.Cu" (type "copper") (thickness 0.035))
      (layer "prepreg 1" (type "prepreg") (thickness 0.2) (material "2116") (epsilon_r 4.25) (loss_tangent 0.019))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (copper_finish "ENIG")
    )
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_line (start 0 0) (end 30 0) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e1"))
  (gr_line (start 30 0) (end 30 20) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e2"))
  (gr_line (start 30 20) (end 0 20) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e3"))
  (gr_line (start 0 20) (end 0 0) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e4"))
  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 5 5 0)
    (attr smd)
    (property "Reference" "U1" (at 0 -1 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "10k" (at 0 1 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -1 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG"))
    (pad "2" smd rect (at 1 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 25 5 0)
    (attr smd)
    (property "Reference" "U2" (at 0 -1 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "10k" (at 0 1 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -1 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG"))
    (pad "2" smd rect (at 1 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
  (segment (start 4 3) (end 20 3) (width 0.25) (layer "F.Cu") (net 2) (uuid "s1"))
  (gr_line (start 20 3) (end 24 3) (stroke (width 0.25) (type default)) (layer "F.Cu") (net 2 "SIG") (uuid "g1"))
  (via (at 6 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.1)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 0 15) (xy 30 15) (xy 30 20) (xy 0 20)))
    (filled_polygon (layer "F.Cu") (pts (xy 0.1 15.1) (xy 29.9 15.1) (xy 29.9 19.9) (xy 0.1 19.9)))
  )
  (zone (net 0) (net_name "") (layer "F.Cu") (hatch edge 0.5)
    (name "KEEPOUT1")
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
    (polygon (pts (xy 10 10) (xy 15 10) (xy 15 13) (xy 10 13)))
  )
)
"""


def _write_synth_board(dir_path: Path) -> Path:
    pcb_path = dir_path / "synth.kicad_pcb"
    pcb_path.write_text(SYNTH_BOARD, encoding="utf-8")
    return pcb_path


def _parse_synth_board() -> model.Board:
    with tempfile.TemporaryDirectory() as tmp:
        pcb_path = _write_synth_board(Path(tmp))
        return model.parse_pcb(pcb_path)


# ---------------------------------------------------------------------------
# geometry.py — pure stdlib primitives
# ---------------------------------------------------------------------------

class GeometryTests(unittest.TestCase):
    def test_arc_points_collinear_degenerates_to_straight_line(self):
        pts = geo.arc_points((0.0, 0.0), None, (10.0, 0.0))
        self.assertEqual(pts, [(0.0, 0.0), (10.0, 0.0)])

    def test_arc_points_quarter_circle_keeps_exact_endpoints(self):
        pts = geo.arc_points((1.0, 0.0), (0.70710678, 0.70710678), (0.0, 1.0))
        self.assertEqual(pts[0], (1.0, 0.0))
        self.assertEqual(pts[-1], (0.0, 1.0))
        self.assertGreater(len(pts), 2)

    def test_circle_polygon_radius(self):
        pts = geo.circle_polygon((0.0, 0.0), 2.0, sides=8)
        self.assertEqual(len(pts), 8)
        for x, y in pts:
            self.assertAlmostEqual((x ** 2 + y ** 2) ** 0.5, 2.0, places=9)

    def test_rotate_90deg_kicad_convention(self):
        # KiCad rotation is CCW on screen with y-down; rotate() negates the
        # angle internally, so +90 deg sends (1, 0) -> (0, -1).
        out = geo.rotate([(1.0, 0.0)], (0.0, 0.0), 90.0)
        self.assertAlmostEqual(out[0][0], 0.0, places=9)
        self.assertAlmostEqual(out[0][1], -1.0, places=9)

    def test_rotate_zero_angle_is_identity_without_copy_mutation(self):
        pts = [(1.0, 2.0)]
        out = geo.rotate(pts, (0.0, 0.0), 0.0)
        self.assertEqual(out, pts)
        self.assertIsNot(out, pts)

    def test_dist_point_to_segment_exact(self):
        d = geo.dist_point_to_segment(0.0, 0.0, 1.0, 1.0, 1.0, -1.0)
        self.assertAlmostEqual(d, 1.0, places=9)

    def test_segments_intersect_true_and_false(self):
        self.assertTrue(geo.segments_intersect(0, 0, 2, 2, 0, 2, 2, 0))
        self.assertFalse(geo.segments_intersect(0, 0, 1, 0, 0, 5, 1, 5))

    def test_dist_segment_to_segment_zero_when_crossing_else_exact_gap(self):
        self.assertEqual(geo.dist_segment_to_segment(0, 0, 2, 2, 0, 2, 2, 0), 0.0)
        self.assertAlmostEqual(geo.dist_segment_to_segment(0, 0, 1, 0, 0, 2, 1, 2), 2.0, places=9)

    def test_shoelace_area_unit_square(self):
        area = geo.shoelace_area([(0, 0), (1, 0), (1, 1), (0, 1)])
        self.assertAlmostEqual(area, 1.0, places=9)
        self.assertEqual(geo.shoelace_area([(0, 0), (1, 1)]), 0.0)

    def test_point_in_polygon_inside_and_outside(self):
        square = [(0, 0), (4, 0), (4, 4), (0, 4)]
        self.assertTrue(geo.point_in_polygon(2, 2, square))
        self.assertFalse(geo.point_in_polygon(10, 10, square))

    def test_bbox_gap_separated_and_touching(self):
        a = (0.0, 0.0, 1.0, 1.0)
        b = (4.0, 0.0, 5.0, 1.0)
        self.assertAlmostEqual(geo.bbox_gap(a, b), 3.0, places=9)
        self.assertEqual(geo.bbox_gap(a, (1.0, 0.0, 2.0, 1.0)), 0.0)

    def test_rect_corners_order(self):
        corners = geo.rect_corners(4.0, 5.0, 1.0, 1.0)
        self.assertEqual(corners, [(3.5, 4.5), (4.5, 4.5), (4.5, 5.5), (3.5, 5.5)])


# ---------------------------------------------------------------------------
# model.py — parse_pcb() over the synthetic board
# ---------------------------------------------------------------------------

class ModelParseTests(unittest.TestCase):
    def setUp(self):
        self.board = _parse_synth_board()

    def test_copper_layer_order(self):
        self.assertEqual(self.board.copper_layers, ["F.Cu", "In1.Cu", "B.Cu"])

    def test_stackup_parses_gracefully_with_addsublayer_marker(self):
        # Must not raise on the bare `addsublayer` atom, and must still
        # produce one dict per `(layer ...)` node (not a new node per ply —
        # the bare marker has no 2-item-list shape so it is simply skipped,
        # and the second ply's keys overwrite the first ply's in the same
        # dict). This is a known, documented limitation, not a crash.
        stackup = self.board.stackup
        self.assertEqual(len(stackup), 5)
        names = [entry["name"] for entry in stackup]
        self.assertEqual(names, ["F.Cu", "dielectric 1", "In1.Cu", "prepreg 1", "B.Cu"])
        dielectric = stackup[1]
        self.assertEqual(dielectric["material"], "PP")
        self.assertEqual(dielectric["epsilon_r"], 1.0)
        self.assertEqual(dielectric["loss_tangent"], 0.0)

    def test_footprint_and_pad_absolute_positions(self):
        by_ref = {fp.reference: fp for fp in self.board.footprints}
        self.assertEqual(set(by_ref), {"U1", "U2"})
        u1 = by_ref["U1"]
        self.assertEqual((u1.x, u1.y), (5.0, 5.0))
        pads_by_num = {p.number: p for p in u1.pads}
        self.assertEqual((pads_by_num["1"].x, pads_by_num["1"].y), (4.0, 5.0))
        self.assertEqual(pads_by_num["1"].net_name, "SIG")
        self.assertEqual((pads_by_num["2"].x, pads_by_num["2"].y), (6.0, 5.0))
        self.assertEqual(pads_by_num["2"].net_name, "GND")
        # Rect pad outline is exact at angle 0 (rect_corners(), unrotated).
        self.assertEqual(pads_by_num["1"].outline(),
                         [(3.5, 4.5), (4.5, 4.5), (4.5, 5.5), (3.5, 5.5)])

    def test_net_assigned_gr_line_parsed_as_graphic_copper_segment(self):
        graphic_segs = [s for s in self.board.segments if s.kind == "graphic"]
        self.assertEqual(len(graphic_segs), 1)
        seg = graphic_segs[0]
        self.assertEqual(seg.net_name, "SIG")
        self.assertEqual(seg.layer, "F.Cu")
        self.assertAlmostEqual(seg.length_mm, 4.0, places=9)
        # The routed track (kind="track") on the same net is still separate.
        track_segs = [s for s in self.board.segments if s.kind == "track" and s.net_name == "SIG"]
        self.assertEqual(len(track_segs), 1)
        self.assertAlmostEqual(track_segs[0].length_mm, 16.0, places=9)

    def test_via_parsed_on_gnd(self):
        self.assertEqual(len(self.board.vias), 1)
        via = self.board.vias[0]
        self.assertEqual((via.x, via.y), (6.0, 10.0))
        self.assertEqual(via.net_name, "GND")
        self.assertEqual(via.via_type, "through")

    def test_keepout_zone_restrictions_and_polygon(self):
        keepouts = [z for z in self.board.zones if z.is_keepout]
        self.assertEqual(len(keepouts), 1)
        kz = keepouts[0]
        self.assertEqual(kz.name, "KEEPOUT1")
        self.assertEqual(kz.keepout_restrictions, {
            "tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed",
            "copperpour": "not_allowed", "footprints": "allowed",
        })
        self.assertTrue(kz.outline_supported)
        self.assertEqual(kz.outline, [(10.0, 10.0), (15.0, 10.0), (15.0, 13.0), (10.0, 13.0)])
        self.assertAlmostEqual(kz.outline_area_mm2, 15.0, places=9)

    def test_copper_pour_zone_fill_and_area(self):
        pours = [z for z in self.board.zones if not z.is_keepout]
        self.assertEqual(len(pours), 1)
        pour = pours[0]
        self.assertEqual(pour.net_name, "GND")
        self.assertTrue(pour.outline_supported)
        self.assertAlmostEqual(pour.outline_area_mm2, 150.0, places=9)
        self.assertTrue(pour.is_filled)
        self.assertIn("F.Cu", pour.fill_polygons)

    def test_nets_and_edge_cuts_outline(self):
        self.assertEqual(self.board.nets(), ["GND", "SIG"])
        self.assertEqual(len(self.board.edge_cuts), 4)
        self.assertEqual(self.board.outline_bbox, (0.0, 0.0, 30.0, 20.0))


# ---------------------------------------------------------------------------
# queries.py — JSON-ready read-only queries over the parsed board
# ---------------------------------------------------------------------------

class QueriesTests(unittest.TestCase):
    def setUp(self):
        self.board = _parse_synth_board()

    def test_stackup_summary(self):
        summary = q.stackup_summary(self.board)
        self.assertEqual(summary["board_thickness_mm"], 1.6)
        self.assertEqual(summary["copper_layers"], ["F.Cu", "In1.Cu", "B.Cu"])
        self.assertEqual(summary["copper_layer_count"], 3)
        self.assertEqual(summary["outline_bbox_mm"], [0.0, 0.0, 30.0, 20.0])
        self.assertTrue(summary["outline_exact"])
        self.assertEqual(len(summary["dielectric_stackup"]), 5)

    def test_component_inventory(self):
        rows = q.component_inventory(self.board)
        self.assertEqual([r["reference"] for r in rows], ["U1", "U2"])
        self.assertEqual(rows[0]["nets"], ["GND", "SIG"])
        self.assertEqual(rows[0]["pad_count"], 2)
        # glob filter
        filtered = q.component_inventory(self.board, ref_glob="U2")
        self.assertEqual([r["reference"] for r in filtered], ["U2"])

    def test_net_routing_sig_combines_track_and_graphic_copper(self):
        result = q.net_routing(self.board, "SIG")
        self.assertTrue(result["found"])
        self.assertAlmostEqual(result["length_mm"], 20.0, places=9)
        self.assertAlmostEqual(result["by_layer_mm"]["F.Cu"], 20.0, places=9)
        self.assertAlmostEqual(result["graphic_copper_by_layer_mm"]["F.Cu"], 4.0, places=9)
        self.assertEqual(result["via_count"], 0)
        self.assertEqual(result["pads"], ["U1.1", "U2.1"])

    def test_net_routing_gnd_has_via_and_zone_layer_no_copper_length(self):
        result = q.net_routing(self.board, "GND")
        self.assertTrue(result["found"])
        self.assertEqual(result["length_mm"], 0)
        self.assertEqual(result["via_count"], 1)
        self.assertEqual(result["zone_layers"], ["F.Cu"])
        self.assertEqual(result["pads"], ["U1.2", "U2.2"])

    def test_net_routing_unknown_net_not_found(self):
        result = q.net_routing(self.board, "NOPE")
        self.assertFalse(result["found"])
        self.assertEqual(result["length_mm"], 0)

    def test_keepout_summary(self):
        rows = q.keepout_summary(self.board)
        self.assertEqual(len(rows), 1)
        entry = rows[0]
        self.assertEqual(entry["restrictions"]["tracks"], "not_allowed")
        self.assertTrue(entry["outline_supported"])
        self.assertAlmostEqual(entry["outline_area_mm2"], 15.0, places=9)
        # U1 (x=5) is within the default 5mm margin of the keepout bbox
        # (x0=10); U2 (x=25) is not.
        self.assertEqual(entry["nearby_components"], ["U1"])

    def test_edge_distance_exact_value(self):
        result = q.edge_distance(self.board, "SIG")
        self.assertAlmostEqual(result["min_mm"], 3.0, places=9)
        self.assertEqual(result["layer"], "F.Cu")
        self.assertTrue(result["exact"])

    def test_edge_distance_no_copper_reports_reason_not_fabricated_number(self):
        result = q.edge_distance(self.board, "NOPE")
        self.assertIsNone(result["min_mm"])
        self.assertIn("no copper", result["reason"])

    def test_net_clearance_sig_gnd_exact_stdlib_engine(self):
        result = q.net_clearance(self.board, "SIG", "GND")
        self.assertEqual(result["engine"], "stdlib_exact")
        self.assertAlmostEqual(result["min_mm"], 1.0, places=9)
        self.assertAlmostEqual(result["per_layer_mm"]["F.Cu"], 1.0, places=9)
        self.assertEqual(result["shared_layers"], ["F.Cu"])

    def test_analyzer_json_overlap_approximate_bbox_gap(self):
        analyzer_json = {
            "footprints": [
                {"reference": "U1", "courtyard": {"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2}},
                {"reference": "U2", "courtyard": {"min_x": 5, "min_y": 0, "max_x": 7, "max_y": 2}},
            ]
        }
        result = q.analyzer_json_overlap(analyzer_json, "U1", "U2")
        self.assertAlmostEqual(result["gap_mm"], 3.0, places=9)
        self.assertIn("approximate", result["method"])

    def test_analyzer_json_overlap_missing_footprints_key(self):
        result = q.analyzer_json_overlap({}, "U1", "U2")
        self.assertIn("error", result)

    def test_analyzer_json_overlap_missing_reference(self):
        analyzer_json = {"footprints": [{"reference": "U1", "x": 0, "y": 0}]}
        result = q.analyzer_json_overlap(analyzer_json, "U1", "U3")
        self.assertIn("error", result)
        self.assertIn("U3", result["error"])

    def test_no_resistance_or_loss_query_is_implemented(self):
        # Explicit limitation: copper resistance / IR drop / trace loss is
        # intentionally not computed anywhere in this module.
        public_names = [n for n in dir(q) if not n.startswith("_")]
        lowered = [n.lower() for n in public_names]
        self.assertFalse(any("resistance" in n for n in lowered))
        self.assertFalse(any("loss" in n for n in lowered))
        self.assertFalse(any("ir_drop" in n or "irdrop" in n for n in lowered))
        doc = " ".join((q.__doc__ or "").lower().split())
        self.assertIn("resistance", doc)
        self.assertIn("intentionally not", doc)
        self.assertIn("implemented here", doc)


# ---------------------------------------------------------------------------
# cli.py — argparse wiring + JSON-on-stdout contract
# ---------------------------------------------------------------------------

class CliInProcessTests(unittest.TestCase):
    """Introspect the argparse tree directly (no subprocess needed)."""

    def test_no_resistance_or_loss_subcommand_exists(self):
        parser = cli.build_parser()
        sub_action = next(a for a in parser._actions
                          if isinstance(a, __import__("argparse")._SubParsersAction))
        names = set(sub_action.choices.keys())
        self.assertEqual(names, {"stackup", "components", "net", "keepouts",
                                 "edge", "clearance", "overlap"})
        self.assertNotIn("resistance", names)
        self.assertNotIn("loss", names)
        self.assertNotIn("impedance", names)

    def test_help_exits_zero_via_argparse(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)


class CliSubprocessTests(unittest.TestCase):
    """End-to-end checks that must run in a clean child process: the
    ``--help`` path must not import Shapely, and ``--pcb`` must work with a
    real synthetic file on disk through the documented ``python -m
    skills.kicad.toolkit`` invocation."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.pcb_path = _write_synth_board(Path(cls._tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "skills.kicad.toolkit", *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
        )

    def test_cli_help_exits_zero_and_lists_commands(self):
        result = self._run("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("stackup", result.stdout)
        self.assertIn("clearance", result.stdout)
        self.assertNotIn("resistance", result.stdout.lower())
        self.assertNotIn("loss", result.stdout.lower())

    def test_cli_help_does_not_import_shapely(self):
        # Isolated child process: checks sys.modules right after the
        # --help SystemExit, independent of whether anything else in this
        # test run (or an installed shapely) has touched sys.modules.
        probe = (
            "import sys\n"
            "sys.argv = ['toolkit', '--help']\n"
            "try:\n"
            "    from skills.kicad.toolkit import cli\n"
            "    cli.main()\n"
            "except SystemExit:\n"
            "    pass\n"
            "print('SHAPELY_IMPORTED=' + str('shapely' in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("SHAPELY_IMPORTED=False", result.stdout)

    def test_cli_json_stackup_from_temp_synthetic_board(self):
        result = self._run("--pcb", str(self.pcb_path), "stackup")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["board_thickness_mm"], 1.6)
        self.assertEqual(payload["copper_layers"], ["F.Cu", "In1.Cu", "B.Cu"])

    def test_cli_json_components_from_temp_synthetic_board(self):
        result = self._run("--pcb", str(self.pcb_path), "components")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([r["reference"] for r in payload], ["U1", "U2"])

    def test_cli_json_net_matches_library_call(self):
        result = self._run("--pcb", str(self.pcb_path), "net", "SIG")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        board = model.parse_pcb(self.pcb_path)
        self.assertEqual(payload, q.net_routing(board, "SIG"))

    def test_cli_json_keepouts_and_edge_and_clearance(self):
        keepouts = json.loads(self._run("--pcb", str(self.pcb_path), "keepouts").stdout)
        self.assertEqual(len(keepouts), 1)
        self.assertAlmostEqual(keepouts[0]["outline_area_mm2"], 15.0, places=9)

        edge = json.loads(self._run("--pcb", str(self.pcb_path), "edge", "SIG").stdout)
        self.assertAlmostEqual(edge["min_mm"], 3.0, places=9)

        clearance = json.loads(self._run("--pcb", str(self.pcb_path), "clearance", "SIG", "GND").stdout)
        self.assertAlmostEqual(clearance["min_mm"], 1.0, places=9)
        self.assertEqual(clearance["engine"], "stdlib_exact")

    def test_cli_json_overlap_from_analyzer_json_file(self):
        analyzer_json = {
            "footprints": [
                {"reference": "U1", "courtyard": {"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2}},
                {"reference": "U2", "courtyard": {"min_x": 5, "min_y": 0, "max_x": 7, "max_y": 2}},
            ]
        }
        analyzer_path = Path(self._tmpdir.name) / "analyzer.json"
        analyzer_path.write_text(json.dumps(analyzer_json), encoding="utf-8")
        result = self._run("overlap", "--analyzer-json", str(analyzer_path), "U1", "U2")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertAlmostEqual(payload["gap_mm"], 3.0, places=9)


if __name__ == "__main__":
    unittest.main()
