"""Fetch the design behind a solve when the viewer holds none in memory.

A viewer opened through the solve cache (a non-clean import of a project
that was solved before), or from a bare solve pickle, never ran the extract,
so ``_loaded_project`` is ``None``. Features that read the design itself -
Auto setup, the Capacitors / Impedance tabs, the report's decoupling
section - load it here on first use, the way a Resolve does: the project's
design-info cache, else a fresh extract.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
)

from fypa.viewer.theme import _file_dialog_options

log = logging.getLogger(__name__)


class _DesignFetchMixin:

    def _design_in_memory(self):
        """The in-memory LoadedProject when it carries an extract, else None."""
        loaded = getattr(self, "_loaded_project", None)
        if loaded is None or getattr(loaded, "extracted", None) is None:
            return None
        return loaded

    def _fetch_design_info(self, title: str, purpose: str):
        """Load the design behind this solve and keep it on the viewer.

        ``title`` heads any dialog shown; ``purpose`` finishes the sentence
        "... reads the design itself - <purpose>." when the project has to be
        located by hand. Returns the LoadedProject, or ``None`` if this is a
        Gerber import, the user cancels, or it can't be read (in which case
        the user has been told why)."""
        md = getattr(self, "metadata", None) or {}
        if md.get("source_kind") == "gerber":
            return None
        prj = Path(md["prjpcb_path"]) if md.get("prjpcb_path") else None
        pcb = Path(md["pcbdoc_path"]) if md.get("pcbdoc_path") else None
        if prj is None or not prj.is_file():
            prj, pcb = self._ask_for_altium_project(prj, pcb, title, purpose)
            if prj is None:
                return None
        loaded, error = self._load_design_busy(prj, pcb, title)
        if loaded is None:
            QMessageBox.warning(
                self, title,
                f"Could not load the Altium project\n{prj}\n\n{error}")
            return None
        # Keep it: the next Resolve reuses it instead of reloading (it still
        # checks the bridge / auto-SERIES settings and reloads on a change).
        self._loaded_project = loaded
        return loaded

    def _ask_for_altium_project(self, prj: Path | None, pcb: Path | None,
                                title: str, purpose: str):
        """Ask for the .PrjPcb when the solve's own path is missing or gone.
        Returns ``(prjpcb, pcbdoc)``; ``(None, None)`` if cancelled."""
        where = (f"The Altium project this solve was made from isn't at\n{prj}\n"
                 "(moved, renamed, or on another machine)." if prj is not None
                 else "This solve doesn't record which Altium project it came from.")
        answer = QMessageBox.question(
            self, "Locate the Altium project",
            f"{where}\n\n{title} reads the design itself - {purpose}. "
            "Locate the project's .PrjPcb file?",
            QMessageBox.Open | QMessageBox.Cancel, QMessageBox.Open)
        if answer != QMessageBox.Open:
            return None, None
        start = (str(prj.parent) if prj is not None and prj.parent.is_dir()
                 else self._menu_start_dir())
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Locate the Altium project", start,
            "Altium project (*.PrjPcb);;All files (*)",
            options=_file_dialog_options())
        if not path_str:
            return None, None
        new_prj = Path(path_str)
        # Keep the same board in a multi-PCB project when it's beside it.
        new_pcb = None
        if pcb is not None and (new_prj.parent / pcb.name).is_file():
            new_pcb = new_prj.parent / pcb.name
        return new_prj, new_pcb

    def _load_design_busy(self, prj: Path, pcb: Path | None, title: str):
        """Design-info cache, else a full extract, on a worker thread behind a
        busy dialog. Returns ``(loaded | None, error text)``."""
        project = getattr(self, "_project", None)
        no_bridge = set(getattr(project, "no_auto_bridge", None) or ()) or None
        out: dict = {}

        def work() -> None:
            try:
                from fypa.cli import _project_cache_dir, _try_load_cached_design_info

                loaded = None
                try:
                    loaded = _try_load_cached_design_info(prj, None, pcbdoc_path=pcb)
                except Exception:  # noqa: BLE001 - a bad cache just means re-extract
                    loaded = None
                if loaded is not None and no_bridge is not None and (
                        {d.upper() for d in no_bridge}
                        != set(getattr(loaded, "no_auto_bridge_applied", ()) or ())):
                    loaded = None
                if loaded is None:
                    from fypa.altium.loader import load_project

                    cache_dir = (_project_cache_dir(prj, pcb)
                                 if pcb is not None and prj.suffix.lower() == ".prjpcb"
                                 else None)
                    loaded = load_project(prj, pcbdoc_selector=pcb,
                                          no_auto_bridge=no_bridge,
                                          cache_dir=cache_dir)
                out["loaded"] = loaded
            except Exception as e:  # noqa: BLE001 - reported to the user
                log.exception("Loading %s failed", prj)
                out["error"] = f"{type(e).__name__}: {e}"

        dlg = QProgressDialog(f"Reading the design…\n{prj.name}", None, 0, 0, self)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setValue(0)
        thread = threading.Thread(target=work, name="design-load", daemon=True)
        thread.start()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            while thread.is_alive():
                QApplication.processEvents(QEventLoop.AllEvents, 50)
                thread.join(0.05)
        finally:
            QApplication.restoreOverrideCursor()
            dlg.close()
        return out.get("loaded"), out.get("error", "")
