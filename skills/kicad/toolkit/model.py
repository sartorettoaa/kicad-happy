"""Parse a ``.kicad_pcb`` once into a plain-dataclass geometry model.

Stdlib-only (reuses ``scripts/sexp_parser.py`` for tokenising). Field names
deliberately mirror ``skills/kicad/scripts/analyze_pcb.py`` ("reference",
"net_name", "abs_x"/"abs_y", "outline_area_mm2", ...) so output from this
toolkit can be cross-referenced against that analyzer's JSON without a
mental remapping step — this is a separate, standalone parse, not a wrapper
around the analyzer.

Known limitations (see also ``toolkit/geometry.py``):
  * KiCad 5 ``(module ...)`` footprints are read on a best-effort basis
    (same fallback the analyzer uses); KiCad 6+ ``(footprint ...)`` is the
    tested path.
  * Zone *fills* are whatever was last computed by "Fill All Zones" in
    KiCad and saved to the file — this module does not run a fill solver.
    A zone with no ``filled_polygon`` children is reported unfilled even if
    the outline would obviously pour.
  * Custom pad shapes (``pad_shape == "custom"``) are approximated by their
    bounding rectangle for clearance/overlap purposes; this is flagged via
    ``Pad.shape_exact = False``.
  * Board (Bezier) edge curves (``gr_curve``) are not discretized exactly —
    their raw control points are kept as a polyline, which is reported via
    ``EdgeSegment.exact = False``. Lines, rects, circles, polygons, and arcs
    ARE exact (arcs to the discretization step in ``toolkit.geometry``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import geometry as geo
from .sexp_compat import load_sexp_parser

Point = Tuple[float, float]


# ── data model ────────────────────────────────────────────────────────────

@dataclass
class Pad:
    number: str
    kind: str                      # smd | thru_hole | np_thru_hole | connect
    shape: str                     # circle | rect | oval | roundrect | custom | trapezoid
    x: float
    y: float
    width: float
    height: float
    angle: float
    layers: Tuple[str, ...]
    net_name: str
    drill: Optional[float] = None
    shape_exact: bool = True       # False when the shape is approximated (custom/roundrect/trapezoid)

    def outline(self) -> List[Point]:
        """Best-available polygon for this pad in board coordinates."""
        if self.shape == "circle":
            pts = geo.circle_polygon((self.x, self.y), self.width / 2)
        elif self.shape == "oval":
            pts = geo.stadium_polygon(self.x, self.y, self.width, self.height)
        else:
            # rect, roundrect, trapezoid, custom: bounding rectangle. This is
            # an exact outline only for "rect"; see module-level limitations.
            pts = geo.rect_corners(self.x, self.y, self.width, self.height)
        return geo.rotate(pts, (self.x, self.y), self.angle)


@dataclass
class Footprint:
    reference: str
    value: str
    library: str
    layer: str
    x: float
    y: float
    angle: float
    dnp: bool = False
    pads: List[Pad] = field(default_factory=list)

    @property
    def pad_count(self) -> int:
        return len(self.pads)


@dataclass
class Segment:
    """A straight copper run: a routed ``segment`` or a net-assigned
    ``gr_line`` ("graphic copper" — net ties / manually-drawn copper that
    the simple track extractor would otherwise miss)."""
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net_name: str
    kind: str = "track"            # track | graphic

    @property
    def points(self) -> List[Point]:
        return [(self.x1, self.y1), (self.x2, self.y2)]

    @property
    def length_mm(self) -> float:
        return geo.polyline_length(self.points)


@dataclass
class Arc:
    """A routed ``arc`` or a net-assigned ``gr_arc``."""
    start: Point
    mid: Optional[Point]
    end: Point
    width: float
    layer: str
    net_name: str
    kind: str = "track"            # track | graphic

    @property
    def points(self) -> List[Point]:
        return geo.arc_points(self.start, self.mid, self.end)

    @property
    def length_mm(self) -> float:
        return geo.polyline_length(self.points)


@dataclass
class Via:
    x: float
    y: float
    size: float
    drill: float
    layers: Tuple[str, ...]
    net_name: str
    via_type: str = "through"      # through | blind | buried | micro


@dataclass
class Zone:
    name: str
    net_name: str
    layers: Tuple[str, ...]
    is_keepout: bool
    keepout_restrictions: Dict[str, str]
    outline: List[Point]                       # exact drawn-boundary polygon (may be empty — see outline_supported)
    outline_supported: bool                    # False => could not extract an exact polygon (report, don't guess)
    fill_polygons: Dict[str, List[List[Point]]] = field(default_factory=dict)   # layer -> [polygon, ...]

    @property
    def outline_area_mm2(self) -> float:
        return round(geo.shoelace_area(self.outline), 4) if self.outline else 0.0

    @property
    def is_filled(self) -> bool:
        return any(self.fill_polygons.values())


@dataclass
class EdgeSegment:
    kind: str                      # line | arc | rect | circle | polygon | curve
    points: List[Point]            # polyline (discretized for arc/circle; raw for curve)
    exact: bool = True             # False for curve (bezier control points, not a true discretization)


@dataclass
class Board:
    path: str
    board_thickness_mm: Optional[float]
    layers: List[dict]                         # raw (number, name, type) layer defs
    copper_layers: List[str]                   # ordered F.Cu -> ... -> B.Cu
    stackup: List[dict]                        # setup/stackup dielectric+copper layer entries
    footprints: List[Footprint]
    segments: List[Segment]
    arcs: List[Arc]
    vias: List[Via]
    zones: List[Zone]
    edge_cuts: List[EdgeSegment]

    @property
    def outline_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        pts = [p for e in self.edge_cuts for p in e.points]
        return geo.bbox(pts) if pts else None

    @property
    def outline_polygon(self) -> List[Point]:
        """Concatenation of all Edge.Cuts polylines as one point list, for a
        best-effort board-outline distance/area query. Does NOT merge
        multiple disjoint board/cutout outlines into a single ring — see
        ``outline_bbox`` for a shape-agnostic summary, and treat per-segment
        distance queries (``toolkit.queries.edge_distance``) as the source of
        truth when the board has internal cutouts."""
        return [p for e in self.edge_cuts for p in e.points]

    def nets(self) -> List[str]:
        names = {s.net_name for s in self.segments if s.net_name}
        names |= {a.net_name for a in self.arcs if a.net_name}
        names |= {v.net_name for v in self.vias if v.net_name}
        names |= {p.net_name for f in self.footprints for p in f.pads if p.net_name}
        names |= {z.net_name for z in self.zones if z.net_name}
        return sorted(names)


# ── net resolution (handles KiCad <=9 numeric nets AND KiCad 10 name-only nets) ──

def _resolve_net(net_node: Optional[list], net_names_by_id: Dict[int, str]) -> str:
    if not net_node or len(net_node) < 2:
        return ""
    if len(net_node) >= 3 and isinstance(net_node[2], str):
        return net_node[2]                     # (net N "name") — KiCad <= 9 inline form
    val = net_node[1]
    if isinstance(val, str) and not val.lstrip("-").isdigit():
        return val                             # (net "name") — KiCad 10, no numeric id at all
    try:
        return net_names_by_id.get(int(val), "")
    except (TypeError, ValueError):
        return str(val)


# ── parsing ──────────────────────────────────────────────────────────────

def _layers_from_root(sx, root: list) -> List[dict]:
    node = sx.find_first(root, "layers")
    out: List[dict] = []
    if not node:
        return out
    for item in node[1:]:
        if isinstance(item, list) and len(item) >= 3:
            num = item[0]
            try:
                num = int(num)
            except (TypeError, ValueError):
                pass
            out.append({"number": num, "name": item[1], "type": item[2]})
    return out


def _copper_layer_order(layers: List[dict]) -> List[str]:
    cu = [l for l in layers if isinstance(l.get("name"), str) and l["name"].endswith(".Cu")]
    cu.sort(key=lambda l: l["number"] if isinstance(l["number"], int) else 0)
    return [l["name"] for l in cu]


def _stackup_from_root(sx, root: list) -> List[dict]:
    setup = sx.find_first(root, "setup")
    if not setup:
        return []
    stackup = sx.find_first(setup, "stackup")
    if not stackup:
        return []
    numeric_keys = {"thickness", "epsilon_r", "loss_tangent"}
    out = []
    for layer in sx.find_all(stackup, "layer"):
        info: Dict[str, object] = {"name": layer[1] if len(layer) > 1 else ""}
        for item in layer[2:]:
            if isinstance(item, list) and len(item) >= 2:
                key, val = item[0], item[1]
                if key in numeric_keys:
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        pass
                info[key] = val
        out.append(info)
    return out


def _xy(sx, node: Optional[list], key: str) -> Optional[Point]:
    n = sx.find_first(node, key) if node else None
    if not n or len(n) < 3:
        return None
    try:
        return float(n[1]), float(n[2])
    except (TypeError, ValueError):
        return None


def _polygon_points(sx, node: Optional[list], sub_key: str = "pts") -> List[Point]:
    pts_node = sx.find_first(node, sub_key) if node else None
    if not pts_node:
        return []
    return [(float(xy[1]), float(xy[2])) for xy in sx.find_all(pts_node, "xy")
            if len(xy) >= 3]


def _parse_footprints(sx, root: list, net_names: Dict[int, str]) -> List[Footprint]:
    out: List[Footprint] = []
    fp_nodes = sx.find_all(root, "footprint") or sx.find_all(root, "module")
    for fp in fp_nodes:
        lib = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""
        at = sx.get_at(fp)
        x, y, angle = at if at else (0.0, 0.0, 0.0)
        layer = sx.get_value(fp, "layer") or "F.Cu"
        ref = sx.get_property(fp, "Reference") or ""
        value = sx.get_property(fp, "Value") or ""
        attr_node = sx.find_first(fp, "attr")
        attr_flags = [a for a in attr_node[1:] if isinstance(a, str)] if attr_node else []
        dnp = "dnp" in attr_flags
        footprint = Footprint(ref, value, lib, layer, x, y, angle, dnp)
        for pad in sx.find_all(fp, "pad"):
            if len(pad) < 4:
                continue
            number, kind, shape = str(pad[1]), str(pad[2]), str(pad[3])
            at_node = sx.find_first(pad, "at")
            if at_node and len(at_node) > 2:
                px, py = float(at_node[1]), float(at_node[2])
                # KiCad serialises the pad's `at` angle as ABSOLUTE (board-frame)
                # orientation, not footprint-relative — a pad with no angle of
                # its own simply inherits the footprint's angle (matches
                # kicad-happy's own analyze_pcb.py and the plugin toolkit).
                pangle = float(at_node[3]) if len(at_node) > 3 else angle
            else:
                px, py, pangle = 0.0, 0.0, angle
            if angle:
                (px, py), = geo.rotate([(px, py)], (0.0, 0.0), angle)
            abs_x, abs_y = x + px, y + py
            size = sx.find_first(pad, "size")
            w = float(size[1]) if size and len(size) > 1 else 0.0
            h = float(size[2]) if size and len(size) > 2 else w
            drill_node = sx.find_first(pad, "drill")
            drill = None
            if drill_node and len(drill_node) > 1:
                try:
                    drill = float(drill_node[1])
                except (TypeError, ValueError):
                    drill = None
            layers_node = sx.find_first(pad, "layers")
            pad_layers = tuple(l for l in layers_node[1:] if isinstance(l, str)) if layers_node else ()
            net_node = sx.find_first(pad, "net")
            net_name = _resolve_net(net_node, net_names)
            shape_exact = shape in ("circle", "rect", "oval")
            footprint.pads.append(Pad(number, kind, shape, abs_x, abs_y, w, h, pangle,
                                      pad_layers, net_name, drill, shape_exact))
        out.append(footprint)
    return out


def _parse_tracks(sx, root: list, net_names: Dict[int, str]) -> Tuple[List[Segment], List[Arc]]:
    segments: List[Segment] = []
    arcs: List[Arc] = []
    for seg in sx.find_all(root, "segment"):
        start, end = _xy(sx, seg, "start"), _xy(sx, seg, "end")
        if not start or not end:
            continue
        width = sx.get_value(seg, "width")
        layer = sx.get_value(seg, "layer") or ""
        net_name = _resolve_net(sx.find_first(seg, "net"), net_names)
        segments.append(Segment(start[0], start[1], end[0], end[1],
                                float(width) if width else 0.0, layer, net_name, "track"))
    for arc in sx.find_all(root, "arc"):
        start, mid, end = _xy(sx, arc, "start"), _xy(sx, arc, "mid"), _xy(sx, arc, "end")
        if not start or not end:
            continue
        width = sx.get_value(arc, "width")
        layer = sx.get_value(arc, "layer") or ""
        net_name = _resolve_net(sx.find_first(arc, "net"), net_names)
        arcs.append(Arc(start, mid, end, float(width) if width else 0.0, layer, net_name, "track"))
    # Net-assigned graphic copper — gr_line/gr_arc on a .Cu layer with a net.
    # Plain (unassigned, Edge.Cuts, or silkscreen) graphics are NOT copper and
    # are excluded; Edge.Cuts is handled separately by _parse_edge_cuts.
    for gr in sx.find_all(root, "gr_line"):
        layer = sx.get_value(gr, "layer") or ""
        if not layer.endswith(".Cu"):
            continue
        net_name = _resolve_net(sx.find_first(gr, "net"), net_names)
        if not net_name:
            continue
        start, end = _xy(sx, gr, "start"), _xy(sx, gr, "end")
        if not start or not end:
            continue
        stroke = sx.find_first(gr, "stroke")
        width = sx.get_value(stroke, "width") if stroke else sx.get_value(gr, "width")
        segments.append(Segment(start[0], start[1], end[0], end[1],
                                float(width) if width else 0.0, layer, net_name, "graphic"))
    for gr in sx.find_all(root, "gr_arc"):
        layer = sx.get_value(gr, "layer") or ""
        if not layer.endswith(".Cu"):
            continue
        net_name = _resolve_net(sx.find_first(gr, "net"), net_names)
        if not net_name:
            continue
        start, mid, end = _xy(sx, gr, "start"), _xy(sx, gr, "mid"), _xy(sx, gr, "end")
        if not start or not end:
            continue
        stroke = sx.find_first(gr, "stroke")
        width = sx.get_value(stroke, "width") if stroke else sx.get_value(gr, "width")
        arcs.append(Arc(start, mid, end, float(width) if width else 0.0, layer, net_name, "graphic"))
    return segments, arcs


def _parse_vias(sx, root: list, net_names: Dict[int, str]) -> List[Via]:
    out: List[Via] = []
    for via in sx.find_all(root, "via"):
        at = sx.get_at(via)
        size = sx.get_value(via, "size")
        drill = sx.get_value(via, "drill")
        net_name = _resolve_net(sx.find_first(via, "net"), net_names)
        layers_node = sx.find_first(via, "layers")
        layers = tuple(l for l in layers_node[1:] if isinstance(l, str)) if layers_node else ("F.Cu", "B.Cu")
        via_type = "through"
        for child in via[1:]:
            if isinstance(child, str) and child in ("blind", "buried", "micro"):
                via_type = child
                break
            if isinstance(child, list):
                break
        if at:
            out.append(Via(at[0], at[1], float(size) if size else 0.0,
                           float(drill) if drill else 0.0, layers, net_name, via_type))
    return out


def _parse_zones(sx, root: list, net_names: Dict[int, str]) -> List[Zone]:
    out: List[Zone] = []
    for zone in sx.find_all(root, "zone"):
        net_name = _resolve_net(sx.find_first(zone, "net"), net_names) or (sx.get_value(zone, "net_name") or "")
        layer = sx.get_value(zone, "layer")
        layers_node = sx.find_first(zone, "layers")
        layers = tuple(l for l in layers_node[1:] if isinstance(l, str)) if layers_node else ((layer,) if layer else ())
        keepout_node = sx.find_first(zone, "keepout")
        is_keepout = keepout_node is not None
        restrictions: Dict[str, str] = {}
        if keepout_node:
            for key in ("tracks", "vias", "pads", "copperpour", "footprints"):
                val = sx.get_value(keepout_node, key)
                if val:
                    restrictions[key] = val
        polygon_node = sx.find_first(zone, "polygon")
        outline = _polygon_points(sx, polygon_node) if polygon_node else []
        outline_supported = bool(outline)
        fill_polys: Dict[str, List[List[Point]]] = {}
        for fp_node in sx.find_all(zone, "filled_polygon"):
            fp_layer = sx.get_value(fp_node, "layer") or layer or ""
            pts = _polygon_points(sx, fp_node)
            if pts:
                fill_polys.setdefault(fp_layer, []).append(pts)
        name = sx.get_value(zone, "name") or ""
        out.append(Zone(name, net_name, layers, is_keepout, restrictions,
                        outline, outline_supported, fill_polys))
    return out


def _parse_edge_cuts(sx, root: list) -> List[EdgeSegment]:
    out: List[EdgeSegment] = []
    for item_type in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve"):
        for item in sx.find_all(root, item_type):
            if sx.get_value(item, "layer") != "Edge.Cuts":
                continue
            if item_type == "gr_line":
                s, e = _xy(sx, item, "start"), _xy(sx, item, "end")
                if s and e:
                    out.append(EdgeSegment("line", [s, e]))
            elif item_type == "gr_arc":
                s, m, e = _xy(sx, item, "start"), _xy(sx, item, "mid"), _xy(sx, item, "end")
                if s and e:
                    out.append(EdgeSegment("arc", geo.arc_points(s, m, e)))
            elif item_type == "gr_rect":
                s, e = _xy(sx, item, "start"), _xy(sx, item, "end")
                if s and e:
                    (x1, y1), (x2, y2) = s, e
                    out.append(EdgeSegment("rect", [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]))
            elif item_type == "gr_circle":
                c, e = _xy(sx, item, "center"), _xy(sx, item, "end")
                if c and e:
                    import math
                    r = math.hypot(e[0] - c[0], e[1] - c[1])
                    out.append(EdgeSegment("circle", geo.circle_polygon(c, r, sides=64)))
            elif item_type == "gr_poly":
                pts = _polygon_points(sx, item)
                if len(pts) >= 3:
                    out.append(EdgeSegment("polygon", pts + [pts[0]]))
            elif item_type == "gr_curve":
                pts = _polygon_points(sx, item)
                if pts:
                    out.append(EdgeSegment("curve", pts, exact=False))
    return out


def parse_pcb(path: Path | str) -> Board:
    """Parse a ``.kicad_pcb`` file into a :class:`Board`. No caching here —
    callers that re-query repeatedly should keep the returned object."""
    sx = load_sexp_parser()
    path = Path(path)
    root = sx.parse_file(str(path))

    net_names: Dict[int, str] = {}
    for item in root:
        if isinstance(item, list) and len(item) >= 3 and item[0] == "net":
            try:
                net_names[int(item[1])] = str(item[2])
            except (TypeError, ValueError):
                continue

    layers = _layers_from_root(sx, root)
    copper_layers = _copper_layer_order(layers)
    stackup = _stackup_from_root(sx, root)

    general = sx.find_first(root, "general")
    thickness = None
    if general:
        t = sx.get_value(general, "thickness")
        if t:
            try:
                thickness = float(t)
            except (TypeError, ValueError):
                thickness = None

    footprints = _parse_footprints(sx, root, net_names)
    segments, arcs = _parse_tracks(sx, root, net_names)
    vias = _parse_vias(sx, root, net_names)
    zones = _parse_zones(sx, root, net_names)
    edge_cuts = _parse_edge_cuts(sx, root)

    return Board(str(path), thickness, layers, copper_layers, stackup,
                footprints, segments, arcs, vias, zones, edge_cuts)
