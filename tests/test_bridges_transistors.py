"""Transistors in the Bridges tab: set a FET up as switched on.

A transistor joins its drain and source only when driven, so FYPA never
bridges one on its own. The Bridges tab lists them so the user can say "this
one is on", choose which pads form each side (a power FET's drain and source
are usually several pads each) and give it Rds(on). These tests pin:

* the candidate record — default sides, Rds(on) pre-fill, pre-merge nets;
* the saved editor directive — pins, nets, and that it resolves to a real
  two-net SERIES element that is never merged;
* the tab — the picker, and the choices it refuses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fypa.altium.annotations import ResistorSpec, parse_annotations
from fypa.altium.extract import (
    ExtractedProject,
    Pt2D,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawSchComponent,
    RawStackupLayer,
)
from fypa.altium.loader import (
    LoadedProject,
    _AbsorbedBridge,
    _premerge_pad_nets,
    _rds_on_from_parameters,
    collect_bridge_candidates,
    replace_loaded,
)

# Net indices
P12, SNS, GATE, GND = 0, 1, 2, 3


def _stackup():
    return (RawStackupLayer(
        layer_id=1, name="Top", copper_thickness_mm=0.035,
        dielectric_thickness_mm=0.0, next_layer_id=0,
        is_plane=False, plane_net_name=None, mech_enabled=True,
    ),)


def _pad(pin: str, net: int, x: float, w: float = 0.7, h: float = 1.0,
         comp: int = 0) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0), width_mm=w, height_mm=h, hole_mm=0,
        shape=2, rotation_deg=0, layer_id=1, net_index=net,
        designator=pin, component_index=comp,
        is_through_hole=False, is_smt=True,
    )


def _fet_proj(*, params=None, drain_net=SNS) -> ExtractedProject:
    """An LFPAK-style FET: source pins 1-3, gate pin 4, a big drain tab 5.

    The drain tab is one pad and so is the gate — only pad *area* tells
    them apart, which is what the default-side guess has to get right.
    """
    params = {"Comment": "PSMNR67-30YLEX"} if params is None else params
    return ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(), stackup=_stackup(), compiled_netlist=None,
        nets=(RawNet("+12V"), RawNet("HS_SNS"), RawNet("HS_GATE1"),
              RawNet("GND")),
        sch_components=(RawSchComponent(
            designator="Q4", schdoc_name="HotSwap.SchDoc",
            parameters=dict(params), pin_designators=("1", "2", "3", "4", "5"),
        ),),
        pcb_components=(RawPcbComponent(
            designator="Q4", center=Pt2D(2, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="LFPAK56", source_designator="Q4",
        ),),
        pads=(_pad("1", P12, 0.0), _pad("2", P12, 1.0), _pad("3", P12, 2.0),
              _pad("4", GATE, 3.0),
              _pad("5", drain_net, 1.5, w=4.0, h=3.5)),
    )


def _loaded(proj, **kw) -> LoadedProject:
    ann = parse_annotations(proj, enabled_layers=[1])
    return LoadedProject(extracted=proj, annotations=ann, **kw)


def _q4(loaded) -> dict:
    return next(r for r in collect_bridge_candidates(loaded)
                if r["designator"] == "Q4")


# --- the candidate record -------------------------------------------------

def test_a_fet_is_listed_as_an_open_transistor():
    rec = _q4(_loaded(_fet_proj()))
    assert rec["fet"] is True
    assert rec["kind"] == "transistor"
    assert rec["state"] == "open"
    assert rec["resistance_ohm"] is None
    # Never an advisory: whether it conducts depends on the gate drive.
    assert rec["impact"] == ""


def test_default_sides_are_drain_tab_and_source_pins_not_the_gate():
    """The drain tab and the gate are both single pads; the gate is the one
    on the net named like a gate (HS_GATE1)."""
    rec = _q4(_loaded(_fet_proj()))
    assert {tuple(rec["default_a_pins"]), tuple(rec["default_b_pins"])} == {
        ("5",), ("1", "2", "3")}
    assert "4" not in rec["default_a_pins"] + rec["default_b_pins"]
    assert {rec["net_a"], rec["net_b"]} == {"+12V", "HS_SNS"}
    assert "HS_GATE1" in rec["nets"]


def test_no_guess_when_the_gate_cannot_be_named():
    """SOT-23 on nets named nothing like a gate (Mangala's Q1: NetQ1_3,
    GPIO1, GND). Equal pads give no clue either, and a wrong guess would
    offer to short the drain to the gate — so no guess."""
    import dataclasses
    proj = _fet_proj()
    sot23 = (_pad("1", GND, 0.0), _pad("2", P12, 1.0), _pad("3", SNS, 2.0))
    proj = dataclasses.replace(proj, pads=sot23)
    rec = _q4(_loaded(proj))
    assert rec["default_a_pins"] == [] and rec["default_b_pins"] == []
    assert "gate could not be told" in rec["why"]


def test_tiny_placeholder_drain_pad_still_gets_the_right_sides():
    """Mangala's Q4: the drain "pad" 5 is a 0.025 mm placeholder (the tab is
    copper outside the pad record). Pad area would call it the gate; the
    gate's net name does not lie."""
    import dataclasses
    proj = _fet_proj()
    pads = proj.pads[:4] + (_pad("5", SNS, 1.5, w=0.0254, h=0.0254),)
    rec = _q4(_loaded(dataclasses.replace(proj, pads=pads)))
    assert rec["default_a_pins"] == ["1", "2", "3"]
    assert rec["default_b_pins"] == ["5"]


