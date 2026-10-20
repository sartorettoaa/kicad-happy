#!/usr/bin/env python3
"""Adapter + synthesis-artifact validator around the Deep Review schema/gate.

Two independent jobs, one script (both stdlib-only, no LLM/Claude call):

1. ``normalize`` -- maps plugin-style agent/verifier finding dicts (the shape
   ``tractian_ai_review/core/deep_review.py`` produces: ``severity``,
   ``rule_id``, ``summary``, ``refs``, ``nets``, free-text ``evidence`` /
   ``computation`` strings, ``confidence`` in ``confirmed|likely|check``,
   ``domain``, ``verification: {status, reason}``) into the canonical
   ``deep_review.schema.json`` finding shape (``detector=deep_review``,
   ``category``, ``severity`` in ``error|warning|info``, ``confidence`` in
   ``high|medium|low``, ``summary``, ``description``/``recommendation``,
   ``evidence.{components,nets,pins,datasheet,computation}``). The original
   raw finding is preserved verbatim under ``provenance.raw`` on every
   normalized item -- nothing is silently dropped. Findings with no design
   anchor and/or no evidence source land in ``quarantined[]`` with a reason,
   never dropped. A finding the plugin's own verifier explicitly refuted
   (``verification.status == "refuted"``) is quarantined too; a finding that
   merely could *not* be verified (``"not_verified"``, e.g. missing
   datasheet) is NOT treated as refuted -- it still becomes a normal finding,
   just with its confidence capped (never promoted to ``high`` on
   unverified say-so).

   Output is a ready-to-gate ``deep_review.json`` document:
   ``python3 deep_review_gate.py <output> --analysis-dir analysis/``.

2. ``validate-synthesis`` -- schema-validates a synthesis artifact (verdict /
   top_risks / verified_ok / not_checked / contradictions, see
   ``schemas/synthesis.schema.json``), cross-checks every ``finding_id`` it
   cites against the durable (gated, ID-stamped) findings in a
   ``deep_review.json``, and errors if the synthesis's
   ``produced_for_run_id`` is missing or does not match the
   ``deep_review.json``'s ``produced_for_run_id`` (a stale synthesis
   produced for a different/earlier run). Read-only: it never edits or
   deletes anything in the synthesis artifact, it only reports schema
   violations and unresolved references.

Exit codes (both subcommands): 0 clean, 1 at least one problem
(quarantined finding / unresolved reference / schema violation), 2 I/O or
top-level shape error.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = Path(__file__).resolve().parent
KICAD_SCRIPTS_DIR = REPO_ROOT / "skills" / "kicad" / "scripts"
for _p in (SCRIPTS_DIR, KICAD_SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _mini_jsonschema import iter_errors        # noqa: E402
from finding_schema import normalize_severity   # noqa: E402
from run_id import generate_run_id              # noqa: E402

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
DEEP_REVIEW_SCHEMA_PATH = SCHEMAS_DIR / "deep_review.schema.json"
SYNTHESIS_SCHEMA_PATH = SCHEMAS_DIR / "synthesis.schema.json"

SCHEMA_VERSION = "1.0"

_CONFIDENCE_MAP = {
    # Plugin agent/verifier vocabulary (core/deep_review.py _CONTRACT)
    "confirmed": "high",
    "likely": "medium",
    "check": "low",
    # Canonical vocabulary passes through unchanged
    "high": "high",
    "medium": "medium",
    "low": "low",
}

_ANCHOR_RAW_KEYS = {
    "components": ("refs", "components", "references"),
    "nets": ("nets",),
    "pins": ("pins",),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _first_str_list(raw: dict, keys: Tuple[str, ...]) -> List[str]:
    for key in keys:
        found = _as_str_list(raw.get(key))
        if found:
            return found
    return []


def _derive_category(raw: dict) -> str:
    for key in ("category", "domain"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    rule_id = raw.get("rule_id")
    if isinstance(rule_id, str) and rule_id.strip():
        prefix = rule_id.strip().split("-", 1)[0].strip()
        if prefix:
            return prefix.lower()
    return "general"


def _derive_confidence(raw: dict) -> str:
    base = _CONFIDENCE_MAP.get(str(raw.get("confidence", "")).strip().lower(), "medium")
    verification = raw.get("verification")
    if isinstance(verification, dict):
        status = verification.get("status")
        if status == "confirmed":
            return "high"
        if status == "not_verified":
            # A verifier that couldn't reach a verdict (e.g. no datasheet to
            # check against) is neither a confirmation nor a refutation --
            # cap the confidence instead of trusting the agent's own label,
            # but never silently discard the finding for it.
            return base if base == "low" else "medium"
    return base


def _collect_anchors(raw: dict) -> Dict[str, List[str]]:
    anchors = {}
    for canonical_key, raw_keys in _ANCHOR_RAW_KEYS.items():
        found = _first_str_list(raw, raw_keys)
        if found:
            anchors[canonical_key] = found
    return anchors


def _collect_evidence(raw: dict, anchors: Dict[str, List[str]]) -> dict:
    """Build the canonical ``evidence`` object from whichever shape `raw` uses.

    Supports, in priority order:
    1. An already-structured ``evidence`` dict (datasheet/computation
       sub-keys) -- passed through, future-proofing a plugin upgrade path.
    2. Explicit structured shortcut fields (``mpn``+``quote``[+``page``], or
       a ``datasheet`` list of such objects).
    3. A structured ``computation`` dict (``description``/``script``/
       ``result``).
    4. The current plugin shape: free-text ``evidence`` and/or
       ``computation`` strings. Never guesses an MPN for these -- they are
       folded verbatim into a single ``evidence.computation.result`` so nyone
       anyone downstream can read exactly what the agent/verifier wrote,
       without fabricating a datasheet citation that was never actually
       structured.
    """
    evidence: dict = {}
    evidence.update(anchors)

    raw_evidence = raw.get("evidence")
    structured_datasheet: Optional[list] = None
    structured_computation: Optional[dict] = None
    evidence_text: Optional[str] = None
    if isinstance(raw_evidence, dict):
        if isinstance(raw_evidence.get("datasheet"), list) and raw_evidence["datasheet"]:
            structured_datasheet = raw_evidence["datasheet"]
        if isinstance(raw_evidence.get("computation"), dict) and raw_evidence["computation"]:
            structured_computation = raw_evidence["computation"]
    elif isinstance(raw_evidence, str) and raw_evidence.strip():
        evidence_text = raw_evidence.strip()

    if raw.get("mpn") and raw.get("quote"):
        entry = {"mpn": str(raw["mpn"]), "quote": str(raw["quote"])}
        if isinstance(raw.get("page"), int):
            entry["page"] = raw["page"]
        structured_datasheet = (structured_datasheet or []) + [entry]
    if isinstance(raw.get("datasheet"), list):
        extra = [d for d in raw["datasheet"] if isinstance(d, dict) and d.get("mpn") and d.get("quote")]
        if extra:
            structured_datasheet = (structured_datasheet or []) + extra

    if structured_datasheet:
        evidence["datasheet"] = structured_datasheet

    if structured_computation is None and isinstance(raw.get("computation"), dict) and raw["computation"]:
        structured_computation = raw["computation"]
    computation_text = raw.get("computation") if isinstance(raw.get("computation"), str) else None

    if structured_computation:
        evidence["computation"] = structured_computation
    elif evidence_text or (computation_text and computation_text.strip()):
        parts = []
        if evidence_text:
            parts.append(f"evidence: {evidence_text}")
        if computation_text and computation_text.strip():
            parts.append(f"computation: {computation_text.strip()}")
        evidence["computation"] = {
            "description": "Agent-reported evidence/computation, adapted verbatim "
                           "from plugin finding fields (no datasheet mpn/page was "
                           "structured, so no evidence.datasheet entry was fabricated).",
            "result": "\n".join(parts),
        }
    return evidence


def adapt_finding(raw: dict, *, force_quarantine_reason: Optional[str] = None) -> Tuple[dict, Optional[str]]:
    """Normalize one plugin-style finding. Returns (finding, quarantine_reason).

    ``quarantine_reason`` is ``None`` when the finding is clean enough to go
    into ``findings[]``; otherwise it explains why the finding was routed to
    ``quarantined[]`` instead (and the finding is never dropped either way).
    """
    anchors = _collect_anchors(raw)
    evidence = _collect_evidence(raw, anchors)

    finding: dict = {
        "detector": "deep_review",
        "category": _derive_category(raw),
        "severity": normalize_severity(raw.get("severity")),
        "confidence": _derive_confidence(raw),
        "summary": str(raw.get("summary") or "").strip(),
        "evidence": evidence,
        "provenance": {"source": "normalize_deep_review.adapt_finding", "raw": copy.deepcopy(raw)},
    }
    if isinstance(raw.get("description"), str) and raw["description"].strip():
        finding["description"] = raw["description"].strip()
    if isinstance(raw.get("recommendation"), str) and raw["recommendation"].strip():
        finding["recommendation"] = raw["recommendation"].strip()

    reasons: List[str] = []
    if force_quarantine_reason:
        reasons.append(force_quarantine_reason)

    verification = raw.get("verification")
    if isinstance(verification, dict) and verification.get("status") == "refuted" and not force_quarantine_reason:
        reasons.append(f"verifier refuted: {verification.get('reason', '(no reason given)')}")

    if not finding["summary"]:
        reasons.append("missing summary")

    has_anchor = any(evidence.get(k) for k in ("components", "nets", "pins"))
    has_source = bool(evidence.get("datasheet")) or bool(evidence.get("computation"))
    if not has_anchor:
        reasons.append("no design anchor: evidence must cite >=1 component/net/pin")
    if not has_source:
        reasons.append("no evidence source: evidence must carry a datasheet "
                       "quote and/or a computation")

    if reasons:
        quarantined = dict(finding)
        quarantined["quarantine_reason"] = "; ".join(reasons)
        return quarantined, quarantined["quarantine_reason"]
    return finding, None


def build_deep_review_doc(raw_input: Any, *, produced_for_run_id: Optional[str] = None) -> dict:
    """Normalize plugin-style input into a canonical deep_review.json document.

    Accepts either:
    - a bare JSON array of plugin-style finding dicts, or
    - the ``run_deep_review()``-shaped dict with ``findings``/``quarantined``/
      ``refuted`` buckets (``core/deep_review.py``'s output).
    """
    if isinstance(raw_input, dict):
        candidates = list(raw_input.get("findings") or [])
        pre_quarantined = list(raw_input.get("quarantined") or [])
        pre_refuted = list(raw_input.get("refuted") or [])
    elif isinstance(raw_input, list):
        candidates, pre_quarantined, pre_refuted = list(raw_input), [], []
    else:
        raise ValueError("raw_input must be a JSON array or a "
                         "{findings, quarantined, refuted} object")

    findings: List[dict] = []
    quarantined: List[dict] = []

    for raw in candidates:
        if not isinstance(raw, dict):
            quarantined.append({"quarantine_reason": "not a JSON object",
                                "provenance": {"source": "normalize_deep_review", "raw": raw}})
            continue
        finding, reason = adapt_finding(raw)
        (quarantined if reason else findings).append(finding)

    for raw in pre_quarantined:
        if not isinstance(raw, dict):
            quarantined.append({"quarantine_reason": "not a JSON object (pre-quarantined by plugin gate)",
                                "provenance": {"source": "normalize_deep_review", "raw": raw}})
            continue
        upstream_reason = raw.get("quarantine_reason")
        force = f"plugin gate: {upstream_reason}" if upstream_reason else "plugin gate: quarantined (no reason given)"
        finding, _reason = adapt_finding(raw, force_quarantine_reason=force)
        quarantined.append(finding)

    for raw in pre_refuted:
        if not isinstance(raw, dict):
            quarantined.append({"quarantine_reason": "not a JSON object (verifier-refuted)",
                                "provenance": {"source": "normalize_deep_review", "raw": raw}})
            continue
        verification = raw.get("verification") if isinstance(raw.get("verification"), dict) else {}
        force = f"verifier refuted: {verification.get('reason', '(no reason given)')}"
        finding, _reason = adapt_finding(raw, force_quarantine_reason=force)
        quarantined.append(finding)

    return {
        "schema_version": SCHEMA_VERSION,
        "produced_for_run_id": produced_for_run_id or generate_run_id(),
        "produced_at": _now_iso(),
        "findings": findings,
        "quarantined": quarantined,
    }


def validate_against_schema(doc: Any, schema_path: Path) -> List[str]:
    schema = json.loads(schema_path.read_text())
    return [f"at /{'/'.join(str(p) for p in e.path)}: {e.message}" if e.path else e.message
            for e in iter_errors(doc, schema)]


def validate_synthesis(synthesis_doc: dict, deep_review_doc: dict) -> List[str]:
    """Schema-validate + cross-check finding_id references. Read-only.

    Also guards against a stale synthesis: ``synthesis_doc.produced_for_run_id``
    must be present and must equal ``deep_review_doc.produced_for_run_id`` --
    missing on either side, or a mismatch (synthesis produced for a
    different/earlier run than the deep_review.json it's being checked
    against), is an error.

    Never mutates `synthesis_doc` -- callers may assert this by comparing a
    deep copy taken before the call.
    """
    errors = validate_against_schema(synthesis_doc, SYNTHESIS_SCHEMA_PATH)

    synthesis_run_id = synthesis_doc.get("produced_for_run_id")
    deep_review_run_id = deep_review_doc.get("produced_for_run_id")
    if not synthesis_run_id or not deep_review_run_id:
        errors.append("produced_for_run_id: missing on synthesis and/or deep_review document "
                      f"(synthesis={synthesis_run_id!r}, deep_review={deep_review_run_id!r})")
    elif synthesis_run_id != deep_review_run_id:
        errors.append(f"produced_for_run_id: stale synthesis -- synthesis was produced for run "
                      f"{synthesis_run_id!r} but deep_review.json is for run {deep_review_run_id!r}")

    durable_ids = {
        f.get("finding_id")
        for f in (deep_review_doc.get("findings") or [])
        if isinstance(f, dict) and f.get("finding_id")
    }

    def _check_ref(section: str, index: int, field: str, fid: Any) -> None:
        if not isinstance(fid, str) or fid not in durable_ids:
            errors.append(f"{section}[{index}].{field}: unresolved finding_id reference {fid!r} "
                          "(not present in deep_review.json findings[])")

    for i, item in enumerate(synthesis_doc.get("top_risks") or []):
        if isinstance(item, dict) and "finding_id" in item:
            _check_ref("top_risks", i, "finding_id", item.get("finding_id"))
    for i, item in enumerate(synthesis_doc.get("verified_ok") or []):
        if isinstance(item, dict) and "finding_id" in item:
            _check_ref("verified_ok", i, "finding_id", item.get("finding_id"))
    for i, item in enumerate(synthesis_doc.get("not_checked") or []):
        if isinstance(item, dict) and "finding_id" in item:
            _check_ref("not_checked", i, "finding_id", item.get("finding_id"))
    for i, item in enumerate(synthesis_doc.get("contradictions") or []):
        if isinstance(item, dict):
            for fid in item.get("finding_ids") or []:
                _check_ref("contradictions", i, "finding_ids", fid)

    return errors


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_normalize(args: argparse.Namespace) -> int:
    try:
        raw_input = json.loads(Path(args.input_json).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"normalize_deep_review: cannot load input: {exc}", file=sys.stderr)
        return 2
    try:
        doc = build_deep_review_doc(raw_input, produced_for_run_id=args.run_id)
    except ValueError as exc:
        print(f"normalize_deep_review: {exc}", file=sys.stderr)
        return 2

    schema_errors = validate_against_schema(doc, DEEP_REVIEW_SCHEMA_PATH)
    out_path = Path(args.output) if args.output else Path(args.input_json)
    out_path.write_text(json.dumps(doc, indent=2) + "\n")

    print(f"normalize_deep_review: {len(doc['findings'])} normalized "
          f"({len(doc['quarantined'])} quarantined) -> {out_path}")
    if schema_errors:
        print("normalize_deep_review: WARNING -- output failed its own schema "
              "(gate will quarantine these items):", file=sys.stderr)
        for err in schema_errors:
            print(f"  {err}", file=sys.stderr)
    return 1 if doc["quarantined"] else 0


def _cmd_validate_synthesis(args: argparse.Namespace) -> int:
    try:
        synthesis_doc = json.loads(Path(args.synthesis_json).read_text())
        deep_review_doc = json.loads(Path(args.deep_review_json).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"normalize_deep_review: cannot load inputs: {exc}", file=sys.stderr)
        return 2
    if not isinstance(synthesis_doc, dict) or not isinstance(deep_review_doc, dict):
        print("normalize_deep_review: both inputs must be JSON objects", file=sys.stderr)
        return 2

    errors = validate_synthesis(synthesis_doc, deep_review_doc)
    if errors:
        print(f"validate-synthesis: {len(errors)} problem(s):")
        for err in errors:
            print(f"  {err}")
        return 1
    n_risks = len(synthesis_doc.get("top_risks") or [])
    n_ok = len(synthesis_doc.get("verified_ok") or [])
    n_gaps = len(synthesis_doc.get("not_checked") or [])
    n_contra = len(synthesis_doc.get("contradictions") or [])
    print(f"validate-synthesis: OK -- {n_risks} top_risks, {n_ok} verified_ok, "
          f"{n_gaps} not_checked, {n_contra} contradictions, all finding_id "
          "references resolved")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    p_norm = sub.add_parser("normalize", help="Adapt plugin-style findings into canonical deep_review.json")
    p_norm.add_argument("input_json", help="Plugin-style findings JSON (array, or run_deep_review()-shaped object)")
    p_norm.add_argument("--output", default=None, help="Output path (default: overwrite input_json in place)")
    p_norm.add_argument("--run-id", default=None, help="produced_for_run_id (default: freshly generated)")
    p_norm.set_defaults(func=_cmd_normalize)

    p_syn = sub.add_parser("validate-synthesis", help="Validate a synthesis artifact against its deep_review.json")
    p_syn.add_argument("synthesis_json", help="Synthesis artifact to validate (read-only)")
    p_syn.add_argument("deep_review_json", help="The gated deep_review.json it was produced from")
    p_syn.set_defaults(func=_cmd_validate_synthesis)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
