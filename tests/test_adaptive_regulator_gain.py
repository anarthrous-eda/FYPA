"""Adaptive SMPS regulator-gain iteration tests."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fypa.altium.annotations import AnnotationResult, RegulatorSpec, TerminalSpec
from fypa.altium.loader import (
    _ADAPTIVE_GAIN_MAX_ITERATIONS,
    _ADAPTIVE_GAIN_REL_TOL,
    solve_problem_adaptive,
)


def _adaptive_smps_regulator() -> RegulatorSpec:
    term = TerminalSpec(pins=())
    return RegulatorSpec(
        designator="U2",
        schdoc_name="Pwr.SchDoc",
        voltage=3.3,
        gain=0.73,
        out_p=term,
        out_n=term,
        in_p=term,
        in_n=term,
        regulator_type="SMPS",
        efficiency=0.9,
        adaptive_gain_eligible=True,
    )


def test_adaptive_gain_not_converged_when_vin_unmeasurable():
    """Vin sampling failure must not report converged with zero gain change."""
    loaded = SimpleNamespace(
        extracted=SimpleNamespace(),
        annotations=AnnotationResult(directives=[_adaptive_smps_regulator()]),
    )
    fake_problem = MagicMock()
    fake_problem.layers = []
    fake_problem.networks = []
    fake_solution = MagicMock()

    with (
        patch(
            "fypa.altium.loader.build_problem",
            return_value=(fake_problem, [], {}, []),
        ),
        patch("pdnsolver.solver.solve", return_value=fake_solution),
        patch("fypa.altium.loader._measured_regulator_vin", return_value=None),
    ):
        *_, adaptive_info = solve_problem_adaptive(
            loaded,
            mesher_config=None,
            adaptive_regulator_gain=True,
        )

    assert adaptive_info["enabled"]
    assert adaptive_info["converged"] is False
    assert adaptive_info["iterations"] == 1


def _run_adaptive(loaded, vin_side_effect):
    fake_problem = MagicMock()
    fake_problem.layers = []
    fake_problem.networks = []
    fake_solution = MagicMock()
    with (
        patch(
            "fypa.altium.loader.build_problem",
            return_value=(fake_problem, [], {}, []),
        ),
        patch("pdnsolver.solver.solve", return_value=fake_solution),
        patch(
            "fypa.altium.loader._measured_regulator_vin",
            side_effect=vin_side_effect,
        ),
    ):
        *_, adaptive_info = solve_problem_adaptive(
            loaded, mesher_config=None, adaptive_regulator_gain=True,
        )
    return adaptive_info


def test_adaptive_gain_converges_to_fixed_point():
    """Constant measured Vin drives gain to V / (Vin·η) and reports it."""
    loaded = SimpleNamespace(
        extracted=SimpleNamespace(),
        annotations=AnnotationResult(directives=[_adaptive_smps_regulator()]),
    )
    adaptive_info = _run_adaptive(loaded, lambda *a: 4.8)

    expected = 3.3 / (4.8 * 0.9)
    assert adaptive_info["converged"] is True
    # gain 0.73 -> refined once, second pass is within tolerance.
    assert adaptive_info["iterations"] == 2
    reported = next(iter(adaptive_info["gains"].values()))
    assert abs(reported - expected) < 1e-9
    # Metadata (read off ``loaded``) must agree with the reported gain.
    assert abs(loaded.annotations.directives[0].gain - expected) < 1e-9


def _multi_smps_loaded(gain: float = 0.2) -> SimpleNamespace:
    term = TerminalSpec(pins=())
    regs = [
        RegulatorSpec(
            designator=f"U{i}", schdoc_name="Pwr.SchDoc",
            voltage=vout, gain=gain, out_p=term, out_n=term,
            in_p=term, in_n=term, regulator_type="SMPS",
            efficiency=0.8, adaptive_gain_eligible=True,
        )
        for i, vout in enumerate((12.0, 3.3, 5.0), start=5)
    ]
    return SimpleNamespace(
        extracted=SimpleNamespace(),
        annotations=AnnotationResult(directives=regs),
    )


def test_adaptive_gain_undamped_when_regulators_are_independent():
    """Several regulators on a stiff rail are not coupled, so they must still
    converge in one full step — damping them would cost real solve passes."""
    loaded = _multi_smps_loaded()
    adaptive_info = _run_adaptive(loaded, lambda *a: 48.0)

    assert adaptive_info["enabled"]
    assert adaptive_info["converged"] is True
    assert adaptive_info["iterations"] == 2
    for d in loaded.annotations.directives:
        assert isinstance(d, RegulatorSpec)
        expected = d.voltage / (48.0 * d.efficiency)
        assert abs(d.gain - expected) / expected < _ADAPTIVE_GAIN_REL_TOL


def test_adaptive_gain_damps_after_residual_changes_sign():
    """Damping engages off an observed sign flip, and the tolerance it converges
    at is the true residual — not the shorter damped step.

    A pure droop model (Vin falling as gain rises) can never produce this: it
    makes the fixed-point Jacobian all-positive, so the iteration is monotone
    and blending only slows it (a diverging ``lam > 1`` stays diverging at
    ``1 - b + b*lam``). Alternation is the one shape a blend can fix, so the
    stub produces one directly: an overshoot on the second pass, then a steady
    rail.
    """
    loaded = SimpleNamespace(
        extracted=SimpleNamespace(),
        annotations=AnnotationResult(directives=[_adaptive_smps_regulator()]),
    )
    loaded.annotations.directives[0] = dataclasses.replace(
        loaded.annotations.directives[0], voltage=12.0, gain=0.5, efficiency=0.8,
    )
    # 0.5 -> target 0.375 (residual -), then target 0.5 (residual +) => flip.
    overshoot = iter([40.0, 30.0])

    def _vin(_solution, _loaded, _d):
        return next(overshoot, 48.0)

    adaptive_info = _run_adaptive(loaded, _vin)

    assert adaptive_info["converged"] is True
    # Damping is doing work: more passes than the 2 an undamped design needs.
    assert adaptive_info["iterations"] > 2
    expected = 12.0 / (48.0 * 0.8)
    gain = loaded.annotations.directives[0].gain
    # Converged means converged. Measuring the damped step against the same
    # threshold would have stopped ~1/blend further out than this allows.
    assert abs(gain - expected) / expected < _ADAPTIVE_GAIN_REL_TOL


def test_adaptive_gain_does_not_damp_a_monotone_droop_design():
    """Coupled droop stays undamped: its residuals never change sign, and
    blending a monotone iteration only costs solve passes."""
    loaded = _multi_smps_loaded(gain=0.5)

    def _vin(_solution, loaded_, _d):
        total_gain = sum(
            r.gain for r in loaded_.annotations.directives
            if isinstance(r, RegulatorSpec)
        )
        return 48.0 - 14.0 * total_gain

    # A blend this aggressive would crawl and blow the iteration cap if it were
    # ever applied — reaching convergence proves it was not.
    with patch("fypa.altium.loader._ADAPTIVE_GAIN_BLEND", 0.05):
        adaptive_info = _run_adaptive(loaded, _vin)

    assert adaptive_info["converged"] is True
    assert adaptive_info["iterations"] < _ADAPTIVE_GAIN_MAX_ITERATIONS


def test_adaptive_gain_reports_gains_used_by_returned_solution():
    """On non-convergence the reported gain must match the gain the returned
    solution was solved with — not the not-yet-applied next iterate."""
    loaded = SimpleNamespace(
        extracted=SimpleNamespace(),
        annotations=AnnotationResult(directives=[_adaptive_smps_regulator()]),
    )
    # Oscillating Vin so the fixed point never settles → forces the
    # max-iterations exit. At each measurement the directive still holds the
    # gain the just-completed solve used, so record it.
    vins = iter([4.0, 5.0] * _ADAPTIVE_GAIN_MAX_ITERATIONS)
    used_gains: list[float] = []

    def _vin(solution, loaded_, d):
        used_gains.append(loaded_.annotations.directives[0].gain)
        return next(vins)

    adaptive_info = _run_adaptive(loaded, _vin)

    assert adaptive_info["converged"] is False
    assert adaptive_info["iterations"] == _ADAPTIVE_GAIN_MAX_ITERATIONS
    reported = next(iter(adaptive_info["gains"].values()))
    assert reported == used_gains[-1]
    assert loaded.annotations.directives[0].gain == used_gains[-1]
