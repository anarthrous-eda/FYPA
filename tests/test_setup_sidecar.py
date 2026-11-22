"""Setup sidecar: sources / sinks set up in FYPA are saved beside the design
and restored on the next import - exactly where the design still matches,
held back (and kept) where it doesn't, and dropped where the schematic now
defines the part."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fypa import setup_sidecar as SS
from fypa.project_file import CopperName, EditorDirective


# --- a tiny design ------------------------------------------------------------

def _comp(des, *, xy=(10.0, 20.0), fp="SOIC-8", comment="LM1117",
          uid="PCB1", suid="SCH1"):
    # Centre as the extract has it: an object with x / y (extract.Pt2D).
    centre = SimpleNamespace(x=xy[0], y=xy[1])
    return SimpleNamespace(designator=des, center=centre, footprint=fp,
                           parameters={"Comment": comment}, unique_id=uid,
                           source_unique_id=suid, layer_name="TOP")


def _design(*comps, nets=("+3V3", "GND", "+5V"), pins=None):
    """``pins``: {designator: {pad: net}}; default gives every part pins
    1 (+3V3) and 2 (GND)."""
    net_names = list(nets)
    pads = []
    for ci, c in enumerate(comps):
        for pad, net in (pins or {}).get(c.designator,
                                          {"1": "+3V3", "2": "GND"}).items():
            pads.append(SimpleNamespace(component_index=ci, designator=pad,
                                        net_index=net_names.index(net)))
    return SimpleNamespace(
        pcb_components=tuple(comps), pads=tuple(pads),
        nets=tuple(SimpleNamespace(name=n) for n in net_names))


def _sink(des="U1", **kw):
    base = {"kind": "component", "role": "SINK", "designator": des,
            "single_net": False, "p_net": "+3V3", "n_net": "GND",
            "current": 0.5}
    base.update(kw)
    return EditorDirective(**base)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """A design folder plus a per-user fallback kept inside tmp_path."""
    folder = tmp_path / "board"
    folder.mkdir()
    prj, pcb = folder / "Board.PrjPcb", folder / "Board.PcbDoc"
    fallback = tmp_path / "cache" / "setup.json"
    monkeypatch.setattr(SS, "fallback_path", lambda *_a: fallback)
    return prj, pcb, fallback


def _saved(paths, directives, design, **kw):
    prj, pcb, _ = paths
    SS.save_setup(prj, pcb, directives, kw.pop("copper", []),
                  extracted=design, **kw)
    return SS.load_setup(prj, pcb)


# --- save / load -------------------------------------------------------------

def test_saves_beside_the_pcbdoc_and_reads_back(paths):
    prj, pcb, _ = paths
    design = _design(_comp("U1"))
    where = SS.save_setup(prj, pcb, [_sink()], [], extracted=design)
    assert where == pcb.with_name("Board.fypa-setup.json")
    back = SS.load_setup(prj, pcb)
    assert [d.designator for d in back.directives] == ["U1"]
    anchor = back.anchors[back.directives[0].id]
    assert anchor["source_unique_id"] == "SCH1"
    assert anchor["pins"] == {"1": "+3V3", "2": "GND"}


def test_unwritable_design_folder_falls_back_to_the_user_copy(
        paths, monkeypatch, tmp_path):
    prj, pcb, fallback = paths
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(SS, "sidecar_path",
                        lambda *_a: blocker / "Board.fypa-setup.json")
    where = SS.save_setup(prj, pcb, [_sink()], [], extracted=_design(_comp("U1")))
    assert where == fallback and fallback.is_file()
    assert SS.load_setup(prj, pcb).directives[0].designator == "U1"


def test_removing_everything_deletes_the_sidecar(paths):
    prj, pcb, _ = paths
    SS.save_setup(prj, pcb, [_sink()], [], extracted=_design(_comp("U1")))
    assert SS.save_setup(prj, pcb, [], []) is None
    assert SS.load_setup(prj, pcb) is None


def test_nothing_to_save_creates_no_file(paths):
    prj, pcb, _ = paths
    assert SS.save_setup(prj, pcb, [], []) is None
    assert not SS.sidecar_path(prj, pcb).exists()


def test_without_the_design_fingerprints_carry_over(paths):
    prj, pcb, _ = paths
    first = _saved(paths, [_sink()], _design(_comp("U1")))
    d = first.directives[0]
    SS.save_setup(prj, pcb, [d], [], extracted=None, previous=first)
    assert SS.load_setup(prj, pcb).anchors[d.id]["unique_id"] == "PCB1"


def test_a_broken_sidecar_is_ignored(paths):
    prj, pcb, _ = paths
    SS.sidecar_path(prj, pcb).write_text("{not json", encoding="utf-8")
    assert SS.load_setup(prj, pcb) is None


def test_a_newer_schema_is_ignored(paths):
    prj, pcb, _ = paths
    SS.sidecar_path(prj, pcb).write_text(
        json.dumps({"schema_version": 99}), encoding="utf-8")
    assert SS.load_setup(prj, pcb) is None


# --- matching ------------------------------------------------------------------

def test_unchanged_design_restores_everything_with_values_intact(paths):
    saved = _saved(paths, [_sink(current=1.25, min_voltage=3.1)],
                   _design(_comp("U1")))
    rep = SS.match_exact(saved, _design(_comp("U1", xy=(50.0, 5.0))))
    assert [d.designator for d in rep.applied] == ["U1"]
    assert rep.applied[0].current == 1.25 and rep.applied[0].min_voltage == 3.1
    assert rep.applied[0].id == saved.directives[0].id
    assert not rep.held and not rep.superseded


@pytest.mark.parametrize("now, reason", [
    (_design(_comp("U2")), "U1 is not on this board"),
    (_design(_comp("U1", suid="SCH9")), "different schematic part"),
    (_design(_comp("U1", suid="", uid="", fp="SOT-223")), "footprint changed"),
    (_design(_comp("U1", suid="", uid="", comment="LM317")), "changed from"),
    (_design(_comp("U1"), nets=("+3V3_A", "GND"),
             pins={"U1": {"1": "+3V3_A", "2": "GND"}}),
     "net +3V3 no longer exists"),
])
def test_changed_parts_are_held_with_a_reason(paths, now, reason):
    saved = _saved(paths, [_sink()], _design(_comp("U1")))
    rep = SS.match_exact(saved, now)
    assert rep.applied == []
    assert len(rep.held) == 1 and reason in rep.held[0].reason


def test_same_ids_win_over_a_footprint_change(paths):
    """The unique ids say it is the same placed part, so a footprint swap
    (0603 to 0402 on the same part) doesn't hold it back."""
    saved = _saved(paths, [_sink()], _design(_comp("U1")))
    rep = SS.match_exact(saved, _design(_comp("U1", fp="QFN-16")))
    assert len(rep.applied) == 1


