"""Tab expands the dashed-yellow copper selection along its net.

With a copper primitive selected, Tab steps the outline out: the clicked
primitive -> every same-net primitive on its layer -> every same-net
primitive on all visible layers -> back to the clicked primitive. These pin
what each level contains (same net only, visible layers only, each record
once), that the cycle wraps, and that a fresh click starts over at the
single primitive rather than inheriting the old level.

The viewer is a stubbed :class:`~fypa.altium_viewer.PdnViewer` (its
``__init__`` opens a whole board); the GL widget is a recorder.
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


def _track(net, lid, x):
    return {"kind": "track", "net": net, "layer_id": lid,
            "ax": x, "ay": 0.0, "bx": x + 1.0, "by": 0.0, "width_mm": 0.2}


# Layers 1 (top), 2 (inner), 3 (bottom); layer 3 is hidden.
GND_L1_A = _track("GND", 1, 0.0)
GND_L1_B = _track("GND", 1, 5.0)       # a disjoint same-net island
GND_L2 = _track("GND", 2, 0.0)
GND_L3 = _track("GND", 3, 0.0)         # on the hidden layer
V3_L1 = _track("+3V3", 1, 10.0)
KEEPOUT = dict(_track("GND", 1, 20.0), is_keepout=True)
VIA_1_3 = {"net": "GND", "x_mm": 2.0, "y_mm": 0.0, "diameter_mm": 0.6,
           "layer_start": 1, "layer_end": 3}
VIA_V3 = {"net": "+3V3", "x_mm": 12.0, "y_mm": 0.0, "diameter_mm": 0.6,
          "layer_start": 1, "layer_end": 3}
PAD_L1 = {"net": "GND", "layer_ids": [1], "x_mm": 3.0, "y_mm": 0.0,
          "outline": [(2.9, -0.1), (3.1, -0.1), (3.1, 0.1), (2.9, 0.1)]}


class _GL:
    def __init__(self):
        self.rings = None

    def set_primitive_selection_outline(self, rings):
        self.rings = rings


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {
        "primitives": {"tracks": [GND_L1_A, GND_L1_B, GND_L2, GND_L3,
                                  V3_L1, KEEPOUT]},
        "vias": [VIA_1_3, VIA_V3],
        "pths": [],
        "pads": [PAD_L1],
    }
    v._primitives_by_layer_net = None
    v._editor_mode = False
    v._editor_selection = None
    v._editor_multi = []
    v._copper_selection = None
    v._phys_name_to_layer_id = {"Top": 1, "Inner": 2, "Bottom": 3}
    v._phys_stackup_rank = {"Top": 0, "Inner": 1, "Bottom": 2}
    # Top / Inner all-copper eyes on; no rails, so the heatmap eyes (all
    # open here, as they are by default) must not count.
    v._visible_all_copper_layer_ids = lambda: {1: "Top", 2: "Inner"}
    v._visible_layers = lambda: ["Top", "Inner", "Bottom"]
    v._visible_rails = lambda: []
    v._gl_viewer = _GL()
    host = QWidget()
    v._test_host = host
    v._copper_props_layout = QVBoxLayout(host)
    return v


def _hit(rec, lid):
    return {"kind": "track", "record": rec, "layer_id": lid,
            "net": rec["net"]}


def _outlined(v):
    """The records whose outline the last Tab pushed, recovered from the
    same hit list the viewer built them from."""
    return v._last_tab_records


@pytest.fixture
def spy(viewer, monkeypatch):
    """Record which primitive records each Tab level outlined."""
    import fypa.altium_viewer as V
    real = V.PdnViewer._same_net_copper_hits

    def wrapped(self, net, layer_ids):
        hits = real(self, net, layer_ids)
        self._last_tab_records = [id(h["record"]) for h in hits]
        return hits

    monkeypatch.setattr(V.PdnViewer, "_same_net_copper_hits", wrapped)
    return viewer


def test_first_tab_outlines_same_net_on_the_clicked_layer(spy):
    v = spy
    v._copper_selection = _hit(GND_L1_A, 1)
    assert v._cycle_tab_expand() is True
    got = set(_outlined(v))
    # Both GND islands on layer 1, the via crossing it, the layer-1 pad.
    assert got == {id(GND_L1_A), id(GND_L1_B), id(VIA_1_3), id(PAD_L1)}
    assert v._gl_viewer.rings


def test_second_tab_adds_visible_layers_only(spy):
    v = spy
    v._copper_selection = _hit(GND_L1_A, 1)
    v._cycle_tab_expand()
    v._cycle_tab_expand()
    got = _outlined(v)
    assert set(got) == {id(GND_L1_A), id(GND_L1_B), id(GND_L2),
                        id(VIA_1_3), id(PAD_L1)}
    # The via spans layers 1-3 but is outlined once, and nothing on the
    # hidden bottom layer is.
    assert len(got) == len(set(got))
    assert id(GND_L3) not in got


def test_rail_eyes_do_not_count_without_rails(spy):
    """Open heatmap eyes with nothing solved draw nothing — the bottom
    layer stays out; only the all-copper eyes decide."""
    v = spy
    v._copper_selection = _hit(GND_L1_A, 1)
    v._cycle_tab_expand()
    v._cycle_tab_expand()
    assert id(GND_L3) not in _outlined(v)


def test_rail_eyes_count_once_a_rail_is_visible(spy):
    v = spy
    v._visible_rails = lambda: ["GND"]
    v._copper_selection = _hit(GND_L1_A, 1)
    v._cycle_tab_expand()
    v._cycle_tab_expand()
    assert id(GND_L3) in _outlined(v)


def test_other_nets_and_keepouts_are_never_included(spy):
    v = spy
    v._copper_selection = _hit(GND_L1_A, 1)
    v._cycle_tab_expand()
    v._cycle_tab_expand()
    got = set(_outlined(v))
    assert not got & {id(V3_L1), id(VIA_V3), id(KEEPOUT)}


def test_third_tab_returns_to_the_clicked_primitive(viewer):
    v = viewer
    hit = _hit(GND_L1_A, 1)
    v._copper_selection = hit
    single = v._primitive_outline_rings(hit)
    for _ in range(3):
        v._cycle_tab_expand()
    assert v._gl_viewer.rings == single


def test_shift_tab_steps_backwards(spy):
    v = spy
    v._copper_selection = _hit(GND_L1_A, 1)
    v._cycle_tab_expand(-1)            # 0 -> 2: straight to all layers
    assert id(GND_L2) in _outlined(v)


def test_a_new_click_starts_over_at_level_one(spy):
    v = spy
    v._copper_selection = _hit(GND_L1_A, 1)
    v._cycle_tab_expand()
    v._cycle_tab_expand()              # level 2 on the old selection
    v._copper_selection = _hit(V3_L1, 1)
    v._cycle_tab_expand()
    assert set(_outlined(v)) == {id(V3_L1), id(VIA_V3)}


def test_tab_without_a_copper_selection_falls_through(viewer):
    """No selection: the key must go back to Qt's focus chain."""
    assert viewer._cycle_tab_expand() is False


