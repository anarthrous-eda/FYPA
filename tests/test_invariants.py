"""Solver invariant tests.

These assert properties that must hold for *any* well-posed solve, independent
of the specific geometry - the residual is small, the system is balanced, the
field is finite, and repeated solves are deterministic.
"""
from __future__ import annotations

import numpy as np


def test_residual_norm_is_negligible(solved_strip):
    """The direct solve must satisfy L * v = r to near machine precision.

    A small residual is exactly the statement of discrete current conservation
    (KCL at every mesh node), so this single check also guards conservation.
    """
    # RHS scale for a 1 A injection is O(1); a converged direct solve lands
    # near 1e-12. 1e-7 is a generous gate that still catches a broken solve.
    assert solved_strip.solver_info.residual_norm < 1e-7


def test_ground_node_current_is_balanced(solved_strip):
    """Current in == current out, so the reference node sources nothing."""
    assert abs(solved_strip.solver_info.ground_node_current) < 1e-9


def test_potential_field_is_finite(solved_strip):
    """No NaN/inf may leak from degenerate triangles or singular pivots."""
    assert np.all(np.isfinite(solved_strip.potentials))


def test_potential_field_is_nontrivial(solved_strip):
    """A 1 A current through a real copper strip must produce a sane IR drop.

    For the 40 x 6 mm strip the end-to-end drop is on the order of a few mV;
    the band below simply rejects a collapsed (all-equal) or wildly wrong field.
    """
    spread = float(solved_strip.potentials.max() - solved_strip.potentials.min())
    assert 1e-4 < spread < 5e-2, f"implausible potential spread: {spread:.4e} V"


def test_solve_is_deterministic(strip_solver):
    """Re-solving identical input must give an identical field.

    Parallel meshing and threaded Laplacian assembly must not introduce
    run-to-run variation, or the golden-file regression tests become flaky.
    """
    first = strip_solver(length_mm=30.0, width_mm=5.0)
    second = strip_solver(length_mm=30.0, width_mm=5.0)

    assert first.potentials.shape == second.potentials.shape
    np.testing.assert_array_equal(first.potentials, second.potentials)
