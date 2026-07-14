"""Tests for File > Recent Projects persistence helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from fypa.altium_viewer import (
    _RECENT_PROJECTS_QS_KEY,
    _THEME_QS_APP,
    _THEME_QS_ORG,
    clear_recent_projects,
    load_recent_projects,
    prune_missing_recent_projects,
    record_recent_project,
)


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path: Path) -> None:
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path),
    )
    clear_recent_projects()
    yield
    clear_recent_projects()


def test_record_and_load_fypa_entry(tmp_path: Path) -> None:
    fypa = tmp_path / "board.fypa"
    fypa.write_text("{}", encoding="utf-8")
    record_recent_project({"kind": "fypa", "path": str(fypa)})
    entries = load_recent_projects()
    assert len(entries) == 1
    assert entries[0]["kind"] == "fypa"
    assert entries[0]["path"] == str(fypa.resolve())


def test_record_dedupes_and_moves_to_front(tmp_path: Path) -> None:
    a = tmp_path / "a.fypa"
    b = tmp_path / "b.fypa"
    a.write_text("{}", encoding="utf-8")
    b.write_text("{}", encoding="utf-8")
    record_recent_project({"kind": "fypa", "path": str(a)})
    record_recent_project({"kind": "fypa", "path": str(b)})
    record_recent_project({"kind": "fypa", "path": str(a)})
    entries = load_recent_projects()
    assert [e["path"] for e in entries] == [str(a.resolve()), str(b.resolve())]


def test_record_max_ten_entries(tmp_path: Path) -> None:
    for i in range(12):
        path = tmp_path / f"p{i}.fypa"
        path.write_text("{}", encoding="utf-8")
        record_recent_project({"kind": "fypa", "path": str(path)})
    entries = load_recent_projects()
    assert len(entries) == 10
    assert entries[0]["path"] == str((tmp_path / "p11.fypa").resolve())


def test_load_ignores_invalid_json() -> None:
    qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
    qs.setValue(_RECENT_PROJECTS_QS_KEY, "not-json")
    assert load_recent_projects() == []


def test_load_ignores_invalid_entries(tmp_path: Path) -> None:
    good = tmp_path / "ok.fypa"
    good.write_text("{}", encoding="utf-8")
    import json

    qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
    qs.setValue(
        _RECENT_PROJECTS_QS_KEY,
        json.dumps([
            {"kind": "fypa"},
            {"kind": "altium", "prjpcb_path": ""},
            {"kind": "fypa", "path": str(good)},
            {"kind": "unknown", "path": "/x"},
        ]),
    )
    entries = load_recent_projects()
    assert len(entries) == 1
    assert entries[0]["path"] == str(good.resolve())


def test_prune_missing_recent_projects(tmp_path: Path) -> None:
    present = tmp_path / "present.fypa"
    present.write_text("{}", encoding="utf-8")
    missing = tmp_path / "gone.fypa"
    missing.write_text("{}", encoding="utf-8")
    record_recent_project({"kind": "fypa", "path": str(present)})
    record_recent_project({"kind": "fypa", "path": str(missing)})
    missing.unlink()
    kept = prune_missing_recent_projects()
    assert len(kept) == 1
    assert kept[0]["path"] == str(present.resolve())
    assert len(load_recent_projects()) == 1


def test_prune_altium_keeps_entry_when_pcbdoc_missing(tmp_path: Path) -> None:
    prjpcb = tmp_path / "board.PrjPcb"
    prjpcb.write_text("FAKE", encoding="utf-8")
    record_recent_project({
        "kind": "altium",
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(tmp_path / "missing.PcbDoc"),
    })
    kept = prune_missing_recent_projects()
    assert len(kept) == 1
    assert kept[0]["prjpcb_path"] == str(prjpcb.resolve())


def test_prune_altium_drops_missing_prjpcb(tmp_path: Path) -> None:
    record_recent_project({
        "kind": "altium",
        "prjpcb_path": str(tmp_path / "gone.PrjPcb"),
    })
    kept = prune_missing_recent_projects()
    assert kept == []


def test_recent_project_label_includes_parent_and_pcbdoc(tmp_path: Path) -> None:
    from fypa.altium_viewer import recent_project_label

    fypa = tmp_path / "designs" / "board.fypa"
    fypa.parent.mkdir()
    fypa.write_text("{}", encoding="utf-8")
    assert recent_project_label({"kind": "fypa", "path": str(fypa)}) == (
        "board.fypa — designs"
    )

    prjpcb = tmp_path / "altium" / "main.PrjPcb"
    prjpcb.parent.mkdir()
    prjpcb.write_text("FAKE", encoding="utf-8")
    pcbdoc = tmp_path / "altium" / "main.PcbDoc"
    pcbdoc.write_text("FAKE", encoding="utf-8")
    assert recent_project_label({
        "kind": "altium",
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(pcbdoc),
    }) == "main.PrjPcb — altium [main.PcbDoc]"


def test_remove_recent_project_entry_normalizes_path(tmp_path: Path) -> None:
    from fypa.altium_viewer import _remove_recent_project_entry

    fypa = tmp_path / "board.fypa"
    fypa.write_text("{}", encoding="utf-8")
    record_recent_project({"kind": "fypa", "path": str(fypa)})
    _remove_recent_project_entry({"kind": "fypa", "path": str(fypa)})
    assert load_recent_projects() == []


def test_clear_recent_projects(tmp_path: Path) -> None:
    path = tmp_path / "x.fypa"
    path.write_text("{}", encoding="utf-8")
    record_recent_project({"kind": "fypa", "path": str(path)})
    clear_recent_projects()
    assert load_recent_projects() == []


def test_viewer_has_unsaved_changes() -> None:
    from types import SimpleNamespace
    from fypa.altium_viewer import _viewer_has_unsaved_changes

    clean = SimpleNamespace(_apply_solve_result=lambda *a, **k: None)
    assert _viewer_has_unsaved_changes(clean) is False

    dirty = SimpleNamespace(
        _apply_solve_result=lambda *a, **k: None,
        _project_dirty=True,
    )
    assert _viewer_has_unsaved_changes(dirty) is True

    launcher = SimpleNamespace()
    assert _viewer_has_unsaved_changes(launcher) is False


def test_drop_pending_altium_recent(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from fypa.altium_viewer import (
        _clear_pending_altium_recent,
        _drop_pending_altium_recent,
        _stash_pending_altium_recent,
    )

    prjpcb = tmp_path / "board.PrjPcb"
    prjpcb.write_text("FAKE", encoding="utf-8")
    record_recent_project({"kind": "altium", "prjpcb_path": str(prjpcb)})
    win = SimpleNamespace()
    _stash_pending_altium_recent(win, prjpcb, None, from_recent=True)
    _drop_pending_altium_recent(win)
    assert load_recent_projects() == []
    assert win._pending_altium_from_recent is False

    record_recent_project({"kind": "altium", "prjpcb_path": str(prjpcb)})
    _stash_pending_altium_recent(win, prjpcb, None, from_recent=False)
    _drop_pending_altium_recent(win)
    assert len(load_recent_projects()) == 1

    _stash_pending_altium_recent(win, prjpcb, None, from_recent=False)
    _clear_pending_altium_recent(win)
    assert len(load_recent_projects()) == 1


def test_drop_pending_altium_recent_by_prjpcb(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from fypa.altium_viewer import (
        _drop_pending_altium_recent,
        _stash_pending_altium_recent,
    )

    prjpcb = tmp_path / "board.PrjPcb"
    prjpcb.write_text("FAKE", encoding="utf-8")
    pcb_a = tmp_path / "a.PcbDoc"
    pcb_a.write_text("FAKE", encoding="utf-8")
    record_recent_project({
        "kind": "altium",
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(pcb_a),
    })
    win = SimpleNamespace()
    pcb_b = tmp_path / "b.PcbDoc"
    pcb_b.write_text("FAKE", encoding="utf-8")
    _stash_pending_altium_recent(win, prjpcb, pcb_b, from_recent=True)
    _drop_pending_altium_recent(win)
    assert load_recent_projects() == []


def test_record_recent_altium_dedupes_by_prjpcb(tmp_path: Path) -> None:
    prjpcb = tmp_path / "board.PrjPcb"
    prjpcb.write_text("FAKE", encoding="utf-8")
    pcb_a = tmp_path / "a.PcbDoc"
    pcb_a.write_text("FAKE", encoding="utf-8")
    pcb_b = tmp_path / "b.PcbDoc"
    pcb_b.write_text("FAKE", encoding="utf-8")
    record_recent_project({
        "kind": "altium",
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(pcb_a),
    })
    record_recent_project({
        "kind": "altium",
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(pcb_b),
    })
    entries = load_recent_projects()
    assert len(entries) == 1
    assert entries[0]["prjpcb_path"] == str(prjpcb.resolve())
    assert entries[0]["pcbdoc_path"] == str(pcb_b.resolve())


def test_record_recent_altium_from_metadata_replaces_entry(tmp_path: Path) -> None:
    from fypa.altium_viewer import _record_recent_altium_from_metadata

    prjpcb = tmp_path / "board.PrjPcb"
    prjpcb.write_text("FAKE", encoding="utf-8")
    pcb_a = tmp_path / "a.PcbDoc"
    pcb_a.write_text("FAKE", encoding="utf-8")
    pcb_b = tmp_path / "b.PcbDoc"
    pcb_b.write_text("FAKE", encoding="utf-8")
    record_recent_project({
        "kind": "altium",
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(pcb_a),
    })
    _record_recent_altium_from_metadata({
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(pcb_b),
    })
    entries = load_recent_projects()
    assert len(entries) == 1
    assert entries[0]["pcbdoc_path"] == str(pcb_b.resolve())


def test_drop_failed_fypa_recent_entry(tmp_path: Path) -> None:
    from fypa.altium_viewer import _drop_failed_fypa_recent_entry

    fypa = tmp_path / "board.fypa"
    fypa.write_text("{}", encoding="utf-8")
    record_recent_project({"kind": "fypa", "path": str(fypa)})
    _drop_failed_fypa_recent_entry(fypa, from_recent=False)
    assert len(load_recent_projects()) == 1
    _drop_failed_fypa_recent_entry(fypa, from_recent=True)
    assert load_recent_projects() == []


def test_stash_pending_altium_recent_normalizes_paths(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from fypa.altium_viewer import _stash_pending_altium_recent

    prjpcb = tmp_path / "board.PrjPcb"
    prjpcb.write_text("FAKE", encoding="utf-8")
    pcbdoc = tmp_path / "board.PcbDoc"
    pcbdoc.write_text("FAKE", encoding="utf-8")
    win = SimpleNamespace()
    _stash_pending_altium_recent(win, prjpcb, pcbdoc, from_recent=True)
    assert win._pending_altium_recent_entry["prjpcb_path"] == str(
        prjpcb.resolve(),
    )
    assert win._pending_altium_recent_entry["pcbdoc_path"] == str(
        pcbdoc.resolve(),
    )


def test_reject_if_background_load_running() -> None:
    from unittest.mock import MagicMock, patch

    import fypa.altium_viewer as av

    owner = MagicMock()
    running = MagicMock()
    running.isRunning.return_value = True
    with patch.object(av, "_BACKGROUND_LOADERS", {running}):
        with patch.object(av, "QMessageBox") as mb:
            assert av._reject_if_background_load_running(owner) is True
            mb.information.assert_called_once()
    with patch.object(av, "_BACKGROUND_LOADERS", set()):
        assert av._reject_if_background_load_running(owner) is False


def test_confirm_replace_project_no_unsaved() -> None:
    from types import SimpleNamespace

    from fypa.altium_viewer import _confirm_replace_project

    viewer = SimpleNamespace(_apply_solve_result=lambda *a, **k: None)
    assert _confirm_replace_project(viewer) is True


def test_confirm_replace_project_cancel() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _confirm_replace_project

    viewer = SimpleNamespace(
        _apply_solve_result=lambda *a, **k: None,
        _project_dirty=True,
    )
    save_btn, discard_btn, cancel_btn = object(), object(), object()
    box = MagicMock()
    box.addButton.side_effect = [save_btn, discard_btn, cancel_btn]
    box.clickedButton.return_value = cancel_btn
    with patch("fypa.altium_viewer.QMessageBox", return_value=box):
        assert _confirm_replace_project(viewer) is False


def test_confirm_replace_project_discard() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _confirm_replace_project

    viewer = SimpleNamespace(
        _apply_solve_result=lambda *a, **k: None,
        _project_dirty=True,
    )
    save_btn, discard_btn, cancel_btn = object(), object(), object()
    box = MagicMock()
    box.addButton.side_effect = [save_btn, discard_btn, cancel_btn]
    box.clickedButton.return_value = discard_btn
    with patch("fypa.altium_viewer.QMessageBox", return_value=box):
        assert _confirm_replace_project(viewer) is True


def test_confirm_replace_project_save_project() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _confirm_replace_project

    viewer = SimpleNamespace(
        _apply_solve_result=lambda *a, **k: None,
        _project_dirty=True,
        _solved_since_save=False,
        _save_project=MagicMock(return_value=True),
    )
    save_btn, discard_btn, cancel_btn = object(), object(), object()
    box = MagicMock()
    box.addButton.side_effect = [save_btn, discard_btn, cancel_btn]
    box.clickedButton.return_value = save_btn
    dlg = MagicMock()
    dlg.exec.return_value = 1  # QDialog.Accepted
    dlg.choice = "project"
    with patch("fypa.altium_viewer.QMessageBox", return_value=box), patch(
        "fypa.altium_viewer._ProjectSaveDialog", return_value=dlg,
    ), patch("fypa.altium_viewer.QDialog") as qd:
        qd.Accepted = 1
        assert _confirm_replace_project(viewer) is True
    viewer._save_project.assert_called_once()


def test_confirm_replace_project_save_cancelled() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _confirm_replace_project

    viewer = SimpleNamespace(
        _apply_solve_result=lambda *a, **k: None,
        _project_dirty=True,
        _solved_since_save=False,
        _save_project=MagicMock(return_value=True),
    )
    save_btn, discard_btn, cancel_btn = object(), object(), object()
    box = MagicMock()
    box.addButton.side_effect = [save_btn, discard_btn, cancel_btn]
    box.clickedButton.return_value = save_btn
    dlg = MagicMock()
    dlg.exec.return_value = 0  # rejected
    dlg.choice = None
    with patch("fypa.altium_viewer.QMessageBox", return_value=box), patch(
        "fypa.altium_viewer._ProjectSaveDialog", return_value=dlg,
    ), patch("fypa.altium_viewer.QDialog") as qd:
        qd.Accepted = 1
        assert _confirm_replace_project(viewer) is False
    viewer._save_project.assert_not_called()


def test_open_recent_altium_uses_clean_import(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _open_recent_project

    prjpcb = tmp_path / "board.PrjPcb"
    pcbdoc = tmp_path / "board.PcbDoc"
    entry = {
        "kind": "altium",
        "prjpcb_path": str(prjpcb),
        "pcbdoc_path": str(pcbdoc),
    }
    window = MagicMock()
    with patch("fypa.altium_viewer._open_altium_project_at") as mock_open:
        _open_recent_project(window, entry)
    mock_open.assert_called_once_with(
        window, prjpcb, pcbdoc, clean=True, from_recent=True,
    )
