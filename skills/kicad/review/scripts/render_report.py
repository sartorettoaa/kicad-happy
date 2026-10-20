#!/usr/bin/env python3
"""Static Deep Review report renderer (v2.0 addendum to report-generation.md).

One command, zero server, zero GUI:

    python3 skills/kicad/review/scripts/render_report.py --analysis-dir analysis/

Reads the canonical `analysis/deep_review.json` plus whatever optional
artifacts are present (`synthesis.json`, `review_plan.json`,
`review_annotations.json`, the merge report, `design_context.json`, the
per-analyzer envelopes, `analyzer_triage.py`'s `triage.json`, and
`board_facts.py`'s `board_facts.json`, used for the Analyzer Verification /
Analyzer Finding Triage / Board Facts summaries) and renders a Markdown
report (canonical output) and, optionally, a self-contained static HTML
triage view.

This tool never writes outside the analysis directory unless you pass an
explicit `--output`/`--html-output` path — it does not touch the source
KiCad project, datasheets, or any other repo file.

Generating this report is not a verification step: every claim in the
output traces back to an existing artifact's own recorded state
(`evidence_checked`, `quarantine_reason`, a reviewer's `status`, an
`analyzer_triage.py` verdict, or an explicit "not found" gap). See the
"Evidence & Verification States" section of the rendered report for the
full legend.

Exit codes: 0 always, except 2 for I/O errors writing requested output
files. This is a renderer, not a gate — `deep_review_gate.py` and
`check_report_sections.py` already own pass/fail semantics.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_data import build_context       # noqa: E402
from render_markdown import render_markdown  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--analysis-dir", required=True,
                     help="Directory containing deep_review.json and friends")
    ap.add_argument("--run", default=None,
                     help="Run id for resolving analyzer envelopes (default: manifest.json 'current')")
    ap.add_argument("--project-dir", default=".",
                     help="Project root, used to resolve relative review_plan.json result_path entries")
    ap.add_argument("--datasheets-dir", default="datasheets")
    ap.add_argument("--output", default=None,
                     help="Markdown output path (default: <analysis-dir>/deep_review_report.md)")
    ap.add_argument("--html", action="store_true",
                     help="Also render the static HTML triage view")
    ap.add_argument("--html-output", default=None,
                     help="HTML output path (default: <analysis-dir>/deep_review_report.html)")
    ap.add_argument("--synthesis", default=None, help="Explicit path to synthesis.json")
    ap.add_argument("--review-plan", default=None, help="Explicit path to review_plan.json")
    ap.add_argument("--review-annotations", default=None, help="Explicit path to review_annotations.json")
    ap.add_argument("--merge-report", default=None, help="Explicit path to the merge report JSON")
    ap.add_argument("--design-context", default=None, help="Explicit path to design_context.json")
    ap.add_argument("--coverage", default=None, help="Explicit path to review_coverage.json")
    ap.add_argument("--execution-metrics", default=None, help="Explicit path to execution_metrics.json")
    ap.add_argument("--kicad-cli", default=None, help="Explicit path to kicad_cli_review result JSON")
    ap.add_argument("--datasheet-fetch", default=None, help="Explicit path to datasheet_fetch.json")
    ap.add_argument("--stdout", action="store_true",
                     help="Also print the Markdown report to stdout")
    args = ap.parse_args(argv)

    ctx = build_context(
        analysis_dir=args.analysis_dir,
        run=args.run,
        project_dir=args.project_dir,
        datasheets_dir=args.datasheets_dir,
        synthesis_path=args.synthesis,
        review_plan_path=args.review_plan,
        review_annotations_path=args.review_annotations,
        merge_report_path=args.merge_report,
        design_context_path=args.design_context,
        coverage_path=args.coverage,
        execution_metrics_path=args.execution_metrics,
        kicad_cli_path=args.kicad_cli,
        datasheet_fetch_path=args.datasheet_fetch,
    )

    md = render_markdown(ctx)
    md_path = Path(args.output) if args.output else Path(args.analysis_dir) / "deep_review_report.md"
    try:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
    except OSError as e:
        print(f"render_report: cannot write {md_path}: {e}", file=sys.stderr)
        return 2
    print(f"render_report: wrote {md_path}")

    if args.stdout:
        print(md)

    if args.html:
        from render_html import render_html  # imported lazily: not needed for MD-only runs
        html = render_html(ctx)
        html_path = Path(args.html_output) if args.html_output else Path(args.analysis_dir) / "deep_review_report.html"
        try:
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(html, encoding="utf-8")
        except OSError as e:
            print(f"render_report: cannot write {html_path}: {e}", file=sys.stderr)
            return 2
        print(f"render_report: wrote {html_path}")

    n_gaps = len(ctx.get("gaps") or [])
    n_findings = len(ctx["deep_review"]["findings"])
    n_quarantined = len(ctx["deep_review"]["quarantined"])
    coverage = ctx.get("coverage") or {}
    coverage_summary = coverage.get("summary") or {}
    candidate_count = coverage_summary.get("candidates", len(coverage.get("candidates") or []))
    print(f"render_report: {n_findings} finding(s), {n_quarantined} quarantined, "
          f"{candidate_count} datasheet candidate(s), {n_gaps} missing/invalid artifact(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
