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


def _region(net, x0, y0, x1, y1, layer=1, pour=True):
    return {"kind": "region", "layer_id": layer, "net": net,
            "outline": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            "holes": [], "polygon_index": 3 if pour else -1}


# A VCC track (fixed copper) crossing the GND pour on Top at y = 3.
_TRACK = {"kind": "track", "layer_id": 1, "net": "VCC", "ax": -3.0,
          "ay": 3.0, "bx": 3.0, "by": 3.0, "width_mm": 0.3}
# A VCC SMD pad on Top at (-3, -3), 1 mm square.
_PAD = {"net": "VCC", "layer_ids": [1],
        "outline": [[-3.5, -3.5], [-2.5, -3.5], [-2.5, -2.5], [-3.5, -2.5]]}

BOARD_MD = {
    "enabled_copper_layer_ids": ENABLED,
    "plane_clearance_mm": 0.3,
    "stackup": [
        {"layer_id": 1, "name": "Top", "is_plane": False},
        {"layer_id": 39, "name": "L2", "is_plane": True},
        {"layer_id": 32, "name": "Bottom", "is_plane": False},
    ],
    "all_copper": [
        # GND on Top runs further left than on Bottom.
        {"layer_id": 1, "net": "GND", "polygons": [_rect(-12, -5, 5, 5)]},
        # VCC pour beside it on Top, plus the track and the pad.
        {"layer_id": 1, "net": "VCC", "polygons": [
            _rect(5.2, -5, 10, 5), _rect(-3, 2.85, 3, 3.15),
            _rect(-3.5, -3.5, -2.5, -2.5)]},
        {"layer_id": 39, "net": "VCC", "polygons": [_rect(-20, -20, 20, 20)]},
        {"layer_id": 32, "net": "GND", "polygons": [_rect(-5, -5, 5, 5)]},
        # A VCC pour on Bottom too, beside GND.
        {"layer_id": 32, "net": "VCC", "polygons": [_rect(5.2, -5, 10, 5)]},
    ],
    "primitives": {
        "tracks": [_TRACK],
        "regions": [_region("GND", -12, -5, 5, 5),
                    _region("VCC", 5.2, -5, 10, 5),
                    _region("GND", -5, -5, 5, 5, layer=32),
                    _region("VCC", 5.2, -5, 10, 5, layer=32)],
    },
    "pads": [_PAD],
    "vias": [
        _md_via(0.0, 0.0),
        # Out of the way: a bigger through via, and a microvia type used
        # both Top -> L2 and L2 -> Bottom (stored bottom-up once).
        _md_via(-10.0, 4.0, dia=0.8, hole=0.4),
        _md_via(-10.0, -4.0, dia=0.25, hole=0.1, a=1, b=39),
        _md_via(-11.0, -4.0, dia=0.25, hole=0.1, a=1, b=39),
        _md_via(-11.0, 4.0, dia=0.25, hole=0.1, a=32, b=39),
    ],
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
    v._primitives_by_layer_net = None
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


def test_arming_is_refused_in_editor_mode(viewer):
    viewer._editor_mode = True
    viewer._whatif_punch_btn.setChecked(True)
    assert not viewer._whatif_punch_armed
    assert not viewer._whatif_punch_btn.isChecked()
    assert "Edit Mode" in viewer.toasts.messages[-1]


def test_arming_with_no_whatif_makes_a_vias_only_one(viewer):
    viewer._whatif_selected_id = None
    viewer._sync_whatif_punch_controls()
    assert viewer._whatif_punch_btn.isEnabled()
    viewer._whatif_punch_btn.setChecked(True)
    assert viewer._whatif_punch_armed
    new = viewer._whatif_selected()
    assert new is not viewer.scen and new.name == "Vias 1"
    assert not new.has_changes() and viewer.unsaved
    viewer._whatif_punch_click(2.0, 2.0)
    assert len(new.added_vias) == 1 and new.has_changes()


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
    # Drawn: the clearance ring, the pad and the drill.
    assert len(viewer._gl_viewer.markers) == 3
    assert viewer._whatif_punch_clear_btn.text() == "Clear (1)"


def test_clicking_an_added_via_removes_it(viewer):
    viewer._whatif_punch_click(2.0, 2.0)
    viewer._whatif_punch_click(2.1, 2.0)           # on its pad
    assert viewer.scen.added_vias == []
    assert "Removed" in viewer.toasts.messages[-1]
    assert viewer._gl_viewer.markers == []


@pytest.mark.parametrize("x,y,expect", [
    (-9.0, 0.0, "no current"),                     # GND on Top only
    (1.0, 3.3, "touch VCC on Top"),                # pad reaches the track
    (-3.65, -3.65, "touch VCC on Top"),            # pad reaches the SMD pad
    (0.1, 0.0, "already a via there"),             # inside the board's via
    (30.0, 30.0, "No copper there"),
])
def test_refused_clicks_say_why_and_add_nothing(viewer, x, y, expect):
    viewer._whatif_punch_click(x, y)
    assert viewer.scen.added_vias == []
    assert expect in viewer.toasts.messages[-1]
    assert viewer.scen.status == W.STATUS_OK       # untouched


def test_a_via_may_land_in_another_nets_pour_which_is_repoured(viewer):
    """The whole point on a real board: a pour of another net (here VCC's,
    beside GND on Top and Bottom) no longer refuses the via."""
    viewer._whatif_punch_click(4.98, 0.0)
    (v,) = viewer.scen.added_vias
    assert v.net == "GND"
    msg = viewer.toasts.messages[-1]
    assert "VCC repoured around it with 0.3 mm clearance" in msg


def test_survey_splits_fixed_copper_from_pour(viewer):
    s = viewer._whatif_punch_survey(4.98, 0.0, "GND", 0.25, [1, 39, 32])
    assert s[1] == (True, set(), {"VCC"})
    assert s[39][2] == {"VCC"}                     # the plane: repoured
    s = viewer._whatif_punch_survey(1.0, 3.3, "GND", 0.25, [1])
    assert s[1] == (True, {"VCC"}, set())          # the track: fixed


def test_clearance_defaults_to_the_board_rule_and_is_per_whatif(viewer):
    spin = viewer._whatif_punch_clearance_spin
    viewer._sync_whatif_punch_controls()
    assert spin.value() == pytest.approx(0.3)      # metadata's rule
    assert not viewer._whatif_punch_clearance_reset_btn.isEnabled()
    viewer._whatif_punch_click(2.0, 2.0)
    viewer.scen.status = W.STATUS_OK               # pretend it was run
    spin.setValue(0.5)
    assert viewer.scen.via_clearance_mm == 0.5
    assert viewer.scen.status == W.STATUS_NOT_RUN  # result no longer fits
    ring = viewer._whatif_punch_marker_groups()[0]
    assert ring.world_diameters_mm[0] == pytest.approx(0.5 + 2 * 0.5)
    assert viewer._whatif_punch_clearance_reset_btn.isEnabled()
    viewer._whatif_punch_clearance_reset_btn.click()
    assert viewer.scen.via_clearance_mm is None
    assert spin.value() == pytest.approx(0.3)


def test_changing_the_clearance_without_vias_keeps_the_result(viewer):
    viewer._whatif_punch_clearance_spin.setValue(0.4)
    assert viewer.scen.via_clearance_mm == 0.4
    assert viewer.scen.status == W.STATUS_OK


def test_rules_let_a_pour_through_but_not_fixed_copper():
    pour_only = {1: (True, set(), {"VCC"}), 39: (False, set(), {"VCC"}),
                 32: (True, set(), {"VCC"})}
    assert _problem(pour_only) == ""
    fixed = {**pour_only, 32: (True, {"VCC"}, set())}
    assert _problem(fixed) == "its pad would touch VCC on Bottom"


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
    assert len(viewer._whatif_punch_marker_groups()) == 3
    viewer._whatif_open["board"] = False
    assert viewer._whatif_punch_marker_groups() == []


# --- moving an added via ------------------------------------------------------

def _drag_via(viewer, path):
    """Press on ``path[0]``, move through the rest, release on the last."""
    import fypa.altium_viewer as V
    (x0, y0), *rest = path
    assert V.PdnViewer._marker_drag_hit_test(viewer, x0, y0)
    V.PdnViewer._on_marker_drag_started(viewer, x0, y0)
    for x, y in rest:
        V.PdnViewer._on_marker_drag_moved(viewer, x, y)
    xe, ye = path[-1]
    V.PdnViewer._on_marker_drag_released(viewer, xe, ye)


@pytest.fixture
def armed(viewer):
    viewer._editor_mode = False
    viewer._set_whatif_punch_armed(True)
    viewer._whatif_punch_click(2.0, 2.0)
    viewer.scen.status = W.STATUS_OK               # as if it had been run
    viewer.scen.summary = {"fails": 0}
    return viewer


def test_dragging_an_added_via_moves_it_and_clears_the_result(armed):
    _drag_via(armed, [(2.1, 2.0), (2.6, 1.0), (3.1, 0.0)])
    (v,) = armed.scen.added_vias
    # The grab offset (0.1 mm) is kept: the via moved by the cursor's travel.
    assert (v.x_mm, v.y_mm) == pytest.approx((3.0, 0.0))
    assert (v.net, v.diameter_mm) == ("GND", 0.5)
    assert armed.scen.status == W.STATUS_NOT_RUN
    assert armed.scen.summary == {}
    assert "Moved the via on GND" in armed.toasts.messages[-1]


def test_a_drag_holds_at_the_last_spot_that_passes_the_rules(armed):
    # Towards the VCC track at y = 3: (2, 2.5) still fits, (2, 2.9) touches.
    _drag_via(armed, [(2.0, 2.0), (2.0, 2.5), (2.0, 2.9)])
    (v,) = armed.scen.added_vias
    assert (v.x_mm, v.y_mm) == pytest.approx((2.0, 2.5))
    msg = armed.toasts.messages[-1]
    assert "held at the last spot" in msg and "touch VCC on Top" in msg


def test_a_drag_with_no_valid_spot_leaves_the_via_and_its_result(armed):
    _drag_via(armed, [(2.0, 2.0), (2.0, 2.9)])     # straight onto the track
    (v,) = armed.scen.added_vias
    assert (v.x_mm, v.y_mm) == (2.0, 2.0)
    assert armed.scen.status == W.STATUS_OK
    assert "Via not moved" in armed.toasts.messages[-1]


def test_a_press_without_moving_removes_the_via(armed):
    _drag_via(armed, [(2.0, 2.0), (2.00001, 2.0)])
    assert armed.scen.added_vias == []


def test_stopping_the_tool_mid_drag_puts_the_via_back(armed):
    import fypa.altium_viewer as V
    V.PdnViewer._on_marker_drag_started(armed, 2.0, 2.0)
    V.PdnViewer._on_marker_drag_moved(armed, 3.0, 2.0)
    assert armed.scen.added_vias[0].x_mm == pytest.approx(3.0)
    armed._whatif_punch_disarm()
    assert armed.scen.added_vias[0].x_mm == 2.0
    assert armed._whatif_punch_drag is None


def test_vias_only_drag_while_punching(armed):
    import fypa.altium_viewer as V
    armed._whatif_punch_disarm()
    assert not V.PdnViewer._marker_drag_hit_test(armed, 2.0, 2.0)


def test_gl_viewer_starts_a_marker_drag_in_punch_mode(gl):
    gl.set_view_center_scale(0.0, 0.0, 1.0)
    starts = []
    gl.editorDragStarted.connect(lambda *a: starts.append(a))
    gl.set_editor_drag_hit_test(lambda wx, wy: True)
    _mouse(gl, "press", 50, 50)
    assert starts == []                            # not punching yet
    _mouse(gl, "release", 50, 50)
    gl.set_punch_mode(True)
    clicks = len(gl.clicks)
    _mouse(gl, "press", 50, 50)
    _mouse(gl, "move", 90, 50)
    _mouse(gl, "release", 90, 50)
    # A drag of the via - not a grid sweep, and no click either.
    assert len(starts) == 1 and gl.bands == [] and len(gl.clicks) == clicks


# --- hover in the Difference view ---------------------------------------------

def _diff_view(viewer, rows):
    viewer._whatif_view = "diff"
    viewer._whatif_diff = {"id": viewer.scen.id, "solution": None,
                           "metadata": {}, "added_via_rows": rows}


def test_difference_view_hover_reports_an_added_vias_whatif_current(viewer):
    _diff_view(viewer, [{"x_mm": 2.0, "y_mm": 2.0, "diameter_mm": 0.5,
                         "current": 0.42, "v_top": 0.987}])
    d2, text = viewer._whatif_added_via_hover(2.1, 2.0)
    assert d2 == pytest.approx(0.01)
    assert text == "   Added via (what-if): I = 0.42 A   V = 0.987 V"
    assert viewer._whatif_added_via_hover(5.0, 5.0) is None


def test_hover_picks_the_added_via_when_no_base_via_is_closer(viewer):
    import fypa.altium_viewer as V
    _diff_view(viewer, [{"x_mm": 2.0, "y_mm": 2.0, "diameter_mm": 0.5,
                         "current": 0.42}])
    base_row = {"x_mm": 2.3, "y_mm": 2.0, "current": 1.5}
    viewer._pick_hovered_via = lambda x, y: base_row
    assert "Added via" in V.PdnViewer._via_hover_info(viewer, 2.05, 2.0)
    assert V.PdnViewer._via_hover_info(viewer, 2.28, 2.0) == (
        "   Via: I = 1.5 A")
    viewer._pick_hovered_via = lambda x, y: None
    assert "I = 0.42 A" in V.PdnViewer._via_hover_info(viewer, 2.0, 2.0)


def test_added_via_hover_is_only_for_the_difference_view(viewer):
    _diff_view(viewer, [{"x_mm": 2.0, "y_mm": 2.0, "diameter_mm": 0.5,
                         "current": 0.42}])
    viewer._whatif_view = "whatif"   # the what-if's own rows cover it there
    assert viewer._whatif_added_via_hover(2.0, 2.0) is None


# --- via types ----------------------------------------------------------------

def test_board_via_types_group_by_size_with_their_spans():
    types = WV.board_via_types(BOARD_MD["vias"], ENABLED)
    assert [(t.hole_diameter_mm, t.diameter_mm, t.count) for t in types] == [
        (0.1, 0.25, 3), (0.25, 0.5, 1), (0.4, 0.8, 1)]
    micro = types[0]
    # Spans top first; the bottom-up record is folded into L2 -> Bottom.
    assert [(sp.layer_start, sp.layer_end, sp.count, sp.kind)
            for sp in micro.spans] == [(1, 39, 2, "micro"),
                                       (39, 32, 1, "micro")]
    assert micro.needs_span_choice and not types[1].needs_span_choice
    assert WV.via_type_label(micro) == "0.1 / 0.25 mm micro (3)"
    assert WV.via_type_label(types[2]) == "0.4 / 0.8 mm through (1)"


def test_via_props_text_names_size_span_and_kind():
    v = WV.AddedVia(0, 0, "GND", 32, 39, 0.25, 0.1)
    assert WV.via_props_text(v, ENABLED, NAMES) == (
        "0.1 / 0.25 mm, L2 → Bottom (micro)")


def test_the_via_dropdown_lists_auto_then_the_boards_types(viewer):
    combo = viewer._whatif_punch_type_combo
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "Nearest on net (auto)", "0.1 / 0.25 mm micro (3)",
        "0.25 / 0.5 mm through (1)", "0.4 / 0.8 mm through (1)"]
    assert combo.currentIndex() == 0
    assert "nearest via" in viewer._whatif_punch_type_info.text()
    assert viewer._whatif_punch_span_combo.isHidden()


