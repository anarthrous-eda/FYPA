"""ParaView VTU export — scalar fields, vias, and voltage_drop reference."""
from __future__ import annotations

import re

import numpy as np

from fypa.lean_solution import (
    LeanLayer,
    LeanLayerSolution,
    LeanProblem,
    LeanSolution,
)
from fypa.paraview_export import export_lean_solution
from pdnsolver.vtu_fields import (
    global_voltage_max,
    per_vertex_fields,
)


def _tri_layer(potentials: list[float]) -> LeanLayerSolution:
    """One single-triangle component carrying the given vertex potentials."""
    xys = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    tris = np.array([[0, 1, 2]], dtype=np.int32)
    return LeanLayerSolution(
        vertex_xys=[xys],
        triangles=[tris],
        potentials=[np.array(potentials, dtype=np.float64)],
        power_densities=[np.array([0.5])],
    )


def _solution(*layers: tuple[str, list[float]]) -> LeanSolution:
    return LeanSolution(
        problem=LeanProblem(
            layers=[LeanLayer(name=n, conductance=58.0, shape=None)
                    for n, _ in layers],
        ),
        layer_solutions=[_tri_layer(p) for _, p in layers],
    )


def _read_field(vtu_text: str, name: str) -> list[float]:
    m = re.search(rf'Name="{name}"[^>]*>([^<]+)<', vtu_text)
    assert m is not None, f"field {name!r} not found"
    return [float(v) for v in m.group(1).split()]


class TestGlobalVoltageMax:
    def test_max_across_layers(self):
        assert global_voltage_max([np.array([1.0, 5.0]),
                                   np.array([3.3, 0.0])]) == 5.0

    def test_all_negative_returns_true_max_not_zero(self):
        # Regression: must not floor at 0.0 for an all-negative solution.
        assert global_voltage_max([np.array([-1.0, -2.0])]) == -1.0

    def test_empty_input_returns_zero(self):
        assert global_voltage_max([]) == 0.0

    def test_empty_arrays_skipped(self):
        assert global_voltage_max([np.array([]), np.array([2.0])]) == 2.0


class TestPerVertexFields:
    def test_explicit_reference_used_for_drop(self):
        fields = per_vertex_fields(
            np.array([[0, 1, 2]], dtype=np.int32),
            np.array([3.3, 3.2, 3.1]),
            None, 58.0,
            voltage_drop_reference=5.0,
        )
        assert np.allclose(fields["voltage_drop"], [-1.7, -1.8, -1.9])
        # power_density absent -> zeros, hence zero current_density.
        assert np.allclose(fields["power_density"], 0.0)
        assert np.allclose(fields["current_density"], 0.0)

    def test_unset_reference_uses_per_island_max(self):
        fields = per_vertex_fields(
            np.array([[0, 1, 2]], dtype=np.int32),
            np.array([5.0, 4.0, 3.0]),
            None, 58.0,
        )
        assert np.allclose(fields["voltage_drop"], [0.0, -1.0, -2.0])


class TestExportLeanSolution:
    def test_each_layer_carries_all_scalar_fields(self, tmp_path):
        sol = _solution(("Top", [1.0, 0.9, 0.8]))
        n = export_lean_solution(sol, tmp_path)
        assert n == 1
        text = (tmp_path / "Top.vtu").read_text()
        for field in ("voltage", "voltage_drop",
                      "current_density", "power_density"):
            assert f'Name="{field}"' in text

    def test_explicit_reference_matches_heatmap(self, tmp_path):
        sol = _solution(("L3", [3.3, 3.2, 3.1]))
        export_lean_solution(sol, tmp_path, voltage_drop_reference=5.0)
        drop = _read_field((tmp_path / "L3.vtu").read_text(), "voltage_drop")
        assert np.allclose(drop, [-1.7, -1.8, -1.9])

    def test_unset_reference_uses_global_max_across_layers(self, tmp_path):
        sol = _solution(("L5", [5.0, 4.9, 4.8]), ("L3", [3.3, 3.2, 3.1]))
        export_lean_solution(sol, tmp_path)  # reference unset -> global 5.0
        drop = _read_field((tmp_path / "L3.vtu").read_text(), "voltage_drop")
        assert np.allclose(drop, [-1.7, -1.8, -1.9])

    def test_via_rows_emit_vias_vtu(self, tmp_path):
        sol = _solution(("Top", [1.0, 0.9, 0.8]))
        n = export_lean_solution(
            sol, tmp_path,
            via_rows=[{"x_mm": 0.5, "y_mm": 0.5, "current": 0.123}],
        )
        assert n == 2
        text = (tmp_path / "vias.vtu").read_text()
        assert "via_current" in text
        assert np.allclose(_read_field(text, "via_current"), [0.123])

    def test_no_via_rows_skips_vias_vtu(self, tmp_path):
        sol = _solution(("Top", [1.0, 0.9, 0.8]))
        export_lean_solution(sol, tmp_path)
        assert not (tmp_path / "vias.vtu").exists()
