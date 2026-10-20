#!/usr/bin/env python3
"""Board facts read straight from the raw ``.kicad_pcb`` file.

Two blind spots of the kicad-happy analyzers, found on a real review:

* **Stackup** — KiCad 10 stores multi-ply dielectrics as one
  ``(layer "dielectric N" ...)`` node with bare ``addsublayer`` markers
  between plies. ``analyze_pcb.py``'s ``extract_setup()`` walks every child
  of that node in order and assigns scalar keys (``thickness``, ``material``,
  ``epsilon_r``, ``loss_tangent``) onto one flat dict — the last ply silently
  overwrites the earlier ones, so dielectric height (and every impedance
  number derived from it) is wrong on these boards. Designers also leave
  sub-layers at epsilon_r ~= 1 / tan-delta = 0 (stackup-wizard placeholders),
  which silently breaks KiCad's own impedance calculator and any fab-notes
  stackup table.
* **Graphic copper** — traces (commonly RF) are sometimes drawn as
  ``gr_line`` / ``gr_arc`` / ``gr_poly`` on a copper layer with a net
  assigned, instead of real tracks. The analyzers' track/via walk ignores
  them, so those nets look short or "unrouted" (RT-001) even though they
  conduct and KiCad DRC sees them as connected.
* **Net names on graphic/pad references** — legacy (KiCad <=9) and track/
  via/zone net references are id-only (``(net 3)``); only pads normally
  carry the name inline (``(net 3 "RF_FEED")``). Code that used the id
  alone as the net key could silently fail to line up a ``gr_line``'s net
  with the same net's pads/tracks. This module resolves every net
  reference it reads (graphic copper and footprint-pad copper) to the
  board's real net name, using the top-level ``(net N "name")`` table as
  the id -> name map; a KiCad 10 name-only ``(net "name")`` reference is
  used as-is (nothing to resolve).

This module is pure functions over the parsed S-expression tree, using
this checkout's own ``sexp_parser`` (no new dependency, no separate pinned
checkout to locate — board_facts.py ships inside the same ``scripts/``
directory it imports from).

CLI:
    python board_facts.py --pcb board.kicad_pcb
    python board_facts.py --pcb board.kicad_pcb --output board_facts.json

Library:
    from board_facts import collect_board_facts, facts_to_dict, facts_markdown
    facts = collect_board_facts(Path("board.kicad_pcb"))
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sexp_parser  # noqa: E402 — local parser shipped with this checkout

# epsilon_r below this is physically impossible for a laminate -> data-entry error
# (vacuum/air is 1.0; even the lowest-k laminates are well above 2).
_MIN_PLAUSIBLE_ER = 2.0
# loss_tangent at/under this is either a vacuum (impossible for a laminate) or an
# unset placeholder left by the stackup wizard — a real laminate always has loss.
_MIN_PLAUSIBLE_TAND = 0.0001

_GRAPHIC_KINDS = ("gr_line", "gr_arc", "gr_poly", "gr_rect", "gr_circle")
_FP_COPPER_KINDS = ("fp_line", "fp_arc", "fp_poly", "fp_rect", "fp_circle")
_NO_NET = (None, "", "0", 0)


@dataclass
class Dielectric:
    """One dielectric ply between two copper layers."""

    name: str
    kind: str  # "core" | "prepreg"
    thickness_mm: float
    material: str
    epsilon_r: float | None
    loss_tangent: float | None


@dataclass
class StackupLayer:
    """Copper layer with the total dielectric height to the next copper below."""

    name: str
    copper_um: float
    dielectric_below_mm: float = 0.0
    plies_below: list[Dielectric] = field(default_factory=list)


@dataclass
class BoardFacts:
    """Raw-file facts the analyzers miss."""

    thickness_mm: float | None = None
    copper_layers: list[StackupLayer] = field(default_factory=list)
    stackup_warnings: list[str] = field(default_factory=list)
    stackup_multi_ply: bool = False
    impedance_control_noted: bool | None = None
    graphic_copper: dict[str, dict[str, float]] = field(default_factory=dict)  # net -> {layer: length_mm}
    unnetted_copper: list[dict] = field(default_factory=list)  # gr_* on Cu with no net
    footprint_copper: dict[str, dict[str, float]] = field(default_factory=dict)  # fp_* on Cu, by touched pad net
    findings: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small parse helpers
# ---------------------------------------------------------------------------

def _f(value) -> float | None:
    """Best-effort float conversion; returns None instead of raising."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _point(node: list, key: str) -> tuple[float, float] | None:
    """(x, y) from a ``(key x y ...)`` child of ``node`` (e.g. start/mid/end/center)."""
    child = sexp_parser.find_first(node, key)
    if not child or len(child) < 3:
        return None
    x, y = _f(child[1]), _f(child[2])
    if x is None or y is None:
        return None
    return (x, y)


