"""PDN_* annotation parser tests — single-net (PDN_NET) validation.

These exercise the parser's pure logic directly (no Altium extraction):
``_terminal_mode`` decides single-net vs two-terminal per channel, and
``_validate_directive_groups`` enforces the cross-directive rules — mode
consistency within an analysis group, the open-loop check, and return-group
assignment. See ``fypa.altium.annotations`` for the schema.
"""
from __future__ import annotations

from unittest.mock import patch

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
    _collect_supply_voltages_by_net,
    _iter_pdn_parameter_sources,
    _lookup_inferred_vin,
    _require_value,
    _resolve_local_net_pins,
    _resolve_terminal,
    _schdoc_for_pcb_instance,
    _instance_resolver,
    _sheet_name_matches,
    _terminal_mode,
    _validate_directive_groups,
    _warn_unknown_pdn_params,
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

    def test_bare_unit_case_insensitive_not_treated_as_prefix(self):
        # A bare trailing unit must not be reinterpreted as an SI prefix,
        # regardless of case: "f"/"F" are Farad, not femto.
        assert parse_si_value("1.5E-9f") == 1.5e-9
        assert parse_si_value("1.5E-9F") == 1.5e-9
        assert parse_si_value("100f") == 100.0
        assert parse_si_value("100F") == 100.0
        # femto remains reachable when a unit follows the prefix.
        assert parse_si_value("2fF") == 2e-15

    def test_regression_engineering_and_si(self):
        assert parse_si_value("500mA") == 0.5
        assert parse_si_value("3V3") == 3.3
        assert parse_si_value("4k7") == 4700.0
        assert parse_si_value("1MΩ") == 1e6
        assert parse_si_value("-2.7") == -2.7
        assert parse_si_value(".001") == 0.001

    def test_ohm_R_shorthand(self):
        # "R" is the EE ohm unit. Without it in the trailing-unit table the
        # milli prefix in "10mR" was silently dropped → 10 Ω (1000× too high)
        # on a very common PDN_R shorthand. Regression for round-2 finding #9.
        assert parse_si_value("10mR") == pytest.approx(0.01)
        assert parse_si_value("0.5mR") == pytest.approx(0.0005)
        assert parse_si_value("10R") == pytest.approx(10.0)
        assert parse_si_value("2R2") == pytest.approx(2.2)   # eng form
        assert parse_si_value("0R01") == pytest.approx(0.01)  # eng form
        # and the equivalent explicit-unit forms agree
        assert parse_si_value("10mOhm") == pytest.approx(0.01)
        assert parse_si_value("10mΩ") == pytest.approx(0.01)

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
    assert any("conflicts with" in e for e in result.errors)


def test_terminal_mode_pins_conflict_suggests_p_pins():
    result = AnnotationResult()
    mode = _terminal_mode(
        {"PDN3_PINS": "A1", "PDN3_P_NET": "LED_R", "PDN3_N_NET": "GND"},
        3, "SINK on U4#3", result,
    )
    assert mode is None
    assert any("PDN3_PINS" in e for e in result.errors)
    assert any("PDN3_P_PINS" in e for e in result.errors)


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


def test_warn_unknown_pdn_pin_typo():
    result = AnnotationResult()
    comp = PdnParameterSource(
        designator="U4", schdoc_name="Main.SchDoc", pcb_index=0,
        parameters={
            "PDN_ROLE": "SINK",
            "PDN2_I": "100mA",
            "PDN2_P_NET": "LED_R",
            "PDN2_N_NET": "GND",
            "PDN2_PIN": "B2",
        },
        sch_lookup_designator="U4",
    )
    _warn_unknown_pdn_params(comp, "SINK", result)
    assert any("PDN2_PIN" in w and "PDN2_P_PINS" in w for w in result.warnings)


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
    _validate_directive_groups(result, None)
    assert not result.errors
    assert {d.return_group for d in result.directives} == {0}


def test_single_net_open_loop_source_without_sink_is_not_an_error():
    # The open-loop check moved out of _validate_directive_groups into
    # loader._flag_open_loop_rails (so the rail is skipped + warned, not a
    # whole-board hard error). Validation must no longer error here.
    result = AnnotationResult(directives=[_single_source(0)])
    _validate_directive_groups(result, None)
    assert not result.errors


def test_single_net_open_loop_sink_without_source_is_not_an_error():
    result = AnnotationResult(directives=[_single_sink(0)])
    _validate_directive_groups(result, None)
    assert not result.errors


def test_group_may_not_mix_single_net_and_two_terminal():
    # Single-net SOURCE and a two-terminal SINK both touch net 0.
    result = AnnotationResult(directives=[
        _single_source(0), _two_terminal_sink(0, 1)])
    _validate_directive_groups(result, None)
    assert any("mixes single-net" in e for e in result.errors)


def test_independent_single_net_groups_get_distinct_return_groups():
    result = AnnotationResult(directives=[
        _single_source(0, "J1"), _single_sink(0, "U1"),
        _single_source(5, "J2"), _single_sink(5, "U2")])
    _validate_directive_groups(result, None)
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
    _validate_directive_groups(result, None)
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


