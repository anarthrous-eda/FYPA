"""Topology model dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field

from fypa.topology.metadata_schema import JumpRowDict


@dataclass
class TopologyPort:
    terminal: str
    net: str
    label: str
    side: str
    x: float
    y: float
    node_id: str
    annotation: str = ""
    is_power_input: bool = False
    tooltip: str = ""
    stub_length: float = 0.0
    wire_x: float | None = None


@dataclass
class TopologyNode:
    node_id: str
    label: str
    designator: str
    role: str
    x: float
    y: float
    width: float
    height: float
    config_label: str
    has_error: bool
    tooltip: str = ""
    ports: list[TopologyPort] = field(default_factory=list)
    bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
    jump_row: JumpRowDict | None = None


@dataclass
class TopologyWire:
    net: str
    path_d: str
    label: str = ""
    label_x: float = 0.0
    label_y: float = 0.0
    label_vertical: bool = False
    label_text_anchor: str = "middle"
    label_leader_x: float = 0.0
    label_leader_y: float = 0.0
    label_has_leader: bool = False
    dashed: bool = False
    src_node: str = ""
    src_terminal: str = ""
    dst_node: str = ""
    dst_terminal: str = ""
    routing_kind: str = ""
    bus_x: float | None = None


@dataclass
class TopologyModel:
    nodes: list[TopologyNode] = field(default_factory=list)
    wires: list[TopologyWire] = field(default_factory=list)
    width: float = 400.0
    height: float = 200.0
    gnd_bus_y: float | None = None
    gnd_symbol_x: float | None = None

    @property
    def components(self) -> list[TopologyNode]:
        """Alias for viewer/tests that still refer to *components*."""
        return self.nodes
