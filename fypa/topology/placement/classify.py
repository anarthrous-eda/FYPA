"""Signal net classification for bus planning and routing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from fypa.topology.constants import GND_NET
from fypa.topology.placement.ports import (
    net_gutter_key,
    port_column_x,
    ports_all_share_column,
    stacked_wire_bus_side,
    wire_routing_key,
)
from fypa.topology.placement.types import (
    ColumnSideKey,
    GapRoutingKey,
    GutterSpanKey,
)
from fypa.topology.types import TopologyPort


@dataclass
class SignalNetGroups:
    """Signal nets grouped by routing strategy (shared by plan + route)."""

    two_port_pairs: list[tuple[TopologyPort, TopologyPort]]
    stack_hub_nets: dict[ColumnSideKey, list[tuple[str, list[TopologyPort]]]]
    gutter_hub_nets: dict[GutterSpanKey, list[tuple[str, list[TopologyPort]]]]


def classify_signal_nets(
    by_net: dict[str, list[TopologyPort]],
) -> SignalNetGroups:
    """Classify routable signal nets: 2-port pairs, stack hubs, gutter hubs."""
    two_port_pairs: list[tuple[TopologyPort, TopologyPort]] = []
    gutter_hub_nets: dict[GutterSpanKey, list[tuple[str, list[TopologyPort]]]] = defaultdict(list)
    stack_hub_nets: dict[ColumnSideKey, list[tuple[str, list[TopologyPort]]]] = defaultdict(list)

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

    return SignalNetGroups(
        two_port_pairs=two_port_pairs,
        stack_hub_nets=dict(stack_hub_nets),
        gutter_hub_nets=dict(gutter_hub_nets),
    )


def group_two_port_pairs(
    pairs: list[tuple[TopologyPort, TopologyPort]],
) -> tuple[
    dict[ColumnSideKey, list[tuple[TopologyPort, TopologyPort]]],
    dict[GapRoutingKey, list[tuple[TopologyPort, TopologyPort]]],
]:
    """Split 2-port nets into same-column stacks vs gutter gaps."""
    stacked_groups: dict[ColumnSideKey, list[tuple[TopologyPort, TopologyPort]]] = defaultdict(list)
    gap_groups: dict[GapRoutingKey, list[tuple[TopologyPort, TopologyPort]]] = defaultdict(list)
    for a, b in pairs:
        key = wire_routing_key(a, b)
        if key[0] == "stack":
            col = round(port_column_x(a), 1)
            side = stacked_wire_bus_side([a, b])
            stacked_groups[(col, side)].append((a, b))
        else:
            gap_groups[key].append((a, b))
    return dict(stacked_groups), dict(gap_groups)
