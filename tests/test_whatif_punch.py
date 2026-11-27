"""What-If via punching: the placement rules, the GL gesture, and the tool.

The rules (:mod:`fypa.whatif_vias`) decide whether a click may become a via
and what it copies; the GL viewer turns a 2D left-drag into a grid sweep
while punching; the viewer mixin (:mod:`fypa.viewer.whatif_punch`) ties
them to the selected scenario. The board used by the tool tests is drawn
here: GND on Top and Bottom, a VCC plane between them, a VCC pour beside
GND on Top and a stretch of GND that exists on Top only.
"""
from __future__ import annotations

import types

import numpy as np
import pytest

from fypa import whatif as W
from fypa import whatif_vias as WV

ENABLED = [1, 39, 32]                 # Top, L2 (plane), Bottom
NAMES = {1: "Top", 39: "L2", 32: "Bottom"}


def _md_via(x=0.0, y=0.0, net="GND", dia=0.5, hole=0.25, a=1, b=32):
    return {"x_mm": x, "y_mm": y, "net": net, "diameter_mm": dia,
            "hole_diameter_mm": hole, "layer_start": a, "layer_end": b}


# --- rules: what a punched via copies ------------------------------------------

def test_template_copies_the_nearest_via_on_the_same_net():
    vias = [_md_via(0, 0, dia=0.5, hole=0.25),
            _md_via(10, 0, dia=0.8, hole=0.4),
            _md_via(1, 0, net="VCC", dia=1.0, hole=0.5)]
    t = WV.via_template(vias, "GND", 8.0, 0.0, ENABLED)
    assert (t.diameter_mm, t.hole_diameter_mm) == (0.8, 0.4)
    assert "nearest via on GND" in t.source


def test_template_falls_back_to_the_most_common_via_then_a_default():
    vias = [_md_via(net="A", dia=0.6, hole=0.3),
            _md_via(net="B", dia=0.6, hole=0.3),
            _md_via(net="C", dia=0.9, hole=0.45)]
    t = WV.via_template(vias, "GND", 0, 0, ENABLED)
    assert (t.diameter_mm, t.hole_diameter_mm) == (0.6, 0.3)
    assert "most common" in t.source
    t = WV.via_template([], "GND", 0, 0, ENABLED)
    assert (t.layer_start, t.layer_end) == (1, 32)
    assert (t.diameter_mm, t.hole_diameter_mm) == (
        WV.DEFAULT_DIAMETER_MM, WV.DEFAULT_HOLE_MM)


def test_template_ignores_vias_on_layers_that_are_not_enabled():
    t = WV.via_template([_md_via(a=1, b=7)], "GND", 0, 0, ENABLED)
    assert "no vias" in t.source


def test_span_follows_stack_order_not_layer_ids():
    # Plane ids (39) sit between Top (1) and Bottom (32) in the stack.
    assert WV.span_layer_ids(1, 32, ENABLED) == [1, 39, 32]
    assert WV.span_layer_ids(32, 39, ENABLED) == [39, 32]
    assert WV.span_layer_ids(1, 7, ENABLED) == []


# --- rules: when a via is refused ---------------------------------------------

def _via(x=0.0, y=0.0, net="GND"):
    return WV.AddedVia(x, y, net, 1, 32, 0.5, 0.25)


def _problem(survey, other_vias=(), via=None, planes=frozenset({39})):
    return WV.punch_problem(via or _via(), survey, ENABLED, set(planes),
                            NAMES, other_vias)


GOOD = {1: (True, set()), 39: (False, {"VCC"}), 32: (True, set())}


def test_a_via_joining_two_layers_is_fine_even_through_another_nets_plane():
    assert _problem(GOOD) == ""


def test_a_via_on_one_layer_of_its_net_is_refused():
    why = _problem({1: (True, set()), 39: (False, set()),
                    32: (False, set())})
    assert "only Top" in why and "no current" in why


def test_a_pad_touching_another_net_on_a_signal_layer_is_refused():
    why = _problem({**GOOD, 1: (True, {"VCC"})})
    assert why == "its pad would touch VCC on Top"


def test_the_same_net_on_a_signal_layer_would_be_refused_if_it_were_a_plane():
    # Not a plane here: the VCC on L2 now counts.
    assert "touch VCC on L2" in _problem(GOOD, planes=())


def test_overlapping_another_nets_via_or_sitting_in_a_same_net_via():
    assert _problem(GOOD, [(0.4, 0.0, "VCC", 0.5)]) == (
        "it would overlap a via on VCC")
    assert _problem(GOOD, [(0.1, 0.0, "GND", 0.5)]) == (
        "there is already a via there")
    # Two same-net pads may touch; only a centre inside a pad is refused.
    assert _problem(GOOD, [(0.4, 0.0, "GND", 0.5)]) == ""


