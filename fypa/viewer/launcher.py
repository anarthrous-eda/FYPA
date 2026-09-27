"""The launcher window shown before a project is open."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.app_menus import _build_help_menu, _build_recent_projects_menu
from fypa.viewer.assets import (
    _force_native_window_icon,
    _load_app_icon,
    _load_fypa_fangs_pixmap,
    _load_fypa_text_pixmap,
    _set_window_aumid,
)
from fypa.viewer.diagnostics import (
    _maybe_show_annotation_errors,
    _maybe_show_mesh_failures,
    _maybe_warn_connectivity_breaks,
    _maybe_warn_needs_directives,
    _maybe_warn_open_loop_rails,
    _maybe_warn_unannotated_bridges,
)
from fypa.viewer.display import _DISPLAY_PERCENTILE_HIGH, _VIA_CURRENT_WARN_A
from fypa.viewer.gerber_import import _GerberImportWorker, _pick_gerber_inputs
from fypa.viewer.prefs import _record_recent_altium_from_metadata, load_auto_solve_on_import
from fypa.viewer.project_open import (
    _altium_import_auto_solve_status_tip,
    _altium_import_clean_status_tip,
    _headless_platform,
    _open_altium_project_at,
    _open_project_file_at,
    _open_solution_at,
)
from fypa.viewer.session import (
    _any_gerber_import_running,
    _clear_pending_altium_recent,
    _drop_pending_altium_recent,
    _register_viewer,
    _retire_thread,
)
from fypa.viewer.settings_tab import _SettingsTabMixin
from fypa.viewer.solve_worker import _abort_solve_worker, _SolveProgressUpdater
from fypa.viewer.theme import _file_dialog_options, _T, current_theme_mode
from fypa.viewer.widgets import _esc
from fypa.viewer.window import PdnViewer

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fypa.viewer.solve_worker import _SolveWorker


class LauncherWindow(_SettingsTabMixin, QMainWindow):
    """Minimal launcher window shown when FYPA is invoked with no project.

    Has just a File menu (Import Altium Design / Open Project File /
    Load Solution / Exit) and a centred
    welcome label. Picking a project runs the same solve worker the main
    viewer uses; on success, a real :class:`PdnViewer` opens and this
    launcher closes itself.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FYPA")
        icon = _load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(720, 520)

        # --- State the shared Settings-tab builder (_SettingsTabMixin) reads.
        # The launcher has no loaded design, so these are placeholders /
        # defaults; ``_settings_launcher_mode`` makes the builder skip the
        # design-only sections (Project/PcbDoc labels, stackup, Re-run /
        # Reload actions) and treat the physics fields as session defaults.
        from fypa.altium.loader import SolveSettings as _SolveSettings
        self._settings_launcher_mode = True
        self._solve_settings = _SolveSettings()
        self.metadata = None
        self._loaded_project = None
        self._gl_viewer = None
        self._via_current_warn_a = _VIA_CURRENT_WARN_A
        self._display_percentile_high = _DISPLAY_PERCENTILE_HIGH
        self._settings_dirty = False

        # Two tabs: the welcome screen (Home) and a Settings tab that mirrors
        # the design-loaded viewer's Settings tab minus its design-only
        # sections (see _SettingsTabMixin._build_settings_tab, launcher mode).
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_home_tab(), "Home")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.setCentralWidget(self.tabs)

        self._build_menubar()

        # Held across the async solve so Qt + Python keep them alive.
        self._solve_worker: _SolveWorker | None = None
        self._solve_progress_dlg: QProgressDialog | None = None
        self._solve_progress_updater: _SolveProgressUpdater | None = None

    def _build_home_tab(self) -> QWidget:
        """Build the Home (welcome) tab. Rebuilt wholesale on a theme change
        (see :meth:`_refresh_inline_theme`) so its inline-styled label, logo
        and background pick up the new colours — hence the auto-solve
        checkbox is created *and wired* here, not in ``_build_menubar``."""
        t = _T()
        centre = QWidget(self)
        layout = QVBoxLayout(centre)
        layout.setContentsMargins(40, 40, 40, 40)
        # Use clickable <a> links (with linkActivated) so the user can
        # launch each File-menu action from this welcome screen too.
        link_style = (
            f"color:{t['accent']}; text-decoration:underline; font-weight:600;"
        )
        label = QLabel(
            "<div style='text-align:center;'>"
            f"<h2 style='color:{t['fg']}; margin-top:2px; margin-bottom:0;'>Altium / Gerber PDN Analyser</h2>"
            f"<p style='color:{t['accent']};'>No project loaded.</p>"
            f"<p style='color:{t['fg_dim']};'>"
            f"<a href='open-project' style='{link_style}'>Import Altium Design&hellip;</a>"
            " (Ctrl+O) to pick a <code>.PrjPcb</code>,<br>"
            f"<a href='open-project-clean' style='{link_style}'>Import Altium Design (Clean)&hellip;</a>"
            " (Ctrl+Shift+L) to force a fresh extract + solve,<br>"
            f"<a href='import-gerber' style='{link_style}'>Import Gerber Files&hellip;</a>"
            " (Ctrl+G) to import Gerber (RS-274X) + Excellon drill files,<br>"
            "or "
            f"<a href='open-solution' style='{link_style}'>Load Solution&hellip;</a>"
            " (Ctrl+Shift+O) to load a saved <code>.pkl</code>."
            "</p>"
            f"<p style='color:{t['fg_dim']}; font-size:smaller; font-style:italic;'>"
            "FYPA is a design-aid tool. Treat its results as guidance "
            "and validate against measurement."
            "</p>"
            "</div>"
        )
        label.setTextFormat(Qt.RichText)
        label.setAlignment(Qt.AlignCenter)
        # Handle the link clicks ourselves and dispatch to the existing
        # menu handlers — don't bounce out to a system browser.
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(
            Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard
        )
        label.linkActivated.connect(self._on_welcome_link)
        layout.setSpacing(0)
        layout.addStretch(1)
        fangs_pm = _load_fypa_fangs_pixmap(256)
        if fangs_pm is not None:
            fangs_label = QLabel()
            fangs_label.setPixmap(fangs_pm)
            fangs_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(fangs_label)
        text_pm = _load_fypa_text_pixmap(50)
        if text_pm is not None:
            text_label = QLabel()
            text_label.setPixmap(text_pm)
            text_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(text_label)
        layout.addWidget(label)
        # "Solve automatically on Altium import" now lives on the Settings tab
        # (General options); the import path reads the persisted pref directly.
        layout.addStretch(1)
        centre.setStyleSheet(f"background-color: {t['bg']};")
        return centre

    def _refresh_inline_theme(self) -> None:
        """Launcher-side theme refresh, invoked by the shared Settings-tab
        theme picker (:meth:`_SettingsTabMixin._on_theme_combo_changed`).

        Rebuilds both launcher tabs so their inline-styled widgets and pinned
        backgrounds repaint in the new theme (the app-level palette + base
        stylesheet were already swapped by ``_on_theme_combo_changed``). The
        Settings tab is rebuilt last so its theme picker / status label are
        the ones left standing for the confirmation message below.
        """
        tabs = getattr(self, "tabs", None)
        if tabs is None:
            return
        keep_current = tabs.currentIndex()
        builders = {"Home": self._build_home_tab,
                    "Settings": self._build_settings_tab}
        for i in range(tabs.count()):
            builder = builders.get(tabs.tabText(i))
            if builder is None:
                continue
            title = tabs.tabText(i)
            old = tabs.widget(i)
            tabs.removeTab(i)
            if old is not None:
                old.setParent(None)
                old.deleteLater()
            tabs.insertTab(i, builder(), title)
        tabs.setCurrentIndex(keep_current)
        status = getattr(self, "_theme_status_label", None)
        if status is not None:
            t = _T()
            status.setText(
                f"<span style='color:{t['ok']};'>"
                f"Theme set to <b>{_esc(current_theme_mode().capitalize())}</b>."
                "</span>"
            )

    def _on_welcome_link(self, href: str) -> None:
        """Dispatch a click on one of the welcome-label hyperlinks to the
        matching File-menu handler."""
        if href == "open-project":
            self._on_menu_open_project(clean=False)
        elif href == "open-project-clean":
            self._on_menu_open_project(clean=True)
        elif href == "import-gerber":
            self._on_menu_import_gerber()
        elif href == "open-solution":
            self._on_menu_open_solution()

    def _build_menubar(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")

        open_proj = QAction("&Import Altium Design…", self)
        open_proj.setShortcut(QKeySequence.Open)  # Ctrl+O
        open_proj.setStatusTip(
            _altium_import_auto_solve_status_tip(
                load_auto_solve_on_import(),
            )
        )
        open_proj.triggered.connect(self._on_menu_open_project)
        file_menu.addAction(open_proj)
        self._act_import_altium = open_proj

        open_proj_clean = QAction("Import Altium Design (&Clean)…", self)
        open_proj_clean.setShortcut("Ctrl+Shift+L")
        open_proj_clean.setStatusTip(
            _altium_import_clean_status_tip(load_auto_solve_on_import()),
        )
        open_proj_clean.triggered.connect(
            lambda: self._on_menu_open_project(clean=True)
        )
        file_menu.addAction(open_proj_clean)

        # "Solve automatically on import" is no longer a File-menu item — it's
        # the Settings > General options entry; the import path reads the
        # persisted pref (load_auto_solve_on_import) directly.

        open_gerber = QAction("Import &Gerber Files…", self)
        open_gerber.setShortcut("Ctrl+G")
        open_gerber.setStatusTip(
            "Pick a set of Gerber (RS-274X) + Excellon drill files; "
            "FYPA imports them and opens the editor so you can add "
            "sources / sinks."
        )
        open_gerber.triggered.connect(self._on_menu_import_gerber)
        file_menu.addAction(open_gerber)

        open_projfile = QAction("Open &Project File…", self)
        open_projfile.setShortcut("Ctrl+Shift+P")
        open_projfile.setStatusTip(
            "Open a .fypa project file — its linked solution plus any "
            "editor-mode changes."
        )
        open_projfile.triggered.connect(self._on_menu_open_project_file)
        file_menu.addAction(open_projfile)

        _build_recent_projects_menu(file_menu, self)

        file_menu.addSeparator()

        open_sol = QAction("&Load Solution…", self)
        open_sol.setShortcut("Ctrl+Shift+O")
        open_sol.setStatusTip(
            "Open a previously-saved solution pickle (no re-solve)."
        )
        open_sol.triggered.connect(self._on_menu_open_solution)
        file_menu.addAction(open_sol)

        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut(QKeySequence.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        _build_help_menu(self)

    def _on_menu_import_gerber(self) -> None:
        """File > Import Gerber Files…  —  picks Gerber + drill files,
        runs the layer-mapping + stackup dialogs, then hands the heavy
        per-layer render + geometry build off to a background
        :class:`_GerberImportWorker` (so Windows doesn't flag the app
        as not responding). A modal :class:`QProgressDialog` shows the
        per-stage progress; on completion, the resulting viewer opens
        and the launcher closes."""
        if _any_gerber_import_running():
            QMessageBox.information(
                self, "Import already running",
                "A previous Gerber import is still finishing in the "
                "background. Please wait a moment before starting another.",
            )
            return
        picked = _pick_gerber_inputs(self)
        if picked is None:
            return
        result, pseudo = picked
        dlg = QProgressDialog(
            "Starting Gerber import…", "Cancel", 0, 0, self,
        )
        dlg.setWindowTitle("Importing Gerber files")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        dlg.show()
        QApplication.processEvents()
        _sz = dlg.size()
        # 2.5x Qt's default width — Gerber import stage labels are longer
        # than the solve dialog's ("Rendering 16 Gerber layers…", etc.)
        # and Qt's auto-sized width clips them.
        dlg.setFixedSize(int(_sz.width() * 2.5), _sz.height())

        worker = _GerberImportWorker(result, pseudo, parent=self)
        updater = _SolveProgressUpdater(dlg, worker, self)
        self._gerber_worker = worker
        self._gerber_dlg = dlg
        self._gerber_updater = updater

        def _cleanup() -> None:
            try:
                updater.stop()
                updater.deleteLater()
            except Exception:
                pass
            try:
                dlg.canceled.disconnect()
            except (RuntimeError, TypeError):
                pass
            dlg.close()
            # close() only hides the parented dialog — delete it so imports
            # don't leak one QProgressDialog each.
            dlg.deleteLater()
            # The worker may still be inside the non-interruptible ProcessPool
            # render phase; retire it safely rather than deleteLater()-ing a
            # running QThread (which would qFatal the app).
            _retire_thread(worker)
            self._gerber_worker = None
            self._gerber_dlg = None
            self._gerber_updater = None

        def _on_ok(res) -> None:
            stub_solution, metadata, loaded, pf = res
            _cleanup()
            self._open_viewer_and_close(
                stub_solution, metadata,
                loaded_project=loaded, project=pf,
            )

        def _on_fail(msg: str) -> None:
            _cleanup()
            QMessageBox.critical(
                self, "Gerber import failed",
                f"Could not finish the Gerber import:\n\n{msg}",
            )

        def _on_cancel() -> None:
            # Ask the worker to unwind at its next stage boundary (the
            # pure-Python packaging phase honours isInterruptionRequested()).
            # The ProcessPoolExecutor render phase can't be interrupted
            # cooperatively, so also drop the worker's signals: whether it
            # stops early or runs to completion, its result is discarded and
            # the dialog closes immediately for the user.
            if self._gerber_worker is not None:
                self._gerber_worker.requestInterruption()
                # Tear down the render pool so queued layers stop instead of
                # running to completion as orphans burning every core.
                try:
                    from fypa.gerber.extract import cancel_active_gerber_pool
                    cancel_active_gerber_pool()
                except Exception:
                    pass
                try:
                    self._gerber_worker.finished_ok.disconnect(_on_ok)
                    self._gerber_worker.failed.disconnect(_on_fail)
                except (RuntimeError, TypeError):
                    pass
            _cleanup()

        worker.finished_ok.connect(_on_ok)
        worker.failed.connect(_on_fail)
        dlg.canceled.connect(_on_cancel)
        worker.start()

    def _on_menu_open_project(self, *, clean: bool = False) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import Altium project (clean)" if clean else "Import Altium project",
            "",
            "Altium project (*.PrjPcb);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return
        _open_altium_project_at(self, Path(path_str), None, clean=clean)

    def _on_solve_cancelled(self) -> None:
        """User clicked Cancel on the solve progress dialog. Forcibly kill
        the worker — the launcher is already the "home" state, so just stay
        here once the dialog is gone."""
        _clear_pending_altium_recent(self)
        _abort_solve_worker(self)

    def _cleanup_solve_worker(self) -> None:
        if self._solve_progress_updater is not None:
            self._solve_progress_updater.stop()
            self._solve_progress_updater.deleteLater()
            self._solve_progress_updater = None
        if self._solve_progress_dlg is not None:
            # QProgressDialog.close() routes through reject() → cancel(),
            # which emits canceled — and our canceled handler tears down
            # the in-flight solve. Drop the connection first so closing a
            # dialog whose worker finished naturally doesn't get treated
            # as a user cancel.
            try:
                self._solve_progress_dlg.canceled.disconnect(self._on_solve_cancelled)
            except (RuntimeError, TypeError):
                pass
            self._solve_progress_dlg.close()
            self._solve_progress_dlg = None
        if self._solve_worker is not None:
            self._solve_worker.deleteLater()
            self._solve_worker = None

    def _on_solve_failed(self, message: str) -> None:
        logging.getLogger(__name__).error("Solve failed: %s", message)
        _drop_pending_altium_recent(self)
        if getattr(self, "_cli_import_pending", False):
            # Drives main()'s exit code — see the tail of main().
            self._cli_import_failed = True
            if _headless_platform():
                # A modal here has no one to dismiss it: a CI job running
                # `FYPA gui` under the offscreen platform would block forever.
                # The message is already on stderr via the log above.
                self.close()
                return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Solve failed")
        lines = message.splitlines()
        if len(lines) > 10 or len(message) > 500:
            box.setText(lines[0] if lines else "Solve failed")
            box.setDetailedText(message)
        else:
            box.setText(message)
        box.exec()

    def _on_solve_finished(self, new_solution, metadata: dict,
                           loaded_project, new_settings) -> None:
        new_win = self._open_viewer_and_close(
            new_solution, metadata,
            initial_settings=new_settings,
            loaded_project=loaded_project,
        )
        if new_win is not None:
            _record_recent_altium_from_metadata(metadata)
        _clear_pending_altium_recent(self)
        # An Altium project with no SOURCE/REGULATOR loads as an editor-mode
        # stub instead of failing — let the user know it's set up for manual
        # marker placement rather than a finished solve.
        _maybe_warn_needs_directives(new_win or self, new_solution)
        _maybe_show_annotation_errors(new_win or self, metadata)
        _maybe_show_mesh_failures(new_win or self, metadata)
        # Rails with only sources or only sinks were skipped — tell the user.
        _maybe_warn_open_loop_rails(new_win or self, metadata)
        # Nets whose source & sink landed on disconnected copper — tell the user.
        _maybe_warn_connectivity_breaks(new_win or self, metadata)
        # Parts joining a solved rail to copper outside the FEM — advisory.
        _maybe_warn_unannotated_bridges(new_win or self, metadata)

    def _on_menu_open_solution(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open solution pickle", "",
            "Solution pickle (*.pkl);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return
        _open_solution_at(self, Path(path_str))

    def _on_menu_open_project_file(self) -> None:
        """File > Open Project File… → load a ``.fypa`` and open a viewer
        bound to it (its linked solution + editor directives)."""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open project file", "",
            "FYPA project (*.fypa);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return
        _open_project_file_at(self, Path(path_str))

    def _open_viewer_and_close(self, solution, metadata: dict | None,
                               *, initial_settings=None,
                               loaded_project=None,
                               project=None,
                               project_path=None) -> PdnViewer | None:
        try:
            kwargs = {"metadata": metadata}
            if initial_settings is not None:
                kwargs["initial_settings"] = initial_settings
            if loaded_project is not None:
                kwargs["loaded_project"] = loaded_project
            if project is not None:
                kwargs["project"] = project
            if project_path is not None:
                kwargs["project_path"] = project_path
            _t = time.monotonic()
            new_win = PdnViewer(solution, **kwargs)
            logging.getLogger(__name__).info(
                "PdnViewer __init__ took %.2fs", time.monotonic() - _t,
            )
        except Exception as e:
            logging.getLogger(__name__).exception("Failed to open viewer")
            QMessageBox.critical(
                self, "Couldn't open viewer",
                f"Solution loaded but the viewer failed to open:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return None
        if project is not None and getattr(project, "editor_directives", None):
            new_win._set_solve_stale(True)
        elif (loaded_project is not None
              and bool(getattr(solution, "solver_info", {}).get("stub"))):
            new_win._mark_awaiting_first_solve()
        _register_viewer(new_win)
        app = QApplication.instance()
        # The new viewer's GL widget show() is processed asynchronously,
        # so there's a one-tick window where visible_windows == 0 if we
        # hide/close the launcher synchronously — that trips
        # quitOnLastWindowClosed and the whole process exits. Disable
        # the auto-quit, hide the launcher, then restore the flag on the
        # next event-loop tick (by which time new_win is fully shown).
        prev_quit = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        new_win.show()
        _force_native_window_icon(new_win)
        _set_window_aumid(new_win)
        self.hide()
        QTimer.singleShot(
            0, lambda: app.setQuitOnLastWindowClosed(prev_quit)
        )
        return new_win
