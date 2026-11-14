"""Rows layout of the topology diagram (``build_topology_model(layout="rows")``)."""

from __future__ import annotations

import pytest

from fypa.topology import build_topology_model, render_topology_svg
from fypa.topology.constants import GND_NET, WIRE_EPS
from fypa.topology.geometry import compute_schematic_geometry, parse_wire_path
from fypa.topology.hit_test import find_tag_at, topology_net_at, topology_tooltip_at
from fypa.topology.types import TopologyModel
from tests.topology_fixtures import _FIXTURES_DIR, load_topology_fixture


def _term(net: str) -> dict:
    return {"requested_net": net, "pins": [{"net": net, "pad": "1"}]}


def _d(role: str, des: str, value_str: str = "", **terms: str) -> dict:
    d = {
        "role": role,
        "designator": des,
        "label": des,
        "terminals": {name: _term(net) for name, net in terms.items()},
    }
    if value_str:
        d["value_str"] = value_str
    return d


def _bridge(des: str, net: str) -> dict:
    d = _d("AUTO_BRIDGE", des, "0.5 mΩ", P=net, N=net)
    d["auto_bridged"] = True
    return d


def _md(*directives: dict) -> dict:
    return {"directives": list(directives), "net_canonical": {}, "annotation_errors": []}


# Source → series → regulator → sink, plus an undriven sense resistor and
# off-tree auto-bridges - the shape of a real mezzanine board.
TREE = _md(
    _d("SOURCE", "J2", "12 V", P="12P0V", N="GND"),
    _d("RESISTOR", "R18", "5 mOhm", P="+12V", N="12P0V"),
    _d("REGULATOR", "U6", "V=1 V", IN_P="+12V", IN_N="GND", OUT_P="+VDD", OUT_N="GND"),
    _d("SINK", "J1", "10 A", P="+VDD", N="GND"),
    _d("RESISTOR", "R24", "100 mOhm", P="+1V8A_O", N="+1V8A"),
    _bridge("R9", "SCL"),
    _bridge("R36", "SCL"),
    _bridge("R11", "SDA"),
)


def _rows(model: TopologyModel) -> list:
    return [b for b in model.bands if b.kind in ("source", "regulator", "undriven", "other")]


def _node(model: TopologyModel, node_id: str):
    return next(n for n in model.nodes if n.node_id == node_id)


def test_one_row_per_origin_regulator_under_its_feed() -> None:
    model = build_topology_model(TREE, layout="rows")
    rows = _rows(model)
    assert [(b.kind, b.title) for b in rows] == [
        ("source", "Source J2"),
        ("regulator", "Regulator U6"),
        ("undriven", "Undriven rail +1V8A_O"),
    ]
    assert rows[1].depth == 1 and rows[1].x > rows[0].x
    # The regulator's own row sits below the source row.
    assert _node(model, "U6").y > _node(model, "R18").y


def test_regulator_is_linked_by_tags_not_a_wire() -> None:
    model = build_topology_model(TREE, layout="rows")
    ref = next(t for t in model.tags if t.kind == "ref")
    feed = next(t for t in model.tags if t.kind == "feed")
    u6 = _node(model, "U6")
    assert ref.text == "U6" and ref.net == "+12V"
    assert feed.net == "+12V"
    # Each end jumps to the other.
    assert ref.target is not None and u6.x <= ref.target[0] <= u6.x + u6.width
    assert feed.target == (ref.x, ref.y)
    # No wire on +12V reaches the regulator's row (bar the feed tag's stub).
    for w in model.wires:
        if w.net == "+12V" and w.routing_kind != "tag_stub":
            ys = [y for _, y in parse_wire_path(w.path_d)]
            assert max(ys) < u6.y


