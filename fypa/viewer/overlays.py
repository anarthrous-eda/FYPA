"""Overlay-layer defaults and the triangle geometry used to draw them."""
from __future__ import annotations

import math
import numpy as np


# Overlay layers shown in the Heatmap tab's "Board Features" control, in
# top-to-bottom row order. Each is (key, label, has_sides). A ``has_sides``
# row carries a split button that breaks the row into separate Top / Bottom
# rows. This list drives only the UI row order — the draw order is fixed
# independently in _refresh_overlay_geometry. Vias are handled by the
# layer-based via cylinder / marker path (see :meth:`_push_via_cylinders`)
# rather than as a Board Features overlay row.
_OVERLAY_LAYERS: list[tuple[str, str, bool]] = [
    ("components", "Components", True),
    ("pads", "Pads", True),
    # "silkscreen" key kept internally; label is "Overlay" to match
    # Altium's layer name (splits into Top Overlay / Bottom Overlay).
    ("silkscreen", "Overlay", True),
    ("designators", "Designators", True),
    # Non-plated through holes (mounting / mechanical holes). A through
    # feature with no side and no per-net association, so its row carries
    # only the "show everywhere" eye (no rails-only eye), like the board
    # outline. Available for both Altium and Gerber imports.
    ("npth", "Non Plated TH", False),
    # The mechanical board outline. It has no per-net association, so its
    # row carries only the "show everywhere" eye (no rails-only eye) and
    # no fill toggle — see _build_overlay_row_widget.
    ("board_outline", "Board outline", False),
]



# Fill style a freshly-built overlay row starts in (True == solid square,
# False == wire-mesh outline square). _OVERLAY_DEFAULT_SOLID is the
# fallback; keys in _OVERLAY_WIREMESH_BY_DEFAULT start as wire-mesh
# instead — Components defaults to wire-mesh so it reads as outlines and
# doesn't paint over the heatmap underneath.
_OVERLAY_DEFAULT_SOLID = True


_OVERLAY_WIREMESH_BY_DEFAULT: frozenset[str] = frozenset({"components"})




def _overlay_default_solid(key: str) -> bool:
    """Initial fill style for the Board Features row ``key`` — solid
    unless the key opts into wire-mesh via _OVERLAY_WIREMESH_BY_DEFAULT."""
    return key not in _OVERLAY_WIREMESH_BY_DEFAULT



# ── Board Features default colours ──────────────────────────────────────────
# Default RGB (each channel 0.0–1.0) every Board Features row is drawn in,
# picked to stand out against the viridis heatmap and from each other.
#
# This table is the single place to change a *default* — edit a tuple here
# and rebuild. At runtime each viewer copies it into ``self._overlay_colors``
# (the primary colour — used by a merged row and the Top side of a split
# one); the per-row colour-swatch button edits that copy. A split layer's
# Bottom row may be given its own colour, kept in ``self._overlay_bottom_
# colors`` (``None`` = follow the primary). Both are persisted to the .fypa
# project file (``viewer_settings["overlay_colors"]``) so a reopened project
# keeps its colours.
_OVERLAY_DEFAULT_COLORS: dict[str, tuple[float, float, float]] = {
    "silkscreen":    (1.00, 1.00, 0.00),   # yellow (#ffff00)
    "pads":          (1.00, 0.82, 0.29),   # amber
    "components":    (0.00, 0.50, 0.00),   # green (#008000)
    "designators":   (1.00, 1.00, 0.00),   # yellow (#ffff00)
    "npth":          (0.00, 0x91 / 255, 0x90 / 255),  # teal (#009190)
    "board_outline": (0.50, 0.00, 0.00),   # dark red / maroon (#800000)
}



# Default Bottom-side colour for a Board Features layer. Applied to the
# layer's bottom side — both the Bottom row of a split layer and the
# bottom-side geometry of a merged row — so the two board faces stay
# visually distinct. A layer absent here defaults to None: its Bottom
# side simply follows the primary colour above. The colour-swatch button
# overrides this per project.
_OVERLAY_DEFAULT_BOTTOM_COLORS: dict[str, tuple[float, float, float]] = {
    "silkscreen": (0.50, 0.50, 0.00),               # olive (#808000)
    "components": (0.0, 0x57 / 255, 0.0),           # dark green (#005700)
    "pads":       (0xb0 / 255, 0x89 / 255, 0x16 / 255),  # brass (#b08916)
}



