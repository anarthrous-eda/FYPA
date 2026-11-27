"""A sink with an ideal return needs an ideal-return source on its rail.

Its current flows into an ideal 0 V node; only a source with an ideal
return can take it back from there. On a rail supplied only through return
copper (an Altium connector or regulator to GND) that node is a variable no
equation touches - an empty row that left every direct solve failing and the
load stuck in the iterative fallback. The rule lives in fypa.open_loop and
is applied by the solve, the Solve button, Manual setup, the Board form
and the saved-setup restore.
"""
from __future__ import annotations

from types import SimpleNamespace

from fypa import open_loop as OL
from fypa.altium.annotations import (
    AnnotationResult,
    SinkSpec,
    SourceSpec,
    TerminalPin,
    TerminalSpec,
)
from fypa.altium.extract import Pt2D
from fypa.autosetup import manual as M
from fypa.editor_directives import (
    apply_editor_directives,
    rail_copper_return,
    stranded_ideal_sinks,
)
from fypa.project_file import EditorDirective
from fypa.setup_sidecar import RestoreReport
from tests.test_autosetup import _board
from tests.test_editor_regulator import NETS, _loaded, _reg, _source
from tests.test_manual_setup import _free


# --- fypa.open_loop -----------------------------------------------------------


def test_ideal_sink_fed_only_through_copper_has_no_return() -> None:
    items = [OL.Item("source", out={"A"}, ret={"GND"}),
             OL.Item("sink", inp={"A"}, ideal=True)]
    dropped, _ = OL.analyse(items)
    # The source then has nothing drawing from it either.
    assert dropped == {1: OL.NO_RETURN, 0: OL.NO_LOAD}


def test_ideal_sink_closes_through_an_ideal_source_across_a_series_part() -> None:
    items = [OL.Item("source", out={"A"}, ideal=True),
             OL.Item("series", out={"A"}, inp={"B"}),
             OL.Item("sink", inp={"B"}, ideal=True)]
    assert OL.analyse(items)[0] == {}


def test_copper_return_sink_is_unaffected() -> None:
    items = [OL.Item("source", out={"A"}, ret={"GND"}),
             OL.Item("sink", inp={"A"}, ret={"GND"})]
    assert OL.analyse(items)[0] == {}


def test_unsupplied_ideal_sink_is_reported_as_unsupplied() -> None:
    assert OL.analyse([OL.Item("sink", inp={"A"}, ideal=True)])[0] == {
        0: OL.NO_SUPPLY}


# --- fypa.editor_directives ---------------------------------------------------


def _ideal_sink(net: str = "+12V") -> EditorDirective:
    return EditorDirective(kind="free", role="SINK", p_net=net, single_net=True,
                           anchor_xy=(1.0, 0.0), layer_id=1, current=1.0)


def test_stranded_ideal_sinks_names_the_rail_ground() -> None:
    sink = _ideal_sink()
    assert stranded_ideal_sinks(_loaded(), [_source(), sink]) == {sink.id: ["GND"]}
    # An ideal-return source on the rail closes it.
    src = EditorDirective(kind="free", role="SOURCE", p_net="+12V", single_net=True,
                          anchor_xy=(0.0, 0.0), layer_id=1, voltage=12.0)
    assert stranded_ideal_sinks(_loaded(), [src, sink]) == {}


def test_rail_copper_return_for_a_new_marker() -> None:
    loaded = _loaded()
    # Supplied through GND copper: a new marker returns through GND too.
    assert rail_copper_return(loaded, [_source()], "+12V") == "GND"
    assert rail_copper_return(
        loaded, [_source(), _reg("U1", "+3V3", "+12V", 3.3)], "+3V3") == "GND"
    # Nothing on the rail yet, or an ideal-return marker already there:
    # keep the ideal return (Gerber boards, single-net setups).
    assert rail_copper_return(loaded, [], "+12V") is None
    ideal_src = EditorDirective(kind="free", role="SOURCE", p_net="+3V3",
                                single_net=True, voltage=3.3,
                                anchor_xy=(0.0, 0.0), layer_id=1)
    assert rail_copper_return(loaded, [ideal_src], "+3V3") is None


def test_editor_sink_shares_the_schematic_rails_ideal_return() -> None:
    """A PDN_NET schematic source and an ideal-return marker on the same rail
    must share one 0 V node - a fresh node of the marker's own is touched by
    nothing else."""
    loaded = _loaded()
    pin = TerminalPin(pad_designator="1", layer_id=1,
                      net_index=NETS.index("+12V"), point=Pt2D(0, 0))
    sch = SourceSpec(designator="J1", schdoc_name="Power.SchDoc", voltage=12.0,
                     p=TerminalSpec(pins=(pin,)), n=None, return_group=0)
    loaded.annotations = AnnotationResult(directives=[sch])
    warn = apply_editor_directives(loaded, [_ideal_sink()])
    sinks = [d for d in loaded.annotations.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1 and sinks[0].return_group == 0, warn


# --- Manual setup -------------------------------------------------------------


def test_manual_setup_rejects_an_ideal_sink_on_a_copper_supplied_rail() -> None:
    view = _board()
    src = M.new_supply()
    src.p_net, src.designator, src.voltage = "+12V", "J1", 12.0
    M.set_ground(src, "GND")
    sink = _free("SINK", "+12V", current=1.0)
    errs = [i for i in M.validate([M.Entry(src), M.Entry(sink)], view)
            if i.level == "error" and i.entry_id == sink.id]
    assert len(errs) == 1
    assert "supplied through copper" in errs[0].text
    assert "pick GND as its ground" in errs[0].text
    # Given the ground, it's fine.
    M.set_ground(sink, "GND")
    assert not [i for i in M.validate([M.Entry(src), M.Entry(sink)], view)
                if i.level == "error"]


def test_manual_setup_allows_an_ideal_sink_with_an_ideal_source() -> None:
    view = _board()
    src = _free("SOURCE", "+12V", voltage=12.0)
    sink = _free("SINK", "+12V", current=1.0)
    assert not [i for i in M.validate([M.Entry(src), M.Entry(sink)], view)
                if i.level == "error"]


# --- saved-setup restore ------------------------------------------------------


def test_restore_report_carries_stranded_sinks() -> None:
    r = RestoreReport(stranded={"abc": ["GND"]})
    assert RestoreReport.from_metadata(r.to_metadata()).stranded == {"abc": ["GND"]}
    # Reports cached before the field existed.
    assert RestoreReport.from_metadata({}).stranded == {}


def test_restore_notice_lists_a_stranded_sink(monkeypatch) -> None:
    from fypa.viewer import setup_restore as SR

    sink = _ideal_sink()
    report = RestoreReport(applied=[sink], stranded={sink.id: ["GND"]})
    shown = {}

    def _notice(_w, title, intro, rows, outro):
        shown.update(title=title, rows=rows)

    monkeypatch.setattr("fypa.viewer.list_dialog.show_list_notice", _notice)
    monkeypatch.setattr(SR, "save_setup_now", lambda w: None)
    monkeypatch.setattr(SR, "stamp_held", lambda w: None)
    project = SimpleNamespace(editor_directives=[], copper_names=[])
    window = SimpleNamespace(_ensure_project=lambda: project)
    assert SR.adopt_restored_setup(
        window, {"setup_restore": report.to_metadata()},
        SimpleNamespace(solver_info={})) is True
    assert shown["title"] == "Saved setup restored - check these entries"
    assert len(shown["rows"]) == 1 and "give it GND as its ground" in shown["rows"][0]
    assert project.editor_directives == [sink]
