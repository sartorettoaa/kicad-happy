"""Read-only queries over a :class:`toolkit.model.Board`.

Every function returns plain dicts/lists (JSON-ready) so a caller — human or
an AI review agent — can cite the result verbatim as evidence. Distances and
areas are millimetres / mm². Nothing here mutates the board or writes files;
see ``toolkit/cli.py`` for the only place stdout is produced.

What is "exact" vs "approximate" (read this before trusting a number):
  * ``component_inventory`` / ``stackup_summary`` — exact transcription of
    the file (no geometry).
  * ``net_routing`` — track/arc lengths are exact for straight segments and
    accurate to the arc discretization step for arcs (``toolkit.geometry``).
  * ``keepout_summary`` — outline vertices/area are exact when the zone has
    a ``(polygon (pts ...))`` node (always true for keepouts drawn with the
    Rule Area / Keepout Area tool). If a keepout cannot be reduced to an
    exact polygon, ``outline_supported`` is ``False`` and no vertex/area
    data is fabricated.
  * ``edge_distance`` — exact polygon-to-polyline distance (segment-to-
    segment, see ``geometry.dist_polyline_to_polyline``) once arcs/pads are
    discretized; accurate to that discretization step, not an approximation
    on top of it.
  * ``net_clearance`` — copper-to-copper (not centerline-to-centerline)
    clearance. Tracks/arcs are modelled as round-cap "capsules": the exact
    stdlib segment-distance engine computes the centerline-to-centerline (or
    boundary-to-boundary, for pad/via/zone-fill polygons which are already
    the true copper outline) distance, then each track/arc side subtracts
    its own half-width — this is exact for straight segments and accurate to
    the arc discretization step (``toolkit.geometry.ARC_MAX_STEP_MM``) for
    arcs. Pad/via/zone-fill polygons contribute zero extra width since the
    stored polygon already *is* the copper boundary; vias (rendered as an
    N-gon) and pads with ``shape_exact=False`` (custom/roundrect/trapezoid,
    approximated by their bounding rectangle) make the result approximate —
    flagged via ``exact=False`` and a ``note`` naming the offending shape(s).
    Overlapping/contained copper (including a polygon fully nested inside
    another with no crossing edges — plain edge-to-edge distance alone
    misses that case) is detected and reported as ``copper_gap_mm <= 0``,
    never a false-positive separation. This is O(edges_a × edges_b) per
    layer, which is fine for tracks/vias/pads but can be slow on nets that
    include a very large zone fill (thousands of vertices). When
    ``shapely`` is installed, pass ``prefer_shapely=True`` to buffer
    tracks/arcs and use ``STRtree``/prepared-geometry for that case instead;
    the result should be numerically equivalent, just faster on big fills.
  * Copper resistance / IR drop / trace loss is intentionally NOT
    implemented here — the fork has no validated model for it yet, and the
    user's instructions are explicit that unvalidated numbers must not be
    reported as exact. Use ``net_routing`` (length/width/layer) as the
    factual input to any such calculation done elsewhere.
"""

from __future__ import annotations

import fnmatch
from typing import Dict, Iterable, List, Optional, Tuple

from . import deps
from . import geometry as geo
from .model import Arc, Board, Footprint, Pad, Segment, Via, Zone

Point = Tuple[float, float]


# ── stackup / facts ──────────────────────────────────────────────────────

def stackup_summary(board: Board) -> dict:
    """Board thickness, layer stack, and outline bounding box/area.

    ``dielectric`` entries come straight from ``(setup (stackup ...))``; a
    board saved without an explicit stackup (older files, or "use default
    nets" boards) will have an empty list here — that is reported as-is,
    not inferred.
    """
    outline = board.outline_bbox
    exact_outline = all(e.exact for e in board.edge_cuts) if board.edge_cuts else True
    return {
        "path": board.path,
        "board_thickness_mm": board.board_thickness_mm,
        "copper_layers": board.copper_layers,
        "copper_layer_count": len(board.copper_layers),
        "all_layers": board.layers,
        "dielectric_stackup": board.stackup,
        "outline_bbox_mm": list(outline) if outline else None,
        "outline_exact": exact_outline,
        "edge_cuts_segment_count": len(board.edge_cuts),
    }


# ── component inventory ──────────────────────────────────────────────────

