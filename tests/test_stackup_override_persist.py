"""Stackup copper-thickness overrides survive Save / reopen.

They used to live only in the Settings-tab fields. The solve pickle kept the
overridden thickness as if it were the design's, so a reopened project showed
the override, treated it as the baseline, and the next Resolve silently solved
with the design thickness again. Now the ``.fypa`` stores them, each solve
records the design thickness next to the solved one, and the Settings tab
keeps the two apart.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from fypa.project_file import ProjectFile
from fypa.viewer.solve_worker import record_stackup_overrides

L6_DESIGN = 0.0350012
L6_OVER = 0.350012


# --- .fypa round trip -------------------------------------------------------

def test_project_file_round_trips_stackup_overrides(tmp_path):
    proj = ProjectFile(stackup_overrides={
        6: {"thickness_mm": L6_OVER, "extracted_thickness_mm": L6_DESIGN},
        3: {"thickness_mm": 0.07, "extracted_thickness_mm": None},
    })
    path = tmp_path / "board.fypa"
    proj.save(path)
    back = ProjectFile.load(path)
    assert back.stackup_overrides == {
        3: {"thickness_mm": 0.07, "extracted_thickness_mm": None},
        6: {"thickness_mm": L6_OVER, "extracted_thickness_mm": L6_DESIGN},
    }


def test_older_project_without_key_loads_empty(tmp_path):
    path = tmp_path / "old.fypa"
    path.write_text('{"schema_version": 2}', encoding="utf-8")
    assert ProjectFile.load(path).stackup_overrides == {}


def test_malformed_entries_are_dropped_not_fatal(tmp_path):
    path = tmp_path / "hand.fypa"
    path.write_text(
        '{"schema_version": 2, "stackup_overrides": {'
        '"6": {"thickness_mm": 0.35, "extracted_thickness_mm": "x"},'
        '"x": {"thickness_mm": 0.1},'
        '"4": {"thickness_mm": -1},'
        '"5": {"nope": 1}}}',
        encoding="utf-8")
    assert ProjectFile.load(path).stackup_overrides == {
        6: {"thickness_mm": 0.35, "extracted_thickness_mm": None},
    }


# --- solve metadata ---------------------------------------------------------

def test_solve_records_design_thickness_beside_solved_one():
    pristine = SimpleNamespace(extracted=SimpleNamespace(stackup=(
        SimpleNamespace(layer_id=1, copper_thickness_mm=0.035),
        SimpleNamespace(layer_id=6, copper_thickness_mm=L6_DESIGN),
    )))
    md = {"stackup": [
        {"layer_id": 1, "copper_thickness_mm": 0.035},
        {"layer_id": 6, "copper_thickness_mm": L6_OVER},
    ]}
    record_stackup_overrides(md, pristine, {6: L6_OVER})
    assert md["stackup_overrides"] == {6: L6_OVER}
    assert [r["extracted_copper_thickness_mm"] for r in md["stackup"]] == [
        0.035, L6_DESIGN]


# --- Settings tab -----------------------------------------------------------

pyside = pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _rows(l6_solved: float, *, recorded: bool) -> list[dict]:
    rows = [{"layer_id": 1, "name": "Top", "copper_thickness_mm": 0.035},
            {"layer_id": 6, "name": "L6_VDD",
             "copper_thickness_mm": l6_solved}]
    if recorded:
        rows[0]["extracted_copper_thickness_mm"] = 0.035
        rows[1]["extracted_copper_thickness_mm"] = L6_DESIGN
    return rows


def _viewer(qapp, metadata: dict, project: ProjectFile | None):
    """A PdnViewer with only the Stackup box built (no board)."""
    from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = metadata
    v._project = project
    v._solve_settings = SimpleNamespace(
        layer_copper_resistivity_20c_microohm_cm={},
        copper_resistivity_20c_microohm_cm=1.68)
    v._via_current_warn_a = 1.0
    v._display_percentile_high = 99.0
    v._settings_dirty = False
    v._settings_launcher_mode = False
    v._refresh_solve_stale_overlay = lambda: None
    host = QWidget()
    QVBoxLayout(host).addWidget(v._build_stackup_settings_box())
    v._test_host = host          # keep the parentless widget tree alive
    return v


def _l6_text(v) -> float:
    return float(v._stackup_thickness_edits[6][0].text())


def test_solved_override_is_an_override_but_not_dirty(qapp):
    v = _viewer(qapp, {"stackup": _rows(L6_OVER, recorded=True),
                       "stackup_overrides": {6: L6_OVER}}, ProjectFile())
    assert _l6_text(v) == pytest.approx(L6_OVER * 1000)
    # Next Resolve keeps sending it ...
    assert v._gather_stackup_overrides() == {6: pytest.approx(L6_OVER)}
    # ... but it is already solved in, so nothing is pending.
    assert v._update_settings_field_styles() is False


def test_reopened_project_restores_override_over_old_pickle(qapp):
    # Solve pickle from before the design thickness was recorded, and
    # solved WITHOUT the override: the project's saved entry wins, and the
    # field is dirty because the solution doesn't reflect it yet.
    proj = ProjectFile(stackup_overrides={
        6: {"thickness_mm": L6_OVER, "extracted_thickness_mm": L6_DESIGN}})
    v = _viewer(qapp, {"stackup": _rows(L6_DESIGN, recorded=False)}, proj)
    assert _l6_text(v) == pytest.approx(L6_OVER * 1000)
    assert v._gather_stackup_overrides() == {6: pytest.approx(L6_OVER)}
    assert v._update_settings_field_styles() is True


def test_old_pickle_with_override_uses_project_design_thickness(qapp):
    # The exact case that bit: pickle solved at the override, no recorded
    # design value. The project's note of the design thickness keeps the
    # override an override.
    proj = ProjectFile(stackup_overrides={
        6: {"thickness_mm": L6_OVER, "extracted_thickness_mm": L6_DESIGN}})
    v = _viewer(qapp, {"stackup": _rows(L6_OVER, recorded=False)}, proj)
    assert v._gather_stackup_overrides() == {6: pytest.approx(L6_OVER)}
    assert v._update_settings_field_styles() is False


def test_save_writes_the_fields_with_design_thickness(qapp):
    v = _viewer(qapp, {"stackup": _rows(L6_DESIGN, recorded=True),
                       "stackup_overrides": {}}, ProjectFile())
    v._stackup_thickness_edits[6][0].setText("350.012")
    assert v._project_stackup_overrides_for_save() == {
        6: {"thickness_mm": pytest.approx(L6_OVER),
            "extracted_thickness_mm": L6_DESIGN}}
    v._stackup_thickness_edits[6][0].setText("abc")
    assert v._project_stackup_overrides_for_save() is None   # keep old


def test_solve_result_syncs_project_and_rebuilds_box(qapp):
    proj = ProjectFile(stackup_overrides={
        6: {"thickness_mm": L6_OVER, "extracted_thickness_mm": L6_DESIGN}})
    v = _viewer(qapp, {"stackup": _rows(L6_OVER, recorded=True),
                       "stackup_overrides": {6: L6_OVER}}, proj)
    old_edit = v._stackup_thickness_edits[6][0]
    # User clears the override and re-solves: the new solution has none.
    v.metadata = {"stackup": _rows(L6_DESIGN, recorded=True),
                  "stackup_overrides": {}}
    v._sync_project_stackup_overrides()
    v._rebuild_stackup_settings_box()
    assert proj.stackup_overrides == {}
    assert v._stackup_thickness_edits[6][0] is not old_edit
    assert _l6_text(v) == pytest.approx(L6_DESIGN * 1000)
    assert v._gather_stackup_overrides() == {}
