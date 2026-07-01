"""Layout pipeline result from ``build_node_layout``."""

from __future__ import annotations

from dataclasses import dataclass

from fypa.topology.placement import BusPlan
from fypa.topology.types import TopologyNode, TopologyPort


@dataclass
class LayoutResult:
    nodes: list[TopologyNode]
    ports: list[TopologyPort]
    content_right: float
    max_col: int
    needs_gnd: bool
    gnd_bus_y: float | None
    directive_nodes: list[TopologyNode]
    node_specs: list[dict]
    net_to_rail: dict[str, str]
    driven_nets: set[str]
    bus_plan: BusPlan
