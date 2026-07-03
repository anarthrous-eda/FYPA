"""Shared bus-slot ordering for gutter and stacked 2-port pairs."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement.ports import gutter_bus_slot_for_source_y
from fypa.topology.types import TopologyPort

Pair = tuple[TopologyPort, TopologyPort]
SlotItem = tuple[int, int, TopologyPort, TopologyPort]


def sort_pairs_by_approach_y(group: list[Pair]) -> list[Pair]:
    """Order pairs by ascending approach-port y (top first)."""
    return sorted(group, key=lambda ab: (min(ab[0].y, ab[1].y), ab[0].net))


def gutter_approach_side(group: list[Pair]) -> str:
    """Port side shared by every pair's approach port (``a`` from classify)."""
    if not group:
        raise ValueError("empty gutter group")
    sides = {ab[0].side for ab in group}
    if len(sides) != 1:
        raise ValueError(f"mixed gutter approach sides: {sorted(sides)}")
    return next(iter(sides))


def bus_slot_assignment_order(n_slots: int, approach_side: str) -> list[int]:
    """Y-slot indices in bus-assignment order (inner bus first)."""
    return sorted(
        range(n_slots),
        key=lambda s: gutter_bus_slot_for_source_y(s, n_slots, approach_side=approach_side),
    )


def iter_gutter_pair_slots(group: list[Pair]) -> tuple[str, list[SlotItem]]:
    """Return ``(approach_side, [(y_slot, bus_slot, a, b), ...])`` for bus planning."""
    sorted_group = sort_pairs_by_approach_y(group)
    approach_side = gutter_approach_side(sorted_group)
    n_slots = len(sorted_group)
    items: list[SlotItem] = []
    for y_slot in bus_slot_assignment_order(n_slots, approach_side):
        bus_slot = gutter_bus_slot_for_source_y(y_slot, n_slots, approach_side=approach_side)
        a, b = sorted_group[y_slot]
        items.append((y_slot, bus_slot, a, b))
    return approach_side, items


def iter_stacked_pair_lanes(group: list[Pair], bus_side: str) -> list[SlotItem]:
    """Return ``[(y_slot, bus_lane, a, b), ...]`` in lane-assignment order."""
    sorted_group = sort_pairs_by_approach_y(group)
    n_lanes = len(sorted_group)
    items: list[SlotItem] = []
    for y_slot in bus_slot_assignment_order(n_lanes, bus_side):
        bus_lane = gutter_bus_slot_for_source_y(y_slot, n_lanes, approach_side=bus_side)
        a, b = sorted_group[y_slot]
        items.append((y_slot, bus_lane, a, b))
    return items


def nominal_gutter_bus_x(
    bus_slot: int,
    n_slots: int,
    channel_lo: float,
    channel_hi: float,
) -> float:
    """Nominal bus x for ``bus_slot`` inside a gutter channel."""
    bus_lo, bus_hi = channel_lo, channel_hi
    if n_slots > 1:
        span = bus_hi - bus_lo
        need = (n_slots - 1) * MIN_PARALLEL_GAP
        bus_x = bus_lo + bus_slot * MIN_PARALLEL_GAP
        if span < need - WIRE_EPS:
            bus_x = bus_lo + bus_slot * (span / max(n_slots - 1, 1))
    else:
        bus_x = (bus_lo + bus_hi) / 2
    return min(bus_hi, max(bus_lo, bus_x))
