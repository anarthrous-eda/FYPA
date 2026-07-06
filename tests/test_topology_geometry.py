"""Geometry, junction, and bridge tests."""

import pytest

from fypa.topology import (
    GND_NET,
    TopologyWire,
    WireSeg,
    build_topology_model,
    classify_hv_intersection,
    compute_schematic_geometry,
    find_junctions,
    render_topology_svg,
    schematic_segments,
    topology_wiring_report,
    vertical_bridge_path,
)
from tests.topology_fixtures import project_b_compact_metadata, list_topology_fixtures, load_topology_fixture

_FOREIGN_OVERLAP_CODES = frozenset({
    "duplicate_vertical_x",
    "duplicate_horizontal_y",
})


def foreign_segment_overlap_issues(model) -> list[dict]:
    """Errors when unlike nets share the same vertical x or horizontal y span."""
    from fypa.topology.validate import check_segment_spacing

    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    return [
        issue
        for issue in check_segment_spacing(geo.segments, geo.junctions, geo.bridges)
        if issue.get("code") in _FOREIGN_OVERLAP_CODES
    ]


@pytest.mark.parametrize("fixture_name", list_topology_fixtures())
def test_no_foreign_net_segment_overlap(fixture_name: str) -> None:
    """Unlike nets must not share collinear vertical or horizontal wire spans."""
    model = build_topology_model(load_topology_fixture(fixture_name))
    issues = foreign_segment_overlap_issues(model)
    assert not issues, issues


def test_sandbox_regulator_overlap_regression() -> None:
    """Sandbox U3/U4 gutter: V+, V-, VDD_5V0 need distinct buses (issue #dump)."""
    model = build_topology_model(load_topology_fixture("sandbox_regulator_overlap"))
    bus_x_by_net: dict[str, float] = {}
    for net in ("V+", "V-", "VDD_5V0"):
        xs = {
            round(w.bus_x, 1)
            for w in model.wires
            if w.net == net and w.bus_x is not None
        }
        assert xs, f"no gutter bus for {net}"
        assert len(xs) == 1, f"{net} should use one bus column, got {xs}"
        bus_x_by_net[net] = next(iter(xs))
    assert len(set(bus_x_by_net.values())) == 3, bus_x_by_net


def test_hv_junction_and_bridge_classified_by_net():
    """Bridges on different nets; same-net T/+ and vertical taps get junction dots."""
    gnd_bus = WireSeg(GND_NET, "H", 50.0, 100.0, 150.0, 100.0)
    gnd_drop = WireSeg(GND_NET, "V", 100.0, 80.0, 100.0, 120.0)
    assert classify_hv_intersection(gnd_bus, gnd_drop) == (
        "junction",
        (100.0, 100.0),
    )

    signal = WireSeg("P1V_TRACE", "H", 80.0, 100.0, 120.0, 100.0)
    assert classify_hv_intersection(signal, gnd_drop) == (
        "bridge",
        (100.0, 100.0),
    )

    corner_h = WireSeg(GND_NET, "H", 100.0, 100.0, 150.0, 100.0)
    corner_v = WireSeg(GND_NET, "V", 100.0, 50.0, 100.0, 100.0)
    assert classify_hv_intersection(corner_h, corner_v) is None

    wires = [
        TopologyWire(net=GND_NET, path_d="M 50,100 H 150"),
        TopologyWire(net=GND_NET, path_d="M 100,80 V 120"),
        TopologyWire(net="P1V_TRACE", path_d="M 80,200 H 120"),
    ]
    segments, _, _, vert_cross = schematic_segments(wires)
    junctions = set(find_junctions(segments))
    assert (100.0, 100.0) in junctions
    assert not vert_cross

    report = topology_wiring_report(build_topology_model(project_b_compact_metadata()))
    j_pts = {(j["x"], j["y"]) for j in report["schematic"]["junctions"]}
    b_pts = {(c["x"], c["y"]) for c in report["schematic"]["bridge_crossings"]}
    assert j_pts.isdisjoint(b_pts)
    for c in report["schematic"]["bridge_crossings"]:
        assert c["vertical_net"] != c["horizontal_net"]


def test_stacked_column_taps_and_corners():
    """Direction degree decides: 3+ same-net dirs = dot, 90° corner = none."""
    net = "P1V8"
    # A stub meeting a through-going vertical of the same net is a 3-way tap.
    stub_h = WireSeg(net, "H", 384.0, 195.0, 404.0, 195.0, wire_index=1)
    through_v = WireSeg(net, "V", 384.0, 93.0, 384.0, 1200.0, wire_index=0)
    segments = [stub_h, through_v]
    assert classify_hv_intersection(stub_h, through_v, segments=segments) == (
        "junction",
        (384.0, 195.0),
    )
    assert (384.0, 195.0) in find_junctions(segments)

    # The same stub meeting only its own vertical's start is a 90° corner.
    own_v = WireSeg(net, "V", 384.0, 195.0, 384.0, 1200.0, wire_index=1)
    assert classify_hv_intersection(
        stub_h, own_v, segments=[stub_h, own_v],
    ) is None
    assert find_junctions([stub_h, own_v]) == []

    # A drop turning onto the end of a bus is a corner — two directions only.
    bus_h = WireSeg(net, "H", 16.0, 1200.0, 384.0, 1200.0, wire_index=0)
    drop_v = WireSeg(net, "V", 384.0, 195.0, 384.0, 1200.0, wire_index=1)
    assert classify_hv_intersection(
        bus_h, drop_v, segments=[bus_h, drop_v],
    ) is None

    # A drop tapping the interior of a bus is a 3-way junction.
    mid_bus = WireSeg(net, "H", 16.0, 1200.0, 700.0, 1200.0, wire_index=0)
    assert classify_hv_intersection(
        mid_bus, drop_v, segments=[mid_bus, drop_v],
    ) == ("junction", (384.0, 1200.0))


def test_gnd_symbol_rail_attachment_gets_junction_dot():
    """GND symbol stub is a third branch at the left rail anchor."""
    for name in ("project_b_compact", "project_b_hub_vdd", "column_gnd_feedback"):
        model = build_topology_model(load_topology_fixture(name))
        geo = compute_schematic_geometry(
            model.wires,
            gnd_symbol_x=model.gnd_symbol_x,
            gnd_bus_y=model.gnd_bus_y,
        )
        pt = (round(model.gnd_symbol_x, 1), round(model.gnd_bus_y, 1))
        assert pt in set(geo.junctions), f"{name}: missing GND symbol junction at {pt}"


def test_topology_schematic_junctions_and_vertical_bridges():
    """Bridges render arc hops; junction dots only when branch count > 2."""
    model = build_topology_model(project_b_compact_metadata())
    svg = render_topology_svg(model)
    assert "<circle" in svg

    arc_path = vertical_bridge_path(100.0, 50.0, 200.0, [120.0])
    assert " A " in arc_path
    assert arc_path.startswith("M 100.0,50.0")
