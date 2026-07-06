"""Unit tests for individual validate_topology issue codes."""

from __future__ import annotations

from fypa.topology.constants import GND_NET
from fypa.topology.geometry import BridgeCrossing, WireSeg
from fypa.topology.types import TopologyModel, TopologyNode, TopologyPort, TopologyWire
from fypa.topology import build_topology_model
from fypa.topology.validate import (
    check_conditional_gnd_names,
    check_gutter_wire_crossings,
    check_segment_spacing,
    validate_topology,
    vertical_segment_overlaps_node_body,
)
from fypa.topology.validate.util import foreign_segments_cross


def _port_node(node_id: str, net: str) -> TopologyNode:
    return TopologyNode(
        node_id=node_id, label=node_id, designator=node_id, role="SINK",
        x=0.0, y=0.0, width=40.0, height=20.0, config_label="",
        has_error=False, bounds=(0.0, 0.0, 40.0, 20.0),
        ports=[TopologyPort(terminal="P", net=net, label=net, side="right",
                            x=0.0, y=10.0, node_id=node_id)],
    )


def test_conditional_gnd_name_warns_when_vss_drawn_separately():
    # VSS is deliberately NOT folded into GND by name (round-2 finding): it
    # draws as its own rail, and validate must WARN so the change isn't silent.
    model = TopologyModel(nodes=[_port_node("U1", "VSS")])
    issues = check_conditional_gnd_names(model)
    assert len(issues) == 1
    assert issues[0]["code"] == "conditional_gnd_name_not_merged"
    assert issues[0]["severity"] == "warning"
    assert issues[0]["net"] == "VSS"


def test_conditional_gnd_name_quiet_for_plain_nets():
    model = TopologyModel(nodes=[_port_node("U1", "GND"), _port_node("U2", "+5V")])
    assert check_conditional_gnd_names(model) == []


def test_duplicate_vertical_x_detected():
    segments = [
        WireSeg("VDD_A", "V", 50.0, 10.0, 50.0, 90.0, wire_index=0),
        WireSeg("VDD_B", "V", 50.0, 20.0, 50.0, 80.0, wire_index=1),
    ]
    issues = check_segment_spacing(segments, [], [])
    assert any(i["code"] == "duplicate_vertical_x" for i in issues)


def test_duplicate_vertical_x_uses_corridor_gap():
    """Foreign verticals closer than MIN_PARALLEL_GAP must be flagged."""
    from fypa.topology.constants import MIN_PARALLEL_GAP

    segments = [
        WireSeg("SNS_A", "V", 100.0, 10.0, 100.0, 90.0, wire_index=0),
        WireSeg("SNS_B", "V", 100.0 + MIN_PARALLEL_GAP / 2, 20.0, 100.0 + MIN_PARALLEL_GAP / 2, 80.0, wire_index=1),
    ]
    issues = check_segment_spacing(segments, [], [])
    assert any(i["code"] == "duplicate_vertical_x" for i in issues)

    far_x = 100.0 + MIN_PARALLEL_GAP + 1.0
    segments_far = [
        WireSeg("SNS_A", "V", 100.0, 10.0, 100.0, 90.0, wire_index=0),
        WireSeg("SNS_B", "V", far_x, 20.0, far_x, 80.0, wire_index=1),
    ]
    assert not check_segment_spacing(segments_far, [], [])


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


def test_foreign_wire_crossing_detected():
    from fypa.topology.geometry import WireSeg

    crossing = [
        WireSeg("SNS_A", "V", 100.0, 10.0, 100.0, 90.0, wire_index=0),
        WireSeg("SNS_B", "H", 40.0, 50.0, 160.0, 50.0, wire_index=1),
    ]
    assert foreign_segments_cross(crossing[:1], crossing[1:])

    clear = [
        WireSeg("SNS_A", "V", 100.0, 10.0, 100.0, 40.0, wire_index=0),
        WireSeg("SNS_B", "H", 40.0, 50.0, 160.0, 50.0, wire_index=1),
    ]
    assert not foreign_segments_cross(clear[:1], clear[1:])


def test_check_gutter_wire_crossings_on_model():
    from tests.topology_fixtures import load_topology_fixture

    model = build_topology_model(load_topology_fixture("project_a_stepper_loop_rails"))
    assert not check_gutter_wire_crossings(model)