def test_resolve_local_net_pins_matches_multichannel_mangled_alias():
    # Repeated ("multi-channel") sheet: the local label "S00A" inside sheet
    # instance SL8M7 is compiled to the alias "S00A_SL8M7" on the flattened
    # physical net (IOUT3), and the netlist keys terminals by the flattened
    # designator "J3_SL8M7". The caller only knows the BASE designator "J3"
    # (comp.lookup_designator) plus the placed instance "J3_SL8M7"
    # (pcb_designator). Naming the bare label "S00A" must still resolve to this
    # channel's pins via the mangled alias + flattened terminal designator.
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="IOUT3",
            aliases=["S00A_SL8M0", "S00A_SL8M7"],
            source_sheets=["SL8_Module.SchDoc"],
            terminals=[
                _FakeTerminal("J3_SL8M0", "29"),
                _FakeTerminal("J3_SL8M7", "29"),
                _FakeTerminal("J3_SL8M7", "30"),
            ],
        ),
    ])
    pins = _resolve_local_net_pins(
        netlist, "J3", "SL8_Module.SchDoc", "S00A",
        pcb_designator="J3_SL8M7",
    )
    assert pins == ["29", "30"]


def test_resolve_local_net_pins_mangled_alias_is_channel_scoped():
    # The "_<channel>" suffix on the alias must match THIS instance's flattened
    # designator suffix. A net carrying only the SL8M0 alias must not resolve
    # for a SL8M7 connector, even though a stray SL8M7 terminal sits on it.
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="IOUT_OTHER",
            aliases=["S00A_SL8M0"],
            source_sheets=["SL8_Module.SchDoc"],
            terminals=[_FakeTerminal("J3_SL8M7", "29")],
        ),
    ])
    pins = _resolve_local_net_pins(
        netlist, "J3", "SL8_Module.SchDoc", "S00A",
        pcb_designator="J3_SL8M7",
    )
    assert pins == []


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


def test_bridge_series_terminals_stay_on_distinct_nets():
    """A SERIES resistor that *creates* a bridge must keep its two terminals
    on distinct nets.

    Topology: a rail RAIL_OUT is fed through two parallel sense resistors
    R1 (PRE_A↔RAIL_OUT) and R2 (PRE_B↔RAIL_OUT). A SOURCE U9 names RAIL_OUT
    but only has pads on PRE_A / PRE_B — with direct-only terminal resolution
    it does not fan into the bridged PRE nets. Each resistor terminal still
    stays on its single literal net.
    """
    # Nets: 0=GND 1=PRE_A 2=PRE_B 3=RAIL_OUT
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("PRE_A"), RawNet("PRE_B"),
              RawNet("RAIL_OUT")),
        sch_components=(
            RawSchComponent(
                designator="U9", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE", "PDN_V": "1",
                    "PDN_P_NET": "RAIL_OUT", "PDN_N_NET": "GND",
                },
                pin_designators=("A1", "A2", "G1"),
            ),
            RawSchComponent(
                designator="R1", schdoc_name="Pwr.SchDoc",
                parameters={"PDN_ROLE": "SERIES", "PDN_R": "0.01"},
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="R2", schdoc_name="Pwr.SchDoc",
                parameters={"PDN_ROLE": "SERIES", "PDN_R": "0.01"},
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(designator="U9", center=Pt2D(0, 0),
                            rotation_deg=0.0, layer_name="TOP",
                            footprint="QFN", source_designator="U9"),
            RawPcbComponent(designator="R1", center=Pt2D(0, 0),
                            rotation_deg=0.0, layer_name="TOP",
                            footprint="0402", source_designator="R1"),
            RawPcbComponent(designator="R2", center=Pt2D(0, 0),
                            rotation_deg=0.0, layer_name="TOP",
                            footprint="0402", source_designator="R2"),
        ),
        pads=(
            _pad(0, "A1", 1, 0),   # U9 on PRE_A
            _pad(0, "A2", 2, 1),   # U9 on PRE_B
            _pad(0, "G1", 0, 2),   # U9 on GND
            _pad(1, "1", 1, 3),    # R1 pad 1 on PRE_A
            _pad(1, "2", 3, 4),    # R1 pad 2 on RAIL_OUT
            _pad(2, "1", 2, 5),    # R2 pad 1 on PRE_B
            _pad(2, "2", 3, 6),    # R2 pad 2 on RAIL_OUT
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])

    assert not any(isinstance(d, SourceSpec) for d in result.directives)
    assert any("RAIL_OUT" in e for e in result.errors)

    for des in ("R1", "R2"):
        r = next(d for d in result.directives
                 if isinstance(d, ResistorSpec) and d.designator == des)
        p_nets = {p.net_index for p in r.p.pins}
        n_nets = {p.net_index for p in r.n.pins}
        assert len(p_nets) == 1, f"{des} P spans {p_nets}"
        assert len(n_nets) == 1, f"{des} N spans {n_nets}"
        assert p_nets.isdisjoint(n_nets), f"{des} P/N overlap: {p_nets}&{n_nets}"
        assert p_nets | n_nets == ({1, 3} if des == "R1" else {2, 3})


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


