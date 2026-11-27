"""Board editor form: pin fields are checkbox lists, as in Manual setup.

The OUT / GND / IN (and P / N) pins of a component directive are picked
from the component's pads on the chosen net. "All" stores ``None``; a saved
restriction comes back ticked; a new net starts at All.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from fypa.project_file import EditorDirective, ProjectFile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _pad(des, net):
    return {"designator": des, "net": net, "layer_ids": [1],
            "outline": [[0, 0], [1, 0], [1, 1], [0, 1]]}


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V
    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._project = ProjectFile()
    v.solution = None
    v._solve_stale = False
    v.metadata = {
        "directives": [],
        "components": [
            {"designator": "U1", "side": "top", "bbox": [0, 0, 1, 1],
             "nets": ["VIN", "GND", "VOUT"]},
            {"designator": "J1", "side": "top", "bbox": [0, 0, 1, 1],
             "nets": ["VIN", "GND"]},
            {"designator": "J2", "side": "top", "bbox": [0, 0, 1, 1],
             "nets": ["VIN", "GND"]},
        ],
        "pads": [
            _pad("U1-1", "VIN"), _pad("U1-2", "GND"), _pad("U1-10", "VOUT"),
            _pad("U1-3", "VOUT"), _pad("U1-5", "GND"),
            _pad("J1-1", "VIN"), _pad("J1-2", "GND"),
            _pad("J2-1", "VIN"), _pad("J2-3", "VIN"),
        ],
    }
    v._mm_per_pixel = 0.01
    # Side effects of Apply that need a whole viewer.
    v._mark_project_dirty = lambda: None
    v._apply_editor_highlight = lambda *_a: None
    v._update_pending_rails = lambda: None
    v._render = lambda: None
    v._connected_nets = lambda n: {n}
    return v


def _open(v, des="U1"):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    host = QWidget()
    v._editor_form_host = host
    v._editor_form_layout = QVBoxLayout(host)
    v._multi_form_layout = QVBoxLayout()
    v._editor_multi = []
    v._update_editor_panel = lambda: None
    v._all_net_names_cache = ["GND", "VIN", "VOUT"]
    comp = next(c for c in v.metadata["components"]
                if c["designator"] == des)
    v._editor_selection = {"kind": "component", "designator": des,
                           "nets": comp["nets"], "bbox": None,
                           "unlocked": False}
    v._populate_editor_form()
    v._host = host  # keep the widgets alive
    return v


def _pads(picker):
    from PySide6.QtCore import Qt
    return [picker.list.item(i).data(Qt.UserRole)
            for i in range(picker.list.count())]


def _ticked(picker):
    from PySide6.QtCore import Qt
    return [picker.list.item(i).data(Qt.UserRole)
            for i in range(picker.list.count())
            if picker.list.item(i).checkState() == Qt.Checked]


def _make_regulator(v):
    v._ef_role.setCurrentText("REGULATOR")
    v._ef_pnet.setCurrentText("VOUT")
    v._ef_nnet.setCurrentText("GND")
    v._ef_innet.setCurrentText("VIN")
    v._ef_value.setText("3.3")


def test_fresh_regulator_lists_the_pads_of_each_net_all_ticked(viewer):
    v = _open(viewer)
    _make_regulator(v)
    assert v._ef_pins_label.text() == "OUT pins"
    assert v._ef_npins_label.text() == "GND pins"
    # Natural pad order: 3 before 10.
    assert _pads(v._ef_pins) == ["3", "10"]
    assert _pads(v._ef_npins) == ["2", "5"]
    assert _pads(v._ef_inpins) == ["1"]
    for picker in (v._ef_pins, v._ef_npins, v._ef_inpins):
        assert picker.all.isChecked()
        assert picker.value() is None
    assert "VOUT (2)" in v._ef_pins.all.text()


def test_apply_with_all_ticked_stores_none(viewer):
    v = _open(viewer)
    _make_regulator(v)
    v._on_editor_apply()
    (d,) = v._project.editor_directives
    assert d.role == "REGULATOR"
    assert d.p_pins is None and d.n_pins is None and d.in_p_pins is None


def test_saved_pins_come_back_ticked(viewer):
    viewer._project.upsert_directive(EditorDirective(
        role="REGULATOR", kind="component", designator="U1",
        single_net=False, p_net="VOUT", n_net="GND", voltage=3.3,
        p_pins=["10"], n_pins=["5"], in_p_net="VIN", in_n_net="GND",
        in_p_pins=["1"], regulator_type="LDO"))
    v = _open(viewer)
    assert not v._ef_pins.all.isChecked()
    assert _pads(v._ef_pins) == ["3", "10"]
    assert _ticked(v._ef_pins) == ["10"]
    assert _ticked(v._ef_npins) == ["5"]
    assert v._ef_inpins.value() == ["1"]
    v._on_editor_apply()
    (d,) = v._project.editor_directives
    assert d.p_pins == ["10"] and d.n_pins == ["5"] and d.in_p_pins == ["1"]


def test_unticking_all_then_one_pin_is_saved(viewer):
    from PySide6.QtCore import Qt
    v = _open(viewer)
    _make_regulator(v)
    v._ef_pins.all.setChecked(False)
    v._ef_pins.list.item(0).setCheckState(Qt.Unchecked)
    assert v._ef_pins.value() == ["10"]
    v._on_editor_apply()
    assert v._project.editor_directives[0].p_pins == ["10"]


def test_nothing_ticked_blocks_apply(viewer):
    from PySide6.QtCore import Qt
    v = _open(viewer)
    _make_regulator(v)
    v._ef_npins.all.setChecked(False)
    for i in range(v._ef_npins.list.count()):
        v._ef_npins.list.item(i).setCheckState(Qt.Unchecked)
    v._on_editor_apply()
    assert not v._project.editor_directives
    assert "GND pins" in v._ef_status.text()


def test_a_new_net_starts_at_all(viewer):
    viewer._project.upsert_directive(EditorDirective(
        role="SOURCE", kind="component", designator="U1",
        single_net=True, p_net="VOUT", voltage=3.3, p_pins=["3"]))
    v = _open(viewer)
    assert v._ef_pins.value() == ["3"]
    v._ef_pnet.setCurrentText("VIN")
    assert v._ef_pins.all.isChecked()
    assert _pads(v._ef_pins) == ["1"]
    # Back to the first net: still All - the old ticks were for a net the
    # user moved away from.
    v._ef_pnet.setCurrentText("VOUT")
    assert v._ef_pins.value() is None


def test_role_change_keeps_the_ticks_on_the_same_net(viewer):
    viewer._project.upsert_directive(EditorDirective(
        role="SOURCE", kind="component", designator="U1",
        single_net=True, p_net="VOUT", voltage=3.3, p_pins=["3"]))
    v = _open(viewer)
    v._ef_role.setCurrentText("SINK")
    assert v._ef_pins.value() == ["3"]


def test_des_lists_the_pads_of_those_parts(viewer):
    v = _open(viewer, "J1")
    v._ef_role.setCurrentText("SOURCE")
    v._ef_two.setChecked(True)
    v._ef_pnet.setCurrentText("VIN")
    assert _pads(v._ef_pins) == ["1"]
    v._ef_pdes.setText("J1, J2")
    v._ef_pdes.editingFinished.emit()
    assert _pads(v._ef_pins) == ["1", "3"]
    assert "their pins" in v._ef_pins.all.text()
    labels = [v._ef_pins.list.item(i).text()
              for i in range(v._ef_pins.list.count())]
    assert labels == ["1   J1, J2", "3   J2"]


def test_a_net_without_the_parts_pads_shows_all(viewer):
    viewer._project.upsert_directive(EditorDirective(
        role="SOURCE", kind="component", designator="J1",
        single_net=False, p_net="VIN", n_net="VOUT", voltage=5.0,
        n_pins=["9"]))
    v = _open(viewer, "J1")
    # J1 has no pad on VOUT: nothing to pick, so All rather than an
    # empty list Apply would refuse.
    assert v._ef_npins.value() is None
    assert not v._ef_npins.all.isEnabled()


def test_free_marker_hides_the_pickers(viewer):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    d = EditorDirective(role="SINK", kind="free", single_net=True,
                        p_net="VIN", current=1.0)
    viewer._project.upsert_directive(d)
    host = QWidget()
    viewer._editor_form_host = host
    viewer._editor_form_layout = QVBoxLayout(host)
    viewer._multi_form_layout = QVBoxLayout()
    viewer._editor_multi = []
    viewer._update_editor_panel = lambda: None
    viewer._all_net_names_cache = ["GND", "VIN", "VOUT"]
    viewer._editor_selection = {"kind": "free", "id": d.id}
    viewer._build_free_marker_location = lambda *_a: None
    viewer._populate_editor_form()
    assert viewer._ef_pins.isHidden()
    assert viewer._ef_npins.isHidden()
