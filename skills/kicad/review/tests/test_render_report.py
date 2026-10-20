"""Isolated tests for the static Deep Review report renderer.

Stdlib-only (unittest). Run with either:

    python3 -m unittest discover -s skills/kicad/review/tests -v
    python3 -m pytest skills/kicad/review/tests -v

This directory is intentionally separate from any CI-owned `tests/`
harness — it only exercises the new renderer added under
`skills/kicad/review/scripts/` (`report_data.py`, `render_markdown.py`,
`render_html.py`, `render_report.py`) and does not touch the toolkit,
schemas, plan script, datasheets skill, or CI config.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import report_data               # noqa: E402
import render_markdown            # noqa: E402
import render_html                # noqa: E402
import render_report              # noqa: E402


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class BuildContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.analysis_dir = Path(self._tmp.name) / "analysis"
        self.analysis_dir.mkdir()

    def test_missing_deep_review_is_explicit_gap(self):
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertFalse(ctx["deep_review"]["present"])
        gap_artifacts = [g["artifact"] for g in ctx["gaps"]]
        self.assertIn("deep_review.json", gap_artifacts)
        # The gap must say *why* (not found), not just that it's missing silently.
        dr_gap = next(g for g in ctx["gaps"] if g["artifact"] == "deep_review.json")
        self.assertIn("not found", dr_gap["reason"])

    def test_missing_optional_artifacts_each_produce_a_gap(self):
        ctx = report_data.build_context(str(self.analysis_dir))
        gap_artifacts = {g["artifact"] for g in ctx["gaps"]}
        for expected in ("synthesis.json", "review_plan.json", "design_context.json",
                          "review_annotations.json", "triage.json", "board_facts.json"):
            self.assertIn(expected, gap_artifacts)
        self.assertFalse(ctx["synthesis"]["present"])
        self.assertFalse(ctx["review_plan"]["present"])
        self.assertFalse(ctx["design_context"]["present"])
        self.assertFalse(ctx["review_annotations"]["present"])
        self.assertFalse(ctx["analyzer_triage"]["present"])
        self.assertFalse(ctx["board_facts"]["present"])

    def test_quarantined_findings_preserved_with_state_and_reason(self):
        _write_json(self.analysis_dir / "deep_review.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "findings": [],
            "quarantined": [
                {"category": "thermal", "summary": "U9 may overheat",
                 "quarantine_reason": "no evidence source: evidence must carry a datasheet quote and/or a computation"},
            ],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertEqual(len(ctx["deep_review"]["quarantined"]), 1)
        q = ctx["deep_review"]["quarantined"][0]
        self.assertEqual(q["_state"], "quarantined")
        self.assertIn("no evidence source", q["quarantine_reason"])
        # Quarantined entries must not disappear from the deep_review.json
        # gap list just because the file itself exists.
        self.assertTrue(ctx["deep_review"]["present"])

    def test_verification_state_mapping(self):
        self.assertEqual(report_data.deep_review_finding_state({"evidence_checked": "full"}),
                          "verified_full")
        self.assertEqual(report_data.deep_review_finding_state({"evidence_checked": "partial"}),
                          "verified_partial")
        self.assertEqual(report_data.deep_review_finding_state({}), "not_checked")
        self.assertEqual(report_data.annotation_state_label("confirmed"), "verified_ok")
        self.assertEqual(report_data.annotation_state_label("suppressed"), "refuted")
        self.assertEqual(report_data.annotation_state_label("escalated"), "escalated")
        self.assertEqual(report_data.annotation_state_label(None), "not_checked")
        self.assertEqual(report_data.annotation_state_label("unknown-status"), "not_checked")

    def test_findings_without_evidence_checked_are_not_checked(self):
        _write_json(self.analysis_dir / "deep_review.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "findings": [
                {"detector": "deep_review", "category": "power", "severity": "warning",
                 "confidence": "medium", "summary": "ungated finding",
                 "evidence": {"components": ["U1"]}},
            ],
            "quarantined": [],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertEqual(ctx["deep_review"]["findings"][0]["_state"], "not_checked")

    def test_malformed_deep_review_reports_schema_errors_without_raising(self):
        _write_json(self.analysis_dir / "deep_review.json", {"not": "a valid document"})
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertTrue(ctx["deep_review"]["present"])
        self.assertTrue(ctx["deep_review"]["schema_errors"])
        self.assertTrue(any(g["artifact"] == "deep_review.json" for g in ctx["gaps"]))


class VibeManifestAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.analysis_dir = self.root / "analysis"
        self.analysis_dir.mkdir()
        self.datasheets_dir = self.root / "datasheets"
        self.datasheets_dir.mkdir()
        _write_json(self.analysis_dir / "review_coverage.json", {
            "candidates": [{"ref": "U1", "mpn": "TPS61023DRLR", "class": "ic"}],
            "missing": [], "excluded": [],
        })
        self.pdf = b"%PDF-1.7\\nlocal manifest PDF bytes\\n"
        (self.datasheets_dir / "vibe-datasheet.pdf").write_bytes(self.pdf)
        self.asset = {
            "type": "datasheet", "id": "a" * 64,
            "filepath": "vibe-datasheet.pdf",
            "sha256": hashlib.sha256(self.pdf).hexdigest(), "size": len(self.pdf),
            "status": "downloaded", "metadata": {"mfr": "Example"},
        }
        self.entry = {
            "requested_mpn": "TPS61023DRLR", "returned_mpn": "TPS61023DRLR",
            "checked_at": "2026-10-19T00:00:00Z", "status": "complete",
            "stale": False, "assets": {f"datasheet:{'a' * 64}": self.asset},
        }

    def _build(self):
        manifest_dir = self.datasheets_dir / "vibe"
        _write_json(manifest_dir / "manifest.json", {"version": 1,
                                                       "mpns": {"TPS61023DRLR": self.entry}})
        return report_data.build_context(str(self.analysis_dir), project_dir=str(self.root),
                                         datasheets_dir=str(self.datasheets_dir))

    def test_manifested_vibe_pdf_is_available_without_verification_claim(self):
        ctx = self._build()
        part = ctx["coverage"]["parts"][0]
        self.assertTrue(part["vibe_asset_available"])
        self.assertEqual(part["vibe_asset_count"], 1)
        self.assertFalse(part["pdf_available"])
        self.assertFalse(part["extraction_available"])
        self.assertFalse(any(g["artifact"] == "Vibe manifest (datasheets/vibe/manifest.json)"
                             for g in ctx["gaps"]))

    def test_missing_or_tampered_manifest_asset_is_not_available(self):
        self.pdf_path().unlink()
        part = self._build()["coverage"]["parts"][0]
        self.assertFalse(part["vibe_asset_available"])

        (self.datasheets_dir / "vibe-datasheet.pdf").write_bytes(b"%PDF-tampered")
        part = self._build()["coverage"]["parts"][0]
        self.assertFalse(part["vibe_asset_available"])

    def test_path_escape_is_not_available(self):
        outside = self.root / "outside.pdf"
        outside.write_bytes(self.pdf)
        self.asset["filepath"] = "../outside.pdf"
        self.asset["sha256"] = hashlib.sha256(self.pdf).hexdigest()
        self.asset["size"] = len(self.pdf)
        part = self._build()["coverage"]["parts"][0]
        self.assertFalse(part["vibe_asset_available"])

    def test_malformed_manifest_adds_explicit_gap(self):
        _write_json(self.datasheets_dir / "vibe" / "manifest.json", {
            "version": 2, "mpns": {"TPS61023DRLR": self.entry}})
        ctx = report_data.build_context(str(self.analysis_dir), project_dir=str(self.root),
                                        datasheets_dir=str(self.datasheets_dir))
        self.assertFalse(ctx["coverage"]["parts"][0]["vibe_asset_available"])
        gap = next(g for g in ctx["gaps"]
                   if g["artifact"] == "Vibe manifest (datasheets/vibe/manifest.json)")
        self.assertIn("version", gap["reason"])

    def test_stale_inventory_remains_distinct_and_not_current(self):
        self.entry["status"] = "failed"
        self.entry["stale"] = True
        part = self._build()["coverage"]["parts"][0]
        self.assertFalse(part["vibe_asset_available"])
        self.assertTrue(part["vibe_inventory_stale"])
        self.assertFalse(part["vibe_inventory_current"])
        self.assertEqual(part["vibe_asset_count"], 1)

    def test_extraction_availability_remains_independent(self):
        extracted = self.datasheets_dir / "extracted" / "TPS61023DRLR.json"
        extracted.parent.mkdir()
        extracted.write_text("{}", encoding="utf-8")
        part = self._build()["coverage"]["parts"][0]
        self.assertTrue(part["vibe_asset_available"])
        self.assertTrue(part["extraction_available"])

    def pdf_path(self):
        return self.datasheets_dir / "vibe-datasheet.pdf"


class SynthesisTests(unittest.TestCase):
    """synthesis.json must follow this fork's schemas/synthesis.schema.json
    exactly: verdict is an object (status/summary), and top_risks/
    verified_ok/not_checked are arrays of objects, not bare strings."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.analysis_dir = Path(self._tmp.name) / "analysis"
        self.analysis_dir.mkdir()

    def test_well_formed_synthesis_normalizes_cleanly(self):
        _write_json(self.analysis_dir / "synthesis.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "verdict": {"status": "needs_review", "summary": "Two open risks on U3 thermal margin."},
            "top_risks": [{"finding_id": "deep_review:abc123", "reason": "Tj margin < 10C"}],
            "verified_ok": [{"note": "Decoupling placement checked board-wide"}],
            "not_checked": [{"reason": "No gerbers available — DFM pass skipped"}],
            "contradictions": [],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertTrue(ctx["synthesis"]["present"])
        self.assertEqual(ctx["synthesis"]["schema_errors"], [])
        self.assertEqual(ctx["synthesis"]["verdict_status"], "needs_review")
        self.assertIn("thermal margin", ctx["synthesis"]["verdict_summary"])
        self.assertEqual(ctx["synthesis"]["top_risks"][0]["finding_id"], "deep_review:abc123")
        self.assertEqual(ctx["synthesis"]["not_checked"][0]["reason"],
                          "No gerbers available — DFM pass skipped")
        md = render_markdown.render_markdown(ctx)
        self.assertIn("needs_review", md)
        self.assertIn("Tj margin", md)
        self.assertIn("Not Checked (explicit gaps)", md)
        self.assertIn("No gerbers available", md)

    def test_malformed_synthesis_verdict_as_bare_string_is_a_schema_violation(self):
        # Old (wrong-clone) shape used a bare string verdict; this fork's
        # schema requires an object with status/summary -- must be flagged,
        # not silently accepted.
        _write_json(self.analysis_dir / "synthesis.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "verdict": "ready",
            "top_risks": [],
            "verified_ok": [],
            "not_checked": [],
            "contradictions": [],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertTrue(ctx["synthesis"]["present"])
        self.assertTrue(ctx["synthesis"]["schema_errors"])
        self.assertIsNone(ctx["synthesis"]["verdict_status"])
        self.assertTrue(any(g["artifact"] == "synthesis.json" for g in ctx["gaps"]))
        md = render_markdown.render_markdown(ctx)
        self.assertIn("Schema violations found while loading `synthesis.json`", md)


class AnalyzerTriageAndBoardFactsTests(unittest.TestCase):
    """analyzer_triage.py's triage.json and board_facts.py's board_facts.json
    are optional, per-run artifacts (skills/kicad/SKILL.md); absence must be
    an explicit gap, presence must render the Layer 1 triage rollup and the
    raw-PCB stackup facts respectively."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.analysis_dir = Path(self._tmp.name) / "analysis"
        self.analysis_dir.mkdir()

    def test_triage_json_present_renders_rollup_and_must_fix_count(self):
        _write_json(self.analysis_dir / "triage.json", {
            "findings": [
                {"rule_id": "KO-001", "severity": "info", "triage": "likely_fp"},
                {"rule_id": "RT-001", "severity": "error", "triage": "real"},
            ],
            "triage_table": [
                {"rule_id": "KO-001", "count": 1, "real": 0, "likely_fp": 1, "review": 0,
                 "reason": "KiCad DRC reports no keepout violations"},
                {"rule_id": "RT-001", "count": 1, "real": 1, "likely_fp": 0, "review": 0, "reason": ""},
            ],
            "must_fix": [{"rule_id": "RT-001", "severity": "error"}],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertTrue(ctx["analyzer_triage"]["present"])
        self.assertEqual(ctx["analyzer_triage"]["finding_count"], 2)
        self.assertEqual(ctx["analyzer_triage"]["must_fix_count"], 1)
        self.assertFalse(any(g["artifact"] == "triage.json" for g in ctx["gaps"]))
        md = render_markdown.render_markdown(ctx)
        self.assertIn("Analyzer Finding Triage (Layer 1", md)
        self.assertIn("KO-001", md)
        self.assertIn("KiCad DRC reports no keepout violations", md)

    def test_triage_json_missing_is_explicit_gap(self):
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertFalse(ctx["analyzer_triage"]["present"])
        gap = next(g for g in ctx["gaps"] if g["artifact"] == "triage.json")
        self.assertIn("not run", gap["reason"])
        md = render_markdown.render_markdown(ctx)
        self.assertIn("`triage.json` not found", md)

    def test_board_facts_json_present_renders_stackup_warnings(self):
        _write_json(self.analysis_dir / "board_facts.json", {
            "thickness_mm": 1.6,
            "copper_layers": [],
            "stackup_warnings": ["Dielectric under F.Cu is multi-ply (core 0.2 + prepreg 0.1 = 0.3000 mm)"],
            "stackup_multi_ply": True,
            "impedance_control_noted": True,
            "graphic_copper": {"RF_FEED": {"F.Cu": 12.3}},
            "unnetted_copper": [],
            "footprint_copper": {},
            "findings": [{"rule_id": "BF-STACKUP-MULTIPLY", "severity": "info"}],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertTrue(ctx["board_facts"]["present"])
        self.assertTrue(ctx["board_facts"]["stackup_multi_ply"])
        self.assertEqual(ctx["board_facts"]["graphic_copper_net_count"], 1)
        self.assertFalse(any(g["artifact"] == "board_facts.json" for g in ctx["gaps"]))
        md = render_markdown.render_markdown(ctx)
        self.assertIn("Board Facts (Raw PCB Parse", md)
        self.assertIn("multi-ply (core 0.2", md)

    def test_board_facts_json_missing_is_explicit_gap(self):
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertFalse(ctx["board_facts"]["present"])
        gap = next(g for g in ctx["gaps"] if g["artifact"] == "board_facts.json")
        self.assertIn("not run", gap["reason"])
        md = render_markdown.render_markdown(ctx)
        self.assertIn("`board_facts.json` not found", md)


class MarkdownRenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.analysis_dir = Path(self._tmp.name) / "analysis"
        self.analysis_dir.mkdir()

    def test_markdown_escapes_untrusted_html_in_finding_text(self):
        _write_json(self.analysis_dir / "deep_review.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "findings": [
                {"detector": "deep_review", "category": "power", "severity": "error",
                 "confidence": "high",
                 "summary": "<script>alert(1)</script> injected summary",
                 "evidence": {"components": ["U1"],
                              "datasheet": [{"mpn": "U1", "quote": "<img src=x onerror=alert(2)>"}]}},
            ],
            "quarantined": [],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        md = render_markdown.render_markdown(ctx)
        self.assertNotIn("<script>alert(1)</script>", md)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", md)
        self.assertNotIn("<img src=x onerror=alert(2)>", md)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", md)

    def test_markdown_escapes_untrusted_html_in_synthesis_text(self):
        _write_json(self.analysis_dir / "synthesis.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "verdict": {"status": "blocked", "summary": "<script>alert(3)</script>"},
            "top_risks": [{"reason": "<img src=x onerror=alert(4)>"}],
            "verified_ok": [], "not_checked": [], "contradictions": [],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        md = render_markdown.render_markdown(ctx)
        self.assertNotIn("<script>alert(3)</script>", md)
        self.assertIn("&lt;script&gt;alert(3)&lt;/script&gt;", md)
        self.assertNotIn("<img src=x onerror=alert(4)>", md)

    def test_markdown_states_deep_review_gap_explicitly(self):
        ctx = report_data.build_context(str(self.analysis_dir))
        md = render_markdown.render_markdown(ctx)
        self.assertIn("Deep Review pass not run", md)
        self.assertIn("Not Performed / Review Limits", md)
        self.assertIn("deep_review.json", md)

    def test_markdown_renders_quarantined_section_never_dropped(self):
        _write_json(self.analysis_dir / "deep_review.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "findings": [],
            "quarantined": [
                {"category": "thermal", "summary": "maybe hot",
                 "quarantine_reason": "no design anchor: evidence must cite >=1 component/net/pin"},
            ],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        md = render_markdown.render_markdown(ctx)
        self.assertIn("Unverified claims (quarantined)", md)
        self.assertIn("maybe hot", md)
        self.assertIn("no design anchor", md)


class HtmlRenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.analysis_dir = Path(self._tmp.name) / "analysis"
        self.analysis_dir.mkdir()

    def test_html_escapes_quarantine_reason_and_summary(self):
        _write_json(self.analysis_dir / "deep_review.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "findings": [],
            "quarantined": [
                {"category": "thermal",
                 "summary": '<script>document.location="https://evil.example"</script>',
                 "quarantine_reason": "</script><script>alert(3)</script>"},
            ],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        html = render_html.render_html(ctx)
        self.assertNotIn('<script>document.location', html)
        self.assertNotIn('</script><script>alert(3)</script>', html)
        self.assertIn("&lt;script&gt;", html)
        # Quarantined rows must still show up, with the quarantined state class.
        self.assertIn('class="state-quarantined"', html)

    def test_embedded_json_payload_cannot_break_out_of_script_tag(self):
        payload = {"a": "</script><script>alert(1)</script>"}
        safe = render_html._safe_json_for_script(payload)
        self.assertNotIn("</script>", safe)
        # Still valid JSON once the escaping is reversed by the browser parser
        # (the browser never un-escapes "<\\/", it just never matches the
        # closing tag early — json.loads on our side confirms the payload
        # survives round-trip once naively unescaped).
        self.assertEqual(json.loads(safe.replace("<\\/", "</")), payload)

    def test_html_contains_no_fetch_or_server_endpoints(self):
        ctx = report_data.build_context(str(self.analysis_dir))
        html = render_html.render_html(ctx)
        for forbidden in ("fetch(", "XMLHttpRequest", "http://", "https://",
                          "<script src=", "<link "):
            self.assertNotIn(forbidden, html)

    def test_html_has_download_and_triage_controls(self):
        _write_json(self.analysis_dir / "deep_review.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "r1",
            "produced_at": "2026-01-01T00:00:00Z",
            "findings": [
                {"detector": "deep_review", "category": "power", "severity": "warning",
                 "confidence": "medium", "summary": "sample finding",
                 "finding_id": "deep_review:abc123",
                 "evidence_checked": "full", "evidence": {"components": ["U1"]}},
            ],
            "quarantined": [],
        })
        ctx = report_data.build_context(str(self.analysis_dir))
        html = render_html.render_html(ctx)
        self.assertIn('data-action="download"', html)
        self.assertIn('data-action="resolve"', html)
        self.assertIn('data-action="ignore"', html)
        self.assertIn('data-finding-id="deep_review:abc123"', html)

    def test_html_sidecar_keyed_by_run_id(self):
        _write_json(self.analysis_dir / "deep_review.json", {
            "schema_version": "1.0",
            "produced_for_run_id": "run-xyz",
            "produced_at": "2026-01-01T00:00:00Z",
            "findings": [], "quarantined": [],
        })
        _write_json(self.analysis_dir / "manifest.json", {"current": "run-xyz"})
        (self.analysis_dir / "run-xyz").mkdir()
        ctx = report_data.build_context(str(self.analysis_dir))
        self.assertEqual(ctx["run_id"], "run-xyz")
        html = render_html.render_html(ctx)
        # The run id must end up inside the embedded triage-data payload so
        # the browser-side localStorage key is scoped per run.
        self.assertIn('"run_id": "run-xyz"', html)


class CliOutputLocationTests(unittest.TestCase):
    """Verify the renderer only writes where it's told to, and defaults
    inside the analysis directory (never the source project tree)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project_dir = Path(self._tmp.name)
        self.analysis_dir = self.project_dir / "analysis"
        self.analysis_dir.mkdir()

    def test_default_markdown_output_lands_inside_analysis_dir(self):
        rc = render_report.main(["--analysis-dir", str(self.analysis_dir)])
        self.assertEqual(rc, 0)
        default_path = self.analysis_dir / "deep_review_report.md"
        self.assertTrue(default_path.is_file())
        # Nothing else should appear in the project dir -- the renderer must
        # never write into the source design tree by default.
        other_files = [p for p in self.project_dir.iterdir() if p != self.analysis_dir]
        self.assertEqual(other_files, [])

    def test_explicit_output_path_is_honored(self):
        out_path = Path(self._tmp.name) / "custom" / "report.md"
        rc = render_report.main(["--analysis-dir", str(self.analysis_dir),
                                  "--output", str(out_path)])
        self.assertEqual(rc, 0)
        self.assertTrue(out_path.is_file())

    def test_html_output_default_also_inside_analysis_dir(self):
        rc = render_report.main(["--analysis-dir", str(self.analysis_dir), "--html"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.analysis_dir / "deep_review_report.html").is_file())


if __name__ == "__main__":
    unittest.main()