def test_a_pin_that_is_gone_holds_the_entry(paths):
    saved = _saved(paths, [_sink(p_pins=["1", "3"])], _design(
        _comp("U1"), pins={"U1": {"1": "+3V3", "2": "GND", "3": "+3V3"}}))
    rep = SS.match_exact(saved, _design(_comp("U1")))
    assert "no pin 3" in rep.held[0].reason


def test_schematic_now_defining_the_part_supersedes_it(paths):
    saved = _saved(paths, [_sink(), _sink("U2", overrides_designator="U2")],
                   _design(_comp("U1"), _comp("U2", uid="P2", suid="S2")))

    class SinkSpec:
        def __init__(self, designator):
            self.designator = designator

    rep = SS.match_exact(
        saved, _design(_comp("U1"), _comp("U2", uid="P2", suid="S2")),
        [SinkSpec("U1"), SinkSpec("U2")])
    assert [h.directive.designator for h in rep.superseded] == ["U1"]
    # An explicit FYPA override of the schematic part stays.
    assert [d.designator for d in rep.applied] == ["U2"]


def test_free_markers_restore_while_their_net_exists(paths):
    free = EditorDirective(kind="free", role="SOURCE", anchor_xy=(1.0, 2.0),
                           layer_id=1, p_net="NEWNET", voltage=5.0)
    name = CopperName(anchor_xy=(1.0, 2.0), layer_id=1, name="NEWNET")
    saved = _saved(paths, [free], _design(_comp("U1")), copper=[name])
    rep = SS.match_exact(saved, _design(_comp("U1")))
    assert len(rep.applied) == 1 and rep.copper_names[0].name == "NEWNET"


def test_held_entries_are_kept_and_retried(paths):
    prj, pcb, _ = paths
    saved = _saved(paths, [_sink()], _design(_comp("U1")))
    rep = SS.match_exact(saved, _design(_comp("U2")))
    assert rep.held
    # Saved again from the changed design: the held entry is written back.
    SS.save_setup(prj, pcb, rep.applied, [], extracted=_design(_comp("U2")),
                  held=rep.held)
    again = SS.load_setup(prj, pcb)
    assert again.directives == [] and len(again.held) == 1
    # The part is back: it restores.
    rep2 = SS.match_exact(again, _design(_comp("U1")))
    assert [d.designator for d in rep2.applied] == ["U1"] and not rep2.held