def test_check_gutter_wire_crossings_uses_all_hub_wires():
    """Crossings on any solid wire count, not only the last wire stored per net."""
    def _gap_node(node_id: str, x: float, net: str, side: str, wire_x: float) -> TopologyNode:
        return TopologyNode(
            node_id=node_id,
            label=node_id,
            designator=node_id,
            role="SINK",
            x=x,
            y=100.0,
            width=40.0,
            height=20.0,
            config_label="",
            has_error=False,
            bounds=(x, 100.0, 40.0, 20.0),
            ports=[
                TopologyPort(
                    terminal="P",
                    net=net,
                    label=net,
                    side=side,
                    x=x,
                    y=110.0,
                    node_id=node_id,
                    wire_x=wire_x,
                ),
            ],
        )

    model = TopologyModel(
        nodes=[
            _gap_node("A1", 100.0, "NET_A", "right", 120.0),
            _gap_node("A2", 300.0, "NET_A", "left", 280.0),
            _gap_node("B1", 100.0, "NET_B", "right", 120.0),
            _gap_node("B2", 300.0, "NET_B", "left", 280.0),
        ],
        wires=[
            TopologyWire(
                net="NET_A",
                path_d="M 100.0,10.0 V 90.0",
                routing_kind="hub",
            ),
            TopologyWire(
                net="NET_A",
                path_d="M 50.0,200.0 H 150.0",
                routing_kind="hub_row",
            ),
            TopologyWire(
                net="NET_B",
                path_d="M 80.0,50.0 H 120.0",
                routing_kind="gutter",
            ),
        ],
    )
    issues = check_gutter_wire_crossings(model)
    assert any(i["code"] == "foreign_wire_crossing" for i in issues)


def test_check_gutter_wire_crossings_includes_hub_nets():
    """Hub-routed nets in a shared gutter are checked against gutter pairs."""
    def _gap_node(node_id: str, x: float, net: str, side: str, wire_x: float) -> TopologyNode:
        return TopologyNode(
            node_id=node_id,
            label=node_id,
            designator=node_id,
            role="SINK",
            x=x,
            y=100.0,
            width=40.0,
            height=20.0,
            config_label="",
            has_error=False,
            bounds=(x, 100.0, 40.0, 20.0),
            ports=[
                TopologyPort(
                    terminal="P",
                    net=net,
                    label=net,
                    side=side,
                    x=x,
                    y=110.0,
                    node_id=node_id,
                    wire_x=wire_x,
                ),
            ],
        )

    model = TopologyModel(
        nodes=[
            _gap_node("H1", 100.0, "NET_HUB", "right", 120.0),
            _gap_node("H2", 200.0, "NET_HUB", "left", 180.0),
            _gap_node("H3", 300.0, "NET_HUB", "left", 280.0),
            _gap_node("B1", 100.0, "NET_B", "right", 120.0),
            _gap_node("B2", 300.0, "NET_B", "left", 280.0),
        ],
        wires=[
            TopologyWire(
                net="NET_HUB",
                path_d="M 100.0,10.0 V 90.0",
                routing_kind="hub",
            ),
            TopologyWire(
                net="NET_HUB",
                path_d="M 50.0,200.0 H 150.0",
                routing_kind="hub_row",
            ),
            TopologyWire(
                net="NET_B",
                path_d="M 80.0,50.0 H 120.0",
                routing_kind="gutter",
            ),
        ],
    )
    issues = check_gutter_wire_crossings(model)
    assert any(i["code"] == "foreign_wire_crossing" for i in issues)


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


def _two_column_model(*, path_d: str, routing_kind: str = "gutter") -> TopologyModel:
    from fypa.topology.constants import NODE_W

    left = TopologyNode(
        node_id="J3",
        label="J3",
        designator="J3",
        role="SOURCE",
        x=36.0,
        y=100.0,
        width=NODE_W,
        height=80.0,
        config_label="",
        has_error=False,
        bounds=(36.0, 100.0, NODE_W, 80.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="SIG",
                label="SIG",
                side="right",
                x=36.0 + NODE_W,
                y=120.0,
                node_id="J3",
            ),
        ],
    )
    right = TopologyNode(
        node_id="U4",
        label="U4",
        designator="U4",
        role="SINK",
        x=264.0,
        y=100.0,
        width=NODE_W,
        height=80.0,
        config_label="",
        has_error=False,
        bounds=(264.0, 100.0, NODE_W, 80.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="SIG",
                label="SIG",
                side="left",
                x=264.0,
                y=120.0,
                node_id="U4",
            ),
        ],
    )
    wire = TopologyWire(
        net="SIG",
        path_d=path_d,
        src_node="J3",
        src_terminal="P",
        dst_node="U4",
        dst_terminal="P",
        routing_kind=routing_kind,
        bus_x=220.0,
    )
    return TopologyModel(
        nodes=[left, right],
        wires=[wire],
        width=500.0,
        height=300.0,
    )


def test_vertical_bus_outside_column_gap_detects_symbol_column():
    from fypa.topology.validate.segments import check_vertical_bus_column_gaps

    model = _two_column_model(path_d="M 100.0,120.0 V 180.0 H 244.0")
    issues = check_vertical_bus_column_gaps(model)
    assert any(i["code"] == "vertical_bus_outside_column_gap" for i in issues)
    assert issues[0]["x"] == 100.0