# --- rules: the drag grid ------------------------------------------------------

def test_grid_is_centred_in_the_box_rows_top_first():
    pts = WV.grid_points(0, 0, 4.5, 2.2, 1.0)
    xs = sorted({round(x, 6) for x, _ in pts})
    ys = [round(y, 6) for _, y in pts[::len(xs)]]
    assert xs == [0.25, 1.25, 2.25, 3.25, 4.25]      # 5 across, centred
    assert ys == [2.1, 1.1, 0.1]                     # top row first
    assert len(pts) == 15


def test_a_box_smaller_than_the_pitch_gives_one_via_at_its_middle():
    assert WV.grid_points(1, 1, 1.4, 1.2, 1.0) == [
        pytest.approx((1.2, 1.1))]


def test_grid_corner_order_does_not_matter():
    assert WV.grid_points(4, 4, 0, 0, 2) == WV.grid_points(0, 0, 4, 4, 2)


def test_grid_refuses_a_huge_sweep_or_a_bad_pitch():
    with pytest.raises(ValueError, match="limit"):
        WV.grid_points(0, 0, 100, 100, 0.5)
    with pytest.raises(ValueError):
        WV.grid_points(0, 0, 1, 1, 0.0)


# --- GL viewer: punch mode -----------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _mouse(v, kind, x, y):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    ev_type, button, buttons = {
        "press": (QEvent.MouseButtonPress, Qt.LeftButton, Qt.LeftButton),
        "move": (QEvent.MouseMove, Qt.NoButton, Qt.LeftButton),
        "release": (QEvent.MouseButtonRelease, Qt.LeftButton, Qt.NoButton),
    }[kind]
    ev = QMouseEvent(ev_type, QPointF(x, y), QPointF(x, y), button, buttons,
                     Qt.NoModifier)
    {"press": v.mousePressEvent, "move": v.mouseMoveEvent,
     "release": v.mouseReleaseEvent}[kind](ev)


@pytest.fixture
def gl(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer
    v = GLMeshViewer()
    v.resize(400, 300)
    v._view_mode = "2d"
    v.bands, v.marquees, v.clicks = [], [], []
    v.punchBandSwept.connect(lambda *a: v.bands.append(a))
    v.editorMarqueeSelected.connect(lambda *a: v.marquees.append(a))
    v.clicked.connect(lambda *a: v.clicks.append(a))
    yield v
    v.deleteLater()


def _drag(v, x0, y0, x1, y1):
    _mouse(v, "press", x0, y0)
    _mouse(v, "move", x1, y1)
    _mouse(v, "release", x1, y1)


def test_punch_mode_turns_a_drag_into_a_punch_band(gl):
    gl.set_view_center_scale(0.0, 0.0, 1.0)
    gl.set_punch_mode(True)
    _drag(gl, 10, 10, 100, 80)
    assert gl.marquees == [] and gl.clicks == []
    assert gl.bands == [pytest.approx((-190.0, 140.0, -100.0, 70.0))]


def test_without_punch_mode_a_viewer_drag_does_nothing(gl):
    _drag(gl, 10, 10, 100, 80)
    assert gl.bands == [] and gl.clicks == []


def test_a_click_in_punch_mode_is_still_a_click(gl):
    gl.set_punch_mode(True)
    _mouse(gl, "press", 50, 50)
    _mouse(gl, "release", 50, 50)
    assert len(gl.clicks) == 1 and gl.bands == []


def test_no_punch_band_in_3d(gl):
    gl.set_punch_mode(True)
    gl._view_mode = "3d"
    _drag(gl, 10, 10, 100, 80)
    assert gl.bands == []


def test_editor_mode_keeps_its_marquee(gl):
    gl.set_editor_mode(True)
    gl.set_punch_mode(True)
    _drag(gl, 10, 10, 100, 80)
    assert len(gl.marquees) == 1 and gl.bands == []


def test_armed_via_badges_the_cursor_and_leaving_punch_mode_restores_it(gl):
    from PySide6.QtCore import Qt
    gl.set_punch_mode(True)
    gl.set_armed_marker("VIA", "#ff9a3c")
    assert gl.cursor().shape() == Qt.BitmapCursor
    gl.set_armed_marker(None)
    gl.set_punch_mode(False)
    assert gl.cursor().shape() == Qt.ArrowCursor


def test_leaving_punch_mode_mid_drag_clears_the_band(gl):
    gl.set_punch_mode(True)
    _mouse(gl, "press", 10, 10)
    _mouse(gl, "move", 100, 80)
    assert gl._marquee_px is not None
    gl.set_punch_mode(False)
    assert gl._marquee_px is None


# --- the tool, on a stub viewer ------------------------------------------------

def _rect(x0, y0, x1, y1):
    return {"exterior": np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                                 dtype=np.float64), "holes": []}


