#!/usr/bin/env python3
"""Unit tests for build_review_plan.py.

Covers: legacy single-arg `build_plan(analysis_dir)` behavior (design_context
task path/shape preserved), the domain task catalog and filtering,
`--notes` / `--notes-file` semantics (emphasis only, never scope-narrowing),
`--no-design-context`, deterministic output modulo timestamp fields, and the
CLI entry point (including error paths).

Run with:
    python3 skills/kicad/review/scripts/test_build_review_plan.py
    python3 -m unittest skills/kicad/review/scripts/test_build_review_plan.py
"""

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

import build_review_plan as brp  # noqa: E402

EXPECTED_DOMAIN_IDS = [
    "rf", "ethernet", "modem", "mcu_memory", "power", "io", "interfaces",
    "board_emc",
]


def _strip_timestamps(plan):
    plan = dict(plan)
    plan.pop("plan_id", None)
    plan.pop("created_at", None)
    return plan


class DomainCatalogTests(unittest.TestCase):
    def test_catalog_has_all_expected_domains_in_order(self):
        catalog = brp.load_domain_catalog()
        self.assertEqual(list(catalog.keys()), EXPECTED_DOMAIN_IDS)

    def test_each_domain_has_required_fields(self):
        catalog = brp.load_domain_catalog()
        for domain_id, domain in catalog.items():
            self.assertTrue(domain["title"])
            self.assertEqual(domain["category_hint"], domain_id)
            self.assertIsInstance(domain["checklist"], list)
            self.assertGreater(len(domain["checklist"]), 0)
            self.assertIsInstance(domain["input_artifacts"], list)
            self.assertGreater(len(domain["input_artifacts"]), 0)


class LegacyBehaviorTests(unittest.TestCase):
    """build_plan(analysis_dir) must still work exactly as before for the
    design_context task -- existing callers (e.g. run_phase4_exercise.py)
    depend on this exact result_path and prompt_path.
    """

    def test_single_positional_arg_still_works(self):
        plan = brp.build_plan(Path("analysis"))
        self.assertIn("tasks", plan)

    def test_design_context_task_shape_unchanged(self):
        plan = brp.build_plan(Path("analysis"))
        design_context_tasks = [t for t in plan["tasks"]
                                 if t["task_id"] == "design_context"]
        self.assertEqual(len(design_context_tasks), 1)
        task = design_context_tasks[0]
        self.assertEqual(task["task_type"], "review")
        self.assertEqual(task["tier"], "B")
        self.assertEqual(task["prompt_path"],
                          "skills/kicad/review/prompts/design_context.md")
        self.assertEqual(task["result_path"], str(Path("analysis") / "design_context.json"))
        self.assertEqual(task["result_schema"],
                          "skills/kicad/review/schemas/design_context.schema.json")
        self.assertEqual(task["input_artifacts"], [
            str(Path("analysis") / "schematic.json"),
            ".kicad-happy.json",
        ])

    def test_design_context_task_is_first_by_default(self):
        plan = brp.build_plan(Path("analysis"))
        self.assertEqual(plan["tasks"][0]["task_id"], "design_context")

    def test_default_plan_now_also_includes_all_domains(self):
        plan = brp.build_plan(Path("analysis"))
        domain_task_ids = {t["task_id"] for t in plan["tasks"]
                            if t.get("task_type") == "deep_review_domain"}
        self.assertEqual(domain_task_ids,
                          {f"domain_{d}" for d in EXPECTED_DOMAIN_IDS})
        self.assertEqual(len(plan["tasks"]), 1 + len(EXPECTED_DOMAIN_IDS))

    def test_plan_level_fields_present(self):
        plan = brp.build_plan(Path("analysis"))
        self.assertEqual(plan["schema_version"], "1.0")
        self.assertEqual(plan["purpose"], "phase4_review")
        self.assertTrue(plan["plan_id"].startswith("review-"))


