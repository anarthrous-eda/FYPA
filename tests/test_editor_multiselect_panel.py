"""Marquee multi-select in the editor side panel.

Dragging a box round several sinks has to give the *same* form a single
sink gives, with a ``*`` standing in wherever the selected sinks disagree,
and one Apply that writes only the rows the user touched. These drive the
real panel widgets through a stubbed :class:`~fypa.altium_viewer.PdnViewer`
(its ``__init__`` opens a whole board) so the ``*`` merge, the dirty-row
tracking, the batch undo and the mixed-selection bail-out are pinned to
the widgets the user actually sees.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from fypa.editor_multiselect import normalise_rect  # noqa: E402
from fypa.project_file import EditorDirective, ProjectFile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# Three sinks in a row, each a single-pad component 10 mm apart, plus one
# source further out. Pads are the marker glyph positions the marquee tests.
# Pad positions per part. Every part carries a pad on each of NETS, so a
# sink pointed at a different rail still has a glyph on screen.
PADS = {
    "U1": [(10.0, 10.0)],
    "U2": [(20.0, 10.0)],
    "U3": [(30.0, 10.0)],
    "J1": [(90.0, 10.0)],
    # A two-pad part: the marquee must enclose BOTH pads to select it.
    "U4": [(40.0, 10.0), (140.0, 10.0)],
}
NETS = ("+3V3", "+1V8", "GND")


def _components():
    return [{
        "designator": des,
        "nets": list(NETS),
        "bbox": (min(x for x, _ in pts) - 1.0, 9.0,
                 max(x for x, _ in pts) + 1.0, 11.0),
        "side": "top",
    } for des, pts in PADS.items()]


@pytest.fixture
def viewer(qapp, monkeypatch):
    from PySide6.QtWidgets import QMainWindow, QMessageBox
    import fypa.altium_viewer as V

    # Remove always confirms; the offscreen platform would block on a modal.
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **k: QMessageBox.Yes))

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {"components": _components(), "directives": []}
    v._project = ProjectFile()
    v._project_path = None
    v._editor_mode = True
    v._editor_selection = None
    v._editor_multi = []
    v._editor_pending_marker = None
    v._marker_drag = None
    v._mf_dirty = set()
    v._marker_undo = []
    v._marker_redo = []
    v._copper_selection = None
    v._phys_name_to_layer_id = {"Top Layer": 1}
    v._project_dirty = False

    v._ensure_project = lambda: v._project
    v._mark_project_dirty = lambda: setattr(v, "_project_dirty", True)
    v._update_pending_rails = lambda: None
    v._render = lambda: None
    v._highlighted = set()
    v._apply_editor_highlight = lambda nets, polys=None: v._highlighted.update(
        nets)
    v._clear_editor_highlight = lambda: v._highlighted.clear()
    v._connected_nets = lambda net: ({net} if net else set())
    v._all_net_names = lambda: ["+3V3", "+1V8", "GND"]
    v._visible_layer_ids = lambda: {1}
    v._directive_rail_visible = lambda d: True
    v._component_center = lambda des: None
    v._free_marker_layer_id = lambda d: d.layer_id
    v._layer_color_for = lambda phys: None
    v._layer_z_for = lambda phys: 0.0
    v._select_component = lambda rec: setattr(
        v, "_editor_selection",
        {"kind": "component", "designator": rec["designator"],
         "nets": list(rec.get("nets") or []), "bbox": rec.get("bbox"),
         "unlocked": False})
    v._select_free_marker = lambda d: setattr(
        v, "_editor_selection", {"kind": "free", "id": d.id})

    def _pad_points(des, nets, visible_layer_ids=None,
                    with_layer_color=False, pin_filter=None):
        pts = []
        for net in nets:
            if net in NETS:
                pts.extend(PADS.get(des, []))
        if with_layer_color:
            return [(x, y, None, 0.0) for x, y in pts]
        return pts

    v._component_pad_points = _pad_points

    class _GL:
        def set_primitive_selection_outline(self, rings):
            pass

        def set_editor_selection_bboxes(self, boxes):
            self.boxes = list(boxes)

        def view_center_scale(self):
            return (0.0, 0.0, 0.1)

    v._gl_viewer = _GL()
    v._editor_panel_width = V.PdnViewer._EDITOR_PANEL_DEFAULT_W
    # Hold a reference: the panel is parentless here, so dropping it would
    # have Python collect it and its child widgets out from under us.
    v._test_panel = V.PdnViewer._build_editor_panel(v)
    v._editor_panel = v._test_panel
    return v


def _sink(des: str, **kw) -> EditorDirective:
    base = {"kind": "component", "role": "SINK", "designator": des,
            "single_net": True, "p_net": "+3V3", "current": 0.5}
    base.update(kw)
    return EditorDirective(**base)


def _sweep(v, x0, y0, x1, y1, additive=False, toggle=False):
    v._on_editor_marquee(x0, y0, x1, y1, additive, toggle)


def _labels(v) -> list[str]:
    return [e["label"] for e in v._editor_multi]


# --- what the box catches ------------------------------------------------

def test_a_box_round_three_sinks_selects_all_three(viewer):
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 35, 20)
    assert sorted(_labels(viewer)) == ["U1", "U2", "U3"]


def test_a_sink_outside_the_box_is_not_selected(viewer):
    for des in ("U1", "U2", "J1"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 35, 20)
    assert sorted(_labels(viewer)) == ["U1", "U2"]


def test_a_partly_enclosed_multi_pad_sink_is_not_selected(viewer):
    """U4 glyphs at two pads 100 mm apart. Clipping one must not drag the
    whole sink in — that is the difference between enclose and touch."""
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(_sink("U2"))
    viewer._project.upsert_directive(_sink("U4"))
    _sweep(viewer, 0, 0, 45, 20)     # covers U4's first pad only
    assert sorted(_labels(viewer)) == ["U1", "U2"]
    _sweep(viewer, 0, 0, 145, 20)    # covers both of U4's pads
    assert "U4" in _labels(viewer)


def test_one_enclosed_sink_collapses_to_the_ordinary_single_selection(viewer):
    viewer._project.upsert_directive(_sink("U1"))
    _sweep(viewer, 0, 0, 15, 20)
    assert viewer._editor_multi == []
    assert viewer._editor_selection["designator"] == "U1"


def test_an_empty_sweep_clears_the_selection(viewer):
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(_sink("U2"))
    _sweep(viewer, 0, 0, 35, 20)
    assert len(viewer._editor_multi) == 2
    _sweep(viewer, 200, 200, 260, 260)
    assert viewer._editor_multi == []
    assert viewer._editor_selection is None


def test_copper_and_roleless_parts_are_never_candidates(viewer):
    """Nothing on the board carries a PDN role, so a box over all of it
    selects nothing — otherwise every drag would catch stray passives and
    degrade to 'multiple objects selected'."""
    _sweep(viewer, -100, -100, 500, 500)
    assert viewer._editor_multi == []


def test_shift_drag_adds_to_the_selection(viewer):
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 25, 20)                      # U1, U2
    _sweep(viewer, 26, 0, 35, 20, additive=True)      # + U3
    assert sorted(_labels(viewer)) == ["U1", "U2", "U3"]


def test_ctrl_drag_trims_the_selection(viewer):
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 35, 20)
    _sweep(viewer, 26, 0, 35, 20, toggle=True)        # drop U3
    assert sorted(_labels(viewer)) == ["U1", "U2"]


# --- the form: shared values vs * ---------------------------------------

def test_shared_values_show_as_values_and_differing_ones_as_a_star(viewer):
    viewer._project.upsert_directive(_sink("U1", current=0.5, min_voltage=3.0))
    viewer._project.upsert_directive(_sink("U2", current=1.25, min_voltage=3.0))
    _sweep(viewer, 0, 0, 25, 20)
    assert viewer._mf_current.text() == "*"        # 0.5 vs 1.25
    assert viewer._mf_min_v.text() == "3"          # both 3.0
    assert viewer._mf_pnet.currentText() == "+3V3"  # same rail
    assert viewer._mf_single.isChecked()           # both single-net


def test_a_shared_unset_field_is_blank_not_a_star(viewer):
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(_sink("U2"))
    _sweep(viewer, 0, 0, 25, 20)
    assert viewer._mf_min_v.text() == ""


def test_a_mixed_current_model_leaves_both_radios_unchecked(viewer):
    viewer._project.upsert_directive(_sink("U1", single_net=True))
    viewer._project.upsert_directive(
        _sink("U2", single_net=False, n_net="GND"))
    _sweep(viewer, 0, 0, 25, 20)
    assert not viewer._mf_single.isChecked()
    assert not viewer._mf_two.isChecked()


def test_a_mixed_net_gets_a_star_entry_at_the_top_of_the_combo(viewer):
    viewer._project.upsert_directive(_sink("U1", p_net="+3V3"))
    viewer._project.upsert_directive(_sink("U2", p_net="+1V8"))
    _sweep(viewer, 0, 0, 25, 20)
    assert viewer._mf_pnet.itemText(0) == "*"
    assert viewer._mf_pnet.currentText() == "*"


def test_the_header_counts_the_selected_sinks(viewer):
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 35, 20)
    texts = _panel_texts(viewer)
    assert any("3 SINKs selected" in t for t in texts)
    assert any("U1, U2, U3" in t for t in texts)


def _panel_texts(v) -> list[str]:
    from PySide6.QtWidgets import QLabel
    return [w.text() for w in v._multi_form_host.findChildren(QLabel)]


# --- mixed selections ----------------------------------------------------

def test_a_mixed_role_selection_shows_only_text_and_no_controls(viewer):
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(_sink("U2"))
    viewer._project.upsert_directive(
        EditorDirective(kind="component", role="SOURCE", designator="U3",
                        single_net=True, p_net="+3V3", voltage=3.3))
    _sweep(viewer, 0, 0, 35, 20)
    assert len(viewer._editor_multi) == 3
    assert any("Multiple objects selected" in t for t in _panel_texts(viewer))
    # No editable controls at all for a mixed bag.
    assert viewer._mf_current is None
    assert viewer._mf_pnet is None
    from PySide6.QtWidgets import QLineEdit, QPushButton
    assert viewer._multi_form_host.findChildren(QLineEdit) == []
    assert viewer._multi_form_host.findChildren(QPushButton) == []


def test_a_mixed_selection_names_the_roles_it_found(viewer):
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(
        EditorDirective(kind="component", role="SOURCE", designator="U2",
                        single_net=True, p_net="+3V3", voltage=3.3))
    _sweep(viewer, 0, 0, 25, 20)
    joined = " ".join(_panel_texts(viewer))
    assert "1 × SINK" in joined and "1 × SOURCE" in joined


# --- Apply ---------------------------------------------------------------

def _currents(v, *des) -> list:
    by = {d.designator: d for d in v._project.editor_directives}
    return [by[x].current for x in des]


def test_apply_writes_an_edited_row_to_every_selected_sink(viewer):
    viewer._project.upsert_directive(_sink("U1", current=0.5))
    viewer._project.upsert_directive(_sink("U2", current=1.25))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    assert _currents(viewer, "U1", "U2") == [2.0, 2.0]


def test_apply_leaves_untouched_rows_alone_on_every_sink(viewer):
    """The whole point of ``*``: editing Current must not flatten Min V,
    which the two sinks disagree on."""
    viewer._project.upsert_directive(_sink("U1", current=0.5, min_voltage=3.0))
    viewer._project.upsert_directive(_sink("U2", current=1.25,
                                           min_voltage=1.7))
    _sweep(viewer, 0, 0, 25, 20)
    assert viewer._mf_min_v.text() == "*"
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    by = {d.designator: d for d in viewer._project.editor_directives}
    assert by["U1"].min_voltage == 3.0
    assert by["U2"].min_voltage == 1.7


def test_apply_with_nothing_edited_changes_nothing(viewer):
    viewer._project.upsert_directive(_sink("U1", current=0.5))
    viewer._project.upsert_directive(_sink("U2", current=1.25))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._on_editor_multi_apply()
    assert _currents(viewer, "U1", "U2") == [0.5, 1.25]
    assert "Nothing to apply" in viewer._mf_status.text()


def test_clearing_min_v_and_applying_drops_the_check_on_all_of_them(viewer):
    viewer._project.upsert_directive(_sink("U1", min_voltage=3.0))
    viewer._project.upsert_directive(_sink("U2", min_voltage=1.7))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_min_v.setText("")
    viewer._mf_dirty.add("min_voltage")
    viewer._on_editor_multi_apply()
    by = {d.designator: d for d in viewer._project.editor_directives}
    assert by["U1"].min_voltage is None
    assert by["U2"].min_voltage is None


def test_a_combo_left_on_the_star_placeholder_is_not_a_choice(viewer):
    """Re-picking ``*`` must read as 'leave it alone', not write a net
    literally called ``*``."""
    viewer._project.upsert_directive(_sink("U1", p_net="+3V3"))
    viewer._project.upsert_directive(_sink("U2", p_net="+1V8"))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_dirty.add("p_net")          # as if the user reselected ``*``
    viewer._on_editor_multi_apply()
    by = {d.designator: d for d in viewer._project.editor_directives}
    assert by["U1"].p_net == "+3V3"
    assert by["U2"].p_net == "+1V8"


def test_a_non_numeric_current_is_refused_and_writes_nothing(viewer):
    viewer._project.upsert_directive(_sink("U1", current=0.5))
    viewer._project.upsert_directive(_sink("U2", current=1.25))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("nope")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    assert _currents(viewer, "U1", "U2") == [0.5, 1.25]
    assert "must be a number" in viewer._mf_status.text()


def test_switching_to_two_nets_without_an_n_net_rolls_the_whole_batch_back(
        viewer):
    """Half the selection left in an unsolvable two-net state would be
    worse than refusing the edit."""
    viewer._project.upsert_directive(_sink("U1", current=0.5))
    viewer._project.upsert_directive(_sink("U2", current=1.25))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._mf_two.setChecked(True)
    viewer._mf_dirty.add("single_net")
    viewer._on_editor_multi_apply()
    by = {d.designator: d for d in viewer._project.editor_directives}
    assert [by["U1"].single_net, by["U2"].single_net] == [True, True]
    assert _currents(viewer, "U1", "U2") == [0.5, 1.25]   # current too
    assert "needs an N net" in viewer._mf_status.text()


def test_two_nets_with_an_n_net_applies_to_all(viewer):
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(_sink("U2"))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_two.setChecked(True)
    viewer._mf_dirty.add("single_net")
    viewer._mf_nnet.setCurrentText("GND")
    viewer._mf_dirty.add("n_net")
    viewer._on_editor_multi_apply()
    for d in viewer._project.editor_directives:
        assert d.single_net is False and d.n_net == "GND"


def test_choosing_single_net_clears_the_n_net_on_all_of_them(viewer):
    viewer._project.upsert_directive(
        _sink("U1", single_net=False, n_net="GND"))
    viewer._project.upsert_directive(
        _sink("U2", single_net=False, n_net="GND"))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_single.setChecked(True)
    viewer._mf_dirty.add("single_net")
    viewer._on_editor_multi_apply()
    for d in viewer._project.editor_directives:
        assert d.single_net is True and d.n_net is None


def test_apply_flags_the_project_dirty_so_resolve_lights_up(viewer):
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(_sink("U2"))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._project_dirty = False
    viewer._on_editor_multi_apply()
    assert viewer._project_dirty is True


def test_the_form_stops_showing_a_star_once_apply_made_the_row_shared(viewer):
    viewer._project.upsert_directive(_sink("U1", current=0.5))
    viewer._project.upsert_directive(_sink("U2", current=1.25))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    assert viewer._mf_current.text() == "2"
    assert viewer._mf_dirty == set()


# --- batch undo / redo ---------------------------------------------------

def test_one_apply_is_one_undo_step(viewer):
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des, current=0.5))
    _sweep(viewer, 0, 0, 35, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    assert _currents(viewer, "U1", "U2", "U3") == [2.0, 2.0, 2.0]
    assert len(viewer._marker_undo) == 1
    viewer._undo_marker_action()
    assert _currents(viewer, "U1", "U2", "U3") == [0.5, 0.5, 0.5]
    assert viewer._marker_undo == []


def test_redo_puts_a_batch_apply_back(viewer):
    for des in ("U1", "U2"):
        viewer._project.upsert_directive(_sink(des, current=0.5))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    viewer._undo_marker_action()
    viewer._redo_marker_action()
    assert _currents(viewer, "U1", "U2") == [2.0, 2.0]


def test_undo_restores_the_selection_the_edit_was_made_against(viewer):
    for des in ("U1", "U2"):
        viewer._project.upsert_directive(_sink(des, current=0.5))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    viewer._undo_marker_action()
    assert sorted(_labels(viewer)) == ["U1", "U2"]


# --- batch remove --------------------------------------------------------

def test_remove_deletes_every_selected_sink_in_one_undo_step(viewer):
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 35, 20)
    viewer._on_editor_multi_remove()
    assert viewer._project.editor_directives == []
    assert len(viewer._marker_undo) == 1
    viewer._undo_marker_action()
    assert sorted(d.designator
                  for d in viewer._project.editor_directives) == [
        "U1", "U2", "U3"]


def test_delete_key_removes_a_whole_marquee_selection(viewer):
    for des in ("U1", "U2"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._delete_selected_free_marker()
    assert viewer._project.editor_directives == []


# --- locked (schematic-defined) sinks ------------------------------------

def _schematic_sink(des: str, current: float, min_v=None) -> dict:
    return {
        "designator": des,
        "role": "SINK",
        "value": current,
        "min_voltage": min_v,
        "schdoc": "board.SchDoc",
        "terminals": {
            "P": {"requested_net": "+3V3",
                  "pins": [{"pad": "1", "net": "+3V3", "component": des,
                            "x_mm": x, "y_mm": y}
                           for x, y in PADS[des]]},
        },
    }


def test_a_schematic_sink_is_selectable_and_shows_its_values_in_the_merge(
        viewer):
    viewer.metadata["directives"] = [_schematic_sink("U1", 0.5),
                                     _schematic_sink("U2", 0.5)]
    _sweep(viewer, 0, 0, 25, 20)
    assert sorted(_labels(viewer)) == ["U1", "U2"]
    assert all(e["kind"] == "schematic" for e in viewer._editor_multi)
    # Both draw 0.5 A per the schematic, so the row is shared, not ``*``.
    assert viewer._mf_current.text() == "0.5"


def test_the_panel_warns_that_locked_sinks_will_be_overridden(viewer):
    viewer.metadata["directives"] = [_schematic_sink("U1", 0.5),
                                     _schematic_sink("U2", 1.0)]
    _sweep(viewer, 0, 0, 25, 20)
    assert any("Altium schematic" in t for t in _panel_texts(viewer))


def test_apply_unlocks_locked_sinks_into_override_directives(viewer):
    viewer.metadata["directives"] = [_schematic_sink("U1", 0.5, min_v=3.0),
                                     _schematic_sink("U2", 1.0)]
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    by = {d.designator: d for d in viewer._project.editor_directives}
    assert sorted(by) == ["U1", "U2"]
    assert [by["U1"].current, by["U2"].current] == [2.0, 2.0]
    # Untouched rows keep what the schematic said, per sink.
    assert by["U1"].min_voltage == 3.0
    assert by["U2"].min_voltage is None
    # And each override is marked as replacing its schematic directive.
    assert by["U1"].overrides_designator == "U1"


def test_undoing_a_batch_unlock_removes_the_directives_it_created(viewer):
    viewer.metadata["directives"] = [_schematic_sink("U1", 0.5),
                                     _schematic_sink("U2", 1.0)]
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.setText("2")
    viewer._mf_dirty.add("current")
    viewer._on_editor_multi_apply()
    assert len(viewer._project.editor_directives) == 2
    viewer._undo_marker_action()
    assert viewer._project.editor_directives == []
    # The selection goes back to the locked entries it was made against.
    assert all(e["kind"] == "schematic" for e in viewer._editor_multi)


def test_an_editor_directive_hides_the_schematic_candidate_for_that_part(
        viewer):
    """Otherwise a part with both would be selected twice over."""
    viewer.metadata["directives"] = [_schematic_sink("U1", 0.5),
                                     _schematic_sink("U2", 1.0)]
    viewer._project.upsert_directive(
        _sink("U1", current=9.0, overrides_designator="U1"))
    _sweep(viewer, 0, 0, 25, 20)
    assert sorted(_labels(viewer)) == ["U1", "U2"]
    kinds = {e["label"]: e["kind"] for e in viewer._editor_multi}
    assert kinds == {"U1": "directive", "U2": "schematic"}


def test_remove_leaves_schematic_only_sinks_alone(viewer):
    viewer.metadata["directives"] = [_schematic_sink("U2", 1.0)]
    viewer._project.upsert_directive(_sink("U1"))
    _sweep(viewer, 0, 0, 25, 20)
    assert sorted(_labels(viewer)) == ["U1", "U2"]
    viewer._on_editor_multi_remove()
    assert viewer._project.editor_directives == []
    # U2 had nothing to delete, so it survives as the (now single) selection.
    assert viewer._editor_selection is not None


# --- geometry helper used by the gesture ---------------------------------

def test_a_drag_started_from_any_corner_selects_the_same_markers(viewer):
    for des in ("U1", "U2"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 25, 20)
    forward = sorted(_labels(viewer))
    _sweep(viewer, 25, 20, 0, 0)
    assert sorted(_labels(viewer)) == forward
    assert normalise_rect(25, 20, 0, 0) == (0.0, 0.0, 25.0, 20.0)


# --- what the viewport shows ---------------------------------------------

def _box_groups(v) -> list:
    """The unfilled yellow selection-box marker groups."""
    return [g for g in v._editor_marker_groups()
            if g.edge_color == "#ffff00" and g.color == "transparent"]


def test_every_multi_selected_sink_is_emphasised_in_the_viewport(viewer):
    """The yellow boxes are the only way to see which sinks Apply will
    write to, so all of them have to be boxed, not just the first."""
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 35, 20)
    boxed = sum(len(g.xs) for g in _box_groups(viewer))
    assert boxed == 3


def test_a_mixed_role_selection_boxes_each_glyph_in_its_own_shape(viewer):
    """One box group per role — a sink's box must not be drawn with the
    source's triangle-up outline."""
    viewer._project.upsert_directive(_sink("U1"))
    viewer._project.upsert_directive(
        EditorDirective(kind="component", role="SOURCE", designator="U2",
                        single_net=True, p_net="+3V3", voltage=3.3))
    _sweep(viewer, 0, 0, 25, 20)
    symbols = sorted(g.symbol for g in _box_groups(viewer))
    assert symbols == ["tri_down_box", "tri_up_box"]


