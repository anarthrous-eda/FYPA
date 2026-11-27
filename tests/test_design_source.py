"""KiCad (beta) across the app: telling the design's tool apart, and what that
changes - wording, the Y shown for What-If vias, report rows, Recent entries,
the setup chooser and Import Setup."""
from __future__ import annotations

from pathlib import Path

import pytest

from fypa import design_source as DS


# --- telling the design's tool apart ------------------------------------------
@pytest.mark.parametrize("md, kind", [
    ({"prjpcb_path": "a/b.PrjPcb"}, "altium"),
    ({"prjpcb_path": "a/b.kicad_pcb"}, "kicad"),
    # Solved before the source_kind field existed: the path decides.
    ({"prjpcb_path": "a/b.KICAD_PCB"}, "kicad"),
    ({"source_kind": "kicad", "prjpcb_path": "x"}, "kicad"),
    ({"source_kind": "gerber", "prjpcb_path": "g/g.fypa-gerber"}, "gerber"),
    ({}, "altium"),
    (None, "altium"),
])
def test_source_kind(md, kind):
    assert DS.source_kind(md) == kind


def test_tool_name_and_y_sign():
    kicad = {"prjpcb_path": "b.kicad_pcb"}
    assert DS.tool_name(kicad) == "KiCad"
    assert DS.tool_name({"prjpcb_path": "b.PrjPcb"}) == "Altium"
    assert DS.display_y_sign(kicad) == -1.0
    assert DS.display_y_sign({"prjpcb_path": "b.PrjPcb"}) == 1.0


@pytest.mark.parametrize("path, live", [
    ("p.PrjPcb", True), ("p.kicad_pcb", True), ("p.kicad_pro", True),
    ("g.fypa-gerber", False), ("s.pkl", False), ("", False), (None, False),
])
def test_is_live_design_path(path, live):
    assert DS.is_live_design_path(path) is live


def test_kicad_pro_is_keyed_by_its_board():
    from fypa.viewer.project_open import _canonical_design_path
    assert _canonical_design_path(Path("d/B.kicad_pro")) == Path("d/B.kicad_pcb")
    assert _canonical_design_path(Path("d/B.PrjPcb")) == Path("d/B.PrjPcb")


# --- What-If via export in the design tool's coordinates ------------------------
def test_whatif_export_shows_kicad_y_down_the_page():
    from fypa.whatif_vias import AddedVia, export_rows
    via = AddedVia(x_mm=10.0, y_mm=-20.0, net="VIN", layer_start=1,
                   layer_end=32, diameter_mm=0.6, hole_diameter_mm=0.3)
    (altium,) = export_rows([via], [1, 32], {1: "Top", 32: "Bottom"})
    (kicad,) = export_rows([via], [1, 32], {1: "Top", 32: "Bottom"}, y_sign=-1.0)
    assert altium[3] == "-20.0000" and kicad[3] == "20.0000"
    assert kicad[2] == altium[2] == "10.0000"          # X is the same


# --- report header ---------------------------------------------------------------
def _report(**kw):
    from fypa.report.model import Report, ReportSettings
    base = {"project_name": "B", "prjpcb_path": "d/B.kicad_pcb",
            "pcbdoc_path": "d/B.kicad_pcb", "generated": "2026-11-27 12:00",
            "fypa_version": "x", "settings": ReportSettings(), "rails": [],
            "design_findings": []}
    base.update(kw)
    return Report(**base)


def test_report_header_names_kicad_beta_and_skips_the_repeated_board():
    from fypa.report.model import design_rows
    rows = dict(design_rows(_report(source_kind="kicad")))
    assert rows["Source"] == "KiCad (beta)"
    assert "PCB" not in rows                       # the board is the project path
    altium = dict(design_rows(_report(prjpcb_path="d/B.PrjPcb",
                                      pcbdoc_path="d/B.PcbDoc")))
    assert altium["PCB"] == "d/B.PcbDoc" and "Source" not in altium


# --- Recent list -------------------------------------------------------------------
def test_recent_kicad_entry_label_and_chip():
    from fypa.viewer.prefs import recent_project_label
    from fypa.viewer.recent_panel import _chip_kind
    entry = {"kind": "altium", "prjpcb_path": "C:/d/Board.kicad_pcb",
             "pcbdoc_path": "C:/d/Board.kicad_pcb"}
    assert recent_project_label(entry) == "Board.kicad_pcb - d [KiCad]"
    assert _chip_kind(entry) == "kicad"
    assert _chip_kind({"kind": "altium", "prjpcb_path": "C:/d/B.PrjPcb"}) == "altium"


# --- setup chooser ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _texts(widget) -> str:
    from PySide6.QtWidgets import QLabel
    return " ".join(lb.text() for lb in widget.findChildren(QLabel))


def test_setup_chooser_words_the_schematic_route_for_kicad(qapp):
    from fypa.viewer import setup_chooser as SC
    dlg = SC.SetupChooserDialog(None, has_schematic=True, tool="KiCad")
    text = _texts(dlg)
    assert "Define in KiCad" in text
    assert "Update PCB from Schematic" in text
    assert "FYPA_PdnParams" not in text             # Altium-only script tip
    altium = _texts(SC.SetupChooserDialog(None, has_schematic=True))
    assert "Define in Altium" in altium and "FYPA_PdnParams" in altium


# --- Import Setup from a KiCad design ------------------------------------------------
def test_import_setup_reads_the_file_beside_a_kicad_board(tmp_path):
    from fypa.setup_sidecar import load_setup_file, sidecar_path
    board = tmp_path / "B.kicad_pcb"
    board.write_text("(kicad_pcb)")
    msg = load_setup_file(tmp_path / "B.kicad_pro")
    assert isinstance(msg, str) and "no saved setup" in msg
    assert sidecar_path(board, board).name == "B.fypa-setup.json"


# --- design issues --------------------------------------------------------------------
def test_kicad_design_carries_a_beta_note_and_kicad_fix_wording():
    from fypa.design_issues import NOTE, collect_design_issues
    md = {"prjpcb_path": "B.kicad_pcb", "source_kind": "kicad",
          "mesh_failures": [{"net": "VIN", "summary": ""}]}
    issues = collect_design_issues(md)
    beta = [i for i in issues if i.kind == "kicad-beta"]
    assert len(beta) == 1 and beta[0].severity == NOTE
    mesh = next(i for i in issues if i.kind == "mesh-failed")
    assert "in KiCad" in mesh.message
    assert not any(i.kind == "kicad-beta"
                   for i in collect_design_issues({"prjpcb_path": "B.PrjPcb"}))


# --- variant picker wording ---------------------------------------------------------
def test_variant_picker_names_the_design_tool():
    from fypa.viewer.variant_ui import combo_entries, loaded_summary
    kicad = {"tool": "KiCad", "altium_current": None, "available": ["Lite"],
             "requested": None, "name": "", "not_fitted": []}
    assert combo_entries(kicad)[0][0] == "KiCad default: [No Variations]"
    assert "KiCad default: the base design" in loaded_summary(kicad)
    altium = {"altium_current": "Bringup", "available": [], "requested": None,
              "name": "Bringup", "not_fitted": []}
    assert combo_entries(altium)[0][0] == "Altium default: Bringup"
