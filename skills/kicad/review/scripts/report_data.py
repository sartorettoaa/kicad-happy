"""Data loading + normalization for the static Deep Review report renderer.

Stdlib-only. This module never raises on a missing or malformed optional
artifact -- every gap is recorded in `ctx["gaps"]` and surfaced in the
rendered report instead of crashing the renderer. The only canonical,
load-bearing input is `analysis/deep_review.json` (per `review/README.md`
spec 3.C/3.D); everything else (`synthesis.json`, `review_plan.json`,
`review_annotations.json`, the merge report, `design_context.json`, the
per-analyzer envelopes, `analyzer_triage.py`'s `triage.json`, and
`board_facts.py`'s `board_facts.json`, coverage roster, execution metrics, and run-local KiCad/Vibe
summaries) are optional and become explicit gaps when absent.

Generating this report is NOT a verification step. All verification
claims come from upstream artifacts (`deep_review_gate.py`'s
`evidence_checked` stamp, `merge_annotations.py`'s annotation status, a
human reviewer, or `analyzer_triage.py`'s evidence-driven triage verdict)
-- this module only reads and normalizes what those tools already
produced.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mini_jsonschema import iter_errors  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
REVIEW_DIR = SCRIPTS_DIR.parent
DEEP_REVIEW_SCHEMA_PATH = REVIEW_DIR / "schemas" / "deep_review.schema.json"
REVIEW_ANNOTATIONS_SCHEMA_PATH = REVIEW_DIR / "schemas" / "review_annotations.schema.json"
SYNTHESIS_SCHEMA_PATH = REVIEW_DIR / "schemas" / "synthesis.schema.json"
EXECUTION_METRICS_SCHEMA_PATH = REVIEW_DIR / "schemas" / "execution_metrics.schema.json"
VIBE_MANIFEST_VERSION = 1
VIBE_MANIFEST_STATUSES = {"complete", "partial", "failed"}
VIBE_ASSET_STATUSES = {"downloaded", "cached"}

ANALYZER_FILES = ["schematic", "pcb", "emc", "thermal", "gerber", "cross_analysis"]

# Normalized triage vocabulary surfaced in the report. These labels are the
# renderer's own display layer over existing, independent data sources --
# they are not a new persisted schema field:
#   - deep_review findings: `evidence_checked` ("full"/"partial"), or absence
#     of that field (gate never ran) -> "not_checked"; entries in the
#     `quarantined[]` array -> "quarantined".
#   - review_annotations.json overlays (schema `status` enum
#     confirmed/suppressed/escalated) -> verified_ok/refuted/escalated;
#     any finding with no annotation at all -> "not_checked".
ANNOTATION_STATE_LABELS = {
    "confirmed": "verified_ok",
    "suppressed": "refuted",
    "escalated": "escalated",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    """Return (data, error). Never raises."""
    path = Path(path)
    if not path.is_file():
        return None, f"not found: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, f"cannot read/parse {path}: {e}"


def resolve_run_dir(analysis_dir: Path, run_id: str | None = None):
    """Mirror deep_review_gate.load_anchor_sets' run resolution.

    Returns (run_dir: Path, run_id_used: str | None).
    """
    analysis_dir = Path(analysis_dir)
    manifest_path = analysis_dir / "manifest.json"
    if manifest_path.is_file():
        manifest, err = load_json(manifest_path)
        if manifest is not None:
            rid = run_id or manifest.get("current")
            if rid and (analysis_dir / rid).is_dir():
                return analysis_dir / rid, rid
    return analysis_dir, run_id


def load_analyzer_envelopes(run_dir: Path):
    """Return (envelopes: dict[str, dict], errors: dict[str, str])."""
    envelopes, errors = {}, {}
    run_dir = Path(run_dir)
    for stem in ANALYZER_FILES:
        path = run_dir / f"{stem}.json"
        if not path.is_file():
            continue
        data, err = load_json(path)
        if err:
            errors[stem] = err
        else:
            envelopes[stem] = data
    return envelopes, errors


def validate_deep_review(doc: dict) -> list[str]:
    """Schema-validate deep_review.json. Returns a list of error strings
    (empty if valid). Safe to call on malformed/partial documents."""
    schema, err = load_json(DEEP_REVIEW_SCHEMA_PATH)
    if err or not isinstance(schema, dict):
        return [f"could not load deep_review schema: {err}"]
    if not isinstance(doc, dict):
        return ["deep_review.json top level is not an object"]
    return [f"/{'/'.join(str(p) for p in e.path)}: {e.message}" if e.path else e.message
            for e in iter_errors(doc, schema)]


def validate_review_annotations(doc: dict) -> list[str]:
    schema, err = load_json(REVIEW_ANNOTATIONS_SCHEMA_PATH)
    if err or not isinstance(schema, dict):
        return [f"could not load review_annotations schema: {err}"]
    if not isinstance(doc, dict):
        return ["review_annotations.json top level is not an object"]
    return [f"/{'/'.join(str(p) for p in e.path)}: {e.message}" if e.path else e.message
            for e in iter_errors(doc, schema)]


def validate_synthesis(doc: dict) -> list[str]:
    """Schema-validate synthesis.json against this fork's schema."""
    schema, err = load_json(SYNTHESIS_SCHEMA_PATH)
    if err or not isinstance(schema, dict):
        return [f"could not load synthesis schema: {err}"]
    if not isinstance(doc, dict):
        return ["synthesis.json top level is not an object"]
    return [f"/{'/'.join(str(p) for p in e.path)}: {e.message}" if e.path else e.message
            for e in iter_errors(doc, schema)]


