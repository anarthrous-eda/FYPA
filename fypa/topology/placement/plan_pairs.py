"""Two-port net bus planning (stacked columns and gutter gaps)."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement.bus_grid import allocate_bus_x
from fypa.topology.placement.gutter_corridors import (
    ColumnGap,
    pick_gutter_bus_x,
)
from fypa.topology.placement.pair_slots import (
    iter_gutter_pair_slots,
    iter_stacked_pair_lanes,
    nominal_gutter_bus_x,
)
from fypa.topology.placement.plan_types import BusPlan
from fypa.topology.placement.ports import (
    bus_outward,
    column_bus_x,
    gutter_bus_x_bounds,
    port_stub_x,
)
from fypa.topology.placement.types import ColumnSideKey, GapRoutingKey
from fypa.topology.types import TopologyPort


def plan_stacked_pair_buses(
    plan: BusPlan,
    stacked_groups: dict[ColumnSideKey, list[tuple[TopologyPort, TopologyPort]]],
) -> None:
    """Assign bus x for 2-port nets stacked in the same column.

    Stack buses use ``column_bus_x`` on the outward column edge (same column as
    the ports), not ``pick_gutter_bus_x``. That places the vertical beside the
    symbol body; validation allows it when the x still falls in a layout gap.
    """
    reserved = plan.reserved_verticals
    for (col, side), group in stacked_groups.items():
        n_lanes = len(group)
        assigned_bus: list[float] = []
        for _y_slot, bus_lane, a, b in iter_stacked_pair_lanes(group, side):
            net = a.net
            bus_x = column_bus_x(col, side, lane=bus_lane, n_lanes=n_lanes)
            y_lo, y_hi = min(a.y, b.y), max(a.y, b.y)
            outward = 1.0 if side == "right" else -1.0
            for prev in assigned_bus:
                if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = prev + outward * MIN_PARALLEL_GAP
            bus_x = allocate_bus_x(
                bus_x,
                y_lo,
                y_hi,
                bus_x - MIN_PARALLEL_GAP,
                bus_x + MIN_PARALLEL_GAP,
                reserved,
                net,
                outward=outward,
            )
            plan.pair_buses[net] = bus_x
            assigned_bus.append(bus_x)
            reserved.append((bus_x, y_lo, y_hi, net))


def plan_gutter_pair_buses(
    plan: BusPlan,
    gap_groups: dict[GapRoutingKey, list[tuple[TopologyPort, TopologyPort]]],
    *,
    column_gaps: list[ColumnGap] | None = None,
) -> None:
    """Assign bus x for 2-port nets routed through a column gutter."""
    reserved = plan.reserved_verticals
    gaps = column_gaps or []
    for key, group in gap_groups.items():
        x_lo, x_hi = key[1], key[2]
        _approach_side, slot_items = iter_gutter_pair_slots(group)
        n_slots = len(slot_items)
        all_stubs = [
            stub for _y, _b, a, b in slot_items for stub in (port_stub_x(a), port_stub_x(b))
        ]
        channel_lo, channel_hi = gutter_bus_x_bounds(all_stubs)
        assigned_bus: list[float] = []
        gkey = (x_lo, x_hi)
        plan.gutter_spans.setdefault(gkey, [])
        for _y_slot, bus_slot, a, b in slot_items:
            net = a.net
            y_lo, y_hi = min(a.y, b.y), max(a.y, b.y)
            anchor_x = port_stub_x(a)
            outward = 1.0 if a.side == "right" else -1.0
            if gaps:
                bus_x = pick_gutter_bus_x(
                    bus_slot,
                    n_slots,
                    channel_lo,
                    channel_hi,
                    gaps,
                    net,
                    y_lo=y_lo,
                    y_hi=y_hi,
                    anchor_x=anchor_x,
                    outward=outward,
                    reserved=reserved,
                    assigned_in_group=assigned_bus,
                )
            else:
                bus_x = nominal_gutter_bus_x(bus_slot, n_slots, channel_lo, channel_hi)
                for prev in assigned_bus:
                    if bus_x < prev + MIN_PARALLEL_GAP - WIRE_EPS:
                        bus_x = prev + MIN_PARALLEL_GAP
                bus_x = min(channel_hi, max(channel_lo, bus_x))
                bus_x = allocate_bus_x(
                    bus_x,
                    y_lo,
                    y_hi,
                    channel_lo,
                    channel_hi,
                    reserved,
                    net,
                    outward=bus_outward(bus_x, channel_lo, channel_hi),
                    assigned_in_group=assigned_bus,
                )
                for prev in assigned_bus:
                    if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                        bus_x = min(channel_hi, prev + MIN_PARALLEL_GAP)
            assigned_bus.append(bus_x)
            plan.pair_buses[net] = bus_x
            plan.gutter_spans[gkey].append(bus_x)
            reserved.append((bus_x, y_lo, y_hi, net))