def test_local_grounds_replace_the_return_rail() -> None:
    model = build_topology_model(TREE, layout="rows")
    assert model.gnd_bus_y is None and model.gnd_symbol_x is None
    gnd_ports = [p for n in model.nodes for p in n.ports if p.net == GND_NET]
    assert gnd_ports and all(p.side == "bottom" for p in gnd_ports)
    assert len(model.gnd_glyphs) == len(gnd_ports)


def test_off_tree_bridges_hidden_by_default() -> None:
    hidden = build_topology_model(TREE, layout="rows")
    assert not any(n.node_id in ("R9", "R36", "R11") for n in hidden.nodes)
    assert any("3 auto-bridge" in note for note in hidden.notes)

    shown = build_topology_model(TREE, layout="rows", show_bridges=True)
    chips = {n.node_id for n in shown.nodes if n.compact}
    assert {"R9", "R36", "R11"} <= chips
    groups = {b.title for b in shown.bands if b.kind == "bridge_group"}
    assert groups == {"SCL", "SDA"}
    assert not shown.notes


def test_bridge_on_a_powered_rail_is_an_inline_chip() -> None:
    md = _md(
        _d("SOURCE", "J1", "5 V", P="V5", N="GND"),
        _bridge("R7", "V5"),
        _d("SINK", "U1", "1 A", P="V5", N="GND"),
    )
    model = build_topology_model(md, layout="rows")
    chip = _node(model, "R7")
    assert chip.compact
    assert not model.notes  # nothing hidden: R7 is on the tree


def test_shunt_to_ground_is_a_full_symbol_not_a_chip() -> None:
    md = _md(
        _d("SOURCE", "J1", "5 V", P="V5", N="GND"),
        _d("RESISTOR", "R3", "50 mOhm", P="V5", N="GND"),
    )
    model = build_topology_model(md, layout="rows")
    r3 = _node(model, "R3")
    assert not r3.compact
    assert [p.side for p in r3.ports if p.net == GND_NET] == ["bottom"]


def test_rail_with_no_loads_ends_in_an_end_tag() -> None:
    model = build_topology_model(TREE, layout="rows")
    end = [t for t in model.tags if t.kind == "end"]
    assert [t.net for t in end] == ["+1V8A"]
    undriven = [t for t in model.tags if t.kind == "undriven"]
    assert [t.net for t in undriven] == ["+1V8A_O"]


@pytest.mark.parametrize("net", ["AY", "BX", "BY"])
def test_loop_back_is_tagged_with_a_jump_to_the_other_part(net: str) -> None:
    # Stepper driver U1 feeds coil J7 on AX; J7 returns to U1 on AY/BX/BY.
    # Those nets can't be tree wires, so both ends are tags that point at
    # each other - not a "no loads" end on U1.
    md = load_topology_fixture("project_a_stepper_loop_rails")
    model = build_topology_model(md, layout="rows")
    tags = [t for t in model.tags if t.net == net]
    assert len(tags) == 2
    assert all(t.kind == "net" and t.target is not None for t in tags)
    u1, j7 = _node(model, "U1"), _node(model, "J7")

    def owner(t):
        for n in (u1, j7):
            if n.x - 1 <= t.target[0] <= n.x + n.width + 1:
                return n.node_id
        return None

    assert {owner(t) for t in tags} == {"U1", "J7"}


