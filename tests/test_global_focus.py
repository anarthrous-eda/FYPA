"""Global Focus: one rail on every tab (fypa.viewer.global_focus)."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from fypa.altium_viewer import EyeButton, PdnViewer  # noqa: E402
from fypa.viewer.editor.nets import _EditorNetsMixin  # noqa: E402
from fypa.viewer.global_focus import (  # noqa: E402
    GlobalFocusChip,
    _GlobalFocusMixin,
    bridge_row_in_focus,
    directive_in_focus,
    directive_power_nets,
    focus_metadata,
    focus_scope_nets,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _term(*nets, ideal=False):
    return {"pins": [{"net": n} for n in nets], "ideal_return": ideal}


# --- scope ---------------------------------------------------------------------

def test_scope_is_the_rail_members_plus_merged_sections():
    members = {"+3V3": ["+3V3", "3V3_SW"], "GND": ["GND"]}
    merged = {"+3V3": ["HS_SNS"]}
    assert focus_scope_nets("+3V3", members, merged) == {
        "+3V3", "3V3_SW", "HS_SNS"}
    # No return net in this version.
    assert "GND" not in focus_scope_nets("+3V3", members, merged)


def test_a_sink_is_in_focus_by_its_p_side_only():
    sink = {"role": "SINK", "terminals": {"P": _term("+3V3"),
                                          "N": _term("GND")}}
    assert directive_power_nets(sink) == {"+3V3"}
    assert directive_in_focus(sink, {"+3V3"})
    assert not directive_in_focus(sink, {"GND"})


def test_a_regulator_belongs_to_both_rails_it_joins():
    reg = {"role": "REGULATOR", "terminals": {
        "OUT_P": _term("+1V8"), "OUT_N": _term("GND"),
        "IN_P": _term("+5V"), "IN_N": _term("GND")}}
    assert directive_power_nets(reg) == {"+1V8", "+5V"}
    assert directive_in_focus(reg, {"+1V8"})
    assert directive_in_focus(reg, {"+5V"})
    assert not directive_in_focus(reg, {"GND"})


def test_a_series_part_is_rail_copper_on_both_sides():
    series = {"role": "RESISTOR", "terminals": {"P": _term("+3V3"),
                                                "N": _term("3V3_SW")}}
    assert directive_power_nets(series) == {"+3V3", "3V3_SW"}


def test_ideal_return_and_requested_net():
    src = {"role": "SOURCE", "terminals": {
        "P": {"requested_net": "+3V3", "pins": []},
        "N": _term("GND", ideal=True)}}
    assert directive_power_nets(src) == {"+3V3"}


def test_focus_metadata_filters_what_the_report_tabs_draw():
    md = {
        "project_name": "x",
        "directives": [
            {"role": "SINK", "terminals": {"P": _term("+3V3")}},
            {"role": "SINK", "terminals": {"P": _term("+5V")}},
        ],
        "active_nets": ["+3V3", "+5V", "GND"],
        "merged_bridges": [{"p_net": "+3V3", "n_net": "HS"},
                           {"p_net": "+5V", "n_net": "X"}],
    }
    out = focus_metadata(md, {"+3V3"})
    assert len(out["directives"]) == 1
    assert out["active_nets"] == ["+3V3"]
    assert out["merged_bridges"] == [{"p_net": "+3V3", "n_net": "HS"}]
    assert out["project_name"] == "x"
    # The original is untouched, and no focus passes it straight through.
    assert len(md["directives"]) == 2
    assert focus_metadata(md, None) is md


def test_bridge_row_in_focus_matches_any_side():
    row = {"net_a": "+3V3", "net_b": "GND", "nets": ["+3V3", "GND", "G"]}
    assert bridge_row_in_focus(row, {"+3V3"})
    assert bridge_row_in_focus({"net_a": "A", "net_b": "B", "nets": ["G"]},
                               {"G"})
    assert not bridge_row_in_focus(row, {"+5V"})


# --- window stub ------------------------------------------------------------------

class _Toasts:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text, _ms=0):  # noqa: N802 - Qt name
        self.messages.append(text)


class _Viewer(_GlobalFocusMixin, _EditorNetsMixin):
    """Just enough window for the focus state machine. Rails here have no
    subnet rows, so their single eye is their visibility."""

    _visible_rails = PdnViewer._visible_rails

    def __init__(self, rails: dict[str, list[str]], visible: set[str]):
        self._rails = list(rails)
        self._rail_to_members = dict(rails)
        self._merged_children = {}
        self._rail_eye_buttons = [
            (r, EyeButton(visible=r in visible)) for r in rails]
        self._subnet_eye_buttons = {}
        self._editor_mode = False
        self._editor_focus_polys = set()
        self._editor_focus_nets = set()
        self._toasts = _Toasts()
        self.renders = 0
        self.titles: list[tuple[str, int]] = []

    def statusBar(self):  # noqa: N802 - Qt name
        return self._toasts

    def _render_with_busy_popup(self):
        self.renders += 1

    def _sync_rail_eye_from_subnets(self, _rail):
        pass

    def _sync_rail_tree_node_partials(self, _rail):
        pass

    def _sync_all_rails_eye(self):
        pass

    def _update_nodes_tab_title(self, n):
        self.titles.append(("nodes", n))

    def _update_vias_tab_title(self, n):
        self.titles.append(("vias", n))

    def _update_caps_tab_title(self, n=None):
        self.titles.append(("caps", n))

    def eyes(self) -> dict[str, bool]:
        return {r: e.isVisibleState() for r, e in self._rail_eye_buttons}


RAILS = {"+3V3": ["+3V3", "3V3_SW"], "+5V": ["+5V"], "GND": ["GND"]}


def test_enter_shows_only_the_rail_and_exit_restores_the_eyes(qapp):
    v = _Viewer(RAILS, visible={"+3V3", "+5V"})
    before = v.eyes()
    v._enter_global_focus("+5V")
    assert v._global_focus_rail() == "+5V"
    assert v._visible_rails() == ["+5V"]
    assert v.eyes() == {"+3V3": False, "+5V": True, "GND": False}
    assert v.renders == 1
    assert "+5V" in v._toasts.messages[-1]

    # Switching keeps the eyes saved on entry, not the focused ones.
    v._enter_global_focus("+3V3")
    assert v._visible_rails() == ["+3V3"]
    assert v._global_focus_nets() == {"+3V3", "3V3_SW"}

    v._exit_global_focus()
    assert not v._global_focus_active()
    assert v._global_focus_nets() is None
    assert v.eyes() == before
    assert v._visible_rails() == ["+3V3", "+5V"]


def test_another_rail_switched_on_still_does_not_draw(qapp):
    """The Fixes tab's Go turns rail eyes on; focus must still win."""
    v = _Viewer(RAILS, visible={"+3V3"})
    v._enter_global_focus("+3V3")
    dict(v._rail_eye_buttons)["+5V"].setVisibleState(True, emit=False)
    assert v._visible_rails() == ["+3V3"]


