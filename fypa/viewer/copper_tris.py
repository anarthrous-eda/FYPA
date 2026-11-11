"""Polygon → GL triangle soup for the copper overlays.

Pure functions with no Qt and no viewer state, so the same code runs in the
GUI process (lazy, per polygon) and in the background worker processes that
pre-triangulate a board's copper (:mod:`fypa.viewer.copper_triangulator`).
"""
from __future__ import annotations

import math

import numpy as np

from fypa.viewer.overlays import _overlay_outline_tris, _OVERLAY_WIRE_HALF_MM

_EMPTY_XY = np.empty((0, 2), dtype=np.float32)


def poly_to_triangle_input(poly) -> tuple[
    list[tuple[float, float]],
    list[tuple[int, int]],
    list[tuple[float, float]],
]:
    """Convert a shapely Polygon into Triangle-library input.

    Returns ``(vertices, segments, hole_markers)``. Each ring (the
    exterior + every interior) contributes its vertices once (the
    duplicated closing vertex is dropped) and a closed loop of
    segment indices. Hole markers are representative points inside
    each interior ring, which tells Triangle to leave the hole
    region un-meshed. Shared with the stub-outline builder so the
    outline and the triangulation see exactly the same geometry.
    """
    from shapely.geometry import Polygon as _Polygon
    verts: list[tuple[float, float]] = []
    segs: list[tuple[int, int]] = []
    hole_markers: list[tuple[float, float]] = []

    # Float32-rounded pour rings can carry coincident vertices that
    # survive as zero-length PSLG edges; Triangle's C core aborts on
    # those, so merge anything closer than this (0.1 µm — far below
    # the finest real copper feature).
    eps = 1e-4

    def _add_ring(ring) -> None:
        if ring is None or ring.is_empty:
            return
        coords = list(ring.coords)
        if len(coords) >= 2 and coords[0] == coords[-1]:
            coords = coords[:-1]
        # Drop consecutive coincident vertices (and the wrap-around
        # duplicate) so every PSLG segment has non-zero length.
        cleaned: list[tuple[float, float]] = []
        for x, y in coords:
            fx, fy = float(x), float(y)
            if cleaned and abs(fx - cleaned[-1][0]) <= eps \
                    and abs(fy - cleaned[-1][1]) <= eps:
                continue
            cleaned.append((fx, fy))
        while len(cleaned) >= 2 \
                and abs(cleaned[0][0] - cleaned[-1][0]) <= eps \
                and abs(cleaned[0][1] - cleaned[-1][1]) <= eps:
            cleaned.pop()
        if len(cleaned) < 3:
            return
        i_first = len(verts)
        verts.extend(cleaned)
        n = len(cleaned)
        for i in range(n):
            segs.append((i_first + i, i_first + (i + 1) % n))

    _add_ring(poly.exterior)
    for hole_ring in poly.interiors:
        _add_ring(hole_ring)
        try:
            hp = _Polygon(hole_ring).representative_point()
            hole_markers.append((float(hp.x), float(hp.y)))
        except Exception:
            continue

    # Weld globally-coincident vertices. The per-ring pass above only
    # drops *consecutive* duplicates; float32-stored pour rings also
    # round genuinely-distinct, non-adjacent vertices onto the exact
    # same coordinate (within and across rings). Shewchuk's Triangle
    # segfaults — silently, no traceback, taking the GUI with it — the
    # instant its vertex list holds a duplicate or any non-finite
    # coordinate, so collapse exact duplicates and bail on NaN/Inf
    # before the PSLG ever reaches the C core.
    if verts:
        welded: list[tuple[float, float]] = []
        index_of: dict[tuple[float, float], int] = {}
        remap: list[int] = []
        for vx, vy in verts:
            if not (math.isfinite(vx) and math.isfinite(vy)):
                return [], [], []   # Triangle would segfault on NaN/Inf
            key = (vx, vy)
            idx = index_of.get(key)
            if idx is None:
                idx = len(welded)
                index_of[key] = idx
                welded.append(key)
            remap.append(idx)
        clean_segs: list[tuple[int, int]] = []
        seen_segs: set[tuple[int, int]] = set()
        for a, b in segs:
            ra, rb = remap[a], remap[b]
            if ra == rb:
                continue                       # collapsed to zero length
            ordered = (ra, rb) if ra < rb else (rb, ra)
            if ordered in seen_segs:
                continue                       # duplicate constraint edge
            seen_segs.add(ordered)
            clean_segs.append((ra, rb))
        verts, segs = welded, clean_segs
    return verts, segs, hole_markers


