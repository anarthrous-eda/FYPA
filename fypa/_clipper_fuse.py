"""Clipper2-backed copper fuse — a fast, in-process alternative to
``shapely.union_all`` for FYPA's per-(layer, net) copper union
(see :func:`fypa.altium_geometry._parallel_union_buckets`).

Backed by `pyclipr` (Clipper2 via pybind11). Unlike a subprocess CAD kernel it
runs *in-process* and takes / returns numpy arrays — no serialization, no temp
files. On real boards the raw Clipper2 union is ~16× faster than GEOS; end to
end (including the shapely rebuild the mesher needs) it benchmarks ~2× faster
than FYPA's threaded ``shapely.union_all`` path, with more headroom available by
feeding rings straight to the triangulator instead of rebuilding shapely.

Backend chosen by ``$FYPA_FUSE_BACKEND`` (default ``"clipper"``):

* ``"clipper"`` (default) — fuse with Clipper2, falling back to
  ``shapely.union_all`` on *any* error (and automatically falling back when
  pyclipr isn't importable), so a board can never fail to build because of this
  path. This is what a normal run uses.
* ``"shapely"`` — force the legacy path; pyclipr is never called. The opt-out.
* ``"verify"`` — fuse with Clipper2 **and** shapely, compare per bucket, and on
  any area disagreement beyond :data:`_VERIFY_TOL_MM2` keep the shapely result
  and log. Use this (or ``tools/bench_fuse.py``) to re-qualify a board.

The Clipper2 path falls back on *exceptions*; it does not catch a silently
valid-but-wrong result. ``verify`` mode is the guard for that — run it
periodically / when geometry handling changes.

Correctness recipe (validated on real boards to µm):

* scale mm → integer at ``1/grid_size`` (1 µm at the usual ``grid_size=1e-3``);
* orient exteriors CCW / holes CW — Clipper2's union respects winding, while
  ``shapely.union_all`` ignores it, so FYPA's inconsistently-wound buffered
  pieces would otherwise cancel into spurious holes;
* ``Union`` with ``FillRule.NonZero``;
* rebuild outer + holes from the returned ``PolyTree`` hierarchy.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable

import numpy as np
import shapely
import shapely.geometry
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

try:  # optional — FYPA imports and runs fine without it (default backend)
    import pyclipr
except Exception:  # pragma: no cover - absence is the default state
    pyclipr = None

# Integer scale used when grid_size is None (display-only geometry, no snap):
# 1 nm, far finer than any PCB feature, so the integerisation is lossless.
_DEFAULT_SCALE = 1_000_000
# Per-bucket area agreement tolerance for "verify" mode. Matches the µm-level
# residual seen between Clipper2 and GEOS at the shared 1 µm snap.
_VERIFY_TOL_MM2 = 1.0e-3


def clipper_available() -> bool:
    """True when the optional ``pyclipr`` backend is importable."""
    return pyclipr is not None


def backend() -> str:
    """Selected fuse backend: ``clipper`` (default), ``shapely`` or ``verify``.

    Defaults to ``clipper`` so a normal run gets the faster path with no env
    var set; it falls back to shapely automatically if pyclipr is missing or a
    bucket errors. Set ``FYPA_FUSE_BACKEND=shapely`` to force the legacy path."""
    return os.environ.get("FYPA_FUSE_BACKEND", "clipper").strip().lower()


# ---------------------------------------------------------------------------
# Geometry-in: oriented rings as numpy arrays
# ---------------------------------------------------------------------------

def _signed_area(pts: np.ndarray) -> float:
    """Shoelace signed area; >0 is CCW. Vectorised so we never pay shapely's
    geometry-rebuilding ``orient`` cost on the hot path.

    Shapely rings are closed (last vertex == first), so the wrap-around term
    of the shoelace sum is identically zero and a plain slice-and-dot gives
    the full sum without ``np.roll`` — which allocates two temporary copies
    per call and dominated this function in profiling (~5x slower)."""
    x = pts[:, 0]
    y = pts[:, 1]
    return 0.5 * float(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1]))


def _iter_polygons(geom: BaseGeometry):
    gt = geom.geom_type
    if gt == "Polygon":
        yield geom
    elif gt in ("MultiPolygon", "GeometryCollection"):
        for g in geom.geoms:
            if g.geom_type == "Polygon" and not g.is_empty:
                yield g


def _rings_of(geom: BaseGeometry) -> list[np.ndarray]:
    """Oriented rings (exterior CCW, holes CW) as ``(N, 2)`` float arrays."""
    rings: list[np.ndarray] = []
    for poly in _iter_polygons(geom):
        if poly.is_empty:
            continue
        # shapely.get_coordinates runs the coordinate copy in C; np.asarray
        # on the CoordinateSequence goes through a slower per-vertex path.
        ext = shapely.get_coordinates(poly.exterior)
        if _signed_area(ext) < 0.0:
            ext = ext[::-1]
        rings.append(ext)
        for interior in poly.interiors:
            h = shapely.get_coordinates(interior)
            if _signed_area(h) > 0.0:  # holes must wind opposite the exterior
                h = h[::-1]
            rings.append(h)
    return rings


def _scale_for(grid_size: float | None) -> int:
    if grid_size and grid_size > 0:
        return max(1, round(1.0 / grid_size))
    return _DEFAULT_SCALE


# ---------------------------------------------------------------------------
# Rings-out: PolyTree -> shapely
# ---------------------------------------------------------------------------

def _tree_to_polys(node, out: list) -> None:
    """Walk a Clipper2 ``PolyTreeD``: non-hole nodes are outer contours whose
    direct hole children are their holes; a hole's children are islands (new
    outers), handled by recursion."""
    for child in node.children:
        if not child.isHole:
            outer = child.polygon
            if len(outer) >= 3:
                holes = [gc.polygon for gc in child.children
                         if gc.isHole and len(gc.polygon) >= 3]
                poly = shapely.geometry.Polygon(outer, holes)
                if not poly.is_empty and poly.area > 0.0:
                    out.append(poly)
            for gc in child.children:
                _tree_to_polys(gc, out)
        else:
            _tree_to_polys(child, out)


def clipper_union_all(geoms: Iterable[BaseGeometry],
                      grid_size: float | None = None) -> BaseGeometry:
    """Fuse copper with Clipper2; signature-compatible with ``shapely.union_all``.

    Raises if ``pyclipr`` is unavailable (callers fall back to shapely)."""
    if pyclipr is None:
        raise RuntimeError("pyclipr is not installed")
    rings: list[np.ndarray] = []
    for g in geoms:
        if g is None or g.is_empty:
            continue
        rings.extend(_rings_of(g))
    if not rings:
        return shapely.geometry.Polygon()
    pc = pyclipr.Clipper()
    pc.scaleFactor = _scale_for(grid_size)
    pc.addPaths(rings, pyclipr.Subject, False)
    tree = pc.executeTree(pyclipr.Union, pyclipr.FillRule.NonZero)
    polys: list = []
    _tree_to_polys(tree, polys)
    if not polys:
        return shapely.geometry.Polygon()
    if len(polys) == 1:
        return polys[0]
    return shapely.geometry.MultiPolygon(polys)


# ---------------------------------------------------------------------------
# Backend-aware entry point used by _parallel_union_buckets
# ---------------------------------------------------------------------------

def fuse(pieces, grid_size: float | None, key=None) -> BaseGeometry:
    """Fuse one (layer, net) bucket with the selected backend.

    Always safe: defaults to shapely, and the Clipper2 path falls back to
    shapely on any error (and, in ``verify`` mode, on any area disagreement),
    so enabling it can never make a board fail to build or change its result
    without a logged warning.
    """
    mode = backend()
    if mode in ("clipper", "verify") and pyclipr is not None:
        try:
            shape = clipper_union_all(pieces, grid_size=grid_size)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Clipper2 fuse failed for %s (%s); using shapely.",
                        key, exc)
            return shapely.union_all(pieces, grid_size=grid_size)
        if mode == "verify":
            ref = shapely.union_all(pieces, grid_size=grid_size)
            if abs(shape.area - ref.area) > _VERIFY_TOL_MM2:
                log.warning(
                    "Clipper2/shapely area mismatch for %s: %.6f vs %.6f mm^2; "
                    "keeping shapely.", key, shape.area, ref.area)
                return ref
        return shape
    return shapely.union_all(pieces, grid_size=grid_size)
