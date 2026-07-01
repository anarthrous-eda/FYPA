"""Geometry, junction, and bridge tests."""

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
from tests.topology_fixtures import front_like_metadata, load_topology_fixture


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

    report = topology_wiring_report(build_topology_model(front_like_metadata()))
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
    for name in ("front_like", "front_hub_vdd", "column_gnd_feedback"):
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
    model = build_topology_model(front_like_metadata())
    svg = render_topology_svg(model)
    assert "<circle" in svg

    arc_path = vertical_bridge_path(100.0, 50.0, 200.0, [120.0])
    assert " A " in arc_path
    assert arc_path.startswith("M 100.0,50.0")
