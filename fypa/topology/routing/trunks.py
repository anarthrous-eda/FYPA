"""Foreign hub trunk detection and gutter transit-row routing."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, OBSTACLE_CLEAR, WIRE_EPS
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.obstacles import horizontal_segment_clear
from fypa.topology.types import TopologyNode, TopologyPort



def hub_trunks_blocking_horizontal(
    ctx: RoutingContext,
    y: float,
    x_lo: float,
    x_hi: float,
    net: str,
    hub_trunk_nets: frozenset[str],
) -> list[tuple[float, float, float]]:
    """Hub trunk columns whose vertical span contains ``y`` inside ``x_lo..x_hi``."""
    if not hub_trunk_nets:
        return []
    lo, hi = min(x_lo, x_hi), max(x_lo, x_hi)
    trunks: list[tuple[float, float, float]] = []
    for vx, vy_lo, vy_hi, vnet in ctx.vertical_bands:
        if vnet == net or vnet not in hub_trunk_nets:
            continue
        if vy_lo > y + WIRE_EPS or vy_hi < y - WIRE_EPS:
            continue
        if lo + WIRE_EPS < vx < hi - WIRE_EPS:
            trunks.append((vx, vy_lo, vy_hi))
    return trunks


def _foreign_horizontal_blocks_row(
    ctx: RoutingContext,
    y: float,
    x_lo: float,
    x_hi: float,
    net: str,
) -> bool:
    lo, hi = min(x_lo, x_hi), max(x_lo, x_hi)
    for by, blo, bhi, bnet in ctx.horizontal_bands:
        if bnet == net or abs(by - y) > WIRE_EPS:
            continue
        if hi <= blo + WIRE_EPS or lo >= bhi - WIRE_EPS:
            continue
        return True
    return False


def _vertical_crosses_foreign_horizontals(
    ctx: RoutingContext,
    x: float,
    y_lo: float,
    y_hi: float,
    net: str,
) -> bool:
    span_lo, span_hi = min(y_lo, y_hi), max(y_lo, y_hi)
    for by, blo, bhi, bnet in ctx.horizontal_bands:
        if bnet == net:
            continue
        if not (blo + WIRE_EPS < x < bhi - WIRE_EPS):
            continue
        if span_lo <= by + WIRE_EPS and span_hi >= by - WIRE_EPS:
            return True
    return False



def _too_close_to_foreign_vertical(
    ctx: RoutingContext,
    x: float,
    net: str,
) -> bool:
    for vx, _vy_lo, _vy_hi, vnet in ctx.vertical_bands:
        if vnet == net:
            continue
        if abs(vx - x) < MIN_PARALLEL_GAP - WIRE_EPS:
            return True
    return False


def _pick_vertical_column(
    col_x: float,
    s_stub: float,
    e_stub: float,
    y_lo: float,
    y_hi: float,
    ctx: RoutingContext,
    net: str,
) -> float:
    """Choose a column for a vertical leg that avoids foreign crossings and min gap."""
    west = min(col_x, s_stub)
    ordered = (west, col_x, e_stub)
    clear = [
        x
        for x in ordered
        if not _vertical_crosses_foreign_horizontals(ctx, x, y_lo, y_hi, net)
    ]
    if not clear:
        return west
    for x in ordered:
        if x not in clear:
            continue
        if not _too_close_to_foreign_vertical(ctx, x, net):
            return x
    return clear[0]


def gutter_transit_y(
    y_port: float,
    trunks: list[tuple[float, float, float]],
    x_lo: float,
    x_hi: float,
    obstacles: list[TopologyNode],
    skip: set[str],
    ctx: RoutingContext,
    net: str,
    *,
    transit_lane: int = 0,
) -> float:
    """Pick a clear row below or above all blocking hub trunk spans."""
    y_min = min(vy_lo for _vx, vy_lo, _vy_hi in trunks)
    y_max = max(vy_hi for _vx, _vy_lo, vy_hi in trunks)
    lane_gap = transit_lane * MIN_PARALLEL_GAP
    y_below = y_max + OBSTACLE_CLEAR + lane_gap
    y_above = y_min - OBSTACLE_CLEAR - lane_gap
    lo, hi = min(x_lo, x_hi), max(x_lo, x_hi)

    def _clear(y: float) -> bool:
        if not horizontal_segment_clear(y, lo, hi, obstacles, skip):
            return False
        return not _foreign_horizontal_blocks_row(ctx, y, lo, hi, net)

    while not _clear(y_below):
        y_below += MIN_PARALLEL_GAP
    while not _clear(y_above):
        y_above -= MIN_PARALLEL_GAP

    below_ok = _clear(y_below)
    above_ok = _clear(y_above)
    if below_ok and above_ok:
        return y_below if y_port >= (y_min + y_max) / 2 else y_above
    if below_ok:
        return y_below
    if above_ok:
        return y_above
    return y_below if abs(y_below - y_port) <= abs(y_above - y_port) else y_above


def _pick_transit_down_column(
    col_x: float,
    s_stub: float,
    y_lo: float,
    y_hi: float,
    ctx: RoutingContext,
    net: str,
) -> float:
    """West stub column for a transit vertical."""
    west = min(col_x, s_stub)
    if not _vertical_crosses_foreign_horizontals(ctx, west, y_lo, y_hi, net):
        return west
    if not _vertical_crosses_foreign_horizontals(ctx, col_x, y_lo, y_hi, net):
        return col_x
    return _pick_vertical_column(col_x, s_stub, col_x, y_lo, y_hi, ctx, net)


def _pick_transit_up_column(
    e_stub: float,
    s_stub: float,
    col_x: float,
    y_lo: float,
    y_hi: float,
    ctx: RoutingContext,
    net: str,
) -> float:
    """East stub column for the return transit vertical."""
    if not _vertical_crosses_foreign_horizontals(ctx, e_stub, y_lo, y_hi, net):
        return e_stub
    return _pick_vertical_column(e_stub, s_stub, col_x, y_lo, y_hi, ctx, net)


def _transit_vertical_columns(
    col_x: float,
    s_stub: float,
    e_stub: float,
    y_row: float,
    transit_y: float,
    end_y: float,
    ctx: RoutingContext,
    net: str,
) -> tuple[float, float]:
    """Pick west/east columns for transit verticals."""
    y_lo, y_hi = min(y_row, transit_y), max(y_row, transit_y)
    down_col = _pick_transit_down_column(col_x, s_stub, y_lo, y_hi, ctx, net)
    up_lo, up_hi = min(transit_y, end_y), max(transit_y, end_y)
    up_col = _pick_transit_up_column(e_stub, s_stub, col_x, up_lo, up_hi, ctx, net)
    if up_col == down_col:
        up_col = e_stub
    return down_col, up_col


def _finish_without_transit(
    prefix: str,
    col_x: float,
    s_stub: float,
    e_stub: float,
    y_row: float,
    end: TopologyPort,
    end_leg: str,
    ctx: RoutingContext,
    net: str,
    *,
    finish_row: float | None = None,
) -> str:
    """Direct stub approach without a hub-trunk transit jog."""
    if abs(y_row - end.y) < WIRE_EPS:
        if abs(col_x - e_stub) > WIRE_EPS:
            prefix = f"{prefix} H {e_stub:.1f}"
        return f"{prefix}{end_leg}"

    via = finish_row if finish_row is not None else end.y
    if abs(y_row - via) > WIRE_EPS:
        prefix = f"{prefix} V {via:.1f}"
        ctx.reserve_vertical(col_x, min(y_row, via), max(y_row, via), net)
    if abs(col_x - e_stub) > WIRE_EPS:
        prefix = f"{prefix} H {e_stub:.1f}"
    if abs(via - end.y) > WIRE_EPS:
        prefix = f"{prefix} V {end.y:.1f}"
        ctx.reserve_vertical(e_stub, min(via, end.y), max(via, end.y), net)
    return f"{prefix}{end_leg}"


def _transit_finish_to_port(
    prefix: str,
    up_col: float,
    e_stub: float,
    transit_y: float,
    end: TopologyPort,
    end_leg: str,
    ctx: RoutingContext,
    net: str,
    *,
    finish_row: float | None = None,
) -> str:
    """Complete a transit jog at the destination stub column."""
    row = finish_row if finish_row is not None else end.y
    if abs(transit_y - row) > WIRE_EPS:
        prefix = f"{prefix} V {row:.1f}"
        ctx.reserve_vertical(up_col, min(transit_y, row), max(transit_y, row), net)
    if abs(up_col - e_stub) > WIRE_EPS:
        prefix = f"{prefix} H {e_stub:.1f}"
    if finish_row is not None and abs(finish_row - end.y) > WIRE_EPS:
        prefix = f"{prefix} V {end.y:.1f}"
        ctx.reserve_vertical(e_stub, min(finish_row, end.y), max(finish_row, end.y), net)
    return f"{prefix}{end_leg}"


def gutter_finish_at_stub(
    prefix: str,
    col_x: float,
    s_stub: float,
    e_stub: float,
    y_row: float,
    end: TopologyPort,
    end_leg: str,
    obstacles: list[TopologyNode],
    skip: set[str],
    *,
    ctx: RoutingContext,
    net: str,
    hub_trunk_nets: frozenset[str],
    transit_lane: int = 0,
    finish_row: float | None = None,
) -> str:
    """Reach the destination stub, detouring via a gutter transit row when needed."""
    lo, hi = min(col_x, s_stub, e_stub), max(col_x, s_stub, e_stub)
    trunks = hub_trunks_blocking_horizontal(ctx, y_row, lo, hi, net, hub_trunk_nets)
    if not trunks:
        return _finish_without_transit(
            prefix,
            col_x,
            s_stub,
            e_stub,
            y_row,
            end,
            end_leg,
            ctx,
            net,
            finish_row=finish_row,
        )

    transit_y = gutter_transit_y(
        y_row,
        trunks,
        lo,
        hi,
        obstacles,
        skip,
        ctx,
        net,
        transit_lane=transit_lane,
    )
    port_y = finish_row if finish_row is not None else end.y
    down_col, up_col = _transit_vertical_columns(
        col_x,
        s_stub,
        e_stub,
        y_row,
        transit_y,
        port_y,
        ctx,
        net,
    )
    if abs(col_x - down_col) > WIRE_EPS:
        prefix = f"{prefix} H {down_col:.1f}"
    if abs(y_row - transit_y) > WIRE_EPS:
        prefix = f"{prefix} V {transit_y:.1f}"
        ctx.reserve_vertical(down_col, min(y_row, transit_y), max(y_row, transit_y), net)
    if abs(down_col - up_col) > WIRE_EPS:
        prefix = f"{prefix} H {up_col:.1f}"
        ctx.reserve_horizontal(transit_y, min(down_col, up_col), max(down_col, up_col), net)
    return _transit_finish_to_port(
        prefix,
        up_col,
        e_stub,
        transit_y,
        end,
        end_leg,
        ctx,
        net,
        finish_row=finish_row,
    )
