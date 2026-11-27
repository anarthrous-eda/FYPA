"""Gerber files & roles page: Add Files appends files from other folders
and skips ones already listed, Remove takes rows out, and the mapping
rules (validate_mapping) report problems and notes without any dialog."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


def test_add_files_appends_from_another_folder_and_skips_duplicates(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from fypa.gerber import import_ui
    from fypa.gerber.extract import LAYER_ID_BOTTOM, LAYER_ID_TOP

    top = _touch(tmp_path / "gerbers" / "board.GTL")
    bot = _touch(tmp_path / "gerbers" / "board.GBL")
    drl = _touch(tmp_path / "nc_drill" / "board.DRL")

    page = import_ui.GerberFilesPage([top, bot])
    assert page._table.rowCount() == 2
    changes = []
    page.changed.connect(lambda: changes.append(1))

    # Pick the drill file from another folder, plus a file already listed.
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(drl), str(top)], "")),
    )
    page._on_add_files()

    assert page._table.rowCount() == 3
    assert page._table.item(2, 0).text() == "board.DRL"
    # Told inline that one file was already listed (no message box).
    assert "1 of the picked file(s)" in page.notice.text()
    assert changes

    chk = page.check()
    assert chk.ok
    assert chk.copper == {LAYER_ID_TOP: top, LAYER_ID_BOTTOM: bot}
    assert chk.drills == [drl]


def test_remove_selected_rows(qapp, tmp_path):
    from fypa.gerber import import_ui

    files = [_touch(tmp_path / n) for n in ("a.GTL", "a.GBL", "a.DRL")]
    page = import_ui.GerberFilesPage(files)
    assert not page.remove_btn.isEnabled()
    # Select through the model: selectRow() honours whatever keyboard
    # modifiers an earlier test left set and may toggle instead.
    from PySide6.QtCore import QItemSelectionModel
    page._table.selectionModel().select(
        page._table.model().index(1, 0),
        QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    assert page.remove_btn.isEnabled()
    assert page.remove_selected() == 1
    assert [p.name for p in page.files()] == ["a.GTL", "a.DRL"]
    assert page._table.rowCount() == 2
    # Roles stay attached to the right files after a removal.
    assert [lid for _p, lid in page.assignments()] == [
        import_ui.classify_file(files[0]), import_ui.classify_file(files[2])]


def test_empty_page_shows_placeholder(qapp):
    from fypa.gerber import import_ui
    page = import_ui.GerberFilesPage()
    assert page._table.isHidden()
    assert not page.empty_label.isHidden()
    assert not page.check().ok


def test_validate_mapping_rules(tmp_path):
    from fypa.gerber.extract import (
        LAYER_ID_BOTTOM, LAYER_ID_DRILL, LAYER_ID_OUTLINE, LAYER_ID_TOP,
    )
    from fypa.gerber.import_ui import validate_mapping

    t, b, i2, o1, o2, d = (tmp_path / n for n in
                           ("t", "b", "i2", "o1", "o2", "d"))

    chk = validate_mapping([(t, LAYER_ID_TOP)])
    assert any("Bottom" in p for p in chk.problems)

    # Inner 2 without Inner 1 is a gap.
    chk = validate_mapping([(t, LAYER_ID_TOP), (b, LAYER_ID_BOTTOM),
                            (i2, 3)])
    assert any("no gaps" in p for p in chk.problems)

    # Two Top files.
    chk = validate_mapping([(t, LAYER_ID_TOP), (i2, LAYER_ID_TOP),
                            (b, LAYER_ID_BOTTOM)])
    assert any("single Gerber" in p for p in chk.problems)

    # Two outlines: first kept, the other ignored with a note; no drill
    # gives a note too. Neither blocks.
    chk = validate_mapping([(t, LAYER_ID_TOP), (b, LAYER_ID_BOTTOM),
                            (o1, LAYER_ID_OUTLINE), (o2, LAYER_ID_OUTLINE)])
    assert chk.ok
    assert chk.outline == o1 and chk.ignored == [o2]
    assert any("Outline" in n for n in chk.notes)
    assert any("No drill" in n for n in chk.notes)

    chk = validate_mapping([(t, LAYER_ID_TOP), (b, LAYER_ID_BOTTOM),
                            (d, LAYER_ID_DRILL)])
    assert chk.ok and not chk.notes