def validate_execution_metrics(doc: dict) -> list[str]:
    """Validate the optional token/duration-only usage artifact."""
    schema, err = load_json(EXECUTION_METRICS_SCHEMA_PATH)
    if err or not isinstance(schema, dict):
        return [f"could not load execution metrics schema: {err}"]
    if not isinstance(doc, dict):
        return ["execution_metrics.json top level is not an object"]
    errors = [f"/{'/'.join(str(p) for p in e.path)}: {e.message}" if e.path else e.message
              for e in iter_errors(doc, schema)]
    for group in (doc.get("agents", []), [doc.get("totals", {})]):
        if not isinstance(group, list):
            continue
        for record in group:
            if not isinstance(record, dict):
                continue
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                value = record.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    errors.append(f"{key} must be a non-negative integer or null")
            value = record.get("duration_seconds")
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                errors.append("duration_seconds must be a finite non-negative number or null")
    return errors


def deep_review_finding_state(finding: dict) -> str:
    """Normalize a kept deep_review finding's verification state."""
    checked = (finding or {}).get("evidence_checked")
    if checked == "full":
        return "verified_full"
    if checked == "partial":
        return "verified_partial"
    return "not_checked"


def annotation_state_label(status: str | None) -> str:
    return ANNOTATION_STATE_LABELS.get(status, "not_checked")


def _normalize_item_list(value, text_key: str) -> list[dict]:
    """Lenient coercion for a synthesis.json array-of-objects field
    (top_risks / verified_ok / not_checked). The schema requires each item
    to be an object with at least `text_key` (`reason` or `note`) and an
    optional `finding_id`; a bare string is tolerated and wrapped so a
    slightly-malformed artifact still renders instead of vanishing."""
    out = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({text_key: item})
    return out


_SYNTHESIS_KNOWN_KEYS = {
    "schema_version", "produced_for_run_id", "produced_at",
    "verdict", "top_risks", "verified_ok", "not_checked", "contradictions",
}


def normalize_synthesis(doc: dict) -> dict:
    """Normalize synthesis.json into the shape the renderers consume,
    following this fork's `schemas/synthesis.schema.json` exactly:
    `verdict` is an object (`status` enum ready/needs_review/blocked +
    free-text `summary`), and `top_risks`/`verified_ok`/`not_checked` are
    arrays of objects (optional `finding_id` + a required text field) --
    not arrays of bare strings. Unrecognized top-level keys are preserved
    under `extra` so nothing is silently dropped."""
    doc = doc if isinstance(doc, dict) else {}
    verdict = doc.get("verdict")
    verdict_status, verdict_summary = None, None
    if isinstance(verdict, dict):
        if isinstance(verdict.get("status"), str):
            verdict_status = verdict["status"]
        if isinstance(verdict.get("summary"), str):
            verdict_summary = verdict["summary"]
    contradictions = [c for c in (doc.get("contradictions") or []) if isinstance(c, dict)]
    return {
        "verdict_status": verdict_status,
        "verdict_summary": verdict_summary,
        "top_risks": _normalize_item_list(doc.get("top_risks"), "reason"),
        "verified_ok": _normalize_item_list(doc.get("verified_ok"), "note"),
        "not_checked": _normalize_item_list(doc.get("not_checked"), "reason"),
        "contradictions": contradictions,
        "extra": {k: v for k, v in doc.items() if k not in _SYNTHESIS_KNOWN_KEYS},
    }


