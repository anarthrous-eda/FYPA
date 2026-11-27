"""Unit tests for the KiCad import adapter (:mod:`fypa.kicad`).

Covers the self-contained S-expression reader and the individual mapping rules
in :mod:`fypa.kicad.extract` (layer ids, arc math, net indexing, zone → region,
pad-shape codes, footprint properties → parameters), plus an end-to-end extract
of the bundled ``KiCad_Sandbox`` example.
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
EXAMPLE_PCB = REPO_ROOT / "ExampleDesigns" / "KiCad_Sandbox" / "KiCad_Sandbox.kicad_pcb"


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
    assert net_index(0) == NO_NET       # KiCad "unconnected"
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

    # Footprint custom fields land in RawPcbComponent.parameters - this is the
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
    # KiCad paths are dispatched inside load_project itself.
    from fypa.altium.loader import load_project
    loaded = load_project(EXAMPLE_PCB)
    assert loaded.is_solveable
    assert not loaded.annotations.errors
    roles = sorted(type(d).__name__ for d in loaded.annotations.directives)
    assert roles == ["SinkSpec", "SourceSpec"]


def test_unfilled_zone_is_skipped_with_warning(caplog):
    """A zone with no filled_polygon must not crash - it's warned and skipped."""
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
    assert any("not filled" in r.message for r in caplog.records)
    # ...and it reaches the user: load_project adds load_notes to the
    # annotation warnings the Design issues panel lists.
    assert any("GND" in n and "Fill All Zones" in n for n in proj.load_notes)


