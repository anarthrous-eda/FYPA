"""Opening projects, solutions and Altium imports into a viewer window."""
from __future__ import annotations

import logging
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from fypa.viewer.diagnostics import _maybe_warn_connectivity_breaks, _maybe_warn_open_loop_rails
from fypa.viewer.prefs import (
    _remove_recent_project_entry,
    load_adaptive_regulator_gain,
    load_auto_solve_on_import,
    load_recent_open_clean,
    record_recent_project,
)
from fypa.viewer.session import (
    _confirm_replace_project,
    _drop_failed_fypa_recent_entry,
    _reject_if_background_load_running,
    _reject_if_solve_running,
    _restore_bundled_design_info,
    _run_background_load,
    _stash_pending_altium_recent,
)
from fypa.viewer.settings_tab import _SettingsTabMixin
from fypa.viewer.solve_worker import _choose_pcbdoc, _SolveProgressUpdater, _SolveWorker


def _altium_import_auto_solve_status_tip(auto_solve: bool) -> str:
    if auto_solve:
        return (
            "Pick a .PrjPcb; reuse the cached solution if the project is "
            "unchanged, otherwise extract + solve."
        )
    return (
        "Pick a .PrjPcb; load the design info, then press ↻ Solve "
        "in the viewer to run the simulation."
    )




def _altium_import_worker_options(
    prjpcb_name: str, *, clean: bool,
    auto_solve: bool | None = None,
) -> dict:
    """Worker flags + progress-dialog copy for Import Altium Design."""
    if auto_solve is None:
        auto_solve = load_auto_solve_on_import()
    if clean:
        if auto_solve:
            return {
                "use_design_cache": False,
                "try_solve_cache_first": False,
                "load_only": False,
                "dialog_title": "Loading project (clean)",
                "dialog_text": (
                    f"Loading {prjpcb_name} and solving…\n"
                    "This can take 10–60 s depending on board size and mesh "
                    "density."
                ),
            }
        return {
            "use_design_cache": False,
            "try_solve_cache_first": False,
            "load_only": True,
            "dialog_title": "Loading project (clean)",
            "dialog_text": (
                f"Loading design info for {prjpcb_name} (ignoring cache)…\n"
                "Press ↻ Solve in the viewer when you're ready to run the "
                "simulation."
            ),
        }
    if auto_solve:
        return {
            "use_design_cache": True,
            "try_solve_cache_first": True,
            "load_only": False,
            "dialog_title": "Loading project",
            "dialog_text": (
                f"Checking solve cache for {prjpcb_name}…\n"
                "On a cache miss this falls through to a full extract + solve "
                "(10–60 s depending on board size)."
            ),
        }
    return {
        "use_design_cache": True,
        "try_solve_cache_first": False,
        "load_only": True,
        "dialog_title": "Loading project",
        "dialog_text": (
            f"Loading design info for {prjpcb_name}…\n"
            "Press ↻ Solve in the viewer when you're ready to run the "
            "simulation."
        ),
    }




def _altium_import_clean_status_tip(auto_solve: bool) -> str:
    if auto_solve:
        return (
            "Pick a .PrjPcb; ignore any cached design info or solution and "
            "re-extract + re-solve from scratch."
        )
    return (
        "Pick a .PrjPcb; ignore any cached design info or solution and "
        "re-extract from scratch (press ↻ Solve when ready)."
    )




def _resolve_project_solution_path(proj) -> str | None:
    """Return a readable solution-pickle path for *proj*, or ``None``."""
    sol_path = proj.solve_pickle
    if not sol_path or not Path(sol_path).exists():
        sol_path = None
        if proj.prjpcb_path:
            try:
                from fypa.cli import _solve_cache_path
                cand = _solve_cache_path(
                    Path(proj.prjpcb_path),
                    Path(proj.pcbdoc_path) if proj.pcbdoc_path else None,
                )
                if cand.exists():
                    sol_path = str(cand)
            except Exception:
                sol_path = None
    return sol_path




