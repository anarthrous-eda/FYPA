"""Gerber/Excellon loader fixes (2026-07 review, Gerber items).

Covers:

* board-outline extraction picks the *largest* ring, not the first ArcPoly
  (a cutout / fiducial region preceding the true contour must not win);
* ``.txt`` files auto-classify as drill only on a positive Excellon ``M48``
  header sniff (readmes / BOM exports stay Ignore);
* the flash-aperture template cache and width-grouped stroke buffering in
  ``render_gerber_to_shapely`` are geometrically identical to the per-primitive
  reference path;
* ``extract_gerber_project`` hard-fails (raises) when a *copper* layer fails to
  render, rather than opening a board with copper silently missing;
* Gerber-sourced projects fold their real input files into the cache
  fingerprint, so a regenerated-in-place Gerber set invalidates the cache.
"""
import math
from pathlib import Path

import pytest

shapely = pytest.importorskip("shapely")
import shapely.geometry  # noqa: E402, F811
gp = pytest.importorskip("gerbonara.graphic_primitives")

from fypa.gerber import extract as gx  # noqa: E402


# --- board outline: largest ring wins ----------------------------------------

def _write_two_square_regions(path: Path, small_first: bool) -> None:
    """A board-outline Gerber with two filled square regions (5 mm and
    20 mm). ``small_first`` controls stream order so we can prove the largest
    is chosen regardless of position."""
    small = (
        "G36*\nX0Y0D02*\nX5000000Y0D01*\nX5000000Y5000000D01*\n"
        "X0Y5000000D01*\nX0Y0D01*\nG37*\n"
    )
    big = (
        "G36*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y20000000D01*\n"
        "X0Y20000000D01*\nX0Y0D01*\nG37*\n"
    )
    body = (small + big) if small_first else (big + small)
    path.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.100*%\nD10*\n"
        + body + "M02*\n"
    )


@pytest.mark.parametrize("small_first", [True, False])
def test_outline_picks_largest_ring(tmp_path, small_first):
    p = tmp_path / "outline.gko"
    _write_two_square_regions(p, small_first=small_first)
    pts = gx.render_outline_to_polyline(p)
    xs = [pt.x for pt in pts]
    ys = [pt.y for pt in pts]
    # The 20 mm square, never the 5 mm one, regardless of stream order.
    assert max(xs) == pytest.approx(20.0, abs=1e-3)
    assert max(ys) == pytest.approx(20.0, abs=1e-3)


# --- .txt Excellon classification --------------------------------------------

def test_txt_with_m48_header_classifies_as_drill(tmp_path):
    p = tmp_path / "nc.txt"
    p.write_text("M48\nFMAT,2\nMETRIC,TZ\nT1C0.300\n%\nT1\nX10Y10\nM30\n")
    assert gx.classify_file(p) == gx.LAYER_ID_DRILL


def test_txt_with_leading_comment_before_m48(tmp_path):
    p = tmp_path / "drill.txt"
    p.write_text("; a header comment\n\nM48\nT1C0.3\n%\nM30\n")
    assert gx.classify_file(p) == gx.LAYER_ID_DRILL


def test_plain_txt_is_ignored(tmp_path):
    p = tmp_path / "readme.txt"
    p.write_text("This board was fabricated in 2026.\nSee notes.\n")
    assert gx.classify_file(p) == gx.LAYER_ID_IGNORE


def test_missing_txt_is_ignored_not_crash(tmp_path):
    # classify_file may see a path that isn't readable; sniff must not raise.
    assert gx.classify_file(tmp_path / "nope.txt") == gx.LAYER_ID_IGNORE


# --- drill slots: G85 canned + rout-mode (round-2 findings #5 / #M1) ----------

def test_g85_slot_does_not_drop_whole_file(tmp_path):
    # A G85 canned slot used to raise inside gerbonara and drop EVERY via in
    # the file. It must now split into endpoint hits and keep the plain holes.
    p = tmp_path / "with_g85.drl"
    p.write_text(
        "M48\nMETRIC\nT1C0.300\n%\nT1\n"
        "X10.0Y10.0\n"
        "X20.0Y20.0G85X25.0Y20.0\n"
        "M30\n"
    )
    vias, npth, warns = gx._excellon_to_vias([p], [gx.LAYER_ID_TOP, gx.LAYER_ID_BOTTOM])
    # 1 plain hole + 2 slot endpoints = 3 vias; the file is NOT dropped.
    assert len(vias) == 3
    assert any("G85" in w for w in warns)


