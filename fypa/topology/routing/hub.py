"""Hub routing: tree of row buses, trunk, and taps."""

from __future__ import annotations

from dataclasses import dataclass

from fypa.topology.constants import WIRE_EPS
from fypa.topology.geometry import parse_wire_path, simplify_wire_path
from fypa.topology.placement import port_stub_x
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.paths import (
    group_ports_by_row,
    hub_edge_tap_path,
    hub_row_groups,
    hub_row_path,
    hub_tap_path,
    hub_tap_path_from_bus,
    hub_tap_vertical_to_row,
)
from fypa.topology.routing.obstacles import obstacle_detour_y
from fypa.topology.routing.util import wire_display_label
from fypa.topology.types import TopologyNode, TopologyPort, TopologyWire


@dataclass
class _HubRowPlan:
    group: list[TopologyPort]
    y_row: float
    span_lo: float
    span_hi: float
    row_lo: float
    row_hi: float
    detoured: bool


def _connector_family(designator: str) -> str | None:
    """J2.1 / J2.2 → ``J2``; plain designators → ``None``."""
    if "." not in designator:
        return None
    return designator.rsplit(".", 1)[0]


def _merge_vertical_at_port(
    port: TopologyPort,
    row_y: float,
    row_port_xs: set[float],
    nodes_by_id: dict[str, TopologyNode],
) -> bool:
    """Vertical at the port column when joining a connector sub-symbol row."""
    if port.x not in row_port_xs:
        return False
    fam = _connector_family(nodes_by_id[port.node_id].designator)
    if fam is None:
        return False
    for node in nodes_by_id.values():
        if _connector_family(node.designator) != fam:
            continue
        if abs(node.y - row_y) < WIRE_EPS and any(abs(p.x - port.x) < WIRE_EPS for p in node.ports):
            return True
    return False


def _trunk_y_at_bus(path_d: str, bus_x: float) -> float | None:
    """Return the Y where a tap path meets the hub trunk column, if any."""
    on_trunk = [y for x, y in parse_wire_path(path_d) if abs(x - bus_x) < WIRE_EPS]
    if not on_trunk:
        return None
    return on_trunk[-1]


def _extend_row_to_bus(row_path: str, bus_x: float) -> str:
    """Insert ``bus_x`` into a hub row path while keeping left-to-right order."""
    pts = parse_wire_path(row_path)
    if not pts:
        return row_path
    y = pts[0][1]
    targets = sorted({round(x, 1) for x, _y in pts[1:]} | {round(bus_x, 1)})
    parts = [f"M {pts[0][0]:.1f},{y:.1f}"]
    for x in targets:
        parts.append(f"H {x:.1f}")
    return simplify_wire_path(" ".join(parts))


def _horizontal_extension(
    path_d: str,
) -> tuple[float, float, float] | None:
    """Return ``(x_lo, y, x_hi)`` when *path_d* is a single horizontal segment."""
    pts = parse_wire_path(path_d)
    if len(pts) != 2:
        return None
    (x1, y1), (x2, y2) = pts
    if abs(y1 - y2) > WIRE_EPS:
        return None
    return min(x1, x2), y1, max(x1, x2)


def _route_hub_tap(
    port: TopologyPort,
    bus_x: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    net: str,
    row_spans: list[tuple[float, float, float, set[float]]],
    nodes_by_id: dict[str, TopologyNode],
) -> tuple[str, float]:
    """Tap one port onto the hub trunk without right-to-left routing."""
    stub = port_stub_x(port)
    for row_y, span_lo, span_hi, row_port_xs in row_spans:
        if abs(port.y - row_y) <= WIRE_EPS:
            break
        if _merge_vertical_at_port(port, row_y, row_port_xs, nodes_by_id):
            return hub_tap_vertical_to_row(port, row_y, merge_at_port=True)
        if span_lo - WIRE_EPS <= stub <= span_hi + WIRE_EPS:
            return hub_tap_vertical_to_row(port, row_y)
    if stub > bus_x + WIRE_EPS:
        return hub_tap_path_from_bus(bus_x, port, obstacles, ctx, net)
    return hub_tap_path(port, bus_x, obstacles, ctx, net)


