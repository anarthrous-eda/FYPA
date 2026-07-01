"""Layout and hit-test tests for topology."""

from pathlib import Path
import pickle

import pytest

from fypa.topology import build_topology_model, find_component_at
from fypa.topology.hit_test import find_wire_at, topology_net_at, topology_tooltip_at
from fypa.topology.render import render_net_highlight_svg
from fypa.topology.constants import MIN_PARALLEL_GAP, NODE_W, PORT_WIRE_STUB
from tests.topology_fixtures import front_like_metadata, load_topology_fixture


def _load_probe_dir(name: str):
    probe = Path("_probe") / name / "topology.pkl"
    if not probe.is_file():
        return None
    with probe.open("rb") as f:
        return build_topology_model(pickle.load(f))


def test_topology_tooltip_only_on_elements():
    """Empty canvas areas must not produce a tooltip; wires/ports/symbols do."""
    model = build_topology_model(front_like_metadata())
    assert topology_tooltip_at(model, 0.0, 0.0) is None
    j1 = next(n for n in model.nodes if n.label == "J1")
    bx, by, bw, bh = j1.bounds
    assert topology_tooltip_at(model, bx + bw / 2, by + bh / 2)
    port = j1.ports[0]
    assert topology_tooltip_at(model, port.x, port.y)
    vdd_row = next(w for w in model.wires if w.routing_kind == "hub_row")
    assert find_wire_at(model, 430.0, 75.0) is vdd_row
    assert topology_tooltip_at(model, 430.0, 75.0)


def test_find_component_at_hit_test():
    model = build_topology_model(front_like_metadata())
    j1 = next(n for n in model.nodes if n.label == "J1")
    bx, by, bw, bh = j1.bounds
    hit = find_component_at(model, bx + bw / 2, by + bh / 2)
    assert hit is not None
    assert hit.label == "J1"
    assert find_component_at(model, 0, 0) is None


def test_topology_net_highlight_on_wire_hover():
    """Hovering a wire yields highlight SVG for the whole net, not symbols."""
    model = build_topology_model(front_like_metadata())
    assert topology_net_at(model, 0.0, 0.0) is None
    j1 = next(n for n in model.nodes if n.label == "J1")
    bx, by, bw, bh = j1.bounds
    assert topology_net_at(model, bx + bw / 2, by + bh / 2) is None
    port = j1.ports[0]
    assert topology_net_at(model, port.x, port.y) == port.net
    net = topology_net_at(model, 430.0, 75.0)
    assert net == "VDD_3V3_PWR"
    svg = render_net_highlight_svg(model, net)
    assert "stroke=" in svg
    assert "430" not in svg or "line" in svg

    model = build_topology_model(front_like_metadata())
    j1 = next(n for n in model.nodes if n.label == "J1")
    bx, by, bw, bh = j1.bounds
    hit = find_component_at(model, bx + bw / 2, by + bh / 2)
    assert hit is not None
    assert hit.label == "J1"
    assert find_component_at(model, 0, 0) is None


