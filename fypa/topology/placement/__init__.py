"""Port-based placement keys and deterministic bus planning (no routing)."""

from __future__ import annotations

from fypa.topology.placement.bus_grid import allocate_bus_x, gnd_column_trunk_x
from fypa.topology.placement.classify import (
    SignalNetGroups,
    classify_signal_nets,
    group_two_port_pairs,
)
from fypa.topology.placement.hub_planning import (
    hub_bus_channel_bounds,
    hub_bus_nominal_x,
    hub_bus_outward,
    hub_destination_anchor,
    sorted_gutter_hub_items,
)
from fypa.topology.placement.gutter_corridors import (
    bus_x_in_column_gaps,
    column_gaps_from_nodes,
)
from fypa.topology.placement.plan import BusPlan, gutter_bus_span_from_plan, plan_signal_buses
from fypa.topology.placement.pair_slots import (
    bus_slot_assignment_order,
    gutter_approach_side,
    iter_gutter_pair_slots,
    iter_stacked_pair_lanes,
    nominal_gutter_bus_x,
    sort_pairs_by_approach_y,
)
from fypa.topology.placement.ports import (
    column_bus_x,
    gutter_bus_slot_for_source_y,
    gutter_bus_x_bounds,
    gutter_groups,
    group_ports_by_net,
    net_gutter_key,
    port_stub_length,
    port_stub_x,
    ports_all_share_column,
    ports_share_column,
    port_column_x,
    stacked_routing_order,
    stacked_wire_bus_side,
    wire_routing_key,
)

__all__ = [
    "BusPlan",
    "SignalNetGroups",
    "allocate_bus_x",
    "bus_x_in_column_gaps",
    "bus_slot_assignment_order",
    "classify_signal_nets",
    "column_bus_x",
    "column_gaps_from_nodes",
    "gnd_column_trunk_x",
    "group_two_port_pairs",
    "gutter_approach_side",
    "gutter_bus_span_from_plan",
    "gutter_bus_slot_for_source_y",
    "gutter_bus_x_bounds",
    "gutter_groups",
    "group_ports_by_net",
    "hub_bus_channel_bounds",
    "hub_bus_nominal_x",
    "hub_bus_outward",
    "hub_destination_anchor",
    "iter_gutter_pair_slots",
    "iter_stacked_pair_lanes",
    "net_gutter_key",
    "nominal_gutter_bus_x",
    "plan_signal_buses",
    "port_stub_length",
    "port_stub_x",
    "ports_all_share_column",
    "ports_share_column",
    "port_column_x",
    "sort_pairs_by_approach_y",
    "sorted_gutter_hub_items",
    "stacked_routing_order",
    "stacked_wire_bus_side",
    "wire_routing_key",
]
