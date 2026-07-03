"""Vertical bus columns: strictly in layout column gaps (between symbol columns)."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement.bus_grid import allocate_bus_x
from fypa.topology.placement.pair_slots import nominal_gutter_bus_x
from fypa.topology.placement.ports import gutter_bus_x_bounds
from fypa.topology.types import TopologyNode

ColumnGap = tuple[float, float]


def column_gaps_from_nodes(nodes: list[TopologyNode]) -> list[ColumnGap]:
    """``(gap_lo, gap_hi)`` between adjacent symbol columns (exclusive of bodies)."""
    from fypa.topology.constants import NODE_W

    col_xs = sorted({round(n.x, 1) for n in nodes if n.role != "GND"})
    if len(col_xs) < 2:
        return []
    return [(col_xs[i] + NODE_W, col_xs[i + 1]) for i in range(len(col_xs) - 1)]


def gutter_vertical_corridors(
    channel_lo: float,
    channel_hi: float,
    column_gaps: list[ColumnGap],
) -> list[ColumnGap]:
    """Column gaps whose horizontal span overlaps the gutter stub channel."""
    lo, hi = min(channel_lo, channel_hi), max(channel_lo, channel_hi)
    corridors: list[ColumnGap] = []
    for gap_lo, gap_hi in column_gaps:
        inner_lo, inner_hi = gutter_bus_x_bounds([gap_lo, gap_hi])
        if inner_hi - inner_lo < MIN_PARALLEL_GAP - WIRE_EPS:
            continue
        # Test channel overlap against the inner (placeable) corridor, not the raw
        # gap: a gap that only grazes the channel within MIN_PARALLEL_GAP of its
        # edge has an inner corridor entirely outside the channel, and clamping
        # bus_x into it would push the bus that far off the stubs it serves.
        if inner_hi <= lo + WIRE_EPS or inner_lo >= hi - WIRE_EPS:
            continue
        corridors.append((inner_lo, inner_hi))
    return corridors


def bus_x_in_column_gaps(x: float, column_gaps: list[ColumnGap]) -> bool:
    for gap_lo, gap_hi in column_gaps:
        inner_lo, inner_hi = gutter_bus_x_bounds([gap_lo, gap_hi])
        if inner_lo - WIRE_EPS <= x <= inner_hi + WIRE_EPS:
            return True
    return False


def _all_gap_corridors(column_gaps: list[ColumnGap]) -> list[ColumnGap]:
    """Inner corridors for every column gap (no minimum width filter)."""
    corridors: list[ColumnGap] = []
    for gap_lo, gap_hi in column_gaps:
        inner_lo, inner_hi = gutter_bus_x_bounds([gap_lo, gap_hi])
        if inner_hi > inner_lo + WIRE_EPS:
            corridors.append((inner_lo, inner_hi))
    return corridors


def _column_gap_corridors(column_gaps: list[ColumnGap]) -> list[ColumnGap]:
    """Inner bus corridors for every layout column gap."""
    corridors: list[ColumnGap] = []
    for gap_lo, gap_hi in column_gaps:
        inner_lo, inner_hi = gutter_bus_x_bounds([gap_lo, gap_hi])
        if inner_hi - inner_lo >= MIN_PARALLEL_GAP - WIRE_EPS:
            corridors.append((inner_lo, inner_hi))
    return corridors


def resolve_gutter_corridor(
    channel_lo: float,
    channel_hi: float,
    column_gaps: list[ColumnGap],
    *,
    anchor_x: float,
    n_slots: int = 1,
) -> ColumnGap | None:
    """Pick the column-gap corridor for gutter bus placement."""
    corridors = gutter_vertical_corridors(channel_lo, channel_hi, column_gaps)
    if not corridors:
        corridors = _column_gap_corridors(column_gaps)
    if not corridors:
        return None
    corridor = _corridor_near_anchor(corridors, anchor_x)
    if n_slots > 1:
        wide_enough = [
            c for c in corridors if c[1] - c[0] >= (n_slots - 1) * MIN_PARALLEL_GAP - WIRE_EPS
        ]
        if wide_enough:
            corridor = _corridor_near_anchor(wide_enough, anchor_x)
    return corridor


def _corridor_near_anchor(
    corridors: list[ColumnGap],
    anchor_x: float,
) -> ColumnGap:
    def _dist(corridor: ColumnGap) -> float:
        lo, hi = corridor
        if lo <= anchor_x <= hi:
            return 0.0
        return min(abs(anchor_x - lo), abs(anchor_x - hi))

    return min(corridors, key=_dist)


def pick_gutter_bus_x(
    bus_slot: int,
    n_slots: int,
    channel_lo: float,
    channel_hi: float,
    column_gaps: list[ColumnGap],
    net: str,
    *,
    y_lo: float,
    y_hi: float,
    anchor_x: float,
    outward: float,
    reserved: list[tuple[float, float, float, str]],
    assigned_in_group: list[float] | None = None,
) -> float:
    """Place a gutter bus x inside a column gap corridor (never on a symbol column)."""
    corridor = resolve_gutter_corridor(
        channel_lo,
        channel_hi,
        column_gaps,
        anchor_x=anchor_x,
        n_slots=n_slots,
    )
    if corridor is None:
        if column_gaps:
            corridors = _all_gap_corridors(column_gaps)
            if corridors:
                bus_lo, bus_hi = _corridor_near_anchor(corridors, anchor_x)
                slot_nominal = max(bus_lo, min(bus_hi, anchor_x + outward * MIN_PARALLEL_GAP))
                return allocate_bus_x(
                    slot_nominal,
                    y_lo,
                    y_hi,
                    bus_lo,
                    bus_hi,
                    reserved,
                    net,
                    outward=outward,
                    assigned_in_group=assigned_in_group,
                )
        nominal = nominal_gutter_bus_x(bus_slot, n_slots, channel_lo, channel_hi)
        return allocate_bus_x(
            nominal,
            y_lo,
            y_hi,
            channel_lo,
            channel_hi,
            reserved,
            net,
            outward=outward,
            assigned_in_group=assigned_in_group,
        )
    bus_lo, bus_hi = corridor
    inner_anchor = anchor_x + outward * MIN_PARALLEL_GAP
    step = MIN_PARALLEL_GAP
    if bus_hi - bus_lo < (n_slots - 1) * MIN_PARALLEL_GAP - WIRE_EPS and n_slots > 1:
        step = (bus_hi - bus_lo) / max(n_slots - 1, 1)
    slot_nominal = inner_anchor + bus_slot * outward * step
    slot_nominal = max(bus_lo, min(bus_hi, slot_nominal))
    return allocate_bus_x(
        slot_nominal,
        y_lo,
        y_hi,
        bus_lo,
        bus_hi,
        reserved,
        net,
        outward=outward,
        assigned_in_group=assigned_in_group,
    )


def adjust_bus_x_for_column_gaps(
    bus_x: float,
    channel_lo: float,
    channel_hi: float,
    column_gaps: list[ColumnGap],
    *,
    anchor_x: float,
) -> float:
    """Snap ``bus_x`` into the nearest valid column-gap corridor."""
    if bus_x_in_column_gaps(bus_x, column_gaps):
        return bus_x
    corridors = gutter_vertical_corridors(channel_lo, channel_hi, column_gaps)
    if not corridors:
        corridors = _column_gap_corridors(column_gaps)
    if not corridors:
        return bus_x
    lo, hi = _corridor_near_anchor(corridors, anchor_x)
    if lo <= bus_x <= hi:
        return bus_x
    if lo <= anchor_x <= hi:
        return anchor_x
    return (lo + hi) / 2