class ReviewCoveragePrerequisiteTests(unittest.TestCase):
    """review_coverage.json is a plan-level prerequisite, not a task --
    it must run before the Vibe fetch and before any task dispatch."""

    def test_prerequisites_present_regardless_of_domain_selection(self):
        plan = brp.build_plan(Path("analysis"), domains=["power"],
                               include_design_context=False)
        self.assertIn("prerequisites", plan)
        self.assertEqual(len(plan["prerequisites"]), 1)

    def test_review_coverage_prerequisite_shape(self):
        analysis_dir = Path("analysis")
        plan = brp.build_plan(analysis_dir)
        prereq = plan["prerequisites"][0]
        self.assertEqual(prereq["id"], "review_coverage")
        self.assertEqual(prereq["script_path"],
                          "skills/datasheets/scripts/review_coverage.py")
        self.assertEqual(prereq["result_path"],
                          str(analysis_dir / "review_coverage.json"))
        self.assertEqual(prereq["input_artifacts"], [
            str(analysis_dir / "schematic.json"),
            str(analysis_dir / "pcb.json"),
        ])
        self.assertEqual(prereq["run_before"], ["vibe_fetch", "domain_dispatch"])
        self.assertIn("review_coverage.py", prereq["command"])
        self.assertIn(str(analysis_dir / "schematic.json"), prereq["command"])
        self.assertIn(str(analysis_dir / "pcb.json"), prereq["command"])

    def test_review_coverage_prerequisite_not_a_dispatched_task(self):
        plan = brp.build_plan(Path("analysis"))
        task_ids = [t.get("task_id") for t in plan["tasks"]]
        self.assertNotIn("review_coverage", task_ids)


class DeterminismTests(unittest.TestCase):
    def test_deterministic_modulo_timestamp(self):
        plan_a = brp.build_plan(Path("analysis"), notes="watch the power tree")
        plan_b = brp.build_plan(Path("analysis"), notes="watch the power tree")
        self.assertEqual(_strip_timestamps(plan_a), _strip_timestamps(plan_b))

    def test_domain_filter_is_deterministic(self):
        plan_a = brp.build_plan(Path("analysis"), domains=["rf", "power"],
                                 include_design_context=False)
        plan_b = brp.build_plan(Path("analysis"), domains=["rf", "power"],
                                 include_design_context=False)
        self.assertEqual(_strip_timestamps(plan_a), _strip_timestamps(plan_b))


class DomainTaskShapeTests(unittest.TestCase):
    def test_domain_task_fields(self):
        plan = brp.build_plan(Path("analysis"), domains=["power"],
                               include_design_context=False)
        self.assertEqual(len(plan["tasks"]), 1)
        task = plan["tasks"][0]
        self.assertEqual(task["task_id"], "domain_power")
        self.assertEqual(task["task_type"], "deep_review_domain")
        self.assertEqual(task["domain"], "power")
        self.assertEqual(task["category_hint"], "power")
        self.assertEqual(task["prompt_path"],
                          "skills/kicad/review/prompts/domain_task.md")
        self.assertTrue(task["checklist"])
        self.assertEqual(
            task["result_path"],
            str(Path("analysis") / "deep_review" / "power.json"))
        self.assertEqual(
            task["result_schema"],
            "skills/kicad/review/schemas/deep_review.schema.json"
            "#/properties/findings/items")

    def test_input_artifacts_and_result_path_are_under_analysis_dir(self):
        analysis_dir = Path("/tmp/project/analysis")
        plan = brp.build_plan(analysis_dir, domains=["ethernet"],
                               include_design_context=False)
        task = plan["tasks"][0]
        self.assertTrue(task["result_path"].startswith(str(analysis_dir)))
        self.assertTrue(task["result_path"].endswith("ethernet.json"))
        for artifact in task["input_artifacts"]:
            self.assertTrue(artifact.startswith(str(analysis_dir)))

    def test_result_schema_json_pointer_resolves(self):
        schema_path = (Path(SCRIPT_DIR).resolve().parents[0]
                       / "schemas" / "deep_review.schema.json")
        schema = json.loads(schema_path.read_text())
        node = schema
        for part in "#/properties/findings/items".lstrip("#/").split("/"):
            node = node[part]
        self.assertEqual(node["required"],
                          ["detector", "category", "severity", "confidence",
                           "summary", "evidence"])


