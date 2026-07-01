"""Wire path geometry, segments, junctions, and bridge crossings."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from fypa.topology.constants import BRIDGE_R, GND_NET, JUNCTION_R, WIRE_EPS
from fypa.topology.types import TopologyNode, TopologyWire

# Re-export for tests / public API
__all__ = [
    "WireSeg",
    "parse_wire_path",
    "path_to_segments",
    "points_to_path_d",
    "simplify_wire_path",
    "hv_intersection",
    "point_on_segment",
    "same_net_branch_count",
    "same_net_needs_junction_dot",
    "find_junctions",
    "find_bridge_crossings",
    "horizontal_crosses_node",
    "vertical_crosses_node",
    "schematic_segments",
    "compute_schematic_geometry",
    "BridgeCrossing",
    "SchematicGeometry",
    "vertical_bridge_path",
    "segment_record",
    "point_record",
    "solid_wire_index_maps",
]


@dataclass
class WireSeg:
    net: str
    orient: str
    x1: float
    y1: float
    x2: float
    y2: float
    wire_index: int = -1

    @property
    def length(self) -> float:
        if self.orient == "H":
            return abs(self.x2 - self.x1)
        return abs(self.y2 - self.y1)


@dataclass
class BridgeCrossing:
    """A different-net H/V crossing drawn as a semicircular hop on the vertical."""

    x: float
    y: float
    vertical_net: str
    horizontal_net: str
    vertical_index: int


@dataclass
class SchematicGeometry:
    """Everything needed to draw, report, and validate wire geometry once."""

    segments: list[WireSeg]
    horizontals: list[WireSeg]
    verticals: list[WireSeg]
    vert_crossings: dict[int, list[float]]
    junctions: list[tuple[float, float]]
    bridges: list[BridgeCrossing]


def _parse_wire_path(path_d: str) -> list[tuple[float, float]]:
    """Polyline vertices from an orthogonal SVG ``path`` (M / H / V only)."""
    tokens = path_d.replace(",", " ").split()
    points: list[tuple[float, float]] = []
    i = 0
    cx = cy = 0.0
    while i < len(tokens):
        cmd = tokens[i]
        if cmd == "M":
            cx, cy = float(tokens[i + 1]), float(tokens[i + 2])
            points.append((cx, cy))
            i += 3
        elif cmd == "H":
            cx = float(tokens[i + 1])
            points.append((cx, cy))
            i += 2
        elif cmd == "V":
            cy = float(tokens[i + 1])
            points.append((cx, cy))
            i += 2
        else:
            i += 1
    return points


parse_wire_path = _parse_wire_path


def _path_to_segments(
    net: str,
    points: list[tuple[float, float]],
    *,
    wire_index: int = -1,
) -> list[WireSeg]:
    segs: list[WireSeg] = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if abs(y1 - y2) < WIRE_EPS:
            y = (y1 + y2) / 2
            segs.append(WireSeg(
                net, "H", min(x1, x2), y, max(x1, x2), y, wire_index,
            ))
        elif abs(x1 - x2) < WIRE_EPS:
            x = (x1 + x2) / 2
            segs.append(WireSeg(
                net, "V", x, min(y1, y2), x, max(y1, y2), wire_index,
            ))
    return segs


path_to_segments = _path_to_segments


def _points_to_path_d(points: list[tuple[float, float]]) -> str:
    """Rebuild an orthogonal SVG path, dropping duplicate vertices."""
    if not points:
        return ""
    slim: list[tuple[float, float]] = []
    for pt in points:
        if slim and abs(pt[0] - slim[-1][0]) < WIRE_EPS and abs(pt[1] - slim[-1][1]) < WIRE_EPS:
            continue
        slim.append(pt)
    if not slim:
        return ""
    x0, y0 = slim[0]
    parts = [f"M {x0:.1f},{y0:.1f}"]
    cx, cy = x0, y0
    for x, y in slim[1:]:
        if abs(y - cy) < WIRE_EPS:
            parts.append(f"H {x:.1f}")
            cx = x
        elif abs(x - cx) < WIRE_EPS:
            parts.append(f"V {y:.1f}")
            cy = y
        else:
            parts.append(f"H {x:.1f}")
            parts.append(f"V {y:.1f}")
            cx, cy = x, y
    return " ".join(parts)


points_to_path_d = _points_to_path_d


def _simplify_wire_path(path_d: str) -> str:
    return _points_to_path_d(_parse_wire_path(path_d))


simplify_wire_path = _simplify_wire_path


def _hv_intersection(h: WireSeg, v: WireSeg) -> tuple[float, float] | None:
    """Orthogonal H/V intersection point, if the segments meet."""
    if v.orient != "V" or h.orient != "H":
        return None
    x = v.x1
    y = h.y1
    h_lo, h_hi = min(h.x1, h.x2), max(h.x1, h.x2)
    v_lo, v_hi = min(v.y1, v.y2), max(v.y1, v.y2)
    if (h_lo - WIRE_EPS <= x <= h_hi + WIRE_EPS
            and v_lo - WIRE_EPS <= y <= v_hi + WIRE_EPS):
        return (x, y)
    return None


hv_intersection = _hv_intersection


def _point_on_segment(seg: WireSeg, x: float, y: float) -> bool:
    """True when ``(x, y)`` lies on the orthogonal segment (inclusive)."""
    if seg.orient == "H":
        if abs(y - seg.y1) > WIRE_EPS:
            return False
        lo, hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
        return lo - WIRE_EPS <= x <= hi + WIRE_EPS
    if abs(x - seg.x1) > WIRE_EPS:
        return False
    lo, hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
    return lo - WIRE_EPS <= y <= hi + WIRE_EPS


point_on_segment = _point_on_segment


def _horizontal_crosses_node(
    node: TopologyNode, y: float, x_lo: float, x_hi: float,
) -> bool:
    """True when a horizontal run at ``y`` over ``[x_lo, x_hi]`` cuts a node body."""
    nx, ny, nw, nh = node.bounds
    if y < ny - WIRE_EPS or y > ny + nh + WIRE_EPS:
        return False
    return x_hi > nx + WIRE_EPS and x_lo < nx + nw - WIRE_EPS


horizontal_crosses_node = _horizontal_crosses_node


def _vertical_crosses_node(
    node: TopologyNode, x: float, y_lo: float, y_hi: float,
) -> bool:
    """True when a vertical run at ``x`` over ``[y_lo, y_hi]`` cuts a node body."""
    nx, ny, nw, nh = node.bounds
    if x < nx - WIRE_EPS or x > nx + nw + WIRE_EPS:
        return False
    return y_hi > ny + WIRE_EPS and y_lo < ny + nh - WIRE_EPS


vertical_crosses_node = _vertical_crosses_node


def _segment_directions(seg: WireSeg, x: float, y: float) -> set[str]:
    """Directions (``U``/``D``/``L``/``R``) in which ``seg`` extends from ``(x, y)``.

    Empty if the point is not on the segment. A pass-through yields two
    opposite directions; an endpoint yields one.
    """
    if not _point_on_segment(seg, x, y):
        return set()
    dirs: set[str] = set()
    if seg.orient == "H":
        lo, hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
        if x > lo + WIRE_EPS:
            dirs.add("L")
        if x < hi - WIRE_EPS:
            dirs.add("R")
    else:
        lo, hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
        if y > lo + WIRE_EPS:
            dirs.add("U")
        if y < hi - WIRE_EPS:
            dirs.add("D")
    return dirs


def _branch_directions(
    segments: list[WireSeg], x: float, y: float, net: str,
) -> set[str]:
    """Distinct wire directions of ``net`` leaving ``(x, y)`` (overlaps merged)."""
    dirs: set[str] = set()
    for s in segments:
        if s.net == net:
            dirs |= _segment_directions(s, x, y)
    return dirs


def _same_net_branch_count(
    segments: list[WireSeg], x: float, y: float, net: str,
) -> int:
    """Distinct same-net wire directions (branches) leaving ``(x, y)`` (max four)."""
    return len(_branch_directions(segments, x, y, net))


same_net_branch_count = _same_net_branch_count


def _same_net_needs_junction_dot(
    segments: list[WireSeg], x: float, y: float, net: str,
) -> bool:
    """Junction dot when three or more same-net directions meet (T or ``+``).

    Two directions are either a straight pass-through or a 90° corner — no dot.
    """
    return _same_net_branch_count(segments, x, y, net) >= 3


same_net_needs_junction_dot = _same_net_needs_junction_dot


def _classify_hv_intersection(
    h: WireSeg,
    v: WireSeg,
    *,
    segments: list[WireSeg] | None = None,
) -> tuple[str, tuple[float, float]] | None:
    """Classify an H/V meet: junction (same net, 3+ dirs) or bridge (diff net)."""
    pt = _hv_intersection(h, v)
    if pt is None:
        return None
    if h.net == v.net:
        segs = segments if segments is not None else [h, v]
        if _same_net_needs_junction_dot(segs, pt[0], pt[1], h.net):
            return ("junction", pt)
        return None
    return ("bridge", pt)


classify_hv_intersection = _classify_hv_intersection


def _gnd_symbol_junctions(
    segments: list[WireSeg],
    gnd_symbol_x: float | None,
    gnd_bus_y: float | None,
) -> set[tuple[float, float]]:
    """Junction at the GND rail where the symbol stub adds a third branch."""
    if gnd_symbol_x is None or gnd_bus_y is None:
        return set()
    x = round(gnd_symbol_x, 1)
    y = round(gnd_bus_y, 1)
    dirs = _branch_directions(segments, x, y, GND_NET)
    dirs.add("D")
    if len(dirs) >= 3:
        return {(x, y)}
    return set()


def find_junctions(segments: list[WireSeg]) -> list[tuple[float, float]]:
    """Points where 3+ same-net wire directions meet (net-agnostic T/+ dots)."""
    candidates: set[tuple[float, float, str]] = set()
    for s in segments:
        candidates.add((round(s.x1, 1), round(s.y1, 1), s.net))
        candidates.add((round(s.x2, 1), round(s.y2, 1), s.net))
    horiz = [s for s in segments if s.orient == "H"]
    vert = [s for s in segments if s.orient == "V"]
    for h in horiz:
        for v in vert:
            if h.net != v.net:
                continue
            pt = _hv_intersection(h, v)
            if pt is not None:
                candidates.add((round(pt[0], 1), round(pt[1], 1), h.net))
    joints: set[tuple[float, float]] = set()
    for x, y, net in candidates:
        if _same_net_needs_junction_dot(segments, x, y, net):
            joints.add((x, y))
    return sorted(joints)


def _segments_from_wires(
    wires: list[TopologyWire],
) -> tuple[list[WireSeg], list[WireSeg], list[WireSeg]]:
    """Decompose solid wires into segments, split into horizontals/verticals."""
    solid = [w for w in wires if not w.dashed]
    segments: list[WireSeg] = []
    for wi, w in enumerate(solid):
        segments.extend(_path_to_segments(
            w.net, _parse_wire_path(w.path_d), wire_index=wi,
        ))
    horizontals = [s for s in segments if s.orient == "H"]
    verticals = [s for s in segments if s.orient == "V"]
    return segments, horizontals, verticals


def _bridge_records(
    horizontals: list[WireSeg],
    verticals: list[WireSeg],
) -> tuple[dict[int, list[float]], list[BridgeCrossing]]:
    """One pass over different-net H/V crossings: arc Ys + rich records.

    Deduped per vertical index and rounded Y so rendering and reports agree.
    """
    vert_crossings: dict[int, list[float]] = defaultdict(list)
    bridges: list[BridgeCrossing] = []
    for vi, v in enumerate(verticals):
        seen: set[float] = set()
        for h in horizontals:
            hit = _classify_hv_intersection(h, v)
            if hit is None or hit[0] != "bridge":
                continue
            x, y = hit[1]
            ry = round(y, 1)
            if ry in seen:
                continue
            seen.add(ry)
            vert_crossings[vi].append(y)
            bridges.append(BridgeCrossing(
                x=round(x, 1),
                y=ry,
                vertical_net=v.net,
                horizontal_net=h.net,
                vertical_index=vi,
            ))
    return vert_crossings, bridges


def find_bridge_crossings(
    horizontals: list[WireSeg],
    verticals: list[WireSeg],
) -> dict[int, list[float]]:
    """Different-net H/V crossings: bridge arc Ys per vertical segment index."""
    vert_crossings, _ = _bridge_records(horizontals, verticals)
    return vert_crossings


def schematic_segments(
    wires: list[TopologyWire],
) -> tuple[
    list[WireSeg],
    list[WireSeg],
    list[WireSeg],
    dict[int, list[float]],
]:
    """Solid-wire segments plus per-vertical bridge crossing Y values."""
    segments, horizontals, verticals = _segments_from_wires(wires)
    vert_crossings = find_bridge_crossings(horizontals, verticals)
    return segments, horizontals, verticals, vert_crossings


def _filter_bridges_near_junctions(
    vert_crossings: dict[int, list[float]],
    bridges: list[BridgeCrossing],
    junctions: list[tuple[float, float]],
) -> tuple[dict[int, list[float]], list[BridgeCrossing]]:
    """Drop bridge arcs that would obscure a junction dot on the same vertical."""
    junction_set = {(round(x, 1), round(y, 1)) for x, y in junctions}
    clearance = BRIDGE_R + JUNCTION_R
    kept: list[BridgeCrossing] = []
    filtered_crossings: dict[int, list[float]] = defaultdict(list)
    for bridge in bridges:
        suppress = False
        for jx, jy in junction_set:
            if (abs(bridge.x - jx) < WIRE_EPS
                    and abs(bridge.y - jy) <= clearance + WIRE_EPS):
                suppress = True
                break
        if suppress:
            continue
        kept.append(bridge)
        filtered_crossings[bridge.vertical_index].append(bridge.y)
    return filtered_crossings, kept


def solid_wire_index_maps(
    wires: list[TopologyWire],
) -> tuple[dict[int, int], dict[int, int]]:
    """Return ``(id(wire) -> solid_i, list_index -> solid_i)`` for geometry."""
    by_id: dict[int, int] = {}
    by_index: dict[int, int] = {}
    solid_i = 0
    for wi, w in enumerate(wires):
        if not w.dashed:
            by_id[id(w)] = solid_i
            by_index[wi] = solid_i
            solid_i += 1
    return by_id, by_index


def compute_schematic_geometry(
    wires: list[TopologyWire],
    *,
    gnd_symbol_x: float | None = None,
    gnd_bus_y: float | None = None,
) -> SchematicGeometry:
    """Single source of truth: segments, junctions, and bridges in one pass.

    Render, report, and label placement all consume this so the drawn SVG,
    the debug report, and validation never diverge.
    """
    segments, horizontals, verticals = _segments_from_wires(wires)
    vert_crossings, bridges = _bridge_records(horizontals, verticals)
    junctions = sorted(
        set(find_junctions(segments))
        | _gnd_symbol_junctions(segments, gnd_symbol_x, gnd_bus_y),
    )
    vert_crossings, bridges = _filter_bridges_near_junctions(
        vert_crossings, bridges, junctions,
    )
    return SchematicGeometry(
        segments=segments,
        horizontals=horizontals,
        verticals=verticals,
        vert_crossings=vert_crossings,
        junctions=junctions,
        bridges=bridges,
    )


def vertical_bridge_path(x: float, y_lo: float, y_hi: float,
                         cross_ys: list[float]) -> str:
    """Vertical segment with semicircular hops (over horizontals) at each ``cross_ys``."""
    r = BRIDGE_R
    parts = [f"M {x:.1f},{y_lo:.1f}"]
    cursor = y_lo
    for cy in cross_ys:
        if cy - r <= cursor + WIRE_EPS:
            continue
        parts.append(f"V {cy - r:.1f}")
        parts.append(f"A {r:.1f},{r:.1f} 0 0 1 {x:.1f},{cy + r:.1f}")
        cursor = cy + r
    if y_hi > cursor + WIRE_EPS:
        parts.append(f"V {y_hi:.1f}")
    return " ".join(parts)


def segment_record(seg: WireSeg) -> dict:
    return {
        "orient": seg.orient,
        "net": seg.net,
        "x1": round(seg.x1, 1),
        "y1": round(seg.y1, 1),
        "x2": round(seg.x2, 1),
        "y2": round(seg.y2, 1),
        "length": round(seg.length, 1),
    }


def point_record(x: float, y: float) -> dict:
    return {"x": round(x, 1), "y": round(y, 1)}
