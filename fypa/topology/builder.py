"""Build a complete topology model from simulation metadata."""

from __future__ import annotations

from fypa.topology.constants import (
    CANVAS_HEIGHT_PAD_GND,
    CANVAS_HEIGHT_PAD_NO_GND,
    GND_SYMBOL_BELOW,
    LEGEND_BELOW_BUS,
    MARGIN,
)
from fypa.topology.geometry import compute_schematic_geometry
from fypa.topology.labels import finalize_wire_labels
from fypa.topology.layout import build_node_layout
from fypa.topology.metadata.feeds import external_feed_wires
from fypa.topology.metadata_schema import TopologyMetadata
from fypa.topology.routing import build_wires
from fypa.topology.types import TopologyModel


def build_topology_model(metadata: TopologyMetadata | None) -> TopologyModel:
    """Build a Flow diagram layout model for the PDN simulation schematic."""
    if metadata is None:
        return TopologyModel()

    layout = build_node_layout(metadata)

    wires, gnd_symbol_x = build_wires(
        layout.ports,
        gnd_bus_y=layout.gnd_bus_y,
        obstacles=layout.directive_nodes,
        bus_plan=layout.bus_plan,
    )

    wires.extend(
        external_feed_wires(layout.ports, layout.driven_nets, layout.net_to_rail),
    )
    geo = compute_schematic_geometry(
        wires, gnd_symbol_x=gnd_symbol_x, gnd_bus_y=layout.gnd_bus_y,
    )
    finalize_wire_labels(wires, nodes=layout.directive_nodes, geo=geo)

    width = layout.content_right + MARGIN
    directive_bottom = max(
        (n.y + n.height for n in layout.directive_nodes),
        default=MARGIN,
    )
    if layout.needs_gnd and layout.gnd_bus_y is not None:
        height = (
            layout.gnd_bus_y + GND_SYMBOL_BELOW + LEGEND_BELOW_BUS + CANVAS_HEIGHT_PAD_GND
        )
    else:
        height = directive_bottom + MARGIN + CANVAS_HEIGHT_PAD_NO_GND

    return TopologyModel(
        nodes=layout.nodes,
        wires=wires,
        width=width,
        height=height,
        gnd_bus_y=layout.gnd_bus_y,
        gnd_symbol_x=gnd_symbol_x,
    )
