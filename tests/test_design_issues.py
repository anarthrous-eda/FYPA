"""The design's setup problems as records (fypa.design_issues), the Design
issues panel that lists them and the one pop-up that announces them
(fypa.viewer.issues_panel).

* the message lists in the solve metadata become records with a severity
  and the parts / nets each message names;
* the panel is there only while the design has an error or a warning, is
  closed by default, and its counts are filters;
* the pop-up comes once per new problem, not after every solve;
* double-click goes to the part: on the Topology diagram when that tab is
  open, else on the Board;
* the same records put the warning icons and hover text on Topology blocks.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from fypa.design_issues import (
    ERROR,
    NOTE,
    WARNING,
    DesignIssue,
    apply_to_topology,
    collect_design_issues,
    count_by_severity,
    issues_by_designator,
    summary_text,
)


def _term(*nets):
    return {"pins": [{"net": n, "x_mm": 1.0, "y_mm": 2.0, "layer_id": 1,
                      "component": "X", "pad": "1"} for n in nets]}


def _directives():
    return [
        {"role": "SOURCE", "designator": "J1", "label": "J1",
         "terminals": {"P": _term("+12V"), "N": _term("GND")}},
        {"role": "SINK", "designator": "U1", "label": "U1",
         "terminals": {"P": _term("+3V3"), "N": _term("GND")}},
        {"role": "SINK", "designator": "U12", "label": "U12",
         "terminals": {"P": _term("+3V3"), "N": _term("GND")}},
        {"role": "REGULATOR", "designator": "U5", "label": "U5",
         "terminals": {"OUT_P": _term("+3V3"), "OUT_N": _term("GND"),
                       "IN_P": _term("+12V"), "IN_N": _term("GND")}},
        {"role": "RESISTOR", "designator": "R7", "label": "R7",
         "terminals": {"P": _term("+3V3"), "N": _term("3V3_SW")}},
    ]


OPEN_LOOP = ("Rail '+5V': has a SINK but no SOURCE - no current can flow, so "
             "this rail will NOT be solved.")
BREAK = ("Net '+3V3': the source and sink markers sit on copper that is not "
         "electrically connected. Markers per island: [U5(Regulator)]; "
         "[U12(Sink)]")
BRIDGE = ("FB3 (ferrite bead) joins +3V3 to copper outside the solve. If it "
          "conducts at DC, give it a resistance; the path through 'VDD_A' is "
          "then solved too.")
SKIPPED = "SINK on U12: net 'VCC_TYPO' does not exist on the PCB"
NOTE_MSG = "SINK on U1: resolved local net 'VIN' via pins ['U1-3']"
PLAIN_WARNING = "U1: unknown PDN parameter 'PDN_CURENT' (ignored)"


def _metadata(**extra):
    md = {
        "directives": _directives(),
        "components": [{"designator": "FB3", "side": "top",
                        "bbox": [10.0, 20.0, 12.0, 21.0], "nets": []},
                       {"designator": "C3", "side": "top",
                        "bbox": [0, 0, 1, 1], "nets": []}],
        "bridge_candidates": [{"designator": "FB3", "excluded_net": "VDD_A"}],
        "active_nets": ["+12V", "+3V3", "+5V", "GND"],
        "annotation_errors": [SKIPPED],
        "connectivity_breaks": [BREAK],
        "open_loop_rails": [OPEN_LOOP],
        "unannotated_bridges": [BRIDGE],
        # The loader also lists the three above in its warnings.
        "annotation_warnings": [OPEN_LOOP, BREAK, BRIDGE, NOTE_MSG,
                                PLAIN_WARNING],
    }
    md.update(extra)
    return md


# --- records ----------------------------------------------------------------

def test_each_list_becomes_issues_of_its_severity_errors_first():
    issues = collect_design_issues(_metadata())
    got = [(i.severity, i.kind) for i in issues]
    assert got == [
        (ERROR, "directive-skipped"),
        (ERROR, "copper-break"),
        (WARNING, "rail-not-solved"),
        (WARNING, "unannotated-bridge"),
        (WARNING, "annotation-warning"),
        (NOTE, "annotation-note"),
    ]
    assert count_by_severity(issues) == {ERROR: 2, WARNING: 3, NOTE: 1}
    assert summary_text(issues) == "2 errors and 3 warnings"


def test_a_message_listed_twice_by_the_loader_is_one_issue():
    """Open-loop, bridge and break messages are also in the warnings."""
    messages = [i.message for i in collect_design_issues(_metadata())]
    assert len(messages) == len(set(messages)) == 6


def test_bridges_are_notes_once_the_user_turned_their_notice_off():
    issues = collect_design_issues(_metadata(), bridges_as_warnings=False)
    bridge = next(i for i in issues if i.kind == "unannotated-bridge")
    assert bridge.severity == NOTE
    assert count_by_severity(issues)[WARNING] == 2


def test_what_fypa_worked_out_by_itself_is_a_note_not_a_warning():
    issues = collect_design_issues({"annotation_warnings": [
        NOTE_MSG,
        "SOURCE on J1: expanding to 4 multi-channel PCB instances (J1A)",
        "FB1 (pwr.SchDoc): auto-bridged as SERIES (1 mΩ) because 0R",
        PLAIN_WARNING,
    ]})
    assert [i.severity for i in issues] == [WARNING, NOTE, NOTE, NOTE]


def test_parts_are_found_as_whole_words_in_the_order_named():
    by_kind = {i.kind: i for i in collect_design_issues(_metadata())}
    # U12, not also U1; a pin (U1-3) names its part, not a part called "3".
    assert by_kind["directive-skipped"].designators == ("U12",)
    assert by_kind["copper-break"].designators == ("U5", "U12")
    assert by_kind["annotation-note"].designators == ("U1",)
    assert by_kind["unannotated-bridge"].designators == ("FB3",)


def test_nets_are_the_quoted_names_the_design_has():
    by_kind = {i.kind: i for i in collect_design_issues(_metadata())}
    assert by_kind["rail-not-solved"].nets == ("+5V",)
    assert by_kind["copper-break"].nets == ("+3V3",)
    # A net that doesn't exist is not somewhere to go; nor is the tail of
    # +3V3 a part called "3V3".
    assert by_kind["directive-skipped"].nets == ()
    assert by_kind["rail-not-solved"].designators == ()
    assert by_kind["rail-not-solved"].where == "+5V"
    assert by_kind["copper-break"].where == "U5, U12"


def test_mesh_failures_and_held_setup_entries_are_issues():
    issues = collect_design_issues({
        "mesh_failed": True,
        "mesh_failures": [{"net": "+3V3", "summary": "sliver on Top",
                           "location_xy": [1, 2], "layer_id": 1}],
        "active_nets": ["+3V3"],
        "setup_held": ["SINK on U9 - no such part", "SINK on U8 - moved"],
    })
    mesh, held = issues
    assert (mesh.severity, mesh.kind, mesh.nets) == (
        ERROR, "mesh-failed", ("+3V3",))
    assert "sliver on Top" in mesh.message
    assert (held.severity, held.kind) == (WARNING, "setup-held")
    assert held.message.startswith("2 saved setup entries were not applied")


def test_a_mesh_failure_nobody_could_locate_still_says_so():
    (issue,) = collect_design_issues({"mesh_failed": True,
                                      "mesh_failures": []})
    assert issue.kind == "mesh-failed" and "no marker" in issue.message


def test_nothing_wrong_is_no_issues_and_no_summary():
    assert collect_design_issues(None) == []
    assert collect_design_issues({"directives": _directives()}) == []
    assert summary_text([]) == ""
    assert summary_text([DesignIssue(WARNING, "k", "L", "m")]) == "1 warning"


# --- Topology blocks --------------------------------------------------------

def test_a_rail_issue_lands_on_the_parts_that_put_the_rail_in_the_solve():
    rail = DesignIssue(WARNING, "rail-not-solved", "Rail not solved",
                       "Rail '+3V3': ...", nets=("+3V3",))
    by_des = issues_by_designator([rail], _directives())
    # The sinks and the regulator on +3V3; not the SERIES part, not J1.
    assert sorted(by_des) == ["U1", "U12", "U5"]


def test_an_issue_that_names_parts_lands_only_on_them():
    issue = DesignIssue(ERROR, "copper-break", "Disconnected copper", "x",
                        designators=("U5", "U12"), nets=("+3V3",))
    assert sorted(issues_by_designator([issue], _directives())) == [
        "U12", "U5"]


def test_notes_put_no_icon_on_a_block():
    note = DesignIssue(NOTE, "annotation-note", "Setup note", "x",
                       designators=("U1",))
    assert issues_by_designator([note], _directives()) == {}


def _node(des, tooltip="SINK " + "x"):
    return SimpleNamespace(designator=des, tooltip=tooltip, has_error=False,
                           has_warning=False)


def test_blocks_get_the_icon_and_the_message_on_hover():
    model = SimpleNamespace(nodes=[_node("U1"), _node("U12"), _node("J1")])
    issues = [
        DesignIssue(ERROR, "directive-skipped", "Directive skipped",
                    SKIPPED, designators=("U12",)),
        DesignIssue(WARNING, "annotation-warning", "Setup warning",
                    PLAIN_WARNING, designators=("U1",)),
    ]
    assert apply_to_topology(model, issues, _directives()) == 2
    u1, u12, j1 = model.nodes
    assert (u1.has_error, u1.has_warning) == (False, True)
    assert (u12.has_error, u12.has_warning) == (True, False)
    assert (j1.has_error, j1.has_warning) == (False, False)
    assert u1.tooltip.startswith("SINK x\n\nWarning - Setup warning: ")
    assert SKIPPED in u12.tooltip and j1.tooltip == "SINK x"


def test_a_warned_block_is_drawn_with_an_amber_triangle():
    from fypa.topology import build_topology_model, render_topology_svg
    from fypa.topology.style import palette
    from tests.topology_fixtures import project_b_compact_metadata

    md = project_b_compact_metadata()
    model = build_topology_model(md)
    j1 = next(n for n in model.nodes if n.designator == "J1")
    assert not j1.has_error
    before = render_topology_svg(model).count("topo-warn")
    apply_to_topology(
        model, [DesignIssue(WARNING, "annotation-warning", "Setup warning",
                            "J1: check this", designators=("J1",))],
        md["directives"])
    svg = render_topology_svg(model)
    assert j1.has_warning and svg.count("topo-warn") == before + 1
    assert palette(None).warn in svg


# --- the panel --------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from fypa.viewer import issues_panel as IP  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp):
    p = IP.DesignIssuesPanel()
    p.set_issues(collect_design_issues(_metadata()))
    return p


def test_the_panel_is_there_only_for_an_error_or_a_warning(qapp):
    p = IP.DesignIssuesPanel()
    assert p.isHidden()
    p.set_issues([DesignIssue(NOTE, "annotation-note", "Setup note", "n")])
    assert p.isHidden()                 # a note alone is not a problem
    p.set_issues([DesignIssue(WARNING, "k", "L", "w")])
    assert not p.isHidden()
    p.set_expanded(True)
    p.set_issues([])                    # fixed: gone, and closed
    assert p.isHidden() and not p.is_expanded()


def test_it_starts_closed_showing_the_counts(panel):
    assert not panel.is_expanded()
    assert panel.table.isHidden()
    assert panel.maximumHeight() == IP.STRIP_HEIGHT_PX
    texts = {s: b.text() for s, b in panel._filter_btns.items()}
    assert texts == {ERROR: "2 Errors", WARNING: "3 Warnings",
                     NOTE: "1 Note"}


def test_a_click_on_the_strip_opens_and_closes_the_list(panel):
    seen = []
    panel.expanded_changed.connect(seen.append)
    panel._strip.clicked.emit()
    assert panel.is_expanded() and not panel.table.isHidden()
    assert panel.maximumHeight() > IP.STRIP_HEIGHT_PX
    panel._strip.clicked.emit()
    assert not panel.is_expanded() and seen == [True, False]


def test_the_counts_are_filters_and_notes_start_off(panel):
    assert [i.severity for i in panel.shown_issues()] == [
        ERROR, ERROR, WARNING, WARNING, WARNING]
    panel._filter_btns[NOTE].click()            # also opens the list
    assert panel.is_expanded()
    assert [i.severity for i in panel.shown_issues()][-1] == NOTE
    panel._filter_btns[WARNING].click()
    assert [i.severity for i in panel.shown_issues()] == [
        ERROR, ERROR, NOTE]
    assert panel.table.rowCount() == 3


def test_a_row_says_what_where_and_the_whole_message(panel):
    row = [panel.table.item(1, c).text() for c in range(1, 4)]
    assert row == ["Disconnected copper", "U5, U12", BREAK]


def test_double_click_asks_to_go_to_the_issue(panel):
    asked = []
    panel.jump_requested.connect(asked.append)
    panel.table.cellDoubleClicked.emit(2, 3)
    assert [i.kind for i in asked] == ["rail-not-solved"]


def test_copy_takes_the_selected_descriptions_or_whole_rows(panel, qapp):
    panel.table.selectRow(0)
    assert panel.copy_selected() == SKIPPED
    assert qapp.clipboard().text() == SKIPPED
    assert panel.copy_selected(whole_rows=True) == (
        f"Error\tDirective skipped\tU12\t{SKIPPED}")
    panel.table.clearSelection()
    assert panel.copy_selected() == ""          # nothing selected: untouched
    assert qapp.clipboard().text().startswith("Error\t")


# --- the window: the pop-up and going to an issue ---------------------------

class _Window(IP._DesignIssuesMixin, QMainWindow):
    _heatmap_tab_index = 0
    _topology_tab_index = 1

    def __init__(self, metadata):
        super().__init__()
        self.metadata = metadata
        self._build_issues_panel(self)
        self.tabs = SimpleNamespace(
            current=0, currentIndex=lambda: self.tabs.current,
            setCurrentIndex=lambda i: setattr(self.tabs, "current", i))
        self.jumps = []

    def _jump_to_node(self, row):
        self.jumps.append(("node", row["designator"], row["x_mm"]))

    def _jump_to_xy(self, x, y, layer_ids):
        self.jumps.append(("xy", x, y))


@pytest.fixture
def popups(monkeypatch):
    """Stands in for the pop-up; ``.answer`` is what the user chose."""
    seen = SimpleNamespace(calls=[], answer=False)

    def show(window, issues):
        seen.calls.append(summary_text(issues))
        return seen.answer

    monkeypatch.setattr(IP, "show_issue_summary", show)
    return seen


def test_a_load_with_problems_pops_up_once_and_solving_again_does_not(
        qapp, popups):
    win = _Window(_metadata())
    win._refresh_design_issues(announce=True, new_design=True)
    assert popups.calls == ["2 errors and 3 warnings"]
    win._refresh_design_issues(announce=True)       # the same problems
    assert len(popups.calls) == 1
    assert not win._issues_panel.is_expanded()      # they chose Close


def test_a_new_problem_after_a_solve_pops_up_again(qapp, popups):
    win = _Window(_metadata(annotation_errors=[]))
    win._refresh_design_issues(announce=True, new_design=True)
    win.metadata = _metadata()                      # U12's directive skipped
    win._refresh_design_issues(announce=True)
    assert popups.calls == ["1 error and 3 warnings",
                            "2 errors and 3 warnings"]


def test_a_fixed_problem_that_comes_back_is_announced_again(qapp, popups):
    win = _Window(_metadata())
    win._refresh_design_issues(announce=True, new_design=True)
    win.metadata = {"directives": _directives()}    # all fixed
    win._refresh_design_issues(announce=True)
    assert win._issues_panel.isHidden()
    win.metadata = _metadata()
    win._refresh_design_issues(announce=True)
    assert len(popups.calls) == 2


def test_another_design_starts_from_nothing_announced(qapp, popups):
    win = _Window(_metadata())
    win._refresh_design_issues(announce=True, new_design=True)
    win._refresh_design_issues(announce=True, new_design=True)
    assert len(popups.calls) == 2


def test_notes_alone_and_quiet_refreshes_never_pop_up(qapp, popups):
    win = _Window({"annotation_warnings": [NOTE_MSG]})
    win._refresh_design_issues(announce=True, new_design=True)
    win.metadata = _metadata()
    win._refresh_design_issues()                    # e.g. a setting changed
    assert popups.calls == []
    assert not win._issues_panel.isHidden()


def test_show_issues_in_the_popup_opens_the_list(qapp, popups):
    popups.answer = True
    win = _Window(_metadata())
    win._refresh_design_issues(announce=True, new_design=True)
    assert win._issues_panel.is_expanded()


def test_the_popup_says_how_many_and_where_to_look():
    issues = collect_design_issues(_metadata())
    headline, detail = IP.issue_summary_text(issues)
    assert headline == "This design has 2 errors and 3 warnings."
    assert "Design issues panel along the bottom" in detail
    assert detail.startswith("An error means")
    _h, warn_only = IP.issue_summary_text(
        [DesignIssue(WARNING, "k", "L", "w")])
    assert warn_only.startswith("They are listed")


def test_no_popup_where_nobody_could_dismiss_it(qapp):
    """Offscreen (a script, CI): returns at once instead of blocking."""
    assert IP.show_issue_summary(None, collect_design_issues(_metadata())) \
        is False


def _issue(win, kind):
    return next(i for i in win._design_issues if i.kind == kind)


def test_going_to_an_issue_zooms_the_board_to_the_part(qapp, popups):
    win = _Window(_metadata())
    win._refresh_design_issues()
    win._on_issue_jump(_issue(win, "directive-skipped"))
    assert win.jumps == [("node", "U12", 1.0)]


def test_a_part_with_no_directive_is_found_by_its_footprint(qapp, popups):
    win = _Window(_metadata())
    win._refresh_design_issues()
    win._on_issue_jump(_issue(win, "unannotated-bridge"))
    assert win.jumps == [("xy", 11.0, 20.5)]


def test_a_rail_issue_goes_to_a_part_on_that_rail(qapp, popups):
    md = _metadata(open_loop_rails=["Rail '+3V3': has a SINK but no SOURCE"],
                   annotation_warnings=[])
    win = _Window(md)
    win._refresh_design_issues()
    win._on_issue_jump(_issue(win, "rail-not-solved"))
    assert win.jumps == [("node", "U1", 1.0)]


def test_on_the_topology_tab_it_pans_the_diagram_instead(qapp, popups):
    win = _Window(_metadata())
    win._refresh_design_issues()
    win.tabs.current = win._topology_tab_index
    gone = []
    win._topology_view = SimpleNamespace(go_to=gone.append)
    win._topology_model = SimpleNamespace(nodes=[SimpleNamespace(
        designator="U12", x=100.0, y=40.0, width=60.0, height=20.0)])
    win._on_issue_jump(_issue(win, "directive-skipped"))
    assert gone == [(130.0, 50.0)] and win.jumps == []
    # A part the diagram doesn't draw: fall back to the Board.
    win._on_issue_jump(_issue(win, "unannotated-bridge"))
    assert win.jumps == [("xy", 11.0, 20.5)]


def test_an_issue_with_nowhere_to_go_says_so(qapp, popups):
    win = _Window({"annotation_warnings": [
        "sheet symbol 'PWR' (top.SchDoc): PDN_X ignored - no such key"]})
    win._refresh_design_issues()
    win._on_issue_jump(win._design_issues[0])
    assert win.jumps == []
    assert "no part or rail" in win.statusBar().currentMessage()