def _write_board(text: str) -> Path:
    """Write *text* to a temp .kicad_pcb and return its path."""
    import tempfile
    with tempfile.NamedTemporaryFile(
        "w", suffix=".kicad_pcb", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        return Path(f.name)


def _extract_from_string(text: str):
    """Write *text* to a temp .kicad_pcb and extract it."""
    path = _write_board(text)
    try:
        return extract_kicad_project(path)
    finally:
        path.unlink(missing_ok=True)


_STACKUP = (
    ' (setup (stackup'
    '   (layer "F.Cu" (type "copper") (thickness 0.035))'
    '   (layer "B.Cu" (type "copper") (thickness 0.035))))'
)


# --- pad rotation: absolute in the file, must not re-add footprint angle -----
def test_pad_rotation_is_absolute_not_footprint_plus_pad():
    """KiCad stores the pad `at` angle as the absolute board orientation.

    The parent footprint's rotation is already baked in, so the extractor must
    use the pad angle as-is - adding the footprint's angle again double-counts
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
    assert pad.rotation_deg == 90.0            # absolute - NOT 90 + 90
    # Position IS footprint-relative → local (1,0) rotated by frot=90° gives
    # (50, 49) in the Y-down file, which is (50, -49) in FYPA's Y-up frame.
    assert math.isclose(pad.center.x, 50.0, abs_tol=1e-6)
    assert math.isclose(pad.center.y, -49.0, abs_tol=1e-6)


def test_pad_at_45_degrees_lines_up_with_its_footprint():
    """Two 2 x 1 pads at local (-2, 0) and (2, 0) on a footprint turned 45°
    lie end to end, so each pad's long side runs along the line joining their
    centres. Pad positions and pad angles go through different maths (the
    footprint rotation vs the pad's absolute angle); reading the Y-down file
    without mirroring it turned the pads across that line, which only shows at
    angles that are not a multiple of 90°. Checked on the copper FYPA builds."""
    from fypa.altium.loader import load_project
    pads = "".join(
        f' (pad "{n}" smd rect (at {x} 0 45) (size 2 1) (layers "F.Cu") (net 1 "N1"))'
        for n, x in (("1", -2), ("2", 2)))
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "N1")' + _STACKUP +
        ' (footprint "test:fp" (layer "F.Cu") (at 50 50 45)'
        '   (property "Reference" "U1")' + pads + '))'
    )
    path = _write_board(board)
    try:
        shape = next(L.shape for L in load_project(path).geometry if L.layer_id == 1)
    finally:
        path.unlink(missing_ok=True)
    parts = list(getattr(shape, "geoms", [shape]))
    assert len(parts) == 2
    a, b = (g.centroid for g in parts)
    ux, uy = b.x - a.x, b.y - a.y
    n = math.hypot(ux, uy)
    ux, uy = ux / n, uy / n
    for g in parts:
        c = g.centroid

        def extent(dx: float, dy: float, g=g, c=c) -> float:
            d = [(x - c.x) * dx + (y - c.y) * dy for x, y in g.exterior.coords]
            return max(d) - min(d)

        assert math.isclose(extent(ux, uy), 2.0, abs_tol=0.05)   # along the line
        assert math.isclose(extent(-uy, ux), 1.0, abs_tol=0.05)  # across it


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
    # The plated barrel must span the slot's length, so a chain of vias - not
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
    assert min(ys) == -20 and max(ys) == 0      # Y-up: file (x, y) -> (x, -y)


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
    assert min(ys) == -30 and max(ys) == 0


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


# --- KiCad 6/7 files keep Reference / Value in fp_text -----------------------
def test_kicad7_reference_comes_from_fp_text():
    board = (
        '(kicad_pcb (version 20221018) (net 0 "") (net 1 "N1")' + _STACKUP +
        ' (footprint "test:fp" (layer "F.Cu") (at 0 0)'
        '   (property "PDN_ROLE" "SINK")'
        '   (fp_text reference "U7" (at 0 0) (layer "F.SilkS"))'
        '   (fp_text value "LOAD" (at 0 0) (layer "F.Fab"))'
        '   (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "N1"))))'
    )
    comp = _extract_from_string(board).pcb_components[0]
    assert comp.designator == "U7"
    assert comp.parameters["Value"] == "LOAD"
    assert comp.parameters["PDN_ROLE"] == "SINK"


# --- stackup: mask is not dielectric; plies and Dk / Df ----------------------
def test_stackup_skips_mask_and_weights_dielectric_plies():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")'
        ' (setup (stackup'
        '   (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))'
        '   (layer "F.Cu" (type "copper") (thickness 0.035))'
        '   (layer "dielectric 1" (type "prepreg") (thickness 0.1) (epsilon_r 4)'
        '     (loss_tangent 0.02) addsublayer (thickness 0.3) (epsilon_r 5)'
        '     (loss_tangent 0.01))'
        '   (layer "B.Cu" (type "copper") (thickness 0.07))'
        '   (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01)))))'
    )
    top, bot = _extract_from_string(board).stackup
    assert math.isclose(top.dielectric_thickness_mm, 0.4)
    assert math.isclose(top.dielectric_dk, (4 * 0.1 + 5 * 0.3) / 0.4)
    assert math.isclose(top.dielectric_df, (0.02 * 0.1 + 0.01 * 0.3) / 0.4)
    assert bot.dielectric_thickness_mm == 0.0      # B.Mask is not a dielectric
    assert bot.dielectric_dk is None
    assert math.isclose(bot.copper_thickness_mm, 0.07)


# --- file-format version ------------------------------------------------------
def test_board_format_note_by_version():
    from fypa.kicad.extract import board_format_note
    with pytest.raises(ValueError, match="KiCad 5"):
        board_format_note(20171130)
    assert "KiCad 6" in (board_format_note(20211014) or "")
    assert board_format_note(20221018) is None      # KiCad 7
    assert board_format_note(20240108) is None      # KiCad 8
    assert board_format_note(20241229) is None      # KiCad 9
    assert board_format_note(20260206) is None      # KiCad 10
    assert "newer than KiCad 10" in (board_format_note(20260410) or "")


def test_newer_board_format_reaches_load_notes():
    board = '(kicad_pcb (version 20990101) (net 0 "")' + _STACKUP + ')'
    assert any("newer" in n for n in _extract_from_string(board).load_notes)


def test_rule_area_zone_is_not_reported_unfilled():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (zone (net 0) (net_name "") (layer "F.Cu")'
        '   (keepout (tracks not_allowed) (copperpour not_allowed))))'
    )
    assert _extract_from_string(board).load_notes == ()


# --- .kicad_pro and dispatch through the Altium entry points -----------------
@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_kicad_pro_resolves_and_extract_project_dispatches():
    from fypa.altium.extract import extract_project
    from fypa.kicad import is_kicad_path
    from fypa.kicad.extract import resolve_kicad_board
    pro = EXAMPLE_PCB.with_suffix(".kicad_pro")
    assert is_kicad_path(pro) and is_kicad_path(EXAMPLE_PCB)
    assert not is_kicad_path(Path("x.PrjPcb"))
    assert resolve_kicad_board(pro) == EXAMPLE_PCB
    proj = extract_project(pro)
    assert proj.prjpcb_path == EXAMPLE_PCB
    assert {c.designator for c in proj.pcb_components} == {"U1", "U2"}


def test_kicad_fingerprint_covers_board_project_and_sheets(tmp_path):
    from fypa.cli import _project_file_fingerprints
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "b.kicad_sch").write_text("(kicad_sch)")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "power.kicad_sch").write_text("(kicad_sch)")
    (tmp_path / "b-backups").mkdir()
    (tmp_path / "b-backups" / "old.kicad_sch").write_text("(kicad_sch)")
    names = {Path(k).name for k in _project_file_fingerprints(board)}
    assert names == {"b.kicad_pcb", "b.kicad_pro", "b.kicad_sch", "power.kicad_sch"}
    # Editing a sub-sheet changes the fingerprint (a cached load is stale).
    before = _project_file_fingerprints(board)
    (tmp_path / "sub" / "power.kicad_sch").write_text("(kicad_sch (changed))")
    assert _project_file_fingerprints(board) != before


# --- copper drawn as graphics -------------------------------------------------
def test_board_copper_polygon_becomes_a_region_with_its_net():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "VIN")' + _STACKUP +
        ' (gr_poly (pts (xy 0 0) (xy 10 0) (xy 10 5) (xy 0 5))'
        '   (stroke (width 0) (type solid)) (fill solid) (layer "F.Cu") (net 1)))'
    )
    proj = _extract_from_string(board)
    assert len(proj.shape_based_regions) == 1
    r = proj.shape_based_regions[0]
    assert (r.layer_id, r.net_index) == (1, 1)
    ys = [v.pos.y for v in r.outline]
    assert min(ys) == -5 and max(ys) == 0             # mirrored to Y-up


def test_unfilled_copper_rect_is_its_stroked_edge():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (gr_rect (start 0 0) (end 10 5) (stroke (width 0.5) (type solid))'
        '   (fill none) (layer "B.Cu")))'
    )
    proj = _extract_from_string(board)
    assert proj.shape_based_regions == ()
    assert len(proj.tracks) == 4
    assert all(t.layer_id == 32 and math.isclose(t.width_mm, 0.5)
               for t in proj.tracks)


def test_footprint_copper_takes_the_net_of_the_pad_it_touches():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "VOUT")' + _STACKUP +
        ' (footprint "test:tab" (layer "F.Cu") (at 20 20)'
        '   (property "Reference" "Q1")'
        '   (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") (net 1 "VOUT"))'
        '   (fp_poly (pts (xy -1 -1) (xy 6 -1) (xy 6 1) (xy -1 1))'
        '     (stroke (width 0) (type solid)) (fill solid) (layer "F.Cu"))'
        '   (fp_poly (pts (xy 10 10) (xy 12 10) (xy 12 12))'
        '     (stroke (width 0) (type solid)) (fill solid) (layer "F.Cu"))))'
    )
    regions = _extract_from_string(board).shape_based_regions
    assert sorted(r.net_index for r in regions) == sorted([NO_NET, 1])


def test_polygon_points_may_contain_arcs():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (gr_poly (pts (xy 0 0) (xy 10 0)'
        '   (arc (start 10 0) (mid 15 5) (end 10 10)) (xy 0 10))'
        '   (stroke (width 0) (type solid)) (fill solid) (layer "F.Cu")))'
    )
    (r,) = _extract_from_string(board).shape_based_regions
    assert len(r.outline) > 6                       # the arc is faceted in
    assert math.isclose(max(v.pos.x for v in r.outline), 15.0, abs_tol=1e-6)


def test_custom_pad_primitives_are_copper_on_the_pad_net():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "GND")' + _STACKUP +
        ' (footprint "test:custom" (layer "F.Cu") (at 0 0)'
        '   (property "Reference" "J1")'
        '   (pad "1" smd custom (at 5 5) (size 1 1) (layers "F.Cu")'
        '     (options (clearance outline) (anchor circle))'
        '     (primitives (gr_poly (pts (xy 0 0) (xy 4 0) (xy 4 2) (xy 0 2))'
        '       (width 0) (fill yes)))'
        '     (net 1 "GND"))))'
    )
    proj = _extract_from_string(board)
    assert proj.pads[0].shape == 1                    # circular anchor
    (r,) = proj.shape_based_regions
    assert (r.layer_id, r.net_index) == (1, 1)
    xs = [v.pos.x for v in r.outline]
    assert min(xs) == 5 and max(xs) == 9              # placed at the pad


def test_through_hole_custom_pad_primitives_reach_every_copper_layer():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "GND")'
        ' (setup (stackup'
        '   (layer "F.Cu" (type "copper") (thickness 0.035))'
        '   (layer "dielectric 1" (type "core") (thickness 0.5))'
        '   (layer "In1.Cu" (type "copper") (thickness 0.035))'
        '   (layer "dielectric 2" (type "core") (thickness 0.5))'
        '   (layer "B.Cu" (type "copper") (thickness 0.035))))'
        ' (footprint "test:th" (layer "F.Cu") (at 0 0)'
        '   (property "Reference" "J2")'
        '   (pad "1" thru_hole custom (at 0 0) (size 2 2) (drill 1) (layers "*.Cu")'
        '     (primitives (gr_circle (center 0 0) (end 2 0) (width 0) (fill yes)))'
        '     (net 1 "GND"))))'
    )
    layers = sorted(r.layer_id
                    for r in _extract_from_string(board).shape_based_regions)
    assert layers == [1, 2, 32]


# --- net ties and Do not populate --------------------------------------------
def test_net_tie_footprint_is_marked_as_a_net_tie():
    from fypa.altium.annotations import NET_TIE_COMPONENT_KINDS
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (footprint "NetTie:NetTie-2_SMD_Pad0.5mm" (layer "F.Cu") (at 0 0)'
        '   (property "Reference" "NT1") (net_tie_pad_groups "1, 2"))'
        ' (footprint "R:R_0603" (layer "F.Cu") (at 5 0)'
        '   (property "Reference" "R1")))'
    )
    kinds = {c.designator: c.component_kind
             for c in _extract_from_string(board).pcb_components}
    assert kinds["NT1"] in NET_TIE_COMPONENT_KINDS
    assert kinds["R1"] == 0


def test_dnp_footprint_is_listed_not_fitted():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP +
        ' (footprint "R:R_0603" (layer "F.Cu") (at 0 0) (attr smd dnp)'
        '   (property "Reference" "R1"))'
        ' (footprint "R:R_0603" (layer "F.Cu") (at 5 0) (attr smd)'
        '   (property "Reference" "R2")))'
    )
    assert _extract_from_string(board).not_fitted_designators == ("R1",)


def test_schematic_dnp_and_per_instance_references(tmp_path):
    parts = "".join(
        f' (footprint "R:R" (layer "F.Cu") (at {i} 0) (property "Reference" "{r}"))'
        for i, r in enumerate(("R1", "R2", "R3")))
    board = '(kicad_pcb (version 20241229) (net 0 "")' + _STACKUP + parts + ')'
    # One symbol on a sheet used twice: its Reference field says R1, but its
    # instances are R1 and R2. R3 is marked Do not populate.
    sch = (
        '(kicad_sch (version 20230121)'
        ' (symbol (lib_id "R") (dnp no) (uuid "a")'
        '   (property "Reference" "R1") (property "PDN_ROLE" "SINK")'
        '   (instances (project "p" (path "/x" (reference "R1") (unit 1))'
        '                           (path "/y" (reference "R2") (unit 1)))))'
        ' (symbol (lib_id "R") (dnp yes) (uuid "b")'
        '   (property "Reference" "R3")'
        '   (instances (project "p" (path "/x" (reference "R3") (unit 1))))))'
    )
    pcb = tmp_path / "p.kicad_pcb"
    pcb.write_text(board, encoding="utf-8")
    (tmp_path / "p.kicad_sch").write_text(sch, encoding="utf-8")
    proj = extract_kicad_project(pcb)
    rows = {c.designator: c for c in proj.sch_components}
    assert set(rows) == {"R1", "R2", "R3"}
    assert rows["R2"].parameters["PDN_ROLE"] == "SINK"
    assert proj.not_fitted_designators == ("R3",)


@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_load_project_leaves_dnp_parts_out(tmp_path):
    """A SINK on a Do-not-populate part draws no current: load_project skips
    it, as it does an Altium variant's Not Fitted part, and says so."""
    from fypa.altium.loader import load_project
    sandbox = EXAMPLE_PCB.read_text(encoding="utf-8")
    board = sandbox.replace('(property "Reference" "U2")',
                            '(property "Reference" "U2") (attr smd dnp)')
    path = tmp_path / "dnp.kicad_pcb"
    path.write_text(board, encoding="utf-8")
    loaded = load_project(path)
    roles = sorted(type(d).__name__ for d in loaded.annotations.directives)
    assert roles == ["SourceSpec"]
    assert any("Do not populate" in w and "U2" in w
               for w in loaded.annotations.warnings)


# --- nets named in place (no numbered table) ----------------------------------
def test_nets_named_in_place_without_a_table():
    board = (
        '(kicad_pcb (version 20990101)' + _STACKUP +
        ' (segment (start 0 0) (end 1 0) (width 0.25) (layer "F.Cu") (net "VIN"))'
        ' (segment (start 1 0) (end 2 0) (width 0.25) (layer "F.Cu") (net "GND"))'
        ' (via (at 1 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VIN")))'
    )
    proj = _extract_from_string(board)
    names = [n.name for n in proj.nets]
    t0, t1 = proj.tracks
    assert names[t0.net_index] == "VIN" and names[t1.net_index] == "GND"
    assert proj.vias[0].net_index == t0.net_index


# --- what Auto setup reads: pin names and power symbols ----------------------
def test_pad_pin_names_and_power_symbols_reach_auto_setup(tmp_path):
    from fypa.altium.loader import load_project
    from fypa.autosetup.design import build_design_view
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "+5V") (net 2 "GND")'
        + _STACKUP +
        ' (footprint "U:LDO" (layer "F.Cu") (at 0 0) (property "Reference" "U1")'
        '   (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")'
        '     (net 1 "+5V") (pinfunction "VIN"))'
        '   (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))))'
    )
    sch = (
        '(kicad_sch (version 20230121)'
        ' (symbol (lib_id "power:+5V") (property "Reference" "#PWR01")'
        '   (property "Value" "+5V"))'
        ' (symbol (lib_id "power:PWR_FLAG") (property "Reference" "#FLG01")'
        '   (property "Value" "PWR_FLAG")))'
    )
    pcb = tmp_path / "p.kicad_pcb"
    pcb.write_text(board, encoding="utf-8")
    (tmp_path / "p.kicad_sch").write_text(sch, encoding="utf-8")
    proj = extract_kicad_project(pcb)
    assert [p.pin_name for p in proj.pads] == ["VIN", ""]
    assert proj.power_port_nets == ("+5V",)          # PWR_FLAG is not a net
    view = build_design_view(load_project(pcb), scan_schdocs=False)
    (u1,) = view.parts
    assert [p.name for p in u1.pins] == ["VIN", "2"]  # pad number as fallback
    assert "+5V" in view.power_ports