def test_a_pad_named_g_marks_the_gate():
    import dataclasses
    proj = _fet_proj()
    proj = dataclasses.replace(
        proj, nets=(RawNet("+12V"), RawNet("HS_SNS"), RawNet("DRV"),
                    RawNet("GND")),
        pads=(_pad("S", P12, 0.0), _pad("G", GATE, 1.0), _pad("D", SNS, 2.0)))
    rec = _q4(_loaded(proj))
    assert {tuple(rec["default_a_pins"]), tuple(rec["default_b_pins"])} == {
        ("S",), ("D",)}


def test_every_pad_is_reported_once_in_pin_order():
    rec = _q4(_loaded(_fet_proj()))
    assert [p["pad"] for p in rec["pads"]] == ["1", "2", "3", "4", "5"]
    assert rec["pin_count"] == 5


def test_a_transistor_on_one_net_is_not_a_candidate():
    proj = _fet_proj(drain_net=P12)
    import dataclasses
    proj = dataclasses.replace(proj, pads=tuple(
        dataclasses.replace(p, net_index=P12) for p in proj.pads))
    assert not [r for r in collect_bridge_candidates(_loaded(proj))
                if r["designator"] == "Q4"]


@pytest.mark.parametrize("params,expected", [
    ({"Drain to Source Resistance": "700µΩ"}, 700e-6),
    ({"Drain to Source Resistance": "700µO"}, 700e-6),   # mangled Ω
    ({"RDS(on)": "1.2 mΩ @ 10V"}, 1.2e-3),
    ({"Rds On Max": "0.0045"}, 0.0045),
    ({"Drain-Source On Resistance": "3.1m"}, 3.1e-3),
])
def test_rds_on_is_read_from_the_part(params, expected):
    assert _rds_on_from_parameters(params) == pytest.approx(expected)


@pytest.mark.parametrize("params", [
    {}, {"Value": "10k"}, {"Drain to Source Voltage (Vds)": "30V"},
    {"Drain to Source Resistance": "n/a"},
])
def test_no_rds_on_when_the_part_does_not_say(params):
    assert _rds_on_from_parameters(params) is None


def test_rds_on_lands_on_the_record():
    rec = _q4(_loaded(_fet_proj(
        params={"Drain to Source Resistance": "700µΩ"})))
    assert rec["rds_on_ohm"] == pytest.approx(700e-6)