def _open_project_file_at(window, path: Path, *, from_recent: bool = False) -> None:
    """Open a ``.fypa`` at *path* without a file dialog."""
    from fypa.project_file import ProjectFile

    def _drop_failed_fypa_recent() -> None:
        _drop_failed_fypa_recent_entry(path, from_recent=from_recent)

    if _reject_if_solve_running(window, title="Load already running"):
        return
    if _reject_if_background_load_running(window, title="Load already running"):
        return
    if not _confirm_replace_project(window):
        return

    if not path.exists():
        QMessageBox.critical(
            window, "Couldn't open project",
            f"Project file not found:\n{path}",
        )
        _drop_failed_fypa_recent()
        return
    try:
        proj = ProjectFile.load(path)
    except Exception as e:
        QMessageBox.critical(
            window, "Couldn't open project",
            f"Failed to load {path}:\n\n{type(e).__name__}: {e}",
        )
        _drop_failed_fypa_recent()
        return
    _restore_bundled_design_info(proj)
    sol_path = _resolve_project_solution_path(proj)
    if not sol_path:
        QMessageBox.critical(
            window, "Couldn't open project",
            "The project file doesn't point to a readable solution "
            "pickle, and no cached solve was found.",
        )
        _drop_failed_fypa_recent()
        return

    def _work():
        from fypa.cli import _load_solution_pickle
        return _load_solution_pickle(Path(sol_path))

    def _ok(result):
        solution, metadata = result
        if hasattr(window, "_open_viewer_and_close"):
            new_win = window._open_viewer_and_close(
                solution, metadata, project=proj, project_path=path,
            )
            if new_win is not None:
                record_recent_project({"kind": "fypa", "path": str(path)})
            _maybe_warn_open_loop_rails(new_win or window, metadata)
            _maybe_warn_connectivity_breaks(new_win or window, metadata)
            return
        ok = window._apply_solve_result(
            solution, metadata, None,
            window._solve_settings,
            window._via_current_warn_a, window._display_percentile_high,
            project=proj, project_path=path,
        )
        if ok:
            record_recent_project({"kind": "fypa", "path": str(path)})

    def _err(exc_type, message):
        QMessageBox.critical(
            window, "Couldn't open project",
            f"Failed to load the linked solution {sol_path}:\n\n"
            f"{exc_type}: {message}",
        )
        _drop_failed_fypa_recent()

    _run_background_load(
        window, _work, _ok, _err,
        title="Loading", label="Loading solution…",
    )




def _open_solution_at(window, path: Path) -> None:
    """Open a solution ``.pkl`` at *path* without a file dialog."""
    if _reject_if_solve_running(window, title="Load already running"):
        return
    if _reject_if_background_load_running(window, title="Load already running"):
        return
    if not _confirm_replace_project(window):
        return

    if not path.exists():
        QMessageBox.critical(
            window, "Couldn't open solution",
            f"Solution file not found:\n{path}",
        )
        return

    def _work():
        from fypa.cli import _load_solution_pickle
        return _load_solution_pickle(path)

    def _ok(result):
        solution, metadata = result
        if hasattr(window, "_open_viewer_and_close"):
            window._open_viewer_and_close(solution, metadata)
            return
        window._apply_solve_result(
            solution, metadata, None,
            window._solve_settings,
            window._via_current_warn_a, window._display_percentile_high,
            is_import=True,
            status_done="Solution loaded.",
        )

    def _err(exc_type, message):
        QMessageBox.critical(
            window, "Couldn't open solution",
            f"Failed to load {path}:\n\n{exc_type}: {message}",
        )

    _run_background_load(
        window, _work, _ok, _err,
        title="Loading", label="Loading solution…",
    )




def _headless_platform() -> bool:
    """True when Qt has no interactive display (``offscreen`` / ``minimal``).

    A modal dialog on such a platform can never be dismissed, so a CI job
    invoking ``FYPA gui`` would block indefinitely rather than fail.
    """
    app = QApplication.instance()
    name = app.platformName() if app is not None else ""
    return name in ("offscreen", "minimal", "")




