"""Wire build entrypoints."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import GND_NET, MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement import (
    BusPlan,
    column_bus_x,
    gutter_bus_x_bounds,
    net_gutter_key,
    port_column_x,
    port_stub_x,
    ports_all_share_column,
    stacked_wire_bus_side,
)
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.gnd import gnd_wire_paths, reserve_gnd_column_trunks
from fypa.topology.routing.hub import route_hub
from fypa.topology.routing.pair import signal_wires_from_pairs
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire

_BUS_ROUTING_KINDS = frozenset({
    "hub", "hub_tap", "hub_row", "gutter", "stack_column",
})


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
    two_port_pairs: list[tuple[TopologyPort, TopologyPort]] = []

    gutter_hub_nets: dict[tuple, list[tuple[str, list[TopologyPort]]]] = defaultdict(list)
    stack_hub_nets: dict[tuple, list[tuple[str, list[TopologyPort]]]] = defaultdict(list)

    for net, group in by_net.items():
        if net == GND_NET or len(group) < 2:
            continue
        ordered = sorted(group, key=lambda p: (p.x, p.y))
        if len(ordered) == 2:
            two_port_pairs.append((ordered[0], ordered[1]))
        elif ports_all_share_column(ordered):
            col = round(port_column_x(ordered[0]), 1)
            side = stacked_wire_bus_side(ordered)
            stack_hub_nets[(col, side)].append((net, ordered))
        else:
            gkey = net_gutter_key(ordered)
            if gkey is not None:
                gutter_hub_nets[gkey].append((net, ordered))

    for (col, side), net_groups in stack_hub_nets.items():
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

    wires.extend(signal_wires_from_pairs(
        two_port_pairs, obstacles=obstacles, ctx=ctx, bus_plan=bus_plan,
    ))

    for gkey, net_groups in gutter_hub_nets.items():
        x_lo, x_hi = gkey
        net_groups.sort(key=lambda t: t[0])
        assigned_bus = sorted({
            w.bus_x
            for w in wires
            if w.bus_x is not None
            and w.routing_kind in _BUS_ROUTING_KINDS
            and x_lo - WIRE_EPS <= w.bus_x <= x_hi + WIRE_EPS
        })
        for net, ports in net_groups:
            if bus_plan and net in bus_plan.hub_buses:
                bus_x = bus_plan.hub_buses[net]
            else:
                stubs = [port_stub_x(p) for p in ports]
                bus_lo, bus_hi = gutter_bus_x_bounds(stubs)
                bus_x = (bus_lo + bus_hi) / 2
                for prev in assigned_bus:
                    if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                        bus_x = prev + MIN_PARALLEL_GAP
                bus_x = min(bus_hi, max(bus_lo, bus_x))
                for prev in assigned_bus:
                    if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                        bus_x = min(bus_hi, prev + MIN_PARALLEL_GAP)
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
            gnd_ports, bus_y=gnd_bus_y, obstacles=obs, ctx=ctx,
        )
        wires.extend(gnd_wires)
    return wires, gnd_symbol_x
