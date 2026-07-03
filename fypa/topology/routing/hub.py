"""Hub routing: tree of row buses, trunk, and taps."""

from __future__ import annotations

from dataclasses import dataclass

from fypa.topology.constants import WIRE_EPS
from fypa.topology.geometry import parse_wire_path, simplify_wire_path
from fypa.topology.placement import port_stub_x
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.paths import (
    _foreign_horizontal_blocks_row,
    _foreign_vertical_blocks_column,
    group_ports_by_row,
    hub_row_path,
    hub_row_groups,
    hub_row_stub_columns,
    hub_row_tap_via_escape_column,
    hub_tap_path,
    hub_tap_path_from_bus,
    hub_tap_vertical_to_row,
    path_from_port_stub,
)
from fypa.topology.routing.obstacles import (
    horizontal_segment_clear,
    obstacle_detour_y,
    obstacle_detour_y_candidates,
    trunk_vertical_clear,
)
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


@dataclass
class _HubRouteState:
    """Mutable hub routing artifacts assembled by ``route_hub``."""

    net: str
    bus_x: float
    obstacles: list[TopologyNode]
    ctx: RoutingContext
    nodes_by_id: dict[str, TopologyNode]
    row_wires: list[TopologyWire]
    row_wire_by_plan: dict[int, TopologyWire]
    tap_wires: list[TopologyWire]
    tap_ys: list[float]
    row_spans: list[tuple[float, float, float, set[float]]]

    def record_tap(self, port: TopologyPort, path_d: str) -> None:
        trunk_y = _trunk_y_at_bus(path_d, self.bus_x)
        if trunk_y is not None:
            self.tap_ys.append(trunk_y)
        self.tap_wires.append(
            TopologyWire(
                net=self.net,
                path_d=path_d,
                src_node=port.node_id,
                src_terminal=port.terminal,
                routing_kind="hub_tap",
                bus_x=self.bus_x,
            )
        )

    def append_singleton_tap(self, port: TopologyPort) -> None:
        path_d, _tap_y = _route_hub_tap(
            port,
            self.bus_x,
            self.obstacles,
            self.ctx,
            self.net,
            self.row_spans,
            self.nodes_by_id,
        )
        if path_d:
            self.record_tap(port, path_d)


def hub_row_edge_x(row_lo: float, row_hi: float, bus_x: float) -> float:
    """Return the row stub column used to feed a hub row toward ``bus_x``."""
    mid = (row_lo + row_hi) / 2
    return row_hi if bus_x >= mid else row_lo


def _connector_family(designator: str) -> str | None:
    """J2.1 / J2.2 → ``J2``; plain designators → ``None``."""
    if "." not in designator:
        return None
    return designator.rsplit(".", 1)[0]