def component_inventory(board: Board, ref_glob: Optional[str] = None) -> List[dict]:
    """Flat list of placed footprints, optionally filtered by a glob on the
    reference designator (e.g. ``"C*"``, ``"U1"``)."""
    rows = []
    for fp in board.footprints:
        if ref_glob and not fnmatch.fnmatchcase(fp.reference, ref_glob):
            continue
        rows.append({
            "reference": fp.reference,
            "value": fp.value,
            "library": fp.library,
            "layer": fp.layer,
            "x": round(fp.x, 4),
            "y": round(fp.y, 4),
            "angle": fp.angle,
            "dnp": fp.dnp,
            "pad_count": fp.pad_count,
            "nets": sorted({p.net_name for p in fp.pads if p.net_name}),
        })
    return sorted(rows, key=lambda r: r["reference"])


# ── net routing (incl. gr_* graphic copper) ───────────────────────────────

def net_routing(board: Board, net_name: str) -> dict:
    """Routing summary for one net: length per layer (track vs. graphic
    copper broken out), width set, via count/list, and pads on the net."""
    segs = [s for s in board.segments if s.net_name == net_name]
    arcs = [a for a in board.arcs if a.net_name == net_name]
    by_layer: Dict[str, float] = {}
    graphic_by_layer: Dict[str, float] = {}
    widths: set = set()
    for s in segs:
        by_layer[s.layer] = by_layer.get(s.layer, 0.0) + s.length_mm
        widths.add((s.layer, round(s.width, 4)))
        if s.kind == "graphic":
            graphic_by_layer[s.layer] = graphic_by_layer.get(s.layer, 0.0) + s.length_mm
    for a in arcs:
        by_layer[a.layer] = by_layer.get(a.layer, 0.0) + a.length_mm
        widths.add((a.layer, round(a.width, 4)))
        if a.kind == "graphic":
            graphic_by_layer[a.layer] = graphic_by_layer.get(a.layer, 0.0) + a.length_mm
    vias = [v for v in board.vias if v.net_name == net_name]
    pads = [f"{f.reference}.{p.number}" for f in board.footprints for p in f.pads if p.net_name == net_name]
    zone_layers = sorted({ly for z in board.zones if z.net_name == net_name for ly in z.layers})
    return {
        "net": net_name,
        "found": bool(segs or arcs or vias or pads or zone_layers),
        "length_mm": round(sum(by_layer.values()), 4),
        "by_layer_mm": {k: round(v, 4) for k, v in sorted(by_layer.items())},
        "graphic_copper_by_layer_mm": {k: round(v, 4) for k, v in sorted(graphic_by_layer.items())},
        "widths": sorted([{"layer": ly, "width_mm": w} for ly, w in widths], key=lambda d: (d["layer"], d["width_mm"])),
        "vias": [{"x": round(v.x, 4), "y": round(v.y, 4), "size_mm": v.size, "drill_mm": v.drill,
                 "type": v.via_type, "layers": list(v.layers)} for v in vias],
        "via_count": len(vias),
        "pads": sorted(pads),
        "zone_layers": zone_layers,
    }


# ── keepouts ───────────────────────────────────────────────────────────────

def keepout_summary(board: Board, nearby_margin_mm: float = 5.0) -> List[dict]:
    """Keepout/rule-area zones with their exact outline (when available).

    ``nearby_components`` lists footprints whose placement point falls
    within ``nearby_margin_mm`` of the keepout's bounding box — the same
    heuristic ``analyze_pcb.py`` uses, kept for easy cross-reference; it is
    a coarse (point, not full-footprint-shape) proximity check.
    """
    rows = []
    for z in board.zones:
        if not z.is_keepout:
            continue
        entry: Dict[str, object] = {
            "name": z.name,
            "layers": list(z.layers),
            "restrictions": z.keepout_restrictions,
            "net": z.net_name,
            "outline_supported": z.outline_supported,
        }
        if z.outline_supported:
            bbox = geo.bbox(z.outline)
            entry["outline_vertices"] = [[round(x, 4), round(y, 4)] for x, y in z.outline]
            entry["outline_area_mm2"] = z.outline_area_mm2
            entry["outline_bbox_mm"] = list(bbox)
            nearby = []
            for fp in board.footprints:
                x0, y0, x1, y1 = bbox
                if x0 - nearby_margin_mm <= fp.x <= x1 + nearby_margin_mm and \
                   y0 - nearby_margin_mm <= fp.y <= y1 + nearby_margin_mm:
                    nearby.append(fp.reference)
            entry["nearby_components"] = sorted(nearby)
        else:
            entry["reason"] = "zone has no (polygon (pts ...)) outline node; unsupported detail"
        rows.append(entry)
    return rows


# ── edge distance ────────────────────────────────────────────────────────