# World-space half-width (mm) of an overlay wire-mesh outline ribbon. The
# Overlays control renders every outline as a thin filled ribbon (rather
# than GL_LINES) so wire-mesh and solid fill share one GL triangle batch.
# Coder-tunable — raise for a heavier wire-mesh, lower for a finer one.
_OVERLAY_WIRE_HALF_MM = 0.025



# Segments a via circle is tessellated into.
_OVERLAY_CIRCLE_SEGMENTS = 20



# Editor-mode connectivity dimming for the all-copper overlay. When a
# selection highlight is active, copper whose net is NOT connected to the
# selection recedes. The overlay-fill batch has no per-vertex alpha (only
# the heatmap mesh does — see _editor_alpha_array), so we fake the same
# "0.1 alpha over background" look by pre-blending the colour toward the
# editor-mode clear colour. Keep _EDITOR_OVERLAY_DIM_BG in sync with
# gl_mesh_viewer._EDITOR_BG_HEX.
_EDITOR_OVERLAY_DIM_BG = (0x27 / 255.0, 0x27 / 255.0, 0x35 / 255.0)


_EDITOR_OVERLAY_DIM_ALPHA = 0.10



# Click hit-radius (screen pixels) for picking a placed free source /
# sink marker in editor mode. Converted to world mm with the current
# view zoom so it tracks the on-screen marker size at any scale.
_EDITOR_MARKER_HIT_PX = 14.0



# Glyph size + outline-width multiplier for the editor marker matching
# the current selection — it draws this much larger / thicker than the
# unselected source / sink markers.
_EDITOR_MARKER_SELECT_SCALE = 1.3



# Base outline width (px) of an editor source / sink marker. Wide enough
# that the P-side black / N-side green edge reads clearly against the
# marker fill; the selected marker scales this by _EDITOR_MARKER_SELECT_
# SCALE, and the yellow selection box reuses the selected width.
_EDITOR_MARKER_EDGE_W = 2.8



# ``schdoc`` value carried by solve-metadata directives that an editor
# directive synthesised (see ``fypa.editor_directives._EDITOR_SCHDOC``).
# Kept as a local copy so this module has no import dependency on the
# editor-directive stack; used to tell an editor-originated solution
# directive apart from a real Altium schematic one so the editor's own
# markers / form don't draw it twice (once as the live editor marker,
# once as a "schematic" directive bound to whatever component its anchor
# happens to sit on).
_EDITOR_SCHDOC = "(editor)"



# Directive terminals carrying the return ("N-side") net. Their pin
# markers are drawn with a green outline (vs black for the P side) and
# skip the rail-visibility filter, so an N net — typically ground, and
# rarely the rail being viewed — always shows its source / sink marker.
_N_SIDE_TERMINALS = frozenset({"N", "OUT_N", "IN_N"})



# Outline colour for an N-side directive marker — green, to set the
# return-net markers apart from the black-outlined P-side ones.
_N_NET_MARKER_EDGE = "#008000"



# Width (px) of the outer layer-colour ring drawn around source / sink /
# series markers, so the marker shows which copper layer it sits on. It's
# stroked around the marker (uniform on every edge), so the band hugs the
# glyph evenly; the marker covers the inner half, leaving ~this many px of
# layer colour showing beyond its own outline.
_MARKER_LAYER_RING_W = 3




