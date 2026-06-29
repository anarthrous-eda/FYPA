"""PDN_* annotation parser tests — single-net (PDN_NET) validation.

These exercise the parser's pure logic directly (no Altium extraction):
``_terminal_mode`` decides single-net vs two-terminal per channel, and
``_validate_directive_groups`` enforces the cross-directive rules — mode
consistency within an analysis group, the open-loop check, and return-group
assignment. See ``fypa.altium.annotations`` for the schema.
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass, field
from pathlib import Path

from fypa.altium.annotations import (
    AnnotationResult,
    PdnParameterSource,
    RegulatorSpec,
    ResistorSpec,
    SinkSpec,
    SourceSpec,
    TerminalPin,
    TerminalSpec,
    _collect_bridge_groups,
    _iter_pdn_parameter_sources,
    _require_value,
    _resolve_local_net_pins,
    _resolve_terminal,
    _schdoc_for_pcb_instance,
    _sheet_name_matches,
    _terminal_mode,
    _validate_directive_groups,
    parse_annotations,
    parse_si_value,
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


# --- parse_si_value -----------------------------------------------------------

class TestParseSiValue:
    def test_whitespace_before_unit(self):
        assert parse_si_value("100 mA") == 0.1
        assert parse_si_value("3.3 V") == 3.3
        assert parse_si_value("  -2.7 V ") == -2.7

    def test_scientific_notation(self):
        assert parse_si_value("1.5E-9") == 1.5e-9
        assert parse_si_value("1.5e-9") == 1.5e-9
        assert parse_si_value("2.2E+6") == 2.2e6
        assert parse_si_value("5E-3") == 0.005
        assert parse_si_value("1.5E-9F") == 1.5e-9

    def test_regression_engineering_and_si(self):
        assert parse_si_value("500mA") == 0.5
        assert parse_si_value("3V3") == 3.3
        assert parse_si_value("4k7") == 4700.0
        assert parse_si_value("1MΩ") == 1e6
        assert parse_si_value("-2.7") == -2.7
        assert parse_si_value(".001") == 0.001

    def test_spaced_exponent_not_joined(self):
        with pytest.raises(ValueError):
            parse_si_value("1.5 E-9")

    def test_require_value_appends_syntax_hint(self):
        result = AnnotationResult()
        assert _require_value({"PDN_I": "foo"}, "PDN_I", "SINK on U1", result) is None
        assert len(result.errors) == 1
        assert "use forms like 100mA, 3V3, 1.5E-9" in result.errors[0]


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


def test_single_net_open_loop_source_without_sink_is_not_an_error():
    # The open-loop check moved out of _validate_directive_groups into
    # loader._flag_open_loop_rails (so the rail is skipped + warned, not a
    # whole-board hard error). Validation must no longer error here.
    result = AnnotationResult(directives=[_single_source(0)])
    _validate_directive_groups(result, None, {})
    assert not result.errors


def test_single_net_open_loop_sink_without_source_is_not_an_error():
    result = AnnotationResult(directives=[_single_sink(0)])
    _validate_directive_groups(result, None, {})
    assert not result.errors


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


# --- _flag_open_loop_rails (loader) ------------------------------------------
#
# Single-type rails (only sources or only sinks) can't carry current. The
# loader flags them: their directives are marked solve_excluded (and skipped
# by build_problem's network loop) but kept in the directive list so the
# viewer still draws the markers, with one warning per skipped rail.

from types import SimpleNamespace  # noqa: E402

from fypa.altium.loader import _flag_open_loop_rails  # noqa: E402


def _fake_loaded(directives, net_names):
    nets = [SimpleNamespace(name=n) for n in net_names]
    return SimpleNamespace(
        extracted=SimpleNamespace(nets=nets),
        annotations=AnnotationResult(directives=list(directives)),
    )


def test_flag_open_loop_source_only_rail_excluded_and_warned():
    loaded = _fake_loaded([_single_source(0, "J1")], ["+3V3"])
    warnings = _flag_open_loop_rails(loaded)
    assert len(warnings) == 1
    assert "+3V3" in warnings[0] and "no SINK" in warnings[0]
    # Directive kept (marker stays) but marked excluded from the FEM.
    assert len(loaded.annotations.directives) == 1
    assert loaded.annotations.directives[0].solve_excluded is True
    assert loaded.annotations.open_loop_rails == warnings


def test_flag_open_loop_sink_only_rail_excluded_and_warned():
    loaded = _fake_loaded([_single_sink(0, "U1")], ["+5V"])
    warnings = _flag_open_loop_rails(loaded)
    assert len(warnings) == 1
    assert "+5V" in warnings[0] and "no SOURCE" in warnings[0]
    assert loaded.annotations.directives[0].solve_excluded is True


def test_flag_open_loop_closed_rail_not_flagged():
    # A normal source+sink rail carries current — nothing excluded or warned.
    loaded = _fake_loaded(
        [_single_source(0, "J1"), _single_sink(0, "U1")], ["+3V3"])
    warnings = _flag_open_loop_rails(loaded)
    assert warnings == []
    assert all(not d.solve_excluded for d in loaded.annotations.directives)


def test_flag_open_loop_skips_one_rail_keeps_other():
    # Net 0 is a closed rail; net 5 is a sink-only rail — only the latter is
    # flagged, the closed rail's directives stay solvable.
    loaded = _fake_loaded([
        _single_source(0, "J1"), _single_sink(0, "U1"),
        _single_sink(5, "U2"),
    ], ["+3V3", "x", "y", "z", "w", "+1V8"])
    warnings = _flag_open_loop_rails(loaded)
    assert len(warnings) == 1
    assert "+1V8" in warnings[0]
    by_des = {d.designator: d for d in loaded.annotations.directives}
    assert by_des["J1"].solve_excluded is False
    assert by_des["U1"].solve_excluded is False
    assert by_des["U2"].solve_excluded is True


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
                    "PDN_I": "100 mA",
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
    assert result.directives[0].current == 0.1


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


def _pad(comp_idx: int, pin: str, net_index: int, x: float = 0.0) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0), width_mm=1, height_mm=1, hole_mm=0,
        shape=2, rotation_deg=0, layer_id=1, net_index=net_index,
        designator=pin, component_index=comp_idx,
        is_through_hole=False, is_smt=True,
    )


def test_regulator_two_indexed_channels():
    # Nets: 0=GND, 1=+5V, 2=+3V3, 3=+1V8
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("+5V"), RawNet("+3V3"), RawNet("+1V8"),
        ),
        sch_components=(
            RawSchComponent(
                designator="U4", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3", "PDN_GAIN": "0.9",
                    "PDN_OUT_P_NET": "+3V3", "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "+5V", "PDN_IN_N_NET": "GND",
                    "PDN1_V": "1.8", "PDN1_GAIN": "0.85",
                    "PDN1_OUT_P_NET": "+1V8", "PDN1_OUT_N_NET": "GND",
                    "PDN1_IN_P_NET": "+5V", "PDN1_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U4",
            ),
        ),
        pads=(
            _pad(0, "1", 2, 0),   # +3V3 out ch0
            _pad(0, "2", 3, 1),   # +1V8 out ch1
            _pad(0, "3", 1, 2),   # +5V in (shared)
            _pad(0, "4", 0, 3),   # GND return (shared)
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    regs = [d for d in result.directives if isinstance(d, RegulatorSpec)]
    assert len(regs) == 2
    by_ch = {d.channel_index: d for d in regs}
    assert by_ch[None].voltage == 3.3
    assert by_ch[1].voltage == 1.8


def test_series_two_indexed_channels_with_pin_overrides():
    proj = _minimal_proj(
        nets=(
            RawNet("NET_A"), RawNet("NET_B"),
            RawNet("NET_C"), RawNet("NET_D"),
        ),
        sch_components=(
            RawSchComponent(
                designator="FB1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "0.1",
                    "PDN1_P_PINS": "1",
                    "PDN1_N_PINS": "2",
                    "PDN2_R": "0.2",
                    "PDN2_P_PINS": "3",
                    "PDN2_N_PINS": "4",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="FB1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1206-4", source_designator="FB1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0),
            _pad(0, "2", 1, 1),
            _pad(0, "3", 2, 2),
            _pad(0, "4", 3, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    series = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(series) == 2
    by_ch = {d.channel_index: d for d in series}
    assert by_ch[1].resistance == 0.1
    assert by_ch[2].resistance == 0.2


def test_series_auto_infer_single_channel_only():
    proj = _minimal_proj(
        nets=(RawNet("NET_A"), RawNet("NET_B")),
        sch_components=(
            RawSchComponent(
                designator="R7", schdoc_name="Pwr.SchDoc",
                parameters={"PDN_ROLE": "SERIES", "PDN_R": "0.01"},
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="R7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="0402", source_designator="R7",
            ),
        ),
        pads=(_pad(0, "1", 0), _pad(0, "2", 1, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    assert len(result.directives) == 1
    assert isinstance(result.directives[0], ResistorSpec)
    assert result.directives[0].channel_index is None

    proj_multi = _minimal_proj(
        nets=(RawNet("A"), RawNet("B"), RawNet("C"), RawNet("D")),
        sch_components=(
            RawSchComponent(
                designator="FB1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "0.1",
                    "PDN2_R": "0.2",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="FB1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1206-4", source_designator="FB1",
            ),
        ),
        pads=(
            _pad(0, "1", 0), _pad(0, "2", 1, 1),
            _pad(0, "3", 2, 2), _pad(0, "4", 3, 3),
        ),
    )
    bad = parse_annotations(proj_multi, enabled_layers=[1])
    assert not bad.ok
    assert any("multi-channel SERIES requires explicit" in e for e in bad.errors)


def test_series_nested_pcb_placement_and_indexed_channels():
    proj = _minimal_proj(
        nets=(RawNet("A"), RawNet("B"), RawNet("C"), RawNet("D")),
        sch_components=(
            RawSchComponent(
                designator="FB1", schdoc_name="Child.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "0.05",
                    "PDN_P_PINS": "1",
                    "PDN_N_PINS": "2",
                    "PDN1_R": "0.1",
                    "PDN1_P_PINS": "1",
                    "PDN1_N_PINS": "2",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="FB1_CH1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="0402", source_designator="FB1",
            ),
            RawPcbComponent(
                designator="FB1_CH2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="0402", source_designator="FB1",
            ),
        ),
        pads=(
            _pad(0, "1", 0), _pad(0, "2", 1, 1),
            _pad(1, "1", 2, 2), _pad(1, "2", 3, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    series = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(series) == 4
    labels = {
        (d.designator, d.channel_index, d.p.pins[0].net_index)
        for d in series
    }
    assert ("FB1_CH1", None, 0) in labels
    assert ("FB1_CH1", 1, 0) in labels
    assert ("FB1_CH2", None, 2) in labels
    assert ("FB1_CH2", 1, 2) in labels


def test_bridge_groups_indexed_series_nets():
    source = PdnParameterSource(
        designator="FB1",
        schdoc_name="Pwr.SchDoc",
        parameters={
            "PDN_ROLE": "SERIES",
            "PDN1_R": "0.1",
            "PDN1_P_NET": "RAIL_A",
            "PDN1_N_NET": "RAIL_B",
            "PDN2_R": "0.1",
            "PDN2_P_NET": "RAIL_C",
            "PDN2_N_NET": "RAIL_D",
        },
    )
    proj = _minimal_proj()
    groups = _collect_bridge_groups([source], proj)
    assert "RAIL_A" in groups
    assert "RAIL_B" in groups["RAIL_A"]
    assert "RAIL_C" in groups
    assert "RAIL_D" in groups["RAIL_C"]
    assert "RAIL_A" not in groups["RAIL_C"]


def test_sheet_name_matches_full_path_not_basename_collision():
    assert _sheet_name_matches(
        "SubA/Power.SchDoc",
        ["SubA/Power.SchDoc"],
    )
    assert not _sheet_name_matches(
        "SubA/Power.SchDoc",
        ["SubB/Power.SchDoc"],
    )
    assert _sheet_name_matches(
        "Power.SchDoc",
        ["SubB/Power.SchDoc"],
    )


def test_pcb_sourced_local_net_scoped_per_instance():
    """Blanket/ECO PCB parameters resolve local net names per pcb_index."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="CH1_+3V3",
            aliases=["+3V3"],
            source_sheets=["child1.schdoc"],
            terminals=[_FakeTerminal("U1", "1")],
        ),
        _FakeNet(
            name="CH2_+3V3",
            aliases=["+3V3"],
            source_sheets=["child2.schdoc"],
            terminals=[_FakeTerminal("U1", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("CH1_+3V3"), RawNet("CH2_+3V3")),
        pcb_components=(
            RawPcbComponent(
                designator="U1_CH1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U1",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "10mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                },
            ),
            RawPcbComponent(
                designator="U1_CH2", center=Pt2D(1, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U1",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "20mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                },
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Child1.SchDoc",
                parameters={"Comment": "IC"}, pin_designators=("1",),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Child2.SchDoc",
                parameters={"Comment": "IC"}, pin_designators=("1",),
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),
            _pad(0, "2", 0, 0.5),
            _pad(1, "1", 2, 1),
            _pad(1, "2", 0, 1.5),
        ),
        compiled_netlist=netlist,
    )
    sources = _iter_pdn_parameter_sources(proj)
    assert len(sources) == 2
    assert _schdoc_for_pcb_instance(proj, 0, "U1") == "child1.schdoc"
    assert _schdoc_for_pcb_instance(proj, 1, "U1") == "child2.schdoc"

    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 2
    by_des = {d.designator: d for d in sinks}
    assert by_des["U1_CH1"].p.pins[0].net_index == 1
    assert by_des["U1_CH2"].p.pins[0].net_index == 2


def test_local_fallback_skips_no_net_pad():
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="Sheet1_+3V3",
            aliases=["+3V3"],
            source_sheets=["power.schdoc"],
            terminals=[
                _FakeTerminal("U1", "1"),
                _FakeTerminal("U1", "2"),
            ],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            RawPad(
                center=Pt2D(0, 0), width_mm=1, height_mm=1, hole_mm=0,
                shape=2, rotation_deg=0, layer_id=1, net_index=-1,
                designator="1", component_index=0,
                is_through_hole=False, is_smt=True,
            ),
            _pad(0, "2", 1, 1),
        ),
        compiled_netlist=netlist,
    )
    spec, errors = _resolve_terminal(
        proj, 0, "+3V3", None, [1], "SINK P",
        sch_lookup_designator="U1", schdoc_name="Power.SchDoc",
    )
    assert not errors
    assert spec is not None
    assert len(spec.pins) == 1
    assert spec.pins[0].pad_designator == "2"
    assert spec.pins[0].net_index == 1