_METRIC_ALIASES = {
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens"),
    "cache_write_tokens": ("cache_write_tokens", "cache_creation_tokens",
                           "cache_creation_input_tokens"),
    "duration_seconds": ("duration_seconds", "duration_s", "elapsed_s"),
}


def _numeric_metric(obj: dict, names: tuple[str, ...]):
    """Return an explicit finite non-negative numeric value, or None."""
    for name in names:
        value = obj.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and value >= 0:
            return value
    return None


def _normalize_metrics_record(record: dict) -> dict:
    """Normalize our optional artifact and existing Claude usage aliases."""
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else record
    normalized = {}
    for key, aliases in _METRIC_ALIASES.items():
        normalized[key] = _numeric_metric(usage, aliases)
    for key in ("domain", "status", "summary"):
        value = record.get(key)
        normalized[key] = value if isinstance(value, str) else None
    return normalized


def _safe_mpn_stem(mpn: str) -> str:
    """Match fetch_datasheet_vibe's filename sanitizer without importing it."""
    cleaned = re.sub(r"[^A-Za-z0-9_.\\-]", "_", mpn.strip()).strip(". ")
    return cleaned or "_"


def _validate_vibe_manifest(data: object) -> tuple[dict | None, str | None]:
    """Validate the read-only Vibe inventory shape; fail closed on any error."""
    if not isinstance(data, dict) or set(data) != {"version", "mpns"}:
        return None, "Invalid Vibe manifest structure"
    if data.get("version") != VIBE_MANIFEST_VERSION or not isinstance(data.get("mpns"), dict):
        return None, "Unsupported Vibe manifest version or MPN inventory"
    for normalized_mpn, entry in data["mpns"].items():
        required = {"requested_mpn", "returned_mpn", "checked_at", "status", "stale", "assets"}
        if not isinstance(normalized_mpn, str) or not normalized_mpn or not isinstance(entry, dict) or set(entry) != required:
            return None, "Invalid Vibe manifest MPN entry"
        requested = entry.get("requested_mpn")
        returned = entry.get("returned_mpn")
        if (not isinstance(requested, str) or requested.strip().upper() != normalized_mpn
                or (returned is not None and (not isinstance(returned, str)
                                              or returned.strip().upper() != normalized_mpn))
                or not isinstance(entry.get("checked_at"), str)
                or entry.get("status") not in VIBE_MANIFEST_STATUSES
                or not isinstance(entry.get("stale"), bool)
                or not isinstance(entry.get("assets"), dict)):
            return None, "Invalid Vibe manifest MPN entry"
        for asset_key, record in entry["assets"].items():
            if not isinstance(record, dict) or set(record) != {
                    "type", "id", "filepath", "sha256", "size", "status", "metadata"}:
                return None, "Invalid Vibe manifest asset record"
            asset_type = record.get("type")
            asset_id = record.get("id")
            expected_id = asset_id.lower() if asset_type == "datasheet" and isinstance(asset_id, str) else asset_id
            sha256 = record.get("sha256")
            size = record.get("size")
            metadata = record.get("metadata")
            allowed_metadata = {"datasheet": {"mpn", "mfr", "strategy", "validated", "ts"},
                                "doc": {"name", "doctype", "filename", "size", "ts"}}
            if (asset_type not in allowed_metadata or not isinstance(asset_id, str)
                    or asset_key != f"{asset_type}:{expected_id}"
                    or not isinstance(record.get("filepath"), str)
                    or not isinstance(record.get("status"), str)
                    or (sha256 is not None and (not isinstance(sha256, str)
                                                or not re.fullmatch(r"[0-9a-f]{64}", sha256)))
                    or (size is not None and (isinstance(size, bool) or not isinstance(size, int)
                                              or size < 0))
                    or not isinstance(metadata, dict)
                    or any(key not in allowed_metadata[asset_type] for key in metadata)):
                return None, "Invalid Vibe manifest asset record"
            for value in metadata.values():
                if isinstance(value, bool) or isinstance(value, int):
                    continue
                if (not isinstance(value, str) or len(value) > 512
                        or "://" in value or value.lower().startswith("www.")):
                    return None, "Invalid Vibe manifest asset metadata"
    return data, None


def _load_vibe_manifest(datasheets_dir: Path) -> tuple[dict | None, str | None, str]:
    """Read the separate Vibe manifest without following its recorded paths."""
    path = datasheets_dir / "vibe" / "manifest.json"
    doc, error = load_json(path)
    if error:
        return None, error, str(path)
    manifest, validation_error = _validate_vibe_manifest(doc)
    return manifest, validation_error, str(path)


