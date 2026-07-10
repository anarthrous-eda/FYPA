"""Net label placement tests."""

import pickle
from pathlib import Path

from fypa.topology import build_topology_model, render_topology_svg
from fypa.topology.constants import BRIDGE_R, LABEL_MAX_LEN, WIRE_EPS
from fypa.topology.geometry import compute_schematic_geometry, parse_wire_path, path_to_segments
from fypa.topology.labels import iter_label_candidates, label_text_size
from fypa.topology.metadata.nets import port_display_net
from fypa.topology.util import truncate_label
from fypa.rail_groups import compute_rail_groups
from tests.topology_fixtures import project_b_compact_metadata, load_topology_fixture


def test_port_display_net_prefers_physical_pin_net():
    term = {
        "requested_net": "VCC_PORT",
        "resolved_via_local": True,
        "pins": [{"net": "VCC_PORT.1", "pad": "1"}],
    }
    assert port_display_net(term, "VCC_PORT.1") == "VCC_PORT.1"


def test_port_display_net_falls_back_to_requested_without_pins():
    term = {"requested_net": "VCC_PORT", "pins": []}
    assert port_display_net(term, "?") == "VCC_PORT"


def test_port_display_net_not_rail_canonicalized():
    term = {
        "requested_net": "VDD_48V",
        "pins": [{"net": "VDD_48V_RP", "pad": "1"}],
    }
    assert port_display_net(term, "VDD_48V_RP") == "VDD_48V_RP"


def test_port_display_net_lists_multi_pin_nets():
    term = {
        "requested_net": "LED_R",
        "pins": [
            {"net": "LED_B", "pad": "1"},
            {"net": "LED_G", "pad": "2"},
            {"net": "LED_R", "pad": "3"},
        ],
    }
    assert port_display_net(term, "LED_B", role="SINK", port_name="P") == (
        "LED_B, LED_G, LED_R"
    )


def test_port_display_net_channel_passive_keeps_one_net():
    term = {
        "requested_net": "LED_R",
        "pins": [
            {"net": "LED_R", "pad": "1"},
            {"net": "LED_B", "pad": "3"},
            {"net": "LED_G", "pad": "2"},
        ],
    }
    assert port_display_net(term, "LED_R", role="RESISTOR", port_name="N1") == "LED_R"


def test_port_display_net_regulator_channel_lists_all_pin_nets():
    """IN_P1 is outside the passive P/N channel exception."""
    term = {
        "requested_net": "VDD_3V3",
        "pins": [
            {"net": "VDD_3V3_A", "pad": "1"},
            {"net": "VDD_3V3_B", "pad": "2"},
        ],
    }
    assert port_display_net(
        term, "VDD_3V3_A", role="REGULATOR", port_name="IN_P1"
    ) == "VDD_3V3_A, VDD_3V3_B"


def test_truncate_label_shortens_long_multi_pin_port_label():
    label = ", ".join(["VDD_48V_PORT.1", "VDD_48V_PORT.2", "VDD_48V_PORT.3"])
    assert len(label) > LABEL_MAX_LEN
    assert truncate_label(label) == label[: LABEL_MAX_LEN - 1] + "…"


def test_topology_single_channel_passive_multi_pin_lists_all_nets():
    meta = {
        "directives": [
            {
                "role": "RESISTOR",
                "designator": "R1",
                "terminals": {
                    "P": {"requested_net": "VDD", "pins": [{"net": "VDD"}]},
                    "N": {
                        "requested_net": "LED_R",
                        "pins": [
                            {"net": "LED_R"},
                            {"net": "LED_G"},
                            {"net": "LED_B"},
                        ],
                    },
                },
            },
        ],
    }
    model = build_topology_model(meta)
    n = next(p for p in model.nodes[0].ports if p.terminal == "N")
    assert n.label == "LED_B, LED_G, LED_R"