def test_report_round_trips_through_metadata(paths):
    saved = _saved(paths, [_sink(), _sink("U9")], _design(_comp("U1"),
                                                          _comp("U9")))
    rep = SS.match_exact(saved, _design(_comp("U1")))
    back = SS.RestoreReport.from_metadata(
        json.loads(json.dumps(rep.to_metadata())))
    assert [d.designator for d in back.applied] == ["U1"]
    assert [h.directive.designator for h in back.held] == ["U9"]
    assert back.path == Path(saved.path)


# --- solve worker -------------------------------------------------------------

def test_worker_takes_what_matched_and_reports_it(paths):
    from fypa.viewer.solve_worker import _SolveWorker

    saved = _saved(paths, [_sink(), _sink("U9")],
                   _design(_comp("U1"), _comp("U9")))
    w = SimpleNamespace(
        _saved_setup=saved, _restore_report=None, _editor_directives=[],
        _copper_names=[],
        stage_changed=SimpleNamespace(emit=lambda *_a: None))
    loaded = SimpleNamespace(extracted=_design(_comp("U1")),
                             annotations=SimpleNamespace(directives=[]))
    _SolveWorker._restore_saved_setup(w, loaded)
    assert [d.designator for d in w._editor_directives] == ["U1"]
    md = {}
    _SolveWorker._attach_restore_report(w, md)
    assert [h["directive"]["designator"]
            for h in md["setup_restore"]["held"]] == ["U9"]


# --- viewer -------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _window(paths, extracted=None):
    from PySide6.QtWidgets import QWidget

    prj, pcb, _ = paths

    class W(QWidget):
        def __init__(self):
            super().__init__()
            self.metadata = {"prjpcb_path": str(prj), "pcbdoc_path": str(pcb)}
            self._project = None
            self._loaded_project = SimpleNamespace(extracted=extracted)
            self.messages = []

        def _ensure_project(self):
            from fypa.project_file import ProjectFile
            if self._project is None:
                self._project = ProjectFile()
            return self._project

        def statusBar(self):
            return SimpleNamespace(
                showMessage=lambda m, *_a: self.messages.append(m))

    return W()


def test_viewer_adopts_restored_entries_and_skips_the_chooser(qapp, paths):
    from fypa.viewer.setup_restore import adopt_restored_setup

    saved = _saved(paths, [_sink()], _design(_comp("U1")))
    rep = SS.match_exact(saved, _design(_comp("U1")))
    win = _window(paths, _design(_comp("U1")))
    md = {"setup_restore": rep.to_metadata()}
    stub = SimpleNamespace(solver_info={"stub": True})
    assert adopt_restored_setup(win, md, stub) is True
    assert [d.designator for d in win._project.editor_directives] == ["U1"]
    assert win._initial_solve_stale is True
    assert "Restored 1 sink" in win.messages[0]
    assert "setup_restore" not in md


def test_viewer_without_a_saved_setup_falls_through(qapp, paths):
    from fypa.viewer.setup_restore import adopt_restored_setup

    assert adopt_restored_setup(_window(paths), {}, None) is False


def test_held_entries_open_the_review_and_stay_saved(qapp, paths, monkeypatch):
    from fypa.viewer import setup_restore
    from fypa.viewer.setup_restore import adopt_restored_setup

    prj, pcb, _ = paths
    opened = []
    monkeypatch.setattr(setup_restore, "open_setup_review",
                        lambda w, **k: opened.append(list(w._setup_review)))
    saved = _saved(paths, [_sink(), _sink("U9")],
                   _design(_comp("U1"), _comp("U9", uid="P9", suid="S9")))
    rep = SS.match_exact(saved, _design(_comp("U1")))
    win = _window(paths, _design(_comp("U1")))
    adopt_restored_setup(win, {"setup_restore": rep.to_metadata()},
                         SimpleNamespace(solver_info={}))
    (items,) = opened
    assert [it.entry.reason for it in items] == ["U9 is not on this board"]
    assert win.metadata["setup_held"][0].startswith("SINK U9")
    again = SS.load_setup(prj, pcb)
    assert [d.designator for d in again.directives] == ["U1"]
    assert [h.directive.designator for h in again.held] == ["U9"]