def _consume_cli_adaptive_flag(window) -> bool:
    """Resolve the adaptive-gain setting for one import, then clear it.

    A CLI ``--adaptive-regulator-gain`` / ``--no-adaptive-regulator-gain``
    value belongs to the import it was passed with. A failed or cancelled CLI
    import leaves the launcher open, and latching the value there would
    override the user's Settings-tab choice for every later File > Import in
    the session. Unset falls back to the persisted preference.
    """
    adaptive = getattr(window, "_cli_adaptive_regulator_gain", None)
    if adaptive is None:
        return load_adaptive_regulator_gain()
    window._cli_adaptive_regulator_gain = None
    return bool(adaptive)




def _start_launcher_altium_solve(
    window, prjpcb_path: Path, pcbdoc_path: Path | None, *, clean: bool,
    from_recent: bool = False, force_solve: bool = False,
) -> None:
    """Launcher-only: run :class:`_SolveWorker` for an Altium import.

    Uses the launcher's :attr:`_solve_settings` (Settings tab / CLI ``gui``
    overrides) and the persisted adaptive-gain preference — same inputs as
    File > Import from an already-open viewer.
    """
    if _reject_if_solve_running(window):
        return
    if _reject_if_background_load_running(window):
        return
    from fypa.altium.loader import SolveSettings

    settings = getattr(window, "_solve_settings", None)
    if settings is None:
        settings = SolveSettings()
        window._solve_settings = settings
    settings.apply_to_modules()
    imp = _altium_import_worker_options(
        prjpcb_path.name, clean=clean,
        auto_solve=True if force_solve else load_auto_solve_on_import(),
    )
    dlg = QProgressDialog(imp["dialog_text"], "Cancel", 0, 0, window)
    dlg.setWindowTitle(imp["dialog_title"])
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
    dlg.canceled.connect(window._on_solve_cancelled)
    dlg.show()
    QApplication.processEvents()
    _sz = dlg.size()
    dlg.setFixedSize(int(_sz.width() * 1.44), _sz.height())

    adaptive = _consume_cli_adaptive_flag(window)

    worker = _SolveWorker(
        prjpcb_path, settings,
        pcbdoc_selector=str(pcbdoc_path) if pcbdoc_path else None,
        use_design_cache=imp["use_design_cache"],
        try_solve_cache_first=imp["try_solve_cache_first"],
        load_only=imp["load_only"],
        adaptive_regulator_gain=bool(adaptive),
        parent=window,
    )
    _stash_pending_altium_recent(
        window, prjpcb_path, pcbdoc_path, from_recent=from_recent,
    )
    window._solve_worker = worker
    window._solve_progress_dlg = dlg
    window._solve_progress_updater = _SolveProgressUpdater(dlg, worker, window)
    worker.finished_ok.connect(
        lambda sol, meta, loaded: window._on_solve_finished(
            sol, meta, loaded, settings,
        )
    )
    worker.failed.connect(window._on_solve_failed)
    worker.finished.connect(window._cleanup_solve_worker)
    worker.start()




def _resync_settings_fields(window, values: dict) -> None:
    """Push ``{settings_attr: value}`` into the matching Settings-tab edits."""
    for key, value in values.items():
        edit = getattr(window, f"settings_edit_{key}", None)
        if edit is None:
            continue
        edit.setText(_SettingsTabMixin._fmt_settings_value(value))
    refresh = getattr(window, "_update_settings_field_styles", None)
    if callable(refresh):
        refresh()




