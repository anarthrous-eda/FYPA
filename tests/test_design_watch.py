"""Reload Design and the "design changed on disk" chip
(fypa.viewer.design_watch).

* the loaded design records the files it was read from, and the record
  reaches the solve metadata through the editor clones;
* the files are compared with that record: a save shows, an unreachable
  project does not read as "everything changed";
* the chip shows only while something differs, and a click asks to reload;
* Reload Design re-reads the project ignoring the design-info cache, keeps
  the editor setup, and refreshes the design-info cache it bypassed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMainWindow,
    QPushButton,
)

from fypa.viewer.design_watch import (  # noqa: E402
    DesignChangedChip,
    _DesignWatchMixin,
    changed_design_files,
    design_changed_tooltip,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def project(tmp_path):
    """A .PrjPcb listing one schematic and one board, all on disk."""
    (tmp_path / "top.SchDoc").write_bytes(b"sch v1")
    (tmp_path / "board.PcbDoc").write_bytes(b"pcb v1")
    prj = tmp_path / "t.PrjPcb"
    prj.write_text(
        "[Document1]\nDocumentPath=top.SchDoc\n"
        "[Document2]\nDocumentPath=board.PcbDoc\n", encoding="utf-8")
    return prj


def _record(prj: Path) -> dict:
    from fypa.cli import _project_file_fingerprints
    return dict(_project_file_fingerprints(prj))


# --- comparing the files with the record ------------------------------------

def test_an_untouched_project_has_no_changed_files(project):
    assert changed_design_files(_record(project), project) == []


def test_a_saved_file_is_named(project):
    record = _record(project)
    (project.parent / "top.SchDoc").write_bytes(b"sch v2, longer")
    assert changed_design_files(record, project) == ["top.SchDoc"]


def test_a_document_added_to_the_project_is_named(project):
    record = _record(project)
    (project.parent / "power.SchDoc").write_bytes(b"new sheet")
    with project.open("a", encoding="utf-8") as f:
        f.write("[Document3]\nDocumentPath=power.SchDoc\n")
    assert changed_design_files(record, project) == [
        "power.SchDoc", "t.PrjPcb"]


def test_no_record_is_not_reported_as_changed(project):
    assert changed_design_files(None, project) == []
    assert changed_design_files({}, project) == []


def test_an_unreachable_project_is_not_reported_as_changed(project):
    """A moved project or a disconnected drive: unknown, not "changed"."""
    record = _record(project)
    project.unlink()
    assert changed_design_files(record, project) == []


def test_a_record_that_came_back_as_lists_still_matches(project):
    """(mtime, size) may come back from a file as a list, not a tuple."""
    record = {p: list(fp) for p, fp in _record(project).items()}
    assert changed_design_files(record, project) == []


def test_the_tooltip_names_the_files_and_caps_a_long_list():
    tip = design_changed_tooltip(["a.SchDoc", "b.PcbDoc"])
    assert "a.SchDoc, b.PcbDoc" in tip and "Ctrl+R" in tip
    many = design_changed_tooltip([f"s{i}.SchDoc" for i in range(9)])
    assert "s5.SchDoc and 3 more" in many and "s6.SchDoc" not in many


# --- the chip ---------------------------------------------------------------

def test_the_chip_shows_only_while_files_differ(qapp):
    chip = DesignChangedChip()
    assert chip.isHidden()
    chip.set_files(["top.SchDoc"])
    assert not chip.isHidden() and "top.SchDoc" in chip.toolTip()
    chip.set_files([])
    assert chip.isHidden() and chip.toolTip() == ""


def test_a_click_on_the_chip_asks_to_reload(qapp):
    chip = DesignChangedChip()
    chip.set_files(["top.SchDoc"])
    chip.show()
    asked = []
    chip.reload_requested.connect(lambda: asked.append(True))
    QTest.mouseClick(chip, Qt.LeftButton, pos=QPoint(4, 4))
    assert asked == [True]


# --- the window side --------------------------------------------------------

class _Window(_DesignWatchMixin, QMainWindow):
    def __init__(self, metadata):
        super().__init__()
        self.metadata = metadata
        self.reloads = 0

    def _on_reload_design_info(self):
        self.reloads += 1


def _wait(qapp, done, timeout_s: float = 5.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        qapp.processEvents()
        if done():
            return True
        time.sleep(0.01)
    return False


def test_a_save_after_the_design_was_read_raises_the_chip(qapp, project):
    win = _Window({"prjpcb_path": str(project),
                   "design_files": _record(project)})
    win._init_design_watch()
    # Wait for the first check to come back clean before changing a file.
    assert _wait(qapp, lambda: win._design_checks_done == 1)
    assert win._design_changed_chip is None

    (project.parent / "board.PcbDoc").write_bytes(b"pcb v2, rerouted")
    win._check_design_changed()
    assert _wait(qapp, lambda: win._design_checks_done == 2)
    chip = win._design_changed_chip
    assert chip is not None and not chip.isHidden()
    assert chip.file_names() == ("board.PcbDoc",)

    # The reloaded design carries a fresh record: the chip goes away.
    win.metadata = {"prjpcb_path": str(project),
                    "design_files": _record(project)}
    win._check_design_changed()
    assert _wait(qapp, lambda: win._design_checks_done == 3)
    assert win._design_changed_files == () and chip.isHidden()


def test_the_chip_reloads_the_design(qapp, project):
    win = _Window({"prjpcb_path": str(project),
                   "design_files": _record(project)})
    win._init_design_watch()
    win._set_design_changed(("top.SchDoc",))
    win._design_changed_chip.reload_requested.emit()
    assert win.reloads == 1


def test_an_overtaken_check_is_ignored(qapp, project):
    win = _Window({"prjpcb_path": str(project),
                   "design_files": _record(project)})
    win._init_design_watch()
    win._design_check_no = 7
    win._on_design_check_done(6, ["top.SchDoc"])        # an older check
    assert win._design_changed_files == ()
    win._on_design_check_done(7, ["top.SchDoc"])
    assert win._design_changed_files == ("top.SchDoc",)


@pytest.mark.parametrize("metadata", [
    None,
    {},
    {"prjpcb_path": "x.PrjPcb"},                              # no record
    {"prjpcb_path": "gerbers/x.fypa-gerber",                  # not Altium
     "design_files": {"gerbers/top.gtl": (1.0, 2)}},
])
def test_designs_that_cannot_be_compared_show_no_chip(qapp, metadata):
    win = _Window(metadata)
    win._init_design_watch()
    assert win._design_watch_target() is None
    assert win._design_changed_files == ()
    assert win._design_changed_chip is None


# --- the record: worker -> loaded design -> metadata ------------------------

def _board():
    from fypa.altium.annotations import parse_annotations
    from fypa.altium.extract import (
        ExtractedProject, Pt2D, RawNet, RawPad, RawPcbComponent,
        RawStackupLayer,
    )
    from fypa.altium.loader import LoadedProject

    def pad(pin, net, x):
        return RawPad(center=Pt2D(x, 0), width_mm=1.0, height_mm=1.0,
                      hole_mm=0, shape=2, rotation_deg=0, layer_id=1,
                      net_index=net, designator=pin, component_index=0,
                      is_through_hole=False, is_smt=True)

    proj = ExtractedProject(
        prjpcb_path=Path("nowhere/t.PrjPcb"),
        pcbdoc_path=Path("nowhere/t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(),
        stackup=(RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0, is_plane=False,
            plane_net_name=None, mech_enabled=True),),
        compiled_netlist=None,
        nets=(RawNet("+12V"), RawNet("GND")),
        sch_components=(),
        pcb_components=(RawPcbComponent(
            designator="J1", center=Pt2D(0.5, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="HDR2", source_designator="J1"),),
        pads=(pad("1", 0, 0.0), pad("2", 1, 1.0)),
    )
    ann = parse_annotations(proj, enabled_layers=[1])
    return LoadedProject(extracted=proj, annotations=ann)


def test_the_record_survives_an_editor_clone():
    from fypa.altium.loader import clone_loaded_for_edit
    loaded = _board()
    loaded.design_files = {"nowhere/t.PcbDoc": (12.5, 340)}
    assert clone_loaded_for_edit(loaded).design_files == loaded.design_files


def test_the_record_reaches_the_solve_metadata(qapp):
    """In-memory design + editor directive (what a Solve sends): the
    record the design carries is what the result reports."""
    from fypa.altium.loader import SolveSettings
    from fypa.project_file import EditorDirective
    from fypa.viewer.solve_worker import _SolveWorker

    loaded = _board()
    loaded.design_files = {"nowhere/t.PcbDoc": (12.5, 340)}
    source = EditorDirective(
        kind="component", role="SOURCE", designator="J1", single_net=False,
        p_net="+12V", n_net="GND", voltage=12.0)
    w = _SolveWorker(Path("nowhere/t.PrjPcb"), SolveSettings(),
                     use_design_cache=False, editor_directives=[source],
                     loaded_project=loaded, load_only=True)
    got = {}
    w.finished_ok.connect(lambda sol, md, ld: got.update(md=md))
    w.failed.connect(lambda msg: got.update(failed=msg))
    w.run()
    assert "failed" not in got, got.get("failed")
    assert got["md"]["design_files"] == {"nowhere/t.PcbDoc": (12.5, 340)}


def test_a_design_with_no_record_reports_none(qapp):
    from fypa.altium.loader import build_solve_metadata
    assert build_solve_metadata(_board(), None)["design_files"] is None


def test_the_worker_lists_the_files_of_an_altium_project_only(qapp, project):
    from fypa.altium.loader import SolveSettings
    from fypa.viewer.solve_worker import _SolveWorker

    w = _SolveWorker(project, SolveSettings())
    files = w._design_files_now()
    assert {Path(p).name for p in files} == {
        "t.PrjPcb", "top.SchDoc", "board.PcbDoc"}
    gerber = _SolveWorker(project.with_suffix(".fypa-gerber"), SolveSettings())
    assert gerber._design_files_now() is None


# --- Reload Design ----------------------------------------------------------

@pytest.fixture
def viewer(qapp, project, monkeypatch):
    import fypa.altium_viewer as V
    import fypa.viewer.file_menu as FM

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {"prjpcb_path": str(project),
                  "pcbdoc_path": str(project.parent / "board.PcbDoc")}
    v._awaiting_first_solve = False
    v._adaptive_gain_check = None
    v._settings_status_label = QLabel()
    v._settings_rerun_btn = QPushButton()
    v._settings_reload_design_btn = QPushButton()
    v._whatif_ensure_base = lambda: None
    v._solve_setup_or_none = lambda: (["a sink"], ["a copper name"])

    class _Settings:
        def apply_to_modules(self):
            pass

    v._gather_settings_from_form = lambda: (_Settings(), 1.0, 99.0, {1: 0.07})
    v.started = []
    v._start_solve_worker = (
        lambda *a, **kw: v.started.append((a, kw)))
    monkeypatch.setattr(FM, "_reject_if_solve_running", lambda owner: False)
    return v


def test_reload_reads_from_disk_and_keeps_the_setup(viewer, project):
    viewer._on_reload_design_info()
    (args, kw), = viewer.started
    assert args[0] == project
    assert kw["use_design_cache"] is False       # read the files again
    assert kw["refresh_design_cache"] is True    # and replace the stale cache
    assert kw["editor_directives"] == ["a sink"]
    assert kw["copper_names"] == ["a copper name"]
    assert kw["stackup_overrides"] == {1: 0.07}
    assert kw["pcbdoc_selector"].endswith("board.PcbDoc")
    assert kw["load_only"] is False
    assert not viewer._settings_reload_design_btn.isEnabled()


def test_reload_of_an_unsolved_design_follows_the_import_preference(
        viewer, monkeypatch):
    import fypa.viewer.file_menu as FM
    viewer._awaiting_first_solve = True
    monkeypatch.setattr(FM, "load_auto_solve_on_import", lambda: False)
    viewer._on_reload_design_info()
    assert viewer.started[-1][1]["load_only"] is True
    monkeypatch.setattr(FM, "load_auto_solve_on_import", lambda: True)
    viewer._on_reload_design_info()
    assert viewer.started[-1][1]["load_only"] is False


def test_reload_stops_when_the_setup_check_says_no(viewer):
    viewer._solve_setup_or_none = lambda: None
    viewer._on_reload_design_info()
    assert viewer.started == []
    assert viewer._settings_reload_design_btn.isEnabled()


# --- Reload Design with nothing changed on disk: ask first ------------------

@pytest.fixture
def asked(viewer, project, monkeypatch):
    """The viewer's design carries a record of the files as they are now;
    returns the list the "reload anyway?" question appends to."""
    import fypa.viewer.file_menu as FM
    viewer.metadata["design_files"] = _record(project)
    viewer._design_reload_pending = lambda: False
    viewer.answer = False
    calls = []

    def ask(parent, name):
        calls.append(name)
        return viewer.answer

    monkeypatch.setattr(FM, "_ask_reload_unchanged", ask)
    return calls


def test_reload_with_nothing_changed_asks_and_stops_on_no(viewer, asked):
    viewer._on_reload_design_info()
    assert asked == ["t.PrjPcb"]
    assert viewer.started == []
    assert viewer._settings_reload_design_btn.isEnabled()


def test_reload_with_nothing_changed_goes_ahead_on_yes(viewer, asked):
    viewer.answer = True
    viewer._on_reload_design_info()
    assert asked == ["t.PrjPcb"] and len(viewer.started) == 1


def test_reload_after_a_save_does_not_ask(viewer, asked, project):
    (project.parent / "top.SchDoc").write_bytes(b"sch v2, a new sink")
    viewer._on_reload_design_info()
    assert asked == [] and len(viewer.started) == 1


def test_reload_does_not_ask_when_it_cannot_tell(viewer, asked):
    """A result from before the files were recorded: unknown, so reload."""
    del viewer.metadata["design_files"]
    viewer._on_reload_design_info()
    assert asked == [] and len(viewer.started) == 1


def test_reload_does_not_ask_while_a_bridges_change_waits(viewer, asked):
    viewer._design_reload_pending = lambda: True
    viewer._on_reload_design_info()
    assert asked == [] and len(viewer.started) == 1


def test_a_caller_with_its_own_reason_is_not_asked(viewer, asked):
    """Load Custom > Tomachie pdn.json reloads with the Altium files as
    they were."""
    viewer._on_reload_design_info(ask_if_unchanged=False)
    assert asked == [] and len(viewer.started) == 1


def test_the_properties_button_reloads_through_the_question(viewer, asked):
    """clicked(bool) must not be taken for ask_if_unchanged."""
    import fypa.altium_viewer as V
    btn = QPushButton()
    btn.clicked.connect(lambda _checked=False: viewer._on_reload_design_info())
    btn.click()
    assert asked == ["t.PrjPcb"] and viewer.started == []
    assert V.PdnViewer._on_reload_design_info.__kwdefaults__ == {
        "ask_if_unchanged": True}


@pytest.mark.parametrize("prjpcb, expect", [
    ("gerbers/x.fypa-gerber", "come from an Altium project"),
    ("", "come from an Altium project"),
    ("gone/away.PrjPcb", "no longer on disk"),
])
def test_reload_says_why_it_cannot(viewer, prjpcb, expect):
    viewer.metadata = {"prjpcb_path": prjpcb}
    viewer._on_reload_design_info()
    assert viewer.started == []
    assert expect in viewer._settings_status_label.text()
    # The toast card (currentMessage() is empty while the window is hidden).
    assert expect in viewer.statusBar()._status.label.text()