def triangulate_via_geos(poly) -> np.ndarray:
    """GEOS constrained-Delaunay fallback for the Triangle-library
    path in :func:`triangulate_simple_polygon`. Returns the same
    flat ``(N*3, 2)`` float32 GL_TRIANGLES soup, or an empty array
    if GEOS also can't triangulate the polygon."""
    try:
        import shapely
        tri_coll = shapely.constrained_delaunay_triangles(poly)
    except Exception:
        return np.empty((0, 2), dtype=np.float32)
    if tri_coll is None or tri_coll.is_empty:
        return np.empty((0, 2), dtype=np.float32)
    tris = list(getattr(tri_coll, "geoms", [tri_coll]))
    coords: list[tuple[float, float]] = []
    for t in tris:
        if t.is_empty:
            continue
        ring = list(t.exterior.coords)
        if len(ring) < 4:
            continue
        coords.append(ring[0])
        coords.append(ring[1])
        coords.append(ring[2])
    if not coords:
        return np.empty((0, 2), dtype=np.float32)
    return np.asarray(coords, dtype=np.float32)


def triangulate_simple_polygon(poly) -> np.ndarray:
    """Constrained-Delaunay triangulate one OGC-valid shapely Polygon
    into a flat ``(N*3, 2)`` float32 GL_TRIANGLES vertex soup.

    Returns an empty array only when both backends decline the
    polygon. The caller must hand in geometry that is already valid —
    see :func:`triangulate_rings` for the validity repair.

    Primary backend is Shewchuk's Triangle (the ``triangle`` PyPI
    package); on dense Gerber-derived pour rings it occasionally
    declines the PSLG silently (returns no triangles) — the polygon
    would then vanish from the all-copper overlay while remaining
    pickable, since the picker tests Shapely containment. The GEOS
    ``constrained_delaunay_triangles`` fallback is the same
    backend the Gerber stub-solution pre-triangulation uses
    successfully, so it picks up the cases Triangle drops.
    """
    verts, segs, hole_markers = poly_to_triangle_input(poly)
    if not verts or not segs:
        return triangulate_via_geos(poly)
    try:
        import triangle as _triangle
        tri_input: dict = {"vertices": verts, "segments": segs}
        if hole_markers:
            tri_input["holes"] = hole_markers
        # ``p`` = constrained planar straight-line graph triangulation;
        # ``Q`` silences Triangle's stdout. No ``q`` / ``a`` quality
        # switches — we just want a geometrically faithful fill, not
        # a FEM-quality mesh.
        out = _triangle.triangulate(tri_input, "pQ")
    except Exception:
        out = None

    out_verts = out.get("vertices") if out else None
    out_tris = out.get("triangles") if out else None
    if out_verts is None or out_tris is None or len(out_tris) == 0:
        return triangulate_via_geos(poly)
    v_arr = np.asarray(out_verts, dtype=np.float32)
    t_arr = np.asarray(out_tris, dtype=np.int32)
    # Expand the index list into a flat (N*3, 2) GL_TRIANGLES vertex
    # soup — matches the buffer layout the GL stub/overlay batch wants.
    return v_arr[t_arr.ravel()].astype(np.float32, copy=False)