def test_pcb_sourced_reused_sheet_slotted_local_net():
    """PCB ECO on a repeated sheet: local net VCC_EFUSE → VCC_EFUSE.N on PCB."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VCC_EFUSE",
            source_sheets=["efuse.schdoc"],
            terminals=[_FakeTerminal("R63", "2")],
        ),
        _FakeNet(
            name="VDD_5V0",
            aliases=["VDD_5V0.1", "VDD_5V0.2", "VDD_5V0.3", "VDD_5V0.4"],
            source_sheets=["can-phy.schdoc", "efuse.schdoc"],
            terminals=[_FakeTerminal("R63", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(
            RawNet("VDD_5V0"),
            RawNet("VCC_EFUSE.1"),
            RawNet("VCC_EFUSE.4"),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="R63.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "0.01",
                    "PDN1_P_NET": "VDD_5V0",
                    "PDN1_N_NET": "VCC_EFUSE",
                },
            ),
            RawPcbComponent(
                designator="R63.4", center=Pt2D(3, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "0.01",
                    "PDN1_P_NET": "VDD_5V0",
                    "PDN1_N_NET": "VCC_EFUSE",
                },
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="R63", schdoc_name="efuse.SchDoc",
                parameters={"Comment": "0R"}, pin_designators=("1", "2"),
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0),
            _pad(0, "2", 1, 1),
            _pad(1, "1", 0, 2),
            _pad(1, "2", 2, 3),
        ),
        compiled_netlist=netlist,
    )
    assert _schdoc_for_pcb_instance(proj, 0, "R63") == "efuse.schdoc"
    assert _schdoc_for_pcb_instance(proj, 1, "R63") == "efuse.schdoc"

    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    series = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(series) == 2
    by_des = {d.designator: d for d in series}
    assert by_des["R63.1"].n.pins[0].net_index == 1
    assert by_des["R63.4"].n.pins[0].net_index == 2


def test_resolve_local_net_pins_dot_channel_alias():
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="IOUT3",
            aliases=["S00A.1", "S00A.4"],
            source_sheets=["Child.SchDoc"],
            terminals=[
                _FakeTerminal("J3.1", "29"),
                _FakeTerminal("J3.4", "29"),
            ],
        ),
    ])
    pins = _resolve_local_net_pins(
        netlist, "J3", "Child.SchDoc", "S00A",
        pcb_designator="J3.4",
    )
    assert pins == ["29"]


def test_resolve_local_net_pins_dot_channel_alias_scoped():
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="IOUT_OTHER",
            aliases=["S00A.1"],
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("J3.4", "29")],
        ),
    ])
    pins = _resolve_local_net_pins(
        netlist, "J3", "Child.SchDoc", "S00A",
        pcb_designator="J3.4",
    )
    assert pins == []


def test_schdoc_inference_pin_centric_without_net_name_match():
    """PCB net VCC_EFUSE.4 must not block efuse.SchDoc vote via pin 2 alone."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VCC_EFUSE",
            source_sheets=["efuse.schdoc"],
            terminals=[_FakeTerminal("R63", "2")],
        ),
        _FakeNet(
            name="VDD_5V0",
            aliases=["VDD_5V0.4"],
            source_sheets=["can-phy.schdoc", "efuse.schdoc"],
            terminals=[_FakeTerminal("R63", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("VDD_5V0"), RawNet("VCC_EFUSE.4")),
        pcb_components=(
            RawPcbComponent(
                designator="R63.4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0),
            _pad(0, "2", 1, 1),
        ),
        compiled_netlist=netlist,
    )
    assert _schdoc_for_pcb_instance(proj, 0, "R63") == "efuse.schdoc"


def test_variant_alias_pattern_via_pad_netlist():
    """MDI.TD_P4 style aliases resolve via pad/netlist cross-check."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="MDI.TD_P",
            aliases=["MDI.TD_P4"],
            source_sheets=["eth.schdoc"],
            terminals=[_FakeTerminal("R1", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("MDI.TD_P4"), RawNet("GND")),
        pcb_components=(
            RawPcbComponent(
                designator="R1_D4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="0402", source_designator="R1",
            ),
        ),
        pads=(_pad(0, "1", 0, 0), _pad(0, "2", 1, 1)),
        compiled_netlist=netlist,
    )
    spec, errors = _resolve_terminal(
        proj, 0, "MDI.TD_P", None, [1], "SERIES P",
        sch_lookup_designator="R1", schdoc_name="eth.schdoc",
    )
    assert not errors
    assert spec is not None
    assert spec.pins[0].net_index == 0


def test_alias_fallback_no_cross_channel_family_leak():
    """Alias fallback must not match every pad in an unscoped label family."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="CH1_+3V3",
            aliases=["+3V3"],
            source_sheets=["child1.schdoc"],
            terminals=[
                _FakeTerminal("U1", "1"),
                _FakeTerminal("U1", "2"),
            ],
        ),
        _FakeNet(
            name="CH2_+3V3",
            aliases=["+3V3"],
            source_sheets=["child2.schdoc"],
            terminals=[_FakeTerminal("U1", "2")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("CH1_+3V3"), RawNet("CH2_+3V3"), RawNet("GND")),
        pcb_components=(
            RawPcbComponent(
                designator="U1_PLACED", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0),
            _pad(0, "2", 1, 1),
            _pad(0, "3", 2, 2),
        ),
        compiled_netlist=netlist,
    )
    with patch(
        "fypa.altium.annotations._resolve_local_net_pins",
        return_value=[],
    ):
        spec, errors = _resolve_terminal(
            proj, 0, "+3V3", None, [1], "SINK P",
            sch_lookup_designator="U1", schdoc_name="child2.schdoc",
        )
    assert not errors
    assert spec is not None
    assert len(spec.pins) == 1
    assert spec.pins[0].pad_designator == "2"
    assert spec.pins[0].net_index == 1


def test_alias_fallback_flattened_terminal_designator():
    """Alias fallback must query netlist rows keyed by flattened designators."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="IOUT3",
            aliases=["S00A.4"],
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("J3.4", "29")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("IOUT3"), RawNet("GND")),
        pcb_components=(
            RawPcbComponent(
                designator="J3.4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J3",
            ),
        ),
        pads=(_pad(0, "29", 0, 0),),
        compiled_netlist=netlist,
    )
    with patch(
        "fypa.altium.annotations._resolve_local_net_pins",
        return_value=[],
    ):
        spec, errors = _resolve_terminal(
            proj, 0, "S00A", None, [1], "SINK P",
            sch_lookup_designator="J3", schdoc_name="Child.SchDoc",
        )
    assert not errors
    assert spec is not None
    assert spec.pins[0].pad_designator == "29"
    assert spec.pins[0].net_index == 0


def test_bridge_validation_scoped_per_instance_no_cross_channel_merge():
    """SERIES bridge expansion must not union slotted nets across channels."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="NET_A.1",
            aliases=["NET_A"],
            source_sheets=["ch1.schdoc"],
            terminals=[_FakeTerminal("R1", "1")],
        ),
        _FakeNet(
            name="NET_B.1",
            aliases=["NET_B"],
            source_sheets=["ch1.schdoc"],
            terminals=[
                _FakeTerminal("R1", "2"),
                _FakeTerminal("U1", "1"),
            ],
        ),
        _FakeNet(
            name="NET_A.2",
            aliases=["NET_A"],
            source_sheets=["ch2.schdoc"],
            terminals=[_FakeTerminal("R1", "1")],
        ),
        _FakeNet(
            name="NET_B.2",
            aliases=["NET_B"],
            source_sheets=["ch2.schdoc"],
            terminals=[
                _FakeTerminal("R1", "2"),
                _FakeTerminal("U2", "1"),
            ],
        ),
    ])
    proj = _minimal_proj(
        nets=(
            RawNet("NET_A.1"), RawNet("NET_B.1"),
            RawNet("NET_A.2"), RawNet("NET_B.2"),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="R1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R1",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "0.01",
                    "PDN1_P_NET": "NET_A",
                    "PDN1_N_NET": "NET_B",
                },
            ),
            RawPcbComponent(
                designator="R1.2", center=Pt2D(1, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R1",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "0.01",
                    "PDN1_P_NET": "NET_A",
                    "PDN1_N_NET": "NET_B",
                },
            ),
            RawPcbComponent(
                designator="U1.1", center=Pt2D(2, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U1",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "10mA",
                    "PDN_NET": "NET_B",
                },
            ),
            RawPcbComponent(
                designator="U2.2", center=Pt2D(3, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U2",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "20mA",
                    "PDN_NET": "NET_B",
                },
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="ch1.SchDoc",
                parameters={"Comment": "IC"}, pin_designators=("1",),
            ),
            RawSchComponent(
                designator="U2", schdoc_name="ch2.SchDoc",
                parameters={"Comment": "IC"}, pin_designators=("1",),
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0), _pad(0, "2", 1, 1),
            _pad(1, "1", 0, 2), _pad(1, "2", 1, 3),
            _pad(2, "1", 1, 1),
            _pad(3, "1", 3, 3),
        ),
        compiled_netlist=netlist,
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 2
    by_des = {d.designator: d for d in sinks}
    assert by_des["U1.1"].return_group != by_des["U2.2"].return_group


