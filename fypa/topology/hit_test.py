"""Hit testing for topology diagram interaction."""

from __future__ import annotations

import math

from fypa.topology.constants import GND_NET, PORT_R, WIRE_EPS, WIRE_HIT_RADIUS
from fypa.topology.geometry import parse_wire_path, path_to_segments
from fypa.topology.types import TopologyModel, TopologyNode, TopologyPort, TopologyWire


def find_component_at(
    model: TopologyModel,
    x: float,
    y: float,
) -> TopologyNode | None:
    """Return the node whose hit bounds contain ``(x, y)``."""
    for node in reversed(model.nodes):
        if node.node_id == GND_NET:
            continue
        bx, by, bw, bh = node.bounds
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return node
    return None


def find_port_at(
    model: TopologyModel,
    x: float,
    y: float,
    *,
    radius: float | None = None,
) -> TopologyPort | None:
    """Return the port whose pin circle contains ``(x, y)``."""
    r = radius if radius is not None else PORT_R + WIRE_HIT_RADIUS
    r2 = r * r
    for node in reversed(model.nodes):
        for port in reversed(node.ports):
            dx = port.x - x
            dy = port.y - y
            if dx * dx + dy * dy <= r2:
                return port
    return None


def _dist_to_wire_segment(
    x: float,
    y: float,
    *,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    if abs(y1 - y2) < WIRE_EPS:
        lo, hi = min(x1, x2), max(x1, x2)
        if x < lo or x > hi:
            return math.hypot(x - max(lo, min(x, hi)), y - y1)
        return abs(y - y1)
    lo, hi = min(y1, y2), max(y1, y2)
    if y < lo or y > hi:
        return math.hypot(y - max(lo, min(y, hi)), x - x1)
    return abs(x - x1)


def find_wire_at(
    model: TopologyModel,
    x: float,
    y: float,
    *,
    radius: float | None = None,
) -> TopologyWire | None:
    """Return the wire nearest ``(x, y)`` within ``radius`` px."""
    hit = radius if radius is not None else WIRE_HIT_RADIUS
    best: TopologyWire | None = None
    best_d = hit
    for wire in model.wires:
        points = parse_wire_path(wire.path_d)
        for (px1, py1), (px2, py2) in zip(points, points[1:]):
            d = _dist_to_wire_segment(x, y, x1=px1, y1=py1, x2=px2, y2=py2)
            if d < best_d:
                best_d = d
                best = wire
    return best


def _wire_tooltip(wire: TopologyWire) -> str:
    if wire.net == GND_NET:
        return "GND"
    if wire.label:
        return wire.label
    return wire.net


def topology_net_at(
    model: TopologyModel,
    x: float,
    y: float,
) -> str | None:
    """Net to highlight: port or wire hit; ``None`` on symbol body or empty space."""
    port = find_port_at(model, x, y)
    if port is not None and port.net:
        return port.net
    if find_component_at(model, x, y) is not None:
        return None
    wire = find_wire_at(model, x, y)
    if wire is None:
        return None
    return wire.net


def topology_tooltip_at(
    model: TopologyModel,
    x: float,
    y: float,
) -> str | None:
    """Tooltip text for hover at diagram coordinates (ports take priority)."""
    port = find_port_at(model, x, y)
    if port is not None and port.tooltip:
        return port.tooltip
    node = find_component_at(model, x, y)
    if node is not None and node.tooltip:
        return node.tooltip
    wire = find_wire_at(model, x, y)
    if wire is not None:
        return _wire_tooltip(wire)
    return None
