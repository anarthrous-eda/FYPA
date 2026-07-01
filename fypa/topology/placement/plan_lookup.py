"""Look up planned bus x values for parity checks against routed wires."""

from __future__ import annotations

from fypa.topology.constants import GND_NET, WIRE_EPS
from fypa.topology.geometry import parse_wire_path
from fypa.topology.placement.plan_types import BusPlan
from fypa.topology.placement.ports import port_column_x, stacked_wire_bus_side
from fypa.topology.placement.types import StackBusKey
from fypa.topology.types import TopologyPort, TopologyWire


def planned_signal_bus_x(
    wire: TopologyWire,
    plan: BusPlan,
    ports: list[TopologyPort],
) -> float | None:
    """Return the bus plan entry that routing should use for ``wire``, if any."""
    if wire.dashed or wire.bus_x is None or wire.net == GND_NET:
        return None
    net = wire.net
    if net in plan.pair_buses:
        return plan.pair_buses[net]
    if net in plan.hub_buses:
        return plan.hub_buses[net]
    net_ports = [p for p in ports if p.net == net]
    if net_ports:
        col = round(port_column_x(net_ports[0]), 1)
        side = stacked_wire_bus_side(net_ports)
        stack_key = (col, side, net)
        if stack_key in plan.stack_buses:
            return plan.stack_buses[stack_key]
    return None


def gnd_trunk_x_from_wire(wire: TopologyWire) -> float | None:
    """Vertical x of a ``gnd_trunk`` wire (``M x,bus_y V …``)."""
    if wire.routing_kind != "gnd_trunk" or wire.net != GND_NET:
        return None
    points = parse_wire_path(wire.path_d)
    if not points:
        return None
    return round(points[0][0], 1)


def routed_gnd_trunk_xs(wires: list[TopologyWire]) -> set[float]:
    """All GND trunk vertical x positions from routed wires."""
    xs: set[float] = set()
    for wire in wires:
        x = gnd_trunk_x_from_wire(wire)
        if x is not None:
            xs.add(x)
    return xs


def planned_gnd_trunk_xs(plan: BusPlan) -> set[float]:
    """All GND trunk x positions recorded on the bus plan."""
    return {round(x, 1) for x in plan.gnd_trunks.values()}


def planned_stack_bus_entries(plan: BusPlan) -> dict[StackBusKey, float]:
    """``(col, side, net) → bus_x`` for stack-column hub lanes."""
    return dict(plan.stack_buses)


def stack_bus_x_matches_routing(
    col: float,
    side: str,
    net: str,
    bus_x: float,
    wires: list[TopologyWire],
) -> bool:
    """True when some routed wire on ``net`` uses this stack bus x."""
    for wire in wires:
        if wire.dashed or wire.net != net or wire.bus_x is None:
            continue
        if abs(wire.bus_x - bus_x) >= WIRE_EPS:
            continue
        if wire.routing_kind in ("hub", "hub_tap", "hub_row", "stack_column"):
            return True
    return False
