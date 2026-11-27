"""Relief axis - the vertical ruler beside the copper in the ignoring-layers
relief view.

Three pieces: the tick values (round numbers inside the colour window, in
the colour bar's unit), where the GL viewer stands the ruler (the corner of
the copper's bounding box that keeps it clear of the copper), and the host
plumbing (pushed only in the ignoring-layers relief, remembered, mirrored
in the View menu).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from fypa.viewer.relief_axis import relief_axis_ticks


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


# --- Tick values -------------------------------------------------------------

def test_linear_ticks_are_round_values_inside_the_window():
    ticks = relief_axis_ticks(0.98, 12.0)
    assert [text for _t, text in ticks] == ["2", "4", "6", "8", "10", "12"]
    # Position is the fraction of the way from Min to Max.
    assert ticks[0][0] == pytest.approx((2.0 - 0.98) / (12.0 - 0.98))
    assert ticks[-1][0] == pytest.approx(1.0)


def test_ticks_read_in_the_colour_bar_unit():
    # -18.3 mV .. +4.2 mV with the colour bar on "mV" (divisor 1e-3).
    ticks = relief_axis_ticks(-0.0183, 0.0042, divisor=1e-3)
    assert [text for _t, text in ticks] == ["-15", "-10", "-5", "0"]
    zero_t = dict((text, t) for t, text in ticks)["0"]
    assert zero_t == pytest.approx(0.0183 / 0.0225)


def test_tight_window_keeps_enough_decimals_to_tell_ticks_apart():
    texts = [text for _t, text in relief_axis_ticks(11.9981, 12.0)]
    assert texts == ["11.9985", "11.9990", "11.9995", "12.0000"]
    assert len(set(texts)) == len(texts)


def test_log_ticks_sit_on_decades_log_spaced():
    ticks = relief_axis_ticks(1e-4, 100.0, log=True)
    assert [text for _t, text in ticks] == [
        "0.0001", "0.001", "0.01", "0.1", "1", "10", "100"]
    ts = [t for t, _text in ticks]
    assert ts == pytest.approx([i / 6.0 for i in range(7)])


def test_narrow_log_window_fills_in_2_and_5():
    assert [text for _t, text in relief_axis_ticks(3.0, 40.0, log=True)] == [
        "5", "10", "20"]


def test_degenerate_window_has_no_ticks():
    assert relief_axis_ticks(1.0, 1.0) == []
    assert relief_axis_ticks(2.0, 1.0) == []
    assert relief_axis_ticks(float("nan"), 1.0) == []
    assert relief_axis_ticks(0.0, 1.0, divisor=0.0) == []


# --- Where the GL viewer stands the ruler --------------------------------------

@pytest.fixture
def gl(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer

    g = GLMeshViewer()
    g.resize(800, 600)
    g._view_mode = "3d"
    g.set_vertical_exaggeration(1.0)
    g._cam_target = (50.0, 25.0, 0.0)
    g._cam_distance = 200.0
    g._cam_pitch_deg = 30.0
    g._cam_yaw_deg = 0.0
    g.set_relief_height(20.0)
    g.set_relief_layers_ignored(True, pivot_z=1.5, stack_depth=1.5)
    g.set_relief_axis([(0.0, "0"), (1.0, "10")], "V", (0.0, 100.0, 0.0, 50.0))
    g.set_relief_axis_visible(True)
    return g


def test_axis_spans_the_relief_from_the_common_base(gl):
    x, y, z_base, z_top = gl.relief_axis_world()
    assert z_base == pytest.approx(1.5)
    assert z_top == pytest.approx(1.5 + 20.0)
    # Just outside a corner of the copper's bounding box.
    m = gl._RELIEF_AXIS_MARGIN_FRAC * 100.0
    assert x in (pytest.approx(-m), pytest.approx(100.0 + m))
    assert y in (pytest.approx(-m), pytest.approx(50.0 + m))


def test_axis_takes_the_corner_furthest_left_on_screen(gl):
    def screen_x(corner):
        # Middle of the ruler standing at this corner (base 1.5, top 21.5).
        return 0.5 * (gl.world_to_screen(corner[0], corner[1], 1.5)[0]
                      + gl.world_to_screen(corner[0], corner[1], 21.5)[0])

    m = gl._RELIEF_AXIS_MARGIN_FRAC * 100.0
    corners = [(-m, -m), (100.0 + m, -m), (100.0 + m, 50.0 + m),
               (-m, 50.0 + m)]
    for yaw in (0.0, 40.0, 130.0, 180.0, 250.0, 320.0):
        gl._cam_yaw_deg = yaw
        x, y, _zb, _zt = gl.relief_axis_world()
        chosen = screen_x((x, y))
        assert chosen == pytest.approx(min(screen_x(c) for c in corners))


def test_axis_moves_corner_as_the_board_is_turned(gl):
    gl._cam_yaw_deg = 0.0
    front = gl.relief_axis_world()[:2]
    gl._cam_yaw_deg = 180.0
    back = gl.relief_axis_world()[:2]
    assert front != back


def test_axis_stays_in_view_when_zoomed_in_on_part_of_the_board(gl):
    # Close in on the right-hand end of the board: the left-hand corners
    # are off screen, so the ruler stands at a right-hand one.
    gl._cam_target = (95.0, 25.0, 10.0)
    gl._cam_distance = 120.0
    x, y, z_base, z_top = gl.relief_axis_world()
    assert x > 100.0
    for z in (z_base, z_top):
        px, py = gl.world_to_screen(x, y, z)
        assert 0.0 <= px <= gl.width() and 0.0 <= py <= gl.height()


def test_axis_only_in_the_ignoring_layers_relief(gl):
    gl.set_relief_layers_ignored(False)        # per-layer relief
    assert gl.relief_axis_world() is None
    gl.set_relief_layers_ignored(True, pivot_z=1.5, stack_depth=1.5)
    assert gl.relief_axis_world() is not None
    gl.set_relief_height(0.0)                  # relief off
    assert gl.relief_axis_world() is None
    gl.set_relief_height(20.0)
    gl._view_mode = "2d"
    assert gl.relief_axis_world() is None


def test_axis_hidden_by_its_toggle_and_without_ticks(gl):
    gl.set_relief_axis_visible(False)
    assert gl.relief_axis_world() is None
    gl.set_relief_axis_visible(True)
    gl.set_relief_axis([], "V", (0.0, 100.0, 0.0, 50.0))
    assert gl.relief_axis_world() is None
    gl.clear_relief_axis()
    assert gl.relief_axis_world() is None


def test_axis_follows_the_relief_height_without_a_re_push(gl):
    gl.set_relief_height(40.0)
    _x, _y, z_base, z_top = gl.relief_axis_world()
    assert z_top - z_base == pytest.approx(40.0)


def test_axis_paints_without_error(gl):
    from PySide6.QtGui import QImage, QPainter

    img = QImage(800, 600, QImage.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    try:
        gl._draw_relief_axis(painter)
    finally:
        painter.end()
    # The ruler and its labels left white pixels behind.
    assert any(img.pixelColor(x, y).lightness() > 200
               for x in range(0, 800, 2) for y in range(0, 600, 2))


# --- Host plumbing -------------------------------------------------------------

class _FakeGL:
    def __init__(self):
        self.axis = None
        self.visible = False

    def set_relief_axis(self, ticks, unit, bounds):
        self.axis = (list(ticks), unit, tuple(bounds))

    def clear_relief_axis(self):
        self.axis = None

    def set_relief_axis_visible(self, on):
        self.visible = bool(on)

    def markers_always_on_top(self):
        return True

    def show_board_origin(self):
        return False


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QCheckBox, QComboBox, QMainWindow
    import fypa.altium_viewer as V
    from fypa.viewer.scale_controls import ScaleController

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    for name in ("view_3d_box", "heatmap_vias_box", "colour_stubs_box",
                 "show_mesh_box", "relief_axis_box"):
        setattr(v, name, QCheckBox())
    v.relief_combo = QComboBox()
    v.relief_combo.addItems(["Off", "Per layer", "Ignoring layers"])
    v.view_3d_box.setChecked(True)
    v.relief_combo.setCurrentIndex(V.PdnViewer._RELIEF_IGNORE_LAYERS)
    v._gl_viewer = _FakeGL()
    v.scale_controller = ScaleController()
    v.scale_controller.setLabelUnit("Voltage Drop", "V")
    v.scale_controller.setRange(-0.0183, 0.0042)
    v._vmin, v._vmax = -0.0183, 0.0042
    v._log_active = False
    v._log_floor = 1e-12
    v._probe_unit = "V"
    v._data_bounds = (0.0, 100.0, 0.0, 50.0)
    v.relief_axis_box.toggled.connect(v._on_relief_axis_toggled)
    V.PdnViewer._build_view_menu(v)
    v._refresh_view_menu_labels()
    return v


def test_push_gives_window_values_in_the_colour_bar_unit(viewer):
    viewer.relief_axis_box.setChecked(True)
    ticks, unit, bounds = viewer._gl_viewer.axis
    assert unit == "mV"
    assert [text for _t, text in ticks] == ["-15", "-10", "-5", "0"]
    assert bounds == (0.0, 100.0, 0.0, 50.0)


def test_push_follows_the_scale_min_max(viewer):
    viewer.relief_axis_box.setChecked(True)
    viewer._vmin, viewer._vmax = -0.010, 0.0
    viewer._push_relief_axis()
    ticks, _unit, _bounds = viewer._gl_viewer.axis
    assert [text for _t, text in ticks] == [
        "-10", "-8", "-6", "-4", "-2", "0"]


def test_no_axis_outside_the_ignoring_layers_relief(viewer):
    import fypa.altium_viewer as V

    viewer.relief_axis_box.setChecked(True)
    assert viewer._gl_viewer.axis is not None
    viewer.relief_combo.setCurrentIndex(V.PdnViewer._RELIEF_PER_LAYER)
    viewer._push_relief_axis()
    assert viewer._gl_viewer.axis is None
    viewer.relief_combo.setCurrentIndex(V.PdnViewer._RELIEF_IGNORE_LAYERS)
    viewer.view_3d_box.setChecked(False)       # 2D: no relief at all
    viewer._push_relief_axis()
    assert viewer._gl_viewer.axis is None


def test_default_is_off():
    from fypa.viewer.prefs import load_show_relief_axis
    assert load_show_relief_axis() is False


def test_box_applies_saves_and_updates_the_menu_label(viewer):
    from fypa.viewer.prefs import load_show_relief_axis

    act = viewer._act_relief_axis
    assert act.text() == "Relief axis [hidden]"
    viewer.relief_axis_box.setChecked(True)
    assert viewer._gl_viewer.visible is True
    assert load_show_relief_axis() is True
    assert act.text() == "Relief axis [shown]"
    act.trigger()                              # the menu drives the same box
    assert not viewer.relief_axis_box.isChecked()
    assert viewer._gl_viewer.visible is False
    assert viewer._gl_viewer.axis is None
    assert load_show_relief_axis() is False


def test_menu_item_only_enabled_in_the_ignoring_layers_relief(viewer):
    import fypa.altium_viewer as V

    assert viewer._act_relief_axis.isEnabled()
    viewer.relief_combo.setCurrentIndex(V.PdnViewer._RELIEF_PER_LAYER)
    viewer._refresh_view_menu_labels()
    assert not viewer._act_relief_axis.isEnabled()
