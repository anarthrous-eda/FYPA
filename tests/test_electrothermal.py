"""Coupled electro-thermal solve: analytic validation and opt-in guarantees.

Copper resistivity rises ~0.39 %/K, so a rail dissipating real power is more
resistive than an isothermal solve assumes — and the error compounds, because
the extra resistance dissipates more power. ``solver.ThermalConfig`` turns on
a fixed-point loop that models this.

A uniform strip is the one geometry where the loop's fixed point can be solved
in closed form, so it pins the physics exactly. With sheet current density
``j = I/W`` [A/mm] the areal power density is ``p = j^2 * Rs(T)`` and the local
rise is ``dT = p * 1e6 / h``, giving

    dT = j^2 * Rs20 * r_th / (1 - j^2 * Rs20 * alpha * r_th),   r_th = 1e6 / h

and a hot resistance of ``R(T) = R20 * (1 + alpha * dT)``.
"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import problem as P
from pdnsolver import solver as S

SIGMA_S_PER_MM = 5.95e4      # copper conductivity
THICKNESS_MM = 0.035         # 1 oz finished copper
_CU = SIGMA_S_PER_MM * THICKNESS_MM      # sheet conductance, S
RS = 1.0 / _CU                           # sheet resistance, ohm/sq
ALPHA = 0.00393                          # /K

# 2 A on a 2 mm-wide 1 oz trace — ~1 A/mm, a realistically warm rail. Far
# above this the closed form runs away (the denominator approaches zero) and
# the trace would not survive in reality either.
WIDTH_MM, LENGTH_MM, PAD_MM, CURRENT_A = 2.0, 100.0, 0.5, 2.0


def _solve_strip(current_a: float = CURRENT_A,
                 thermal: S.ThermalConfig | None = None,
                 width_mm: float = WIDTH_MM):
    """Solve a strip with full-width equipotential end pads; return
    ``(solution, resistance_ohm)``."""
    layer = P.Layer(shape=MultiPolygon([box(0.0, 0.0, LENGTH_MM, width_mm)]),
                    name="strip", conductance=_CU)
    src, snk = P.NodeID(), P.NodeID()
    net = P.Network(
        connections=[
            P.Connection(layer=layer, point=Point(0.01, width_mm / 2),
                         node_id=src, region=box(0.0, 0.0, PAD_MM, width_mm)),
            P.Connection(layer=layer, point=Point(LENGTH_MM - 0.01, width_mm / 2),
                         node_id=snk,
                         region=box(LENGTH_MM - PAD_MM, 0.0, LENGTH_MM, width_mm)),
        ],
        elements=[P.CurrentSource(f=src, t=snk, current=current_a)],
    )
    sol = S.solve(
        P.Problem(layers=[layer], networks=[net], project_name="thermal"),
        thermal=thermal,
    )
    vals = np.concatenate([np.asarray(zf.values, np.float64)
                           for ls in sol.layer_solutions for zf in ls.potentials])
    return sol, float(vals.max() - vals.min()) / current_a


def _analytic(h_w_per_m2k: float, current_a: float = CURRENT_A,
              width_mm: float = WIDTH_MM) -> tuple[float, float]:
    """Closed-form ``(temperature_rise_k, hot_resistance_ohm)``."""
    r_th = 1.0e6 / h_w_per_m2k          # K.mm^2/W
    j = current_a / width_mm            # A/mm
    dt = (j * j * RS * r_th) / (1.0 - j * j * RS * ALPHA * r_th)
    r20 = RS * (LENGTH_MM - 2.0 * PAD_MM) / width_mm
    return dt, r20 * (1.0 + ALPHA * dt)


# --------------------------------------------------------------------------
# Opt-in guarantees: the default path must be untouched.
# --------------------------------------------------------------------------

def test_thermal_defaults_to_off():
    assert S.ThermalConfig().enabled is False


def test_omitting_thermal_is_bit_identical_to_disabling_it():
    """A disabled config must not perturb the solve by even one ULP —
    electro-thermal is opt-in, so every existing result has to be reproduced
    exactly."""
    _, r_default = _solve_strip()
    _, r_disabled = _solve_strip(thermal=S.ThermalConfig(enabled=False))
    assert r_default == r_disabled


def test_disabled_thermal_reports_no_iterations():
    sol, _ = _solve_strip(thermal=S.ThermalConfig(enabled=False))
    info = sol.solver_info
    assert info.thermal_iterations == 0
    assert info.max_temperature_rise_c == 0.0
    assert info.thermal_converged is True


def test_disabled_thermal_leaves_temperature_field_empty():
    sol, _ = _solve_strip()
    for ls in sol.layer_solutions:
        assert ls.temperature_rises == []


# --------------------------------------------------------------------------
# Physics.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("h_w_per_m2k", [20.0, 50.0])
def test_temperature_rise_matches_closed_form(h_w_per_m2k):
    """The converged rise must match the scalar fixed point. Tolerance is
    loose (5 %) only because the FEM's end-pad regions are cooler than the
    uniform interior the closed form assumes."""
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=h_w_per_m2k,
                          alpha_per_c=ALPHA, max_iterations=40,
                          tolerance_c=1e-4)
    sol, _ = _solve_strip(thermal=cfg)
    dt_exact, _ = _analytic(h_w_per_m2k)
    measured = sol.solver_info.max_temperature_rise_c
    rel_err = abs(measured - dt_exact) / dt_exact
    assert rel_err < 0.05, (
        f"h={h_w_per_m2k}: rise {measured:.3f} K deviates {rel_err:.2%} from "
        f"closed form {dt_exact:.3f} K"
    )


@pytest.mark.parametrize("h_w_per_m2k", [20.0, 50.0])
def test_hot_resistance_matches_closed_form(h_w_per_m2k):
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=h_w_per_m2k,
                          alpha_per_c=ALPHA, max_iterations=40,
                          tolerance_c=1e-4)
    _, r_hot = _solve_strip(thermal=cfg)
    _, r_exact = _analytic(h_w_per_m2k)
    rel_err = abs(r_hot - r_exact) / r_exact
    assert rel_err < 0.01, (
        f"h={h_w_per_m2k}: R_hot {r_hot * 1e3:.4f} mohm deviates "
        f"{rel_err:.2%} from closed form {r_exact * 1e3:.4f} mohm"
    )


def test_self_heating_always_raises_resistance():
    """Sign check — the coupling is positive feedback, never negative."""
    _, r_cold = _solve_strip()
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=20.0)
    _, r_hot = _solve_strip(thermal=cfg)
    assert r_hot > r_cold


def test_more_current_means_more_heating():
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=20.0)
    sol_low, r_low = _solve_strip(current_a=0.5, thermal=cfg)
    sol_high, r_high = _solve_strip(current_a=2.0, thermal=cfg)
    assert r_high > r_low
    assert (sol_high.solver_info.max_temperature_rise_c
            > sol_low.solver_info.max_temperature_rise_c)


def test_better_cooling_means_less_heating():
    """Raising the heat-transfer coefficient must cool the board."""
    warm = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=20.0)
    cool = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=200.0)
    sol_warm, r_warm = _solve_strip(thermal=warm)
    sol_cool, r_cool = _solve_strip(thermal=cool)
    assert (sol_cool.solver_info.max_temperature_rise_c
            < sol_warm.solver_info.max_temperature_rise_c)
    assert r_cool < r_warm


def test_loop_converges_and_reports_it():
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=20.0,
                          max_iterations=40, tolerance_c=1e-4)
    sol, _ = _solve_strip(thermal=cfg)
    info = sol.solver_info
    assert info.thermal_converged is True
    assert 0 < info.thermal_iterations < 40


def test_temperature_field_is_populated_and_parallel_to_power_density():
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=20.0)
    sol, _ = _solve_strip(thermal=cfg)
    seen = 0
    for ls in sol.layer_solutions:
        assert len(ls.temperature_rises) == len(ls.power_densities)
        for rise, power in zip(ls.temperature_rises, ls.power_densities):
            assert rise.values.size == power.values.size
            assert np.all(np.isfinite(rise.values))
            assert np.all(rise.values >= 0.0)
            seen += 1
    assert seen > 0, "no meshes carried a temperature field"


def test_runaway_heating_is_clamped_and_warned():
    """An unphysically low heat-transfer coefficient would otherwise drive
    the conductance toward zero and the matrix singular."""
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=0.5,
                          max_rise_c=150.0, max_iterations=6)
    with pytest.warns(S.SolverWarning, match="rise clamp"):
        sol, _ = _solve_strip(thermal=cfg)
    assert sol.solver_info.max_temperature_rise_c <= 150.0 + 1e-9


def test_zero_heat_transfer_is_inert_rather_than_dividing_by_zero():
    """h = 0 means 'no cooling path modelled'; treat it as no rise instead
    of raising."""
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=0.0,
                          max_iterations=3)
    sol, r = _solve_strip(thermal=cfg)
    _, r_cold = _solve_strip()
    assert sol.solver_info.max_temperature_rise_c == 0.0
    assert r == pytest.approx(r_cold, rel=1e-9)
