#!/usr/bin/env python3
"""Unit tests for board_facts.py.

Self-contained: every board is a tiny synthetic S-expression string built
inline (no fixture files, no network, no external deps) and parsed with this
checkout's own ``sexp_parser``, so the tests also exercise the real KiCad 10
``addsublayer`` multi-ply format that motivated this module.

Run from the repo root:
    python -m unittest skills.kicad.scripts.test_board_facts
    python -m unittest skills.kicad.scripts.test_board_facts -v

Or directly:
    python skills/kicad/scripts/test_board_facts.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# board_facts.py and sexp_parser.py live next to this file — insert that dir
# on sys.path so the local (unqualified) imports below resolve regardless of
# how this test module itself was addressed (flat script vs. dotted module
# path from the repo root).
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import board_facts  # noqa: E402
import sexp_parser  # noqa: E402


def _parse(text: str) -> list:
    return sexp_parser.parse(text)


# ---------------------------------------------------------------------------
# Synthetic board text
# ---------------------------------------------------------------------------

SINGLE_PLY_STACKUP = """
(kicad_pcb (version 20240101) (generator pcbnew)
  (general (thickness 1.6))
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core")
        (thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)
      )
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
)
"""

NO_STACKUP_BOARD = """
(kicad_pcb (version 20240101) (generator pcbnew)
  (general (thickness 1.6))
)
"""

# Exercises every board_facts code path in one board:
# - multi-ply dielectric under F.Cu split at a bare `addsublayer` marker
#   (KiCad 10), one ply with implausible epsilon_r and one with a zero
#   loss_tangent
# - a single-ply prepreg under In1.Cu (must NOT also trip the multi-ply
#   warning)
# - an "Impedance Control" / "Yes" fab-notes gr_text pair on the same row
# - a top-level net table (net 0 "" / net 3 "RF_FEED"), as every real
#   .kicad_pcb has — graphic copper below references net 3 by id only
#   (legacy encoding, still used by tracks/vias/zones on every version), and
#   must resolve to "RF_FEED" through that table, matching the pad's own
#   inline net name
# - graphic copper (gr_line + gr_poly) on F.Cu, net 3 (id-only)
# - an unnetted gr_arc on B.Cu (net 0 == no net)
# - a footprint with one fp_line touching a netted pad and one touching
#   nothing (orphan)
MULTI_PLY_BOARD = """
(kicad_pcb (version 20240101) (generator pcbnew)
  (general (thickness 1.6))
  (net 0 "")
  (net 3 "RF_FEED")
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core")
        (thickness 0.1) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)
        addsublayer
        (thickness 0.1) (material "PP") (epsilon_r 1.0) (loss_tangent 0.0)
      )
      (layer "In1.Cu" (type "copper") (thickness 0.035))
      (layer "prepreg 1" (type "prepreg")
        (thickness 0.2) (material "2116") (epsilon_r 4.25) (loss_tangent 0.019)
      )
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
  (gr_text "Impedance Control" (at 10 10 0) (layer "F.SilkS"))
  (gr_text "Yes" (at 30 10 0) (layer "F.SilkS"))
  (gr_line (start 0 0) (end 5 0) (layer "F.Cu") (net 3))
  (gr_poly (pts (xy 0 0) (xy 1 0) (xy 1 1)) (layer "F.Cu") (net 3))
  (gr_arc (start 0 0) (mid 1 1) (end 2 0) (layer "B.Cu") (net 0))
  (footprint "Antenna:PrintedAnt" (layer "F.Cu")
    (property "Reference" "AE1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "RF_FEED"))
    (fp_line (start 0 0) (end 2 0) (layer "F.Cu"))
    (fp_line (start 10 10) (end 12 10) (layer "F.Cu"))
  )
)
"""


class ParseStackupTests(unittest.TestCase):
    def test_single_ply_has_no_warnings(self):
        facts = board_facts.parse_stackup(_parse(SINGLE_PLY_STACKUP))
        self.assertEqual(facts.thickness_mm, 1.6)
        self.assertEqual([cl.name for cl in facts.copper_layers], ["F.Cu", "B.Cu"])
        f_cu = facts.copper_layers[0]
        self.assertAlmostEqual(f_cu.copper_um, 35.0)
        self.assertEqual(len(f_cu.plies_below), 1)
        self.assertAlmostEqual(f_cu.dielectric_below_mm, 1.51)
        self.assertEqual(facts.stackup_warnings, [])
        self.assertFalse(facts.stackup_multi_ply)

    def test_missing_stackup_node_warns_and_yields_no_layers(self):
        facts = board_facts.parse_stackup(_parse(NO_STACKUP_BOARD))
        self.assertEqual(len(facts.stackup_warnings), 1)
        self.assertIn("No (stackup)", facts.stackup_warnings[0])
        self.assertEqual(facts.copper_layers, [])

    def test_multi_ply_addsublayer_splits_and_sums_correctly(self):
        facts = board_facts.parse_stackup(_parse(MULTI_PLY_BOARD))
        f_cu = next(cl for cl in facts.copper_layers if cl.name == "F.Cu")
        in1 = next(cl for cl in facts.copper_layers if cl.name == "In1.Cu")

        # Two plies split at the bare `addsublayer` atom, heights summed.
        self.assertEqual(len(f_cu.plies_below), 2)
        self.assertAlmostEqual(f_cu.dielectric_below_mm, 0.2)
        self.assertEqual(f_cu.plies_below[0].epsilon_r, 4.5)
        self.assertEqual(f_cu.plies_below[1].epsilon_r, 1.0)
        self.assertEqual(f_cu.plies_below[1].loss_tangent, 0.0)

        # A single-ply prepreg must not be mistaken for a multi-ply dielectric.
        self.assertEqual(len(in1.plies_below), 1)
        self.assertTrue(facts.stackup_multi_ply)
        self.assertEqual(sum("multi-ply" in w for w in facts.stackup_warnings), 1)

    def test_implausible_epsilon_r_warns(self):
        facts = board_facts.parse_stackup(_parse(MULTI_PLY_BOARD))
        er_warnings = [w for w in facts.stackup_warnings if "epsilon_r" in w]
        self.assertEqual(len(er_warnings), 1)
        self.assertIn("epsilon_r = 1.0", er_warnings[0])
        self.assertIn("implausible", er_warnings[0])

    def test_implausible_loss_tangent_warns(self):
        facts = board_facts.parse_stackup(_parse(MULTI_PLY_BOARD))
        tand_warnings = [w for w in facts.stackup_warnings if "loss_tangent" in w]
        self.assertEqual(len(tand_warnings), 1)
        self.assertIn("loss_tangent = 0.0", tand_warnings[0])

    def test_plausible_values_do_not_warn(self):
        facts = board_facts.parse_stackup(_parse(SINGLE_PLY_STACKUP))
        self.assertFalse(any("epsilon_r" in w for w in facts.stackup_warnings))
        self.assertFalse(any("loss_tangent" in w for w in facts.stackup_warnings))


class ImpedanceNoteTests(unittest.TestCase):
    def test_detects_yes_on_same_row(self):
        self.assertTrue(board_facts._impedance_note(_parse(MULTI_PLY_BOARD)))

    def test_none_when_no_fab_note_present(self):
        self.assertIsNone(board_facts._impedance_note(_parse(SINGLE_PLY_STACKUP)))


class GraphicCopperTests(unittest.TestCase):
    def setUp(self):
        self.root = _parse(MULTI_PLY_BOARD)

    def test_net_assigned_graphic_copper_sums_by_net_and_layer(self):
        """gr_line/gr_poly reference net 3 by id only (legacy encoding); the
        board's top-level (net 3 "RF_FEED") table resolves it to the real
        name, matching the pad's own inline name below."""
        gc = board_facts.graphic_copper(self.root)
        self.assertEqual(set(gc.keys()), {"RF_FEED"})
        self.assertAlmostEqual(gc["RF_FEED"]["F.Cu"], 5.0)  # gr_line length; gr_poly contributes 0.0

    def test_graphic_copper_ignores_non_copper_layers_and_unnetted_shapes(self):
        root = _parse("""
            (kicad_pcb
              (gr_line (start 0 0) (end 1 0) (layer "F.SilkS") (net 3))
              (gr_line (start 0 0) (end 1 0) (layer "F.Cu") (net 0))
            )
        """)
        self.assertEqual(board_facts.graphic_copper(root), {})

    def test_unnetted_graphic_copper_is_found_with_location_and_length(self):
        unnetted = board_facts.unnetted_graphic_copper(self.root)
        self.assertEqual(len(unnetted), 1)
        self.assertEqual(unnetted[0]["kind"], "gr_arc")
        self.assertEqual(unnetted[0]["layer"], "B.Cu")
        self.assertGreater(unnetted[0]["length_mm"], 0.0)

    def test_footprint_copper_attributes_touching_shape_to_pad_net(self):
        fpc = board_facts.footprint_copper(self.root)
        self.assertIn("RF_FEED", fpc)
        self.assertAlmostEqual(fpc["RF_FEED"]["F.Cu"], 2.0)

    def test_footprint_copper_shape_touching_no_pad_is_unconnected(self):
        fpc = board_facts.footprint_copper(self.root)
        self.assertIn("<unconnected:AE1>", fpc)
        self.assertAlmostEqual(fpc["<unconnected:AE1>"]["F.Cu"], 2.0)

    def test_nets_with_extra_copper_excludes_unconnected_marker(self):
        bf = board_facts.BoardFacts(
            graphic_copper=board_facts.graphic_copper(self.root),
            footprint_copper=board_facts.footprint_copper(self.root),
        )
        self.assertEqual(board_facts.nets_with_extra_copper(bf), {"RF_FEED"})


