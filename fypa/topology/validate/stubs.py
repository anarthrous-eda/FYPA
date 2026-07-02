"""Port stub end connectivity checks for ``validate_topology``."""

from __future__ import annotations

from fypa.topology.constants import GND_NET, WIRE_EPS
from fypa.topology.geometry import (
    SchematicGeometry,
    compute_schematic_geometry,
    point_on_segment,
    same_net_branch_count,
)
from fypa.topology.issues import make_issue
from fypa.topology.placement import port_stub_x
from fypa.topology.types import TopologyModel, TopologyPort, TopologyWire


def collect_routed_ports(wires: list[TopologyWire]) -> set[tuple[str, str, str]]:
    """``(node_id, terminal, net)`` for every solid wire endpoint on a port."""
    routed: set[tuple[str, str, str]] = set()
    for wire in wires:
        if wire.dashed or not wire.net:
            continue
        if wire.src_node:
            routed.add((wire.src_node, wire.src_terminal, wire.net))
        if wire.dst_node:
            routed.add((wire.dst_node, wire.dst_terminal, wire.net))
    return routed


def has_horizontal_stub(port: TopologyPort) -> bool:
    """True when the port has a non-zero horizontal stub outward from the pin."""
    return abs(port_stub_x(port) - port.x) >= WIRE_EPS


def stub_end_connected(
    stub_x: float,
    port_y: float,
    net: str,
    geo: SchematicGeometry,
) -> bool:
    """True when ``(stub_x, port_y)`` joins same-net routing (not a dead stub end).

    Uses schematic segments — the same geometry as junction detection:

    1. Junction dot at the stub end.
    2. Two or more wire directions (corner, T, or pass-through).
    3. Two or more collinear horizontal segments meeting at the stub end
       (merged into one direction by ``same_net_branch_count``).
    """
    x, y = stub_x, port_y
    pt = (round(x, 1), round(y, 1))
    junctions = {(round(jx, 1), round(jy, 1)) for jx, jy in geo.junctions}
    if pt in junctions:
        return True
    if same_net_branch_count(geo.segments, x, y, net) >= 2:
        return True
    touching = [seg for seg in geo.segments if seg.net == net and point_on_segment(seg, x, y)]
    return len(touching) >= 2


def check_open_stub_ends(
    model: TopologyModel,
    *,
    geo: SchematicGeometry | None = None,
) -> list[dict]:
    """Every routed port stub end must join vertical/horizontal routing on its net."""
    if geo is None:
        geo = compute_schematic_geometry(
            model.wires,
            gnd_symbol_x=model.gnd_symbol_x,
            gnd_bus_y=model.gnd_bus_y,
        )
    routed_ports = collect_routed_ports(model.wires)
    issues: list[dict] = []

    for node in model.nodes:
        for port in node.ports:
            if not port.net or port.net == "?":
                continue
            if (port.node_id, port.terminal, port.net) not in routed_ports:
                continue
            if not has_horizontal_stub(port):
                continue

            stub_x = port_stub_x(port)
            pt = (round(stub_x, 1), round(port.y, 1))
            if stub_end_connected(stub_x, port.y, port.net, geo):
                continue

            code = "open_gnd_stub" if port.net == GND_NET else "open_signal_stub"
            net_label = "GND" if port.net == GND_NET else port.net
            message = (
                f"{node.node_id}.{port.terminal} {net_label} stub at "
                f"({pt[0]:.1f},{pt[1]:.1f}) has no vertical continuation"
                if code == "open_gnd_stub"
                else (
                    f"{node.node_id}.{port.terminal} ({port.net}) stub at "
                    f"({pt[0]:.1f},{pt[1]:.1f}) is not connected to routing"
                )
            )
            issues.append(
                make_issue(
                    code,
                    message,
                    node_id=node.node_id,
                    terminal=port.terminal,
                    net=port.net,
                    x=pt[0],
                    y=pt[1],
                )
            )

    return issues