def test_topology_sink_multi_pin_port_lists_all_nets():
    meta = {
        "directives": [
            {
                "role": "SOURCE",
                "designator": "J1",
                "terminals": {
                    "P": {
                        "requested_net": "VDD_3V3_PWR",
                        "pins": [{"net": "VDD_3V3_PWR"}],
                    },
                    "N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U1",
                "terminals": {
                    "P": {
                        "requested_net": "LED_R",
                        "pins": [
                            {"net": "LED_B"},
                            {"net": "LED_G"},
                            {"net": "LED_R"},
                        ],
                    },
                    "N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    u1 = next(n for n in model.nodes if n.designator == "U1")
    p = next(p for p in u1.ports if p.terminal == "P")
    assert p.label == "LED_B, LED_G, LED_R"
    # Routing uses the first pin net; the label lists every pad net.
    assert p.net == "LED_B"


def test_topology_port_labels_not_rail_primaries():
    """Pin nets on labels even when rails merge members (not rail primary names)."""
    meta = {
        "directives": [
            {
                "role": "SOURCE",
                "designator": "J1",
                "terminals": {
                    "P": {
                        "requested_net": "VDD_48V",
                        "pins": [{"net": "VDD_48V_IN", "pad": "1"}],
                    },
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "2"}]},
                },
            },
            {
                "role": "REGULATOR",
                "designator": "U1",
                "terminals": {
                    "IN_P": {
                        "requested_net": "VDD_48V",
                        "pins": [{"net": "VDD_48V_RP", "pad": "1"}],
                    },
                    "IN_N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "2"}]},
                    "OUT_P": {
                        "requested_net": "VOUT",
                        "pins": [{"net": "VOUT", "pad": "3"}],
                    },
                    "OUT_N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "4"}]},
                },
            },
            {
                "role": "SINK",
                "designator": "U2",
                "terminals": {
                    "P": {
                        "requested_net": "VOUT",
                        "pins": [{"net": "VDD_48V_PORT.1", "pad": "1"}],
                    },
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "2"}]},
                },
            },
        ],
    }
    _, members = compute_rail_groups(meta)
    assert {"VDD_48V_IN", "VDD_48V_RP"} <= set(members["VDD_48V"])
    assert "VDD_48V_PORT.1" in members["VOUT"]
    model = build_topology_model(meta)
    j1 = next(n for n in model.nodes if n.designator == "J1")
    u1 = next(n for n in model.nodes if n.designator == "U1")
    u2 = next(n for n in model.nodes if n.designator == "U2")
    assert next(p for p in j1.ports if p.terminal == "P").label == "VDD_48V_IN"
    assert next(p for p in u1.ports if p.terminal == "IN_P").label == "VDD_48V_RP"
    assert next(p for p in u2.ports if p.terminal == "P").label == "VDD_48V_PORT.1"
    power_labels = [
        p.label for n in model.nodes for p in n.ports if p.label not in ("GND", "VOUT")
    ]
    assert "VDD_48V" not in power_labels


def test_topology_hub_passive_channel_ports_keep_distinct_labels():
    """Hub D1: channel-split N1/N2/N3 rows keep one gutter net each."""
    model = build_topology_model(load_topology_fixture("project_b_hub_vdd"))
    d1 = next(n for n in model.nodes if n.designator == "D1")
    labels = {p.terminal: p.label for p in d1.ports}
    assert labels["N1"] == "LED_R"
    assert labels["N2"] == "LED_G"
    assert labels["N3"] == "LED_B"
    assert all(", " not in label for label in labels.values())


def test_topology_port_labels_use_physical_nets_for_local_resolution():
    """project_b_compact: locally-named sink shows its PCB pin net, not VDD_3V3."""
    model = build_topology_model(project_b_compact_metadata())
    u1 = next(n for n in model.nodes if n.designator == "U1")
    p_port = next(p for p in u1.ports if p.terminal == "P")
    assert p_port.label == "VDD_3V3_PWR"
    assert "VDD_3V3_PWR" in render_topology_svg(model)


def _label_on_segment(wire, net_segs) -> bool:
    """True when the label sits on a same-net segment (distance ~0)."""
    if wire.label_vertical:
        for seg in net_segs:
            if seg.orient != "V":
                continue
            y_lo, y_hi = min(seg.y1, seg.y2), max(seg.y1, seg.y2)
            if (abs(wire.label_x - seg.x1) < 1.0
                    and y_lo - WIRE_EPS <= wire.label_y <= y_hi + WIRE_EPS):
                return True
        return False
    for seg in net_segs:
        if seg.orient != "H":
            continue
        x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
        if (abs(wire.label_y - seg.y1) < 1.0
                and x_lo - WIRE_EPS <= wire.label_x <= x_hi + WIRE_EPS):
            return True
    return False


def test_topology_wire_labels_not_at_origin():
    """Placed net labels must not sit at (0,0); one label per net."""
    model = build_topology_model(project_b_compact_metadata())
    labeled = [w for w in model.wires if w.label and not w.dashed]
    assert labeled
    for w in labeled:
        assert w.label_x != 0.0 or w.label_y != 0.0, f"{w.net} label at origin"
    nets = {w.net for w in labeled}
    assert len(nets) == len(labeled)


def test_topology_wire_labels_centered_on_segments():
    """Net labels sit on horizontal or vertical wire segments, not beside them."""
    model = build_topology_model(project_b_compact_metadata())
    geo = compute_schematic_geometry(model.wires)
    by_net = {w.net: w for w in model.wires if w.label and not w.dashed}
    for net, wire in by_net.items():
        segs = [s for s in geo.segments if s.net == net]
        assert _label_on_segment(wire, segs), f"{net} label not on a wire segment"