def _vibe_asset_availability(entry: dict | None, datasheets_dir: Path) -> dict:
    """Verify recorded local PDF bytes; never access URL/source metadata."""
    result = {"available": False, "asset_count": 0}
    if not isinstance(entry, dict) or not isinstance(entry.get("assets"), dict):
        return result
    for record in entry["assets"].values():
        if not isinstance(record, dict):
            continue
        filepath = record.get("filepath")
        if not isinstance(filepath, str) or "\\" in filepath:
            continue
        relative = PurePosixPath(filepath)
        windows_relative = PureWindowsPath(filepath)
        if (relative.is_absolute() or windows_relative.is_absolute() or windows_relative.drive
                or len(relative.parts) != 1 or relative.parts[0] in ("", ".", "..")
                or relative.suffix.lower() != ".pdf"):
            continue
        candidate = datasheets_dir / relative.parts[0]
        try:
            root = datasheets_dir.resolve()
            resolved = candidate.resolve(strict=True)
            if resolved == root or root not in resolved.parents or not resolved.is_file():
                continue
            body = resolved.read_bytes()
        except OSError:
            continue
        expected_size = record.get("size")
        expected_hash = record.get("sha256")
        if (not body.startswith(b"%PDF") or isinstance(expected_size, bool)
                or not isinstance(expected_size, int) or expected_size != len(body)
                or not isinstance(expected_hash, str)
                or hashlib.sha256(body).hexdigest() != expected_hash
                or record.get("status") not in VIBE_ASSET_STATUSES):
            continue
        result["available"] = True
        result["asset_count"] += 1
    return result


