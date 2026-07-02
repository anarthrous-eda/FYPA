"""Two-port net bus planning (stacked columns and gutter gaps)."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement.bus_grid import allocate_bus_x
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
    """Assign bus x for 2-port nets stacked in the same column."""
    reserved = plan.reserved_verticals
    for (col, side), group in stacked_groups.items():
        group.sort(key=lambda ab: (ab[0].net, ab[0].y, ab[1].y))
        n_lanes = len(group)
        for lane, (a, b) in enumerate(group):
            net = a.net
            bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
            y_lo, y_hi = min(a.y, b.y), max(a.y, b.y)
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
            plan.pair_buses[net] = bus_x
            reserved.append((bus_x, y_lo, y_hi, net))


def plan_gutter_pair_buses(
    plan: BusPlan,
    gap_groups: dict[GapRoutingKey, list[tuple[TopologyPort, TopologyPort]]],
) -> None:
    """Assign bus x for 2-port nets routed through a column gutter."""
    reserved = plan.reserved_verticals
    for key, group in gap_groups.items():
        group.sort(key=lambda ab: (min(ab[0].y, ab[1].y), ab[0].net))
        x_lo, x_hi = key[1], key[2]
        n_slots = len(group)
        all_stubs = [stub for a, b in group for stub in (port_stub_x(a), port_stub_x(b))]
        channel_lo, channel_hi = gutter_bus_x_bounds(all_stubs)
        assigned_bus: list[float] = []
        gkey = (x_lo, x_hi)
        plan.gutter_spans.setdefault(gkey, [])
        for slot, (a, b) in enumerate(group):
            net = a.net
            bus_lo, bus_hi = channel_lo, channel_hi
            if n_slots > 1:
                span = bus_hi - bus_lo
                need = (n_slots - 1) * MIN_PARALLEL_GAP
                bus_x = bus_lo + slot * MIN_PARALLEL_GAP
                if span < need - WIRE_EPS:
                    bus_x = bus_lo + slot * (span / max(n_slots - 1, 1))
            else:
                bus_x = (bus_lo + bus_hi) / 2
            bus_x = min(bus_hi, max(bus_lo, bus_x))
            y_lo, y_hi = min(a.y, b.y), max(a.y, b.y)
            for prev in assigned_bus:
                if bus_x < prev + MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = prev + MIN_PARALLEL_GAP
            bus_x = min(bus_hi, max(bus_lo, bus_x))
            bus_x = allocate_bus_x(
                bus_x,
                y_lo,
                y_hi,
                bus_lo,
                bus_hi,
                reserved,
                net,
                outward=bus_outward(bus_x, bus_lo, bus_hi),
                assigned_in_group=assigned_bus,
            )
            for prev in assigned_bus:
                if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = min(bus_hi, prev + MIN_PARALLEL_GAP)
            assigned_bus.append(bus_x)
            plan.pair_buses[net] = bus_x
            plan.gutter_spans[gkey].append(bus_x)
            reserved.append((bus_x, y_lo, y_hi, net))
