"""Tests for FEM mesh failure reporting and geometry guards."""

import numpy as np
import shapely.geometry

from fypa.altium_viewer import (
    _mesh_failure_outline_rings,
)
from fypa.altium.loader import _filter_tiny_pieces
from pdnsolver.mesh import (
    MeshingException,
    _dedupe_ring_coords,
    _humanize_triangle_error,
    _parse_triangle_location,
    repair_polygon_for_triangulation,
)


def test_parse_triangle_precision_location():
    msg = (
        "Error:  Ran out of precision at (12.491, 36.196).\n"
        "I attempted to split a segment to a smaller size than"
    )
    assert _parse_triangle_location(msg) == (12.491, 36.196)


def test_humanize_invalid_geometry_message():
    cause = _humanize_triangle_error(
        "Triangulation failed -- probably because of invalid geometry on input.",
    )
    assert "invalid" in cause.lower()
    assert "self-intersect" in cause.lower() or "degenerate" in cause.lower()


def test_meshing_exception_user_message_includes_layer_and_location():
    exc = MeshingException(
        "triangle.triangulate failed: bad",
        layer_name="Top|+3V3",
        geom_index=2,
        area_mm2=0.05,
        bounds=(12.0, 35.0, 13.0, 37.0),
        location_xy=(12.491, 36.196),
        triangle_cause=_humanize_triangle_error("invalid geometry on input"),
    )
    text = exc.format_user_message()
    assert "Top|+3V3" in text
    assert "island #3" in text
    assert "12.491" in text
    assert "36.196" in text


def test_repair_polygon_welds_near_duplicate_vertices():
    # Two nearly-coincident vertices on a skinny rectangle.
    poly = shapely.geometry.Polygon([
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 1e-5),
        (10.0 + 1e-7, 1.0),
        (0.0, 1.0),
    ])
    repaired = repair_polygon_for_triangulation(poly)
    assert not repaired.is_empty
    assert repaired.is_valid


def test_filter_tiny_pieces_drops_unanchored_slivers():
    big = shapely.geometry.box(0, 0, 10, 10)
    tiny = shapely.geometry.box(20, 20, 20.0005, 20.0005)
    shape = shapely.geometry.MultiPolygon([big, tiny])
    kept, dropped = _filter_tiny_pieces(shape, 1e-4, [], [])
    assert dropped == [tiny]
    assert kept.area == big.area


def test_mesh_failure_outline_uses_local_marker_not_whole_pour():
    huge = shapely.geometry.box(0, 0, 200, 150)
    rec = {
        "location_xy": [12.491, 36.196],
        "exterior": np.asarray(huge.exterior.coords[:-1], dtype=np.float32),
    }
    rings = _mesh_failure_outline_rings(rec)
    assert len(rings) >= 2  # inner + outer circle
    # Must not include the 200 mm pour outline.
    max_span = max(
        max(x for x, _y in ring) - min(x for x, _y in ring)
        for ring in rings
    )
    assert max_span < 20.0


def test_dedupe_ring_coords_collapses_coincident_vertices():
    coords = np.asarray([
        [0.0, 0.0],
        [1e-7, 0.0],
        [10.0, 0.0],
        [10.0, 10.0],
        [0.0, 10.0],
    ], dtype=np.float64)
    out = _dedupe_ring_coords(coords, 1e-4)
    assert out.shape[0] == 4


def test_filter_tiny_pieces_returns_multipolygon_for_single_piece():
    poly = shapely.geometry.box(0, 0, 10, 10)
    kept, dropped = _filter_tiny_pieces(poly, 1e-4, [], [])
    assert dropped == []
    assert kept.geom_type == "MultiPolygon"
    assert len(kept.geoms) == 1


def test_filter_tiny_pieces_keeps_anchored_sliver_with_pin():
    tiny = shapely.geometry.box(0, 0, 0.0005, 0.0005)
    kept, dropped = _filter_tiny_pieces(
        tiny, 1e-4, [(0.0001, 0.0001)], [],
    )
    assert dropped == []
    assert not kept.is_empty