def _overlay_ribbon_offsets(polyline, half_w: float, *,
                            closed: bool):
    """Mitered outer / inner edge points of a constant-width ribbon
    centred on ``polyline``.

    Returns ``(pts, outer, inner)`` — the cleaned centre-line vertices
    and the two offset edges, each a float64 ``(n, 2)`` array — or
    ``None`` when the polyline is too short to form a ribbon. The
    offset at each vertex bisects its two adjacent edge normals, so the
    band keeps a clean constant width even when the polyline has many
    short segments. Sharp corners are clamped via a tight miter limit
    (max offset ≈ 1.25 × half_w) so spikes are sub-pixel at typical
    zoom; the residual notch left at very sharp turns is < 0.25 ×
    half_w, also visually negligible.

    Shared by :func:`_overlay_outline_tris` (which fills the band) and
    :func:`_overlay_ribbon_outline_tris` (which traces its perimeter for
    the wire-mesh fill style)."""
    pts = np.asarray(polyline, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2:
        return None
    if closed and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    n = pts.shape[0]
    if n < 2:
        return None
    half_w = max(float(half_w), 1e-4)

    # Outgoing-edge unit direction + right-hand normal at each vertex.
    if closed:
        edges = np.roll(pts, -1, axis=0) - pts
    else:
        edges = np.empty_like(pts)
        edges[:-1] = pts[1:] - pts[:-1]
        edges[-1] = edges[-2]          # last vertex reuses the final edge
    elen = np.linalg.norm(edges, axis=1)
    elen[elen == 0.0] = 1.0
    edir = edges / elen[:, None]
    enorm = np.column_stack([edir[:, 1], -edir[:, 0]])

    # Per-vertex offset = bisector of the incoming + outgoing edge normals.
    prev_norm = np.roll(enorm, 1, axis=0)
    if not closed:
        prev_norm[0] = enorm[0]        # open start has no incoming edge
    bis = enorm + prev_norm
    blen = np.linalg.norm(bis, axis=1)
    # Near-hairpin (incoming ≈ -outgoing): bisector sum is near zero,
    # normalised direction is numerically arbitrary. Treat as flat — the
    # offset on either side of pts[i] is just ±enorm × half_w.
    flat = blen < 1e-3
    bis[flat] = enorm[flat]
    blen[flat] = 1.0
    bis /= blen[:, None]
    # Miter length = half_w / cos(theta/2). Earlier iterations tried a
    # generous cap (4 × half_w) followed by a proper bevel-join split;
    # both produced visible artifacts on dense PCB pour outlines — the
    # generous cap as long spikes, the bevel as small wedges at concave
    # corners where the inserted bevel quad oriented incorrectly
    # against the polygon interior. A tight clamp at |dot| ≥ 0.8 gives
    # a max miter offset of 1.25 × half_w (so any spike is ≤ 0.25 ×
    # half_w ≈ 6 µm at default half_w — sub-pixel at every zoom we
    # render at) without touching the segment-quad structure of the
    # downstream tessellator. Note: |dot| is used so sign noise from
    # the near-hairpin branch above cannot invert the offset direction.
    dot = np.einsum("ij,ij->i", bis, enorm)
    dot = np.maximum(np.abs(dot), 0.8)
    off = bis * (half_w / dot)[:, None]
    return pts, pts + off, pts - off




def _overlay_outline_tris(polyline, half_w: float, *,
                          closed: bool) -> np.ndarray:
    """Triangulate a thin, uniform-width outline ribbon along a polyline.

    Returns an ``(3k, 2)`` float32 array of triangle vertices filling the
    band between the polyline's two mitered offset edges (see
    :func:`_overlay_ribbon_offsets`). ``closed`` joins the last vertex back
    to the first (pad / via / component outlines); open polylines
    (silkscreen tracks) get square ends."""
    res = _overlay_ribbon_offsets(polyline, half_w, closed=closed)
    if res is None:
        return np.empty((0, 2), dtype=np.float32)
    pts, outer, inner = res
    n = pts.shape[0]
    seg = n if closed else n - 1
    cur = np.arange(seg)
    nxt = (cur + 1) % n
    out = np.empty((seg * 6, 2), dtype=np.float32)
    out[0::6] = outer[cur]
    out[1::6] = inner[cur]
    out[2::6] = outer[nxt]
    out[3::6] = outer[nxt]
    out[4::6] = inner[cur]
    out[5::6] = inner[nxt]
    return out




def _overlay_cap_arc(center, p_from, outward,
                     segments: int = 8) -> np.ndarray:
    """Interior points of a ribbon end's semicircular round cap.

    Sweeps a half-circle of radius ``|p_from - center|`` starting at
    ``p_from`` and bulging toward ``outward`` (the ribbon's travel
    direction at that end), ending at the antipode of ``p_from``. Returns
    ``segments - 1`` interior points as a float64 ``(k, 2)`` array — the
    two diametric endpoints are supplied by the caller from the outer /
    inner offset edges, so they are omitted to avoid duplicate vertices."""
    center = np.asarray(center, dtype=np.float64)
    v = np.asarray(p_from, dtype=np.float64) - center
    r = float(np.linalg.norm(v))
    if r < 1e-9 or segments < 2:
        return np.empty((0, 2), dtype=np.float64)
    a0 = math.atan2(v[1], v[0])
    # Rotating v by +90° gives (-v.y, v.x); pick the sweep sign whose
    # mid-arc direction points the same way as `outward`.
    rot90 = np.array([-v[1], v[0]], dtype=np.float64)
    sign = (1.0 if float(rot90 @ np.asarray(outward, dtype=np.float64)) >= 0.0
            else -1.0)
    k = np.arange(1, segments)
    ang = a0 + sign * math.pi * k / segments
    return np.column_stack([center[0] + r * np.cos(ang),
                            center[1] + r * np.sin(ang)])




def _overlay_ribbon_outline_tris(polyline, half_w: float, wire_w: float, *,
                                 closed: bool) -> np.ndarray:
    """Wire-mesh of a constant-width ribbon — its hollow perimeter.

    Traces the boundary of the band :func:`_overlay_outline_tris` would
    fill and renders that boundary as its own thin ribbon of half-width
    ``wire_w``. ``half_w`` is the source ribbon's half-width. Used by the
    Overlay and Designators rows' wire-mesh fill style, whose geometry is
    open stroke polylines rather than closed shapes.

    For an open polyline the perimeter is one closed loop: out along the
    outer edge, a round end cap, back along the inner edge, a round start
    cap. For a closed polyline it is two concentric loops (the band's
    outer and inner rims)."""
    res = _overlay_ribbon_offsets(polyline, half_w, closed=closed)
    if res is None:
        return np.empty((0, 2), dtype=np.float32)
    pts, outer, inner = res
    if closed:
        return np.concatenate([
            _overlay_outline_tris(outer, wire_w, closed=True),
            _overlay_outline_tris(inner, wire_w, closed=True),
        ], axis=0)
    # Open ribbon: round end + start caps bridge the outer and inner edges
    # into a single closed perimeter (matching the round caps the solid
    # fill style draws as separate circles). At an open end the offset is
    # exactly ±half_w along the edge normal, so outer[-1] / inner[-1] are
    # antipodal on the cap circle — the arc joins them cleanly.
    end_cap = _overlay_cap_arc(pts[-1], outer[-1], pts[-1] - pts[-2])
    start_cap = _overlay_cap_arc(pts[0], inner[0], pts[0] - pts[1])
    ring = np.concatenate([outer, end_cap, inner[::-1], start_cap], axis=0)
    return _overlay_outline_tris(ring, wire_w, closed=True)




def _overlay_fan_tris(ring) -> np.ndarray:
    """Triangulate a convex polygon ring as a centroid fan.

    Returns an ``(3k, 2)`` float32 array. A trailing closing vertex (last
    point equal to the first) is dropped. Pad / via / component-box rings
    are convex, so a centroid fan fills them exactly."""
    pts = np.asarray(ring, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 3:
        return np.empty((0, 2), dtype=np.float32)
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    n = pts.shape[0]
    if n < 3:
        return np.empty((0, 2), dtype=np.float32)
    cx, cy = pts.mean(axis=0)
    out: list[tuple[float, float]] = []
    for i in range(n):
        j = (i + 1) % n
        out += [(cx, cy), (pts[i, 0], pts[i, 1]), (pts[j, 0], pts[j, 1])]
    return np.asarray(out, dtype=np.float32)




def _overlay_circle_ring(cx: float, cy: float, r: float,
                         n: int = _OVERLAY_CIRCLE_SEGMENTS) -> np.ndarray:
    """A closed ``(n+1, 2)`` ring approximating a circle."""
    ang = np.linspace(0.0, 2.0 * math.pi, n + 1)
    return np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang)])




