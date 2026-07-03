"""Unsymmetric PARDISO fallback path (`_pardiso_solve_unsym`).

Used when the MNA matrix is asymmetric (a `VoltageRegulator`) or the symmetric
solve is rejected. It builds a fresh solver and frees its factorisation
immediately (the old `_pypardiso.spsolve` kept a module-global LU resident for
the process lifetime).

The direct test guards the CSC→CSR handling: passing a CSC matrix straight to
PyPardisoSolver takes its transposed-solve path and returns the WRONG answer, so
we must feed CSR (as `spsolve` does). A residual-only end-to-end test wouldn't
catch that — `_solve_robust` would silently fall back to SuperLU — so we check
the function directly against a known solution, plus an end-to-end regulator
solve for integration coverage.
"""
from __future__ import annotations

import numpy as np
import pytest
import scipy.sparse
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import problem as P
from pdnsolver import solver as S

pytest.importorskip("pypardiso")

_CU = 5.95e4 * 0.035


def test_pardiso_unsym_matches_known_solution():
    if not S._HAVE_PARDISO:
        pytest.skip("PARDISO not available")
    rng = np.random.default_rng(0)
    n = 300
    A = (scipy.sparse.random(n, n, density=0.04, rng=rng, format="csc")
         + scipy.sparse.eye(n) * 5.0).tocsc()   # diagonally dominant, nonsingular
    x_true = rng.standard_normal(n)
    b = A @ x_true
    x = S._pardiso_solve_unsym(A, b)
    assert np.abs(x - x_true).max() < 1e-9


def _strip(x0, y0, x1, y1, name):
    return P.Layer(shape=MultiPolygon([box(x0, y0, x1, y1)]),
                   name=name, conductance=_CU)


def test_voltage_regulator_solves_via_unsym_path():
    """A VoltageRegulator makes the matrix asymmetric → the unsymmetric PARDISO
    path is the primary solve. The system must still solve cleanly."""
    rail = _strip(0, 0, 30, 5, "rail")
    gnd = _strip(0, 10, 30, 15, "gnd")
    vp, vn, sf, st = (P.NodeID() for _ in range(4))
    vreg = P.Network(
        connections=[
            P.Connection(layer=rail, point=Point(1, 2.5), node_id=sf),
            P.Connection(layer=gnd, point=Point(1, 12.5), node_id=st),
            P.Connection(layer=rail, point=Point(29, 2.5), node_id=vp),
            P.Connection(layer=gnd, point=Point(29, 12.5), node_id=vn),
        ],
        elements=[P.VoltageRegulator(v_p=vp, v_n=vn, s_f=sf, s_t=st,
                                     voltage=3.3, gain=10.0)],
    )
    lf, lt = P.NodeID(), P.NodeID()
    load = P.Network(
        connections=[
            P.Connection(layer=rail, point=Point(29, 2.5), node_id=lf),
            P.Connection(layer=gnd, point=Point(29, 12.5), node_id=lt),
        ],
        elements=[P.CurrentSource(f=lf, t=lt, current=1.0)],
    )
    sol = S.solve(P.Problem(layers=[rail, gnd], networks=[vreg, load],
                            project_name="vreg"))
    assert sol.solver_info.residual_norm < 1e-6
    for ls in sol.layer_solutions:
        for zf in ls.potentials:
            assert np.all(np.isfinite(np.asarray(zf.values, np.float64)))
