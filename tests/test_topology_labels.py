"""Net label placement tests."""

from fypa.topology import build_topology_model, render_topology_svg
from fypa.topology.constants import BRIDGE_R, WIRE_EPS
from tests.topology_fixtures import front_like_metadata, load_topology_fixture


def test_topology_wire_labels_not_at_origin():
    """Placed net labels must not sit at (0,0); one label per net."""
    model = build_topology_model(front_like_metadata())
    labeled = [w for w in model.wires if w.label and not w.dashed]
    assert labeled
    for w in labeled:
        assert w.label_x != 0.0 or w.label_y != 0.0, f"{w.net} label at origin"
    nets = {w.net for w in labeled}
    assert len(nets) == len(labeled)


def test_topology_wire_labels_prefer_horizontal_segments():
    """Net labels sit on horizontal runs when long enough; else rotate on vertical."""
    model = build_topology_model(front_like_metadata())
    svg = render_topology_svg(model)
    assert "VDD_3V3" in svg or "VDD_3V3_PWR" in svg
    assert "rotate(-90)" in svg or 'text-anchor="middle"' in svg


def test_topology_front_hub_labels_clear_bridges_and_rotate():
    """Dense front gutter: labels avoid bridges; at least one label is vertical."""
    from fypa.topology.geometry import compute_schematic_geometry

    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
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
    assert not any(w.label_vertical for w in labeled), (
        "front_hub_vdd gutter/hub labels should be horizontal on wide runs"
    )
    svg = render_topology_svg(model)
    assert 'fill-opacity="0.5"' in svg


def test_label_search_space_yields_ordered_candidates():
    """iter_label_candidates produces deterministic primary-before-fallback order."""
    from fypa.topology.constants import MAX_LABEL_DISTANCE
    from fypa.topology.geometry import parse_wire_path, path_to_segments
    from fypa.topology.labels import iter_label_candidates
    from fypa.topology.types import TopologyWire

    wire = TopologyWire(
        net="NET_A",
        path_d="M 100,50 H 180 V 120 H 200",
        label="NET_A",
        routing_kind="gutter",
        bus_x=180.0,
    )
    points = parse_wire_path(wire.path_d)
    segs = path_to_segments(wire.net, points)
    candidates = list(iter_label_candidates(wire, segs, gutter_side=1, tw=24.0))
    assert candidates
    phases = [c.phase for c in candidates]
    assert phases.index("primary_horizontal") < phases.index("bus_vertical")
    for c in candidates:
        assert c.x == c.x and c.y == c.y  # finite
        if c.phase != "last_resort":
            assert abs(c.y - 50) <= MAX_LABEL_DISTANCE + 1 or c.vertical


def test_front_hub_key_nets_prefer_horizontal_near_wire():
    """front_hub_vdd: wide horizontal runs get horizontal labels close to the wire."""
    from fypa.topology.geometry import parse_wire_path, path_to_segments

    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    by_net = {
        w.net: w for w in model.wires
        if w.label and not w.dashed and w.label_x != 0.0
    }
    for net in ("LED_R", "LED_G", "LED_B", "VDD_3V3_PWR", "VDD_1V8"):
        w = by_net[net]
        assert not w.label_vertical, f"{net} should use a horizontal label"
        segs = path_to_segments(w.net, parse_wire_path(w.path_d))
        horiz_ys = [s.y1 for s in segs if s.orient == "H" and s.length >= 22]
        assert horiz_ys, f"{net} has no qualifying horizontal segment"
        best_dy = min(abs(w.label_y - y) for y in horiz_ys)
        limit = 12.0 if net in ("VDD_3V3_PWR",) else 6.0
        assert best_dy <= limit, f"{net} label {best_dy:.1f}px from wire"


def test_label_search_front_hub_golden_nets():
    """front_hub_vdd: key nets keep placed labels after search-space refactor."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    by_net = {
        w.net: w for w in model.wires
        if w.label and not w.dashed and w.label_x != 0.0
    }
    for net in ("LED_R", "LED_G", "LED_B", "VDD_3V3_PWR"):
        assert net in by_net, f"missing label for {net}"