class DomainSelectionTests(unittest.TestCase):
    def test_explicit_subset_preserves_requested_order(self):
        plan = brp.build_plan(Path("analysis"), domains=["io", "rf"],
                               include_design_context=False)
        self.assertEqual([t["task_id"] for t in plan["tasks"]],
                          ["domain_io", "domain_rf"])

    def test_unknown_domain_raises(self):
        with self.assertRaises(ValueError):
            brp.build_plan(Path("analysis"), domains=["not_a_real_domain"])

    def test_no_design_context_flag_omits_task(self):
        plan = brp.build_plan(Path("analysis"), include_design_context=False)
        task_ids = [t["task_id"] for t in plan["tasks"]]
        self.assertNotIn("design_context", task_ids)
        self.assertEqual(len(task_ids), len(EXPECTED_DOMAIN_IDS))


class NotesSemanticsTests(unittest.TestCase):
    def test_notes_applied_to_every_task_and_plan_level(self):
        plan = brp.build_plan(Path("analysis"), notes="prioritize isolation")
        self.assertEqual(plan["reviewer_notes"], "prioritize isolation")
        for task in plan["tasks"]:
            self.assertEqual(task["notes"], "prioritize isolation")

    def test_notes_semantics_documented_and_says_no_narrowing(self):
        plan = brp.build_plan(Path("analysis"))
        semantics = plan["reviewer_notes_semantics"].lower()
        self.assertIn("emphasis", semantics)
        self.assertIn("never narrow", semantics)

    def test_no_notes_still_covers_every_domain(self):
        plan = brp.build_plan(Path("analysis"), notes=None)
        domain_ids = {t["domain"] for t in plan["tasks"] if "domain" in t}
        self.assertEqual(domain_ids, set(EXPECTED_DOMAIN_IDS))


class CliTests(unittest.TestCase):
    def _run_cli(self, *extra_args):
        with tempfile.TemporaryDirectory() as tmp:
            analysis_dir = Path(tmp) / "analysis"
            out_path = Path(tmp) / "plan.json"
            cmd = [sys.executable,
                   os.path.join(SCRIPT_DIR, "build_review_plan.py"),
                   "--analysis-dir", str(analysis_dir),
                   "--output", str(out_path)] + list(extra_args)
            result = subprocess.run(cmd, capture_output=True, text=True)
            plan = None
            if out_path.exists():
                plan = json.loads(out_path.read_text())
            return result, plan

    def test_cli_default_emits_design_context_and_all_domains(self):
        result, plan = self._run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        task_ids = [t["task_id"] for t in plan["tasks"]]
        self.assertIn("design_context", task_ids)
        for d in EXPECTED_DOMAIN_IDS:
            self.assertIn(f"domain_{d}", task_ids)

    def test_cli_domains_and_no_design_context(self):
        result, plan = self._run_cli("--domains", "rf", "ethernet",
                                      "--no-design-context")
        self.assertEqual(result.returncode, 0, result.stderr)
        task_ids = [t["task_id"] for t in plan["tasks"]]
        self.assertEqual(task_ids, ["domain_rf", "domain_ethernet"])

    def test_cli_notes_flag(self):
        result, plan = self._run_cli("--notes", "hello from the reviewer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["reviewer_notes"], "hello from the reviewer")

    def test_cli_notes_file_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = Path(tmp) / "notes.txt"
            notes_path.write_text("watch the regulator sequencing\n")
            result, plan = self._run_cli("--notes-file", str(notes_path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(plan["reviewer_notes"],
                              "watch the regulator sequencing")

    def test_cli_notes_and_notes_file_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = Path(tmp) / "notes.txt"
            notes_path.write_text("x")
            result, _ = self._run_cli("--notes", "x", "--notes-file",
                                       str(notes_path))
            self.assertNotEqual(result.returncode, 0)

    def test_cli_unknown_domain_errors(self):
        result, _ = self._run_cli("--domains", "not_a_real_domain")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown domain", result.stderr)


if __name__ == "__main__":
    unittest.main()