def test_edits_save_after_a_short_delay_and_flush_on_demand(qapp, paths):
    from fypa.viewer.setup_restore import flush_setup_save, schedule_setup_save

    prj, pcb, _ = paths
    win = _window(paths, _design(_comp("U1")))
    win._ensure_project().editor_directives = [_sink()]
    schedule_setup_save(win)
    assert SS.load_setup(prj, pcb) is None       # not yet
    flush_setup_save(win)
    assert SS.load_setup(prj, pcb).directives[0].designator == "U1"


def test_a_save_for_a_design_no_longer_shown_is_dropped(qapp, paths, tmp_path):
    from fypa.viewer.setup_restore import _fire_save, schedule_setup_save

    prj, pcb, _ = paths
    win = _window(paths, _design(_comp("U1")))
    win._ensure_project().editor_directives = [_sink()]
    schedule_setup_save(win)
    win.metadata = {"prjpcb_path": str(tmp_path / "Other.PrjPcb"),
                    "pcbdoc_path": str(tmp_path / "Other.PcbDoc")}
    _fire_save(win)
    assert SS.load_setup(prj, pcb) is None
    assert not (tmp_path / "Other.fypa-setup.json").exists()


def test_gerber_designs_have_no_sidecar(qapp, paths):
    from fypa.viewer.setup_restore import save_setup_now

    win = _window(paths, _design(_comp("U1")))
    win.metadata["source_kind"] = "gerber"
    win._ensure_project().editor_directives = [_sink()]
    assert save_setup_now(win) is None


# --- stage 2: parts that changed ---------------------------------------------

def _plain(des, **kw):
    """A part with no Altium unique ids - only footprint, value, position
    and pins to go on."""
    return _comp(des, uid="", suid="", **kw)


def test_reannotated_part_follows_its_unique_id(paths):
    saved = _saved(paths, [_sink("U1")], _design(_comp("U1", xy=(5.0, 5.0))))
    rep = SS.match_saved(saved, _design(_comp("U7", xy=(5.0, 5.0))))
    assert [d.designator for d in rep.applied] == ["U7"]
    assert rep.applied[0].id == saved.directives[0].id
    assert any("U1 → U7" in c for c in rep.changes[rep.applied[0].id])
    assert not rep.held and not rep.review


def test_reannotation_without_ids_matches_the_same_spot(paths):
    saved = _saved(paths, [_sink("U1")], _design(_plain("U1")))
    now = _design(_plain("U7"), _plain("U8", xy=(90.0, 90.0)))
    rep = SS.match_saved(saved, now)
    assert [d.designator for d in rep.applied] == ["U7"]


def test_a_renamed_net_on_the_same_part_is_proposed_not_applied(paths):
    saved = _saved(paths, [_sink("U1")], _design(_comp("U1")))
    now = _design(_comp("U1"), nets=("+3V3_MAIN", "GND"),
                  pins={"U1": {"1": "+3V3_MAIN", "2": "GND"}})
    rep = SS.match_saved(saved, now)
    assert rep.applied == []
    (item,) = rep.review
    assert item.status == SS.REVIEW_NET_CHANGED
    cand = item.candidates[0]
    assert cand.directive.p_net == "+3V3_MAIN"
    assert cand.directive.designator == "U1"
    assert "net +3V3 → +3V3_MAIN" in cand.changes


def test_a_moved_and_renamed_part_without_ids_is_a_likely_match(paths):
    saved = _saved(paths, [_sink("U1")], _design(_plain("U1")))
    rep = SS.match_saved(saved, _design(_plain("U4", xy=(12.0, 21.0))))
    (item,) = rep.review
    assert item.status == SS.REVIEW_LIKELY
    assert item.candidates[0].designator == "U4"
    assert rep.applied == []


def test_two_equal_parts_make_it_ambiguous(paths):
    saved = _saved(paths, [_sink("U1")], _design(_plain("U1")))
    now = _design(_plain("U4", xy=(12.0, 21.0)), _plain("U5", xy=(8.0, 19.0)))
    (item,) = SS.match_saved(saved, now).review
    assert item.status == SS.REVIEW_AMBIGUOUS
    assert {c.designator for c in item.candidates} == {"U4", "U5"}


def test_nothing_like_it_is_missing(paths):
    saved = _saved(paths, [_sink("U1")], _design(_plain("U1")))
    now = _design(_plain("R1", fp="0402", comment="10k", xy=(80.0, 80.0)),
                  pins={"R1": {"1": "+5V", "2": "GND"}})
    (item,) = SS.match_saved(saved, now).review
    assert item.status == SS.REVIEW_MISSING and item.candidates == []


