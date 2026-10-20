#!/usr/bin/env python3
"""Unit tests for toolkit/model.py.

Run with:
    python3 skills/kicad/toolkit/test_model.py
    python3 -m unittest skills/kicad/toolkit/test_model.py
"""

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from toolkit.model import Board, _resolve_net, parse_pcb  # noqa: E402

FIXTURE = os.path.join(SCRIPT_DIR, "fixtures", "sample.kicad_pcb")


class TestResolveNet(unittest.TestCase):
    """Net resolution must handle both KiCad <= 9 (numeric id + top-level
    declarations) and KiCad 10 (name-only, no declarations at all)."""

    def test_inline_name_kicad_le9_pad_form(self):
        # (net 2 "NET1") — pads carry the name inline even on KiCad <= 9.
        self.assertEqual(_resolve_net(["net", "2", "NET1"], {}), "NET1")

    def test_numeric_id_looked_up_in_declarations(self):
        # (net 1) on a segment/via refers to a top-level (net 1 "GND").
        self.assertEqual(_resolve_net(["net", "1"], {1: "GND"}), "GND")

    def test_kicad10_name_only_no_declarations(self):
        # (net "GND") — KiCad 10 drops numeric net declarations entirely.
        self.assertEqual(_resolve_net(["net", "GND"], {}), "GND")

    def test_missing_or_empty_node(self):
        self.assertEqual(_resolve_net(None, {1: "GND"}), "")
        self.assertEqual(_resolve_net(["net"], {1: "GND"}), "")

    def test_unknown_numeric_id_returns_empty(self):
        self.assertEqual(_resolve_net(["net", "99"], {1: "GND"}), "")


class TestParsePcb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board: Board = parse_pcb(FIXTURE)

    def test_board_thickness_and_layers(self):
        self.assertEqual(self.board.board_thickness_mm, 1.6)
        self.assertEqual(self.board.copper_layers, ["F.Cu", "B.Cu"])

    def test_stackup_dielectric_entries(self):
        names = [l["name"] for l in self.board.stackup]
        self.assertEqual(names, ["F.Cu", "dielectric 1", "B.Cu"])
        core = self.board.stackup[1]
        self.assertEqual(core["epsilon_r"], 4.5)
        self.assertEqual(core["thickness"], 1.51)

    def test_footprints_and_pad_nets(self):
        refs = {f.reference: f for f in self.board.footprints}
        self.assertEqual(set(refs), {"R1", "R2"})
        r1 = refs["R1"]
        self.assertEqual(r1.pad_count, 2)
        pad_nets = {p.number: p.net_name for p in r1.pads}
        self.assertEqual(pad_nets, {"1": "NET1", "2": "GND"})
        # Absolute pad position = footprint position + local pad offset (angle 0 here).
        pad1 = next(p for p in r1.pads if p.number == "1")
        self.assertAlmostEqual(pad1.x, 4.5, places=6)
        self.assertAlmostEqual(pad1.y, 5.0, places=6)

    def test_segments_and_graphic_copper_are_distinguished(self):
        kinds = {(s.net_name, s.kind) for s in self.board.segments}
        self.assertIn(("NET1", "track"), kinds)
        self.assertIn(("NET1", "graphic"), kinds)

    def test_vias(self):
        self.assertEqual(len(self.board.vias), 1)
        via = self.board.vias[0]
        self.assertEqual(via.net_name, "GND")
        self.assertEqual(via.via_type, "through")
        self.assertEqual(via.layers, ("F.Cu", "B.Cu"))

    def test_zones_split_keepout_from_copper(self):
        keepouts = [z for z in self.board.zones if z.is_keepout]
        copper = [z for z in self.board.zones if not z.is_keepout]
        self.assertEqual(len(keepouts), 1)
        self.assertEqual(len(copper), 1)
        self.assertEqual(keepouts[0].name, "KEEPOUT1")
        self.assertTrue(keepouts[0].outline_supported)
        self.assertAlmostEqual(keepouts[0].outline_area_mm2, 8.0, places=6)
        self.assertEqual(copper[0].net_name, "GND")
        self.assertTrue(copper[0].is_filled)

    def test_outline_bbox(self):
        self.assertEqual(self.board.outline_bbox, (0.0, 0.0, 20.0, 15.0))

    def test_nets_listing(self):
        self.assertEqual(self.board.nets(), ["GND", "NET1"])


if __name__ == "__main__":
    unittest.main()
