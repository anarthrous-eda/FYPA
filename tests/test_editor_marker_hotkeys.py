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
    from fypa.altium_viewer import _HELP_TAB_BODY

    # The first shortcut table now says which tab it applies to.
    assert "<h3>Heatmap tab</h3>" in _HELP_TAB_BODY
    editor = _HELP_TAB_BODY.split("<h3>Editor mode")[1]
    assert "<kbd>S</kbd>" in editor
    assert "<kbd>L</kbd>" in editor