def test_each_multi_selected_component_gets_its_own_selection_box(viewer):
    for des in ("U1", "U2", "U3"):
        viewer._project.upsert_directive(_sink(des))
    _sweep(viewer, 0, 0, 35, 20)
    viewer._refresh_editor_selection()
    assert len(viewer._gl_viewer.boxes) == 3


def test_the_highlight_covers_every_selected_sink_s_nets(viewer):
    viewer._project.upsert_directive(_sink("U1", p_net="+3V3"))
    viewer._project.upsert_directive(_sink("U2", p_net="+1V8"))
    _sweep(viewer, 0, 0, 25, 20)
    assert viewer._highlighted == {"+3V3", "+1V8"}


# --- the dirty-row wiring ------------------------------------------------
#
# Everything above sets ``_mf_dirty`` by hand. These drive the widgets the
# way a user does, so a mis-wired signal (``textChanged`` instead of
# ``textEdited``, say, which fires on the form's own setText) is caught.

def test_building_the_form_does_not_mark_any_row_edited(viewer):
    """The form seeds every row with setText / setCurrentIndex. If that
    counted as an edit, Apply would flatten rows the user never touched."""
    viewer._project.upsert_directive(_sink("U1", current=0.5, min_voltage=3.0))
    viewer._project.upsert_directive(_sink("U2", current=1.25))
    _sweep(viewer, 0, 0, 25, 20)
    assert viewer._mf_dirty == set()


