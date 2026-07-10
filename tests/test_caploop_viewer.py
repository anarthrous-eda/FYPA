"""Capacitors-tab logic in the viewer, exercised without a live GUI.

The row build, warn/flag classification, override write-back and overlay
triangle emission are pure functions of the loaded design + project file, so
they are driven here through a stand-in object carrying just the attributes
those methods read. That keeps the real defect surface (report shape,
override semantics, overlay signature) under test without spinning up Qt.
"""
from __future__ import annotations

import types

import pytest

from fypa.altium_viewer import PdnViewer
from fypa.caploop.constants import CapLoopSettings
from fypa.project_file import ProjectFile
from tests.test_caploop_identify import (
    RAILS,
    _directives,
    _standard_cap_project,
)


class _FakeViewer:
    """Enough of PdnViewer for the cap-report / override methods."""

    _CAPS_TABLE_COLUMNS = PdnViewer._CAPS_TABLE_COLUMNS
    _CAPS_ACTION_COL = PdnViewer._CAPS_ACTION_COL

    # Methods under test, bound unchanged from the real class.
    _cap_net_layer_shapes = PdnViewer._cap_net_layer_shapes
    _caploop_package_library = PdnViewer._caploop_package_library
    _compute_cap_report = PdnViewer._compute_cap_report
    _get_or_compute_cap_rows = PdnViewer._get_or_compute_cap_rows
    _caploop_settings = PdnViewer._caploop_settings
    _cap_l_best_nh = PdnViewer._cap_l_best_nh
    _cap_row_is_warn = PdnViewer._cap_row_is_warn
    _caps_overlay_signature = PdnViewer._caps_overlay_signature
    _emit_cap_inductance_overlay = PdnViewer._emit_cap_inductance_overlay
    _set_cap_override = PdnViewer._set_cap_override
    _invalidate_caps_cache = PdnViewer._invalidate_caps_cache
    _ensure_project = PdnViewer._ensure_project

    def __init__(self, extracted, directives=None, project=None):
        self._loaded_project = types.SimpleNamespace(extracted=extracted)
        self.metadata = {"directives": directives or []}
        self._rail_to_members = RAILS
        self._project = project
        self._caps_rows_cache = None
        self._caps_identity_cache = None
        self._caps_shapes_cache = None
        self._caps_table_populated = False
        self._display_dirty = False
        self._caploop_settings_obj = None
        self._caploop_package_lib = None


def _viewer(**kwargs) -> _FakeViewer:
    return _FakeViewer(_standard_cap_project(), **kwargs)


# --- the row report ---------------------------------------------------------


def test_cap_report_builds_a_row_per_capacitor():
    rows = _viewer(directives=_directives())._get_or_compute_cap_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["designator"] == "C1"
    assert row["rail"] == "+3V3"
    assert row["vias_str"] == "2+2"
    assert row["target_label"] == "U5"
    assert row["design_voltage_v"] == pytest.approx(3.3)
    assert row["l1_nh"] > 0.0
    # Tier 2/3 stay empty until the user runs the solve.
    assert row["l2_nh"] is None and row["l3_nh"] is None
    assert row["cavity_str"] == "PWR Plane ↔ GND Plane"


def test_cap_report_row_cache_is_reused():
    v = _viewer()
    first = v._get_or_compute_cap_rows()
    assert v._get_or_compute_cap_rows() is first


def test_cap_report_empty_without_design_info():
    v = _viewer()
    v._loaded_project = None
    assert v._get_or_compute_cap_rows() == []
    assert "Reload Design Info" in v._caps_empty_reason


def test_cap_report_explains_gerber_projects():
    import dataclasses
    v = _viewer()
    v._loaded_project = types.SimpleNamespace(
        extracted=dataclasses.replace(_standard_cap_project(),
                                      pcb_components=()))
    assert v._get_or_compute_cap_rows() == []
    assert "Gerber" in v._caps_empty_reason


# --- warn classification -------------------------------------------------------


def test_best_l_prefers_the_highest_tier():
    v = _viewer()
    assert v._cap_l_best_nh({"l1_nh": 1.0, "l2_nh": 2.0, "l3_nh": 3.0}) == 3.0
    assert v._cap_l_best_nh({"l1_nh": 1.0, "l2_nh": 2.0, "l3_nh": None}) == 2.0
    assert v._cap_l_best_nh({"l1_nh": 1.0, "l2_nh": None, "l3_nh": None}) == 1.0
    assert v._cap_l_best_nh({"l1_nh": None}) is None


def test_flagged_row_warns_and_excluded_row_never_does():
    v = _viewer()
    flagged = {"included": True, "flags": ("single-via",), "l1_nh": 0.1}
    assert v._cap_row_is_warn(flagged)
    assert not v._cap_row_is_warn({**flagged, "included": False})