# --- via protection (IPC-4761) --------------------------------------------------
@pytest.mark.parametrize("tokens, expect", [
    ("", 0),
    ("(tenting (front yes) (back none))", 1),
    ("(tenting front back)", 2),                                 # legacy form
    ("(covering (front yes) (back yes))", 4),
    ("(plugging (front yes) (back no))", 5),
    ("(filling yes)", 9),
    ("(filling yes) (covering (front yes) (back no))", 10),
    ("(filling yes) (capping yes)", 12),
    ("(filling none) (capping none)", 0),
])
def test_via_protection_maps_to_ipc4761_type(tokens, expect):
    from fypa.kicad.extract import via_ipc4761_type
    via = sexpr.parse(f'(via (at 0 0) (size 0.6) (drill 0.3) {tokens})')
    assert via_ipc4761_type(via) == expect


# --- unused via pads ------------------------------------------------------------
_FOUR_LAYERS = (
    ' (setup (stackup'
    '   (layer "F.Cu" (type "copper") (thickness 0.035))'
    '   (layer "dielectric 1" (type "prepreg") (thickness 0.2))'
    '   (layer "In1.Cu" (type "copper") (thickness 0.035))'
    '   (layer "dielectric 2" (type "core") (thickness 1.0))'
    '   (layer "In2.Cu" (type "copper") (thickness 0.035))'
    '   (layer "dielectric 3" (type "prepreg") (thickness 0.2))'
    '   (layer "B.Cu" (type "copper") (thickness 0.035))))'
)