def _vertical_drop_to_row_clear(
    ctx: RoutingContext,
    col_x: float,
    y_lo: float,
    y_hi: float,
    net: str,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> bool:
    """True when a vertical from a port stub column onto a row may be routed."""
    return trunk_vertical_clear(
        col_x, y_lo, y_hi, obstacles, skip
    ) and not _foreign_vertical_blocks_column(
        ctx,
        col_x,
        y_lo,
        y_hi,
        net,
    )


def _merge_vertical_at_port(
    port: TopologyPort,
    row_y: float,
    row_port_xs: set[float],
    nodes_by_id: dict[str, TopologyNode],
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    net: str,
) -> bool:
    """Vertical at the port column when joining a connector sub-symbol row."""
    if port.x not in row_port_xs:
        return False
    y_lo, y_hi = min(port.y, row_y), max(port.y, row_y)
    if not _vertical_drop_to_row_clear(ctx, port.x, y_lo, y_hi, net, obstacles, {port.node_id}):
        return False
    fam = _connector_family(nodes_by_id[port.node_id].designator)
    if fam is None:
        return False
    for node in nodes_by_id.values():
        if _connector_family(node.designator) != fam:
            continue
        if abs(node.y - row_y) < WIRE_EPS and any(abs(p.x - port.x) < WIRE_EPS for p in node.ports):
            return True
        if any(abs(p.x - port.x) < WIRE_EPS and p.x in row_port_xs for p in node.ports):
            return True
    return False


def _trunk_y_at_bus(path_d: str, bus_x: float) -> float | None:
    """Return the Y where a tap path meets the hub trunk column, if any."""
    on_trunk = [y for x, y in parse_wire_path(path_d) if abs(x - bus_x) < WIRE_EPS]
    if not on_trunk:
        return None
    return on_trunk[-1]


def _row_meets_net_vertical(
    ctx: RoutingContext,
    plan: _HubRowPlan,
    net: str,
) -> bool:
    """True when the row span already crosses a same-net vertical at ``plan.y_row``."""
    y = plan.y_row
    for vx, vy_lo, vy_hi, vnet in ctx.vertical_bands:
        if vnet != net:
            continue
        if vy_lo > y + WIRE_EPS or vy_hi < y - WIRE_EPS:
            continue
        if plan.span_lo - WIRE_EPS <= vx <= plan.span_hi + WIRE_EPS:
            return True
    return False


def _connect_row_to_bus(
    plan: _HubRowPlan,
    bus_x: float,
    ctx: RoutingContext,
    net: str,
    obstacles: list[TopologyNode],
) -> tuple[float | None, str | None]:
    """Attach a hub row to the trunk column at ``bus_x``.

    Returns ``(trunk_y on bus column, optional feed wire)``. When the row can
    reach ``bus_x``, emit a feed wire and the Y where it meets the trunk.
    """
    edge_x = hub_row_edge_x(plan.row_lo, plan.row_hi, bus_x)
    if plan.row_lo - WIRE_EPS <= bus_x <= plan.row_hi + WIRE_EPS:
        return plan.y_row, None
    if _row_meets_net_vertical(ctx, plan, net):
        return None, None
    lo, hi = min(edge_x, bus_x), max(edge_x, bus_x)

    def _clearance_skip(y_feed: float) -> set[str]:
        if abs(y_feed - plan.y_row) <= WIRE_EPS:
            return {p.node_id for p in plan.group}
        return set()

    def _feed_at(y_feed: float) -> str | None:
        skip = _clearance_skip(y_feed)
        if not horizontal_segment_clear(y_feed, lo, hi, obstacles, skip):
            return None
        if _foreign_horizontal_blocks_row(ctx, y_feed, lo, hi, net):
            return None
        if abs(y_feed - plan.y_row) > WIRE_EPS:
            y_lo, y_hi = min(plan.y_row, y_feed), max(plan.y_row, y_feed)
            if not trunk_vertical_clear(edge_x, y_lo, y_hi, obstacles, set()):
                return None
            if _foreign_vertical_blocks_column(ctx, edge_x, y_lo, y_hi, net):
                return None
            ctx.reserve_vertical(edge_x, y_lo, y_hi, net)
            ctx.reserve_horizontal(y_feed, lo, hi, net)
            return simplify_wire_path(
                f"M {edge_x:.1f},{plan.y_row:.1f} V {y_feed:.1f} H {bus_x:.1f}",
            )
        ctx.reserve_horizontal(
            y_feed,
            min(plan.span_lo, bus_x),
            max(plan.span_hi, bus_x),
            net,
        )
        return simplify_wire_path(
            f"M {edge_x:.1f},{plan.y_row:.1f} H {bus_x:.1f}",
        )

    for y_feed in obstacle_detour_y_candidates(
        ctx,
        plan.y_row,
        lo,
        hi,
        obstacles,
        set(),
        net,
    ):
        path_d = _feed_at(y_feed)
        if path_d is not None:
            trunk_y = plan.y_row if abs(y_feed - plan.y_row) <= WIRE_EPS else y_feed
            return trunk_y, path_d
    return None, None


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
    for row_y, row_lo, row_hi, row_port_xs in row_spans:
        if abs(port.y - row_y) <= WIRE_EPS:
            if (
                row_lo - WIRE_EPS <= stub <= row_hi + WIRE_EPS
                and port.x in row_port_xs
            ):
                return "", row_y
            continue
        if _merge_vertical_at_port(port, row_y, row_port_xs, nodes_by_id, obstacles, ctx, net):
            return hub_tap_vertical_to_row(
                port,
                row_y,
                merge_at_port=True,
                ctx=ctx,
                net=net,
            )
        if row_lo - WIRE_EPS <= stub <= row_hi + WIRE_EPS:
            start_leg, col_x, _ = path_from_port_stub(port)
            y_lo, y_hi = min(port.y, row_y), max(port.y, row_y)
            skip = {port.node_id}
            if not _foreign_vertical_blocks_column(ctx, col_x, y_lo, y_hi, net):
                return hub_tap_vertical_to_row(
                    port,
                    row_y,
                    ctx=ctx,
                    net=net,
                )
            if abs(col_x - bus_x) > WIRE_EPS:
                lo, hi = min(col_x, bus_x), max(col_x, bus_x)
                if horizontal_segment_clear(
                    port.y, lo, hi, obstacles, skip
                ) and not _foreign_horizontal_blocks_row(ctx, port.y, lo, hi, net):
                    return hub_tap_vertical_to_row(
                        port,
                        row_y,
                        bus_x=bus_x,
                        obstacles=obstacles,
                        skip=skip,
                        ctx=ctx,
                        net=net,
                    )
            escaped = hub_row_tap_via_escape_column(
                port,
                row_y,
                col_x,
                start_leg,
                ctx,
                net,
                obstacles,
                skip,
            )
            if escaped is not None:
                return escaped
            continue
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
            row_lo, row_hi = hub_row_stub_columns(row_sorted)
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


def _row_port_xs(plan: _HubRowPlan) -> set[float]:
    if plan.detoured:
        return {p.x for p in plan.group}
    return {p.x for p in plan.group if abs(p.y - plan.y_row) < WIRE_EPS}


def _emit_hub_row_wires(state: _HubRouteState, row_plans: list[_HubRowPlan]) -> None:
    for plan_idx, plan in enumerate(row_plans):
        row_sorted = sorted(plan.group, key=lambda p: p.x)
        row_path = hub_row_path(plan.group, plan.y_row)
        state.ctx.reserve_horizontal(plan.y_row, plan.span_lo, plan.span_hi, state.net)
        row_wire = TopologyWire(
            net=state.net,
            path_d=row_path,
            src_node=row_sorted[0].node_id,
            src_terminal=row_sorted[0].terminal,
            dst_node=row_sorted[-1].node_id,
            dst_terminal=row_sorted[-1].terminal,
            routing_kind="hub_row",
            bus_x=state.bus_x,
        )
        state.row_wires.append(row_wire)
        state.row_wire_by_plan[plan_idx] = row_wire
        state.row_spans.append(
            (plan.y_row, plan.row_lo, plan.row_hi, _row_port_xs(plan))
        )


def _route_detoured_port_to_row(
    state: _HubRouteState,
    plan: _HubRowPlan,
    port: TopologyPort,
) -> str | None:
    col_x = port_stub_x(port)
    y_lo, y_hi = min(port.y, plan.y_row), max(port.y, plan.y_row)
    if _vertical_drop_to_row_clear(
        state.ctx,
        col_x,
        y_lo,
        y_hi,
        state.net,
        state.obstacles,
        {port.node_id},
    ):
        path_d, _tap_y = hub_tap_vertical_to_row(
            port,
            plan.y_row,
            ctx=state.ctx,
            net=state.net,
        )
        return path_d or None
    start_leg, col_x, _ = path_from_port_stub(port)
    escaped = hub_row_tap_via_escape_column(
        port,
        plan.y_row,
        col_x,
        start_leg,
        state.ctx,
        state.net,
        state.obstacles,
        {port.node_id},
    )
    if escaped is not None:
        path_d, _tap_y = escaped
        return path_d or None
    path_d, _tap_y = _route_hub_tap(
        port,
        state.bus_x,
        state.obstacles,
        state.ctx,
        state.net,
        state.row_spans,
        state.nodes_by_id,
    )
    return path_d or None


def _emit_detoured_row_drops(state: _HubRouteState, plan: _HubRowPlan) -> None:
    for port in plan.group:
        if abs(port.y - plan.y_row) <= WIRE_EPS:
            continue
        path_d = _route_detoured_port_to_row(state, plan, port)
        if path_d:
            state.record_tap(port, path_d)


def _emit_row_bus_feed(state: _HubRouteState, plan: _HubRowPlan) -> None:
    trunk_y, bus_leg = _connect_row_to_bus(
        plan,
        state.bus_x,
        state.ctx,
        state.net,
        state.obstacles,
    )
    if trunk_y is not None:
        state.tap_ys.append(trunk_y)
    if bus_leg is None:
        return
    mid = (plan.row_lo + plan.row_hi) / 2
    attach = plan.group[-1] if state.bus_x >= mid else plan.group[0]
    state.tap_wires.append(
        TopologyWire(
            net=state.net,
            path_d=bus_leg,
            src_node=attach.node_id,
            src_terminal=attach.terminal,
            routing_kind="hub_tap",
            bus_x=state.bus_x,
        )
    )


def _connect_row_plans(state: _HubRouteState, row_plans: list[_HubRowPlan]) -> None:
    for plan_idx, plan in enumerate(row_plans):
        if plan.detoured:
            _emit_detoured_row_drops(state, plan)
        if state.row_wire_by_plan.get(plan_idx) is not None:
            _emit_row_bus_feed(state, plan)


def _assemble_hub_wires(
    label: str,
    net: str,
    bus_x: float,
    ctx: RoutingContext,
    row_wires: list[TopologyWire],
    tap_wires: list[TopologyWire],
    tap_ys: list[float],
) -> list[TopologyWire]:
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
    elif row_wires:
        row_wires[0].label = label
    elif tap_wires:
        tap_wires[0].label = label
    wires.extend(row_wires)
    wires.extend(tap_wires)
    return wires


def route_hub(
    net: str,
    ports: list[TopologyPort],
    bus_x: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
) -> list[TopologyWire]:
    """Hub as a tree: collinear row buses, one vertical trunk, and row taps."""
    ordered = sorted(ports, key=lambda p: (p.y, p.x))
    label = wire_display_label(ordered, net)

    row_plans, singletons = _plan_hub_rows(ordered, obstacles, ctx, net)
    state = _HubRouteState(
        net=net,
        bus_x=bus_x,
        obstacles=obstacles,
        ctx=ctx,
        nodes_by_id={n.node_id: n for n in obstacles},
        row_wires=[],
        row_wire_by_plan={},
        tap_wires=[],
        tap_ys=[],
        row_spans=[],
    )
    min_row_y = min((plan.y_row for plan in row_plans), default=float("inf"))
    upstream_singletons = [item for item in singletons if item[1].y < min_row_y - WIRE_EPS]
    downstream_singletons = [item for item in singletons if item[1].y >= min_row_y - WIRE_EPS]

    _emit_hub_row_wires(state, row_plans)
    for _group, port in upstream_singletons:
        state.append_singleton_tap(port)
    _connect_row_plans(state, row_plans)
    for _group, port in downstream_singletons:
        state.append_singleton_tap(port)

    return _assemble_hub_wires(
        label,
        net,
        bus_x,
        ctx,
        state.row_wires,
        state.tap_wires,
        state.tap_ys,
    )