def test_unknown_rail_is_ignored(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    v._enter_global_focus("NOPE")
    assert not v._global_focus_active()


def test_focus_exits_when_its_rail_is_gone_after_a_rebuild(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    v._enter_global_focus("+5V")
    v._rails = ["+3V3", "GND"]
    v._rail_to_members = {"+3V3": ["+3V3"], "GND": ["GND"]}
    v._sync_global_focus()
    assert not v._global_focus_active()
    assert "+5V is no longer a solved rail" in v._toasts.messages[-1]


def test_focus_follows_a_rail_whose_members_changed(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    v._enter_global_focus("+3V3")
    v._rail_to_members = {**RAILS, "+3V3": ["+3V3", "3V3_SW", "NEW"]}
    v._sync_global_focus()
    assert v._global_focus_nets() == {"+3V3", "3V3_SW", "NEW"}


def test_exit_when_not_focused_is_silent(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    v._exit_global_focus()
    assert v._toasts.messages == []


# --- copper hiding reuses the editor net focus ---------------------------------

def test_editor_focus_nets_follow_global_focus(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    assert not v._editor_focus_active()
    v._enter_global_focus("+3V3")
    # Viewer mode: the rail's nets, and focus is active (copper, vias and
    # markers outside the rail are hidden).
    assert v._editor_focus_active()
    assert v._editor_focus_nets == {"+3V3", "3V3_SW"}
    # Editor mode: a user focus inside the rail narrows it...
    v._editor_mode = True
    v._editor_focus_nets = {"3V3_SW"}
    assert v._editor_focus_nets == {"3V3_SW"}
    # ...one outside it can't empty the set and show everything.
    v._editor_focus_nets = {"+5V"}
    assert v._editor_focus_nets == {"+3V3", "3V3_SW"}
    v._editor_focus_nets = set()
    v._exit_global_focus()
    assert v._editor_focus_nets == set()
    assert not v._editor_focus_active()


def test_user_net_focus_without_global_focus_is_unchanged(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    v._editor_mode = True
    v._editor_focus_nets = {"GND"}
    assert v._editor_focus_nets == {"GND"}
    assert v._editor_focus_active()
    v._editor_mode = False
    assert not v._editor_focus_active()


# --- per-tab helpers -----------------------------------------------------------

def test_warn_counts_are_for_the_focused_rail(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    v._via_current_warn_a = 1.0
    v._nodes_rows_cache = [
        {"net": "+3V3", "status": "FAIL"}, {"net": "+5V", "status": "FAIL"},
        {"net": "3V3_SW", "status": "PASS"}]
    v._vias_rows_cache = [
        {"net": "3V3_SW", "current": -2.0}, {"net": "+5V", "current": 3.0},
        {"net": "+3V3", "current": 0.1}]
    assert v._global_focus_warn_count("nodes", 2) == 2  # focus off
    v._enter_global_focus("+3V3")
    assert v._global_focus_warn_count("nodes", 2) == 1
    assert v._global_focus_warn_count("vias", 2) == 1
    # Rows not built yet: the board count stands rather than a false 0.
    assert v._global_focus_warn_count("caps", 5) == 5


def test_tab_pickers_stand_for_the_focused_rail(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    combo = QComboBox()
    combo.addItems(["All rails", "+3V3", "+5V"])
    assert v._global_focus_rail_choice(combo) == "All rails"
    v._enter_global_focus("+5V")
    assert v._global_focus_rail_choice(combo) == "+5V"
    assert v._global_focus_allows_rail("+5V")
    assert not v._global_focus_allows_rail("+3V3")
    assert v._global_focus_allows_net("+5V")
    assert not v._global_focus_allows_net("GND")


def test_rail_pickers_are_refilled_with_the_current_rails(qapp):
    """The Nodes / Vias / Capacitors pickers used to keep the rails of the
    solve they were built for."""
    v = _Viewer(RAILS, visible={"+3V3"})
    v.nodes_rail_combo = QComboBox()
    v.nodes_rail_combo.addItems(["All rails", "OLD", "+5V"])
    v.nodes_rail_combo.setCurrentText("+5V")
    v._sync_global_focus()
    items = [v.nodes_rail_combo.itemText(i)
             for i in range(v.nodes_rail_combo.count())]
    assert items == ["All rails", "+3V3", "+5V", "GND"]
    assert v.nodes_rail_combo.currentText() == "+5V"


def test_pickers_hide_while_focused(qapp):
    v = _Viewer(RAILS, visible={"+3V3"})
    v.nodes_rail_combo = QComboBox()
    v.nodes_rail_label = QComboBox()
    v._enter_global_focus("+3V3")
    assert v.nodes_rail_combo.isHidden() and v.nodes_rail_label.isHidden()
    v._exit_global_focus()
    assert not v.nodes_rail_combo.isHidden()


# --- the control ---------------------------------------------------------------

def test_chip_states_and_signals(qapp):
    chip = GlobalFocusChip()
    assert chip.rail_text() == "Off"
    assert chip.close_button().isHidden()
    chip.set_state("+3V3", enabled=True)
    assert chip.rail_text() == "+3V3"
    assert not chip.close_button().isHidden()
    assert "+3V3" in chip.toolTip()
    exits = []
    chip.exit_requested.connect(lambda: exits.append(1))
    chip.close_button().click()
    assert exits == [1]
    chip.set_state(None, enabled=False, disabled_tip="solve first")
    assert not chip.isEnabled()
    assert chip.toolTip() == "solve first"


# --- editor form pickers -------------------------------------------------------

def test_editor_net_pickers_list_only_the_focused_rail(qapp):
    from fypa.viewer.editor.form import _EditorFormMixin

    class _Form(_Viewer):
        _focus_restrict_net_pickers = _EditorFormMixin._focus_restrict_net_pickers

    v = _Form(RAILS, visible={"+3V3"})
    v._enter_global_focus("+3V3")
    # A sink on the rail: its P picker keeps only the rail's nets.
    v._ef_pnet = QComboBox()
    v._ef_pnet.addItems(["GND", "+3V3", "+5V", "3V3_SW"])
    v._ef_pnet.setCurrentText("+3V3")
    # A regulator feeding the rail: its IN net is on another rail, so that
    # entry stays, alone and read-only.
    v._ef_innet = QComboBox()
    v._ef_innet.addItems(["+5V", "+3V3"])
    v._ef_innet.setCurrentText("+5V")
    v._ef_nnet = QComboBox()
    v._ef_nnet.addItems(["GND", "+3V3"])
    v._focus_restrict_net_pickers()

    def items(c):
        return [c.itemText(i) for i in range(c.count())]

    assert items(v._ef_pnet) == ["+3V3", "3V3_SW"]
    assert v._ef_pnet.currentText() == "+3V3" and v._ef_pnet.isEnabled()
    assert items(v._ef_innet) == ["+5V"] and not v._ef_innet.isEnabled()
    assert "+3V3" in v._ef_innet.toolTip()
    # The return-net picker stays as it was.
    assert items(v._ef_nnet) == ["GND", "+3V3"]
