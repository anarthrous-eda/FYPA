"""Tests for the PDN topology schematic."""

import json

from fypa.topology import (
    GND_NET,
    build_topology_model,
    render_topology_svg,
)


from tests.topology_fixtures import front_like_metadata as _front_like_metadata

def test_topology_model_has_nodes_and_wires():
    model = build_topology_model(_front_like_metadata())
    directive_nodes = [n for n in model.nodes if n.role != "GND"]
    assert len(directive_nodes) == 4
    assert any(n.role == "REGULATOR" for n in directive_nodes)
    assert len(model.wires) >= 3


def test_topology_marks_errored_directive():
    model = build_topology_model(_front_like_metadata())
    u2 = next(n for n in model.nodes if n.designator == "U2")
    assert u2.has_error is True
    j1 = next(n for n in model.nodes if n.designator == "J1")
    assert j1.has_error is False


def test_topology_svg_contains_designators_and_ports():
    model = build_topology_model(_front_like_metadata())
    svg = render_topology_svg(model)
    assert "J1" in svg
    assert "U2" in svg
    assert "VDD_3V3_PWR" in svg or "VDD_3V3" in svg
    assert "<path" in svg
    assert "<svg" in svg


def test_topology_nodes_have_input_output_ports():
    model = build_topology_model(_front_like_metadata())
    j1 = next(n for n in model.nodes if n.designator == "J1")
    sides = {p.side for p in j1.ports}
    assert "left" in sides and "right" in sides
    reg = next(n for n in model.nodes if n.designator == "U2")
    assert sum(1 for p in reg.ports if p.side == "left") == 2
    assert sum(1 for p in reg.ports if p.side == "right") == 2


def test_gnd_bus_aligns_with_leftmost_drop():
    model = build_topology_model(_front_like_metadata())
    from fypa.topology.routing import port_stub_x

    gnd_ports = [p for n in model.nodes for p in n.ports if p.net == GND_NET]
    bus_min = min(port_stub_x(p) for p in gnd_ports)
    bus_wire = next(
        w for w in model.wires
        if w.net == GND_NET and w.path_d.count("V") == 0
    )
    assert bus_wire.path_d.startswith(f"M {bus_min:.1f},")
    assert model.gnd_symbol_x == bus_min


def test_topology_gnd_symbol_and_wires():
    model = build_topology_model(_front_like_metadata())
    assert not any(n.node_id == GND_NET for n in model.nodes)
    assert model.gnd_symbol_x is not None
    assert model.gnd_bus_y is not None
    gnd_wires = [w for w in model.wires if w.net == GND_NET]
    assert len(gnd_wires) >= 1
    svg = render_topology_svg(model)
    assert "GND" in svg


def test_topology_svg_shows_values_in_tooltips():
    model = build_topology_model(_front_like_metadata())
    j1 = next(n for n in model.nodes if n.designator == "J1")
    u1 = next(n for n in model.nodes if n.designator == "U1")
    assert "5 V" in j1.tooltip
    assert "50 mA" in u1.tooltip
    svg = render_topology_svg(model)
    assert "50 mA" not in svg


def test_topology_svg_has_no_solve_overlay():
    model = build_topology_model(_front_like_metadata())
    svg = render_topology_svg(model)
    assert "(not solved)" not in svg
    assert "configuration only" not in svg