def test_rout_mode_slot_stamps_via_chain(tmp_path):
    # A rout-mode slot (G00/M15/G01/M16) is a gerbonara Line object; the old
    # code assumed .x/.y and dropped it as a "malformed record".
    p = tmp_path / "rout.drl"
    p.write_text(
        "M48\nMETRIC\nT1C0.500\n%\nG05\nT1\n"
        "G00X30.0Y30.0\nM15\nG01X35.0Y30.0\nM16\n"
        "X10.0Y10.0\n"
        "M30\n"
    )
    vias, npth, warns = gx._excellon_to_vias([p], [gx.LAYER_ID_TOP, gx.LAYER_ID_BOTTOM])
    # A 5 mm slot with a 0.5 mm tool stamps a multi-via chain (not one hole).
    assert len(vias) > 5


def test_preprocess_g85_is_format_agnostic():
    text = "X20.0Y20.0G85X25.0Y20.0\n"
    cleaned, n = gx._preprocess_excellon_g85(text)
    assert n == 1
    assert cleaned.splitlines() == ["X20.0Y20.0", "X25.0Y20.0"]
    # No G85 → untouched.
    assert gx._preprocess_excellon_g85("X1Y1\n") == ("X1Y1\n", 0)


# --- negative-image planes + .gp classifier (round-2 finding #6) --------------

_NEG_PLANE = (
    "%FSLAX46Y46*%\n%MOMM*%\n%IPNEG*%\n%ADD10C,1.0*%\nD10*\n"
    "X5000000Y5000000D03*\nX15000000Y5000000D03*\nM02*\n"
)


def test_gp_files_classify_as_inner_copper(tmp_path):
    # .GP<n> (Altium internal plane) used to fall through to Ignore.
    assert gx.classify_file(tmp_path / "plane.gp1") != gx.LAYER_ID_IGNORE
    assert gx.classify_file(tmp_path / "plane.gp2") != gx.LAYER_ID_IGNORE
    # plain inner .g<n> still works
    assert gx.classify_file(tmp_path / "inner.g1") != gx.LAYER_ID_IGNORE


def test_negative_image_plane_floods_not_tiny_discs(tmp_path):
    p = tmp_path / "plane.gp1"
    p.write_text(_NEG_PLANE)
    geom = gx.render_gerber_to_shapely(p)
    # Flood over the artwork bbox minus the two anti-pad clears — far larger
    # than the ~1.57 mm² the two 1 mm discs would occupy if rendered positive.
    assert geom.area > 10.0


def test_positive_layer_unaffected_by_negative_branch(tmp_path):
    # Same artwork without %IPNEG must still render as the two discs.
    p = tmp_path / "sig.gtl"
    p.write_text(_NEG_PLANE.replace("%IPNEG*%\n", ""))
    geom = gx.render_gerber_to_shapely(p)
    assert geom.area < 3.0


# --- aperture holes are transparent, not subtractive (finding 4.1) -----------

_SOLID_3MM = math.pi * 1.5 ** 2  # area of the underlying 3mm-diameter pad


def test_aperture_hole_does_not_erase_underlying_copper(tmp_path):
    # A 3mm solid pad, then a donut aperture (1.5mm OD, 0.8mm hole) flashed on
    # top at the same centre. Per Gerber spec §4.4.6 the aperture hole is
    # transparent to whatever sits under the flash — the 3mm pad must show
    # through the hole, leaving NO interior hole in the accumulated copper.
    p = tmp_path / "hole.gtl"
    p.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,3.000*%\n%ADD11C,1.500X0.800*%\n"
        "D10*\nX0Y0D03*\nD11*\nX0Y0D03*\nM02*\n")
    geom = gx.render_gerber_to_shapely(p)
    n_holes = sum(len(poly.interiors) for poly in gx._polygons_in(geom))
    assert n_holes == 0                       # copper shows through the hole
    assert geom.area == pytest.approx(_SOLID_3MM, abs=0.05)


