"""The padne viewer's spatial indices are built from the meshes' flat arrays
(no per-vertex / per-face Python loop) and give the same points, values, and
nearest-lookups as the old stub-walking implementation.
"""
from __future__ import annotations

import numpy as np
import pytest
import shapely.geometry

pytest.importorskip("OpenGL")
pytest.importorskip("PySide6")

from pdnsolver import mesh as M  # noqa: E402
from pdnsolver import solver as S  # noqa: E402
from pdnsolver.ui import VertexSpatialIndex, FaceSpatialIndex  # noqa: E402


def _small_layer_solution():
    # Two triangles forming a unit square.
    pts = [M.Point(0.0, 0.0), M.Point(1.0, 0.0),
           M.Point(1.0, 1.0), M.Point(0.0, 1.0)]
    tris = [(0, 1, 2), (0, 2, 3)]
    msh = M.Mesh.from_triangle_soup(pts, tris)

    potentials = M.ZeroForm(msh)
    potentials.values[:] = [0.5, 1.5, 2.5, 3.5]
    powers = M.TwoForm(msh)
    powers.values[:] = [10.0, 20.0]

    layer = S.problem.Layer(
        shape=shapely.geometry.MultiPolygon(
            [shapely.geometry.box(0.0, 0.0, 1.0, 1.0)]),
        name="sq", conductance=1.0)
    ls = S.LayerSolution(meshes=[msh], potentials=[potentials],
                         power_densities=[powers])
    return layer, ls, msh


def test_vertex_index_matches_stub_walk():
    layer, ls, msh = _small_layer_solution()
    pts, vals = VertexSpatialIndex._extract_points_and_values(ls)
    # Reference: the old per-vertex walk.
    ref_pts = np.array([[v.p.x, v.p.y] for v in msh.vertices])
    ref_vals = np.array([ls.potentials[0][v] for v in msh.vertices])
    assert np.array_equal(pts, ref_pts)
    assert np.array_equal(vals, ref_vals)


def test_face_index_centroids_and_values():
    layer, ls, msh = _small_layer_solution()
    pts, vals = FaceSpatialIndex._extract_points_and_values(ls)
    # Independent reference: centroid = mean of each triangle's 3 vertices.
    # (The old face.centroid walk needed the half-edge graph, which
    # from_triangle_soup doesn't build — so the flat-array path is also more
    # robust, not just faster.)
    vpts = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    tris = np.array([[0, 1, 2], [0, 2, 3]])
    ref_pts = vpts[tris].mean(axis=1)
    assert np.allclose(pts, ref_pts)
    assert np.array_equal(vals, np.array([10.0, 20.0]))


def test_query_nearest_uses_prepared_contains():
    layer, ls, _ = _small_layer_solution()
    idx = VertexSpatialIndex.from_layer_data(layer, ls)
    assert idx.prepared is not None
    # A point inside the square returns the nearest vertex value...
    assert idx.query_nearest(0.05, 0.05) == pytest.approx(0.5)
    assert idx.query_nearest(0.95, 0.05) == pytest.approx(1.5)
    # ...and a point outside the copper returns None.
    assert idx.query_nearest(5.0, 5.0) is None


def test_empty_layer_solution():
    layer = S.problem.Layer(
        shape=shapely.geometry.MultiPolygon(), name="empty", conductance=1.0)
    ls = S.LayerSolution(meshes=[], potentials=[], power_densities=[])
    idx = VertexSpatialIndex.from_layer_data(layer, ls)
    assert idx.tree is None
    assert idx.query_nearest(0.0, 0.0) is None
