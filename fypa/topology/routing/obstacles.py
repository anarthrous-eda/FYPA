"""Obstacle detours and segment clearance checks."""

from __future__ import annotations

from fypa.topology.constants import GND_NET, MIN_PARALLEL_GAP, OBSTACLE_CLEAR, WIRE_EPS
from fypa.topology.geometry import (
    horizontal_crosses_node,
    vertical_crosses_node,
)
from fypa.topology.placement import port_stub_x
from fypa.topology.routing.context import RoutingContext
from fypa.topology.types import TopologyNode, TopologyPort


def detour_y_for_horizontal(
    y_nominal: float,
    x_lo: float,
    x_hi: float,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> float | None:
    blocked_bottom = y_nominal
    any_block = False
    for node in obstacles:
        if node.node_id in skip:
            continue
        if horizontal_crosses_node(node, y_nominal, x_lo, x_hi):
            any_block = True
            blocked_bottom = max(blocked_bottom, node.y + node.height)
    if not any_block:
        return None
    return blocked_bottom + OBSTACLE_CLEAR


def obstacle_detour_y(
    ctx: RoutingContext,
    y_nominal: float,
    x_lo: float,
    x_hi: float,
    obstacles: list[TopologyNode],
    skip_node_ids: set[str],
    net: str | None = None,
) -> float:
    """Return a Y that clears obstacles and reserved horizontal bands."""
    y = y_nominal
    detour = detour_y_for_horizontal(y_nominal, x_lo, x_hi, obstacles, skip_node_ids)
    if detour is not None:
        y = detour
    lo, hi = min(x_lo, x_hi), max(x_lo, x_hi)
    for _ in range(len(ctx.horizontal_bands) + 1):
        blocked = False
        for by, blo, bhi, bnet in ctx.horizontal_bands:
            if net is not None and bnet == net:
                continue
            if (abs(by - y) < WIRE_EPS
                    and hi > blo + WIRE_EPS
                    and lo < bhi + WIRE_EPS):
                y = by + MIN_PARALLEL_GAP
                blocked = True
                break
            if (abs(by - y) < MIN_PARALLEL_GAP - WIRE_EPS
                    and hi > blo + WIRE_EPS
                    and lo < bhi + WIRE_EPS):
                y = by + MIN_PARALLEL_GAP
                blocked = True
                break
        if not blocked:
            break
    detour2 = detour_y_for_horizontal(y, lo, hi, obstacles, skip_node_ids)
    if detour2 is not None and detour2 > y + WIRE_EPS:
        y = detour2
    return y


def horizontal_segment_clear(
    y: float,
    x_lo: float,
    x_hi: float,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> bool:
    for node in obstacles:
        if node.node_id in skip:
            continue
        if horizontal_crosses_node(node, y, x_lo, x_hi):
            return False
    return True


def trunk_vertical_clear(
    x: float,
    y_lo: float,
    y_hi: float,
    obstacles: list[TopologyNode],
    skip: set[str],
) -> bool:
    lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
    for node in obstacles:
        if node.node_id in skip:
            continue
        if vertical_crosses_node(node, x, lo, hi):
            return False
    return True


def gnd_drop_x(
    port: TopologyPort,
    bus_y: float,
    obstacles: list[TopologyNode],
) -> float:
    """X for a GND drop whose vertical run to ``bus_y`` clears foreign nodes."""
    x = port_stub_x(port)
    outward = 1.0 if port.side == "right" else -1.0
    y_lo, y_hi = min(port.y, bus_y), max(port.y, bus_y)
    skip = {port.node_id}

    for node in obstacles:
        if node.node_id in skip:
            continue
        if vertical_crosses_node(node, x, y_lo, y_hi):
            nx, _ny, nw, _nh = node.bounds
            if outward > 0:
                x = max(x, nx + nw + OBSTACLE_CLEAR)
            else:
                x = min(x, nx - OBSTACLE_CLEAR)
    h_lo, h_hi = min(port.x, x), max(port.x, x)
    stub_y = port.y
    detour = detour_y_for_horizontal(stub_y, h_lo, h_hi, obstacles, skip)
    if detour is not None:
        pass  # horizontal stub Y detour handled in tap path via obstacle_detour_y
    for node in obstacles:
        if node.node_id in skip:
            continue
        if horizontal_crosses_node(node, port.y, h_lo, h_hi):
            nx, _ny, nw, _nh = node.bounds
            if outward > 0:
                x = max(x, nx + nw + OBSTACLE_CLEAR)
            else:
                x = min(x, nx - OBSTACLE_CLEAR)
    return x


def foreign_vertical_covers_y(
    ctx: RoutingContext,
    x: float,
    y: float,
    net: str,
) -> bool:
    for vx, vy_lo, vy_hi, vnet in ctx.vertical_bands:
        if vnet == net or abs(vx - x) >= WIRE_EPS:
            continue
        if vy_lo - WIRE_EPS <= y <= vy_hi + WIRE_EPS:
            return True
    return False
