"""Build a review plan JSON for dispatcher consumption.

design_context is an optional Deep Review input (v2.0 spec §3.B); the
Layer 2 reviewer role is retired (spec §5). Emits a plan describing work
for an **interactive Claude Code session** (or equivalent interactive
agent session): an optional design_context task, plus one
`deep_review_domain` task per requested domain (default: every domain in
`skills/kicad/review/prompts/domain_checklists.json` -- RF, Ethernet,
modem, MCU/memory, power, I/O, interfaces, board EMC). The plan also
carries a `prerequisites[]` entry describing the shared datasheet coverage
roster step (`skills/datasheets/scripts/review_coverage.py`) that the
interactive session must run once, before the Vibe datasheet fetch and
before any task in `tasks[]` is dispatched -- see
`_review_coverage_prerequisite`. Nothing in this script spawns a
subprocess, invokes the Claude CLI, or calls an agent API -- it only emits
a JSON description of work; the interactive session reads the plan and
does the work itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
DOMAIN_CATALOG_PATH = PROMPTS_DIR / "domain_checklists.json"

DESIGN_CONTEXT_PROMPT_PATH = "skills/kicad/review/prompts/design_context.md"
DESIGN_CONTEXT_SCHEMA_PATH = "skills/kicad/review/schemas/design_context.schema.json"
DOMAIN_TASK_PROMPT_PATH = "skills/kicad/review/prompts/domain_task.md"
DEEP_REVIEW_SCHEMA_PATH = "skills/kicad/review/schemas/deep_review.schema.json"
REVIEW_COVERAGE_SCRIPT_PATH = "skills/datasheets/scripts/review_coverage.py"

REVIEW_COVERAGE_PREREQUISITE_NOTE = (
    "review_coverage.json is a shared preparation artifact, not a dispatched "
    "task -- build_review_plan.py does not invoke review_coverage.py itself. "
    "Run it once per plan, before the Vibe datasheet fetch and before any "
    "task below is dispatched, so the fetch step and every domain task read "
    "the same roster instead of re-deriving it independently."
)

REVIEWER_NOTES_SEMANTICS = (
    "Reviewer notes are emphasis only. They direct extra attention within "
    "the full-coverage checklist of every task; they never narrow domain "
    "selection or truncate a domain's scope. See "
    "skills/kicad/review/prompts/domain_task.md -> 'Scope discipline'."
)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_domain_catalog():
    """Load the domain checklist catalog.

    Returns an ordered dict: domain id -> definition
    (`title`, `category_hint`, `input_artifacts`, `checklist`), in the
    catalog file's order.
    """
    data = json.loads(DOMAIN_CATALOG_PATH.read_text())
    return {d["id"]: d for d in data["domains"]}


def _design_context_task(analysis_dir, notes):
    return {
        "task_id": "design_context",
        "task_type": "review",
        "tier": "B",
        "prompt_path": DESIGN_CONTEXT_PROMPT_PATH,
        "result_path": str(analysis_dir / "design_context.json"),
        "result_schema": DESIGN_CONTEXT_SCHEMA_PATH,
        "input_artifacts": [
            str(analysis_dir / "schematic.json"),
            ".kicad-happy.json",
        ],
        "notes": notes,
    }


def _review_coverage_prerequisite(analysis_dir):
    """Describe the shared datasheet coverage roster preparation step.

    Not a dispatched task: ``review_coverage.py`` (skills/datasheets) is a
    standalone, read-only script this function only *describes* -- it is
    never invoked here. The interactive session must run it once, before
    the Vibe datasheet fetch and before any task in `tasks[]` is dispatched,
    so the fetch step and every `deep_review_domain` task read the same
    roster instead of re-deriving candidates/missing/excluded independently.
    """
    schematic_path = analysis_dir / "schematic.json"
    pcb_path = analysis_dir / "pcb.json"
    result_path = analysis_dir / "review_coverage.json"
    return {
        "id": "review_coverage",
        "description": REVIEW_COVERAGE_PREREQUISITE_NOTE,
        "command": (
            f"python3 {REVIEW_COVERAGE_SCRIPT_PATH} {schematic_path} "
            f"[{pcb_path}] --output {result_path}"
        ),
        "script_path": REVIEW_COVERAGE_SCRIPT_PATH,
        "input_artifacts": [str(schematic_path), str(pcb_path)],
        "result_path": str(result_path),
        "run_before": ["vibe_fetch", "domain_dispatch"],
    }


def _domain_task(analysis_dir, domain, notes):
    return {
        "task_id": f"domain_{domain['id']}",
        "task_type": "deep_review_domain",
        "tier": "B",
        "domain": domain["id"],
        "title": domain["title"],
        "category_hint": domain["category_hint"],
        "checklist": list(domain["checklist"]),
        "prompt_path": DOMAIN_TASK_PROMPT_PATH,
        "result_path": str(analysis_dir / "deep_review" / f"{domain['id']}.json"),
        # Each result is a bare findings[] array (fan-out shape per
        # deep-review.md), not a full deep_review.schema.json document --
        # point at the item fragment via JSON Pointer rather than adding a
        # new schema file.
        "result_schema": f"{DEEP_REVIEW_SCHEMA_PATH}#/properties/findings/items",
        "input_artifacts": [
            str(analysis_dir / name) for name in domain["input_artifacts"]
        ],
        "notes": notes,
    }


def build_plan(analysis_dir, *, domains=None, include_design_context=True,
                notes=None):
    """Build a review plan: optional design_context task + domain tasks.

    Args:
        analysis_dir: directory holding analyzer JSON output (schematic.json,
            pcb.json, emc.json, ...) -- same convention as every analyzer
            script in this repo.
        domains: iterable of domain ids to include. `None` (default) means
            every catalog domain, in catalog order (full coverage). Pass an
            explicit subset to narrow which domains get a task; unknown ids
            raise `ValueError`. This is the only thing that narrows scope --
            `notes` never does (see below).
        include_design_context: whether to emit the design_context task.
            Defaults to `True`, preserving the pre-existing plan's task and
            its `result_path` for existing callers. Pass `False` to omit it.
        notes: optional reviewer emphasis string, attached verbatim to every
            task's `notes` field (and surfaced once at the plan level too).
            Per `skills/kicad/review/prompts/domain_task.md`, this is
            emphasis layered on top of full coverage -- it never narrows
            which domains run or what inside a domain gets checked.

    Returns a JSON-serializable dict. Deterministic for identical arguments,
    except `plan_id` / `created_at` (timestamp-derived).
    """
    analysis_dir = Path(analysis_dir)
    catalog = load_domain_catalog()

    if domains is None:
        selected_ids = list(catalog.keys())
    else:
        selected_ids = list(domains)
        unknown = [d for d in selected_ids if d not in catalog]
        if unknown:
            raise ValueError(
                f"unknown domain id(s): {unknown} -- known domains: "
                f"{sorted(catalog)}"
            )

    now = _now_iso()
    tasks = []
    if include_design_context:
        tasks.append(_design_context_task(analysis_dir, notes))
    for domain_id in selected_ids:
        tasks.append(_domain_task(analysis_dir, catalog[domain_id], notes))

    plan = {
        "schema_version": "1.0",
        "plan_id": f"review-{now}",
        "created_at": now,
        "purpose": "phase4_review",
        "reviewer_notes": notes,
        "reviewer_notes_semantics": REVIEWER_NOTES_SEMANTICS,
        "prerequisites": [_review_coverage_prerequisite(analysis_dir)],
        "tasks": tasks,
    }
    return plan


def _resolve_notes(args):
    if args.notes and args.notes_file:
        raise SystemExit("--notes and --notes-file are mutually exclusive")
    if args.notes_file:
        return args.notes_file.read_text().strip()
    if args.notes:
        return args.notes
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--analysis-dir", required=True, type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument(
        "--domains", nargs="+", default=None, metavar="DOMAIN_ID",
        help="Subset of domain ids to include (default: all). See "
             "skills/kicad/review/prompts/domain_checklists.json for ids.",
    )
    p.add_argument(
        "--no-design-context", action="store_true",
        help="Omit the design_context task (included by default).",
    )
    p.add_argument(
        "--notes",
        help="Reviewer emphasis note, applied to every task. Emphasis "
             "only -- never narrows domain coverage.",
    )
    p.add_argument(
        "--notes-file", type=Path,
        help="Read the reviewer emphasis note from a file (mutually "
             "exclusive with --notes).",
    )
    args = p.parse_args()

    notes = _resolve_notes(args)
    try:
        plan = build_plan(
            args.analysis_dir,
            domains=args.domains,
            include_design_context=not args.no_design_context,
            notes=notes,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(out)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