def _load_optional_context(ctx: dict, *, analysis_dir: Path, run_dir: Path,
                           run_id: str | None, project_dir: Path,
                           datasheets_dir: Path, coverage_path: str | None,
                           execution_metrics_path: str | None, kicad_cli_path: str | None,
                           datasheet_fetch_path: str | None) -> None:
    """Load optional report summaries; no metric is inferred from absence."""
    coverage_file = Path(coverage_path) if coverage_path else analysis_dir / "review_coverage.json"
    vibe_manifest, vibe_error, vibe_manifest_path = _load_vibe_manifest(datasheets_dir)
    if vibe_error:
        ctx["gaps"].append({"artifact": "Vibe manifest (datasheets/vibe/manifest.json)",
                            "path": vibe_manifest_path,
                            "reason": f"Vibe availability not loaded: {vibe_error}"})
    vibe_mpns = vibe_manifest.get("mpns", {}) if isinstance(vibe_manifest, dict) else {}
    roster, roster_error = load_json(coverage_file)
    if not isinstance(roster, dict):
        reason = f"review_coverage.py not run: {roster_error}" if roster_error else "coverage roster must be a JSON object"
        ctx["gaps"].append({"artifact": "review_coverage.json", "path": str(coverage_file), "reason": reason})
        ctx["coverage"] = {"present": False, "path": str(coverage_file), "summary": {},
                            "candidates": [], "missing": [], "excluded": [], "parts": [],
                            "vibe_manifest_present": vibe_manifest is not None,
                            "vibe_manifest_path": vibe_manifest_path}
    else:
        candidates = roster.get("candidates") if isinstance(roster.get("candidates"), list) else []
        missing = roster.get("missing") if isinstance(roster.get("missing"), list) else []
        excluded = roster.get("excluded") if isinstance(roster.get("excluded"), list) else []
        parts = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("mpn"), str):
                continue
            mpn = candidate["mpn"].strip()
            if not mpn:
                continue
            stem = _safe_mpn_stem(mpn)
            pdf_path = datasheets_dir / f"{stem}.pdf"
            extraction_path = datasheets_dir / "extracted" / f"{stem}.json"
            vibe_entry = vibe_mpns.get(mpn.upper())
            vibe_availability = _vibe_asset_availability(vibe_entry, datasheets_dir)
            vibe_current = (isinstance(vibe_entry, dict)
                            and vibe_entry.get("status") == "complete"
                            and vibe_entry.get("stale") is False)
            parts.append({"ref": candidate.get("ref"), "mpn": mpn,
                          "class": candidate.get("class"),
                          "pdf_available": pdf_path.is_file(),
                          "extraction_available": extraction_path.is_file(),
                          "vibe_asset_available": bool(vibe_current and vibe_availability["available"]),
                          "vibe_asset_count": vibe_availability["asset_count"],
                          "vibe_inventory_current": bool(vibe_current),
                          "vibe_inventory_stale": bool(isinstance(vibe_entry, dict)
                                                        and vibe_entry.get("stale") is True)})
        ctx["coverage"] = {"present": True, "path": str(coverage_file),
                            "summary": roster.get("summary") if isinstance(roster.get("summary"), dict) else {},
                            "candidates": [x for x in candidates if isinstance(x, dict)],
                            "missing": [x for x in missing if isinstance(x, dict)],
                            "excluded": [x for x in excluded if isinstance(x, dict)],
                            "parts": parts,
                            "vibe_manifest_present": vibe_manifest is not None,
                            "vibe_manifest_path": vibe_manifest_path,
                            "unreadable_sources": roster.get("unreadable_sources") or []}

    metrics_file = Path(execution_metrics_path) if execution_metrics_path else analysis_dir / "execution_metrics.json"
    metrics_doc, metrics_error = load_json(metrics_file)
    if not isinstance(metrics_doc, dict):
        reason = f"execution metrics not recorded: {metrics_error}" if metrics_error else "execution_metrics.json must be a JSON object"
        ctx["gaps"].append({"artifact": "execution_metrics.json", "path": str(metrics_file), "reason": reason})
        ctx["execution_metrics"] = {"present": False, "path": str(metrics_file), "agents": [], "totals": {}, "schema_errors": []}
    else:
        errors = validate_execution_metrics(metrics_doc)
        metrics_run = metrics_doc.get("produced_for_run_id")
        expected_run = (ctx.get("deep_review") or {}).get("meta", {}).get("produced_for_run_id") or run_id
        if expected_run and metrics_run and metrics_run != expected_run:
            errors.append(f"metrics run id {metrics_run!r} does not match review run {expected_run!r}")
        agents = [_normalize_metrics_record(x) for x in metrics_doc.get("agents", []) if isinstance(x, dict)]
        totals = _normalize_metrics_record(metrics_doc.get("totals", {}))
        ctx["execution_metrics"] = {"present": True, "path": str(metrics_file),
                                    "produced_for_run_id": metrics_run,
                                    "model": metrics_doc.get("model") if isinstance(metrics_doc.get("model"), str) else None,
                                    "agents": agents, "totals": totals, "schema_errors": errors}
        if errors:
            ctx["gaps"].append({"artifact": "execution_metrics.json", "path": str(metrics_file),
                                "reason": f"{len(errors)} metrics schema/run issue(s) — values shown best effort"})

    cli_file = Path(kicad_cli_path) if kicad_cli_path else run_dir / "kicad_cli" / "result.json"
    cli_doc, cli_error = load_json(cli_file)
    if not isinstance(cli_doc, dict):
        reason = f"kicad_cli_review.py not run: {cli_error}" if cli_error else "kicad-cli result must be a JSON object"
        ctx["gaps"].append({"artifact": "kicad_cli_review", "path": str(cli_file), "reason": reason})
        ctx["kicad_cli"] = {"present": False, "path": str(cli_file), "erc_ran": False,
                             "drc_ran": False, "summary": {}, "findings": [], "notes": []}
    else:
        cli = cli_doc.get("kicad_cli") if isinstance(cli_doc.get("kicad_cli"), dict) else {}
        summary = cli_doc.get("summary") if isinstance(cli_doc.get("summary"), dict) else {}
        ran = bool(cli.get("command_prefix"))
        erc_ran = ran and bool(cli_doc.get("erc_path"))
        drc_ran = ran and bool(cli_doc.get("drc_path")) and summary.get("drc_ran") is True
        ctx["kicad_cli"] = {"present": True, "path": str(cli_file), "erc_ran": erc_ran,
                             "drc_ran": drc_ran, "summary": summary,
                             "findings": cli_doc.get("findings") if isinstance(cli_doc.get("findings"), list) else [],
                             "notes": cli_doc.get("notes") if isinstance(cli_doc.get("notes"), list) else []}
        if not ran:
            ctx["gaps"].append({"artifact": "kicad_cli_review", "path": str(cli_file),
                                "reason": "kicad-cli command was unavailable; ERC/DRC status is not a pass"})

    fetch_file = Path(datasheet_fetch_path) if datasheet_fetch_path else run_dir / "datasheet_fetch.json"
    fetch_doc, fetch_error = load_json(fetch_file)
    if not isinstance(fetch_doc, dict):
        reason = f"Vibe fetch report not recorded: {fetch_error}" if fetch_error else "Vibe fetch report must be a JSON object"
        ctx["gaps"].append({"artifact": "datasheet_fetch.json", "path": str(fetch_file), "reason": reason})
        ctx["datasheet_fetch"] = {"present": False, "path": str(fetch_file), "results": []}
    else:
        fetch_run = fetch_doc.get("produced_for_run_id")
        expected_run = (ctx.get("deep_review") or {}).get("meta", {}).get("produced_for_run_id") or run_id
        results = fetch_doc.get("results") if isinstance(fetch_doc.get("results"), list) else []
        if expected_run and fetch_run and fetch_run != expected_run:
            ctx["gaps"].append({"artifact": "datasheet_fetch.json", "path": str(fetch_file),
                                "reason": f"Vibe fetch results belong to run {fetch_run!r}, expected {expected_run!r}"})
            results = []
        ctx["datasheet_fetch"] = {"present": True, "path": str(fetch_file),
                                  "produced_for_run_id": fetch_run,
                                  "results": [item for item in results if isinstance(item, dict)]}
        fetch_by_mpn = {str(item.get("mpn", "")).strip().upper(): item.get("status")
                        for item in ctx["datasheet_fetch"]["results"]}
        for part in ctx.get("coverage", {}).get("parts", []):
            part["fetch_status"] = fetch_by_mpn.get(str(part.get("mpn", "")).strip().upper(), "not_recorded")