class NetResolutionTests(unittest.TestCase):
    """Net id -> name resolution across the encodings KiCad has used."""

    def test_net_id_map_reads_top_level_net_table(self):
        net_map = board_facts._net_id_map(_parse(MULTI_PLY_BOARD))
        self.assertEqual(net_map, {"0": "", "3": "RF_FEED"})

    def test_legacy_id_only_net_falls_back_to_bare_id_without_net_table(self):
        """No top-level (net N "name") table at all (older/hand-built file):
        graphic copper degrades to the bare id instead of losing the net."""
        root = _parse("""
            (kicad_pcb
              (gr_line (start 0 0) (end 5 0) (layer "F.Cu") (net 3))
            )
        """)
        gc = board_facts.graphic_copper(root)
        self.assertEqual(set(gc.keys()), {"3"})

    def test_kicad10_name_only_net_is_used_as_is(self):
        """KiCad 10 name-only ``(net "name")`` encoding (no numeric id):
        nothing to resolve, used directly."""
        root = _parse("""
            (kicad_pcb
              (gr_line (start 0 0) (end 5 0) (layer "F.Cu") (net "RF_FEED"))
            )
        """)
        gc = board_facts.graphic_copper(root)
        self.assertEqual(set(gc.keys()), {"RF_FEED"})

    def test_no_net_returns_none_regardless_of_encoding(self):
        net_map = {"0": ""}
        self.assertIsNone(board_facts._resolve_net(["gr_line", ["net", "0"]], net_map))
        self.assertIsNone(board_facts._resolve_net(["gr_line", ["net", "0", ""]], net_map))
        self.assertIsNone(board_facts._resolve_net(["gr_line"], net_map))


