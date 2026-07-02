"""Node column layout and port placement for the topology schematic."""

from __future__ import annotations

from fypa.topology.constants import GND_BUS_BELOW, MARGIN
from fypa.topology.layout.columns import place_nodes, refine_place_nodes_for_gnd
from fypa.topology.layout.stubs import assign_edge_wire_columns, assign_stacked_stub_lengths
from fypa.topology.layout_result import LayoutResult
from fypa.topology.metadata.layout_bridge import parse_topology_directives, specs_by_column
from fypa.topology.metadata_schema import TopologyMetadata
from fypa.topology.placement import BusPlan

__all__ = [
    "assign_edge_wire_columns",
    "assign_stacked_stub_lengths",
    "build_node_layout",
    "place_nodes",
    "refine_place_nodes_for_gnd",
]


def build_node_layout(
    metadata: TopologyMetadata | None,
) -> LayoutResult:
    """Parse metadata and place nodes; returns layout state for wire routing."""
    empty = LayoutResult(
        nodes=[],
        ports=[],
        content_right=MARGIN,
        max_col=0,
        needs_gnd=False,
        gnd_bus_y=None,
        directive_nodes=[],
        node_specs=[],
        net_to_rail={},
        driven_nets=set(),
        bus_plan=BusPlan(),
    )
    if metadata is None:
        return empty

    parsed = parse_topology_directives(metadata)
    by_col, max_col = specs_by_column(parsed.node_specs, parsed.columns)
    nodes, all_ports, content_right, bus_plan, gaps = place_nodes(
        parsed.node_specs,
        by_col=by_col,
        max_col=max_col,
    )

    directive_nodes = [n for n in nodes if n.role != "GND"]
    directive_bottom = max((n.y + n.height for n in directive_nodes), default=MARGIN)
    gnd_bus_y = directive_bottom + GND_BUS_BELOW if parsed.needs_gnd else None

    if parsed.needs_gnd and gnd_bus_y is not None:
        nodes, all_ports, content_right, bus_plan, gaps = refine_place_nodes_for_gnd(
            parsed.node_specs,
            by_col=by_col,
            max_col=max_col,
            gaps=gaps,
            gnd_bus_y=gnd_bus_y,
        )
        directive_nodes = [n for n in nodes if n.role != "GND"]
        directive_bottom = max((n.y + n.height for n in directive_nodes), default=MARGIN)
        gnd_bus_y = directive_bottom + GND_BUS_BELOW

    return LayoutResult(
        nodes=nodes,
        ports=all_ports,
        content_right=content_right,
        max_col=max_col,
        needs_gnd=parsed.needs_gnd,
        gnd_bus_y=gnd_bus_y,
        directive_nodes=directive_nodes,
        node_specs=parsed.node_specs,
        net_to_rail=parsed.net_to_rail,
        driven_nets=parsed.driven_nets,
        bus_plan=bus_plan,
    )
