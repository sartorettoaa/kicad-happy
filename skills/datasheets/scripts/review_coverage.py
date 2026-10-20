#!/usr/bin/env python3
"""Build a deterministic datasheet coverage roster from analyzer artifacts.

Standalone sibling of ``datasheet_lookup.py`` / ``plan_extraction.py`` — not
wired into the Vibe pipeline or any other skill script. It reads one or more
analyzer JSON files (``analyze_schematic.py`` / ``analyze_pcb.py`` output —
see ``skills/kicad/SKILL.md`` "Output JSON Schema Quick Reference" for the
``components[]`` / ``bom[]`` contract) and produces a stable JSON roster for
the "datasheet coverage roster" step described in
``skills/kicad/SKILL.md`` ("Datasheet Acquisition" section):

    candidates  — MPN-bearing components in a datasheet-warranted class
    missing     — datasheet-warranted components with no MPN field at all
                  (coverage gaps, not silently dropped)
    excluded    — DNP / mechanical / testpoint / fiducial / placeholder-MPN /
                  generic-value components that never need a datasheet
                  (this also covers a datasheet-warranted part whose MPN
                  field is only a sentinel like "TBD"/"NA" — not a gap,
                  but not a verifiable part either)

Pure stdlib, read-only (no network, no writes besides --output). Every
component read from the input artifacts lands in exactly one of the three
buckets, so bucket sizes always sum to the total component count.

CLI:
    python3 review_coverage.py schematic.json [pcb.json ...] [--output roster.json]

Without --output, the roster JSON is printed to stdout. With --output, it is
written to that explicit path (parent directories are not created implicitly
beyond what already exists — callers choose the location).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# MPN extraction — robust to property-key spelling variance
# ---------------------------------------------------------------------------

# Flat component dict keys that may carry the manufacturer part number.
# Normalized (lowercased, spaces/dashes -> underscore) before matching, so
# "Manufacturer Part Number", "manufacturer-part-number" and
# "MANUFACTURER_PART_NUMBER" all resolve to the same bucket.
_MPN_KEY_CANDIDATES = (
    "mpn",
    "manufacturer_part_number",
    "manufacturerpartnumber",
    "part_number",
    "partnumber",
)

# Nested dicts some analyzer/property shapes stash arbitrary KiCad symbol
# fields under (defensive — the documented schematic/pcb contract is flat,
# but reading these too costs nothing and catches shapes like a raw KiCad
# "properties" map keyed by display name).
_NESTED_PROPERTY_KEYS = ("properties", "fields", "custom_fields")

# Sentinel / placeholder MPN tokens that carry no real part number. Matched
# only against the *entire* trimmed, lowercased field value — never a
# substring match — so a real MPN that happens to contain one of these as a
# fragment (unlikely, but be conservative) is never dropped.
_PLACEHOLDER_MPN_TOKENS = {
    "", "na", "n/a", "none", "tbd", "-", "--", "?", "dnp", "dnm", "unknown",
}


def _normalize_key(key: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(key).strip().lower())


def _iter_candidate_fields(comp: Dict[str, object]):
    """Yield (key, raw_value) pairs from the component and any nested property maps."""
    for key, value in comp.items():
        yield key, value
    for nested_key in _NESTED_PROPERTY_KEYS:
        nested = comp.get(nested_key)
        if isinstance(nested, dict):
            for key, value in nested.items():
                yield key, value


def _raw_mpn_field(comp: Dict[str, object]) -> Optional[str]:
    """Return the first non-empty raw MPN-ish field value, or None if absent.

    Unlike ``_candidate_mpn`` below, this does NOT filter placeholder tokens —
    it is used to distinguish "field absent/empty" (-> missing coverage) from
    "field present but a sentinel value" (-> excluded/placeholder_mpn).
    """
    for key, value in _iter_candidate_fields(comp):
        if _normalize_key(key) in _MPN_KEY_CANDIDATES:
            text = str(value).strip() if value is not None else ""
            if text:
                return text
    return None


def _candidate_mpn(comp: Dict[str, object]) -> str:
    """Return a usable manufacturer part number, or "" when none is usable.

    ``manufacturer``/``manufacturer_name``-style fields hold the vendor name,
    never the part number, and are deliberately excluded from
    ``_MPN_KEY_CANDIDATES``.
    """
    raw = _raw_mpn_field(comp)
    if raw is None:
        return ""
    # BOM-style fields sometimes carry multiple alternates separated by ';'.
    first = raw.split(";")[0].strip()
    if first.lower() in _PLACEHOLDER_MPN_TOKENS:
        return ""
    return first


# ---------------------------------------------------------------------------
# Reference-designator / value classification
# ---------------------------------------------------------------------------

_REF_PREFIX_RE = re.compile(r"^[A-Z]+")

# Datasheet-warranted reference-designator prefixes, broader than the legacy
# U/IC/A-only set. Keys are matched against the FULL leading-letter run of
# the reference (e.g. "TVS1" -> "TVS", not "T"), so more specific prefixes
# (TVS, FB, ANT, BAT, SC) never collide with their shorter relatives (T, F,
# A, B, S) — dict lookup is an exact match on the extracted prefix string.
_CLASS_BY_PREFIX: Dict[str, str] = {
    # IC / module — legacy set, kept as-is.
    "U": "ic",
    "IC": "ic",
    "A": "ic",
    # Transistor / switch.
    "Q": "transistor_switch",
    # Transformer / magnetics.
    "T": "transformer_magnetics",
    # Crystal / oscillator.
    "Y": "crystal_oscillator",
    "X": "crystal_oscillator",
    # Connector.
    "J": "connector",
    "P": "connector",
    # Protection diode (incl. TVS / zener-style refs some libraries use).
    "D": "protection_diode",
    "TVS": "protection_diode",
    "Z": "protection_diode",
    # Power / RF inductor, ferrite bead.
    "L": "power_inductor_ferrite",
    "FB": "power_inductor_ferrite",
    # Antenna / RF module. "E" is intentionally narrow — see
    # _is_plausible_antenna_e below; it is never classified on prefix alone.
    "AE": "antenna_module",
    "ANT": "antenna_module",
    # Supercap / battery.
    "SC": "supercap_battery",
    "BAT": "supercap_battery",
    # Switch / relay / misc electromechanical — only matters when a real
    # MPN is present (enforced by the candidate gate, not here). "SW" is
    # the standard KiCad/ANSI reference prefix for a switch (e.g. "SW1" for
    # a tactile pushbutton) and "K" is the standard ANSI prefix for a relay
    # — both are distinct full letter-runs from the legacy "S"/"W" entries,
    # so without their own keys a part like "SW1" falls through entirely
    # (its extracted prefix is "SW", not "S").
    "S": "switch_other",
    "SW": "switch_other",
    "W": "switch_other",
    "K": "switch_other",
}

# Reference prefixes that are *never* datasheet-relevant regardless of MPN
# field contents (board mechanics / probes), independent of the generic
# value/footprint token sniffing in _exclusion_reason.
_MECHANICAL_REF_PREFIXES = ("MH", "MTG")
_FIDUCIAL_REF_PREFIXES = ("FID",)
_TESTPOINT_REF_PREFIXES = ("TP",)

_MECHANICAL_TOKENS = (
    "mounting", "mountinghole", "mounting_hole", "logo",
    "solder_jumper", "solderjumper", "mechanical",
)
_FIDUCIAL_TOKENS = ("fiducial",)
_TESTPOINT_TOKENS = ("testpoint", "test_point")


def _ref_prefix(ref: str) -> str:
    m = _REF_PREFIX_RE.match(ref.upper())
    return m.group(0) if m else ""


def _text_blob(comp: Dict[str, object]) -> str:
    """Lowercased join of the fields exclusion sniffing looks at."""
    parts = [
        str(comp.get(k) or "")
        for k in ("value", "footprint", "type", "component_type", "category")
    ]
    return " ".join(parts).lower()


def _is_dnp(comp: Dict[str, object]) -> bool:
    val = comp.get("dnp")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("yes", "true", "1", "dnp")
    return bool(val)


def _is_plausible_antenna_e(ref_prefix: str, blob: str) -> bool:
    """Guard against the single-letter 'E' prefix matching unrelated parts.

    'E' is not a standard KiCad reference-designator prefix for anything in
    particular, so it is only ever treated as an antenna/RF element when the
    value/category text itself mentions antenna/RF — never on ref prefix
    alone. This keeps the broadened prefix set from flagging arbitrary
    one-off library ref schemes as antennas.
    """
    if ref_prefix != "E":
        return False
    return any(t in blob for t in ("antenna", "aerial", " rf ", "rf_", "_rf"))


def _datasheet_class(ref_prefix: str, blob: str) -> str:
    """Return the datasheet-warranted class for a reference prefix, or ""."""
    if ref_prefix == "E":
        return "antenna_module" if _is_plausible_antenna_e(ref_prefix, blob) else ""
    return _CLASS_BY_PREFIX.get(ref_prefix, "")


# ---------------------------------------------------------------------------
# Per-component classification
# ---------------------------------------------------------------------------


def _mechanical_exclusion_reason(ref_prefix: str, blob: str) -> Optional[str]:
    if ref_prefix in _TESTPOINT_REF_PREFIXES or any(t in blob for t in _TESTPOINT_TOKENS):
        return "testpoint"
    if ref_prefix in _FIDUCIAL_REF_PREFIXES or any(t in blob for t in _FIDUCIAL_TOKENS):
        return "fiducial"
    if ref_prefix in _MECHANICAL_REF_PREFIXES or any(t in blob for t in _MECHANICAL_TOKENS):
        return "mechanical"
    return None


def _classify_component(comp: Dict[str, object], source: str) -> Tuple[str, Dict[str, object]]:
    """Classify one component dict.

    Returns (bucket, entry) where bucket is one of
    "candidate" | "missing" | "excluded".
    """
    ref = str(comp.get("reference", "")).strip()
    value = str(comp.get("value", "") or "")
    footprint = str(comp.get("footprint", "") or "")
    ctype = str(comp.get("type") or comp.get("component_type") or "")
    ref_prefix = _ref_prefix(ref)
    blob = _text_blob(comp)

    base_entry: Dict[str, object] = {
        "ref": ref,
        "value": value,
        "footprint": footprint,
        "type": ctype,
        "source": source,
    }

    # DNP wins over every other classification — an unpopulated part never
    # needs datasheet verification for this build.
    if _is_dnp(comp):
        entry = dict(base_entry, reason="dnp")
        return "excluded", entry

    mech_reason = _mechanical_exclusion_reason(ref_prefix, blob)
    if mech_reason is not None:
        entry = dict(base_entry, reason=mech_reason)
        return "excluded", entry

    ds_class = _datasheet_class(ref_prefix, blob)
    mpn = _candidate_mpn(comp)
    raw_mpn_field = _raw_mpn_field(comp)

    if ds_class:
        if mpn:
            entry = dict(base_entry, mpn=mpn, **{"class": ds_class},
                         reason=f"ref prefix '{ref_prefix}' -> {ds_class}, real MPN present")
            return "candidate", entry
        if raw_mpn_field is not None:
            # MPN field present but a sentinel/placeholder token (TBD/NA/...):
            # an explicit exclusion, not a silent gap and not a candidate.
            entry = dict(base_entry, reason=f"placeholder_mpn ('{raw_mpn_field}')")
            return "excluded", entry
        # Relevant class, no MPN field at all: a genuine coverage gap, not a
        # silent drop.
        reason = f"ref prefix '{ref_prefix}' -> {ds_class}, no MPN field present"
        entry = dict(base_entry, **{"class": ds_class}, reason=reason)
        return "missing", entry

    # Not a datasheet-warranted class. A placeholder/sentinel MPN value on a
    # non-relevant part is its own exclusion reason; an absent MPN on a
    # generic passive is the catch-all "generic_value" exclusion.
    if raw_mpn_field is not None and not mpn:
        entry = dict(base_entry, reason="placeholder_mpn")
        return "excluded", entry
    if mpn:
        # A non-relevant-class part that nonetheless carries a real MPN
        # (e.g. a precision-spec resistor) — conservative: still excluded
        # from the datasheet roster by class, but the real MPN is preserved
        # in the reason instead of silently discarded.
        entry = dict(base_entry, reason=f"ref prefix '{ref_prefix}' not datasheet-relevant (MPN '{mpn}' noted)")
        return "excluded", entry
    entry = dict(base_entry, reason="generic_value")
    return "excluded", entry


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------


def _load_artifact(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _artifact_components(data: dict) -> List[Dict[str, object]]:
    """Return the component list from an analyzer artifact.

    ``components[]`` (schematic analyzer) is the primary, documented
    contract. When absent, fall back to ``bom[]`` (also documented, present
    on some artifact shapes) so a BOM-only input is still usable.
    """
    comps = data.get("components")
    if isinstance(comps, list) and comps:
        return [c for c in comps if isinstance(c, dict)]
    bom = data.get("bom")
    if isinstance(bom, list):
        return [c for c in bom if isinstance(c, dict)]
    return []


# ---------------------------------------------------------------------------
# Roster building
# ---------------------------------------------------------------------------


def build_roster(artifact_paths: List[Path]) -> Dict[str, object]:
    """Build the deterministic coverage roster for the given artifact paths.

    Every component in every readable artifact lands in exactly one of
    candidates / missing / excluded, deduplicated per (ref, mpn) across all
    input artifacts (first occurrence wins, preserving the input order
    passed on the CLI).
    """
    candidates: List[Dict[str, object]] = []
    missing: List[Dict[str, object]] = []
    excluded: List[Dict[str, object]] = []
    seen_candidates: set = set()
    seen_missing: set = set()
    seen_excluded: set = set()
    unreadable: List[str] = []

    for path in artifact_paths:
        source = str(path)
        data = _load_artifact(path)
        if data is None:
            unreadable.append(source)
            continue
        for comp in _artifact_components(data):
            ref = str(comp.get("reference", "")).strip()
            if not ref:
                continue
            bucket, entry = _classify_component(comp, source)
            if bucket == "candidate":
                key = (entry["ref"], entry["mpn"])
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                candidates.append(entry)
            elif bucket == "missing":
                key = (entry["ref"], entry["class"])
                if key in seen_missing:
                    continue
                seen_missing.add(key)
                missing.append(entry)
            else:
                key = (entry["ref"], entry["reason"], source)
                if key in seen_excluded:
                    continue
                seen_excluded.add(key)
                excluded.append(entry)

    # Deterministic ordering: by reference, then (for candidates) by MPN.
    candidates.sort(key=lambda e: (e["ref"], e["mpn"]))
    missing.sort(key=lambda e: (e["ref"], e["class"]))
    excluded.sort(key=lambda e: (e["ref"], e["reason"]))

    total = len(candidates) + len(missing) + len(excluded)
    roster: Dict[str, object] = {
        "schema_version": "1.0",
        "sources": [str(p) for p in artifact_paths],
        "summary": {
            "total_components": total,
            "candidates": len(candidates),
            "missing": len(missing),
            "excluded": len(excluded),
        },
        "candidates": candidates,
        "missing": missing,
        "excluded": excluded,
    }
    if unreadable:
        roster["unreadable_sources"] = unreadable
    return roster


def _dumps(roster: Dict[str, object]) -> str:
    """Deterministic JSON text — sorted keys, stable float/str formatting."""
    return json.dumps(roster, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Build a deterministic datasheet coverage roster (candidates / "
            "missing / excluded) from one or more analyzer JSON artifacts."
        )
    )
    ap.add_argument(
        "artifacts", nargs="+", type=Path,
        help="Path(s) to analyzer JSON output (schematic.json, pcb.json, ...).",
    )
    ap.add_argument(
        "--output", type=Path, default=None,
        help="Explicit output path for the roster JSON. Default: stdout.",
    )
    args = ap.parse_args(argv)

    missing_inputs = [str(p) for p in args.artifacts if not p.is_file()]
    if missing_inputs:
        for p in missing_inputs:
            print(f"error: artifact not found: {p}", file=sys.stderr)
        return 2

    roster = build_roster(args.artifacts)
    text = _dumps(roster)

    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
        print(f"coverage roster written to {args.output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
