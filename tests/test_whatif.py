"""What-If scenarios: the Qt-free model, merge, mapping and comparison."""
from __future__ import annotations

import numpy as np
import pytest

from fypa import whatif as W
from fypa.altium.loader import SolveSettings
from fypa.lean_solution import (
    LeanLayer,
    LeanLayerSolution,
    LeanProblem,
    LeanSolution,
)
from fypa.project_file import ProjectFile


# --- model -----------------------------------------------------------------

def _scenario() -> W.WhatIfScenario:
    return W.WhatIfScenario(
        name="Cost-down",
        notes="1 oz inner",
        settings_overrides={"temperature_c": 60.0, "electrothermal": True,
                            "conductive_fill_mode": "none"},
        stackup_thickness_mm={39: 0.0175},
        layer_resistivity={1: 2.0},
    )


def test_round_trip_keeps_everything():
    s = _scenario()
    s.status = W.STATUS_OK
    s.summary = {"worst_drop_v": -0.1, "bad": float("nan")}
    back = W.WhatIfScenario.from_dict(s.to_dict())
    assert back.id == s.id and back.name == "Cost-down"
    assert back.settings_overrides == s.settings_overrides
    assert back.stackup_thickness_mm == {39: 0.0175}
    assert back.layer_resistivity == {1: 2.0}
    assert back.status == W.STATUS_OK
    # NaN is not valid JSON: dropped to None.
    assert back.summary == {"worst_drop_v": -0.1, "bad": None}


def test_from_dict_drops_unknown_settings_and_bad_entries():
    back = W.WhatIfScenario.from_dict({
        "name": "x",
        "settings_overrides": {"mesh_max_size_mm": 0.1, "temperature_c": 30},
        "stackup_thickness_mm": {"1": "oops", "2": 0.07},
        "status": "weird",
    })
    assert back.settings_overrides == {"temperature_c": 30}
    assert back.stackup_thickness_mm == {2: 0.07}
    assert back.status == W.STATUS_NOT_RUN


def test_copy_is_unrun_and_independent():
    s = _scenario()
    s.status = W.STATUS_OK
    c = s.copy_as("Copy")
    assert c.id != s.id and c.status == W.STATUS_NOT_RUN
    c.stackup_thickness_mm[39] = 1.0
    assert s.stackup_thickness_mm[39] == 0.0175


def test_unique_name():
    assert W.unique_name([]) == "What-if 1"
    assert W.unique_name(["What-if 1", "What-if 2"]) == "What-if 3"


# --- merging onto the base --------------------------------------------------

def test_scenario_settings_merges_without_touching_base():
    base = SolveSettings(temperature_c=25.0,
                         layer_copper_resistivity_20c_microohm_cm={40: 3.0})
    out = W.scenario_settings(base, _scenario())
    assert out.temperature_c == 60.0
    assert out.electrothermal is True
    assert out.conductive_fill_mode == "none"
    assert out.layer_copper_resistivity_20c_microohm_cm == {40: 3.0, 1: 2.0}
    # Mesh settings are never a what-if's business.
    assert out.mesh_max_size_mm == base.mesh_max_size_mm
    assert base.temperature_c == 25.0
    assert base.layer_copper_resistivity_20c_microohm_cm == {40: 3.0}


def test_stackup_overrides_merge_and_design_value_drops():
    s = W.WhatIfScenario(name="t", stackup_thickness_mm={1: 0.035, 2: 0.07})
    out = W.scenario_stackup_overrides({1: 0.05, 3: 0.02}, s,
                                       design_thickness_mm={1: 0.035})
    # Layer 1 back to its design value: the base override is removed.
    assert out == {2: 0.07, 3: 0.02}


def test_fingerprint_is_stable_and_sensitive():
    a = W.base_fingerprint(SolveSettings(), {1: 0.03}, {"p": "x"})
    assert a == W.base_fingerprint(SolveSettings(), {1: 0.03}, {"p": "x"})
    assert a != W.base_fingerprint(SolveSettings(temperature_c=21.0),
                                   {1: 0.03}, {"p": "x"})
    assert a != W.base_fingerprint(SolveSettings(), {1: 0.04}, {"p": "x"})
    assert a != W.base_fingerprint(SolveSettings(), {1: 0.03}, {"p": "y"})


def test_stale_only_when_run_against_another_base():
    s = W.WhatIfScenario(name="t", base_fingerprint="aaa",
                         status=W.STATUS_OK)
    assert not s.is_stale("aaa")
    assert s.is_stale("bbb")
    s.status = W.STATUS_NOT_RUN
    assert not s.is_stale("bbb")