def test_choosing_a_type_places_that_via(viewer):
    viewer._whatif_punch_type_combo.setCurrentIndex(3)     # 0.4 / 0.8
    assert viewer._whatif_punch_span_combo.isHidden()      # through only
    assert viewer._whatif_punch_type_info.text() == (
        "Drill / pad 0.4 / 0.8 mm, Top → Bottom (through)")
    viewer._whatif_punch_click(2.0, 2.0)
    (v,) = viewer.scen.added_vias
    assert (v.hole_diameter_mm, v.diameter_mm) == (0.4, 0.8)
    assert "0.4 / 0.8 mm, Top → Bottom (through)" in (
        viewer.toasts.messages[-1])


def test_a_partial_span_type_shows_the_span_sub_choice(viewer):
    viewer._whatif_punch_type_combo.setCurrentIndex(1)     # the microvia
    span = viewer._whatif_punch_span_combo
    assert not span.isHidden()
    assert [span.itemText(i) for i in range(span.count())] == [
        "Top → L2 (micro, 2)", "L2 → Bottom (micro, 1)"]
    span.setCurrentIndex(1)
    assert viewer._whatif_punch_type_info.text() == (
        "Drill / pad 0.1 / 0.25 mm, L2 → Bottom (micro)")
    # GND has no copper on L2 here, so the via would join one layer only.
    viewer._whatif_punch_click(2.0, 2.0)
    assert viewer.scen.added_vias == []
    assert "only Bottom" in viewer.toasts.messages[-1]


