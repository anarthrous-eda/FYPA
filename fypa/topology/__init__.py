"""PDN topology schematic — public API.

See ``README.md`` in this package for architecture, routing, validation, and tests.

Imports are lazy so ``import fypa.topology`` (or submodule loads of this package)
do not pull in the full build pipeline.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GND_NET",
    "MIN_PARALLEL_GAP",
    "PORT_WIRE_STUB",
    "ROLE_COLORS",
    "BridgeCrossing",
    "SchematicGeometry",
    "TopologyMetadata",
    "TopologyModel",
    "TopologyNode",
    "TopologyPort",
    "TopologyWire",
    "WireSeg",
    "assert_topology_metadata",
    "build_topology_model",
    "classify_hv_intersection",
    "compute_schematic_geometry",
    "external_feed_wires",
    "find_component_at",
    "find_junctions",
    "find_port_at",
    "find_wire_at",
    "format_directive_value",
    "merge_validation_issues",
    "parse_topology_directives",
    "parse_wire_path",
    "path_to_segments",
    "reformat_legacy_value_str",
    "render_topology_svg",
    "schematic_segments",
    "solid_wire_index_maps",
    "topology_net_at",
    "topology_tooltip_at",
    "topology_wiring_report",
    "topology_wiring_report_json",
    "truncate_label",
    "validate_topology",
    "vertical_bridge_path",
]

_LAZY: dict[str, tuple[str, str]] = {
    "GND_NET": ("fypa.topology.constants", "GND_NET"),
    "MIN_PARALLEL_GAP": ("fypa.topology.constants", "MIN_PARALLEL_GAP"),
    "PORT_WIRE_STUB": ("fypa.topology.constants", "PORT_WIRE_STUB"),
    "ROLE_COLORS": ("fypa.topology.constants", "ROLE_COLORS"),
    "BridgeCrossing": ("fypa.topology.geometry", "BridgeCrossing"),
    "SchematicGeometry": ("fypa.topology.geometry", "SchematicGeometry"),
    "TopologyMetadata": ("fypa.topology.metadata_schema", "TopologyMetadata"),
    "TopologyModel": ("fypa.topology.types", "TopologyModel"),
    "TopologyNode": ("fypa.topology.types", "TopologyNode"),
    "TopologyPort": ("fypa.topology.types", "TopologyPort"),
    "TopologyWire": ("fypa.topology.types", "TopologyWire"),
    "WireSeg": ("fypa.topology.geometry", "WireSeg"),
    "assert_topology_metadata": (
        "fypa.topology.metadata_schema",
        "assert_topology_metadata",
    ),
    "build_topology_model": ("fypa.topology.builder", "build_topology_model"),
    "classify_hv_intersection": (
        "fypa.topology.geometry",
        "classify_hv_intersection",
    ),
    "compute_schematic_geometry": (
        "fypa.topology.geometry",
        "compute_schematic_geometry",
    ),
    "external_feed_wires": ("fypa.topology.metadata.feeds", "external_feed_wires"),
    "find_component_at": ("fypa.topology.hit_test", "find_component_at"),
    "find_junctions": ("fypa.topology.geometry", "find_junctions"),
    "find_port_at": ("fypa.topology.hit_test", "find_port_at"),
    "find_wire_at": ("fypa.topology.hit_test", "find_wire_at"),
    "format_directive_value": ("fypa.topology.util", "format_directive_value"),
    "merge_validation_issues": ("fypa.topology.validate", "merge_validation_issues"),
    "parse_topology_directives": (
        "fypa.topology.metadata.layout_bridge",
        "parse_topology_directives",
    ),
    "parse_wire_path": ("fypa.topology.geometry", "parse_wire_path"),
    "path_to_segments": ("fypa.topology.geometry", "path_to_segments"),
    "reformat_legacy_value_str": (
        "fypa.topology.util",
        "reformat_legacy_value_str",
    ),
    "render_topology_svg": ("fypa.topology.render", "render_topology_svg"),
    "schematic_segments": ("fypa.topology.geometry", "schematic_segments"),
    "solid_wire_index_maps": ("fypa.topology.geometry", "solid_wire_index_maps"),
    "topology_net_at": ("fypa.topology.hit_test", "topology_net_at"),
    "topology_tooltip_at": ("fypa.topology.hit_test", "topology_tooltip_at"),
    "topology_wiring_report": ("fypa.topology.report", "topology_wiring_report"),
    "topology_wiring_report_json": (
        "fypa.topology.report",
        "topology_wiring_report_json",
    ),
    "truncate_label": ("fypa.topology.util", "truncate_label"),
    "validate_topology": ("fypa.topology.validate", "validate_topology"),
    "vertical_bridge_path": ("fypa.topology.geometry", "vertical_bridge_path"),
}


def __getattr__(name: str) -> Any:
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = spec
    import importlib

    return getattr(importlib.import_module(module_name), attr)
