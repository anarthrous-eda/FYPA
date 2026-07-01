"""Shared geometry helpers for topology validation."""

from __future__ import annotations

from fypa.topology.constants import WIRE_EPS
from fypa.topology.geometry import WireSeg
from fypa.topology.types import TopologyNode


def intervals_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return hi1 > lo2 + WIRE_EPS and lo1 < hi2 - WIRE_EPS


def segment_span(seg: WireSeg) -> tuple[float, float]:
    if seg.orient == "V":
        return min(seg.y1, seg.y2), max(seg.y1, seg.y2)
    return min(seg.x1, seg.x2), max(seg.x1, seg.x2)


def vertical_segment_overlaps_node_body(
    node: TopologyNode,
    x: float,
    y_lo: float,
    y_hi: float,
) -> bool:
    """True when a vertical segment at ``x`` overlaps the node body on Y."""
    nx, ny, nw, nh = node.bounds
    if x < nx - WIRE_EPS or x > nx + nw + WIRE_EPS:
        return False
    return intervals_overlap(y_lo, y_hi, ny, ny + nh)
