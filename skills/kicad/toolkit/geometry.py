"""Pure-stdlib 2-D geometry helpers (millimetres), no third-party dependency.

These are the only geometry primitives the rest of ``toolkit`` relies on for
"exact" results: straight-line segments and explicitly discretized arcs.

Limitations (read before trusting a "min distance" number):
  * Arcs (KiCad ``arc``/``gr_arc``) are discretized into a polyline with a
    maximum chord step of ``ARC_MAX_STEP_MM``. Distances touching an arc are
    therefore accurate to within roughly that step, not mathematically exact.
  * ``dist_segment_to_segment`` / polygon-vs-polyline distances ARE exact for
    straight edges (the classic "clamped projection" formula), so once a
    curve has been discretized the rest of the pipeline introduces no further
    error.
  * None of this module does polygon clipping or area-of-intersection — only
    distances and (for simple, non-self-intersecting polygons) exact shoelace
    area. True polygon overlap/union is left to the optional Shapely path in
    ``toolkit.deps`` / ``toolkit.queries`` where that matters (e.g. very large
    zone fill polygons), with a clearly labelled stdlib fallback otherwise.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float]

ARC_MAX_STEP_MM = 0.05
CIRCLE_SIDES = 24


def arc_points(start: Point, mid: Optional[Point], end: Point,
               max_step_mm: float = ARC_MAX_STEP_MM) -> List[Point]:
    """Discretize the circular arc through start→mid→end into a polyline.

    Returns ``[start, ..., end]`` with exact endpoints preserved (so chained
    track/outline segments still node together). Falls back to a straight
    line when the three points are (numerically) collinear or ``mid`` is
    unavailable, matching how a zero-radius arc degenerates in KiCad.
    """
    if mid is None:
        return [start, end]
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1)
          + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3)
          + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)
    ccw = (a3 - a1) % (2 * math.pi)
    sweep = ccw if (a2 - a1) % (2 * math.pi) <= ccw else ccw - 2 * math.pi
    n = max(2, int(abs(sweep) * r / max_step_mm) + 1)
    pts = [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n))
           for i in range(1, n)]
    return [start] + pts + [end]


def circle_polygon(center: Point, radius: float, sides: int = CIRCLE_SIDES) -> List[Point]:
    """N-gon approximation of a circle (vias, round pads, drill holes)."""
    cx, cy = center
    return [(cx + radius * math.cos(2 * math.pi * i / sides),
             cy + radius * math.sin(2 * math.pi * i / sides)) for i in range(sides)]


def rotate(points: Sequence[Point], origin: Point, angle_deg: float) -> List[Point]:
    """Rotate points about origin using KiCad's convention (CCW on screen,
    y-down → negate the angle to get a math-convention rotation)."""
    if not angle_deg:
        return list(points)
    rad = math.radians(-angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    ox, oy = origin
    out = []
    for x, y in points:
        dx, dy = x - ox, y - oy
        out.append((ox + dx * cos_a - dy * sin_a, oy + dx * sin_a + dy * cos_a))
    return out


def polyline_length(points: Sequence[Point]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def dist_point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Exact distance from a point to a line segment (clamped projection)."""
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _ccw(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_intersect(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    d1 = _ccw(cx, cy, dx, dy, ax, ay)
    d2 = _ccw(cx, cy, dx, dy, bx, by)
    d3 = _ccw(ax, ay, bx, by, cx, cy)
    d4 = _ccw(ax, ay, bx, by, dx, dy)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True

    def on_seg(px, py, qx, qy, rx, ry) -> bool:
        return min(px, rx) - 1e-9 <= qx <= max(px, rx) + 1e-9 and min(py, ry) - 1e-9 <= qy <= max(py, ry) + 1e-9

    if d1 == 0 and on_seg(cx, cy, ax, ay, dx, dy):
        return True
    if d2 == 0 and on_seg(cx, cy, bx, by, dx, dy):
        return True
    if d3 == 0 and on_seg(ax, ay, cx, cy, bx, by):
        return True
    if d4 == 0 and on_seg(ax, ay, dx, dy, bx, by):
        return True
    return False


def dist_segment_to_segment(ax, ay, bx, by, cx, cy, dx, dy) -> float:
    """Exact minimum distance between segments AB and CD (0.0 if they cross)."""
    if segments_intersect(ax, ay, bx, by, cx, cy, dx, dy):
        return 0.0
    return min(
        dist_point_to_segment(ax, ay, cx, cy, dx, dy),
        dist_point_to_segment(bx, by, cx, cy, dx, dy),
        dist_point_to_segment(cx, cy, ax, ay, bx, by),
        dist_point_to_segment(dx, dy, ax, ay, bx, by),
    )


def dist_point_to_polyline(px: float, py: float, points: Sequence[Point], closed: bool = False) -> float:
    pairs = list(zip(points, points[1:])) + ([(points[-1], points[0])] if closed and len(points) > 2 else [])
    if not pairs:
        return math.hypot(px - points[0][0], py - points[0][1]) if points else math.inf
    return min(dist_point_to_segment(px, py, a[0], a[1], b[0], b[1]) for a, b in pairs)


def dist_polyline_to_polyline(a: Sequence[Point], b: Sequence[Point],
                              closed_a: bool = False, closed_b: bool = False) -> float:
    """Exact min distance between two polylines (treat as closed polygons to
    also cover edges that wrap from the last vertex back to the first)."""
    edges_a = list(zip(a, a[1:])) + ([(a[-1], a[0])] if closed_a and len(a) > 2 else [])
    edges_b = list(zip(b, b[1:])) + ([(b[-1], b[0])] if closed_b and len(b) > 2 else [])
    if not edges_a or not edges_b:
        return math.inf
    best = math.inf
    for (ax, ay), (bx, by) in edges_a:
        for (cx, cy), (dx, dy) in edges_b:
            d = dist_segment_to_segment(ax, ay, bx, by, cx, cy, dx, dy)
            if d < best:
                best = d
                if best == 0.0:
                    return 0.0
    return best


def shoelace_area(points: Sequence[Point]) -> float:
    """Exact polygon area (mm²) via the shoelace formula. 0 for <3 points."""
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def point_in_polygon(px: float, py: float, polygon: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon test (same algorithm the fork's own
    ``analyze_pcb.py`` uses for zone-fill queries, so results agree)."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-300) + xi):
            inside = not inside
        j = i
    return inside


def bbox(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_gap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    """Minimum gap between two axis-aligned bounding boxes (0 if they overlap/touch)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return math.hypot(dx, dy)


def rect_corners(cx: float, cy: float, w: float, h: float) -> List[Point]:
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
            (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


def stadium_polygon(cx: float, cy: float, w: float, h: float, sides_per_cap: int = 8) -> List[Point]:
    """KiCad "oval" pad: a stadium shape (rect body + two semicircular caps).

    Degenerates to ``circle_polygon`` when ``w == h``.
    """
    if abs(w - h) < 1e-9:
        return circle_polygon((cx, cy), w / 2, sides=2 * sides_per_cap)
    horizontal = w >= h
    r = min(w, h) / 2
    half_len = (max(w, h) / 2) - r
    pts: List[Point] = []
    if horizontal:
        centers = [(cx + half_len, cy), (cx - half_len, cy)]
        start_angles = [-90, 90]
    else:
        centers = [(cx, cy + half_len), (cx, cy - half_len)]
        start_angles = [0, 180]
    for (ccx, ccy), start_deg in zip(centers, start_angles):
        for i in range(sides_per_cap + 1):
            a = math.radians(start_deg + 180 * i / sides_per_cap)
            pts.append((ccx + r * math.cos(a), ccy + r * math.sin(a)))
    return pts
