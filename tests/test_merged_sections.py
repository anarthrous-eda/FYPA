"""Shorted nets as switchable sections of the net they were merged into.

The low-Ω merge folds HS_SNS into +12V before the solve, so there is one +12V
mesh and no HS_SNS mesh. The Rails control lists HS_SNS under +12V; switching
it off drops the triangles that lie in HS_SNS's original copper from the
+12V mesh (and switching +12V's own eye off leaves only the sections).
"""
from __future__ import annotations

import numpy as np
import pytest

from fypa.rail_groups import (
    RailTreeNode,
    attach_merged_children,
    flatten_rail_tree,
)
from fypa.viewer import merged_sections as ms


def _sq(x0, y0, x1, y1):
    return {"exterior": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            "holes": []}


META = {
    "premerged_copper": [
        {"layer_id": 1, "net": "HS_SNS", "canonical": "+12V",
         "polygons": [_sq(4, -1, 6, 1)]},
        {"layer_id": 1, "net": "+12V_IN", "canonical": "+12V",
         "polygons": [_sq(-6, -1, -4, 1)]},
        {"layer_id": 2, "net": "HS_SNS", "canonical": "HS_SNS",
         "whole_layer": True, "polygons": []},
    ],
    "net_canonical": {"HS_SNS": "+12V", "+12V_IN": "+12V"},
}


# --- pure helpers ------------------------------------------------------------

def test_children_listed_under_their_surviving_net():
    assert ms.merged_children(META, {"+12V", "GND"}) == {
        "+12V": ["+12V_IN", "HS_SNS"]}


def test_children_match_the_viewer_name_case_insensitively():
    assert ms.merged_children(META, {"+12v"}) == {"+12v": ["+12V_IN", "HS_SNS"]}


def test_no_children_without_records():
    assert ms.merged_children({}, {"+12V"}) == {}
    assert ms.merged_children(None, {"+12V"}) == {}


def test_a_whole_layer_plane_resolves_its_canonical_from_the_other_records():
    names, polys, whole = ms.section_records(META, 2, "+12V")
    assert names == ["HS_SNS"] and whole == "HS_SNS"


def test_triangles_are_labelled_by_the_section_their_centroid_is_in():
    names, polys, whole = ms.section_records(META, 1, "+12V")
    assert names == ["+12V_IN", "HS_SNS"] and whole is None
    # Three small triangles: one in +12V_IN, one in +12V's own copper,
    # one in HS_SNS.
    xs = np.array([-5.5, -4.5, -5.0, -0.5, 0.5, 0.0, 4.5, 5.5, 5.0])
    ys = np.array([-0.5, -0.5, 0.5] * 3)
    tris = np.arange(9).reshape(3, 3)
    labels = ms.triangle_labels(xs, ys, tris, names, polys, whole)
    assert labels.tolist() == [1, 0, 2]
    keep = ms.keep_mask(labels, names, frozenset({"HS_SNS"}), "+12V")
    assert keep.tolist() == [True, True, False]
    only_sns = ms.keep_mask(labels, names, frozenset({"+12V", "+12V_IN"}),
                            "+12V")
    assert only_sns.tolist() == [False, False, True]


def test_a_plane_layer_is_wholly_the_section():
    names, polys, whole = ms.section_records(META, 2, "+12V")
    labels = ms.label_points(np.array([0.0, 50.0]), np.array([0.0, 9.0]),
                             names, polys, whole)
    assert labels.tolist() == [1, 1]


def test_merged_children_hang_under_their_net_in_the_rail_tree():
    tree = RailTreeNode("+12V", (RailTreeNode("+12V_SW"),))
    out = attach_merged_children({"+12V": tree},
                                 {"+12V": ["HS_SNS"], "+12V_SW": ["X"]})
    assert flatten_rail_tree(out["+12V"]) == [
        ("+12V", 1), ("+12V_SW", 2), ("X", 3), ("HS_SNS", 2)]


def test_a_net_already_in_the_tree_is_not_added_again():
    tree = RailTreeNode("+12V", (RailTreeNode("HS_SNS"),))
    out = attach_merged_children({"+12V": tree}, {"+12V": ["HS_SNS"]})
    assert out["+12V"] == tree