def _plan_hub_rows(
    ordered: list[TopologyPort],
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    net: str,
) -> tuple[list[_HubRowPlan], list[tuple[list[TopologyPort], TopologyPort]]]:
    """Plan row buses and singleton groups (rows before taps)."""
    row_plans: list[_HubRowPlan] = []
    singletons: list[tuple[list[TopologyPort], TopologyPort]] = []
    by_row = group_ports_by_row(ordered)
    for y_key in sorted(by_row.keys()):
        row_ports = sorted(by_row[y_key], key=lambda p: p.x)
        for group in hub_row_groups(row_ports, obstacles):
            if len(group) < 2:
                singletons.append((group, group[0]))
                continue
            row_sorted = sorted(group, key=lambda p: p.x)
            stubs_row = [port_stub_x(p) for p in row_sorted]
            row_lo, row_hi = min(stubs_row), max(stubs_row)
            span_lo = min(row_sorted[0].x, row_lo)
            span_hi = max(row_sorted[-1].x, row_hi)
            skip = {p.node_id for p in row_sorted}
            y_nominal = row_sorted[0].y
            y_row = obstacle_detour_y(
                ctx,
                y_nominal,
                span_lo,
                span_hi,
                obstacles,
                skip,
                net,
            )
            row_plans.append(
                _HubRowPlan(
                    group=group,
                    y_row=y_row,
                    span_lo=span_lo,
                    span_hi=span_hi,
                    row_lo=row_lo,
                    row_hi=row_hi,
                    detoured=abs(y_row - y_nominal) > WIRE_EPS,
                )
            )
    return row_plans, singletons


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

    row_plans, singletons = _plan_hub_rows(ordered, obstacles, ctx, net)
    nodes_by_id = {n.node_id: n for n in obstacles}
    row_wires: list[TopologyWire] = []
    row_wire_by_plan: dict[int, TopologyWire] = {}
    tap_wires: list[TopologyWire] = []
    tap_ys: list[float] = []
    row_spans: list[tuple[float, float, float, set[float]]] = []

    for plan_idx, plan in enumerate(row_plans):
        if plan.detoured:
            continue
        row_sorted = sorted(plan.group, key=lambda p: p.x)
        row_path = hub_row_path(plan.group, plan.y_row)
        ctx.reserve_horizontal(plan.y_row, plan.span_lo, plan.span_hi, net)
        row_wire = TopologyWire(
            net=net,
            path_d=row_path,
            src_node=row_sorted[0].node_id,
            src_terminal=row_sorted[0].terminal,
            dst_node=row_sorted[-1].node_id,
            dst_terminal=row_sorted[-1].terminal,
            routing_kind="hub_row",
            bus_x=bus_x,
        )
        row_wires.append(row_wire)
        row_wire_by_plan[plan_idx] = row_wire
        row_port_xs = {p.x for p in plan.group if abs(p.y - plan.y_row) < WIRE_EPS}
        row_spans.append((plan.y_row, plan.span_lo, plan.span_hi, row_port_xs))

    for plan_idx, plan in enumerate(row_plans):
        if plan.detoured:
            for port in plan.group:
                path_d, tap_y = _route_hub_tap(
                    port,
                    bus_x,
                    obstacles,
                    ctx,
                    net,
                    row_spans,
                    nodes_by_id,
                )
                trunk_y = _trunk_y_at_bus(path_d, bus_x)
                if trunk_y is not None:
                    tap_ys.append(trunk_y)
                tap_wires.append(
                    TopologyWire(
                        net=net,
                        path_d=path_d,
                        src_node=port.node_id,
                        src_terminal=port.terminal,
                        routing_kind="hub_tap",
                        bus_x=bus_x,
                    )
                )
            continue
        if (
            abs(bus_x - (plan.row_hi if bus_x >= (plan.row_lo + plan.row_hi) / 2 else plan.row_lo))
            < WIRE_EPS
        ):
            tap_ys.append(plan.y_row)
            continue
        mid = (plan.row_lo + plan.row_hi) / 2
        attach = plan.group[-1] if bus_x >= mid else plan.group[0]
        edge_x = plan.row_hi if bus_x >= mid else plan.row_lo
        path_d, tap_y = hub_edge_tap_path(
            plan.y_row,
            edge_x,
            bus_x,
            obstacles,
            ctx,
            net,
            skip=set(),
            port=attach,
        )
        extension = _horizontal_extension(path_d)
        row_wire = row_wire_by_plan.get(plan_idx)
        if extension is not None and row_wire is not None:
            _x_lo, y_ext, x_hi = extension
            row_wire.path_d = _extend_row_to_bus(row_wire.path_d, bus_x)
            ctx.reserve_horizontal(
                y_ext,
                min(plan.span_lo, _x_lo),
                max(plan.span_hi, bus_x),
                net,
            )
            trunk_y = _trunk_y_at_bus(row_wire.path_d, bus_x)
            if trunk_y is not None:
                tap_ys.append(trunk_y)
            continue
        trunk_y = _trunk_y_at_bus(path_d, bus_x)
        if trunk_y is not None:
            tap_ys.append(trunk_y)
        tap_wires.append(
            TopologyWire(
                net=net,
                path_d=path_d,
                src_node=attach.node_id,
                src_terminal=attach.terminal,
                routing_kind="hub_tap",
                bus_x=bus_x,
            )
        )

    for _group, port in singletons:
        path_d, tap_y = _route_hub_tap(
            port,
            bus_x,
            obstacles,
            ctx,
            net,
            row_spans,
            nodes_by_id,
        )
        trunk_y = _trunk_y_at_bus(path_d, bus_x)
        if trunk_y is not None:
            tap_ys.append(trunk_y)
        tap_wires.append(
            TopologyWire(
                net=net,
                path_d=path_d,
                src_node=port.node_id,
                src_terminal=port.terminal,
                routing_kind="hub_tap",
                bus_x=bus_x,
            )
        )

    wires: list[TopologyWire] = []
    if tap_ys and (y_hi := max(tap_ys)) - (y_lo := min(tap_ys)) > WIRE_EPS:
        ctx.reserve_vertical(bus_x, y_lo, y_hi, net)
        wires.append(
            TopologyWire(
                net=net,
                path_d=f"M {bus_x:.1f},{y_lo:.1f} V {y_hi:.1f}",
                label=label,
                routing_kind="hub",
                bus_x=bus_x,
            )
        )
    else:
        if row_wires:
            row_wires[0].label = label
        elif tap_wires:
            tap_wires[0].label = label
    wires.extend(row_wires)
    wires.extend(tap_wires)
    return wires