def _split_plies(node: list) -> list[list]:
    """Split a dielectric node's children into plies at each ``addsublayer`` atom.

    KiCad 10 writes multi-ply dielectrics as one ``(layer "name" (type ...)
    (thickness ...) ... addsublayer (thickness ...) ...)`` node — the bare
    ``addsublayer`` atom (not a list) separates one ply's attribute group
    from the next.
    """
    plies: list[list] = [[]]
    for child in node[2:]:  # skip "layer" tag and the layer name
        if child == "addsublayer":
            plies.append([])
        else:
            plies[-1].append(child)
    return [p for p in plies if p]


# ---------------------------------------------------------------------------
# Stackup
# ---------------------------------------------------------------------------

def parse_stackup(root: list) -> BoardFacts:
    """Extract copper layers, real (summed) dielectric heights, and data-entry errors."""
    facts = BoardFacts()
    general = sexp_parser.find_first(root, "general")
    if general:
        facts.thickness_mm = _f(sexp_parser.get_value(general, "thickness"))

    setup = sexp_parser.find_first(root, "setup")
    stackup = sexp_parser.find_first(setup, "stackup") if setup else None
    if not stackup:
        facts.stackup_warnings.append("No (stackup) in the board file — impedance cannot be computed")
        return facts

    current: StackupLayer | None = None
    for layer in sexp_parser.find_all(stackup, "layer"):
        if len(layer) < 2:
            continue
        name = str(layer[1])
        ltype = str(sexp_parser.get_value(layer, "type") or "")
        if ltype == "copper":
            thickness_mm = _f(sexp_parser.get_value(layer, "thickness")) or 0.0
            current = StackupLayer(name=name, copper_um=thickness_mm * 1000)
            facts.copper_layers.append(current)
        elif ltype in ("core", "prepreg") and current is not None:
            for ply in _split_plies(layer):
                d = Dielectric(
                    name=name,
                    kind=ltype,
                    thickness_mm=_f(sexp_parser.get_value(ply, "thickness")) or 0.0,
                    material=str(sexp_parser.get_value(ply, "material") or ""),
                    epsilon_r=_f(sexp_parser.get_value(ply, "epsilon_r")),
                    loss_tangent=_f(sexp_parser.get_value(ply, "loss_tangent")),
                )
                current.plies_below.append(d)
                current.dielectric_below_mm += d.thickness_mm
                if d.epsilon_r is not None and d.epsilon_r < _MIN_PLAUSIBLE_ER:
                    facts.stackup_warnings.append(
                        f"{name} ply '{d.material or '?'}' {d.thickness_mm} mm has epsilon_r = {d.epsilon_r} "
                        "— implausible for a laminate (data-entry error); impedance/fab tables will be wrong"
                    )
                if d.loss_tangent is not None and d.loss_tangent <= _MIN_PLAUSIBLE_TAND:
                    facts.stackup_warnings.append(
                        f"{name} ply '{d.material or '?'}' {d.thickness_mm} mm has loss_tangent = {d.loss_tangent} "
                        "— a real laminate is never lossless; likely an unset stackup-wizard placeholder"
                    )

    for cl in facts.copper_layers:
        if len(cl.plies_below) > 1:
            facts.stackup_multi_ply = True
            plies = " + ".join(f"{p.material or p.kind} {p.thickness_mm}" for p in cl.plies_below)
            facts.stackup_warnings.append(
                f"Dielectric under {cl.name} is multi-ply ({plies} = {cl.dielectric_below_mm:.4f} mm); "
                "tools that read only one ply under- or over-estimate the height"
            )
    return facts


