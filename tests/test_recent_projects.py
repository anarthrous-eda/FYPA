"""Tests for File > Recent Projects persistence helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from fypa.altium_viewer import (
    _RECENT_PROJECTS_QS_KEY,
    _THEME_QS_APP,
    _THEME_QS_ORG,
    clear_recent_projects,
    load_recent_projects,
    prune_missing_recent_projects,
    record_recent_project,
)


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _qs():
    # Looked up at call time so it is conftest's isolated QSettings, never
    # the real (registry-backed) one.
    from PySide6 import QtCore
    return QtCore.QSettings(_THEME_QS_ORG, _THEME_QS_APP)


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
    qs = _qs()
    qs.setValue(_RECENT_PROJECTS_QS_KEY, "not-json")
    assert load_recent_projects() == []


def test_load_ignores_invalid_entries(tmp_path: Path) -> None:
    good = tmp_path / "ok.fypa"
    good.write_text("{}", encoding="utf-8")
    import json

    qs = _qs()
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
    from fypa.viewer import session

    owner = MagicMock()
    running = MagicMock()
    running.isRunning.return_value = True
    with patch.object(session, "_BACKGROUND_LOADERS", {running}):
        with patch.object(session, "QMessageBox") as mb:
            assert av._reject_if_background_load_running(owner) is True
            mb.information.assert_called_once()
    with patch.object(session, "_BACKGROUND_LOADERS", set()):
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
    with patch("fypa.viewer.session.QMessageBox", return_value=box):
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
    with patch("fypa.viewer.session.QMessageBox", return_value=box):
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
    with patch("fypa.viewer.session.QMessageBox", return_value=box), patch(
        "fypa.viewer.file_menu._ProjectSaveDialog", return_value=dlg,
    ), patch("fypa.viewer.session.QDialog") as qd:
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
    with patch("fypa.viewer.session.QMessageBox", return_value=box), patch(
        "fypa.viewer.file_menu._ProjectSaveDialog", return_value=dlg,
    ), patch("fypa.viewer.session.QDialog") as qd:
        qd.Accepted = 1
        assert _confirm_replace_project(viewer) is False
    viewer._save_project.assert_not_called()


def test_open_recent_altium_uses_cached_import_by_default(
        tmp_path: Path) -> None:
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
    with patch("fypa.viewer.project_open._open_altium_project_at") as mock_open:
        _open_recent_project(window, entry)
    mock_open.assert_called_once_with(
        window, prjpcb, pcbdoc, clean=False, from_recent=True,
    )


def test_open_recent_altium_honours_clean_preference(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import (
        _open_recent_project,
        load_recent_open_clean,
        save_recent_open_clean,
    )

    assert load_recent_open_clean() is False
    save_recent_open_clean(True)
    assert load_recent_open_clean() is True

    prjpcb = tmp_path / "board.PrjPcb"
    entry = {"kind": "altium", "prjpcb_path": str(prjpcb)}
    window = MagicMock()
    try:
        with patch("fypa.viewer.project_open._open_altium_project_at") as mock_open:
            _open_recent_project(window, entry)
        mock_open.assert_called_once_with(
            window, prjpcb, None, clean=True, from_recent=True,
        )
    finally:
        save_recent_open_clean(False)


def test_open_recent_altium_explicit_clean_overrides_preference(
        tmp_path: Path) -> None:
    """The Recent panel's two icons pass clean explicitly; the File-menu
    preference must not override them."""
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _open_recent_project, save_recent_open_clean

    prjpcb = tmp_path / "board.PrjPcb"
    entry = {"kind": "altium", "prjpcb_path": str(prjpcb)}
    window = MagicMock()
    save_recent_open_clean(True)
    try:
        with patch("fypa.viewer.project_open._open_altium_project_at") as mock_open:
            _open_recent_project(window, entry, clean=False)
        mock_open.assert_called_once_with(
            window, prjpcb, None, clean=False, from_recent=True,
        )
    finally:
        save_recent_open_clean(False)
    with patch("fypa.viewer.project_open._open_altium_project_at") as mock_open:
        _open_recent_project(window, entry, clean=True)
    assert mock_open.call_args.kwargs["clean"] is True


# --- solution + gerber entry kinds -------------------------------------------


def _fake_gerber_result(tmp_path: Path):
    from types import SimpleNamespace

    gerbers = tmp_path / "gerbers"
    gerbers.mkdir()
    top = gerbers / "board.GTL"
    bot = gerbers / "board.GBL"
    drl = gerbers / "board.DRL"
    gko = gerbers / "board.GKO"
    for p in (top, bot, drl, gko):
        p.write_text("G04*", encoding="utf-8")
    stack = [
        SimpleNamespace(layer_id=1, name="Top", copper_thickness_mm=0.035,
                        dielectric_thickness_mm=1.5),
        SimpleNamespace(layer_id=32, name="Bottom", copper_thickness_mm=0.035,
                        dielectric_thickness_mm=0.0),
    ]
    result = SimpleNamespace(
        copper_files={1: top, 32: bot}, drill_files=[drl], outline_file=gko,
        silk_top_file=None, silk_bot_file=None, ignored_files=[], stackup=stack,
    )
    pseudo = gerbers / "gerbers.fypa-gerber"
    return result, pseudo


def test_record_and_load_solution_entry(tmp_path: Path) -> None:
    from fypa.altium_viewer import recent_project_label

    sol = tmp_path / "runs" / "board.pkl"
    sol.parent.mkdir()
    sol.write_bytes(b"x")
    record_recent_project({"kind": "solution", "path": str(sol)})
    entries = load_recent_projects()
    assert entries == [{"kind": "solution", "path": str(sol.resolve())}]
    assert recent_project_label(entries[0]) == "board.pkl — runs"
    sol.unlink()
    assert prune_missing_recent_projects() == []


def test_solution_and_fypa_same_path_are_distinct(tmp_path: Path) -> None:
    p = tmp_path / "x.pkl"
    p.write_bytes(b"x")
    record_recent_project({"kind": "solution", "path": str(p)})
    record_recent_project({"kind": "fypa", "path": str(p)})
    assert [e["kind"] for e in load_recent_projects()] == ["fypa", "solution"]


def test_record_gerber_round_trip(tmp_path: Path) -> None:
    from fypa.viewer.prefs import record_recent_gerber, recent_project_label
    from fypa.viewer.project_open import _gerber_import_inputs_from_recent

    result, pseudo = _fake_gerber_result(tmp_path)
    record_recent_gerber(result, pseudo)
    entries = load_recent_projects()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "gerber"
    assert recent_project_label(entry).endswith("[Gerber, 2 layers]")

    rebuilt, rebuilt_pseudo = _gerber_import_inputs_from_recent(entry)
    assert rebuilt_pseudo == pseudo.resolve()
    assert {k: v.resolve() for k, v in rebuilt.copper_files.items()} == {
        k: v.resolve() for k, v in result.copper_files.items()
    }
    assert [p.resolve() for p in rebuilt.drill_files] == [
        p.resolve() for p in result.drill_files
    ]
    assert rebuilt.outline_file.resolve() == result.outline_file.resolve()
    assert rebuilt.silk_top_file is None
    assert [(s.layer_id, s.copper_thickness_mm, s.dielectric_thickness_mm)
            for s in rebuilt.stackup] == [(1, 0.035, 1.5), (32, 0.035, 0.0)]


def test_gerber_entry_dedupes_by_folder(tmp_path: Path) -> None:
    from fypa.viewer.prefs import record_recent_gerber

    result, pseudo = _fake_gerber_result(tmp_path)
    record_recent_gerber(result, pseudo)
    record_recent_gerber(result, pseudo)
    assert len(load_recent_projects()) == 1


def test_gerber_entry_missing_file_is_pruned(tmp_path: Path) -> None:
    from fypa.viewer.prefs import record_recent_gerber, recent_gerber_missing_files

    result, pseudo = _fake_gerber_result(tmp_path)
    record_recent_gerber(result, pseudo)
    result.drill_files[0].unlink()
    entry = load_recent_projects()[0]
    assert recent_gerber_missing_files(entry) == [
        str(result.drill_files[0].resolve()),
    ]
    assert prune_missing_recent_projects() == []


def test_malformed_gerber_entries_are_dropped(tmp_path: Path) -> None:
    import json

    good_stack = [{"layer_id": 1, "name": "Top",
                   "copper_thickness_mm": 0.035,
                   "dielectric_thickness_mm": 0.0}]
    qs = _qs()
    qs.setValue(_RECENT_PROJECTS_QS_KEY, json.dumps([
        {"kind": "gerber", "folder": str(tmp_path), "copper": [],
         "stackup": good_stack},
        {"kind": "gerber", "folder": str(tmp_path),
         "copper": [["top", str(tmp_path / "a.gtl")]], "stackup": good_stack},
        {"kind": "gerber", "folder": str(tmp_path),
         "copper": [[1, str(tmp_path / "a.gtl")]],
         "stackup": [{"layer_id": 1, "copper_thickness_mm": "nan",
                      "dielectric_thickness_mm": 0}]},
        {"kind": "gerber", "folder": str(tmp_path),
         "copper": [[1, str(tmp_path / "a.gtl")]], "stackup": good_stack},
    ]))
    entries = load_recent_projects()
    assert len(entries) == 1
    assert entries[0]["copper"] == [[1, str((tmp_path / "a.gtl").resolve())]]


def test_open_recent_dispatches_solution_and_gerber(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _open_recent_project
    from fypa.viewer.prefs import recent_gerber_entry_from_result

    window = MagicMock()
    sol = tmp_path / "s.pkl"
    with patch("fypa.viewer.project_open._open_solution_at") as mock_sol:
        _open_recent_project(window, {"kind": "solution", "path": str(sol)})
    mock_sol.assert_called_once_with(window, sol, from_recent=True)

    result, pseudo = _fake_gerber_result(tmp_path)
    entry = recent_gerber_entry_from_result(result, pseudo)
    _open_recent_project(window, entry)
    window._start_gerber_import.assert_called_once()
    _res, called_pseudo = window._start_gerber_import.call_args.args
    assert called_pseudo == pseudo


def test_open_recent_gerber_missing_files_drops_entry(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from fypa.altium_viewer import _open_recent_project
    from fypa.viewer.prefs import record_recent_gerber

    result, pseudo = _fake_gerber_result(tmp_path)
    record_recent_gerber(result, pseudo)
    entry = load_recent_projects()[0]
    result.copper_files[32].unlink()
    window = MagicMock()
    with patch("fypa.viewer.project_open.QMessageBox") as mb:
        _open_recent_project(window, entry)
    mb.critical.assert_called_once()
    window._start_gerber_import.assert_not_called()
    assert load_recent_projects() == []


def test_recent_panel_state_and_listener(qapp, tmp_path: Path) -> None:
    """Panel rows follow the shared list (via the listener) and the
    expanded state persists, collapsed by default."""
    from unittest.mock import MagicMock

    from fypa.viewer.prefs import load_recent_panel_expanded
    from fypa.viewer.recent_panel import RecentPanel

    assert load_recent_panel_expanded() is False
    panel = RecentPanel(MagicMock())
    assert panel.is_expanded() is False
    assert panel.entries() == []

    prj = tmp_path / "b.PrjPcb"
    prj.write_text("FAKE", encoding="utf-8")
    sol = tmp_path / "s.pkl"
    sol.write_bytes(b"x")
    record_recent_project({"kind": "altium", "prjpcb_path": str(prj)})
    record_recent_project({"kind": "solution", "path": str(sol)})
    assert [e["kind"] for e in panel.entries()] == ["solution", "altium"]
    # Solution row: one load icon; Altium row: load + clean.
    assert panel._table.cellWidget(0, 2) is not None
    assert panel._table.cellWidget(0, 3) is None
    assert panel._table.cellWidget(1, 3) is not None

    panel.set_expanded(True)
    assert load_recent_panel_expanded() is True
    assert RecentPanel(MagicMock()).is_expanded() is True
    panel.set_expanded(False)

    clear_recent_projects()
    assert panel.entries() == []