BOARD_MD = {
    "enabled_copper_layer_ids": ENABLED,
    "stackup": [
        {"layer_id": 1, "name": "Top", "is_plane": False},
        {"layer_id": 39, "name": "L2", "is_plane": True},
        {"layer_id": 32, "name": "Bottom", "is_plane": False},
    ],
    "all_copper": [
        # GND on Top runs further left than on Bottom.
        {"layer_id": 1, "net": "GND", "polygons": [_rect(-12, -5, 5, 5)]},
        {"layer_id": 1, "net": "VCC", "polygons": [_rect(5.2, -5, 10, 5)]},
        {"layer_id": 39, "net": "VCC", "polygons": [_rect(-20, -20, 20, 20)]},
        {"layer_id": 32, "net": "GND", "polygons": [_rect(-5, -5, 5, 5)]},
    ],
    "vias": [_md_via(0.0, 0.0)],
}


class _FakeGL:
    def __init__(self):
        self.punch = False
        self.armed = None
        self.markers = None

    def set_punch_mode(self, on):
        self.punch = on

    def set_armed_marker(self, role, color=None):
        self.armed = role

    def set_markers(self, groups):
        self.markers = list(groups)

    def view_center_scale(self):
        return (0.0, 0.0, 0.01)          # 0.01 mm / px


class _Toasts:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text, _ms=0):
        self.messages.append(text)

    def clearMessage(self):
        pass


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = dict(BOARD_MD)
    v.solution = types.SimpleNamespace(solver_info={})
    v._editor_mode = False
    v._editor_pending_marker = None
    v._copper_selection = None
    v._highlight_via_xy = None
    v._layer_eye2_buttons = []
    v._phys_name_to_layer_id = {"Top": 1, "L2": 39, "Bottom": 32}
    v._phys_stackup_rank = {"Top": 0, "L2": 1, "Bottom": 2}
    v._init_whatif_state()
    scen = W.WhatIfScenario(name="Stitch", status=W.STATUS_OK,
                            summary={"fails": 0}, base_fingerprint="abc")
    v._whatifs = [scen]
    v._whatif_selected_id = scen.id
    v._whatif_open = {"board": True, "topology": False}
    v._non_editor_marker_groups = []
    v._editor_marker_groups = lambda: []
    v._gl_viewer = _FakeGL()
    v.toasts = _Toasts()
    v.statusBar = lambda: v.toasts
    v.unsaved = []
    v._whatif_mark_unsaved = lambda: v.unsaved.append(1)
    v._refresh_whatif_panel = lambda: v._sync_whatif_punch_controls()
    v._held = QWidget()
    V.PdnViewer._build_whatif_punch_row(v, QVBoxLayout(v._held))
    v.scen = scen
    return v


def test_arming_puts_the_viewport_in_punch_mode(viewer):
    viewer._whatif_punch_btn.setChecked(True)
    assert viewer._whatif_punch_armed
    assert viewer._gl_viewer.punch and viewer._gl_viewer.armed == "VIA"
    viewer._whatif_punch_btn.setChecked(False)
    assert not viewer._gl_viewer.punch and viewer._gl_viewer.armed is None


def test_arming_is_refused_in_editor_mode_or_without_a_whatif(viewer):
    viewer._editor_mode = True
    viewer._whatif_punch_btn.setChecked(True)
    assert not viewer._whatif_punch_armed
    assert not viewer._whatif_punch_btn.isChecked()
    assert "Edit Mode" in viewer.toasts.messages[-1]
    viewer._editor_mode = False
    viewer._whatif_selected_id = None
    viewer._sync_whatif_punch_controls()
    assert not viewer._whatif_punch_btn.isEnabled()


def test_a_click_on_copper_adds_a_via_copied_from_the_nearest(viewer):
    import fypa.altium_viewer as V
    viewer._set_whatif_punch_armed(True)
    V.PdnViewer._on_gl_clicked(viewer, 2.0, 2.0)   # routed to the tool
    (v,) = viewer.scen.added_vias
    assert (v.x_mm, v.y_mm, v.net) == (2.0, 2.0, "GND")
    assert (v.diameter_mm, v.hole_diameter_mm) == (0.5, 0.25)
    assert (v.layer_start, v.layer_end) == (1, 32)
    # Its old result no longer belongs to it.
    assert viewer.scen.status == W.STATUS_NOT_RUN
    assert viewer.scen.summary == {} and viewer.scen.base_fingerprint is None
    assert viewer.unsaved
    # Drawn: a pad and a drill marker.
    assert len(viewer._gl_viewer.markers) == 2
    assert viewer._whatif_punch_clear_btn.text() == "Clear (1)"