def _overlay_box_ring(x0: float, y0: float,
                      x1: float, y1: float) -> np.ndarray:
    """A closed ``(5, 2)`` ring tracing an axis-aligned rectangle."""
    return np.asarray([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]],
                      dtype=np.float64)




def _overlay_obround_ring(cx: float, cy: float,
                          length: float, width: float,
                          rotation_deg: float,
                          n: int = _OVERLAY_CIRCLE_SEGMENTS) -> np.ndarray:
    """A closed ring tracing an obround (stadium): a ``length`` × ``width``
    capsule centred at ``(cx, cy)`` and rotated ``rotation_deg`` (CCW).

    ``length`` is the long axis, ``width`` the short axis (= the drilled
    bore of a slotted hole). The two semicircular end caps have radius
    ``width / 2``; when ``length <= width`` it degenerates to a circle.
    Used to draw slotted drill holes the same way Altium renders them."""
    r = 0.5 * max(width, 0.0)
    s = max(0.5 * length - r, 0.0)  # half-length of the straight section
    m = max(2, n // 2)
    # Right cap sweeps -90°..+90°, left cap +90°..+270°, in local coords.
    a_right = np.linspace(-0.5 * math.pi, 0.5 * math.pi, m)
    a_left = np.linspace(0.5 * math.pi, 1.5 * math.pi, m)
    lx = np.concatenate([s + r * np.cos(a_right), -s + r * np.cos(a_left)])
    ly = np.concatenate([r * np.sin(a_right), r * np.sin(a_left)])
    lx = np.append(lx, lx[0])
    ly = np.append(ly, ly[0])
    th = math.radians(rotation_deg)
    c, sn = math.cos(th), math.sin(th)
    return np.column_stack([cx + lx * c - ly * sn, cy + lx * sn + ly * c])




def _overlay_rect_ring(cx: float, cy: float,
                       length: float, width: float,
                       rotation_deg: float) -> np.ndarray:
    """A closed ``(5, 2)`` ring tracing a ``length`` × ``width`` rectangle
    centred at ``(cx, cy)`` and rotated ``rotation_deg`` (CCW).

    The square-cornered counterpart of :func:`_overlay_obround_ring`, used to
    draw Altium "Rectangular" slotted / square holes (``hole_shape == 1``)."""
    hl, hw = 0.5 * length, 0.5 * width
    lx = np.array([-hl, hl, hl, -hl, -hl])
    ly = np.array([-hw, -hw, hw, hw, -hw])
    th = math.radians(rotation_deg)
    c, sn = math.cos(th), math.sin(th)
    return np.column_stack([cx + lx * c - ly * sn, cy + lx * sn + ly * c])




def _rec_slot_tuple(rec: dict
                    ) -> tuple[float, float, float, bool] | None:
    """``(length_mm, width_mm, rotation_deg, rounded)`` non-round drill of a
    hole record, or ``None`` for a round bore.

    Reads the slot fields a non-round NPTH / PTH record carries (see
    :func:`fypa.altium.loader._slot_record_fields`): ``slot_length_mm`` is
    the long axis, ``slot_width_mm`` the drilled bore (short axis),
    ``slot_rotation_deg`` the absolute rotation and ``slot_kind`` selects the
    end style — ``rounded`` is ``True`` for an obround slot, ``False`` for a
    square-cornered rectangle."""
    if not rec.get("is_slot"):
        return None
    length = float(rec.get("slot_length_mm", 0.0) or 0.0)
    width = float(rec.get("slot_width_mm", 0.0) or 0.0)
    if width <= 0.0 or length <= 0.0:
        return None
    rounded = rec.get("slot_kind", "obround") != "rect"
    return (length, width, float(rec.get("slot_rotation_deg", 0.0) or 0.0),
            rounded)




def _overlay_hole_ring(rec: dict) -> np.ndarray:
    """Ring for a drill-hole metadata record (NPTH / PTH / via dict).

    Returns the obround ring when the record carries slot fields
    (``is_slot``), otherwise a plain circle at ``diameter_mm`` /
    ``hole_diameter_mm``. Returns an empty ``(0, 2)`` array for a
    degenerate (zero-size) hole so callers can skip it."""
    cx = float(rec.get("x_mm", 0.0))
    cy = float(rec.get("y_mm", 0.0))
    slot = _rec_slot_tuple(rec)
    if slot is not None:
        length, width, rot, rounded = slot
        if rounded:
            return _overlay_obround_ring(cx, cy, length, width, rot)
        return _overlay_rect_ring(cx, cy, length, width, rot)
    d = float(rec.get("hole_diameter_mm", 0.0) or 0.0) \
        or float(rec.get("diameter_mm", 0.0) or 0.0)
    if d <= 0.0:
        return np.empty((0, 2), dtype=np.float64)
    return _overlay_circle_ring(cx, cy, 0.5 * d)
