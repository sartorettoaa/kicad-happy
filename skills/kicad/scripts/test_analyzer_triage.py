#!/usr/bin/env python3
"""Unit tests for analyzer_triage.py.

Self-contained: inputs are hand-built finding dicts and a ``TriageContext``
(no analyzer run, no board file, no network), matching the module's own
contract of pure functions over plain dicts.

Run from the repo root:
    python -m unittest skills.kicad.scripts.test_analyzer_triage
    python -m unittest skills.kicad.scripts.test_analyzer_triage -v

Or directly:
    python skills/kicad/scripts/test_analyzer_triage.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# analyzer_triage.py lives next to this file — insert that dir on sys.path so
# the local (unqualified) import below resolves regardless of how this test
# module itself was addressed (flat script vs. dotted module path).
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import analyzer_triage as at  # noqa: E402
import board_facts  # noqa: E402
import sexp_parser  # noqa: E402


def _finding(rule_id: str, severity: str = "error", **extra) -> dict:
    f = {"rule_id": rule_id, "severity": severity, "summary": f"{rule_id} test finding"}
    f.update(extra)
    return f


class TriageContextFromDictTests(unittest.TestCase):
    def test_none_input_falls_back_to_cant_judge_defaults(self):
        ctx = at.TriageContext.from_dict(None)
        self.assertEqual(ctx.drc_summary, {})
        self.assertEqual(ctx.graphic_copper_nets, set())
        self.assertFalse(ctx.stackup_multi_ply)
        self.assertEqual(ctx.mousebite_refs, set())

    def test_populated_dict_is_normalized(self):
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
    """KO-001: bounding-box keepout test — only a clean real DRC run can
    disprove it; the heuristic alone never downgrades."""

    def test_no_drc_data_stays_review_not_downgraded(self):
        f = _finding("KO-001")
        out = at.triage_findings([f], at.TriageContext())[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "error")  # untouched
        self.assertNotIn("original_severity", out)

    def test_nonzero_keepout_count_stays_review(self):
        f = _finding("KO-001")
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 2})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)

    def test_zero_keepout_count_with_drc_ran_is_likely_fp(self):
        f = _finding("KO-001")
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)
        self.assertEqual(out["severity"], "info")
        self.assertEqual(out["original_severity"], "error")

    def test_zero_keepout_without_drc_ran_is_not_clean(self):
        """A drc_summary with a zero count but no (or false) ``drc_ran`` means
        DRC never actually ran (or failed) — must not be treated as 'clean'."""
        f = _finding("KO-001")
        ctx = at.TriageContext(drc_summary={"drc_keepout": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)

    def test_zero_keepout_with_drc_ran_false_is_not_clean(self):
        """``drc_ran`` explicitly False (DRC attempted but failed/timed out)
        must not be treated as 'clean' either, even with a zero count."""
        f = _finding("KO-001")
        ctx = at.TriageContext(drc_summary={"drc_ran": False, "drc_keepout": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)


class Rt001Tests(unittest.TestCase):
    """RT-001: unrouted/short net — graphic copper or a clean DRC unconnected
    count are both acceptable evidence of a false positive."""

    def test_net_matches_graphic_copper_is_likely_fp(self):
        f = _finding("RT-001", nets=["RF_FEED"])
        ctx = at.TriageContext(graphic_copper_nets={"RF_FEED"})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)
        self.assertIn("graphic copper", out["triage_reason"])
        self.assertEqual(out["severity"], "info")
        self.assertEqual(out["original_severity"], "error")

    def test_net_not_in_graphic_copper_is_unaffected_by_other_nets(self):
        f = _finding("RT-001", nets=["SPI_CLK"])
        ctx = at.TriageContext(graphic_copper_nets={"RF_FEED"})
        out = at.triage_findings([f], ctx)[0]
        self.assertNotEqual(out["triage"], at.LIKELY_FP)

    def test_drc_unconnected_zero_is_likely_fp(self):
        f = _finding("RT-001", nets=["SPI_CLK"])
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_unconnected": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_drc_unconnected_zero_without_drc_ran_stays_real(self):
        """P1 regression: an absent/failed DRC must never supply a zero count
        that looks like '0 unconnected' evidence. Without ``drc_ran`` true,
        RT-001 must not be downgraded even though the count key reads 0."""
        f = _finding("RT-001", nets=["SPI_CLK"])
        ctx = at.TriageContext(drc_summary={"drc_unconnected": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REAL)
        self.assertEqual(out["severity"], "error")

    def test_drc_unconnected_zero_with_drc_ran_false_stays_real(self):
        f = _finding("RT-001", nets=["SPI_CLK"])
        ctx = at.TriageContext(drc_summary={"drc_ran": False, "drc_unconnected": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REAL)

    def test_no_corroborating_evidence_defaults_to_real(self):
        f = _finding("RT-001", nets=["SPI_CLK"])
        out = at.triage_findings([f], at.TriageContext())[0]
        self.assertEqual(out["triage"], at.REAL)
        self.assertEqual(out["severity"], "error")


class Su001Tests(unittest.TestCase):
    """SU-001: stackup/layer-role finding — a multi-ply (addsublayer) board
    means the analyzer's own dielectric-height parse is corrupted, but that
    is never strong enough evidence to call a finding a false positive
    outright (a genuine layer-role/thickness problem can still exist on a
    multi-ply board)."""

    def test_multi_ply_stackup_stays_review_not_fp(self):
        f = _finding("SU-001", severity="warning")
        ctx = at.TriageContext(stackup_multi_ply=True)
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "warning")
        self.assertNotIn("original_severity", out)
        self.assertIn("board_facts", out["triage_reason"])

    def test_single_ply_stackup_stays_review_and_keeps_severity(self):
        f = _finding("SU-001", severity="warning")
        ctx = at.TriageContext(stackup_multi_ply=False)
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "warning")
        self.assertNotIn("original_severity", out)

    def test_genuine_layer_role_claim_not_downgraded_by_multi_ply(self):
        """Regression: a real copper-layer-role finding (not a stackup
        parse/ply artifact) must stay at full severity under REVIEW even on
        a multi-ply board."""
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
    """PM-001/PM-002: mechanical placement near the panel edge."""

    def test_known_mousebite_ref_is_likely_fp(self):
        f = _finding("PM-001", components=["H1"])
        ctx = at.TriageContext(mousebite_refs={"H1"})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_fiducial_prefix_is_likely_fp_even_without_explicit_ref(self):
        f = _finding("PM-001", components=["FID1"])
        out = at.triage_findings([f], at.TriageContext())[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_ordinary_reference_is_not_downgraded_by_prefix_rule(self):
        f = _finding("PM-001", components=["U3"])
        out = at.triage_findings([f], at.TriageContext())[0]
        self.assertNotEqual(out["triage"], at.LIKELY_FP)

    def test_pm001_clean_courtyard_drc_is_likely_fp(self):
        f = _finding("PM-001", components=["U3"])
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_courtyard": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.LIKELY_FP)

    def test_pm001_clean_courtyard_without_drc_ran_stays_real(self):
        f = _finding("PM-001", components=["U3"])
        ctx = at.TriageContext(drc_summary={"drc_courtyard": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertNotEqual(out["triage"], at.LIKELY_FP)

    def test_pm002_clean_edge_drc_stays_review_not_fp(self):
        """A clean DRC edge-clearance pass is good evidence but a generic
        fixed guideline distance can still be stricter than DRC — never a
        definitive false positive, only 'review'."""
        f = _finding("PM-002", components=["U3"])
        ctx = at.TriageContext(drc_summary={"drc_ran": True, "drc_edge": 0})
        out = at.triage_findings([f], ctx)[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertEqual(out["severity"], "error")  # review keeps severity
        self.assertNotIn("original_severity", out)

    def test_pm002_without_evidence_has_no_verdict_from_this_rule(self):
        f = _finding("PM-002", components=["U3"])
        out = at.triage_findings([f], at.TriageContext())[0]
        self.assertEqual(out["triage"], at.REAL)


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


class TriageFindingsContractTests(unittest.TestCase):
    def test_never_discards_or_reorders_findings(self):
        findings = [_finding("XX-001"), _finding("KO-001"), _finding("YY-002")]
        out = at.triage_findings(findings, at.TriageContext())
        self.assertEqual(len(out), 3)
        self.assertEqual([f["rule_id"] for f in out], ["XX-001", "KO-001", "YY-002"])

    def test_unknown_rule_id_defaults_to_real_and_is_otherwise_untouched(self):
        f = _finding("ZZ-999", severity="warning", nets=["A"])
        out = at.triage_findings([f], at.TriageContext())[0]
        self.assertEqual(out["triage"], at.REAL)
        self.assertEqual(out["severity"], "warning")
        self.assertNotIn("triage_reason", out)
        self.assertNotIn("original_severity", out)

    def test_all_other_keys_preserved_verbatim(self):
        f = _finding("YY-002", severity="warning", evidence="E", recommendation="R", refs=["r1"], nets=["N"])
        out = at.triage_findings([f], at.TriageContext())[0]
        for key in ("evidence", "recommendation", "refs", "nets"):
            self.assertEqual(out[key], f[key])

    def test_original_findings_list_and_dicts_are_not_mutated(self):
        f = _finding("KO-001")
        findings = [f]
        at.triage_findings(findings, at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 0}))
        self.assertNotIn("triage", f)
        self.assertEqual(f["severity"], "error")

    def test_review_verdict_never_sets_original_severity(self):
        f = _finding("SU-001", severity="warning")
        out = at.triage_findings([f], at.TriageContext(stackup_multi_ply=False))[0]
        self.assertEqual(out["triage"], at.REVIEW)
        self.assertNotIn("original_severity", out)

    def test_non_dict_entries_pass_through_unchanged(self):
        out = at.triage_findings(["not-a-dict"], at.TriageContext())
        self.assertEqual(out, ["not-a-dict"])


class TriageTableTests(unittest.TestCase):
    def test_rollup_counts_and_sample_reason(self):
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
    def test_filters_to_error_severity_findings_not_downgraded(self):
        findings = at.triage_findings(
            [
                _finding("KO-001", severity="error"),    # -> likely_fp (clean DRC)
                _finding("XX-001", severity="error"),    # -> real, stays error
                _finding("YY-002", severity="warning"),  # -> real, not error severity
            ],
            at.TriageContext(drc_summary={"drc_ran": True, "drc_keepout": 0}),
        )
        mf = at.must_fix(findings)
        self.assertEqual([f["rule_id"] for f in mf], ["XX-001"])


if __name__ == "__main__":
    unittest.main()