# --- viewer: which nets draw, which sections hide ---------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QCheckBox, QMainWindow
    import fypa.altium_viewer as V
    from fypa.viewer.widgets import EyeButton
    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._rail_to_members = {"+12V": ["+12V"]}
    v._merged_children = {"+12V": ["+12V_IN", "HS_SNS"]}
    v._merged_parent = {"+12V_IN": "+12V", "HS_SNS": "+12V"}
    v._subnet_eye_buttons = {"+12V": {
        n: EyeButton(visible=True) for n in ("+12V", "+12V_IN", "HS_SNS")}}
    v.rail_only_box = QCheckBox()
    return v


def _set(v, net, on):
    v._subnet_eye_buttons["+12V"][net].setVisibleState(on, emit=False)


def test_display_members_list_the_sections(viewer):
    assert viewer._rail_display_members("+12V") == ["+12V", "+12V_IN",
                                                    "HS_SNS"]


def test_everything_on_hides_nothing(viewer):
    assert viewer._merged_hidden_sections() == {}
    assert viewer._effective_rail_members(["+12V"]) == ["+12V"]


def test_only_hs_sns(viewer):
    _set(viewer, "+12V", False)
    _set(viewer, "+12V_IN", False)
    # The +12V mesh still draws — it carries HS_SNS's copper — with
    # everything but the HS_SNS section masked off.
    assert viewer._effective_rail_members(["+12V"]) == ["+12V"]
    assert viewer._merged_hidden_sections() == {
        "+12V": frozenset({"+12V", "+12V_IN"})}


def test_parent_off_with_several_members_still_draws_for_a_section(viewer):
    from fypa.viewer.widgets import EyeButton
    viewer._rail_to_members = {"+12V": ["+12V", "+12V_SW"]}
    viewer._subnet_eye_buttons["+12V"]["+12V_SW"] = EyeButton(visible=False)
    _set(viewer, "+12V", False)
    _set(viewer, "+12V_IN", False)
    assert viewer._effective_rail_members(["+12V"]) == ["+12V"]
    _set(viewer, "HS_SNS", False)
    assert viewer._effective_rail_members(["+12V"]) == []


def test_section_keep_mask_for_a_layer(viewer):
    viewer.metadata = META
    viewer._phys_name_to_layer_id = {"Top": 1, "Mid": 3}
    viewer._merged_label_cache = {}
    xs = np.array([-5.5, -4.5, -5.0, -0.5, 0.5, 0.0, 4.5, 5.5, 5.0])
    ys = np.array([-0.5, -0.5, 0.5] * 3)
    entry = {"xs": xs, "ys": ys, "tris": np.arange(9).reshape(3, 3)}
    _set(viewer, "HS_SNS", False)
    hidden = viewer._merged_hidden_sections()
    keep = viewer._section_keep(0, "Top", "+12V", entry, hidden)
    assert keep.tolist() == [True, True, False]
    # A layer with no section on it is all +12V's own copper.
    assert viewer._section_keep(1, "Mid", "+12V", entry, hidden) is None
    _set(viewer, "+12V", False)
    hidden = viewer._merged_hidden_sections()
    assert not viewer._section_keep(1, "Mid", "+12V", entry, hidden).any()


def test_hover_skips_a_hidden_section():
    from matplotlib.tri import Triangulation
    import fypa.altium_viewer as V
    xs = np.array([0.0, 1.0, 0.0, 5.0, 6.0, 5.0])
    ys = np.array([0.0, 0.0, 1.0, 0.0, 0.0, 1.0])
    tri = Triangulation(xs, ys, np.array([[0, 1, 2], [3, 4, 5]]))
    lp = {"triangulation": tri, "tri_keep": np.array([True, False])}
    assert not V.PdnViewer._in_hidden_section(lp, 0.2, 0.2)
    assert V.PdnViewer._in_hidden_section(lp, 5.2, 0.2)
    lp["tri_keep"] = None
    assert not V.PdnViewer._in_hidden_section(lp, 5.2, 0.2)
