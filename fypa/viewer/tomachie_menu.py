# Copyright (c) 2026 Tomachie LLC.
"""File > Load Tomachie pdn.json… (Ctrl+Shift+T): set up the PDN from a Tomachie file.

Tomachie reads the schematic and the parts' datasheets and writes ``<design>.pdn.json``:
sources, regulators, series parts and every load's current and minimum supply voltage.

* With a project open, the file is applied to it: the design is reloaded with the
  file's directives (see :mod:`fypa.tomachie_pdn`).
* With none open, the project the file names (``project_file``, in the file's folder or
  up to two above it) is opened with it; when it is not found, the user picks the ``.PrjPcb``.

The choice rides on the window (and its ``.fypa`` project) so every later Resolve /
Re-run keeps it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

log = logging.getLogger(__name__)

SHORTCUT = "Ctrl+Shift+T"
CORNERS = {"Typical currents and resistances": "typ",
           "Worst case (maximum currents and resistances)": "max"}


def pdn_choice(window) -> tuple[Path | None, str]:
    """The pdn.json (and corner) the next solve on ``window`` loads, if any."""
    chosen = getattr(window, "_tomachie_pdn", None)
    if chosen:
        return chosen
    project = getattr(window, "_project", None)
    path = getattr(project, "pdn_json", None)
    return (Path(path) if path else None), getattr(project, "pdn_corner", "typ") or "typ"


def _read(pdn_path: Path) -> dict:
    try:
        return json.loads(pdn_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _prjpcb_of(win) -> Path | None:
    meta = getattr(win, "metadata", None)
    p = meta.get("prjpcb_path") if isinstance(meta, dict) else None
    return Path(p) if p and Path(p).exists() else None


def _open_viewer(window):
    """The viewer holding the open Altium project: ``window`` itself, else the
    active one of FYPA's top-level windows that has a project loaded. The action
    can run from the start window or a dialog whose parent is not the viewer."""
    if _prjpcb_of(window) is not None:
        return window
    from PySide6.QtWidgets import QApplication
    wins = [w for w in QApplication.topLevelWidgets()
            if w.isVisible() and _prjpcb_of(w) is not None]
    active = QApplication.activeWindow()
    for w in wins:
        if w is active:
            return w
    return wins[0] if wins else None


def _project_for(window, pdn_path: Path, pdn: dict) -> Path | None:
    """The project the file names (``project_file``), found by its exact name in the
    file's folder or up to two above it (Tomachie writes the pdn.json under the
    project's output / enrich folder); else the user picks one."""
    named = pdn.get("project_file")
    if named:
        folder = pdn_path.parent
        for _ in range(3):
            candidate = folder / Path(named).name
            if candidate.exists():
                return candidate
            folder = folder.parent
    design = pdn.get("design", "")
    title = f"Altium project for {design}" if design else "Altium project"
    if named:
        title += f" ({Path(named).name})"
    prj_str, _ = QFileDialog.getOpenFileName(
        window, title,
        str(pdn_path.parent), "Altium project (*.PrjPcb);;All files (*)")
    return Path(prj_str) if prj_str else None


def written_for_another_project(pdn: dict, prjpcb: Path) -> str | None:
    """The project the file was written for, when it is not ``prjpcb``."""
    named = pdn.get("project_file")
    if named and Path(named).name.lower() != prjpcb.name.lower():
        return Path(named).name
    return None


def _confirm_project(window, pdn_path: Path, pdn: dict, prjpcb: Path) -> bool:
    """A pdn.json applied to a board it was not written for gives confident
    nonsense: say so and let the user decide."""
    other = written_for_another_project(pdn, prjpcb)
    if other is None:
        return True
    return QMessageBox.question(
        window, "Load Tomachie pdn.json",
        f"{pdn_path.name} was written for {other}, but the project is {prjpcb.name}."
        "\n\nApply it anyway?") == QMessageBox.Yes


def on_load_tomachie_pdn(window) -> None:
    from fypa.viewer.project_open import _open_altium_project_at

    viewer = _open_viewer(window)
    open_prj = _prjpcb_of(viewer) if viewer is not None else None
    log.info("Load Tomachie pdn.json: open project %s", open_prj or "(none)")
    if viewer is not None:
        window = viewer
    start = str(open_prj.parent) if open_prj else ""
    pdn_str, _ = QFileDialog.getOpenFileName(
        window, "Load Tomachie pdn.json", start,
        "Tomachie PDN (*.pdn.json);;JSON (*.json)")
    if not pdn_str:
        return
    pdn_path = Path(pdn_str)
    label, ok = QInputDialog.getItem(
        window, "Load Tomachie pdn.json", "Solve with:", list(CORNERS), 0, False)
    if not ok:
        return
    corner = CORNERS[label]
    pdn = _read(pdn_path)
    prj = open_prj or _project_for(window, pdn_path, pdn)
    if prj is None or not _confirm_project(window, pdn_path, pdn, prj):
        return
    window._tomachie_pdn = (pdn_path, corner)
    project = getattr(window, "_project", None)
    if project is not None and hasattr(project, "pdn_json"):
        project.pdn_json, project.pdn_corner = str(pdn_path), corner
    if open_prj is not None and hasattr(window, "_on_reload_design_info"):
        window._on_reload_design_info()     # re-extract with the file, re-solve
        return
    _open_altium_project_at(window, prj, None, clean=False, force_solve=True)
