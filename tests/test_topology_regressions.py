"""Named regression tests mapped to past topology bugs."""

from __future__ import annotations

import pytest

from fypa.topology import (
    GND_NET,
    MIN_PARALLEL_GAP,
    build_topology_model,
    parse_wire_path,
    path_to_segments,
    topology_wiring_report,
)
from fypa.topology.placement import port_stub_length, port_stub_x
from fypa.topology.constants import MAX_CANVAS_WIDTH, WIRE_EPS
from tests.topology_fixtures import load_topology_fixture


def test_regression_column_gnd_feedback_stays_compact():
    """REGULATOR OUT_N must not propagate columns via GND (5772px canvas bug)."""
    model = build_topology_model(load_topology_fixture("column_gnd_feedback"))
    j1 = next(n for n in model.nodes if n.designator == "J1")
    u2 = next(n for n in model.nodes if n.designator == "U2")
    assert j1.x < u2.x
    assert model.width < MAX_CANVAS_WIDTH


def test_regression_smart_footpiece_rail_merge_stays_compact():
    """SERIES-bridged rails must not collapse columns (11k+ px canvas bug)."""
    model = build_topology_model(load_topology_fixture("smart_footpiece_rails"))
    assert model.width < MAX_CANVAS_WIDTH
    l4 = next(n for n in model.nodes if n.designator == "L4")
    u6 = next(n for n in model.nodes if n.designator == "U6")
    assert l4.x < u6.x
    assert max(n.x for n in model.nodes if n.role != "GND") < 2000.0


def test_regression_rudder_stepper_loop_stays_compact():
    """U1↔J7 loop must not inflate empty columns (8892px canvas bug)."""
    from fypa.topology.metadata.layout_bridge import parse_topology_directives

    meta = load_topology_fixture("rudder_stepper_loop_rails")
    parsed = parse_topology_directives(meta)
    model = build_topology_model(meta)
    j3 = next(n for n in model.nodes if n.designator == "J3")
    u1 = next(n for n in model.nodes if n.designator == "U1")
    j7 = next(n for n in model.nodes if n.designator == "J7")

    assert model.width < MAX_CANVAS_WIDTH
    assert j3.x < u1.x < j7.x
    assert u1.x - j3.x < 500.0
    assert parsed.columns["J7"] == parsed.columns["U1"] + 1
    assert parsed.columns["U1"] == parsed.columns["J3"] + 1


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
    """Foreign verticals may share x only when their y spans do not overlap."""
    from fypa.topology.geometry import compute_schematic_geometry

    model = build_topology_model(load_topology_fixture("front_hub_vdd"))
    geo = compute_schematic_geometry(model.wires)
    verticals = [s for s in geo.segments if s.orient == "V"]
    for i, a in enumerate(verticals):
        for b in verticals[i + 1:]:
            if abs(a.x1 - b.x1) >= 0.6:
                continue
            if a.net == b.net:
                continue
            a_lo, a_hi = sorted((a.y1, a.y2))
            b_lo, b_hi = sorted((b.y1, b.y2))
            if a_hi + WIRE_EPS < b_lo or b_hi + WIRE_EPS < a_lo:
                continue
            pytest.fail(
                f"Vertical x collision at {a.x1:.1f}: "
                f"{a.net} wire {a.wire_index} vs {b.net} wire {b.wire_index}",
            )


def test_regression_rudder_inline_passives_before_sinks():
    """Inline passives feeding sinks sit at or before the sink column."""
    from pathlib import Path
    import pickle

    from fypa.topology.metadata.layout_bridge import parse_topology_directives

    probe = Path("_probe/rudder/topology.pkl")
    if not probe.is_file():
        import pytest

        pytest.skip("rudder probe missing")
    with probe.open("rb") as f:
        meta = pickle.load(f)
    parsed = parse_topology_directives(meta.get("metadata", meta))
    cols = parsed.columns
    sink_cols = {
        cols[s["node_id"]]
        for s in parsed.node_specs
        if s["role"] == "SINK"
    }
    assert len(sink_cols) == 1
    sink_col = next(iter(sink_cols))
    # L2 bridges VDD_3V3 → VDD_PHY before the U3 sink (not in the sink column).
    assert cols["L2"] < cols["U3"]
    assert cols["L2"] < sink_col
    # L1 may share the sink column when it is the immediate feed to U2 on VDD_MCU.
    assert cols["L1"] <= sink_col
    assert cols["U2"] == sink_col


