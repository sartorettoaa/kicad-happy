"""Isolated tests for the static HTML renderer.

Run with ``python -I -m unittest`` from the repository root, naming this
file and the existing ``test_render_report.py``. The fixture below mirrors
``report_data.build_context``'s normalized shape and stays local to this test.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import render_html  # noqa: E402


class HtmlRendererTests(unittest.TestCase):
    def setUp(self):
        self.ctx = {
            "generated_at": "2026-10-19T12:00:00Z",
            "analysis_dir": "/tmp/project/analysis",
            "run_id": "run-html-test",
            "run_dir": "/tmp/project/analysis/run-html-test",
            "deep_review": {
                "present": True,
                "path": "/tmp/project/analysis/deep_review.json",
                "meta": {
                    "schema_version": "1.0",
                    "produced_for_run_id": "run-html-test",
                    "produced_at": "2026-10-19T11:00:00Z",
                },
                "schema_errors": [],
                "findings": [
                    {
                        "detector": "deep_review",
                        "finding_id": "deep_review:html1",
                        "category": "power",
                        "severity": "warning",
                        "confidence": "high",
                        "summary": "U1 input margin needs review",
                        "description": "The recorded operating point is close to the limit.",
                        "recommendation": "Check the upstream load assumptions.",
                        "_state": "verified_full",
                        "evidence": {
                            "components": ["U1"],
                            "nets": ["VIN"],
                            "pins": ["U1.3"],
                            "datasheet": [{
                                "mpn": "EXAMPLE-123",
                                "page": 7,
                                "quote": "Input range is 4.5 V to 12 V.",
                            }],
                            "computation": {
                                "description": "Worst-case input calculation",
                                "result": "4.8 V",
                                "script": "tools/check_margin.py",
                            },
                        },
                    },
                    {
                        "detector": "deep_review",
                        "finding_id": "deep_review:html2",
                        "category": "thermal",
                        "severity": "error",
                        "confidence": "medium",
                        "summary": "Thermal evidence is partial",
                        "_state": "verified_partial",
                        "evidence": {"components": ["Q2"]},
                    },
                ],
                "quarantined": [{
                    "category": "thermal",
                    "summary": "Possible unverified hotspot",
                    "quarantine_reason": "No supporting source was attached.",
                    "_state": "quarantined",
                    "evidence": {"components": ["Q3"]},
                }],
            },
            "synthesis": {
                "present": True,
                "path": "/tmp/project/analysis/synthesis.json",
                "schema_errors": [],
                "verdict_status": "needs_review",
                "verdict_summary": "One risk remains open.",
                "top_risks": [{"finding_id": "deep_review:html1", "reason": "Narrow margin."}],
                "verified_ok": [{"note": "The recorded power sequencing check passed."}],
                "not_checked": [{"reason": "No extraction availability was recorded."}],
                "contradictions": [{
                    "finding_ids": ["deep_review:html1", "deep_review:html2"],
                    "description": "Two recorded assessments disagree.",
                }],
                "extra": {},
            },
            "review_annotations": {
                "present": True,
                "path": "/tmp/project/analysis/review_annotations.json",
                "schema_errors": [],
                "annotations": [{
                    "finding_id": "detector:ABC123",
                    "status": "suppressed",
                    "reason": "The stated condition does not apply to this board revision.",
                    "confidence": "high",
                    "suggested_severity": "info",
                    "reviewed_at": "2026-10-19T11:30:00Z",
                    "_state": "refuted",
                }],
                "reviewer_observations": [{
                    "origin": "llm_novel",
                    "observation": "Consider an additional startup check.",
                    "severity": "info",
                    "confidence": "medium",
                    "reasoning": "The supplied artifacts leave this startup case unclear.",
                    "related_findings": [],
                    "reviewed_at": "2026-10-19T11:30:00Z",
                }],
            },
            "review_plan": {
                "present": True,
                "path": "/tmp/project/analysis/review_plan.json",
                "plan_id": "plan-html-test",
                "created_at": "2026-10-19T10:00:00Z",
                "purpose": "phase4_review",
                "tasks": [
                    {"task_id": "domain_power", "task_type": "deep_review_domain",
                     "tier": "B", "result_path": "analysis/deep_review/power.json",
                     "_result_exists": True},
                    {"task_id": "design_context", "task_type": "review",
                     "tier": "B", "result_path": "analysis/design_context.json",
                     "_result_exists": False},
                ],
            },
            "coverage": {
                "present": True,
                "summary": {"candidates": 1, "missing": 0, "excluded": 0},
                "parts": [{"ref": "U1", "mpn": "EXAMPLE-123", "class": "ic",
                           "pdf_available": True, "extraction_available": False,
                           "vibe_asset_available": True, "vibe_asset_count": 2,
                           "vibe_inventory_current": True, "vibe_inventory_stale": False,
                           "fetch_status": "complete"}],
                "vibe_manifest_present": True,
            },
            "analyzer_triage": {
                "present": True,
                "path": "/tmp/project/analysis/run-html-test/triage.json",
                "finding_count": 3,
                "must_fix_count": 1,
                "triage_table": [{
                    "rule_id": "ERC-001", "count": 3, "real": 1,
                    "likely_fp": 1, "review": 1,
                    "reason": "One item requires manual review.",
                }],
            },
            "analyzers": [
                {"name": "schematic", "present": True, "finding_count": 3,
                 "path": "/tmp/project/analysis/run-html-test/schematic.json"},
                {"name": "pcb", "present": False, "path": "/tmp/project/analysis/run-html-test/pcb.json"},
            ],
            "design_context": {
                "present": True,
                "path": "/tmp/project/analysis/design_context.json",
                "data": {
                    "design_category": {"effective": "industrial", "inferred": "industrial",
                                        "declared": "industrial"},
                    "environment": {"effective": "indoor", "inferred": "indoor",
                                    "declared": "indoor"},
                    "compliance_targets": ["IEC 61000-4-2"],
                    "user_declared_intent": "Control board",
                    "confidence": "medium",
                    "resolution": "declared",
                    "evidence": ["project brief"],
                },
            },
            "board_facts": {
                "present": True,
                "path": "/tmp/project/analysis/run-html-test/board_facts.json",
                "stackup_warnings": ["Multi-ply dielectric noted from raw board data."],
                "stackup_multi_ply": True,
                "impedance_control_noted": False,
                "graphic_copper_net_count": 2,
                "unnetted_copper_count": 1,
                "finding_count": 1,
            },
            "gaps": [{
                "artifact": "board_facts-extra.json",
                "path": "/tmp/project/analysis/board_facts-extra.json",
                "reason": "Optional artifact was not produced.",
            }],
            # Optional metric payloads are not currently required by the context
            # loader. They exercise display of actual numeric values only.
            "metrics": {
                "prompt_tokens": 321,
                "completion_tokens": 123,
                "duration_seconds": 8.5,
                "total_cost_usd": 99.0,
            },
        }

    def test_controls_and_filter_attributes_are_present(self):
        html = render_html.render_html(self.ctx)
        for selector in ('id="filter-severity"', 'id="filter-source"',
                         'id="filter-state"', 'id="filter-search"',
                         'id="visible-count"', 'id="total-count"'):
            self.assertIn(selector, html)
        for attribute in ('data-severity="warning"', 'data-severity="error"',
                          'data-source="deep_review"',
                          'data-source="review_annotations"',
                          'data-state="verified_full"', 'data-state="refuted"',
                          'data-action="resolve"', 'data-action="ignore"',
                          'data-action="reset"', 'data-action="download"',
                          'data-comment-for='):
            self.assertIn(attribute, html)
        self.assertIn("localStorage", html)
        self.assertIn("deep_review_triage_sidecar", html)
        self.assertIn("informal", html)

    def test_all_report_sections_render_from_normalized_context(self):
        html = render_html.render_html(self.ctx)
        for section in (
            'id="synthesis"', 'id="deep-review"', 'id="review-annotations"',
            'id="review-plan"', 'id="analyzer-triage"',
            'id="coverage-availability"', 'id="kicad-ground-truth"',
            'id="metrics"', 'id="interactive-triage"', 'id="explicit-gaps"',
        ):
            self.assertIn(section, html)
        for content in (
            "One risk remains open.", "Input range is 4.5 V to 12 V.",
            "Possible unverified hotspot", "No supporting source was attached.",
            "Contradictions", "refuted", "domain_power", "Result artifacts present",
            "ERC-001", "Local PDF availability", "Extraction artifact", "Multi-ply dielectric noted",
            "Graphic-copper nets", "321", "8.5",
            "board_facts-extra.json", "unknown",
        ):
            self.assertIn(content, html)
        self.assertNotIn("99.0", html)

    def test_all_untrusted_report_text_is_escaped(self):
        self.ctx["analysis_dir"] = '<img src=x onerror="alert(1)">'
        self.ctx["synthesis"]["verdict_summary"] = '<script>alert("synthesis")</script>'
        self.ctx["deep_review"]["findings"][0]["summary"] = '<svg onload="alert(2)">'
        self.ctx["deep_review"]["findings"][0]["evidence"]["datasheet"][0]["quote"] = (
            '<img src=x onerror="alert(3)">'
        )
        self.ctx["deep_review"]["quarantined"][0]["quarantine_reason"] = (
            '</script><script>alert("quarantine")</script>'
        )
        self.ctx["review_plan"]["tasks"][0]["task_id"] = '" onmouseover="alert(4)'
        self.ctx["gaps"][0]["reason"] = '<iframe src="javascript:alert(5)">'

        html = render_html.render_html(self.ctx)
        for raw in (
            '<img src=x onerror="alert(1)">',
            '<script>alert("synthesis")</script>',
            '<svg onload="alert(2)">',
            '<img src=x onerror="alert(3)">',
            '</script><script>alert("quarantine")</script>',
            '" onmouseover="alert(4)',
            '<iframe src="javascript:alert(5)">',
        ):
            self.assertNotIn(raw, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;iframe", html)

    def test_json_script_payload_cannot_close_script_element(self):
        payload = {"run_id": '</script><script>alert("xss")</script>'}
        safe = render_html._safe_json_for_script(payload)
        self.assertNotIn("</script>", safe.lower())
        self.assertEqual(json.loads(safe.replace("<\\/", "</")), payload)

    def test_document_is_static_offline_and_has_no_remote_resources(self):
        html = render_html.render_html(self.ctx)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        self.assertNotRegex(html, r"<script\\b[^>]*\\bsrc\\s*=")
        self.assertNotRegex(html, r"<link\\b[^>]*\\bhref\\s*=")
        self.assertNotRegex(html, r"<[^>]+\\b(?:href|src)\\s*=\\s*['\\\"]https?://")
        self.assertIn("<style>", html)
        self.assertIn("<script>", html)
        self.assertNotIn("data:application", html)

    def test_metrics_show_only_explicit_numeric_tokens_and_duration(self):
        metrics = render_html._actual_metrics(self.ctx)
        self.assertIn(("metrics.prompt_tokens", 321), metrics)
        self.assertIn(("metrics.completion_tokens", 123), metrics)
        self.assertIn(("metrics.duration_seconds", 8.5), metrics)
        self.assertFalse(any("cost" in name.lower() or "usd" in name.lower()
                             for name, _ in metrics))


if __name__ == "__main__":
    unittest.main()
