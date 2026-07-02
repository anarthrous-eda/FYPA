"""Multi-port hub bus planning (stack columns and gutter hubs)."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement.bus_grid import allocate_bus_x, nudge_bus_from_gnd_columns
from fypa.topology.placement.hub_planning import (
    hub_bus_channel_bounds,
    hub_bus_nominal_x,
    hub_bus_outward,
    hub_destination_anchor,
    separate_from_assigned_buses,
    sorted_gutter_hub_items,
)
from fypa.topology.placement.plan_types import BusPlan
from fypa.topology.placement.ports import column_bus_x, port_stub_x
from fypa.topology.placement.types import ColumnSideKey, GutterSpanKey
from fypa.topology.types import TopologyPort


def plan_stack_hub_buses(
    plan: BusPlan,
    stack_hub_nets: dict[ColumnSideKey, list[tuple[str, list[TopologyPort]]]],
) -> None:
    """Assign bus x for 3+ port nets sharing one column."""
    reserved = plan.reserved_verticals
    for (col, side), net_groups in stack_hub_nets.items():
        net_groups.sort(key=lambda t: t[0])
        n_lanes = len(net_groups)
        for lane, (net, ports) in enumerate(net_groups):
            bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
            y_lo = min(p.y for p in ports)
            y_hi = max(p.y for p in ports)
            outward = 1.0 if side == "right" else -1.0
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
            plan.stack_buses[(col, side, net)] = bus_x
            plan.hub_buses[net] = bus_x
            reserved.append((bus_x, y_lo, y_hi, net))


def plan_gutter_hub_buses(
    plan: BusPlan,
    gutter_hub_nets: dict[GutterSpanKey, list[tuple[str, list[TopologyPort]]]],
) -> None:
    """Assign bus x for 3+ port nets spanning a column gutter."""
    reserved = plan.reserved_verticals
    gutter_hub_items = sorted_gutter_hub_items(gutter_hub_nets)

    gutter_assigned: dict[GutterSpanKey, list[float]] = {
        gkey: sorted(
            {
                bx
                for bx in plan.gutter_spans.get(gkey, [])
                if gkey[0] - WIRE_EPS <= bx <= gkey[1] + WIRE_EPS
            }
        )
        for gkey in gutter_hub_nets
    }

    for gkey, net, ports in gutter_hub_items:
        plan.gutter_spans.setdefault(gkey, [])
        assigned_bus = gutter_assigned.setdefault(gkey, [])
        bus_lo, bus_hi = hub_bus_channel_bounds(ports)
        y_lo = min(p.y for p in ports)
        y_hi = max(p.y for p in ports)
        bus_x = hub_bus_nominal_x(ports, bus_lo, bus_hi)
        anchor_stub = port_stub_x(hub_destination_anchor(ports))
        bus_x = nudge_bus_from_gnd_columns(
            bus_x,
            y_lo,
            y_hi,
            reserved,
            anchor_stub=anchor_stub,
        )
        bus_hi = max(bus_hi, bus_x)
        outward = hub_bus_outward(ports, bus_x, bus_lo, bus_hi)
        bus_x = separate_from_assigned_buses(
            bus_x,
            assigned_bus,
            outward=outward,
            bus_lo=bus_lo,
            bus_hi=bus_hi,
        )
        bus_hi = max(bus_hi, bus_x)
        bus_x = min(bus_hi, max(bus_lo, bus_x))
        bus_x = allocate_bus_x(
            bus_x,
            y_lo,
            y_hi,
            bus_lo,
            bus_hi,
            reserved,
            net,
            outward=outward,
            assigned_in_group=assigned_bus,
        )
        bus_x = separate_from_assigned_buses(
            bus_x,
            assigned_bus,
            outward=outward,
            bus_lo=bus_lo,
            bus_hi=bus_hi,
        )
        assigned_bus.append(bus_x)
        plan.hub_buses[net] = bus_x
        plan.gutter_spans[gkey].append(bus_x)
        reserved.append((bus_x, y_lo, y_hi, net))
