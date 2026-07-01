"""GND rail, trunks, and taps."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import GND_NET, MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement import gnd_column_trunk_x, port_stub_x
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.obstacles import gnd_drop_x, obstacle_detour_y
from fypa.topology.routing.paths import path_from_port_stub
from fypa.topology.geometry import simplify_wire_path
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire


def gnd_tap_path(
    port: TopologyPort,
    trunk_x: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    *,
    bus_y: float | None = None,
) -> str:
    stub = port_stub_x(port)
    y = port.y
    start_leg, _, _ = path_from_port_stub(port)
    if abs(trunk_x - stub) < WIRE_EPS:
        ctx.reserve_horizontal(y, min(port.x, stub), max(port.x, stub), GND_NET)
        return simplify_wire_path(start_leg)
    gutter_lo, gutter_hi = min(stub, trunk_x), max(stub, trunk_x)
    y_clear = obstacle_detour_y(
        ctx,
        y,
        gutter_lo,
        gutter_hi,
        obstacles,
        {port.node_id},
        GND_NET,
    )
    if abs(y_clear - y) < WIRE_EPS:
        y_clear = y + MIN_PARALLEL_GAP
        if bus_y is not None and y_clear > bus_y - WIRE_EPS:
            y_clear = y - MIN_PARALLEL_GAP
    ctx.reserve_vertical(stub, min(y, y_clear), max(y, y_clear), GND_NET)
    ctx.reserve_horizontal(y_clear, gutter_lo, gutter_hi, GND_NET)
    return simplify_wire_path(f"{start_leg} V {y_clear:.1f} H {trunk_x:.1f}")


def reserve_gnd_column_trunks(
    ports: list[TopologyPort],
    bus_y: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
) -> dict[float, list[TopologyPort]]:
    if not ports:
        return {}
    groups: dict[float, list[TopologyPort]] = defaultdict(list)
    for port in ports:
        nominal = gnd_drop_x(port, bus_y, obstacles)
        groups[round(nominal, 1)].append(port)

    column_plan: dict[float, list[TopologyPort]] = defaultdict(list)
    for group in groups.values():
        trunk_x = gnd_column_trunk_x(group)
        top_y = min(p.y for p in group)
        y_lo, y_hi = min(bus_y, top_y), max(bus_y, top_y)
        column_plan[round(trunk_x, 1)].extend(group)
        for port in group:
            stub = port_stub_x(port)
            ctx.reserve_horizontal(
                port.y,
                min(port.x, stub),
                max(port.x, stub),
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

    port_nominals: list[tuple[TopologyPort, float]] = [
        (p, gnd_drop_x(p, bus_y, obs)) for p in ports
    ]
    groups: dict[float, list[TopologyPort]] = defaultdict(list)
    for port, nominal_x in port_nominals:
        groups[round(nominal_x, 1)].append(port)

    column_plan: dict[float, list[TopologyPort]] = defaultdict(list)
    for col_key in sorted(groups.keys()):
        group = groups[col_key]
        trunk_x = gnd_column_trunk_x(group)
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
