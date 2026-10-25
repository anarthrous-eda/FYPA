"""Current density is reported per mm² of copper cross-section (A/mm²),
so layers of different copper weight compare directly (issue #59).

A 2D solve yields sheet current density (A per mm of copper width); dividing
by the layer thickness, recovered as sheet conductance / conductivity, gives
the volume density. The same current through the same width on 2 oz copper
must read half the A/mm² of 1 oz.
"""
from __future__ import annotations

import numpy as np
import pytest

from fypa.altium_geometry import (
    solve_copper_conductivity,
    volume_current_density,
)
from fypa.viewer.display import _MODES, _current_density_per_vertex
from pdnsolver.vtu_fields import per_vertex_fields

SIGMA = 5.95e4          # S/mm
T_1OZ = 0.035           # mm
T_2OZ = 0.070           # mm
TRIS = np.array([[0, 1, 2]], dtype=np.int32)
POTS = np.array([1.0, 0.9, 0.8])


def _power_density(sheet_j: float, conductance: float) -> np.ndarray:
    # p = J_s^2 / G for a uniform sheet current J_s (A/mm) -> W/mm^2.
    return np.array([sheet_j * sheet_j / conductance])


class TestVolumeCurrentDensity:
    def test_divides_sheet_density_by_thickness(self):
        # 1 A/mm on 35 um copper = 1 / 0.035 = 28.57 A/mm^2.
        assert volume_current_density(1.0, SIGMA * T_1OZ, SIGMA) == (
            pytest.approx(1.0 / T_1OZ))

    def test_accepts_arrays(self):
        out = volume_current_density(np.array([0.0, 2.0]), SIGMA * T_2OZ,
                                     SIGMA)
        assert np.allclose(out, [0.0, 2.0 / T_2OZ])

    def test_zero_conductance_gives_zero(self):
        assert volume_current_density(1.0, 0.0, SIGMA) == 0.0

    def test_solve_conductivity_read_from_metadata(self):
        md = {"physics_constants": {"copper_conductivity_S_per_mm": 5.0e4}}
        assert solve_copper_conductivity(md) == 5.0e4

    def test_solve_conductivity_falls_back_without_metadata(self):
        import fypa.altium_geometry as g
        assert solve_copper_conductivity(None) == g.COPPER_CONDUCTIVITY_S_PER_MM
        assert solve_copper_conductivity({"physics_constants": {}}) == (
            g.COPPER_CONDUCTIVITY_S_PER_MM)


class TestViewerMode:
    def test_unit_is_per_mm2(self):
        units = {label: unit for label, unit, _fn in _MODES}
        assert units["Current Density"] == "A/mm^2"

    def test_thicker_copper_reads_lower_for_same_sheet_current(self):
        # Same 1 A/mm through both layers: the sheet solve sees the same
        # current per width, but 2 oz copper carries it at half the density.
        vals = {}
        for t in (T_1OZ, T_2OZ):
            g = SIGMA * t
            vals[t] = _current_density_per_vertex(
                TRIS, POTS, _power_density(1.0, g), g, 3, SIGMA)
        assert np.allclose(vals[T_1OZ], 1.0 / T_1OZ)
        assert np.allclose(vals[T_2OZ], 1.0 / T_2OZ)
        assert np.allclose(vals[T_2OZ], 0.5 * vals[T_1OZ])


class TestParaViewFields:
    def test_volume_and_sheet_density_both_exported(self):
        g = SIGMA * T_1OZ
        fields = per_vertex_fields(TRIS, POTS, _power_density(2.0, g), g,
                                   copper_conductivity=SIGMA)
        assert np.allclose(fields["sheet_current_density"], 2.0)
        assert np.allclose(fields["current_density"], 2.0 / T_1OZ)