def test_describe_changes():
    rows = [{"layer_id": 39, "name": "L2", "copper_thickness_mm": 0.035},
            {"layer_id": 1, "name": "Top", "copper_thickness_mm": 0.035}]
    lines = W.describe_changes(_scenario(), SolveSettings(), rows)
    assert "Board temperature: 20 → 60 °C" in lines
    assert "Coupled electro-thermal solve: off → on" in lines
    assert any(x.startswith("L2 copper: 35 → 17.5 µm") for x in lines)
    assert "Top resistivity: 1.68 → 2 µΩ·cm" in lines


# --- mapping a field between meshes ----------------------------------------

def _square(n: int):
    xs, ys = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n))
    xys = np.column_stack([xs.ravel(), ys.ravel()])
    tris = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            tris += [(a, a + 1, a + n), (a + 1, a + n + 1, a + n)]
    return xys, np.asarray(tris, dtype=np.int32)


def test_identical_meshes_map_exactly():
    xys, tris = _square(5)
    vals = xys[:, 0] * 3.0
    out, method = W.map_field_to_points(xys, tris, vals, xys[:, 0], xys[:, 1])
    assert method == "identical"
    assert np.array_equal(out, vals)


def test_linear_field_interpolates_exactly_on_another_mesh():
    xys, tris = _square(4)
    vals = 2.0 * xys[:, 0] - xys[:, 1] + 0.5
    dst, _ = _square(7)
    out, method = W.map_field_to_points(xys, tris, vals, dst[:, 0], dst[:, 1])
    assert method == "linear"
    assert np.allclose(out, 2.0 * dst[:, 0] - dst[:, 1] + 0.5)


def test_points_off_the_mesh_take_the_nearest_vertex():
    xys, tris = _square(3)
    vals = np.arange(len(xys), dtype=float)
    out, _ = W.map_field_to_points(xys, tris, vals,
                                   np.array([5.0]), np.array([5.0]))
    # (1, 1) is the last vertex of the grid.
    assert out[0] == vals[-1]


def _lean(pots: np.ndarray, pd: np.ndarray, *, scale: float = 1.0):
    xys, tris = _square(3)
    layer = LeanLayer(name="Top|VCC", conductance=1.0, shape=None)
    ls = LeanLayerSolution(vertex_xys=[xys], triangles=[tris],
                           potentials=[pots * scale],
                           power_densities=[pd * scale])
    return LeanSolution(problem=LeanProblem(layers=[layer]),
                        layer_solutions=[ls]), xys, tris


def test_layer_vertex_field_and_copper_loss():
    from fypa.viewer.display import _voltage_per_vertex
    xys, tris = _square(3)
    pots = np.linspace(1.0, 0.9, len(xys))
    pd = np.full(len(tris), 2.0)   # W/mm² over a 1 mm² board
    sol, _, _ = _lean(pots, pd)
    f_xys, f_tris, f_vals = W.layer_vertex_field(sol, 0, _voltage_per_vertex,
                                                 1.0)
    assert np.array_equal(f_vals, pots) and f_tris.shape == tris.shape
    assert W.total_copper_loss_w(sol) == pytest.approx(2.0)


# --- comparing rows ------------------------------------------------------

def _row(desig, term, drop, voltage, margin=None, role="SINK", status=None,
         rail="VCC"):
    return {"role": role, "designator": desig, "terminal": term, "pad": "1",
            "rail": rail,
            "net": "VCC" if term == "P" else "GND", "layer_id": 1,
            "x_mm": 1.0, "y_mm": 2.0, "drop": drop, "voltage": voltage,
            "margin": margin, "status": status, "current_density": 1.0,
            "power_density": 0.1}


def test_drop_verdict_uses_magnitude():
    base = [_row("U1", "P", -0.050, 3.25)]
    worse = [_row("U1", "P", -0.080, 3.22)]
    better = [_row("U1", "P", -0.020, 3.28)]
    assert W.compare_node_rows(base, worse)[0]["verdict"] == "worse"
    assert W.compare_node_rows(base, better)[0]["verdict"] == "better"
    assert W.compare_node_rows(base, base)[0]["verdict"] == "same"


def test_voltage_verdict_flips_on_return_pins():
    base = [_row("U1", "P", -0.05, 3.25), _row("U1", "N", 0, 0.010)]
    scen = [_row("U1", "P", -0.06, 3.24), _row("U1", "N", 0, 0.015)]
    rows = W.compare_node_rows(base, scen, "voltage")
    assert [r["verdict"] for r in rows] == ["worse", "worse"]
    assert rows[1]["delta"] == pytest.approx(0.005)


def test_rows_missing_on_one_side_are_kept():
    rows = W.compare_node_rows([_row("U1", "P", -0.05, 3.2)],
                               [_row("U2", "P", -0.05, 3.2)])
    assert len(rows) == 2
    assert rows[0]["whatif"] is None and rows[1]["base"] is None
    assert rows[0]["verdict"] == ""