def _via_board(via_tokens: str) -> str:
    # The via meets a F.Cu track and a filled In2.Cu zone of its net; In1.Cu
    # and B.Cu have nothing of it.
    return (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "VIN")' + _FOUR_LAYERS +
        ' (segment (start 0 0) (end 5 0) (width 0.3) (layer "F.Cu") (net 1))'
        f' (via (at 5 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") {via_tokens}'
        '   (net 1))'
        ' (zone (net 1) (net_name "VIN") (layer "In2.Cu")'
        '   (filled_polygon (layer "In2.Cu")'
        '     (pts (xy 0 -5) (xy 10 -5) (xy 10 5) (xy 0 5)))))'
    )


@pytest.mark.parametrize("tokens, unconnected", [
    ("", ()),                                                     # keep all
    ("(remove_unused_layers yes) (keep_end_layers no)", (2, 32)),
    ("(remove_unused_layers) ", (2, 32)),                         # KiCad 6 / 7
    ("(remove_unused_layers yes) (keep_end_layers yes)", (2,)),
    ("(start_end_only yes)", (2, 3)),
    # The file lists no zone-connected layer: the In2 zone doesn't count.
    ("(remove_unused_layers yes) (zone_layer_connections)", (2, 3, 32)),
])
def test_unused_via_pads(tokens, unconnected):
    (via,) = _extract_from_string(_via_board(tokens)).vias
    assert via.unconnected_layer_ids == unconnected