def _all_fixture_names() -> list[str]:
    return sorted(p.stem for p in _FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("name", _all_fixture_names())
def test_fixture_layout_is_clean(name: str) -> None:
    model = build_topology_model(load_topology_fixture(name), layout="rows")
    assert model.layout == "rows"
    render_topology_svg(model)  # must not raise

    geo = compute_schematic_geometry(model.wires)
    # Tree wiring never crosses another net.
    assert not any(geo.vert_crossings.values()), name

    # No two symbols overlap.
    boxes = [(n.node_id, n.x, n.y, n.x + n.width, n.y + n.height) for n in model.nodes]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            overlap = a[1] < b[3] - 1 and b[1] < a[3] - 1 and a[2] < b[4] - 1 and b[2] < a[4] - 1
            assert not overlap, (name, a[0], b[0])

    # No wire runs through a symbol it doesn't connect to.
    for seg in geo.segments:
        for n in model.nodes:
            x_lo, x_hi = sorted((seg.x1, seg.x2))
            y_lo, y_hi = sorted((seg.y1, seg.y2))
            inside_x = x_lo < n.x + n.width - WIRE_EPS and x_hi > n.x + WIRE_EPS
            inside_y = y_lo < n.y + n.height - WIRE_EPS and y_hi > n.y + WIRE_EPS
            assert not (inside_x and inside_y), (name, seg, n.node_id)

    # Every rail port is wired or tagged; every ground port is grounded.
    ends = set()
    for w in model.wires:
        pts = parse_wire_path(w.path_d)
        for x, y in (pts[0], pts[-1]):
            ends.add((round(x, 1), round(y, 1)))
    for n in model.nodes:
        for p in n.ports:
            assert (round(p.x, 1), round(p.y, 1)) in ends, (name, n.node_id, p.terminal)

    # Everything fits on the canvas.
    for n in model.nodes:
        assert n.x + n.width <= model.width and n.y + n.height <= model.height
    for t in model.tags:
        assert t.x >= 0 and t.x + t.width <= model.width


def test_add_sink_handles_are_off_by_default() -> None:
    model = build_topology_model(TREE, layout="rows")
    assert not any(t.kind == "add" for t in model.tags)
    assert not any(w.routing_kind == "add_tap" for w in model.wires)


def test_add_sink_handle_counts_parts_not_already_on_the_rail() -> None:
    cands = {
        "+vdd": {"J1", "U9", "u10"},   # J1 is already the sink: 2 left
        "+12V": {"U6"},                # U6 is drawn there (fed regulator)
        "+1V8A": {"U9"},               # no loads: the end tag offers it
        "12P0V": {"J2"},               # J2 is the source on it
    }
    model = build_topology_model(TREE, layout="rows", add_candidates=cands)
    (add,) = [t for t in model.tags if t.kind == "add"]
    assert (add.net, add.text) == ("+VDD", "+ SINK (2)")
    # One tap below the sink, in the sink's column, wired dashed off its bus.
    j1 = _node(model, "J1")
    assert add.x == pytest.approx(j1.x) and add.y > j1.y + j1.height
    (tap,) = [w for w in model.wires if w.routing_kind == "add_tap"]
    assert tap.dashed and tap.net == "+VDD"
    pts = parse_wire_path(tap.path_d)
    assert pts[-1] == pytest.approx((add.x, add.y))
    # It starts on the rail's own solid wiring (bus, or trunk when straight).
    sx, sy = pts[0]
    on_rail = [
        s for s in compute_schematic_geometry(model.wires).segments
        if s.net == "+VDD"
        and min(s.x1, s.x2) - WIRE_EPS <= sx <= max(s.x1, s.x2) + WIRE_EPS
        and min(s.y1, s.y2) - WIRE_EPS <= sy <= max(s.y1, s.y2) + WIRE_EPS
    ]
    assert on_rail
    # Clickable and highlights its rail.
    assert find_tag_at(model, add.x + add.width / 2, add.y) is add
    assert topology_net_at(model, add.x + add.width / 2, add.y) == "+VDD"
    assert "Manual setup" in topology_tooltip_at(model, add.x + 4, add.y)
    assert 'stroke-dasharray="3 2"' in render_topology_svg(model)


def test_no_add_handle_when_every_candidate_is_already_on_the_rail() -> None:
    model = build_topology_model(TREE, layout="rows", add_candidates={"+VDD": {"J1"}})
    assert not any(t.kind == "add" for t in model.tags)


@pytest.mark.parametrize("name", _all_fixture_names())
def test_fixture_layout_is_clean_with_add_handles(name: str) -> None:
    md = load_topology_fixture(name)
    plain = build_topology_model(md, layout="rows")
    cands = {w.net: {"ZZ999"} for w in plain.wires if w.net and w.net != GND_NET}
    model = build_topology_model(md, layout="rows", add_candidates=cands)
    adds = [t for t in model.tags if t.kind == "add"]
    taps = [w for w in model.wires if w.routing_kind == "add_tap"]
    assert len(adds) == len(taps)
    geo = compute_schematic_geometry(model.wires)
    assert not any(geo.vert_crossings.values()), name

    def overlaps(a, b) -> bool:
        return a[0] < b[2] - 1 and b[0] < a[2] - 1 and a[1] < b[3] - 1 and b[1] < a[3] - 1

    node_boxes = [(n.x, n.y, n.x + n.width, n.y + n.height) for n in model.nodes]
    tag_boxes = [(t.x, t.y - t.height / 2, t.x + t.width, t.y + t.height / 2)
                 for t in model.tags]
    for t in adds:
        box = (t.x, t.y - t.height / 2, t.x + t.width, t.y + t.height / 2)
        assert not any(overlaps(box, nb) for nb in node_boxes), (name, t.net)
        assert sum(overlaps(box, tb) for tb in tag_boxes) == 1, (name, t.net)  # itself
        assert t.x + t.width <= model.width and t.y + t.height / 2 <= model.height
    # The dashed taps run clear of every symbol and every other net's wire.
    for w in taps:
        pts = parse_wire_path(w.path_d)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            lo_x, hi_x = sorted((x1, x2))
            lo_y, hi_y = sorted((y1, y2))
            for nb in node_boxes:
                assert not (lo_x < nb[2] - WIRE_EPS and hi_x > nb[0] + WIRE_EPS
                            and lo_y < nb[3] - WIRE_EPS and hi_y > nb[1] + WIRE_EPS), name
            for s in geo.segments:
                if s.net == w.net:
                    continue
                s_lo_x, s_hi_x = sorted((s.x1, s.x2))
                s_lo_y, s_hi_y = sorted((s.y1, s.y2))
                touch = (lo_x <= s_hi_x + WIRE_EPS and s_lo_x <= hi_x + WIRE_EPS
                         and lo_y <= s_hi_y + WIRE_EPS and s_lo_y <= hi_y + WIRE_EPS)
                assert not touch, (name, w.net, s.net)


def test_tag_hit_testing() -> None:
    model = build_topology_model(TREE, layout="rows")
    ref = next(t for t in model.tags if t.kind == "ref")
    x, y = ref.x + ref.width / 2, ref.y
    assert find_tag_at(model, x, y) is ref
    assert topology_net_at(model, x, y) == "+12V"
    assert "U6" in (topology_tooltip_at(model, x, y) or "")


def test_jump_icon_on_parts_with_a_pad() -> None:
    from fypa.topology.hit_test import find_jump_icon_at, jump_icon_center

    md = load_topology_fixture("project_b_compact")   # pins carry x/y
    for layout in ("rows", "columns"):
        model = build_topology_model(md, layout=layout)
        with_pad = [n for n in model.nodes if n.jump_row]
        assert with_pad, layout
        for node in with_pad:
            cx, cy = jump_icon_center(node)
            assert node.x < cx < node.x + node.width and node.y < cy < node.y + node.height
            assert find_jump_icon_at(model, cx, cy) is node
            assert "Heatmap" in topology_tooltip_at(model, cx, cy)
        assert 'class="topo-jump"' in render_topology_svg(model)
    # No pad → no icon.
    model = build_topology_model(TREE, layout="rows")
    assert all(jump_icon_center(n) is None for n in model.nodes)


def test_classic_layout_is_still_the_api_default() -> None:
    model = build_topology_model(TREE)
    assert model.layout == "columns"
    assert not model.tags and not model.bands