def test_base_view_hover_names_an_added_vias_properties(viewer):
    import fypa.altium_viewer as V
    viewer._whatif_punch_click(2.0, 2.0)
    viewer._pick_hovered_via = lambda x, y: None
    assert V.PdnViewer._via_hover_info(viewer, 2.05, 2.0) == (
        "   Added via: GND, 0.25 / 0.5 mm, Top → Bottom (through)")


# --- a vias-only what-if ----------------------------------------------------

def test_run_refuses_a_whatif_with_nothing_changed(viewer, monkeypatch):
    import fypa.viewer.whatif_panel as P
    shown = []
    monkeypatch.setattr(P.QMessageBox, "information",
                        lambda *a, **k: shown.append(a))
    empty = W.WhatIfScenario(name="Empty")
    viewer._whatifs.append(empty)
    viewer._whatif_selected_id = empty.id
    viewer._whatif_running_id = None
    viewer._on_whatif_run()
    assert shown and "Punch vias" in shown[0][2]


def test_the_dialog_saves_a_whatif_with_nothing_changed_but_wont_run_it(qapp):
    from fypa.altium.loader import SolveSettings
    from fypa.viewer.whatif_panel import WhatIfDialog
    s = W.WhatIfScenario(name="Vias")
    dlg = WhatIfDialog(None, s, SolveSettings(), [], [])
    dlg._apply()                                   # Save: fine
    dlg.run_requested = True
    with pytest.raises(ValueError, match="Punch vias"):
        dlg._apply()                               # Save and Run: refused


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