def test_two_saved_parts_never_claim_the_same_new_part(paths):
    saved = _saved(paths, [_sink("U1"), _sink("U2")],
                   _design(_plain("U1"), _plain("U2", xy=(10.4, 20.0))))
    rep = SS.match_saved(saved, _design(_plain("U9", xy=(10.2, 20.0))))
    assert [d.designator for d in rep.applied].count("U9") <= 1
    assert len(rep.applied) + len(rep.review) == 2
    # Both entries pointed at U9: neither is the clear answer.
    for item in rep.review:
        assert item.status != SS.REVIEW_LIKELY


def test_entries_on_one_part_move_together(paths):
    reg = EditorDirective(kind="component", role="REGULATOR", designator="U1",
                          single_net=False, p_net="+3V3", n_net="GND",
                          in_p_net="+5V", voltage=3.3, regulator_type="LDO")
    pins = {"1": "+3V3", "2": "GND", "3": "+5V"}
    saved = _saved(paths, [reg, _sink("U1")],
                   _design(_comp("U1"), pins={"U1": pins}))
    rep = SS.match_saved(saved, _design(_comp("U3"), pins={"U3": pins}))
    assert sorted(d.role for d in rep.applied) == ["REGULATOR", "SINK"]
    assert {d.designator for d in rep.applied} == {"U3"}


def test_a_part_picked_by_hand_is_checked(paths):
    saved = _saved(paths, [_sink("U1")], _design(_plain("U1")))
    now = _design(_plain("U4", xy=(60.0, 60.0)),
                  _plain("R1", fp="0402", comment="10k"),
                  pins={"U4": {"1": "+3V3", "2": "GND"},
                        "R1": {"1": "+5V", "2": "+5V"}})
    (item,) = SS.match_saved(saved, now).review
    ok = SS.retarget_to(item.entry, now, "U4")
    assert isinstance(ok, SS.Candidate) and ok.directive.designator == "U4"
    bad = SS.retarget_to(item.entry, now, "R1")
    assert isinstance(bad, str) and "R1" in bad
    assert "not on this board" in SS.retarget_to(item.entry, now, "U99")


def test_review_survives_the_trip_to_the_viewer(paths):
    saved = _saved(paths, [_sink("U1")], _design(_plain("U1")))
    rep = SS.match_saved(saved, _design(_plain("U4", xy=(12.0, 21.0))))
    back = SS.RestoreReport.from_metadata(
        json.loads(json.dumps(rep.to_metadata())))
    assert back.review[0].status == SS.REVIEW_LIKELY
    assert back.review[0].candidates[0].directive.designator == "U4"


# --- review dialog and what it does ------------------------------------------

def _review_window(paths, report, extracted):
    win = _window(paths, extracted)
    win._mark_project_dirty = lambda: setattr(win, "dirty", True)
    win._ensure_project().editor_directives = list(report.applied)
    win._setup_held = list(report.held)
    win._setup_review = list(report.review)
    return win


def test_dialog_preselects_clear_answers_only(qapp, paths):
    from fypa.viewer.setup_review import ACCEPT, KEEP, SetupReviewDialog

    saved = _saved(paths, [_sink("U1"), _sink("U2")],
                   _design(_plain("U1"), _plain("U2", xy=(70.0, 70.0))))
    now = _design(_plain("U4", xy=(12.0, 21.0)),
                  _plain("U5", xy=(68.0, 70.0)), _plain("U6", xy=(72.0, 70.0)))
    rep = SS.match_saved(saved, now)
    statuses = {it.entry.directive.designator: it.status for it in rep.review}
    assert statuses == {"U1": SS.REVIEW_LIKELY, "U2": SS.REVIEW_AMBIGUOUS}
    dlg = SetupReviewDialog(None, rep.review)
    got = {it.entry.directive.designator: (a, c.designator if c else None)
           for it, a, c in dlg.decisions()}
    assert got == {"U1": (ACCEPT, "U4"), "U2": (KEEP, None)}


def test_applying_decisions_moves_removes_and_keeps(qapp, paths):
    from fypa.viewer.setup_restore import apply_review_decisions, save_setup_now
    from fypa.viewer.setup_review import ACCEPT, KEEP, REMOVE

    prj, pcb, _ = paths
    saved = _saved(paths, [_sink("U1"), _sink("U2"), _sink("U3")],
                   _design(_plain("U1"), _plain("U2", xy=(40.0, 40.0)),
                           _plain("U3", xy=(70.0, 70.0))))
    now = _design(_plain("U4", xy=(12.0, 21.0)))
    rep = SS.match_saved(saved, now)
    win = _review_window(paths, rep, now)
    by = {it.entry.directive.designator: it for it in rep.review}
    apply_review_decisions(win, [
        (by["U1"], ACCEPT, by["U1"].candidates[0]),
        (by["U2"], REMOVE, None),
        (by["U3"], KEEP, None),
    ])
    assert [d.designator for d in win._project.editor_directives] == ["U4"]
    assert [h.directive.designator for h in win._setup_held] == ["U3"]
    assert win.dirty is True
    assert win.metadata["setup_held"][0].startswith("SINK U3")
    save_setup_now(win)
    back = SS.load_setup(prj, pcb)
    assert [d.designator for d in back.directives] == ["U4"]
    assert [h.directive.designator for h in back.held] == ["U3"]


