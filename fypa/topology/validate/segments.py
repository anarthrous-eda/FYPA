"""Segment spacing, bus-gap, and node-crossing validation."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import (
    BRIDGE_R,
    GND_NET,
    JUNCTION_R,
    MIN_PARALLEL_GAP,
    WIRE_EPS,
)
from fypa.topology.geometry import (
    SchematicGeometry,
    WireSeg,
    horizontal_crosses_node,
    parse_wire_path,
    path_to_segments,
    vertical_crosses_node,
)
from fypa.topology.issues import make_issue
from fypa.topology.placement import (
    bus_x_in_column_gaps,
    column_gaps_from_nodes,
    gutter_groups,
    port_stub_x,
)
from fypa.topology.types import TopologyModel, TopologyNode, TopologyWire
from fypa.topology.validate.util import (
    foreign_segments_cross,
    intervals_overlap,
    parallel_corridors_too_close,
    segment_span,
    vertical_segment_overlaps_node_body,
)


def check_segment_spacing(
    segments: list[WireSeg],
    junctions: list[tuple[float, float]],
    bridges: list,
) -> list[dict]:
    """Enforce unique vertical x / horizontal y (foreign nets) and junction/bridge gap."""
    issues: list[dict] = []
    verticals = [s for s in segments if s.orient == "V"]
    horizontals = [s for s in segments if s.orient == "H"]

    for i, a in enumerate(verticals):
        a_lo, a_hi = segment_span(a)
        for b in verticals[i + 1 :]:
            if not parallel_corridors_too_close(a.x1, b.x1):
                continue
            b_lo, b_hi = segment_span(b)
            if not intervals_overlap(a_lo, a_hi, b_lo, b_hi):
                continue
            if a.net != b.net:
                if GND_NET in (a.net, b.net):
                    continue
                gap = abs(a.x1 - b.x1)
                issues.append(
                    make_issue(
                        "duplicate_vertical_x",
                        (
                            f"Vertical segments at x={a.x1:.1f} and x={b.x1:.1f} "
                            f"are only {gap:.1f}px apart "
                            f"({a.net} wire {a.wire_index}, {b.net} wire {b.wire_index})"
                        ),
                        x=round(a.x1, 1),
                        x2=round(b.x1, 1),
                        gap=round(gap, 1),
                        net_a=a.net,
                        net_b=b.net,
                    )
                )

    for i, a in enumerate(horizontals):
        a_lo, a_hi = segment_span(a)
        for b in horizontals[i + 1 :]:
            if a.net == b.net:
                continue
            if not parallel_corridors_too_close(a.y1, b.y1):
                continue
            b_lo, b_hi = segment_span(b)
            if not intervals_overlap(a_lo, a_hi, b_lo, b_hi):
                continue
            gap = abs(a.y1 - b.y1)
            issues.append(
                make_issue(
                    "duplicate_horizontal_y",
                    (
                        f"Horizontal segments at y={a.y1:.1f} and y={b.y1:.1f} "
                        f"are only {gap:.1f}px apart ({a.net} and {b.net})"
                    ),
                    y=round(a.y1, 1),
                    y2=round(b.y1, 1),
                    gap=round(gap, 1),
                    net_a=a.net,
                    net_b=b.net,
                )
            )

    junction_set = {(round(x, 1), round(y, 1)) for x, y in junctions}
    clearance = BRIDGE_R + JUNCTION_R
    for bridge in bridges:
        bx, by = bridge.x, bridge.y
        for jx, jy in junction_set:
            if abs(bx - jx) < WIRE_EPS and abs(by - jy) <= clearance + WIRE_EPS:
                issues.append(
                    make_issue(
                        "junction_near_bridge",
                        (
                            f"Junction at ({jx:.1f},{jy:.1f}) overlaps bridge "
                            f"at ({bx:.1f},{by:.1f}) on vertical {bridge.vertical_net}"
                        ),
                        junction_x=jx,
                        junction_y=jy,
                        bridge_x=bx,
                        bridge_y=by,
                    )
                )
    return issues


def check_wires_through_foreign_nodes(model: TopologyModel) -> list[dict]:
    """Per-wire segments must not cross foreign directive node bodies."""
    issues: list[dict] = []
    directive_nodes = [n for n in model.nodes if n.role != "GND"]

    for wi, wire in enumerate(model.wires):
        if wire.dashed:
            continue
        skip_nodes = {wire.src_node, wire.dst_node} - {""}
        points = parse_wire_path(wire.path_d)
        for seg in path_to_segments(wire.net, points):
            for node in directive_nodes:
                if node.node_id in skip_nodes:
                    continue
                if seg.orient == "H":
                    y = seg.y1
                    x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
                    if horizontal_crosses_node(node, y, x_lo, x_hi):
                        issues.append(
                            make_issue(
                                "segment_through_foreign_node",
                                (
                                    f"Wire {wi} ({wire.net}) horizontal segment at "
                                    f"y={y:.1f} crosses node {node.designator}"
                                ),
                                wire_id=wi,
                                net=wire.net,
                                node_id=node.node_id,
                                y=round(y, 1),
                            )
                        )
                else:
                    x = seg.x1
                    y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
                    if vertical_crosses_node(node, x, y_lo, y_hi):
                        issues.append(
                            make_issue(
                                "segment_through_foreign_node",
                                (
                                    f"Wire {wi} ({wire.net}) vertical segment at "
                                    f"x={x:.1f} crosses node {node.designator}"
                                ),
                                wire_id=wi,
                                net=wire.net,
                                node_id=node.node_id,
                                x=round(x, 1),
                            )
                        )
    return issues


def check_parallel_vertical_gap(model: TopologyModel) -> list[dict]:
    """Flag foreign vertical buses closer than MIN_PARALLEL_GAP within one gutter."""
    all_ports = [p for n in model.nodes for p in n.ports]
    bus_x_by_net: dict[str, float] = {}
    for w in model.wires:
        if w.dashed or w.bus_x is None:
            continue
        bus_x_by_net[w.net] = round(w.bus_x, 1)

    gutter_bus_xs: dict[tuple, list[float]] = defaultdict(list)
    for gkey, nets_in_gutter in gutter_groups(all_ports).items():
        for net in nets_in_gutter:
            if net in bus_x_by_net:
                gutter_bus_xs[gkey].append(bus_x_by_net[net])

    issues: list[dict] = []
    for xs in gutter_bus_xs.values():
        unique = sorted(set(xs))
        for i in range(1, len(unique)):
            gap = unique[i] - unique[i - 1]
            if gap < MIN_PARALLEL_GAP - WIRE_EPS:
                issues.append(
                    make_issue(
                        "parallel_vertical_gap",
                        (
                            f"Vertical buses at x={unique[i - 1]:.1f} and "
                            f"x={unique[i]:.1f} are only {gap:.1f}px apart "
                            f"(min {MIN_PARALLEL_GAP:.1f}px)"
                        ),
                        x1=unique[i - 1],
                        x2=unique[i],
                        gap=round(gap, 1),
                    )
                )
    return issues


def check_signal_vs_gnd_drop_gap(model: TopologyModel) -> list[dict]:
    """Signal vertical buses must not sit too close to GND column drops."""
    signal_spans: dict[float, tuple[float, float]] = {}
    for w in model.wires:
        if w.dashed or w.net == GND_NET or w.bus_x is None:
            continue
        bx = round(w.bus_x, 1)
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient != "V" or abs(seg.x1 - bx) > WIRE_EPS:
                continue
            lo, hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
            if bx in signal_spans:
                slo, shi = signal_spans[bx]
                signal_spans[bx] = (min(slo, lo), max(shi, hi))
            else:
                signal_spans[bx] = (lo, hi)

    gnd_spans: dict[float, tuple[float, float]] = {}
    for w in model.wires:
        if w.routing_kind not in ("gnd_drop", "gnd_trunk", "gnd_tap"):
            continue
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient != "V":
                continue
            gx = round(seg.x1, 1)
            lo, hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
            if gx in gnd_spans:
                glo, ghi = gnd_spans[gx]
                gnd_spans[gx] = (min(glo, lo), max(ghi, hi))
            else:
                gnd_spans[gx] = (lo, hi)

    issues: list[dict] = []
    for sx, (sy_lo, sy_hi) in signal_spans.items():
        for gx, (gy_lo, gy_hi) in gnd_spans.items():
            if sy_hi <= gy_lo + WIRE_EPS or sy_lo >= gy_hi - WIRE_EPS:
                continue
            gap = abs(sx - gx)
            if WIRE_EPS < gap < MIN_PARALLEL_GAP - WIRE_EPS:
                issues.append(
                    make_issue(
                        "signal_vs_gnd_drop_gap",
                        (
                            f"Signal vertical x={sx:.1f} is only {gap:.1f}px "
                            f"from GND drop x={gx:.1f}"
                        ),
                        signal_x=sx,
                        gnd_x=gx,
                        gap=round(gap, 1),
                    )
                )
    return issues


def _signal_net_port_columns(
    model: TopologyModel,
    net: str,
) -> tuple[set[float], set[float]]:
    """Return ``(port_x, stub_x)`` sets for every port on ``net``."""
    port_xs: set[float] = set()
    stub_xs: set[float] = set()
    for node in model.nodes:
        for port in node.ports:
            if port.net != net:
                continue
            port_xs.add(round(port.x, 1))
            stub_xs.add(round(port_stub_x(port), 1))
    return port_xs, stub_xs


def _allowed_signal_vertical_x(
    x: float,
    net: str,
    wire: TopologyWire,
    column_gaps: list[tuple[float, float]],
    port_xs: set[float],
    stub_xs: set[float],
    model: TopologyModel,
) -> bool:
    """Column-gap bus columns, plus short port stub / source-port drops."""
    if bus_x_in_column_gaps(x, column_gaps):
        return True
    rx = round(x, 1)
    if rx in stub_xs:
        return True
    if not wire.src_node or not wire.src_terminal:
        return False
    for node in model.nodes:
        if node.node_id != wire.src_node:
            continue
        for port in node.ports:
            if port.terminal != wire.src_terminal or port.net != net:
                continue
            if abs(rx - round(port.x, 1)) <= WIRE_EPS:
                return True
    return False


def check_vertical_bus_column_gaps(model: TopologyModel) -> list[dict]:
    """Signal vertical segments must sit in layout column gaps, not on symbol columns."""
    column_gaps = column_gaps_from_nodes(model.nodes)
    if not column_gaps:
        return []
    issues: list[dict] = []
    port_columns: dict[str, tuple[set[float], set[float]]] = {}
    for wi, wire in enumerate(model.wires):
        if wire.dashed or wire.net == GND_NET:
            continue
        if wire.net not in port_columns:
            port_columns[wire.net] = _signal_net_port_columns(model, wire.net)
        port_xs, stub_xs = port_columns[wire.net]
        for seg in path_to_segments(wire.net, parse_wire_path(wire.path_d)):
            if seg.orient != "V":
                continue
            x = seg.x1
            if _allowed_signal_vertical_x(
                x,
                wire.net,
                wire,
                column_gaps,
                port_xs,
                stub_xs,
                model,
            ):
                continue
            issues.append(
                make_issue(
                    "vertical_bus_outside_column_gap",
                    (f"Wire {wi} ({wire.net}) vertical at x={x:.1f} is not in a column gap"),
                    wire_id=wi,
                    net=wire.net,
                    x=round(x, 1),
                )
            )
    return issues


def _gutter_crossing_segments(wire: TopologyWire) -> list[WireSeg]:
    """Segments that participate in gutter bus geometry (trunk verticals + horizontals)."""
    segs = path_to_segments(wire.net, parse_wire_path(wire.path_d))
    if wire.bus_x is None:
        return segs
    bus_x = wire.bus_x
    return [
        seg
        for seg in segs
        if seg.orient == "H" or (seg.orient == "V" and abs(seg.x1 - bus_x) < WIRE_EPS)
    ]


def check_gutter_wire_crossings(model: TopologyModel) -> list[dict]:
    """Flag foreign H/V crossings between nets sharing a column gutter."""
    from collections import defaultdict

    all_ports = [p for n in model.nodes for p in n.ports]
    wires_by_net: dict[str, list[TopologyWire]] = defaultdict(list)
    for wire in model.wires:
        if wire.dashed or not wire.net:
            continue
        wires_by_net[wire.net].append(wire)
    hub_nets = {
        w.net for w in model.wires if w.net and not w.dashed and w.routing_kind.startswith("hub")
    }
    issues: list[dict] = []
    for _gkey, nets in gutter_groups(all_ports).items():
        active = sorted(net for net in nets if net in wires_by_net)
        if len(active) < 2:
            continue
        segs_by_net = {
            net: [seg for wire in wires_by_net[net] for seg in _gutter_crossing_segments(wire)]
            for net in active
        }
        for i, net_a in enumerate(active):
            for net_b in active[i + 1 :]:
                if net_a in hub_nets and net_b in hub_nets:
                    continue
                if foreign_segments_cross(segs_by_net[net_a], segs_by_net[net_b]):
                    issues.append(
                        make_issue(
                            "foreign_wire_crossing",
                            f"{net_a} and {net_b} cross in a shared gutter",
                            net_a=net_a,
                            net_b=net_b,
                        )
                    )
    return issues


def check_vertical_under_node(
    model: TopologyModel,
    geo: SchematicGeometry,
    *,
    directive_nodes: list[TopologyNode] | None = None,
) -> list[dict]:
    """Warn when a vertical segment runs under a directive node body."""
    nodes = directive_nodes
    if nodes is None:
        nodes = [n for n in model.nodes if n.role != "GND"]

    issues: list[dict] = []
    for seg in geo.verticals:
        if seg.orient != "V":
            continue
        x = seg.x1
        y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
        for node in nodes:
            if vertical_segment_overlaps_node_body(node, x, y_lo, y_hi):
                issues.append(
                    make_issue(
                        "vertical_under_node",
                        (
                            f"Vertical segment at x={x:.1f} ({seg.net}) "
                            f"runs under node {node.designator}"
                        ),
                        severity="warning",
                        net=seg.net,
                        node_id=node.node_id,
                        x=round(x, 1),
                    )
                )
    return issues
