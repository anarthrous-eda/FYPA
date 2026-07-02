"""Wire label placement validation."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import MAX_LABEL_DISTANCE, WIRE_EPS
from fypa.topology.geometry import SchematicGeometry, WireSeg
from fypa.topology.issues import make_issue
from fypa.topology.types import TopologyModel


def check_wire_labels(
    model: TopologyModel,
    geo: SchematicGeometry,
) -> list[dict]:
    """Labels must be placed away from (0,0) and near their net's wire segments."""
    segments_by_net: dict[str, list[WireSeg]] = defaultdict(list)
    for seg in geo.segments:
        segments_by_net[seg.net].append(seg)

    issues: list[dict] = []
    for wi, wire in enumerate(model.wires):
        if not wire.label or wire.dashed:
            continue
        if wire.label_x == 0.0 and wire.label_y == 0.0:
            issues.append(
                make_issue(
                    "label_not_at_origin",
                    f"Wire {wi} ({wire.net}) label '{wire.label}' at (0,0)",
                    wire_id=wi,
                    net=wire.net,
                )
            )
            continue
        best_d = float("inf")
        for seg in segments_by_net.get(wire.net, []):
            if seg.orient == "H":
                x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
                if x_lo - WIRE_EPS <= wire.label_x <= x_hi + WIRE_EPS:
                    best_d = min(best_d, abs(wire.label_y - seg.y1))
            else:
                y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
                if y_lo - WIRE_EPS <= wire.label_y <= y_hi + WIRE_EPS:
                    best_d = min(best_d, abs(wire.label_x - seg.x1))
        if best_d > MAX_LABEL_DISTANCE + WIRE_EPS:
            issues.append(
                make_issue(
                    "label_anchor_distance",
                    (
                        f"Wire {wi} ({wire.net}) label is {best_d:.1f}px "
                        f"from the nearest wire segment"
                    ),
                    wire_id=wi,
                    net=wire.net,
                    distance=round(best_d, 1),
                )
            )
    return issues
