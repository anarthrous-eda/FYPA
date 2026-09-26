"""Placeholders on tabs that have nothing to show yet.

* Nodes / Vias: every value is sampled from the FEM, so on a stub (no solve)
  they say "Run a solve first" instead of showing a table of blank cells.
* Capacitors / Impedance: grouped by rail, and rails come from SOURCE / SINK
  directives — not from the solve. They show a placeholder only while no
  rail exists.
* Topology: shows a message instead of a diagram while no source or sink
  has been set up.

A tab showing its placeholder stays unpopulated until the placeholder clears.
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
    QTabWidget,
    QWidget,
)

import fypa.altium_viewer as av  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Viewer(av.PdnViewer):
    """PdnViewer with just the state the placeholder tabs touch.

    Nodes / Vias use their real builders; Capacitors / Impedance get a bare
    widget in the same wrapper, since only the placeholder logic is under
    test and their real builders want a loaded design.
    """

    def __init__(self, *, stub: bool, rails=("+3V3",), mesh_failed=False,
                 editor_roles=(), components=True):
        QMainWindow.__init__(self)
        self.tabs = QTabWidget()
        self._rails = list(rails)
        self._via_current_warn_a = 1.0
        self.solution = types.SimpleNamespace(solver_info={"stub": stub})
        self.metadata = {"mesh_failed": mesh_failed}
        self._project = types.SimpleNamespace(
            editor_directives=[types.SimpleNamespace(role=r)
                               for r in editor_roles],
            copper_names=[],
        )
        self._loaded_project = types.SimpleNamespace(
            extracted=types.SimpleNamespace(
                pcb_components=["C1"] if components else []))
        self.populated = []
        self._nodes_tab_index = self.tabs.addTab(
            self._build_nodes_tab(), "Nodes")
        self._vias_tab_index = self.tabs.addTab(
            self._build_vias_tab(), "Vias")
        self._caps_stack = self._wrap_placeholder(QWidget())
        self._caps_tab_index = self.tabs.addTab(self._caps_stack, "Capacitors")
        self._impedance_stack = self._wrap_placeholder(QWidget())
        self._impedance_tab_index = self.tabs.addTab(
            self._impedance_stack, "Impedance")
        self._sync_placeholder_tabs()
        self._nodes_table_populated = False
        self._vias_table_populated = False
        self._caps_table_populated = False
        self._impedance_populated = False

    def _populate_nodes_table(self):
        self.populated.append("nodes")

    def _populate_vias_table(self):
        self.populated.append("vias")

    def _ensure_cap_rows_async(self, then):
        then()

    def _populate_caps_table(self):
        self.populated.append("caps")

    def _populate_impedance_tab(self):
        self.populated.append("impedance")

    def open_all(self):
        for idx in (self._nodes_tab_index, self._vias_tab_index,
                    self._caps_tab_index, self._impedance_tab_index):
            self._on_tabs_current_changed(idx)


def _text(stack):
    return stack.widget(1).text() if stack.currentIndex() == 1 else ""


def test_stub_with_rails_hides_only_nodes_and_vias(qapp):
    """A schematic-annotated design has rails before any solve, so the
    capacitor analysis has something to show."""
    v = _Viewer(stub=True)
    for stack in (v._nodes_stack, v._vias_stack):
        assert _text(stack).startswith("Run a solve first to show ")
    assert _text(v._caps_stack) == ""
    assert _text(v._impedance_stack) == ""
    v.open_all()
    assert v.populated == ["caps", "impedance"]
    assert v._nodes_table_populated is False
    assert v._vias_table_populated is False


def test_no_rails_hides_caps_and_impedance(qapp):
    v = _Viewer(stub=True, rails=())
    for stack in (v._caps_stack, v._impedance_stack):
        assert _text(stack).startswith("No sources or sinks set up yet.")
    v.open_all()
    assert v.populated == []


def test_editor_sources_need_a_solve_to_define_rails(qapp):
    v = _Viewer(stub=True, rails=(), editor_roles=("SOURCE",))
    assert _text(v._caps_stack).startswith("Run a solve first to show ")


def test_gerber_import_says_components_are_missing(qapp):
    v = _Viewer(stub=True, rails=(), components=False)
    assert "Gerber" in _text(v._caps_stack)


def test_solved_shows_everything(qapp):
    v = _Viewer(stub=False)
    for stack in (v._nodes_stack, v._vias_stack,
                  v._caps_stack, v._impedance_stack):
        assert stack.currentIndex() == 0
    v.open_all()
    assert v.populated == ["nodes", "vias", "caps", "impedance"]


def test_solve_landing_swaps_placeholders_out(qapp):
    v = _Viewer(stub=True, rails=())
    v.solution = types.SimpleNamespace(solver_info={})
    v._rails = ["+3V3"]
    v._sync_placeholder_tabs()
    for stack in (v._nodes_stack, v._vias_stack,
                  v._caps_stack, v._impedance_stack):
        assert stack.currentIndex() == 0


def test_mesh_failure_says_so(qapp):
    v = _Viewer(stub=True, mesh_failed=True)
    assert "failed to mesh" in _text(v._nodes_stack)


class _TopologyView:
    def __init__(self):
        self.empty_message = None
        self.svg = None

    def set_empty_message(self, message):
        self.empty_message = message

    def set_diagram_svg(self, svg):
        self.svg = svg


class _TopologyViewer(av.PdnViewer):
    def __init__(self, directives):
        QMainWindow.__init__(self)
        self.metadata = {"directives": directives}
        self._project = None
        self._project_dirty = False
        self._initial_solve_stale = False
        self._loaded_project = None
        self._topology_view = _TopologyView()
        self._topology_hint = QLabel("")


def test_topology_without_sources_or_sinks_says_so(qapp):
    v = _TopologyViewer([{"role": "RESISTOR", "designator": "R1",
                          "terminals": {}}])
    v._populate_topology()
    assert v._topology_view.empty_message.startswith(
        "No sources or sinks set up yet.")
    assert v._topology_view.svg is None


def test_topology_live_preview_covers_saved_unsolved_edits(qapp):
    v = _TopologyViewer([])
    v._project = types.SimpleNamespace(
        editor_directives=[types.SimpleNamespace(role="SOURCE")],
        copper_names=[])
    assert v._topology_live_preview_needed() is False
    v._initial_solve_stale = True
    assert v._topology_live_preview_needed() is True