def test_topology_nodes_do_not_overlap_in_column():
    """Mixed single-net and two-port nodes must stack without overlapping."""
    meta = {
        "directives": [
            {
                "role": "SOURCE",
                "designator": "J19",
                "label": "J19",
                "value_str": "1 V",
                "terminals": {
                    "P": {"requested_net": "NET_A", "pins": [{"net": "NET_A", "pad": "1"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND", "pad": "2"}]},
                },
            },
            {
                "role": "SOURCE",
                "designator": "J21",
                "label": "J21",
                "value_str": "1 V",
                "terminals": {
                    "P": {"requested_net": "NET_B", "pins": [{"net": "NET_B", "pad": "1"}]},
                    "N": {"ideal_return": True, "pin_count": 0, "pins": []},
                },
            },
            {
                "role": "SOURCE",
                "designator": "J23",
                "label": "J23",
                "value_str": "1 V",
                "terminals": {
                    "P": {"requested_net": "NET_C", "pins": [{"net": "NET_C", "pad": "1"}]},
                    "N": {"ideal_return": True, "pin_count": 0, "pins": []},
                },
            },
        ],
    }
    model = build_topology_model(meta)
    sources = sorted(
        (n for n in model.nodes if n.role == "SOURCE"),
        key=lambda n: n.y,
    )
    for above, below in zip(sources, sources[1:]):
        assert above.y + above.height <= below.y, (
            f"{above.label} overlaps {below.label}"
        )


def test_topology_front_like_layout_stays_compact():
    """REGULATOR OUT_N must not propagate columns via GND (oscillation)."""
    model = build_topology_model(front_like_metadata())
    j1 = next(n for n in model.nodes if n.designator == "J1")
    u2 = next(n for n in model.nodes if n.designator == "U2")
    assert j1.x < u2.x
    assert model.width < 1200.0


def test_all_sinks_share_rightmost_column():
    """SINK symbols align in the last column even when propagation stops early."""
    from fypa.topology.metadata.layout_bridge import parse_topology_directives, specs_by_column

    parsed = parse_topology_directives(load_topology_fixture("front_hub_vdd"))
    _, max_col = specs_by_column(parsed.node_specs, parsed.columns)
    sink_cols = {
        parsed.columns[s["node_id"]]
        for s in parsed.node_specs
        if s["role"] == "SINK"
    }
    assert sink_cols == {max_col}


def test_hub_wires_no_horizontal_backtrack_on_probe() -> None:
    """Hub routing must not zig-zag horizontally (schematic left → right)."""
    from pathlib import Path
    import pickle

    from fypa.topology.geometry import parse_wire_path

    probe = Path("_probe/front/topology.pkl")
    if not probe.is_file():
        probe = Path("_probe/topology.pkl")
    if not probe.is_file():
        return
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta)
    for wire in model.wires:
        if wire.net != "VDD_5V0":
            continue
        points = parse_wire_path(wire.path_d)
        for i in range(len(points) - 2):
            x0, y0 = points[i]
            x1, y1 = points[i + 1]
            x2, y2 = points[i + 2]
            if abs(y0 - y1) < 0.5 and abs(y1 - y2) < 0.5:
                d1, d2 = x1 - x0, x2 - x1
                assert not (d1 * d2 < 0 and abs(d1) > 0.5 and abs(d2) > 0.5), (
                    f"horizontal backtrack in {wire.path_d}"
                )


def test_probe_vdd_5v0_runs_above_u3() -> None:
    """VDD_5V0 gutter bus should clear U3 from above, not detour below it."""
    from pathlib import Path
    import pickle

    from fypa.topology.geometry import parse_wire_path

    probe = Path("_probe/front/topology.pkl")
    if not probe.is_file():
        probe = Path("_probe/topology.pkl")
    if not probe.is_file():
        return
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta)
    by_des = {n.designator: n for n in model.nodes}
    if "U3" not in by_des or not any(w.net == "VDD_5V0" for w in model.wires):
        return
    u3_top = by_des["U3"].y
    tap = next(
        w for w in model.wires
        if w.net == "VDD_5V0"
        and w.routing_kind == "hub_tap"
        and w.src_node == by_des["U3"].node_id
    )
    ys = [y for _x, y in parse_wire_path(tap.path_d)]
    bus_y = min(ys)
    assert bus_y < u3_top - 1.0, tap.path_d


def test_probe_v_plus_minus_junction_near_connector() -> None:
    """V+/V- feeds merge near J2.1 / J2.2 (short stubs, trunk at sink column)."""
    from pathlib import Path
    import pickle

    from fypa.topology.geometry import parse_wire_path

    probe = Path("_probe/front/topology.pkl")
    if not probe.is_file():
        probe = Path("_probe/topology.pkl")
    if not probe.is_file():
        return
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta)
    by_des = {n.designator: n for n in model.nodes}
    if "J2.1" not in by_des:
        return
    j21_x = next(
        p.x for n in model.nodes for p in n.ports
        if n.designator == "J2.1" and p.net == "V+"
    )
    vplus = [w for w in model.wires if w.net == "V+"]
    assert any(
        w.routing_kind == "hub_tap" and w.path_d.startswith(f"M {j21_x:.1f},")
        for w in vplus
    ), "J2.2 should drop vertically at the J2.1 column"
    vminus = [w for w in model.wires if w.net == "V-"]
    trunk = next(w for w in vminus if w.routing_kind == "hub")
    assert trunk.bus_x is not None and abs(trunk.bus_x - 712.0) < 1.0, (
        "V- trunk should sit on the J2 N-port stub column"
    )
    assert not any(
        "V 120.0" in w.path_d or "V 222.0" in w.path_d
        for w in vminus if w.routing_kind == "hub_tap"
    ), "V- J2 taps should not loop outward before joining the trunk"
    u4_vminus = next(
        w for w in vminus
        if w.src_node == by_des["U4"].node_id and w.routing_kind == "hub_tap"
    )
    verts = parse_wire_path(u4_vminus.path_d)
    assert verts[-1][0] >= 700.0, u4_vminus.path_d


