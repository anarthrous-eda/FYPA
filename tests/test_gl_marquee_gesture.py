"""The editor-mode rubber-band gesture in the GL viewport.

Left-drag in the viewport used to be dead input — tracked only so an
accidental wobble would not read as a click. Editor mode now claims it as a
marquee, which puts it next to three gestures it must not disturb: the
click that selects one object, the free-marker drag, and the right/middle
button pan and zoom. These pin which one wins.
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


@pytest.fixture
def gl(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer
    v = GLMeshViewer()
    v.resize(400, 300)
    v._view_mode = "2d"
    v.set_editor_mode(True)
    v.marquees = []
    v.clicks = []
    v.drag_starts = []
    v.editorMarqueeSelected.connect(
        lambda *a: v.marquees.append(a))
    v.clicked.connect(lambda *a: v.clicks.append(a))
    v.editorDragStarted.connect(lambda *a: v.drag_starts.append(a))
    yield v
    v.deleteLater()


def _press(v, x, y, mods=None):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    v.mousePressEvent(QMouseEvent(
        QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
        Qt.LeftButton, Qt.LeftButton, mods or Qt.NoModifier))


def _move(v, x, y):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    v.mouseMoveEvent(QMouseEvent(
        QEvent.MouseMove, QPointF(x, y), QPointF(x, y),
        Qt.NoButton, Qt.LeftButton, Qt.NoModifier))


def _release(v, x, y):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    v.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))


def _drag(v, x0, y0, x1, y1, mods=None):
    _press(v, x0, y0, mods)
    _move(v, x1, y1)
    _release(v, x1, y1)


# --- the gesture ---------------------------------------------------------

def test_a_left_drag_sweeps_a_band_and_commits_it_on_release(gl):
    _press(gl, 10, 10)
    _move(gl, 100, 80)
    assert gl._marquee_px == (10.0, 10.0, 100.0, 80.0)
    _release(gl, 100, 80)
    assert gl._marquee_px is None
    assert len(gl.marquees) == 1


def test_the_committed_band_is_in_world_mm_not_pixels(gl):
    # 400x300 widget centred on world (0, 0) at 1 mm/px: the top-left corner
    # of the viewport is world (-200, +150), and screen y grows downward.
    gl.set_view_center_scale(0.0, 0.0, 1.0)
    _drag(gl, 10, 10, 100, 80)
    x0, y0, x1, y1 = gl.marquees[0][:4]
    assert (x0, y0) == pytest.approx((-190.0, 140.0))
    assert (x1, y1) == pytest.approx((-100.0, 70.0))


def test_a_marquee_drag_does_not_also_fire_a_click(gl):
    """Otherwise the release would clear the selection the sweep just made."""
    _drag(gl, 10, 10, 100, 80)
    assert gl.clicks == []


def test_a_press_and_release_without_movement_is_still_a_click(gl):
    _press(gl, 50, 50)
    _release(gl, 50, 50)
    assert gl._marquee_px is None
    assert gl.marquees == []
    assert len(gl.clicks) == 1


def test_a_wobble_under_the_drag_threshold_does_not_start_a_band(gl):
    _press(gl, 50, 50)
    _move(gl, 52, 51)          # < _CLICK_DRAG_THRESHOLD_PX
    assert gl._marquee_px is None
    _release(gl, 52, 51)
    assert gl.marquees == []
    assert len(gl.clicks) == 1


@pytest.mark.parametrize("x0,y0,x1,y1", [
    (10, 10, 100, 80),     # down-right
    (100, 80, 10, 10),     # up-left
    (100, 10, 10, 80),     # down-left
    (10, 80, 100, 10),     # up-right
])
def test_a_drag_from_any_corner_commits_a_band(gl, x0, y0, x1, y1):
    _drag(gl, x0, y0, x1, y1)
    assert len(gl.marquees) == 1


# --- modifiers ----------------------------------------------------------

def test_shift_held_at_press_marks_the_sweep_additive(gl):
    from PySide6.QtCore import Qt
    _drag(gl, 10, 10, 100, 80, mods=Qt.ShiftModifier)
    additive, toggle = gl.marquees[0][4:]
    assert (additive, toggle) == (True, False)


def test_ctrl_held_at_press_marks_the_sweep_a_toggle(gl):
    from PySide6.QtCore import Qt
    _drag(gl, 10, 10, 100, 80, mods=Qt.ControlModifier)
    additive, toggle = gl.marquees[0][4:]
    assert (additive, toggle) == (False, True)


def test_a_plain_sweep_is_neither_additive_nor_a_toggle(gl):
    _drag(gl, 10, 10, 100, 80)
    assert gl.marquees[0][4:] == (False, False)


def test_the_modifier_is_latched_at_press_not_read_at_release(gl):
    """Releasing Shift part-way through a sweep must not change the gesture
    the user started."""
    from PySide6.QtCore import Qt
    _press(gl, 10, 10, mods=Qt.ShiftModifier)
    _move(gl, 100, 80)         # no modifier on the move / release events
    _release(gl, 100, 80)
    assert gl.marquees[0][4] is True


# --- gestures it must not disturb ---------------------------------------

def test_no_marquee_outside_editor_mode(gl):
    gl.set_editor_mode(False)
    _drag(gl, 10, 10, 100, 80)
    assert gl._marquee_px is None
    assert gl.marquees == []
    # And a drag still swallows the click, as it always did.
    assert gl.clicks == []


def test_no_marquee_in_3d(gl):
    """A box swept across a perspective view would not mean what it looks
    like, so the gesture is 2D only — same gate as the marker drag."""
    gl._view_mode = "3d"
    _drag(gl, 10, 10, 100, 80)
    assert gl._marquee_px is None
    assert gl.marquees == []


def test_a_press_on_a_marker_starts_a_marker_drag_not_a_marquee(gl):
    gl.set_editor_drag_hit_test(lambda wx, wy: True)
    _press(gl, 10, 10)
    _move(gl, 100, 80)
    assert gl._marquee_px is None
    assert len(gl.drag_starts) == 1
    _release(gl, 100, 80)
    assert gl.marquees == []


def test_a_press_off_a_marker_still_starts_a_marquee(gl):
    gl.set_editor_drag_hit_test(lambda wx, wy: False)
    _drag(gl, 10, 10, 100, 80)
    assert len(gl.marquees) == 1


def test_leaving_editor_mode_mid_drag_clears_the_band(gl):
    _press(gl, 10, 10)
    _move(gl, 100, 80)
    assert gl._marquee_px is not None
    gl.set_editor_mode(False)
    assert gl._marquee_px is None


# --- the selection-box overlay -----------------------------------------

def test_selection_boxes_accept_one_box_or_many(gl):
    gl.set_editor_selection_bbox((1.0, 2.0, 3.0, 4.0))
    assert gl._editor_selection_bboxes == [(1.0, 2.0, 3.0, 4.0)]
    gl.set_editor_selection_bbox(None)
    assert gl._editor_selection_bboxes == []
    gl.set_editor_selection_bboxes([(0, 0, 1, 1), (5, 5, 6, 6)])
    assert len(gl._editor_selection_bboxes) == 2


def test_pushing_the_same_boxes_again_does_not_request_a_repaint(gl):
    """``_refresh_editor_selection`` pushes on every render, so an unchanged
    push has to be free."""
    calls = []
    gl.update = lambda *a: calls.append(1)
    gl.set_editor_selection_bboxes([(0, 0, 1, 1)])
    assert len(calls) == 1
    gl.set_editor_selection_bboxes([(0, 0, 1, 1)])
    assert len(calls) == 1
