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
from pathlib import Path

import pytest

shapely = pytest.importorskip("shapely")
import shapely.geometry  # noqa: E402
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