# --- trapezoid and chamfered pads ----------------------------------------------
def test_trapezoid_pad_is_drawn_exactly():
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "N")' + _STACKUP +
        ' (footprint "T:T" (layer "F.Cu") (at 0 0) (property "Reference" "U1")'
        '   (pad "1" smd trapezoid (at 0 0) (size 2 1) (rect_delta 0.5 0)'
        '     (layers "F.Cu") (net 1 "N"))))'
    )
    proj = _extract_from_string(board)
    (pad,) = proj.pads
    assert (pad.width_mm, pad.height_mm) == (2.0, 0.5)    # inside the outline
    (r,) = proj.shape_based_regions
    corners = {(round(v.pos.x, 6), round(v.pos.y, 6)) for v in r.outline}
    # KiCad: the left side is size.y + delta.x tall, the right size.y - delta.x.
    assert corners == {(-1.0, -0.75), (1.0, -0.25), (1.0, 0.25), (-1.0, 0.75)}
    assert r.net_index == 1


def test_chamfered_pad_cuts_only_the_named_corner():
    from shapely.geometry import Polygon
    board = (
        '(kicad_pcb (version 20241229) (net 0 "") (net 1 "N")' + _STACKUP +
        ' (footprint "C:C" (layer "F.Cu") (at 0 0) (property "Reference" "U1")'
        '   (pad "1" smd roundrect (at 0 0) (size 2 2) (roundrect_rratio 0)'
        '     (chamfer_ratio 0.25) (chamfer top_left) (layers "F.Cu") (net 1 "N"))))'
    )
    proj = _extract_from_string(board)
    (r,) = proj.shape_based_regions
    poly = Polygon([(v.pos.x, v.pos.y) for v in r.outline])
    assert math.isclose(poly.area, 4.0 - 0.5 * 0.5 / 2.0, abs_tol=1e-9)
    # Top-left in KiCad (Y down) is top-left in FYPA (Y up): x < 0, y > 0.
    assert not poly.buffer(-1e-6).contains(
        __import__("shapely.geometry", fromlist=["Point"]).Point(-0.95, 0.95))
    (pad,) = proj.pads
    assert (pad.width_mm, pad.height_mm) == (1.0, 1.0)


