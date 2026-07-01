"""Wire label placement for the topology schematic."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from collections.abc import Iterator

from fypa.topology.constants import (
    BRIDGE_R,
    GND_NET,
    GUTTER_LABEL_MIN_H,
    LABEL_CHAR_WIDTH,
    LABEL_JUNCTION_CLEAR,
    LABEL_MIN_SPACING,
    LABEL_NODE_MARGIN,
    LABEL_SHORT_VERTICAL_THRESHOLD,
    LABEL_TEXT_HEIGHT,
    LABEL_TEXT_MIN_WIDTH,
    MIN_LABEL_HORIZONTAL,
    PORT_WIRE_STUB,
    STUB_SEGMENT_TOLERANCE,
)
from fypa.topology.geometry import (
    BridgeCrossing,
    SchematicGeometry,
    WireSeg,
    compute_schematic_geometry,
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

_SEGMENT_ANCHOR_FRACTIONS = (0.5, 0.35, 0.65, 0.25, 0.75, 0.4, 0.6)


@dataclass(frozen=True)
class LabelCandidate:
    x: float
    y: float
    vertical: bool
    anchor: str = "middle"
    phase: str = "primary"


def label_text_size(text: str) -> tuple[float, float]:
    """Return ``(width, height)`` for an 8pt net label."""
    tw = max(len(text) * LABEL_CHAR_WIDTH, LABEL_TEXT_MIN_WIDTH)
    return tw, LABEL_TEXT_HEIGHT


def label_hit_bounds(
    wire: TopologyWire,
) -> tuple[float, float, float, float] | None:
    """Axis-aligned hit box ``(x_lo, y_lo, x_hi, y_hi)`` for a placed label."""
    if not wire.label or (wire.label_x == 0.0 and wire.label_y == 0.0):
        return None
    tw, th = label_text_size(wire.label)
    x, y = wire.label_x, wire.label_y
    pad = 2.0
    if wire.label_vertical:
        half_w = th / 2 + pad
        half_h = tw / 2
    else:
        half_w = tw / 2 + pad
        half_h = th / 2
    return (x - half_w, y - half_h, x + half_w, y + half_h)


def _is_port_stub_segment(seg: WireSeg) -> bool:
    return seg.orient == "H" and seg.length <= PORT_WIRE_STUB + STUB_SEGMENT_TOLERANCE


def _centered_on_horizontal(seg: WireSeg, *, phase: str) -> Iterator[LabelCandidate]:
    x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
    for t in _SEGMENT_ANCHOR_FRACTIONS:
        yield LabelCandidate(x_lo + (x_hi - x_lo) * t, seg.y1, vertical=False, phase=phase)


def _centered_on_vertical(seg: WireSeg, *, phase: str) -> Iterator[LabelCandidate]:
    y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
    for t in _SEGMENT_ANCHOR_FRACTIONS:
        yield LabelCandidate(seg.x1, y_lo + (y_hi - y_lo) * t, vertical=True, phase=phase)


def _min_span_for_label(
    *,
    vertical: bool,
    tw: float,
    th: float,
) -> float:
    """Minimum wire length needed to center the label on a segment."""
    # Both orientations reserve the text width along the wire (the label is
    # centered on the segment); ``vertical``/``th`` are kept for call-site clarity.
    return tw + 4.0


def _sorted_net_segments(
    net_segs: list[WireSeg],
) -> tuple[list[WireSeg], list[WireSeg]]:
    horiz = [s for s in net_segs if s.orient == "H" and not _is_port_stub_segment(s)]
    vert = [s for s in net_segs if s.orient == "V"]
    horiz.sort(key=lambda s: s.length, reverse=True)
    vert.sort(key=lambda s: s.length, reverse=True)
    return horiz, vert


def iter_label_candidates(
    net_segs: list[WireSeg],
    *,
    tw: float,
    th: float,
) -> Iterator[LabelCandidate]:
    """Search order: long horizontal on-wire, long vertical on-wire, then fallbacks."""
    horiz, vert = _sorted_net_segments(net_segs)
    min_h = max(MIN_LABEL_HORIZONTAL, _min_span_for_label(vertical=False, tw=tw, th=th))
    min_v = max(
        LABEL_SHORT_VERTICAL_THRESHOLD,
        _min_span_for_label(vertical=True, tw=tw, th=th),
    )

    for seg in horiz:
        if seg.length >= min_h:
            yield from _centered_on_horizontal(seg, phase="horizontal_long")

    for seg in vert:
        if seg.length >= min_v:
            yield from _centered_on_vertical(seg, phase="vertical_long")

    for seg in horiz:
        if GUTTER_LABEL_MIN_H <= seg.length < min_h:
            yield from _centered_on_horizontal(seg, phase="horizontal_short")

    for seg in vert:
        if seg.length < min_v:
            yield from _centered_on_vertical(seg, phase="vertical_short")

    if horiz:
        yield from _centered_on_horizontal(horiz[0], phase="horizontal_last")
    if vert:
        yield from _centered_on_vertical(vert[0], phase="vertical_last")


def _clear_of_segments(
    x: float,
    y: float,
    *,
    vertical: bool,
    tw: float,
    th: float,
    segments: list[WireSeg],
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

    placed: list[tuple[float, float, bool]] = []
    directive_nodes = [n for n in (nodes or []) if n.role != "GND"]
    solid_by_id, _ = solid_wire_index_maps(wires)

    for net, net_wires in sorted(by_net.items()):
        label = next((w.label for w in net_wires if w.label), "")
        if not label:
            label = truncate_label(net)
        net_segs = [s for s in geo.segments if s.net == net]
        placed_carrier: TopologyWire | None = None
        for carrier in _label_carriers_in_priority_order(net_wires):
            carrier.label = label
            place_wire_label(
                carrier,
                wire_index=solid_by_id.get(id(carrier)),
                nodes=directive_nodes,
                avoid=avoid,
                placed=placed,
                segments=geo.segments,
                net_segments=net_segs,
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
                place_wire_label(
                    carrier,
                    wire_index=solid_by_id.get(id(carrier)),
                    nodes=directive_nodes,
                    avoid=avoid,
                    placed=placed,
                    segments=geo.segments,
                    net_segments=net_segs,
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
    segments: list[WireSeg] | None = None,
    net_segments: list[WireSeg] | None = None,
    bridges: list[BridgeCrossing] | None = None,
    bridge_clear: float = BRIDGE_R + LABEL_JUNCTION_CLEAR,
    relax_label_spacing: bool = False,
) -> None:
    """Center each net label on a long enough wire segment (horizontal or vertical)."""
    avoid = avoid or []
    placed = placed or []
    all_segments = segments or []
    net_segs = net_segments or [s for s in all_segments if s.net == wire.net]
    tw, th = label_text_size(wire.label)
    wire.label_text_anchor = "middle"
    wire.label_has_leader = False

    def _clear_of_points(x: float, y: float) -> bool:
        return all((x - px) ** 2 + (y - py) ** 2 > LABEL_JUNCTION_CLEAR**2 for px, py in avoid)

    def _clear_of_bridges(x: float, y: float, *, vertical: bool) -> bool:
        for bridge in bridges or []:
            if (x - bridge.x) ** 2 + (y - bridge.y) ** 2 >= bridge_clear**2:
                continue
            return False
        return True

    def _clear_of_nodes(x: float, y: float, vertical: bool) -> bool:
        half_w = (th / 2 if vertical else tw / 2) + 2
        half_h = (tw / 2 if vertical else th / 2) + 2
        for node in nodes:
            nx, ny, nw, nh = node.bounds
            if (
                nx - LABEL_NODE_MARGIN <= x + half_w
                and x - half_w <= nx + nw + LABEL_NODE_MARGIN
                and ny - LABEL_NODE_MARGIN <= y + half_h
                and y - half_h <= ny + nh + LABEL_NODE_MARGIN
            ):
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
        if not (
            _clear_of_points(x, y)
            and _clear_of_bridges(x, y, vertical=vertical)
            and _clear_of_nodes(x, y, vertical)
            and _clear_of_other_labels(x, y, vertical)
            and _clear_of_segments(
                x,
                y,
                vertical=vertical,
                tw=tw,
                th=th,
                segments=all_segments,
                skip_net=wire.net,
            )
        ):
            return False
        wire.label_x, wire.label_y = x, y
        wire.label_vertical = vertical
        wire.label_text_anchor = candidate.anchor
        wire.label_has_leader = False
        placed.append((x, y, vertical))
        return True

    for candidate in iter_label_candidates(net_segs, tw=tw, th=th):
        if _commit(candidate):
            return
