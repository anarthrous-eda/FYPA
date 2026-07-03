"""GND rail, trunks, and taps."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import GND_NET, WIRE_EPS
from fypa.topology.placement import gnd_column_trunk_x, port_column_x, port_stub_x
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.obstacles import obstacle_detour_y
from fypa.topology.geometry import simplify_wire_path
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire


def _apply_gnd_column_trunk_attach(group: list[TopologyPort]) -> float:
    """Pin every GND port in a column to the shared trunk wire column."""
    trunk_x = gnd_column_trunk_x(group)
    for port in group:
        port.wire_x = trunk_x
    return trunk_x


def gnd_tap_path(
    port: TopologyPort,
    trunk_x: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    *,
    bus_y: float | None = None,
) -> str:
    """Horizontal tap from the port stub onto the column trunk at ``trunk_x``."""
    del bus_y  # trunk height is fixed; taps meet the trunk at ``port.y``
    y = port.y
    stub_x = port_stub_x(port)
    x_lo, x_hi = min(port.x, stub_x, trunk_x), max(port.x, stub_x, trunk_x)
    y_clear = obstacle_detour_y(
        ctx,
        y,
        x_lo,
        x_hi,
        obstacles,
        {port.node_id},
        GND_NET,
    )
    if abs(y_clear - y) > WIRE_EPS:
        ctx.reserve_vertical(stub_x, min(y, y_clear), max(y, y_clear), GND_NET)
        ctx.reserve_horizontal(y_clear, x_lo, x_hi, GND_NET)
        return simplify_wire_path(
            f"M {port.x:.1f},{y:.1f} H {stub_x:.1f} V {y_clear:.1f} H {trunk_x:.1f}"
        )
    ctx.reserve_horizontal(y, x_lo, x_hi, GND_NET)
    if abs(stub_x - trunk_x) < WIRE_EPS:
        return simplify_wire_path(f"M {port.x:.1f},{y:.1f} H {stub_x:.1f}")
    return simplify_wire_path(f"M {port.x:.1f},{y:.1f} H {stub_x:.1f} H {trunk_x:.1f}")


def _gnd_port_groups(ports: list[TopologyPort]) -> list[list[TopologyPort]]:
    """Group GND ports by layout column and port side (never mix left/right)."""
    groups: dict[tuple[float, str], list[TopologyPort]] = defaultdict(list)
    for port in ports:
        groups[(round(port_column_x(port), 1), port.side)].append(port)
    return list(groups.values())


def reserve_gnd_column_trunks(
    ports: list[TopologyPort],
    bus_y: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
) -> dict[float, list[TopologyPort]]:
    if not ports:
        return {}
    column_plan: dict[float, list[TopologyPort]] = defaultdict(list)
    for group in _gnd_port_groups(ports):
        trunk_x = _apply_gnd_column_trunk_attach(group)
        top_y = min(p.y for p in group)
        y_lo, y_hi = min(bus_y, top_y), max(bus_y, top_y)
        column_plan[round(trunk_x, 1)].extend(group)
        for port in group:
            stub_x = port_stub_x(port)
            ctx.reserve_horizontal(
                port.y,
                min(port.x, stub_x, trunk_x),
                max(port.x, stub_x, trunk_x),
                GND_NET,
            )
        ctx.reserve_vertical(trunk_x, y_lo, y_hi, GND_NET)
    return column_plan


def gnd_wire_paths(
    ports: list[TopologyPort],
    *,
    bus_y: float,
    obstacles: list[TopologyNode] | None = None,
    ctx: RoutingContext | None = None,
) -> tuple[list[TopologyWire], float]:
    if not ports:
        return [], 0.0
    obs = obstacles or []
    ctx = ctx or RoutingContext()

    column_plan: dict[float, list[TopologyPort]] = defaultdict(list)
    for group in _gnd_port_groups(ports):
        trunk_x = _apply_gnd_column_trunk_attach(group)
        column_plan[round(trunk_x, 1)].extend(group)

    trunk_xs: list[float] = []
    tap_wires: list[TopologyWire] = []

    for trunk_x in sorted(column_plan.keys()):
        members = column_plan[trunk_x]
        top_y = min(p.y for p in members)
        trunk_xs.append(trunk_x)
        if max(bus_y, top_y) - min(bus_y, top_y) > WIRE_EPS:
            tap_wires.append(
                TopologyWire(
                    net=GND_NET,
                    path_d=f"M {trunk_x:.1f},{bus_y:.1f} V {top_y:.1f}",
                    routing_kind="gnd_trunk",
                )
            )
        for port in sorted(members, key=lambda p: (p.y, p.x)):
            tap_wires.append(
                TopologyWire(
                    net=GND_NET,
                    path_d=gnd_tap_path(port, trunk_x, obs, ctx, bus_y=bus_y),
                    src_node=port.node_id,
                    src_terminal=port.terminal,
                    routing_kind="gnd_tap",
                )
            )

    bus_min = min(trunk_xs)
    bus_max = max(trunk_xs)
    wires: list[TopologyWire] = []
    if bus_max - bus_min > WIRE_EPS:
        wires.append(
            TopologyWire(
                net=GND_NET,
                path_d=f"M {bus_min:.1f},{bus_y:.1f} H {bus_max:.1f}",
                routing_kind="gnd_rail",
            )
        )
    wires.extend(tap_wires)
    return wires, bus_min
