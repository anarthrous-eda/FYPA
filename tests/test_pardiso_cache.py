"""Cached symmetric-PARDISO factorisation: reuse is correct and safe.

``_pardiso_solve_sym`` keeps the factorisation alive between solves and reuses
it when the stiffness matrix is unchanged (e.g. only sink-current magnitudes
changed — those touch the RHS, not L). These tests check that the reused path
gives the right answer (a linear system's response scales with its RHS), that
the cache is actually exercised (same matrix → same fingerprint, no
re-factorisation), and that ``free_pardiso_cache`` resets cleanly.
"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import problem as P
from pdnsolver import solver as S

pytest.importorskip("pypardiso")

_CU = 5.95e4 * 0.035


def _strip_problem(current: float) -> P.Problem:
    """One copper strip, full-width end pads, a CurrentSource of `current` A.
    The stiffness matrix is independent of `current` (it only enters the RHS),
    so two problems that differ only in `current` share one factorisation."""
    layer = P.Layer(shape=MultiPolygon([box(0.0, 0.0, 40.0, 4.0)]),
                    name="strip", conductance=_CU)
    src, snk = P.NodeID(), P.NodeID()
    net = P.Network(
        connections=[
            P.Connection(layer=layer, point=Point(0.01, 2.0), node_id=src,
                         region=box(0.0, 0.0, 0.5, 4.0)),
            P.Connection(layer=layer, point=Point(39.99, 2.0), node_id=snk,
                         region=box(39.5, 0.0, 40.0, 4.0)),
        ],
        elements=[P.CurrentSource(f=src, t=snk, current=current)],
    )
    return P.Problem(layers=[layer], networks=[net], project_name="strip")


def _potential_range(sol) -> float:
    vals = np.concatenate([np.asarray(zf.values, np.float64)
                           for ls in sol.layer_solutions for zf in ls.potentials])
    return float(vals.max() - vals.min())


@pytest.fixture(autouse=True)
def _clean_cache():
    S.free_pardiso_cache()
    yield
    S.free_pardiso_cache()


def test_reused_factorization_scales_with_rhs():
    """Same matrix, RHS ×3 → response ×3. If the reused factorisation were
    wrong the linearity would break."""
    if not S._HAVE_PARDISO:
        pytest.skip("PARDISO not available")

    sol1 = S.solve(_strip_problem(1.0))
    fp_after_first = S._sym_fingerprint
    sol3 = S.solve(_strip_problem(3.0))
    fp_after_second = S._sym_fingerprint

    assert fp_after_first is not None, "cache not populated after first solve"
    # Identical stiffness matrix → identical fingerprint → factorisation reused.
    assert fp_after_second == fp_after_first

    dv1 = _potential_range(sol1)
    dv3 = _potential_range(sol3)
    assert dv1 > 0
    assert dv3 == pytest.approx(3.0 * dv1, rel=1e-9)


def test_free_pardiso_cache_resets_and_resolves():
    """Dropping the cache must not break the next solve."""
    if not S._HAVE_PARDISO:
        pytest.skip("PARDISO not available")

    sol_a = S.solve(_strip_problem(2.0))
    S.free_pardiso_cache()
    assert S._sym_fingerprint is None
    sol_b = S.solve(_strip_problem(2.0))  # re-factorises from scratch

    assert _potential_range(sol_a) == pytest.approx(_potential_range(sol_b),
                                                    rel=1e-9)


def test_free_pardiso_cache_is_safe_when_empty():
    """Calling the free hook with nothing cached is a no-op, never raises."""
    S.free_pardiso_cache()
    S.free_pardiso_cache()