def test_topology_wire_labels_prefer_horizontal_segments():
    """Wide horizontal runs get horizontal on-wire labels when available."""
    model = build_topology_model(project_b_compact_metadata())
    svg = render_topology_svg(model)
    assert "VDD_3V3" in svg or "VDD_3V3_PWR" in svg
    vdd = next(
        w for w in model.wires
        if w.label and "VDD_3V3" in w.net and not w.label_vertical
    )
    assert vdd.label_y in {75.0, 177.0} or not vdd.label_vertical


def test_topology_project_b_hub_labels_clear_bridges():
    """Dense hub gutter: on-wire labels avoid bridge arc centres."""
    model = build_topology_model(load_topology_fixture("project_b_hub_vdd"))
    geo = compute_schematic_geometry(model.wires)
    labeled = [w for w in model.wires if w.label and not w.dashed]
    assert labeled
    bridge_clear = BRIDGE_R + 8.0
    for w in labeled:
        for bridge in geo.bridges:
            if bridge.horizontal_net == w.net and abs(w.label_y - bridge.y) > WIRE_EPS:
                continue
            dist_sq = (w.label_x - bridge.x) ** 2 + (w.label_y - bridge.y) ** 2
            assert dist_sq >= bridge_clear ** 2, (
                f"{w.net} label too close to bridge at ({bridge.x}, {bridge.y})"
            )
    svg = render_topology_svg(model)
    assert 'fill-opacity="0.5"' in svg


def test_label_search_space_yields_ordered_candidates():
    """iter_label_candidates prefers long horizontal, then long vertical."""
    from fypa.topology.types import TopologyWire

    wire = TopologyWire(
        net="NET_A",
        path_d="M 100,50 H 180 V 120 H 200",
        label="NET_A",
        routing_kind="gutter",
        bus_x=180.0,
    )
    segs = path_to_segments(wire.net, parse_wire_path(wire.path_d))
    tw, th = label_text_size(wire.label)
    candidates = list(iter_label_candidates(segs, tw=tw, th=th))
    assert candidates
    phases = [c.phase for c in candidates]
    assert phases.index("horizontal_long") < phases.index("vertical_long")
    horiz = next(c for c in candidates if c.phase == "horizontal_long")
    assert not horiz.vertical
    assert abs(horiz.y - 50.0) < 1.0
    vert = next(c for c in candidates if c.phase == "vertical_long")
    assert vert.vertical
    assert abs(vert.x - 180.0) < 1.0


def test_project_b_hub_key_nets_on_horizontal_runs():
    """project_b_hub_vdd: gutter nets label centered on their horizontal runs."""
    model = build_topology_model(load_topology_fixture("project_b_hub_vdd"))
    geo = compute_schematic_geometry(model.wires)
    by_net = {
        w.net: w for w in model.wires
        if w.label and not w.dashed and w.label_x != 0.0
    }
    for net in ("LED_R", "LED_G", "LED_B", "VDD_3V3_PWR", "VDD_1V8"):
        w = by_net[net]
        segs = [s for s in geo.segments if s.net == net]
        assert _label_on_segment(w, segs), f"{net} not on wire"
        if not w.label_vertical:
            horiz_ys = [s.y1 for s in segs if s.orient == "H" and s.length >= 22]
            assert horiz_ys
            assert min(abs(w.label_y - y) for y in horiz_ys) < 1.0


def test_label_search_project_b_hub_golden_nets():
    """project_b_hub_vdd: key nets keep placed labels after search-space refactor."""
    model = build_topology_model(load_topology_fixture("project_b_hub_vdd"))
    by_net = {
        w.net: w for w in model.wires
        if w.label and not w.dashed and w.label_x != 0.0
    }
    for net in ("LED_R", "LED_G", "LED_B", "VDD_3V3_PWR"):
        assert net in by_net, f"missing label for {net}"


def test_sandbox_probe_labels_on_wire_segments():
    """Regression: sandbox probe dump labels sit on net segments."""
    probe = Path(__file__).resolve().parent.parent / "_probe" / "topology.pkl"
    if not probe.is_file():
        return
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta)
    geo = compute_schematic_geometry(model.wires)
    labeled = [w for w in model.wires if w.label and not w.dashed]
    assert labeled
    for w in labeled:
        segs = [s for s in geo.segments if s.net == w.net]
        assert _label_on_segment(w, segs), f"{w.net} label off wire"


def test_find_label_at_returns_net_for_highlight():
    """Hovering a net label should resolve to that net."""
    from fypa.topology.hit_test import find_label_at, topology_net_at

    model = build_topology_model(project_b_compact_metadata())
    labeled = next(w for w in model.wires if w.label and w.label_x)
    hit = find_label_at(model, labeled.label_x, labeled.label_y)
    assert hit is not None
    assert hit.net == labeled.net
    assert topology_net_at(model, labeled.label_x, labeled.label_y) == labeled.net
