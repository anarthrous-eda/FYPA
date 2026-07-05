"""Unit tests for the KiCAD import adapter (:mod:`fypa.kicad`).

Covers the self-contained S-expression reader and the individual mapping rules
in :mod:`fypa.kicad.extract` (layer ids, arc math, net indexing, zone → region,
pad-shape codes, footprint properties → parameters), plus an end-to-end extract
of the bundled ``KiCAD_Sandbox`` example.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from fypa.altium.extract import NO_NET
from fypa.kicad import sexpr
from fypa.kicad.extract import (
    _arc_from_three_points,
    _build_nets,
    extract_kicad_project,
    kicad_layer_to_fypa_id,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PCB = REPO_ROOT / "ExampleDesigns" / "KiCAD_Sandbox" / "KiCAD_Sandbox.kicad_pcb"


# --- S-expression reader ----------------------------------------------------
def test_sexpr_parses_tags_atoms_and_nesting():
    node = sexpr.parse('(foo (bar 1 2) (baz "hello world") 42)')
    assert node.tag == "foo"
    assert node.node("bar").atoms == ["1", "2"]
    assert node.node("bar").f_at(1) == 2.0
    assert node.s("baz") == "hello world"      # quoted string, spaces preserved
    assert node.atom(0) == "42"                # trailing bare atom


def test_sexpr_handles_escaped_quotes_and_backslashes():
    node = sexpr.parse(r'(net 3 "Net-(R1-\"A\")")')
    assert node.atom(0) == "3"
    assert node.atom(1) == 'Net-(R1-"A")'


def test_sexpr_empty_quoted_string_is_preserved():
    node = sexpr.parse('(net 0 "")')
    assert node.atom(0) == "0"
    assert node.atom(1) == ""


def test_sexpr_rejects_unbalanced():
    with pytest.raises(ValueError):
        sexpr.parse("(foo (bar 1)")


# --- layer-id mapping -------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("F.Cu", 1),
    ("B.Cu", 32),
    ("In1.Cu", 2),
    ("In7.Cu", 8),
    ("In30.Cu", 31),
    ("F.SilkS", None),
    ("Edge.Cuts", None),
    ("F.Mask", None),
])
def test_layer_id_mapping(name, expected):
    assert kicad_layer_to_fypa_id(name) == expected


# --- arc math ---------------------------------------------------------------
def test_arc_sweep_passes_through_midpoint():
    from fypa.altium.extract import Pt2D
    start, mid, end = Pt2D(0.0, 0.0), Pt2D(10.0, 10.0), Pt2D(20.0, 0.0)
    center, radius, a0, a1 = _arc_from_three_points(start, mid, end)
    # Center is equidistant from all three points.
    for p in (start, mid, end):
        assert math.isclose(math.hypot(p.x - center.x, p.y - center.y),
                            radius, rel_tol=1e-9)
    # The CCW sweep a0 -> a0 + (a1-a0)%360 must contain the midpoint angle.
    sweep = (a1 - a0) % 360.0
    a_mid = math.degrees(math.atan2(mid.y - center.y, mid.x - center.x))
    assert (a_mid - a0) % 360.0 <= sweep + 1e-9


def test_arc_collinear_points_return_none():
    from fypa.altium.extract import Pt2D
    assert _arc_from_three_points(
        Pt2D(0, 0), Pt2D(1, 0), Pt2D(2, 0)) is None


# --- net indexing -----------------------------------------------------------
def test_net_index_uses_kicad_number_and_maps_net0_to_no_net():
    pcb = sexpr.parse('(kicad_pcb (net 0 "") (net 1 "+5V") (net 2 "GND"))')
    nets, net_index = _build_nets(pcb)
    assert [n.name for n in nets] == ["", "+5V", "GND"]
    assert net_index(0) == NO_NET       # KiCAD "unconnected"
    assert net_index(1) == 1
    assert net_index(2) == 2
    assert net_index(None) == NO_NET
    assert net_index(99) == NO_NET      # out of range


# --- end-to-end extract of the bundled example ------------------------------
@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_example_board_extracts_expected_records():
    proj = extract_kicad_project(EXAMPLE_PCB)
    assert [n.name for n in proj.nets] == ["", "+5V", "GND"]
    assert len(proj.tracks) == 1
    assert len(proj.vias) == 2
    assert len(proj.pads) == 4
    assert len(proj.shape_based_regions) == 1        # the B.Cu GND pour
    assert proj.enabled_copper_layer_ids() == [1, 32]

    # Footprint custom fields land in RawPcbComponent.parameters — this is the
    # PDN_* directive source.
    by_des = {c.designator: c for c in proj.pcb_components}
    assert by_des["U1"].parameters["PDN_ROLE"] == "SOURCE"
    assert by_des["U1"].parameters["PDN_V"] == "5"
    assert by_des["U2"].parameters["PDN_ROLE"] == "SINK"

    # Schematic parsed for parity (Reference + fields + pins).
    sch = {c.designator: c for c in proj.sch_components}
    assert set(sch) == {"U1", "U2"}
    assert sch["U1"].pin_designators == ("1", "2")


@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_example_roundrect_pad_shape_and_corner_radius():
    proj = extract_kicad_project(EXAMPLE_PCB)
    pad = proj.pads[0]
    assert pad.shape == 4                 # PAD_SHAPE_ROUNDED_RECTANGLE
    # roundrect_rratio 0.25 -> corner_radius_pct = 0.25 * 200 = 50.
    assert pad.corner_radius_pct == 50


@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_example_through_hole_via_spans_top_to_bottom():
    proj = extract_kicad_project(EXAMPLE_PCB)
    via = proj.vias[0]
    assert (via.layer_start, via.layer_end) == (1, 32)
    assert via.net_index == 2             # GND


@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_example_loads_and_is_solveable():
    from fypa.kicad.loader import load_kicad_project
    loaded = load_kicad_project(EXAMPLE_PCB)
    assert loaded.is_solveable
    assert not loaded.annotations.errors
    roles = sorted(type(d).__name__ for d in loaded.annotations.directives)
    assert roles == ["SinkSpec", "SourceSpec"]


def test_unfilled_zone_is_skipped_with_warning(caplog):
    """A zone with no filled_polygon must not crash — it's warned and skipped."""
    board = (
        '(kicad_pcb (version 20241229)'
        ' (net 0 "") (net 2 "GND")'
        ' (setup (stackup'
        '   (layer "F.Cu" (type "copper") (thickness 0.035))'
        '   (layer "B.Cu" (type "copper") (thickness 0.035))))'
        ' (zone (net 2) (net_name "GND") (layer "B.Cu")))'
    )
    import logging
    with caplog.at_level(logging.WARNING):
        proj = _extract_from_string(board)
    assert len(proj.shape_based_regions) == 0
    assert any("filled_polygon" in r.message for r in caplog.records)


