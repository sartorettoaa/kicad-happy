#!/usr/bin/env python3
"""Blinded benchmark scorer for Deep Review output. Offline, stdlib-only.

    python3 skills/kicad/review/scripts/score_review.py \
        analysis/deep_review.json --benchmark /path/to/private_benchmark.json

Compares the canonical, durable `deep_review.json` (schema at
`schemas/deep_review.schema.json`) against a private benchmark of known
issues and reports recall, unmatched generated findings (potential false
positives), duplicate matches, severity agreement, and evidence/artifact
preconditions.

Privacy model (hard requirement -- do not weaken):
  - The benchmark is never bundled with this repo and is never read unless
    the caller passes `--benchmark <path>` explicitly on the command line.
    There is no default/fallback benchmark path.
  - This script is a standalone, human-invoked scoring tool. It must never
    be wired into `build_review_plan.py`, `deep_review_gate.py`,
    `merge_annotations.py`, or any other step a review agent runs during a
    normal pass -- the benchmark's existence and contents must stay
    invisible to the agent being scored.
  - By default the report omits benchmark issue `description`/`text`
    content, reporting only structural fields (issue_id, category,
    severity, target) that are not proprietary. Pass
    `--include-benchmark-text` to add the full phrasing back in for
    interactive human debugging; never do this when writing `--output` to
    a location the normal pipeline or a review agent might later read.
  - Never point `--output` at the `analysis/` pipeline tree (or any other
    location a normal run or review agent reads) -- write scores to a
    private, out-of-band location (stdout, or an explicit path outside the
    project's analysis output).

Benchmark JSON shape (defined by this tool; not a kicad-happy pipeline
artifact, so it has no entry under schemas/):
  {
    "benchmark_id": "<string>",
    "required_evidence": [
      {"id": "<string>", "path": "<relative or absolute path>",
       "targets": ["<target id>", ...]}
    ],
    "issues": [
      {"issue_id": "<string>", "category": "<string>",
       "severity": "error|warning|info", "target": "<target id, optional>",
       "anchors": {"components": [...], "nets": [...], "pins": [...]},
       "text": ["<phrase that should appear in a matching finding>", ...],
       "description": "<optional human-readable note>"}
    ]
  }
`anchors` requires >=1 of components/nets/pins per issue (conservative
matching needs a concrete design identity, not just topic similarity).
`text` is optional; when present, matching requires at least one phrase
(normalized, substring) to appear in the finding's summary/description/
recommendation. An issue whose `target` names a `required_evidence` entry
whose `path` does not resolve to a file is marked unscorable (not missed)
and the overall `status` becomes "partial".

Matching (conservative, deterministic, no LLM/network):
  A generated finding matches a benchmark issue iff (a) its evidence
  anchors (components, or the component half of a pin, or nets, or exact
  pins) overlap the issue's anchors, AND (b) if the issue lists `text`
  phrases, at least one phrase is found (Unicode NFKC + lowercase +
  non-alnum-collapsed substring match) in the finding's summary +
  description + recommendation.

Exit codes: 0 all scorable issues matched and no missing evidence
(status "complete"); 1 scoring completed but status is "partial" (some
issues unscorable due to missing required evidence, or recall < 1.0 is
NOT by itself a failure -- only unscorable targets flip this; see
`status`); 2 I/O, argument, or benchmark-shape error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SEVERITIES = ("error", "warning", "info")
ANCHOR_KEYS = ("components", "nets", "pins")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_text(s) -> str:
    """Conservative text normalization: Unicode NFKC, lowercase, collapse
    everything that isn't a letter/digit to single spaces. Mirrors the
    normalization style used by deep_review_gate.py's quote matching, kept
    local here so this script has no cross-file dependency."""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _norm_anchor(s) -> str:
    return str(s).strip().upper()


def _finding_anchors(finding: dict) -> dict:
    """Union of evidence-nested and top-level-mirrored anchors.

    `deep_review_gate.py` mirrors `evidence.{components,nets,pins}` onto
    the finding's top level once gated; un-gated findings only carry the
    `evidence` form. Scoring must work against either shape.
    """
    ev = finding.get("evidence") or {}
    out = {}
    for key in ANCHOR_KEYS:
        out[key] = list(ev.get(key) or []) + list(finding.get(key) or [])
    return out


def _effective_components(anchors: dict) -> set:
    """Component refs cited directly, plus the component half of any pin
    citation (a finding/issue citing U3.7 also counts as citing U3)."""
    comps = {_norm_anchor(c) for c in anchors.get("components") or []}
    for pin in anchors.get("pins") or []:
        ref = str(pin).rsplit(".", 1)[0]
        comps.add(_norm_anchor(ref))
    return comps


def anchor_overlap(a: dict, b: dict) -> bool:
    """True if two anchor dicts ({components,nets,pins}) share >=1 design
    identity: a component (directly or via a shared pin's component),
    a net, or an exact pin."""
    if _effective_components(a) & _effective_components(b):
        return True
    nets_a = {_norm_anchor(n) for n in a.get("nets") or []}
    nets_b = {_norm_anchor(n) for n in b.get("nets") or []}
    if nets_a & nets_b:
        return True
    pins_a = {_norm_anchor(p) for p in a.get("pins") or []}
    pins_b = {_norm_anchor(p) for p in b.get("pins") or []}
    if pins_a & pins_b:
        return True
    return False


def _finding_text(finding: dict) -> str:
    parts = [finding.get("summary") or "", finding.get("description") or "",
             finding.get("recommendation") or ""]
    return " ".join(p for p in parts if p)


def text_match(phrases, haystack: str) -> bool:
    """True if `phrases` is empty (anchor-only issue), or any phrase
    (normalized) appears as a substring of the normalized haystack."""
    if not phrases:
        return True
    hay = _norm_text(haystack)
    for phrase in phrases:
        needle = _norm_text(phrase)
        if needle and needle in hay:
            return True
    return False


def _validate_benchmark_shape(benchmark) -> list:
    """Lightweight structural validation (no schema file -- this format is
    owned entirely by this standalone tool). Returns a list of error
    strings; empty means valid."""
    errors = []
    if not isinstance(benchmark, dict):
        return ["benchmark root must be a JSON object"]
    issues = benchmark.get("issues")
    if not isinstance(issues, list):
        errors.append("benchmark.issues must be an array")
        issues = []
    for i, issue in enumerate(issues):
        if not isinstance(issue, dict):
            errors.append(f"issues[{i}] must be an object")
            continue
        label = issue.get("issue_id") or f"issues[{i}]"
        if not issue.get("issue_id"):
            errors.append(f"issues[{i}] missing required field 'issue_id'")
        if issue.get("severity") not in SEVERITIES:
            errors.append(
                f"{label}: severity {issue.get('severity')!r} not one of {SEVERITIES}")
        anchors = issue.get("anchors")
        if not isinstance(anchors, dict) or not any(
                anchors.get(k) for k in ANCHOR_KEYS):
            errors.append(
                f"{label}: must declare >=1 anchor (components/nets/pins) "
                "for conservative matching")
        text = issue.get("text")
        if text is not None and not isinstance(text, list):
            errors.append(f"{label}: 'text' must be an array of phrases")
    req_ev = benchmark.get("required_evidence")
    if req_ev is not None:
        if not isinstance(req_ev, list):
            errors.append("benchmark.required_evidence must be an array")
        else:
            for i, req in enumerate(req_ev):
                if not isinstance(req, dict) or not req.get("path"):
                    errors.append(f"required_evidence[{i}] missing 'path'")
    return errors


def check_required_evidence(benchmark: dict, project_dir) -> tuple:
    """Resolve each `required_evidence[].path` against `project_dir`.

    Returns (evidence_checks, missing_targets):
      evidence_checks: list of {id, path, available, targets}
      missing_targets: set of target ids affected by >=1 missing path
    """
    project_dir = Path(project_dir)
    checks = []
    missing_targets = set()
    for req in benchmark.get("required_evidence") or []:
        path = req.get("path")
        resolved = project_dir / path if path else None
        available = bool(resolved and resolved.is_file())
        targets = list(req.get("targets") or [])
        checks.append({
            "id": req.get("id"),
            "path": path,
            "available": available,
            "targets": targets,
        })
        if not available:
            missing_targets.update(targets)
    return checks, missing_targets


def classify_issues(benchmark: dict, missing_targets: set) -> tuple:
    """Split benchmark issues into (scorable, unscorable).

    An issue is unscorable (not missed) iff its `target` names a
    `required_evidence` entry whose artifact path did not resolve.
    Issues with no `target`, or a target not tied to any required
    evidence, are always scorable.
    """
    scorable, unscorable = [], []
    for issue in benchmark.get("issues") or []:
        target = issue.get("target")
        if target and target in missing_targets:
            unscorable.append(issue)
        else:
            scorable.append(issue)
    return scorable, unscorable


def match_issues(scorable_issues: list, findings: list) -> tuple:
    """Match each scorable benchmark issue against generated findings.

    Returns (matches, missed):
      matches: list of {"issue": <issue>, "finding_indices": [int, ...]}
               (>1 index means the same issue was independently flagged
               by multiple findings -- a duplicate match)
      missed:  list of issues with zero matching findings
    """
    matches, missed = [], []
    for issue in scorable_issues:
        issue_anchors = issue.get("anchors") or {}
        phrases = issue.get("text") or []
        hits = []
        for idx, finding in enumerate(findings):
            if not anchor_overlap(issue_anchors, _finding_anchors(finding)):
                continue
            if not text_match(phrases, _finding_text(finding)):
                continue
            hits.append(idx)
        if hits:
            matches.append({"issue": issue, "finding_indices": hits})
        else:
            missed.append(issue)
    return matches, missed


def _issue_summary(issue: dict, *, include_text: bool) -> dict:
    out = {
        "issue_id": issue.get("issue_id"),
        "category": issue.get("category"),
        "severity": issue.get("severity"),
        "target": issue.get("target"),
    }
    if include_text:
        out["description"] = issue.get("description")
        out["text"] = issue.get("text")
    return out


def build_report(deep_review: dict, benchmark: dict, project_dir, *,
                  include_benchmark_text: bool = False) -> dict:
    """Score `deep_review`'s findings[] against `benchmark`. Pure function;
    performs no I/O (callers resolve/read the JSON inputs)."""
    findings = list(deep_review.get("findings") or [])
    quarantined = list(deep_review.get("quarantined") or [])

    evidence_checks, missing_targets = check_required_evidence(benchmark, project_dir)
    scorable, unscorable = classify_issues(benchmark, missing_targets)
    matches, missed = match_issues(scorable, findings)

    matched_finding_indices = set()
    for m in matches:
        matched_finding_indices.update(m["finding_indices"])
    unmatched_findings = [i for i in range(len(findings))
                           if i not in matched_finding_indices]

    severity_summary = {}
    for sev in SEVERITIES:
        sev_scorable = [i for i in scorable if i.get("severity") == sev]
        sev_matched = [m for m in matches if m["issue"].get("severity") == sev]
        total = len(sev_scorable)
        matched_n = len(sev_matched)
        severity_summary[sev] = {
            "total": total,
            "matched": matched_n,
            "missed": total - matched_n,
            "recall": (matched_n / total) if total else None,
        }

    total_scorable = len(scorable)
    total_matched = len(matches)
    overall_recall = (total_matched / total_scorable) if total_scorable else None

    severity_agreement = []
    agree_count = disagree_count = 0
    for m in matches:
        issue = m["issue"]
        expected = issue.get("severity")
        for idx in m["finding_indices"]:
            found_sev = findings[idx].get("severity")
            agree = found_sev == expected
            agree_count += int(agree)
            disagree_count += int(not agree)
            severity_agreement.append({
                "issue_id": issue.get("issue_id"),
                "finding_index": idx,
                "finding_id": findings[idx].get("finding_id"),
                "expected_severity": expected,
                "finding_severity": found_sev,
                "agree": agree,
            })

    duplicate_matches = [
        {
            "issue_id": m["issue"].get("issue_id"),
            "finding_indices": m["finding_indices"],
            "finding_ids": [findings[i].get("finding_id") for i in m["finding_indices"]],
        }
        for m in matches if len(m["finding_indices"]) > 1
    ]

    status = "complete" if not unscorable else "partial"

    return {
        "schema_version": "1.0",
        "scored_at": _now_iso(),
        "benchmark_id": benchmark.get("benchmark_id"),
        "deep_review_run_id": deep_review.get("produced_for_run_id"),
        "status": status,
        "evidence_preconditions": evidence_checks,
        "unscorable_targets": sorted(missing_targets),
        "summary": {
            "total_issues": len(benchmark.get("issues") or []),
            "scorable_issues": total_scorable,
            "unscorable_issues": len(unscorable),
            "matched": total_matched,
            "missed": len(missed),
            "recall": overall_recall,
            "by_severity": severity_summary,
        },
        "matched_issues": [
            {
                **_issue_summary(m["issue"], include_text=include_benchmark_text),
                "finding_indices": m["finding_indices"],
                "finding_ids": [findings[i].get("finding_id") for i in m["finding_indices"]],
            }
            for m in matches
        ],
        "missed_issues": [
            _issue_summary(issue, include_text=include_benchmark_text) for issue in missed
        ],
        "unscorable_issue_details": [
            _issue_summary(issue, include_text=include_benchmark_text) for issue in unscorable
        ],
        "duplicate_matches": duplicate_matches,
        "severity_agreement": {
            "agree": agree_count,
            "disagree": disagree_count,
            "mismatches": [sa for sa in severity_agreement if not sa["agree"]],
        },
        "unmatched_generated_findings": [
            {
                "index": i,
                "finding_id": findings[i].get("finding_id"),
                "category": findings[i].get("category"),
                "severity": findings[i].get("severity"),
                "summary": findings[i].get("summary"),
            }
            for i in unmatched_findings
        ],
        "quarantined_count": len(quarantined),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Score a deep_review.json against a private benchmark "
                     "of known issues. Offline, stdlib-only, no LLM calls.")
    ap.add_argument("deep_review_json", type=Path,
                     help="Path to the canonical deep_review.json to score")
    ap.add_argument("--benchmark", required=True, type=Path,
                     help="Path to the private benchmark JSON (never bundled "
                          "with this repo -- must be supplied explicitly)")
    ap.add_argument("--project-dir", type=Path, default=Path("."),
                     help="Root used to resolve required_evidence paths "
                          "(default: current directory)")
    ap.add_argument("--output", type=Path,
                     help="Write the score report here instead of stdout. "
                          "Never point this at analysis/ or any location a "
                          "normal run or review agent reads.")
    ap.add_argument("--include-benchmark-text", action="store_true",
                     help="Include benchmark issue description/text phrases "
                          "in the report (off by default).")
    args = ap.parse_args()

    try:
        deep_review = json.loads(args.deep_review_json.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"score_review: cannot read deep_review JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(deep_review, dict) or "findings" not in deep_review:
        print("score_review: deep_review JSON missing required 'findings' array",
              file=sys.stderr)
        return 2

    try:
        benchmark = json.loads(args.benchmark.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"score_review: cannot read benchmark JSON: {e}", file=sys.stderr)
        return 2

    shape_errors = _validate_benchmark_shape(benchmark)
    if shape_errors:
        print("score_review: benchmark file is malformed:", file=sys.stderr)
        for err in shape_errors:
            print(f"  {err}", file=sys.stderr)
        return 2

    report = build_report(deep_review, benchmark, args.project_dir,
                           include_benchmark_text=args.include_benchmark_text)

    out = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(out)
    else:
        print(out)

    s = report["summary"]
    print(f"score_review: status={report['status']} "
          f"recall={s['recall']} matched={s['matched']}/{s['scorable_issues']} "
          f"unscorable={s['unscorable_issues']}", file=sys.stderr)
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