class CollectBoardFactsEndToEndTests(unittest.TestCase):
    def test_full_board_produces_every_finding_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            pcb_path = Path(tmp) / "board.kicad_pcb"
            pcb_path.write_text(MULTI_PLY_BOARD, encoding="utf-8")
            facts = board_facts.collect_board_facts(pcb_path)

        rule_ids = {f["rule_id"] for f in facts.findings}
        self.assertEqual(
            rule_ids,
            {
                "BF-STACKUP-ER",
                "BF-STACKUP-TAND",
                "BF-STACKUP-MULTIPLY",
                "BF-GRAPHIC-COPPER",
                "BF-UNNETTED-COPPER",
                "BF-FP-COPPER-ORPHAN",
            },
        )
        # Impedance Control noted "Yes" on this board -> implausible epsilon_r is an error.
        er_finding = next(f for f in facts.findings if f["rule_id"] == "BF-STACKUP-ER")
        self.assertEqual(er_finding["severity"], "error")

    def test_malformed_or_missing_file_degrades_without_raising(self):
        facts = board_facts.collect_board_facts(Path("/does/not/exist.kicad_pcb"))
        self.assertEqual(facts.copper_layers, [])
        self.assertTrue(any("board facts unavailable" in w for w in facts.stackup_warnings))
        self.assertEqual(facts.findings, [])


if __name__ == "__main__":
    unittest.main()