def _extract_from_string(text: str):
    """Write *text* to a temp .kicad_pcb and extract it."""
    import tempfile
    with tempfile.NamedTemporaryFile(
        "w", suffix=".kicad_pcb", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        path = f.name
    try:
        return extract_kicad_project(path)
    finally:
        Path(path).unlink(missing_ok=True)


_STACKUP = (
    ' (setup (stackup'
    '   (layer "F.Cu" (type "copper") (thickness 0.035))'
    '   (layer "B.Cu" (type "copper") (thickness 0.035))))'
)


# --- pad rotation: absolute in the file, must not re-add footprint angle -----
def test_pad_rotation_is_absolute_not_footprint_plus_pad():
    """KiCAD stores the pad `at` angle as the absolute board orientation.

    The parent footprint's rotation is already baked in, so the extractor must
    use the pad angle as-is — adding the footprint's angle again double-counts
    it. The pad *position*, by contrast, is footprint-relative and is rotated
    by the footprint angle. This board (footprint at 90°, pad at absolute 90°)
    pins both behaviours.
    """
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "N1")' + _STACKUP +
        ' (footprint "test:fp" (layer "F.Cu") (at 50 50 90)'
        '   (property "Reference" "U1")'
        '   (pad "1" smd rect (at 1 0 90) (size 2 1)'
        '     (layers "F.Cu") (net 1 "N1"))))'
    )
    proj = _extract_from_string(board)
    assert len(proj.pads) == 1
    pad = proj.pads[0]
    assert pad.rotation_deg == 90.0            # absolute — NOT 90 + 90
    # Position IS footprint-relative → local (1,0) rotated by frot=90° (Y-down).
    assert math.isclose(pad.center.x, 50.0, abs_tol=1e-6)
    assert math.isclose(pad.center.y, 49.0, abs_tol=1e-6)