def test_expand_net_names_scoped_to_pcb_instance():
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VCC_EFUSE",
            source_sheets=["efuse.schdoc"],
            terminals=[_FakeTerminal("R63", "2")],
        ),
    ])
    proj = _minimal_proj(
        nets=(
            RawNet("VDD_5V0"),
            RawNet("VCC_EFUSE.1"),
            RawNet("VCC_EFUSE.4"),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="R63.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
            ),
            RawPcbComponent(
                designator="R63.4", center=Pt2D(1, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
            ),
        ),
        pads=(
            _pad(0, "2", 1, 0),
            _pad(1, "2", 2, 1),
        ),
        compiled_netlist=netlist,
    )
    resolver = _instance_resolver(proj)
    assert "VCC_EFUSE.1" in resolver.expand_net_names("VCC_EFUSE", pcb_index=0)
    assert "VCC_EFUSE.4" not in resolver.expand_net_names("VCC_EFUSE", pcb_index=0)
    assert "VCC_EFUSE.4" in resolver.expand_net_names("VCC_EFUSE", pcb_index=1)
    assert "VCC_EFUSE.1" not in resolver.expand_net_names("VCC_EFUSE", pcb_index=1)


def test_bridge_groups_expand_slotted_local_names():
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VCC_EFUSE",
            source_sheets=["efuse.schdoc"],
            terminals=[_FakeTerminal("R63", "2")],
        ),
        _FakeNet(
            name="VDD_5V0",
            source_sheets=["efuse.schdoc"],
            terminals=[_FakeTerminal("R63", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("VDD_5V0"), RawNet("VCC_EFUSE.4")),
        pcb_components=(
            RawPcbComponent(
                designator="R63.4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "0.01",
                    "PDN1_P_NET": "VDD_5V0",
                    "PDN1_N_NET": "VCC_EFUSE",
                },
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0),
            _pad(0, "2", 1, 1),
        ),
        compiled_netlist=netlist,
    )
    expanded = _instance_resolver(proj).expand_net_names("VCC_EFUSE")
    assert "VCC_EFUSE" in expanded
    assert "VCC_EFUSE.4" in expanded

    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors


def test_resolve_terminal_no_double_suffix_on_qualified_net():
    """Degraded mode must not append another channel suffix to VCC_EFUSE.4."""
    proj = _minimal_proj(
        nets=(RawNet("VCC_EFUSE.4"),),
        pcb_components=(
            RawPcbComponent(
                designator="R63.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
            ),
        ),
        pads=(_pad(0, "2", 0, 0),),
        compiled_netlist=None,
    )
    spec, errors = _resolve_terminal(
        proj, 0, "VCC_EFUSE.4", None, [1], "SERIES N",
    )
    assert not errors
    assert spec is not None
    assert spec.pins[0].net_index == 0


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