def test_probe_stacked_stub_lengths_bottom_to_top() -> None:
    """Stacked edge stubs grow from bottom (short) to top (long)."""
    from pathlib import Path
    import pickle

    from fypa.topology.placement import port_stub_length

    probe = Path("_probe/front/topology.pkl")
    if not probe.is_file():
        probe = Path("_probe/topology.pkl")
    if not probe.is_file():
        return
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta)
    d1 = next(n for n in model.nodes if n.designator == "D1")
    leds = sorted(
        [p for p in d1.ports if p.net.startswith("LED_")],
        key=lambda p: p.y,
    )
    assert len(leds) == 3
    lengths = [port_stub_length(p) for p in leds]
    assert lengths[0] > lengths[1] > lengths[2], lengths


def test_probe_regulator_power_gnd_share_wire_column() -> None:
    """Regulator power from above and GND below share one routing column."""
    from pathlib import Path
    import pickle

    from fypa.topology.placement import port_stub_x

    probe = Path("_probe/front/topology.pkl")
    if not probe.is_file():
        probe = Path("_probe/topology.pkl")
    if not probe.is_file():
        return
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta)
    u2 = next(n for n in model.nodes if n.designator == "U2")
    left = [p for p in u2.ports if p.side == "left"]
    pwr = next(p for p in left if p.net != "__GND__")
    gnd = next(p for p in left if p.net == "__GND__")
    assert port_stub_x(pwr) == port_stub_x(gnd)


def test_probe_front_gutter_leds_route_via_stub_columns() -> None:
    """Stacked gutter LEDs turn vertical at each stub column (no crossed bus detours)."""
    from fypa.topology import parse_wire_path, path_to_segments
    from fypa.topology.placement import port_stub_x

    model = _load_probe_dir("front")
    if model is None:
        return
    d1 = next(n for n in model.nodes if n.designator == "D1")
    for net in ("LED_R", "LED_G", "LED_B"):
        port = next(p for p in d1.ports if p.net == net)
        wire = next(w for w in model.wires if w.net == net)
        segs = path_to_segments(net, parse_wire_path(wire.path_d))
        assert segs[0].orient == "H"
        assert segs[1].orient == "V"
        assert abs(segs[1].x1 - port_stub_x(port)) < 1.0


def test_probe_smart_footpiece_no_foreign_vertical_overlap() -> None:
    """VDD and GND must not share overlapping vertical spans."""
    from tests.test_topology_geometry import foreign_segment_overlap_issues

    model = _load_probe_dir("smart footpiece")
    if model is None:
        return
    assert not foreign_segment_overlap_issues(model)


def test_probe_smart_footpiece_top_regulator_separate_power_gnd_columns() -> None:
    """Top regulator must not share a wire column when its feed routes below GND."""
    from fypa.topology.placement import port_stub_x

    model = _load_probe_dir("smart footpiece")
    if model is None:
        return
    u4 = next(n for n in model.nodes if n.designator == "U4")
    left = [p for p in u4.ports if p.side == "left"]
    pwr = next(p for p in left if p.net != "__GND__")
    gnd = next(p for p in left if p.net == "__GND__")
    assert port_stub_x(pwr) != port_stub_x(gnd)


def test_probe_smart_footpiece_u3_regulator_separate_power_gnd_columns() -> None:
    """Bottom regulator GND must not share the VDD stub column (no stacked verticals)."""
    from fypa.topology.constants import GND_NET
    from fypa.topology.placement import port_stub_x

    from fypa.topology import parse_wire_path, path_to_segments

    model = _load_probe_dir("smart footpiece")
    if model is None:
        return
    u3 = next(n for n in model.nodes if n.designator == "U3")
    left = [p for p in u3.ports if p.side == "left"]
    pwr = next(p for p in left if p.net != GND_NET)
    gnd = next(p for p in left if p.net == GND_NET)
    assert port_stub_x(pwr) != port_stub_x(gnd)
    pwr_x = round(port_stub_x(pwr), 1)
    for wire in model.wires:
        if wire.net != GND_NET:
            continue
        for seg in path_to_segments(wire.net, parse_wire_path(wire.path_d)):
            if seg.orient == "V" and abs(seg.x1 - pwr_x) < 1.0:
                pytest.fail(
                    f"GND vertical on U3 VDD column x={pwr_x}: {wire.path_d}",
                )


