"""Named regression tests mapped to past topology bugs."""

from __future__ import annotations

import pytest

from fypa.topology import (
    GND_NET,
    MIN_PARALLEL_GAP,
    PORT_WIRE_STUB,
    build_topology_model,
    parse_wire_path,
    path_to_segments,
    topology_wiring_report,
)
from fypa.topology.placement import port_stub_length, port_stub_x
from fypa.topology.constants import MAX_CANVAS_WIDTH
from tests.topology_fixtures import load_topology_fixture


def test_regression_column_gnd_feedback_stays_compact():
    """REGULATOR OUT_N must not propagate columns via GND (5772px canvas bug)."""
    model = build_topology_model(load_topology_fixture("column_gnd_feedback"))
    j1 = next(n for n in model.nodes if n.designator == "J1")
    u2 = next(n for n in model.nodes if n.designator == "U2")
    assert j1.x < u2.x
    assert model.width < MAX_CANVAS_WIDTH


def test_regression_front_hub_vdd_avoids_resistor_body():
    """VDD_3V3_PWR must not run horizontally through the D1 block."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    d1 = next(n for n in model.nodes if n.designator == "D1")
    nx, ny, nw, nh = d1.bounds

    def _crosses_d1(wire) -> bool:
        for seg in path_to_segments(wire.net, parse_wire_path(wire.path_d)):
            if seg.orient != "H":
                continue
            y = seg.y1
            x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
            if ny <= y <= ny + nh and x_hi > nx and x_lo < nx + nw:
                return True
        return False

    vdd = [w for w in model.wires if w.net == "VDD_3V3_PWR"]
    assert vdd
    assert not any(_crosses_d1(w) for w in vdd)


def test_regression_front_j1_d1_direct_vdd():
    """VDD hub row connects J1 to D1 at y=75; no detour vertical on symbol edges."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    j1 = next(n for n in model.nodes if n.designator == "J1")
    d1 = next(n for n in model.nodes if n.designator == "D1")
    u3 = next(n for n in model.nodes if n.designator == "U3")
    vdd = [w for w in model.wires if w.net == "VDD_3V3_PWR"]
    assert any(w.routing_kind == "hub_row" for w in vdd)

    y_j1 = next(p.y for p in j1.ports if p.net == "VDD_3V3_PWR")
    j1_stub = next(
        p.x + 20 for p in j1.ports if p.net == "VDD_3V3_PWR" and p.side == "right"
    )
    d1_stub = next(
        p.x - 20 for p in d1.ports if p.net == "VDD_3V3_PWR" and p.side == "left"
    )
    row_at_j1 = False
    for w in vdd:
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient == "H" and abs(seg.y1 - y_j1) < 0.6:
                x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
                if x_lo <= j1_stub + 1 and x_hi >= d1_stub - 1:
                    row_at_j1 = True
    assert row_at_j1, "expected horizontal J1–D1 segment at J1 row"

    symbol_edges = {round(j1.x, 1), round(j1.x + j1.width, 1),
                    round(u3.x, 1), round(u3.x + u3.width, 1)}
    for w in vdd:
        if w.routing_kind not in ("hub_tap", "hub_row"):
            continue
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient == "V" and round(seg.x1, 1) in symbol_edges:
                pytest.fail(
                    f"VDD tap has vertical on symbol edge x={seg.x1:.1f}: {w.path_d}",
                )


def test_regression_front_no_vertical_x_collision():
    """No two foreign vertical segments may share the same x coordinate."""
    from fypa.topology.geometry import compute_schematic_geometry

    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    geo = compute_schematic_geometry(model.wires)
    verticals = [s for s in geo.segments if s.orient == "V"]
    for i, a in enumerate(verticals):
        for b in verticals[i + 1:]:
            if abs(a.x1 - b.x1) >= 0.6:
                continue
            if a.net != b.net:
                pytest.fail(
                    f"Vertical x collision at {a.x1:.1f}: "
                    f"{a.net} wire {a.wire_index} vs {b.net} wire {b.wire_index}",
                )


