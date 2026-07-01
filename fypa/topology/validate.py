"""Topology model validation checks."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import (
    BRIDGE_R,
    GND_NET,
    JUNCTION_R,
    MAX_CANVAS_WIDTH,
    MAX_LABEL_DISTANCE,
    MIN_PARALLEL_GAP,
    NODE_W,
    WIRE_EPS,
)
from fypa.topology.geometry import (
    WireSeg,
    compute_schematic_geometry,
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
    port_stub_x,
)
from fypa.topology.types import TopologyModel, TopologyNode


# Node-crossing policy:
# - ``segment_through_foreign_node`` (error): per-wire segments; skips src/dst
#   and hub-net nodes so routed taps through their own column are allowed.
# - ``vertical_under_node`` (warning): post-geometry audit of every vertical;
#   no skip — flags layout debt even when the wire owns the node.


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    **extra,
) -> dict:
    return make_issue(code, message, severity=severity, **extra)


def _intervals_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return hi1 > lo2 + WIRE_EPS and lo1 < hi2 - WIRE_EPS


def vertical_segment_overlaps_node_body(
    node: TopologyNode,
    x: float,
    y_lo: float,
    y_hi: float,
) -> bool:
    """True when a vertical segment at ``x`` overlaps the node body on Y."""
    nx, ny, nw, nh = node.bounds
    if x < nx - WIRE_EPS or x > nx + nw + WIRE_EPS:
        return False
    return _intervals_overlap(y_lo, y_hi, ny, ny + nh)


def _segment_span(seg: WireSeg) -> tuple[float, float]:
    if seg.orient == "V":
        return min(seg.y1, seg.y2), max(seg.y1, seg.y2)
    return min(seg.x1, seg.x2), max(seg.x1, seg.x2)


def _check_segment_spacing(
    segments: list[WireSeg],
    junctions: list[tuple[float, float]],
    bridges: list,
) -> list[dict]:
    """Enforce unique vertical x / horizontal y (foreign nets) and junction/bridge gap."""
    issues: list[dict] = []
    verticals = [s for s in segments if s.orient == "V"]
    horizontals = [s for s in segments if s.orient == "H"]

    for i, a in enumerate(verticals):
        a_lo, a_hi = _segment_span(a)
        for b in verticals[i + 1:]:
            if abs(a.x1 - b.x1) >= WIRE_EPS:
                continue
            b_lo, b_hi = _segment_span(b)
            if not _intervals_overlap(a_lo, a_hi, b_lo, b_hi):
                continue
            if a.net != b.net:
                issues.append(_issue(
                    "duplicate_vertical_x",
                    (
                        f"Vertical segments at x={a.x1:.1f} overlap "
                        f"({a.net} wire {a.wire_index}, {b.net} wire {b.wire_index})"
                    ),
                    x=round(a.x1, 1),
                    net_a=a.net,
                    net_b=b.net,
                ))

    for i, a in enumerate(horizontals):
        a_lo, a_hi = _segment_span(a)
        for b in horizontals[i + 1:]:
            if abs(a.y1 - b.y1) >= WIRE_EPS:
                continue
            if a.net == b.net:
                continue
            b_lo, b_hi = _segment_span(b)
            if not _intervals_overlap(a_lo, a_hi, b_lo, b_hi):
                continue
            issues.append(_issue(
                "duplicate_horizontal_y",
                (
                    f"Horizontal segments at y={a.y1:.1f} overlap "
                    f"({a.net} and {b.net})"
                ),
                y=round(a.y1, 1),
                net_a=a.net,
                net_b=b.net,
            ))

    junction_set = {(round(x, 1), round(y, 1)) for x, y in junctions}
    clearance = BRIDGE_R + JUNCTION_R
    for bridge in bridges:
        bx, by = bridge.x, bridge.y
        for jx, jy in junction_set:
            if abs(bx - jx) < WIRE_EPS and abs(by - jy) <= clearance + WIRE_EPS:
                issues.append(_issue(
                    "junction_near_bridge",
                    (
                        f"Junction at ({jx:.1f},{jy:.1f}) overlaps bridge "
                        f"at ({bx:.1f},{by:.1f}) on vertical {bridge.vertical_net}"
                    ),
                    junction_x=jx,
                    junction_y=jy,
                    bridge_x=bx,
                    bridge_y=by,
                ))
    return issues


def _check_open_stub_ends(model: TopologyModel) -> list[dict]:
    """Every routed port stub end must join a vertical or continue on the same net."""
    issues: list[dict] = []
    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    junctions = {(round(x, 1), round(y, 1)) for x, y in geo.junctions}

    routed_ports: set[tuple[str, str, str]] = set()
    for wire in model.wires:
        if wire.dashed or not wire.net:
            continue
        if wire.src_node:
            routed_ports.add((wire.src_node, wire.src_terminal, wire.net))
        if wire.dst_node:
            routed_ports.add((wire.dst_node, wire.dst_terminal, wire.net))

    for node in model.nodes:
        for port in node.ports:
            if not port.net or port.net == "?":
                continue
            if (port.node_id, port.terminal, port.net) not in routed_ports:
                continue
            stub_x = port_stub_x(port)
            if abs(stub_x - port.x) < WIRE_EPS:
                continue
            y = round(port.y, 1)
            pt = (round(stub_x, 1), y)

            if pt in junctions:
                continue

            if port.net == GND_NET:
                has_vertical = any(
                    seg.net == GND_NET
                    and seg.orient == "V"
                    and abs(seg.x1 - stub_x) < WIRE_EPS
                    and min(seg.y1, seg.y2) - WIRE_EPS <= port.y
                    <= max(seg.y1, seg.y2) + WIRE_EPS
                    and (
                        max(seg.y1, seg.y2) - port.y > WIRE_EPS
                        or port.y - min(seg.y1, seg.y2) > WIRE_EPS
                    )
                    for seg in geo.segments
                )
                if not has_vertical:
                    issues.append(_issue(
                        "open_gnd_stub",
                        (
                            f"{node.node_id}.{port.terminal} GND stub at "
                            f"({pt[0]:.1f},{pt[1]:.1f}) has no vertical continuation"
                        ),
                        node_id=node.node_id,
                        terminal=port.terminal,
                        x=pt[0],
                        y=pt[1],
                    ))
                continue

            connected = False
            for wire in model.wires:
                if wire.dashed or wire.net != port.net:
                    continue
                points = parse_wire_path(wire.path_d)
                for i, (px, py) in enumerate(points):
                    if abs(px - stub_x) > WIRE_EPS or abs(py - port.y) > WIRE_EPS:
                        continue
                    if 0 < i < len(points) - 1:
                        connected = True
                        break
                    if i == len(points) - 2:
                        connected = True
                        break
                    if i == 0 and len(points) > 1:
                        nx, ny = points[1]
                        if abs(nx - stub_x) > WIRE_EPS or abs(ny - port.y) > WIRE_EPS:
                            connected = True
                            break
                    if i == len(points) - 1 and len(points) > 1:
                        px0, py0 = points[-2]
                        if abs(px0 - stub_x) > WIRE_EPS or abs(py0 - port.y) > WIRE_EPS:
                            connected = True
                            break
                if connected:
                    break

            if not connected:
                issues.append(_issue(
                    "open_signal_stub",
                    (
                        f"{node.node_id}.{port.terminal} ({port.net}) stub at "
                        f"({pt[0]:.1f},{pt[1]:.1f}) is not connected to routing"
                    ),
                    node_id=node.node_id,
                    terminal=port.terminal,
                    net=port.net,
                    x=pt[0],
                    y=pt[1],
                ))

    return issues


def _parallel_vertical_gap_issues(model: TopologyModel) -> list[dict]:
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
                issues.append(_issue(
                    "parallel_vertical_gap",
                    (
                        f"Vertical buses at x={unique[i - 1]:.1f} and "
                        f"x={unique[i]:.1f} are only {gap:.1f}px apart "
                        f"(min {MIN_PARALLEL_GAP:.1f}px)"
                    ),
                    x1=unique[i - 1],
                    x2=unique[i],
                    gap=round(gap, 1),
                ))
    return issues


def validate_topology(model: TopologyModel) -> list[dict]:
    """Run model-level topology validation checks."""
    issues: list[dict] = []
    directive_nodes = [n for n in model.nodes if n.role != "GND"]

    for wi, wire in enumerate(model.wires):
        if wire.dashed:
            continue
        skip_nodes = {wire.src_node, wire.dst_node} - {""}
        if wire.routing_kind in ("hub", "hub_tap", "hub_row"):
            skip_nodes |= {
                p.node_id
                for n in model.nodes
                for p in n.ports
                if p.net == wire.net
            }
        points = parse_wire_path(wire.path_d)
        for seg in path_to_segments(wire.net, points):
            for node in directive_nodes:
                if node.node_id in skip_nodes:
                    continue
                if seg.orient == "H":
                    y = seg.y1
                    x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
                    if horizontal_crosses_node(node, y, x_lo, x_hi):
                        issues.append(_issue(
                            "segment_through_foreign_node",
                            (
                                f"Wire {wi} ({wire.net}) horizontal segment at "
                                f"y={y:.1f} crosses node {node.designator}"
                            ),
                            wire_id=wi,
                            net=wire.net,
                            node_id=node.node_id,
                            y=round(y, 1),
                        ))
                else:
                    x = seg.x1
                    y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
                    if vertical_crosses_node(node, x, y_lo, y_hi):
                        issues.append(_issue(
                            "segment_through_foreign_node",
                            (
                                f"Wire {wi} ({wire.net}) vertical segment at "
                                f"x={x:.1f} crosses node {node.designator}"
                            ),
                            wire_id=wi,
                            net=wire.net,
                            node_id=node.node_id,
                            x=round(x, 1),
                        ))

    issues.extend(_parallel_vertical_gap_issues(model))

    signal_bus_xs = sorted({
        round(w.bus_x, 1)
        for w in model.wires
        if not w.dashed and w.net != GND_NET and w.bus_x is not None
    })
    gnd_drop_xs = sorted({
        round(s.x1, 1)
        for w in model.wires
        if w.routing_kind in ("gnd_drop", "gnd_trunk", "gnd_tap")
        for s in path_to_segments(w.net, parse_wire_path(w.path_d))
        if s.orient == "V"
    })
    if signal_bus_xs and gnd_drop_xs:
        for sx in signal_bus_xs:
            for gx in gnd_drop_xs:
                gap = abs(sx - gx)
                if WIRE_EPS < gap < MIN_PARALLEL_GAP - WIRE_EPS:
                    issues.append(_issue(
                        "signal_vs_gnd_drop_gap",
                        (
                            f"Signal vertical x={sx:.1f} is only {gap:.1f}px "
                            f"from GND drop x={gx:.1f}"
                        ),
                        signal_x=round(sx, 1),
                        gnd_x=round(gx, 1),
                        gap=round(gap, 1),
                    ))

    for wi, wire in enumerate(model.wires):
        if not wire.label or wire.dashed:
            continue
        if wire.label_x == 0.0 and wire.label_y == 0.0:
            issues.append(_issue(
                "label_not_at_origin",
                f"Wire {wi} ({wire.net}) label '{wire.label}' at (0,0)",
                wire_id=wi,
                net=wire.net,
            ))
            continue
        points = parse_wire_path(wire.path_d)
        if not points:
            continue
        best_d = float("inf")
        for (px1, py1), (px2, py2) in zip(points, points[1:]):
            if abs(py1 - py2) < WIRE_EPS and abs(px1 - px2) < WIRE_EPS:
                continue
            if abs(px1 - px2) < WIRE_EPS:
                lo, hi = min(py1, py2), max(py1, py2)
                if lo - WIRE_EPS <= wire.label_y <= hi + WIRE_EPS:
                    d = abs(wire.label_x - px1)
                    best_d = min(best_d, d)
            elif abs(py1 - py2) < WIRE_EPS:
                lo, hi = min(px1, px2), max(px1, px2)
                if lo - WIRE_EPS <= wire.label_x <= hi + WIRE_EPS:
                    d = abs(wire.label_y - py1)
                    best_d = min(best_d, d)
        if best_d > MAX_LABEL_DISTANCE + WIRE_EPS:
            issues.append(_issue(
                "label_anchor_distance",
                (
                    f"Wire {wi} ({wire.net}) label is {best_d:.1f}px "
                    f"from the nearest wire segment"
                ),
                wire_id=wi,
                net=wire.net,
                distance=round(best_d, 1),
            ))

    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    issues.extend(_check_segment_spacing(geo.segments, geo.junctions, geo.bridges))
    issues.extend(_check_open_stub_ends(model))

    for seg in geo.verticals:
        if seg.orient != "V":
            continue
        x = seg.x1
        y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
        for node in directive_nodes:
            if vertical_segment_overlaps_node_body(node, x, y_lo, y_hi):
                issues.append(_issue(
                    "vertical_under_node",
                    (
                        f"Vertical segment at x={x:.1f} ({seg.net}) "
                        f"runs under node {node.designator}"
                    ),
                    severity="warning",
                    net=seg.net,
                    node_id=node.node_id,
                    x=round(x, 1),
                ))

    if model.width > MAX_CANVAS_WIDTH + WIRE_EPS:
        issues.append(_issue(
            "canvas_width_reasonable",
            (
                f"Canvas width {model.width:.1f}px exceeds "
                f"maximum {MAX_CANVAS_WIDTH:.1f}px"
            ),
            width=round(model.width, 1),
            max_width=MAX_CANVAS_WIDTH,
        ))

    return issues


def merge_validation_issues(
    model: TopologyModel,
    wire_issues: list[dict],
) -> list[dict]:
    """Combine per-wire heuristic issues with model-level validation."""
    return list(wire_issues) + validate_topology(model)