# --- nets the low-ohm merge folded away -----------------------------------

def _merged_q4() -> dict:
    """Mangala's case: a 750 µΩ shunt merged HS_SNS into +12V, so the drain
    tab now reports +12V like the source pins."""
    proj = _fet_proj(drain_net=P12)   # post-merge: drain pad on +12V
    absorbed = _AbsorbedBridge(
        designator="R80", resistance=0.00075,
        p_layer_id=1, p_x_mm=0.0, p_y_mm=0.0,
        n_layer_id=1, n_x_mm=1.0, n_y_mm=0.0,
        canonical_net_index=P12, p_net_index=P12, n_net_index=SNS,
    )
    loaded = _loaded(proj, absorbed_bridges=[absorbed],
                     premerge_pad_nets={(0, "5"): SNS})
    return _q4(loaded)


def test_pre_merge_net_is_shown_and_blamed_on_the_merging_part():
    rec = _merged_q4()
    tab = next(p for p in rec["pads"] if p["pad"] == "5")
    assert tab["orig_net"] == "HS_SNS"
    assert tab["net"] == "+12V"
    assert tab["merged_by"] == ["R80"]


def _bridge(des, a, b, canonical=P12):
    return _AbsorbedBridge(
        designator=des, resistance=0.0005,
        p_layer_id=1, p_x_mm=0.0, p_y_mm=0.0,
        n_layer_id=1, n_x_mm=1.0, n_y_mm=0.0,
        canonical_net_index=canonical, p_net_index=a, n_net_index=b)


def test_multi_hop_merge_names_every_link_in_order():
    """Mangala: R80 joins HS_SNS to +12V_IN, and R1 / R2 join +12V_IN to
    +12V through +12V_MEZZ. Blaming R80 alone said HS_SNS was merged into
    +12V by R80, which is not what R80 does."""
    import dataclasses
    IN, MEZZ = 4, 5
    proj = _fet_proj(drain_net=P12)
    proj = dataclasses.replace(
        proj, nets=proj.nets + (RawNet("+12V_IN"), RawNet("+12V_MEZZ")))
    bridges = [_bridge("R2", MEZZ, P12), _bridge("R80", IN, SNS),
               _bridge("R1", IN, MEZZ)]
    rec = _q4(_loaded(proj, absorbed_bridges=bridges,
                      premerge_pad_nets={(0, "5"): SNS}))
    tab = next(p for p in rec["pads"] if p["pad"] == "5")
    assert tab["merge_path"] == [
        {"via": "R80", "net": "+12V_IN"},
        {"via": "R1", "net": "+12V_MEZZ"},
        {"via": "R2", "net": "+12V"},
    ]
    assert tab["merged_by"] == ["R80", "R1", "R2"]


def test_merge_path_takes_the_shortest_chain_and_handles_no_chain():
    from fypa.altium.loader import merge_path
    names = ["A", "B", "C", "D"].__getitem__
    bridges = [_bridge("R1", 0, 1), _bridge("R2", 1, 2), _bridge("R3", 0, 2)]
    assert merge_path(bridges, 0, 2, names) == [{"via": "R3", "net": "C"}]
    assert merge_path(bridges, 0, 3, names) == []
    assert merge_path(bridges, 1, 1, names) == []


def test_sides_are_still_guessed_from_the_pre_merge_nets():
    rec = _merged_q4()
    assert {tuple(rec["default_a_pins"]), tuple(rec["default_b_pins"])} == {
        ("5",), ("1", "2", "3")}


def test_premerge_pad_nets_records_only_moved_component_pads():
    proj = _fet_proj()
    got = _premerge_pad_nets(proj, {SNS: P12})
    assert got == {(0, "5"): SNS}


def test_replace_loaded_keeps_premerge_pad_nets():
    loaded = _loaded(_fet_proj(), premerge_pad_nets={(0, "5"): SNS})
    assert replace_loaded(loaded).premerge_pad_nets == {(0, "5"): SNS}


