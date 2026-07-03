"""Gerber arc-region handling.

Regression coverage for a bug where a filled region (``G36`` in Gerber, a
gerbonara ``ArcPoly``) whose boundary contained a circular arc would raise
``TypeError`` deep in ``_arcpoly_to_polygon`` — because gerbonara stores each
arc segment as ``(clockwise, (cx, cy))`` but the code unpacked it as a bare
``(cx, cy)``. That exception was swallowed by the per-layer guard in
``extract_gerber_project``, silently dropping the *entire copper layer*.

These tests build ``ArcPoly`` primitives directly (no ``.gbr`` fixture needed)
and check that (a) an arc region no longer raises, and (b) the arc direction
(``clockwise``) is honoured — a bulge that curves outward vs inward must give
the correct, distinct area rather than always taking the short way around.
"""
import math

import pytest

gp = pytest.importorskip("gerbonara.graphic_primitives")

from fypa.gerber.extract import _arcpoly_to_polygon  # noqa: E402


# A unit square with the right-hand edge (10,0)->(10,10) replaced by a
# semicircular arc of radius 5 centred at (10,5). Whether that semicircle
# bulges outward (right, area > square) or inward (left, area < square) is
# determined solely by the arc's ``clockwise`` flag.
_SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
_CENTRE = (10.0, 5.0)
_SQUARE_AREA = 100.0
_SEMI_AREA = math.pi * 5.0 ** 2 / 2.0  # ~39.27 mm^2


def _arcpoly(clockwise: bool):
    # arc_centers parallels outline; only the (10,0)->(10,10) segment (index 1)
    # is an arc, the rest are straight (None).
    arc_centers = [None, (clockwise, _CENTRE), None, None]
    return gp.ArcPoly(outline=list(_SQUARE), arc_centers=arc_centers)


def test_arc_region_does_not_raise():
    """The bare-`(cx, cy)` unpack used to raise TypeError on any arc segment."""
    for clockwise in (False, True):
        poly = _arcpoly_to_polygon(_arcpoly(clockwise).outline,
                                   _arcpoly(clockwise).arc_centers)
        assert not poly.is_empty
        assert poly.area > 0.0


def test_arc_region_ccw_bulges_outward():
    """clockwise=False sweeps CCW: the semicircle bulges out, area > square."""
    ap = _arcpoly(clockwise=False)
    poly = _arcpoly_to_polygon(ap.outline, ap.arc_centers)
    expected = _SQUARE_AREA + _SEMI_AREA
    assert poly.area == pytest.approx(expected, abs=0.5)
    assert poly.area > _SQUARE_AREA + 30.0  # unambiguously an outward bulge


def test_arc_region_cw_bulges_inward():
    """clockwise=True sweeps CW: the semicircle bulges in, area < square.

    This is the case the old short-way normalisation got wrong — it ignored
    the flag and always produced the outward (CCW) bulge.
    """
    ap = _arcpoly(clockwise=True)
    poly = _arcpoly_to_polygon(ap.outline, ap.arc_centers)
    expected = _SQUARE_AREA - _SEMI_AREA
    assert poly.area == pytest.approx(expected, abs=0.5)
    assert poly.area < _SQUARE_AREA - 30.0  # unambiguously an inward bulge


def test_direction_flag_changes_area():
    """The two directions must bound genuinely different areas."""
    ccw = _arcpoly_to_polygon(*_ap_args(False))
    cw = _arcpoly_to_polygon(*_ap_args(True))
    assert ccw.area - cw.area == pytest.approx(2.0 * _SEMI_AREA, abs=1.0)


def _ap_args(clockwise: bool):
    ap = _arcpoly(clockwise)
    return ap.outline, ap.arc_centers
