"""Topology model validation checks."""

from __future__ import annotations

from fypa.topology.constants import MAX_CANVAS_WIDTH, WIRE_EPS
from fypa.topology.geometry import compute_schematic_geometry
from fypa.topology.issues import make_issue
from fypa.topology.net_aliases import is_conditional_gnd_name
from fypa.topology.types import TopologyModel
from fypa.topology.validate.labels import check_wire_labels
from fypa.topology.validate.segments import (
    check_gutter_wire_crossings,
    check_parallel_vertical_gap,
    check_segment_spacing,
    check_signal_vs_gnd_drop_gap,
    check_vertical_bus_column_gaps,
    check_vertical_under_node,
    check_wires_through_foreign_nodes,
)
from fypa.topology.validate.hub import check_hub_net_disconnected
from fypa.topology.validate.stubs import check_open_stub_ends
from fypa.topology.validate.util import vertical_segment_overlaps_node_body
from fypa.topology.validate.wires import check_dangling_wire_endpoints

__all__ = [
    "check_conditional_gnd_names",
    "check_dangling_wire_endpoints",
    "check_open_stub_ends",
    "check_segment_spacing",
    "merge_validation_issues",
    "validate_topology",
    "vertical_segment_overlaps_node_body",
]


def validate_topology(model: TopologyModel) -> list[dict]:
    """Run model-level topology validation checks."""
    issues: list[dict] = []
    directive_nodes = [n for n in model.nodes if n.role != "GND"]

    issues.extend(check_wires_through_foreign_nodes(model))
    issues.extend(check_parallel_vertical_gap(model))
    issues.extend(check_vertical_bus_column_gaps(model))
    issues.extend(check_gutter_wire_crossings(model))
    issues.extend(check_signal_vs_gnd_drop_gap(model))

    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )

    issues.extend(check_wire_labels(model, geo))
    issues.extend(check_segment_spacing(geo.segments, geo.junctions, geo.bridges))
    issues.extend(check_open_stub_ends(model, geo=geo))
    issues.extend(check_dangling_wire_endpoints(model, geo))
    issues.extend(check_hub_net_disconnected(model, geo))
    issues.extend(
        check_vertical_under_node(model, geo, directive_nodes=directive_nodes),
    )

    if model.width > MAX_CANVAS_WIDTH + WIRE_EPS:
        issues.append(
            make_issue(
                "canvas_width_reasonable",
                (f"Canvas width {model.width:.1f}px exceeds maximum {MAX_CANVAS_WIDTH:.1f}px"),
                width=round(model.width, 1),
                max_width=MAX_CANVAS_WIDTH,
            )
        )

    issues.extend(check_conditional_gnd_names(model))

    return issues


def check_conditional_gnd_names(model: TopologyModel) -> list[dict]:
    """Warn when a ground-looking net (e.g. ``VSS``) is drawn as its own rail.

    ``VSS`` and similar names are deliberately *not* folded into GND by name
    alone (a real board can carry a negative rail called VSS), so such a net
    is drawn as its own bus unless the electrical rail grouping ties it to a
    GND-grouped net — in which case its ports already carry the GND net here.
    A port still carrying the conditional name means it was kept separate; a
    single ``warning`` per distinct name tells the user it was not assumed to
    be ground (rather than letting the change be silent)."""
    seen: set[str] = set()
    for node in model.nodes:
        for port in node.ports:
            net = port.net
            if net and is_conditional_gnd_name(net) and net not in seen:
                seen.add(net)
    issues: list[dict] = []
    for net in sorted(seen):
        issues.append(
            make_issue(
                "conditional_gnd_name_not_merged",
                (f"Net {net!r} is drawn as its own rail, not merged with GND "
                 f"(a name like VSS is treated as ground only when electrically "
                 f"tied to a GND-grouped net). Verify this matches the design."),
                severity="warning",
                net=net,
            )
        )
    return issues


def merge_validation_issues(
    model: TopologyModel,
    wire_issues: list[dict],
) -> list[dict]:
    """Combine per-wire heuristic issues with model-level validation."""
    return list(wire_issues) + validate_topology(model)
