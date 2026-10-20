#!/usr/bin/env python3
"""Unit tests for toolkit/cli.py — argument parsing and stdout-JSON contract.

Run with:
    python3 skills/kicad/toolkit/test_cli.py
    python3 -m unittest skills/kicad/toolkit/test_cli.py
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from toolkit.cli import main  # noqa: E402

FIXTURE = os.path.join(SCRIPT_DIR, "fixtures", "sample.kicad_pcb")


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


class TestCli(unittest.TestCase):
    def test_no_out_flag_exists(self):
        # Contract: every command prints to stdout; there must be no flag
        # that writes a result file anywhere in the parser tree.
        from toolkit.cli import build_parser
        parser = build_parser()
        subparsers_action = next(a for a in parser._actions if a.choices)
        all_parsers = [parser] + list(subparsers_action.choices.values())
        for p in all_parsers:
            dests = {a.dest for a in p._actions}
            self.assertNotIn("out", dests)
            self.assertNotIn("output", dests)

    def test_stackup_prints_json_to_stdout(self):
        rc, out = _run(["--pcb", FIXTURE, "stackup"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["board_thickness_mm"], 1.6)

    def test_components(self):
        rc, out = _run(["--pcb", FIXTURE, "components"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual([r["reference"] for r in data], ["R1", "R2"])

    def test_components_ref_filter(self):
        rc, out = _run(["--pcb", FIXTURE, "components", "--ref", "R2"])
        data = json.loads(out)
        self.assertEqual([r["reference"] for r in data], ["R2"])

    def test_net(self):
        rc, out = _run(["--pcb", FIXTURE, "net", "NET1"])
        data = json.loads(out)
        self.assertAlmostEqual(data["length_mm"], 15.0, places=6)

    def test_keepouts(self):
        rc, out = _run(["--pcb", FIXTURE, "keepouts"])
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "KEEPOUT1")

    def test_edge(self):
        rc, out = _run(["--pcb", FIXTURE, "edge", "NET1"])
        data = json.loads(out)
        self.assertAlmostEqual(data["min_mm"], 4.05, places=6)

    def test_clearance(self):
        # See test_queries.TestQueries.test_net_clearance_stdlib_exact: the
        # tightest copper-to-copper gap on this fixture is the GND via vs.
        # the NET1 track (0.075 mm after subtracting the track's half-width),
        # not the 0.10 mm pad/pad centerline gap.
        rc, out = _run(["--pcb", FIXTURE, "clearance", "NET1", "GND", "--layer", "F.Cu"])
        data = json.loads(out)
        self.assertAlmostEqual(data["min_mm"], 0.075, places=6)

    def test_missing_pcb_exits(self):
        with self.assertRaises(SystemExit):
            main(["stackup"])

    def test_overlap_from_analyzer_json(self):
        payload = {"footprints": [
            {"reference": "U1", "x": 0.0, "y": 0.0, "courtyard": {"min_x": -1, "min_y": -1, "max_x": 1, "max_y": 1}},
            {"reference": "U2", "x": 5.0, "y": 0.0, "courtyard": {"min_x": 4, "min_y": -1, "max_x": 6, "max_y": 1}},
        ]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(payload, fh)
            path = fh.name
        try:
            rc, out = _run(["overlap", "--analyzer-json", path, "U1", "U2"])
            data = json.loads(out)
            self.assertAlmostEqual(data["gap_mm"], 3.0, places=6)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
