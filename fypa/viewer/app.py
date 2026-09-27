"""Application entry point."""
from __future__ import annotations

import logging
import sys
from PySide6.QtWidgets import QApplication

from fypa.viewer.assets import (
    _force_native_window_icon,
    _load_app_icon,
    _set_window_aumid,
    _set_windows_app_user_model_id,
)
from fypa.viewer.diagnostics import _maybe_show_mesh_failures
from fypa.viewer.launcher import LauncherWindow
from fypa.viewer.prefs import _apply_performance_prefs
from fypa.viewer.project_open import _schedule_cli_altium_import
from fypa.viewer.theme import apply_app_theme, load_saved_theme_mode
from fypa.viewer.window import PdnViewer


def main(solution, warnings_list=None, metadata=None,
         gerber_import_target=None, altium_import_target=None) -> int:
    """CLI entry — show the viewer for the given Solution and run the Qt
    event loop. Returns the QApplication exit code.

    If ``solution is None``, opens an empty :class:`LauncherWindow` instead
    so the user can pick a project / pickle from the File menu.

    ``gerber_import_target`` (CLI ``FYPA gerber-gui`` path): when not None,
    the launcher window opens, then immediately triggers the Gerber-import
    flow. Pass a folder Path or a saved ``.fypa`` Path; pass any non-None
    value to open the file picker without pre-selection.

    ``altium_import_target`` (CLI ``FYPA gui <PrjPcb>``): dict with at least
    ``prjpcb_path`` and a resolved ``pcbdoc_path`` (silent first-board /
    ``--pcbdoc`` selection — no multi-board picker). Optional ``clean`` and
    mesh overrides (only when the user passed ``--mesh-*``). Starts the same
    File > Import Altium Design worker path after the launcher is shown.
    """
    # Route Python warnings.warn() (e.g. padne's SolverWarning) into the
    # logging system for the whole process. Set once here at startup rather
    # than per-solve: it's global process state, and toggling it from the
    # solve-worker thread on every solve is a needless mutation of shared
    # state that was never reset.
    logging.captureWarnings(True)
    # Windows taskbar grouping — must happen BEFORE any window is shown.
    _set_windows_app_user_model_id()
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication(sys.argv)
    # Theme — load the persisted choice (default: dark) and apply the
    # matching palette + base stylesheet to the QApplication BEFORE any
    # window is constructed, so the side panel, menubar and dialogs
    # follow the theme on every machine regardless of system palette.
    apply_app_theme(app, load_saved_theme_mode())
    # Apply persisted Performance prefs (fuse backend, mesh workers, solver
    # timeout) to os.environ + pdnsolver before any solve can run.
    _apply_performance_prefs()
    # Application-wide icon (covers Qt's WM_SETICON path).
    icon = _load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    if solution is None:
        win = LauncherWindow()
        if gerber_import_target is not None:
            # Defer the dialog one event-loop tick so the launcher window
            # has a chance to lay out + show before the modal file picker
            # pops over it.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, win._on_menu_import_gerber)
        elif altium_import_target is not None:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(
                0,
                lambda: _schedule_cli_altium_import(win, altium_import_target),
            )
    else:
        win = PdnViewer(solution, metadata=metadata)
    win.show()
    # Belt and braces: also push the .ico directly via WM_SETICON in
    # case Qt's icon path doesn't reach the taskbar, and bind our AUMID
    # to the window so Windows uses our icon for the taskbar grouping.
    _force_native_window_icon(win)
    _set_window_aumid(win)
    # ``show`` / a direct solution open: still pop the mesh-failure notice
    # (launcher import uses ``_on_solve_finished`` instead).
    if solution is not None and (metadata or {}).get("mesh_failed"):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(
            0, lambda: _maybe_show_mesh_failures(win, metadata),
        )
    if owns_app:
        code = app.exec()
        # `FYPA gui <PrjPcb>` used to raise SystemExit(1) from _solve_loaded on
        # a failed load/solve. That now happens on a worker thread inside the
        # event loop, so without this the command exits 0 and no scripted
        # caller (Run_FYPA.pas, CI) can tell success from failure.
        if code == 0 and getattr(win, "_cli_import_failed", False):
            return 1
        return code
    return 0
