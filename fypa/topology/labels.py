"""Wire label placement for the topology schematic."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterator

from fypa.topology.constants import (
    BRIDGE_R,
    GND_NET,
    GUTTER_LABEL_MIN_H,
    LABEL_ANCHOR_FRACTIONS,
    LABEL_BESIDE_VERTICAL,
    LABEL_CHAR_WIDTH,
    LABEL_FALLBACK_ANCHOR_FRACTIONS,
    LABEL_FALLBACK_OFFSETS,
    LABEL_JUNCTION_CLEAR,
    LABEL_MIN_SPACING,
    LABEL_NODE_MARGIN,
    LABEL_SEARCH_OFFSETS,
    LABEL_SHORT_VERTICAL_THRESHOLD,
    LABEL_TEXT_HEIGHT,
    LABEL_TEXT_MIN_WIDTH,
    LABEL_VERTICAL_ANCHOR_FRACTIONS,
    LABEL_WIRE_OFFSET,
    MAX_LABEL_DISTANCE,
    MIN_LABEL_HORIZONTAL,
    PORT_WIRE_STUB,
    STUB_SEGMENT_TOLERANCE,
    WIRE_EPS,
)
from fypa.topology.geometry import (
    BridgeCrossing,
    SchematicGeometry,
    WireSeg,
    compute_schematic_geometry,
    parse_wire_path,
    path_to_segments,
    solid_wire_index_maps,
)
from fypa.topology.types import TopologyNode, TopologyWire
from fypa.topology.util import truncate_label

_LABEL_KIND_PRIORITY = (
    "hub_row",
    "hub",
    "gutter",
    "stack_column",
    "hub_tap",
    "gnd_tap",
)


@dataclass(frozen=True)
class LabelCandidate:
    x: float
    y: float
    vertical: bool
    anchor: str = "middle"
    phase: str = "primary"


def _is_port_stub_segment(seg: WireSeg) -> bool:
    return seg.orient == "H" and seg.length <= PORT_WIRE_STUB + STUB_SEGMENT_TOLERANCE


def _pick_label_horizontal(
    wire: TopologyWire,
    segs: list[WireSeg],
) -> WireSeg | None:
    horiz = [s for s in segs if s.orient == "H" and not _is_port_stub_segment(s)]
    if not horiz:
        return None
    if wire.bus_x is not None:
        bx = wire.bus_x
        at_bus = [
            s for s in horiz
            if min(s.x1, s.x2) - WIRE_EPS <= bx <= max(s.x1, s.x2) + WIRE_EPS
        ]
        if at_bus:
            qualified = [s for s in at_bus if s.length >= GUTTER_LABEL_MIN_H]
            pool = qualified or at_bus
            return min(pool, key=lambda s: (s.y1, -s.length))
    qualified = [s for s in horiz if s.length >= GUTTER_LABEL_MIN_H]
    if qualified:
        return max(qualified, key=lambda s: s.length)
    return max(horiz, key=lambda s: s.length)


def _clear_of_segments(
    x: float,
    y: float,
    *,
    vertical: bool,
    tw: float,
    th: float,
    segments: list[WireSeg],
    skip_wire_index: int | None = None,
    skip_net: str | None = None,
) -> bool:
    half_w = (th / 2 if vertical else tw / 2) + 2
    half_h = (tw / 2 if vertical else th / 2) + 2
    bx_lo, bx_hi = x - half_w, x + half_w
    by_lo, by_hi = y - half_h, y + half_h

    if vertical:
        v_block = half_w
    else:
        v_block = th / 2

    for seg in segments:
        if skip_wire_index is not None and seg.wire_index == skip_wire_index:
            continue
        if skip_net is not None and seg.net == skip_net:
            continue
        if seg.orient == "H":
            sy = seg.y1
            sx_lo, sx_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
            if sx_hi <= bx_lo or sx_lo >= bx_hi:
                continue
            if abs(sy - y) < half_h:
                return False
        else:
            sx = seg.x1
            sy_lo, sy_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
            if sx <= bx_lo or sx >= bx_hi:
                continue
            if sy_hi <= by_lo or sy_lo >= by_hi:
                continue
            if abs(sx - x) < v_block:
                return False
    return True


def _label_carriers_in_priority_order(wires: list[TopologyWire]) -> list[TopologyWire]:
    carriers: list[TopologyWire] = []
    for kind in _LABEL_KIND_PRIORITY:
        candidates = [w for w in wires if w.routing_kind == kind]
        if candidates:
            carriers.append(max(candidates, key=lambda w: len(w.path_d)))
    return carriers


def _anchor_distance(seg: WireSeg, x: float, y: float) -> float:
    if seg.orient == "H":
        x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
        if x < x_lo - WIRE_EPS or x > x_hi + WIRE_EPS:
            return float("inf")
        return abs(y - seg.y1)
    y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
    if y < y_lo - WIRE_EPS or y > y_hi + WIRE_EPS:
        return float("inf")
    return abs(x - seg.x1)


def _vertical_label_ys(seg: WireSeg) -> list[float]:
    y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
    span = y_hi - y_lo
    if span < LABEL_SHORT_VERTICAL_THRESHOLD:
        return [(y_lo + y_hi) / 2]
    return [y_lo + span * t for t in LABEL_VERTICAL_ANCHOR_FRACTIONS]


def _horizontal_candidates(
    seg: WireSeg,
    *,
    anchor_fractions: tuple[float, ...],
    offsets: tuple[float, ...],
    phase: str,
) -> Iterator[LabelCandidate]:
    x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
    y0 = seg.y1
    cx_values = {x_lo + (x_hi - x_lo) * t for t in anchor_fractions}
    cx_values.add((seg.x1 + seg.x2) / 2)
    for cx in sorted(cx_values):
        for offset in offsets:
            if offset > MAX_LABEL_DISTANCE + WIRE_EPS:
                continue
            for sign in (1, -1):
                y = y0 + sign * offset
                if _anchor_distance(seg, cx, y) > MAX_LABEL_DISTANCE + WIRE_EPS:
                    continue
                yield LabelCandidate(cx, y, vertical=False, phase=phase)


def _vertical_candidates(
    seg: WireSeg,
    sides: tuple[int, ...],
    tw: float,
    *,
    phase: str,
) -> Iterator[LabelCandidate]:
    for y_pos in _vertical_label_ys(seg):
        for side in sides:
            lx = seg.x1 + side * (LABEL_BESIDE_VERTICAL + tw / 2)
            if _anchor_distance(seg, lx, y_pos) > MAX_LABEL_DISTANCE + WIRE_EPS:
                continue
            yield LabelCandidate(lx, y_pos, vertical=True, phase=phase)


def _emit_horizontal_phases(
    wire: TopologyWire,
    horiz: list[WireSeg],
    *,
    segs: list[WireSeg],
) -> Iterator[LabelCandidate]:
    """Horizontal label candidates before vertical ones when a run is wide enough."""
    label_seg = _pick_label_horizontal(wire, segs)
    if label_seg and label_seg.length >= max(GUTTER_LABEL_MIN_H, MIN_LABEL_HORIZONTAL):
        yield from _horizontal_candidates(
            label_seg,
            anchor_fractions=LABEL_ANCHOR_FRACTIONS,
            offsets=LABEL_SEARCH_OFFSETS,
            phase="primary_horizontal",
        )

    horiz_sorted = sorted(horiz, key=lambda s: s.length, reverse=True)
    for seg in horiz_sorted:
        if seg.length < GUTTER_LABEL_MIN_H:
            continue
        yield from _horizontal_candidates(
            seg,
            anchor_fractions=LABEL_ANCHOR_FRACTIONS,
            offsets=LABEL_SEARCH_OFFSETS,
            phase="horizontal_long",
        )

    for seg in horiz_sorted:
        if seg.length < MIN_LABEL_HORIZONTAL:
            continue
        yield from _horizontal_candidates(
            seg,
            anchor_fractions=LABEL_ANCHOR_FRACTIONS,
            offsets=LABEL_SEARCH_OFFSETS,
            phase="horizontal_short",
        )


def iter_label_candidates(
    wire: TopologyWire,
    segs: list[WireSeg],
    *,
    gutter_side: int,
    tw: float,
) -> Iterator[LabelCandidate]:
    """Explicit search order for label placement (documented in README)."""
    vert = [s for s in segs if s.orient == "V"]
    horiz = [s for s in segs if s.orient == "H"]

    yield from _emit_horizontal_phases(wire, horiz, segs=segs)

    vert_sorted = sorted(vert, key=lambda s: s.length, reverse=True)
    if wire.bus_x is not None:
        at_bus = [s for s in vert if abs(s.x1 - wire.bus_x) < WIRE_EPS]
        if at_bus:
            vert_sorted = sorted(
                at_bus, key=lambda s: s.length, reverse=True,
            ) + [s for s in vert_sorted if s not in at_bus]

    for seg in vert_sorted:
        if seg.length < LABEL_SHORT_VERTICAL_THRESHOLD:
            continue
        yield from _vertical_candidates(
            seg, (gutter_side, -gutter_side), tw, phase="bus_vertical",
        )

    if vert_sorted:
        yield from _vertical_candidates(
            vert_sorted[0], (gutter_side, -gutter_side), tw, phase="vertical_fallback",
        )

    horiz_sorted = sorted(horiz, key=lambda s: s.length, reverse=True)
    if horiz_sorted:
        yield from _horizontal_candidates(
            horiz_sorted[0],
            anchor_fractions=LABEL_FALLBACK_ANCHOR_FRACTIONS,
            offsets=LABEL_FALLBACK_OFFSETS,
            phase="last_resort",
        )


def _gutter_side_for_carrier(
    carrier: TopologyWire,
    gutter_wires: list[TopologyWire],
    gutter_center: float,
    net: str,
) -> int:
    gutter_side = -1
    if carrier.bus_x is not None:
        gutter_side = -1 if carrier.bus_x < gutter_center else 1
        if gutter_wires:
            idx = next(
                (i for i, gw in enumerate(gutter_wires) if gw.net == net),
                0,
            )
            gutter_side = -1 if idx % 2 == 0 else 1
    return gutter_side


def finalize_wire_labels(
    wires: list[TopologyWire],
    *,
    nodes: list[TopologyNode] | None = None,
    geo: SchematicGeometry | None = None,
) -> None:
    """Place one label per net after all wires exist (junction / bridge aware)."""
    by_net: dict[str, list[TopologyWire]] = defaultdict(list)
    for w in wires:
        if w.dashed or w.net == GND_NET:
            continue
        by_net[w.net].append(w)

    if not by_net:
        return

    geo = geo or compute_schematic_geometry(wires)
    avoid = list(geo.junctions)
    bridge_clear = BRIDGE_R + LABEL_JUNCTION_CLEAR

    gutter_wires = sorted(
        [w for w in wires if w.bus_x is not None and not w.dashed],
        key=lambda w: (w.bus_x or 0.0, w.net),
    )
    bus_xs = [w.bus_x for w in gutter_wires if w.bus_x is not None]
    gutter_center = sum(bus_xs) / len(bus_xs) if bus_xs else 0.0

    placed: list[tuple[float, float, bool]] = []
    directive_nodes = [n for n in (nodes or []) if n.role != "GND"]
    solid_by_id, _ = solid_wire_index_maps(wires)

    for net, net_wires in sorted(by_net.items()):
        label = next((w.label for w in net_wires if w.label), "")
        if not label:
            label = truncate_label(net)
        placed_carrier: TopologyWire | None = None
        for carrier in _label_carriers_in_priority_order(net_wires):
            carrier.label = label
            gutter_side = _gutter_side_for_carrier(
                carrier, gutter_wires, gutter_center, net,
            )
            place_wire_label(
                carrier,
                wire_index=solid_by_id.get(id(carrier)),
                nodes=directive_nodes,
                avoid=avoid,
                placed=placed,
                gutter_side=gutter_side,
                segments=geo.segments,
                bridges=geo.bridges,
                bridge_clear=bridge_clear,
            )
            if carrier.label_x != 0.0 or carrier.label_y != 0.0:
                placed_carrier = carrier
                break
            carrier.label = ""
        if placed_carrier is None:
            for carrier in _label_carriers_in_priority_order(net_wires):
                carrier.label = label
                gutter_side = _gutter_side_for_carrier(
                    carrier, gutter_wires, gutter_center, net,
                )
                place_wire_label(
                    carrier,
                    wire_index=solid_by_id.get(id(carrier)),
                    nodes=directive_nodes,
                    avoid=avoid,
                    placed=placed,
                    gutter_side=gutter_side,
                    segments=geo.segments,
                    bridges=geo.bridges,
                    bridge_clear=bridge_clear,
                    relax_label_spacing=True,
                )
                if carrier.label_x != 0.0 or carrier.label_y != 0.0:
                    placed_carrier = carrier
                    break
                carrier.label = ""
        for w in net_wires:
            if w is not placed_carrier:
                w.label = ""


def place_wire_label(
    wire: TopologyWire,
    *,
    wire_index: int | None = None,
    nodes: list[TopologyNode],
    avoid: list[tuple[float, float]] | None = None,
    placed: list[tuple[float, float, bool]] | None = None,
    gutter_side: int = 1,
    segments: list[WireSeg] | None = None,
    bridges: list[BridgeCrossing] | None = None,
    bridge_clear: float = BRIDGE_R + LABEL_JUNCTION_CLEAR,
    relax_label_spacing: bool = False,
) -> None:
    """Anchor label to a wire segment using the documented candidate search."""
    points = parse_wire_path(wire.path_d)
    segs = path_to_segments(wire.net, points, wire_index=wire_index or -1)
    avoid = avoid or []
    placed = placed or []
    all_segments = segments or segs
    text = wire.label
    tw = max(len(text) * LABEL_CHAR_WIDTH, LABEL_TEXT_MIN_WIDTH)
    th = LABEL_TEXT_HEIGHT
    wire.label_text_anchor = "middle"
    wire.label_has_leader = False

    def _clear_of_points(x: float, y: float) -> bool:
        return all(
            (x - px) ** 2 + (y - py) ** 2 > LABEL_JUNCTION_CLEAR ** 2
            for px, py in avoid
        )

    def _clear_of_bridges(x: float, y: float) -> bool:
        for bridge in bridges or []:
            if (x - bridge.x) ** 2 + (y - bridge.y) ** 2 >= bridge_clear ** 2:
                continue
            if bridge.horizontal_net == wire.net and abs(y - bridge.y) > WIRE_EPS:
                continue
            return False
        return True

    def _clear_of_nodes(x: float, y: float, vertical: bool) -> bool:
        half_w = (th / 2 if vertical else tw / 2) + 2
        half_h = (tw / 2 if vertical else th) + 2
        for node in nodes:
            nx, ny, nw, nh = node.bounds
            if (nx - LABEL_NODE_MARGIN <= x + half_w
                    and x - half_w <= nx + nw + LABEL_NODE_MARGIN
                    and ny - LABEL_NODE_MARGIN <= y + half_h
                    and y - half_h <= ny + nh + LABEL_NODE_MARGIN):
                return False
        return True

    def _clear_of_other_labels(x: float, y: float, vertical: bool) -> bool:
        spacing = LABEL_MIN_SPACING / 2 if relax_label_spacing else LABEL_MIN_SPACING
        for px, py, pv in placed:
            if abs(x - px) < spacing and abs(y - py) < spacing:
                return False
            if not vertical and not pv and abs(y - py) < spacing:
                return False
        return True

    def _commit(candidate: LabelCandidate) -> bool:
        x, y = candidate.x, candidate.y
        vertical = candidate.vertical
        skip_seg = relax_label_spacing and candidate.phase == "last_resort"
        if not (_clear_of_points(x, y) and _clear_of_bridges(x, y)
                and _clear_of_nodes(x, y, vertical)
                and _clear_of_other_labels(x, y, vertical)
                and (skip_seg or _clear_of_segments(
                    x, y, vertical=vertical, tw=tw, th=th,
                    segments=all_segments, skip_wire_index=wire_index,
                    skip_net=wire.net,
                ))):
            return False
        wire.label_x, wire.label_y = x, y
        wire.label_vertical = vertical
        wire.label_text_anchor = candidate.anchor
        wire.label_has_leader = False
        placed.append((x, y, vertical))
        return True

    for candidate in iter_label_candidates(
        wire, segs, gutter_side=gutter_side, tw=tw,
    ):
        if _commit(candidate):
            return
