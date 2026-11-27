"""Topology model dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field

from fypa.topology.metadata_schema import JumpRowDict


@dataclass
class NodeSection:
    """One stacked role block inside a multi-role topology symbol."""
    role: str
    y: float
    height: float


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
    role: str = ""
    # Supply-side ports of a SOURCE / SINK / REGULATOR: "ROLE:label:TERM"
    # (see fypa.report.collect.topology_results), and once solved the
    # voltage there - drawn beside the port when port voltages are on.
    vkey: str = ""
    voltage: float | None = None
    voltage_stale: bool = False


@dataclass
class GaugeReading:
    """Solved voltage at a load's pins, for one :class:`NodeGauge`.

    Voltages are differential: the lowest supply pin minus the highest return
    pin, so ground rise counts (the same figure as the design report's
    "voltage at load"). ``low`` / ``high`` span the supply's range (and light
    load, where a maximum is checked); with no range they are equal.
    """

    low: float
    high: float
    nominal: float              # at the solved load, supply at nominal
    margin: float | None        # smallest margin to a limit, worst case
    status: str                 # "PASS" | "WARN" | "FAIL"
    setpoint: float | None = None    # the rail's supply, nominal
    src_low: float | None = None     # the supply's range
    src_high: float | None = None
    limit: str = "min"          # which limit ``margin`` is to: "min" / "max"
    tooltip: str = ""


@dataclass
class NodeGauge:
    """A voltage gauge in a SINK or regulator-input block: the load's
    allowed window (``min_v``, optional ``max_v``) and, once solved, where
    its supply actually sits. ``x``/``y``/``width``/``height`` are the area
    it draws in (all its lines), in diagram coordinates; ``style`` is
    ``"bar"`` or ``"text"`` (see :mod:`fypa.topology.gauges`)."""

    key: str                    # "SINK:U5", see LoadResult.key
    label: str                  # channel tag when a block has several, else ""
    min_v: float
    max_v: float | None
    x: float
    y: float
    width: float
    height: float
    reading: GaugeReading | None = None
    stale: bool = False         # reading is from a solve that predates edits
    style: str = "bar"
    # Show the voltage at the pins with the margin (both styles). Off when
    # the port beside it already shows that voltage.
    show_value: bool = True

    @property
    def tooltip(self) -> str:
        limits = f"min {self.min_v:.4g} V" + (
            f", max {self.max_v:.4g} V" if self.max_v is not None else "")
        if self.reading is None:
            return (f"Allowed: {limits}\nNo solved voltage yet: Solve to "
                    "check this load.")
        tip = self.reading.tooltip
        if self.stale:
            tip += "\n(From the last solve; Solve to include pending edits.)"
        return tip


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
    single_net: bool = False
    ports: list[TopologyPort] = field(default_factory=list)
    bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
    jump_row: JumpRowDict | None = None
    sections: list[NodeSection] = field(default_factory=list)
    # Rows layout: a one-line chip (designator · value) instead of a full
    # symbol - used for 0 Ω auto-bridges.
    compact: bool = False
    value_str: str = ""
    # Rows layout: voltage gauges (a SINK with PDN_MIN_V, a regulator input
    # with a dropout / PDN_VIN_MIN), one row each under the port rows.
    gauges: list[NodeGauge] = field(default_factory=list)
    # A free marker dropped on the Board, not a part: drawn dashed, and
    # ``label`` is its name (``designator`` stays its ``EDIT_<id>`` key).
    marker: bool = False
    # A design issue of warning severity names this part or its rail
    # (fypa.design_issues.apply_to_topology): an amber triangle, where
    # ``has_error`` draws a red one.
    has_warning: bool = False


@dataclass
class TopologyTag:
    """Power-port tag (rows layout): a net that continues somewhere else.

    Drawn as a pentagon pointing right (the power-flow direction). ``x``/``y``
    are the body's left edge and vertical centre. ``target`` is the diagram
    point the viewer pans to when the tag is clicked (the other end of the
    connection), ``None`` when there is nowhere to go.
    """

    net: str
    text: str
    kind: str  # "net" | "feed" | "ref" | "undriven"
    x: float
    y: float
    width: float
    height: float
    tooltip: str = ""
    target: tuple[float, float] | None = None
    target_node: str = ""


@dataclass
class TopologyBand:
    """Header line above one row of the rows layout."""

    title: str
    subtitle: str
    x: float
    y: float
    width: float
    kind: str  # "source" | "regulator" | "undriven" | "bridges"
    depth: int = 0
    # Rows only: stable id across rebuilds (what the viewer remembers as
    # collapsed), and whether this row is drawn as its header alone.
    key: str = ""
    collapsed: bool = False


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
    # "columns" (classic single-row flow) or "rows" (one row per power origin).
    layout: str = "columns"
    tags: list[TopologyTag] = field(default_factory=list)
    bands: list[TopologyBand] = field(default_factory=list)
    # Rows layout: local ground symbols, as (x, y) of the symbol's top.
    gnd_glyphs: list[tuple[float, float]] = field(default_factory=list)
    # Rows layout: one-line notes drawn under the diagram (e.g. hidden items).
    notes: list[str] = field(default_factory=list)
    # Draw each solved port voltage beside its port.
    port_voltages: bool = False

    @property
    def components(self) -> list[TopologyNode]:
        """Alias for viewer/tests that still refer to *components*."""
        return self.nodes
