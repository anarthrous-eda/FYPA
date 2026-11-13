"""Unsolved SERIES parts are visible before any solve.

A FET set up as switched on in the Bridges tab is an editor SERIES directive;
it only joins its drain and source nets in a solve, and with no SOURCE and
SINK there is nothing to solve. Until a solve includes it the canvas draws a
dashed link between its two sides and the hover says
"joined to +12V via Q4 (unsolved)".
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from fypa.project_file import EditorDirective, ProjectFile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _q4(**over):
    kw = {"kind": "component", "role": "SERIES", "designator": "Q4",
              "single_net": False, "p_net": "HS_SNS", "n_net": "+12V",
              "p_pins": ["5"], "n_pins": ["1", "2", "3"], "resistance": 700e-6,
              "overrides_designator": "Q4"}
    kw.update(over)
    return EditorDirective(**kw)


def _pad(pin, net, x, lid=1):
    return {"designator": f"Q4-{pin}", "net": net, "layer_ids": [lid],
            "outline": [[x - 0.2, -0.2], [x + 0.2, -0.2],
                        [x + 0.2, 0.2], [x - 0.2, 0.2]]}


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V
    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._project = ProjectFile()
    v._project.upsert_directive(_q4())
    v.metadata = {
        "directives": [],
        "pads": [_pad("1", "+12V", 0.0), _pad("2", "+12V", 1.0),
                 _pad("3", "+12V", 2.0), _pad("4", "HS_GATE1", 3.0),
                 _pad("5", "HS_SNS", 10.0)],
    }
    v._phys_name_to_layer_id = {"Top": 1}
    v._visible_layer_ids = lambda: {1}
    v._layer_color_for = lambda phys: "#ff0000"
    v._layer_z_for = lambda phys: 0.0
    return v


def _pending(v):
    import fypa.altium_viewer as V
    return [d.designator for d in V.PdnViewer._pending_series_directives(v)]


def test_an_unsolved_series_part_is_pending(viewer):
    assert _pending(viewer) == ["Q4"]


def test_once_solved_it_is_no_longer_pending(viewer):
    viewer.metadata["directives"] = [{"role": "RESISTOR", "designator": "Q4"}]
    assert _pending(viewer) == []


@pytest.mark.parametrize("over", [
    {"resistance": None}, {"resistance": 0.0}, {"role": "SOURCE"},
    {"n_net": None},
])
def test_incomplete_or_other_directives_are_not_links(viewer, over):
    viewer._project.editor_directives = [_q4(**over)]
    assert _pending(viewer) == []


def test_link_joins_the_chosen_pads_of_each_side(viewer):
    import fypa.altium_viewer as V
    links = V.PdnViewer._pending_series_links(viewer)
    assert len(links) == 1
    ln = links[0]
    assert [p[0] for p in ln["a"]] == [10.0]               # drain pad 5
    assert sorted(p[0] for p in ln["b"]) == [0.0, 1.0, 2.0]  # source 1-3
    assert ln["label"] == "Q4 (unsolved)"


def test_pin_filter_is_honoured(viewer):
    import fypa.altium_viewer as V
    viewer._project.editor_directives = [_q4(n_pins=["1"])]
    ln = V.PdnViewer._pending_series_links(viewer)[0]
    assert [p[0] for p in ln["b"]] == [0.0]


def test_no_link_when_its_pads_are_on_hidden_layers(viewer):
    import fypa.altium_viewer as V
    viewer._visible_layer_ids = lambda: {32}
    assert V.PdnViewer._pending_series_links(viewer) == []


def _label(v, net):
    import fypa.altium_viewer as V
    return V.PdnViewer._with_original_net(
        v, {"net": net, "physical": "Top"}, 0.0, 0.0)["net"]


def test_hover_says_what_the_net_is_joined_to(viewer):
    assert _label(viewer, "+12V") == "+12V   joined to HS_SNS via Q4 (unsolved)"
    assert _label(viewer, "HS_SNS") == "HS_SNS   joined to +12V via Q4 (unsolved)"
    assert _label(viewer, "HS_GATE1") == "HS_GATE1"


def test_hover_note_stops_once_solved(viewer):
    viewer.metadata["directives"] = [{"role": "RESISTOR", "designator": "Q4"}]
    assert _label(viewer, "+12V") == "+12V"


def test_hover_note_combines_with_the_original_net(viewer):
    """Merged copper keeps its "(original)" tag ahead of the note."""
    import fypa.altium_viewer as V
    viewer._original_net_at = lambda *a, **k: "+12V_MEZZ"
    out = V.PdnViewer._with_original_net(
        viewer, {"net": "+12V", "physical": "Top"}, 0.0, 0.0)["net"]
    assert out == "+12V (+12V_MEZZ)   joined to HS_SNS via Q4 (unsolved)"


# --- the overlay itself ------------------------------------------------------

class _FakeGL:
    """Just the state ``set_pending_links`` / ``_draw_pending_links`` use."""
    def __init__(self):
        self._pending_links = None
        self.updates = 0

    def update(self):
        self.updates += 1

    def world_to_screen(self, x, y, z=0.0):
        return x * 10.0, y * 10.0


def test_overlay_set_clear_and_no_repaint_when_unchanged(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer
    gl = _FakeGL()
    link = {"a": [(0, 0, 0)], "b": [(1, 1, 0)], "label": "Q4 (unsolved)"}
    GLMeshViewer.set_pending_links(gl, [link])
    GLMeshViewer.set_pending_links(gl, [link])
    assert gl.updates == 1
    GLMeshViewer.set_pending_links(gl, [])
    assert gl._pending_links is None and gl.updates == 2


def test_overlay_draws(qapp):
    from PySide6.QtGui import QImage, QPainter
    from fypa.gl_mesh_viewer import GLMeshViewer
    gl = _FakeGL()
    GLMeshViewer.set_pending_links(gl, [
        {"a": [(1, 1, 0)], "b": [(5, 1, 0), (6, 1, 0)], "label": "Q4"}])
    img = QImage(100, 40, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    GLMeshViewer._draw_pending_links(gl, p)
    p.end()
    # Something amber was painted on the line between the two sides.
    assert img.pixelColor(30, 10).alpha() > 0
