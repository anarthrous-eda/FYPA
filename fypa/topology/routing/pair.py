"""Two-port and gutter signal wire routing."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement import (
    BusPlan,
    column_bus_x,
    group_two_port_pairs,
    gutter_bus_x_bounds,
    port_stub_x,
    stacked_routing_order,
)
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.paths import stacked_wire_path, two_port_wire_path
from fypa.topology.routing.util import wire_display_label
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire


def _bus_x_for_pair(
    a: TopologyPort,
    b: TopologyPort,
    *,
    bus_plan: BusPlan | None,
    ctx: RoutingContext,
    col: float,
    side: str,
    lane: int,
    n_lanes: int,
    slot: int,
    n_slots: int,
    channel_lo: float,
    channel_hi: float,
    assigned_bus: list[float],
) -> float:
    if bus_plan is not None and a.net in bus_plan.pair_buses:
        return bus_plan.pair_buses[a.net]
    _y_lo, _y_hi = min(a.y, b.y), max(a.y, b.y)
    if n_slots > 1 and channel_lo != channel_hi:
        bus_lo, bus_hi = channel_lo, channel_hi
        span = bus_hi - bus_lo
        need = (n_slots - 1) * MIN_PARALLEL_GAP
        bus_x = bus_lo + slot * MIN_PARALLEL_GAP
        if span < need - WIRE_EPS:
            bus_x = bus_lo + slot * (span / max(n_slots - 1, 1))
    elif col and side:
        bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
    else:
        bus_x = (channel_lo + channel_hi) / 2
    bus_x = min(channel_hi, max(channel_lo, bus_x))
    for prev in assigned_bus:
        if bus_x < prev + MIN_PARALLEL_GAP - WIRE_EPS:
            bus_x = prev + MIN_PARALLEL_GAP
    return min(channel_hi, max(channel_lo, bus_x))


def signal_wires_from_pairs(
    pairs: list[tuple[TopologyPort, TopologyPort]],
    *,
    obstacles: list[TopologyNode] | None = None,
    ctx: RoutingContext | None = None,
    bus_plan: BusPlan | None = None,
) -> list[TopologyWire]:
    ctx = ctx or RoutingContext()
    stacked_groups, gap_groups = group_two_port_pairs(pairs)

    wires: list[TopologyWire] = []
    for (col, side), group in stacked_groups.items():
        group.sort(key=lambda ab: (ab[0].net, ab[0].y, ab[1].y))
        n_lanes = len(group)
        for lane, (a, b) in enumerate(group):
            net = a.net
            if bus_plan and net in bus_plan.pair_buses:
                bus_x = bus_plan.pair_buses[net]
            elif bus_plan and (col, side, net) in bus_plan.stack_buses:
                bus_x = bus_plan.stack_buses[(col, side, net)]
            else:
                bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
            path_d = stacked_wire_path(a, b, bus_x=bus_x, obstacles=obstacles, ctx=ctx)
            start, end = stacked_routing_order(a, b)
            wires.append(
                TopologyWire(
                    net=net,
                    path_d=path_d,
                    label=wire_display_label([a, b], net),
                    src_node=start.node_id,
                    src_terminal=start.terminal,
                    dst_node=end.node_id,
                    dst_terminal=end.terminal,
                    routing_kind="stack_column",
                    bus_x=bus_x,
                )
            )

    for key, group in gap_groups.items():
        group.sort(key=lambda ab: (min(ab[0].y, ab[1].y), ab[0].net))
        _x_lo, _x_hi = key[1], key[2]
        n_slots = len(group)
        all_stubs = [stub for a, b in group for stub in (port_stub_x(a), port_stub_x(b))]
        channel_lo, channel_hi = gutter_bus_x_bounds(all_stubs)
        assigned_bus: list[float] = []
        for slot, (a, b) in enumerate(group):
            net = a.net
            bus_x = _bus_x_for_pair(
                a,
                b,
                bus_plan=bus_plan,
                ctx=ctx,
                col=0,
                side="",
                lane=0,
                n_lanes=0,
                slot=slot,
                n_slots=n_slots,
                channel_lo=channel_lo,
                channel_hi=channel_hi,
                assigned_bus=assigned_bus,
            )
            assigned_bus.append(bus_x)
            path_d = two_port_wire_path(a, b, bus_x=bus_x, obstacles=obstacles, ctx=ctx)
            wires.append(
                TopologyWire(
                    net=net,
                    path_d=path_d,
                    label=wire_display_label([a, b], net),
                    src_node=a.node_id,
                    src_terminal=a.terminal,
                    dst_node=b.node_id,
                    dst_terminal=b.terminal,
                    routing_kind="gutter",
                    bus_x=bus_x,
                )
            )
    return wires
