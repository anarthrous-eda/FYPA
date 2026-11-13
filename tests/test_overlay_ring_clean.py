"""Board-outline ring cleaning before ribbon offsetting.

Altium board outlines can carry sub-micron duplicate vertices and a short
back-tracking spur where an arc meets a straight edge (seen on Signaloid
Hermes Mezz). Either gives a vertex a miter bisector along the edge, so
the outline ribbon tapered to nothing along that edge.
"""
import numpy as np

from fypa.viewer.overlays import _clean_closed_ring


def test_drops_duplicates_closing_point_and_spur():
    ring = [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 0.0000001),        # near-duplicate
        (10.0, 5.0),
        (2.0, 5.0),               # spur: runs left...
        (2.06, 5.0),              # ...doubles back right...
        (0.0, 5.0),               # ...then left again to the corner
        (0.0, 0.0),               # explicit closing duplicate
    ]
    out = _clean_closed_ring(ring)
    # No zero-length edges and no hairpin turns remain.
    edges = np.roll(out, -1, axis=0) - out
    elen = np.linalg.norm(edges, axis=1)
    assert elen.min() > 1e-4
    d = edges / elen[:, None]
    cos_turn = np.einsum("ij,ij->i", np.roll(d, 1, axis=0), d)
    assert cos_turn.min() > -0.999
    # The rectangle's four corners survive.
    for corner in ((0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0)):
        assert np.any(np.all(np.isclose(out, corner, atol=1e-6), axis=1))


def test_clean_ring_is_unchanged():
    ring = np.array([(0, 0), (4, 0), (4, 3), (0, 3)], dtype=float)
    np.testing.assert_array_equal(_clean_closed_ring(ring), ring)
