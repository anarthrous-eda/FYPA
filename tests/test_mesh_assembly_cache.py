"""Cached mesh + Laplacian assembly: reuse is correct, gated, and freeable.

``solve()`` caches the meshes and their cotangent-Laplacian triples (the
dominant cost) keyed on a content fingerprint of the geometry, per-layer
conductance, connection seeds, and mesher config — everything that determines
them, and nothing else. A value-only re-solve (only source/sink magnitudes
changed) hashes identically and skips re-meshing + re-assembling the Laplacian;
a geometry or conductance change misses and rebuilds. These tests check that
the reused path gives the right answer (a linear system scales with its RHS and
matches a from-scratch solve), that the fingerprint tracks exactly the mesh-
determining inputs, and that ``free_mesh_assembly_cache`` resets cleanly.
"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import mesh as MESH
from pdnsolver import problem as P
from pdnsolver import solver as S

_CU = 5.95e4 * 0.035


def _strip_problem(current: float, *, length: float = 40.0,
                   conductance: float = _CU) -> P.Problem:
    """One copper strip, full-width end pads, a CurrentSource of `current` A.
    The meshes + Laplacian depend on `length` and `conductance` but never on
    `current` (it only enters the RHS), so two problems that differ only in
    `current` share one cached assembly."""
    layer = P.Layer(shape=MultiPolygon([box(0.0, 0.0, length, 4.0)]),
                    name="strip", conductance=conductance)
    src, snk = P.NodeID(), P.NodeID()
    net = P.Network(
        connections=[
            P.Connection(layer=layer, point=Point(0.01, 2.0), node_id=src,
                         region=box(0.0, 0.0, 0.5, 4.0)),
            P.Connection(layer=layer, point=Point(length - 0.01, 2.0),
                         node_id=snk,
                         region=box(length - 0.5, 0.0, length, 4.0)),
        ],
        elements=[P.CurrentSource(f=src, t=snk, current=current)],
    )
    return P.Problem(layers=[layer], networks=[net], project_name="strip")


def _potential_range(sol) -> float:
    vals = np.concatenate([np.asarray(zf.values, np.float64)
                           for ls in sol.layer_solutions for zf in ls.potentials])
    return float(vals.max() - vals.min())


@pytest.fixture(autouse=True)
def _clean_caches():
    S.free_mesh_assembly_cache()
    S.free_pardiso_cache()
    yield
    S.free_mesh_assembly_cache()
    S.free_pardiso_cache()


def test_fingerprint_ignores_source_magnitude():
    """Only source/sink magnitude differs → identical fingerprint (RHS-only)."""
    cfg = MESH.Mesher(None).config
    assert (S._mesh_assembly_fingerprint(_strip_problem(1.0), cfg)
            == S._mesh_assembly_fingerprint(_strip_problem(7.0), cfg))


def test_fingerprint_tracks_geometry_and_conductance():
    """Geometry or conductance changes the meshes/Laplacian → different hash."""
    cfg = MESH.Mesher(None).config
    base = S._mesh_assembly_fingerprint(_strip_problem(1.0), cfg)
    assert S._mesh_assembly_fingerprint(
        _strip_problem(1.0, length=60.0), cfg) != base
    assert S._mesh_assembly_fingerprint(
        _strip_problem(1.0, conductance=_CU * 2), cfg) != base


def test_value_only_resolve_reuses_assembly_and_scales():
    """A value-only re-solve reuses the cached assembly verbatim and, because
    the matrix is then bit-identical, RHS ×3 → response ×3."""
    sol1 = S.solve(_strip_problem(1.0))
    cached_first = S._mesh_assembly_cache
    assert cached_first is not None, "cache not populated after first solve"

    sol3 = S.solve(_strip_problem(3.0))
    cached_second = S._mesh_assembly_cache
    # Same fingerprint → the cache object is reused, not rebuilt/replaced.
    assert cached_second is cached_first

    dv1 = _potential_range(sol1)
    dv3 = _potential_range(sol3)
    assert dv1 > 0
    assert dv3 == pytest.approx(3.0 * dv1, rel=1e-7)


def test_cached_resolve_matches_fresh_resolve():
    """The cached value-only re-solve equals a from-scratch solve of the same
    problem — reuse changes speed, never the result."""
    S.solve(_strip_problem(1.0))            # populate the cache
    sol_cached = S.solve(_strip_problem(2.5))   # reuse it (value-only)

    S.free_mesh_assembly_cache()
    sol_fresh = S.solve(_strip_problem(2.5))     # fresh mesh + assembly

    assert _potential_range(sol_cached) == pytest.approx(
        _potential_range(sol_fresh), rel=1e-9)


def test_geometry_change_rebuilds_assembly():
    """A different board misses the cache and replaces it, still solving right."""
    S.solve(_strip_problem(1.0, length=40.0))
    first = S._mesh_assembly_cache
    assert first is not None

    sol = S.solve(_strip_problem(1.0, length=60.0))
    second = S._mesh_assembly_cache
    assert second is not first
    assert second.fingerprint != first.fingerprint
    assert _potential_range(sol) > 0


def test_conductance_change_rebuilds_assembly():
    """Same geometry, changed conductance (a stackup edit) rebuilds — the
    Laplacian scales with conductance, so the cached triples can't be reused."""
    S.solve(_strip_problem(1.0))
    first = S._mesh_assembly_cache
    S.solve(_strip_problem(1.0, conductance=_CU * 2))
    second = S._mesh_assembly_cache
    assert second.fingerprint != first.fingerprint


def test_free_mesh_assembly_cache_resets_and_resolves():
    """Dropping the cache must not break the next solve."""
    sol_a = S.solve(_strip_problem(2.0))
    S.free_mesh_assembly_cache()
    assert S._mesh_assembly_cache is None
    sol_b = S.solve(_strip_problem(2.0))  # re-meshes from scratch

    assert _potential_range(sol_a) == pytest.approx(_potential_range(sol_b),
                                                    rel=1e-9)


def test_free_mesh_assembly_cache_is_safe_when_empty():
    """Calling the free hook with nothing cached is a no-op, never raises."""
    S.free_mesh_assembly_cache()
    S.free_mesh_assembly_cache()
