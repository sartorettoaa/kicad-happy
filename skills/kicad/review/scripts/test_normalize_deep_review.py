#!/usr/bin/env python3
"""Unit tests for normalize_deep_review.py.

Isolated: no fixtures outside this file, no network, no LLM/Claude
invocation. Exercises the adapter (plugin-style finding -> canonical
deep_review schema shape), the quarantine/refutation/not_verified
disposition rules, round-trip schema validity against the real
deep_review.schema.json + _mini_jsonschema, and the synthesis-artifact
validator (schema + finding_id cross-reference, read-only).

Run with:
    python3 skills/kicad/review/scripts/test_normalize_deep_review.py
    python3 -m unittest skills/kicad/review/scripts/test_normalize_deep_review.py
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import normalize_deep_review as ndr               # noqa: E402
from _mini_jsonschema import iter_errors          # noqa: E402


DEEP_REVIEW_SCHEMA = json.loads(ndr.DEEP_REVIEW_SCHEMA_PATH.read_text())
SYNTHESIS_SCHEMA = json.loads(ndr.SYNTHESIS_SCHEMA_PATH.read_text())


def _schema_errors(doc, schema):
    return [e.message for e in iter_errors(doc, schema)]


class AdaptFindingTests(unittest.TestCase):
    def test_good_finding_is_kept_and_normalized(self):
        raw = {
            "severity": "error",
            "rule_id": "RF-SWITCH-POWER",
            "domain": "rf",
            "summary": "U30 RF switch exceeds its power rating under worst-case TX burst",
            "refs": ["U30"],
            "nets": ["/LTE Modem/LTE_ANT"],
            "evidence": "SKY13373 datasheet: max input power +33 dBm; GSM burst can reach +35 dBm",
            "computation": "35 dBm - 33 dBm = 2 dB over rating",
            "recommendation": "Add 3 dB pad or use a higher-rated switch",
            "confidence": "confirmed",
        }
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNone(reason)
        self.assertEqual(finding["detector"], "deep_review")
        self.assertEqual(finding["category"], "rf")
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["confidence"], "high")
        self.assertEqual(finding["recommendation"], "Add 3 dB pad or use a higher-rated switch")
        self.assertEqual(finding["evidence"]["components"], ["U30"])
        self.assertEqual(finding["evidence"]["nets"], ["/LTE Modem/LTE_ANT"])
        comp = finding["evidence"]["computation"]
        self.assertIn("SKY13373 datasheet", comp["result"])
        self.assertIn("35 dBm - 33 dBm", comp["result"])
        self.assertNotIn("datasheet", finding["evidence"])  # no fabricated structured datasheet entry
        # original is preserved verbatim, nothing silently dropped
        self.assertEqual(finding["provenance"]["raw"], raw)

    def test_category_derived_from_rule_id_when_no_domain_or_category(self):
        raw = {
            "rule_id": "PP-001",
            "summary": "x",
            "refs": ["U1"],
            "evidence": "something with a computation",
        }
        finding, _ = ndr.adapt_finding(raw)
        self.assertEqual(finding["category"], "pp")

    def test_structured_datasheet_shortcut_fields_are_used_directly(self):
        raw = {
            "severity": "warning",
            "summary": "Input cap below datasheet minimum",
            "refs": ["C12"],
            "mpn": "TPS61023",
            "quote": "minimum input capacitance 4.7 uF",
            "page": 7,
        }
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNone(reason)
        self.assertEqual(finding["evidence"]["datasheet"],
                         [{"mpn": "TPS61023", "quote": "minimum input capacitance 4.7 uF", "page": 7}])

    def test_missing_anchor_is_quarantined_not_dropped(self):
        raw = {"severity": "warning", "summary": "vague concern",
               "evidence": "computation: some arithmetic with numbers"}
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNotNone(reason)
        self.assertIn("no design anchor", reason)
        self.assertEqual(finding["quarantine_reason"], reason)
        self.assertEqual(finding["provenance"]["raw"], raw)

    def test_missing_evidence_source_is_quarantined_not_dropped(self):
        raw = {"severity": "warning", "summary": "cites a part but nothing else", "refs": ["U9"]}
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNotNone(reason)
        self.assertIn("no evidence source", reason)

    def test_missing_summary_is_quarantined(self):
        raw = {"refs": ["U9"], "evidence": "computation: x"}
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNotNone(reason)
        self.assertIn("missing summary", reason)

    def test_refuted_finding_is_quarantined_even_with_full_evidence(self):
        raw = {
            "severity": "error", "summary": "claimed bug", "refs": ["U1"],
            "evidence": "computation: looked right at first",
            "confidence": "confirmed",
            "verification": {"status": "refuted", "reason": "could not reproduce; datasheet says otherwise"},
        }
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNotNone(reason)
        self.assertIn("verifier refuted", reason)
        self.assertIn("could not reproduce", reason)

    def test_not_verified_is_not_treated_as_refutation_and_caps_confidence(self):
        raw = {
            "severity": "error", "summary": "plausible bug", "refs": ["U1"],
            "evidence": "computation: arithmetic shown",
            "confidence": "confirmed",
            "verification": {"status": "not_verified", "reason": "no datasheet available to check against"},
        }
        finding, reason = ndr.adapt_finding(raw)
        # NOT quarantined just because the verifier couldn't resolve it --
        # missing docs/failed verification is not a refutation.
        self.assertIsNone(reason)
        # But also not silently promoted to full trust on the agent's own say-so.
        self.assertEqual(finding["confidence"], "medium")

    def test_not_verified_never_exceeds_low_when_agent_confidence_was_already_low(self):
        raw = {
            "severity": "info", "summary": "minor note", "refs": ["U1"],
            "evidence": "computation: x",
            "confidence": "check",
            "verification": {"status": "not_verified", "reason": "n/a"},
        }
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNone(reason)
        self.assertEqual(finding["confidence"], "low")

    def test_confirmed_verification_promotes_to_high_confidence(self):
        raw = {
            "severity": "warning", "summary": "double-checked", "refs": ["U1"],
            "evidence": "computation: x", "confidence": "likely",
            "verification": {"status": "confirmed", "reason": "reproduced exactly"},
        }
        finding, reason = ndr.adapt_finding(raw)
        self.assertIsNone(reason)
        self.assertEqual(finding["confidence"], "high")

    def test_critical_severity_maps_to_error(self):
        raw = {"severity": "critical", "summary": "x", "refs": ["U1"], "evidence": "computation: y"}
        finding, _ = ndr.adapt_finding(raw)
        self.assertEqual(finding["severity"], "error")


class BuildDeepReviewDocTests(unittest.TestCase):
    def test_bare_list_input(self):
        raw_list = [
            {"severity": "error", "summary": "a", "refs": ["U1"], "evidence": "computation: 1"},
            {"severity": "info", "summary": "b"},  # no anchor/evidence -> quarantined
        ]
        doc = ndr.build_deep_review_doc(raw_list, produced_for_run_id="test-run")
        self.assertEqual(doc["schema_version"], "1.0")
        self.assertEqual(doc["produced_for_run_id"], "test-run")
        self.assertEqual(len(doc["findings"]), 1)
        self.assertEqual(len(doc["quarantined"]), 1)

    def test_run_deep_review_shaped_dict_input_routes_all_three_buckets(self):
        raw_input = {
            "findings": [
                {"severity": "error", "summary": "kept", "refs": ["U1"], "evidence": "computation: 1"},
            ],
            "quarantined": [
                {"severity": "warning", "summary": "already bad per plugin gate",
                 "quarantine_reason": "no refs/nets"},
            ],
            "refuted": [
                {"severity": "error", "summary": "disproven", "refs": ["U2"],
                 "evidence": "computation: 2",
                 "verification": {"status": "refuted", "reason": "datasheet contradicts it"}},
            ],
        }
        doc = ndr.build_deep_review_doc(raw_input)
        self.assertEqual(len(doc["findings"]), 1)
        self.assertEqual(len(doc["quarantined"]), 2)
        reasons = [q["quarantine_reason"] for q in doc["quarantined"]]
        self.assertTrue(any("plugin gate" in r for r in reasons))
        self.assertTrue(any("verifier refuted" in r for r in reasons))

    def test_invalid_input_raises(self):
        with self.assertRaises(ValueError):
            ndr.build_deep_review_doc("not a list or dict")

    def test_output_validates_against_real_deep_review_schema(self):
        raw_list = [
            {"severity": "error", "summary": "a", "refs": ["U1"], "evidence": "computation: 1",
             "recommendation": "fix it"},
        ]
        doc = ndr.build_deep_review_doc(raw_list, produced_for_run_id="schema-check")
        errors = _schema_errors(doc, DEEP_REVIEW_SCHEMA)
        self.assertEqual(errors, [])

    def test_quarantined_schema_only_requires_quarantine_reason(self):
        raw_list = [{"summary": "no anchor at all"}]
        doc = ndr.build_deep_review_doc(raw_list, produced_for_run_id="schema-check-2")
        errors = _schema_errors(doc, DEEP_REVIEW_SCHEMA)
        self.assertEqual(errors, [])
        self.assertEqual(len(doc["quarantined"]), 1)


class ValidateSynthesisTests(unittest.TestCase):
    def _deep_review_doc(self):
        return {
            "schema_version": "1.0",
            "produced_for_run_id": "run-1",
            "produced_at": "2026-10-19T00:00:00Z",
            "findings": [
                {"detector": "deep_review", "category": "power", "severity": "error",
                 "confidence": "high", "summary": "x", "finding_id": "deep_review:aaaaaaaaaaaa",
                 "evidence": {"components": ["U1"], "computation": {"description": "d", "result": "r"}}},
                {"detector": "deep_review", "category": "rf", "severity": "warning",
                 "confidence": "medium", "summary": "y", "finding_id": "deep_review:bbbbbbbbbbbb",
                 "evidence": {"components": ["U2"], "computation": {"description": "d", "result": "r"}}},
            ],
            "quarantined": [],
        }

    def _valid_synthesis_doc(self):
        return {
            "schema_version": "1.0",
            "produced_for_run_id": "run-1",
            "produced_at": "2026-10-19T00:00:00Z",
            "verdict": {"status": "needs_review", "summary": "One real risk, one confirmed-ok."},
            "top_risks": [{"finding_id": "deep_review:aaaaaaaaaaaa", "reason": "will not work at worst case"}],
            "verified_ok": [{"finding_id": "deep_review:bbbbbbbbbbbb", "note": "margin is fine"},
                            {"note": "board-wide decoupling check, not tied to one finding"}],
            "not_checked": [{"reason": "thermal not run -- no PCB JSON this pass"}],
            "contradictions": [],
        }

    def test_valid_synthesis_has_no_errors(self):
        errors = ndr.validate_synthesis(self._valid_synthesis_doc(), self._deep_review_doc())
        self.assertEqual(errors, [])

    def test_unresolved_finding_id_is_reported(self):
        synthesis = self._valid_synthesis_doc()
        synthesis["top_risks"].append({"finding_id": "deep_review:does-not-exist", "reason": "ghost finding"})
        errors = ndr.validate_synthesis(synthesis, self._deep_review_doc())
        self.assertTrue(any("does-not-exist" in e for e in errors))

    def test_contradiction_references_are_checked(self):
        synthesis = self._valid_synthesis_doc()
        synthesis["contradictions"].append({
            "finding_ids": ["deep_review:aaaaaaaaaaaa", "deep_review:missing"],
            "description": "one says safe, one says not",
        })
        errors = ndr.validate_synthesis(synthesis, self._deep_review_doc())
        self.assertTrue(any("missing" in e and "contradictions" in e for e in errors))

    def test_schema_violation_is_reported(self):
        synthesis = self._valid_synthesis_doc()
        del synthesis["verdict"]
        errors = ndr.validate_synthesis(synthesis, self._deep_review_doc())
        self.assertTrue(any("verdict" in e for e in errors))

    def test_quarantined_findings_have_no_durable_id_and_cannot_be_cited(self):
        deep_review = self._deep_review_doc()
        deep_review["quarantined"] = [{"quarantine_reason": "no evidence"}]
        synthesis = self._valid_synthesis_doc()
        synthesis["top_risks"].append({"finding_id": "deep_review:cccccccccccc", "reason": "quarantined claim"})
        errors = ndr.validate_synthesis(synthesis, deep_review)
        self.assertTrue(any("cccccccccccc" in e for e in errors))

    def test_stale_run_id_mismatch_is_reported(self):
        synthesis = self._valid_synthesis_doc()
        synthesis["produced_for_run_id"] = "run-2"
        errors = ndr.validate_synthesis(synthesis, self._deep_review_doc())
        self.assertTrue(any("produced_for_run_id" in e and "run-2" in e and "run-1" in e for e in errors))

    def test_missing_run_id_on_synthesis_is_reported(self):
        synthesis = self._valid_synthesis_doc()
        del synthesis["produced_for_run_id"]
        errors = ndr.validate_synthesis(synthesis, self._deep_review_doc())
        self.assertTrue(any("produced_for_run_id" in e and "missing" in e for e in errors))

    def test_missing_run_id_on_deep_review_is_reported(self):
        synthesis = self._valid_synthesis_doc()
        deep_review = self._deep_review_doc()
        del deep_review["produced_for_run_id"]
        errors = ndr.validate_synthesis(synthesis, deep_review)
        self.assertTrue(any("produced_for_run_id" in e and "missing" in e for e in errors))

    def test_validator_never_mutates_the_synthesis_document(self):
        synthesis = self._valid_synthesis_doc()
        before = copy.deepcopy(synthesis)
        ndr.validate_synthesis(synthesis, self._deep_review_doc())
        self.assertEqual(synthesis, before)

    def test_synthesis_example_validates_against_its_own_schema(self):
        errors = _schema_errors(self._valid_synthesis_doc(), SYNTHESIS_SCHEMA)
        self.assertEqual(errors, [])


class CliTests(unittest.TestCase):
    def test_normalize_cli_round_trip(self):
        raw_list = [
            {"severity": "error", "summary": "a", "refs": ["U1"], "evidence": "computation: 1"},
            {"severity": "info", "summary": "b"},
        ]
        with tempfile.TemporaryDirectory() as td:
            in_path = Path(td) / "raw.json"
            out_path = Path(td) / "deep_review.json"
            in_path.write_text(json.dumps(raw_list))
            result = subprocess.run(
                [sys.executable, str(Path(SCRIPT_DIR) / "normalize_deep_review.py"),
                 "normalize", str(in_path), "--output", str(out_path), "--run-id", "cli-run"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)  # 1 findings quarantined
            doc = json.loads(out_path.read_text())
            self.assertEqual(doc["produced_for_run_id"], "cli-run")
            self.assertEqual(len(doc["findings"]), 1)
            self.assertEqual(len(doc["quarantined"]), 1)

    def test_validate_synthesis_cli(self):
        deep_review = {
            "schema_version": "1.0", "produced_for_run_id": "r", "produced_at": "2026-10-19T00:00:00Z",
            "findings": [{"detector": "deep_review", "category": "power", "severity": "error",
                         "confidence": "high", "summary": "x", "finding_id": "deep_review:aaaaaaaaaaaa",
                         "evidence": {"components": ["U1"], "computation": {"description": "d", "result": "r"}}}],
            "quarantined": [],
        }
        synthesis = {
            "schema_version": "1.0", "produced_for_run_id": "r", "produced_at": "2026-10-19T00:00:00Z",
            "verdict": {"status": "ready", "summary": "fine"},
            "top_risks": [], "verified_ok": [{"finding_id": "deep_review:aaaaaaaaaaaa", "note": "ok"}],
            "not_checked": [], "contradictions": [],
        }
        with tempfile.TemporaryDirectory() as td:
            dr_path = Path(td) / "deep_review.json"
            syn_path = Path(td) / "synthesis.json"
            dr_path.write_text(json.dumps(deep_review))
            syn_path.write_text(json.dumps(synthesis))
            result = subprocess.run(
                [sys.executable, str(Path(SCRIPT_DIR) / "normalize_deep_review.py"),
                 "validate-synthesis", str(syn_path), str(dr_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