def test_isolated_hole_flash_keeps_its_hole(tmp_path):
    # A donut with nothing underneath must still render as an annulus: the
    # aperture-level pad−hole is preserved, only underlying copper is spared.
    p = tmp_path / "donut.gtl"
    p.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD11C,1.500X0.800*%\n"
        "D11*\nX0Y0D03*\nM02*\n")
    geom = gx.render_gerber_to_shapely(p)
    holes = sum(len(poly.interiors) for poly in gx._polygons_in(geom))
    assert holes == 1
    # Annulus area = OD disc − hole disc.
    expected = math.pi * (0.75 ** 2 - 0.40 ** 2)
    assert geom.area == pytest.approx(expected, abs=0.02)


def test_rectangular_pad_hole_is_transparent(tmp_path):
    # Rectangular pad with a round hole (R,WxHxhole) over a plane: same rule.
    p = tmp_path / "recthole.gtl"
    p.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,4.000*%\n%ADD12R,2.000X2.000X0.800*%\n"
        "D10*\nX0Y0D03*\nD12*\nX0Y0D03*\nM02*\n")
    geom = gx.render_gerber_to_shapely(p)
    n_holes = sum(len(poly.interiors) for poly in gx._polygons_in(geom))
    assert n_holes == 0


# --- localized clear difference is identity (finding 4.2) --------------------

def test_bbox_limited_difference_matches_full_difference():
    a = shapely.geometry.box(0, 0, 1, 1)
    b = shapely.geometry.box(10, 10, 11, 11)      # far-away island
    c = shapely.geometry.box(20, 20, 22, 22)
    base = shapely.geometry.MultiPolygon([a, b, c])
    cutter = shapely.geometry.box(10.2, 10.2, 10.8, 10.8)  # only bites `b`
    fast = gx._bbox_limited_difference(base, cutter)
    slow = base.difference(cutter)
    assert fast.symmetric_difference(slow).area < 1e-9
    # `b` gained a hole; `a` and `c` are untouched pass-throughs.
    assert sum(len(p.interiors) for p in gx._polygons_in(fast)) == 1


# --- X2 drill span: drop, don't clamp, when out of range (finding 4.3) -------

def test_x2_span_out_of_range_returns_none():
    # A 2,5 buried via imported into a Top/In4/Bottom subset (ids [1,5,32])
    # references position 5 > 3 imported layers — must NOT clamp to a
    # fabricated In4→Bottom span.
    assert gx._x2_drill_span_to_layer_ids(
        ("Plated", "2", "5", "PTH", "Drill"), [1, 5, 32]) is None


def test_x2_span_in_range_maps():
    assert gx._x2_drill_span_to_layer_ids(
        ("Plated", "1", "2", "PTH", "Drill"), [1, 32]) == (1, 32)


def test_x2_span_no_numbers_defaults_full_stack():
    # A plain through-drill with no numeric span → full imported top↔bottom.
    assert gx._x2_drill_span_to_layer_ids(
        ("Plated", "PTH", "Drill"), [1, 2, 32]) == (1, 32)


# --- flash cache + width grouping equivalence --------------------------------

def test_flash_cache_circle_matches_reference():
    cache = {}
    prim = gp.Circle(3.0, -2.0, 0.4, polarity_dark=True)
    cached = gx._flash_polygon_cached(prim, cache)
    ref = gx._circle_to_polygon(3.0, -2.0, 0.4)
    assert cached.equals_exact(ref, tolerance=1e-9) or (
        cached.symmetric_difference(ref).area < 1e-9)


def test_flash_cache_rectangle_matches_reference():
    cache = {}
    prim = gp.Rectangle(1.5, 4.0, 2.0, 0.8, rotation=0.7, polarity_dark=True)
    cached = gx._flash_polygon_cached(prim, cache)
    ref = gx._rectangle_to_polygon(1.5, 4.0, 2.0, 0.8, 0.7)
    assert cached.symmetric_difference(ref).area < 1e-9