def _net_copper_edges(board: Board, net_name: str, layer: Optional[str] = None) -> Dict[str, List[List[Point]]]:
    """All copper belonging to ``net_name`` as closed/open polylines per
    layer: track/graphic segments and arcs (as polylines), pad outlines
    (closed), via barrels (closed, N-gon). Zone fills are included too
    (already-polygon, from the file's last computed fill)."""
    by_layer: Dict[str, List[List[Point]]] = {}

    def add(ly: str, pts: List[Point]) -> None:
        if layer and ly != layer:
            return
        by_layer.setdefault(ly, []).append(pts)

    for s in board.segments:
        if s.net_name == net_name:
            add(s.layer, s.points)
    for a in board.arcs:
        if a.net_name == net_name:
            add(a.layer, a.points)
    for f in board.footprints:
        for p in f.pads:
            if p.net_name != net_name:
                continue
            outline = p.outline()
            for ly in p.layers:
                if ly == "*.Cu":
                    for cu in board.copper_layers:
                        add(cu, outline)
                elif ly.endswith(".Cu"):
                    add(ly, outline)
    for v in board.vias:
        if v.net_name == net_name:
            circle = geo.circle_polygon((v.x, v.y), v.size / 2)
            for ly in v.layers:
                add(ly, circle)
    for z in board.zones:
        if z.net_name != net_name:
            continue
        for ly, polys in z.fill_polygons.items():
            for poly in polys:
                add(ly, poly)
    return by_layer


def _net_copper_pieces(board: Board, net_name: str, layer: Optional[str] = None) -> Dict[str, List[Dict[str, object]]]:
    """Like :func:`_net_copper_edges` but keeps the per-piece metadata
    :func:`net_clearance` needs for a *copper-to-copper* (not centerline-to-
    centerline) answer:

    * ``half_width`` — half the trace width to subtract for a track/arc
      centerline; ``0.0`` for pad/via/zone-fill polygons, which already
      store the true copper boundary (nothing extra to subtract).
    * ``closed`` — whether ``points`` is a closed outline (pad/via/zone
      fill) vs. an open centerline polyline (track/arc).
    * ``exact`` — ``False`` for geometry that is only approximated (a pad
      with ``shape_exact=False``, or a via — always rendered as an N-gon
      standing in for a true circle); mirrors the limitations already
      documented on :class:`toolkit.model.Pad`/``Via``, just surfaced here.
    """
    by_layer: Dict[str, List[Dict[str, object]]] = {}

    def add(ly: str, pts: List[Point], *, closed: bool, half_width: float,
            source: str, exact: bool = True) -> None:
        if layer and ly != layer:
            return
        if len(pts) < 2:
            return
        by_layer.setdefault(ly, []).append({
            "points": pts, "closed": closed, "half_width": max(0.0, half_width),
            "source": source, "exact": exact,
        })

    for s in board.segments:
        if s.net_name == net_name:
            add(s.layer, s.points, closed=False, half_width=s.width / 2.0, source="track")
    for a in board.arcs:
        if a.net_name == net_name:
            add(a.layer, a.points, closed=False, half_width=a.width / 2.0, source="arc")
    for f in board.footprints:
        for p in f.pads:
            if p.net_name != net_name:
                continue
            outline = p.outline()
            for ly in p.layers:
                if ly == "*.Cu":
                    for cu in board.copper_layers:
                        add(cu, outline, closed=True, half_width=0.0, source="pad", exact=p.shape_exact)
                elif ly.endswith(".Cu"):
                    add(ly, outline, closed=True, half_width=0.0, source="pad", exact=p.shape_exact)
    for v in board.vias:
        if v.net_name == net_name:
            circle = geo.circle_polygon((v.x, v.y), v.size / 2)
            for ly in v.layers:
                add(ly, circle, closed=True, half_width=0.0, source="via", exact=False)
    for z in board.zones:
        if z.net_name != net_name:
            continue
        for ly, polys in z.fill_polygons.items():
            for poly in polys:
                add(ly, poly, closed=True, half_width=0.0, source="zone_fill")
    return by_layer


def _piece_boundary_distance(pa: Dict[str, object], pb: Dict[str, object]) -> float:
    """Exact boundary-to-boundary distance between two copper pieces'
    ``points`` (centerlines for track/arc, true outlines for pad/via/zone
    fill), including the case plain edge-to-edge distance misses: one closed
    polygon fully nested inside the other with no crossing edges (e.g. a
    zone fill contained in another net's zone fill) — that is copper overlap
    too, reported as ``0.0``, never a false-positive gap."""
    pts_a, pts_b = pa["points"], pb["points"]
    closed_a = bool(pa["closed"]) and len(pts_a) > 2
    closed_b = bool(pb["closed"]) and len(pts_b) > 2
    d = geo.dist_polyline_to_polyline(pts_a, pts_b, closed_a=closed_a, closed_b=closed_b)
    if d > 0.0 and (closed_a or closed_b):
        if closed_b and any(geo.point_in_polygon(x, y, pts_b) for x, y in pts_a):
            return 0.0
        if closed_a and any(geo.point_in_polygon(x, y, pts_a) for x, y in pts_b):
            return 0.0
    return d


