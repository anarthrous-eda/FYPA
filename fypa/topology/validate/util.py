"""Shared geometry helpers for topology validation."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.geometry import WireSeg
from fypa.topology.types import TopologyNode


def intervals_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return hi1 > lo2 + WIRE_EPS and lo1 < hi2 - WIRE_EPS


def foreign_segments_cross(a: list[WireSeg], b: list[WireSeg]) -> bool:
    """True when a horizontal and vertical segment from different nets cross."""
    for sa in a:
        for sb in b:
            if sa.orient == "H" and sb.orient == "V":
                x_lo, x_hi = min(sa.x1, sa.x2), max(sa.x1, sa.x2)
                y_lo, y_hi = min(sb.y1, sb.y2), max(sb.y1, sb.y2)
                if x_lo < sb.x1 < x_hi and y_lo < sa.y1 < y_hi:
                    return True
            elif sa.orient == "V" and sb.orient == "H":
                x_lo, x_hi = min(sb.x1, sb.x2), max(sb.x1, sb.x2)
                y_lo, y_hi = min(sa.y1, sa.y2), max(sa.y1, sa.y2)
                if x_lo < sa.x1 < x_hi and y_lo < sb.y1 < y_hi:
                    return True
    return False


def parallel_corridors_too_close(
    a: float,
    b: float,
    *,
    min_gap: float = MIN_PARALLEL_GAP,
) -> bool:
    """True when two parallel wire columns are closer than ``min_gap``."""
    return abs(a - b) < min_gap - WIRE_EPS


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
