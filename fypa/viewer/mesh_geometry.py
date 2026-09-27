"""Mesh helpers: via cylinders, prism extrusion, fast triangle sampling."""
from __future__ import annotations

import math
import numpy as np

from fypa.viewer.overlays import _overlay_obround_ring, _overlay_rect_ring


def _drill_footprint_xy(x: float, y: float, radius: float,
                        slot: tuple[float, float, float, bool] | None,
                        n_segments: int) -> np.ndarray:
    """Open perimeter ring ``(M, 2)`` of a drill footprint centred at
    ``(x, y)`` — a circle of ``radius``, or a slot when ``slot`` =
    ``(length_mm, width_mm, rotation_deg, rounded)`` (an obround when
    ``rounded`` else a square-cornered rectangle).

    The ring is open (last vertex != first) so callers can ``np.roll`` it to
    pair each vertex with its neighbour. For the circle case the layout is
    identical to the legacy ``radius * (cos, sin)`` sampling so a plain via /
    PTH renders bit-for-bit as before."""
    if slot is not None:
        length, width, rot, rounded = slot
        if rounded:
            ring = _overlay_obround_ring(x, y, length, width, rot,
                                         n=2 * n_segments)
        else:
            ring = _overlay_rect_ring(x, y, length, width, rot)
        return ring[:-1]  # drop the closing duplicate vertex → open ring
    angles = np.linspace(0.0, 2.0 * np.pi, n_segments, endpoint=False)
    return np.column_stack((x + radius * np.cos(angles),
                            y + radius * np.sin(angles)))