def test_summary_and_headline_comparison():
    base_rows = [_row("U1", "P", -0.05, 3.25, margin=0.1, status="PASS"),
                 _row("U2", "P", -0.09, 3.21, margin=-0.01, status="FAIL")]
    scen_rows = [_row("U1", "P", -0.03, 3.27, margin=0.12, status="PASS"),
                 _row("U2", "P", -0.06, 3.24, margin=0.02, status="PASS")]
    b = W.summarise(base_rows)
    s = W.summarise(scen_rows)
    assert b["worst_drop_v"] == -0.09 and b["fails"] == 1
    heads = {h["key"]: h for h in W.compare_summaries(b, s)}
    assert heads["worst_drop_v"]["verdict"] == "better"
    assert heads["worst_drop_v"]["delta"] == pytest.approx(30.0)   # mV
    assert heads["fails"]["verdict"] == "better"
    assert heads["worst_margin_v"]["verdict"] == "better"
    assert heads["worst_drop_v"]["rail"] == "VCC"
    assert heads["fails"]["rail"] == "VCC"


def test_headline_rail_names_where_the_worst_case_moved():
    base = W.summarise([_row("U1", "P", -0.09, 3.2, rail="3V3"),
                        _row("U2", "P", -0.05, 1.7, rail="1V8")])
    scen = W.summarise([_row("U1", "P", -0.03, 3.2, rail="3V3"),
                        _row("U2", "P", -0.06, 1.7, rail="1V8")])
    heads = {h["key"]: h for h in W.compare_summaries(base, scen)}
    assert heads["worst_drop_v"]["rail"] == "1V8 (base: 3V3)"
    assert "U2.1 on 1V8" in heads["worst_drop_v"]["rail_tip"]
    assert heads["fails"]["rail"] == "none"
    loss = {h["key"]: h for h in W.compare_summaries(
        {"copper_loss_w": 1.0}, {"copper_loss_w": 2.0})}
    assert loss["copper_loss_w"]["rail"] == "All rails"
    # Summaries saved before rails were recorded.
    old = {h["key"]: h for h in W.compare_summaries(
        {"worst_drop_v": -0.1}, {"worst_drop_v": -0.2})}
    assert old["worst_drop_v"]["rail"] == "-"


# --- project file ---------------------------------------------------------

def test_project_file_round_trips_what_ifs(tmp_path):
    result = tmp_path / "proj_whatifs" / "abc.pkl"
    result.parent.mkdir()
    result.write_bytes(b"x")
    s = _scenario()
    s.result_pickle = str(result)
    pf = ProjectFile(what_ifs=[s.to_dict()])
    path = tmp_path / "proj.fypa"
    pf.save(path)
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Stored relative so the folder stays portable.
    assert not raw["what_ifs"][0]["result_pickle"].startswith(str(tmp_path))
    back = ProjectFile.load(path)
    w = W.WhatIfScenario.from_dict(back.what_ifs[0])
    assert w.stackup_thickness_mm == {39: 0.0175}
    assert w.result_pickle == str(result.resolve())


def test_older_project_has_no_what_ifs(tmp_path):
    path = tmp_path / "old.fypa"
    path.write_text('{"schema_version": 2}', encoding="utf-8")
    assert ProjectFile.load(path).what_ifs == []


# --- editor dialog --------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _dialog(scenario=None):
    from fypa.viewer.whatif_panel import WhatIfDialog
    rows = [{"layer_id": 1, "name": "Top", "copper_thickness_mm": 0.035}]
    return WhatIfDialog(None, scenario or W.WhatIfScenario(name="t"),
                        SolveSettings(), rows, [])


def _dirty(w) -> bool:
    return w.property("dirty") == "true"


def test_dialog_outlines_only_changed_fields(qapp):
    dlg = _dialog()
    temp = dlg._edits["temperature_c"]
    fill = dlg._edits["conductive_fill_mode"]
    thk = dlg._thk_edits[1]
    rho = dlg._rho_edits[1]
    assert not any(_dirty(w) for w in (temp, fill, thk, rho))
    temp.setText("60")
    thk.setText("17.5")
    fill.setCurrentIndex(fill.findData("all"))
    assert _dirty(temp) and _dirty(thk) and _dirty(fill)
    assert not _dirty(rho)
    # Typing the base value back, or clearing, removes the outline.
    temp.setText("20")
    thk.setText("")
    fill.setCurrentIndex(0)
    assert not any(_dirty(w) for w in (temp, fill, thk))
    # Half-typed / invalid input stays outlined, as on the Properties tab.
    rho.setText("-")
    assert _dirty(rho)


def test_dialog_outlines_an_existing_whatifs_changes_on_open(qapp):
    dlg = _dialog(W.WhatIfScenario(
        name="t", settings_overrides={"electrothermal": True},
        stackup_thickness_mm={1: 0.07}))
    assert _dirty(dlg._edits["electrothermal"])
    assert _dirty(dlg._thk_edits[1])
    assert not _dirty(dlg._edits["temperature_c"])
