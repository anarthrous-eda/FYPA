"""The window's one Solve button, on the tab row.

* it is pinned at the right-hand end of the tab strip, outside the part
  that scrolls, and survives the strip being rebuilt;
* it is always there and always called Solve: enabled when there is
  something to solve, greyed with a tooltip that says why when there is not;
* the button, File > Solve and F5 all go through one handler.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMainWindow,
    QPushButton,
)

import fypa.altium_viewer as av  # noqa: E402
from fypa.viewer.tab_strip import (  # noqa: E402
    GroupedTabWidget,
    TabGroup,
    TabStrip,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


# --- the strip's pinned corner ----------------------------------------------

def _strip():
    tabs = GroupedTabWidget()
    strip = TabStrip(tabs)
    tabs.strip = strip
    for name in ("Board", "Topology", "Nodes"):
        tabs.addTab(QLabel(name), name)
    strip.rebuild([TabGroup([0, 1]), TabGroup([2], caption="RESULTS")])
    return tabs, strip


def test_the_corner_widget_sits_outside_the_scrolling_tabs(qapp):
    _tabs, strip = _strip()
    btn = QPushButton("Solve")
    strip.set_corner_widget(btn)
    assert strip.corner_widget() is btn
    assert btn.parent() is strip                 # not the scrolled row
    assert not strip._scroll.widget().isAncestorOf(btn)


def test_the_corner_widget_is_right_of_the_tabs_and_survives_a_rebuild(qapp):
    _tabs, strip = _strip()
    btn = QPushButton("Solve")
    strip.set_corner_widget(btn)
    strip.resize(900, strip.sizeHint().height())
    strip.show()
    qapp.processEvents()
    assert btn.isVisible()
    assert btn.geometry().left() >= strip._scroll.geometry().right()
    assert btn.geometry().right() <= strip.width()
    strip.rebuild([TabGroup([0, 1, 2])])         # theme switch, tabs changed
    qapp.processEvents()
    assert strip.corner_widget() is btn and btn.isVisible()
    strip.close()


def test_the_corner_widget_stays_in_view_when_the_tabs_overflow(qapp):
    _tabs, strip = _strip()
    btn = QPushButton("Solve")
    strip.set_corner_widget(btn)
    strip.resize(170, strip.sizeHint().height())     # too narrow for the tabs
    strip.show()
    for _ in range(5):
        qapp.processEvents()
    assert strip._scroll.horizontalScrollBar().maximum() > 0
    assert btn.isVisible() and btn.geometry().right() <= strip.width()
    strip.close()


def test_the_corner_widget_can_be_replaced_and_removed(qapp):
    _tabs, strip = _strip()
    a, b = QPushButton("a"), QPushButton("b")
    strip.set_corner_widget(a)
    strip.set_corner_widget(b)
    assert a.parent() is None and strip.corner_widget() is b
    strip.set_corner_widget(None)
    assert strip.corner_widget() is None and b.parent() is None


# --- the button -------------------------------------------------------------

@pytest.fixture
def viewer(qapp):
    v = av.PdnViewer.__new__(av.PdnViewer)
    QMainWindow.__init__(v)
    v.state = SimpleNamespace(design=True, enabled=True, solveable=True)
    v._awaiting_first_solve = False
    v._has_design_to_solve = lambda: v.state.design
    v._resolve_button_enabled = lambda: v.state.enabled
    v._has_solveable_directives = lambda: v.state.solveable
    v._build_solve_button()
    return v


def test_it_is_a_labelled_button_with_the_play_icon(viewer):
    from fypa.viewer.assets import _solve_icon
    btn = viewer._solve_btn
    assert btn.text() == "Solve" and not btn.icon().isNull()
    icon = _solve_icon("#2ca92c", "#202020", "#909090")
    size = icon.availableSizes()[0]
    on = icon.pixmap(size, QIcon.Normal).toImage()
    off = icon.pixmap(size, QIcon.Disabled).toImage()
    assert on != off                             # greyed when disabled


def test_with_changes_to_solve_it_is_on_and_names_the_shortcut(viewer):
    viewer._refresh_solve_button()
    btn = viewer._solve_btn
    assert btn.isEnabled() and btn.text() == "Solve"
    assert btn.toolTip() == ("Run the solver again with the current setup "
                             "and settings (F5)")


def test_before_the_first_solve_it_says_so(viewer):
    viewer._awaiting_first_solve = True
    viewer._refresh_solve_button()
    assert viewer._solve_btn.isEnabled()
    assert viewer._solve_btn.toolTip() == (
        "Run the solver on the loaded design (F5)")


def test_up_to_date_it_is_greyed_and_says_why(viewer):
    viewer.state.enabled = False
    viewer._refresh_solve_button()
    btn = viewer._solve_btn
    assert not btn.isEnabled() and btn.text() == "Solve"   # same name
    assert btn.toolTip().startswith("The result is up to date.")
    assert "F5" not in btn.toolTip()


def test_with_no_closed_rail_it_says_what_is_missing(viewer):
    viewer.state.enabled = viewer.state.solveable = False
    viewer._refresh_solve_button()
    assert not viewer._solve_btn.isEnabled()
    assert viewer._solve_btn.toolTip() == av.PdnViewer._NOTHING_TO_SOLVE_TIP


def test_a_bridges_change_with_nothing_to_solve_offers_a_rebuild(viewer):
    viewer.state.solveable = False               # enabled: a rebuild waits
    viewer._refresh_solve_button()
    assert viewer._solve_btn.isEnabled()
    assert viewer._solve_btn.toolTip().startswith("Rebuild the design")


def test_a_bare_result_cannot_be_solved(viewer):
    viewer.state.design = False
    viewer._refresh_solve_button()
    assert not viewer._solve_btn.isEnabled()
    assert "isn't linked to a design" in viewer._solve_btn.toolTip()


def test_the_button_solves_and_closes_the_topology_sidebar_first(viewer):
    calls = []
    viewer._topology_tab_index = 1
    viewer.tabs = SimpleNamespace(currentIndex=lambda: viewer.current)
    viewer._topology_close_sidebar = lambda: calls.append("close sidebar")
    viewer._on_resolve_clicked = lambda: calls.append("solve")
    viewer._refresh_solve_button()

    viewer.current = 0                           # the Board
    viewer._solve_btn.click()
    assert calls == ["solve"]
    viewer.current = 1                           # Topology
    viewer._on_solve_requested()                 # what F5 / the menu call
    assert calls == ["solve", "close sidebar", "solve"]


def test_there_is_no_solve_button_left_on_the_board_or_topology():
    for gone in ("_sync_resolve_label", "_topology_refresh_resolve_btn",
                 "_on_topology_resolve"):
        assert not hasattr(av.PdnViewer, gone)