def test_unnamed_copper_does_not_expand(viewer):
    rec = _track("", 1, 30.0)
    viewer._copper_selection = _hit(rec, 1)
    before = viewer._gl_viewer.rings
    assert viewer._cycle_tab_expand() is True
    assert viewer._gl_viewer.rings is before


def test_editor_mode_uses_the_copper_selection(spy):
    v = spy
    v._editor_mode = True
    v._pad_overlay_visible = lambda rec: True
    v._editor_selection = {"kind": "copper", "net": "GND", "layer_id": 1,
                           "hit": _hit(GND_L1_A, 1)}
    assert v._cycle_tab_expand() is True
    assert id(GND_L1_B) in _outlined(v)


def test_editor_mode_skips_pads_hidden_by_the_overlay(spy):
    v = spy
    v._editor_mode = True
    v._pad_overlay_visible = lambda rec: False
    v._editor_selection = {"kind": "copper", "net": "GND", "layer_id": 1}
    v._cycle_tab_expand()
    assert id(PAD_L1) not in _outlined(v)


# --- the GL draw copes with a whole-net outline ----------------------------

def test_outline_draw_culls_and_paints(qapp):
    from PySide6.QtGui import QImage, QPainter
    from fypa.gl_mesh_viewer import GLMeshViewer

    gl = GLMeshViewer()
    gl.resize(200, 100)
    gl._view_mode = "2d"
    gl._view_center_x, gl._view_center_y = 0.0, 0.0
    gl._mm_per_pixel = 0.1
    on = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    off = [(500.0, 500.0), (501.0, 500.0), (501.0, 501.0)]
    gl.set_primitive_selection_outline([on, off] * 500)
    assert gl._primitive_selection_xy.shape == (3500, 2)
    img = QImage(200, 100, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    try:
        gl._draw_primitive_selection(p)
    finally:
        p.end()
    # The on-screen square spans x 90-110, y 40-60 px: its top / bottom
    # edges must carry yellow dashes.
    yellow = [(x, y) for y in (39, 40, 41, 59, 60, 61)
              for x in range(88, 113)
              if img.pixelColor(x, y).name() == "#ffff00"]
    assert yellow
    gl.set_primitive_selection_outline(None)
    assert gl._primitive_selection_xy is None
    gl.deleteLater()
