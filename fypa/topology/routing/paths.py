"""Wire path construction between ports."""

from __future__ import annotations

from fypa.topology.constants import (
    MIN_PARALLEL_GAP,
    PORT_WIRE_STUB,
    WIRE_EPS,
)
from fypa.topology.geometry import simplify_wire_path
from fypa.topology.placement import (
    port_stub_x,
    ports_share_column,
    stacked_routing_order,
)
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.obstacles import (
    foreign_vertical_covers_y,
    horizontal_segment_clear,
    obstacle_detour_y,
)
from fypa.topology.types import TopologyNode, TopologyPort


def outward_escape_stub_x(port: TopologyPort) -> float:
    if port.side == "left":
        return port.x + PORT_WIRE_STUB
    return port.x - PORT_WIRE_STUB


def away_from_symbol_x(port: TopologyPort, stub_x: float) -> float:
    if port.side == "left":
        return stub_x - MIN_PARALLEL_GAP
    return stub_x + MIN_PARALLEL_GAP


def toward_bus_x(x: float, bus_x: float) -> float:
    """Step from ``x`` one gap toward ``bus_x`` (schematic left → right)."""
    if bus_x >= x:
        return x + MIN_PARALLEL_GAP
    return x - MIN_PARALLEL_GAP


def _hub_horizontal_target_x(port: TopologyPort, bus_x: float) -> float:
    """First horizontal stop from a port toward a hub trunk (always the stub column)."""
    return port_stub_x(port)


def _opposite_column_gutter_pair(start: TopologyPort, end: TopologyPort) -> bool:
    if ports_share_column(start, end):
        return False
    return start.side != end.side


def _stub_column_gutter_path(
    start: TopologyPort,
    end: TopologyPort,
    *,
    obstacles: list[TopologyNode] | None,
    ctx: RoutingContext,
    net: str,
) -> str:
    """Gutter route via the source stub column, then horizontal at the destination row."""
    s_stub = port_stub_x(start)
    e_stub = port_stub_x(end)
    start_leg, _, _ = path_from_port_stub(start)
    end_leg = path_into_port(end)
    obs = obstacles or []
    skip = {start.node_id, end.node_id}
    x_lo, x_hi = min(s_stub, e_stub), max(s_stub, e_stub)
    y_clear = obstacle_detour_y(ctx, end.y, x_lo, x_hi, obs, skip, net)
    if abs(y_clear - end.y) > WIRE_EPS:
        ctx.reserve_vertical(
            s_stub,
            min(start.y, y_clear),
            max(start.y, y_clear),
            net,
        )
        ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
        path = f"{start_leg} V {y_clear:.1f} H {e_stub:.1f}{end_leg}"
        return simplify_wire_path(path)
    ctx.reserve_vertical(s_stub, min(start.y, end.y), max(start.y, end.y), net)
    ctx.reserve_horizontal(end.y, x_lo, x_hi, net)
    path = f"{start_leg} V {end.y:.1f} H {e_stub:.1f}{end_leg}"
    return simplify_wire_path(path)


def path_from_port_stub(port: TopologyPort) -> tuple[str, float, float]:
    stub = port_stub_x(port)
    return (
        f"M {port.x:.1f},{port.y:.1f} H {stub:.1f}",
        stub,
        port.y,
    )


def path_into_port(port: TopologyPort) -> str:
    stub = port_stub_x(port)
    if abs(stub - port.x) < WIRE_EPS:
        return ""
    return f" H {port.x:.1f}"


