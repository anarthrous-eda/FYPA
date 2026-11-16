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
        ("source", "12P0V"),
        ("regulator", "+VDD"),
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
        "+1V8A": {"R24"},              # only the resistor itself: no handle
        "12P0V": {"J2"},               # J2 is the source on it
    }
    model = build_topology_model(TREE, layout="rows", add_candidates=cands)
    assert [t.net for t in model.tags if t.kind == "end"] == ["+1V8A"]
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


def test_add_sink_handle_on_the_far_side_of_a_series_part() -> None:
    # A regulator feeding its loads through a series resistor: the rail past
    # the resistor has nothing on it yet, so it ends in a "+ SINK" handle
    # (not a grey "no loads" tag), wired and labelled off the resistor.
    md = _md(
        _d("SOURCE", "J2", "12 V", P="+12V", N="GND"),
        _d("REGULATOR", "U6", "V=1 V", IN_P="+12V", IN_N="GND", OUT_P="VREG", OUT_N="GND"),
        _d("SERIES", "R5", "0 mOhm", P="VREG", N="+VDD"),
    )
    cands = {"VREG": {"U6", "R5"}, "+VDD": {"R5", "U2", "U3"}}
    model = build_topology_model(md, layout="rows", add_candidates=cands)
    assert not any(t.kind == "end" for t in model.tags)
    (add,) = [t for t in model.tags if t.kind == "add"]
    assert (add.net, add.text) == ("+VDD", "+ SINK (2)")
    r5 = _node(model, "R5")
    out = next(p for p in r5.ports if p.net == "+VDD")
    assert out.side == "right" and add.y == pytest.approx(out.y) and add.x > out.x
    stub = next(w for w in model.wires
                if w.net == "+VDD" and w.src_node == "R5" and w.src_terminal == out.terminal)
    assert stub.label == "+VDD" and not stub.dashed
    assert parse_wire_path(stub.path_d)[-1] == pytest.approx((add.x, add.y))
    assert find_tag_at(model, add.x + add.width / 2, add.y) is add
    # Handles off: back to the end tag.
    plain = build_topology_model(md, layout="rows")
    assert [t.net for t in plain.tags if t.kind == "end"] == ["+VDD"]


def _linked_rail(*extra: dict) -> dict:
    # U10 → VOUT_2V5 → 0 Ω R106 → +ETH_2V5 (merged into VOUT_2V5 by the
    # loader, so the link is an auto-bridge on one net).
    link = _bridge("R106", "VOUT_2V5")
    link["shorted_nets"] = "VOUT_2V5 ↔ +ETH_2V5"
    return _md(
        _d("SOURCE", "J1", "12 V", P="+12V", N="GND"),
        _d("REGULATOR", "U10", "V=2.5 V", IN_P="+12V", IN_N="GND",
           OUT_P="VOUT_2V5", OUT_N="GND"),
        link, *extra,
    )


_LINK_SIDES = {"VOUT_2V5": {"U10", "R106", "C52"}, "+ETH_2V5": {"R106", "J9", "TP11"}}


def test_merged_link_draws_its_far_side_with_the_parts_past_it() -> None:
    md = _linked_rail(
        _d("SINK", "J9", "1 A", P="VOUT_2V5", N="GND"),
        _d("SINK", "U7", "1 A", P="VOUT_2V5", N="GND"),   # not in the map: near side
    )
    model = build_topology_model(md, layout="rows", premerge_parts=_LINK_SIDES)
    r106, j9, u7 = _node(model, "R106"), _node(model, "J9"), _node(model, "U7")
    assert r106.compact
    assert [(p.side, p.label) for p in r106.ports] == [
        ("left", "VOUT_2V5"), ("right", "+ETH_2V5")]
    far = next(p for p in r106.ports if p.side == "right")
    # J9 hangs off R106's far side; U7 stays on the regulator's side.
    assert j9.x > r106.x + r106.width and u7.x <= r106.x
    trunk = next(w for w in model.wires
                 if w.src_node == "R106" and w.src_terminal == far.terminal)
    assert trunk.label == "+ETH_2V5" and trunk.net == "VOUT_2V5"
    # Without the map (e.g. the report): a one-port chip, as before.
    plain = build_topology_model(md, layout="rows")
    assert [p.side for p in _node(plain, "R106").ports] == ["left"]


