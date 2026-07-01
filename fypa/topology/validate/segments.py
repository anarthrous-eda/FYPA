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
    group_ports_by_net,
    gutter_groups,
    net_gutter_key,
)
from fypa.topology.types import TopologyModel, TopologyNode
from fypa.topology.validate.util import (
    intervals_overlap,
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
            if abs(a.x1 - b.x1) >= WIRE_EPS:
                continue
            b_lo, b_hi = segment_span(b)
            if not intervals_overlap(a_lo, a_hi, b_lo, b_hi):
                continue
            if a.net != b.net:
                issues.append(
                    make_issue(
                        "duplicate_vertical_x",
                        (
                            f"Vertical segments at x={a.x1:.1f} overlap "
                            f"({a.net} wire {a.wire_index}, {b.net} wire {b.wire_index})"
                        ),
                        x=round(a.x1, 1),
                        net_a=a.net,
                        net_b=b.net,
                    )
                )

    for i, a in enumerate(horizontals):
        a_lo, a_hi = segment_span(a)
        for b in horizontals[i + 1 :]:
            if abs(a.y1 - b.y1) >= WIRE_EPS:
                continue
            if a.net == b.net:
                continue
            b_lo, b_hi = segment_span(b)
            if not intervals_overlap(a_lo, a_hi, b_lo, b_hi):
                continue
            issues.append(
                make_issue(
                    "duplicate_horizontal_y",
                    (f"Horizontal segments at y={a.y1:.1f} overlap ({a.net} and {b.net})"),
                    y=round(a.y1, 1),
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
        if wire.routing_kind in ("hub", "hub_tap", "hub_row"):
            skip_nodes |= {p.node_id for n in model.nodes for p in n.ports if p.net == wire.net}
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
    by_net = group_ports_by_net(all_ports)
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
    for net, ports in by_net.items():
        if net == GND_NET or len(ports) < 2:
            continue
        gkey = net_gutter_key(ports)
        if gkey is not None and net in bus_x_by_net:
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