def test_regression_gnd_column_single_trunk():
    """Each GND column has at most one vertical trunk wire."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    trunks_by_x: dict[float, int] = {}
    for w in model.wires:
        if w.routing_kind != "gnd_trunk":
            continue
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient == "V":
                key = round(seg.x1, 1)
                trunks_by_x[key] = trunks_by_x.get(key, 0) + 1
    for x, count in trunks_by_x.items():
        assert count == 1, f"GND column x={x} has {count} trunks"


def test_regression_gutter_parallel_min_bus_gap():
    """Parallel stacked-column buses stay at least MIN_PARALLEL_GAP apart."""
    model = build_topology_model(load_topology_fixture("gutter_parallel_four_nets"))
    bus_xs = sorted({
        w.bus_x for w in model.wires if w.bus_x is not None
    })
    gaps = [bus_xs[i] - bus_xs[i - 1] for i in range(1, len(bus_xs))]
    assert min(gaps) >= MIN_PARALLEL_GAP - 0.6


def test_regression_sandbox_signal_clears_gnd_drops():
    """Signal gutter buses must not crowd sink GND drop verticals."""
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
            assert abs(sx - gx) >= MIN_PARALLEL_GAP - 0.6


def test_regression_port_horizontal_stub_before_vertical():
    """Wires leaving a port edge must run horizontally to the stub before turning."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))

    def _start_port(wire):
        for node in model.nodes:
            if node.node_id != wire.src_node:
                continue
            for port in node.ports:
                if port.terminal == wire.src_terminal and port.net == wire.net:
                    return port
        return None

    for wire in model.wires:
        port = _start_port(wire)
        if port is None:
            continue
        verts = parse_wire_path(wire.path_d)
        if not verts:
            continue
        start_x, _ = verts[0]
        stub = port_stub_x(port)
        segs = path_to_segments(wire.net, parse_wire_path(wire.path_d))
        if not segs:
            continue
        if abs(start_x - port.x) < 1.0:
            assert segs[0].orient == "H", (
                f"{wire.net} ({wire.routing_kind}) must start with horizontal stub: "
                f"{wire.path_d}"
            )
            assert abs(segs[0].length - port_stub_length(port)) < 4.0, (
                f"{wire.net} stub length {segs[0].length:.1f}px, "
                f"expected ~{port_stub_length(port):.1f}: "
                f"{wire.path_d}"
            )
        elif abs(start_x - stub) > 1.0:
            pytest.fail(
                f"{wire.net} wire starts away from port/stub: {wire.path_d}",
            )
        for seg in segs:
            if seg.orient == "V" and abs(seg.x1 - port.x) < 1.0:
                pytest.fail(
                    f"{wire.net} vertical on symbol edge x={seg.x1:.1f}: {wire.path_d}",
                )


def test_regression_gnd_trunk_at_stub_column():
    """GND column trunks align with the outward port stub line."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    from fypa.topology.placement import port_stub_length, port_stub_x

    for wire in model.wires:
        if wire.routing_kind != "gnd_tap":
            continue
        port = None
        for node in model.nodes:
            if node.node_id != wire.src_node:
                continue
            for p in node.ports:
                if p.terminal == wire.src_terminal and p.net == wire.net:
                    port = p
        assert port is not None
        stub = port_stub_x(port)
        verts = parse_wire_path(wire.path_d)
        assert verts[-1] == (stub, port.y), (
            f"GND tap should end at stub column: {wire.path_d}"
        )


def test_regression_gnd_tap_min_stub_front():
    """GND taps leave the port with a horizontal stub before joining the trunk."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))

    def _port_for(wire):
        for node in model.nodes:
            if node.node_id != wire.src_node:
                continue
            for p in node.ports:
                if p.terminal == wire.src_terminal and p.net == wire.net:
                    return p
        return None

    for wire in model.wires:
        if wire.routing_kind != "gnd_tap":
            continue
        port = _port_for(wire)
        assert port is not None
        segs = path_to_segments(wire.net, parse_wire_path(wire.path_d))
        assert segs and segs[0].orient == "H"
        min_len = port_stub_length(port) - 1.0
        assert segs[0].length >= min_len, (
            f"GND tap stub too short ({segs[0].length:.1f}px, "
            f"expected >={min_len:.1f}): {wire.path_d}"
        )