def triangulate_rings(exterior, holes) -> np.ndarray:
    """Solid-fill triangles for one polygon given as rings.

    ``exterior`` and each of ``holes`` is an ``(N, 2)`` array or a nested
    list. Returns an ``(N*3, 2)`` float32 array; consecutive triples form
    one GL_TRIANGLES triangle. Empty when the polygon can't be built.

    Shewchuk's Triangle (via the ``triangle`` PyPI package) does a
    constrained Delaunay of the exterior + interior rings with one hole
    marker per interior — fills non-convex shapes (including those with
    holes) exactly.
    """
    from shapely.geometry import Polygon as _Polygon
    if exterior is None or (hasattr(exterior, "size") and exterior.size == 0) \
            or (not hasattr(exterior, "size") and not exterior):
        exterior = []
    holes = holes or []
    try:
        poly = _Polygon(exterior, holes=holes) if len(exterior) >= 3 else None
    except Exception:
        poly = None
    if poly is None or poly.is_empty:
        return np.empty((0, 2), dtype=np.float32)

    # Shewchuk's Triangle (the C core behind the ``triangle`` package)
    # aborts the whole process — uncatchably, with no Python traceback
    # — on a degenerate PSLG: self-touching rings, zero-length edges or
    # duplicate vertices. ``all_copper`` rings are stored as float32,
    # which on a dense real-board copper pour routinely rounds
    # near-coincident vertices onto each other. Repair the polygon to
    # strict OGC validity before meshing it. This is exactly why
    # solid-fill "show all copper" crashed where wire-mesh — a
    # pure-numpy path that never touches Triangle — did not.
    if not poly.is_valid:
        try:
            repaired = poly.buffer(0)
        except Exception:
            repaired = None
        if repaired is not None and not repaired.is_empty:
            poly = repaired
    # Even on technically-valid polygons, shapely's ``unary_union`` of
    # many disk-capped Line buffers (one per Gerber track segment)
    # can leave tiny near-collinear vertex clusters along the trace
    # boundary: two segments running parallel at sub-precision float32
    # noise. Triangle interprets those as self-touching PSLG segments
    # and silently drops the triangles around them, producing small
    # V-shaped cuts in the solid fill of long traces. Simplify with a
    # tolerance just above the float32-coordinate noise floor (~1e-5
    # mm at typical board coordinates) and well below any real copper
    # feature size (typical PCB precision is 25 µm = 2.5e-2 mm).
    # 1e-4 mm = 100 nm welds the noise out without visibly shifting
    # any real curve or corner.
    try:
        simplified = poly.simplify(1e-4, preserve_topology=True)
    except Exception:
        simplified = None
    if (simplified is not None
            and not simplified.is_empty
            and simplified.is_valid):
        poly = simplified

    # buffer(0) can split an invalid polygon into several pieces; mesh
    # each component polygon on its own PSLG and concatenate (a single
    # shared PSLG would let Triangle fill the gaps between them).
    if poly.geom_type == "Polygon":
        parts = [poly]
    elif hasattr(poly, "geoms"):
        parts = [g for g in poly.geoms if g.geom_type == "Polygon"]
    else:
        parts = []
    chunks: list[np.ndarray] = []
    for part in parts:
        if part.is_empty:
            continue
        piece = triangulate_simple_polygon(part)
        if piece.size:
            chunks.append(piece)
    return (np.concatenate(chunks, axis=0) if chunks
            else np.empty((0, 2), dtype=np.float32))


def wire_rings(exterior, holes) -> np.ndarray:
    """Mitered wire-mesh outline triangles for one polygon given as rings
    (its exterior plus any holes), as an ``(N*3, 2)`` float32 array."""
    chunks: list[np.ndarray] = []
    if exterior is not None and len(exterior) >= 2:
        chunks.append(_overlay_outline_tris(
            exterior, _OVERLAY_WIRE_HALF_MM, closed=True))
    for hole in holes or []:
        if hole is not None and len(hole) >= 2:
            chunks.append(_overlay_outline_tris(
                hole, _OVERLAY_WIRE_HALF_MM, closed=True))
    return (np.concatenate(chunks, axis=0) if chunks
            else np.empty((0, 2), dtype=np.float32))


# Copper-overlay fill styles and the polygon-dict key each one's triangles
# are cached under.
SOLID = "solid"
WIRE = "wire"
CACHE_KEY = {SOLID: "_tris_cache", WIRE: "_wire_cache"}
_BUILDERS = {SOLID: triangulate_rings, WIRE: wire_rings}


def build_polygon(kind: str, exterior, holes) -> np.ndarray:
    """Triangles for one polygon in the given fill style."""
    return _BUILDERS[kind](exterior, holes)


def build_chunk(kind: str, rings: list) -> tuple[np.ndarray, np.ndarray]:
    """Worker-process entry point: triangulate a run of polygons.

    ``rings`` is a list of ``(exterior, holes)``. Returns the polygons'
    triangles concatenated into one ``(N, 2)`` float32 array, plus each
    polygon's vertex count, so the result crosses the process boundary as
    two arrays rather than one small array per polygon.
    """
    build = _BUILDERS[kind]
    parts: list[np.ndarray] = []
    counts = np.zeros(len(rings), dtype=np.int64)
    for i, (exterior, holes) in enumerate(rings):
        try:
            arr = build(exterior, holes)
        except Exception:
            arr = _EMPTY_XY
        counts[i] = arr.shape[0]
        if arr.shape[0]:
            parts.append(arr)
    xy = np.concatenate(parts, axis=0) if parts else _EMPTY_XY
    return np.ascontiguousarray(xy, dtype=np.float32), counts
