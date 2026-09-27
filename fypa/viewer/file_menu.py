"""File menu, and project file save / open."""
from __future__ import annotations

import logging
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

from fypa.viewer.assets import _force_native_window_icon, _set_window_aumid
from fypa.viewer.prefs import _record_recent_altium_from_metadata, load_auto_solve_on_import
from fypa.viewer.session import (
    _any_gerber_import_running,
    _clear_pending_altium_recent,
    _drop_pending_altium_recent,
    _register_viewer,
    _reject_if_background_load_running,
    _reject_if_solve_running,
    _retire_thread,
    _retire_viewer,
    _stash_pending_altium_recent,
)
from fypa.viewer.solve_worker import _abort_solve_worker, _SolveProgressUpdater, _SolveWorker
from fypa.viewer.theme import _file_dialog_options, _T
from fypa.viewer.widgets import _esc


class _ProjectSaveDialog(QDialog):
    """The Ctrl+S popup. Two choices: save the project file only, or save
    the project file plus the latest solver run. ``choice`` is ``"project"``,
    ``"all"``, or ``None`` (cancelled) after :meth:`exec`.

    Keyboard: ``S`` (or a second ``Ctrl+S``) picks project-only; ``A`` picks
    save-all when it's available, falling back to project-only otherwise.
    """

    def __init__(self, parent, allow_all: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save project")
        self.setModal(True)
        self.choice: str | None = None
        self._allow_all = bool(allow_all)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Save the FYPA project?"))

        self._save_btn = QPushButton("Save project  (S)")
        self._save_btn.clicked.connect(lambda: self._pick("project"))
        lay.addWidget(self._save_btn)

        self._all_btn = QPushButton("Save project + latest solver run  (A)")
        self._all_btn.setEnabled(self._allow_all)
        if not self._allow_all:
            self._all_btn.setToolTip(
                "No solver run since the last save."
            )
        self._all_btn.clicked.connect(lambda: self._pick("all"))
        lay.addWidget(self._all_btn)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        lay.addWidget(cancel)
        self._save_btn.setDefault(True)

    def _pick(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key_S:          # S or Ctrl+S → project-only save
            self._pick("project")
        elif key == Qt.Key_A:        # A → save-all (or project-only if N/A)
            self._pick("all" if self._allow_all else "project")
        else:
            super().keyPressEvent(event)


class _FileMenuMixin:
    """File menu, and project file save / open."""

    # --- File menu ----------------------------------------------------------

    def _build_menubar(self) -> None:
        """File menu so the viewer can be used standalone — pick a .PrjPcb
        to solve and view, save the current solution, or open a
        previously-pickled solution."""
        from fypa.viewer.app_menus import _build_help_menu, _build_recent_projects_menu
        from fypa.viewer.project_open import (
            _altium_import_auto_solve_status_tip,
            _altium_import_clean_status_tip,
        )
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")

        open_proj = QAction("&Import Altium Design…", self)
        open_proj.setShortcut(QKeySequence.Open)         # Ctrl+O
        open_proj.setStatusTip(
            _altium_import_auto_solve_status_tip(
                load_auto_solve_on_import(),
            )
        )
        open_proj.triggered.connect(
            lambda: self._on_menu_open_project(clean=False)
        )
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

        # "Solve automatically on import" is set in Settings > General options,
        # not the File menu; the import flow reads the persisted pref directly.

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

        save_proj = QAction("Save &Project", self)
        save_proj.setShortcut("Ctrl+S")
        save_proj.setStatusTip(
            "Save the .fypa project file (editor changes, net renames, and "
            "links to the design-info / solution pickles)."
        )
        save_proj.triggered.connect(self._on_ctrl_s)
        file_menu.addAction(save_proj)

        save_proj_as = QAction("Save Project &As…", self)
        save_proj_as.setStatusTip(
            "Save the project to a new .fypa file."
        )
        save_proj_as.triggered.connect(self._on_menu_save_project_as)
        file_menu.addAction(save_proj_as)

        save_sol = QAction("&Save Solution…", self)
        save_sol.setStatusTip(
            "Save just the current solution to a .pkl file (remembers the "
            ".PrjPcb directory and selected .PcbDoc so it can be re-solved)."
        )
        save_sol.triggered.connect(self._on_menu_save_solution)
        file_menu.addAction(save_sol)

        open_sol = QAction("&Load Solution…", self)
        open_sol.setShortcut("Ctrl+Shift+O")
        open_sol.setStatusTip(
            "Open a previously-saved solution pickle (no re-solve)."
        )
        open_sol.triggered.connect(self._on_menu_open_solution)
        file_menu.addAction(open_sol)

        file_menu.addSeparator()
        export_menu = file_menu.addMenu("&Export")

        export_paraview = QAction("&ParaView…", self)
        export_paraview.setStatusTip(
            "Write the current solution as VTK files (one .vtu per copper "
            "layer, with per-vertex voltage) into a directory of your choice "
            "for opening in ParaView."
        )
        export_paraview.triggered.connect(self._on_menu_export_paraview)
        export_menu.addAction(export_paraview)

        export_report = QAction("&Report…", self)
        export_report.setShortcut("Ctrl+Shift+R")
        export_report.setStatusTip(
            "Write a design report (HTML or PDF) documenting the solved "
            "rails: an executive summary of every rail's pass / fail status, "
            "then each rail's voltage at loads, heatmaps, vias and "
            "decoupling."
        )
        export_report.triggered.connect(self._on_menu_export_report)
        export_menu.addAction(export_report)

        file_menu.addSeparator()
        close_proj = QAction("&Close Project", self)
        close_proj.setShortcut("Ctrl+W")
        close_proj.setStatusTip(
            "Close the current project and return to the launcher window"
        )
        close_proj.triggered.connect(self._on_menu_close_project)
        file_menu.addAction(close_proj)

        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut(QKeySequence.Quit)          # Ctrl+Q
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # View menu sits between File and Help.
        self._build_view_menu()
        _build_help_menu(self)

    def _build_view_menu(self) -> None:
        """View menu — the display toggles that used to be side-panel
        checkboxes (2D/3D view, via heatmap, no-current copper colour, FEM
        mesh overlay).

        Each action drives its hidden backing checkbox via ``toggle()`` so
        the rest of the viewer's logic is untouched, and carries no
        ``QShortcut`` of its own — the existing window hotkeys (see
        :meth:`_install_hotkeys`) still own the keys, and the menu labels
        merely echo them. Labels flip with state; see
        :meth:`_refresh_view_menu_labels`."""
        view_menu = self.menuBar().addMenu("&View")

        self._act_view_3d = QAction(self)
        self._act_view_3d.setStatusTip(
            "Switch between the 2D top-down heatmap and the 3D perspective "
            "view of the stacked copper layers."
        )
        self._act_view_3d.triggered.connect(
            lambda: self.view_3d_box.toggle())
        view_menu.addAction(self._act_view_3d)

        self._act_heatmap_vias = QAction(self)
        self._act_heatmap_vias.setStatusTip(
            "Colour via and plated-through-hole cylinders by the active "
            "heatmap mode instead of solid orange / grey (3D mode only)."
        )
        self._act_heatmap_vias.triggered.connect(
            lambda: self.heatmap_vias_box.toggle())
        view_menu.addAction(self._act_heatmap_vias)

        self._act_colour_stubs = QAction(self)
        self._act_colour_stubs.setStatusTip(
            "Draw copper that carries no current as flat dim grey instead "
            "of its approximate (voltage-sampled) heatmap colour."
        )
        self._act_colour_stubs.triggered.connect(
            lambda: self.colour_stubs_box.toggle())
        view_menu.addAction(self._act_colour_stubs)

        self._act_show_mesh = QAction(self)
        self._act_show_mesh.setStatusTip(
            "Overlay the FEM triangulation on the heatmap as a fine dark "
            "wireframe — useful for judging local mesh density."
        )
        self._act_show_mesh.triggered.connect(
            lambda: self.show_mesh_box.toggle())
        view_menu.addAction(self._act_show_mesh)

    def _refresh_view_menu_labels(self) -> None:
        """Update the View menu item labels to track the current display
        state. Wired to each backing checkbox's ``toggled`` signal, so it
        fires whether the toggle came from the menu, a hotkey, or a
        restored project setting."""
        if not hasattr(self, "_act_view_3d"):
            return
        self._act_view_3d.setText(
            "2D View (2)" if self.view_3d_box.isChecked()
            else "3D View (3)")
        self._act_heatmap_vias.setText(
            "Heatmap vias / PTH [enabled] (V)"
            if self.heatmap_vias_box.isChecked()
            else "Heatmap vias / PTH [disabled] (V)")
        self._act_colour_stubs.setText(
            "No current copper colour [heatmap]"
            if self.colour_stubs_box.isChecked()
            else "No current copper colour [grey]")
        self._act_show_mesh.setText(
            "Hide Mesh" if self.show_mesh_box.isChecked()
            else "Show Mesh")

    def _menu_start_dir(self) -> str:
        """Best-effort starting directory for the file dialogs: the folder
        of the currently-loaded project if we have one, else CWD."""
        if self.metadata:
            current = self.metadata.get("prjpcb_path")
            if current:
                parent = Path(current).parent
                if parent.exists():
                    return str(parent)
        return ""

    def _project_cache_dir_str(self) -> str:
        """Best-effort cache subfolder for the currently-loaded project,
        as a string suitable for QFileDialog. Empty string if we don't
        know enough to compute one — the dialog will then default to the
        user's CWD."""
        if not self.metadata:
            return ""
        prjpcb = self.metadata.get("prjpcb_path")
        pcbdoc = self.metadata.get("pcbdoc_path")
        if not prjpcb:
            return ""
        try:
            from fypa.cli import _project_cache_dir
            cache_dir = _project_cache_dir(
                Path(prjpcb), Path(pcbdoc) if pcbdoc else None,
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            return str(cache_dir)
        except Exception:
            return ""

    def _on_menu_import_gerber(self) -> None:
        """File > Import Gerber Files…  —  same flow as the launcher's
        handler, but the resulting viewer replaces the current one.

        PdnViewer doesn't share LauncherWindow's ``_open_viewer_and_close``
        helper, so we open the new viewer directly and retire this one
        (the same pattern ``_on_solve_finished`` / ``_on_resolve_finished``
        use). The heavy work runs on a :class:`_GerberImportWorker` with
        a modal progress dialog so the existing viewer stays responsive
        while the import is running."""
        from fypa.viewer.gerber_import import _GerberImportWorker, _pick_gerber_inputs
        from fypa.viewer.window import PdnViewer
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
        log = logging.getLogger(__name__)

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
            prev_geometry = self.geometry()
            prev_maximized = self.isMaximized()
            prev_fullscreen = self.isFullScreen()
            try:
                new_win = PdnViewer(
                    stub_solution,
                    metadata=metadata,
                    loaded_project=loaded,
                    project=pf,
                )
                new_win._mark_awaiting_first_solve()
                _register_viewer(new_win)
                new_win.setGeometry(prev_geometry)
                if prev_fullscreen:
                    new_win.showFullScreen()
                elif prev_maximized:
                    new_win.showMaximized()
                else:
                    new_win._pending_maximize = False
                    new_win.show()
                _force_native_window_icon(new_win)
                _set_window_aumid(new_win)
            except Exception as e:
                log.exception("Failed to open viewer after Gerber import")
                QMessageBox.critical(
                    self, "Couldn't open viewer",
                    "Gerber import succeeded but the viewer failed to "
                    f"open:\n\n{type(e).__name__}: {e}",
                )
                return
            QTimer.singleShot(0, lambda: _retire_viewer(self))

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
            # stops early or runs to completion, its result is discarded.
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
        """File > Import Altium Design[ (Clean)]  →  pick a .PrjPcb and
        load it into this viewer. Clean ignores caches; both paths honour
        the auto-solve preference."""
        from fypa.viewer.project_open import _open_altium_project_at
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import Altium project (clean)" if clean else "Import Altium project",
            self._menu_start_dir(),
            "Altium project (*.PrjPcb);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return
        _open_altium_project_at(self, Path(path_str), None, clean=clean)

    def _on_menu_save_solution(self) -> None:
        """File > Save Solution…  →  prompt for a path (defaulting to this
        project's cache folder) and write the current solution there. The
        embedded metadata still carries ``prjpcb_path`` + ``pcbdoc_path``
        so the saved file can drive a later Re-run / Reload Design Info."""
        if self.solution is None:
            QMessageBox.information(
                self, "Nothing to save",
                "There's no solution loaded in this window to save.",
            )
            return
        start_dir = self._project_cache_dir_str() or self._menu_start_dir()
        project_name = "solution"
        if self.metadata:
            prjpcb = self.metadata.get("prjpcb_path")
            if prjpcb:
                project_name = Path(prjpcb).stem
        default_name = f"{project_name}.pkl"
        default_path = str(Path(start_dir) / default_name) if start_dir else default_name
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save solution",
            default_path,
            "Solution pickle (*.pkl);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return
        try:
            from fypa.cli import save_solution_file
            save_solution_file(Path(path_str), self.solution, self.metadata)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't save solution",
                f"Failed to write {path_str}:\n\n{type(e).__name__}: {e}",
            )
            return
        label = getattr(self, "_settings_status_label", None)
        if label is not None:
            label.setText(
                f"<span style='color:{_T()['ok']};'>Saved solution to "
                f"{_esc(path_str)}</span>"
            )

    def _on_menu_export_paraview(self) -> None:
        """File > Export > ParaView…  →  prompt for an output directory and
        write one ``.vtu`` per copper layer there (per-vertex voltage scalar
        field). The files open natively in ParaView for 3-D stackup views,
        slicing, contours, etc. — see docs/user-guide/06-paraview-export.md.

        On any failure we surface a short modal and leave the full traceback
        in the Messages tab / log file (via ``logger.exception``) so the
        user has somewhere to look without us dumping a wall of text into
        the popup."""
        log = logging.getLogger(__name__)
        if self.solution is None:
            QMessageBox.information(
                self, "Nothing to export",
                "There's no solution loaded in this window to export.",
            )
            return
        start_dir = self._project_cache_dir_str() or self._menu_start_dir()
        out_dir_str = QFileDialog.getExistingDirectory(
            self, "Export to ParaView — pick output directory", start_dir,
            options=_file_dialog_options() | QFileDialog.Option.ShowDirsOnly,
        )
        if not out_dir_str:
            return
        n_files: int | None = None
        try:
            from fypa.paraview_export import export_lean_solution
            via_rows: list[dict] | None = None
            try:
                via_rows = self._get_or_compute_via_rows()
            except Exception:
                log.warning(
                    "Via report unavailable for ParaView export", exc_info=True)
            n_files = export_lean_solution(
                self.solution,
                Path(out_dir_str),
                via_rows=via_rows,
                voltage_drop_reference=self._last_drop_reference,
            )
        except Exception:
            log.exception(
                "ParaView export to %s failed", out_dir_str)
            QMessageBox.critical(
                self, "Export Failed",
                "Export Failed, see Messages tab or log file",
            )
            return
        log.info("ParaView export wrote %d VTU file(s) to %s",
                 n_files, out_dir_str)
        label = getattr(self, "_settings_status_label", None)
        if label is not None:
            label.setText(
                f"<span style='color:{_T()['ok']};'>Exported {n_files} "
                f"ParaView VTU file(s) to {_esc(out_dir_str)}</span>"
            )
        self.statusBar().showMessage(
            f"Exported {n_files} ParaView VTU file(s) to {out_dir_str}", 5000)
        QMessageBox.information(
            self, "Export Successful",
            f"Export Successful — wrote {n_files} VTU file(s) to:\n\n"
            f"{out_dir_str}",
        )

    # --- Project file save / open -------------------------------------------

    def _project_pickle_paths(self) -> tuple[str | None, str | None]:
        """Best-effort ``(design_info_pkl, solve_pkl)`` cache paths for the
        loaded project. The design-info path is returned only if the file
        actually exists; the solve path is the standard cache location."""
        prjpcb = self.metadata.get("prjpcb_path") if self.metadata else None
        pcbdoc = self.metadata.get("pcbdoc_path") if self.metadata else None
        if not prjpcb:
            return None, None
        try:
            from fypa.cli import _design_info_cache_path, _solve_cache_path
            di = _design_info_cache_path(
                Path(prjpcb), Path(pcbdoc) if pcbdoc else None)
            sv = _solve_cache_path(
                Path(prjpcb), Path(pcbdoc) if pcbdoc else None)
            return (str(di) if di.exists() else None, str(sv))
        except Exception:
            return None, None

    def _prompt_project_path(self) -> Path | None:
        """Ask the user where to write the ``.fypa`` project file."""
        start = self._menu_start_dir()
        name = "project.fypa"
        if self.metadata and self.metadata.get("prjpcb_path"):
            name = Path(self.metadata["prjpcb_path"]).stem + ".fypa"
        default = str(Path(start) / name) if start else name
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save project as", default,
            "FYPA project (*.fypa);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return None
        if not path_str.lower().endswith(".fypa"):
            path_str += ".fypa"
        return Path(path_str)

    def _store_viewer_settings(self, proj) -> None:
        """Fold persistable viewer state into ``proj.viewer_settings`` so the
        next :meth:`fypa.project_file.ProjectFile.save` writes it: the
        capacitor loop-inductance knobs and the Board Features overlay
        colours and visibility.

        Each entry is ``{"primary": [r, g, b]}``, stored in full so a
        reopened project shows exactly the colours the user left even if a
        built-in default later changes. A ``"bottom"`` key is added only
        when the user pinned a distinct Bottom-side colour for a split
        layer — i.e. the bottom colour is written only once changed."""
        # Capacitor loop-inductance knobs — persisted whenever they've been
        # materialised (reading them is what the Capacitors tab / Settings
        # group does on first use). Overlay visibility is stored even when
        # overlay colours are not yet initialised.
        caploop = getattr(self, "_caploop_settings_obj", None)
        if caploop is not None:
            proj.viewer_settings["caploop"] = caploop.to_dict()

        # No footprint_convention here: _set_footprint_convention already
        # writes it into the project's viewer_settings the moment the user
        # picks one, and this method does not clear the dict. Repeating it
        # read from self._project while writing into the PASSED proj (the
        # wrong project for any future Save-As), and stamped "auto" into
        # every saved .fypa for users who never touched the setting.

        sidebar_w = getattr(self, "_sidebar_requested_w", None)
        if isinstance(sidebar_w, int) and sidebar_w > 0:
            proj.viewer_settings["sidebar_content_w"] = int(sidebar_w)

        overlay_state = getattr(self, "_overlay_state", None)
        if overlay_state:
            proj.viewer_settings["overlay_state"] = {
                key: {
                    "split": bool(st.get("split", False)),
                    **{
                        variant: {
                            "vis": sub.get("vis"),
                            "solid": bool(sub.get("solid", False)),
                            "alpha_step": int(sub.get("alpha_step", 0)),
                        }
                        for variant, sub in st.items()
                        if variant in ("both", "top", "bottom")
                        and isinstance(sub, dict)
                    },
                }
                for key, st in overlay_state.items()
                if isinstance(st, dict)
            }

        colors = getattr(self, "_overlay_colors", None)
        if not colors:
            return
        bottoms = getattr(self, "_overlay_bottom_colors", {})

        def _enc(rgb) -> list[float]:
            return [round(float(c), 6) for c in rgb]

        out: dict[str, dict] = {}
        for key, rgb in colors.items():
            entry = {"primary": _enc(rgb)}
            bot = bottoms.get(key)
            if bot is not None:
                entry["bottom"] = _enc(bot)
            out[key] = entry
        proj.viewer_settings["overlay_colors"] = out

    def _save_project(self, path: Path | None = None) -> bool:
        """Write the ``.fypa`` project file. Falls back to a Save-As prompt
        when no path is known yet. Returns True on success."""
        proj = self._ensure_project()
        if path is None:
            path = self._project_path
        if path is None:
            path = self._prompt_project_path()
            if path is None:
                return False
        path = Path(path)
        if self.metadata:
            proj.prjpcb_path = (proj.prjpcb_path
                                or self.metadata.get("prjpcb_path"))
            proj.pcbdoc_path = (proj.pcbdoc_path
                                or self.metadata.get("pcbdoc_path"))
        self._store_viewer_settings(proj)
        # Copy the solve + design-info cache pickles into the project folder
        # so the .fypa references its own sidecars (relative paths) and the
        # folder can be shared as a unit — see _bundle_caches_beside_project.
        self._bundle_caches_beside_project(proj, path)
        try:
            proj.save(path)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't save project",
                f"Failed to write {path}:\n\n{type(e).__name__}: {e}",
            )
            return False
        self._project_path = path
        self._project_dirty = False
        self._display_dirty = False
        self.statusBar().showMessage(f"Saved project to {path}", 5000)
        return True

    def _bundle_caches_beside_project(self, proj, path: Path) -> None:
        """Copy the solve + design-info cache pickles into the ``.fypa``'s
        own folder and re-point ``proj`` at those sidecars.

        Without this, a saved project only *references* the per-machine
        solve cache (``_solve_cache_path``), whose directory is keyed to the
        original ``.PrjPcb`` location and does not exist on any other
        machine. Sharing just the ``.fypa`` then fails to open with "doesn't
        point to a readable solution pickle". Copying the pickles beside the
        ``.fypa`` (stored relative on save) makes the whole project folder
        portable.

        Best-effort: a failed copy leaves ``proj`` pointing at the original
        source so saving still succeeds (just non-portable), rather than
        aborting the save.
        """
        import shutil

        di_cache, sv_cache = self._project_pickle_paths()

        def _adopt(current: str | None, cache: str | None,
                   suffix: str) -> str | None:
            target = path.with_name(path.stem + suffix)
            src = None
            if current and Path(current).exists():
                src = Path(current)
            elif cache and Path(cache).exists():
                src = Path(cache)
            if src is None:
                return current
            try:
                if src.resolve() != target.resolve():
                    shutil.copy2(src, target)
                return str(target)
            except OSError:
                return str(src)

        proj.solve_pickle = _adopt(
            proj.solve_pickle, sv_cache, "_solve.pkl")
        # Fall back to the in-memory solution when no pickle exists on disk
        # yet (e.g. a board solved this session whose cache was cleared).
        if (not proj.solve_pickle
                or not Path(proj.solve_pickle).exists()) \
                and self.solution is not None:
            solve_target = path.with_name(path.stem + "_solve.pkl")
            try:
                from fypa.cli import save_solution_file
                save_solution_file(solve_target, self.solution, self.metadata)
                proj.solve_pickle = str(solve_target)
            except Exception:
                pass

        proj.design_info_pickle = _adopt(
            proj.design_info_pickle, di_cache, "_design-info.pkl")

    def _save_project_and_solution(self) -> bool:
        """Write the ``.fypa`` plus the current solution. The solution goes
        to a pickle beside the ``.fypa`` (not the shared design cache, whose
        solve.pkl must stay the un-edited on-disk-project solve)."""
        path = self._project_path or self._prompt_project_path()
        if path is None:
            return False
        path = Path(path)
        solve_path = path.with_name(path.stem + "_solve.pkl")
        try:
            from fypa.cli import save_solution_file
            save_solution_file(solve_path, self.solution, self.metadata)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't save solution",
                f"Failed to write {solve_path}:\n\n{type(e).__name__}: {e}",
            )
            return False
        self._ensure_project().solve_pickle = str(solve_path)
        if not self._save_project(path):
            return False
        self._solved_since_save = False
        self.statusBar().showMessage(
            f"Saved project + solution to {path}", 5000)
        return True

    def _on_ctrl_s(self) -> None:
        """Ctrl+S → the save popup. ``S`` saves the project only; ``A``
        also writes the latest solver run (when there is an unsaved one)."""
        dlg = _ProjectSaveDialog(self, allow_all=self._solved_since_save)
        if dlg.exec() != QDialog.Accepted or dlg.choice is None:
            return
        if dlg.choice == "all" and self._solved_since_save:
            self._save_project_and_solution()
        else:
            self._save_project()

    def _on_menu_save_project_as(self) -> None:
        """File > Save Project As… → pick a new ``.fypa`` path and save."""
        path = self._prompt_project_path()
        if path is not None:
            self._save_project(path)

    def _on_menu_open_project_file(self) -> None:
        """File > Open Project File… → load a ``.fypa`` and open a viewer
        bound to it (its linked solution + editor directives)."""
        from fypa.viewer.project_open import _open_project_file_at
        start = self._menu_start_dir()
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open project file", start,
            "FYPA project (*.fypa);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return
        _open_project_file_at(self, Path(path_str))

    def _on_menu_open_solution(self) -> None:
        """File > Load Solution…  →  load a pickled LeanSolution + metadata
        in place (no re-solve)."""
        from fypa.viewer.project_open import _open_solution_at
        start_dir = self._project_cache_dir_str() or self._menu_start_dir()
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open solution pickle",
            start_dir,
            "Solution pickle (*.pkl);;All files (*)",
            options=_file_dialog_options(),
        )
        if not path_str:
            return
        _open_solution_at(self, Path(path_str))

    def _on_menu_close_project(self) -> None:
        """File > Close Project  →  open a fresh launcher window and close
        this viewer. Uses the same quitOnLastWindowClosed dance as the
        launcher's _open_viewer_and_close: there's a one-tick window where
        no window is visible, which would otherwise trip the auto-quit and
        kill the whole process."""
        from fypa.viewer.launcher import LauncherWindow
        app = QApplication.instance()
        launcher = LauncherWindow()
        _register_viewer(launcher)
        prev_quit = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        launcher.show()
        _force_native_window_icon(launcher)
        _set_window_aumid(launcher)
        self.close()
        QTimer.singleShot(
            0, lambda: app.setQuitOnLastWindowClosed(prev_quit)
        )


    def _on_rerun_solver(self) -> None:
        """Re-solve the current project with the values in the Settings
        tab. The actual solve runs on a :class:`_SolveWorker` QThread so
        the UI stays responsive and the progress dialog can spin; the
        heatmap refreshes in place via :meth:`_apply_solve_result` once the
        worker emits its result."""
        # 1. Validate the project path is still in metadata + on disk.
        prjpcb_str = ""
        if self.metadata:
            prjpcb_str = str(self.metadata.get("prjpcb_path") or "")
        prjpcb_path = Path(prjpcb_str) if prjpcb_str else None
        if prjpcb_path is None or not prjpcb_path.exists():
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Can't re-solve: this pickle has "
                "no project path, or the .PrjPcb is no longer on disk. Open "
                "the project via <code>FYPA.py gui &lt;.PrjPcb&gt;</code> "
                "to enable Re-run.</span>"
            )
            return

        # 2. Read the form fields into a SolveSettings + display values
        # + stackup overrides.
        try:
            (new_settings, warn_a, pct,
             stackup_overrides) = self._gather_settings_from_form()
        except ValueError as e:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Invalid input — {_esc(str(e))}</span>"
            )
            return

        # 3. Apply the new physics constants on the MAIN thread before
        # the worker starts — keeps the monkey-patch ordering unambiguous
        # (any later main-thread code looking at module constants sees
        # the new values immediately).
        new_settings.apply_to_modules()

        # 4. Lock the button so a double-click can't kick off two solves.
        self._settings_rerun_btn.setEnabled(False)
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Re-solving with new parameters…</span>"
        )

        # 5-6. Spawn the progress dialog + worker. Both the Re-run button
        # and the File > Open Project menu share this plumbing.
        # Pin the PcbDoc to whichever one this pickle was solved with so
        # re-runs of a multi-PCB project don't silently switch boards or
        # re-prompt the user.
        pcbdoc_selector = None
        if self.metadata:
            pinned = self.metadata.get("pcbdoc_path")
            if pinned:
                pcbdoc_selector = str(pinned)
        adaptive_gain = (
            getattr(self, "_adaptive_gain_check", None) is not None
            and self._adaptive_gain_check.isEnabled()
            and self._adaptive_gain_check.isChecked()
        )
        self._start_solve_worker(
            prjpcb_path, new_settings, warn_a, pct,
            stackup_overrides=stackup_overrides,
            pcbdoc_selector=pcbdoc_selector,
            dialog_title="Re-running solver",
            dialog_text=("Re-solving with new parameters…\n"
                         "This can take 10–60 s depending on board size "
                         "and mesh density."),
            adaptive_regulator_gain=adaptive_gain,
        )

    def _on_reload_design_info(self) -> None:
        """Re-extract the design (geometry + annotations) from the on-disk
        project ignoring the design-info cache, then re-solve with whatever
        is currently in the Settings tab. Used when the user has edited the
        .PrjPcb / .PcbDoc in Altium and wants FYPA to pick up the change
        without going via File > Import Altium Design (Clean).

        Pinned to the same PcbDoc this pickle was solved with, so multi-PCB
        projects don't silently switch boards or re-prompt the user."""
        prjpcb_str = ""
        if self.metadata:
            prjpcb_str = str(self.metadata.get("prjpcb_path") or "")
        prjpcb_path = Path(prjpcb_str) if prjpcb_str else None
        if prjpcb_path is None or not prjpcb_path.exists():
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Can't reload: this pickle has "
                "no project path, or the .PrjPcb is no longer on disk.</span>"
            )
            return

        try:
            (new_settings, warn_a, pct,
             stackup_overrides) = self._gather_settings_from_form()
        except ValueError as e:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Invalid input — {_esc(str(e))}</span>"
            )
            return

        new_settings.apply_to_modules()

        self._settings_rerun_btn.setEnabled(False)
        self._settings_reload_design_btn.setEnabled(False)
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Re-extracting design info and re-solving…</span>"
        )

        pcbdoc_selector = None
        if self.metadata:
            pinned = self.metadata.get("pcbdoc_path")
            if pinned:
                pcbdoc_selector = str(pinned)
        self._start_solve_worker(
            prjpcb_path, new_settings, warn_a, pct,
            stackup_overrides=stackup_overrides,
            pcbdoc_selector=pcbdoc_selector,
            use_design_cache=False,
            dialog_title="Reloading design info",
            dialog_text=("Re-extracting design info from the .PrjPcb and "
                         "re-solving…\nThis can take 10–60 s depending on "
                         "board size and mesh density."),
        )

    def _start_solve_worker(
        self, prjpcb_path: Path, settings, warn_a: float, pct: float,
        *,
        sink_overrides: dict | None = None,
        stackup_overrides: dict | None = None,
        pcbdoc_selector: str | None = None,
        use_design_cache: bool = True,
        try_solve_cache_first: bool = False,
        editor_directives: list | None = None,
        copper_names: list | None = None,
        loaded_project: object | None = None,
        is_resolve: bool | None = None,
        is_import: bool = False,
        load_only: bool = False,
        adaptive_regulator_gain: bool = False,
        dialog_title: str = "Running solver",
        dialog_text: str | None = None,
        dialog_width_scale: float = 1.44,
        pending_altium_recent_pcbdoc: Path | None = None,
        pending_altium_from_recent: bool = False,
    ) -> None:
        """Show an indeterminate progress dialog and run :class:`_SolveWorker`
        off-thread; on success, refresh this viewer in place via
        :meth:`_apply_solve_result`. Called by the Re-run button (with the
        Settings-tab form values) and by File > Import Altium Design (with
        defaults from the current viewer). Set ``use_design_cache=False`` for
        the "Clean" / Reload Design Info flows that must re-extract."""
        if _reject_if_solve_running(self):
            return
        if _reject_if_background_load_running(self):
            return
        if dialog_text is None:
            dialog_text = (f"Loading {prjpcb_path.name} and solving…\n"
                           "This can take 10–60 s depending on board size "
                           "and mesh density.")
        # Indeterminate (min == max == 0) — the C++ Triangle mesher doesn't
        # expose a progress hook, so we show a spinning barber-pole.
        dlg = QProgressDialog(dialog_text, "Cancel", 0, 0, self)
        dlg.setWindowTitle(dialog_title)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        dlg.canceled.connect(self._on_solve_cancelled)
        dlg.show()
        QApplication.processEvents()
        # 44% wider than Qt's auto-sized width so the longer per-stage
        # status messages ("Packaging solution: building metadata…",
        # "Opening viewer…", etc.) aren't truncated. Callers can widen
        # further via ``dialog_width_scale``.
        _sz = dlg.size()
        dlg.setFixedSize(int(_sz.width() * dialog_width_scale), _sz.height())

        # Stash refs on ``self`` so the QThread + dialog survive past this
        # handler returning — Qt + Python both need them alive until the
        # signals fire.
        worker = _SolveWorker(
            prjpcb_path, settings,
            sink_overrides=sink_overrides,
            stackup_overrides=stackup_overrides,
            pcbdoc_selector=pcbdoc_selector,
            use_design_cache=use_design_cache,
            try_solve_cache_first=try_solve_cache_first,
            editor_directives=editor_directives,
            copper_names=copper_names,
            loaded_project=loaded_project,
            load_only=load_only,
            adaptive_regulator_gain=adaptive_regulator_gain,
            # Bridges-tab opt-outs. Read from the live project rather than
            # passed in: every solve path (Resolve, Re-run, import) must
            # honour them, and they live with the editor directives.
            no_auto_bridge={
                d.strip().upper()
                for d in (getattr(getattr(self, "_project", None),
                                  "no_auto_bridge", []) or [])
            },
            parent=self,
        )
        if is_import:
            pcbdoc = pending_altium_recent_pcbdoc
            if pcbdoc is None and pcbdoc_selector:
                pcbdoc = Path(pcbdoc_selector)
            _stash_pending_altium_recent(
                self, prjpcb_path, pcbdoc, from_recent=pending_altium_from_recent,
            )
        self._solve_worker = worker
        self._solve_progress_dlg = dlg
        # Wires stage_changed / substage_changed to the dialog, with a
        # live elapsed-time counter for the current stage so the user can
        # see the opaque "Meshing + solving" step is still progressing.
        self._solve_progress_updater = _SolveProgressUpdater(dlg, worker, self)

        # A resolve opens the fresh viewer bound to the same project file so
        # the editor state carries over. Default heuristic: editor directives
        # present ⇒ resolve. Callers can pin it explicitly via ``is_resolve``
        # for the Settings-tab-only case (no editor directives but project
        # context still must be preserved).
        if is_resolve is None:
            is_resolve = bool(editor_directives)
        if is_resolve:
            worker.finished_ok.connect(
                lambda sol, meta, loaded: self._on_resolve_finished(
                    sol, meta, loaded, warn_a, pct, settings,
                )
            )
        else:
            worker.finished_ok.connect(
                lambda sol, meta, loaded: self._on_solve_finished(
                    sol, meta, loaded, warn_a, pct, settings,
                    is_import=is_import,
                )
            )
        worker.failed.connect(self._on_solve_failed)
        worker.finished.connect(self._cleanup_solve_worker)
        worker.start()

    def _cleanup_solve_worker(self) -> None:
        """Close the progress dialog and drop worker references.

        Fires from ``QThread.finished``, which is guaranteed regardless of
        success/failure, so this is the safe place to free both."""
        updater = getattr(self, "_solve_progress_updater", None)
        if updater is not None:
            updater.stop()
            updater.deleteLater()
            self._solve_progress_updater = None
        dlg = getattr(self, "_solve_progress_dlg", None)
        if dlg is not None:
            # QProgressDialog.close() routes through reject() → cancel(),
            # which emits canceled — and our canceled handler spawns a
            # launcher window. Drop the connection first so closing a
            # dialog whose worker finished naturally doesn't pop the
            # launcher on top of the new viewer.
            try:
                dlg.canceled.disconnect(self._on_solve_cancelled)
            except (RuntimeError, TypeError):
                pass
            dlg.close()
            # close() only hides the dialog; it stays alive parented to the
            # window, leaking one QProgressDialog per solve/import. Schedule
            # its destruction.
            dlg.deleteLater()
            self._solve_progress_dlg = None
        worker = getattr(self, "_solve_worker", None)
        if worker is not None:
            # Detach later — deleteLater is safer than direct del while
            # Qt is still emitting the finished() chain.
            worker.deleteLater()
            self._solve_worker = None

    def _on_solve_cancelled(self) -> None:
        """User clicked Cancel on the solve progress dialog. Kill the
        worker, then return to a fresh launcher window (same dance as
        File > Close Project)."""
        from fypa.viewer.launcher import LauncherWindow
        # _abort_solve_worker retires the worker via _retire_thread, which
        # reparents it off ``self`` if it couldn't be reaped (the GIL-holding
        # packaging case). That MUST happen before self.close() below —
        # otherwise destroying the viewer with the QThread still parented and
        # running would qFatal the process.
        _clear_pending_altium_recent(self)
        _abort_solve_worker(self)
        app = QApplication.instance()
        launcher = LauncherWindow()
        _register_viewer(launcher)
        prev_quit = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        launcher.show()
        _force_native_window_icon(launcher)
        _set_window_aumid(launcher)
        self.close()
        QTimer.singleShot(
            0, lambda: app.setQuitOnLastWindowClosed(prev_quit)
        )

    def _on_solve_failed(self, message: str) -> None:
        """Worker emitted ``failed``. Show the error inline in the Settings
        tab + as a modal dialog so the user can't miss it."""
        logging.getLogger(__name__).error("Solve failed: %s", message)
        _drop_pending_altium_recent(self)
        # Compact one-line version for the status label; full traceback
        # in the dialog for copy-pasting.
        first_line = message.splitlines()[0] if message else "Solve failed"
        self._settings_status_label.setText(
            f"<span style='color:{_T()['err']};'>Solve failed: {_esc(first_line)}</span>"
        )
        self._settings_rerun_btn.setEnabled(True)
        reload_btn = getattr(self, "_settings_reload_design_btn", None)
        if reload_btn is not None:
            reload_btn.setEnabled(True)
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

    def _on_solve_finished(
        self, new_solution, metadata: dict, loaded_project,
        warn_a: float, pct: float, new_settings,
        *, is_import: bool = False,
    ) -> None:
        """Worker emitted ``finished_ok`` — refresh this viewer in place."""
        ok = self._apply_solve_result(
            new_solution, metadata, loaded_project, new_settings,
            warn_a, pct, is_import=is_import,
        )
        if is_import:
            if ok:
                _record_recent_altium_from_metadata(metadata)
            _clear_pending_altium_recent(self)

    def _on_resolve_finished(
        self, new_solution, metadata: dict, loaded_project,
        warn_a: float, pct: float, new_settings,
    ) -> None:
        """Resolve worker finished — refresh this viewer in place."""
        self._apply_solve_result(
            new_solution, metadata, loaded_project, new_settings,
            warn_a, pct, mark_solved_since_save=True,
        )