def test_regression_stacked_stub_lengths():
    """Stacked signal stubs stagger; GND stubs stay short on every edge."""
    from fypa.topology.constants import (
        GND_NET,
        GND_PORT_WIRE_STUB,
        PORT_WIRE_STUB,
        PORT_WIRE_STUB_MIN,
    )

    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    d1 = next(n for n in model.nodes if n.node_id == "D1")
    right = sorted([p for p in d1.ports if p.side == "right"], key=lambda p: p.y)
    assert len(right) == 3
    assert port_stub_length(right[0]) == PORT_WIRE_STUB
    assert port_stub_length(right[-1]) == PORT_WIRE_STUB_MIN

    u4 = next(n for n in model.nodes if n.node_id == "U4")
    left = sorted([p for p in u4.ports if p.side == "left"], key=lambda p: p.y)
    signals = [p for p in left if p.net != GND_NET]
    gnd = next(p for p in left if p.net == GND_NET)
    assert port_stub_length(gnd) == GND_PORT_WIRE_STUB
    assert port_stub_length(signals[0]) == PORT_WIRE_STUB_MIN
    assert port_stub_length(signals[-1]) == PORT_WIRE_STUB

    u2 = next(n for n in model.nodes if n.node_id == "U2")
    out_gnd = next(p for p in u2.ports if p.net == GND_NET and p.side == "right")
    assert port_stub_length(out_gnd) == GND_PORT_WIRE_STUB


def test_regression_no_open_stub_ends():
    """Port stub ends must connect to verticals or continue on the routed net."""
    from fypa.topology.validate import _check_open_stub_ends

    for fixture_name in ("front_hub_vdd", "front_like", "gnd_junction_tap"):
        model = build_topology_model(load_topology_fixture(fixture_name))
        issues = _check_open_stub_ends(model)
        assert not issues, issues


def test_bus_plan_matches_routed_bus_x():
    """Planned bus_x values must match routed wires (single-pass consistency)."""
    from fypa.topology.constants import GND_NET, WIRE_EPS
    from fypa.topology.layout import build_node_layout
    from fypa.topology.routing import build_wires

    meta = load_topology_fixture("front_hub_vdd")
    layout = build_node_layout(meta)
    ports = layout.ports
    gnd_bus_y = layout.gnd_bus_y
    directive_nodes = layout.directive_nodes
    plan = layout.bus_plan
    wires, _ = build_wires(
        ports, gnd_bus_y=gnd_bus_y, obstacles=directive_nodes, bus_plan=plan,
    )
    for w in wires:
        if w.dashed or w.bus_x is None or w.net == GND_NET:
            continue
        expected = plan.hub_buses.get(w.net) or plan.pair_buses.get(w.net)
        if expected is not None:
            assert abs(w.bus_x - expected) < WIRE_EPS, (
                f"{w.net}: routed {w.bus_x} planned {expected}"
            )


def test_single_pass_builder_zero_issues():
    """Builder must not need a second layout pass (issues == 0 on front)."""
    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    report = topology_wiring_report(model)
    assert report["summary"]["issues"] == 0


def test_regression_gnd_taps_dotted_corners_not():
    """GND taps (3-way) get dots; rail corners (2-way) do not."""
    report = topology_wiring_report(
        build_topology_model(load_topology_fixture("gnd_junction_tap")),
    )
    junctions = {(j["x"], j["y"]) for j in report["schematic"]["junctions"]}
    rail = next(
        w for w in report["wires"] if w["routing_kind"] == "gnd_rail"
    )
    rail_y = rail["vertices"][0]["y"]
    rail_xs = {round(v["x"], 1) for v in rail["vertices"]}

    # A stacked column tap (horizontal meets a through vertical) is dotted.
    tap_195 = next(
        w for w in report["wires"]
        if w["routing_kind"] == "gnd_tap"
        and any(v["y"] == 195.0 for v in w["vertices"])
    )
    trunk_x = round(tap_195["vertices"][-1]["x"], 1)
    assert (trunk_x, 195.0) in junctions

    # Where a trunk meets the rail end is a 90° corner — no dot,
    # except at the GND symbol anchor (rail + trunk + symbol stub).
    gnd_anchor = (round(report["canvas"]["gnd_symbol_x"], 1), rail_y)
    for w in report["wires"]:
        if w["routing_kind"] != "gnd_trunk":
            continue
        base = w["vertices"][0]
        if base["y"] == rail_y and round(base["x"], 1) in (
            min(rail_xs), max(rail_xs),
        ):
            pt = (base["x"], base["y"])
            if pt == gnd_anchor:
                assert pt in junctions
            else:
                assert pt not in junctions
