#!/usr/bin/env python3
"""Lightweight tests for run_phase4_exercise.py's step 3.

Only step 3 (build review plan + assert every expected domain task is
present) is covered here -- steps 1/2/4/5 need the gitignored Phase 4
fixture and a full Layer 1 analyzer run, which is out of scope for a fast
unit test. Step 3 only needs an `analysis/` directory to write into, so it
can run in isolation.

Run with:
    python3 skills/kicad/review/scripts/test_run_phase4_exercise.py
    python3 -m unittest skills/kicad/review/scripts/test_run_phase4_exercise.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import run_phase4_exercise as rpe  # noqa: E402


class Step3BuildReviewPlanTests(unittest.TestCase):
    def test_builds_plan_with_design_context_and_every_domain_task(self):
        """End-to-end (real build_review_plan.py subprocess): the plan
        step_3 writes must satisfy its own assertions without raising,
        and must contain design_context + one task per EXPECTED_DOMAIN_IDS.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp)
            (fixture_dir / "analysis").mkdir()

            rpe.step_3_build_review_plan(fixture_dir)  # must not raise

            plan_path = fixture_dir / "analysis" / "review_plan.json"
            self.assertTrue(plan_path.exists())
            plan = json.loads(plan_path.read_text())
            task_ids = {t["task_id"] for t in plan["tasks"]}
            self.assertIn("design_context", task_ids)
            for domain_id in rpe.EXPECTED_DOMAIN_IDS:
                self.assertIn(f"domain_{domain_id}", task_ids)
            self.assertEqual(len(task_ids), 1 + len(rpe.EXPECTED_DOMAIN_IDS))

    def test_raises_if_plan_is_missing_a_domain_task(self):
        """step_3 must not silently accept a plan missing a domain task --
        stub `_run` to write a deliberately incomplete plan (no real
        build_review_plan.py invocation needed for this negative case)."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp)
            (fixture_dir / "analysis").mkdir()

            def _fake_run(cmd, cwd=None):
                out_path = Path(cmd[cmd.index("--output") + 1])
                incomplete = {
                    "tasks": [{"task_id": "design_context"}] + [
                        {"task_id": f"domain_{d}"}
                        for d in rpe.EXPECTED_DOMAIN_IDS if d != "rf"
                    ],
                }
                out_path.write_text(json.dumps(incomplete))

            orig_run = rpe._run
            rpe._run = _fake_run
            try:
                with self.assertRaises(AssertionError):
                    rpe.step_3_build_review_plan(fixture_dir)
            finally:
                rpe._run = orig_run

    def test_raises_if_plan_is_missing_design_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp)
            (fixture_dir / "analysis").mkdir()

            def _fake_run(cmd, cwd=None):
                out_path = Path(cmd[cmd.index("--output") + 1])
                incomplete = {
                    "tasks": [{"task_id": f"domain_{d}"}
                              for d in rpe.EXPECTED_DOMAIN_IDS],
                }
                out_path.write_text(json.dumps(incomplete))

            orig_run = rpe._run
            rpe._run = _fake_run
            try:
                with self.assertRaises(AssertionError):
                    rpe.step_3_build_review_plan(fixture_dir)
            finally:
                rpe._run = orig_run


if __name__ == "__main__":
    unittest.main()
