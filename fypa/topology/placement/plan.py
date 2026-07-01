"""Bus plan dataclass and ``plan_signal_buses`` orchestration."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.placement.classify import (
    classify_signal_nets,
    group_two_port_pairs,
)
from fypa.topology.placement.plan_gnd import plan_gnd_trunks
from fypa.topology.placement.plan_hubs import plan_gutter_hub_buses, plan_stack_hub_buses
from fypa.topology.placement.plan_pairs import plan_gutter_pair_buses, plan_stacked_pair_buses
from fypa.topology.placement.plan_types import BusPlan
from fypa.topology.placement.ports import (
    gutter_groups,
    group_ports_by_net,
    net_gutter_key,
)
from fypa.topology.placement.types import GutterSpanKey
from fypa.topology.types import TopologyPort


def plan_signal_buses(
    by_net: dict[str, list[TopologyPort]],
    *,
    gnd_ports: list[TopologyPort] | None = None,
    gnd_bus_y: float | None = None,
) -> BusPlan:
    """Deterministic bus-x plan mirroring ``build_signal_wires`` slot order."""
    plan = BusPlan()

    if gnd_ports and gnd_bus_y is not None:
        plan_gnd_trunks(plan, gnd_ports, gnd_bus_y)

    groups = classify_signal_nets(by_net)
    plan_stack_hub_buses(plan, groups.stack_hub_nets)

    stacked_groups, gap_groups = group_two_port_pairs(groups.two_port_pairs)
    plan_stacked_pair_buses(plan, stacked_groups)
    plan_gutter_pair_buses(plan, gap_groups)
    plan_gutter_hub_buses(plan, groups.gutter_hub_nets)

    return plan


def gutter_bus_span_from_plan(
    plan: BusPlan,
    all_ports: list[TopologyPort],
) -> dict[GutterSpanKey, tuple[float, float, int]]:
    """Per gutter span: ``(x_min, x_max, n_buses)`` from the bus plan."""
    by_net = group_ports_by_net(all_ports)
    gutter_nets = gutter_groups(all_ports)
    result: dict[GutterSpanKey, tuple[float, float, int]] = {}

    bus_xs_by_gutter: dict[GutterSpanKey, set[float]] = defaultdict(set)
    for gkey, xs in plan.gutter_spans.items():
        for x in xs:
            bus_xs_by_gutter[gkey].add(round(x, 1))
    for net, bus_x in plan.hub_buses.items():
        group = by_net.get(net, [])
        if len(group) < 3:
            continue
        gkey = net_gutter_key(group)
        if gkey is not None:
            bus_xs_by_gutter[gkey].add(round(bus_x, 1))
    for net, bus_x in plan.pair_buses.items():
        group = by_net.get(net, [])
        if len(group) == 2:
            gkey = net_gutter_key(group)
            if gkey is not None:
                bus_xs_by_gutter[gkey].add(round(bus_x, 1))

    for gkey in gutter_nets:
        xs = sorted(bus_xs_by_gutter.get(gkey, set()))
        if xs:
            result[gkey] = (xs[0], xs[-1], len(xs))
    return result
