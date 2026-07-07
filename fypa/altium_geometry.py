"""Altium copper geometry builder for FYPA.

Consumes an :class:`fypa.altium.extract.ExtractedProject` and produces one
:class:`GeometryLayer` per enabled copper layer. Each layer carries:

* a Shapely ``MultiPolygon`` of all filled copper on that layer
  (tracks ∪ arcs ∪ regions ∪ pads ∪ via barrels), with drill holes subtracted;
* a ``conductance`` in Siemens computed as
  ``copper_thickness_mm × COPPER_CONDUCTIVITY_S_PER_MM``, matching padne's
  surface-conductivity convention so the geometry can be handed straight to a
  2D Laplace solver.

Units: millimetres everywhere. Conductivity is therefore in S/mm (not S/m).

Geometry rules
--------------
* **Tracks** with ``is_keepout`` or ``is_polygon_outline`` are skipped; the
  remaining tracks are buffered LineStrings of half-width with round caps.
* **Arcs** are discretised to a polyline whose chord error is bounded by
  :data:`ARC_CHORD_TOLERANCE_MM`, then buffered with round caps.
* **Regions** (Altium ``Regions6``) are included when ``kind == 0`` (copper),
  not a polygon outline, not a keepout, and not a board cutout.
* **Pads** on layer id ``MULTI_LAYER_PAD_LAYER_ID`` (74) are through-hole and
  appear on every copper layer; SMT pads appear only on their assigned layer.
  Drill holes are subtracted from the pad shape.
* **Vias** span the inclusive range ``[layer_start, layer_end]`` along the
  enabled copper stack; the barrel is the outer disc minus the drill hole.
* **Plane layers** (``stackup.is_plane``) are flooded with their net's copper
  over the board outline (see :func:`_plane_sheet_polygon`). Anti-pad and
  thermal-relief detail around foreign-net through features is a deferred
  fidelity refinement; it does not affect per-net connectivity.
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
import time
from dataclasses import dataclass

import numpy as np
import shapely
import shapely.affinity
import shapely.geometry
import shapely.ops

from fypa import _clipper_fuse

from fypa.altium.extract import (
    ExtractedProject,
    NO_NET,
    NO_POLYGON,
    RawArc,
    RawFill,
    RawPad,
    RawRegion,
    RawRegionVertex,
    RawShapeBasedRegion,
    RawTrack,
    RawVia,
)


log = logging.getLogger(__name__)


# Padne's convention: conductivity stored in S/mm so that
#   surface_conductance [S] = thickness [mm] × conductivity [S/mm]
COPPER_CONDUCTIVITY_S_PER_MM: float = 5.95e4

# Altium layer-id sentinels.
MULTI_LAYER_PAD_LAYER_ID: int = 74

# Pad shape codes (altium_monkey.altium_pcb_enums.PadShape).
PAD_SHAPE_CIRCLE: int = 1
PAD_SHAPE_RECTANGLE: int = 2
PAD_SHAPE_OCTAGONAL: int = 3
PAD_SHAPE_ROUNDED_RECTANGLE: int = 4
PAD_SHAPE_CUSTOM: int = 10

# Discretisation tolerances (mm).
ARC_CHORD_TOLERANCE_MM: float = 0.025  # ≈ 1 mil — keeps mesh-quality artifacts below copper width
CIRCLE_RESOLUTION: int = 32             # segments per full circle for round buffers
# ``Geometry.buffer(d)`` defaults to quad_segs=16 (per quarter circle), whereas
# the top-level ``shapely.buffer(array, d)`` defaults to 8. Pin 16 when batching
# so array-buffered rings are byte-for-byte identical to the per-geometry
# ``.buffer()`` calls they replace.
_DEFAULT_BUFFER_QUAD_SEGS: int = 16


@dataclass(frozen=True)
class GeometryLayer:
    """One enabled copper layer ready for FEM meshing.

    A GeometryLayer can represent either:

    * **a full physical layer with all nets merged** (``net_index == NO_NET``) —
      legacy single-union geometry, used for quicklook PNGs and as a fallback
      shape lookup; or
    * **one net's copper on one physical layer** (``net_index >= 0``) — used by
      the FEM-facing pipeline so each net is its own electrical conductor and
      cross-net unioning artefacts cannot bleed voltage between rails.

    Conductance is purely a property of the physical layer (thickness ×
    conductivity), independent of which net's copper it carries.
    """

    layer_id: int
    name: str
    shape: shapely.geometry.MultiPolygon
    conductance: float                  # Siemens
    is_plane: bool
    plane_net_index: int                # NO_NET unless ``is_plane``
    net_index: int = NO_NET             # NO_NET = all nets unioned together (legacy)


# --- per-primitive shape helpers ---------------------------------------------

def _track_polygon(t: RawTrack) -> shapely.geometry.Polygon:
    # A zero-length track (a == b) buffers to an EMPTY polygon via LineString,
    # but Altium renders it as a filled copper dot of the track width — and
    # such dots are used to hand-stitch features together, so dropping them
    # loses a connection. Buffer the endpoint as a disc instead.
    if t.a.x == t.b.x and t.a.y == t.b.y:
        return shapely.geometry.Point(t.a.x, t.a.y).buffer(
            t.width_mm / 2.0, resolution=CIRCLE_RESOLUTION // 4)
    line = shapely.geometry.LineString([(t.a.x, t.a.y), (t.b.x, t.b.y)])
    # cap_style=1 → round, join_style=1 → round (Shapely 2.x constants).
    return line.buffer(t.width_mm / 2.0, cap_style=1, join_style=1,
                       resolution=CIRCLE_RESOLUTION // 4)


def _arc_polyline_points(a: RawArc) -> list[tuple[float, float]]:
    """Discretise an arc to a chord-bounded polyline."""
    sweep = (a.end_angle_deg - a.start_angle_deg) % 360.0
    if sweep == 0.0:
        # Altium repour circles arrive as a 360°-multiple sweep and are meant
        # to be a full disc — but a genuinely degenerate standalone arc
        # (collapsed edit, buggy exporter) also lands here and becomes a full
        # annulus of copper that can short across a clearance. We can't tell
        # the two apart from sweep alone, so keep the (usually-correct) full-
        # circle promotion but log it so the annulus case is diagnosable.
        log.debug(
            "Arc at (%.3f, %.3f) r=%.3f has zero net sweep — promoting to a "
            "full circle (repour convention); verify if unexpected.",
            a.center.x, a.center.y, a.radius_mm,
        )
        sweep = 360.0  # full circle convention used by Altium repour output
    # Maximum sub-angle for a given chord tolerance: 2·acos(1 - tol/r)
    if a.radius_mm <= 0.0:
        return [(a.center.x, a.center.y)]
    cos_arg = max(-1.0, 1.0 - ARC_CHORD_TOLERANCE_MM / a.radius_mm)
    max_step_rad = 2.0 * math.acos(cos_arg)
    if max_step_rad <= 0.0:
        n = max(8, int(round(sweep / 1.0)))
    else:
        n = max(8, int(math.ceil(math.radians(sweep) / max_step_rad)))
    angles = np.linspace(math.radians(a.start_angle_deg),
                         math.radians(a.start_angle_deg + sweep), n + 1)
    # Vectorised trig over the whole angle array — iterating numpy scalars with
    # math.cos/sin in a list comp is ~10× slower for the same float64 values.
    xs = a.center.x + a.radius_mm * np.cos(angles)
    ys = a.center.y + a.radius_mm * np.sin(angles)
    return list(zip(xs.tolist(), ys.tolist()))


def _arc_polygon(a: RawArc) -> shapely.geometry.Polygon:
    pts = _arc_polyline_points(a)
    if len(pts) < 2:
        return shapely.geometry.Polygon()
    line = shapely.geometry.LineString(pts)
    return line.buffer(a.width_mm / 2.0, cap_style=1, join_style=1,
                       resolution=CIRCLE_RESOLUTION // 4)


def _shape_based_polygon_indices(proj: ExtractedProject) -> frozenset[int]:
    """Polygon indices already covered by ``ShapeBasedRegions6`` for this
    project. Used to dedupe legacy ``Regions6`` records that modern Altium
    dual-stores in both streams — see :class:`RawRegion.polygon_index`.

    Recomputed per call; ``ExtractedProject`` is a frozen+slots dataclass
    so attribute caching is awkward, and the set is small (one entry per
    polygon, typically < 100)."""
    return frozenset(
        r.polygon_index for r in proj.shape_based_regions
        if r.polygon_index != NO_POLYGON
    )


def _skip_region_as_duplicate(r: RawRegion,
                              sbr_polygon_indices: frozenset[int]) -> bool:
    """Whether ``r`` is the legacy ``Regions6`` copy of a polygon-pour
    region that's also stored — with truer geometry — in
    ``ShapeBasedRegions6``. Drop the legacy copy to avoid double-counting
    copper and to skip the degenerate zero-width sliver records that
    modern Altium tends to emit there."""
    return (r.polygon_index != NO_POLYGON
            and r.polygon_index in sbr_polygon_indices)


def _region_polygon(
    r: RawRegion,
) -> shapely.geometry.base.BaseGeometry:
    """Build a Shapely (Multi)Polygon from a Regions6 record.

    Same sanitisation flow as :func:`_shape_based_region_polygon` — an
    invalid outline goes through :func:`shapely.make_valid` and only the
    polygonal fragments are kept. Returns an empty polygon for fully
    degenerate input so the caller can drop it. ``make_valid`` is more
    topologically faithful than ``buffer(0)``; the latter could leave
    near-zero-area slivers that the FEM mesher rejects.
    """
    outline = [(p.x, p.y) for p in r.outline]
    holes = [[(p.x, p.y) for p in ring] for ring in r.holes]
    if _shoelace_area(outline) <= 0.0:
        # Zero-area input — Altium emits these as vestigial slivers when a
        # polygon repour encounters near-zero-width clearances. They aren't
        # copper. Skip silently at DEBUG; otherwise every repour pollutes
        # the log with a dozen identical warnings.
        # Guard the eager arg: _summarise_region_input does an O(V) shoelace +
        # bbox + string preview, wasted when DEBUG is off (the common case).
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Region on layer %d dropped (zero raw area). %s",
                r.layer_id, _summarise_region_input(outline, holes),
            )
        return shapely.geometry.Polygon()
    try:
        poly: shapely.geometry.base.BaseGeometry = shapely.geometry.Polygon(
            outline, holes,
        )
    except Exception as e:
        log.warning(
            "Region on layer %d skipped: Polygon ctor failed (%s). %s",
            r.layer_id, e, _summarise_region_input(outline, holes),
        )
        return shapely.geometry.Polygon()
    if not poly.is_valid:
        try:
            poly = shapely.make_valid(poly)
        except Exception as e:
            log.warning(
                "Region on layer %d skipped: make_valid failed (%s). %s",
                r.layer_id, e,
                _summarise_region_input(outline, holes),
            )
            return shapely.geometry.Polygon()
    poly = _keep_polygonal(poly)
    if poly.is_empty or poly.area <= 0.0:
        log.warning(
            "Region on layer %d dropped: degenerate after sanitisation. "
            "net_index=%s kind=%s polygon_outline=%s keepout=%s cutout=%s %s",
            r.layer_id, r.net_index, r.kind, r.is_polygon_outline,
            r.is_keepout, r.is_board_cutout,
            _summarise_region_input(outline, holes),
        )
        return shapely.geometry.Polygon()
    return poly


def _shoelace_area(outline: list[tuple[float, float]]) -> float:
    """Absolute polygon area from the shoelace formula. Returns 0.0 for
    fewer than 3 vertices or for outlines that collapse to a line.
    Coordinate units are honoured (mm² in / mm² out)."""
    n = len(outline)
    if n < 3:
        return 0.0
    signed_2a = 0.0
    for i in range(n):
        x1, y1 = outline[i]
        x2, y2 = outline[(i + 1) % n]
        signed_2a += x1 * y2 - x2 * y1
    return abs(signed_2a) * 0.5


def _summarise_region_input(
    outline: list[tuple[float, float]],
    holes: list[list[tuple[float, float]]] | None = None,
    arc_count: int | None = None,
) -> str:
    """One-line summary of a region's raw outline for the drop warning.

    Includes vertex count, raw signed area (shoelace, ignores holes — tells
    us if the polygon is collinear or self-cancelling), bbox dimensions,
    a handful of leading vertices, and hole / arc counts when relevant.
    Used by :func:`_region_polygon` and :func:`_shape_based_region_polygon`
    so a drop warning carries enough context to identify the failure mode
    without re-running with the debugger attached."""
    n = len(outline)
    if n == 0:
        return "outline: empty"
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    bbox_w = max(xs) - min(xs)
    bbox_h = max(ys) - min(ys)
    # Shoelace: signed area * 2. Tells us collinear (~0) vs filled.
    signed_2a = 0.0
    for i in range(n):
        x1, y1 = outline[i]
        x2, y2 = outline[(i + 1) % n]
        signed_2a += x1 * y2 - x2 * y1
    raw_area = abs(signed_2a) * 0.5
    # First few vertices (rounded for readability).
    preview = ", ".join(
        f"({x:.4f},{y:.4f})" for x, y in outline[:min(6, n)]
    )
    suffix = "" if n <= 6 else f", … (+{n - 6} more)"
    parts = [
        f"verts={n}",
        f"area={raw_area:.6g} mm²",
        f"bbox={bbox_w:.4f}×{bbox_h:.4f} mm",
    ]
    if holes:
        parts.append(f"holes={len(holes)}({[len(h) for h in holes]})")
    if arc_count is not None:
        parts.append(f"arcs={arc_count}")
    parts.append(f"head=[{preview}{suffix}]")
    return " ".join(parts)


def _shape_based_outline_points(
    vertices: tuple[RawRegionVertex, ...],
) -> list[tuple[float, float]]:
    """Sample a shape-based region's closed outline into a polyline.

    Each edge runs from ``vertices[i]`` to ``vertices[(i + 1) % n]``. For
    straight edges only the start vertex's position is emitted (the next
    iteration emits the end). For arc edges, intermediate samples are
    inserted between the two endpoints; the number of samples is chosen
    so the chord error stays below :data:`ARC_CHORD_TOLERANCE_MM` — same
    bound the arc primitive uses, so the meshed geometry matches.

    Altium stores the arc's start/end angles in degrees, measured CCW from
    +x about ``vertex.center``, and — like the standalone ``Arcs6`` primitive
    (:func:`_arc_polyline_points`) — the sweep is always CCW: normalise
    ``(end - start) % 360`` into ``[0, 360)``. The previous code took the raw
    signed difference and treated a negative value as a CW sweep, which traced a
    wrap-around corner the long way (e.g. a ``360° → 90°`` rounded corner became
    a 270° arc instead of the intended 90°). Verified on the example corpus:
    every negative raw sweep present was a ``-270°`` that is really a ``+90°``
    CCW corner, and all non-wrapping arcs are already positive, so this only
    corrects the wrap-around case.
    """
    n = len(vertices)
    if n < 3:
        return [(v.pos.x, v.pos.y) for v in vertices]
    pts: list[tuple[float, float]] = []
    for cur in vertices:
        pts.append((cur.pos.x, cur.pos.y))
        if not cur.is_arc or cur.radius_mm <= 0.0:
            continue
        raw_sweep = cur.end_angle_deg - cur.start_angle_deg
        if raw_sweep == 0.0:
            continue  # start == end: degenerate zero-length arc edge
        sweep_deg = raw_sweep % 360.0
        if sweep_deg == 0.0:
            sweep_deg = 360.0  # raw was a non-zero multiple of 360 → full circle
        cos_arg = max(-1.0, 1.0 - ARC_CHORD_TOLERANCE_MM / cur.radius_mm)
        max_step_rad = 2.0 * math.acos(cos_arg)
        sweep_rad = math.radians(sweep_deg)
        if max_step_rad <= 0.0:
            steps = max(8, int(math.ceil(abs(sweep_deg))))
        else:
            steps = max(2, int(math.ceil(abs(sweep_rad) / max_step_rad)))
        # Emit ``steps - 1`` intermediate points; the arc's endpoint is
        # contributed as the next vertex's start position so we don't
        # duplicate it here. Vectorised over k — same float64 values as the
        # per-point math.cos/sin loop, ~10× faster on arc-dense pours.
        ks = np.arange(1, steps)
        rads = np.radians(cur.start_angle_deg + sweep_deg * (ks / steps))
        xs = cur.center.x + cur.radius_mm * np.cos(rads)
        ys = cur.center.y + cur.radius_mm * np.sin(rads)
        pts.extend(zip(xs.tolist(), ys.tolist()))
    return pts


def _fill_polygon(f: RawFill) -> shapely.geometry.Polygon | None:
    """Rectangular copper fill, rotated about its geometric centre."""
    x_lo, x_hi = sorted((f.x1_mm, f.x2_mm))
    y_lo, y_hi = sorted((f.y1_mm, f.y2_mm))
    if x_hi - x_lo <= 0.0 or y_hi - y_lo <= 0.0:
        return None
    box = shapely.geometry.box(x_lo, y_lo, x_hi, y_hi)
    if f.rotation_deg:
        cx = 0.5 * (x_lo + x_hi)
        cy = 0.5 * (y_lo + y_hi)
        box = shapely.affinity.rotate(box, f.rotation_deg, origin=(cx, cy))
    return box


def _shape_based_region_polygon(
    r: RawShapeBasedRegion,
) -> shapely.geometry.base.BaseGeometry:
    """Build a Shapely (Multi)Polygon from a shape-based region.

    Discretises any arc edges in the outline. Holes are polylines (Altium's
    ``ShapeBasedRegions6`` stream stores them as straight segments).

    The result is sanitised before returning: an invalid outline (e.g.
    self-intersecting from an off-convention arc sweep, or a degenerate
    sliver) gets run through :func:`shapely.make_valid`, and only the
    polygonal fragments are kept. If nothing polygonal survives, an empty
    polygon is returned so the caller's ``geom.is_empty`` check drops it
    cleanly. Without this guard, a single bad shape-based region can
    propagate through ``unary_union`` and reach the FEM mesher as a
    near-zero-area sliver that ``triangle.triangulate`` rejects with
    "invalid geometry on input".
    """
    outline = _shape_based_outline_points(r.outline)
    holes = [[(p.x, p.y) for p in ring] for ring in r.holes]
    arc_count = sum(1 for v in r.outline if v.is_arc)
    if _shoelace_area(outline) <= 0.0:
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Shape-based region on layer %d dropped (zero raw area). %s",
                r.layer_id, _summarise_region_input(outline, holes, arc_count),
            )
        return shapely.geometry.Polygon()
    try:
        poly: shapely.geometry.base.BaseGeometry = shapely.geometry.Polygon(
            outline, holes,
        )
    except Exception as e:
        log.warning(
            "Shape-based region on layer %d skipped: Polygon ctor failed "
            "(%s). %s %s",
            r.layer_id, e,
            _summarise_region_input(outline, holes, arc_count),
            _summarise_shape_based_vertices(r.outline),
        )
        return shapely.geometry.Polygon()
    if not poly.is_valid:
        try:
            poly = shapely.make_valid(poly)
        except Exception as e:
            log.warning(
                "Shape-based region on layer %d skipped: make_valid failed "
                "(%s). %s %s",
                r.layer_id, e,
                _summarise_region_input(outline, holes, arc_count),
                _summarise_shape_based_vertices(r.outline),
            )
            return shapely.geometry.Polygon()
    poly = _keep_polygonal(poly)
    if poly.is_empty or poly.area <= 0.0:
        log.warning(
            "Shape-based region on layer %d dropped: degenerate after "
            "sanitisation. net_index=%s kind=%s polygon_outline=%s "
            "keepout=%s cutout=%s %s %s",
            r.layer_id, r.net_index, r.kind, r.is_polygon_outline,
            r.is_keepout, r.is_board_cutout,
            _summarise_region_input(outline, holes, arc_count),
            _summarise_shape_based_vertices(r.outline),
        )
        return shapely.geometry.Polygon()
    return poly


def _summarise_shape_based_vertices(
    vertices: tuple[RawRegionVertex, ...],
) -> str:
    """Per-vertex summary for the first few entries of a shape-based
    region's raw outline. Surfaces arc parameters (center, radius,
    start/end angles, computed sweep) so a degenerate-arc bug is obvious
    from the log without re-running with a debugger attached."""
    if not vertices:
        return "raw_vertices: empty"
    rows: list[str] = []
    for i, v in enumerate(vertices[:6]):
        if v.is_arc:
            sweep = v.end_angle_deg - v.start_angle_deg
            rows.append(
                f"  v{i}: pos=({v.pos.x:.4f},{v.pos.y:.4f}) ARC "
                f"c=({v.center.x:.4f},{v.center.y:.4f}) r={v.radius_mm:.4f} "
                f"start={v.start_angle_deg:.3f}° end={v.end_angle_deg:.3f}° "
                f"sweep={sweep:.3f}°"
            )
        else:
            rows.append(
                f"  v{i}: pos=({v.pos.x:.4f},{v.pos.y:.4f})"
            )
    if len(vertices) > 6:
        rows.append(f"  … (+{len(vertices) - 6} more)")
    return "raw_vertices:\n" + "\n".join(rows)


def _keep_polygonal(
    geom: shapely.geometry.base.BaseGeometry,
) -> shapely.geometry.base.BaseGeometry:
    """Drop non-areal fragments from ``geom``.

    ``make_valid`` can return a ``GeometryCollection`` mixing Polygons with
    LineStrings or Points (the latter representing collapsed slivers). The
    FEM mesher wants pure polygonal input, so we keep only Polygon /
    MultiPolygon members and union them back together. Returns an empty
    Polygon if nothing polygonal remains.
    """
    if geom.is_empty:
        return geom
    gt = geom.geom_type
    if gt in ("Polygon", "MultiPolygon"):
        return geom
    if gt == "GeometryCollection":
        polys = [g for g in geom.geoms
                 if g.geom_type in ("Polygon", "MultiPolygon")
                 and not g.is_empty]
        if not polys:
            return shapely.geometry.Polygon()
        if len(polys) == 1:
            return polys[0]
        return shapely.ops.unary_union(polys)
    # LineString, Point, MultiLineString, MultiPoint — all non-areal.
    return shapely.geometry.Polygon()


def _pad_layer_geom(p: RawPad, layer_id: int | None
                    ) -> tuple[int, float, float, int]:
    """Resolve ``(shape, width_mm, height_mm, corner_radius_pct)`` for a pad on
    the requested copper layer. ``layer_id is None`` (or a pad with no per-layer
    variations) yields the top-level values, so the simple-pad path is
    unchanged. Pads with an Altium top-mid-bot / full-stack pad stack return the
    layer-specific entry when one was recorded for ``layer_id``."""
    variations = getattr(p, "layer_variations", ())
    if layer_id is not None and variations:
        for (lid, shape, w, h, cr) in variations:
            if lid == layer_id:
                return shape, w, h, cr
    return p.shape, p.width_mm, p.height_mm, getattr(p, 'corner_radius_pct', 0)


def _pad_outer_shape(p: RawPad, layer_id: int | None = None
                     ) -> shapely.geometry.Polygon | None:
    """Return the pad's outer copper outline (no drill subtraction yet).

    ``layer_id`` selects the copper layer so pads with a per-layer pad stack
    (different shape / size on different layers) render correctly; ``None``
    uses the top-level (top-layer) values."""
    cx, cy = p.center.x, p.center.y
    shape, w, h, corner_pct = _pad_layer_geom(p, layer_id)
    if shape == PAD_SHAPE_CIRCLE:
        if w <= 0 or h <= 0:
            return None
        # Altium's "Round" pad is a circle only when width == height. With
        # unequal dimensions it is an oblong / obround (a stadium: a rectangle
        # with fully-rounded semicircular ends of radius min(w, h) / 2). Build
        # it as the core box buffered by that radius — when w == h the box
        # collapses to a point and the buffer yields the plain circle.
        r = min(w, h) / 2.0
        s = shapely.geometry.box(cx - w / 2.0 + r, cy - h / 2.0 + r,
                                 cx + w / 2.0 - r, cy + h / 2.0 - r).buffer(
            r, resolution=CIRCLE_RESOLUTION // 4)
        if p.rotation_deg:
            s = shapely.affinity.rotate(s, p.rotation_deg, origin=(cx, cy))
        return s
    if shape == PAD_SHAPE_RECTANGLE:
        if w <= 0 or h <= 0:
            return None
        s = shapely.geometry.box(cx - w / 2.0, cy - h / 2.0,
                                 cx + w / 2.0, cy + h / 2.0)
        if p.rotation_deg:
            s = shapely.affinity.rotate(s, p.rotation_deg, origin=(cx, cy))
        return s
    if shape == PAD_SHAPE_OCTAGONAL:
        if w <= 0 or h <= 0:
            return None
        # Regular octagon inscribed in the w×h box.
        ax, ay = w / 2.0, h / 2.0
        c = min(ax, ay) * (math.sqrt(2.0) - 1.0)  # chamfer length
        pts = [
            (cx - ax + c, cy - ay), (cx + ax - c, cy - ay),
            (cx + ax,     cy - ay + c), (cx + ax,     cy + ay - c),
            (cx + ax - c, cy + ay), (cx - ax + c, cy + ay),
            (cx - ax,     cy + ay - c), (cx - ax,     cy - ay + c),
        ]
        s = shapely.geometry.Polygon(pts)
        if p.rotation_deg:
            s = shapely.affinity.rotate(s, p.rotation_deg, origin=(cx, cy))
        return s
    if shape == PAD_SHAPE_ROUNDED_RECTANGLE:
        if w <= 0 or h <= 0:
            return None
        pct = corner_pct
        if pct > 0:
            r = (pct / 100.0) * min(w, h) / 2.0
        else:
            r = 0.2 * min(w, h)
        r = min(r, min(w, h) / 2.0)
        s = shapely.geometry.box(cx - w / 2.0 + r, cy - h / 2.0 + r,
                                 cx + w / 2.0 - r, cy + h / 2.0 - r).buffer(
            r, resolution=CIRCLE_RESOLUTION // 4)
        if p.rotation_deg:
            s = shapely.affinity.rotate(s, p.rotation_deg, origin=(cx, cy))
        return s
    # PAD_SHAPE_CUSTOM (and any other unhandled code) has no primitive geometry
    # on RawPad, so we can only approximate it by its w×h bounding rectangle.
    # For a custom-shape pad (RF stub, thermal pad with cutouts) this overstates
    # copper by bbox-minus-shape and can bridge to adjacent same-net features /
    # understate resistance right where a directive pin lands. Name the pad so
    # the approximation is traceable. (A faithful fix needs altium_monkey to
    # expose the pad's constituent Regions6 children.)
    designator = getattr(p, "designator", "") or "?"
    if shape == PAD_SHAPE_CUSTOM:
        log.warning(
            "Custom-shape pad %r at (%.3f, %.3f) approximated by its %.3f×%.3f "
            "mm bounding rectangle — copper may be overstated.",
            designator, cx, cy, w, h,
        )
    else:
        log.warning(
            "Unhandled pad shape code %d (pad %r) at (%.3f, %.3f) — falling "
            "back to bounding rectangle", shape, designator, cx, cy,
        )
    if w <= 0 or h <= 0:
        return None
    s = shapely.geometry.box(cx - w / 2.0, cy - h / 2.0,
                             cx + w / 2.0, cy + h / 2.0)
    if p.rotation_deg:
        s = shapely.affinity.rotate(s, p.rotation_deg, origin=(cx, cy))
    return s


def _pad_polygon(p: RawPad, layer_id: int | None = None
                 ) -> shapely.geometry.Polygon | None:
    # NOTE: We deliberately do NOT subtract the drill hole. A plated through-
    # hole pad has copper continuity across the hole's footprint (the barrel
    # plating fills the hole's cross-section for in-plane current flow). For
    # 2.5D PDN-FEM purposes the pad is one solid copper disc on each layer
    # it touches; the hole becomes a separate inter-layer coupling element
    # injected by fypa.altium.loader.build_problem(). Subtracting the hole would
    # leave a no-copper point at the pad centre — making the FEM unable to
    # attach via-coupling Connections at the via location.
    #
    # ``layer_id`` selects the copper layer so a pad with a per-layer pad stack
    # contributes its layer-specific shape; ``None`` uses the top-level values.
    return _pad_outer_shape(p, layer_id)


def _via_polygon(v: RawVia) -> shapely.geometry.Polygon | None:
    if v.diameter_mm <= 0:
        return None
    # Same reasoning as `_pad_polygon`: do not subtract the drill hole. The
    # plated barrel makes the via a solid copper disc on each layer for the
    # purposes of in-plane FEM; the inter-layer coupling is added separately
    # as a small Resistor network at the via centre.
    return shapely.geometry.Point(v.center.x, v.center.y).buffer(
        v.diameter_mm / 2.0, resolution=CIRCLE_RESOLUTION // 4)


# --- layer assembly -----------------------------------------------------------

def _pad_on_layer(p: RawPad, layer_id: int) -> bool:
    if p.is_through_hole or p.layer_id == MULTI_LAYER_PAD_LAYER_ID:
        return True
    return p.layer_id == layer_id


def _via_on_layer(v: RawVia, layer_id: int, enabled: list[int],
                  pos: dict[int, int] | None = None) -> bool:
    """Whether via ``v``'s barrel spans ``layer_id`` in the enabled stack.

    ``pos`` is an optional ``{layer_id: position}`` map; pass it (built once by
    the caller) to avoid three O(L) ``enabled.index(...)`` scans per via — this
    function is called per via × per layer, so the scans were O(vias × L²)."""
    if pos is None:
        pos = {lid: i for i, lid in enumerate(enabled)}
    i_layer = pos.get(layer_id)
    i_a = pos.get(v.layer_start)
    i_b = pos.get(v.layer_end)
    if i_layer is None or i_a is None or i_b is None:
        return False
    lo, hi = (i_a, i_b) if i_a <= i_b else (i_b, i_a)
    return lo <= i_layer <= hi


def _ensure_multipolygon(geom) -> shapely.geometry.MultiPolygon:
    if geom.is_empty:
        return shapely.geometry.MultiPolygon()
    if geom.geom_type == "Polygon":
        return shapely.geometry.MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    # GeometryCollection — keep only the polygonal pieces.
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    return shapely.geometry.MultiPolygon(polys)


# Cache of {net_name: first_index} for the most recent project. Bounded to one
# entry (cleared on a new project), keyed by id() with an identity re-check so a
# recycled id can never return a stale map. ExtractedProject is frozen+slots so
# the cache can't live on the object itself.
_net_name_index_cache: dict[int, tuple[ExtractedProject, dict[str, int]]] = {}


def _net_index_by_name(proj: ExtractedProject, name: str | None) -> int:
    """Index of the first net named ``name`` (case-sensitive, matching the old
    linear scan), or ``NO_NET``. Cached per project so repeated lookups across
    plane / sheet builds are O(1) instead of O(nets) each."""
    if not name:
        return NO_NET
    entry = _net_name_index_cache.get(id(proj))
    if entry is None or entry[0] is not proj:
        name_to_index: dict[str, int] = {}
        for i, net in enumerate(proj.nets):
            name_to_index.setdefault(net.name, i)  # first index wins, as before
        _net_name_index_cache.clear()  # keep only the current project
        _net_name_index_cache[id(proj)] = (proj, name_to_index)
        entry = _net_name_index_cache[id(proj)]
    return entry[1].get(name, NO_NET)


def _drop_holes(geom: shapely.geometry.base.BaseGeometry):
    """Return ``geom`` with all interior rings removed (solid footprint)."""
    if geom.is_empty:
        return geom
    if geom.geom_type == "Polygon":
        return shapely.geometry.Polygon(geom.exterior)
    if geom.geom_type == "MultiPolygon":
        return shapely.ops.unary_union(
            [shapely.geometry.Polygon(g.exterior) for g in geom.geoms])
    return geom


class _ThroughFeatureCache:
    """Layer-independent through-feature footprints, precomputed once and reused
    across every plane layer (``_plane_sheet_polygon`` is called per plane, and
    all through features span the whole stack). A through-hole pad's footprint
    is layer-independent unless it carries a per-layer pad stack
    (``layer_variations`` — rare), and a via disc never depends on the layer;
    only via *membership* (``_via_on_layer``) is per-layer. So we cache the disc
    geometry and the non-variation pad footprints, and recompute only the rare
    variation pads per layer. Read-only after construction — safe to share
    across the per-layer thread pool in :func:`build_layer_geometries`."""

    __slots__ = ("pad_footprint_by_id", "via_feats")

    def __init__(self, proj: ExtractedProject) -> None:
        # id(pad) → hole-dropped footprint, for through pads WITHOUT a per-layer
        # stack (their shape is identical on every layer). Pads with variations
        # are absent here and get recomputed per layer.
        self.pad_footprint_by_id: dict[int, shapely.geometry.base.BaseGeometry] = {}
        for p in proj.pads:
            if not (p.is_through_hole or p.layer_id == MULTI_LAYER_PAD_LAYER_ID):
                continue
            if getattr(p, "layer_variations", ()):
                continue  # per-layer stack — recompute per layer
            poly = _pad_polygon(p, None)
            if poly is not None and not poly.is_empty:
                self.pad_footprint_by_id[id(p)] = _drop_holes(poly)
        # (via, hole-dropped disc, net) — disc reused; membership per layer.
        self.via_feats: list[tuple[RawVia, shapely.geometry.base.BaseGeometry, int]] = []
        for v, disc in _batch_via_polygons(proj.vias):
            if not disc.is_empty:
                self.via_feats.append((v, _drop_holes(disc), v.net_index))


def _through_features_on_layer(
    proj: ExtractedProject, layer_id: int, enabled_layers: list[int],
    cache: _ThroughFeatureCache | None = None,
) -> list[tuple[shapely.geometry.base.BaseGeometry, int]]:
    """Solid copper footprints of every through feature (through-hole / multi
    layer pad, and via barrel) crossing ``layer_id``, paired with their net.

    ``cache`` (see :class:`_ThroughFeatureCache`) supplies the layer-independent
    footprints so a per-plane caller doesn't rebuild every pad/via polygon for
    each plane; built lazily when omitted. Output order matches the un-cached
    path (pads in ``proj.pads`` order, then vias in ``proj.vias`` order)."""
    if cache is None:
        cache = _ThroughFeatureCache(proj)
    feats: list[tuple[shapely.geometry.base.BaseGeometry, int]] = []
    for p in proj.pads:
        if not (p.is_through_hole or p.layer_id == MULTI_LAYER_PAD_LAYER_ID):
            continue
        cached_fp = cache.pad_footprint_by_id.get(id(p))
        if cached_fp is not None:
            feats.append((cached_fp, p.net_index))
        else:
            # Pad with a per-layer stack (or one whose footprint was empty):
            # recompute for this specific layer.
            poly = _pad_polygon(p, layer_id)
            if poly is not None and not poly.is_empty:
                feats.append((_drop_holes(poly), p.net_index))
    enabled_pos = {lid: i for i, lid in enumerate(enabled_layers)}
    for v, disc, net in cache.via_feats:
        if _via_on_layer(v, layer_id, enabled_layers, enabled_pos):
            feats.append((disc, net))
    return feats


def _thermal_spokes(
    footprint: shapely.geometry.base.BaseGeometry,
    air_gap_mm: float,
    conductor_mm: float,
    entries: int,
) -> list[shapely.geometry.base.BaseGeometry]:
    """Thermal-relief spokes bridging a same-net feature to the plane.

    ``entries`` equal-width arms of width ``conductor_mm`` radiate from the
    feature centre, long enough to cross the ``air_gap_mm`` clearance ring and
    overlap into the surrounding plane copper, so the feature stays connected
    through the relief instead of floating in its anti-pad."""
    if entries <= 0 or conductor_mm <= 0.0:
        return []
    c = footprint.centroid
    minx, miny, maxx, maxy = footprint.bounds
    feat_radius = 0.5 * math.hypot(maxx - minx, maxy - miny)
    reach = feat_radius + air_gap_mm + conductor_mm
    spokes: list[shapely.geometry.base.BaseGeometry] = []
    start = math.pi / 4.0  # 45° — Altium's default relief orientation
    for i in range(entries):
        ang = start + i * (2.0 * math.pi / entries)
        tip = (c.x + reach * math.cos(ang), c.y + reach * math.sin(ang))
        arm = shapely.geometry.LineString([(c.x, c.y), tip]).buffer(
            conductor_mm / 2.0, cap_style="flat")
        spokes.append(arm)
    return spokes


def _plane_sheet_polygon(
    proj: ExtractedProject,
    stackup,
    enabled_layers: list[int],
    through_cache: _ThroughFeatureCache | None = None,
) -> shapely.geometry.base.BaseGeometry | None:
    """Negative-copper sheet for one internal plane.

    Built as: the board outline inset by the plane's pullback, minus an
    anti-pad (footprint + ``plane_clearance``) around every foreign-net through
    feature, minus a thermal air-gap around every same-net through feature,
    with relief spokes added back so same-net features stay connected. Foreign
    nets are isolated in their own (layer, net) buckets regardless, so the
    anti-pads are a fidelity refinement; the pullback and reliefs are what make
    the rendered/meshed plane match Altium.

    Returns None when the project carries no usable board outline.
    """
    pts = proj.board_outline
    if len(pts) < 3:
        return None
    try:
        base: shapely.geometry.base.BaseGeometry = shapely.geometry.Polygon(
            [(p.x, p.y) for p in pts])
    except Exception as e:  # pragma: no cover — defensive
        log.warning("Plane sheet skipped: board-outline Polygon ctor failed (%s)", e)
        return None
    if not base.is_empty and not base.is_valid:
        base = shapely.make_valid(base)

    pullback = float(getattr(stackup, "plane_pullback_mm", 0.0) or 0.0)
    if pullback > 0.0:
        base = base.buffer(-pullback, join_style="mitre")
    if base.is_empty:
        return None

    net_index = _net_index_by_name(proj, stackup.plane_net_name)
    clearance = float(proj.plane_clearance_mm)
    air_gap = float(proj.plane_relief_air_gap_mm)
    conductor = float(proj.plane_relief_conductor_width_mm)
    entries = int(proj.plane_relief_entries)

    # Buffer every anti-pad in ONE array-form shapely.buffer with a per-feature
    # distance (air-gap for same-net, clearance for foreign) — far fewer
    # Python↔C round trips than a per-feature .buffer(). Feature ORDER is
    # preserved (the anti-pad union below is order-sensitive), and quad_segs is
    # pinned to the value Geometry.buffer() uses by default so the buffered
    # rings are byte-for-byte the same as the old per-feature buffers; only
    # same-net features get thermal-relief spokes, added in the same order.
    footprints: list[shapely.geometry.base.BaseGeometry] = []
    distances: list[float] = []
    spokes: list[shapely.geometry.base.BaseGeometry] = []
    for footprint, fnet in _through_features_on_layer(
            proj, stackup.layer_id, enabled_layers, through_cache):
        footprints.append(footprint)
        if net_index != NO_NET and fnet == net_index:
            distances.append(air_gap)
            spokes.extend(_thermal_spokes(footprint, air_gap, conductor, entries))
        else:
            distances.append(clearance)

    if footprints:
        holes = shapely.buffer(
            np.array(footprints, dtype=object),
            np.array(distances, dtype=np.float64),
            quad_segs=_DEFAULT_BUFFER_QUAD_SEGS,
        ).tolist()
    else:
        holes = []

    # Physical cuts that remove plane copper regardless of net: board cutouts
    # (a plane can't conduct across a milled slot / internal cutout) and the
    # bores of non-plated through-holes (a drilled hole with no plated barrel).
    # These are subtracted *after* the thermal-relief union so a spoke can never
    # fill them back in.
    plane_lid = stackup.layer_id
    extra_cuts: list[shapely.geometry.base.BaseGeometry] = []
    for r in proj.regions:
        if getattr(r, "is_board_cutout", False) and len(r.outline) >= 3:
            poly = _region_polygon(r)
            if poly is not None and not poly.is_empty:
                extra_cuts.append(poly)
    for r in proj.shape_based_regions:
        if getattr(r, "is_board_cutout", False) and len(r.outline) >= 3:
            poly = _shape_based_region_polygon(r)
            if poly is not None and not poly.is_empty:
                extra_cuts.append(poly)
    for p in proj.pads:
        if (p.is_through_hole and not getattr(p, "is_plated", True)
                and getattr(p, "hole_mm", 0.0) > 0.0):
            extra_cuts.append(
                shapely.geometry.Point(p.center.x, p.center.y).buffer(
                    p.hole_mm / 2.0, resolution=CIRCLE_RESOLUTION // 4))

    # Split-plane detection: a plane layer carrying *copper* regions on a net
    # other than its own means the artwork splits the layer between multiple
    # nets — which this single-net flood model can't represent (the other
    # net's copper is missing). Altium stores split-plane copper as filled
    # Regions6 (`proj.regions`, net inherited from the parent Polygons6),
    # NOT only as ShapeBasedRegions — the previous check looked at the SBR
    # stream alone and so never fired on the common case. Scan BOTH streams
    # and name the foreign nets so the warning is actionable.
    #
    # NB this only WARNS; it does not yet re-model the split geometry
    # (subtract each foreign region from the primary flood and synthesise a
    # per-net sub-plane). That geometry change is golden-affecting and needs a
    # corpus split-plane board to validate the region→net mapping and the
    # flood subtraction; it is deliberately deferred rather than shipped blind.
    foreign_net_names: set[str] = set()
    for stream in (proj.regions, proj.shape_based_regions):
        for r in stream:
            if (r.layer_id == plane_lid and r.kind == 0 and not r.is_keepout
                    and not getattr(r, "is_polygon_outline", False)
                    and not getattr(r, "is_board_cutout", False)
                    and r.net_index not in (NO_NET, net_index)):
                if 0 <= r.net_index < len(proj.nets):
                    foreign_net_names.add(proj.nets[r.net_index].name)
                else:
                    foreign_net_names.add(f"#{r.net_index}")
    if foreign_net_names:
        log.warning(
            "Plane layer %d (net %r) carries copper on other net(s) %s — this "
            "is a SPLIT plane. FYPA models it as a single %r flood, so the "
            "other net(s)' copper on this layer is NOT represented and their "
            "return-path resistance will be overstated. Split the plane into "
            "separate nets in Altium, or treat these rails' results with "
            "caution.", plane_lid, stackup.plane_net_name,
            ", ".join(sorted(foreign_net_names)), stackup.plane_net_name)

    sheet = base
    if holes:
        sheet = sheet.difference(shapely.ops.unary_union(holes))
    if spokes:
        sheet = sheet.union(shapely.ops.unary_union(spokes)).intersection(base)
    if extra_cuts:
        sheet = sheet.difference(shapely.ops.unary_union(extra_cuts))
    if not sheet.is_empty and not sheet.is_valid:
        sheet = shapely.make_valid(sheet)

    mp = _ensure_multipolygon(sheet)
    return mp if not mp.is_empty else None


def build_layer_geometry(proj: ExtractedProject, layer_id: int,
                         enabled_layers: list[int],
                         through_cache: _ThroughFeatureCache | None = None
                         ) -> GeometryLayer:
    stackup_by_id = {s.layer_id: s for s in proj.stackup}
    stackup = stackup_by_id[layer_id]

    if stackup.is_plane:
        # Planes are negative copper: the board outline (inset by the plane
        # pullback) flooded on the plane's net, cleared around foreign features
        # and thermal-relieved to same-net features (see _plane_sheet_polygon).
        sheet = _plane_sheet_polygon(proj, stackup, enabled_layers, through_cache)
        if sheet is None:
            log.warning("Layer %d (%s) is a plane (net=%s) but the board has no"
                        " outline to flood — emitting empty layer.",
                        layer_id, stackup.name, stackup.plane_net_name)
            shape = shapely.geometry.MultiPolygon()
        else:
            shape = _ensure_multipolygon(sheet)
        return GeometryLayer(
            layer_id=layer_id,
            name=stackup.name,
            shape=shape,
            conductance=stackup.copper_thickness_mm * COPPER_CONDUCTIVITY_S_PER_MM,
            is_plane=True,
            plane_net_index=_net_index_by_name(proj, stackup.plane_net_name),
        )

    pieces: list[shapely.geometry.base.BaseGeometry] = []

    # Batch the track / arc buffers through one shapely C dispatch each (the
    # same helpers the per-net path uses) instead of a per-primitive
    # LineString.buffer() Python call — bit-identical output (verified
    # WKB-for-WKB), far fewer Python↔C round trips on track-heavy layers.
    # (The all-nets union below now runs through the Clipper2 fuse seam at the
    # 1 nm lossless scale — see the fuse() call at the end of this function.)
    valid_tracks = [t for t in proj.tracks
                    if t.layer_id == layer_id and not t.is_keepout
                    and not t.is_polygon_outline and t.width_mm > 0]
    pieces.extend(_batch_buffer_tracks(valid_tracks))

    valid_arcs = [a for a in proj.arcs
                  if a.layer_id == layer_id and not a.is_keepout
                  and not a.is_polygon_outline
                  and a.width_mm > 0]
    pieces.extend(_batch_buffer_arcs(valid_arcs))

    sbr_poly_indices = _shape_based_polygon_indices(proj)
    for r in proj.regions:
        if r.layer_id != layer_id:
            continue
        if r.is_keepout or r.is_polygon_outline or r.is_board_cutout:
            continue
        if r.kind != 0 or len(r.outline) < 3:
            continue
        if _skip_region_as_duplicate(r, sbr_poly_indices):
            continue
        poly = _region_polygon(r)
        if poly.is_empty:
            continue
        pieces.append(poly)

    for r in proj.shape_based_regions:
        if r.layer_id != layer_id:
            continue
        if r.is_keepout or r.is_polygon_outline or r.is_board_cutout:
            continue
        if r.kind != 0 or len(r.outline) < 3:
            continue
        poly = _shape_based_region_polygon(r)
        if poly.is_empty:
            continue
        pieces.append(poly)

    for f in proj.fills:
        if f.layer_id != layer_id or f.is_keepout:
            continue
        poly = _fill_polygon(f)
        if poly is not None:
            pieces.append(poly)

    for p in proj.pads:
        if not _pad_on_layer(p, layer_id):
            continue
        poly = _pad_polygon(p, layer_id)
        if poly is not None:
            pieces.append(poly)

    enabled_pos = {lid: i for i, lid in enumerate(enabled_layers)}
    for v in proj.vias:
        if not _via_on_layer(v, layer_id, enabled_layers, enabled_pos):
            continue
        poly = _via_polygon(v)
        if poly is not None:
            pieces.append(poly)

    if not pieces:
        shape: shapely.geometry.MultiPolygon = shapely.geometry.MultiPolygon()
    else:
        # Fuse all-nets copper via the selected backend — Clipper2 by default
        # (~10-16x faster than GEOS on a full-layer union), shapely on any error
        # or when FYPA_FUSE_BACKEND=shapely. grid_size=None selects Clipper2's
        # 1 nm integer scale, lossless for PCB coordinates, so the display
        # geometry does not shift at a µm snap (the reason this union was
        # historically kept on raw GEOS); FYPA_FUSE_BACKEND=verify re-qualifies
        # a board by comparing Clipper2 vs shapely areas per layer.
        unioned = _clipper_fuse.fuse(
            pieces, grid_size=None, key=f"layer {layer_id} (all nets)")
        shape = _ensure_multipolygon(unioned)

    return GeometryLayer(
        layer_id=layer_id,
        name=stackup.name,
        shape=shape,
        conductance=stackup.copper_thickness_mm * COPPER_CONDUCTIVITY_S_PER_MM,
        is_plane=False,
        plane_net_index=NO_NET,
    )


def build_layer_geometries(proj: ExtractedProject) -> list[GeometryLayer]:
    """Legacy single-union geometry — one :class:`GeometryLayer` per physical
    layer with **all nets unioned together**. Used for quicklook PNGs and as
    a fallback shape lookup; the FEM-facing pipeline uses
    :func:`build_per_net_geometry_layers` instead so that nets remain
    electrically isolated.

    Layers are built on a thread pool. Each layer's ``unary_union`` of its
    thousands of copper primitives dominates the cost, and shapely 2
    releases the GIL inside the union — so one worker per layer gives
    near-linear speedup (a 16-layer board's ~75 s serial build drops to
    roughly 10–15 s). Each :func:`build_layer_geometry` call only reads
    the frozen ``proj`` and builds its own geometry, so it is thread-safe.
    """
    enabled = proj.enabled_copper_layer_ids()
    if not enabled:
        log.warning("No enabled copper layers detected on %s", proj.prjpcb_path.name)
        return []
    # Through-feature footprints are layer-independent (see
    # _ThroughFeatureCache): build them ONCE and share across every plane layer
    # instead of rebuilding all pad/via polygons per plane. Read-only, so safe
    # to hand to the per-layer thread pool below.
    through_cache = _ThroughFeatureCache(proj)
    # Small boards: thread-pool overhead isn't worth it.
    if len(enabled) < 3:
        return [build_layer_geometry(proj, lid, enabled, through_cache)
                for lid in enabled]

    # The Clipper2 fuse backend (the default) is GIL-bound — unlike shapely 2's
    # union it does not release the GIL — so a per-layer thread pool would only
    # thrash the GIL. Run serial when it's active; serial Clipper2 still beats
    # the threaded shapely path on large boards (same trade-off as
    # _parallel_union_buckets). The shapely / verify backends keep the pool.
    if _clipper_fuse.backend() == "clipper" and _clipper_fuse.clipper_available():
        return [build_layer_geometry(proj, lid, enabled, through_cache)
                for lid in enabled]

    import concurrent.futures
    import os
    # union_all releases the GIL but still pegs a core per task; one
    # worker per layer, capped, is plenty.
    max_workers = min(8, (os.cpu_count() or 4), len(enabled))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        # ex.map preserves input order, so the result list lines up with
        # ``enabled`` exactly as the old list comprehension did.
        return list(ex.map(
            lambda lid: build_layer_geometry(proj, lid, enabled, through_cache),
            enabled,
        ))


def build_per_net_geometry_layers(proj: ExtractedProject) -> list[GeometryLayer]:
    """One :class:`GeometryLayer` per (physical layer, net) pair.

    Each layer contains only that net's copper (tracks + arcs + regions +
    pads + vias of that net), unioned within the net so its connected
    components are correct. Because nets are never unioned across each other,
    the FEM treats each (layer, net) as an independent conductor — making
    inter-net shorts via accidentally-overlapping copper geometrically
    impossible.

    Empty per-net shapes (a net having no copper on that layer) are dropped.
    Internal-plane layers are flooded with their net's sheet over the board
    outline and included as ordinary per-net conductors.
    """
    enabled = proj.enabled_copper_layer_ids()
    if not enabled:
        log.warning("No enabled copper layers detected on %s", proj.prjpcb_path.name)
        return []
    return _shapes_to_geometry_layers(
        proj, build_net_layer_shapes(proj, enabled, include_vias=True))


def _shapes_to_geometry_layers(
    proj: ExtractedProject,
    shapes: dict[tuple[int, int], shapely.geometry.base.BaseGeometry],
) -> list[GeometryLayer]:
    """Wrap a ``{(layer_id, net_index): unioned_shape}`` dict into
    :class:`GeometryLayer` objects, dropping empty shapes.

    Internal-plane layers are included (their flooded sheet is bucketed under
    the plane net by :func:`_build_net_layer_buckets`); the resulting layer is
    tagged ``is_plane`` with ``plane_net_index`` set to its net."""
    stackup_by_id = {s.layer_id: s for s in proj.stackup}
    out: list[GeometryLayer] = []
    for (lid, net_index), raw_shape in shapes.items():
        if raw_shape.is_empty:
            continue
        stackup = stackup_by_id.get(lid)
        if stackup is None:
            continue
        mp = _ensure_multipolygon(raw_shape)
        if mp.is_empty:
            continue
        net_name = proj.nets[net_index].name if 0 <= net_index < len(proj.nets) else "?"
        is_plane = stackup.is_plane and net_index == _net_index_by_name(
            proj, stackup.plane_net_name)
        out.append(GeometryLayer(
            layer_id=lid,
            name=f"{stackup.name}|{net_name}",
            shape=mp,
            conductance=stackup.copper_thickness_mm * COPPER_CONDUCTIVITY_S_PER_MM,
            is_plane=is_plane,
            plane_net_index=net_index if is_plane else NO_NET,
            net_index=net_index,
        ))
    return out


def build_per_net_geometry_layers_split(
    proj: ExtractedProject,
    active_nets: set[int],
) -> tuple[list[GeometryLayer], concurrent.futures.Future]:
    """Active-net :class:`GeometryLayer` objects synchronously, plus the
    remaining (non-active) nets as a background :class:`~concurrent.futures.Future`.

    The FEM only needs the few rails a directive touches; the other
    ~thousands of (layer, net) pairs feed the viewer's "all copper" overlay
    only. Unioning them is the bulk of the geometry cost, so it is pushed
    onto a background thread — the caller uses ``active_layers`` for the FEM
    immediately and joins ``rest_future`` (which yields the non-active
    GeometryLayers) once its own remaining work is done, overlapping the
    two. The buffering pass is shared, so this is the same total work as
    :func:`build_per_net_geometry_layers`, just reordered.

    When ``active_nets`` is empty everything is treated as active (the
    split would otherwise leave the FEM with no geometry).
    """
    enabled = proj.enabled_copper_layer_ids()
    if not enabled:
        log.warning("No enabled copper layers detected on %s", proj.prjpcb_path.name)
        empty: concurrent.futures.Future = concurrent.futures.Future()
        empty.set_result([])
        return [], empty

    buckets = _build_net_layer_buckets(proj, enabled, include_vias=True)
    if active_nets:
        active_b = {k: v for k, v in buckets.items() if k[1] in active_nets}
        rest_b = {k: v for k, v in buckets.items() if k[1] not in active_nets}
    else:
        # No directive touches a net — treat every real net as active, but
        # keep NO_NET copper out of the FEM regardless (it carries no rail
        # current; it exists only for the viewer's "all copper" overlay).
        active_b = {k: v for k, v in buckets.items() if k[1] != NO_NET}
        rest_b = {k: v for k, v in buckets.items() if k[1] == NO_NET}

    # Active nets get the mesher-safe grid-snapped union; the non-active
    # nets feed the display-only overlay and never reach the mesher, so
    # they take the faster plain union (snap=False).
    active_layers = _shapes_to_geometry_layers(
        proj, _parallel_union_buckets(active_b, snap=True))

    # Union the non-active nets on a background thread (shapely releases the
    # GIL inside union_all, so this genuinely overlaps the caller's work).
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    rest_future = ex.submit(
        lambda: _shapes_to_geometry_layers(
            proj, _parallel_union_buckets(rest_b, snap=False)))
    ex.shutdown(wait=False)   # task still completes; executor frees when done

    # The caller awaits ``rest_future.result()`` at the end of a long
    # ``build_problem`` — but if that raises first, the future is never
    # awaited and a background-union failure would vanish silently. Log it
    # from a done-callback so it is always observed regardless of the
    # caller's control flow.
    def _log_rest_union_exc(fut: concurrent.futures.Future) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            log.warning("Background non-active-net union failed: %s", exc)

    rest_future.add_done_callback(_log_rest_union_exc)
    return active_layers, rest_future


def _batch_buffer_tracks(tracks: list[RawTrack]) -> list[shapely.geometry.Polygon]:
    """Vectorise the per-track LineString.buffer() Python loop.

    Each call to ``LineString(...).buffer(half_width, cap_style=round, ...)``
    is one shapely C call wrapped in Python — for boards with thousands of
    tracks the per-call overhead dominates. ``shapely.linestrings`` +
    ``shapely.buffer`` route the whole batch through a single C dispatch.
    Same output as calling ``_track_polygon`` per track, just faster.
    """
    if not tracks:
        return []
    # Build (N, 2, 2) endpoint array, then one linestrings() call. shapely
    # treats axis -2 as the per-vertex axis.
    coords = np.empty((len(tracks), 2, 2), dtype=np.float64)
    half_widths = np.empty(len(tracks), dtype=np.float64)
    for i, t in enumerate(tracks):
        coords[i, 0, 0] = t.a.x
        coords[i, 0, 1] = t.a.y
        coords[i, 1, 0] = t.b.x
        coords[i, 1, 1] = t.b.y
        half_widths[i] = t.width_mm * 0.5
    # Zero-length tracks (a == b) buffer to EMPTY as LineStrings but Altium
    # draws them as filled copper dots — route them through Point instead so a
    # stitching dot isn't silently dropped (see _track_polygon).
    degenerate = ((coords[:, 0, 0] == coords[:, 1, 0])
                  & (coords[:, 0, 1] == coords[:, 1, 1]))
    geoms = np.empty(len(tracks), dtype=object)
    if degenerate.any():
        geoms[degenerate] = shapely.points(coords[degenerate, 0, :])
    normal = ~degenerate
    if normal.any():
        geoms[normal] = shapely.linestrings(coords[normal])
    polys = shapely.buffer(
        geoms, half_widths,
        cap_style="round", join_style="round",
        quad_segs=CIRCLE_RESOLUTION // 4,
    )
    return list(polys)


def _batch_via_polygons(
    vias: list[RawVia],
) -> list[tuple[RawVia, shapely.geometry.Polygon]]:
    """Vectorise the per-via ``Point().buffer()`` loop into one ``shapely.buffer``
    C dispatch, returning ``(via, disc)`` for every via with a positive diameter
    in input order. Byte-identical to :func:`_via_polygon` — same radius, same
    ``quad_segs``, round cap — but one GEOS call instead of 10⁴+ Python→GEOS
    round trips on a stitched board."""
    valid = [v for v in vias if v.diameter_mm > 0]
    if not valid:
        return []
    n = len(valid)
    xs = np.fromiter((v.center.x for v in valid), dtype=np.float64, count=n)
    ys = np.fromiter((v.center.y for v in valid), dtype=np.float64, count=n)
    radii = np.fromiter((v.diameter_mm * 0.5 for v in valid),
                        dtype=np.float64, count=n)
    discs = shapely.buffer(
        shapely.points(xs, ys), radii,
        cap_style="round", join_style="round",
        quad_segs=CIRCLE_RESOLUTION // 4,
    )
    return list(zip(valid, discs))


def _batch_buffer_arcs(arcs: list[RawArc]) -> list[shapely.geometry.Polygon]:
    """Vectorise the per-arc LineString.buffer() loop.

    Each arc has a different polyline vertex count, so we can't push them
    through one ``shapely.linestrings`` call (it requires a uniform shape).
    The expensive step is the buffer, though — building one ``LineString``
    per arc the normal way and then passing the whole numpy object-array
    of LineStrings into one ``shapely.buffer`` call routes every buffer
    through GEOS in a single C dispatch instead of N Python-level calls."""
    if not arcs:
        return []
    lines = np.empty(len(arcs), dtype=object)
    half_widths = np.empty(len(arcs), dtype=np.float64)
    for i, a in enumerate(arcs):
        pts = _arc_polyline_points(a)
        lines[i] = shapely.geometry.LineString(pts)
        half_widths[i] = a.width_mm * 0.5
    polys = shapely.buffer(
        lines, half_widths,
        cap_style="round", join_style="round",
        quad_segs=CIRCLE_RESOLUTION // 4,
    )
    return list(polys)


def _build_net_layer_buckets(
    proj: ExtractedProject,
    enabled_layers: list[int],
    include_vias: bool = False,
) -> dict[tuple[int, int], list[shapely.geometry.base.BaseGeometry]]:
    """Per-(layer_id, net_index) lists of un-unioned copper primitive
    polygons — the bucketing (and buffering) half of
    :func:`build_net_layer_shapes`.

    Split out so callers that want to union the active and non-active nets
    separately (see :func:`build_per_net_geometry_layers_split`) can share
    this single buffering pass instead of paying for it twice.

    ``include_vias`` — see :func:`build_net_layer_shapes`.
    """
    buckets: dict[tuple[int, int], list[shapely.geometry.base.BaseGeometry]] = {}
    enabled_set = set(enabled_layers)
    # On a negative internal-plane layer, tracks / arcs / regions / fills are
    # plane-defining artwork (the flood boundary and split lines), NOT copper —
    # e.g. the boundary track is drawn centred on the board edge, so half its
    # width sits outside the outline. The plane's copper is the synthesised
    # sheet alone, so this artwork is excluded from the copper buckets.
    plane_layer_ids = {s.layer_id for s in proj.stackup if s.is_plane}
    _t_buckets = time.monotonic()

    def _add(layer_id: int, net_index: int, geom):
        # NO_NET copper (an unassigned arc / region / free pour) is bucketed
        # under the NO_NET key rather than dropped: it never reaches the FEM
        # — build_per_net_geometry_layers_split routes the NO_NET bucket to
        # the non-active geometry — but the viewer's "all copper" overlay
        # needs it so unassigned copper still renders.
        if geom is None or geom.is_empty:
            return
        if layer_id not in enabled_set:
            return
        buckets.setdefault((layer_id, net_index), []).append(geom)

    # Tracks: batch-buffer all valid tracks in one shapely call, then route.
    valid_tracks = [t for t in proj.tracks
                    if not t.is_keepout
                    and not t.is_polygon_outline
                    and t.width_mm > 0
                    and t.layer_id not in plane_layer_ids]
    track_polys = _batch_buffer_tracks(valid_tracks)
    for t, poly in zip(valid_tracks, track_polys):
        _add(t.layer_id, t.net_index, poly)

    # Arcs: same vectorised-buffer trick. Exclude polygon-pour *outline* arcs
    # (boundary artwork, not copper) exactly as the track filter above does.
    valid_arcs = [a for a in proj.arcs
                  if not a.is_keepout and not a.is_polygon_outline
                  and a.width_mm > 0
                  and a.layer_id not in plane_layer_ids]
    arc_polys = _batch_buffer_arcs(valid_arcs)
    for a, poly in zip(valid_arcs, arc_polys):
        _add(a.layer_id, a.net_index, poly)

    sbr_poly_indices = _shape_based_polygon_indices(proj)
    for r in proj.regions:
        if r.is_keepout or r.is_polygon_outline or r.is_board_cutout or r.kind != 0:
            continue
        if len(r.outline) < 3 or r.layer_id in plane_layer_ids:
            continue
        if _skip_region_as_duplicate(r, sbr_poly_indices):
            continue
        _add(r.layer_id, r.net_index, _region_polygon(r))

    for r in proj.shape_based_regions:
        if r.is_keepout or r.is_polygon_outline or r.is_board_cutout or r.kind != 0:
            continue
        if len(r.outline) < 3 or r.layer_id in plane_layer_ids:
            continue
        _add(r.layer_id, r.net_index, _shape_based_region_polygon(r))

    for f in proj.fills:
        if f.is_keepout or f.layer_id in plane_layer_ids:
            continue
        _add(f.layer_id, f.net_index, _fill_polygon(f))

    for p in proj.pads:
        if p.is_through_hole or p.layer_id == MULTI_LAYER_PAD_LAYER_ID:
            # Through-hole / multi-layer pads sit on every enabled copper layer.
            if getattr(p, "layer_variations", ()):
                # Per-layer pad stack (different shape / size per layer): build
                # the layer-specific shape for each layer.
                for lid in enabled_layers:
                    poly = _pad_polygon(p, lid)
                    if poly is not None:
                        _add(lid, p.net_index, poly)
            else:
                # Same shape on every layer (the overwhelming majority): build
                # the polygon ONCE and share the immutable object across layers
                # instead of rebuilding an identical box/buffer per enabled
                # layer.
                poly = _pad_polygon(p, None)
                if poly is not None:
                    for lid in enabled_layers:
                        _add(lid, p.net_index, poly)
        else:
            poly = _pad_polygon(p, p.layer_id)
            if poly is not None:
                _add(p.layer_id, p.net_index, poly)

    if include_vias:
        enabled_pos = {lid: i for i, lid in enumerate(enabled_layers)}
        # One vectorised buffer for all via discs, then distribute to layers.
        for v, poly in _batch_via_polygons(proj.vias):
            # Span by physical stack position (not raw layer id): internal
            # planes carry ids 39-54 that fall outside a Top..Bottom id range
            # but sit physically between them, so a raw-id range would skip
            # them. _via_on_layer walks the enabled order, so the via barrel
            # correctly lands on (and stitches to) any plane it passes through.
            for lid in enabled_layers:
                if _via_on_layer(v, lid, enabled_layers, enabled_pos):
                    _add(lid, v.net_index, poly)

    # Internal-plane layers carry no copper primitives of their own: flood each
    # enabled plane layer with its net's solid sheet so the plane is a real
    # conductor in the per-net FEM. It unions with the same-net via/pad discs
    # already bucketed onto this layer above (the vias that stitch the plane to
    # top/bottom copper), keeping the rail connected.
    # Through-feature footprints are layer-independent — build once, reuse for
    # every plane sheet below instead of rebuilding all pad/via polygons per
    # plane (see _ThroughFeatureCache). Built lazily: only if there's a plane.
    _through_cache: _ThroughFeatureCache | None = None
    for s in proj.stackup:
        if not s.is_plane or s.layer_id not in enabled_set:
            continue
        net_index = _net_index_by_name(proj, s.plane_net_name)
        if net_index == NO_NET:
            continue
        if _through_cache is None:
            _through_cache = _ThroughFeatureCache(proj)
        # Per-plane: pullback and net differ, so the sheet is built per layer.
        plane_sheet = _plane_sheet_polygon(proj, s, enabled_layers, _through_cache)
        if plane_sheet is not None:
            _add(s.layer_id, net_index, plane_sheet)

    log.info("build_net_layer_shapes: buffered primitives into %d (layer, net) "
             "bucket(s) in %.2fs", len(buckets), time.monotonic() - _t_buckets)
    return buckets


def build_net_layer_shapes(
    proj: ExtractedProject,
    enabled_layers: list[int],
    include_vias: bool = False,
) -> dict[tuple[int, int], shapely.geometry.base.BaseGeometry]:
    """Per-(layer_id, net_index) copper shape, unioned per-net.

    ``include_vias`` controls whether via primitives contribute:

    * ``False`` (default) — non-via primitives only. Used as the ground-truth
      net-membership oracle for the legacy single-union via-coupling filter,
      so a via's own disc can't self-confirm an otherwise-illegitimate
      coupling.
    * ``True`` — full per-net copper including via discs. Used to build the
      :class:`GeometryLayer` objects fed to padne in the per-net FEM pipeline,
      where each net needs its own conductor including its vias.

    Through-hole pads contribute to every enabled copper layer (plated
    through, copper on every layer they span). Primitives without a net
    assignment are bucketed under the :data:`NO_NET` key — kept so the
    viewer's "all copper" overlay can render unassigned copper, while the
    FEM pipeline filters the NO_NET bucket out of its active set.
    """
    buckets = _build_net_layer_buckets(proj, enabled_layers, include_vias)
    # Per-(layer, net) unary_union: shapely 2 releases the GIL inside
    # unary_union, so a thread pool gives real parallelism here. Threading
    # beats process pools — no pickling of shapely geometries needed.
    _t_union = time.monotonic()
    unioned = _parallel_union_buckets(buckets)
    log.info("build_net_layer_shapes: per-net union of %d bucket(s) done "
             "in %.2fs", len(buckets), time.monotonic() - _t_union)
    return unioned


def _parallel_union_buckets(
    buckets: dict[tuple[int, int], list[shapely.geometry.base.BaseGeometry]],
    snap: bool = True,
) -> dict[tuple[int, int], shapely.geometry.base.BaseGeometry]:
    """Union each (layer, net) bucket's primitive polygons into one shape.

    With ``snap`` true (the default), ``shapely.union_all`` runs ON a 1 µm
    precision grid (``grid_size=_UNION_SNAP_GRID_MM``): the result is already
    snapped — no near-duplicate vertices for ``triangle.triangulate`` to
    choke on — and valid, in a single GEOS pass. This replaces the old
    union-then-``set_precision`` two-step, whose separate snap dominated the
    geometry path's cost.

    A grid union is slower per bucket than a plain one, so ``snap=False``
    skips it — used for display-only geometry (the viewer's "all copper"
    overlay) that is never handed to the mesher and so needs no snap.

    shapely 2 releases the GIL inside ``union_all``, so a thread pool gives
    real parallelism; only spun up once the workload is large enough to
    amortise the thread-pool overhead.
    """
    total_pieces = sum(len(p) for p in buckets.values())
    big_buckets = sum(1 for v in buckets.values() if len(v) > 1)
    use_threads = big_buckets >= 4 and total_pieces >= 200
    # The Clipper2 backend (pyclipr, the default) is GIL-bound — it does NOT
    # release the GIL the way shapely 2's union does — so running it across the
    # thread pool only thrashes the GIL and is slower than serial. Force serial
    # whenever it's the active backend; serial Clipper2 still beats threaded
    # shapely on large boards. "verify" runs both, so leave it threaded (the
    # shapely half parallelises). shapely backend keeps the thread pool.
    if _clipper_fuse.backend() == "clipper" and _clipper_fuse.clipper_available():
        use_threads = False
    grid = _UNION_SNAP_GRID_MM if snap else None

    def _union_one(key, pieces):
        # Fuse via the selected backend (shapely by default; opt-in Clipper2
        # with shapely fallback — see fypa._clipper_fuse). Default path is
        # identical to shapely.union_all(pieces, grid_size=grid).
        return _sanitise_unioned_shape(
            _clipper_fuse.fuse(pieces, grid, key=key), key,
        )

    if not use_threads:
        return {key: _union_one(key, pieces)
                for key, pieces in buckets.items()}

    import concurrent.futures
    import os
    # Cap workers — union_all releases the GIL but still pegs a core per
    # task; min(8, cpu_count()) is plenty.
    max_workers = min(8, (os.cpu_count() or 4))
    result: dict[tuple[int, int], shapely.geometry.base.BaseGeometry] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_key = {
            ex.submit(_union_one, key, pieces): key
            for key, pieces in buckets.items()
        }
        for fut in concurrent.futures.as_completed(future_to_key):
            result[future_to_key[fut]] = fut.result()
    return result


# Grid size used by :func:`_sanitise_unioned_shape`'s ``set_precision``
# snap. 1 μm is well below any meaningful PCB feature dimension (smallest
# track widths are ~75 μm; via drill ≥ 150 μm) but coarse enough to absorb
# float-rounding jitter that produces vertices "almost but not quite the
# same" — the exact pathology that makes ``triangle.triangulate`` fail.
_UNION_SNAP_GRID_MM: float = 1.0e-3


def _sanitise_unioned_shape(
    shape: shapely.geometry.base.BaseGeometry,
    key: tuple[int, int] | None = None,
) -> shapely.geometry.base.BaseGeometry:
    """Keep only the polygonal content of a grid-unioned (layer, net) shape.

    The 1 µm precision snap that protects ``triangle.triangulate`` from
    near-duplicate vertices is now folded into the union itself
    (:func:`shapely.union_all` with ``grid_size`` — see
    :func:`_parallel_union_buckets`), and a precision-grid union is valid by
    construction. So this is just a cheap guard: a grid union of polygons
    is already a Polygon/MultiPolygon (the fast path), and only a degenerate
    input that collapses to a mixed result needs the polygonal parts picked
    out. ``key`` is used only for diagnostic logging.
    """
    if shape is None or shape.is_empty:
        return shape
    if shape.geom_type in ("Polygon", "MultiPolygon"):
        return shape
    poly = _keep_polygonal(shape)
    if poly.is_empty or poly.area <= 0.0:
        log.warning("Sanitise %s: union output has no polygonal content.", key)
        return shape
    return poly


# --- self-check ---------------------------------------------------------------

def _summarise(layers: list[GeometryLayer]) -> str:
    lines = [f"Built {len(layers)} copper layers:"]
    for L in layers:
        n_polys = len(L.shape.geoms) if not L.shape.is_empty else 0
        area = L.shape.area if not L.shape.is_empty else 0.0
        plane_tag = "  [PLANE]" if L.is_plane else ""
        lines.append(
            f"  id={L.layer_id:>2}  {L.name:<14}  "
            f"polys={n_polys:>4}  area={area:>9.2f} mm^2  "
            f"G={L.conductance:.3g} S{plane_tag}"
        )
    return "\n".join(lines)


def _save_quicklook(layers: list[GeometryLayer], out_path: str) -> None:
    """Render each layer to a single PNG for visual sanity-checking."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as MplPolygon
    from matplotlib.collections import PatchCollection

    n = len(layers)
    if n == 0:
        return
    fig, axes = plt.subplots(1, n, figsize=(7 * n, 7), squeeze=False)
    for ax, layer in zip(axes[0], layers):
        patches = []
        for poly in layer.shape.geoms:
            patches.append(MplPolygon(list(poly.exterior.coords)))
            for ring in poly.interiors:
                patches.append(MplPolygon(list(ring.coords)))
        pc = PatchCollection(patches, facecolor=(0.85, 0.55, 0.20),
                             edgecolor="black", linewidths=0.1)
        ax.add_collection(pc)
        ax.set_aspect("equal")
        ax.autoscale_view()
        ax.set_title(f"{layer.name} (id={layer.layer_id})\n"
                     f"{len(layer.shape.geoms)} polys · "
                     f"{layer.shape.area:.1f} mm^2 · G={layer.conductance:.2e} S",
                     fontsize=10)
        ax.set_xlabel("x [mm]"); ax.set_ylabel("y [mm]")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    import sys
    from fypa.altium.extract import extract_project

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) < 2:
        print("usage: python altium_geometry.py PATH_TO.PrjPcb [out.png]",
              file=sys.stderr)
        sys.exit(2)
    proj = extract_project(sys.argv[1])
    layers = build_layer_geometries(proj)
    print(_summarise(layers))
    if len(sys.argv) >= 3:
        _save_quicklook(layers, sys.argv[2])
        print(f"Wrote {sys.argv[2]}")