def test_topology_single_net_hides_ideal_return():
    meta = {
        "directives": [
            {
                "role": "SOURCE",
                "designator": "J21",
                "label": "J21",
                "value_str": "1 V",
                "terminals": {
                    "P": {
                        "requested_net": "P1V_SINGLE",
                        "pins": [{"net": "P1V_SINGLE", "pad": "1"}],
                    },
                    "N": {"ideal_return": True, "pin_count": 0, "pins": []},
                },
            },
            {
                "role": "SINK",
                "designator": "J22",
                "label": "J22",
                "value_str": "1000 mA",
                "terminals": {
                    "P": {
                        "requested_net": "P1V_SINGLE",
                        "pins": [{"net": "P1V_SINGLE", "pad": "1"}],
                    },
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "2"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    j21 = next(n for n in model.nodes if n.designator == "J21")
    assert len(j21.ports) == 1
    assert j21.ports[0].side == "right"
    assert "single-net" in j21.tooltip
    svg = render_topology_svg(model)
    assert "ideal" not in svg.lower()
    gnd_ports = [p for p in model.nodes if p.role != "GND" for p in p.ports if p.net == GND_NET]
    assert all(p.node_id != "J21" for p in gnd_ports)


def test_topology_regulator_has_jump_row():
    model = build_topology_model(_front_like_metadata())
    u2 = next(n for n in model.nodes if n.designator == "U2")
    assert u2.jump_row is not None
    assert u2.jump_row.get("x_mm") == 3.0


def test_topology_merges_multi_channel_sink_into_one_symbol():
    meta = {
        "directives": [
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#1",
                "channel_index": 1,
                "value_str": "100 mA",
                "terminals": {
                    "P": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "B2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#2",
                "channel_index": 2,
                "value_str": "50 mA",
                "terminals": {
                    "P": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "C2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SOURCE",
                "designator": "J1",
                "label": "J1",
                "value_str": "3.3 V",
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "2"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    u4_nodes = [n for n in model.nodes if n.designator == "U4"]
    assert len(u4_nodes) == 1
    u4 = u4_nodes[0]
    assert u4.label == "U4"
    p_ports = [p for p in u4.ports if p.terminal.startswith("P")]
    assert len(p_ports) == 2
    n_ports = [p for p in u4.ports if p.terminal.startswith("N")]
    assert len(n_ports) == 1
    extern = [w for w in model.wires if w.dashed and w.label == "extern"]
    assert len(extern) >= 2


def test_topology_external_stub_when_no_upstream_driver():
    meta = {
        "directives": [
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4",
                "value_str": "10 mA",
                "terminals": {
                    "P": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "1"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "2"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    assert any(w.dashed and w.label == "extern" for w in model.wires)
    svg = render_topology_svg(model)
    assert "extern" in svg


def test_topology_format_resistance_compact():
    from fypa.topology import format_directive_value, reformat_legacy_value_str

    assert format_directive_value(
        {"role": "RESISTOR", "value": 10.0, "unit": "Ohm"},
    ) == "10 \u03a9"
    assert format_directive_value(
        {"role": "RESISTOR", "value": 1000.0, "unit": "Ohm"},
    ) == "1 k\u03a9"
    assert reformat_legacy_value_str("1e+04 mOhm") == "10 \u03a9"
    assert format_directive_value(
        {"role": "SINK", "value": 0.05, "unit": "A"},
    ) == "50 mA"


def test_topology_wires_in_same_gutter_use_distinct_buses():
    """Nets sharing a column gap must not draw on top of each other."""
    meta = {
        "directives": [
            {
                "role": "RESISTOR",
                "designator": "D1",
                "label": "D1#1",
                "channel_index": 1,
                "value": 10.0,
                "unit": "Ohm",
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "2"}]},
                },
            },
            {
                "role": "RESISTOR",
                "designator": "D1",
                "label": "D1#2",
                "channel_index": 2,
                "value": 10.0,
                "unit": "Ohm",
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "3"}]},
                },
            },
            {
                "role": "RESISTOR",
                "designator": "D1",
                "label": "D1#3",
                "channel_index": 3,
                "value": 10.0,
                "unit": "Ohm",
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "LED_B", "pins": [{"net": "LED_B", "pad": "4"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#1",
                "channel_index": 1,
                "value": 0.1,
                "unit": "A",
                "terminals": {
                    "P": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "B2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#2",
                "channel_index": 2,
                "value": 0.1,
                "unit": "A",
                "terminals": {
                    "P": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "C2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#3",
                "channel_index": 3,
                "value": 0.1,
                "unit": "A",
                "terminals": {
                    "P": {"requested_net": "LED_B", "pins": [{"net": "LED_B", "pad": "D2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)

    def _vertical_bus_x(path_d: str, net: str) -> float:
        from fypa.topology import parse_wire_path, path_to_segments

        verts = [
            s for s in path_to_segments(net, parse_wire_path(path_d))
            if s.orient == "V"
        ]
        assert len(verts) == 1, path_d
        return verts[0].x1

    led_bus = {
        _vertical_bus_x(w.path_d, w.net)
        for w in model.wires
        if w.net in {"LED_R", "LED_G", "LED_B"}
    }
    assert len(led_bus) == 3


def test_series_shared_anode_merges_to_one_left_port():
    """Multi-channel SERIES with the same P pad shows one shared input."""
    meta = {
        "net_canonical": {"VDD_3V3": "VDD_3V3_PWR", "VDD_3V3_PWR": "VDD_3V3_PWR"},
        "directives": [
            {
                "role": "SERIES",
                "designator": "D1",
                "label": "D1#1",
                "channel_index": 1,
                "terminals": {
                    "P": {"requested_net": "VDD_3V3", "pins": [{"net": "VDD_3V3_PWR", "pad": "4"}]},
                    "N": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "1"}]},
                },
            },
            {
                "role": "SERIES",
                "designator": "D1",
                "label": "D1#2",
                "channel_index": 2,
                "terminals": {
                    "P": {"requested_net": "VDD_3V3", "pins": [{"net": "VDD_3V3_PWR", "pad": "4"}]},
                    "N": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "2"}]},
                },
            },
            {
                "role": "SERIES",
                "designator": "D1",
                "label": "D1#3",
                "channel_index": 3,
                "terminals": {
                    "P": {"requested_net": "VDD_3V3", "pins": [{"net": "VDD_3V3_PWR", "pad": "4"}]},
                    "N": {"requested_net": "LED_B", "pins": [{"net": "LED_B", "pad": "3"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    d1 = next(n for n in model.nodes if n.designator == "D1")
    left = [p for p in d1.ports if p.side == "left"]
    right = sorted((p for p in d1.ports if p.side == "right"), key=lambda p: p.y)
    assert len(left) == 1
    assert left[0].terminal == "P"
    assert len(right) == 3
    assert right[0].y == left[0].y


def test_stacked_column_wires_route_beside_symbol():
    """Wires between stacked nodes must not pass through the symbol body."""
    from fypa.topology import parse_wire_path, path_to_segments

    meta = {
        "directives": [
            {
                "role": "RESISTOR",
                "designator": "D1",
                "label": "D1#1",
                "channel_index": 1,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "2"}]},
                },
            },
            {
                "role": "RESISTOR",
                "designator": "D1",
                "label": "D1#2",
                "channel_index": 2,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "3"}]},
                },
            },
            {
                "role": "RESISTOR",
                "designator": "D1",
                "label": "D1#3",
                "channel_index": 3,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "LED_B", "pins": [{"net": "LED_B", "pad": "4"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#1",
                "channel_index": 1,
                "terminals": {
                    "P": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "B2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#2",
                "channel_index": 2,
                "terminals": {
                    "P": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "C2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#3",
                "channel_index": 3,
                "terminals": {
                    "P": {"requested_net": "LED_B", "pins": [{"net": "LED_B", "pad": "D2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    d1 = next(n for n in model.nodes if n.designator == "D1")
    u4 = next(n for n in model.nodes if n.designator == "U4")
    assert d1.x == u4.x
    interior_lo = d1.x + 4
    interior_hi = d1.x + d1.width - 4
    led_wires = [w for w in model.wires if w.net in {"LED_R", "LED_G", "LED_B"}]
    assert led_wires
    for w in led_wires:
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient != "V":
                continue
            assert not (interior_lo <= seg.x1 <= interior_hi), (
                f"{w.net} vertical x={seg.x1} crosses {d1.label} body"
            )


def test_series_distinct_anode_pads_stay_separate():
    """Do not merge P ports when channels use different physical pads."""
    meta = {
        "directives": [
            {
                "role": "SERIES",
                "designator": "D2",
                "label": "D2#1",
                "channel_index": 1,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "1"}]},
                    "N": {"requested_net": "NET_A", "pins": [{"net": "NET_A", "pad": "2"}]},
                },
            },
            {
                "role": "SERIES",
                "designator": "D2",
                "label": "D2#2",
                "channel_index": 2,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "3"}]},
                    "N": {"requested_net": "NET_B", "pins": [{"net": "NET_B", "pad": "4"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    d2 = next(n for n in model.nodes if n.designator == "D2")
    left = sorted((p for p in d2.ports if p.side == "left"), key=lambda p: p.y)
    right = sorted((p for p in d2.ports if p.side == "right"), key=lambda p: p.y)
    assert len(left) == 2
    assert left[0].y == right[0].y
    assert left[1].y == right[1].y


def test_stacked_led_wires_use_distinct_buses():
    """LED nets from D1 to U4 below must not share the same vertical bus."""
    from fypa.topology import parse_wire_path, path_to_segments

    meta = {
        "directives": [
            {
                "role": "SERIES",
                "designator": "D1",
                "label": "D1#1",
                "channel_index": 1,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "4"}]},
                    "N": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "1"}]},
                },
            },
            {
                "role": "SERIES",
                "designator": "D1",
                "label": "D1#2",
                "channel_index": 2,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "4"}]},
                    "N": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "2"}]},
                },
            },
            {
                "role": "SERIES",
                "designator": "D1",
                "label": "D1#3",
                "channel_index": 3,
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD", "pad": "4"}]},
                    "N": {"requested_net": "LED_B", "pins": [{"net": "LED_B", "pad": "3"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#1",
                "channel_index": 1,
                "terminals": {
                    "P": {"requested_net": "LED_R", "pins": [{"net": "LED_R", "pad": "B2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#2",
                "channel_index": 2,
                "terminals": {
                    "P": {"requested_net": "LED_G", "pins": [{"net": "LED_G", "pad": "C2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U4",
                "label": "U4#3",
                "channel_index": 3,
                "terminals": {
                    "P": {"requested_net": "LED_B", "pins": [{"net": "LED_B", "pad": "D2"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "D1"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    d1 = next(n for n in model.nodes if n.designator == "D1")
    u4 = next(n for n in model.nodes if n.designator == "U4")
    assert d1.x == u4.x
    led_wires = [w for w in model.wires if w.net in {"LED_R", "LED_G", "LED_B"}]
    assert len(led_wires) == 3
    interior_lo = d1.x + 4
    interior_hi = d1.x + d1.width - 4
    bus_xs: set[float] = set()
    for w in led_wires:
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient == "V":
                assert not (interior_lo <= seg.x1 <= interior_hi)
                bus_xs.add(round(seg.x1, 1))
    assert len(bus_xs) == 3


def test_sandbox_gutter_verticals_have_min_parallel_gap():
    """Parallel vertical buses in the column gutter stay at least 16px apart."""
    from fypa.topology import MIN_PARALLEL_GAP, topology_wiring_report
    from tests.topology_fixtures import load_topology_fixture

    report = topology_wiring_report(
        build_topology_model(load_topology_fixture("sandbox_subset")),
    )
    bus_xs = sorted({
        w["bus_x"]
        for w in report["wires"]
        if w.get("routing_kind") == "gutter" and w.get("bus_x") is not None
    })
    assert len(bus_xs) >= 2
    gaps = [bus_xs[i] - bus_xs[i - 1] for i in range(1, len(bus_xs))]
    assert min(gaps) >= MIN_PARALLEL_GAP - 0.6


def test_sandbox_parallel_verticals_clear_gnd_drops():
    """Signal gutter buses must not crowd sink GND drop verticals."""
    from fypa.topology import GND_NET, MIN_PARALLEL_GAP, topology_wiring_report
    from tests.topology_fixtures import load_topology_fixture

    report = topology_wiring_report(
        build_topology_model(load_topology_fixture("sandbox_subset")),
    )
    signal_xs = sorted({
        s["x1"]
        for w in report["wires"]
        for s in w["segments"]
        if s["orient"] == "V" and w["net"] != GND_NET
    })
    gnd_xs = sorted({
        s["x1"]
        for w in report["wires"]
        for s in w["segments"]
        if s["orient"] == "V" and w["net"] == GND_NET
    })
    assert signal_xs and gnd_xs
    for sx in signal_xs:
        for gx in gnd_xs:
            assert abs(sx - gx) >= MIN_PARALLEL_GAP - 0.6, (
                f"signal vertical x={sx} only {abs(sx - gx):.1f}px from GND drop x={gx}"
            )


def test_sandbox_topology_draws_bridge_arcs_and_gutter_labels():
    """Sandbox signal wires crossing the GND bus get bridge arcs; labels sit on gutter runs."""
    from fypa.topology import parse_wire_path, path_to_segments, topology_wiring_report
    from tests.topology_fixtures import load_topology_fixture

    model = build_topology_model(load_topology_fixture("sandbox_subset"))
    svg = render_topology_svg(model)
    report = topology_wiring_report(model)

    unique_crossings = {
        (c["x"], c["y"]) for c in report["schematic"]["bridge_crossings"]
    }
    assert len(unique_crossings) >= 4
    assert " A " in svg
    assert svg.count(" A ") >= 10

    gutter = next(
        w for w in model.wires
        if w.routing_kind == "gutter" and w.net == "P1V_1W100L"
    )
    assert not gutter.label_vertical
    segs = path_to_segments(gutter.net, parse_wire_path(gutter.path_d))
    horiz = [s for s in segs if s.orient == "H"]
    best = max(horiz, key=lambda s: abs(s.x2 - s.x1))
    assert best.x1 <= gutter.label_x <= best.x2
    assert gutter.label_y < best.y1 - 4 or gutter.label_y > best.y1 + 4


def test_topology_wiring_report_structure():
    from fypa.topology import topology_wiring_report, topology_wiring_report_json

    model = build_topology_model(_front_like_metadata())
    report = topology_wiring_report(model)
    assert report["version"] == 1
    assert report["summary"]["wires"] == len(model.wires)
    assert report["summary"]["ports"] > 0
    assert len(report["ports"]) == report["summary"]["ports"]
    for w in report["wires"]:
        assert "path_d" in w
        assert "vertices" in w
        assert "segments" in w
        assert "routing_kind" in w
        assert w["routing_kind"]
    assert "junctions" in report["schematic"]
    assert "bridge_crossings" in report["schematic"]
    parsed = json.loads(topology_wiring_report_json(model))
    assert parsed["summary"]["issues"] == sum(
        1 for i in parsed["issues"] if i.get("severity", "error") != "warning"
    )


def test_topology_wiring_report_detects_backtrack():
    from fypa.topology import TopologyWire, topology_wiring_report

    model = build_topology_model(_front_like_metadata())
    model.wires.append(TopologyWire(
        net="FAKE",
        path_d="M 10.0,10.0 H 50.0 H 30.0 H 40.0",
        routing_kind="test",
    ))
    codes = {i["code"] for i in topology_wiring_report(model)["issues"]}
    assert "horizontal_backtrack" in codes
    assert "dangling_start" in codes


def test_gutter_wire_same_row_has_no_vertical_segment():
    """Source and sink on one row route as a flat horizontal."""
    from fypa.topology import topology_wiring_report

    model = build_topology_model(_front_like_metadata())
    gutter_wires = [
        w for w in model.wires
        if w.routing_kind == "gutter" and w.net == "VIN"
    ]
    assert len(gutter_wires) == 1
    assert " V " not in gutter_wires[0].path_d
    assert topology_wiring_report(model)["summary"]["issues"] == 0


def test_sanitize_metadata_strips_prepared_shapes():
    import pickle

    import shapely.prepared as sp
    from shapely.geometry import Point

    from fypa.cli import sanitize_metadata_for_pickle

    meta = {
        "primitives": [{"_prepared_shape": sp.prep(Point(0, 0).buffer(1))}],
        "all_copper": [[{"_prepared_shape_cache": sp.prep(Point(1, 1).buffer(1))}]],
    }
    clean = sanitize_metadata_for_pickle(meta)
    pickle.dumps({"metadata": clean})
    assert "_prepared_shape" not in str(clean)