def test_solve_goes_ahead_when_nothing_is_held_or_it_was_acknowledged(
        qapp, paths):
    from fypa.viewer.setup_restore import confirm_solve_with_held

    win = _window(paths)
    assert confirm_solve_with_held(win) is True
    win._setup_held = [SS.HeldEntry(_sink(), "U1 is not on this board")]
    win._setup_held_ack = True
    assert confirm_solve_with_held(win) is True


# --- importing a setup from another design --------------------------------------

def test_import_source_can_be_a_setup_file_a_project_or_a_pcbdoc(paths, tmp_path):
    from fypa.project_file import ProjectFile

    prj, pcb, _ = paths
    _saved(paths, [_sink("U1")], _design(_comp("U1")))
    assert SS.load_setup_file(SS.sidecar_path(prj, pcb)).directives
    assert SS.load_setup_file(pcb).directives[0].designator == "U1"
    proj = tmp_path / "other.fypa"
    ProjectFile(editor_directives=[_sink("U4")]).save(proj)
    assert SS.load_setup_file(proj).directives[0].designator == "U4"
    lonely = tmp_path / "Lonely.PcbDoc"
    assert "no saved setup" in SS.load_setup_file(lonely)
    junk = tmp_path / "junk.json"
    junk.write_text("[1, 2]", encoding="utf-8")
    assert "not a FYPA" in SS.load_setup_file(junk)


def test_import_gives_fresh_ids_and_follows_parts(paths):
    source = _saved(paths, [_sink("U1")], _design(_comp("U1")))
    here = _design(_comp("U30"))                  # same unique ids, renamed
    rep, skipped = SS.match_import(source, here)
    (d,) = rep.applied
    assert d.designator == "U30" and d.id != source.directives[0].id
    assert not skipped


def test_import_skips_copper_markers_and_parts_already_set_up(paths):
    free = EditorDirective(kind="free", role="SOURCE", anchor_xy=(1.0, 2.0),
                           layer_id=1, p_net="+3V3", voltage=3.3)
    source = _saved(paths, [_sink("U1"), _sink("U2"), free],
                    _design(_comp("U1"), _comp("U2", uid="P2", suid="S2")))
    here = _design(_comp("U1"), _comp("U2", uid="P2", suid="S2"))
    rep, skipped = SS.match_import(source, here, existing=[_sink("U2")])
    assert [d.designator for d in rep.applied] == ["U1"]
    reasons = sorted(h.reason for h in skipped)
    assert any("already set up" in r for r in reasons)
    assert any("placed on copper" in r for r in reasons)


def test_import_without_fingerprints_matches_by_designator(paths, tmp_path):
    from fypa.project_file import ProjectFile

    proj = tmp_path / "other.fypa"
    ProjectFile(editor_directives=[_sink("U1"), _sink("U9")]).save(proj)
    source = SS.load_setup_file(proj)
    rep, _skipped = SS.match_import(source, _design(_comp("U1")))
    assert [d.designator for d in rep.applied] == ["U1"]
    (item,) = rep.review
    assert item.entry.directive.designator == "U9"
    assert item.status == SS.REVIEW_MISSING


def _import_window(paths, extracted):
    win = _review_window(paths, SS.RestoreReport(), extracted)
    win._topology_loaded_for_setup = lambda _title="": SimpleNamespace(
        extracted=extracted, annotations=SimpleNamespace(directives=[]))
    return win


