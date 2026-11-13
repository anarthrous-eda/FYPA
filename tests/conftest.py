"""Shared pytest fixtures for the FYPA test suite.

These tests exercise the vendored FEM solver (:mod:`pdnsolver`) directly,
without the Altium-loading or GUI layers, so they are fast and dependency-light.
The end-to-end regression tests (``test_regression.py``) shell out to
``FYPA.py`` instead and are marked ``slow``.

Helpers are exposed as fixtures (the factory-fixture pattern) so test modules
never need to import from this file.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest
import shapely
from shapely.geometry import MultiPolygon, Point

from pdnsolver import mesh as MeshMod
from pdnsolver import problem as P
from pdnsolver import solver as S

# Copper conductivity used throughout FYPA (see altium_loader.py): 5.95e4 S/mm.
COPPER_CONDUCTIVITY_S_PER_MM = 5.95e4
# 1 oz/ft^2 finished copper ~= 0.035 mm.
DEFAULT_THICKNESS_MM = 0.035


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path_factory, monkeypatch):
    """Keep every test off the developer's real FYPA preferences.

    The app builds its settings as ``QSettings(org, app)``, and that
    constructor always uses the native format (the registry on Windows) —
    ``QSettings.setDefaultFormat`` does NOT apply to it. So swap the class the
    app's lazy ``from PySide6.QtCore import QSettings`` imports pick up for one
    that sends any ``(org, app)`` construction to a per-test INI file.
    Without this, tests that record / clear recent projects or save a
    preference rewrite the real ones.
    """
    try:
        from PySide6 import QtCore
    except ImportError:  # solver-only environments
        yield
        return
    ini = tmp_path_factory.mktemp("qsettings") / "fypa.ini"
    original = QtCore.QSettings

    class _IsolatedQSettings(original):
        def __init__(self, *args, **kwargs):
            if (len(args) >= 2 and isinstance(args[0], str)
                    and isinstance(args[1], str)):
                super().__init__(str(ini), original.Format.IniFormat)
            else:
                super().__init__(*args, **kwargs)

    monkeypatch.setattr(QtCore, "QSettings", _IsolatedQSettings)
    yield


def _strip_conductance(thickness_mm: float = DEFAULT_THICKNESS_MM) -> float:
    """Sheet conductance G = conductivity [S/mm] * thickness [mm]  ->  Siemens."""
    return COPPER_CONDUCTIVITY_S_PER_MM * thickness_mm


@dataclass
class SolvedStrip:
    """Flattened solver output for a single-component strip solve."""

    xys: np.ndarray          # (N, 2) vertex coordinates, mm
    potentials: np.ndarray   # (N,)  per-vertex potential, V
    expected: dict           # analytical reference values
    solver_info: S.SolverInfo

    def interior_slope(self, lo_frac: float = 0.3, hi_frac: float = 0.7) -> float:
        """Least-squares |dV/dx| over the uniform interior band of the strip.

        Restricting the fit to ``[lo_frac, hi_frac] * length`` excludes the
        injection points, where the 2-D point-source log singularity makes the
        field non-linear.
        """
        length = self.expected["length_mm"]
        x = self.xys[:, 0]
        band = (x >= lo_frac * length) & (x <= hi_frac * length)
        assert band.sum() >= 10, "too few interior vertices for a stable fit"
        slope, _intercept = np.polyfit(x[band], self.potentials[band], 1)
        return abs(slope)


def _make_strip_problem(
    length_mm: float = 40.0,
    width_mm: float = 6.0,
    conductance_s: float | None = None,
    current_a: float = 1.0,
    inset_mm: float = 1.0,
) -> tuple[P.Problem, dict]:
    """Build a single-layer rectangular copper strip carrying a known current.

    A :class:`~pdnsolver.problem.CurrentSource` injects ``current_a`` between
    two point terminals, one near each short edge. Current flows lengthwise and,
    away from the injection points, the potential field is linear in x with a
    slope set purely by the sheet resistance:  dV/dx = -I / (G * W).
    """
    if conductance_s is None:
        conductance_s = _strip_conductance()

    rect = shapely.box(0.0, 0.0, length_mm, width_mm)
    layer = P.Layer(shape=MultiPolygon([rect]), name="strip", conductance=conductance_s)

    node_a = P.NodeID()
    node_b = P.NodeID()
    source = P.CurrentSource(f=node_b, t=node_a, current=current_a)
    connections = [
        P.Connection(layer=layer, point=Point(inset_mm, width_mm / 2.0), node_id=node_a),
        P.Connection(
            layer=layer,
            point=Point(length_mm - inset_mm, width_mm / 2.0),
            node_id=node_b,
        ),
    ]
    network = P.Network(connections=connections, elements=[source])
    problem = P.Problem(layers=[layer], networks=[network], project_name="strip-test")

    expected = {
        "length_mm": length_mm,
        "width_mm": width_mm,
        "conductance_s": conductance_s,
        "current_a": current_a,
        # |dV/dx| in the uniform-flow interior of the strip.
        "slope_v_per_mm": current_a / (conductance_s * width_mm),
    }
    return problem, expected


def _solve_strip(
    problem: P.Problem, expected: dict, mesh_size: float | None = None
) -> SolvedStrip:
    """Run the FEM solver on a strip problem and flatten the first component."""
    mesher_config = (
        MeshMod.Mesher.Config(maximum_size=mesh_size) if mesh_size is not None else None
    )
    solution = S.solve(problem, mesher_config=mesher_config)
    layer_solution = solution.layer_solutions[0]
    assert layer_solution.meshes, "solver produced no mesh for the strip layer"

    mesh = layer_solution.meshes[0]
    xys = np.asarray(mesh._source_xys, dtype=np.float64)
    potentials = np.asarray(layer_solution.potentials[0].values, dtype=np.float64)
    assert xys.shape[0] == potentials.shape[0] > 0
    return SolvedStrip(
        xys=xys,
        potentials=potentials,
        expected=expected,
        solver_info=solution.solver_info,
    )


@pytest.fixture
def strip_solver():
    """Factory: build a copper-strip problem, solve it, return a :class:`SolvedStrip`.

    Accepts every :func:`_make_strip_problem` keyword plus an optional
    ``mesh_size`` (mm) forwarded to the mesher. Usage::

        result = strip_solver(length_mm=80.0, width_mm=4.0, mesh_size=0.3)
    """
    def _factory(mesh_size: float | None = None, **kwargs) -> SolvedStrip:
        problem, expected = _make_strip_problem(**kwargs)
        return _solve_strip(problem, expected, mesh_size=mesh_size)

    return _factory


@pytest.fixture(scope="session")
def solved_strip() -> SolvedStrip:
    """A solved 40 x 6 mm copper strip — reused across tests (solve is not free)."""
    problem, expected = _make_strip_problem()
    return _solve_strip(problem, expected)
