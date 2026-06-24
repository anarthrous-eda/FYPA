"""Clipper2 (pyclipr) copper-fuse adapter: orientation correctness, agreement
with shapely.union_all, and safe backend selection / fallback.

The Clipper2 cross-checks skip cleanly when the optional ``pyclipr`` backend
isn't installed (the default state); the backend-selection and orientation
tests run regardless.
"""
from __future__ import annotations

import numpy as np
import pytest
import shapely
import shapely.geometry as G
from shapely.geometry.polygon import orient

from fypa import _clipper_fuse as cf

GRID = 1.0e-3
has_pyclipr = cf.clipper_available()
needs_pyclipr = pytest.mark.skipif(not has_pyclipr, reason="pyclipr not installed")


# --- pure helpers (no pyclipr needed) --------------------------------------

def test_signed_area_sign():
    ccw = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    assert cf._signed_area(ccw) > 0
    assert cf._signed_area(ccw[::-1]) < 0


def test_rings_orientation_normalised():
    # a CW exterior with a CCW hole — both "wrong" way round
    cw_outer = [(0, 0), (0, 10), (10, 10), (10, 0)]          # CW
    ccw_hole = [(3, 3), (7, 3), (7, 7), (3, 7)]              # CCW
    poly = G.Polygon(cw_outer, [ccw_hole])
    rings = cf._rings_of(poly)
    assert len(rings) == 2
    assert cf._signed_area(rings[0]) > 0    # exterior forced CCW
    assert cf._signed_area(rings[1]) < 0    # hole forced CW


def test_default_backend_is_clipper(monkeypatch):
    # No env var set == a normal run: clipper is the default, with shapely
    # fallback. Result agrees with shapely (µm-level when clipper runs).
    monkeypatch.delenv("FYPA_FUSE_BACKEND", raising=False)
    assert cf.backend() == "clipper"
    pieces = [G.box(0, 0, 10, 10), G.box(5, 5, 15, 15)]
    got = cf.fuse(pieces, GRID)
    ref = shapely.union_all(pieces, grid_size=GRID)
    tol = 1e-6 if cf.clipper_available() else 1e-9  # exact when falling back
    assert abs(got.area - ref.area) < tol


def test_shapely_override_forces_legacy(monkeypatch):
    # The opt-out: FYPA_FUSE_BACKEND=shapely is pure shapely (exact match).
    monkeypatch.setenv("FYPA_FUSE_BACKEND", "shapely")
    pieces = [G.box(0, 0, 10, 10), G.box(5, 5, 15, 15)]
    got = cf.fuse(pieces, GRID)
    ref = shapely.union_all(pieces, grid_size=GRID)
    assert abs(got.area - ref.area) < 1e-9


def test_fuse_falls_back_when_clipper_absent(monkeypatch):
    # Default clipper but pyclipr missing -> automatic shapely fallback.
    monkeypatch.delenv("FYPA_FUSE_BACKEND", raising=False)
    monkeypatch.setattr(cf, "pyclipr", None)  # simulate not installed
    pieces = [G.box(0, 0, 10, 10), G.box(5, 5, 15, 15)]
    got = cf.fuse(pieces, GRID)
    ref = shapely.union_all(pieces, grid_size=GRID)
    assert abs(got.area - ref.area) < 1e-9


# --- agreement with shapely (requires pyclipr) -----------------------------

_CASES = {
    "two_overlap": [G.box(0, 0, 10, 10), G.box(5, 5, 15, 15)],
    "touching_L": [G.box(0, 0, 2, 10), G.box(0, 0, 10, 2)],
    "disjoint": [G.box(0, 0, 3, 3), G.box(10, 10, 13, 13)],
    "ring_with_hole": [G.box(0, 0, 20, 20).difference(G.box(5, 5, 15, 15))],
    "plane_two_holes": [G.box(0, 0, 30, 30)
                        .difference(G.box(4, 4, 8, 8))
                        .difference(G.box(20, 20, 26, 26))],
    # mixed-winding inputs — the case that broke the naive encoder
    "mixed_winding": [orient(G.box(0, 0, 10, 10), 1.0),
                      orient(G.box(8, 0, 18, 10), -1.0)],
}


@needs_pyclipr
@pytest.mark.parametrize("name", list(_CASES))
def test_clipper_matches_shapely(name):
    pieces = _CASES[name]
    got = cf.clipper_union_all(pieces, grid_size=GRID)
    ref = shapely.union_all(pieces, grid_size=GRID)
    assert abs(got.area - ref.area) < 1e-6, (name, got.area, ref.area)
    assert got.symmetric_difference(ref).area < 1e-6, name


@needs_pyclipr
def test_verify_mode_uses_clipper_when_matching(monkeypatch):
    monkeypatch.setenv("FYPA_FUSE_BACKEND", "verify")
    pieces = [G.box(0, 0, 10, 10), G.box(5, 5, 15, 15)]
    got = cf.fuse(pieces, GRID)
    ref = shapely.union_all(pieces, grid_size=GRID)
    assert abs(got.area - ref.area) < 1e-6


@needs_pyclipr
def test_empty_and_degenerate_inputs():
    assert cf.clipper_union_all([], grid_size=GRID).is_empty
    assert cf.clipper_union_all([G.Polygon()], grid_size=GRID).is_empty