def test_no_default_sides_when_drain_was_merged_without_a_record():
    """A design cached before pre-merge nets were kept: the drain tab reads
    +12V like the source pins, leaving +12V and the gate. Pairing those
    would offer to short +12V to the gate, so no guess is made."""
    loaded = _loaded(_fet_proj(drain_net=P12))
    rec = _q4(loaded)
    assert rec["default_a_pins"] == [] and rec["default_b_pins"] == []
    assert "pick the pins yourself" in rec["why"]


def test_multichannel_fets_are_one_row_per_placement():
    """Every channel shares the schematic designator; a row (and a saved
    directive) keyed on it would bind to whichever placement resolves
    first. Rows use the physical designator, and the schematic part still
    supplies Rds(on)."""
    import dataclasses
    from fypa.editor_directives import apply_editor_directives
    base = _fet_proj(params={"Drain to Source Resistance": "2m"})
    comp = base.pcb_components[0]
    ch0 = dataclasses.replace(comp, designator="Q4_CH0")
    ch1 = dataclasses.replace(comp, designator="Q4_CH1",
                              center=Pt2D(20, 0))
    pads = base.pads + tuple(dataclasses.replace(
        p, component_index=1, center=Pt2D(p.center.x + 20, 0))
        for p in base.pads)
    proj = dataclasses.replace(base, pcb_components=(ch0, ch1), pads=pads)
    loaded = _loaded(proj)
    rows = {r["designator"]: r for r in collect_bridge_candidates(loaded)
            if r.get("fet")}
    assert set(rows) == {"Q4_CH0", "Q4_CH1"}
    assert rows["Q4_CH1"]["rds_on_ohm"] == pytest.approx(2e-3)

    from fypa.project_file import EditorDirective
    apply_editor_directives(loaded, [EditorDirective(
        kind="component", role="SERIES", designator="Q4_CH1",
        single_net=False, p_net="HS_SNS", n_net="+12V",
        p_pins=["5"], n_pins=["1", "2", "3"], resistance=2e-3,
        overrides_designator="Q4_CH1")])
    spec = next(d for d in loaded.annotations.directives
                if isinstance(d, ResistorSpec) and d.designator == "Q4_CH1")
    assert {p.point.x for p in spec.n.pins} == {20.0, 21.0, 22.0}
    states = {r["designator"]: r["state"]
              for r in collect_bridge_candidates(loaded) if r.get("fet")}
    assert states == {"Q4_CH0": "open", "Q4_CH1": "series"}


def test_old_cached_project_without_the_field_still_lists_fets():
    loaded = _loaded(_fet_proj())
    del loaded.premerge_pad_nets       # an older design-info pickle
    assert _q4(loaded)["state"] == "open"


# --- the saved directive ---------------------------------------------------

def _fet_directive(r=700e-6, a=("5",), b=("1", "2", "3"),
                   p="HS_SNS", n="+12V"):
    from fypa.project_file import EditorDirective
    return EditorDirective(
        kind="component", role="SERIES", designator="Q4", single_net=False,
        p_net=p, n_net=n, p_pins=list(a), n_pins=list(b),
        resistance=r, overrides_designator="Q4")


def test_editor_fet_series_resolves_to_the_chosen_pads():
    from fypa.editor_directives import apply_editor_directives
    loaded = _loaded(_fet_proj())
    warnings = apply_editor_directives(loaded, [_fet_directive()])
    assert warnings == []
    spec = next(d for d in loaded.annotations.directives
                if isinstance(d, ResistorSpec) and d.designator == "Q4")
    assert [p.pad_designator for p in spec.p.pins] == ["5"]
    assert sorted(p.pad_designator for p in spec.n.pins) == ["1", "2", "3"]
    assert {p.net_index for p in spec.p.pins} == {SNS}
    assert {p.net_index for p in spec.n.pins} == {P12}


