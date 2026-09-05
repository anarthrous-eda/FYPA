"""The Bridges tab — the single place net bridging is seen and controlled.

Bridging used to be spread across four mechanisms with no one view of the
result (Altium ``PDN_ROLE=SERIES``, the Net Tie / 0 ohm auto-bridge, the
low-ohm net merge that absorbs those, and editor-mode SERIES), which is how a
board could end up silently shorted in several places with nothing on screen
saying so. These tests pin the behaviours that make the tab trustworthy:
filters select the right rows, edits go into the project's editor directives
rather than a parallel store, an auto-bridge can actually be switched off, and
the two footguns (merging rails, shorting two supplies) are surfaced.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from fypa.project_file import ProjectFile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _candidates() -> list[dict]:
    return [
        {"designator": "FB1", "kind": "ferrite bead", "value": "600R",
         "footprint": "0603", "pin_count": 2,
         "net_a": "GND", "net_b": "AGND", "state": "unmodelled",
         "resistance_ohm": None, "why": "",
         "impact": "joins the solved rail GND to AGND",
         "touches_active_rail": True,
         "x_mm": 5.0, "y_mm": 2.0, "layer_id": 1},
        {"designator": "R9", "kind": "resistor", "value": "0R",
         "footprint": "R0603", "pin_count": 2,
         "net_a": "VIN", "net_b": "VIN_F", "state": "auto",
         "resistance_ohm": 0.0005, "why": "value 0R reads as a link",
         "impact": "", "touches_active_rail": True,
         "x_mm": 1.0, "y_mm": 1.0, "layer_id": 1},
        {"designator": "F3", "kind": "fuse", "value": "2A",
         "footprint": "1206", "pin_count": 2,
         "net_a": "+5V", "net_b": "+5V_SW", "state": "series",
         "resistance_ohm": 0.03, "why": "annotated PDN_ROLE=SERIES",
         "impact": "", "touches_active_rail": True,
         "x_mm": 9.0, "y_mm": 4.0, "layer_id": 1},
    ]


@pytest.fixture
def viewer(qapp, monkeypatch):
    """A PdnViewer with only the Bridges tab wired up.

    ``PdnViewer.__init__`` opens a whole board, so run just the Qt base
    initialiser and stub the few collaborators the tab touches. Modal
    dialogs are recorded rather than shown — under the offscreen platform
    they would block forever.
    """
    from PySide6.QtWidgets import QMainWindow, QMessageBox
    import fypa.altium_viewer as V

    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(
        lambda *a, **k: (dialogs.append(
            ("warning", a[1] if len(a) > 1 else "")), QMessageBox.Cancel)[1]))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(
        lambda *a, **k: dialogs.append(("info", a[1] if len(a) > 1 else ""))))

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {"bridge_candidates": _candidates()}
    v._project = ProjectFile()
    v._project_path = None
    v.tabs = None
    v._highlight_via_xy = None
    v._ensure_project = lambda: v._project
    v._mark_project_dirty = lambda: None
    v._update_pending_rails = lambda: None
    v._render = lambda: None
    # Hold the tab widget: in the app QTabWidget.addTab takes ownership, but
    # here it is parentless (``tabs`` is None), so dropping the reference
    # would have Python garbage-collect it and its table out from under us.
    v._test_tab_widget = V.PdnViewer._build_bridges_tab(v)
    V.PdnViewer._populate_bridges_table(v)
    v._test_dialogs = dialogs
    return v


def _visible(v) -> list[str]:
    t = v.bridges_table
    return [t.item(i, 0).text() for i in range(t.rowCount())
            if not t.isRowHidden(i)]


def _series_directives(v) -> list:
    return [d for d in v._project.editor_directives if d.role == "SERIES"]


# --- table + filters -------------------------------------------------------

def test_every_candidate_gets_a_row(viewer):
    assert viewer.bridges_table.rowCount() == 3


def test_summary_counts_the_rows_that_change_an_answer(viewer):
    assert "1 affecting a solved rail" in viewer.bridges_summary_label.text()


@pytest.mark.parametrize("mode,expected", [
    ("All parts", ["FB1", "R9", "F3"]),
    ("Modelled as SERIES", ["F3"]),
    ("Auto-bridged", ["R9"]),
    ("Not modelled", ["FB1"]),
    ("Affects a solved rail", ["FB1"]),
])
def test_filters_select_the_right_rows(viewer, mode, expected):
    viewer.bridges_state_combo.setCurrentText(mode)
    assert _visible(viewer) == expected


# --- editing ---------------------------------------------------------------

def test_setting_a_resistance_writes_an_editor_directive(viewer):
    """Edits must land in the project's own editor directives — the same
    store and undo path as Edit mode — not a parallel one owned by the tab."""
    import fypa.altium_viewer as V
    V.PdnViewer._apply_bridge_series(viewer, _candidates()[0], 0.05)
    eds = _series_directives(viewer)
    assert len(eds) == 1
    assert eds[0].designator == "FB1"
    assert eds[0].resistance == pytest.approx(0.05)
    assert (eds[0].p_net, eds[0].n_net) == ("GND", "AGND")
    assert eds[0].overrides_designator == "FB1"


def test_editing_twice_replaces_rather_than_accumulates(viewer):
    import fypa.altium_viewer as V
    rec = _candidates()[0]
    V.PdnViewer._apply_bridge_series(viewer, rec, 0.05)
    V.PdnViewer._apply_bridge_series(viewer, rec, 0.08)
    eds = _series_directives(viewer)
    assert len(eds) == 1 and eds[0].resistance == pytest.approx(0.08)


def test_modelling_a_part_clears_its_impact_warning(viewer):
    import fypa.altium_viewer as V
    V.PdnViewer._apply_bridge_series(viewer, _candidates()[0], 0.05)
    row = next(r for r in V.PdnViewer._bridge_rows(viewer)
               if r["designator"] == "FB1")
    assert row["state"] == "series"
    assert not row["impact"]


def test_remove_drops_the_directive(viewer):
    import fypa.altium_viewer as V
    rec = _candidates()[0]
    V.PdnViewer._apply_bridge_series(viewer, rec, 0.05)
    V.PdnViewer._apply_bridge_series(viewer, rec, None)
    assert _series_directives(viewer) == []


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_non_positive_resistance_is_refused_and_explained(viewer, bad):
    """Zero would short the pads through an ideal wire and divide by zero."""
    import fypa.altium_viewer as V
    viewer._test_dialogs.clear()
    V.PdnViewer._apply_bridge_series(viewer, _candidates()[0], bad)
    assert _series_directives(viewer) == []
    assert any("positive" in str(d[1]).lower() for d in viewer._test_dialogs)


# --- auto-bridge opt-out ---------------------------------------------------

def test_disable_records_the_opt_out(viewer):
    import fypa.altium_viewer as V
    viewer.bridges_table.selectRow(
        next(i for i in range(viewer.bridges_table.rowCount())
             if viewer.bridges_table.item(i, 0).text() == "R9"))
    V.PdnViewer._on_bridge_disable(viewer)
    assert viewer._project.no_auto_bridge == ["R9"]
    row = next(r for r in V.PdnViewer._bridge_rows(viewer)
               if r["designator"] == "R9")
    assert row["state"] == "off"


def test_remove_also_lifts_an_opt_out(viewer):
    """One button undoes either kind of edit, so the user never has to know
    which mechanism produced the current state."""
    import fypa.altium_viewer as V
    viewer._project.no_auto_bridge = ["R9"]
    V.PdnViewer._apply_bridge_series(viewer, _candidates()[1], None)
    assert viewer._project.no_auto_bridge == []


def test_modelling_a_part_also_lifts_its_opt_out(viewer):
    """Otherwise a part could be both disabled and modelled at once."""
    import fypa.altium_viewer as V
    viewer._project.no_auto_bridge = ["R9"]
    V.PdnViewer._apply_bridge_series(viewer, _candidates()[1], 0.02)
    assert viewer._project.no_auto_bridge == []


# --- footguns --------------------------------------------------------------

def test_merge_threshold_is_warned_before_it_bites(viewer):
    """Below the threshold the two nets stop existing separately and a rail
    name disappears — startling if you just typed a small number."""
    viewer.bridges_r_edit.setText("0.0002")
    assert "merge" in viewer.bridges_hint_label.text()


def test_above_the_threshold_says_the_nets_stay_separate(viewer):
    viewer.bridges_r_edit.setText("0.05")
    assert "stay separate" in viewer.bridges_hint_label.text()


def test_ground_pairs_short_without_a_prompt(viewer):
    """AGND to GND through a ferrite is standard layout, not a mistake."""
    import fypa.altium_viewer as V
    assert V.PdnViewer._warn_if_shorting_power_rails(
        viewer, _candidates()[0]) is True


def test_same_supply_shorts_without_a_prompt(viewer):
    """+5V and +5V_SW are one supply seen either side of a switch."""
    import fypa.altium_viewer as V
    assert V.PdnViewer._warn_if_shorting_power_rails(
        viewer, _candidates()[2]) is True


def test_two_different_supplies_prompt_and_can_be_declined(viewer):
    import fypa.altium_viewer as V
    rec = dict(_candidates()[0], net_a="+5V", net_b="+3V3")
    viewer._test_dialogs.clear()
    assert V.PdnViewer._warn_if_shorting_power_rails(viewer, rec) is False
    assert any("supplies" in str(d[1]).lower() for d in viewer._test_dialogs)
