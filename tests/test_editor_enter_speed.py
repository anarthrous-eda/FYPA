"""Entering editor mode on a big board.

Switching on Edit builds the connected-copper index over every
``all_copper`` polygon. On a large board (Mangala: ~190k polygons, ~14k
vias) that used to take 30-45 s on the GUI thread, long enough for Windows
to mark the window "Not Responding". These tests pin:

* the vectorised polygon build and the bulk via/PTH coupling give exactly
  what the per-record / per-via code they replaced gave, and
* a big board builds the index behind the busy dialog with the event loop
  still running, while a small board still enters synchronously.
"""
from __future__ import annotations

import time

import numpy as np
import pytest
import shapely.strtree as st

from fypa.viewer.editor.selection import (
    _bridge_polygon_hits_bulk,
    _bridge_polygon_hits_slow,
    _polygon_from_record,
    _polygons_from_records,
)


def _sq(x0, y0, s, dtype=np.float32, closed=True):
    pts = [(x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s)]
    if closed:
        pts.append((x0, y0))
    return np.array(pts, dtype=dtype)


def _wkb(g):
    return None if g is None else g.wkb


def test_polygons_from_records_matches_per_record():
    recs = [
        {"exterior": _sq(0, 0, 10), "holes": []},
        {"exterior": _sq(0, 0, 10), "holes": [_sq(2, 2, 2), _sq(6, 6, 2)]},
        {"exterior": _sq(20, 0, 5, dtype=np.float64)},          # no holes key
        {"exterior": _sq(30, 0, 5, closed=False), "holes": []},  # open ring
        {"exterior": _sq(0, 0, 10), "holes": [_sq(1, 1, 1, closed=False)]},
        {"exterior": [(0, 0), (1, 0), (1, 1), (0, 0)], "holes": []},  # list
        {"exterior": np.zeros((0, 2), np.float32), "holes": []},  # empty
        {"exterior": None},
        {"exterior": np.array([(0, 0), (1, 1), (0, 0)], np.float32)},  # short
        {"exterior": _sq(40, 0, 1), "holes": None},
    ]
    got = _polygons_from_records(recs)
    want = [_polygon_from_record(r) for r in recs]
    assert [_wkb(g) for g in got] == [_wkb(w) for w in want]
    assert got[0] is not None and got[1] is not None
    assert len(got[1].interiors) == 2
    assert got[6] is None and got[7] is None


def _indexed(layers):
    out = {}
    for lid, rings in layers.items():
        polys = [{"exterior": r, "holes": []} for r in rings]
        shapes = [_polygon_from_record(p) for p in polys]
        out[lid] = {"tree": st.STRtree(shapes), "shapes": shapes,
                    "polys": polys, "nets": ["N"] * len(polys)}
    return out


def test_bridge_hits_bulk_matches_per_via():
    indexed = _indexed({
        # Layer 1: two overlapping pours plus a small pad — a via in the
        # overlap must pick the same (first) one as the per-via code.
        1: [_sq(0, 0, 10), _sq(5, 5, 10), _sq(30, 30, 1)],
        2: [_sq(0, 0, 20), _sq(30, 30, 1)],
        4: [_sq(-5, -5, 50)],
    })
    bridges = [
        (7.0, 7.0, [1, 2, 4]),     # inside the layer-1 overlap
        (10.0, 3.0, [1, 2]),       # on a layer-1 boundary (counts as hit)
        (30.5, 30.5, [1, 2, 3, 4]),  # layer 3 has no copper at all
        (100.0, 100.0, [1, 2]),    # bare substrate
        (12.0, 12.0, [2, 4]),
    ]
    slow = _bridge_polygon_hits_slow(indexed, bridges)
    fast = _bridge_polygon_hits_bulk(indexed, bridges)
    assert fast == slow
    assert len(fast[0]) == 3 and fast[3] == []
    assert _bridge_polygon_hits_bulk(indexed, []) == []


def test_bridge_hits_bulk_random_layout_matches_per_via():
    rng = np.random.default_rng(7)
    layers = {lid: [_sq(*rng.uniform(0, 90, 2), rng.uniform(1, 25))
                    for _ in range(60)] for lid in (1, 2, 3)}
    indexed = _indexed(layers)
    bridges = [(float(x), float(y), [1, 2, 3])
               for x, y in rng.uniform(0, 100, (400, 2))]
    assert (_bridge_polygon_hits_bulk(indexed, bridges)
            == _bridge_polygon_hits_slow(indexed, bridges))


# --- deferred entry -------------------------------------------------------

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
    from PySide6.QtWidgets import QMainWindow, QToolButton
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {
        "all_copper": [
            {"layer_id": 1, "net": "GND",
             "polygons": [{"exterior": _sq(0, 0, 10), "holes": []}]},
            {"layer_id": 2, "net": "GND",
             "polygons": [{"exterior": _sq(0, 0, 10), "holes": []}]},
        ],
        "vias": [{"layer_start": 1, "layer_end": 2, "x_mm": 5, "y_mm": 5}],
    }
    v._phys_name_to_layer_id = {"Top": 1, "Bottom": 2}
    v._layer_strtrees_cache = None
    v._cc_cache = None
    v._all_copper_bridges_cache = None
    v.applied = []
    v._apply_editor_mode = lambda checked: v.applied.append(
        (checked, v._cc_cache is not None))
    btn = QToolButton(v)
    btn.setCheckable(True)
    btn.toggled.connect(v._on_editor_mode_toggled)
    v._editor_toggle_btn = btn
    yield v
    v.deleteLater()


def test_small_board_enters_synchronously(viewer):
    viewer._editor_toggle_btn.setChecked(True)
    assert viewer.applied == [(True, False)]


def test_big_board_builds_index_behind_busy_dialog(viewer, qapp,
                                                   monkeypatch):
    from PySide6.QtWidgets import QProgressDialog
    monkeypatch.setattr(type(viewer), "_EDITOR_PREP_BACKGROUND_MIN_POLYS", 2)

    viewer._editor_toggle_btn.setChecked(True)
    # Returns at once; entry waits for the index.
    assert viewer.applied == []
    assert any(isinstance(w, QProgressDialog) and w.isVisible()
               for w in qapp.topLevelWidgets())
    deadline = time.monotonic() + 10
    while not viewer.applied and time.monotonic() < deadline:
        qapp.processEvents()
    assert viewer.applied == [(True, True)]
    # The two GND polygons are joined through the via.
    assert len(viewer._cc_cache["members_of"]) == 1
    qapp.processEvents()
    assert not any(isinstance(w, QProgressDialog) and w.isVisible()
                   for w in qapp.topLevelWidgets())

    # Leaving, and re-entering with the index cached, are immediate.
    viewer._editor_toggle_btn.setChecked(False)
    viewer._editor_toggle_btn.setChecked(True)
    assert viewer.applied[1:] == [(False, True), (True, True)]
