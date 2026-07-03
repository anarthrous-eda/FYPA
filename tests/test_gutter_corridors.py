"""Regressions for gutter corridor selection (placement/gutter_corridors.py).

Channel-overlap must be tested against the inner (placeable) corridor bounds,
not the raw column gap. A gap that only grazes the stub channel within
MIN_PARALLEL_GAP of its edge has an inner corridor entirely outside the channel,
and treating it as a channel corridor pushes the bus off the stubs it serves.
"""

from __future__ import annotations

from fypa.topology.placement.gutter_corridors import (
    gutter_vertical_corridors,
    resolve_gutter_corridor,
)


def test_gutter_vertical_corridors_excludes_gap_whose_inner_is_outside_channel():
    # Raw gap (380, 510) grazes channel [500, 520], but inner corridor (396, 494)
    # lies entirely left of it -> not a channel corridor.
    assert gutter_vertical_corridors(500.0, 520.0, [(380.0, 510.0)]) == []


def test_gutter_vertical_corridors_keeps_genuinely_overlapping_corridor():
    corridors = gutter_vertical_corridors(500.0, 520.0, [(490.0, 700.0)])
    assert corridors, "an inner corridor overlapping the channel must be kept"
    (lo, hi), = corridors
    assert lo < 520.0 and hi > 500.0


def test_resolve_gutter_corridor_prefers_channel_over_grazing_gap():
    # Two gaps: one grazes the channel (inner outside), one genuinely overlaps.
    # Even with the anchor near the grazing gap, the channel corridor must win.
    gaps = [(380.0, 510.0), (490.0, 700.0)]
    lo, hi = resolve_gutter_corridor(500.0, 520.0, gaps, anchor_x=400.0)
    assert lo < 520.0 and hi > 500.0, (
        f"selected corridor ({lo}, {hi}) does not overlap the stub channel"
    )