# --- schematic connectivity ------------------------------------------------------
def test_pin_position_rotation_and_mirror():
    from fypa.kicad.schematic_nets import pin_position
    # Library pin 1 of a vertical resistor sits above its centre (Y up).
    assert pin_position(0, 3.81, (100, 100, 0), None) == pytest.approx((100, 96.19))
    assert pin_position(0, 3.81, (100, 100, 90), None) == pytest.approx((96.19, 100))
    assert pin_position(0, 3.81, (100, 100, 0), "x") == pytest.approx((100, 103.81))
    assert pin_position(3.81, 0, (100, 100, 0), "y") == pytest.approx((96.19, 100))


_LIB_R = (
    ' (lib_symbols (symbol "Device:R"'
    '   (symbol "R_1_1"'
    '     (pin passive line (at 0 3.81 270) (length 1.27)'
    '       (name "~") (number "1"))'
    '     (pin passive line (at 0 -3.81 90) (length 1.27)'
    '       (name "~") (number "2")))))'
)


def _r(ref_paths, x, y, rot=0, props=""):
    inst = "".join(f'(path "{p}" (reference "{r}") (unit 1))' for p, r in ref_paths)
    return (f' (symbol (lib_id "Device:R") (at {x} {y} {rot}) (unit 1)'
            f' (uuid "u-{ref_paths[0][1]}") (property "Reference" "{ref_paths[0][1]}")'
            f' {props} (instances (project "p" {inst})))')


