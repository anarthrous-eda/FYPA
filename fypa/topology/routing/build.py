"""Wire build entrypoints."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import GND_NET, MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement import (
    BusPlan,
    classify_signal_nets,
    column_bus_x,
    hub_bus_channel_bounds,
    hub_bus_nominal_x,
    sorted_gutter_hub_items,
)
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.gnd import gnd_wire_paths, reserve_gnd_column_trunks
from fypa.topology.routing.hub import route_hub
from fypa.topology.routing.pair import signal_wires_from_pairs
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire

_BUS_ROUTING_KINDS = frozenset(
    {
        "hub",
        "hub_tap",
        "hub_row",
        "gutter",
        "stack_column",
    }
)


def build_signal_wires(
    by_net: dict[str, list[TopologyPort]],
    obstacles: list[TopologyNode],
    ctx: RoutingContext | None = None,
    bus_plan: BusPlan | None = None,
) -> list[TopologyWire]:
    """Route signal nets: pairs for 2-port nets, hub wires for 3+ ports."""
    ctx = ctx or RoutingContext()
    if bus_plan is not None:
        for vx, vy_lo, vy_hi, vnet in bus_plan.reserved_verticals:
            ctx.reserve_vertical(vx, vy_lo, vy_hi, vnet)

    wires: list[TopologyWire] = []
    groups = classify_signal_nets(by_net)

    for (col, side), net_groups in groups.stack_hub_nets.items():
        net_groups.sort(key=lambda t: t[0])
        n_lanes = len(net_groups)
        for lane, (net, ports) in enumerate(net_groups):
            if bus_plan and (col, side, net) in bus_plan.stack_buses:
                bus_x = bus_plan.stack_buses[(col, side, net)]
            elif bus_plan and net in bus_plan.hub_buses:
                bus_x = bus_plan.hub_buses[net]
            else:
                bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
            wires.extend(route_hub(net, ports, bus_x, obstacles, ctx))

    wires.extend(
        signal_wires_from_pairs(
            groups.two_port_pairs,
            obstacles=obstacles,
            ctx=ctx,
            bus_plan=bus_plan,
        )
    )

    gutter_assigned: dict[tuple, list[float]] = {
        gkey: sorted(
            {
                w.bus_x
                for w in wires
                if w.bus_x is not None
                and w.routing_kind in _BUS_ROUTING_KINDS
                and gkey[0] - WIRE_EPS <= w.bus_x <= gkey[1] + WIRE_EPS
            }
        )
        for gkey in groups.gutter_hub_nets
    }
    for gkey, net, ports in sorted_gutter_hub_items(groups.gutter_hub_nets):
        assigned_bus = gutter_assigned.setdefault(gkey, [])
        if bus_plan and net in bus_plan.hub_buses:
            bus_x = bus_plan.hub_buses[net]
        else:
            bus_lo, bus_hi = hub_bus_channel_bounds(ports)
            bus_x = hub_bus_nominal_x(ports, bus_lo, bus_hi)
            for prev in assigned_bus:
                if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = prev + MIN_PARALLEL_GAP
            bus_x = max(bus_lo, bus_x)
        assigned_bus.append(bus_x)
        wires.extend(route_hub(net, ports, bus_x, obstacles, ctx))

    return wires


def build_wires(
    ports: list[TopologyPort],
    *,
    gnd_bus_y: float | None = None,
    obstacles: list[TopologyNode] | None = None,
    bus_plan: BusPlan | None = None,
) -> tuple[list[TopologyWire], float | None]:
    """Build all schematic wires including signal routes and GND."""
    by_net: dict[str, list[TopologyPort]] = defaultdict(list)
    for p in ports:
        if not p.net:
            continue
        by_net[p.net].append(p)

    ctx = RoutingContext()
    obs = obstacles or []
    gnd_ports = by_net.get(GND_NET, [])
    if gnd_ports and gnd_bus_y is not None:
        reserve_gnd_column_trunks(gnd_ports, gnd_bus_y, obs, ctx)
    wires = build_signal_wires(by_net, obs, ctx, bus_plan=bus_plan)
    gnd_symbol_x: float | None = None
    if gnd_ports and gnd_bus_y is not None:
        gnd_wires, gnd_symbol_x = gnd_wire_paths(
            gnd_ports,
            bus_y=gnd_bus_y,
            obstacles=obs,
            ctx=ctx,
        )
        wires.extend(gnd_wires)
    return wires, gnd_symbol_x
