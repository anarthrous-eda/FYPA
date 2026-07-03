"""Adaptive SMPS regulator-gain iteration tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fypa.altium.annotations import AnnotationResult, RegulatorSpec, TerminalSpec
from fypa.altium.loader import (
    _ADAPTIVE_GAIN_MAX_ITERATIONS,
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