def test_hierarchical_net_names_resolve_per_sheet_instance(tmp_path):
    """One child sheet used twice: on each use, its hierarchical label VIN is
    joined by the parent to a different rail. A PDN field naming VIN on a
    part in the child must read as that instance's board net."""
    root = (
        '(kicad_sch (version 20230121) (uuid "root")' + _LIB_R
        + _r([("/root", "R1")], 100, 100) + _r([("/root", "R3")], 160, 100)
        + ' (wire (pts (xy 100 96.19) (xy 120 96.19)))'
        + ' (wire (pts (xy 160 96.19) (xy 180 96.19)))'
        + ' (sheet (at 120 90) (size 10 10) (uuid "s1")'
          '   (property "Sheetname" "A") (property "Sheetfile" "child.kicad_sch")'
          '   (pin "VIN" input (at 120 96.19 180)))'
        + ' (sheet (at 180 90) (size 10 10) (uuid "s2")'
          '   (property "Sheetname" "B") (property "Sheetfile" "child.kicad_sch")'
          '   (pin "VIN" input (at 180 96.19 180))))'
    )
    # The child's resistor is turned 90°: pin 1 lands at (46.19, 50).
    child = (
        '(kicad_sch (version 20230121) (uuid "child")' + _LIB_R
        + _r([("/root/s1", "R2"), ("/root/s2", "R4")], 50, 50, 90,
             '(property "PDN_P_NET" "VIN")')
        + ' (hierarchical_label "VIN" (shape input) (at 46.19 50 0)))'
    )
    fps = "".join(
        f' (footprint "R:R" (layer "F.Cu") (at {i * 5} 0) (property "Reference" "{ref}")'
        f'{extra}'
        f' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {n1} "{nm}"))'
        f' (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 3 "GND")))'
        for i, (ref, n1, nm, extra) in enumerate((
            ("R1", 1, "+5V", ""), ("R2", 1, "+5V", ' (property "PDN_P_NET" "VIN")'),
            ("R3", 2, "+3V3", ""), ("R4", 2, "+3V3", ' (property "PDN_P_NET" "VIN")'))))
    board = ('(kicad_pcb (version 20241229) (net 0 "") (net 1 "+5V") (net 2 "+3V3")'
             ' (net 3 "GND")' + _STACKUP + fps + ')')
    (tmp_path / "p.kicad_sch").write_text(root, encoding="utf-8")
    (tmp_path / "child.kicad_sch").write_text(child, encoding="utf-8")
    pcb = tmp_path / "p.kicad_pcb"
    pcb.write_text(board, encoding="utf-8")
    proj = extract_kicad_project(pcb)
    nets = {c.designator: c.parameters.get("PDN_P_NET") for c in proj.pcb_components}
    assert nets["R2"] == "+5V" and nets["R4"] == "+3V3"
    rows = {c.designator: c.parameters.get("PDN_P_NET") for c in proj.sch_components}
    assert rows["R2"] == "+5V" and rows["R4"] == "+3V3"


