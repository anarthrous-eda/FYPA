"""Open-window registry, background loaders and project-replacement guards."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QDialog, QMainWindow, QMessageBox, QProgressDialog

from fypa.viewer.prefs import _normalize_recent_path, _remove_recent_project_entry


def _metadata_has_adaptive_smps(metadata: dict | None) -> bool:
    if not metadata:
        return False
    for d in metadata.get("directives", []):
        if d.get("role") != "REGULATOR":
            continue
        eligible = d.get("adaptive_gain_eligible")
        if eligible is True:
            return True
        if eligible is False:
            continue
        # Legacy solve pickles predating adaptive_gain_eligible: treat SMPS
        # regulators as eligible (explicit PDN_GAIN was not recorded then).
        if d.get("regulator_type") == "SMPS":
            return True
    return False




def _viewer_has_adaptive_smps(
    metadata: dict | None,
    loaded_project: object | None,
) -> bool:
    """True when the open design has at least one adaptive-eligible SMPS."""
    if _metadata_has_adaptive_smps(metadata):
        return True
    if loaded_project is not None:
        try:
            from fypa.altium.loader import has_adaptive_smps_regulators
            return has_adaptive_smps_regulators(loaded_project)
        except Exception:
            pass
    return False




# Strong-ref list of every viewer window we hand off to the user. Without
# this, freshly-created QMainWindows can be garbage-collected the moment
# the spawning function returns — PySide6's ``QApplication.setProperty``
# does NOT hold a reliable Python reference across signal boundaries, so
# we anchor at module scope instead. Dead entries are pruned each append.
_LIVE_VIEWERS: list[QMainWindow] = []




def _restore_bundled_design_info(proj) -> None:
    """Copy a project's bundled ``design-info`` pickle into this machine's
    design cache, so a later editor *Resolve* can reuse the extract instead
    of re-reading Altium source that may not have travelled with a shared
    project.

    A saved ``.fypa`` references its design-info pickle by path, but the
    editor's re-solve loads design info from the per-machine design cache
    (keyed to the original ``.PrjPcb`` location), never from the project's
    own pickle — so on another machine that pickle would go unused. Seeding
    the cache from it bridges the gap. Best-effort; failures are ignored.
    """
    di = getattr(proj, "design_info_pickle", None)
    if not di or not Path(di).exists() or not proj.prjpcb_path:
        return
    try:
        import shutil
        from fypa.cli import _design_info_cache_path
        dest = _design_info_cache_path(
            Path(proj.prjpcb_path),
            Path(proj.pcbdoc_path) if proj.pcbdoc_path else None,
        )
        if Path(di).resolve() == dest.resolve() or dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(di, dest)
    except Exception:
        pass




# Strong references to QThreads that outlived their dialog (e.g. a Gerber
# import cancelled mid-render, whose ProcessPoolExecutor phase can't be
# interrupted cooperatively and so runs to completion in the background). We
# keep them referenced until they emit ``finished`` — dropping the last Python
# reference to a still-running QThread destroys the C++ object and triggers
# ``qFatal("QThread: Destroyed while thread is still running")``.
_ORPHANED_THREADS: set = set()




def _retire_thread(worker) -> None:
    """Safely dispose of a QThread that may still be running.

    Never ``deleteLater()`` a running QThread: ``~QThread`` calls ``qFatal`` if
    the thread is still executing, aborting the whole app. If the worker has
    already stopped, delete it now; otherwise ask it to interrupt, stash a
    strong reference so neither Python GC nor ``deleteLater`` can destroy it
    while it runs, and let it self-delete once it emits ``finished``."""
    if worker is None:
        return
    try:
        running = worker.isRunning()
    except RuntimeError:
        return  # C++ side already gone
    if not running:
        worker.deleteLater()
        return
    try:
        worker.requestInterruption()
    except RuntimeError:
        pass
    # Detach from the Qt parent. A worker constructed with ``parent=self`` (the
    # solve / Gerber-import workers are) would otherwise be destroyed by Qt's
    # parent teardown when that parent dies — e.g. at app exit or when the
    # owning window closes — and ``~QThread`` on a still-running thread calls
    # qFatal. The _ORPHANED_THREADS strong ref only stops Python GC; it can't
    # stop C++ parent teardown, so we must reparent to None.
    try:
        worker.setParent(None)
    except RuntimeError:
        pass
    _ORPHANED_THREADS.add(worker)

    def _dispose() -> None:
        _ORPHANED_THREADS.discard(worker)
        worker.deleteLater()

    worker.finished.connect(_dispose)
    # Race guard: if the worker emitted ``finished`` between the isRunning()
    # check above and this connect, ``_dispose`` would never fire and the
    # strong ref would leak in _ORPHANED_THREADS (and the worker never gets
    # deleteLater'd). Re-check and dispose now; ``_dispose`` is idempotent.
    try:
        if not worker.isRunning():
            _dispose()
    except RuntimeError:
        _ORPHANED_THREADS.discard(worker)




# Strong refs to in-flight background loaders (see _run_background_load). Held
# for the worker's lifetime so the Python QThread wrapper can't be GC'd mid-run
# (which would destroy the C++ object → qFatal). Discarded once ``finished``
# has been delivered and the worker deleteLater'd.
_BACKGROUND_LOADERS: set = set()




class _BackgroundLoadWorker(QThread):
    """Run a blocking, Qt-free load callable off the GUI thread.

    ``work`` runs in :meth:`run` (the worker thread) and must touch no Qt
    widgets — file I/O and unpickling only. Its return value / exception are
    stashed on the instance and delivered by :meth:`_deliver`, which runs back
    on the GUI thread because ``finished`` is a queued cross-thread signal (the
    worker object's affinity is the GUI thread that created it). Exactly one of
    ``on_success(result)`` / ``on_error(exc_type_name, message)`` then fires.

    Note: unpickling is largely GIL-bound, so this doesn't make the load itself
    faster — but it keeps the Qt event loop serviced (the GIL is released
    periodically), so the window keeps repainting the busy dialog instead of
    going "Not Responding". The fully-robust option is a subprocess load; see
    the §3 "Move the solve to a subprocess" note in the perf review.
    """

    def __init__(self, work, on_success, on_error, parent=None) -> None:
        super().__init__(parent)
        self._work = work
        self._on_success = on_success
        self._on_error = on_error
        self._result = None
        self._error: tuple[str, str] | None = None
        self.finished.connect(self._deliver)

    def run(self) -> None:
        try:
            self._result = self._work()
        except Exception as e:  # noqa: BLE001 — surfaced on the GUI thread
            self._error = (type(e).__name__, str(e))

    def _deliver(self) -> None:
        if self._error is not None:
            self._on_error(*self._error)
        else:
            self._on_success(self._result)




def _run_background_load(parent, work, on_success, on_error, *,
                         title: str, label: str) -> None:
    """Run ``work()`` (a blocking, Qt-free load) on a worker thread behind a
    modal busy dialog, then call ``on_success(result)`` or
    ``on_error(exc_type_name, message)`` back on the GUI thread.

    The dialog is application-modal with no cancel button — the load is one
    synchronous unpickle that can't be interrupted mid-flight — but the event
    loop keeps running, so the window stays responsive (marquee animating)
    instead of freezing for the 5 s–1 min a large solution pickle takes.

    Callers that must not overlap another load are responsible for checking
    :func:`_reject_if_background_load_running` *before* calling this — the
    check must NOT live here, because :meth:`PdnViewer._ensure_caps_rows`
    relies on its continuation always running (a dropped continuation leaves
    ``_caps_rows_pending`` stuck and the Capacitors tab permanently blank)."""
    dlg = QProgressDialog(label, "", 0, 0, parent)
    dlg.setWindowTitle(title)
    dlg.setCancelButton(None)
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)

    def _cleanup() -> None:
        dlg.close()
        dlg.deleteLater()
        _BACKGROUND_LOADERS.discard(worker)
        worker.deleteLater()  # safe: finished has fired before _deliver runs

    def _ok(result) -> None:
        # Tear the dialog down before the continuation, which may itself open
        # windows / run more blocking GUI-thread work (e.g. PdnViewer.__init__).
        _cleanup()
        on_success(result)

    def _fail(exc_type: str, message: str) -> None:
        _cleanup()
        on_error(exc_type, message)

    worker = _BackgroundLoadWorker(work, _ok, _fail, parent)
    _BACKGROUND_LOADERS.add(worker)
    dlg.show()
    worker.start()




def _any_gerber_import_running() -> bool:
    """True if any orphaned Gerber-import worker is still running (its
    background render pool is still burning cores). Used to refuse launching a
    second import that would over-subscribe the CPU."""
    from fypa.viewer.gerber_import import _GerberImportWorker
    for w in list(_ORPHANED_THREADS):
        if not isinstance(w, _GerberImportWorker):
            continue
        try:
            if w.isRunning():
                return True
        except RuntimeError:
            _ORPHANED_THREADS.discard(w)
    return False




def _any_solve_running(owner) -> bool:
    """True if *owner* has an in-flight :class:`_SolveWorker`."""
    worker = getattr(owner, "_solve_worker", None)
    if worker is None:
        return False
    try:
        return worker.isRunning()
    except RuntimeError:
        return False




def _reject_if_solve_running(owner, *, title: str = "Import already running") -> bool:
    """Show a modal and return True when an Altium import/solve is busy."""
    if not _any_solve_running(owner):
        return False
    QMessageBox.information(
        owner, title,
        "An Altium import or solve is already in progress. "
        "Please wait for it to finish before starting another.",
    )
    return True




def _reject_if_background_load_running(
    owner, *, title: str = "Load already running",
) -> bool:
    """Show a modal and return True when a pickle load is already in flight."""
    for worker in list(_BACKGROUND_LOADERS):
        try:
            if worker.isRunning():
                QMessageBox.information(
                    owner, title,
                    "A project or solution is still loading in the "
                    "background. Please wait for it to finish before "
                    "starting another.",
                )
                return True
        except RuntimeError:
            _BACKGROUND_LOADERS.discard(worker)
    return False




def _viewer_has_unsaved_changes(viewer) -> bool:
    """True when *viewer* is a :class:`PdnViewer` with unsaved edits."""
    if not hasattr(viewer, "_apply_solve_result"):
        return False
    return bool(
        getattr(viewer, "_project_dirty", False)
        or getattr(viewer, "_display_dirty", False)
        or getattr(viewer, "_settings_dirty", False)
        or getattr(viewer, "_solved_since_save", False)
    )




def _confirm_replace_project(viewer) -> bool:
    """Ask before replacing the loaded project in-place.

    Returns ``True`` when the caller should proceed (no unsaved state, user
    chose Discard, or Save succeeded). ``False`` on Cancel or a failed save."""
    from fypa.viewer.file_menu import _ProjectSaveDialog
    if not _viewer_has_unsaved_changes(viewer):
        return True
    box = QMessageBox(viewer)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Replace current project?")
    box.setText(
        "The current project has unsaved changes.\n"
        "Save before opening another project?"
    )
    save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
    discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
    cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(save_btn)
    box.exec()
    clicked = box.clickedButton()
    if clicked == cancel_btn:
        return False
    if clicked == discard_btn:
        return True
    if clicked == save_btn:
        dlg = _ProjectSaveDialog(
            viewer, allow_all=getattr(viewer, "_solved_since_save", False),
        )
        if dlg.exec() != QDialog.Accepted or dlg.choice is None:
            return False
        if dlg.choice == "all" and getattr(viewer, "_solved_since_save", False):
            return bool(viewer._save_project_and_solution())
        return bool(viewer._save_project())
    return False




def _drop_failed_fypa_recent_entry(path: Path, *, from_recent: bool) -> None:
    """Remove a stale ``.fypa`` recent entry after a failed open."""
    if from_recent:
        _remove_recent_project_entry({"kind": "fypa", "path": str(path)})




def _stash_pending_altium_recent(
    window, prjpcb_path: Path, pcbdoc_path: Path | None,
    *, from_recent: bool = False,
) -> None:
    """Remember which Altium project is being opened.

    ``from_recent`` gates whether a later solve failure should drop the
    matching File > Recent Projects entry (menu imports must not)."""
    prjpcb_s = _normalize_recent_path(str(prjpcb_path)) or str(prjpcb_path)
    entry: dict[str, str] = {
        "kind": "altium", "prjpcb_path": prjpcb_s,
    }
    if pcbdoc_path is not None:
        pcbdoc_s = _normalize_recent_path(str(pcbdoc_path))
        if pcbdoc_s:
            entry["pcbdoc_path"] = pcbdoc_s
    window._pending_altium_recent_entry = entry
    window._pending_altium_from_recent = bool(from_recent)




def _clear_pending_altium_recent(window) -> None:
    if hasattr(window, "_pending_altium_recent_entry"):
        window._pending_altium_recent_entry = None
    if hasattr(window, "_pending_altium_from_recent"):
        window._pending_altium_from_recent = False




def _drop_pending_altium_recent(window) -> None:
    if not getattr(window, "_pending_altium_from_recent", False):
        _clear_pending_altium_recent(window)
        return
    pending = getattr(window, "_pending_altium_recent_entry", None)
    if pending:
        _remove_recent_project_entry(pending)
    _clear_pending_altium_recent(window)




def _register_viewer(win: QMainWindow) -> None:
    """Append ``win`` to the module-level strong-ref list, pruning any
    entries whose underlying QObject has already been destroyed."""
    global _LIVE_VIEWERS
    pruned: list[QMainWindow] = []
    for w in _LIVE_VIEWERS:
        try:
            w.objectName()      # raises if the C++ object is gone
        except RuntimeError:
            continue
        pruned.append(w)
    pruned.append(win)
    _LIVE_VIEWERS = pruned




def _retire_viewer(win: QMainWindow) -> None:
    """Close, unregister and destroy a viewer that's being replaced on a
    reload, releasing its Solution / mesh data right away.

    Without this every reload leaks: ``_register_viewer`` only prunes
    viewers whose C++ object is *already* destroyed, and a plain
    ``close()`` merely hides the window — so the previous PdnViewer stays
    pinned in ``_LIVE_VIEWERS`` with its full (gigabyte-scale, on a large
    board) ``Solution`` still in RAM. A long session then accumulates one
    Solution per load until the next solve runs short of memory and PARDISO
    pages to disk — which is what turns an ~8 s linear solve into ~50 s.
    """
    global _LIVE_VIEWERS
    _LIVE_VIEWERS = [w for w in _LIVE_VIEWERS if w is not win]
    try:
        win.close()
    except RuntimeError:
        return  # C++ object already gone — nothing left to release.
    # Drop the heavy payload explicitly so it's freed now, rather than
    # whenever the window wrapper happens to be garbage-collected.
    for attr in ("solution", "metadata"):
        try:
            setattr(win, attr, None)
        except Exception:
            pass
    # Free the process-global solver caches too. These are freed on solve-cancel
    # but otherwise live for the process — the mesh + Laplacian assembly is
    # hundreds of MB and the PARDISO factorisation can be gigabytes. Retiring a
    # viewer means a full reload/replacement (value-only re-solves refresh in
    # place and keep the caches warm), so the cached assembly/factorisation for
    # the outgoing board is dead weight; drop it now. The next solve re-builds.
    try:
        from pdnsolver.solver import (
            free_mesh_assembly_cache, free_pardiso_cache,
        )
        free_pardiso_cache()
        free_mesh_assembly_cache()
    except Exception:
        pass
    win.deleteLater()