def test_sub_milliohm_editor_series_is_not_merged():
    """Editor directives are applied after the merge pass, so a 0.7 mΩ FET
    stays a real element between two distinct nets (what the tab's hint
    now says) instead of collapsing them."""
    from fypa.editor_directives import apply_editor_directives
    loaded = _loaded(_fet_proj())
    apply_editor_directives(loaded, [_fet_directive(r=700e-6)])
    spec = next(d for d in loaded.annotations.directives
                if isinstance(d, ResistorSpec) and d.designator == "Q4")
    assert spec.resistance == pytest.approx(700e-6)
    assert ({p.net_index for p in spec.p.pins}
            != {p.net_index for p in spec.n.pins})
    assert not loaded.merged_net_names


def test_modelled_fet_reports_series_with_its_pins():
    from fypa.editor_directives import apply_editor_directives
    loaded = _loaded(_fet_proj())
    apply_editor_directives(loaded, [_fet_directive()])
    rec = _q4(loaded)
    assert rec["state"] == "series"
    assert rec["resistance_ohm"] == pytest.approx(700e-6)
    assert rec["a_pins"] == ["5"]
    assert sorted(rec["b_pins"]) == ["1", "2", "3"]
    assert (rec["net_a"], rec["net_b"]) == ("HS_SNS", "+12V")


def test_fet_directive_round_trips_through_the_project_file(tmp_path):
    from fypa.project_file import ProjectFile
    proj = ProjectFile()
    proj.upsert_directive(_fet_directive())
    path = tmp_path / "p.fypa"
    proj.save(path)
    ed = ProjectFile.load(path).editor_directives[0]
    assert ed.p_pins == ["5"] and ed.n_pins == ["1", "2", "3"]


# --- the tab ---------------------------------------------------------------

def _fet_row(**over) -> dict:
    rec = {
        "designator": "Q4", "kind": "transistor", "value": "PSMNR67-30YLEX",
        "footprint": "LFPAK56", "pin_count": 5,
        "net_a": "HS_SNS", "net_b": "+12V",
        "nets": ["HS_SNS", "+12V", "HS_GATE1"],
        "state": "open", "resistance_ohm": None, "why": "left open",
        "impact": "", "excluded_net": "", "touches_active_rail": False,
        "x_mm": 2.0, "y_mm": 0.0, "layer_id": 1, "fet": True,
        "pads": [
            {"pad": "1", "net": "+12V", "orig_net": "+12V", "merged_by": []},
            {"pad": "2", "net": "+12V", "orig_net": "+12V", "merged_by": []},
            {"pad": "3", "net": "+12V", "orig_net": "+12V", "merged_by": []},
            {"pad": "4", "net": "HS_GATE1", "orig_net": "HS_GATE1",
             "merged_by": []},
            {"pad": "5", "net": "HS_SNS", "orig_net": "HS_SNS",
             "merged_by": []},
        ],
        "default_a_pins": ["5"], "default_b_pins": ["1", "2", "3"],
        "rds_on_ohm": 700e-6,
    }
    rec.update(over)
    return rec


_MANGALA_PATH = [{"via": "R80", "net": "+12V_IN"},
                 {"via": "R1", "net": "+12V_MEZZ"},
                 {"via": "R2", "net": "+12V"}]


def _merged_row() -> dict:
    row = _fet_row()
    row["pads"][4] = {"pad": "5", "net": "+12V", "orig_net": "HS_SNS",
                      "merge_path": _MANGALA_PATH,
                      "merged_by": ["R80", "R1", "R2"]}
    return row


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _make_viewer(monkeypatch, rows):
    from PySide6.QtWidgets import QMainWindow, QMessageBox
    import fypa.altium_viewer as V
    from fypa.project_file import ProjectFile

    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(
        lambda *a, **k: (dialogs.append(
            ("warning", a[2] if len(a) > 2 else "")), QMessageBox.Cancel)[1]))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(
        lambda *a, **k: dialogs.append(("info", a[2] if len(a) > 2 else ""))))

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {"bridge_candidates": rows}
    v._project = ProjectFile()
    v._project_path = None
    v.tabs = None
    v._highlight_via_xy = None
    v._ensure_project = lambda: v._project
    v._mark_project_dirty = lambda: None
    v._update_pending_rails = lambda: None
    v._update_bridges_tab_title = lambda: None
    v._render = lambda: None
    v._test_tab_widget = V.PdnViewer._build_bridges_tab(v)
    V.PdnViewer._populate_bridges_table(v)
    v._test_dialogs = dialogs
    return v


