"""Adaptive SMPS regulator-gain iteration tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fypa.altium.annotations import AnnotationResult, RegulatorSpec, TerminalSpec
from fypa.altium.loader import solve_problem_adaptive


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
