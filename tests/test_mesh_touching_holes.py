"""Regression: copper whose repaired outline has holes touching the exterior.

An Altium board (manufactured), "Top Layer|GND" piece around a
2x8 connector: the pour outline is one ring that touches itself between
the pads (zero-width copper pinches). ``buffer(0)`` repairs it into a
valid polygon with 14 holes, each touching the exterior at one point.
Both rings listed that point, so Triangle received two coincident input
vertices; it discards one while the segments still reference it, and the
worker died with a segfault (intermittent, ~90 % of runs on raw arrays).
The preparation must hand Triangle each boundary point exactly once.
"""

import numpy as np
import scipy.spatial
import shapely.geometry

from pdnsolver.mesh import (
    _MESH_VERTEX_SIMPLIFY_TOL_MM,
    _prepare_polygon_for_triangle_arrays,
    _triangulate_adaptive,
    _triangulate_arrays,
    repair_polygon_for_triangulation,
)

# Exterior ring of the copper piece as the loader builds it (mm, invalid:
# self-touching at 14 points between the connector pads).
PINCHED_GND_RING = [
    (-1.093, -1.138), (-1.046, -1.124), (-1.002, -1.1), (-0.963, -1.069), (-0.932, -1.03),
    (-0.908, -0.986), (-0.894, -0.939), (-0.889, -0.889), (-0.894, -0.839), (-0.908, -0.792),
    (-0.927, -0.757), (-0.927, 0.757), (-0.908, 0.792), (-0.894, 0.839), (-0.889, 0.889),
    (-0.894, 0.939), (-0.908, 0.986), (-0.932, 1.03), (-0.963, 1.069), (-1.002, 1.1),
    (-1.046, 1.124), (-1.093, 1.138), (-1.143, 1.143), (-1.193, 1.138), (-1.24, 1.124),
    (-1.284, 1.1), (-1.323, 1.069), (-1.354, 1.03), (-1.378, 0.986), (-1.392, 0.939),
    (-1.397, 0.889), (-1.392, 0.839), (-1.38, 0.8), (-1.414, 0.8), (-1.402, 0.839),
    (-1.397, 0.889), (-1.402, 0.939), (-1.416, 0.986), (-1.44, 1.03), (-1.471, 1.069),
    (-1.51, 1.1), (-1.554, 1.124), (-1.601, 1.138), (-1.651, 1.143), (-1.701, 1.138),
    (-1.748, 1.124), (-1.792, 1.1), (-1.831, 1.069), (-1.862, 1.03), (-1.886, 0.986),
    (-1.9, 0.939), (-1.905, 0.889), (-1.9, 0.839), (-1.888, 0.8), (-1.922, 0.8),
    (-1.91, 0.839), (-1.905, 0.889), (-1.91, 0.939), (-1.924, 0.986), (-1.948, 1.03),
    (-1.979, 1.069), (-2.018, 1.1), (-2.062, 1.124), (-2.109, 1.138), (-2.159, 1.143),
    (-2.209, 1.138), (-2.256, 1.124), (-2.3, 1.1), (-2.339, 1.069), (-2.37, 1.03),
    (-2.394, 0.986), (-2.408, 0.939), (-2.413, 0.889), (-2.408, 0.839), (-2.396, 0.8),
    (-2.43, 0.8), (-2.418, 0.839), (-2.413, 0.889), (-2.418, 0.939), (-2.432, 0.986),
    (-2.456, 1.03), (-2.487, 1.069), (-2.526, 1.1), (-2.57, 1.124), (-2.617, 1.138),
    (-2.667, 1.143), (-2.717, 1.138), (-2.764, 1.124), (-2.808, 1.1), (-2.847, 1.069),
    (-2.878, 1.03), (-2.902, 0.986), (-2.916, 0.939), (-2.921, 0.889), (-2.916, 0.839),
    (-2.904, 0.8), (-2.938, 0.8), (-2.926, 0.839), (-2.921, 0.889), (-2.926, 0.939),
    (-2.94, 0.986), (-2.964, 1.03), (-2.995, 1.069), (-3.034, 1.1), (-3.078, 1.124),
    (-3.125, 1.138), (-3.175, 1.143), (-3.225, 1.138), (-3.272, 1.124), (-3.316, 1.1),
    (-3.355, 1.069), (-3.386, 1.03), (-3.41, 0.986), (-3.424, 0.939), (-3.429, 0.889),
    (-3.424, 0.839), (-3.412, 0.8), (-3.446, 0.8), (-3.434, 0.839), (-3.429, 0.889),
    (-3.434, 0.939), (-3.448, 0.986), (-3.472, 1.03), (-3.503, 1.069), (-3.542, 1.1),
    (-3.586, 1.124), (-3.633, 1.138), (-3.683, 1.143), (-3.733, 1.138), (-3.78, 1.124),
    (-3.824, 1.1), (-3.863, 1.069), (-3.894, 1.03), (-3.918, 0.986), (-3.932, 0.939),
    (-3.937, 0.889), (-3.932, 0.839), (-3.92, 0.8), (-3.954, 0.8), (-3.942, 0.839),
    (-3.937, 0.889), (-3.942, 0.939), (-3.956, 0.986), (-3.98, 1.03), (-4.011, 1.069),
    (-4.05, 1.1), (-4.094, 1.124), (-4.141, 1.138), (-4.191, 1.143), (-4.241, 1.138),
    (-4.288, 1.124), (-4.332, 1.1), (-4.371, 1.069), (-4.402, 1.03), (-4.426, 0.986),
    (-4.44, 0.939), (-4.445, 0.889), (-4.44, 0.839), (-4.428, 0.8), (-4.462, 0.8),
    (-4.45, 0.839), (-4.445, 0.889), (-4.45, 0.939), (-4.464, 0.986), (-4.488, 1.03),
    (-4.519, 1.069), (-4.558, 1.1), (-4.602, 1.124), (-4.649, 1.138), (-4.699, 1.143),
    (-4.749, 1.138), (-4.796, 1.124), (-4.84, 1.1), (-4.879, 1.069), (-4.91, 1.03),
    (-4.934, 0.986), (-4.948, 0.939), (-4.953, 0.889), (-4.948, 0.839), (-4.934, 0.792),
    (-4.927, 0.779), (-4.927, -0.779), (-4.934, -0.792), (-4.948, -0.839), (-4.953, -0.889),
    (-4.948, -0.939), (-4.934, -0.986), (-4.91, -1.03), (-4.879, -1.069), (-4.84, -1.1),
    (-4.796, -1.124), (-4.749, -1.138), (-4.699, -1.143), (-4.649, -1.138), (-4.602, -1.124),
    (-4.558, -1.1), (-4.519, -1.069), (-4.488, -1.03), (-4.464, -0.986), (-4.45, -0.939),
    (-4.445, -0.889), (-4.45, -0.839), (-4.462, -0.8), (-4.428, -0.8), (-4.44, -0.839),
    (-4.445, -0.889), (-4.44, -0.939), (-4.426, -0.986), (-4.402, -1.03), (-4.371, -1.069),
    (-4.332, -1.1), (-4.288, -1.124), (-4.241, -1.138), (-4.191, -1.143), (-4.141, -1.138),
    (-4.094, -1.124), (-4.05, -1.1), (-4.011, -1.069), (-3.98, -1.03), (-3.956, -0.986),
    (-3.942, -0.939), (-3.937, -0.889), (-3.942, -0.839), (-3.954, -0.8), (-3.92, -0.8),
    (-3.932, -0.839), (-3.937, -0.889), (-3.932, -0.939), (-3.918, -0.986), (-3.894, -1.03),
    (-3.863, -1.069), (-3.824, -1.1), (-3.78, -1.124), (-3.733, -1.138), (-3.683, -1.143),
    (-3.633, -1.138), (-3.586, -1.124), (-3.542, -1.1), (-3.503, -1.069), (-3.472, -1.03),
    (-3.448, -0.986), (-3.434, -0.939), (-3.429, -0.889), (-3.434, -0.839), (-3.446, -0.8),
    (-3.412, -0.8), (-3.424, -0.839), (-3.429, -0.889), (-3.424, -0.939), (-3.41, -0.986),
    (-3.386, -1.03), (-3.355, -1.069), (-3.316, -1.1), (-3.272, -1.124), (-3.225, -1.138),
    (-3.175, -1.143), (-3.125, -1.138), (-3.078, -1.124), (-3.034, -1.1), (-2.995, -1.069),
    (-2.964, -1.03), (-2.94, -0.986), (-2.926, -0.939), (-2.921, -0.889), (-2.926, -0.839),
    (-2.938, -0.8), (-2.904, -0.8), (-2.916, -0.839), (-2.921, -0.889), (-2.916, -0.939),
    (-2.902, -0.986), (-2.878, -1.03), (-2.847, -1.069), (-2.808, -1.1), (-2.764, -1.124),
    (-2.717, -1.138), (-2.667, -1.143), (-2.617, -1.138), (-2.57, -1.124), (-2.526, -1.1),
    (-2.487, -1.069), (-2.456, -1.03), (-2.432, -0.986), (-2.418, -0.939), (-2.413, -0.889),
    (-2.418, -0.839), (-2.43, -0.8), (-2.396, -0.8), (-2.408, -0.839), (-2.413, -0.889),
    (-2.408, -0.939), (-2.394, -0.986), (-2.37, -1.03), (-2.339, -1.069), (-2.3, -1.1),
    (-2.256, -1.124), (-2.209, -1.138), (-2.159, -1.143), (-2.109, -1.138), (-2.062, -1.124),
    (-2.018, -1.1), (-1.979, -1.069), (-1.948, -1.03), (-1.924, -0.986), (-1.91, -0.939),
    (-1.905, -0.889), (-1.91, -0.839), (-1.922, -0.8), (-1.888, -0.8), (-1.9, -0.839),
    (-1.905, -0.889), (-1.9, -0.939), (-1.886, -0.986), (-1.862, -1.03), (-1.831, -1.069),
    (-1.792, -1.1), (-1.748, -1.124), (-1.701, -1.138), (-1.651, -1.143), (-1.601, -1.138),
    (-1.554, -1.124), (-1.51, -1.1), (-1.471, -1.069), (-1.44, -1.03), (-1.416, -0.986),
    (-1.402, -0.939), (-1.397, -0.889), (-1.402, -0.839), (-1.414, -0.8), (-1.38, -0.8),
    (-1.392, -0.839), (-1.397, -0.889), (-1.392, -0.939), (-1.378, -0.986), (-1.354, -1.03),
    (-1.323, -1.069), (-1.284, -1.1), (-1.24, -1.124), (-1.193, -1.138), (-1.143, -1.143),
]

