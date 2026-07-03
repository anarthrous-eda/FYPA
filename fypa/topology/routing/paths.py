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


def _start_prefix_at_row(
    start: TopologyPort,
    bus_x: float,
    y: float,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> tuple[str, float]:
    """Prefix at row ``y``; returns ``(prefix, column_x)`` after any horizontal legs.

    Never draws port→bus horizontal through foreign symbol bodies.
    """
    s_stub = port_stub_x(start)
    direct_lo, direct_hi = min(start.x, bus_x), max(start.x, bus_x)
    if horizontal_segment_clear(y, direct_lo, direct_hi, obstacles, skip):
        leg, _, _ = path_start_to_bus_x(start, bus_x)
        return leg, bus_x
    stub_leg, _, _ = path_from_port_stub(start)
    stub_lo, stub_hi = min(s_stub, bus_x), max(s_stub, bus_x)
    if horizontal_segment_clear(y, stub_lo, stub_hi, obstacles, skip):
        return f"{stub_leg} H {bus_x:.1f}", bus_x
    return stub_leg, s_stub


def _append_bus_column_at_row(
    start_prefix: str,
    col_x: float,
    start: TopologyPort,
    bus_x: float,
    y: float,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> tuple[str, float]:
    """Extend ``start_prefix`` onto ``bus_x`` at row ``y`` when clear."""
    if abs(col_x - bus_x) < WIRE_EPS:
        return start_prefix, col_x
    lo, hi = min(col_x, bus_x), max(col_x, bus_x)
    if horizontal_segment_clear(y, lo, hi, obstacles, skip):
        return f"{start_prefix} H {bus_x:.1f}", bus_x
    return start_prefix, col_x


def _horizontal_chain_at_row(
    prefix: str,
    col_x: float,
    targets: list[float],
    y: float,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> tuple[str, float]:
    """Append cleared horizontals toward each target in order."""
    x = col_x
    for target in targets:
        if abs(x - target) < WIRE_EPS:
            continue
        lo, hi = min(x, target), max(x, target)
        if horizontal_segment_clear(y, lo, hi, obstacles, skip):
            prefix = f"{prefix} H {target:.1f}"
            x = target
    return prefix, x


def path_start_to_bus_x(port: TopologyPort, bus_x: float) -> tuple[str, float, float]:
    """First leg: port to the planned vertical bus column (no stub detour)."""
    return (
        f"M {port.x:.1f},{port.y:.1f} H {bus_x:.1f}",
        bus_x,
        port.y,
    )


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


def _dest_leg_from_row(
    e_stub: float,
    y_row: float,
    end: TopologyPort,
    end_leg: str,
) -> str:
    """Horizontal to the destination stub column, then down/up to the port row."""
    if abs(y_row - end.y) > WIRE_EPS:
        return f" H {e_stub:.1f} V {end.y:.1f}{end_leg}"
    return f" H {e_stub:.1f}{end_leg}"


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
    end_leg = path_into_port(end)
    obs = obstacles or []
    skip = {start.node_id, end.node_id}
    ctx = ctx or RoutingContext()

    if abs(start.x - end.x) < WIRE_EPS and abs(start.y - end.y) < WIRE_EPS:
        # Degenerate: both ports occupy the same point. Emit a single stub rather
        # than a self-overlapping stub-out/stub-back/stub-out double-back.
        return simplify_wire_path(path_from_port_stub(start)[0])

    if abs(start.y - end.y) < WIRE_EPS:
        y = start.y
        x_lo, x_hi = min(s_stub, bus_x, e_stub, start.x), max(s_stub, bus_x, e_stub, start.x)
        y_clear = obstacle_detour_y(ctx, y, x_lo, x_hi, obs, skip, net)
        start_prefix, col_x = _start_prefix_at_row(start, bus_x, y, obs, skip)
        if abs(y_clear - y) > WIRE_EPS:
            detour = f"{start_prefix} V {y_clear:.1f}"
            detour, col_at = _append_bus_column_at_row(
                detour, col_x, start, bus_x, y_clear, obs, skip
            )
            detour, col_at = _horizontal_chain_at_row(detour, col_at, [e_stub], y_clear, obs, skip)
            # The detour row (y_clear) differs from the shared port row (y == end.y);
            # drop back down/up to the destination port's row before the final leg,
            # otherwise the wire terminates on the detour row and leaves the port open.
            path = f"{detour} V {end.y:.1f}{end_leg}"
            ctx.reserve_vertical(col_x, min(y, y_clear), max(y, y_clear), net)
            if abs(col_at - col_x) > WIRE_EPS:
                ctx.reserve_horizontal(y_clear, min(col_x, col_at), max(col_x, col_at), net)
            ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
            ctx.reserve_vertical(col_at, min(y_clear, end.y), max(y_clear, end.y), net)
            return simplify_wire_path(path)
        detour, col_at = _horizontal_chain_at_row(start_prefix, col_x, [e_stub], y, obs, skip)
        path = f"{detour}{end_leg}"
        ctx.reserve_horizontal(y, x_lo, x_hi, net)
        return simplify_wire_path(path)

    x_lo, x_hi = min(s_stub, bus_x), max(s_stub, bus_x)
    y_clear = obstacle_detour_y(ctx, start.y, x_lo, x_hi, obs, skip, net)
    x_approach_lo, x_approach_hi = min(bus_x, e_stub), max(bus_x, e_stub)
    y_end_clear = obstacle_detour_y(ctx, end.y, x_approach_lo, x_approach_hi, obs, skip, net)
    start_prefix, col_x = _start_prefix_at_row(start, bus_x, start.y, obs, skip)
    if abs(y_clear - start.y) > WIRE_EPS:
        detour = f"{start_prefix} V {y_clear:.1f}"
        detour, col_at = _append_bus_column_at_row(detour, col_x, start, bus_x, y_clear, obs, skip)
        path = (
            f"{detour} V {y_end_clear:.1f}{_dest_leg_from_row(e_stub, y_end_clear, end, end_leg)}"
        )
        ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
        ctx.reserve_horizontal(y_end_clear, min(col_at, e_stub), max(col_at, e_stub), net)
        ctx.reserve_vertical(col_x, min(start.y, y_clear), max(start.y, y_clear), net)
        if abs(y_end_clear - y_clear) > WIRE_EPS:
            ctx.reserve_vertical(
                col_at,
                min(y_clear, y_end_clear),
                max(y_clear, y_end_clear),
                net,
            )
        return simplify_wire_path(path)
    if abs(y_end_clear - end.y) > WIRE_EPS:
        approach, col_at = _append_bus_column_at_row(
            start_prefix,
            col_x,
            start,
            bus_x,
            start.y,
            obs,
            skip,
        )
        path = (
            f"{approach} V {y_end_clear:.1f}{_dest_leg_from_row(e_stub, y_end_clear, end, end_leg)}"
        )
        ctx.reserve_horizontal(start.y, x_lo, x_hi, net)
        ctx.reserve_horizontal(y_end_clear, min(col_at, e_stub), max(col_at, e_stub), net)
        if abs(y_end_clear - end.y) > WIRE_EPS:
            ctx.reserve_vertical(e_stub, min(y_end_clear, end.y), max(y_end_clear, end.y), net)
        ctx.reserve_vertical(
            col_at,
            min(start.y, y_end_clear),
            max(start.y, y_end_clear),
            net,
        )
        return simplify_wire_path(path)
    approach, col_at = _append_bus_column_at_row(
        start_prefix,
        col_x,
        start,
        bus_x,
        start.y,
        obs,
        skip,
    )
    path = f"{approach} V {end.y:.1f} H {e_stub:.1f}{end_leg}"
    ctx.reserve_horizontal(start.y, x_lo, x_hi, net)
    ctx.reserve_vertical(col_at, min(start.y, end.y), max(start.y, end.y), net)
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


def hub_row_stub_columns(group: list[TopologyPort]) -> tuple[float, float]:
    """Return ``(row_lo, row_hi)`` stub columns for a hub row group."""
    stubs = [port_stub_x(port) for port in group]
    return min(stubs), max(stubs)


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


def hub_tap_feed_column(
    ctx: RoutingContext,
    port: TopologyPort,
    bus_x: float,
    obstacles: list[TopologyNode],
    net: str,
) -> float:
    """Pick the trunk column for an eastward hub tap onto ``port``.

    Prefer the easternmost same-net vertical west of the planned bus column
    when its span covers ``port.y`` and a horizontal feed to the stub is clear.
    Otherwise use ``bus_x``.
    """
    stub = port_stub_x(port)
    y = port.y
    skip = {port.node_id}
    west_candidates: list[float] = []
    for vx, vy_lo, vy_hi, vnet in ctx.vertical_bands:
        if vnet != net:
            continue
        if vy_lo > y + WIRE_EPS or vy_hi < y - WIRE_EPS:
            continue
        if vx >= stub - WIRE_EPS:
            continue
        lo, hi = min(vx, stub), max(vx, stub)
        if not horizontal_segment_clear(y, lo, hi, obstacles, skip):
            continue
        if _foreign_horizontal_blocks_row(ctx, y, lo, hi, net):
            continue
        if vx < bus_x - WIRE_EPS:
            west_candidates.append(vx)
    if west_candidates:
        return max(west_candidates)
    return bus_x


def hub_tap_path_from_bus(
    bus_x: float,
    port: TopologyPort,
    obstacles: list[TopologyNode],
    ctx: RoutingContext,
    net: str,
) -> tuple[str, float]:
    """Route from a hub trunk column eastward into a downstream port."""
    stub = port_stub_x(port)
    y = port.y
    feed_x = hub_tap_feed_column(ctx, port, bus_x, obstacles, net)
    end_leg = path_into_port(port)
    x_lo, x_hi = min(feed_x, stub), max(feed_x, stub)
    y_clear = obstacle_detour_y(ctx, y, x_lo, x_hi, obstacles, set(), net)
    if abs(y_clear - y) > WIRE_EPS:
        ctx.reserve_vertical(feed_x, min(y, y_clear), max(y, y_clear), net)
        ctx.reserve_horizontal(y_clear, x_lo, x_hi, net)
        path = f"M {feed_x:.1f},{y_clear:.1f} H {stub:.1f} V {y:.1f}{end_leg}"
        return simplify_wire_path(path), y_clear
    ctx.reserve_horizontal(y, x_lo, x_hi, net)
    path = f"M {feed_x:.1f},{y:.1f} H {stub:.1f}{end_leg}"
    return simplify_wire_path(path), y


def _foreign_vertical_blocks_column(
    ctx: RoutingContext,
    x: float,
    y_lo: float,
    y_hi: float,
    net: str,
) -> bool:
    """True when a foreign reserved vertical shares this column across ``y_lo..y_hi``."""
    from fypa.topology.validate.util import intervals_overlap, parallel_corridors_too_close

    for vx, vy_lo, vy_hi, vnet in ctx.vertical_bands:
        if vnet == net or not parallel_corridors_too_close(x, vx):
            continue
        if intervals_overlap(y_lo, y_hi, vy_lo, vy_hi):
            return True
    return False


def _foreign_horizontal_blocks_row(
    ctx: RoutingContext,
    y: float,
    x_lo: float,
    x_hi: float,
    net: str,
) -> bool:
    """True when a foreign reserved horizontal occupies the same row across ``x_lo..x_hi``."""
    lo, hi = min(x_lo, x_hi), max(x_lo, x_hi)
    for by, blo, bhi, bnet in ctx.horizontal_bands:
        if bnet == net or abs(by - y) > WIRE_EPS:
            continue
        if hi <= blo + WIRE_EPS or lo >= bhi - WIRE_EPS:
            continue
        return True
    return False


def hub_tap_vertical_to_row(
    port: TopologyPort,
    row_y: float,
    *,
    bus_x: float | None = None,
    merge_at_port: bool = False,
    obstacles: list[TopologyNode] | None = None,
    skip: set[str] | None = None,
    ctx: RoutingContext | None = None,
    net: str | None = None,
) -> tuple[str, float]:
    """Drop from a port onto an existing hub row.

    When ``bus_x`` is set, route onto the trunk column before the vertical.
    """
    if merge_at_port:
        if ctx is not None and net is not None:
            ctx.reserve_vertical(
                port.x,
                min(port.y, row_y),
                max(port.y, row_y),
                net,
            )
        return (
            simplify_wire_path(f"M {port.x:.1f},{port.y:.1f} V {row_y:.1f}"),
            row_y,
        )
    start_leg, col_x, _ = path_from_port_stub(port)
    if bus_x is not None and abs(col_x - bus_x) > WIRE_EPS:
        obs = obstacles or []
        sk = skip or {port.node_id}
        lo, hi = min(col_x, bus_x), max(col_x, bus_x)
        if horizontal_segment_clear(port.y, lo, hi, obs, sk):
            if ctx is not None and net is not None:
                ctx.reserve_vertical(
                    bus_x,
                    min(port.y, row_y),
                    max(port.y, row_y),
                    net,
                )
                ctx.reserve_horizontal(port.y, lo, hi, net)
            return (
                simplify_wire_path(f"{start_leg} H {bus_x:.1f} V {row_y:.1f}"),
                row_y,
            )
    if ctx is not None and net is not None:
        ctx.reserve_vertical(
            col_x,
            min(port.y, row_y),
            max(port.y, row_y),
            net,
        )
    return simplify_wire_path(f"{start_leg} V {row_y:.1f}"), row_y


def hub_row_tap_via_escape_column(
    port: TopologyPort,
    row_y: float,
    col_x: float,
    start_leg: str,
    ctx: RoutingContext,
    net: str,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> tuple[str, float] | None:
    """Join a hub row on a nearby clear column when the stub column is reserved."""
    y_lo, y_hi = min(port.y, row_y), max(port.y, row_y)
    outward = -1.0 if col_x <= port.x else 1.0
    for step in range(1, 8):
        escape_x = round(col_x + outward * MIN_PARALLEL_GAP * step, 1)
        if _foreign_vertical_blocks_column(ctx, escape_x, y_lo, y_hi, net):
            continue
        lo, hi = min(col_x, escape_x), max(col_x, escape_x)
        if not horizontal_segment_clear(port.y, lo, hi, obstacles, skip):
            continue
        if _foreign_horizontal_blocks_row(ctx, port.y, lo, hi, net):
            continue
        ctx.reserve_vertical(escape_x, y_lo, y_hi, net)
        ctx.reserve_horizontal(port.y, lo, hi, net)
        return (
            simplify_wire_path(f"{start_leg} H {escape_x:.1f} V {row_y:.1f}"),
            row_y,
        )
    return None


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


def group_ports_by_row(ports: list[TopologyPort]) -> dict[float, list[TopologyPort]]:
    from collections import defaultdict

    rows: dict[float, list[TopologyPort]] = defaultdict(list)
    for port in ports:
        rows[round(port.y, 1)].append(port)
    return rows
