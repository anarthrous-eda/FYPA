"""With a net focused, a click picks the focused net's copper on any layer.

Net focus (the net table's crosshair) hides every other net, but the click
picker still took the topmost copper under the cursor - on most boards a
top-layer GND pour - and the focus check then rejected that hidden hit, so
the click read as bare substrate. Copper on any layer but the top could not
be selected. The picker must skip focus-hidden copper (pours, vias, pads,
heatmap rails) and reach the focused net underneath.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _square(x0, x1, y0, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


TOP, INNER = 1, 2

# A GND pour covering the board on the top layer, and a +3V3 track on the
# inner layer running underneath it.
GND_POUR = {"kind": "region", "net": "GND", "layer_id": TOP,
            "layer_ids": [TOP], "outline": _square(0.0, 10.0, 0.0, 10.0),
            "holes": [], "polygon_index": 65535}
V3_TRACK = {"kind": "track", "net": "+3V3", "layer_id": INNER,
            "layer_ids": [INNER], "ax": 2.0, "ay": 5.0, "bx": 8.0, "by": 5.0,
            "width_mm": 1.0}
# A GND via sitting on the track - also hidden by the focus.
GND_VIA = {"net": "GND", "x_mm": 3.0, "y_mm": 5.0, "diameter_mm": 0.6,
           "layer_start": TOP, "layer_end": INNER}


class _GL:
    def __init__(self):
        self.rings = None

    def set_primitive_selection_outline(self, rings):
        self.rings = rings


class _Eye:
    def isVisibleState(self):
        return True


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    pour = np.array(_square(0.0, 10.0, 0.0, 10.0) + [[0.0, 0.0]],
                    dtype=np.float32)
    track = np.array(_square(2.0, 8.0, 4.5, 5.5) + [[2.0, 4.5]],
                     dtype=np.float32)
    v.metadata = {
        "primitives": {"regions": [GND_POUR], "tracks": [V3_TRACK]},
        "vias": [GND_VIA], "pths": [], "pads": [],
        "all_copper": [
            {"layer_id": TOP, "net": "GND",
             "polygons": [{"exterior": pour, "holes": []}]},
            {"layer_id": INNER, "net": "+3V3",
             "polygons": [{"exterior": track, "holes": []}]},
        ],
    }
    for rec in (GND_POUR, V3_TRACK):
        rec.pop("_shape", None)
        rec.pop("_prepared_shape", None)
    v._primitives_by_layer_net = None
    v._layer_strtrees_cache = None
    v._phys_name_to_layer_id = {"Top": TOP, "Inner": INNER}
    v._phys_stackup_rank = {"Top": 0, "Inner": 1}
    v._layer_eye2_buttons = [("Top", _Eye()), ("Inner", _Eye())]
    v._selected_layer = None
    v._layer_probes = []
    v._editor_mode = True
    v._editor_focus_net = None
    v._editor_focus_nets = set()
    v._editor_focus_polys = set()
    v._gl_viewer = _GL()
    return v


def _focus(v, net):
    v._editor_focus_net = net
    v._editor_focus_nets = {net}


def test_without_focus_the_top_pour_wins(viewer):
    hit = viewer._primitive_at_point(5.0, 5.0)
    assert hit is not None and hit["net"] == "GND"
    assert hit["layer_id"] == TOP


def test_focus_picks_the_focused_net_on_a_lower_layer(viewer):
    _focus(viewer, "+3V3")
    hit = viewer._primitive_at_point(5.0, 5.0)
    assert hit is not None
    assert hit["record"] is V3_TRACK
    assert hit["layer_id"] == INNER


def test_a_hidden_via_does_not_shadow_the_focused_copper(viewer):
    _focus(viewer, "+3V3")
    hit = viewer._primitive_at_point(3.0, 5.0)
    assert hit is not None and hit["record"] is V3_TRACK


def test_focus_off_the_focused_net_is_bare_substrate(viewer):
    _focus(viewer, "+3V3")
    assert viewer._all_copper_at_point(5.0, 8.0) is None
    assert viewer._primitive_at_point(5.0, 8.0) is None


def test_focus_skips_a_hidden_heatmap_rail(viewer):
    """The solved-rail probe (rail mesh) is tried before all-copper; a
    focus-hidden rail on top must not catch the click either."""
    import shapely.geometry as sg
    import shapely.prepared as sp

    shape = sp.prep(sg.box(0.0, 0.0, 10.0, 10.0))
    viewer._layer_probes = [{"net": "GND", "layer_id": TOP,
                             "physical": "Top", "prepared_shape": shape}]
    viewer._in_hidden_section = lambda lp, x, y: False
    viewer._ensure_interpolator = lambda lp: (lambda x, y: 1.0)
    assert viewer._probe_at_point(5.0, 5.0) is not None
    _focus(viewer, "+3V3")
    assert viewer._probe_at_point(5.0, 5.0) is None
    pick = viewer._visible_editor_copper_pick(5.0, 5.0)
    assert pick is not None and pick["net"] == "+3V3"
    assert pick["layer_id"] == INNER