# Directive / via seeds on the first pad (as the solver passes them).
PINCHED_SEEDS = np.array([
    (-1.143, -0.889), (-1.118, -0.889), (-1.12532233, -0.87132233),
    (-1.143, -0.864), (-1.16067767, -0.87132233), (-1.168, -0.889),
    (-1.16067767, -0.90667767), (-1.143, -0.914), (-1.12532233, -0.90667767),
])

# Switches the solver used for this piece (width-aware max size).
PINCHED_SWITCHES = "pq20a0.0140594Q"


def _mesh_area(vertices: np.ndarray, triangles: np.ndarray) -> float:
    p = vertices[triangles]
    d1 = p[:, 1] - p[:, 0]
    d2 = p[:, 2] - p[:, 0]
    return float(0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]).sum())


def _assert_no_coincident_vertices(vertices: np.ndarray) -> None:
    pairs = scipy.spatial.cKDTree(vertices).query_pairs(
        _MESH_VERTEX_SIMPLIFY_TOL_MM)
    assert not pairs, f"coincident Triangle input vertices: {sorted(pairs)[:5]}"


def _assert_segments_valid(vertices: np.ndarray, segments: np.ndarray) -> None:
    assert segments.min() >= 0 and segments.max() < vertices.shape[0]
    assert np.all(segments[:, 0] != segments[:, 1])