@pytest.fixture
def viewer(qapp, monkeypatch):
    return _make_viewer(monkeypatch, [_fet_row()])


def _select(v, des="Q4"):
    t = v.bridges_table
    row = next(i for i in range(t.rowCount()) if t.item(i, 0).text() == des)
    t.selectRow(row)
    return row


def _fet_eds(v):
    return [d for d in v._project.editor_directives
            if d.role == "SERIES" and d.designator == "Q4"]


def test_transistor_filter_shows_fets_and_not_modelled_does_not(viewer):
    viewer.bridges_state_combo.setCurrentText("Transistors")
    assert not viewer.bridges_table.isRowHidden(0)
    viewer.bridges_state_combo.setCurrentText("Not modelled")
    assert viewer.bridges_table.isRowHidden(0)


def test_gate_net_is_shown_in_the_nets_column(viewer):
    assert "HS_GATE1" in viewer.bridges_table.item(0, 4).text()


def test_picker_appears_only_for_a_transistor(qapp, monkeypatch):
    from tests.test_bridges_tab import _candidates
    v = _make_viewer(monkeypatch, _candidates() + [_fet_row()])
    _select(v, "FB1")
    assert not v.bridges_pin_box.isVisibleTo(v._test_tab_widget)
    _select(v, "Q4")
    assert v.bridges_pin_box.isVisibleTo(v._test_tab_widget)
    assert v.bridges_pin_table.rowCount() == 5


def test_picker_starts_from_the_default_guess(viewer):
    import fypa.altium_viewer as V
    _select(viewer)
    assert V.PdnViewer._bridge_pin_selection(viewer) == (["5"], ["1", "2", "3"])
    assert "HS_SNS" in viewer.bridges_pin_status.text()


def test_clicking_a_fet_prefills_its_rds_on(viewer):
    import fypa.altium_viewer as V
    row = _select(viewer)
    # Column 1, not the Part cell: a Part click also jumps to the Heatmap,
    # which this stub viewer doesn't have. The prefill runs either way.
    V.PdnViewer._on_bridges_cell_clicked(viewer, row, 1)
    assert float(viewer.bridges_r_edit.text()) == pytest.approx(700e-6)


def test_saving_writes_a_pin_filtered_series_directive(viewer):
    import fypa.altium_viewer as V
    _select(viewer)
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), 700e-6)
    eds = _fet_eds(viewer)
    assert len(eds) == 1
    ed = eds[0]
    assert (ed.p_net, ed.n_net) == ("HS_SNS", "+12V")
    assert ed.p_pins == ["5"] and ed.n_pins == ["1", "2", "3"]
    assert ed.resistance == pytest.approx(700e-6)
    assert ed.overrides_designator == "Q4"
    # Transistors are never auto-bridged; nothing to opt out of.
    assert viewer._project.no_auto_bridge == []


def test_saving_uses_the_users_pin_choice(viewer):
    """Only two of the three source pins: the user's call, honoured."""
    import fypa.altium_viewer as V
    _select(viewer)
    V.PdnViewer._set_bridge_pin_side(viewer, "3", "")
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), 1e-3)
    assert _fet_eds(viewer)[0].n_pins == ["1", "2"]


def test_saved_pins_come_back_after_a_save(viewer):
    import fypa.altium_viewer as V
    _select(viewer)
    V.PdnViewer._set_bridge_pin_side(viewer, "3", "")
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), 1e-3)
    row = next(r for r in V.PdnViewer._bridge_rows(viewer)
               if r["designator"] == "Q4")
    assert row["state"] == "series"
    assert row["b_pins"] == ["1", "2"]
    # The row stays selected and the picker shows the saved choice.
    assert V.PdnViewer._bridge_pin_selection(viewer) == (["5"], ["1", "2"])


