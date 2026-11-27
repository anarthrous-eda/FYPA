"""The yellow jump ring (Vias / Nodes Go, Topology "show on Board") goes away
on a Board left-click anywhere but on the ring, in viewer and editor mode."""

import pytest

import fypa.altium_viewer as V


class _GL:
    def __init__(self, mm_per_pixel: float) -> None:
        self._mpp = mm_per_pixel

    def view_center_scale(self):
        return 0.0, 0.0, self._mpp


class _Viewer:
    _JUMP_RING_HIT_PX = V.PdnViewer._JUMP_RING_HIT_PX
    _click_on_jump_ring = V.PdnViewer._click_on_jump_ring
    _on_gl_clicked = V.PdnViewer._on_gl_clicked

    def __init__(self, editor: bool) -> None:
        self._editor_mode = editor
        self._gl_viewer = _GL(0.1)      # ring hit radius = 1.6 mm
        self._highlight_via_xy = (10.0, 20.0)
        self.renders = 0
        self.editor_clicks = []

    def _render(self):
        self.renders += 1

    def _on_editor_click(self, x, y):
        self.editor_clicks.append((x, y))

    def _primitive_at_point(self, x, y):
        return None

    def _clear_copper_selection(self):
        pass


@pytest.mark.parametrize("editor", [False, True])
def test_click_off_the_ring_clears_it(editor):
    v = _Viewer(editor)
    v._on_gl_clicked(15.0, 20.0)
    assert v._highlight_via_xy is None
    assert v.renders == 1
    assert v.editor_clicks == ([(15.0, 20.0)] if editor else [])


@pytest.mark.parametrize("editor", [False, True])
def test_click_on_the_ring_keeps_it(editor):
    v = _Viewer(editor)
    v._on_gl_clicked(11.0, 20.5)
    assert v._highlight_via_xy == (10.0, 20.0)
    assert v.renders == 0
    assert v.editor_clicks == ([(11.0, 20.5)] if editor else [])
