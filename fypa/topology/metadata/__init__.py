"""Directive and component spec parsing for topology layout.

Exports are lazy so importing a single helper does not load the full bridge.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ParsedLayoutInput",
    "assign_columns",
    "canonical_net",
    "directives_to_component_specs",
    "driven_power_nets",
    "external_feed_wires",
    "is_gnd_alias",
    "is_ideal_return",
    "jump_row_for_directive",
    "natural_sort_key",
    "net_to_rail_map",
    "parse_topology_directives",
    "port_display_net",
    "port_tooltip",
    "specs_by_column",
    "terminal_net",
    "wire_net",
]

_LAZY: dict[str, tuple[str, str]] = {
    "ParsedLayoutInput": (
        "fypa.topology.metadata.layout_bridge",
        "ParsedLayoutInput",
    ),
    "assign_columns": ("fypa.topology.metadata.layout_bridge", "assign_columns"),
    "canonical_net": ("fypa.topology.metadata.nets", "canonical_net"),
    "directives_to_component_specs": (
        "fypa.topology.metadata.specs",
        "directives_to_component_specs",
    ),
    "driven_power_nets": ("fypa.topology.metadata.specs", "driven_power_nets"),
    "external_feed_wires": ("fypa.topology.metadata.feeds", "external_feed_wires"),
    "is_gnd_alias": ("fypa.topology.net_aliases", "is_gnd_alias"),
    "is_ideal_return": ("fypa.topology.metadata.nets", "is_ideal_return"),
    "jump_row_for_directive": (
        "fypa.topology.metadata.specs",
        "jump_row_for_directive",
    ),
    "natural_sort_key": ("fypa.topology.metadata.specs", "natural_sort_key"),
    "net_to_rail_map": ("fypa.topology.metadata.nets", "net_to_rail_map"),
    "parse_topology_directives": (
        "fypa.topology.metadata.layout_bridge",
        "parse_topology_directives",
    ),
    "port_display_net": ("fypa.topology.metadata.nets", "port_display_net"),
    "port_tooltip": ("fypa.topology.metadata.tooltips", "port_tooltip"),
    "specs_by_column": ("fypa.topology.metadata.layout_bridge", "specs_by_column"),
    "terminal_net": ("fypa.topology.metadata.nets", "terminal_net"),
    "wire_net": ("fypa.topology.metadata.nets", "wire_net"),
}


def __getattr__(name: str) -> Any:
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = spec
    import importlib

    return getattr(importlib.import_module(module_name), attr)