def test_vertical_bus_column_gap_allows_gap_bus_and_port_stub():
    from fypa.topology.validate.segments import check_vertical_bus_column_gaps

    model = _two_column_model(path_d="M 164.0,120.0 H 220.0 V 180.0 H 244.0")
    assert not check_vertical_bus_column_gaps(model)

    model = _two_column_model(
        path_d="M 264.0,120.0 V 180.0 H 220.0",
        routing_kind="hub_tap",
    )
    model.wires[0].src_node = "U4"
    model.wires[0].src_terminal = "P"
    assert not check_vertical_bus_column_gaps(model)


def test_hub_net_disconnected_detected():
    from fypa.topology.validate.hub import check_hub_net_disconnected
    from fypa.topology.geometry import compute_schematic_geometry

    left = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="REGULATOR",
        x=100.0,
        y=200.0,
        width=80.0,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(100.0, 200.0, 80.0, 40.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="right",
                x=120.0,
                y=220.0,
                node_id="U1",
            ),
        ],
    )
    right = TopologyNode(
        node_id="U2",
        label="U2",
        designator="U2",
        role="REGULATOR",
        x=300.0,
        y=200.0,
        width=80.0,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 200.0, 80.0, 40.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="left",
                x=320.0,
                y=220.0,
                node_id="U2",
            ),
        ],
    )
    orphan = TopologyNode(
        node_id="U3",
        label="U3",
        designator="U3",
        role="REGULATOR",
        x=300.0,
        y=300.0,
        width=80.0,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 300.0, 80.0, 40.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="left",
                x=320.0,
                y=320.0,
                node_id="U3",
            ),
        ],
    )
    row_wire = TopologyWire(
        net="VDD",
        path_d="M 120.0,220.0 H 320.0",
        routing_kind="hub_row",
        bus_x=400.0,
    )
    model = TopologyModel(
        nodes=[left, right, orphan],
        wires=[row_wire],
        width=500.0,
        height=300.0,
    )
    geo = compute_schematic_geometry(model.wires)
    issues = check_hub_net_disconnected(model, geo)
    assert any(i["code"] == "hub_net_disconnected" for i in issues)
    assert issues[0]["net"] == "VDD"


def test_hub_net_ports_connected_treats_stub_as_port_body():
    from fypa.topology.geometry import compute_schematic_geometry
    from fypa.topology.validate.hub import hub_net_ports_connected

    left = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="REGULATOR",
        x=100.0,
        y=200.0,
        width=80.0,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(100.0, 200.0, 80.0, 40.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="right",
                x=120.0,
                y=220.0,
                node_id="U1",
                wire_x=140.0,
            ),
        ],
    )
    right = TopologyNode(
        node_id="U2",
        label="U2",
        designator="U2",
        role="REGULATOR",
        x=300.0,
        y=200.0,
        width=80.0,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 200.0, 80.0, 40.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="left",
                x=320.0,
                y=220.0,
                node_id="U2",
                wire_x=300.0,
            ),
        ],
    )
    row_wire = TopologyWire(
        net="VDD",
        path_d="M 120.0,220.0 H 140.0 H 300.0",
        routing_kind="hub_row",
        bus_x=400.0,
    )
    model = TopologyModel(
        nodes=[left, right],
        wires=[row_wire],
        width=500.0,
        height=300.0,
    )
    geo = compute_schematic_geometry(model.wires)
    assert hub_net_ports_connected(model, geo, "VDD")


def test_hub_orphaned_detected_without_hub_row_wire():
    from fypa.topology.geometry import compute_schematic_geometry
    from fypa.topology.validate.hub import check_hub_net_disconnected

    left = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="REGULATOR",
        x=100.0,
        y=200.0,
        width=80.0,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(100.0, 200.0, 80.0, 40.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="right",
                x=120.0,
                y=220.0,
                node_id="U1",
            ),
        ],
    )
    right = TopologyNode(
        node_id="U2",
        label="U2",
        designator="U2",
        role="REGULATOR",
        x=300.0,
        y=200.0,
        width=80.0,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 200.0, 80.0, 40.0),
        ports=[
            TopologyPort(
                terminal="P",
                net="VDD",
                label="VDD",
                side="left",
                x=320.0,
                y=220.0,
                node_id="U2",
            ),
        ],
    )
    tap_wire = TopologyWire(
        net="VDD",
        path_d="M 120.0,220.0 H 200.0",
        routing_kind="hub_tap",
        bus_x=400.0,
    )
    model = TopologyModel(
        nodes=[left, right],
        wires=[tap_wire],
        width=500.0,
        height=300.0,
    )
    geo = compute_schematic_geometry(model.wires)
    issues = check_hub_net_disconnected(model, geo)
    assert any(i["code"] == "hub_net_disconnected" for i in issues)
