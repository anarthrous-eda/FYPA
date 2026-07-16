"""Impedance tab: rail setup, package library editing, and the plot.

Drives the tab through Qt (offscreen) rather than testing the engine again —
what can only break here is the wiring: which rails appear, whether the mask
and VRM round-trip through the project file, whether a package edit reaches
every capacitor of that case size, and whether an unmodellable part is
excluded loudly rather than silently.
"""
from __future__ import annotations

import math
import os
import types

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("matplotlib")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import matplotlib  # noqa: E402

matplotlib.use("qtagg")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QMainWindow,
    QTabWidget,
)

import fypa.altium_viewer as av  # noqa: E402
from fypa.project_file import ProjectFile  # noqa: E402
from tests.test_caploop_identify import (  # noqa: E402
    RAILS,
    _comp,
    _directives,
    _standard_cap_project,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _project_with(**comp_kwargs):
    """The standard fixture board, but with a capacitance the part parser can
    read — otherwise every capacitor is (correctly) excluded from Z(f)."""
    params = {"Value": "100nF"}
    params.update(comp_kwargs.pop("params", {}))
    return _standard_cap_project(
        pcb_components=(_comp("C1", params=params, **comp_kwargs),))


class _Viewer(av.PdnViewer):
    def __init__(self, project_data=None):
        QMainWindow.__init__(self)
        self.tabs = QTabWidget()
        self._rails = ["+3V3", "GND"]
        self._rail_to_members = RAILS
        self._project = ProjectFile()
        self._caploop_settings_obj = None
        self._caploop_package_lib = None
        self._caps_rows_cache = None
        self._caps_identity_cache = None
        self._caps_shapes_cache = None
        self._caps_table_populated = True
        self._caps_tab_index = -1
        self._impedance_populated = True
        self._imp_plane_cache = {}
        self._display_dirty = False
        self._loaded_project = types.SimpleNamespace(
            extracted=project_data or _project_with())
        self.metadata = {"directives": _directives()}

    def _render(self):
        pass


@pytest.fixture
def viewer(qapp):
    v = _Viewer()
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")
    v._populate_caps_table()
    v._impedance_tab_index = v.tabs.addTab(v._build_impedance_tab(),
                                           "Impedance")
    rails = v._impedance_rails()
    v.imp_rail_combo.addItems(rails)
    v._load_impedance_rail_config(rails[0])
    v._replot_impedance()
    return v


# --- rails and the mask ---------------------------------------------------


def test_only_rails_with_included_caps_are_offered(viewer):
    assert viewer._impedance_rails() == ["+3V3"]
    viewer._set_cap_override("C1", include=False)
    assert viewer._impedance_rails() == []


def test_z_target_is_v_times_ripple_over_transient_current(viewer):
    # The fixture SOURCE is 3.3 V; defaults are 5 % and 1 A.
    r = viewer._compute_rail_impedance("+3V3")
    assert r.target.voltage_v == pytest.approx(3.3)
    assert r.target.z_target_ohm == pytest.approx(0.165)
    assert viewer.imp_ztarget_label.text() == "165 mΩ"


def test_mask_and_vrm_round_trip_through_the_project_file(viewer):
    viewer.imp_ripple_edit.setText("2")
    viewer.imp_itran_edit.setText("10")
    viewer.imp_fmax_edit.setText("100")
    viewer.imp_vrm_r_edit.setText("5")       # mΩ
    viewer.imp_vrm_l_edit.setText("20")      # nH
    viewer._on_impedance_apply()

    cfg = viewer._caploop_rail_config("+3V3")
    assert cfg["ripple_pct"] == 2.0
    assert cfg["transient_current_a"] == 10.0
    assert cfg["f_max_hz"] == 100e6
    assert cfg["vrm_r_ohm"] == pytest.approx(5e-3)
    assert cfg["vrm_l_h"] == pytest.approx(20e-9)
    assert viewer._project.viewer_settings["caploop_rails"]["+3V3"]

    # 3.3 V × 2 % / 10 A = 6.6 mΩ
    assert viewer._compute_rail_impedance("+3V3").target.z_target_ohm == \
        pytest.approx(6.6e-3)


def test_invalid_mask_input_is_rejected_without_persisting(viewer, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(av.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a[2]))
    viewer.imp_itran_edit.setText("not a number")
    viewer._on_impedance_apply()
    assert warned and "not a number" in warned[0]
    assert "caploop_rails" not in viewer._project.viewer_settings


def test_transient_current_loads_with_dot_decimal_notation(viewer):
    viewer._set_caploop_rail_config("+3V3", {
        **viewer._caploop_rail_config("+3V3"),
        "transient_current_a": 0.5,
    })
    viewer._load_impedance_rail_config("+3V3")
    assert viewer.imp_itran_edit.text() == "0.5"


def test_transient_current_accepts_comma_decimal_on_apply(viewer):
    viewer.imp_itran_edit.setText("5,00E-01")
    viewer._on_impedance_apply()
    assert viewer._caploop_rail_config("+3V3")["transient_current_a"] == 0.5


def test_impedance_mask_edits_use_standard_notation_validator(viewer):
    from PySide6.QtGui import QDoubleValidator

    for edit in (viewer.imp_ripple_edit, viewer.imp_itran_edit,
                 viewer.imp_fmax_edit, viewer.imp_vrm_r_edit,
                 viewer.imp_vrm_l_edit):
        validator = edit.validator()
        assert isinstance(validator, QDoubleValidator)
        assert validator.notation() == QDoubleValidator.StandardNotation


def test_vrm_resistance_sets_the_dc_floor(viewer):
    viewer._set_caploop_rail_config("+3V3", {
        **viewer._caploop_rail_config("+3V3"), "vrm_r_ohm": 4e-3})
    r = viewer._compute_rail_impedance("+3V3")
    assert abs(r.z_ohm[0]) == pytest.approx(4e-3, rel=0.02)


# --- the plot ---------------------------------------------------------------


def test_plot_draws_the_trace_and_the_mask(viewer):
    ax = viewer._imp_axes
    labels = [line.get_label() for line in ax.lines]
    assert any("|Z|" in str(l) for l in labels)
    assert ax.get_xscale() == "log" and ax.get_yscale() == "log"
    assert "Z_target" in ax.get_legend_handles_labels()[1][-1]


def test_plot_x_axis_is_pinned_to_the_swept_band(viewer):
    """The F_MAX rule and the mask stop short of the sweep's ends; without a
    pinned limit matplotlib autoscales to them and leaves dead space."""
    result = viewer._compute_rail_impedance("+3V3")
    lo, hi = viewer._imp_axes.get_xlim()
    assert lo == pytest.approx(result.freqs_hz[0])
    assert hi == pytest.approx(result.freqs_hz[-1])


def test_showing_individual_branches_adds_a_curve_per_capacitor(viewer):
    before = len(viewer._imp_axes.lines)
    viewer.imp_show_branches.setChecked(True)
    assert len(viewer._imp_axes.lines) > before


def _empty_plot(v) -> str:
    v._replot_impedance()          # no rail selected
    assert v._imp_axes.texts
    return v._imp_axes.texts[0].get_text()


def test_empty_rail_list_draws_an_explanation_not_a_crash(qapp):
    v = _Viewer()
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")
    v._populate_caps_table()
    v._impedance_tab_index = v.tabs.addTab(v._build_impedance_tab(),
                                           "Impedance")
    # Every capacitor excluded: the rail list is empty, but the reason is the
    # exclusion, not a missing rail.
    v._set_cap_override("C1", include=False)
    text = _empty_plot(v)
    assert "excluded" in text and "Tick Use" in text


def test_empty_state_reuses_the_capacitors_tab_reason(qapp):
    """A Gerber import carries no component data at all. Telling the user "no
    rail carries an included decoupling capacitor" sends them hunting for a
    capacitor that was never going to be found."""
    import dataclasses

    v = _Viewer()
    v._loaded_project = types.SimpleNamespace(
        extracted=dataclasses.replace(_project_with(), pcb_components=()))
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")
    v._populate_caps_table()
    v._impedance_tab_index = v.tabs.addTab(v._build_impedance_tab(),
                                           "Impedance")
    text = _empty_plot(v)
    assert "Gerber" in text
    assert "No rail" not in text


def test_empty_state_before_the_capacitors_tab_has_run(qapp):
    v = _Viewer()
    v._caps_rows_cache = None
    v._impedance_tab_index = v.tabs.addTab(v._build_impedance_tab(),
                                           "Impedance")
    assert "Open the Capacitors tab" in _empty_plot(v)


def test_empty_state_is_drawn_in_the_app_theme(qapp):
    """An unstyled axes is white with black text — near-invisible on the dark
    theme, which is what makes an empty plot look like a broken one."""
    v = _Viewer()
    v._caps_rows_cache = None
    v._impedance_tab_index = v.tabs.addTab(v._build_impedance_tab(),
                                           "Impedance")
    v._replot_impedance()
    theme = av._T()
    assert v._imp_axes.texts[0].get_color() == theme["fg"]
    assert v._imp_figure.get_facecolor() == \
        matplotlib.colors.to_rgba(theme["bg"])
    # And the stale readouts are cleared, not left showing another rail's.
    assert v.imp_ztarget_label.text() == "—"
    assert v.imp_plane_label.text() == "—"


# --- plane-pair capacitance ---------------------------------------------------


def test_plane_capacitance_is_computed_and_explained(viewer):
    c, note = viewer._rail_plane_capacitance_f("+3V3")
    assert c > 0.0
    assert "PWR Plane ↔ GND Plane" in note
    # The fixture stackup carries no Dk, so the fallback must be declared.
    assert "not in the stackup" in note


def test_plane_capacitance_can_be_excluded(viewer):
    viewer.imp_plane_check.setChecked(False)
    assert viewer._compute_rail_impedance("+3V3").c_plane_f == 0.0
    viewer.imp_plane_check.setChecked(True)
    assert viewer._compute_rail_impedance("+3V3").c_plane_f > 0.0


# --- the package library ---------------------------------------------------------


def test_package_table_lists_every_smd_case_size(viewer):
    from fypa.caploop.packages import DEFAULT_PACKAGE_MODELS
    assert viewer.imp_pkg_table.rowCount() == len(DEFAULT_PACKAGE_MODELS)
    assert viewer.imp_pkg_table.item(0, 0).text() == "01005"


def test_editing_a_package_reaches_every_cap_of_that_size(viewer):
    table = viewer.imp_pkg_table
    row = next(r for r in range(table.rowCount())
               if table.item(r, 0).text() == "0402")
    before = viewer._compute_rail_impedance("+3V3").branches[0].esl_h

    table.item(row, 1).setText("1.5")       # ESL, nH
    after = viewer._compute_rail_impedance("+3V3").branches[0]
    assert after.esl_h == pytest.approx(1.5e-9)
    assert after.esl_h > before
    # …and it persists.
    assert viewer._project.viewer_settings["caploop_packages"]["0402"][
        "esl_h"] == pytest.approx(1.5e-9)


def test_a_bad_package_edit_reverts_to_the_previous_value(viewer):
    table = viewer.imp_pkg_table
    row = next(r for r in range(table.rowCount())
               if table.item(r, 0).text() == "0402")
    table.item(row, 1).setText("banana")
    lib = viewer._caploop_package_library()
    assert table.item(row, 1).text() == f"{lib.get('0402').esl_nh:.4g}"
    assert lib.is_default("0402")


def test_resetting_the_library_restores_the_defaults(viewer):
    lib = viewer._caploop_package_library()
    lib.set_values("0402", 9e-9, 9e-3)
    viewer._on_package_reset()
    assert viewer._caploop_package_library().is_default("0402")
    assert viewer._project.viewer_settings["caploop_packages"] == {}


# --- per-part overrides feed the model ------------------------------------------------


def test_a_per_part_override_beats_the_package_in_the_model(viewer):
    viewer._set_cap_override("C1", esl_h=0.1e-9, esr_ohm=1e-3)
    branch = viewer._compute_rail_impedance("+3V3").branches[0]
    assert branch.esl_h == pytest.approx(0.1e-9)
    assert branch.esr_ohm == pytest.approx(1e-3)
    assert branch.esl_is_override and branch.esr_is_override


def test_a_non_smd_part_is_excluded_with_a_reason(qapp):
    import dataclasses
    proj = _project_with()
    proj = dataclasses.replace(proj, pcb_components=(
        dataclasses.replace(proj.pcb_components[0],
                            footprint="FP-TCJD-MFG"),))
    v = _Viewer(project_data=proj)
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")
    v._populate_caps_table()
    v._impedance_tab_index = v.tabs.addTab(v._build_impedance_tab(),
                                           "Impedance")
    r = v._compute_rail_impedance("+3V3")
    assert r.branches == []
    assert r.skipped and "unsupported package" in r.skipped[0][1]

    # …until the user supplies both parasitics by hand.
    v._set_cap_override("C1", esl_h=2.5e-9, esr_ohm=50e-3)
    r2 = v._compute_rail_impedance("+3V3")
    assert len(r2.branches) == 1 and r2.skipped == []
    assert r2.branches[0].package is None


def test_summary_reports_the_verdict_and_exclusions(viewer):
    html = viewer.imp_summary_label.text()
    assert "capacitor(s) modelled" in html
    assert "Meets target" in html or "Misses target" in html


def test_mounted_inductance_moves_the_self_resonance(viewer):
    """The whole point of wiring Z(f) to the loop-inductance extraction: a
    worse mount pushes the capacitor's useful band down."""
    r = viewer._compute_rail_impedance("+3V3")
    branch = r.branches[0]
    assert branch.l_mount_h > 0.0
    good = branch.srf_hz

    row = viewer._caps_rows_cache[0]
    row["l3_nh"] = row["l1_nh"] * 4.0        # a much worse mount
    worse = viewer._compute_rail_impedance("+3V3").branches[0]
    assert worse.l_mount_h > branch.l_mount_h
    assert worse.srf_hz < good
    assert math.isfinite(worse.srf_hz)