def _regulator_proj_with_source(**extra_regulator_params):
    """SOURCE J1 @5V + REGULATOR U2 on +5V→+3V3 with two pads each side."""
    reg_params = {
        "PDN_ROLE": "REGULATOR",
        "PDN_V": "3.3",
        "PDN_OUT_P_NET": "+3V3",
        "PDN_OUT_N_NET": "GND",
        "PDN_IN_P_NET": "+5V",
        "PDN_IN_N_NET": "GND",
    }
    reg_params.update(extra_regulator_params)
    return _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("+5V"), RawNet("+3V3"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "5",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters=reg_params,
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1", center=Pt2D(-5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "1", 1, -5),
            _pad(0, "2", 0, -4),
            _pad(1, "1", 2, 0),
            _pad(1, "2", 0, 1),
            _pad(1, "3", 1, 2),
            _pad(1, "4", 0, 3),
        ),
    )


def test_regulator_ldo_auto_gain():
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="LDO",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.gain == 1.0
    assert reg.regulator_type == "LDO"
    assert not reg.adaptive_gain_eligible


def test_regulator_smps_auto_gain():
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="SMPS",
        PDN_REGULATOR_EFFICIENCY="0.9",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.regulator_type == "SMPS"
    assert reg.adaptive_gain_eligible
    assert abs(reg.gain - (3.3 / (5.0 * 0.9))) < 1e-6


def test_regulator_explicit_gain_overrides_type():
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="SMPS",
        PDN_REGULATOR_EFFICIENCY="0.9",
        PDN_GAIN="0.5",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.gain == 0.5
    assert not reg.adaptive_gain_eligible
    assert any("overrides" in w for w in result.warnings)


def test_lookup_inferred_vin_ignores_series_bridge_groups():
    """Sense paths through GND must not make Vin ambiguous (project_a / PDN5_R)."""
    supply_map = {"VDD_48V": 48.0, "VDD_12V": 12.0}
    assert _lookup_inferred_vin("VDD_48V", supply_map) == 48.0
    assert _lookup_inferred_vin("VDD_12V", supply_map) == 12.0