def test_typing_in_a_field_marks_that_row_edited(viewer):
    from PySide6.QtTest import QTest
    viewer._project.upsert_directive(_sink("U1", current=0.5))
    viewer._project.upsert_directive(_sink("U2", current=1.25))
    _sweep(viewer, 0, 0, 25, 20)
    viewer._mf_current.clear()
    QTest.keyClicks(viewer._mf_current, "2")
    assert viewer._mf_dirty == {"current"}
    viewer._on_editor_multi_apply()
    assert _currents(viewer, "U1", "U2") == [2.0, 2.0]


def test_clicking_a_current_model_radio_marks_that_row_edited(viewer):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    viewer._project.upsert_directive(_sink("U1", single_net=True))
    viewer._project.upsert_directive(
        _sink("U2", single_net=False, n_net="GND"))
    _sweep(viewer, 0, 0, 25, 20)
    assert viewer._mf_dirty == set()
    QTest.mouseClick(viewer._mf_single, Qt.LeftButton)
    assert "single_net" in viewer._mf_dirty


def test_the_n_net_row_hides_once_single_net_is_chosen(viewer):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    viewer._test_panel.show()
    try:
        viewer._project.upsert_directive(
            _sink("U1", single_net=False, n_net="GND"))
        viewer._project.upsert_directive(
            _sink("U2", single_net=False, n_net="GND"))
        _sweep(viewer, 0, 0, 25, 20)
        assert viewer._mf_nnet.isVisible()
        QTest.mouseClick(viewer._mf_single, Qt.LeftButton)
        assert not viewer._mf_nnet.isVisible()
    finally:
        viewer._test_panel.hide()


def test_a_mixed_model_keeps_the_n_net_row_visible(viewer):
    """The model is still undecided, so an N net may well be what the user
    is about to set."""
    viewer._test_panel.show()
    try:
        viewer._project.upsert_directive(_sink("U1", single_net=True))
        viewer._project.upsert_directive(
            _sink("U2", single_net=False, n_net="GND"))
        _sweep(viewer, 0, 0, 25, 20)
        assert viewer._mf_nnet.isVisible()
    finally:
        viewer._test_panel.hide()