def test_regression_rudder_regulator_column_single_gnd_trunk():
    """Stacked loads in one column share one GND trunk (not one per stub lane)."""
    from pathlib import Path
    import pickle

    probe = Path("_probe/rudder/topology.pkl")
    if not probe.is_file():
        import pytest

        pytest.skip("rudder probe missing")
    with probe.open("rb") as f:
        meta = pickle.load(f)
    model = build_topology_model(meta.get("metadata", meta))
    col_x = next(n.x for n in model.nodes if n.designator == "U2")
    trunk_xs: set[float] = set()
    for w in model.wires:
        if w.routing_kind != "gnd_trunk":
            continue
        for seg in path_to_segments(w.net, parse_wire_path(w.path_d)):
            if seg.orient != "V":
                continue
            if seg.x1 > col_x - 40 and seg.x1 < col_x + 10:
                trunk_xs.add(round(seg.x1, 1))
    assert len(trunk_xs) == 1, f"expected one GND trunk beside sink column, got {trunk_xs}"


def test_regression_probe_boards_no_foreign_gutter_wire_crossings():
    """Gutter signal pairs must not cross each other on probe boards.

    Hub tap/trunk geometry in a shared gutter is validated separately (hub
    fixtures filter ``foreign_wire_crossing``; rudder AX×VDD_3V3 is a known
    open routing item, not a pair-vs-pair regression).
    """
    from pathlib import Path
    import pickle

    from fypa.topology.validate import validate_topology

    for board in ("front", "rudder", "smart footpiece"):
        probe = Path(f"_probe/{board}/topology.pkl")
        if not probe.is_file():
            import pytest

            pytest.skip(f"{board} probe missing")
        with probe.open("rb") as f:
            meta = pickle.load(f)
        model = build_topology_model(meta.get("metadata", meta))
        hub_nets = {
            w.net
            for w in model.wires
            if w.net and not w.dashed and w.routing_kind.startswith("hub")
        }
        crossings = [
            i
            for i in validate_topology(model)
            if i["code"] == "foreign_wire_crossing"
            and i.get("net_a") not in hub_nets
            and i.get("net_b") not in hub_nets
        ]
        assert not crossings, f"{board}: {crossings}"


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


def test_regression_gutter_vertical_avoids_symbol_columns():
    """Vertical signal segments must lie strictly in layout column gaps."""
    from fypa.topology.validate.segments import check_vertical_bus_column_gaps
    from fypa.topology.validate.util import foreign_segments_cross
    from fypa.topology.geometry import parse_wire_path, path_to_segments

    model = build_topology_model(load_topology_fixture("rudder_stepper_loop_rails"))
    gap_issues = check_vertical_bus_column_gaps(model)
    assert not gap_issues, gap_issues
    sns = [w for w in model.wires if w.net in ("SNS_A", "SNS_B")]
    if len(sns) == 2:
        a = path_to_segments(sns[0].net, parse_wire_path(sns[0].path_d))
        b = path_to_segments(sns[1].net, parse_wire_path(sns[1].path_d))
        assert not foreign_segments_cross(a, b)


def test_regression_gutter_parallel_min_bus_gap():
    """Parallel stacked-column buses stay at least MIN_PARALLEL_GAP apart."""
    model = build_topology_model(load_topology_fixture("gutter_parallel_four_nets"))
    bus_xs = sorted({
        w.bus_x for w in model.wires if w.bus_x is not None
    })
    gaps = [bus_xs[i] - bus_xs[i - 1] for i in range(1, len(bus_xs))]
    assert min(gaps) >= MIN_PARALLEL_GAP - 0.6


def test_regression_stacked_column_buses_use_column_edge():
    """Stack-column pair buses use column_bus_x and still pass gap validation."""
    from fypa.topology.validate.segments import check_vertical_bus_column_gaps

    model = build_topology_model(load_topology_fixture("gutter_parallel_four_nets"))
    stack_wires = [w for w in model.wires if w.routing_kind == "stack_column"]
    assert stack_wires, "fixture should include stack_column pair wires"
    assert not check_vertical_bus_column_gaps(model)