def _generate_disk_cap(
    x: float, y: float, z: float, radius: float,
    color_rgb: tuple[float, float, float],
    n_segments: int = 10,
    slot: tuple[float, float, float, bool] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Filled drill-footprint cap at ``(x, y, z)`` — triangle fan from centre
    to rim. A circle by default, or an obround when ``slot`` is given.

    Returns ``(positions, colors)`` float32 arrays ready for GL_TRIANGLES.
    Used to cap via / PTH cylinders at each copper-layer junction so the
    endpoint colour is visible from any camera angle.
    """
    fp = _drill_footprint_xy(x, y, radius, slot, n_segments)
    m = fp.shape[0]
    rim_a = np.column_stack((fp, np.full(m, z)))
    rim_b = np.column_stack((np.roll(fp, -1, axis=0), np.full(m, z)))
    out = np.empty((m * 3, 3), dtype=np.float32)
    out[0::3] = [x, y, z]
    out[1::3] = rim_a
    out[2::3] = rim_b
    col = np.broadcast_to(np.asarray(color_rgb, dtype=np.float32),
                          (out.shape[0], 3)).copy()
    return out, col




def _generate_via_cylinder(x: float, y: float, z_top: float, z_bottom: float,
                            radius: float, color_rgb: tuple[float, float, float],
                            n_segments: int = 10,
                            slot: tuple[float, float, float, bool] | None = None,
                            ) -> tuple[np.ndarray, np.ndarray]:
    """Generate the side-wall triangles of an open drill prism centred at
    ``(x, y)`` extending from ``z_top`` to ``z_bottom``.

    A round barrel (cylinder) by default, or an obround barrel (slot) when
    ``slot`` = ``(length_mm, width_mm, rotation_deg)`` — the 3D counterpart
    of a slotted plated through-hole. Returns ``(positions, colors)`` float32
    arrays suitable for :meth:`gl_mesh_viewer.GLMeshViewer.set_cylinders`.
    Each side facet contributes two triangles (six vertices) drawn via
    ``GL_TRIANGLES``. Caps are omitted — the user's view only ever sees the
    side surface in PDN inspection.
    """
    fp = _drill_footprint_xy(x, y, radius, slot, n_segments)
    fp_next = np.roll(fp, -1, axis=0)
    m = fp.shape[0]
    # Per-side-facet quad corners.
    top1 = np.column_stack((fp, np.full(m, z_top)))
    top2 = np.column_stack((fp_next, np.full(m, z_top)))
    bot1 = np.column_stack((fp, np.full(m, z_bottom)))
    bot2 = np.column_stack((fp_next, np.full(m, z_bottom)))
    # Interleave as triangle pairs per facet:
    #   T1 = top1, bot1, top2     T2 = top2, bot1, bot2
    out = np.empty((m * 6, 3), dtype=np.float32)
    out[0::6] = top1
    out[1::6] = bot1
    out[2::6] = top2
    out[3::6] = top2
    out[4::6] = bot1
    out[5::6] = bot2
    col = np.broadcast_to(np.asarray(color_rgb, dtype=np.float32),
                          (out.shape[0], 3)).copy()
    return out, col




def _generate_via_cylinder_gradient(
    x: float, y: float, z_top: float, z_bottom: float,
    radius: float,
    color_top_rgb: tuple[float, float, float],
    color_bottom_rgb: tuple[float, float, float],
    n_segments: int = 10,
    slot: tuple[float, float, float, bool] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Same geometry as :func:`_generate_via_cylinder` (round, or an obround
    barrel when ``slot`` is given), but each side facet's top vertices use
    ``color_top_rgb`` and its bottom vertices use ``color_bottom_rgb`` so the
    GPU interpolates the colour smoothly along the barrel's axis."""
    fp = _drill_footprint_xy(x, y, radius, slot, n_segments)
    fp_next = np.roll(fp, -1, axis=0)
    m = fp.shape[0]
    top1 = np.column_stack((fp, np.full(m, z_top)))
    top2 = np.column_stack((fp_next, np.full(m, z_top)))
    bot1 = np.column_stack((fp, np.full(m, z_bottom)))
    bot2 = np.column_stack((fp_next, np.full(m, z_bottom)))
    out = np.empty((m * 6, 3), dtype=np.float32)
    out[0::6] = top1
    out[1::6] = bot1
    out[2::6] = top2
    out[3::6] = top2
    out[4::6] = bot1
    out[5::6] = bot2
    ct = np.asarray(color_top_rgb, dtype=np.float32)
    cb = np.asarray(color_bottom_rgb, dtype=np.float32)
    col = np.empty((m * 6, 3), dtype=np.float32)
    col[0::6] = ct
    col[1::6] = cb
    col[2::6] = ct
    col[3::6] = ct
    col[4::6] = cb
    col[5::6] = cb
    return out, col




def _apply_alpha(col_rgb: np.ndarray, alpha: float) -> np.ndarray:
    """Append a constant alpha column to an (N, 3) RGB colour array,
    returning an (N, 4) RGBA array. Used to fade via-barrel sections that
    carry no rail current — the cylinder batch is alpha-blended, so a
    sub-1.0 alpha renders that section as a faint ghost."""
    a = np.full((col_rgb.shape[0], 1), float(alpha), dtype=np.float32)
    return np.concatenate(
        (np.asarray(col_rgb, dtype=np.float32), a), axis=1)




def _sample_cmap_lut(lut: np.ndarray, value: float,
                     vmin: float, vmax: float,
                     ) -> tuple[float, float, float]:
    """Pick the LUT row matching ``value`` clamped to ``[vmin, vmax]``.
    ``lut`` is the (256, 4) uint8 RGBA array built by
    :func:`_build_cmap_lut`. Returns an (R, G, B) triple in 0..1."""
    if not math.isfinite(value):
        return (0.5, 0.5, 0.5)
    span = vmax - vmin
    if span <= 0:
        t = 0.0
    else:
        t = (value - vmin) / span
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    n = lut.shape[0]
    idx = int(round(t * (n - 1)))
    return (lut[idx, 0] / 255.0,
            lut[idx, 1] / 255.0,
            lut[idx, 2] / 255.0)




def _extrude_to_prism(
    xs: np.ndarray, ys: np.ndarray, vs: np.ndarray, tris: np.ndarray,
    z_center: float, thickness: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Extrude a flat 2D triangle mesh into a thin closed prism.

    The input is the per-layer copper mesh (xs, ys, vs at a single z =
    ``z_center``). The output has:

    * **Top face**: original triangles at ``z = z_center + thickness/2``
    * **Bottom face**: original triangles at ``z = z_center - thickness/2``
      with reversed winding (outward normal points down)
    * **Side walls**: two triangles per *boundary* edge (edges incident
      to exactly one input triangle); interior edges are hidden inside
      the prism so we don't waste triangles on them.

    Per-vertex value (used by the colormap shader) is duplicated top↔bottom
    so the copper is uniformly coloured through its thickness — matches the
    FEM's sheet-conductor assumption (no in-z variation).

    Returns ``(xs, ys, zs, vs, tris)`` ready to feed into the GL viewer's
    combined batch.
    """
    n = xs.size
    if n == 0 or tris.size == 0:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty, empty, empty, np.empty((0, 3), dtype=np.int32)

    # Extrude DOWNWARD only: top face sits at z_center (the layer's
    # plane) so it stays coplanar with the outline overlay and the via
    # cylinders meet the copper flush. Bottom face is pushed down by
    # ``thickness``.
    z_top = z_center
    z_bot = z_center - thickness

    new_xs = np.concatenate([xs, xs])
    new_ys = np.concatenate([ys, ys])
    new_zs = np.empty(2 * n, dtype=np.float64)
    new_zs[:n] = z_top
    new_zs[n:] = z_bot
    new_vs = np.concatenate([vs, vs])

    top_tris = tris.astype(np.int64, copy=False)
    bot_tris = tris[:, [0, 2, 1]].astype(np.int64) + n

    # Boundary edges = those incident to exactly one triangle.
    m = tris.shape[0]
    edges = np.empty((3 * m, 2), dtype=np.int64)
    edges[0::3] = np.sort(tris[:, [0, 1]], axis=1)
    edges[1::3] = np.sort(tris[:, [1, 2]], axis=1)
    edges[2::3] = np.sort(tris[:, [2, 0]], axis=1)
    _, inverse, counts = np.unique(edges, axis=0,
                                    return_inverse=True, return_counts=True)
    boundary_mask = counts[inverse] == 1
    bedges = edges[boundary_mask]

    # Each boundary edge (a, b) → two triangles forming the side-wall
    # quad: (a_top, b_top, b_bot) and (a_top, b_bot, a_bot).
    a = bedges[:, 0]
    b = bedges[:, 1]
    wall_1 = np.column_stack([a, b, b + n])
    wall_2 = np.column_stack([a, b + n, a + n])

    all_tris = np.concatenate([top_tris, bot_tris, wall_1, wall_2],
                              axis=0).astype(np.int32, copy=False)
    return new_xs, new_ys, new_zs, new_vs, all_tris




def _shape_outline_segments(shape) -> np.ndarray:
    """Convert a shapely Polygon / MultiPolygon's outline (exterior +
    interior rings) into an (N, 2) float32 array of GL_LINES vertices.

    Consecutive vertex pairs form one segment, so N == 2 * num_segments.
    Returns an empty array when the shape is empty / has no rings. Used
    by the per-layer outline overlay in :class:`GLMeshViewer`.
    """
    if shape is None or shape.is_empty:
        return np.empty((0, 2), dtype=np.float32)
    if hasattr(shape, "geoms"):
        polys = list(shape.geoms)
    else:
        polys = [shape]
    rings: list[np.ndarray] = []
    for poly in polys:
        ext = poly.exterior
        if ext is not None and not ext.is_empty:
            rings.append(np.asarray(ext.coords, dtype=np.float32))
        for hole in poly.interiors:
            if hole is not None and not hole.is_empty:
                rings.append(np.asarray(hole.coords, dtype=np.float32))
    segs: list[np.ndarray] = []
    for ring in rings:
        # Each ring already includes the closing-vertex duplicate
        # (ring[-1] == ring[0]); turning it into GL_LINES pairs gives
        # ``len(ring) - 1`` segments, each two consecutive points.
        if ring.shape[0] < 2:
            continue
        pairs = np.empty((2 * (ring.shape[0] - 1), 2), dtype=np.float32)
        pairs[0::2] = ring[:-1]
        pairs[1::2] = ring[1:]
        segs.append(pairs)
    if not segs:
        return np.empty((0, 2), dtype=np.float32)
    return np.concatenate(segs, axis=0)




class _FastTriSampler:
    """Point-samples a triangulated linear scalar field — a drop-in
    replacement for :class:`matplotlib.tri.LinearTriInterpolator` that
    skips its ``TrapezoidMapTriFinder``.

    Matplotlib builds that trapezoid map eagerly inside the interpolator
    constructor. On a large copper plane (a GND pour reaches 300k+
    triangles) that takes 3–10 s and froze the GUI for several seconds
    the first time the hover probe touched the plane — the per-layer
    interpolator cache is why only the *first* hover stalled.

    This sampler instead builds a ``cKDTree`` over triangle centroids
    (pure C, ~50–80x faster to construct) and, at query time,
    barycentric-interpolates the nearest triangle that contains the
    point. The interpolation is mathematically identical — linear over
    each triangle — so results match ``LinearTriInterpolator`` to
    floating-point precision (verified <1e-16 V across every GND plane
    in the example projects). Same pure-C-spatial-index swap the Vias /
    Nodes report tables already use (see :meth:`PdnViewer._compute_via_report`).
    """

    # Candidate triangles examined per query. For any quality mesh the
    # containing triangle's centroid is among the nearest few; 32 is a
    # wide safety margin — verified to give zero strict-inside misses
    # across every GND plane in the bundled example designs.
    _K: int = 32

    def __init__(self, triangulation, values) -> None:
        x = np.ascontiguousarray(triangulation.x, dtype=np.float64)
        y = np.ascontiguousarray(triangulation.y, dtype=np.float64)
        tris = np.ascontiguousarray(triangulation.triangles, dtype=np.intp)
        z = np.ascontiguousarray(values, dtype=np.float64)
        i0, i1, i2 = tris[:, 0], tris[:, 1], tris[:, 2]
        # Per-triangle anchor vertex + the two edge vectors, so a query
        # is just a point offset and a 2x2 solve for the barycentrics.
        self._ax, self._ay = x[i0], y[i0]
        self._v0x, self._v0y = x[i1] - self._ax, y[i1] - self._ay
        self._v1x, self._v1y = x[i2] - self._ax, y[i2] - self._ay
        det = self._v0x * self._v1y - self._v1x * self._v0y
        # 1/det, zeroed on degenerate (zero-area) triangles so they can
        # never win the argmax below.
        self._inv_det = np.where(np.abs(det) > 1e-30, 1.0 / det, 0.0)
        self._va, self._vb, self._vc = z[i0], z[i1], z[i2]
        centroids = np.column_stack([
            (x[i0] + x[i1] + x[i2]) / 3.0,
            (y[i0] + y[i1] + y[i2]) / 3.0,
        ])
        from scipy.spatial import cKDTree
        # balanced_tree / compact_nodes off → ~3x faster build; queries
        # stay in the microsecond range, and we build once per layer.
        self._tree = cKDTree(centroids, balanced_tree=False,
                             compact_nodes=False)
        self._k = int(min(self._K, max(1, tris.shape[0])))

    def __call__(self, x: float, y: float):
        """Sample the field at world coords (x, y). Returns a 0-d
        ``numpy`` masked array — the same type ``LinearTriInterpolator``
        returns — masked when the point is off the mesh."""
        _d, idx = self._tree.query((x, y), k=self._k)
        idx = np.atleast_1d(idx)
        px = x - self._ax[idx]
        py = y - self._ay[idx]
        v0x, v0y = self._v0x[idx], self._v0y[idx]
        v1x, v1y = self._v1x[idx], self._v1y[idx]
        inv = self._inv_det[idx]
        # Barycentric weights of (x, y) for each candidate triangle:
        # p - a = u*(b - a) + w*(c - a); the third weight is 1 - u - w.
        u = (px * v1y - v1x * py) * inv
        w = (v0x * py - px * v0y) * inv
        t = 1.0 - u - w
        min_w = np.minimum(np.minimum(t, u), w)
        min_w[inv == 0.0] = -np.inf          # never pick a degenerate tri
        j = int(np.argmax(min_w))            # triangle the point is "most inside"
        if min_w[j] < -0.5:
            # Well outside every candidate triangle — a genuine off-mesh
            # point (e.g. a meshing gap inside the copper outline). Match
            # LinearTriInterpolator's masked off-mesh result.
            return np.ma.masked_array(0.0, mask=True)
        val = (t[j] * self._va[idx[j]]
               + u[j] * self._vb[idx[j]]
               + w[j] * self._vc[idx[j]])
        return np.ma.masked_array(float(val), mask=False)




def _triangulate_polygon_for_stub(poly):
    """Constrained Delaunay triangulation of a Shapely Polygon.

    Returns ``(vertex_xys, triangles)`` as numpy arrays — ``(N, 2)``
    float64 and ``(M, 3)`` int32 — in the format the viewer's
    :class:`LeanLayerSolution` expects.

    Uses :func:`shapely.constrained_delaunay_triangles` (GEOS 3.10+),
    which respects the polygon boundary so the result has no triangles
    outside the copper. Empty / invalid polygons return empty arrays.
    """
    import shapely
    if poly is None or poly.is_empty or poly.area <= 0:
        return np.zeros((0, 2), dtype=np.float64), np.zeros((0, 3), dtype=np.int32)
    try:
        tri_coll = shapely.constrained_delaunay_triangles(poly)
    except Exception:
        return np.zeros((0, 2), dtype=np.float64), np.zeros((0, 3), dtype=np.int32)
    vert_index: dict[tuple[float, float], int] = {}
    verts: list[tuple[float, float]] = []
    tri_indices: list[tuple[int, int, int]] = []
    tris = getattr(tri_coll, "geoms", [tri_coll])
    for t in tris:
        if t.is_empty:
            continue
        coords = list(t.exterior.coords)
        if len(coords) < 4:                  # need 3 distinct verts + close
            continue
        idx: list[int] = []
        for x, y in coords[:3]:
            k = (float(x), float(y))
            i = vert_index.get(k)
            if i is None:
                i = len(verts)
                vert_index[k] = i
                verts.append(k)
            idx.append(i)
        tri_indices.append((idx[0], idx[1], idx[2]))
    if not verts:
        return np.zeros((0, 2), dtype=np.float64), np.zeros((0, 3), dtype=np.int32)
    return (
        np.asarray(verts, dtype=np.float64),
        np.asarray(tri_indices, dtype=np.int32),
    )
