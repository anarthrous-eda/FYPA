"""Setup chooser: the dialog offered when a design loads with no PDN setup,
the routes its cards take, and the "Don't show this again" preference."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QPushButton, QTabWidget, QWidget


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeWindow(QWidget):
    """Just enough of a viewer for the chooser's routes (a QWidget, so it
    can parent the dialog)."""

    def __init__(self, *, source_kind: str | None = None) -> None:
        super().__init__()
        self.metadata = {"source_kind": source_kind} if source_kind else {}
        self.tabs = QTabWidget()
        self._heatmap_tab_index = self.tabs.addTab(QWidget(), "Board")
        self._topology_tab_index = self.tabs.addTab(QWidget(), "Topology")
        self.tabs.setCurrentIndex(self._topology_tab_index)
        self._editor_toggle_btn = QPushButton()
        self._editor_toggle_btn.setCheckable(True)
        self.calls: list[str] = []
        self.messages: list[str] = []

    def _topology_run_autosetup(self) -> None:
        self.calls.append("auto")

    def _topology_run_manual_setup(self) -> None:
        self.calls.append("manual")

    def statusBar(self):
        return SimpleNamespace(
            showMessage=lambda msg, *_a: self.messages.append(msg))


def test_every_route_is_offered_for_an_altium_design(qapp):
    from fypa.viewer import setup_chooser as SC

    dlg = SC.SetupChooserDialog(None, has_schematic=True, on_load=True)
    assert set(dlg.card_buttons) == {
        SC.ROUTE_ALTIUM, SC.ROUTE_BOARD, SC.ROUTE_AUTO, SC.ROUTE_MANUAL}
    assert all(b.isEnabled() for b in dlg.card_buttons.values())


def test_gerber_design_offers_only_the_board(qapp):
    from fypa.viewer import setup_chooser as SC

    dlg = SC.SetupChooserDialog(None, has_schematic=False, on_load=True)
    enabled = {r for r, b in dlg.card_buttons.items() if b.isEnabled()}
    assert enabled == {SC.ROUTE_BOARD}


def test_a_card_button_picks_its_route(qapp):
    from fypa.viewer import setup_chooser as SC

    dlg = SC.SetupChooserDialog(None)
    dlg.card_buttons[SC.ROUTE_MANUAL].click()
    assert dlg.route == SC.ROUTE_MANUAL
    assert dlg.result() == dlg.DialogCode.Accepted


def test_altium_card_explains_instead_of_closing(qapp):
    from fypa.viewer import setup_chooser as SC

    dlg = SC.SetupChooserDialog(None)
    dlg.card_buttons[SC.ROUTE_ALTIUM].click()
    assert dlg.showing_altium_help()
    assert dlg.route is None


def test_dont_show_box_only_on_the_automatic_prompt(qapp):
    from fypa.viewer import setup_chooser as SC

    assert SC.SetupChooserDialog(None, on_load=True)._dont_show is not None
    assert SC.SetupChooserDialog(None, on_load=False)._dont_show is None


@pytest.mark.parametrize("route, tab, call", [
    ("board", "_heatmap_tab_index", None),
    ("auto", "_topology_tab_index", "auto"),
    ("manual", "_topology_tab_index", "manual"),
])
def test_routes_open_the_right_place(qapp, route, tab, call):
    from fypa.viewer.setup_chooser import run_setup_route

    win = _FakeWindow()
    win.tabs.setCurrentIndex(1 - getattr(win, tab))
    run_setup_route(win, route)
    assert win.tabs.currentIndex() == getattr(win, tab)
    assert win.calls == ([call] if call else [])
    assert win._editor_toggle_btn.isChecked() == (route == "board")


def test_no_route_does_nothing(qapp):
    from fypa.viewer.setup_chooser import run_setup_route

    win = _FakeWindow()
    run_setup_route(win, None)
    assert win.calls == [] and not win._editor_toggle_btn.isChecked()


def test_ticking_dont_show_saves_the_preference(qapp, monkeypatch):
    from fypa.viewer import setup_chooser as SC
    from fypa.viewer.prefs import load_show_setup_chooser

    def _exec(dlg):
        dlg._dont_show.setChecked(True)
        return 0
    monkeypatch.setattr(SC.SetupChooserDialog, "exec", _exec)
    assert load_show_setup_chooser()
    SC.open_setup_chooser(_FakeWindow(), on_load=True)
    assert not load_show_setup_chooser()


def _stub(needs: bool):
    return SimpleNamespace(solver_info={"needs_directives": True} if needs else {})


def test_unconfigured_load_opens_the_chooser(qapp, monkeypatch):
    from fypa.viewer import diagnostics
    from fypa.viewer import setup_chooser as SC

    opened = []
    monkeypatch.setattr(SC, "open_setup_chooser",
                        lambda win, **kw: opened.append(kw))
    diagnostics._maybe_warn_needs_directives(_FakeWindow(), _stub(True))
    assert opened == [{"on_load": True}]

    opened.clear()
    diagnostics._maybe_warn_needs_directives(_FakeWindow(), _stub(False))
    assert opened == []


def test_chooser_switched_off_leaves_a_status_hint(qapp, monkeypatch):
    from fypa.viewer import diagnostics
    from fypa.viewer import setup_chooser as SC
    from fypa.viewer.prefs import save_show_setup_chooser

    save_show_setup_chooser(False)
    opened = []
    monkeypatch.setattr(SC, "open_setup_chooser",
                        lambda win, **kw: opened.append(kw))
    win = _FakeWindow()
    diagnostics._maybe_warn_needs_directives(win, _stub(True))
    assert opened == []
    assert any("Set up sources and sinks" in m for m in win.messages)


# --- Empty-state panel -------------------------------------------------------

def test_panel_setup_state_offers_the_chooser_and_routes(qapp):
    from fypa.viewer.empty_state import EmptyStatePanel

    got = []
    p = EmptyStatePanel(got.append)
    p.set_state("No sources or sinks set up yet", "Body.", "setup")
    assert not p.button.isHidden()
    assert p.button.text() == "Set up sources and sinks…"
    assert p.text() == "No sources or sinks set up yet. Body."
    for route in ("board", "auto", "manual"):
        assert f"href='{route}'" in p._links.text()
    p.button.click()
    p._links.linkActivated.emit("auto")
    assert got == ["chooser", "auto"]


def test_panel_on_a_gerber_design_links_only_the_board(qapp):
    from fypa.viewer.empty_state import EmptyStatePanel

    p = EmptyStatePanel()
    p.set_state("T", "B", "setup", has_schematic=False)
    assert "href='board'" in p._links.text()
    assert "href='auto'" not in p._links.text()
    assert "href='manual'" not in p._links.text()


def test_panel_solve_state_and_no_action(qapp):
    from fypa.viewer.empty_state import EmptyStatePanel

    got = []
    p = EmptyStatePanel(got.append)
    p.set_state("Not solved yet", "Run a solve.", "solve")
    assert p.button.text() == "Solve" and p._links.isHidden()
    p.button.click()
    assert got == ["solve"]
    p.set_state("", "Needs component data.", None)
    assert p.button.isHidden() and p._links.isHidden()


def test_panel_actions_reach_the_viewer(qapp, monkeypatch):
    from fypa.viewer import empty_state
    from fypa.viewer import setup_chooser as SC

    win = _FakeWindow()
    solves = []
    win._on_resolve_clicked = lambda: solves.append(1)
    opened = []
    monkeypatch.setattr(SC, "open_setup_chooser",
                        lambda w, **kw: opened.append(kw))
    empty_state.run_empty_state_action(win, "chooser")
    empty_state.run_empty_state_action(win, "solve")
    empty_state.run_empty_state_action(win, "manual")
    assert opened == [{}] and solves == [1] and win.calls == ["manual"]


def test_topology_panel_covers_the_view_greys_solve_keeps_whatif(qapp):
    from PySide6.QtWidgets import QMainWindow

    import fypa.altium_viewer as av
    from fypa.viewer.empty_state import EmptyStatePanel
    from fypa.viewer.tabs.topology import _TopologyView

    class _V(av.PdnViewer):
        def __init__(self):
            QMainWindow.__init__(self)
            self.metadata = {}
            self._topology_view = _TopologyView(self, None)
            self._topology_view.resize(800, 600)
            self._topology_view.set_empty_panel(EmptyStatePanel())
            self._topology_whatif_btn = QPushButton("What-If")
            self._topology_whatif_btn.setToolTip("what-if tip")
            self._topology_resolve_btn = QPushButton("Solve")

        def _resolve_button_enabled(self):
            return False

    v = _V()
    v._topology_view.show()
    v._topology_show_setup_panel(True)
    panel = v._topology_view._empty_panel
    assert panel.isVisible() and panel.action == "setup"
    assert panel.geometry() == v._topology_view.viewport().geometry()
    # What-If stays selectable, like the Board's: its panel says why.
    assert v._topology_whatif_btn.isEnabled()
    assert "set up" in v._topology_resolve_btn.toolTip().lower()

    v._topology_show_setup_panel(False)
    assert not panel.isVisible()
    assert v._topology_whatif_btn.isEnabled()
    assert v._topology_whatif_btn.toolTip() == "what-if tip"
    assert "set up" not in v._topology_resolve_btn.toolTip().lower()
    v._topology_view.close()