def test_importing_adds_matches_and_what_the_review_accepts(
        qapp, paths, monkeypatch, tmp_path):
    from fypa.viewer import list_dialog, setup_restore
    from fypa.viewer.setup_import import import_setup_from_design
    from fypa.viewer.setup_review import ACCEPT, KEEP

    other = tmp_path / "other"
    other.mkdir()
    o_prj, o_pcb = other / "Old.PrjPcb", other / "Old.PcbDoc"
    SS.save_setup(o_prj, o_pcb, [_sink("U1"), _sink("U2"), _sink("U3")], [],
                  extracted=_design(_comp("U1"),
                                    _plain("U2", xy=(40.0, 40.0)),
                                    _plain("U3", xy=(70.0, 70.0))))
    here = _design(_comp("U1"), _plain("U5", xy=(41.0, 41.0)))
    win = _import_window(paths, here)

    def _decide(_w, items, **kw):
        assert kw["importing"] == "Old.fypa-setup.json"
        by = {it.entry.directive.designator: it for it in items}
        return [(by["U2"], ACCEPT, by["U2"].candidates[0]),
                (by["U3"], KEEP, None)]

    monkeypatch.setattr(setup_restore, "open_review_dialog", _decide)
    shown = []
    monkeypatch.setattr(list_dialog, "show_list_notice",
                        lambda *a, **k: shown.append(a))
    import_setup_from_design(win, SS.sidecar_path(o_prj, o_pcb))
    assert sorted(d.designator for d in win._project.editor_directives) == [
        "U1", "U5"]
    assert win.dirty is True
    assert "Imported 2" in win.messages[-1] and "1 left out" in win.messages[-1]
    assert not getattr(win, "_setup_held", [])


def test_gerber_designs_refuse_an_import(qapp, paths, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from fypa.viewer.setup_import import import_setup_from_design

    said = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: said.append(a[2])))
    win = _import_window(paths, _design(_comp("U1")))
    win.metadata["source_kind"] = "gerber"
    import_setup_from_design(win, "whatever.fypa")
    assert said and "Gerber" in said[0]


def test_chooser_links_to_the_import(qapp, monkeypatch):
    from fypa.viewer import setup_chooser as SC
    from fypa.viewer import setup_import

    assert SC.SetupChooserDialog(None, has_schematic=True).import_link
    assert not hasattr(SC.SetupChooserDialog(None, has_schematic=False),
                       "import_link")
    called = []
    monkeypatch.setattr(setup_import, "import_setup_from_design",
                        lambda w: called.append(w))
    SC.run_setup_route("win", SC.ROUTE_IMPORT)
    assert called == ["win"]


def test_review_dialog_in_import_mode_offers_dont_import(qapp, paths):
    from fypa.viewer.setup_review import KEEP, SetupReviewDialog

    source = _saved(paths, [_sink("U9")], _design(_plain("U9")))
    rep, _ = SS.match_import(source, _design(
        _plain("R1", fp="0402", comment="10k", xy=(80.0, 80.0)),
        pins={"R1": {"1": "+5V", "2": "GND"}}))
    dlg = SetupReviewDialog(None, rep.review, importing="Old.fypa-setup.json")
    combo = dlg._combos[0]
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert "Don't import" in labels and "Remove from the setup" not in labels
    assert dlg.decisions()[0][1] == KEEP


# --- solve cache ---------------------------------------------------------------

def test_setup_hash_ignores_when_and_where_it_was_saved(paths, tmp_path):
    prj, pcb, _ = paths
    design = _design(_comp("U1"))
    first = _saved(paths, [_sink()], design)
    # Saved again later, and once more in another folder: same key.
    other = tmp_path / "elsewhere"
    other.mkdir()
    SS.save_setup(other / "B.PrjPcb", other / "B.PcbDoc", first.directives, [],
                  extracted=design)
    moved = SS.load_setup(other / "B.PrjPcb", other / "B.PcbDoc")
    assert SS.setup_hash(first) == SS.setup_hash(moved)


def test_setup_hash_follows_what_the_restore_reads(paths):
    design = _design(_comp("U1"))
    base = SS.setup_hash(_saved(paths, [_sink(current=0.5)], design))
    assert SS.setup_hash(_saved(paths, [_sink(current=0.6)], design)) != base
    moved_part = _design(_comp("U1", xy=(99.0, 99.0)))
    assert SS.setup_hash(_saved(paths, [_sink(current=0.5)], moved_part)) != base


def test_cache_key_gains_the_setup_only_when_there_is_one():
    from fypa.cli import _project_fingerprint

    prj = Path("ExampleDesigns/Sandbox/Sandbox.PrjPcb")
    plain = _project_fingerprint(prj, None)
    keyed = _project_fingerprint(prj, None, setup_hash="abc123")
    assert "setup" not in plain
    assert keyed["setup"] == "abc123"
    assert {k: v for k, v in keyed.items() if k != "setup"} == plain


