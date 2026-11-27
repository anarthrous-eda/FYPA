"""View > Board origin (0, 0).

Off by default; the menu item toggles the origin glyph on the board view,
flips its label and is remembered for the next launch. The glyph itself
is drawn at world (0 mm, 0 mm) and skipped when off screen.
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
    def __init__(self, origin: bool):
        self._origin = origin

    def markers_always_on_top(self) -> bool:
        return True

    def set_show_board_origin(self, on: bool) -> None:
        self._origin = bool(on)

    def show_board_origin(self) -> bool:
        return self._origin


@pytest.fixture
def viewer(qapp):
    """Stub viewer with the real View menu (``PdnViewer.__init__`` would
    open a whole board)."""
    from PySide6.QtWidgets import QCheckBox, QComboBox, QMainWindow
    import fypa.altium_viewer as V
    from fypa.viewer.prefs import load_show_board_origin

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    for name in ("view_3d_box", "heatmap_vias_box", "colour_stubs_box",
                 "show_mesh_box"):
        setattr(v, name, QCheckBox())
    v.relief_combo = QComboBox()
    v.relief_combo.addItems(["off", "per layer", "ignoring layers"])
    v._gl_viewer = _FakeGL(load_show_board_origin())
    V.PdnViewer._build_view_menu(v)
    v._refresh_view_menu_labels()
    return v


def test_default_is_off():
    from fypa.viewer.prefs import load_show_board_origin
    assert load_show_board_origin() is False


def test_menu_item_toggles_applies_and_saves(viewer):
    from fypa.viewer.prefs import load_show_board_origin
    act = viewer._act_board_origin
    assert act.text() == "Board origin (0, 0) [hidden]"
    act.trigger()
    assert viewer._gl_viewer.show_board_origin() is True
    assert load_show_board_origin() is True
    assert act.text() == "Board origin (0, 0) [shown]"
    act.trigger()
    assert viewer._gl_viewer.show_board_origin() is False
    assert load_show_board_origin() is False
    assert act.text() == "Board origin (0, 0) [hidden]"


def _render(gl, w=200, h=200):
    from PySide6.QtGui import QImage, QPainter
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(0xFF808080)
    p = QPainter(img)
    gl._draw_board_origin(p)
    p.end()
    return img


@pytest.fixture
def gl(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer
    g = GLMeshViewer()
    g.resize(200, 200)
    g.set_view_mode("2d")
    return g


def test_glyph_drawn_at_world_origin_only_when_shown(gl):
    gl.set_view_center_scale(0.0, 0.0, 0.1)
    assert gl.show_board_origin() is False
    assert _render(gl).pixel(100, 100) == 0xFF808080
    gl.set_show_board_origin(True)
    # The cross passes through the centre pixel; far corner untouched.
    img = _render(gl)
    assert img.pixel(100, 100) != 0xFF808080
    assert img.pixel(5, 5) == 0xFF808080


def test_glyph_skipped_when_origin_off_screen(gl):
    gl.set_show_board_origin(True)
    gl.set_view_center_scale(1000.0, 1000.0, 0.1)
    img = _render(gl)
    assert all(img.pixel(x, y) == 0xFF808080
               for x in range(0, 200, 7) for y in range(0, 200, 7))
