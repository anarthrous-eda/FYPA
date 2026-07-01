"""Layout and hit-test tests for topology."""

from fypa.topology import build_topology_model, find_component_at
from fypa.topology.hit_test import find_wire_at, topology_net_at, topology_tooltip_at
from fypa.topology.render import render_net_highlight_svg
from fypa.topology.constants import MIN_PARALLEL_GAP, NODE_W, PORT_WIRE_STUB
from tests.topology_fixtures import front_like_metadata, load_topology_fixture


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


def test_topology_front_hub_gutter_wide_enough():
    """Measured bus span must fit in the gutter between D1 and the U-stack."""
    from fypa.topology.constants import MIN_PARALLEL_GAP, PORT_WIRE_STUB

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
    for w in model.wires:
        if w.bus_x is None:
            continue
        bx = w.bus_x
        for n in model.nodes:
            if n.role == "GND":
                continue
            nx = n.x
            if nx <= bx <= nx + NODE_W:
                assert not (d1.x <= nx <= u_stack.x), (
                    f"bus_x={bx} inside node {n.designator}"
                )
