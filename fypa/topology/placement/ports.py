"""Port stubs, column keys, and gutter routing keys."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import (
    COLUMN_BUS_PAD,
    GND_NET,
    MIN_PARALLEL_GAP,
    NODE_W,
    PORT_WIRE_STUB,
    WIRE_EPS,
    WIRE_STAGGER,
)
from fypa.topology.placement.types import (
    GutterSpanKey,
    WireRoutingKey,
)
from fypa.topology.types import TopologyPort


def port_stub_length(port: TopologyPort) -> float:
    """Horizontal stub length for this port (stacked edges use staggered lengths)."""
    if port.wire_x is not None:
        return abs(port.x - port.wire_x)
    if port.stub_length > WIRE_EPS:
        return port.stub_length
    return PORT_WIRE_STUB


def port_stub_x(port: TopologyPort) -> float:
    """Wire column x outward from the node edge (routing attach point)."""
    if port.wire_x is not None:
        return port.wire_x
    length = port_stub_length(port)
    if port.side == "right":
        return port.x + length
    return port.x - length


def port_column_x(port: TopologyPort) -> float:
    """Left edge x of the node column this port belongs to."""
    return port.x if port.side == "left" else port.x - NODE_W


def ports_share_column(a: TopologyPort, b: TopologyPort) -> bool:
    if a.node_id == b.node_id:
        return False
    return abs(port_column_x(a) - port_column_x(b)) < WIRE_EPS


def ports_all_share_column(ports: list[TopologyPort]) -> bool:
    if len(ports) < 2:
        return False
    col = round(port_column_x(ports[0]), 1)
    return all(abs(port_column_x(p) - col) < WIRE_EPS for p in ports[1:])


def wire_routing_key(a: TopologyPort, b: TopologyPort) -> WireRoutingKey:
    if ports_share_column(a, b):
        return ("stack", *sorted((a.node_id, b.node_id)))
    x_lo = min(a.x, b.x)
    x_hi = max(a.x, b.x)
    return ("gap", round(x_lo, 1), round(x_hi, 1))


def net_gutter_key(ports: list[TopologyPort]) -> GutterSpanKey | None:
    """Gutter span key for a signal net (``(x_lo, x_hi)`` rounded)."""
    if len(ports) < 2:
        return None
    if len(ports) == 2:
        key = wire_routing_key(ports[0], ports[1])
        if key[0] == "gap":
            return key[1:]
        return None
    if ports_all_share_column(ports):
        return None
    xs = [p.x for p in ports]
    return (round(min(xs), 1), round(max(xs), 1))


def gutter_groups(all_ports: list[TopologyPort]) -> dict[GutterSpanKey, set[str]]:
    """Map each column-gap gutter span ``(x_lo, x_hi)`` to the nets sharing it."""
    by_net: dict[str, list[TopologyPort]] = defaultdict(list)
    for p in all_ports:
        if p.net:
            by_net[p.net].append(p)
    gutter_nets: dict[GutterSpanKey, set[str]] = defaultdict(set)
    for net, group in by_net.items():
        if net == GND_NET or len(group) < 2:
            continue
        gkey = net_gutter_key(group)
        if gkey is not None:
            gutter_nets[gkey].add(net)
    return gutter_nets


def group_ports_by_net(ports: list[TopologyPort]) -> dict[str, list[TopologyPort]]:
    by_net: dict[str, list[TopologyPort]] = defaultdict(list)
    for p in ports:
        if p.net:
            by_net[p.net].append(p)
    return by_net


def stacked_routing_order(
    a: TopologyPort,
    b: TopologyPort,
) -> tuple[TopologyPort, TopologyPort]:
    if a.side == "right" and b.side == "left":
        return a, b
    if a.side == "left" and b.side == "right":
        return b, a
    if a.y <= b.y:
        return a, b
    return b, a


def stacked_wire_bus_side(ports: list[TopologyPort]) -> str:
    if any(p.side == "right" for p in ports):
        return "right"
    return "left"


def column_bus_x(
    col_x: float,
    side: str,
    *,
    lane: int,
    n_lanes: int,
) -> float:
    stagger = (lane - (n_lanes - 1) / 2) * max(WIRE_STAGGER, MIN_PARALLEL_GAP)
    if side == "right":
        return col_x + NODE_W + PORT_WIRE_STUB + COLUMN_BUS_PAD + stagger
    return col_x - PORT_WIRE_STUB - COLUMN_BUS_PAD - stagger


def gutter_bus_slot_for_source_y(
    slot: int,
    n_slots: int,
    *,
    approach_side: str,
) -> int:
    """Map y-sorted gutter pairs to bus columns without H/V crossings.

    Pairs are processed in ascending approach-port y (top first). Right-side
    approaches need the topmost wire on the outermost bus so its horizontal leg
    does not sweep across a lower wire's vertical corridor.
    """
    if approach_side == "right":
        return n_slots - 1 - slot
    return slot


def gutter_bus_x_bounds(stubs: list[float]) -> tuple[float, float]:
    lo, hi = min(stubs), max(stubs)
    inner_lo = lo + MIN_PARALLEL_GAP
    inner_hi = hi - MIN_PARALLEL_GAP
    if inner_hi - inner_lo < MIN_PARALLEL_GAP - WIRE_EPS:
        mid = (lo + hi) / 2
        return mid - MIN_PARALLEL_GAP / 2, mid + MIN_PARALLEL_GAP / 2
    return inner_lo, inner_hi


def bus_outward(bus_x: float, x_lo: float, x_hi: float) -> float:
    mid = (x_lo + x_hi) / 2
    return 1.0 if bus_x >= mid else -1.0