def edge_distance(board: Board, net_name: str) -> Optional[dict]:
    """Minimum distance from ``net_name``'s copper to the board outline
    (Edge.Cuts), across all layers that carry both outline geometry and the
    net's copper. Returns ``None`` (with a reason) when there is no
    Edge.Cuts geometry or the net has no copper at all — never a fabricated
    number."""
    if not board.edge_cuts:
        return {"net": net_name, "min_mm": None, "reason": "no Edge.Cuts geometry on this board"}
    by_layer = _net_copper_edges(board, net_name)
    if not by_layer:
        return {"net": net_name, "min_mm": None, "reason": "net has no copper (tracks/arcs/pads/vias/zone fill)"}
    outline_polys = [e.points for e in board.edge_cuts]
    best = None
    best_layer = None
    for layer, polys in by_layer.items():
        for poly in polys:
            for outline in outline_polys:
                d = geo.dist_polyline_to_polyline(poly, outline, closed_a=True, closed_b=True)
                if best is None or d < best:
                    best, best_layer = d, layer
    exact = all(e.exact for e in board.edge_cuts)
    return {"net": net_name, "min_mm": round(best, 4) if best is not None else None,
           "layer": best_layer, "exact": exact,
           "note": None if exact else "board outline includes a gr_curve; distance may be approximate near it"}


# ── clearance / overlap (raw geometry, stdlib-exact; shapely optional for scale) ──