def build_context(analysis_dir: str, run: str | None = None,
                  project_dir: str = ".", datasheets_dir: str = "datasheets",
                  synthesis_path: str | None = None,
                  review_plan_path: str | None = None,
                  review_annotations_path: str | None = None,
                  merge_report_path: str | None = None,
                  design_context_path: str | None = None,
                  coverage_path: str | None = None,
                  execution_metrics_path: str | None = None,
                  kicad_cli_path: str | None = None,
                  datasheet_fetch_path: str | None = None) -> dict:
    """Assemble the single context dict consumed by both renderers."""
    analysis_dir = Path(analysis_dir)
    project_dir = Path(project_dir)
    datasheets_dir = Path(datasheets_dir)
    if not datasheets_dir.is_absolute():
        datasheets_dir = project_dir / datasheets_dir
    gaps = []
    ctx = {
        "generated_at": _now_iso(),
        "analysis_dir": str(analysis_dir),
        "gaps": gaps,
    }

    if not analysis_dir.is_dir():
        gaps.append({"artifact": "analysis_dir", "path": str(analysis_dir),
                      "reason": "directory not found"})

    # --- Canonical: deep_review.json (flat, sibling of manifest.json) ---
    dr_path = analysis_dir / "deep_review.json"
    dr_doc, dr_err = load_json(dr_path)
    deep_review = {"present": dr_doc is not None, "path": str(dr_path),
                    "findings": [], "quarantined": [], "meta": {},
                    "schema_errors": []}
    if dr_err:
        gaps.append({"artifact": "deep_review.json", "path": str(dr_path), "reason": dr_err})
    elif dr_doc is not None:
        schema_errors = validate_deep_review(dr_doc)
        deep_review["schema_errors"] = schema_errors
        if schema_errors:
            gaps.append({"artifact": "deep_review.json", "path": str(dr_path),
                         "reason": f"{len(schema_errors)} schema violation(s) — see Deep Review section"})
        deep_review["meta"] = {
            "schema_version": dr_doc.get("schema_version"),
            "produced_for_run_id": dr_doc.get("produced_for_run_id"),
            "produced_at": dr_doc.get("produced_at"),
        }
        for f in dr_doc.get("findings") or []:
            if isinstance(f, dict):
                f = dict(f)
                f["_state"] = deep_review_finding_state(f)
                deep_review["findings"].append(f)
        for q in dr_doc.get("quarantined") or []:
            if isinstance(q, dict):
                q = dict(q)
                q["_state"] = "quarantined"
                deep_review["quarantined"].append(q)
    else:
        gaps.append({"artifact": "deep_review.json", "path": str(dr_path),
                     "reason": "not found — Deep Review pass not run or not yet written"})
    # Preserve the canonical quarantined state and derive a separate
    # presentation disposition from the gate's provenance reason prefix.
    for item in deep_review["quarantined"]:
        reason = str(item.get("quarantine_reason") or "")
        item["_state"] = "quarantined"
        item["_disposition"] = ("verifier_refuted" if reason.startswith("verifier refuted:")
                                else "gate_quarantined")
    ctx["deep_review"] = deep_review

    # --- Optional: synthesis.json ---
    syn_path = Path(synthesis_path) if synthesis_path else (analysis_dir / "synthesis.json")
    syn_doc, syn_err = load_json(syn_path)
    if syn_doc is not None:
        schema_errors = validate_synthesis(syn_doc)
        ctx["synthesis"] = normalize_synthesis(syn_doc)
        ctx["synthesis"]["present"] = True
        ctx["synthesis"]["path"] = str(syn_path)
        ctx["synthesis"]["schema_errors"] = schema_errors
        if schema_errors:
            gaps.append({"artifact": "synthesis.json", "path": str(syn_path),
                         "reason": f"{len(schema_errors)} schema violation(s) — see Verdict & Synthesis section"})
    else:
        gaps.append({"artifact": "synthesis.json", "path": str(syn_path),
                     "reason": syn_err or "not found"})
        ctx["synthesis"] = {"present": False, "path": str(syn_path),
                            "verdict_status": None, "verdict_summary": None,
                            "top_risks": [], "verified_ok": [], "not_checked": [],
                            "contradictions": [], "extra": {}, "schema_errors": []}

    # --- Optional: review_plan.json ---
    rp_path = Path(review_plan_path) if review_plan_path else (analysis_dir / "review_plan.json")
    rp_doc, rp_err = load_json(rp_path)
    if rp_doc is not None:
        tasks = []
        for t in (rp_doc.get("tasks") or []) if isinstance(rp_doc, dict) else []:
            if not isinstance(t, dict):
                continue
            result_path = t.get("result_path")
            result_exists = False
            if result_path:
                p = Path(result_path)
                if not p.is_absolute():
                    p = project_dir / result_path
                result_exists = p.is_file()
            tasks.append({**t, "_result_exists": result_exists})
        ctx["review_plan"] = {"present": True, "path": str(rp_path),
                              "plan_id": rp_doc.get("plan_id"),
                              "created_at": rp_doc.get("created_at"),
                              "purpose": rp_doc.get("purpose"),
                              "tasks": tasks}
    else:
        gaps.append({"artifact": "review_plan.json", "path": str(rp_path),
                     "reason": rp_err or "not found"})
        ctx["review_plan"] = {"present": False, "path": str(rp_path), "tasks": []}

    # --- Optional: design_context.json ---
    dc_path = Path(design_context_path) if design_context_path else (analysis_dir / "design_context.json")
    dc_doc, dc_err = load_json(dc_path)
    if dc_doc is not None:
        ctx["design_context"] = {"present": True, "path": str(dc_path), "data": dc_doc}
    else:
        gaps.append({"artifact": "design_context.json", "path": str(dc_path),
                     "reason": dc_err or "not found"})
        ctx["design_context"] = {"present": False, "path": str(dc_path), "data": {}}

    # --- Optional: review_annotations.json (Layer 2 detector-finding triage) ---
    ra_path = Path(review_annotations_path) if review_annotations_path else (analysis_dir / "review_annotations.json")
    ra_doc, ra_err = load_json(ra_path)
    if ra_doc is not None:
        schema_errors = validate_review_annotations(ra_doc)
        annotations = []
        for a in (ra_doc.get("annotations") or []) if isinstance(ra_doc, dict) else []:
            if isinstance(a, dict):
                a = dict(a)
                a["_state"] = annotation_state_label(a.get("status"))
                annotations.append(a)
        ctx["review_annotations"] = {
            "present": True, "path": str(ra_path), "schema_errors": schema_errors,
            "annotations": annotations,
            "reviewer_observations": (ra_doc.get("reviewer_observations") or [])
            if isinstance(ra_doc, dict) else [],
        }
        if schema_errors:
            gaps.append({"artifact": "review_annotations.json", "path": str(ra_path),
                         "reason": f"{len(schema_errors)} schema violation(s) — see Triage section"})
    else:
        gaps.append({"artifact": "review_annotations.json", "path": str(ra_path),
                     "reason": ra_err or "not found"})
        ctx["review_annotations"] = {"present": False, "path": str(ra_path),
                                     "annotations": [], "reviewer_observations": []}

    # --- Optional: merge report (analysis/merged/_merge_report.json) ---
    mr_path = Path(merge_report_path) if merge_report_path else (analysis_dir / "merged" / "_merge_report.json")
    mr_doc, mr_err = load_json(mr_path)
    if mr_doc is not None:
        ctx["merge_report"] = {"present": True, "path": str(mr_path), "data": mr_doc}
    else:
        gaps.append({"artifact": "merge report (merged/_merge_report.json)", "path": str(mr_path),
                     "reason": mr_err or "not found"})
        ctx["merge_report"] = {"present": False, "path": str(mr_path), "data": {}}

    # --- Analyzer envelopes (facts the Deep Review pass was checked against) ---
    run_dir, run_id_used = resolve_run_dir(analysis_dir, run)
    envelopes, envelope_errors = load_analyzer_envelopes(run_dir)
    analyzers = []
    for stem in ANALYZER_FILES:
        present = stem in envelopes
        entry = {"name": stem, "present": present, "path": str(run_dir / f"{stem}.json")}
        if present:
            env = envelopes[stem]
            n_findings = len(env.get("findings") or []) if isinstance(env, dict) else None
            entry["finding_count"] = n_findings
        elif stem in envelope_errors:
            entry["error"] = envelope_errors[stem]
            gaps.append({"artifact": f"{stem}.json", "path": entry["path"],
                         "reason": envelope_errors[stem]})
        else:
            gaps.append({"artifact": f"{stem}.json", "path": entry["path"],
                         "reason": "not found — analyzer not run, or run against a different run id"})
        analyzers.append(entry)
    ctx["run_dir"] = str(run_dir)
    ctx["run_id"] = run_id_used
    ctx["analyzers"] = analyzers
    _load_optional_context(ctx, analysis_dir=analysis_dir, run_dir=run_dir,
                           run_id=run_id_used, project_dir=project_dir,
                           datasheets_dir=datasheets_dir,
                           coverage_path=coverage_path,
                           execution_metrics_path=execution_metrics_path,
                           kicad_cli_path=kicad_cli_path,
                           datasheet_fetch_path=datasheet_fetch_path)

    # --- Optional: analyzer_triage.py output (Layer 1 real/likely_fp/review
    # verdicts, per skills/kicad/scripts/analyzer_triage.py and
    # skills/kicad/SKILL.md's documented `analysis/<run_id>/triage.json`) ---
    triage_path = run_dir / "triage.json"
    triage_doc, triage_err = load_json(triage_path)
    if triage_doc is not None and isinstance(triage_doc, dict):
        findings = triage_doc.get("findings") or []
        ctx["analyzer_triage"] = {
            "present": True, "path": str(triage_path),
            "finding_count": len(findings) if isinstance(findings, list) else None,
            "triage_table": triage_doc.get("triage_table") or [],
            "must_fix_count": len(triage_doc.get("must_fix") or []),
        }
    else:
        gaps.append({"artifact": "triage.json", "path": str(triage_path),
                     "reason": (f"analyzer_triage.py not run: {triage_err}" if triage_err
                               else "analyzer_triage.py output is invalid")})
        ctx["analyzer_triage"] = {"present": False, "path": str(triage_path),
                                  "finding_count": None, "triage_table": [], "must_fix_count": 0}

    # --- Optional: board_facts.py output (raw .kicad_pcb stackup + graphic
    # copper facts, per skills/kicad/scripts/board_facts.py's documented
    # `analysis/<run_id>/board_facts.json`; PCB-only, narrow supplementary
    # tool -- absence is still an explicit gap, never a silent omission) ---
    bf_path = run_dir / "board_facts.json"
    bf_doc, bf_err = load_json(bf_path)
    if bf_doc is not None and isinstance(bf_doc, dict):
        ctx["board_facts"] = {
            "present": True, "path": str(bf_path),
            "stackup_warnings": bf_doc.get("stackup_warnings") or [],
            "stackup_multi_ply": bool(bf_doc.get("stackup_multi_ply", False)),
            "impedance_control_noted": bf_doc.get("impedance_control_noted"),
            "graphic_copper_net_count": len(bf_doc.get("graphic_copper") or {}),
            "unnetted_copper_count": len(bf_doc.get("unnetted_copper") or []),
            "finding_count": len(bf_doc.get("findings") or []),
        }
    else:
        gaps.append({"artifact": "board_facts.json", "path": str(bf_path),
                     "reason": (f"board_facts.py not run: {bf_err}" if bf_err
                               else "board_facts.py output is invalid (PCB-only tool)")})
        ctx["board_facts"] = {"present": False, "path": str(bf_path),
                              "stackup_warnings": [], "stackup_multi_ply": False,
                              "impedance_control_noted": None,
                              "graphic_copper_net_count": 0, "unnetted_copper_count": 0,
                              "finding_count": 0}

    return ctx