def two_port_path(
    start: TopologyPort,
    end: TopologyPort,
    *,
    bus_x: float,
    net: str,
    obstacles: list[TopologyNode] | None = None,
    ctx: RoutingContext | None = None,
) -> str:
    """Route between two ports via a vertical segment at ``bus_x``."""
    s_stub = port_stub_x(start)
    e_stub = port_stub_x(end)
    start_leg, _, _ = path_from_port_stub(start)
    end_leg = path_into_port(end)
    obs = obstacles or []
    skip = {start.node_id, end.node_id}
    ctx = ctx or RoutingContext()

    if _opposite_column_gutter_pair(start, end) and abs(start.y - end.y) > WIRE_EPS:
        return _stub_column_gutter_path(
            start,
            end,
            obstacles=obs,
            ctx=ctx,
            net=net,
        )

    if abs(start.y - end.y) < WIRE_EPS:
        y = start.y
        x_lo, x_hi = min(s_stub, bus_x, e_stub), max(s_stub, bus_x, e_stub)
        y_clear = obstacle_detour_y(ctx, y, x_lo, x_hi, obs, skip, net)
        if abs(y_clear - y) > WIRE_EPS:
            h_lo, h_hi = min(s_stub, bus_x), max(s_stub, bus_x)
            if horizontal_segment_clear(y, h_lo, h_hi, obs, skip):
                path = f"{start_leg} H {bus_x:.1f} V {y_clear:.1f} H {e_stub:.1f}{end_leg}"
                ctx.reserve_vertical(bus_x, min(y, y_clear), max(y, y_clear), net)
            else:
                path = f"{start_leg} V {y_clear:.1f} H {bus_x:.1f} H {e_stub:.1f}{end_leg}"
                ctx.reserve_vertical(s_stub, min(y, y_clear), max(y, y_clear), net)
            ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
            return simplify_wire_path(path)
        chain = sorted([s_stub, bus_x, e_stub])
        horiz = " ".join(f"H {x:.1f}" for x in chain)
        path = f"{start_leg} {horiz}{end_leg}"
        ctx.reserve_horizontal(y, x_lo, x_hi, net)
        return simplify_wire_path(path)

    x_lo, x_hi = min(s_stub, bus_x), max(s_stub, bus_x)
    y_clear = obstacle_detour_y(ctx, start.y, x_lo, x_hi, obs, skip, net)
    y_v_lo = min(start.y, end.y, y_clear)
    y_v_hi = max(start.y, end.y, y_clear)
    x_end_lo, x_end_hi = min(e_stub, end.x), max(e_stub, end.x)
    y_end_clear = obstacle_detour_y(ctx, end.y, x_end_lo, x_end_hi, obs, skip, net)
    if abs(y_clear - start.y) > WIRE_EPS:
        h_lo, h_hi = min(s_stub, bus_x), max(s_stub, bus_x)
        if horizontal_segment_clear(start.y, h_lo, h_hi, obs, skip):
            path = f"{start_leg} H {bus_x:.1f} V {y_end_clear:.1f} H {e_stub:.1f}{end_leg}"
            ctx.reserve_horizontal(y_end_clear, min(bus_x, e_stub), max(bus_x, e_stub), net)
            ctx.reserve_vertical(
                bus_x,
                min(start.y, end.y, y_clear, y_end_clear),
                max(start.y, end.y, y_clear, y_end_clear),
                net,
            )
        else:
            path = (
                f"{start_leg} V {y_clear:.1f} "
                f"H {bus_x:.1f} V {y_end_clear:.1f} H {e_stub:.1f}{end_leg}"
            )
            ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
            ctx.reserve_horizontal(y_end_clear, min(bus_x, e_stub), max(bus_x, e_stub), net)
            ctx.reserve_vertical(s_stub, min(start.y, y_clear), max(start.y, y_clear), net)
            ctx.reserve_vertical(
                bus_x,
                min(start.y, end.y, y_clear, y_end_clear),
                max(start.y, end.y, y_clear, y_end_clear),
                net,
            )
        return simplify_wire_path(path)
    if abs(y_end_clear - end.y) > WIRE_EPS:
        path = f"{start_leg} H {bus_x:.1f} V {y_end_clear:.1f} H {e_stub:.1f}{end_leg}"
        ctx.reserve_horizontal(start.y, x_lo, x_hi, net)
        ctx.reserve_horizontal(y_end_clear, min(bus_x, e_stub), max(bus_x, e_stub), net)
        ctx.reserve_vertical(
            bus_x,
            min(start.y, end.y, y_end_clear),
            max(start.y, end.y, y_end_clear),
            net,
        )
        return simplify_wire_path(path)
    path = f"{start_leg} H {bus_x:.1f} V {end.y:.1f} H {e_stub:.1f}{end_leg}"
    ctx.reserve_horizontal(start.y, x_lo, x_hi, net)
    ctx.reserve_vertical(bus_x, y_v_lo, y_v_hi, net)
    return simplify_wire_path(path)