def test_probe_smart_footpiece_vminus_j2_min_input_stub() -> None:
    """J2 V- taps must leave the port with at least PORT_WIRE_STUB_MIN before turning."""
    from fypa.topology.constants import PORT_WIRE_STUB_MIN
    from fypa.topology.placement import port_stub_x

    from fypa.topology import parse_wire_path, path_to_segments

    model = _load_probe_dir("smart footpiece")
    if model is None:
        return
    by_des = {n.designator: n for n in model.nodes}
    if "J2.1" not in by_des:
        return
    for des in ("J2.1", "J2.2"):
        port = next(
            p for n in model.nodes for p in n.ports
            if n.designator == des and p.net == "V-"
        )
        tap = next(
            w for w in model.wires
            if w.net == "V-" and w.routing_kind == "hub_tap" and w.src_node == port.node_id
        )
        segs = path_to_segments("V-", parse_wire_path(tap.path_d))
        stub = port_stub_x(port)
        if port.side == "left":
            port_seg = next(
                (s for s in segs if s.orient == "H" and abs(s.x2 - port.x) < 1.0),
                None,
            )
        else:
            port_seg = next(
                (s for s in segs if s.orient == "H" and abs(s.x1 - port.x) < 1.0),
                None,
            )
        assert port_seg is not None, tap.path_d
        assert port_seg.length >= PORT_WIRE_STUB_MIN - 0.6, (
            f"{des} V- stub {port_seg.length:.1f}px < {PORT_WIRE_STUB_MIN}: {tap.path_d}"
        )
        assert abs(port_seg.x1 - stub) < 1.0 or abs(port_seg.x2 - stub) < 1.0, tap.path_d
    trunk = next(w for w in model.wires if w.net == "V-" and w.routing_kind == "hub")
    assert trunk.bus_x is not None and trunk.bus_x < port_stub_x(
        next(p for n in model.nodes for p in n.ports if n.designator == "J2.1" and p.net == "V-"),
    ), "V- trunk should sit west of the J2 stub column"


def test_direct_neighbors_share_row_y():
    """Resistors align vertically with their directly connected sink load."""
    from pathlib import Path
    import pickle

    probe = Path("_probe/front/topology.pkl")
    if not probe.is_file():
        probe = Path("_probe/topology.pkl")
    if not probe.is_file():
        return
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta)
    by_des = {n.designator: n for n in model.nodes if n.role != "GND"}
    if not all(d in by_des for d in ("L2", "L3", "L4", "U2", "U5", "U6")):
        return
    for left, right in (("L2", "U2"), ("L3", "U5"), ("L4", "U6")):
        assert abs(by_des[left].y - by_des[right].y) < 0.5, (
            f"{left} should align with {right}"
        )


def test_topology_front_hub_gutter_wide_enough():
    """Measured bus span must fit in the gutter between D1 and the U-stack."""

    meta = load_topology_fixture("front_hub_vdd")
    model = build_topology_model(meta)
    d1 = next(n for n in model.nodes if n.designator == "D1")
    u_stack = sorted(
        (n for n in model.nodes if n.designator.startswith("U")),
        key=lambda n: n.x,
    )[0]
    gap_width = u_stack.x - (d1.x + d1.width)
    bus_xs = sorted({
        round(w.bus_x, 1)
        for w in model.wires
        if w.bus_x is not None and w.net != "__GND__"
        and d1.x + d1.width <= w.bus_x <= u_stack.x
    })
    if len(bus_xs) >= 2:
        span = bus_xs[-1] - bus_xs[0]
        min_gap = (len(bus_xs) - 1) * MIN_PARALLEL_GAP
        assert span >= min_gap - 0.6
        assert gap_width >= span + 2 * PORT_WIRE_STUB - 4.0
    gutter_lo = d1.x + d1.width
    gutter_hi = u_stack.x
    for w in model.wires:
        if w.bus_x is None:
            continue
        bx = w.bus_x
        if not (gutter_lo <= bx <= gutter_hi):
            continue
        for n in model.nodes:
            if n.role == "GND":
                continue
            nx = n.x
            if nx <= bx <= nx + NODE_W:
                assert not (d1.x <= nx <= u_stack.x), (
                    f"bus_x={bx} inside node {n.designator}"
                )
