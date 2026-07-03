"""Shared helpers for hub routing regression tests."""

from __future__ import annotations

from fypa.topology import build_topology_model, parse_wire_path, path_to_segments
from fypa.topology.constants import WIRE_EPS
from fypa.topology.geometry import (
    compute_schematic_geometry,
    horizontal_crosses_node,
)
from fypa.topology.types import TopologyModel, TopologyNode, TopologyWire
from tests.topology_fixtures import load_topology_fixture

# Committed layout fixtures (exported from full-board metadata).
FIXTURE_ROW_DETOUR = "hub_gutter_row_detour"
FIXTURE_ESCAPE_BRANCH = "hub_escape_vertical_branch"

HUB_FIXTURES = (FIXTURE_ROW_DETOUR, FIXTURE_ESCAPE_BRANCH)


def build_hub_fixture(name: str) -> TopologyModel:
    return build_topology_model(load_topology_fixture(name))


def hub_row_wires(model: TopologyModel, net: str) -> list[TopologyWire]:
    return [w for w in model.wires if w.net == net and w.routing_kind == "hub_row"]


def hub_bus_column(model: TopologyModel, net: str) -> float:
    xs = {
        w.bus_x
        for w in model.wires
        if w.net == net and w.bus_x is not None
    }
    assert len(xs) == 1, f"expected one bus column for {net}, got {xs}"
    return next(iter(xs))


def regulator_on_hub_row(model: TopologyModel, row_wire: TopologyWire) -> TopologyNode:
    row_y = parse_wire_path(row_wire.path_d)[0][1]
    for node in model.nodes:
        if node.role != "REGULATOR":
            continue
        for port in node.ports:
            if port.net == row_wire.net and abs(port.y - row_y) < WIRE_EPS:
                return node
    raise AssertionError(f"no regulator on hub row {row_wire.path_d!r}")


def upstream_escape_tap(model: TopologyModel, net: str) -> TopologyWire:
    """Tap that drops from a high port onto a lower hub row via an escape column."""
    row_ys = {parse_wire_path(w.path_d)[0][1] for w in hub_row_wires(model, net)}
    candidates: list[TopologyWire] = []
    for wire in model.wires:
        if wire.net != net or wire.routing_kind != "hub_tap":
            continue
        pts = parse_wire_path(wire.path_d)
        if len(pts) < 2 or " V " not in wire.path_d:
            continue
        if pts[0][1] + WIRE_EPS < min(row_ys):
            candidates.append(wire)
    assert len(candidates) == 1, [w.path_d for w in candidates]
    return candidates[0]


def escape_vertical_x(escape_tap: TopologyWire) -> float:
    return parse_wire_path(escape_tap.path_d)[-1][0]


def eastward_singleton_tap(model: TopologyModel, net: str) -> TopologyWire:
    """Downstream singleton fed horizontally from the upstream escape column."""
    col_x = escape_vertical_x(upstream_escape_tap(model, net))
    taps = [
        w
        for w in model.wires
        if w.net == net
        and w.routing_kind == "hub_tap"
        and " V " not in w.path_d
        and abs(parse_wire_path(w.path_d)[0][0] - col_x) < WIRE_EPS
    ]
    assert len(taps) == 1, [w.path_d for w in taps]
    return taps[0]


def detoured_row_feed(model: TopologyModel, net: str) -> TopologyWire:
    """Row-to-bus feed that steps off the row before running toward the trunk."""
    bus_x = hub_bus_column(model, net)
    feeds = []
    for wire in model.wires:
        if wire.net != net or wire.routing_kind != "hub_tap":
            continue
        pts = parse_wire_path(wire.path_d)
        if len(pts) < 3:
            continue
        if abs(pts[-1][0] - bus_x) > WIRE_EPS:
            continue
        if abs(pts[0][1] - pts[1][1]) < WIRE_EPS:
            continue
        if abs(pts[1][0] - pts[0][0]) < WIRE_EPS and abs(pts[1][1] - pts[0][1]) > WIRE_EPS:
            feeds.append(wire)
    assert len(feeds) == 1, [w.path_d for w in feeds]
    return feeds[0]


def horizontal_segments_crossing_node(
    model: TopologyModel,
    net: str,
    node: TopologyNode,
) -> list[tuple[TopologyWire, float]]:
    hits: list[tuple[TopologyWire, float]] = []
    for wire in model.wires:
        if wire.net != net:
            continue
        for seg in path_to_segments(net, parse_wire_path(wire.path_d)):
            if seg.orient != "H":
                continue
            y = seg.y1
            x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
            if horizontal_crosses_node(node, y, x_lo, x_hi):
                hits.append((wire, y))
    return hits


def all_net_ports_connected(model: TopologyModel, net: str) -> bool:
    """True when every port on *net* lies in one wire-graph component."""
    from fypa.topology.validate.hub import hub_net_ports_connected

    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    return hub_net_ports_connected(model, geo, net)