def test_clicking_an_added_via_removes_it(viewer):
    viewer._whatif_punch_click(2.0, 2.0)
    viewer._whatif_punch_click(2.1, 2.0)           # on its pad
    assert viewer.scen.added_vias == []
    assert "Removed" in viewer.toasts.messages[-1]
    assert viewer._gl_viewer.markers == []


@pytest.mark.parametrize("x,y,expect", [
    (-9.0, 0.0, "no current"),                     # GND on Top only
    (4.98, 0.0, "touch VCC on Top"),               # pad reaches the VCC pour
    (0.1, 0.0, "already a via there"),             # inside the board's via
    (30.0, 30.0, "No copper there"),
])
def test_refused_clicks_say_why_and_add_nothing(viewer, x, y, expect):
    viewer._whatif_punch_click(x, y)
    assert viewer.scen.added_vias == []
    assert expect in viewer.toasts.messages[-1]
    assert viewer.scen.status == W.STATUS_OK       # untouched


def test_a_drag_punches_a_grid_skipping_spots_that_fail(viewer):
    viewer._whatif_punch_armed = True
    viewer._whatif_punch_pitch.setValue(2.0)
    viewer._on_whatif_punch_band(-4.0, -4.0, 4.0, 4.0)
    # 5 x 5 grid; the centre sits on the board's own via.
    assert len(viewer.scen.added_vias) == 24
    assert {v.net for v in viewer.scen.added_vias} == {"GND"}
    assert (0.0, 0.0) not in {(v.x_mm, v.y_mm)
                              for v in viewer.scen.added_vias}
    msg = viewer.toasts.messages[-1]
    assert "Added 24" in msg and "1 spot(s) skipped" in msg


def test_a_grid_checks_each_new_via_against_the_ones_it_just_placed(viewer):
    viewer._whatif_punch_armed = True
    viewer._whatif_punch_pitch.setValue(0.2)       # tighter than a pad
    viewer._on_whatif_punch_band(2.0, 2.0, 2.4, 2.0)
    xs = sorted(v.x_mm for v in viewer.scen.added_vias)
    # 0.2 mm apart would put each centre inside the last pad (r = 0.25).
    assert xs == pytest.approx([2.0, 2.4])


def test_a_drag_is_ignored_when_the_tool_is_not_armed(viewer):
    viewer._on_whatif_punch_band(-4.0, -4.0, 4.0, 4.0)
    assert viewer.scen.added_vias == []


def test_clear_removes_every_added_via(viewer):
    viewer._whatif_punch_click(2.0, 2.0)
    viewer._whatif_punch_click(-2.0, -2.0)
    viewer._on_whatif_punch_clear()
    assert viewer.scen.added_vias == []
    assert viewer._whatif_punch_clear_btn.text() == "Clear"


def test_escape_stops_punching(viewer):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    viewer._set_whatif_punch_armed(True)
    viewer.isActiveWindow = lambda: True
    esc = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    assert viewer.eventFilter(viewer, esc) is True
    assert not viewer._whatif_punch_armed and not viewer._gl_viewer.punch


def test_closing_the_panel_or_another_whatif_stops_punching(viewer):
    viewer._set_whatif_punch_armed(True)
    viewer._whatif_open["board"] = False
    viewer._whatif_place_panel = lambda: None
    viewer._whatif_punch_disarm()
    assert not viewer._whatif_punch_armed
    viewer._whatif_open["board"] = True
    viewer._set_whatif_punch_armed(True)
    viewer._whatif_running_id = "x"                # a solve started
    viewer._sync_whatif_punch_controls()
    assert not viewer._whatif_punch_armed


def test_markers_show_only_while_the_panel_is_open_on_the_board(viewer):
    viewer._whatif_punch_click(2.0, 2.0)
    assert len(viewer._whatif_punch_marker_groups()) == 2
    viewer._whatif_open["board"] = False
    assert viewer._whatif_punch_marker_groups() == []


def test_a_whatif_with_added_vias_cannot_become_the_base(viewer, monkeypatch):
    import fypa.viewer.whatif_panel as P
    shown = []
    monkeypatch.setattr(P.QMessageBox, "information",
                        lambda *a, **k: shown.append(a))
    monkeypatch.setattr(P.QMessageBox, "question", lambda *a, **k: pytest.fail(
        "asked to confirm a base change it must refuse"))
    viewer._whatif_punch_click(2.0, 2.0)
    viewer._on_whatif_apply()
    assert shown and "aren't on the board" in shown[0][2]
