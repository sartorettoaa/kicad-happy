#!/usr/bin/env python3
"""Self-contained unittest suite for review_coverage.py.

Stdlib only (unittest + json + tempfile) — no pytest, no network, synthetic
component rows only.

Run directly:
    python3 test_review_coverage.py

Or via unittest discovery:
    python3 -m unittest skills.datasheets.scripts.test_review_coverage -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_coverage as rc  # noqa: E402


def _artifact(tmpdir: Path, name: str, components: list) -> Path:
    path = tmpdir / name
    path.write_text(json.dumps({"components": components}), encoding="utf-8")
    return path


class BuildRosterBucketingTests(unittest.TestCase):
    """Every component lands in exactly one of candidates/missing/excluded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_classic_ic_prefix_is_a_candidate(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "U1", "value": "KSZ9131RNXI", "footprint": "QFN-48",
             "mpn": "KSZ9131RNXI"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 1)
        cand = roster["candidates"][0]
        self.assertEqual(cand["ref"], "U1")
        self.assertEqual(cand["mpn"], "KSZ9131RNXI")
        self.assertEqual(cand["class"], "ic")
        self.assertEqual(cand["source"], str(artifact))
        self.assertIn("reason", cand)

    def test_broadened_prefixes_classify_as_expected(self):
        rows = [
            {"reference": "Q3", "value": "2N7002", "mpn": "2N7002,215"},
            {"reference": "T1", "value": "EP13", "mpn": "750315836"},
            {"reference": "Y2", "value": "12MHz", "mpn": "ABM8G-12.000MHZ-T"},
            {"reference": "J5", "value": "USB-C", "mpn": "USB4110-GF-A"},
            {"reference": "D4", "value": "TVS", "mpn": "SMAJ5.0A"},
            {"reference": "TVS1", "value": "TVS", "mpn": "PESD5V0X1BT"},
            {"reference": "Z1", "value": "Zener", "mpn": "BZT52C5V1"},
            {"reference": "L2", "value": "4.7uH", "mpn": "XFL4020-472MEC"},
            {"reference": "FB1", "value": "Ferrite", "mpn": "BLM18PG221SN1D"},
            {"reference": "ANT1", "value": "Antenna", "mpn": "W3012"},
            {"reference": "AE1", "value": "Antenna", "mpn": "2450AT18B100"},
            {"reference": "SC1", "value": "Supercap", "mpn": "DMF3Z5R5H105M3DTA0"},
            {"reference": "BAT1", "value": "Battery", "mpn": "CR2032"},
            {"reference": "S1", "value": "Switch", "mpn": "TL3342F160QG"},
            {"reference": "SW1", "value": "Switch", "mpn": "B3U-1000P"},
            {"reference": "K1", "value": "Relay", "mpn": "G5LE-14-DC12"},
        ]
        artifact = _artifact(self.tmpdir, "schematic.json", rows)
        roster = rc.build_roster([artifact])
        by_ref = {c["ref"]: c for c in roster["candidates"]}
        self.assertEqual(len(roster["candidates"]), len(rows))
        self.assertEqual(by_ref["Q3"]["class"], "transistor_switch")
        self.assertEqual(by_ref["T1"]["class"], "transformer_magnetics")
        self.assertEqual(by_ref["Y2"]["class"], "crystal_oscillator")
        self.assertEqual(by_ref["J5"]["class"], "connector")
        self.assertEqual(by_ref["D4"]["class"], "protection_diode")
        self.assertEqual(by_ref["TVS1"]["class"], "protection_diode")
        self.assertEqual(by_ref["Z1"]["class"], "protection_diode")
        self.assertEqual(by_ref["L2"]["class"], "power_inductor_ferrite")
        self.assertEqual(by_ref["FB1"]["class"], "power_inductor_ferrite")
        self.assertEqual(by_ref["ANT1"]["class"], "antenna_module")
        self.assertEqual(by_ref["AE1"]["class"], "antenna_module")
        self.assertEqual(by_ref["SC1"]["class"], "supercap_battery")
        self.assertEqual(by_ref["BAT1"]["class"], "supercap_battery")
        self.assertEqual(by_ref["S1"]["class"], "switch_other")
        self.assertEqual(by_ref["SW1"]["class"], "switch_other")
        self.assertEqual(by_ref["K1"]["class"], "switch_other")

    def test_sw_prefix_with_real_mpn_is_a_candidate_not_excluded(self):
        """Regression: the ref-prefix regex extracts the FULL leading letter
        run ("SW", not "S"), so a tactile switch referenced "SW1" must be
        classified via its own "SW" dict entry — not silently excluded
        because only the shorter "S" prefix was mapped."""
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "SW1", "value": "Tactile Switch", "footprint": "SW_PUSH_6mm",
             "mpn": "B3U-1000P"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 1)
        self.assertEqual(roster["summary"]["excluded"], 0)
        cand = roster["candidates"][0]
        self.assertEqual(cand["ref"], "SW1")
        self.assertEqual(cand["mpn"], "B3U-1000P")
        self.assertEqual(cand["class"], "switch_other")

    def test_sw_prefix_with_no_mpn_field_is_missing_coverage(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "SW2", "value": "Tactile Switch", "footprint": "SW_PUSH_6mm"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 0)
        self.assertEqual(roster["summary"]["missing"], 1)
        gap = roster["missing"][0]
        self.assertEqual(gap["ref"], "SW2")
        self.assertEqual(gap["class"], "switch_other")

    def test_k_prefix_relay_with_real_mpn_is_a_candidate(self):
        """"K" is the standard ANSI reference prefix for a relay; the
        switch_other class's own docstring names relays, so a relay MPN
        must land in candidates, not excluded/generic_value."""
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "K1", "value": "Relay SPDT", "mpn": "G5LE-14-DC12"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 1)
        cand = roster["candidates"][0]
        self.assertEqual(cand["ref"], "K1")
        self.assertEqual(cand["class"], "switch_other")

    def test_generic_e_prefix_is_not_antenna_without_context(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "E1", "value": "some_weird_part", "mpn": "ACME-1234"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 0)
        self.assertEqual(roster["excluded"][0]["ref"], "E1")

    def test_e_prefix_is_antenna_when_value_says_so(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "E2", "value": "Antenna 2.4GHz", "mpn": "2450AT18A100"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 1)
        self.assertEqual(roster["candidates"][0]["class"], "antenna_module")

    def test_dnp_component_is_excluded_even_if_otherwise_a_candidate(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "U9", "value": "LM358", "mpn": "LM358DR", "dnp": True},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 0)
        self.assertEqual(roster["excluded"][0]["reason"], "dnp")

    def test_mechanical_testpoint_fiducial_are_excluded_with_distinct_reasons(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "MH1", "value": "MountingHole", "footprint": "MountingHole_3.2mm"},
            {"reference": "FID1", "value": "Fiducial", "footprint": "Fiducial_1mm"},
            {"reference": "TP1", "value": "TestPoint", "footprint": "TestPoint_Pad_1mm"},
        ])
        roster = rc.build_roster([artifact])
        reasons = {e["ref"]: e["reason"] for e in roster["excluded"]}
        self.assertEqual(reasons["MH1"], "mechanical")
        self.assertEqual(reasons["FID1"], "fiducial")
        self.assertEqual(reasons["TP1"], "testpoint")
        self.assertEqual(roster["summary"]["excluded"], 3)

    def test_placeholder_mpn_on_relevant_class_is_excluded_not_missing(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "U5", "value": "TBD", "mpn": "TBD"},
            {"reference": "U6", "value": "NA", "mpn": "NA"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 0)
        reasons = {e["ref"]: e["reason"] for e in roster["excluded"]}
        self.assertIn("placeholder", reasons["U5"])
        self.assertIn("placeholder", reasons["U6"])

    def test_relevant_class_with_no_mpn_field_is_missing_coverage(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "Y3", "value": "16MHz crystal", "footprint": "HC49"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 0)
        self.assertEqual(roster["summary"]["missing"], 1)
        gap = roster["missing"][0]
        self.assertEqual(gap["ref"], "Y3")
        self.assertEqual(gap["class"], "crystal_oscillator")

    def test_generic_passive_without_mpn_is_excluded_as_generic_value(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "R10", "value": "10k", "footprint": "R_0402"},
            {"reference": "C20", "value": "100nF", "footprint": "C_0402"},
        ])
        roster = rc.build_roster([artifact])
        reasons = {e["ref"]: e["reason"] for e in roster["excluded"]}
        self.assertEqual(reasons["R10"], "generic_value")
        self.assertEqual(reasons["C20"], "generic_value")

    def test_non_relevant_class_with_real_mpn_is_not_silently_dropped(self):
        """A precision resistor with a real MPN keeps that MPN in the reason,
        even though it is still excluded from the datasheet-class roster."""
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "R11", "value": "10k 0.01%", "mpn": "PFC-W0402R10K0BST1"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 0)
        entry = roster["excluded"][0]
        self.assertIn("PFC-W0402R10K0BST1", entry["reason"])
        self.assertNotEqual(entry["reason"], "generic_value")

    def test_manufacturer_field_is_never_used_as_mpn(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "U20", "value": "part", "manufacturer": "Texas Instruments"},
        ])
        roster = rc.build_roster([artifact])
        self.assertEqual(roster["summary"]["candidates"], 0)
        self.assertEqual(roster["summary"]["missing"], 1)

    def test_robust_mpn_key_spellings(self):
        rows = [
            {"reference": "U1", "value": "a", "MPN": "ABC123"},
            {"reference": "U2", "value": "b", "Manufacturer Part Number": "DEF456"},
            {"reference": "U3", "value": "c", "part_number": "GHI789"},
        ]
        artifact = _artifact(self.tmpdir, "schematic.json", rows)
        roster = rc.build_roster([artifact])
        by_ref = {c["ref"]: c["mpn"] for c in roster["candidates"]}
        self.assertEqual(by_ref["U1"], "ABC123")
        self.assertEqual(by_ref["U2"], "DEF456")
        self.assertEqual(by_ref["U3"], "GHI789")

    def test_dedupes_by_ref_and_mpn_across_artifacts(self):
        a1 = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "U1", "value": "x", "mpn": "ABC123"},
        ])
        a2 = _artifact(self.tmpdir, "pcb_extra.json", [
            {"reference": "U1", "value": "x", "mpn": "ABC123"},
        ])
        roster = rc.build_roster([a1, a2])
        self.assertEqual(roster["summary"]["candidates"], 1)

    def test_bom_fallback_when_components_key_is_absent(self):
        path = self.tmpdir / "bom_only.json"
        path.write_text(json.dumps({"bom": [
            {"reference": "U1", "value": "x", "mpn": "ABC123", "manufacturer": "Acme"},
        ]}), encoding="utf-8")
        roster = rc.build_roster([path])
        self.assertEqual(roster["summary"]["candidates"], 1)
        self.assertEqual(roster["candidates"][0]["mpn"], "ABC123")

    def test_malformed_artifact_is_recorded_as_unreadable_not_a_crash(self):
        bad = self.tmpdir / "bad.json"
        bad.write_text("{ not valid json", encoding="utf-8")
        roster = rc.build_roster([bad])
        self.assertEqual(roster["summary"]["total_components"], 0)
        self.assertIn(str(bad), roster["unreadable_sources"])

    def test_bucket_sizes_sum_to_total_components(self):
        rows = [
            {"reference": "U1", "value": "x", "mpn": "ABC123"},
            {"reference": "U2", "value": "x", "mpn": "TBD"},
            {"reference": "Y1", "value": "x"},
            {"reference": "R1", "value": "10k"},
            {"reference": "TP2", "value": "tp"},
        ]
        artifact = _artifact(self.tmpdir, "schematic.json", rows)
        roster = rc.build_roster([artifact])
        s = roster["summary"]
        self.assertEqual(s["total_components"], len(rows))
        self.assertEqual(s["candidates"] + s["missing"] + s["excluded"], len(rows))

    def test_roster_json_is_deterministic_across_runs(self):
        rows = [
            {"reference": "U2", "value": "x", "mpn": "BBB"},
            {"reference": "U1", "value": "x", "mpn": "AAA"},
        ]
        artifact = _artifact(self.tmpdir, "schematic.json", rows)
        text1 = rc._dumps(rc.build_roster([artifact]))
        text2 = rc._dumps(rc.build_roster([artifact]))
        self.assertEqual(text1, text2)
        # Candidate ordering is by (ref, mpn), independent of input order.
        self.assertLess(text1.index('"U1"'), text1.index('"U2"'))


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_writes_to_explicit_output_path(self):
        artifact = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "U1", "value": "x", "mpn": "ABC123"},
        ])
        out_path = self.tmpdir / "roster.json"
        rc_exit = rc.main([str(artifact), "--output", str(out_path)])
        self.assertEqual(rc_exit, 0)
        self.assertTrue(out_path.is_file())
        data = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["candidates"], 1)

    def test_cli_accepts_multiple_artifact_paths(self):
        a1 = _artifact(self.tmpdir, "schematic.json", [
            {"reference": "U1", "value": "x", "mpn": "ABC123"},
        ])
        a2 = _artifact(self.tmpdir, "schematic2.json", [
            {"reference": "U2", "value": "x", "mpn": "DEF456"},
        ])
        out_path = self.tmpdir / "roster.json"
        rc_exit = rc.main([str(a1), str(a2), "--output", str(out_path)])
        self.assertEqual(rc_exit, 0)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["candidates"], 2)

    def test_cli_errors_on_missing_input_path(self):
        missing = self.tmpdir / "nope.json"
        rc_exit = rc.main([str(missing)])
        self.assertEqual(rc_exit, 2)


if __name__ == "__main__":
    unittest.main()