def _schedule_cli_altium_import(window, target: dict) -> None:
    """Apply CLI ``gui`` overrides, then import like File > Import Altium."""
    settings = getattr(window, "_solve_settings", None)
    overrides = {
        key: target[key]
        for key in ("mesh_min_angle_deg", "mesh_max_size_mm")
        if target.get(key) is not None
    }
    if overrides and settings is None:
        logging.getLogger(__name__).warning(
            "Ignoring CLI mesh override(s) %s: this window has no solve "
            "settings to apply them to.", ", ".join(sorted(overrides)),
        )
    elif overrides:
        for key, value in overrides.items():
            setattr(settings, key, float(value))
        # The Settings tab was populated from ``settings`` during __init__, so
        # its line edits still show the pre-override values — they would read
        # as user edits (dirty red outline) and would overwrite the CLI values
        # if the user pressed Apply. Push the new values into the widgets.
        _resync_settings_fields(window, overrides)
    adaptive = target.get("adaptive_regulator_gain")
    if adaptive is not None:
        window._cli_adaptive_regulator_gain = bool(adaptive)
    pcbdoc = target.get("pcbdoc_path")
    window._cli_import_pending = True
    _open_altium_project_at(
        window,
        Path(target["prjpcb_path"]),
        Path(pcbdoc) if pcbdoc else None,
        clean=bool(target.get("clean", False)),
        force_solve=bool(target.get("force_solve", False)),
    )




def _open_altium_project_at(
    window,
    prjpcb_path: Path,
    pcbdoc_path: Path | None = None,
    *,
    clean: bool = False,
    from_recent: bool = False,
    force_solve: bool = False,
) -> None:
    """Import an Altium ``.PrjPcb`` without a file dialog when possible.

    ``force_solve`` overrides the persisted "Solve automatically on Altium
    import" preference. The CLI sets it when the user passed a mesh or
    adaptive-gain flag: those mean nothing without a solve, so deferring to a
    GUI checkbox there parses the flags and silently discards them.
    """
    if _reject_if_solve_running(window):
        return
    if _reject_if_background_load_running(window):
        return
    if not prjpcb_path.exists():
        QMessageBox.critical(
            window, "Couldn't open project",
            f"Project file not found:\n{prjpcb_path}",
        )
        entry: dict[str, str] = {
            "kind": "altium", "prjpcb_path": str(prjpcb_path),
        }
        if pcbdoc_path is not None:
            entry["pcbdoc_path"] = str(pcbdoc_path)
        _remove_recent_project_entry(entry)
        return

    selected_pcbdoc = pcbdoc_path
    if selected_pcbdoc is not None and not selected_pcbdoc.exists():
        selected_pcbdoc = None
    if selected_pcbdoc is None:
        proceed, selected_pcbdoc = _choose_pcbdoc(
            window, prjpcb_path, default=pcbdoc_path,
        )
        if not proceed:
            return

    if hasattr(window, "_open_viewer_and_close"):
        _start_launcher_altium_solve(
            window, prjpcb_path, selected_pcbdoc, clean=clean,
            from_recent=from_recent, force_solve=force_solve,
        )
        return

    if not _confirm_replace_project(window):
        return

    window._solve_settings.apply_to_modules()
    imp = _altium_import_worker_options(
        prjpcb_path.name, clean=clean,
        auto_solve=True if force_solve else load_auto_solve_on_import(),
    )
    window._start_solve_worker(
        prjpcb_path, window._solve_settings,
        window._via_current_warn_a, window._display_percentile_high,
        pcbdoc_selector=str(selected_pcbdoc) if selected_pcbdoc else None,
        use_design_cache=imp["use_design_cache"],
        try_solve_cache_first=imp["try_solve_cache_first"],
        load_only=imp["load_only"],
        adaptive_regulator_gain=load_adaptive_regulator_gain(),
        is_import=True,
        dialog_title=imp["dialog_title"],
        dialog_text=imp["dialog_text"],
        pending_altium_recent_pcbdoc=selected_pcbdoc,
        pending_altium_from_recent=from_recent,
    )




def _open_recent_project(window, entry: dict) -> None:
    """Re-open a persisted recent-project entry."""
    kind = entry.get("kind")
    if kind == "fypa":
        _open_project_file_at(window, Path(entry["path"]), from_recent=True)
    elif kind == "altium":
        pcbdoc = entry.get("pcbdoc_path")
        _open_altium_project_at(
            window,
            Path(entry["prjpcb_path"]),
            Path(pcbdoc) if pcbdoc else None,
            clean=load_recent_open_clean(),
            from_recent=True,
        )