def test_regulator_smps_vin_not_ambiguous_through_sense_bridges():
    """SMPS on VDD_48V stays valid when sense resistors bridge rails via GND."""
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VDD_48V"), RawNet("AX"),
            RawNet("SNS_A"), RawNet("VDD_12V"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J3", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "48",
                    "PDN_P_NET": "VDD_48V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Stepper.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "16m",
                    "PDN1_P_NET": "VDD_48V",
                    "PDN1_N_NET": "AX",
                    "PDN2_R": "16m",
                    "PDN2_P_NET": "AX",
                    "PDN2_N_NET": "SNS_A",
                    "PDN5_R": "100m",
                    "PDN5_P_NET": "VDD_12V",
                    "PDN5_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4", "5"),
            ),
            RawSchComponent(
                designator="R3", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "50m",
                    "PDN_P_NET": "SNS_A",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U4", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.85",
                    "PDN_V": "12",
                    "PDN_OUT_P_NET": "VDD_12V",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VDD_48V",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J3", center=Pt2D(-10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J3",
            ),
            RawPcbComponent(
                designator="U1", center=Pt2D(-5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
            RawPcbComponent(
                designator="R3", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="0402", source_designator="R3",
            ),
            RawPcbComponent(
                designator="U4", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U4",
            ),
        ),
        pads=(
            _pad(0, "1", 1, -10), _pad(0, "2", 0, -9),
            _pad(1, "1", 1, -5), _pad(1, "2", 2, -4),
            _pad(1, "3", 3, -3), _pad(1, "4", 0, -2),
            _pad(1, "5", 4, -1),
            _pad(2, "1", 3, 0), _pad(2, "2", 0, 1),
            _pad(3, "1", 4, 5), _pad(3, "2", 0, 6),
            _pad(3, "3", 1, 7), _pad(3, "4", 0, 8),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    reg = next(d for d in result.directives if d.designator == "U4")
    assert isinstance(reg, RegulatorSpec)
    assert reg.regulator_type == "SMPS"
    assert abs(reg.gain - (12.0 / (48.0 * 0.85))) < 1e-6
    assert not any("cannot infer input voltage" in e for e in result.errors)


def test_regulator_smps_invalid_efficiency_aborts_gain():
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="SMPS",
        PDN_REGULATOR_EFFICIENCY="not_a_number",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert not any(isinstance(d, RegulatorSpec) for d in result.directives)
    assert any("PDN_REGULATOR_EFFICIENCY" in e for e in result.errors)


def test_regulator_smps_vin_from_upstream_regulator_chain():
    """Second-stage SMPS infers Vin_nom from an upstream REGULATOR output net."""
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VDD_48V"), RawNet("VDD_12V"), RawNet("VDD_3V3"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "48",
                    "PDN_P_NET": "VDD_48V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U4", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.9",
                    "PDN_V": "12",
                    "PDN_OUT_P_NET": "VDD_12V",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VDD_48V",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="U5", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.85",
                    "PDN_V": "3.3",
                    "PDN_OUT_P_NET": "VDD_3V3",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VDD_12V",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1", center=Pt2D(-10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
            RawPcbComponent(
                designator="U4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U4",
            ),
            RawPcbComponent(
                designator="U5", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U5",
            ),
        ),
        pads=(
            _pad(0, "1", 1, -10), _pad(0, "2", 0, -9),
            _pad(1, "1", 1, 0), _pad(1, "2", 0, 1),
            _pad(1, "3", 2, 2), _pad(1, "4", 0, 3),
            _pad(2, "1", 3, 10), _pad(2, "2", 0, 11),
            _pad(2, "3", 2, 12), _pad(2, "4", 0, 13),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    regs = {d.designator: d for d in result.directives if isinstance(d, RegulatorSpec)}
    assert abs(regs["U4"].gain - (12.0 / (48.0 * 0.9))) < 1e-6
    assert abs(regs["U5"].gain - (3.3 / (12.0 * 0.85))) < 1e-6


def test_regulator_smps_missing_upstream_voltage():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_V": "3.3",
                    "PDN_OUT_P_NET": "+3V3",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "+5V",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "1", 2, 0),
            _pad(0, "2", 0, 1),
            _pad(0, "3", 1, 2),
            _pad(0, "4", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any("cannot infer input voltage" in e for e in result.errors)


def test_regulator_quiescent_parsed():
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="LDO",
        PDN_QUIESCENT="5mA",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.quiescent_current == 0.005


def test_regulator_quiescent_defaults_to_zero():
    proj = _regulator_proj_with_source(PDN_REGULATOR_TYPE="LDO")
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.quiescent_current == 0.0


def test_regulator_quiescent_rejects_negative():
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="LDO",
        PDN_QUIESCENT="-1mA",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any("QUIESCENT" in e and ">= 0" in e for e in result.errors)


def test_regulator_quiescent_unparseable_aborts_spec():
    """A set-but-garbage PDN_QUIESCENT must not silently build with Iq=0."""
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="LDO",
        PDN_QUIESCENT="not_a_current",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert not any(isinstance(d, RegulatorSpec) for d in result.directives)
    assert any("PDN_QUIESCENT" in e for e in result.errors)


def test_regulator_unparseable_gain_aborts_not_auto():
    """A set-but-garbage PDN_GAIN must abort, not fall through to auto-gain."""
    proj = _regulator_proj_with_source(
        PDN_REGULATOR_TYPE="SMPS",
        PDN_REGULATOR_EFFICIENCY="0.9",
        PDN_GAIN="oops",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert not any(isinstance(d, RegulatorSpec) for d in result.directives)
    assert any("PDN_GAIN" in e for e in result.errors)


def test_regulator_efficiency_ignored_warns_with_manual_gain():
    """PDN_REGULATOR_EFFICIENCY alongside a manual PDN_GAIN (no type) warns."""
    proj = _regulator_proj_with_source(
        PDN_GAIN="0.6",
        PDN_REGULATOR_EFFICIENCY="0.9",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.gain == 0.6
    assert any(
        "REGULATOR_EFFICIENCY" in w and "ignored" in w for w in result.warnings
    )


def test_regulator_quiescent_indexed_channel():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "5",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN1_V": "1.8",
                    "PDN1_REGULATOR_TYPE": "LDO",
                    "PDN1_QUIESCENT": "2mA",
                    "PDN1_OUT_P_NET": "+1V8",
                    "PDN1_OUT_N_NET": "GND",
                    "PDN1_IN_P_NET": "+5V",
                    "PDN1_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1", center=Pt2D(-5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "1", 1, -5),
            _pad(0, "2", 0, -4),
            _pad(1, "1", 2, 0),
            _pad(1, "2", 0, 1),
            _pad(1, "3", 1, 2),
            _pad(1, "4", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.channel_index == 1
    assert reg.quiescent_current == 0.002


def test_terminal_resolution_direct_connectivity_only():
    """Kelvin shunt: terminals collect only pads directly on the named net."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("SNS_A")),
        sch_components=(
            RawSchComponent(
                designator="R3", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "50m",
                    "PDN_P_NET": "SNS_A",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2", "2a"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="R3", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="3216R-SHUNT", source_designator="R3",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),
            _pad(0, "2", 0, 1),
            _pad(0, "2a", 0, 2),
            _pad(0, "3", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    r = next(d for d in result.directives if isinstance(d, ResistorSpec))
    assert {p.pad_designator for p in r.p.pins} == {"1"}
    assert {p.pad_designator for p in r.n.pins} == {"2", "2a", "3"}


def test_terminal_resolution_no_bridge_expansion():
    """SINK on downstream net does not collect pads on the upstream SERIES net."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VDD_3V3"), RawNet("VDD_MCU")),
        sch_components=(
            RawSchComponent(
                designator="L1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "0.48",
                    "PDN_P_NET": "VDD_3V3",
                    "PDN_N_NET": "VDD_MCU",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "350mA",
                    "PDN_P_NET": "VDD_MCU",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="L1", center=Pt2D(-2, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="0402", source_designator="L1",
            ),
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "1", 1, -2), _pad(0, "2", 2, -1),
            _pad(1, "1", 2, 0),
            _pad(1, "2", 1, 1),
            _pad(1, "3", 0, 2),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    sink = next(d for d in result.directives if isinstance(d, SinkSpec))
    assert {p.pad_designator for p in sink.p.pins} == {"1"}
    assert not any(p.net_index == 1 for p in sink.p.pins)


def test_multichannel_series_no_cross_channel_pads():
    """Bridged SERIES nets must not leak pads into another channel's terminal."""
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VDD_48V"), RawNet("AX"),
            RawNet("AY"), RawNet("SNS_A"),
        ),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Stepper.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN1_R": "16m",
                    "PDN1_P_NET": "VDD_48V",
                    "PDN1_N_NET": "AX",
                    "PDN2_R": "16m",
                    "PDN2_P_NET": "AY",
                    "PDN2_N_NET": "SNS_A",
                },
                pin_designators=("E1", "E3", "E4", "E10", "E11"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "E1", 1, 0),
            _pad(0, "E3", 3, 1),
            _pad(0, "E4", 2, 2),
            _pad(0, "E10", 4, 3),
            _pad(0, "E11", 4, 4),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    ch2 = next(
        d for d in result.directives
        if isinstance(d, ResistorSpec) and d.channel_index == 2
    )
    assert {p.pad_designator for p in ch2.p.pins} == {"E3"}
    assert {p.pad_designator for p in ch2.n.pins} == {"E10", "E11"}



# --- per-channel role (mixed-role parts) --------------------------------------

def test_mixed_role_source_and_sink_on_one_part():
    # A DAC: supply pins SINK current (AVDD, DVDD); output pins SOURCE current
    # (DAC_OUT0/1). One physical part carries both roles via per-channel
    # PDN<n>_ROLE overrides on a part-wide SINK default.
    # Nets: 0=GND 1=AVDD 2=DVDD 3=DAC_OUT0 4=DAC_OUT1
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("AVDD"), RawNet("DVDD"),
            RawNet("DAC_OUT0"), RawNet("DAC_OUT1"),
        ),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Analog.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "80mA", "PDN_P_NET": "AVDD", "PDN_N_NET": "GND",
                    "PDN1_I": "20mA", "PDN1_P_NET": "DVDD", "PDN1_N_NET": "GND",
                    "PDN2_ROLE": "SOURCE",
                    "PDN2_V": "2.5",
                    "PDN2_P_NET": "DAC_OUT0", "PDN2_N_NET": "GND",
                    "PDN3_ROLE": "SOURCE",
                    "PDN3_V": "1.8",
                    "PDN3_P_NET": "DAC_OUT1", "PDN3_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4", "5"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U7",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),   # AVDD
            _pad(0, "2", 2, 1),   # DVDD
            _pad(0, "3", 3, 2),   # DAC_OUT0
            _pad(0, "4", 4, 3),   # DAC_OUT1
            _pad(0, "5", 0, 4),   # GND (shared return)
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = {d.channel_index: d
             for d in result.directives if isinstance(d, SinkSpec)}
    sources = {d.channel_index: d
               for d in result.directives if isinstance(d, SourceSpec)}
    assert set(sinks) == {None, 1}
    assert set(sources) == {2, 3}
    assert sinks[None].current == 0.08
    assert sinks[1].current == 0.02
    assert sources[2].voltage == 2.5
    assert sources[3].voltage == 1.8
    # SOURCE P terminals landed on the DAC output nets, not the supply nets.
    assert sources[2].p.pins[0].net_index == 3    # DAC_OUT0
    assert sources[3].p.pins[0].net_index == 4    # DAC_OUT1


def test_two_sinks_need_no_per_channel_role():
    # The uniform-role case is unchanged by the per-channel-role feature: two
    # sinks are still just PDN_ROLE=SINK plus PDN_I / PDN1_I, no PDN<n>_ROLE.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="M.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA", "PDN_P_NET": "+3V3", "PDN_N_NET": "GND",
                    "PDN1_I": "250mA", "PDN1_P_NET": "+1V8", "PDN1_N_NET": "GND",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 2, 1), _pad(0, "3", 0, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 2
    assert {d.channel_index for d in sinks} == {None, 1}


def test_per_channel_role_rejects_unknown_role():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="M.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "10mA", "PDN_P_NET": "+3V3", "PDN_N_NET": "GND",
                    "PDN1_ROLE": "BOGUS",
                    "PDN1_V": "5", "PDN1_P_NET": "+3V3", "PDN1_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any("PDN1_ROLE" in e and "BOGUS" in e for e in result.errors)
    # The valid SINK channel still parses despite the bad override channel.
    assert any(isinstance(d, SinkSpec) for d in result.directives)


def test_per_channel_role_missing_value_param_errors():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("OUT")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="M.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "10mA", "PDN_P_NET": "+3V3", "PDN_N_NET": "GND",
                    # Declares SOURCE but forgot the PDN1_V value param.
                    "PDN1_ROLE": "SOURCE",
                    "PDN1_P_NET": "OUT", "PDN1_N_NET": "GND",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 0, 1), _pad(0, "3", 2, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any("PDN1_V" in e for e in result.errors)


def test_series_channel_on_mixed_part_registers_bridge():
    # A part that SINKs on one channel and bridges two nets with a SERIES
    # channel: the SERIES channel must still parse and register its bridge
    # even though the part-wide role is SINK.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V"), RawNet("5V_SW")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="M.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA", "PDN_P_NET": "+5V", "PDN_N_NET": "GND",
                    "PDN1_ROLE": "SERIES",
                    "PDN1_R": "0.01",
                    "PDN1_P_NET": "+5V", "PDN1_N_NET": "5V_SW",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),   # +5V
            _pad(0, "2", 0, 1),   # GND
            _pad(0, "3", 2, 2),   # 5V_SW
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert any(isinstance(d, SinkSpec) for d in result.directives)
    assert any(isinstance(d, ResistorSpec) for d in result.directives)
    groups = _collect_bridge_groups(_iter_pdn_parameter_sources(proj), proj)
    assert groups.get("+5V") == frozenset({"+5V", "5V_SW"})


def test_indexed_roles_only_mixed_series_and_sink():
    # SERIES on the input path plus SINK on a separate IC supply — no
    # part-wide PDN_ROLE required.
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VIN"), RawNet("VOUT"), RawNet("VCC"),
        ),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN1_ROLE": "SERIES",
                    "PDN1_R": "7m",
                    "PDN1_P_NET": "VIN",
                    "PDN1_N_NET": "VOUT",
                    "PDN2_ROLE": "SINK",
                    "PDN2_I": "10mA",
                    "PDN2_P_NET": "VCC",
                    "PDN2_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),   # VIN
            _pad(0, "2", 2, 1),   # VOUT
            _pad(0, "3", 3, 2),   # VCC
            _pad(0, "4", 0, 3),   # GND
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    series = [d for d in result.directives if isinstance(d, ResistorSpec)]
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(series) == 1
    assert len(sinks) == 1
    assert series[0].channel_index == 1
    assert sinks[0].channel_index == 2
    assert series[0].resistance == pytest.approx(0.007)
    assert sinks[0].current == pytest.approx(0.01)


def test_indexed_roles_only_bridge_group_registers():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("VOUT"), RawNet("VCC")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN1_ROLE": "SERIES",
                    "PDN1_R": "7m",
                    "PDN1_P_NET": "VIN",
                    "PDN1_N_NET": "VOUT",
                    "PDN2_ROLE": "SINK",
                    "PDN2_I": "10mA",
                    "PDN2_P_NET": "VCC",
                    "PDN2_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),
            _pad(0, "2", 2, 1),
            _pad(0, "3", 3, 2),
            _pad(0, "4", 0, 3),
        ),
    )
    groups = _collect_bridge_groups(_iter_pdn_parameter_sources(proj), proj)
    assert groups.get("VIN") == frozenset({"VIN", "VOUT"})