@pytest.fixture
def sandbox_copy(tmp_path):
    import shutil

    src = Path("ExampleDesigns/Sandbox")
    if not (src / "Sandbox.PrjPcb").exists():
        pytest.skip("Sandbox example design not available")
    dst = tmp_path / "Sandbox"
    shutil.copytree(src, dst)
    yield dst / "Sandbox.PrjPcb", dst / "Sandbox.PcbDoc"
    from fypa.cli import _project_cache_dir
    shutil.rmtree(_project_cache_dir(dst / "Sandbox.PrjPcb",
                                     dst / "Sandbox.PcbDoc"),
                  ignore_errors=True)


def _run_import(prj, pcb, *, restore=True):
    """One Altium import through the solve worker, run on this thread:
    ``(stage messages, metadata)``."""
    from fypa.altium.loader import SolveSettings
    from fypa.viewer.solve_worker import _SolveWorker

    stages, done, failed = [], [], []
    w = _SolveWorker(prj, SolveSettings(), pcbdoc_selector=str(pcb),
                     try_solve_cache_first=True, restore_setup=restore)
    w.stage_changed.connect(stages.append)
    w.finished_ok.connect(lambda sol, md, _l: done.append(md))
    w.failed.connect(failed.append)
    w.run()
    assert not failed, failed[0][:2000]
    return stages, done[0]


def test_reopening_with_the_same_setup_reuses_the_solve(qapp, sandbox_copy):
    from fypa.altium.loader import load_project

    prj, pcb = sandbox_copy
    loaded = load_project(prj)
    schem = SS.schematic_designators(loaded.annotations.directives)
    pins = SS._pins_by_component(loaded.extracted)
    part, nets = next(
        (c.designator, sorted({n for n in pins.get(i, {}).values() if n}))
        for i, c in enumerate(loaded.extracted.pcb_components)
        if c.designator.upper() not in schem
        and len({n for n in pins.get(i, {}).values() if n}) >= 2)
    sink = EditorDirective(kind="component", role="SINK", designator=part,
                           single_net=False, p_net=nets[1], n_net=nets[0],
                           current=0.05)
    SS.save_setup(prj, pcb, [sink], [], extracted=loaded.extracted)

    def hit(stages):
        return any("Solve cache hit" in s for s in stages)

    stages, md = _run_import(prj, pcb)
    assert not hit(stages)
    assert [d["designator"] for d in md["setup_restore"]["applied"]] == [part]

    stages, md = _run_import(prj, pcb)
    assert hit(stages)
    assert [d["designator"] for d in md["setup_restore"]["applied"]] == [part]

    # A plain import (no setup restore) must never get the with-setup solve.
    # (A changed setup is a different key too - see the setup_hash tests.)
    stages, md = _run_import(prj, pcb, restore=False)
    assert not hit(stages) and "setup_restore" not in md


def test_report_warns_about_held_entries():
    from fypa.report.checks import design_findings

    out = design_findings({"setup_held": ["SINK U3 on +3V3 - gone"]}, None,
                          False)
    (f,) = [f for f in out if f.code == "setup-held"]
    assert "SINK U3" in f.detail


# --- clean import -------------------------------------------------------------

def test_clean_import_sets_the_saved_setup_aside_as_a_backup(paths):
    prj, pcb, _ = paths
    where = SS.save_setup(prj, pcb, [_sink()], [], extracted=_design(_comp("U1")))
    old_bak = where.with_name(where.name + ".bak")
    old_bak.write_text("{}")            # an older backup is replaced
    assert SS.set_aside_setup(prj, pcb) == [old_bak]
    assert not where.exists() and SS.load_setup(prj, pcb) is None
    # The backup is still a setup file Import Setup can read.
    back = SS.load_setup_file(old_bak)
    assert [d.designator for d in back.directives] == ["U1"]
    # Nothing saved: nothing to set aside.
    assert SS.set_aside_setup(prj, pcb) == []


def test_clean_import_tells_the_viewer_where_the_setup_went() -> None:
    from fypa.viewer.setup_restore import adopt_restored_setup

    shown = []
    window = SimpleNamespace(
        _ensure_project=lambda: None,
        statusBar=lambda: SimpleNamespace(showMessage=lambda t, _ms: shown.append(t)))
    metadata = {"setup_set_aside": [r"C:\b\Board.fypa-setup.json.bak"]}
    # Nothing restored, so the caller still offers the setup chooser.
    assert adopt_restored_setup(window, metadata, None) is False
    assert "setup_set_aside" not in metadata
    assert len(shown) == 1 and "Board.fypa-setup.json.bak" in shown[0]
    assert "Import Setup from Another Design" in shown[0]
