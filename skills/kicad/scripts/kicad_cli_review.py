#!/usr/bin/env python3
"""kicad-cli ground truth: ERC, DRC (zone refill + schematic parity), sexpr netlist export.

kicad-happy's analyzers re-implement connectivity and rule checks in Python
and can disagree with KiCad itself. KiCad's own ERC/DRC are the ground truth
a review should cross-check against: a DRC with 0 unconnected / 0 parity
issues rules out a whole class of "unrouted" analyzer claims, and real
DRC/ERC violations become findings with ``analysis_source = "kicad"``.

kicad-cli is not on PATH on Windows (it lives under the KiCad install's
``bin``) and is not on PATH at all on Flatpak Linux installs (it has to be
invoked through ``flatpak run --command=kicad-cli org.kicad.KiCad`` — see the
Flatpak note in ``skills/kicad/SKILL.md``) — so it is located explicitly
rather than assumed to be on PATH. Every step is best-effort: a missing
kicad-cli or a failing command degrades to a note, never an exception.

Source files are read-only: every command's working directory is
``output_dir`` (never the project directory), DRC never passes
``--save-board``, and nothing here ever writes into the schematic/PCB project
folder — all artifacts (erc.json, drc.json, netlist.net) land in the explicit
``output_dir`` the caller provides. Because cwd is ``output_dir`` rather than
the caller's cwd, ``schematic``/``pcb``/``output_dir`` are resolved to
absolute paths before being used in any command — otherwise a relative input
path would be looked up relative to ``output_dir`` and never be found. Each
``-o`` target is also removed before its command runs, so a stale artifact
left over from a previous (possibly failed) invocation into the same
``output_dir`` can never be mistaken for ground truth this run produced.

Usage:
    python3 kicad_cli_review.py --pcb board.kicad_pcb --output-dir ./out
    python3 kicad_cli_review.py --schematic proj.kicad_sch --pcb board.kicad_pcb --output-dir ./out
    python3 kicad_cli_review.py --schematic proj.kicad_sch --output-dir ./out --output result.json

Python 3.10+ stdlib only — no pcbnew / wx.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Newest first — a project saved by KiCad 10 cannot be read by an older CLI.
KICAD_VERSIONS = ("10.0", "9.0", "8.0")
CLI_TIMEOUT_S = 900  # DRC with --refill-zones on a dense 6-layer board takes minutes

# Severity mapping into the error|warning|info vocabulary used across
# kicad-happy (see finding_schema.VALID_SEVERITIES). Excluded violations
# (designer-waived in KiCad) are downgraded to info rather than dropped.
_SEVERITY = {"error": "error", "warning": "warning", "info": "info", "ignore": "info"}

# DRC violation types that are about libraries/annotations, not board
# geometry. Kept separate so a caller can trust "0 geometric DRC" even when
# lib/footprint bookkeeping issues are present.
NON_GEOMETRIC_DRC = (
    "lib_footprint_issues", "lib_footprint_mismatch", "footprint_symbol_mismatch",
    "missing_footprint", "extra_footprint", "net_conflict", "duplicate_footprints",
    "footprint_type_mismatch", "silk_over_copper", "silk_overlap", "text_height",
    "text_thickness", "silk_edge_clearance", "lib_footprint_issues",
)


@dataclass
class KicadCliResult:
    """Outcome of the kicad-cli ground-truth stage."""

    command_prefix: list[str] | None = None
    version: str = ""
    erc_path: Path | None = None
    drc_path: Path | None = None
    netlist_path: Path | None = None
    findings: list[dict] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # True only when a PCB was supplied AND kicad-cli actually produced a DRC
    # report (drc_path is not None) — i.e. "0 violations" is real ground truth,
    # not an artifact of DRC never having run. Downstream triage (RT-001,
    # KO-001, ...) must gate "clean DRC" evidence on this flag, never on the
    # mere presence/absence of count keys in ``summary``.
    drc_ran: bool = False
    # Canonical (absolute, resolved) input/output paths run_kicad_cli() actually
    # used for every kicad-cli invocation — set unconditionally (even when
    # kicad-cli can't be found) so a caller building the JSON payload echoes
    # what kicad-cli was actually pointed at, never the original possibly
    # relative path the top-level caller supplied.
    resolved_schematic: Path | None = None
    resolved_pcb: Path | None = None
    resolved_output_dir: Path | None = None


# ---------------------------------------------------------------------------
# kicad-cli discovery
# ---------------------------------------------------------------------------

def find_kicad_cli() -> list[str] | None:
    """Locate kicad-cli: env override, PATH, standard install dirs, then Flatpak.

    Returns the full command prefix to prepend to every invocation — usually
    a single-element list holding the executable path, but on a Flatpak-only
    Linux install it is
    ``["flatpak", "run", "--command=kicad-cli", "org.kicad.KiCad"]``.
    ``None`` means kicad-cli could not be located at all.
    """
    override = os.environ.get("KICAD_CLI_PATH")
    if override and Path(override).is_file():
        return [override]
    found = shutil.which("kicad-cli")
    if found:
        return [found]
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates += [program_files / "KiCad" / v / "bin" / "kicad-cli.exe" for v in KICAD_VERSIONS]
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"))
    else:
        candidates += [Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")]
    for cand in candidates:
        if cand.is_file():
            return [str(cand)]
    if not sys.platform.startswith("win") and sys.platform != "darwin" and shutil.which("flatpak"):
        # Best-effort: Flatpak KiCad ships kicad-cli inside its sandbox, never
        # on PATH. If org.kicad.KiCad isn't actually installed the version
        # probe in run_kicad_cli() below degrades to a note like every other
        # best-effort step here — this is not validated up front.
        return ["flatpak", "run", "--command=kicad-cli", "org.kicad.KiCad"]
    return None


def _run(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# ERC / DRC JSON -> findings
# ---------------------------------------------------------------------------

def _violation_to_finding(v: dict, kind: str, sheet: str = "") -> dict:
    """Convert one ERC/DRC JSON violation into a flat finding dict."""
    items = v.get("items") or []
    descriptions = [str(i.get("description", "")) for i in items if isinstance(i, dict)]
    refs: list[str] = []
    for d in descriptions:
        # "Symbol PS1 Pin 4 [...]" / "Footprint H3" / "Pad 2 [GND] of U5 on F.Cu"
        words = d.replace("[", " ").split()
        for marker in ("Symbol", "Footprint", "of"):
            if marker in words:
                idx = words.index(marker)
                if idx + 1 < len(words):
                    refs.append(words[idx + 1])
    excluded = bool(v.get("excluded"))
    severity = "info" if excluded else _SEVERITY.get(str(v.get("severity", "warning")).lower(), "warning")
    vtype = str(v.get("type", "unknown"))
    where = f" [{sheet}]" if sheet else ""
    pos = items[0].get("pos") if items and isinstance(items[0], dict) else None
    return {
        "rule_id": f"{kind}-{vtype}",
        "severity": severity,
        "summary": f"{kind}: {v.get('description', vtype)}{where}" + (" (excluded in KiCad)" if excluded else ""),
        "evidence": "; ".join(descriptions)[:600],
        "refs": sorted(set(refs)),
        "nets": [],
        "position": pos,
        "excluded": excluded,
        "analysis_source": "kicad",
        "confidence": "deterministic",
    }


def parse_erc(path: Path) -> list[dict]:
    """Findings from a kicad-cli ERC JSON report (``--format json``)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for sheet in data.get("sheets") or []:
        for v in sheet.get("violations") or []:
            if isinstance(v, dict):
                out.append(_violation_to_finding(v, "ERC", str(sheet.get("path", ""))))
    return out


