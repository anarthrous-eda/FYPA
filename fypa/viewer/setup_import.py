"""File > Import setup from another design…

Copies the sources, sinks, regulators and series parts set up on another
design (another board, or another version of this one) onto the design in
the window, using the same part matching as a restore
(:func:`fypa.setup_sidecar.match_import`):

* entries whose part is matched with certainty are added straight away;
* the rest go through the review dialog in import mode - move each to a
  suggested part or one picked by hand, or leave it out;
* markers placed on copper and parts already set up the same way are
  skipped and listed.

Nothing imported is held for later: an entry the user leaves out is simply
not imported. What is imported becomes part of this design's setup (and its
setup file) like any other edit.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

_FILTER = ("FYPA setup or project (*.fypa-setup.json *.fypa-setup.json.bak "
           "*.fypa);;"
           "Altium design with a saved setup (*.PcbDoc *.PrjPcb);;"
           "KiCad design with a saved setup (*.kicad_pcb *.kicad_pro);;"
           "All files (*)")


def import_setup_from_design(window, path: str | Path | None = None) -> None:
    """Ask for a source (unless ``path``), match it to this design and add
    what fits. Altium and KiCad designs only - a Gerber import has no parts."""
    from fypa.setup_sidecar import describe, load_setup_file, match_import
    from fypa.viewer.theme import _file_dialog_options

    md = getattr(window, "metadata", None) or {}
    if md.get("source_kind") == "gerber":
        QMessageBox.information(
            window, "Import setup",
            "This design was imported from Gerber files, which carry no "
            "parts to match a setup to. Place sources and sinks on the board "
            "instead.")
        return
    if path is None:
        start = str(Path(md["pcbdoc_path"]).parent) if md.get("pcbdoc_path") else ""
        path_str, _ = QFileDialog.getOpenFileName(
            window, "Import setup from another design", start, _FILTER,
            options=_file_dialog_options())
        if not path_str:
            return
        path = path_str
    source = load_setup_file(Path(path))
    if isinstance(source, str):
        QMessageBox.information(window, "Import setup", source)
        return
    loaded = window._topology_loaded_for_setup("Import setup")
    if loaded is None:
        return

    proj = window._ensure_project()
    report, skipped = match_import(source, loaded.extracted,
                                   loaded.annotations.directives,
                                   existing=proj.editor_directives)
    name = Path(path).name
    by_id = {d.id: d for d in report.applied}
    notes = [f"{describe(d)}"
             + (f": {'; '.join(report.changes[d.id])}"
                if d.id in report.changes else "")
             for d in report.applied]
    accepted = list(report.applied)
    if report.review:
        from fypa.viewer.setup_restore import open_review_dialog
        from fypa.viewer.setup_review import ACCEPT

        decisions = open_review_dialog(window, report.review,
                                       applied_notes=notes, importing=name,
                                       extracted=loaded.extracted)
        for _item, action, cand in decisions or []:
            if action == ACCEPT and cand is not None:
                accepted.append(cand.directive)

    if accepted:
        proj.editor_directives = list(proj.editor_directives) + accepted
        window._mark_project_dirty()
        from fypa.viewer.setup_restore import _refresh_after_setup_change
        _refresh_after_setup_change(window)

    left_out = list(skipped) + list(report.superseded)
    n_review_left = len(report.review) - (len(accepted) - len(by_id))
    msg = f"Imported {len(accepted)} from {name}"
    if left_out or n_review_left:
        msg += f"; {len(left_out) + n_review_left} left out"
    window.statusBar().showMessage(msg + ".", 12000)
    if left_out:
        from fypa.viewer.list_dialog import show_list_notice
        show_list_notice(
            window, "Setup imported",
            f"Imported {len(accepted)} "
            f"{'entry' if len(accepted) == 1 else 'entries'} from "
            f"<b>{name}</b>. These were left out:",
            [f"{describe(h.directive)} - {h.reason}" for h in left_out],
            "Press Solve to include the imported entries.")


__all__ = ["import_setup_from_design"]
