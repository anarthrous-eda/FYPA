"""The grouped tab strip: setup tabs | results tabs.

* the strip mirrors the tab widget it drives (current tab, tab titles);
* the results group says whether its numbers are current, out of date, or
  not there yet, and marks each results tab to match;
* the Capacitors tab carries the pending icon until its check has run for
  the loaded design, since its warning count isn't known before then;
* Messages / Settings / Help follow the results, not pushed to the right;
* the strip has no Solve / Resolve button of its own; those stay on the
  Heatmap and Topology tabs.
"""
from __future__ import annotations

import os
import types

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMainWindow,
)

import fypa.altium_viewer as av  # noqa: E402
from fypa.viewer.tab_strip import (  # noqa: E402
    STRETCH,
    GroupedTabWidget,
    TabGroup,
    TabStrip,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _strip(names=("Heatmap", "Topology", "Nodes", "Help")):
    tabs = GroupedTabWidget()
    strip = TabStrip(tabs)
    tabs.strip = strip
    for name in names:
        tabs.addTab(QLabel(name), name)
    strip.rebuild([
        TabGroup([0]),
        TabGroup([1], caption="SETUP", key="setup"),
        TabGroup([2], caption="RESULTS", key="results", tone="dim"),
        STRETCH,
        TabGroup([3]),
    ])
    return tabs, strip


def test_strip_follows_the_tab_widget(qapp):
    tabs, strip = _strip()
    assert tabs.tabBar().isHidden()
    assert strip.tab_order() == [0, 1, 2, 3]
    assert strip.tab_button(0).isChecked()
    tabs.setCurrentIndex(2)
    assert strip.tab_button(2).isChecked()
    assert not strip.tab_button(0).isChecked()


def test_clicking_a_strip_tab_switches_page(qapp):
    tabs, strip = _strip()
    strip.tab_button(3).click()
    assert tabs.currentIndex() == 3


def test_tab_title_badges_reach_the_strip(qapp):
    tabs, strip = _strip()
    tabs.setTabText(2, "Nodes ⚠ 3")
    assert strip.tab_button(2).text() == "Nodes ⚠ 3"


def test_missing_tabs_are_skipped(qapp):
    tabs = GroupedTabWidget()
    strip = TabStrip(tabs)
    tabs.addTab(QLabel("a"), "Heatmap")
    strip.rebuild([TabGroup([0, -1]),
                   TabGroup([-1], caption="RESULTS", key="results")])
    assert strip.tab_order() == [0]
    assert strip.group_caption("results") == ("", "")


class _Viewer(av.PdnViewer):
    """PdnViewer with just the state the strip reads."""

    def __init__(self, *, stub: bool, stale: bool = False,
                 mesh_failed: bool = False, rails=("+3V3",)):
        QMainWindow.__init__(self)
        self.tabs = GroupedTabWidget()
        self._tab_strip = TabStrip(self.tabs)
        self.tabs.strip = self._tab_strip
        self._rails = list(rails)
        self._via_current_warn_a = 1.0
        self.solution = types.SimpleNamespace(
            solver_info={"stub": stub}, sensitivity={"x": 1})
        self.metadata = {"mesh_failed": mesh_failed}
        self._project = None
        self._loaded_project = None
        self._solve_stale = stale
        self._awaiting_first_solve = stub
        self._heatmap_tab_index = self.tabs.addTab(QLabel(""), "Heatmap")
        self._topology_tab_index = self.tabs.addTab(QLabel(""), "Topology")
        self._nodes_tab_index = self.tabs.addTab(
            self._build_nodes_tab(), "Nodes")
        self._vias_tab_index = self.tabs.addTab(
            self._build_vias_tab(), "Vias")
        self._caps_stack = self._wrap_placeholder(QLabel(""))
        self._caps_tab_index = self.tabs.addTab(self._caps_stack, "Capacitors")
        self._rebuild_tab_strip()

    def states(self):
        return [self._tab_strip.tab_state(i) for i in (
            self._nodes_tab_index, self._vias_tab_index, self._caps_tab_index)]


def test_solved_and_current(qapp):
    v = _Viewer(stub=False)
    assert v._tab_strip.group_caption("results") == ("RESULTS", "ok")
    assert v.states() == ["", "", ""]


def test_setup_change_marks_results_out_of_date(qapp):
    v = _Viewer(stub=False, stale=True)
    assert v._tab_strip.group_caption("results") == (
        "RESULTS - OUT OF DATE", "warn")
    assert v.states() == ["stale", "stale", "stale"]


def test_unsolved_dims_only_tabs_with_nothing_to_show(qapp):
    """Capacitors are grouped by rail, and rails exist before any solve."""
    v = _Viewer(stub=True)
    assert v._tab_strip.group_caption("results") == (
        "RESULTS - NOT SOLVED YET", "dim")
    assert v.states() == ["empty", "empty", ""]


def test_failed_mesh_is_reported(qapp):
    v = _Viewer(stub=True, mesh_failed=True)
    assert v._tab_strip.group_caption("results") == (
        "RESULTS - SOLVE FAILED", "err")


def test_order_is_setup_then_results(qapp):
    v = _Viewer(stub=False)
    order = v._tab_strip.tab_order()
    assert order.index(v._topology_tab_index) < order.index(
        v._nodes_tab_index)
    assert order[0] == v._heatmap_tab_index


def test_solve_landing_clears_the_marks(qapp):
    v = _Viewer(stub=False, stale=True)
    v._solve_stale = False
    v._sync_placeholder_tabs()
    assert v.states() == ["", "", ""]
    assert v._tab_strip.group_caption("results") == ("RESULTS", "ok")


def _caps_title(v):
    return v.tabs.tabText(v._caps_tab_index)


def _caps_pending(v):
    return v._tab_strip.tab_pending(v._caps_tab_index)


def test_capacitors_unchecked_until_its_rows_are_built(qapp):
    v = _Viewer(stub=False)
    v._update_caps_tab_title()
    assert _caps_title(v) == "Capacitors"
    assert _caps_pending(v)
    assert not v._tab_strip.tab_button(v._caps_tab_index).icon().isNull()
    assert "Not checked" in v._tab_strip.tab_button(
        v._caps_tab_index).toolTip()
    v._caps_rows_cache = []
    v._update_caps_tab_title(3)
    assert _caps_title(v) == "Capacitors ⚠ 3"
    v._update_caps_tab_title(0)
    assert _caps_title(v) == "Capacitors"
    assert v._tab_strip.tab_button(v._caps_tab_index).toolTip() == ""
    assert not _caps_pending(v)
    assert v._tab_strip.tab_button(v._caps_tab_index).icon().isNull()


def test_reloading_the_design_brings_the_pending_icon_back(qapp):
    """A load drops the rows without rebuilding them - the old count no
    longer stands until the tab is opened again."""
    v = _Viewer(stub=False)
    v._caps_rows_cache = []
    v._update_caps_tab_title(2)
    v._invalidate_caps_cache(repopulate=False, heavy=True)
    assert _caps_title(v) == "Capacitors"
    assert _caps_pending(v)


def test_no_pending_icon_while_capacitors_show_a_placeholder(qapp):
    v = _Viewer(stub=True, rails=())
    v._update_caps_tab_title()
    assert not _caps_pending(v)


def test_groups_pack_left_without_a_stretch_marker(qapp):
    tabs = GroupedTabWidget()
    strip = TabStrip(tabs)
    for name in ("Heatmap", "Nodes", "Help"):
        tabs.addTab(QLabel(name), name)
    strip.rebuild([TabGroup([0]),
                   TabGroup([1], caption="RESULTS", key="results"),
                   TabGroup([2])])
    strip.resize(1200, 60)
    strip.show()
    QApplication.processEvents()
    help_btn = strip.tab_button(2)
    right = help_btn.mapTo(strip, help_btn.rect().topRight()).x()
    assert right < 600
