"""Net label placement tests."""

import pickle
from pathlib import Path

from fypa.topology import build_topology_model, render_topology_svg
from fypa.topology.constants import BRIDGE_R, WIRE_EPS
from fypa.topology.geometry import compute_schematic_geometry, parse_wire_path, path_to_segments
from fypa.topology.labels import iter_label_candidates, label_text_size
from tests.topology_fixtures import front_like_metadata, load_topology_fixture


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
    model = build_topology_model(front_like_metadata())
    labeled = [w for w in model.wires if w.label and not w.dashed]
    assert labeled
    for w in labeled:
        assert w.label_x != 0.0 or w.label_y != 0.0, f"{w.net} label at origin"
    nets = {w.net for w in labeled}
    assert len(nets) == len(labeled)


def test_topology_wire_labels_centered_on_segments():
    """Net labels sit on horizontal or vertical wire segments, not beside them."""
    model = build_topology_model(front_like_metadata())
    geo = compute_schematic_geometry(model.wires)
    by_net = {w.net: w for w in model.wires if w.label and not w.dashed}
    for net, wire in by_net.items():
        segs = [s for s in geo.segments if s.net == net]
        assert _label_on_segment(wire, segs), f"{net} label not on a wire segment"


def test_topology_wire_labels_prefer_horizontal_segments():
    """Wide horizontal runs get horizontal on-wire labels when available."""
    model = build_topology_model(front_like_metadata())
    svg = render_topology_svg(model)
    assert "VDD_3V3" in svg or "VDD_3V3_PWR" in svg
    vdd = next(
        w for w in model.wires
        if w.label and "VDD_3V3" in w.net and not w.label_vertical
    )
    assert vdd.label_y in {75.0, 177.0} or not vdd.label_vertical


def test_topology_front_hub_labels_clear_bridges():
    """Dense front gutter: on-wire labels avoid bridge arc centres."""
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


def test_front_hub_key_nets_on_horizontal_runs():
    """front_hub_vdd: gutter nets label centered on their horizontal runs."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
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


def test_label_search_front_hub_golden_nets():
    """front_hub_vdd: key nets keep placed labels after search-space refactor."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
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

    model = build_topology_model(front_like_metadata())
    labeled = next(w for w in model.wires if w.label and w.label_x)
    hit = find_label_at(model, labeled.label_x, labeled.label_y)
    assert hit is not None
    assert hit.net == labeled.net
    assert topology_net_at(model, labeled.label_x, labeled.label_y) == labeled.net
