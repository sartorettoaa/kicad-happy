#!/usr/bin/env python3
"""Unit tests for analyzer_triage.py.

Inputs are hand-built finding dicts (no analyzer run, no board file) per the
module's own contract: pure functions over plain dicts.

Run with:
    python3 -m unittest discover -s skills/kicad/scripts/tests -v
    python3 skills/kicad/scripts/tests/test_analyzer_triage_package.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import analyzer_triage as at
import board_facts
import sexp_parser


def _finding(rule_id: str, severity: str = "error", **extra) -> dict:
    f = {"rule_id": rule_id, "severity": severity, "summary": f"{rule_id} test finding"}
    f.update(extra)
    return f


class TriageContextTests(unittest.TestCase):
    def test_from_dict_defaults(self):
        ctx = at.TriageContext.from_dict(None)
        self.assertEqual(ctx.drc_summary, {})
        self.assertEqual(ctx.graphic_copper_nets, set())
        self.assertFalse(ctx.stackup_multi_ply)
        self.assertEqual(ctx.mousebite_refs, set())

    def test_from_dict_populated(self):
        ctx = at.TriageContext.from_dict({
            "drc_summary": {"drc_geometric": 5, "drc_keepout": 0},
            "graphic_copper_nets": ["RF_FEED"],
            "stackup_multi_ply": True,
            "mousebite_refs": ["H1", "H2"],
            "unknown_key": "ignored",
        })
        self.assertEqual(ctx.drc_summary["drc_keepout"], 0)
        self.assertEqual(ctx.graphic_copper_nets, {"RF_FEED"})
        self.assertTrue(ctx.stackup_multi_ply)
        self.assertEqual(ctx.mousebite_refs, {"H1", "H2"})


class Ko001Tests(unittest.TestCase):
    def test_no_drc_data_never_downgrades_on_bbox_alone(self):
        """KO-001 must not be called a false positive from the bbox heuristic
        alone — only a clean DRC run can disprove it."""
        f = _finding("KO-001")
        ctx = at.TriageContext()  # no drc_summary at all
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "error")  # untouched — review keeps severity
        self.assertNotIn("original_severity", out)

    def test_drc_keepout_nonzero_stays_review(self):
        f = _finding("KO-001")
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 2})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)

    def test_drc_keepout_zero_is_likely_fp(self):
        f = _finding("KO-001")
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)
        self.assertEqual(out["severity"], "info")
        self.assertEqual(out["original_severity"], "error")

    def test_drc_keepout_zero_without_drc_ran_stays_review(self):
        """A zero count without ``drc_ran`` true means DRC never actually
        ran (or failed) — must not be treated as 'clean'."""
        f = _finding("KO-001")
        ctx = at.TriageContext(drc_summary={"drc_keepout": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)


class Rt001Tests(unittest.TestCase):
    def test_graphic_copper_net_match_is_likely_fp(self):
        f = _finding("RT-001", nets=["RF_FEED"])
        ctx = at.TriageContext(graphic_copper_nets={"RF_FEED"})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)
        self.assertIn("graphic copper", out["triage_reason"])

    def test_drc_unconnected_zero_is_likely_fp(self):
        f = _finding("RT-001", nets=["SPI_CLK"])
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_unconnected": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_drc_unconnected_zero_without_drc_ran_stays_real(self):
        """P1 regression: an absent/failed DRC must never supply a zero
        count that looks like '0 unconnected' evidence."""
        f = _finding("RT-001", nets=["SPI_CLK"])
        ctx = at.TriageContext(drc_summary={"drc_unconnected": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REAL)
        self.assertEqual(out["severity"], "error")

    def test_no_evidence_is_real(self):
        f = _finding("RT-001", nets=["SPI_CLK"])
        ctx = at.TriageContext()
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REAL)
        self.assertEqual(out["severity"], "error")


class Su001Tests(unittest.TestCase):
    def test_multi_ply_stackup_stays_review_not_fp(self):
        """Multi-ply stackup alone must never downgrade a finding outright:
        it only means the analyzer's own stackup parse is suspect, not that
        the finding is wrong — the safe default is REVIEW, never
        LIKELY_FP, with severity untouched."""
        f = _finding("SU-001", severity="warning")
        ctx = at.TriageContext(stackup_multi_ply=True)
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "warning")
        self.assertNotIn("original_severity", out)
        self.assertIn("board_facts", out["triage_reason"])

    def test_single_ply_stays_review(self):
        f = _finding("SU-001", severity="warning")
        ctx = at.TriageContext(stackup_multi_ply=False)
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "warning")

    def test_genuine_layer_role_claim_not_downgraded_by_multi_ply(self):
        """Regression: a real copper-layer-role finding (not a stackup
        parse/ply artifact) must stay at full severity under REVIEW even on
        a multi-ply board — multi-ply evidence concerns dielectric height,
        not layer role/order, and must never silently mute it."""
        f = _finding(
            "SU-001", severity="error",
            summary="In2.Cu is configured as a signal layer but routed as a solid ground plane",
        )
        ctx = at.TriageContext(stackup_multi_ply=True)
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "error")
        self.assertNotIn("original_severity", out)


class PmEdgeTests(unittest.TestCase):
    def test_mousebite_ref_is_likely_fp(self):
        f = _finding("PM-001", components=["H1"])
        ctx = at.TriageContext(mousebite_refs={"H1"})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_fiducial_prefix_is_likely_fp_without_explicit_ref(self):
        f = _finding("PM-001", components=["FID1"])
        ctx = at.TriageContext()
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_pm002_clean_drc_edge_stays_review_not_fp(self):
        f = _finding("PM-002", components=["U3"])
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_edge": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "error")

    def test_pm001_clean_drc_courtyard_is_likely_fp(self):
        f = _finding("PM-001", components=["U3"])
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_courtyard": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_pm001_clean_drc_courtyard_without_drc_ran_stays_real(self):
        f = _finding("PM-001", components=["U3"])
        ctx = at.TriageContext(drc_summary={"drc_courtyard": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertNotEqual(out["triage"], at.LIKELY_FP)


class TriageFindingsContractTests(unittest.TestCase):
    def test_never_discards_or_reorders(self):
        findings = [_finding("XX-001"), _finding("KO-001"), _finding("YY-002")]
        out = at.triage_findings(findings, at.TriageContext())
        self.assertEqual(len(out), 3)
        self.assertEqual([f["rule_id"] for f in out], ["XX-001", "KO-001", "YY-002"])

    def test_unknown_rule_defaults_to_real_and_is_untouched(self):
        f = _finding("ZZ-999", severity="warning", nets=["A"])
        out = at.triage_findings([f], at.TriageContext())[0]
        self.assertEqual(out["triage"], at.REAL)
        self.assertEqual(out["severity"], "warning")
        self.assertNotIn("triage_reason", out)
        self.assertNotIn("original_severity", out)

    def test_input_list_not_mutated(self):
        f = _finding("KO-001")
        findings = [f]
        at.triage_findings(findings, at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 0}))
        self.assertNotIn("triage", f)  # original dict untouched — triage_findings works on copies
        self.assertEqual(f["severity"], "error")


class BoardFactsIntegrationTests(unittest.TestCase):
    """RT-001 fed real ``board_facts`` output (not a hand-typed set): proves
    the legacy id-only ``(net 3)`` graphic copper on a board that only
    declares the name via the top-level net table resolves to "RF_FEED" and
    lines up with an analyzer finding reported against that same net name."""

    BOARD = """
        (kicad_pcb (version 20240101) (generator pcbnew)
          (net 0 "")
          (net 3 "RF_FEED")
          (gr_line (start 0 0) (end 5 0) (layer "F.Cu") (net 3))
        )
    """

    def test_legacy_net_id_resolves_and_matches_rt001_finding(self):
        root = sexp_parser.parse(self.BOARD)
        facts = board_facts.BoardFacts(graphic_copper=board_facts.graphic_copper(root))
        nets = board_facts.nets_with_extra_copper(facts)
        self.assertEqual(nets, {"RF_FEED"})

        f = _finding("RT-001", nets=["RF_FEED"])
        ctx = at.TriageContext(graphic_copper_nets=nets)
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)
        self.assertIn("graphic copper", out["triage_reason"])


class TriageTableTests(unittest.TestCase):
    def test_rollup_counts(self):
        findings = at.triage_findings(
            [
                _finding("KO-001"),
                _finding("KO-001"),
                _finding("RT-001", nets=["A"]),
            ],
            at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 0}),
        )
        table = at.triage_table(findings)
        ko_row = next(r for r in table if r["rule_id"] == "KO-001")
        self.assertEqual(ko_row["count"], 2)
        self.assertEqual(ko_row[at.LIKELY_FP], 2)
        self.assertTrue(ko_row["reason"])


class MustFixTests(unittest.TestCase):
    def test_filters_to_error_severity_non_fp(self):
        findings = at.triage_findings(
            [
                _finding("KO-001", severity="error"),  # -> likely_fp (drc clean)
                _finding("XX-001", severity="error"),   # -> real, stays error
                _finding("YY-002", severity="warning"),  # -> real, but not error severity
            ],
            at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 0}),
        )
        mf = at.must_fix(findings)
        self.assertEqual([f["rule_id"] for f in mf], ["XX-001"])


class CliTests(unittest.TestCase):
    SCRIPT = str(SCRIPTS_DIR / "analyzer_triage.py")

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--findings", result.stdout)

    def test_end_to_end_with_context_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings_path = Path(tmp) / "findings.json"
            context_path = Path(tmp) / "context.json"
            out_path = Path(tmp) / "triaged.json"

            findings_path.write_text(json.dumps({
                "findings": [
                    _finding("KO-001"),
                    _finding("RT-001", nets=["RF_FEED"]),
                ]
            }), encoding="utf-8")
            context_path.write_text(json.dumps({
                "drc_summary": {"drc_ran": True, "drc_keepout": 0},
                "graphic_copper_nets": ["RF_FEED"],
            }), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, self.SCRIPT,
                 "--findings", str(findings_path),
                 "--context", str(context_path),
                 "--output", str(out_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.is_file())

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["findings"]), 2)
            self.assertTrue(all(f["triage"] == at.LIKELY_FP for f in payload["findings"]))
            self.assertIn("triage_table", payload)
            self.assertIn("must_fix", payload)

    def test_end_to_end_missing_drc_ran_does_not_downgrade(self):
        """P1 regression, exercised through the CLI entry point: a
        ``drc_summary`` with zero counts but no ``drc_ran`` (an absent or
        failed DRC run) must leave KO-001/RT-001 at full severity."""
        with tempfile.TemporaryDirectory() as tmp:
            findings_path = Path(tmp) / "findings.json"
            context_path = Path(tmp) / "context.json"
            out_path = Path(tmp) / "triaged.json"

            findings_path.write_text(json.dumps({
                "findings": [
                    _finding("KO-001"),
                    _finding("RT-001", nets=["SPI_CLK"]),
                ]
            }), encoding="utf-8")
            context_path.write_text(json.dumps({
                "drc_summary": {"drc_keepout": 0, "drc_unconnected": 0},
            }), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, self.SCRIPT,
                 "--findings", str(findings_path),
                 "--context", str(context_path),
                 "--output", str(out_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            ko = next(f for f in payload["findings"] if f["rule_id"] == "KO-001")
            rt = next(f for f in payload["findings"] if f["rule_id"] == "RT-001")
            self.assertEqual(ko["triage"], at.REVIEW)
            self.assertEqual(rt["triage"], at.REAL)
            self.assertEqual(ko["severity"], "error")
            self.assertEqual(rt["severity"], "error")

    def test_bare_findings_array_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings_path = Path(tmp) / "findings.json"
            findings_path.write_text(json.dumps([_finding("XX-001")]), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, self.SCRIPT, "--findings", str(findings_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["findings"][0]["triage"], at.REAL)


if __name__ == "__main__":
    unittest.main()
