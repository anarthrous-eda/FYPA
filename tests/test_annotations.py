"""PDN_* annotation parser tests — single-net (PDN_NET) validation.

These exercise the parser's pure logic directly (no Altium extraction):
``_terminal_mode`` decides single-net vs two-terminal per channel, and
``_validate_directive_groups`` enforces the cross-directive rules — mode
consistency within an analysis group, the open-loop check, and return-group
assignment. See ``fypa.altium.annotations`` for the schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fypa.altium.annotations import (
    AnnotationResult,
    SinkSpec,
    SourceSpec,
    TerminalPin,
    TerminalSpec,
    _iter_pdn_parameter_sources,
    _resolve_local_net_pins,
    _resolve_terminal,
    _terminal_mode,
    _validate_directive_groups,
    parse_annotations,
)
from fypa.altium.extract import (
    ExtractedProject,
    Pt2D,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawSchComponent,
    RawStackupLayer,
)


# --- _terminal_mode -----------------------------------------------------------

def test_terminal_mode_single_net():
    result = AnnotationResult()
    assert _terminal_mode({"PDN_NET": "VBATT"}, None, "SOURCE on J1",
                          result) == "single"
    assert not result.errors


def test_terminal_mode_two_terminal():
    result = AnnotationResult()
    assert _terminal_mode({"PDN_P_NET": "+5V", "PDN_N_NET": "GND"}, None,
                          "SOURCE on U1", result) == "two"
    assert not result.errors


def test_terminal_mode_rejects_mixing_pdn_net_with_p_net():
    result = AnnotationResult()
    mode = _terminal_mode({"PDN_NET": "VBATT", "PDN_P_NET": "+5V"}, None,
                          "SOURCE on J1", result)
    assert mode is None
    assert any("cannot be combined" in e for e in result.errors)


def test_terminal_mode_rejects_no_terminal_net():
    result = AnnotationResult()
    mode = _terminal_mode({}, None, "SINK on U1", result)
    assert mode is None
    assert any("no terminal net" in e for e in result.errors)


def test_terminal_mode_indexed_channel():
    result = AnnotationResult()
    assert _terminal_mode({"PDN2_NET": "VBATT"}, 2, "SINK on U1#2",
                          result) == "single"
    assert not result.errors


# --- _validate_directive_groups ----------------------------------------------

def _term(net_index: int) -> TerminalSpec:
    return TerminalSpec(pins=(TerminalPin(
        pad_designator="1", layer_id=1, net_index=net_index,
        point=Pt2D(0.0, 0.0)),))


def _single_source(net: int, des: str = "J1") -> SourceSpec:
    return SourceSpec(designator=des, schdoc_name="s.SchDoc", voltage=5.0,
                      p=_term(net), n=None)


def _single_sink(net: int, des: str = "U1") -> SinkSpec:
    return SinkSpec(designator=des, schdoc_name="s.SchDoc", current=1.0,
                    p=_term(net), n=None)


def _two_terminal_sink(p_net: int, n_net: int, des: str = "U2") -> SinkSpec:
    return SinkSpec(designator=des, schdoc_name="s.SchDoc", current=1.0,
                    p=_term(p_net), n=_term(n_net))


def test_single_net_group_ok_and_shares_return_group():
    result = AnnotationResult(directives=[
        _single_source(0), _single_sink(0)])
    _validate_directive_groups(result, None, {})
    assert not result.errors
    assert {d.return_group for d in result.directives} == {0}


def test_single_net_open_loop_source_without_sink():
    result = AnnotationResult(directives=[_single_source(0)])
    _validate_directive_groups(result, None, {})
    assert any("no SINK" in e for e in result.errors)


def test_single_net_open_loop_sink_without_source():
    result = AnnotationResult(directives=[_single_sink(0)])
    _validate_directive_groups(result, None, {})
    assert any("no SOURCE" in e for e in result.errors)


def test_group_may_not_mix_single_net_and_two_terminal():
    # Single-net SOURCE and a two-terminal SINK both touch net 0.
    result = AnnotationResult(directives=[
        _single_source(0), _two_terminal_sink(0, 1)])
    _validate_directive_groups(result, None, {})
    assert any("mixes single-net" in e for e in result.errors)


def test_independent_single_net_groups_get_distinct_return_groups():
    result = AnnotationResult(directives=[
        _single_source(0, "J1"), _single_sink(0, "U1"),
        _single_source(5, "J2"), _single_sink(5, "U2")])
    _validate_directive_groups(result, None, {})
    assert not result.errors
    by_des = {d.designator: d for d in result.directives}
    assert by_des["J1"].return_group == by_des["U1"].return_group
    assert by_des["J2"].return_group == by_des["U2"].return_group
    assert by_des["J1"].return_group != by_des["J2"].return_group


def test_two_terminal_only_board_is_unaffected():
    # A normal analysis: no PDN_NET anywhere, no errors, no return groups.
    result = AnnotationResult(directives=[
        SourceSpec(designator="U1", schdoc_name="s.SchDoc", voltage=5.0,
                   p=_term(0), n=_term(1)),
        _two_terminal_sink(0, 1, des="U2")])
    _validate_directive_groups(result, None, {})
    assert not result.errors
    assert all(d.return_group is None for d in result.directives)


# --- PCB parameters + local net resolution ------------------------------------

@dataclass
class _FakeTerminal:
    designator: str
    pin: str


@dataclass
class _FakeNet:
    name: str
    terminals: list[_FakeTerminal] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    source_sheets: list[str] = field(default_factory=list)


@dataclass
class _FakeNetlist:
    nets: list[_FakeNet]


def _minimal_stackup() -> tuple[RawStackupLayer, ...]:
    return (
        RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0,
            is_plane=False, plane_net_name=None, mech_enabled=True,
        ),
    )


def _minimal_proj(**overrides) -> ExtractedProject:
    base = {
        "prjpcb_path": Path("t.PrjPcb"),
        "pcbdoc_path": Path("t.PcbDoc"),
        "tracks": (), "arcs": (), "vias": (), "pads": (), "regions": (),
        "shape_based_regions": (), "fills": (),
        "pcb_components": (), "nets": (), "stackup": _minimal_stackup(),
        "sch_components": (),
        "compiled_netlist": None,
    }
    base.update(overrides)
    return ExtractedProject(**base)


def test_resolve_local_net_pins_finds_alias_on_sheet():
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="Sheet1_+3V3",
            aliases=["+3V3"],
            source_sheets=["power.schdoc"],
            terminals=[_FakeTerminal("U1", "14"), _FakeTerminal("C1", "1")],
        ),
    ])
    pins = _resolve_local_net_pins(netlist, "U1", "Power.SchDoc", "+3V3")
    assert pins == ["14"]


def test_resolve_terminal_local_net_per_channel_instance():
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="CH1_+3V3",
            aliases=["+3V3"],
            source_sheets=["child.schdoc"],
            terminals=[_FakeTerminal("U1", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("CH1_+3V3"), RawNet("CH2_+3V3")),
        pcb_components=(
            RawPcbComponent(
                designator="U1_CH1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U1",
            ),
            RawPcbComponent(
                designator="U1_CH2", center=Pt2D(1, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U1",
            ),
        ),
        pads=(
            RawPad(
                center=Pt2D(0, 0), width_mm=1, height_mm=1, hole_mm=0,
                shape=2, rotation_deg=0, layer_id=1, net_index=0,
                designator="1", component_index=0,
                is_through_hole=False, is_smt=True,
            ),
            RawPad(
                center=Pt2D(1, 0), width_mm=1, height_mm=1, hole_mm=0,
                shape=2, rotation_deg=0, layer_id=1, net_index=1,
                designator="1", component_index=1,
                is_through_hole=False, is_smt=True,
            ),
        ),
        compiled_netlist=netlist,
    )
    warnings: list[str] = []
    spec0, err0 = _resolve_terminal(
        proj, 0, "+3V3", None, [1], "SINK P",
        warnings=warnings,
        sch_lookup_designator="U1", schdoc_name="Child.SchDoc",
    )
    spec1, err1 = _resolve_terminal(
        proj, 1, "+3V3", None, [1], "SINK P",
        warnings=warnings,
        sch_lookup_designator="U1", schdoc_name="Child.SchDoc",
    )
    assert not err0 and not err1
    assert spec0 is not None and spec1 is not None
    assert spec0.pins[0].net_index == 0
    assert spec1.pins[0].net_index == 1
    assert any("resolved local net" in w for w in warnings)


def test_pcb_parameters_create_sink_when_schematic_has_no_role():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        pcb_components=(
            RawPcbComponent(
                designator="U1_PWR", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN",
                source_designator="U1",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                },
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Power.SchDoc",
                parameters={"Comment": "IC"}, pin_designators=("1", "2"),
            ),
        ),
        pads=(
            RawPad(
                center=Pt2D(0, 0), width_mm=1, height_mm=1, hole_mm=0,
                shape=2, rotation_deg=0, layer_id=1, net_index=1,
                designator="1", component_index=0,
                is_through_hole=False, is_smt=True,
            ),
            RawPad(
                center=Pt2D(1, 0), width_mm=1, height_mm=1, hole_mm=0,
                shape=2, rotation_deg=0, layer_id=1, net_index=0,
                designator="2", component_index=0,
                is_through_hole=False, is_smt=True,
            ),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    assert len(result.directives) == 1
    assert isinstance(result.directives[0], SinkSpec)
    assert result.directives[0].designator == "U1_PWR"


def test_schematic_pdn_role_takes_priority_over_pcb_parameters():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN",
                source_designator="U1",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "999mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "50mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pads=(
            RawPad(
                center=Pt2D(0, 0), width_mm=1, height_mm=1, hole_mm=0,
                shape=2, rotation_deg=0, layer_id=1, net_index=1,
                designator="1", component_index=0,
                is_through_hole=False, is_smt=True,
            ),
            RawPad(
                center=Pt2D(1, 0), width_mm=1, height_mm=1, hole_mm=0,
                shape=2, rotation_deg=0, layer_id=1, net_index=0,
                designator="2", component_index=0,
                is_through_hole=False, is_smt=True,
            ),
        ),
    )
    sources = _iter_pdn_parameter_sources(proj)
    assert len(sources) == 1
    assert sources[0].parameters["PDN_I"] == "50mA"
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    assert len(result.directives) == 1
    assert result.directives[0].current == 0.05
