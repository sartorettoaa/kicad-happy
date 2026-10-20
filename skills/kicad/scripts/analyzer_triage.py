#!/usr/bin/env python3
"""Triage of kicad-happy analyzer findings against corroborating evidence.

On a real multi-layer board the analyzers can produce a pile of error-level
findings where most are false positives: bounding-box keepout tests,
mousebite courtyards, a mis-parsed multi-ply stackup, nets routed with
graphic copper the track/via walk ignores, generic edge-clearance
guidelines the real DRC already clears... Printing every one as "must-fix"
drowns the handful that matter.

This module tags each finding with a ``triage`` verdict so a report can
separate real issues from noise, and records *why* for every downgrade:

  * ``real``      — keep severity, eligible for must-fix
  * ``likely_fp`` — downgraded to info, reason recorded (evidence-based rule)
  * ``review``    — keep severity but flag: plausible false positive, a
                    human/AI must confirm (the evidence available does not
                    *prove* the finding wrong — e.g. the only evidence is a
                    bounding-box heuristic, never grounds for a downgrade)

Every rule is evidence-driven (real KiCad DRC results, board_facts.py's
corrected stackup / graphic-copper nets, named mechanical references) —
never a blanket rule-id mute. Findings are never discarded: triage_findings
returns one annotated copy per input finding, in the same order, and the
original ``severity``/all other keys are preserved verbatim except on an
explicit ``likely_fp`` downgrade (where the original value is kept in
``original_severity``).

Pure functions; inputs are plain dicts so this module has no dependency on
any particular analyzer's internal types — it works equally well fed by
``analyze_pcb.py`` findings, board_facts.py findings, or a hand-built
fixture in a test.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

REAL = "real"
LIKELY_FP = "likely_fp"
REVIEW = "review"


@dataclass
class TriageContext:
    """Facts the rules consult. All optional — a missing fact means 'can't judge'.

    ``drc_summary`` is the ``{category: count}`` rollup kicad-happy's KiCad-CLI
    DRC wrapper produces (e.g. ``drc_keepout``, ``drc_edge``, ``drc_courtyard``,
    ``drc_unconnected``, and the boolean ``drc_ran`` key that marks whether
    kicad-cli's DRC step actually produced a report). A missing or false
    ``drc_ran`` means DRC did not run at all (no PCB, kicad-cli missing, DRC
    timed out/failed, ...) — every count key is 0 in that case too, so
    ``drc_ran`` is the only reliable way to tell "DRC ran clean" apart from
    "DRC never ran". ``graphic_copper_nets`` and ``stackup_multi_ply``
    normally come straight from ``board_facts.collect_board_facts()``.
    """

    drc_summary: dict[str, int] = field(default_factory=dict)
    graphic_copper_nets: Iterable[str] = field(default_factory=set)
    stackup_multi_ply: bool = False
    mousebite_refs: Iterable[str] = field(default_factory=set)

    @classmethod
    def from_dict(cls, data: dict | None) -> "TriageContext":
        """Build a context from a generic JSON-shaped dict (CLI entry point).

        Unknown keys are ignored; missing keys fall back to the "can't judge"
        defaults above, so a partial context never crashes a rule.
        """
        data = data or {}
        return cls(
            drc_summary=dict(data.get("drc_summary") or {}),
            graphic_copper_nets=set(data.get("graphic_copper_nets") or []),
            stackup_multi_ply=bool(data.get("stackup_multi_ply", False)),
            mousebite_refs=set(data.get("mousebite_refs") or []),
        )


@dataclass
class TriageRecord:
    verdict: str
    reason: str


# ── helpers ───────────────────────────────────────────────────────────────────

def _components(f: dict) -> list[str]:
    v = f.get("components") or f.get("refs") or []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _nets(f: dict) -> list[str]:
    v = f.get("nets") or []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _drc_clean(ctx: TriageContext, key: str) -> bool:
    """True when KiCad DRC actually ran (``drc_summary["drc_ran"]`` is true)
    and reported zero violations of the given category.

    A missing/false ``drc_ran`` means DRC did not run, was skipped (no PCB),
    or failed/timed out — treated as "can't judge", never as "clean", no
    matter what the per-category count happens to read (absent DRC reports
    0 for every category, which must never be mistaken for a clean run).
    """
    return bool(ctx.drc_summary.get("drc_ran")) and ctx.drc_summary.get(key, 1) == 0


# ── rules ─────────────────────────────────────────────────────────────────────

def _rule_ko001(f: dict, ctx: TriageContext) -> TriageRecord | None:
    """KO-001 'object inside a keepout rule area' uses a bounding-box test and
    ignores which object types a rule area actually forbids.

    Never downgrade on the bbox heuristic alone: a real KiCad DRC run (real
    rule-area polygons + each area's allowed-object flags) is the only
    evidence strong enough to call this a false positive. No DRC data (or a
    non-zero keepout count) keeps the finding at full severity under review.
    """
    if _drc_clean(ctx, "drc_keepout"):
        return TriageRecord(LIKELY_FP, "KiCad DRC (real rule-area polygons + allowed-object flags) reports no keepout violations")
    return TriageRecord(REVIEW, "analyzer checks keepout bounding boxes, not polygons or the area's allowed objects — confirm with KiCad DRC")


def _rule_rt001(f: dict, ctx: TriageContext) -> TriageRecord | None:
    """RT-001 'net is unrouted/short': graphic copper (gr_*) and a real,
    actually-run KiCad DRC both see connectivity the analyzer's track/via
    walk does not. An absent/failed DRC must never look like "0 unconnected"
    — that requires DRC to have actually run (see ``_drc_clean``)."""
    gc = set(ctx.graphic_copper_nets)
    if any(n in gc for n in _nets(f)):
        return TriageRecord(LIKELY_FP, "net is routed with graphic copper (gr_*) that board_facts found but the analyzer ignores")
    if _drc_clean(ctx, "drc_unconnected"):
        return TriageRecord(LIKELY_FP, "KiCad DRC reports 0 unconnected items")
    return None


def _rule_su001(f: dict, ctx: TriageContext) -> TriageRecord | None:
    """SU-001 stackup/layer-role finding: analyze_pcb.py's extract_setup() only
    keeps one ply's attributes per dielectric, so KiCad 10's multi-ply
    (addsublayer) boards feed it a corrupted dielectric-height read.

    That corruption is about dielectric thickness/epsilon_r/loss_tangent —
    it says nothing about whether a given finding is actually wrong. A
    genuine copper-layer-role claim (e.g. "layer X is wired as a plane but
    configured as a signal layer") is not disproven by a mis-parsed ply
    count, so multi-ply stackup alone must never downgrade a finding to
    ``likely_fp``: it only means the analyzer's *own* stackup read is
    suspect, which is exactly the "needs a human/AI to confirm" case —
    every SU-001 finding stays ``review``, pointing at board_facts.py's
    corrected (summed) stackup as the evidence to confirm or clear it with.
    """
    if ctx.stackup_multi_ply:
        return TriageRecord(
            REVIEW,
            "analyzer mis-parses multi-ply dielectrics (addsublayer) — confirm this finding "
            "against board_facts.py's corrected (summed) stackup before treating it as real",
        )
    return TriageRecord(REVIEW, "verify layer roles (plane vs signal) against the real stackup")


def _rule_pm_edge(f: dict, ctx: TriageContext) -> TriageRecord | None:
    """PM-001/PM-002 mechanical placement note: mousebite/fiducial/reference
    footprints are expected near or over the panel edge by design, and
    PM-002's edge-clearance guideline is a generic fixed distance — a clean
    DRC edge-clearance pass is good evidence but does not make the finding
    definitively wrong (fab/mechanical requirements can exceed DRC), so it
    stays ``review`` rather than ``likely_fp``.
    """
    comps = _components(f)
    mb = set(ctx.mousebite_refs)
    if comps and all(c in mb or c.startswith(("H", "MBRD", "REF", "FID")) for c in comps):
        return TriageRecord(LIKELY_FP, "mechanical/mousebite/fiducial footprint near or over the edge by design")
    if f.get("rule_id") == "PM-001" and _drc_clean(ctx, "drc_courtyard"):
        return TriageRecord(LIKELY_FP, "courtyard overlap not reported by KiCad DRC (allowed by a design rule)")
    if f.get("rule_id") == "PM-002" and _drc_clean(ctx, "drc_edge"):
        return TriageRecord(REVIEW, "KiCad DRC edge clearance passes; analyzer uses a generic guideline distance — confirm with fab/mechanics")
    return None


_RULES: dict[str, Callable[[dict, TriageContext], TriageRecord | None]] = {
    "KO-001": _rule_ko001,
    "RT-001": _rule_rt001,
    "SU-001": _rule_su001,
    "PM-001": _rule_pm_edge,
    "PM-002": _rule_pm_edge,
}


# ── public API ────────────────────────────────────────────────────────────────

def triage_findings(findings: list[dict], ctx: TriageContext) -> list[dict]:
    """Return copies of ``findings`` with ``triage`` / ``triage_reason`` set.

    Never discards or reorders: the result has exactly one entry per input
    finding, in the same order. ``likely_fp`` findings keep their original
    severity in ``original_severity`` and drop to ``info`` so they never
    reach a must-fix list. Findings without a matching rule, or whose rule
    returns no verdict, are ``real`` by default (the analyzer is trusted
    unless disproven) and are returned unmodified apart from the two new
    triage keys.
    """
    out: list[dict] = []
    for f in findings:
        item = dict(f) if isinstance(f, dict) else f
        if not isinstance(item, dict):
            out.append(item)
            continue
        rule = _RULES.get(str(item.get("rule_id", "")))
        record = rule(item, ctx) if rule else None
        if record is None:
            record = TriageRecord(REAL, "")
        item["triage"] = record.verdict
        if record.reason:
            item["triage_reason"] = record.reason
        if record.verdict == LIKELY_FP and str(item.get("severity", "")).lower() != "info":
            item["original_severity"] = item.get("severity")
            item["severity"] = "info"
        out.append(item)
    return out


def triage_table(findings: list[dict]) -> list[dict]:
    """Per-rule rollup for the report: rule, count, verdict counts, sample reason."""
    rows: dict[str, dict] = {}
    for f in findings:
        if not isinstance(f, dict):
            continue
        rid = str(f.get("rule_id", "?"))
        row = rows.setdefault(rid, {"rule_id": rid, "count": 0, REAL: 0, LIKELY_FP: 0, REVIEW: 0, "reason": ""})
        row["count"] += 1
        verdict = f.get("triage", REAL)
        row[verdict] = row.get(verdict, 0) + 1
        if f.get("triage_reason") and not row["reason"]:
            row["reason"] = f["triage_reason"]
    return sorted(rows.values(), key=lambda r: (-r[LIKELY_FP], -r["count"]))


def must_fix(findings: list[dict]) -> list[dict]:
    """Error-severity findings that survived triage (real or still under review)."""
    return [f for f in findings
            if isinstance(f, dict)
            and str(f.get("severity", "")).lower() in ("error", "critical", "high")
            and f.get("triage", REAL) != LIKELY_FP]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_json(path: str):
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"error: file not found: {path!r}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: cannot parse {path!r}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Annotate analyzer findings with a real/likely_fp/review triage verdict."
    )
    parser.add_argument(
        "--findings", required=True,
        help="Analyzer JSON (with a top-level 'findings' list) or a bare findings[] JSON array",
    )
    parser.add_argument(
        "--context",
        help="JSON object matching TriageContext.from_dict() "
             "(drc_summary, graphic_copper_nets, stackup_multi_ply, mousebite_refs)",
    )
    parser.add_argument("--output", "-o", help="Output JSON file (default: stdout)")
    args = parser.parse_args()

    raw = _load_json(args.findings)
    findings = raw.get("findings", []) if isinstance(raw, dict) else raw
    if not isinstance(findings, list):
        raise SystemExit(f"error: no findings[] list found in {args.findings!r}")

    ctx = TriageContext.from_dict(_load_json(args.context)) if args.context else TriageContext()
    annotated = triage_findings(findings, ctx)
    payload = {
        "findings": annotated,
        "triage_table": triage_table(annotated),
        "must_fix": must_fix(annotated),
    }
    output = json.dumps(payload, indent=2, default=str)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