def test_pick_gutter_bus_x_prefers_layout_gap_over_stub_channel():
    """When resolve finds no corridor, still place in a column gap if gaps exist."""
    from fypa.topology.placement.gutter_corridors import pick_gutter_bus_x

    column_gaps = [(400.0, 528.0)]
    channel_lo, channel_hi = 50.0, 70.0
    bus_x = pick_gutter_bus_x(
        0,
        1,
        channel_lo,
        channel_hi,
        column_gaps,
        "NET",
        y_lo=0.0,
        y_hi=100.0,
        anchor_x=420.0,
        outward=1.0,
        reserved=[],
    )
    assert column_gaps[0][0] < bus_x < column_gaps[0][1]
    assert not (channel_lo - 5.0 <= bus_x <= channel_hi + 5.0)


def test_regression_sandbox_signal_clears_gnd_drops():
    """Signal gutter buses must not crowd sink GND drop verticals."""
    report = topology_wiring_report(
        build_topology_model(load_topology_fixture("sandbox_subset")),
    )
    signal_xs = sorted({
        round(w["bus_x"], 1)
        for w in report["wires"]
        if w["net"] != GND_NET and w.get("bus_x") is not None
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
        bus_x = wire.bus_x
        if (
            wire.routing_kind == "hub_tap"
            and bus_x is not None
            and stub > bus_x + 1.0
        ):
            # Downstream of the gutter bus: trunk feeds east into the port.
            assert abs(verts[-1][0] - port.x) < 1.0, (
                f"{wire.net} bus-fed tap should end on port: {wire.path_d}"
            )
            continue
        segs = path_to_segments(wire.net, parse_wire_path(wire.path_d))
        if not segs:
            continue
        if abs(start_x - port.x) < 1.0:
            assert segs[0].orient == "H", (
                f"{wire.net} ({wire.routing_kind}) must start with horizontal stub: "
                f"{wire.path_d}"
            )
            if wire.routing_kind in ("gutter", "stack_column") and bus_x is not None:
                expected = abs(bus_x - port.x)
            else:
                expected = port_stub_length(port)
            assert abs(segs[0].length - expected) < 4.0, (
                f"{wire.net} first horizontal {segs[0].length:.1f}px, "
                f"expected ~{expected:.1f}: "
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
    from fypa.topology.placement import port_stub_x

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
    assert port_stub_length(signals[0]) == PORT_WIRE_STUB
    assert port_stub_length(signals[-1]) == PORT_WIRE_STUB_MIN

    u2 = next(n for n in model.nodes if n.node_id == "U2")
    in_pwr = next(p for p in u2.ports if p.net != GND_NET and p.side == "left")
    in_gnd = next(p for p in u2.ports if p.net == GND_NET and p.side == "left")
    assert in_gnd.stub_length == GND_PORT_WIRE_STUB
    assert port_stub_length(in_gnd) == port_stub_length(in_pwr)
    assert port_stub_x(in_pwr) == port_stub_x(in_gnd)


def test_regression_no_open_stub_ends():
    """Port stub ends must connect to verticals or continue on the routed net."""
    from fypa.topology.validate.stubs import check_open_stub_ends

    for fixture_name in ("front_hub_vdd", "front_like", "gnd_junction_tap"):
        model = build_topology_model(load_topology_fixture(fixture_name))
        issues = check_open_stub_ends(model)
        assert not issues, issues


def test_regression_no_dangling_wire_endpoints():
    """Wire ends must join a port, GND symbol, or another wire on the same net."""
    from pathlib import Path
    import pickle

    from fypa.topology.geometry import compute_schematic_geometry
    from fypa.topology.validate import check_dangling_wire_endpoints

    for fixture_name in ("front_hub_vdd", "front_like", "gnd_junction_tap"):
        model = build_topology_model(load_topology_fixture(fixture_name))
        geo = compute_schematic_geometry(
            model.wires,
            gnd_symbol_x=model.gnd_symbol_x,
            gnd_bus_y=model.gnd_bus_y,
        )
        issues = check_dangling_wire_endpoints(model, geo)
        assert not issues, issues

    probe = Path("_probe/topology.pkl")
    if probe.is_file():
        with probe.open("rb") as f:
            meta = pickle.load(f)
        model = build_topology_model(meta)
        geo = compute_schematic_geometry(
            model.wires,
            gnd_symbol_x=model.gnd_symbol_x,
            gnd_bus_y=model.gnd_bus_y,
        )
        issues = check_dangling_wire_endpoints(model, geo)
        assert not issues, issues


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