def test_merged_link_far_side_gets_its_own_add_handle() -> None:
    cands = {"VOUT_2V5": {"U10", "J9", "TP11", "U8"}}
    model = build_topology_model(
        _linked_rail(), layout="rows", add_candidates=cands, premerge_parts=_LINK_SIDES)
    adds = sorted((t.x, t.text) for t in model.tags if t.kind == "add")
    r106 = _node(model, "R106")
    far = next(p for p in r106.ports if p.side == "right")
    # U8 (unknown side) on the regulator's side; J9 and TP11 past R106.
    assert [text for _, text in adds] == ["+ SINK (1)", "+ SINK (2)"]
    (far_add,) = [t for t in model.tags if t.kind == "add" and t.text == "+ SINK (2)"]
    assert far_add.y == pytest.approx(far.y) and far_add.x > far.x
    assert far_add.net == "VOUT_2V5" and "+ETH_2V5" in far_add.tooltip
    # Handles off: the far side ends in a "no loads" tag named for its side.
    plain = build_topology_model(_linked_rail(), layout="rows", premerge_parts=_LINK_SIDES)
    assert [(t.text, t.net) for t in plain.tags if t.kind == "end"] == [
        ("+ETH_2V5", "VOUT_2V5")]


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
    # Under a rail's last load (dashed tap), or at a load-less rail's port
    # in place of its "no loads" end tag.
    assert not any(t.kind == "end" for t in model.tags), name
    assert len(adds) >= len(taps)
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


def test_row_header_names_the_output_rail_then_the_part() -> None:
    rows = _rows(build_topology_model(TREE, layout="rows"))
    assert rows[0].subtitle.startswith("J2")
    assert rows[1].subtitle.startswith("U6") and "+12V" in rows[1].subtitle


def test_collapsed_row_is_header_only_and_rows_below_close_up() -> None:
    full = build_topology_model(TREE, layout="rows")
    key = _rows(full)[0].key
    folded = build_topology_model(TREE, layout="rows", collapsed={key})
    src, reg = _rows(folded)[:2]
    assert src.collapsed and not reg.collapsed
    ids = {n.node_id for n in folded.nodes}
    assert "J2" not in ids and "R18" not in ids and "U6" in ids
    assert reg.y < _rows(full)[1].y
    assert folded.height < full.height
    # Nothing is left stranded off the canvas.
    assert all(n.y < folded.height for n in folded.nodes)
    assert all(t.y < folded.height for t in folded.tags)
    # U6's feed tag now jumps to the collapsed source's header.
    feed = next(t for t in folded.tags if t.net == "+12V" and t.target)
    assert src.y <= feed.target[1] <= src.y + 26


def test_series_chip_widens_to_fit_a_long_designator() -> None:
    from fypa.topology.rows import CHIP_W
    from fypa.topology.util import estimate_text_width

    md = _md(
        _d("SOURCE", "J2", "12 V", P="+12V", N="GND"),
        _bridge("R101_VCC_HDIO_SENSE", "+12V"),
        _d("SINK", "J1", "1 A", P="+12V", N="GND"),
    )
    model = build_topology_model(md, layout="rows")
    chip = _node(model, "R101_VCC_HDIO_SENSE")
    assert chip.compact and chip.width > CHIP_W
    need = (estimate_text_width(chip.label, 9)
            + estimate_text_width(chip.value_str, 8) + 12 + 10 + 8)
    assert chip.width >= need
    svg = render_topology_svg(model)
    assert "R101_VCC_HDIO_SENSE<" in svg


def test_merged_rail_driver_side_uses_its_own_premerge_name() -> None:
    # J4 → +12V_MEZZ → R80 → +12V_IN → U31, merged under the name +12V_IN:
    # the source's side is labelled +12V_MEZZ, the far side +12V_IN.
    link = _bridge("R80", "+12V_IN")
    link["shorted_nets"] = "+12V_IN ↔ +12V_MEZZ"
    md = _md(
        _d("SOURCE", "J4", "12 V", P="+12V_IN", N="GND"),
        link,
        _d("SINK", "U31", "10 mA", P="+12V_IN", N="GND"),
    )
    sides = {"+12V_IN": {"R80", "U31"}, "+12V_MEZZ": {"J4", "R80"}}
    model = build_topology_model(md, layout="rows", premerge_parts=sides)
    j4, r80 = _node(model, "J4"), _node(model, "R80")
    assert [p.label for p in j4.ports if p.net != GND_NET] == ["+12V_MEZZ"]
    near = next(w for w in model.wires if w.src_node == "J4" and w.label)
    far_port = next(p for p in r80.ports if p.side == "right")
    far = next(w for w in model.wires
               if w.src_node == "R80" and w.src_terminal == far_port.terminal)
    assert near.label == "+12V_MEZZ" and near.net == "+12V_IN"
    assert far.label == "+12V_IN"
    assert _rows(model)[0].title == "+12V_MEZZ"
    # Without the pre-merge map nothing is renamed.
    plain = build_topology_model(md, layout="rows")
    assert _rows(plain)[0].title == "+12V_IN"