def parse_drc(path: Path) -> list[dict]:
    """Findings from a kicad-cli DRC JSON report (violations, unconnected, parity)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for v in data.get("violations") or []:
        if isinstance(v, dict):
            out.append(_violation_to_finding(v, "DRC"))
    for v in data.get("unconnected_items") or []:
        if isinstance(v, dict):
            f = _violation_to_finding(v, "DRC")
            f["rule_id"] = "DRC-unconnected"
            f["severity"] = "error"
            out.append(f)
    for v in data.get("schematic_parity") or []:
        if isinstance(v, dict):
            f = _violation_to_finding(v, "DRC")
            f["rule_id"] = "DRC-parity-" + str(v.get("type", "unknown"))
            out.append(f)
    return out


def _vtype(f: dict) -> str:
    return f["rule_id"].split("-", 1)[1] if "-" in f["rule_id"] else ""


def drc_types(findings: list[dict]) -> list[str]:
    """Distinct non-excluded DRC violation types (e.g. 'courtyards_overlap')."""
    return sorted({_vtype(f) for f in findings if f["rule_id"].startswith("DRC-") and not f.get("excluded")})


def summarize(findings: list[dict], *, drc_ran: bool = False) -> dict[str, int]:
    """Ground-truth counts (e.g. 0 unconnected) a review can cross-check against.

    ``drc_ran`` must reflect whether kicad-cli's DRC step actually produced a
    report (``KicadCliResult.drc_ran``) — every count below is 0 both when
    DRC genuinely found nothing AND when DRC never ran at all (no PCB, DRC
    timed out, kicad-cli missing, ...), so ``drc_ran`` is the only reliable
    way for a caller (e.g. analyzer_triage's ``TriageContext``) to tell those
    two cases apart. Never infer "DRC ran" from a count being present/absent.
    """
    geometric = [f for f in findings if f["rule_id"].startswith("DRC-")
                 and not f["rule_id"].startswith(("DRC-unconnected", "DRC-parity-"))
                 and not f.get("excluded") and _vtype(f) not in NON_GEOMETRIC_DRC]
    return {
        "drc_ran": bool(drc_ran),
        "drc_geometric": len(geometric),
        "drc_courtyard": sum(1 for f in geometric if "courtyard" in _vtype(f)),
        "drc_keepout": sum(1 for f in geometric if _vtype(f) in ("items_not_allowed", "keepout")),
        "drc_edge": sum(1 for f in geometric if "edge" in _vtype(f)),
        "erc_violations": sum(1 for f in findings if f["rule_id"].startswith("ERC-") and not f.get("excluded")),
        "erc_excluded": sum(1 for f in findings if f["rule_id"].startswith("ERC-") and f.get("excluded")),
        "drc_violations": sum(1 for f in findings if f["rule_id"].startswith("DRC-")
                              and not f["rule_id"].startswith(("DRC-unconnected", "DRC-parity-"))
                              and not f.get("excluded")),
        "drc_unconnected": sum(1 for f in findings if f["rule_id"] == "DRC-unconnected"),
        "drc_parity": sum(1 for f in findings if f["rule_id"].startswith("DRC-parity-")),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_kicad_cli(
    schematic: Path | None,
    pcb: Path | None,
    output_dir: Path,
    *,
    refill_zones: bool = True,
    timeout: int = CLI_TIMEOUT_S,
) -> KicadCliResult:
    """Run ERC + sexpr netlist export (schematic) and DRC (PCB) into ``output_dir``.

    DRC runs with ``--refill-zones`` by default (stale fills give wrong
    clearance/unconnected results) and with ``--schematic-parity`` whenever a
    schematic was also supplied — but NEVER with ``--save-board``: the board
    file on disk is never modified. Every command's cwd is ``output_dir``
    rather than the project directory, so inputs stay read-only.

    ``schematic``, ``pcb`` and ``output_dir`` are canonicalized (resolved to
    absolute paths) up front: since every command below runs with
    ``cwd=output_dir``, a relative ``schematic``/``pcb`` path supplied by the
    caller would otherwise be resolved by kicad-cli against ``output_dir``
    instead of the caller's actual working directory and silently fail to
    find the input file.
    """
    result = KicadCliResult()
    output_dir = Path(output_dir).resolve()
    schematic = Path(schematic).resolve() if schematic else None
    pcb = Path(pcb).resolve() if pcb else None
    # Record the canonical paths up front — unconditionally, even if kicad-cli
    # turns out to be missing below — so build_payload() always has the
    # resolved paths this function actually used, never the caller's
    # possibly-relative originals.
    result.resolved_schematic = schematic
    result.resolved_pcb = pcb
    result.resolved_output_dir = output_dir
    cli = find_kicad_cli()
    if not cli:
        result.notes.append("kicad-cli not found (set KICAD_CLI_PATH) — ERC/DRC ground truth skipped")
        # Explicit drc_ran=False rather than leaving summary {} — consumers
        # must be able to read summary["drc_ran"] even on this early-exit
        # path instead of inferring "didn't run" from a missing key.
        result.summary = summarize(result.findings, drc_ran=False)
        return result
    result.command_prefix = cli
    try:
        ver = _run([*cli, "version"], output_dir, 30)
        result.version = (ver.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    def _step(name: str, command: list[str], target: Path) -> Path | None:
        # Remove any stale artifact (e.g. an erc.json left over from a
        # previous run into the same output_dir) before invoking kicad-cli.
        # Without this, ``target.is_file()`` below could see a leftover file
        # from a prior invocation and report success/ground-truth from data
        # this run never produced — e.g. after a crash or a bad input that
        # makes kicad-cli exit without writing ``-o`` at all.
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            proc = _run(command, output_dir, timeout)
        except subprocess.TimeoutExpired:
            result.notes.append(f"kicad-cli {name} timed out after {timeout}s")
            return None
        except OSError as exc:
            result.notes.append(f"kicad-cli {name} could not start: {exc}")
            return None
        if target.is_file():
            return target
        detail = (proc.stderr or proc.stdout or "no output").strip()[:300]
        result.notes.append(f"kicad-cli {name} failed: {detail}")
        return None

    if schematic:
        erc = output_dir / "erc.json"
        result.erc_path = _step("ERC", [*cli, "sch", "erc", "--format", "json", "--severity-all",
                                        "-o", str(erc), str(schematic)], erc)
        net = output_dir / "netlist.net"
        result.netlist_path = _step("netlist", [*cli, "sch", "export", "netlist", "--format", "kicadsexpr",
                                                "-o", str(net), str(schematic)], net)
        if result.erc_path:
            result.findings += parse_erc(result.erc_path)
    if pcb:
        drc = output_dir / "drc.json"
        command = [*cli, "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm"]
        if schematic:
            command.append("--schematic-parity")
        if refill_zones:
            command.append("--refill-zones")
        result.drc_path = _step("DRC", command + ["-o", str(drc), str(pcb)], drc)
        result.drc_ran = result.drc_path is not None
        if result.drc_path:
            result.findings += parse_drc(result.drc_path)
    result.summary = summarize(result.findings, drc_ran=result.drc_ran)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _path_or_none(s: str | None) -> Path | None:
    return Path(s) if s else None


def build_payload(
    result: KicadCliResult,
    *,
    schematic: Path | None,
    pcb: Path | None,
    output_dir: Path,
) -> dict:
    """Shape the JSON envelope the CLI prints / saves.

    Echoes the canonical (resolved) ``schematic``/``pcb``/``output_dir``
    ``run_kicad_cli()`` actually pointed kicad-cli at — ``result.resolved_*``
    — rather than the caller's originals, which may still be relative (cwd
    for every kicad-cli command is ``output_dir``, so a relative original
    would otherwise look right here while being wrong for every command
    ``run_kicad_cli()`` ran). Falls back to the ``schematic``/``pcb``/
    ``output_dir`` parameters when ``result`` predates resolution (e.g. a
    ``KicadCliResult`` built directly by a test/caller without going through
    ``run_kicad_cli()``).
    """
    schematic = result.resolved_schematic or schematic
    pcb = result.resolved_pcb or pcb
    output_dir = result.resolved_output_dir or output_dir
    return {
        "schema": "kicad_cli_review/1",
        "schematic": str(schematic) if schematic else None,
        "pcb": str(pcb) if pcb else None,
        "output_dir": str(output_dir),
        "kicad_cli": {
            "command_prefix": result.command_prefix,
            "version": result.version,
        },
        "erc_path": str(result.erc_path) if result.erc_path else None,
        "drc_path": str(result.drc_path) if result.drc_path else None,
        "netlist_path": str(result.netlist_path) if result.netlist_path else None,
        "findings": result.findings,
        "summary": result.summary,
        "notes": result.notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="kicad-cli ground truth: ERC, DRC (--refill-zones / --schematic-parity), sexpr netlist export.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 kicad_cli_review.py --pcb board.kicad_pcb --output-dir ./out
  python3 kicad_cli_review.py --schematic proj.kicad_sch --pcb board.kicad_pcb --output-dir ./out
  python3 kicad_cli_review.py --schematic proj.kicad_sch --output-dir ./out --output result.json
""",
    )
    parser.add_argument("--schematic", default=None,
                        help="Path to .kicad_sch — enables ERC + sexpr netlist export")
    parser.add_argument("--pcb", default=None, help="Path to .kicad_pcb — enables DRC")
    parser.add_argument("--output-dir", required=True,
                        help="Directory for erc.json/drc.json/netlist.net (created if missing). "
                             "Source files are never written to.")
    parser.add_argument("--no-refill-zones", action="store_true",
                        help="Skip --refill-zones on DRC (faster; stale fills can misreport "
                             "clearance/unconnected)")
    parser.add_argument("--timeout", type=int, default=CLI_TIMEOUT_S,
                        help=f"Per-command timeout in seconds (default {CLI_TIMEOUT_S})")
    parser.add_argument("--output", "-o", default=None,
                        help="Save the JSON result to this path. Default: print to stdout only.")
    parser.add_argument("--compact", action="store_true", help="Compact JSON output")
    args = parser.parse_args(argv)

    schematic = _path_or_none(args.schematic)
    pcb = _path_or_none(args.pcb)
    if not schematic and not pcb:
        print("error: provide --schematic and/or --pcb", file=sys.stderr)
        return 1
    if schematic and not schematic.is_file():
        print(f"error: schematic not found: {schematic}", file=sys.stderr)
        return 1
    if pcb and not pcb.is_file():
        print(f"error: pcb not found: {pcb}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = run_kicad_cli(
        schematic, pcb, output_dir,
        refill_zones=not args.no_refill_zones,
        timeout=args.timeout,
    )
    payload = build_payload(result, schematic=schematic, pcb=pcb, output_dir=output_dir)
    text = json.dumps(payload, indent=None if args.compact else 2, default=str)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
