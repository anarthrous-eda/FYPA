"""Two-port and gutter signal wire routing."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement import (
    BusPlan,
    column_bus_x,
    group_two_port_pairs,
    gutter_bus_x_bounds,
    iter_gutter_pair_slots,
    iter_stacked_pair_lanes,
    nominal_gutter_bus_x,
    port_stub_x,
    stacked_routing_order,
)
from fypa.topology.placement.bus_grid import allocate_bus_x
from fypa.topology.placement.gutter_corridors import (
    ColumnGap,
    adjust_bus_x_for_column_gaps,
    column_gaps_from_nodes,
    pick_gutter_bus_x,
    resolve_gutter_corridor,
)
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.paths import stacked_wire_path, two_port_wire_path
from fypa.topology.routing.util import wire_display_label
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire


def _gutter_bus_bounds(
    channel_lo: float,
    channel_hi: float,
    column_gaps: list[ColumnGap],
    *,
    anchor_x: float,
    n_slots: int,
) -> tuple[float, float]:
    """Routing limits: column-gap corridor when known, else stub channel."""
    if column_gaps:
        corridor = resolve_gutter_corridor(
            channel_lo,
            channel_hi,
            column_gaps,
            anchor_x=anchor_x,
            n_slots=n_slots,
        )
        if corridor is not None:
            return corridor
    return channel_lo, channel_hi


def _separate_from_assigned_buses(
    bus_x: float,
    assigned_bus: list[float],
    outward: float,
    lo: float,
    hi: float,
    *,
    y_lo: float,
    y_hi: float,
    net: str,
    reserved: list[tuple[float, float, float, str]] | None = None,
) -> float:
    """Keep ``bus_x`` at least ``MIN_PARALLEL_GAP`` from prior buses inside ``lo..hi``."""
    reserved = reserved or []
    for prev in assigned_bus:
        if abs(bus_x - prev) >= MIN_PARALLEL_GAP - WIRE_EPS:
            continue
        outward_cand = prev + outward * MIN_PARALLEL_GAP
        inward_cand = prev - outward * MIN_PARALLEL_GAP

        def _in_range(x: float) -> bool:
            return lo - WIRE_EPS <= x <= hi + WIRE_EPS

        candidates = [c for c in (outward_cand, inward_cand) if _in_range(c)]
        if candidates:
            bus_x = min(candidates, key=lambda c: abs(c - bus_x))
        elif _in_range(outward_cand):
            bus_x = outward_cand
        elif _in_range(inward_cand):
            bus_x = inward_cand
        else:
            return allocate_bus_x(
                bus_x,
                y_lo,
                y_hi,
                lo,
                hi,
                reserved,
                net,
                outward=outward,
                assigned_in_group=assigned_bus,
            )
    return min(hi, max(lo, bus_x))


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
    obstacles: list[TopologyNode] | None = None,
) -> float:
    y_lo, y_hi = min(a.y, b.y), max(a.y, b.y)
    anchor_x = port_stub_x(a)
    outward = 1.0 if a.side == "right" else -1.0
    column_gaps = column_gaps_from_nodes(obstacles) if obstacles else []
    reserved = list(ctx.vertical_bands) if ctx is not None else []
    if bus_plan is not None and a.net in bus_plan.pair_buses:
        return bus_plan.pair_buses[a.net]
    if channel_lo != channel_hi and column_gaps:
        return pick_gutter_bus_x(
            slot,
            n_slots,
            channel_lo,
            channel_hi,
            column_gaps,
            a.net,
            y_lo=y_lo,
            y_hi=y_hi,
            anchor_x=anchor_x,
            outward=outward,
            reserved=reserved,
            assigned_in_group=assigned_bus,
        )
    if n_slots > 1 and channel_lo != channel_hi:
        bus_x = nominal_gutter_bus_x(slot, n_slots, channel_lo, channel_hi)
    elif col and side:
        bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
    else:
        bus_x = (channel_lo + channel_hi) / 2
    lo, hi = _gutter_bus_bounds(
        channel_lo,
        channel_hi,
        column_gaps,
        anchor_x=anchor_x,
        n_slots=n_slots,
    )
    bus_x = _separate_from_assigned_buses(
        bus_x,
        assigned_bus,
        outward,
        lo,
        hi,
        y_lo=y_lo,
        y_hi=y_hi,
        net=a.net,
        reserved=reserved,
    )
    if column_gaps:
        bus_x = adjust_bus_x_for_column_gaps(
            bus_x,
            channel_lo,
            channel_hi,
            column_gaps,
            anchor_x=anchor_x,
        )
        bus_x = min(hi, max(lo, bus_x))
    return bus_x


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
        n_lanes = len(group)
        for _y_slot, bus_lane, a, b in iter_stacked_pair_lanes(group, side):
            net = a.net
            if bus_plan and net in bus_plan.pair_buses:
                bus_x = bus_plan.pair_buses[net]
            elif bus_plan and (col, side, net) in bus_plan.stack_buses:
                bus_x = bus_plan.stack_buses[(col, side, net)]
            else:
                bus_x = column_bus_x(col, side, lane=bus_lane, n_lanes=n_lanes)
            start, end = stacked_routing_order(a, b)
            path_d = stacked_wire_path(a, b, bus_x=bus_x, obstacles=obstacles, ctx=ctx)
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
        _approach_side, slot_items = iter_gutter_pair_slots(group)
        _x_lo, _x_hi = key[1], key[2]
        n_slots = len(slot_items)
        all_stubs = [
            stub for _y, _b, a, b in slot_items for stub in (port_stub_x(a), port_stub_x(b))
        ]
        channel_lo, channel_hi = gutter_bus_x_bounds(all_stubs)
        assigned_bus: list[float] = []
        for _y_slot, bus_slot, a, b in slot_items:
            net = a.net
            start, end = stacked_routing_order(a, b)
            bus_x = _bus_x_for_pair(
                a,
                b,
                bus_plan=bus_plan,
                ctx=ctx,
                col=0,
                side="",
                lane=0,
                n_lanes=0,
                slot=bus_slot,
                n_slots=n_slots,
                channel_lo=channel_lo,
                channel_hi=channel_hi,
                assigned_bus=assigned_bus,
                obstacles=obstacles,
            )
            assigned_bus.append(bus_x)
            path_d = two_port_wire_path(start, end, bus_x=bus_x, obstacles=obstacles, ctx=ctx)
            wires.append(
                TopologyWire(
                    net=net,
                    path_d=path_d,
                    label=wire_display_label([start, end], net),
                    src_node=start.node_id,
                    src_terminal=start.terminal,
                    dst_node=end.node_id,
                    dst_terminal=end.terminal,
                    routing_kind="gutter",
                    bus_x=bus_x,
                )
            )
    return wires
