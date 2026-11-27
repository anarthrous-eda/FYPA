"""View > Markers always on top.

The same remembered preference as the Settings tab's Display options
checkbox: on by default, toggled from either place with the other kept in
step, applied live to the board view and saved for the next launch.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class _FakeGL:
    def __init__(self, on: bool):
        self._on = on

    def set_markers_always_on_top(self, on: bool) -> None:
        self._on = bool(on)

    def markers_always_on_top(self) -> bool:
        return self._on


@pytest.fixture
def viewer(qapp):
    """Stub viewer with the real View menu and the Settings checkbox
    (``PdnViewer.__init__`` would open a whole board)."""
    from PySide6.QtWidgets import QCheckBox, QComboBox, QMainWindow
    import fypa.altium_viewer as V
    from fypa.viewer.prefs import load_markers_always_on_top

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    for name in ("view_3d_box", "heatmap_vias_box", "colour_stubs_box",
                 "show_mesh_box"):
        setattr(v, name, QCheckBox())
    v.relief_combo = QComboBox()
    v.relief_combo.addItems(["off", "per layer", "ignoring layers"])
    v._gl_viewer = _FakeGL(load_markers_always_on_top())
    v._settings_markers_on_top_check = QCheckBox()
    v._settings_markers_on_top_check.setChecked(
        v._gl_viewer.markers_always_on_top())
    v._settings_markers_on_top_check.toggled.connect(
        v._on_markers_on_top_toggled)
    V.PdnViewer._build_view_menu(v)
    v._refresh_view_menu_labels()
    return v


def test_default_is_on():
    from fypa.viewer.prefs import load_markers_always_on_top
    assert load_markers_always_on_top() is True


def test_menu_item_toggles_applies_saves_and_syncs_settings(viewer):
    from fypa.viewer.prefs import load_markers_always_on_top
    act = viewer._act_markers_on_top
    assert act.text() == "Markers always on top [enabled]"
    act.trigger()
    assert viewer._gl_viewer.markers_always_on_top() is False
    assert load_markers_always_on_top() is False
    assert not viewer._settings_markers_on_top_check.isChecked()
    assert act.text() == "Markers always on top [disabled]"
    act.trigger()
    assert viewer._gl_viewer.markers_always_on_top() is True
    assert load_markers_always_on_top() is True
    assert viewer._settings_markers_on_top_check.isChecked()


def test_settings_checkbox_updates_menu_label(viewer):
    viewer._settings_markers_on_top_check.setChecked(False)
    assert viewer._gl_viewer.markers_always_on_top() is False
    assert (viewer._act_markers_on_top.text()
            == "Markers always on top [disabled]")