def test_pwr_flag_does_not_merge_nets(tmp_path):
    from fypa.kicad.schematic_nets import build_schematic_nets
    flag_lib = (
        ' (lib_symbols (symbol "power:PWR_FLAG" (power)'
        '   (symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90) (length 0)'
        '     (name "pwr") (number "1")))))')
    sch = ('(kicad_sch (version 20230121) (uuid "r")' + flag_lib
           + ''.join(f' (symbol (lib_id "power:PWR_FLAG") (at {x} 0 0) (unit 1)'
                     f' (property "Reference" "#FLG0{i}") (property "Value" "PWR_FLAG"))'
                     for i, x in enumerate((0, 50))) + ')')
    path = tmp_path / "f.kicad_sch"
    path.write_text(sch, encoding="utf-8")
    assert build_schematic_nets(path, {}).global_ == {}


# --- KiCad 10 design variants ------------------------------------------------------
def _variant_board() -> str:
    sandbox = EXAMPLE_PCB.read_text(encoding="utf-8")
    sandbox = sandbox.replace(
        '(net 0 "")',
        '(variants (variant (name "Lite") (description "low power"))'
        ' (variant (name "Bare")))\n  (net 0 "")', 1)
    # U2 (the 1 A sink): Lite draws 0.25 A instead; Bare leaves it off.
    return sandbox.replace(
        '(property "Reference" "U2")',
        '(property "Reference" "U2")'
        ' (variant (name "Lite") (field (name "PDN_I") (value "0.25")))'
        ' (variant (name "Bare") (dnp yes))', 1)


@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_kicad_variants_are_read(tmp_path):
    from fypa.altium.variants import read_variants
    path = tmp_path / "v.kicad_pcb"
    path.write_text(_variant_board(), encoding="utf-8")
    pv = read_variants(path)
    assert pv.current is None and pv.names == ["Lite", "Bare"]
    assert pv.get("Lite").param_overrides == {"U2": {"PDN_I": "0.25"}}
    assert pv.get("Lite").not_fitted == frozenset()
    assert pv.get("Bare").not_fitted == frozenset({"U2"})


def test_variant_dnp_no_fits_a_base_dnp_part(tmp_path):
    from fypa.kicad.variants import read_kicad_variants
    board = (
        '(kicad_pcb (version 20260206) (variants (variant (name "Full")))'
        + _STACKUP +
        ' (footprint "R:R" (layer "F.Cu") (at 0 0) (attr smd dnp)'
        '   (property "Reference" "R1") (variant (name "Full") (dnp no)))'
        ' (footprint "R:R" (layer "F.Cu") (at 5 0) (attr smd dnp)'
        '   (property "Reference" "R2")))'
    )
    path = tmp_path / "d.kicad_pcb"
    path.write_text(board, encoding="utf-8")
    assert read_kicad_variants(path).get("Full").not_fitted == frozenset({"R2"})


@pytest.mark.skipif(not EXAMPLE_PCB.exists(), reason="example board missing")
def test_load_project_applies_a_kicad_variant(tmp_path):
    from fypa.altium.loader import load_project
    path = tmp_path / "v.kicad_pcb"
    path.write_text(_variant_board(), encoding="utf-8")

    def sink_amps(variant):
        loaded = load_project(path, variant=variant)
        sinks = [d for d in loaded.annotations.directives
                 if type(d).__name__ == "SinkSpec"]
        return [s.current for s in sinks]

    assert sink_amps(None) == [1.0]                 # base design
    assert sink_amps("Lite") == [0.25]
    assert sink_amps("Bare") == []                  # U2 not fitted


def test_via_protection_follows_the_board_default():
    """KiCad 10 writes the board-wide protection in (setup ...); a via with
    no token of its own (or ``none``) follows it."""
    board = (
        '(kicad_pcb (version 20260206)'
        ' (setup (tenting (front yes) (back yes)) (filling no) (capping no))'
        + _STACKUP +
        ' (via (at 0 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "A"))'
        ' (via (at 2 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
        '   (filling yes) (capping yes) (net "A"))'
        ' (via (at 4 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
        '   (tenting (front none) (back no)) (net "A")))'
    )
    assert [v.ipc4761_via_type for v in _extract_from_string(board).vias] == [2, 12, 1]

