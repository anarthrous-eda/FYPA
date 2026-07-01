"""Unit tests for individual validate_topology issue codes."""

from __future__ import annotations

from fypa.topology.constants import GND_NET
from fypa.topology.geometry import BridgeCrossing, WireSeg
from fypa.topology.types import TopologyModel, TopologyNode, TopologyPort, TopologyWire
from fypa.topology.validate import (
    check_segment_spacing,
    validate_topology,
    vertical_segment_overlaps_node_body,
)


def test_duplicate_vertical_x_detected():
    segments = [
        WireSeg(GND_NET, "V", 50.0, 10.0, 50.0, 90.0, wire_index=0),
        WireSeg("VDD", "V", 50.0, 20.0, 50.0, 80.0, wire_index=1),
    ]
    issues = check_segment_spacing(segments, [], [])
    assert any(i["code"] == "duplicate_vertical_x" for i in issues)


def test_duplicate_horizontal_y_detected():
    segments = [
        WireSeg("VDD", "H", 10.0, 40.0, 90.0, 40.0, wire_index=0),
        WireSeg("GND", "H", 20.0, 40.0, 80.0, 40.0, wire_index=1),
    ]
    issues = check_segment_spacing(segments, [], [])
    assert any(i["code"] == "duplicate_horizontal_y" for i in issues)


def test_junction_near_bridge_detected():
    segments = [
        WireSeg("VDD", "V", 60.0, 10.0, 60.0, 90.0, wire_index=0),
        WireSeg("GND", "H", 10.0, 50.0, 110.0, 50.0, wire_index=1),
    ]
    junctions = [(60.0, 50.0)]
    bridges = [
        BridgeCrossing(
            x=60.0,
            y=50.0,
            vertical_net="VDD",
            horizontal_net="GND",
            vertical_index=0,
        ),
    ]
    issues = check_segment_spacing(segments, junctions, bridges)
    assert any(i["code"] == "junction_near_bridge" for i in issues)


def test_open_gnd_stub_detected():
    node = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="SINK",
        x=10.0,
        y=10.0,
        width=50.0,
        height=20.0,
        config_label="",
        has_error=False,
        bounds=(10.0, 10.0, 50.0, 20.0),
        ports=[
            TopologyPort(
                terminal="N",
                net=GND_NET,
                label="GND",
                side="left",
                x=10.0,
                y=20.0,
                node_id="U1",
                stub_length=12.0,
            ),
        ],
    )
    wire = TopologyWire(
        net=GND_NET,
        path_d="M 22,20 H 40",
        src_node="U1",
        src_terminal="N",
        routing_kind="gnd_tap",
    )
    model = TopologyModel(nodes=[node], wires=[wire], width=100.0, height=100.0)
    issues = validate_topology(model)
    assert any(i["code"] == "open_gnd_stub" for i in issues)


def test_open_signal_stub_detected():
    node = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="SINK",
        x=10.0,
        y=10.0,
        width=50.0,
        height=20.0,
        config_label="",
        has_error=False,
        bounds=(10.0, 10.0, 50.0, 20.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="left",
                x=10.0,
                y=20.0,
                node_id="U1",
                stub_length=20.0,
            ),
        ],
    )
    wire = TopologyWire(
        net="VDD",
        path_d="M 30,20 H 50",
        src_node="U1",
        src_terminal="P",
        routing_kind="gutter",
    )
    model = TopologyModel(nodes=[node], wires=[wire], width=100.0, height=100.0)
    issues = validate_topology(model)
    assert any(i["code"] == "open_signal_stub" for i in issues)


def test_vertical_segment_overlaps_node_body_helper():
    node = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="SINK",
        x=10.0,
        y=10.0,
        width=50.0,
        height=20.0,
        config_label="",
        has_error=False,
        bounds=(10.0, 10.0, 50.0, 20.0),
    )
    assert vertical_segment_overlaps_node_body(node, 30.0, 5.0, 50.0)
    assert not vertical_segment_overlaps_node_body(node, 80.0, 5.0, 50.0)
