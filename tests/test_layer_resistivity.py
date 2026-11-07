"""Per-layer copper resistivity overrides (Settings > Stackup).

A layer can carry its own 20 °C resistivity instead of the board-wide copper
value. It is temperature-corrected with the board's coefficient, changes only
that layer's sheet conductance (via barrels stay copper), and is recorded in
the solve metadata so current density backs the thickness out with the
layer's own conductivity rather than the board's.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import fypa.altium_geometry as _geom
from fypa.altium.extract import RawStackupLayer
from fypa.altium.loader import SolveSettings
from fypa.altium_geometry import (
    _layer_conductance,
    layer_copper_conductivity,
    solve_copper_conductivity,
    stackup_layer_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "ExampleDesigns" / "Sandbox" / "Sandbox.PrjPcb"


@pytest.fixture(autouse=True)
def _restore_module_physics(monkeypatch):
    """apply_to_modules patches module globals; put them back afterwards so
    the rest of the suite sees plain copper."""
    import fypa.altium.loader as _loader
    monkeypatch.setattr(_geom, "COPPER_CONDUCTIVITY_S_PER_MM",
                        _geom.COPPER_CONDUCTIVITY_S_PER_MM)
    monkeypatch.setattr(_geom, "LAYER_CONDUCTIVITY_S_PER_MM", {})
    for name in ("COPPER_RESISTIVITY_OHM_MM", "PLATING_THICKNESS_MM",
                 "PLATING_BLIND_MM", "PLATING_BURIED_MM",
                 "PLATING_MICROVIA_MM", "COUPLING_RESISTANCE_OHM",
                 "FALLBACK_VIA_RESISTANCE_OHM",
                 "CONDUCTIVE_FILL_RESISTIVITY_OHM_MM", "CONDUCTIVE_FILL_MODE",
                 "AREA_WEIGHTED_PIN_COUPLING"):
        monkeypatch.setattr(_loader, name, getattr(_loader, name))
    yield


def _layer(layer_id: int, thickness_mm: float = 0.035,
           name: str = "Top") -> RawStackupLayer:
    return RawStackupLayer(
        layer_id=layer_id, name=name, copper_thickness_mm=thickness_mm,
        dielectric_thickness_mm=0.2, next_layer_id=0,
        is_plane=False, plane_net_name=None, mech_enabled=True,
    )


class TestSettings:
    def test_default_has_no_overrides(self):
        s = SolveSettings()
        assert s.layer_copper_resistivity_20c_microohm_cm == {}
        assert s.layer_conductivity_s_per_mm == {}

    def test_default_instances_do_not_share_the_map(self):
        a, b = SolveSettings(), SolveSettings()
        a.layer_copper_resistivity_20c_microohm_cm[1] = 3.0
        assert b.layer_copper_resistivity_20c_microohm_cm == {}

    def test_override_converts_to_conductivity(self):
        s = SolveSettings(layer_copper_resistivity_20c_microohm_cm={1: 3.36})
        # 3.36 µΩ·cm -> 0.1 / 3.36e-6 S/mm, exactly half of 1.68 copper.
        assert s.layer_conductivity_s_per_mm[1] == pytest.approx(
            0.5 * s.copper_conductivity_s_per_mm)

    def test_override_is_temperature_corrected(self):
        s = SolveSettings(temperature_c=70.0,
                          layer_copper_resistivity_20c_microohm_cm={1: 3.36})
        ratio = s.copper_conductivity_s_per_mm / s.layer_conductivity_s_per_mm[1]
        # Same alpha and T on both, so the ratio is the 20 °C ratio.
        assert ratio == pytest.approx(2.0)
        cold = SolveSettings(layer_copper_resistivity_20c_microohm_cm={1: 3.36})
        assert s.layer_conductivity_s_per_mm[1] < cold.layer_conductivity_s_per_mm[1]

    @pytest.mark.parametrize("bad", [0.0, -1.0, "x", None])
    def test_unusable_values_are_ignored(self, bad):
        s = SolveSettings(layer_copper_resistivity_20c_microohm_cm={1: bad})
        assert s.layer_conductivity_s_per_mm == {}

    def test_apply_to_modules_patches_the_layer_map(self):
        s = SolveSettings(layer_copper_resistivity_20c_microohm_cm={1: 3.36})
        s.apply_to_modules()
        assert layer_copper_conductivity(1) == pytest.approx(
            0.5 * _geom.COPPER_CONDUCTIVITY_S_PER_MM)
        assert layer_copper_conductivity(32) == _geom.COPPER_CONDUCTIVITY_S_PER_MM

    def test_from_metadata_round_trip(self):
        # JSON turns int keys into strings; both forms must restore.
        for key in (1, "1"):
            md = {"physics_constants": {
                "layer_copper_resistivity_20c_microohm_cm": {key: 2.5}}}
            s = SolveSettings.from_metadata(md)
            assert s.layer_copper_resistivity_20c_microohm_cm == {1: 2.5}

    def test_from_old_metadata_has_no_overrides(self):
        s = SolveSettings.from_metadata({"physics_constants": {
            "copper_resistivity_20c_microohm_cm": 1.7}})
        assert s.layer_copper_resistivity_20c_microohm_cm == {}

    def test_overrides_change_the_solve_fingerprint(self):
        from fypa.cli import _settings_fingerprint
        plain = _settings_fingerprint(SolveSettings())
        tuned = _settings_fingerprint(SolveSettings(
            layer_copper_resistivity_20c_microohm_cm={1: 3.0}))
        assert plain != tuned


class TestLayerConductance:
    def test_overridden_layer_uses_its_own_sigma(self):
        SolveSettings(
            layer_copper_resistivity_20c_microohm_cm={1: 3.36}
        ).apply_to_modules()
        top = _layer_conductance(_layer(1))
        bottom = _layer_conductance(_layer(32, name="Bottom"))
        assert top == pytest.approx(0.5 * bottom)
        assert bottom == pytest.approx(0.035 * _geom.COPPER_CONDUCTIVITY_S_PER_MM)


class TestMetadataLookup:
    MD = {"physics_constants": {
        "copper_conductivity_S_per_mm": 5.95e4,
        "layer_copper_conductivity_S_per_mm": {"Top": 2.0e4},
    }}

    def test_stackup_name_of_fem_layer(self):
        assert stackup_layer_name("Top|VCC") == "Top"
        assert stackup_layer_name("Top") == "Top"

    def test_per_layer_value_wins(self):
        assert solve_copper_conductivity(self.MD, "Top") == 2.0e4
        assert solve_copper_conductivity(self.MD, "Top|P1V") == 2.0e4

    def test_other_layers_use_the_board_value(self):
        assert solve_copper_conductivity(self.MD, "Bottom|P1V") == 5.95e4
        assert solve_copper_conductivity(self.MD) == 5.95e4

    def test_current_density_backs_out_the_real_thickness(self):
        # A 35 µm layer at half copper's conductivity: G = 0.035 * sigma/2.
        sigma = solve_copper_conductivity(self.MD, "Top|P1V")
        g = 0.035 * sigma
        assert _geom.volume_current_density(1.0, g, sigma) == pytest.approx(
            1.0 / 0.035)


class TestParaViewExport:
    def test_overridden_layer_uses_its_own_sigma(self, tmp_path):
        from fypa.lean_solution import (
            LeanLayer, LeanLayerSolution, LeanProblem, LeanSolution,
        )
        from fypa.paraview_export import export_lean_solution

        xys = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
        tris = np.array([[0, 1, 2]], dtype=np.int32)
        sigma_top, sigma_cu, t = 2.0e4, 5.95e4, 0.035

        def _ls(g):
            # A uniform sheet current of 1 A/mm on conductance g has power
            # density p = J^2 / g.
            return LeanLayerSolution(
                vertex_xys=[xys], triangles=[tris],
                potentials=[np.array([1.0, 0.9, 0.8])],
                power_densities=[np.array([1.0 / g])],
            )
        layers = [LeanLayer(name="Top|N", conductance=sigma_top * t, shape=None),
                  LeanLayer(name="Bottom|N", conductance=sigma_cu * t, shape=None)]
        sol = LeanSolution(problem=LeanProblem(layers=layers),
                           layer_solutions=[_ls(l.conductance) for l in layers])
        md = {"physics_constants": {
            "copper_conductivity_S_per_mm": sigma_cu,
            "layer_copper_conductivity_S_per_mm": {"Top": sigma_top},
        }}
        export_lean_solution(sol, tmp_path,
                             copper_conductivity=sigma_cu, metadata=md)

        from tests.test_paraview_export import _read_field
        files = sorted(tmp_path.glob("*.vtu"))
        assert len(files) == 2
        for path in files:
            j = _read_field(path.read_text(), "current_density")
            # Both layers are 35 µm, so both must read 1 / 0.035 A/mm².
            assert np.allclose(j, 1.0 / t), (path.name, j)


def test_sandbox_layer_override_doubles_top_drop():
    """End to end: doubling Top's resistivity doubles a Top-only pour's
    drop, and the metadata records it for the readers."""
    pytest.importorskip("altium_monkey")
    if not SANDBOX.exists():
        pytest.skip(f"example design not present: {SANDBOX}")
    from pdnsolver import mesh as _pdn_mesh

    from fypa.altium.loader import (
        build_solve_metadata, load_project, solve_problem_adaptive,
    )

    def span(solution, net):
        out = 0.0
        for layer, ls in zip(solution.problem.layers,
                             solution.layer_solutions):
            if layer.name.split("|", 1)[-1] != net:
                continue
            pots = [np.asarray(getattr(p, "values", p), float)
                    for p in ls.potentials]
            if pots:
                f = np.concatenate(pots)
                out = max(out, float(f.max() - f.min()))
        return out

    plain = SolveSettings()
    plain.apply_to_modules()
    loaded = load_project(SANDBOX)
    base = solve_problem_adaptive(loaded, _pdn_mesh.Mesher.Config())[0]
    # The stackup layer the test pour sits on.
    pour_layer = next(l.name for l in base.problem.layers
                      if l.name.endswith("|P1V_1W100L"))
    top = next(s for s in loaded.extracted.stackup
               if s.name == stackup_layer_name(pour_layer))

    tuned = SolveSettings(layer_copper_resistivity_20c_microohm_cm={
        top.layer_id: 2.0 * plain.copper_resistivity_20c_microohm_cm})
    tuned.apply_to_modules()
    loaded = load_project(SANDBOX)
    solved = solve_problem_adaptive(loaded, _pdn_mesh.Mesher.Config())[0]

    assert span(solved, "P1V_1W100L") == pytest.approx(
        2.0 * span(base, "P1V_1W100L"), rel=1e-3)

    md = build_solve_metadata(loaded, None, settings=tuned)
    phys = md["physics_constants"]
    assert phys["layer_copper_resistivity_20c_microohm_cm"] == {
        top.layer_id: 2.0 * plain.copper_resistivity_20c_microohm_cm}
    assert phys["layer_copper_conductivity_S_per_mm"][top.name] == (
        pytest.approx(0.5 * phys["copper_conductivity_S_per_mm"]))
    row = next(r for r in md["stackup"] if r["layer_id"] == top.layer_id)
    assert row["resistivity_override"] is True
    assert SolveSettings.from_metadata(md) == tuned
