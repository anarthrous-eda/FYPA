"""Unit tests for geometry-based port stub validation."""

from __future__ import annotations

from fypa.topology.constants import GND_NET
from fypa.topology.geometry import (
    WireSeg,
    compute_schematic_geometry,
)
from fypa.topology.types import TopologyModel, TopologyNode, TopologyPort, TopologyWire
from fypa.topology.validate.stubs import (
    check_open_stub_ends,
    collect_routed_ports,
    stub_end_connected,
)


def _geo(net: str, path_d: str):
    wire = TopologyWire(net=net, path_d=path_d)
    return compute_schematic_geometry([wire])


def test_stub_end_connected_open_horizontal_end():
    geo = _geo("VDD", "M 100,50 H 120")
    assert not stub_end_connected(120.0, 50.0, "VDD", geo)


def test_stub_end_connected_corner():
    geo = _geo("VDD", "M 100,50 H 120 V 80")
    assert stub_end_connected(120.0, 50.0, "VDD", geo)


def test_stub_end_connected_collinear_segments_at_stub_end():
    """Two horizontal segments sharing an endpoint (stack_column gutter row)."""
    segs = [
        WireSeg("LED", "H", 244.0, 195.0, 442.0, 195.0),
        WireSeg("LED", "H", 244.0, 195.0, 264.0, 195.0),
    ]
    geo = compute_schematic_geometry(
        [TopologyWire(net="LED", path_d="M 0,0")],
    )
    geo = type(geo)(
        segments=segs,
        horizontals=segs,
        verticals=[],
        vert_crossings={},
        junctions=[],
        bridges=[],
    )
    assert stub_end_connected(244.0, 195.0, "LED", geo)


def test_stub_end_connected_interior_pass_through():
    geo = _geo("VDD", "M 80,50 H 160")
    assert stub_end_connected(120.0, 50.0, "VDD", geo)


def test_stub_end_connected_gnd_vertical_tap():
    geo = _geo(GND_NET, "M 100,50 H 88 V 120")
    assert stub_end_connected(88.0, 50.0, GND_NET, geo)


def test_collect_routed_ports_skips_dashed():
    wires = [
        TopologyWire(net="VDD", path_d="M 0,0 H 10", src_node="U1", src_terminal="P"),
        TopologyWire(net="EXT", path_d="M 0,0 H 5", dashed=True, src_node="J1", src_terminal="P"),
    ]
    assert collect_routed_ports(wires) == {("U1", "P", "VDD")}


def test_check_open_stub_ends_flags_dead_stub():
    port = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=100.0,
        y=50.0,
        node_id="U1",
        stub_length=20.0,
    )
    node = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="SINK",
        x=100.0,
        y=50.0,
        width=50.0,
        height=20.0,
        config_label="",
        has_error=False,
        ports=[port],
        bounds=(100.0, 50.0, 50.0, 20.0),
    )
    model = TopologyModel(
        nodes=[node],
        wires=[
            TopologyWire(
                net="VDD",
                path_d="M 100,50 H 120",
                src_node="U1",
                src_terminal="P",
            ),
        ],
    )
    issues = check_open_stub_ends(model)
    assert len(issues) == 1
    assert issues[0]["code"] == "open_signal_stub"


def test_check_open_stub_ends_accepts_bus_corner():
    port = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=100.0,
        y=50.0,
        node_id="U1",
        stub_length=20.0,
    )
    node = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="SINK",
        x=100.0,
        y=50.0,
        width=50.0,
        height=20.0,
        config_label="",
        has_error=False,
        ports=[port],
        bounds=(100.0, 50.0, 50.0, 20.0),
    )
    model = TopologyModel(
        nodes=[node],
        wires=[
            TopologyWire(
                net="VDD",
                path_d="M 100,50 H 120 V 80",
                src_node="U1",
                src_terminal="P",
            ),
        ],
    )
    assert not check_open_stub_ends(model)
