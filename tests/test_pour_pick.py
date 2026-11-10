"""Clicking a polygon pour selects the fused copper, not one Altium fragment.

Altium stores a poured polygon as many region records (thousands on a big
board), so outlining the single record under the cursor showed an arbitrary
block of the pour. A click on a pour fragment outlines - and reports - the
fused ``all_copper`` polygon under the click instead; a free region (not
part of a pour) still outlines just itself.
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


def _square(x0, x1, y0=0.0, y1=1.0):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _region(net, x0, x1, polygon_index, y0=0.0, y1=1.0):
    return {"kind": "region", "net": net, "layer_id": 1, "layer_ids": [1],
            "outline": _square(x0, x1, y0, y1), "holes": [],
            "polygon_index": polygon_index}


POUR_A = _region("GND", 0.0, 1.0, 3)      # two abutting fragments of pour 3
POUR_B = _region("GND", 1.0, 2.0, 3)
FREE = _region("GND", 5.0, 6.0, 65535)    # a region outside any pour


class _GL:
    def __init__(self):
        self.rings = None

    def set_primitive_selection_outline(self, rings):
        self.rings = rings


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    fused = np.array(_square(0.0, 2.0) + [[0.0, 0.0]], dtype=np.float32)
    free = np.array(_square(5.0, 6.0) + [[5.0, 0.0]], dtype=np.float32)
    v.metadata = {
        "primitives": {"regions": [POUR_A, POUR_B, FREE]},
        "vias": [], "pths": [], "pads": [],
        "all_copper": [{"layer_id": 1, "net": "GND", "polygons": [
            {"exterior": fused, "holes": []},
            {"exterior": free, "holes": []}]}],
    }
    for rec in (POUR_A, POUR_B, FREE):
        rec.pop("_shape", None)
        rec.pop("_prepared_shape", None)
    v._primitives_by_layer_net = None
    v._layer_strtrees_cache = None
    v._visible_all_copper_layer_ids = lambda: {1: "Top"}
    v._all_copper_at_point = lambda x, y: {"layer_id": 1, "net": "GND"}
    return v


def _bounds(rings):
    pts = np.array([p for ring in rings for p in ring])
    return tuple(round(float(v), 3) for v in (*pts.min(0), *pts.max(0)))


def test_pour_click_outlines_fused_copper(viewer):
    hit = viewer._primitive_at_point(0.5, 0.5)
    assert hit is not None and hit["record"] is POUR_A
    assert hit.get("pour_xy") == (0.5, 0.5)
    assert _bounds(viewer._primitive_outline_rings(hit)) == (0.0, 0.0, 2.0, 1.0)
    assert viewer._pour_fragment_count(hit) == 2


def test_free_region_outlines_itself(viewer):
    hit = viewer._primitive_at_point(5.5, 0.5)
    assert hit is not None and hit["record"] is FREE
    assert "pour_xy" not in hit
    assert _bounds(viewer._primitive_outline_rings(hit)) == (5.0, 0.0, 6.0, 1.0)


def test_pour_without_fused_polygon_falls_back_to_fragment(viewer):
    viewer.metadata["all_copper"] = []
    viewer._layer_strtrees_cache = None
    hit = viewer._primitive_at_point(1.5, 0.5)
    assert hit is not None and hit["record"] is POUR_B
    assert _bounds(viewer._primitive_outline_rings(hit)) == (1.0, 0.0, 2.0, 1.0)
