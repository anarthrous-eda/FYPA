"""Voltage Drop mode measures every rail from its own source.

One reference shared by all the visible rails made a 1 V rail read about
-11 V of "drop" beside a 12 V one. ``_drop_references`` gives each net its
rail's own reference, so several rails share one colour scale (and, in the
relief view, one height scale): each hangs below its own zero.
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


def _pin(net: str, x: float, layer_id: int = 1) -> dict:
    return {"net": net, "x_mm": x, "y_mm": 0.0, "layer_id": layer_id}


# Pin potentials of the "solution", keyed by (net, x).
_POTENTIALS = {
    ("+12V", 0.0): 12.0,      # J1 source +
    ("+12V", 5.0): 11.98,     # U1 regulator input
    ("+12V_F", 6.0): 11.95,   # after a ferrite inside the +12V rail
    ("+1V0", 0.0): 1.0,       # U1 regulator output
    ("+1V0", 9.0): 0.97,      # U2 sink
    ("GND", 0.0): 0.001,      # J1 source return
    ("GND", 9.0): 0.004,      # U2 sink return
}


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._rail_to_members = {
        "+12V": ["+12V", "+12V_F"],
        "+1V0": ["+1V0"],
        "GND": ["GND"],
    }
    v._subnet_eye_buttons = {}
    v._merged_children = {}
    v._phys_name_to_layer_id = {"Top": 1}
    v._index_by_pair = {("Top", "+12V"): 0, ("Top", "+12V_F"): 1,
                        ("Top", "+1V0"): 2, ("Top", "GND"): 3,
                        ("Top", "SIG"): 4}
    v.metadata = {"directives": [
        {"role": "SOURCE", "terminals": {
            "P": {"pins": [_pin("+12V", 0.0)]},
            "N": {"pins": [_pin("GND", 0.0)]}}},
        {"role": "REGULATOR", "terminals": {
            "IN_P": {"pins": [_pin("+12V", 5.0)]},
            "OUT_P": {"pins": [_pin("+1V0", 0.0)]}}},
        {"role": "SINK", "terminals": {
            "P": {"pins": [_pin("+1V0", 9.0), _pin("+12V_F", 6.0)]},
            "N": {"pins": [_pin("GND", 9.0)]}}},
    ]}
    v._solution_pin_potential = (
        lambda _lid, net, x, _y: _POTENTIALS.get((net, x)))
    return v


def test_each_rail_is_referenced_to_its_own_source(viewer):
    refs = viewer._drop_references(["+12V", "+1V0"])
    assert refs["+12V"] == pytest.approx(12.0)
    # No SOURCE feeds +1V0: its highest directive pin (the regulator
    # output) is the reference - not the 12 V source next to it.
    assert refs["+1V0"] == pytest.approx(1.0)


def test_every_net_of_a_rail_shares_the_rail_reference(viewer):
    refs = viewer._drop_references(["+12V"])
    # The net behind the ferrite is measured from the rail's source, so
    # the drop across the ferrite still counts.
    assert refs == {"+12V": pytest.approx(12.0),
                    "+12V_F": pytest.approx(12.0)}


def test_reference_does_not_depend_on_the_other_rails_showing(viewer):
    alone = viewer._drop_references(["+1V0"])["+1V0"]
    beside = viewer._drop_references(["+12V", "+1V0", "GND"])["+1V0"]
    assert alone == beside == pytest.approx(1.0)


def test_ground_is_referenced_to_its_source_return_pin(viewer):
    # A SOURCE has a pin on GND (its return), so that is GND's reference;
    # the sink-side ground rise then reads as a small positive value.
    assert viewer._drop_references(["GND"]) == {"GND": pytest.approx(0.001)}


def test_rail_with_no_sampled_pin_gets_no_reference(viewer):
    viewer._rail_to_members["+5V"] = ["+5V"]
    assert "+5V" not in viewer._drop_references(["+12V", "+5V"])


def test_hidden_subnet_is_left_out_unless_all_members_are_asked_for(viewer):
    class _Eye:
        def __init__(self, on):
            self._on = on

        def isVisibleState(self):
            return self._on

    viewer._subnet_eye_buttons = {
        "+12V": {"+12V": _Eye(True), "+12V_F": _Eye(False)}}
    import fypa.viewer.render as R
    orig = R._qt_widget_alive
    R._qt_widget_alive = lambda _w: True
    try:
        assert set(viewer._drop_references(["+12V"])) == {"+12V"}
        assert set(viewer._drop_references(["+12V"], all_members=True)) == {
            "+12V", "+12V_F"}
    finally:
        R._qt_widget_alive = orig


def test_per_vertex_offsets_follow_the_mesh_block_order(viewer):
    viewer._mesh_blocks = [
        {"net": "+12V", "n_vertices": 2},
        {"net": "SIG", "n_vertices": 1},      # no reference: left as is
        {"net": "+1V0", "n_vertices": 3},
    ]
    off = viewer._per_vertex_net_offsets({"+12V": 12.0, "+1V0": 1.0}, 6)
    assert off.tolist() == [12.0, 12.0, 0.0, 1.0, 1.0, 1.0]
    # Two rails end up on one scale: both top out at 0 at their source.
    volts = np.array([12.0, 11.98, 3.3, 1.0, 0.99, 0.97])
    drop = volts - off
    assert drop[[0, 3]].tolist() == [0.0, 0.0]
    assert drop[[1, 4, 5]] == pytest.approx([-0.02, -0.01, -0.03])
    # A length mismatch (stale blocks) or nothing to shift gives None.
    assert viewer._per_vertex_net_offsets({"+12V": 12.0}, 7) is None
    assert viewer._per_vertex_net_offsets({}, 6) is None


def test_export_references_cover_every_rail_layer_by_solution_index(viewer):
    refs = viewer._export_drop_references()
    assert refs == {0: pytest.approx(12.0), 1: pytest.approx(12.0),
                    2: pytest.approx(1.0), 3: pytest.approx(0.001)}
    assert 4 not in refs        # a net on no rail keeps the fallback


def test_stub_colours_use_the_stub_nets_own_reference(viewer):
    import fypa.altium_viewer as V
    from fypa.viewer.display import _VOLTAGE_DROP_MODE

    seen: list[float] = []
    viewer._stubs_coloured_by_voltage = lambda _mode: True
    viewer._cmap_name = "viridis"
    viewer._vmin, viewer._vmax = -0.05, 0.0
    viewer._sample_stub_voltage = (
        lambda _stub, net: {"+12V": 11.99, "+1V0": 0.98}[net])
    viewer._lut_lookup = (
        lambda _lut, val, _lo, _hi: (seen.append(val), (0.0, 0.0, 0.0))[1])
    V.PdnViewer._bake_stub_colors(
        viewer, [({}, "+12V", 3), ({}, "+1V0", 3)], _VOLTAGE_DROP_MODE,
        {"+12V": 12.0, "+1V0": 1.0})
    assert seen == pytest.approx([-0.01, -0.02])
