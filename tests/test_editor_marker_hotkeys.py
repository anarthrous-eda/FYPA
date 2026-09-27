"""The S / L hotkeys that arm a free SOURCE / SINK drop.

The viewport triangle buttons were the only way to arm a free-marker
placement, so these pin the keyboard route to the same guards the mouse
route has: editor-mode only, named copper only, press-again disarms, and
the button reflects what the key did. Also that the tooltips and the Help
tab name the key, since a shortcut nobody can discover is not one.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def viewer(qapp):
    """Stubbed viewer with the real overlay buttons built on a real GL
    stand-in widget (``PdnViewer.__init__`` would open a whole board)."""
    from PySide6.QtWidgets import QMainWindow, QWidget
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    # One piece of named copper, so free markers have somewhere to anchor
    # and the buttons come up enabled.
    v.metadata = {"all_copper": [{"net": "+3V3"}]}
    v._project = None
    v._editor_mode = False
    v._editor_pending_marker = None
    v._held = QWidget()          # holds the buttons' parent alive
    v._gl_viewer = v._held
    V.PdnViewer._build_editor_overlay_buttons(v)
    V.PdnViewer._install_hotkeys(v)
    return v


def _bound_keys(v) -> list[str]:
    return [sc.key().toString() for sc in v._hotkey_shortcuts]


def test_s_and_l_are_bound_and_unique(viewer):
    keys = _bound_keys(viewer)
    assert "S" in keys and "L" in keys
    assert len(keys) == len(set(keys)), "a hotkey is bound twice"


def test_hotkey_arms_the_role_and_checks_the_button(viewer):
    viewer._editor_mode = True
    viewer._hotkey_arm_source_marker()
    assert viewer._editor_pending_marker == "SOURCE"
    assert viewer._editor_add_source_btn.isChecked()
    assert not viewer._editor_add_sink_btn.isChecked()

    viewer._hotkey_arm_sink_marker()
    assert viewer._editor_pending_marker == "SINK"
    assert viewer._editor_add_sink_btn.isChecked()
    assert not viewer._editor_add_source_btn.isChecked()


def test_same_hotkey_again_disarms(viewer):
    viewer._editor_mode = True
    viewer._hotkey_arm_sink_marker()
    viewer._hotkey_arm_sink_marker()
    assert viewer._editor_pending_marker is None
    assert not viewer._editor_add_sink_btn.isChecked()


def test_hotkey_is_a_noop_outside_editor_mode(viewer):
    viewer._editor_mode = False
    viewer._hotkey_arm_source_marker()
    viewer._hotkey_arm_sink_marker()
    assert viewer._editor_pending_marker is None


def test_hotkey_does_not_arm_without_named_copper(viewer):
    """Same guard the disabled button carries — nothing to anchor to."""
    viewer.metadata = {"all_copper": [{"net": "(none)"}]}
    viewer._editor_mode = True
    viewer._hotkey_arm_source_marker()
    assert viewer._editor_pending_marker is None


def test_button_tooltips_name_the_shortcut(viewer):
    import fypa.altium_viewer as V

    assert "(S)" in V.PdnViewer._MARKER_TIPS["SOURCE"]
    assert "(L)" in V.PdnViewer._MARKER_TIPS["SINK"]
    # Build time and the live _sync_marker_buttons text must agree.
    assert (viewer._editor_add_source_btn.toolTip()
            == V.PdnViewer._MARKER_TIPS["SOURCE"])
    assert (viewer._editor_add_sink_btn.toolTip()
            == V.PdnViewer._MARKER_TIPS["SINK"])


def test_help_tab_documents_the_keys_and_the_heatmap_heading():
    from fypa.altium_viewer import _HELP_SECTIONS

    # Help is organised by tab; the editor keys live in the Heatmap section.
    sections = dict(_HELP_SECTIONS)
    heatmap = sections["Heatmap"]
    assert "<h3>Keyboard shortcuts</h3>" in heatmap
    editor = heatmap.split("<h3>Editor mode")[1]
    assert "<kbd>S</kbd>" in editor
    assert "<kbd>L</kbd>" in editor


# --- Armed-drop cursor ------------------------------------------------------

class _CursorGL:
    """GL stand-in that records what the host tells it about the armed
    free-marker drop."""

    def __init__(self):
        self.calls: list = []

    def set_armed_marker(self, role, color=None):
        self.calls.append((role, color))


def test_arming_badges_the_viewport_cursor(viewer):
    import fypa.altium_viewer as V

    gl = _CursorGL()
    viewer._gl_viewer = gl
    viewer._editor_mode = True
    viewer._hotkey_arm_source_marker()
    assert gl.calls[-1] == (
        "SOURCE", V.PdnViewer._ROLE_MARKER_STYLE["SOURCE"]["color"])
    viewer._hotkey_arm_sink_marker()
    assert gl.calls[-1] == (
        "SINK", V.PdnViewer._ROLE_MARKER_STYLE["SINK"]["color"])
    viewer._hotkey_arm_sink_marker()
    assert gl.calls[-1] == (None, None)


def test_escape_disarms_a_pending_drop(viewer, qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    gl = _CursorGL()
    viewer._gl_viewer = gl
    viewer._editor_mode = True
    viewer._copper_selection = None
    viewer._hotkey_arm_source_marker()
    viewer.isActiveWindow = lambda: True
    esc = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    assert viewer.eventFilter(viewer, esc) is True
    assert viewer._editor_pending_marker is None
    assert not viewer._editor_add_source_btn.isChecked()
    assert gl.calls[-1] == (None, None)


def test_gl_viewer_swaps_the_idle_cursor_while_armed(qapp):
    from PySide6.QtCore import Qt
    from fypa.gl_mesh_viewer import GLMeshViewer

    g = GLMeshViewer()
    g.set_editor_mode(True)
    assert g.cursor().shape() == Qt.ArrowCursor
    g.set_armed_marker("SINK", "#3aa8ff")
    assert g.cursor().shape() == Qt.BitmapCursor
    assert g.cursor().hotSpot().toTuple() == (0, 0)
    # Legend-chip hover still wins over the armed badge ...
    g._apply_editor_cursor("pointing")
    assert g.cursor().shape() == Qt.PointingHandCursor
    # ... and the badge returns once the hover ends.
    g._apply_editor_cursor("default")
    assert g.cursor().shape() == Qt.BitmapCursor
    g.set_armed_marker(None)
    assert g.cursor().shape() == Qt.ArrowCursor
    # Outside editor mode the badge never shows.
    g.set_armed_marker("SOURCE", "#ff3030")
    g.set_editor_mode(False)
    assert g.cursor().shape() == Qt.ArrowCursor


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_armed_cursor_matches_the_platform_pointer_size(qapp, dpr):
    """Windows keeps the pointer at its own size under display scaling, so
    the badged arrow must not grow with the screen's device-pixel ratio."""
    from fypa.gl_mesh_viewer import _marker_drop_cursor, _system_cursor_px

    pm = _marker_drop_cursor("#ff3030", True, dpr).pixmap()
    px = _system_cursor_px()
    assert (pm.width(), pm.height()) == (px, px)
    assert pm.devicePixelRatio() == pytest.approx(dpr)


def test_armed_cursor_tip_sits_on_its_hotspot(qapp):
    """The arrow's tip pixel is the hotspot, so swapping to the armed
    cursor doesn't shift the pointer (on Windows it is the system arrow's
    own bitmap; elsewhere the drawn look-alike puts its tip on (0, 0))."""
    from fypa.gl_mesh_viewer import _marker_drop_cursor

    cur = _marker_drop_cursor("#3aa8ff", False, 1.0)
    img = cur.pixmap().toImage()
    hx, hy = cur.hotSpot().toTuple()
    assert img.pixelColor(hx, hy).alpha() > 128
    # Nothing opaque above or left of the hotspot.
    assert all(img.pixelColor(x, y).alpha() < 40
               for y in range(img.height()) for x in range(img.width())
               if x < hx or y < hy)
