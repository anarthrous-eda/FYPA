"""ShapeBasedRegion arc edges sweep CCW (Arcs6-consistent), not the long way.

`_shape_based_outline_points` used the raw signed `end - start` sweep and treated
a negative value as a clockwise arc. For a wrap-around rounded corner (Altium
stores e.g. `start=360°, end=90°`, raw sweep −270°) that traced the corner the
long way around (a 270° arc) instead of the intended 90° CCW quarter. The fix
normalises the sweep `% 360` into `[0, 360)` — exactly what the standalone
`Arcs6` primitive (`_arc_polyline_points`) does. Verified on the example corpus:
every negative raw sweep present was a −270° that is really a +90° corner.
"""
from __future__ import annotations

import math

from fypa.altium.extract import Pt2D, RawRegionVertex
from fypa.altium_geometry import _shape_based_outline_points


def _angles_of_intermediate_points(center, pts, endpoints):
    """Angles (deg, 0–360) about `center` of the tessellated points that are
    NOT one of the region's declared corner vertices."""
    corners = {(round(x, 6), round(y, 6)) for x, y in endpoints}
    out = []
    for x, y in pts:
        if (round(x, 6), round(y, 6)) in corners:
            continue
        out.append(math.degrees(math.atan2(y - center.y, x - center.x)) % 360.0)
    return out


def test_wraparound_arc_sweeps_short_ccw_not_long_cw():
    """A 360°→90° arc edge is a 90° CCW quarter, so its intermediate samples
    stay in the first quadrant (angles 0–90) — not the long way through
    180°/270°."""
    center = Pt2D(0.0, 0.0)
    r = 5.0
    # Arc edge from (5,0) [angle 360≡0] to (0,5) [angle 90], centre origin.
    v_arc = RawRegionVertex(pos=Pt2D(r, 0.0), is_arc=True, center=center,
                            radius_mm=r, start_angle_deg=360.0, end_angle_deg=90.0)
    v_end = RawRegionVertex(pos=Pt2D(0.0, r))
    v_close = RawRegionVertex(pos=Pt2D(0.0, 0.0))
    pts = _shape_based_outline_points((v_arc, v_end, v_close))

    angles = _angles_of_intermediate_points(
        center, pts, [(r, 0.0), (0.0, r), (0.0, 0.0)])
    assert angles, "expected intermediate arc samples"
    # Every intermediate sample must be on the short 90° CCW arc (0°–90°),
    # i.e. first quadrant. The old long-way −270° sweep put them at 270°→90°
    # through the other three quadrants.
    assert all(0.0 <= a <= 90.0 for a in angles), (
        f"arc samples left the first quadrant (long-way sweep): {angles}")


def test_ordinary_arc_unchanged():
    """A plain positive small sweep (a normal rounded corner) is unaffected —
    the fix only touches wrap-around/negative sweeps."""
    center = Pt2D(0.0, 0.0)
    r = 4.0
    v_arc = RawRegionVertex(pos=Pt2D(r, 0.0), is_arc=True, center=center,
                            radius_mm=r, start_angle_deg=0.0, end_angle_deg=60.0)
    v_end = RawRegionVertex(pos=Pt2D(r * math.cos(math.radians(60)),
                                     r * math.sin(math.radians(60))))
    v_close = RawRegionVertex(pos=Pt2D(0.0, 0.0))
    pts = _shape_based_outline_points((v_arc, v_end, v_close))
    angles = _angles_of_intermediate_points(
        center, pts, [(v_arc.pos.x, v_arc.pos.y), (v_end.pos.x, v_end.pos.y),
                      (0.0, 0.0)])
    assert angles
    assert all(0.0 <= a <= 60.0 for a in angles), angles