def _impedance_note(root: list) -> bool | None:
    """Detect a fab-notes 'Impedance Control: Yes/No' table on the board.

    The label and its Yes/No value are separate ``gr_text`` items on the same
    row (same y). Returns True/False when found, None when the board has no
    such note.
    """
    texts = [t for t in sexp_parser.find_all(root, "gr_text") if len(t) > 1]
    for t in texts:
        if "impedance control" not in str(t[1]).lower():
            continue
        at = sexp_parser.get_at(t)
        if not at:
            continue
        y = at[1]
        for other in texts:
            if other is t:
                continue
            val = str(other[1]).strip().lower()
            oat = sexp_parser.get_at(other)
            if val in ("yes", "no") and oat and oat[1] == y:
                return val == "yes"
    return None


# ---------------------------------------------------------------------------
# Graphic copper (board-level gr_* and footprint-level fp_*)
# ---------------------------------------------------------------------------

def _arc_length(start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]) -> float:
    """Length of the circular arc through three points (chord fallback if degenerate)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return math.hypot(x3 - x1, y3 - y1)
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def _ccw(a, b):
        return (b - a) % (2 * math.pi)

    sweep = _ccw(a1, a3)
    if _ccw(a1, a2) > sweep:  # mid not on the ccw path -> arc goes clockwise
        sweep = 2 * math.pi - sweep
    return r * sweep


def _shape_length(node: list) -> float:
    """Trace length of a line/arc shape (gr_* or fp_*); 0 for polys/rects/circles."""
    kind = str(node[0])
    if kind.endswith("_line"):
        s, e = _point(node, "start"), _point(node, "end")
        if s and e:
            return math.hypot(e[0] - s[0], e[1] - s[1])
    elif kind.endswith("_arc"):
        s, m, e = _point(node, "start"), _point(node, "mid"), _point(node, "end")
        if s and m and e:
            return _arc_length(s, m, e)
    return 0.0


def _shape_points(node: list) -> list[tuple[float, float]]:
    """Characteristic points of a shape (endpoints, mid, polygon vertices, rect corners)."""
    pts = [p for p in (_point(node, k) for k in ("start", "mid", "end", "center")) if p]
    poly = sexp_parser.find_first(node, "pts")
    if poly:
        pts += [xy for xy in (sexp_parser.get_xy(c) for c in sexp_parser.find_all(poly, "xy")) if xy]
    return pts


def _add(out: dict[str, dict[str, float]], net: str, layer: str, length: float) -> None:
    per_layer = out.setdefault(net, {})
    per_layer[layer] = round(per_layer.get(layer, 0.0) + length, 3)


def _net_id_map(root: list) -> dict[str, str]:
    """Net id -> net name, from the board's top-level ``(net N "name")`` table.

    This is the authoritative global net table KiCad writes once per board
    (direct children of the ``kicad_pcb`` root, alongside ``setup``,
    ``footprint``, ``gr_*``, ...), independent of how any individual item
    references its net. Used to resolve id-only net references (legacy
    KiCad <=9 pads, and tracks/vias/zones/graphic-copper on every version)
    to the same name a netlist-aware reader (or a pad's own inline name)
    would show.
    """
    out: dict[str, str] = {}
    for net in sexp_parser.find_all(root, "net"):
        if len(net) >= 3:
            out[str(net[1])] = str(net[2])
    return out


def _resolve_net(node: list, net_map: dict[str, str]) -> str | None:
    """Net name referenced by the ``(net ...)`` child of ``node``, or ``None``.

    Handles every encoding seen across KiCad versions:
    - ``(net N "name")``  — id + inline name (e.g. pads): name wins, no
      lookup needed; falls back to ``net_map`` only if the inline name is
      empty but the id is a real net.
    - ``(net N)``         — id-only (legacy KiCad <=9 and current tracks/
      vias/zones/graphic-copper): resolved through ``net_map``; the bare id
      is returned unresolved if the board has no matching top-level entry
      (degrades gracefully instead of losing the reference).
    - ``(net "name")``    — KiCad 10 name-only encoding: already a name,
      used as-is.
    ``(net 0 ...)``/missing/empty is "no net" and returns ``None``.
    """
    net = sexp_parser.find_first(node, "net")
    if not net or len(net) < 2:
        return None
    if net[1] in _NO_NET:
        return None
    if len(net) >= 3:
        name = str(net[2])
        return name or net_map.get(str(net[1]), str(net[1]))
    value = str(net[1])
    if not value.isdigit():
        return value  # KiCad 10 name-only encoding — already a name
    return net_map.get(value, value)


def graphic_copper(root: list) -> dict[str, dict[str, float]]:
    """Net-assigned board-level graphic copper: ``{net: {layer: length_mm}}``.

    Polygons and rectangles contribute a 0.0 entry so the net is still listed
    as "has graphic copper" (it connects pads even though it has no trace
    length). Net references are resolved to names (see ``_resolve_net``).
    """
    net_map = _net_id_map(root)
    out: dict[str, dict[str, float]] = {}
    for node in root:
        if not (isinstance(node, list) and node and node[0] in _GRAPHIC_KINDS):
            continue
        layer = str(sexp_parser.get_value(node, "layer") or "")
        if not layer.endswith(".Cu"):
            continue
        net = _resolve_net(node, net_map)
        if net is None:
            continue
        _add(out, net, layer, _shape_length(node))
    return out


def unnetted_graphic_copper(root: list) -> list[dict]:
    """Board-level copper shapes WITHOUT a net: they conduct, but KiCad treats
    them as belonging to no net (no connectivity, no clearance-by-net)."""
    found: list[dict] = []
    for node in root:
        if not (isinstance(node, list) and node and node[0] in _GRAPHIC_KINDS):
            continue
        layer = str(sexp_parser.get_value(node, "layer") or "")
        if not layer.endswith(".Cu") or sexp_parser.get_value(node, "net") not in _NO_NET:
            continue
        pts = _shape_points(node)
        found.append({
            "kind": node[0],
            "layer": layer,
            "at": [round(pts[0][0], 3), round(pts[0][1], 3)] if pts else None,
            "length_mm": round(_shape_length(node), 3),
        })
    return found


def footprint_copper(root: list, touch_tol_mm: float = 0.05) -> dict[str, dict[str, float]]:
    """Copper shapes inside footprints (printed antennas, net-ties, custom pads).

    ``fp_*`` shapes carry no net of their own; each is attributed to the net
    of the footprint pad it touches (any shape point within the pad's
    half-size + tolerance). Returns ``{net: {layer: length_mm}}``; shapes
    touching no netted pad are filed under ``"<unconnected:REF>"`` so they
    still surface. Net references are resolved to names (see
    ``_resolve_net``).
    """
    net_map = _net_id_map(root)
    out: dict[str, dict[str, float]] = {}
    for fp in sexp_parser.find_all(root, "footprint"):
        ref = sexp_parser.get_property(fp, "Reference") or "?"
        pads = []
        for pad in sexp_parser.find_all(fp, "pad"):
            at = sexp_parser.get_at(pad)
            size = sexp_parser.find_first(pad, "size")
            net = _resolve_net(pad, net_map)
            if at and size and len(size) > 1 and net is not None:
                hw = (_f(size[1]) or 0.0) / 2
                hh = (_f(size[2]) if len(size) > 2 else _f(size[1]) or 0.0) / 2
                pads.append((at[0], at[1], hw, hh, net))
        for shape in fp:
            if not (isinstance(shape, list) and shape and shape[0] in _FP_COPPER_KINDS):
                continue
            layer = str(sexp_parser.get_value(shape, "layer") or "")
            if not layer.endswith(".Cu"):
                continue
            # Shape points and pad 'at' are both footprint-local, so compare
            # locally with a radius bound (rotation-independent: max(half-w, half-h)).
            pts = _shape_points(shape)
            net = None
            for px, py, hw, hh, pnet in pads:
                reach = max(hw, hh) + touch_tol_mm
                if any(math.hypot(x - px, y - py) <= reach for x, y in pts):
                    net = pnet
                    break
            _add(out, net or f"<unconnected:{ref}>", layer, _shape_length(shape))
    return out


def nets_with_extra_copper(facts: BoardFacts) -> set:
    """Nets whose copper the analyzers under-count (board gr_* + footprint fp_* on Cu)."""
    nets = set(facts.graphic_copper)
    nets.update(n for n in facts.footprint_copper if not n.startswith("<unconnected:"))
    return nets


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

def _facts_findings(facts: BoardFacts) -> list[dict]:
    findings: list[dict] = []

    bad_er = [w for w in facts.stackup_warnings if "epsilon_r" in w]
    if bad_er:
        findings.append({
            "rule_id": "BF-STACKUP-ER",
            "severity": "error" if facts.impedance_control_noted else "warning",
            "summary": f"Stackup has {len(bad_er)} dielectric ply(s) with implausible epsilon_r"
                       + (" while the fab notes say 'Impedance Control: Yes'" if facts.impedance_control_noted else ""),
            "evidence": "; ".join(bad_er)[:900],
            "recommendation": "Set real epsilon_r/tan-delta per ply (e.g. 2116 ~ 4.25, 2313 ~ 4.05, 7628 ~ 4.6, "
                              "FR4 core ~ 4.5) and give the fab an impedance table (target, layer, width/gap, "
                              "reference planes).",
            "refs": [], "nets": [],
            "analysis_source": "board_facts", "confidence": "deterministic",
        })

    bad_tand = [w for w in facts.stackup_warnings if "loss_tangent" in w]
    if bad_tand:
        findings.append({
            "rule_id": "BF-STACKUP-TAND",
            "severity": "warning",
            "summary": f"Stackup has {len(bad_tand)} dielectric ply(s) with an implausible/unset loss tangent",
            "evidence": "; ".join(bad_tand)[:900],
            "recommendation": "Fill in the real tan-delta per ply from the laminate datasheet; a 0 value silently "
                              "under-estimates insertion loss in impedance/SI tools.",
            "refs": [], "nets": [],
            "analysis_source": "board_facts", "confidence": "deterministic",
        })

    multi_ply = [w for w in facts.stackup_warnings if "multi-ply" in w]
    if multi_ply:
        findings.append({
            "rule_id": "BF-STACKUP-MULTIPLY",
            "severity": "info",
            "summary": f"{len(multi_ply)} copper layer(s) sit over a multi-ply dielectric (KiCad 10 addsublayer)",
            "evidence": "; ".join(multi_ply)[:900],
            "recommendation": "Confirm the analyzer/fab stackup table sums every ply — tools that read only one "
                              "ply (first or last) under- or over-estimate total dielectric height.",
            "refs": [], "nets": [],
            "analysis_source": "board_facts", "confidence": "deterministic",
        })

    if facts.graphic_copper:
        nets = sorted(facts.graphic_copper)
        total = sum(sum(v.values()) for v in facts.graphic_copper.values())
        findings.append({
            "rule_id": "BF-GRAPHIC-COPPER",
            "severity": "info",
            "summary": f"{len(nets)} net(s) are partly routed with graphic copper (gr_*), {total:.1f} mm total — "
                       "analyzer lengths/'unrouted' results for these nets are unreliable",
            "evidence": ", ".join(f"{n}: {sum(v.values()):.1f} mm" for n, v in sorted(facts.graphic_copper.items()))[:900],
            "recommendation": "Treat RT-001 'unrouted' and length findings on these nets as false positives; "
                              "prefer real tracks/arcs for RF routing so DRC length tuning and tools see them.",
            "refs": [], "nets": nets,
            "analysis_source": "board_facts", "confidence": "deterministic",
        })

    if facts.unnetted_copper:
        where = "; ".join(f"{u['kind']} on {u['layer']} at {u['at']}" for u in facts.unnetted_copper[:12])
        findings.append({
            "rule_id": "BF-UNNETTED-COPPER",
            "severity": "warning",
            "summary": f"{len(facts.unnetted_copper)} graphic copper shape(s) have no net — they conduct, but "
                       "KiCad does not track their connectivity or apply net-based clearance",
            "evidence": where[:900],
            "recommendation": "Assign the intended net to each shape (Properties -> Net) or replace it with a "
                              "track/zone; if it is a deliberate isolated feature (logo, test coupon), move it to "
                              "a non-copper layer.",
            "refs": [], "nets": [],
            "analysis_source": "board_facts", "confidence": "deterministic",
        })

    orphan = sorted(n for n in facts.footprint_copper if n.startswith("<unconnected:"))
    if orphan:
        findings.append({
            "rule_id": "BF-FP-COPPER-ORPHAN",
            "severity": "info",
            "summary": f"Copper shapes inside {len(orphan)} footprint(s) touch no netted pad",
            "evidence": ", ".join(o[len("<unconnected:"):-1] for o in orphan)[:900],
            "recommendation": "Confirm these are intentional (printed antenna radiator, shield/fence copper) and "
                              "not a pad-less trace that should belong to a net.",
            "refs": [o[len("<unconnected:"):-1] for o in orphan], "nets": [],
            "analysis_source": "board_facts", "confidence": "deterministic",
        })

    return findings


# ---------------------------------------------------------------------------
# Entry point + serialization
# ---------------------------------------------------------------------------

def collect_board_facts(pcb: Path) -> BoardFacts:
    """Parse ``pcb`` once and return stackup + graphic-copper facts (never raises)."""
    try:
        root = sexp_parser.parse_file(str(pcb))
    except Exception as exc:  # malformed/missing file — degrade, don't abort
        facts = BoardFacts()
        facts.stackup_warnings.append(f"board facts unavailable: {exc}")
        return facts
    facts = parse_stackup(root)
    facts.impedance_control_noted = _impedance_note(root)
    facts.graphic_copper = graphic_copper(root)
    facts.unnetted_copper = unnetted_graphic_copper(root)
    facts.footprint_copper = footprint_copper(root)
    facts.findings = _facts_findings(facts)
    return facts


def facts_to_dict(facts: BoardFacts) -> dict:
    """JSON-serializable view (written to ``board_facts.json``)."""
    return {
        "thickness_mm": facts.thickness_mm,
        "copper_layers": [
            {
                "name": cl.name,
                "copper_um": round(cl.copper_um, 1),
                "dielectric_below_mm": round(cl.dielectric_below_mm, 4),
                "plies_below": [p.__dict__ for p in cl.plies_below],
            }
            for cl in facts.copper_layers
        ],
        "stackup_warnings": facts.stackup_warnings,
        "stackup_multi_ply": facts.stackup_multi_ply,
        "impedance_control_noted": facts.impedance_control_noted,
        "graphic_copper": facts.graphic_copper,
        "unnetted_copper": facts.unnetted_copper,
        "footprint_copper": facts.footprint_copper,
        "findings": facts.findings,
    }


def facts_markdown(facts: BoardFacts) -> list[str]:
    """Digest section so every AI pass sees the corrected stackup."""
    if not (facts.copper_layers or facts.graphic_copper or facts.footprint_copper or facts.unnetted_copper):
        return []
    lines = ["Stackup (parsed from the raw board file — authoritative; analyzer stackup may be wrong):"]
    for cl in facts.copper_layers:
        lines.append(f"- {cl.name} ({cl.copper_um:.0f} um)")
        if cl.plies_below:
            plies = ", ".join(
                f"{p.kind} {p.material or '?'} {p.thickness_mm} mm epsilon_r={p.epsilon_r}" for p in cl.plies_below
            )
            lines.append(f"  - dielectric {cl.dielectric_below_mm:.4f} mm: {plies}")
    if facts.impedance_control_noted is not None:
        lines.append(f"- Fab notes: Impedance Control = {'Yes' if facts.impedance_control_noted else 'No'}")
    for w in facts.stackup_warnings:
        lines.append(f"- WARNING: {w}")
    if facts.graphic_copper:
        lines.append("Nets with graphic copper (gr_*) the analyzers do not count:")
        for net, layers in sorted(facts.graphic_copper.items()):
            lines.append(f"- `{net}`: " + ", ".join(f"{ly} {mm:.1f} mm" for ly, mm in layers.items()))
    if facts.footprint_copper:
        lines.append("Copper shapes inside footprints (fp_* on Cu), attributed to the pad net they touch:")
        for net, layers in sorted(facts.footprint_copper.items()):
            lines.append(f"- `{net}`: " + ", ".join(f"{ly} {mm:.1f} mm" for ly, mm in layers.items()))
    if facts.unnetted_copper:
        lines.append(f"Graphic copper with NO net ({len(facts.unnetted_copper)} shape(s)) — conducts but is untracked:")
        for u in facts.unnetted_copper[:12]:
            lines.append(f"- {u['kind']} on {u['layer']} at {u['at']} ({u['length_mm']} mm)")
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raw-file board facts the kicad-happy analyzers miss "
                    "(real multi-ply stackup heights, implausible epsilon_r/tan-delta, "
                    "graphic copper net lengths, unnetted copper shapes)."
    )
    parser.add_argument("--pcb", required=True, help="Path to the .kicad_pcb file")
    parser.add_argument("--output", "-o", help="Output JSON file (default: stdout)")
    args = parser.parse_args()

    pcb_path = Path(args.pcb)
    if not pcb_path.is_file():
        parser.error(f"file not found: {args.pcb!r}")

    facts = collect_board_facts(pcb_path)
    output = json.dumps(facts_to_dict(facts), indent=2, default=str)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
