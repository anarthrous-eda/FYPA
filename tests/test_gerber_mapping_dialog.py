"""Gerber layer-mapping dialog: the Add Files button appends files from
other folders and skips ones already listed."""
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
    from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

    from fypa.gerber import import_ui
    from fypa.gerber.extract import LAYER_ID_BOTTOM, LAYER_ID_TOP

    top = _touch(tmp_path / "gerbers" / "board.GTL")
    bot = _touch(tmp_path / "gerbers" / "board.GBL")
    drl = _touch(tmp_path / "nc_drill" / "board.DRL")

    dlg = import_ui.GerberLayerMappingDialog([top, bot])
    assert dlg._table.rowCount() == 2

    # Pick the drill file from another folder, plus a file already listed.
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(drl), str(top)], "")),
    )
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: infos.append(a)),
    )
    dlg._on_add_files()

    assert dlg._table.rowCount() == 3
    assert dlg._table.item(2, 0).text() == "board.DRL"
    assert len(infos) == 1  # told one file was already listed

    dlg._on_accept()
    assert dlg.result() == QDialog.Accepted
    assert dlg.copper_files() == {LAYER_ID_TOP: top, LAYER_ID_BOTTOM: bot}
    assert dlg.drill_files() == [drl]