def stacked_wire_path(
    a: TopologyPort,
    b: TopologyPort,
    *,
    bus_x: float,
    obstacles: list[TopologyNode] | None = None,
    ctx: RoutingContext | None = None,
) -> str:
    start, end = stacked_routing_order(a, b)
    return two_port_path(
        start,
        end,
        bus_x=bus_x,
        net=a.net,
        obstacles=obstacles,
        ctx=ctx,
    )


def two_port_wire_path(
    a: TopologyPort,
    b: TopologyPort,
    *,
    bus_x: float,
    obstacles: list[TopologyNode] | None = None,
    ctx: RoutingContext | None = None,
) -> str:
    return two_port_path(
        a,
        b,
        bus_x=bus_x,
        net=a.net,
        obstacles=obstacles,
        ctx=ctx,
    )


def hub_row_path(group: list[TopologyPort], y: float) -> str:
    ordered = sorted(group, key=lambda p: p.x)
    left = ordered[0]
    xs: list[float] = [port_stub_x(port) for port in ordered]
    right = ordered[-1]
    if abs(right.x - port_stub_x(right)) > WIRE_EPS:
        xs.append(right.x)
    parts = [f"M {left.x:.1f},{y:.1f}"]
    for x in sorted(xs):
        parts.append(f"H {x:.1f}")
    return simplify_wire_path(" ".join(parts))


def hub_row_groups(
    row_ports: list[TopologyPort],
    obstacles: list[TopologyNode],
) -> list[list[TopologyPort]]:
    if not row_ports:
        return []
    ordered = sorted(row_ports, key=lambda p: port_stub_x(p))
    groups: list[list[TopologyPort]] = []
    current: list[TopologyPort] = [ordered[0]]
    for port in ordered[1:]:
        combined = current + [port]
        stubs = [port_stub_x(p) for p in combined]
        x_lo, x_hi = min(stubs), max(stubs)
        left = min(combined, key=lambda p: port_stub_x(p))
        right = max(combined, key=lambda p: port_stub_x(p))
        skip = {left.node_id, right.node_id}
        if horizontal_segment_clear(port.y, x_lo, x_hi, obstacles, skip):
            current.append(port)
        else:
            groups.append(current)
            current = [port]
    groups.append(current)
    return groups


def hub_tap_path_from_bus(
    bus_x: float,
    port: TopologyPort,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    net: str,
) -> tuple[str, float]:
    """Route from the hub trunk eastward into a downstream port (port right of bus)."""
    stub = port_stub_x(port)
    y = port.y
    end_leg = path_into_port(port)
    x_lo, x_hi = min(bus_x, stub), max(bus_x, stub)
    y_clear = obstacle_detour_y(ctx, y, x_lo, x_hi, obstacles, set(), net)
    if abs(y_clear - y) > WIRE_EPS:
        ctx.reserve_vertical(bus_x, min(y, y_clear), max(y, y_clear), net)
        ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
        path = f"M {bus_x:.1f},{y_clear:.1f} H {stub:.1f} V {y:.1f}{end_leg}"
        return simplify_wire_path(path), y_clear
    ctx.reserve_horizontal(y, x_lo, x_hi, net)
    path = f"M {bus_x:.1f},{y:.1f} H {stub:.1f}{end_leg}"
    return simplify_wire_path(path), y