def test_indexed_roles_channel_without_role_errors():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("OUT")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="M.SchDoc",
                parameters={
                    "PDN1_ROLE": "SINK",
                    "PDN1_I": "10mA", "PDN1_P_NET": "+3V3", "PDN1_N_NET": "GND",
                    "PDN2_I": "5mA", "PDN2_P_NET": "OUT", "PDN2_N_NET": "GND",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 0, 1), _pad(0, "3", 2, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any(
        "PDN2_ROLE" in e and "no part-wide PDN_ROLE" in e
        for e in result.errors
    )


def test_stray_pdn_params_without_any_role_warns():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="M.SchDoc",
                parameters={
                    "PDN_I": "10mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="R", source_designator="U1",
            ),
        ),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.directives
    assert any("no PDN_ROLE or PDN<n>_ROLE" in w for w in result.warnings)


def test_indexed_roles_only_source_channel_contributes_supply_voltage():
    # A SOURCE channel on an indexed-only part (no part-wide PDN_ROLE) must
    # both produce its directive and register its nominal rail voltage.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U3", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN1_ROLE": "SOURCE",
                    "PDN1_V": "5V",
                    "PDN1_P_NET": "+5V",
                    "PDN1_N_NET": "GND",
                    "PDN2_ROLE": "SINK",
                    "PDN2_I": "10mA",
                    "PDN2_P_NET": "+3V3",
                    "PDN2_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U3", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U3",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),   # +5V
            _pad(0, "2", 0, 1),   # GND
            _pad(0, "3", 2, 2),   # +3V3
            _pad(0, "4", 0, 3),   # GND
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sources = [d for d in result.directives if isinstance(d, SourceSpec)]
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sources) == 1
    assert len(sinks) == 1
    assert sources[0].channel_index == 1
    assert sources[0].voltage == pytest.approx(5.0)

    supply_map = _collect_supply_voltages_by_net(_iter_pdn_parameter_sources(proj))
    assert supply_map == {"+5V": 5.0}


