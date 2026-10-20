#!/usr/bin/env python3
"""Unit tests for score_review.py.

Uses synthetic, non-proprietary benchmark and deep_review fixtures defined
inline -- never reads a real benchmark file. Covers anchor/text matching,
evidence preconditions, recall/severity reporting, duplicate matches,
unmatched (potential FP) generated findings, and the CLI's exit codes.

Run with:
    python3 skills/kicad/review/scripts/test_score_review.py
    python3 -m unittest skills/kicad/review/scripts/test_score_review.py
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import score_review  # noqa: E402


def _finding(**kw):
    base = {
        "detector": "deep_review",
        "category": "power_input",
        "severity": "error",
        "confidence": "high",
        "summary": "",
        "evidence": {},
    }
    base.update(kw)
    return base


class TestNormalization(unittest.TestCase):
    def test_norm_text_collapses_punct_and_case(self):
        self.assertEqual(score_review._norm_text("L2 (220 nH)!"), "l2 220 nh")

    def test_norm_text_unicode_nfkc(self):
        # fullwidth digits/letters fold to ASCII under NFKC
        self.assertEqual(score_review._norm_text("５V"), "5v")


class TestAnchorOverlap(unittest.TestCase):
    def test_component_overlap(self):
        a = {"components": ["U3"], "nets": [], "pins": []}
        b = {"components": ["u3", "L2"], "nets": [], "pins": []}
        self.assertTrue(score_review.anchor_overlap(a, b))

    def test_net_overlap(self):
        a = {"components": [], "nets": ["VBUS"], "pins": []}
        b = {"components": [], "nets": ["vbus"], "pins": []}
        self.assertTrue(score_review.anchor_overlap(a, b))

    def test_pin_overlap_exact(self):
        a = {"components": [], "nets": [], "pins": ["U3.7"]}
        b = {"components": [], "nets": [], "pins": ["u3.7"]}
        self.assertTrue(score_review.anchor_overlap(a, b))

    def test_pin_matches_component(self):
        a = {"components": ["U3"], "nets": [], "pins": []}
        b = {"components": [], "nets": [], "pins": ["U3.7"]}
        self.assertTrue(score_review.anchor_overlap(a, b))

    def test_no_overlap(self):
        a = {"components": ["U3"], "nets": ["VBUS"], "pins": []}
        b = {"components": ["U9"], "nets": ["GND"], "pins": ["U4.2"]}
        self.assertFalse(score_review.anchor_overlap(a, b))


class TestTextMatch(unittest.TestCase):
    def test_empty_phrases_always_match(self):
        self.assertTrue(score_review.text_match([], "anything at all"))

    def test_phrase_found_case_and_punct_insensitive(self):
        phrases = ["minimum inductance 470 nH"]
        hay = "...the MINIMUM INDUCTANCE: 470-nH requirement is violated..."
        self.assertTrue(score_review.text_match(phrases, hay))

    def test_phrase_not_found(self):
        phrases = ["minimum inductance 470 nH"]
        hay = "completely unrelated finding about thermal derating"
        self.assertFalse(score_review.text_match(phrases, hay))

    def test_any_phrase_matches(self):
        phrases = ["never appears", "470 nH minimum"]
        hay = "the inductor must be at least 470 nH minimum per datasheet"
        self.assertTrue(score_review.text_match(phrases, hay))


class TestValidateBenchmarkShape(unittest.TestCase):
    def test_valid_benchmark_has_no_errors(self):
        benchmark = {
            "benchmark_id": "synthetic-1",
            "issues": [
                {"issue_id": "i1", "severity": "error",
                 "anchors": {"components": ["U3"]}},
            ],
        }
        self.assertEqual(score_review._validate_benchmark_shape(benchmark), [])

    def test_non_object_root(self):
        self.assertEqual(
            score_review._validate_benchmark_shape([1, 2]),
            ["benchmark root must be a JSON object"],
        )

    def test_missing_issue_id_and_bad_severity(self):
        benchmark = {"issues": [{"severity": "critical", "anchors": {"nets": ["X"]}}]}
        errors = score_review._validate_benchmark_shape(benchmark)
        self.assertTrue(any("issue_id" in e for e in errors))
        self.assertTrue(any("severity" in e for e in errors))

    def test_issue_without_anchor_flagged(self):
        benchmark = {"issues": [{"issue_id": "i1", "severity": "warning", "anchors": {}}]}
        errors = score_review._validate_benchmark_shape(benchmark)
        self.assertTrue(any("anchor" in e for e in errors))

    def test_required_evidence_missing_path_flagged(self):
        benchmark = {
            "issues": [],
            "required_evidence": [{"id": "guide"}],
        }
        errors = score_review._validate_benchmark_shape(benchmark)
        self.assertTrue(any("required_evidence[0]" in e for e in errors))


class TestRequiredEvidenceAndClassification(unittest.TestCase):
    def test_check_required_evidence_available_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            present = project_dir / "present.pdf"
            present.write_text("x")
            benchmark = {
                "required_evidence": [
                    {"id": "a", "path": "present.pdf", "targets": ["t1"]},
                    {"id": "b", "path": "missing.pdf", "targets": ["t2", "t3"]},
                ]
            }
            checks, missing = score_review.check_required_evidence(benchmark, project_dir)
            self.assertEqual(len(checks), 2)
            self.assertTrue(checks[0]["available"])
            self.assertFalse(checks[1]["available"])
            self.assertEqual(missing, {"t2", "t3"})

    def test_classify_issues_marks_unscorable_by_target(self):
        benchmark = {
            "issues": [
                {"issue_id": "i1", "severity": "error", "target": "t1",
                 "anchors": {"components": ["U1"]}},
                {"issue_id": "i2", "severity": "warning", "target": "t2",
                 "anchors": {"components": ["U2"]}},
                {"issue_id": "i3", "severity": "info", "anchors": {"components": ["U3"]}},
            ]
        }
        scorable, unscorable = score_review.classify_issues(benchmark, {"t2"})
        self.assertEqual({i["issue_id"] for i in scorable}, {"i1", "i3"})
        self.assertEqual({i["issue_id"] for i in unscorable}, {"i2"})


class TestMatchIssues(unittest.TestCase):
    def test_anchor_and_text_match(self):
        issue = {"issue_id": "i1", "severity": "error",
                  "anchors": {"components": ["U3"], "nets": ["VBUS"]},
                  "text": ["minimum inductance of 470 nH"]}
        findings = [_finding(
            summary="L2 is below the TPS61023 minimum inductance of 470 nH",
            evidence={"components": ["U3", "L2"], "nets": ["VBUS"]},
        )]
        matches, missed = score_review.match_issues([issue], findings)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["finding_indices"], [0])
        self.assertEqual(missed, [])

    def test_missed_when_no_anchor_overlap(self):
        issue = {"issue_id": "i1", "severity": "error",
                  "anchors": {"components": ["U9"]}, "text": []}
        findings = [_finding(summary="unrelated", evidence={"components": ["U3"]})]
        matches, missed = score_review.match_issues([issue], findings)
        self.assertEqual(matches, [])
        self.assertEqual(len(missed), 1)

    def test_missed_when_anchor_overlaps_but_text_absent(self):
        issue = {"issue_id": "i1", "severity": "error",
                  "anchors": {"components": ["U3"]},
                  "text": ["very specific phrase not present anywhere"]}
        findings = [_finding(summary="U3 has some other problem",
                              evidence={"components": ["U3"]})]
        matches, missed = score_review.match_issues([issue], findings)
        self.assertEqual(matches, [])
        self.assertEqual(len(missed), 1)

    def test_duplicate_match_multiple_findings(self):
        issue = {"issue_id": "i1", "severity": "error",
                  "anchors": {"components": ["U3"]}, "text": []}
        findings = [
            _finding(summary="first report on U3", evidence={"components": ["U3"]}),
            _finding(summary="second report on U3", evidence={"components": ["U3"]}),
        ]
        matches, missed = score_review.match_issues([issue], findings)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["finding_indices"], [0, 1])


class TestBuildReport(unittest.TestCase):
    def setUp(self):
        self.benchmark = {
            "benchmark_id": "synthetic-1",
            "required_evidence": [
                {"id": "sim7600_guide", "path": "datasheets/SIM7600_Hardware_Design.pdf",
                 "targets": ["modem"]},
            ],
            "issues": [
                {"issue_id": "power-001", "category": "power_input", "severity": "error",
                 "anchors": {"components": ["U3", "L2"], "nets": ["VBUS"]},
                 "text": ["minimum inductance of 470 nH"],
                 "description": "proprietary: inductor undersized"},
                {"issue_id": "power-002", "category": "power_input", "severity": "warning",
                 "anchors": {"components": ["U5"]}, "text": [],
                 "description": "proprietary: thermal derating"},
                {"issue_id": "modem-001", "category": "rf", "severity": "error",
                 "target": "modem", "anchors": {"components": ["U10"]}, "text": [],
                 "description": "proprietary: modem antenna matching"},
            ],
        }
        self.deep_review = {
            "produced_for_run_id": "run-1",
            "findings": [
                _finding(finding_id="f1", category="power_input", severity="error",
                         summary="L2 (220 nH) is below the TPS61023 minimum inductance of 470 nH",
                         evidence={"components": ["U3", "L2"], "nets": ["VBUS"]}),
                _finding(finding_id="f2", category="power_input", severity="info",
                         summary="U5 thermal note", evidence={"components": ["U5"]}),
                _finding(finding_id="f3", category="emc", severity="warning",
                         summary="completely unrelated finding with no benchmark match",
                         evidence={"components": ["U99"]}),
            ],
            "quarantined": [{"quarantine_reason": "no evidence source"}],
        }

    def test_partial_status_when_evidence_missing(self):
        with tempfile.TemporaryDirectory() as td:
            report = score_review.build_report(self.deep_review, self.benchmark, td)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["unscorable_targets"], ["modem"])
        self.assertEqual(
            {i["issue_id"] for i in report["unscorable_issue_details"]}, {"modem-001"})
        # unscorable issue content stays out of the report by default
        self.assertNotIn("description", report["unscorable_issue_details"][0])

    def test_complete_status_when_evidence_present(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            guide = project_dir / "datasheets"
            guide.mkdir()
            (guide / "SIM7600_Hardware_Design.pdf").write_text("x")
            report = score_review.build_report(self.deep_review, self.benchmark, project_dir)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["unscorable_targets"], [])

    def test_recall_and_severity_breakdown(self):
        with tempfile.TemporaryDirectory() as td:
            report = score_review.build_report(self.deep_review, self.benchmark, td)
        s = report["summary"]
        # power-001 and power-002 scorable (modem-001 unscorable); both matched
        self.assertEqual(s["scorable_issues"], 2)
        self.assertEqual(s["matched"], 2)
        self.assertEqual(s["missed"], 0)
        self.assertEqual(s["recall"], 1.0)
        self.assertEqual(s["by_severity"]["error"]["total"], 1)
        self.assertEqual(s["by_severity"]["error"]["matched"], 1)
        self.assertEqual(s["by_severity"]["warning"]["total"], 1)
        self.assertEqual(s["by_severity"]["warning"]["matched"], 1)

    def test_severity_disagreement_reported(self):
        # power-002 expects "warning"; f2 is severity "info" -> mismatch
        with tempfile.TemporaryDirectory() as td:
            report = score_review.build_report(self.deep_review, self.benchmark, td)
        mismatches = report["severity_agreement"]["mismatches"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["issue_id"], "power-002")
        self.assertEqual(mismatches[0]["expected_severity"], "warning")
        self.assertEqual(mismatches[0]["finding_severity"], "info")

    def test_unmatched_finding_reported_as_potential_fp(self):
        with tempfile.TemporaryDirectory() as td:
            report = score_review.build_report(self.deep_review, self.benchmark, td)
        unmatched = report["unmatched_generated_findings"]
        self.assertEqual([u["finding_id"] for u in unmatched], ["f3"])

    def test_quarantined_count_surfaced(self):
        with tempfile.TemporaryDirectory() as td:
            report = score_review.build_report(self.deep_review, self.benchmark, td)
        self.assertEqual(report["quarantined_count"], 1)

    def test_include_benchmark_text_flag(self):
        with tempfile.TemporaryDirectory() as td:
            report = score_review.build_report(
                self.deep_review, self.benchmark, td, include_benchmark_text=True)
        matched_ids = {m["issue_id"]: m for m in report["matched_issues"]}
        self.assertEqual(
            matched_ids["power-001"]["description"], "proprietary: inductor undersized")

    def test_duplicate_matches_detected(self):
        benchmark = {
            "issues": [
                {"issue_id": "dup-1", "severity": "error",
                 "anchors": {"components": ["U20"]}, "text": []},
            ]
        }
        deep_review = {
            "produced_for_run_id": "run-2",
            "findings": [
                _finding(finding_id="d1", evidence={"components": ["U20"]}),
                _finding(finding_id="d2", evidence={"components": ["U20"]}),
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            report = score_review.build_report(deep_review, benchmark, td)
        self.assertEqual(len(report["duplicate_matches"]), 1)
        self.assertEqual(report["duplicate_matches"][0]["finding_ids"], ["d1", "d2"])


class TestCli(unittest.TestCase):
    def _run(self, argv):
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["score_review.py"] + argv), \
             patch("sys.stdout", stdout):
            code = score_review.main()
        return code, stdout.getvalue()

    def test_cli_complete_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            benchmark_path = project_dir / "benchmark.json"
            review_path = project_dir / "deep_review.json"
            benchmark_path.write_text(json.dumps({
                "benchmark_id": "b1",
                "issues": [
                    {"issue_id": "i1", "severity": "error",
                     "anchors": {"components": ["U3"]}, "text": []},
                ],
            }))
            review_path.write_text(json.dumps({
                "produced_for_run_id": "run-1",
                "findings": [_finding(finding_id="f1", evidence={"components": ["U3"]})],
                "quarantined": [],
            }))
            code, output = self._run([
                str(review_path), "--benchmark", str(benchmark_path),
                "--project-dir", str(project_dir),
            ])
        self.assertEqual(code, 0)
        parsed = json.loads(output)
        self.assertEqual(parsed["status"], "complete")

    def test_cli_partial_exit_one(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            benchmark_path = project_dir / "benchmark.json"
            review_path = project_dir / "deep_review.json"
            benchmark_path.write_text(json.dumps({
                "benchmark_id": "b1",
                "required_evidence": [
                    {"id": "guide", "path": "missing.pdf", "targets": ["t1"]},
                ],
                "issues": [
                    {"issue_id": "i1", "severity": "error", "target": "t1",
                     "anchors": {"components": ["U3"]}, "text": []},
                ],
            }))
            review_path.write_text(json.dumps({
                "produced_for_run_id": "run-1", "findings": [], "quarantined": [],
            }))
            code, _output = self._run([
                str(review_path), "--benchmark", str(benchmark_path),
                "--project-dir", str(project_dir),
            ])
        self.assertEqual(code, 1)

    def test_cli_malformed_benchmark_exit_two(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            benchmark_path = project_dir / "benchmark.json"
            review_path = project_dir / "deep_review.json"
            benchmark_path.write_text(json.dumps({"issues": [{"severity": "bogus"}]}))
            review_path.write_text(json.dumps({"produced_for_run_id": "run-1", "findings": []}))
            code, _output = self._run([
                str(review_path), "--benchmark", str(benchmark_path),
            ])
        self.assertEqual(code, 2)

    def test_cli_missing_deep_review_file_exit_two(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            benchmark_path = project_dir / "benchmark.json"
            benchmark_path.write_text(json.dumps({"issues": []}))
            code, _output = self._run([
                str(project_dir / "does_not_exist.json"), "--benchmark", str(benchmark_path),
            ])
        self.assertEqual(code, 2)

    def test_cli_benchmark_required(self):
        with tempfile.TemporaryDirectory() as td:
            review_path = Path(td) / "deep_review.json"
            review_path.write_text(json.dumps({"produced_for_run_id": "r", "findings": []}))
            with patch.object(sys, "argv", ["score_review.py", str(review_path)]):
                with self.assertRaises(SystemExit) as cm:
                    score_review.main()
            self.assertNotEqual(cm.exception.code, 0)

    def test_cli_output_file_written(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            benchmark_path = project_dir / "benchmark.json"
            review_path = project_dir / "deep_review.json"
            out_path = project_dir / "score.json"
            benchmark_path.write_text(json.dumps({"issues": []}))
            review_path.write_text(json.dumps({
                "produced_for_run_id": "run-1", "findings": [], "quarantined": [],
            }))
            code, output = self._run([
                str(review_path), "--benchmark", str(benchmark_path),
                "--output", str(out_path),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(output, "")  # report went to file, not stdout
            self.assertTrue(out_path.is_file())
            parsed = json.loads(out_path.read_text())
            self.assertEqual(parsed["status"], "complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
