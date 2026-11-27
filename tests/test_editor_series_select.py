"""Series elements are selectable in editor mode and explain themselves.

Clicking a SERIES or BRIDGE square selects its part, and the
component form opens with a series block: where the model came from, the
solved current / drop / loss, and the actions that change it.
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


def _pin(net, x, pad="1"):
    return {"pad": pad, "layer_id": 1, "net": net, "x_mm": x, "y_mm": 0.0}


def _resistor(des, p, n, r, schdoc="Power.SchDoc"):
    return {"role": "RESISTOR", "designator": des, "label": des,
            "schdoc": schdoc, "value": r,
            "terminals": {"P": {"pins": [_pin(p, 0.0)]},
                          "N": {"pins": [_pin(n, 1.0, "2")]}}}


def _row(des, state, a, b, r, why="", footprint="R0402"):
    return {"designator": des, "state": state, "net_a": a, "net_b": b,
            "resistance_ohm": r, "why": why, "footprint": footprint,
            "fet": False}


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V
    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._project = ProjectFile()
    v.solution = None
    v._solve_stale = False
    v.metadata = {
        "directives": [
            _resistor("R1", "VIN", "VIN_F", 0.01),
            _resistor("R2", "+3V3", "+3V3_A", 0.047),
            {"role": "AUTO_BRIDGE", "designator": "R3", "schdoc":
             "(auto-bridge)", "value": 0.0, "shorted_nets": "GND ↔ AGND",
             "terminals": {"P": {"pins": [_pin("GND", 5.0)]},
                           "N": {"pins": [_pin("GND", 6.0, "2")]}}},
            {"role": "SOURCE", "designator": "J1", "value": 3.3,
             "terminals": {"P": {"pins": [_pin("+3V3", 9.0)]}}},
        ],
        "bridge_candidates": [
            _row("R2", "auto", "+3V3", "+3V3_A", 0.047,
                 why="modelled as SERIES: value '47m' is at or below the "
                     "100 mΩ auto-SERIES limit"),
            _row("R3", "auto", "GND", "AGND", 0.0,
                 why="Altium ComponentKind marks it a Net Tie"),
        ],
        "components": [
            {"designator": d, "side": "top", "bbox": [0, 0, 1, 1],
             "nets": []} for d in ("R1", "R2", "R3", "R4")],
    }
    v._mm_per_pixel = 0.01
    return v


# --- Click target ------------------------------------------------------------

def test_a_click_on_a_series_square_names_its_part(viewer):
    viewer._solved_series_click_rows = [
        {"x_mm": 0.0, "y_mm": 0.0, "designator": "R1", "size_px": 12,
         "legend_key": "SERIES"},
        {"x_mm": 5.0, "y_mm": 0.0, "designator": "R3", "size_px": 12,
         "legend_key": "BRIDGE"},
    ]
    viewer._editor_series_click_rows = []
    assert viewer._pick_series_marker(0.02, 0.0) == "R1"
    assert viewer._pick_series_marker(5.0, 0.03) == "R3"
    assert viewer._pick_series_marker(2.5, 0.0) is None


def test_editor_series_squares_are_click_targets(viewer):
    viewer._solved_series_click_rows = []
    viewer._editor_series_click_rows = [
        {"x_mm": 3.0, "y_mm": 3.0, "designator": "R4", "size_px": 16}]
    assert viewer._pick_series_marker(3.0, 3.05) == "R4"


# --- Where the model came from ----------------------------------------------

def test_schematic_series(viewer):
    info = viewer._series_info("R1")
    assert info["source"] == "schematic"
    assert (info["p_net"], info["n_net"]) == ("VIN", "VIN_F")
    assert info["resistance"] == pytest.approx(0.01)


def test_auto_series_keeps_its_reason(viewer):
    info = viewer._series_info("R2")
    assert info["source"] == "auto"
    assert "auto-SERIES limit" in info["why"]


def test_merged_bridge_reports_the_original_nets(viewer):
    info = viewer._series_info("R3")
    assert info["source"] == "merged" and info["merged"]
    assert (info["p_net"], info["n_net"]) == ("GND", "AGND")


def test_editor_series_is_pending_until_solved(viewer):
    viewer._project.upsert_directive(EditorDirective(
        kind="component", role="SERIES", designator="R4", single_net=False,
        p_net="A", n_net="B", resistance=0.02))
    info = viewer._series_info("R4")
    assert info["source"] == "editor" and info["pending"]


def test_disabled_auto_series_reads_as_off(viewer):
    viewer._project.no_auto_bridge = ["R2"]
    assert viewer._series_info("R2")["source"] == "off"


def test_a_plain_part_has_no_series_block(viewer):
    assert viewer._series_info("C7") is None


def test_auto_parts_are_not_listed_as_schematic_directives(viewer):
    sel = {"designator": "R2", "bbox": None}
    assert viewer._schematic_directives_for(sel) == []
    sel = {"designator": "R3", "bbox": None}
    assert viewer._schematic_directives_for(sel) == []
    sel = {"designator": "R1", "bbox": None}
    assert [d["designator"] for d in viewer._schematic_directives_for(sel)] \
        == ["R1"]


# --- Solved values -----------------------------------------------------------

class _Sampler:
    def __init__(self, volts):
        self.volts = volts

    def sample_pin(self, pin):
        return self.volts.get(pin["net"])


def test_current_drop_and_loss_come_from_the_pin_voltages(viewer):
    viewer._series_sampler = lambda: _Sampler({"+3V3": 3.30, "+3V3_A": 3.253})
    info = viewer._series_info("R2")
    assert info["solved"]
    assert info["delta_v"] == pytest.approx(0.047)
    assert info["current"] == pytest.approx(1.0)
    assert info["loss"] == pytest.approx(0.047)
    assert info["rail"] and info["setpoint"] == pytest.approx(3.3)


def test_off_mesh_pins_degrade_to_a_note(viewer):
    viewer._series_sampler = lambda: _Sampler({"+3V3": 3.3})
    info = viewer._series_info("R2")
    assert not info["solved"] and info["off_mesh"]
    lines = " ".join(viewer._series_result_lines(info))
    assert "off the solved copper" in lines


def test_loss_is_checked_against_the_chip_size(viewer):
    viewer._series_sampler = lambda: _Sampler({"+3V3": 3.30, "+3V3_A": 3.253})
    lines = " ".join(viewer._series_result_lines(viewer._series_info("R2")))
    # 47 mW in an 0402 (typ. 63 mW) is within rating.
    assert "within the typical 63 mW for a 0402" in lines
    viewer._series_sampler = lambda: _Sampler({"+3V3": 3.30, "+3V3_A": 3.206})
    lines = " ".join(viewer._series_result_lines(viewer._series_info("R2")))
    assert "over the typical" in lines


def test_a_merged_bridge_says_no_current_is_solved(viewer):
    lines = " ".join(viewer._series_result_lines(viewer._series_info("R3")))
    assert "no current or drop is solved" in lines


def test_stale_results_are_marked_out_of_date(viewer):
    viewer._series_sampler = lambda: _Sampler({"+3V3": 3.30, "+3V3_A": 3.253})
    viewer._solve_stale = True
    lines = viewer._series_result_lines(viewer._series_info("R2"))
    assert lines[0].startswith("<i>")
    assert "Out of date" in lines[-1]


# --- The form itself ---------------------------------------------------------

def _form_text(v, des):
    from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget
    host = QWidget()
    v._editor_form_host = host
    v._editor_form_layout = QVBoxLayout(host)
    v._multi_form_layout = QVBoxLayout()
    v._editor_multi = []
    v._update_editor_panel = lambda: None
    v._all_net_names_cache = ["A", "B"]
    v._editor_selection = {"kind": "component", "designator": des,
                           "nets": [], "bbox": None, "unlocked": False}
    v._populate_editor_form()
    labels = " ".join(w.text() for w in host.findChildren(QLabel))
    buttons = [b.text() for b in host.findChildren(QPushButton)]
    return labels, buttons


def test_auto_series_form_shows_the_block_and_its_actions(viewer):
    labels, buttons = _form_text(viewer, "R2")
    assert "Auto-SERIES (set by FYPA)" in labels
    assert "Disable auto-SERIES" in buttons
    assert "Make it an editor SERIES" in buttons
    assert "Show in Bridges tab" in buttons
    assert "Focus both nets" in buttons
    # FYPA's own model: no blank SOURCE / SINK form under it.
    assert "Apply" not in buttons


def test_schematic_series_form_keeps_the_unlock_path(viewer):
    labels, buttons = _form_text(viewer, "R1")
    assert "Schematic (PDN_ROLE=SERIES)" in labels
    assert "Defined in the schematic" in labels
    assert any("Unlock" in b for b in buttons)


def test_a_plain_part_gets_the_ordinary_form(viewer):
    labels, buttons = _form_text(viewer, "R4")
    assert "SERIES</b>" not in labels
    assert "Apply" in buttons
