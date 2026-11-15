"""Vias / Plated TH Board Features rows - they replaced the VIA / PTH legend
toggles, so these pin down the visibility, colour and transparency they
drive on the 2D via / PTH markers, and that a row change refreshes the via
path rather than the overlay batch."""

from __future__ import annotations

import pytest

from fypa.viewer.overlays import _OVERLAY_LAYERS, _VIA_OVERLAY_KEYS
from fypa.viewer.vias import _fade_chunk, _rgba_hex


class _FakeGL:
    def __init__(self) -> None:
        self.markers = []
        self.legend = None

    def set_markers(self, groups) -> None:
        self.markers = list(groups)

    def set_series_links(self, _links) -> None:
        pass

    def set_overlay_top_right_legend(self, rows) -> None:
        self.legend = list(rows)


class _Box:
    def __init__(self, on: bool = False, text: str = "Voltage") -> None:
        self._on = on
        self._text = text

    def isChecked(self) -> bool:
        return self._on

    def currentText(self) -> str:
        return self._text


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._project = None
    v.metadata = {
        "source_kind": "altium",
        "directives": [],
        # Layer 1 = top, 2 = bottom.
        "vias": [
            {"x_mm": 1.0, "y_mm": 1.0, "net": "VCC", "diameter_mm": 0.6,
             "hole_diameter_mm": 0.3, "layer_start": 1, "layer_end": 2},
            {"x_mm": 2.0, "y_mm": 2.0, "net": "SIG", "diameter_mm": 0.6,
             "hole_diameter_mm": 0.3, "layer_start": 1, "layer_end": 2},
        ],
        "pths": [
            {"x_mm": 5.0, "y_mm": 5.0, "net": "VCC", "diameter_mm": 1.8,
             "hole_diameter_mm": 1.0, "layer_start": 1, "layer_end": 2},
            {"x_mm": 6.0, "y_mm": 6.0, "net": "CHASSIS", "diameter_mm": 1.8,
             "hole_diameter_mm": 1.0, "layer_start": 1, "layer_end": 2},
        ],
    }
    v._phys_name_to_layer_id = {"Top": 1, "Bottom": 2}
    v.view_3d_box = _Box(False)
    v.mode_combo = _Box(text="Voltage")
    v._gl_viewer = _FakeGL()
    v._hidden_legend_keys = set()
    v._highlight_via_xy = None
    v._editor_mode = False
    v._copper_eye = set()
    v._visible_all_copper_layer_ids = lambda: dict.fromkeys(v._copper_eye)
    v._effective_rail_members = lambda rails: list(rails)
    v._editor_focus_active = lambda: False
    v._overridden_designators = lambda: set()
    v._editor_marker_groups = list
    v._editor_marker_hover_rows = list
    v._set_marker_hover_rows = lambda rows: None
    v._refresh_pending_links = lambda: None
    v._init_overlay_state()
    return v


def _points(v, color_rgb) -> set[tuple[float, float]]:
    """Marker positions drawn in the given row colour (alpha ignored)."""
    want = _rgba_hex(color_rgb)[3:]
    out: set[tuple[float, float]] = set()
    for g in v._gl_viewer.markers:
        if g.color[3:] == want:
            out.update(zip(g.xs.tolist(), g.ys.tolist()))
    return out


def _vias(v):
    return _points(v, v._overlay_colors["vias"])


def _pths(v):
    return _points(v, v._overlay_colors["pths"])


def test_rows_exist_and_start_on_rails_eye(viewer) -> None:
    keys = [k for k, _l, _s in _OVERLAY_LAYERS]
    assert "vias" in keys and "pths" in keys
    for key in _VIA_OVERLAY_KEYS:
        assert viewer._overlay_state[key]["both"]["vis"] == "rails"
        assert "top" not in viewer._overlay_state[key]   # no Top/Bottom split


def test_rails_eye_keeps_layer_driven_behaviour(viewer) -> None:
    viewer._update_markers_and_legend(["Top"], ["VCC"])
    assert _vias(viewer) == {(1.0, 1.0)}
    assert _pths(viewer) == {(5.0, 5.0)}
    # The legend no longer carries VIA / PTH rows.
    assert not {r.key for r in viewer._gl_viewer.legend} & {"VIA", "PTH"}


def test_rails_eye_follows_all_copper_eye(viewer) -> None:
    viewer._copper_eye = {1}
    viewer._update_markers_and_legend([], [])
    assert _vias(viewer) == {(1.0, 1.0), (2.0, 2.0)}
    assert _pths(viewer) == {(5.0, 5.0), (6.0, 6.0)}


def test_all_eye_shows_every_site_with_no_layer_or_rail(viewer) -> None:
    viewer._overlay_state["pths"]["both"]["vis"] = "all"
    viewer._update_markers_and_legend([], [])
    assert _pths(viewer) == {(5.0, 5.0), (6.0, 6.0)}
    assert _vias(viewer) == set()        # Vias row still on the rails eye


def test_hidden_row_hides_only_that_kind(viewer) -> None:
    viewer._overlay_state["vias"]["both"]["vis"] = None
    viewer._update_markers_and_legend(["Top"], ["VCC"])
    assert _vias(viewer) == set()
    assert _pths(viewer) == {(5.0, 5.0)}


def test_row_colour_and_transparency_reach_the_markers(viewer) -> None:
    from fypa.viewer.widgets import _transparency_alpha
    viewer._overlay_colors["pths"] = (0.0, 0.0, 1.0)
    viewer._overlay_state["pths"]["both"]["alpha_step"] = 2
    viewer._update_markers_and_legend(["Top"], ["VCC"])
    (grp,) = [g for g in viewer._gl_viewer.markers
              if g.color.endswith("0000ff")]
    assert grp.color == _rgba_hex((0.0, 0.0, 1.0), _transparency_alpha(2))
    assert grp.color != _rgba_hex((0.0, 0.0, 1.0))   # actually faded
    # Fully transparent behaves as hidden.
    from fypa.viewer.widgets import _TRANSPARENCY_STEPS
    viewer._overlay_state["pths"]["both"]["alpha_step"] = (
        _TRANSPARENCY_STEPS - 1)
    viewer._update_markers_and_legend(["Top"], ["VCC"])
    assert _pths(viewer) == set()


def test_via_row_change_refreshes_via_path_not_overlay_batch(viewer) -> None:
    calls: list[str] = []
    viewer._visible_rails = lambda: ["VCC"]
    viewer._visible_layers = lambda: ["Top"]
    viewer._refresh_overlay_geometry = lambda rails: calls.append("overlay")
    viewer._refresh_board_outline = lambda: calls.append("outline")
    viewer._update_markers_and_legend = (
        lambda phys, rails: calls.append("markers"))
    viewer._on_overlay_changed("vias")
    assert calls == ["markers"]
    calls.clear()
    viewer._on_overlay_changed("pads")
    assert calls == ["overlay", "outline"]


def test_fade_chunk_does_not_mutate_cached_colours() -> None:
    import numpy as np
    rgb = np.ones((3, 3), dtype=np.float32)
    out = _fade_chunk(rgb, 0.5)
    assert out.shape == (3, 4) and np.allclose(out[:, 3], 0.5)
    rgba = np.ones((2, 4), dtype=np.float32)
    _fade_chunk(rgba, 0.25)
    assert np.allclose(rgba[:, 3], 1.0)