def test_altium_gnd_pinched_pour_prepares_without_duplicate_vertices():
    poly = shapely.geometry.Polygon(PINCHED_GND_RING)
    assert not poly.is_valid  # the loader hands the mesher this ring
    repaired = repair_polygon_for_triangulation(poly)
    assert repaired.geom_type == "Polygon"
    assert len(repaired.interiors) == 14
    vertices, segments, holes = _prepare_polygon_for_triangle_arrays(
        poly, PINCHED_SEEDS)
    # Checked BEFORE triangulating: a regression here would segfault Triangle.
    _assert_no_coincident_vertices(vertices)
    _assert_segments_valid(vertices, segments)
    assert holes.shape == (14, 2)


def test_altium_gnd_pinched_pour_meshes_and_keeps_its_area():
    poly = shapely.geometry.Polygon(PINCHED_GND_RING)
    copper_area = repair_polygon_for_triangulation(poly).area
    assert abs(copper_area - 8.718776) < 1e-6
    vertices, segments, holes = _prepare_polygon_for_triangle_arrays(
        poly, PINCHED_SEEDS)
    _assert_no_coincident_vertices(vertices)
    out_v, out_t = _triangulate_arrays(
        vertices, segments, holes, PINCHED_SWITCHES)
    assert out_t.shape[0] > 0
    assert abs(_mesh_area(out_v, out_t) - copper_area) < 1e-9 * max(1.0, copper_area)
    # Every seed is still a mesh node.
    d, _ = scipy.spatial.cKDTree(out_v).query(PINCHED_SEEDS)
    assert d.max() < 1e-9


def test_altium_gnd_pinched_pour_meshes_adaptive():
    poly = shapely.geometry.Polygon(PINCHED_GND_RING)
    copper_area = repair_polygon_for_triangulation(poly).area
    out_v, out_t = _triangulate_adaptive(
        poly, PINCHED_SEEDS, (20.0, 0.6, 4.0, 0.75, 5.0))
    assert abs(_mesh_area(out_v, out_t) - copper_area) < 1e-9 * max(1.0, copper_area)


def test_hole_touching_exterior_at_a_point_is_one_vertex():
    # 10 x 10 square with a triangular hole whose apex touches the bottom
    # edge's vertex at (5, 0) — a valid polygon, shared point in two rings.
    shell = [(0, 0), (5, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(5, 0), (6, 3), (4, 3)]
    poly = shapely.geometry.Polygon(shell, [hole])
    assert poly.is_valid
    seeds = np.array([(5.0, 0.0), (2.0, 2.0), (2.0, 2.0)])  # on a vertex, dup
    vertices, segments, holes = _prepare_polygon_for_triangle_arrays(poly, seeds)
    _assert_no_coincident_vertices(vertices)
    _assert_segments_valid(vertices, segments)
    assert vertices.shape[0] == 5 + 3 - 1 + 1
    assert segments.shape[0] == 5 + 3
    out_v, out_t = _triangulate_arrays(vertices, segments, holes, "pq20a1Q")
    assert abs(_mesh_area(out_v, out_t) - poly.area) < 1e-9 * poly.area