def hub_tap_vertical_to_row(
    port: TopologyPort,
    row_y: float,
    *,
    merge_at_port: bool = False,
) -> tuple[str, float]:
    """Drop from a port onto an existing hub row (same stub column)."""
    if merge_at_port:
        return (
            simplify_wire_path(f"M {port.x:.1f},{port.y:.1f} V {row_y:.1f}"),
            row_y,
        )
    start_leg, _, _ = path_from_port_stub(port)
    return simplify_wire_path(f"{start_leg} V {row_y:.1f}"), row_y


def hub_tap_path(
    port: TopologyPort,
    bus_x: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    net: str,
) -> tuple[str, float]:
    attach = _hub_horizontal_target_x(port, bus_x)
    y = port.y
    x_lo, x_hi = min(attach, bus_x), max(attach, bus_x)
    y_clear = obstacle_detour_y(ctx, y, x_lo, x_hi, obstacles, set(), net)
    if abs(attach - port.x) < WIRE_EPS:
        start_leg = f"M {port.x:.1f},{y:.1f}"
    elif attach == bus_x and abs(bus_x - port.x) > WIRE_EPS:
        start_leg = f"M {port.x:.1f},{y:.1f} H {bus_x:.1f}"
    else:
        start_leg, attach, _ = path_from_port_stub(port)
    if abs(y_clear - y) > WIRE_EPS:
        ctx.reserve_vertical(attach, min(y, y_clear), max(y, y_clear), net)
        ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
        path = f"{start_leg} V {y_clear:.1f} H {bus_x:.1f}"
        return simplify_wire_path(path), y_clear
    if foreign_vertical_covers_y(ctx, attach, y, net):
        escape = outward_escape_stub_x(port)
        ctx.reserve_horizontal(
            y,
            min(port.x, escape),
            max(port.x, escape),
            net,
        )
        ctx.reserve_horizontal(
            y,
            min(escape, bus_x),
            max(escape, bus_x),
            net,
        )
        path = f"M {port.x:.1f},{y:.1f} H {escape:.1f} H {bus_x:.1f}"
        return simplify_wire_path(path), y
    ctx.reserve_horizontal(y, x_lo, x_hi, net)
    if abs(attach - bus_x) < WIRE_EPS:
        return simplify_wire_path(start_leg), y
    return simplify_wire_path(f"{start_leg} H {bus_x:.1f}"), y


def hub_edge_tap_path(
    y: float,
    edge_x: float,
    bus_x: float,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    net: str,
    *,
    skip: set[str],
    port: TopologyPort | None = None,
) -> tuple[str, float]:
    x_lo, x_hi = min(edge_x, bus_x), max(edge_x, bus_x)
    y_clear = obstacle_detour_y(ctx, y, x_lo, x_hi, obstacles, skip, net)
    if abs(y_clear - y) > WIRE_EPS:
        ctx.reserve_vertical(edge_x, min(y, y_clear), max(y, y_clear), net)
        ctx.reserve_horizontal(
            y_clear,
            min(edge_x, bus_x),
            max(edge_x, bus_x),
            net,
        )
        path = f"M {edge_x:.1f},{y:.1f} V {y_clear:.1f} H {bus_x:.1f}"
        return simplify_wire_path(path), y_clear
    ctx.reserve_horizontal(y, x_lo, x_hi, net)
    return simplify_wire_path(f"M {edge_x:.1f},{y:.1f} H {bus_x:.1f}"), y


def group_ports_by_row(ports: list[TopologyPort]) -> dict[float, list[TopologyPort]]:
    from collections import defaultdict

    rows: dict[float, list[TopologyPort]] = defaultdict(list)
    for port in ports:
        rows[round(port.y, 1)].append(port)
    return rows