# --- oval (slot) drills stamp a chain, don't collapse to one circle ----------
def test_oval_plated_slot_stamps_via_chain():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "N1")' + _STACKUP +
        ' (footprint "test:fp" (layer "F.Cu") (at 10 10 0)'
        '   (property "Reference" "J1")'
        '   (pad "1" thru_hole oval (at 0 0 0) (size 3 1.5)'
        '     (drill oval 2 1) (layers "*.Cu") (net 1 "N1"))))'
    )
    proj = _extract_from_string(board)
    assert len(proj.pads) == 1
    pad = proj.pads[0]
    assert pad.is_through_hole
    assert pad.hole_shape == 2                  # obround slot preserved
    assert math.isclose(pad.slot_length_mm, 2.0)
    assert math.isclose(pad.hole_mm, 1.0)       # short-axis bore, not 2.0
    # The plated barrel must span the slot's length, so a chain of vias — not
    # the single small circle the old code produced.
    assert len(proj.vias) >= 2
    xs = sorted(v.center.x for v in proj.vias)
    assert math.isclose(xs[0], 9.5) and math.isclose(xs[-1], 10.5)
    assert all(math.isclose(v.hole_diameter_mm, 1.0) for v in proj.vias)
    assert all((v.layer_start, v.layer_end) == (1, 32) for v in proj.vias)
    assert all(v.net_index == 1 for v in proj.vias)


def test_oval_npth_slot_stamps_hole_chain():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (footprint "test:fp" (layer "F.Cu") (at 10 10 0)'
        '   (property "Reference" "H1")'
        '   (pad "" np_thru_hole oval (at 0 0 0) (size 2 1)'
        '     (drill oval 2 1) (layers "*.Cu"))))'
    )
    proj = _extract_from_string(board)
    assert len(proj.pads) == 0                  # NPTH is not a copper pad
    assert len(proj.vias) == 0                   # non-plated → no barrel
    assert len(proj.npth_holes) >= 2            # a chain of holes, not one
    assert all(math.isclose(h.diameter_mm, 1.0) for h in proj.npth_holes)


# --- board outline from rect / circle / poly tools ---------------------------
def test_outline_from_gr_rect():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (gr_rect (start 0 0) (end 10 20) (layer "Edge.Cuts")))'
    )
    ol = _extract_from_string(board).board_outline
    assert len(ol) >= 4
    xs = [p.x for p in ol]
    ys = [p.y for p in ol]
    assert min(xs) == 0 and max(xs) == 10
    assert min(ys) == 0 and max(ys) == 20


def test_outline_from_gr_circle():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (gr_circle (center 5 5) (end 15 5) (layer "Edge.Cuts")))'
    )
    ol = _extract_from_string(board).board_outline
    assert len(ol) >= 8
    xs = [p.x for p in ol]
    assert math.isclose(min(xs), -5.0, abs_tol=0.2)
    assert math.isclose(max(xs), 15.0, abs_tol=0.2)


def test_outline_from_gr_poly():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (gr_poly (pts (xy 0 0) (xy 30 0) (xy 30 30) (xy 0 30))'
        '   (layer "Edge.Cuts")))'
    )
    ol = _extract_from_string(board).board_outline
    xs = [p.x for p in ol]
    ys = [p.y for p in ol]
    assert min(xs) == 0 and max(xs) == 30
    assert min(ys) == 0 and max(ys) == 30


def test_open_outline_warns(caplog):
    import logging
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts")))'
    )
    with caplog.at_level(logging.WARNING):
        _extract_from_string(board)
    assert any("closed board outline" in r.message for r in caplog.records)


# --- robustness: a malformed numeric token warns + degrades, doesn't crash ---
def test_malformed_net_token_degrades_to_no_net(caplog):
    import logging
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "N1")' + _STACKUP +
        ' (segment (start 0 0) (end 1 0) (width 0.25)'
        '   (layer "F.Cu") (net "bogus")))'
    )
    with caplog.at_level(logging.WARNING):
        proj = _extract_from_string(board)
    assert len(proj.tracks) == 1
    assert proj.tracks[0].net_index == NO_NET
    assert any("malformed" in r.message for r in caplog.records)


# --- sexpr reader: cached multi-tag lookups stay correct ---------------------
def test_sexpr_repeated_tag_lookups_are_consistent():
    node = sexpr.parse("(root (a 1) (b 2) (a 3) (b 4))")
    assert [n.f_at(0) for n in node.nodes("a")] == [1.0, 3.0]
    assert [n.f_at(0) for n in node.nodes("b")] == [2.0, 4.0]
    # A second pass must hit the tag index and return the same nodes.
    assert [n.f_at(0) for n in node.nodes("a")] == [1.0, 3.0]
    assert node.node("a").f_at(0) == 1.0
    assert len(list(node.nodes(None))) == 4        # all child SNodes