def net_clearance(board: Board, net_a: str, net_b: str, layer: Optional[str] = None,
                  prefer_shapely: bool = False) -> dict:
    """Minimum copper-to-copper gap between two nets, per shared layer.

    Returns both numbers so a caller can see the correction being applied:

    * ``centerline_min_mm`` — raw minimum distance between the stored
      geometries (track/arc centerlines; pad/via/zone-fill true outlines),
      with no width subtracted. This is what a naive "distance between the
      drawn lines" query would report, and over-states the real copper gap
      whenever either side is a track or arc.
    * ``copper_gap_mm`` (also exposed as ``min_mm`` for backward
      compatibility) — the actual copper-to-copper clearance: each
      track/arc side's half-width is subtracted from the centerline
      distance (round-cap "capsule" model, exact for straight segments,
      accurate to the arc discretization step for arcs); pad/via/zone-fill
      polygons contribute no extra subtraction since they already store the
      true boundary. Overlapping or contained copper — including a polygon
      fully nested inside another with no crossing edges, which plain
      edge-to-edge distance alone would misreport as a positive gap — comes
      back ``<= 0``, never a false-positive separation.

    By default uses the exact stdlib segment-distance engine
    (``toolkit.geometry``). Set ``prefer_shapely=True`` when a net includes
    a large zone fill and Shapely is installed, for a faster (not more
    correct) computation — tracks/arcs are buffered by their half-width
    with a round cap (``buffer(..., cap_style=1)``) to match the stdlib
    capsule model, and Shapely's own polygon distance already handles
    containment correctly. Falls back to the stdlib path automatically if
    Shapely is unavailable, with ``engine`` in the result telling you which
    one actually ran.

    ``exact`` is ``False`` (with a ``note`` naming the shape) when a
    contributing pad is only approximated by its bounding rectangle
    (``shape_exact=False`` — custom/roundrect/trapezoid) or a via is
    involved (always rendered as an N-gon standing in for a true circle).
    """
    pieces_a = _net_copper_pieces(board, net_a, layer)
    pieces_b = _net_copper_pieces(board, net_b, layer)
    layers = sorted(set(pieces_a) & set(pieces_b))
    engine = "stdlib_exact"
    use_shapely = prefer_shapely and deps.has_shapely()
    if use_shapely:
        engine = "shapely"
        shp = deps.shapely_geometry()

    per_layer_gap: Dict[str, Optional[float]] = {}
    per_layer_centerline: Dict[str, Optional[float]] = {}
    inexact_sources: set = set()

    def _note_exactness(piece: Dict[str, object]) -> None:
        if not piece["exact"]:
            inexact_sources.add(str(piece["source"]))

    for ly in layers:
        for pc in pieces_a[ly]:
            _note_exactness(pc)
        for pc in pieces_b[ly]:
            _note_exactness(pc)

        if use_shapely:
            def to_geom(pieces, buffered: bool):
                from shapely.ops import unary_union
                parts = []
                for pc in pieces:
                    pts = pc["points"]
                    if len(pts) < 2:
                        continue
                    if pc["closed"] and len(pts) >= 3:
                        g = shp.Polygon(pts)
                        g = g if g.is_valid else g.buffer(0)
                    else:
                        g = shp.LineString(pts)
                        if buffered and pc["half_width"] > 0:
                            g = g.buffer(pc["half_width"], cap_style=1)
                    parts.append(g)
                return unary_union(parts) if parts else None

            ga_raw, gb_raw = to_geom(pieces_a[ly], False), to_geom(pieces_b[ly], False)
            per_layer_centerline[ly] = (round(ga_raw.distance(gb_raw), 4)
                                        if ga_raw is not None and gb_raw is not None else None)
            ga_buf, gb_buf = to_geom(pieces_a[ly], True), to_geom(pieces_b[ly], True)
            per_layer_gap[ly] = (round(ga_buf.distance(gb_buf), 4)
                                 if ga_buf is not None and gb_buf is not None else None)
        else:
            best_gap = None
            best_centerline = None
            for pa in pieces_a[ly]:
                for pb in pieces_b[ly]:
                    raw = _piece_boundary_distance(pa, pb)
                    gap = raw - float(pa["half_width"]) - float(pb["half_width"])
                    if best_gap is None or gap < best_gap:
                        best_gap, best_centerline = gap, raw
            per_layer_gap[ly] = round(best_gap, 4) if best_gap is not None else None
            per_layer_centerline[ly] = round(best_centerline, 4) if best_centerline is not None else None

    valid_gap = [v for v in per_layer_gap.values() if v is not None]
    valid_centerline = [v for v in per_layer_centerline.values() if v is not None]
    overall_gap = min(valid_gap) if valid_gap else None
    overall_centerline = min(valid_centerline) if valid_centerline else None
    exact = not inexact_sources
    return {
        "net_a": net_a, "net_b": net_b, "engine": engine,
        "model": "centerline distance minus each side's half trace-width (round-cap capsule) "
                 "for tracks/arcs; pad/via/zone-fill polygons used as their true outline as-is",
        "per_layer_mm": per_layer_gap,
        "centerline_per_layer_mm": per_layer_centerline,
        "min_mm": overall_gap,
        "copper_gap_mm": overall_gap,
        "centerline_min_mm": overall_centerline,
        "shared_layers": layers,
        "exact": exact,
        "note": None if exact else (
            "clearance includes approximated geometry on a shared layer (" +
            ", ".join(sorted(inexact_sources)) +
            ") — pad shape_exact=False is a bounding-rectangle approximation, via is an N-gon "
            "standing in for a true circle; treat the number as directionally correct, not exact "
            "to machine precision"
        ),
    }


def analyzer_json_overlap(analyzer_json: dict, ref_a: str, ref_b: str) -> dict:
    """Approximate proximity between two footprints using a previously
    generated ``analyze_pcb.py`` JSON (the analyzer/triage pipeline is out
    of scope here — this only *reads* its output). Uses the courtyard
    bounding box when present, else the footprint placement point; this is
    a bounding-box gap, NOT exact copper-to-copper distance. Use
    :func:`net_clearance` on the raw board for an exact answer.
    """
    footprints = analyzer_json.get("footprints")
    if footprints is None:
        return {"error": "input JSON has no 'footprints' key — is this an analyze_pcb.py output?"}
    by_ref = {fp.get("reference"): fp for fp in footprints}
    fa, fb = by_ref.get(ref_a), by_ref.get(ref_b)
    if fa is None or fb is None:
        missing = [r for r, f in ((ref_a, fa), (ref_b, fb)) if f is None]
        return {"error": f"reference(s) not found in analyzer JSON: {missing}"}

    def bbox_of(fp: dict):
        crt = fp.get("courtyard")
        if crt:
            return (crt["min_x"], crt["min_y"], crt["max_x"], crt["max_y"])
        x, y = fp.get("x", 0.0), fp.get("y", 0.0)
        return (x, y, x, y)

    bbox_a, bbox_b = bbox_of(fa), bbox_of(fb)
    return {
        "ref_a": ref_a, "ref_b": ref_b, "method": "bounding_box_gap (approximate)",
        "bbox_a": list(bbox_a), "bbox_b": list(bbox_b),
        "gap_mm": round(geo.bbox_gap(bbox_a, bbox_b), 4),
        "note": "approximate: courtyard/placement bounding boxes, not exact copper or body silhouette",
    }
