"""Analytical benchmark tests for the FEM solver.

These pin solver output against closed-form physics. A uniform copper strip
carrying a known current has a potential field that, away from the injection
points, is linear in the length coordinate with slope

    dV/dx  =  -I / (G * W)

where ``G`` is the sheet conductance (S) and ``W`` the strip width (mm).
Measuring the slope over the strip *interior* isolates the bulk sheet
resistance from the 2-D point-source singularity at the injection terminals.

P1 (linear) elements represent a linear field exactly, on any mesh, so the
interior slope matches theory to solver precision (measured < 1e-6 at every
mesh size). An earlier version of this file allowed 8 % and described a
"~5 % point-injection bias"; that bias came from an unsigned cotangent in the
stiffness matrix, long since fixed (see ``_half_cotangent``). Point injection
only perturbs the field near the terminals, never the interior. The
end-to-end and geometry checks against closed forms live in
``test_validation.py``.
"""
from __future__ import annotations

import pytest

# The interior field is linear, which P1 elements reproduce exactly.
SLOPE_ABS_TOL = 1e-5
SLOPE_REL_TOL = 1e-5


@pytest.mark.parametrize(
    ("length_mm", "width_mm"),
    [
        (40.0, 6.0),    # baseline
        (80.0, 6.0),    # longer trace
        (40.0, 3.0),    # narrower trace
        (40.0, 10.0),   # wider pour
    ],
)
def test_strip_interior_slope_matches_sheet_resistance(strip_solver, length_mm, width_mm):
    result = strip_solver(length_mm=length_mm, width_mm=width_mm, mesh_size=0.3)

    measured = result.interior_slope()
    analytical = result.expected["slope_v_per_mm"]
    rel_err = abs(measured - analytical) / analytical

    assert rel_err < SLOPE_ABS_TOL, (
        f"strip {length_mm}x{width_mm} mm: interior dV/dx {measured:.4e} V/mm "
        f"deviates {rel_err:.1e} from analytical {analytical:.4e} V/mm"
    )


@pytest.mark.parametrize("mesh_size", [0.6, 0.2])
def test_interior_slope_is_exact_at_any_mesh_size(strip_solver, mesh_size):
    """The linear interior field is exact whatever the element size, so a
    coarse mesh must be as right as a fine one here (a mesh-dependent error
    in the interior would mean a broken stiffness matrix, not resolution)."""
    result = strip_solver(length_mm=40.0, width_mm=3.0, mesh_size=mesh_size)
    measured = result.interior_slope()
    analytical = result.expected["slope_v_per_mm"]
    assert measured == pytest.approx(analytical, rel=SLOPE_ABS_TOL)


def test_slope_scales_linearly_with_current(strip_solver):
    """Doubling the injected current doubles the potential gradient (exact)."""
    base = strip_solver(current_a=1.0)
    doubled = strip_solver(current_a=2.0)

    ratio = doubled.interior_slope() / base.interior_slope()
    assert ratio == pytest.approx(2.0, rel=SLOPE_REL_TOL)


def test_slope_inversely_proportional_to_conductance(strip_solver):
    """Halving the sheet conductance doubles the potential gradient (exact)."""
    base = strip_solver(conductance_s=1000.0)
    halved = strip_solver(conductance_s=500.0)

    ratio = halved.interior_slope() / base.interior_slope()
    assert ratio == pytest.approx(2.0, rel=SLOPE_REL_TOL)