# --- is_solveable: annotation errors are non-fatal ----------------------------

def test_is_solveable_skips_bad_directive_and_solves_the_rest():
    """A directive that fails to resolve (its net doesn't exist) is dropped and
    recorded as an error, but must NOT blank the board â€” the valid source/sink
    rail still solves. Regression for the 'one bad PDN parameter blanks every
    rail' report."""
    from types import SimpleNamespace

    from fypa.altium.loader import LoadedProject

    extracted = SimpleNamespace(enabled_copper_layer_ids=lambda: [1], nets=())
    ann = AnnotationResult(
        directives=[_single_source(0), _single_sink(0)],
        errors=[
            "SINK on J3 terminal: net 'S00A' does not exist on the PCB "
            "and could not be resolved as a local schematic net."
        ],
    )
    loaded = LoadedProject(extracted, ann)
    assert loaded.is_solveable


def test_is_solveable_still_false_without_a_source():
    """Structural impossibility still blocks: a board with copper but no
    SOURCE / REGULATOR has nothing to drive current and is not solveable."""
    from types import SimpleNamespace

    from fypa.altium.loader import LoadedProject

    extracted = SimpleNamespace(enabled_copper_layer_ids=lambda: [1], nets=())
    ann = AnnotationResult(directives=[_single_sink(0)])  # sink only
    loaded = LoadedProject(extracted, ann)
    assert not loaded.is_solveable





def test_format_solve_blockers_lists_errors():
    from types import SimpleNamespace

    from fypa.altium.loader import format_solve_blockers

    loaded = SimpleNamespace(
        extracted=SimpleNamespace(
            enabled_copper_layer_ids=lambda: [1],
        ),
        annotations=AnnotationResult(
            errors=["SINK on U4#3: PDN3_PINS conflicts with PDN3_P_NET"],
            warnings=["U4: unknown parameter 'PDN2_PIN'"],
            directives=[],
        ),
    )
    text = format_solve_blockers(loaded)
    assert "Project is not solveable" in text
    assert "PDN3_PINS" in text
    assert "PDN2_PIN" in text
    assert "no SOURCE or REGULATOR" in text
    assert "fypa.log" in text
