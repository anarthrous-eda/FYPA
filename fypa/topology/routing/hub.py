"""Hub routing: tree of row buses, trunk, and taps."""

from __future__ import annotations

from fypa.topology.constants import WIRE_EPS
from fypa.topology.placement import port_stub_x
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.paths import (
    group_ports_by_row,
    hub_edge_tap_path,
    hub_row_groups,
    hub_row_path,
    hub_tap_path,
)
from fypa.topology.routing.util import wire_display_label
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire


def route_hub(
    net: str,
    ports: list[TopologyPort],
    bus_x: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
) -> list[TopologyWire]:
    """Hub as a tree: collinear row buses, one vertical trunk, row taps."""
    ordered = sorted(ports, key=lambda p: (p.y, p.x))
    label = wire_display_label(ordered, net)

    row_wires: list[TopologyWire] = []
    tap_wires: list[TopologyWire] = []
    tap_ys: list[float] = []
    by_row = group_ports_by_row(ordered)
    for y_key in sorted(by_row.keys()):
        row_ports = sorted(by_row[y_key], key=lambda p: p.x)
        for group in hub_row_groups(row_ports, obstacles):
            if len(group) >= 2:
                row_sorted = sorted(group, key=lambda p: p.x)
                y_row = row_sorted[0].y
                row_path = hub_row_path(group, y_row)
                stubs_row = [port_stub_x(p) for p in row_sorted]
                row_lo, row_hi = min(stubs_row), max(stubs_row)
                span_lo = min(row_sorted[0].x, row_lo)
                span_hi = max(row_sorted[-1].x, row_hi)
                ctx.reserve_horizontal(y_row, span_lo, span_hi, net)
                row_wires.append(TopologyWire(
                    net=net,
                    path_d=row_path,
                    src_node=row_sorted[0].node_id,
                    src_terminal=row_sorted[0].terminal,
                    dst_node=row_sorted[-1].node_id,
                    dst_terminal=row_sorted[-1].terminal,
                    routing_kind="hub_row",
                    bus_x=bus_x,
                ))
                mid = (row_lo + row_hi) / 2
                attach = group[-1] if bus_x >= mid else group[0]
                edge_x = row_hi if bus_x >= mid else row_lo
                path_d, tap_y = hub_edge_tap_path(
                    y_row, edge_x, bus_x, obstacles, ctx, net,
                    skip=set(),
                    port=attach,
                )
                tap_ys.append(tap_y)
                tap_wires.append(TopologyWire(
                    net=net,
                    path_d=path_d,
                    src_node=attach.node_id,
                    src_terminal=attach.terminal,
                    routing_kind="hub_tap",
                    bus_x=bus_x,
                ))
            else:
                port = group[0]
                path_d, tap_y = hub_tap_path(port, bus_x, obstacles, ctx, net)
                tap_ys.append(tap_y)
                tap_wires.append(TopologyWire(
                    net=net,
                    path_d=path_d,
                    src_node=port.node_id,
                    src_terminal=port.terminal,
                    routing_kind="hub_tap",
                    bus_x=bus_x,
                ))

    y_lo, y_hi = min(tap_ys), max(tap_ys)
    wires: list[TopologyWire] = []
    if y_hi - y_lo > WIRE_EPS:
        ctx.reserve_vertical(bus_x, y_lo, y_hi, net)
        wires.append(TopologyWire(
            net=net,
            path_d=f"M {bus_x:.1f},{y_lo:.1f} V {y_hi:.1f}",
            label=label,
            routing_kind="hub",
            bus_x=bus_x,
        ))
    else:
        if row_wires:
            row_wires[0].label = label
        elif tap_wires:
            tap_wires[0].label = label
    wires.extend(row_wires)
    wires.extend(tap_wires)
    return wires