def test_high_inductance_row_warns_without_flags():
    v = _viewer()
    v._caploop_settings_obj = CapLoopSettings(cap_l_warn_nh=2.0)
    assert v._cap_row_is_warn({"included": True, "flags": (), "l1_nh": 2.5})
    assert not v._cap_row_is_warn({"included": True, "flags": (),
                                   "l1_nh": 1.5})


# --- overrides -------------------------------------------------------------------


def test_exclude_override_persists_and_recomputes():
    project = ProjectFile()
    v = _viewer(project=project)
    v._get_or_compute_cap_rows()
    v._set_cap_override("C1", include=False)
    assert project.cap_override_maps()[0] == {"C1": False}
    # The cache was dropped, so the next read reflects the override.
    assert v._get_or_compute_cap_rows()[0]["included"] is False
    assert v._display_dirty


def test_target_override_persists_and_repoints_the_loop():
    project = ProjectFile()
    v = _viewer(directives=_directives(), project=project)
    v._set_cap_override("C1", target_label="U9")
    row = v._get_or_compute_cap_rows()[0]
    assert row["target_label"] == "U9"
    assert row["target_is_override"]


def test_settings_change_invalidates_the_rows():
    v = _viewer()
    first = v._get_or_compute_cap_rows()
    # Nothing is within 0.01 mm of a pad, so no via is even a candidate.
    v._caploop_settings_obj = CapLoopSettings(escape_via_search_mm=0.01)
    v._invalidate_caps_cache(repopulate=False)
    rows = v._get_or_compute_cap_rows()
    assert rows is not first
    assert "no-escape-via" in rows[0]["flags"]


def test_cluster_limit_keeps_the_nearest_via_as_a_single_escape():
    """Tightening the cluster limit below every candidate must not strand the
    capacitor: the nearest via inside the search radius is kept as the only
    escape (flagged single-via), never dropped."""
    v = _viewer()
    v._caploop_settings_obj = CapLoopSettings(escape_via_max_dist_mm=0.01)
    row = v._get_or_compute_cap_rows()[0]
    assert row["vias_str"] == "1+1"
    assert "single-via" in row["flags"]
    assert "no-escape-via" not in row["flags"]


# --- heatmap overlay ---------------------------------------------------------------


def _emit_collector():
    emitted = []

    def _emit(verts_xy, z, rgb, *, under=False, top=False, alpha=1.0):
        emitted.append((verts_xy, z, rgb, top, alpha))

    return _emit, emitted


def test_overlay_is_a_no_op_when_unchecked():
    v = _viewer()
    v.caps_overlay_box = types.SimpleNamespace(isChecked=lambda: False)
    _emit, emitted = _emit_collector()
    labels: list[dict] = []
    v._emit_cap_inductance_overlay(_emit, labels, True,
                                   {"top": 0.0, "bottom": -1.0})
    assert emitted == [] and labels == []
    assert v._caps_overlay_signature() == ()


def test_overlay_emits_pad_triangles_and_labels():
    v = _viewer()
    v.caps_overlay_box = types.SimpleNamespace(isChecked=lambda: True)
    v._get_or_compute_cap_rows()
    _emit, emitted = _emit_collector()
    labels: list[dict] = []
    v._emit_cap_inductance_overlay(_emit, labels, True,
                                   {"top": 0.5, "bottom": -0.5})
    # Two pads → two fans, each a multiple of 3 vertices, on the top side.
    assert len(emitted) == 2
    for verts, z, _rgb, top, _alpha in emitted:
        assert verts.shape[0] % 3 == 0 and verts.shape[1] == 2
        assert z == 0.5 and top is True
    assert len(labels) == 1
    assert labels[0]["text"].startswith("C1 ")
    assert labels[0]["text"].endswith(" nH")


def test_overlay_signature_tracks_inductance_changes():
    v = _viewer()
    v.caps_overlay_box = types.SimpleNamespace(isChecked=lambda: True)
    rows = v._get_or_compute_cap_rows()
    sig_before = v._caps_overlay_signature()
    rows[0]["l3_nh"] = 9.9        # a Tier-3 solve landed
    assert v._caps_overlay_signature() != sig_before


def test_overlay_skips_excluded_capacitors():
    project = ProjectFile()
    v = _viewer(project=project)
    v.caps_overlay_box = types.SimpleNamespace(isChecked=lambda: True)
    v._set_cap_override("C1", include=False)
    _emit, emitted = _emit_collector()
    labels: list[dict] = []
    v._emit_cap_inductance_overlay(_emit, labels, True,
                                   {"top": 0.0, "bottom": -1.0})
    assert emitted == [] and labels == []