def test_flash_cache_reuses_template_across_flashes():
    cache = {}
    a = gx._flash_polygon_cached(gp.Circle(0.0, 0.0, 0.5), cache)
    b = gx._flash_polygon_cached(gp.Circle(10.0, 5.0, 0.5), cache)
    # One template built (same rounded radius), two distinct placements.
    assert len(cache) == 1
    assert a.symmetric_difference(
        gx._circle_to_polygon(0.0, 0.0, 0.5)).area < 1e-9
    assert b.symmetric_difference(
        gx._circle_to_polygon(10.0, 5.0, 0.5)).area < 1e-9


def test_flash_cache_zero_size_returns_none():
    assert gx._flash_polygon_cached(gp.Circle(0.0, 0.0, 0.0), {}) is None
    assert gx._flash_polygon_cached(
        gp.Rectangle(0.0, 0.0, 0.0, 1.0, rotation=0.0), {}) is None


def test_width_grouped_buffer_matches_individual_union():
    """The core equivalence the width-grouping optimisation relies on:
    buffering a MultiLineString equals unioning each line's own buffer."""
    a = shapely.geometry.LineString([(0, 0), (10, 0)])
    b = shapely.geometry.LineString([(0, 5), (10, 5)])
    w = 0.3
    grouped = shapely.geometry.MultiLineString([a, b]).buffer(
        w / 2.0, cap_style="round", join_style="round")
    individual = a.buffer(w / 2.0, cap_style="round", join_style="round").union(
        b.buffer(w / 2.0, cap_style="round", join_style="round"))
    assert grouped.symmetric_difference(individual).area < 1e-9


# --- copper-layer render failure is fatal ------------------------------------

def test_missing_copper_layer_aborts_import(tmp_path):
    stack = [gx.GerberStackupLayer(
        layer_id=gx.LAYER_ID_TOP, name="Top",
        copper_thickness_mm=0.035, dielectric_thickness_mm=0.0)]
    with pytest.raises(RuntimeError, match="copper layer"):
        gx.extract_gerber_project(
            copper_files={gx.LAYER_ID_TOP: tmp_path / "does_not_exist.gtl"},
            drill_files=[],
            outline_file=None,
            stackup=stack,
            pseudo_prjpcb_path=tmp_path / "x.fypa-gerber",
        )


def test_good_copper_layer_imports(tmp_path):
    # A trivially-valid single-flash copper layer should import cleanly.
    p = tmp_path / "top.gtl"
    p.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,1.000*%\nD10*\nX0Y0D03*\nM02*\n")
    stack = [gx.GerberStackupLayer(
        layer_id=gx.LAYER_ID_TOP, name="Top",
        copper_thickness_mm=0.035, dielectric_thickness_mm=0.0)]
    proj, warns = gx.extract_gerber_project(
        copper_files={gx.LAYER_ID_TOP: p},
        drill_files=[],
        outline_file=None,
        stackup=stack,
        pseudo_prjpcb_path=tmp_path / "x.fypa-gerber",
    )
    assert len(proj.shape_based_regions) >= 1


# --- Gerber cache fingerprint ------------------------------------------------

def test_gerber_pseudo_path_fingerprints_input_files(tmp_path):
    from fypa import cli
    (tmp_path / "top.gtl").write_text("copper top\n")
    (tmp_path / "bot.gbl").write_text("copper bot\n")
    (tmp_path / "drill.drl").write_text("M48\n")
    (tmp_path / "readme.md").write_text("not a gerber\n")
    pseudo = tmp_path / f"{tmp_path.name}.fypa-gerber"

    fp = cli._project_file_fingerprints(pseudo)
    names = {Path(k).name for k in fp}
    assert names == {"top.gtl", "bot.gbl", "drill.drl"}  # readme.md excluded


def test_gerber_fingerprint_changes_on_regenerate(tmp_path):
    from fypa import cli
    f = tmp_path / "top.gtl"
    f.write_text("original copper\n")
    pseudo = tmp_path / f"{tmp_path.name}.fypa-gerber"
    before = cli._project_file_fingerprints(pseudo)
    # Regenerate in place with different content (→ different size).
    f.write_text("regenerated copper artwork with more data\n")
    after = cli._project_file_fingerprints(pseudo)
    assert before != after