def test_a_side_spanning_two_nets_is_refused(viewer):
    import fypa.altium_viewer as V
    _select(viewer)
    V.PdnViewer._set_bridge_pin_side(viewer, "4", "B")   # gate onto source
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), 1e-3)
    assert _fet_eds(viewer) == []
    assert any("spans" in d[1] for d in viewer._test_dialogs)


def test_an_empty_side_is_refused(viewer):
    import fypa.altium_viewer as V
    _select(viewer)
    V.PdnViewer._set_bridge_pin_side(viewer, "5", "")
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), 1e-3)
    assert _fet_eds(viewer) == []
    assert "side A" in viewer.bridges_pin_status.text()


def test_non_positive_rds_is_refused(viewer):
    import fypa.altium_viewer as V
    _select(viewer)
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), 0.0)
    assert _fet_eds(viewer) == []


def test_merged_drain_is_refused_and_the_merging_part_named(qapp, monkeypatch):
    """Both sides on +12V after R80's merge: saving would short Q4 to
    itself (and name a net that owns no copper). Say who merged it."""
    import fypa.altium_viewer as V
    v = _make_viewer(monkeypatch, [_merged_row()])
    _select(v)
    assert "R80" in v.bridges_pin_status.text()
    V.PdnViewer._apply_bridge_series(v, _merged_row(), 700e-6)
    assert _fet_eds(v) == []
    msg = " ".join(d[1] for d in v._test_dialogs)
    assert ("HS_SNS was merged into +12V through HS_SNS ─R80→ +12V_IN "
            "─R1→ +12V_MEZZ ─R2→ +12V") in msg
    # Any link in the chain separates drain from source; R80 is the one
    # that also keeps HS_SNS on its own.
    assert "Break any link in that chain" in msg
    assert "R80 is the one that keeps HS_SNS on its own" in msg
    assert "joined to +12V_IN" in msg


@pytest.mark.parametrize("change", ["opt_out", "series"])
def test_a_link_already_changed_here_says_press_resolve(
        qapp, monkeypatch, change):
    """The user disabled R1's auto-bridge: the chain IS broken, but the rows
    come from the last solve. Telling them to break it again was a dead end;
    the message must say the change waits for a rebuild."""
    from fypa.project_file import EditorDirective
    v = _make_viewer(monkeypatch, [_merged_row()])
    if change == "opt_out":
        v._project.no_auto_bridge = ["R1"]
    else:
        v._project.upsert_directive(EditorDirective(
            kind="component", role="SERIES", designator="R1",
            single_net=False, p_net="+12V_IN", n_net="+12V_MEZZ",
            resistance=0.01, overrides_designator="R1"))
    _select(v)
    status = v.bridges_pin_status.text()
    assert "already changed R1" in status
    assert "press Resolve" in status
    assert "Break any link" not in status


def test_merged_net_is_flagged_in_the_picker(qapp, monkeypatch):
    v = _make_viewer(monkeypatch, [_merged_row()])
    _select(v)
    text = v.bridges_pin_table.item(4, 1).text()
    assert text.startswith("HS_SNS → +12V")
    assert "─R80→ +12V_IN ─R1→ +12V_MEZZ ─R2→ +12V" in text


def test_older_record_without_a_path_still_names_the_links(qapp, monkeypatch):
    row = _merged_row()
    del row["pads"][4]["merge_path"]
    v = _make_viewer(monkeypatch, [row])
    _select(v)
    assert "merged by R80, R1, R2" in v.bridges_pin_table.item(4, 1).text()


def test_remove_drops_the_fet_directive(viewer):
    import fypa.altium_viewer as V
    _select(viewer)
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), 1e-3)
    V.PdnViewer._apply_bridge_series(viewer, _fet_row(), None)
    assert _fet_eds(viewer) == []
