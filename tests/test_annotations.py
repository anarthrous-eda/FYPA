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
    _LOCAL_NET_TIER_ALIAS,
    _LOCAL_NET_TIER_NAME,
    _LOCAL_NET_TIER_PCB,
    _arbitrate_overlapping_terminals,
    _collect_series_upstream_map,
    _collect_supply_voltages_by_net,
    _iter_pdn_parameter_sources,
    _iter_series_bridge_pairs,
    _union_series_bridge_net_indices,
    _lookup_inferred_vin,
    _local_net_label_matches,
    _SeriesVinGraph,
    _SERIES_VIN_MAX_HOPS,
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
    RawSchSheetSymbol,
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
    pins, _tier = _resolve_local_net_pins(netlist, "U1", "Power.SchDoc", "+3V3")
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
    pins, _tier = _resolve_local_net_pins(
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
    pins, _tier = _resolve_local_net_pins(
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
    spec0, err0, _t0 = _resolve_terminal(
        proj, 0, "+3V3", None, [1], "SINK P",
        warnings=warnings,
        sch_lookup_designator="U1", schdoc_name="Child.SchDoc",
    )
    spec1, err1, _t1 = _resolve_terminal(
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


def test_series_template_r_with_indexed_nets_multi_pin():
    """Unindexed PDN_R is a template; indexed nets alone define two channels.

    Multi-pin footprints must still resolve pads from the named nets (no
    2-pin auto-infer, no legacy '10 pads' error).
    """
    # Nets: 0=VIN_A 1=VOUT_A 2=VIN_B 3=VOUT_B 4=CTRL
    proj = _minimal_proj(
        nets=(
            RawNet("VIN_A"), RawNet("VOUT_A"),
            RawNet("VIN_B"), RawNet("VOUT_B"), RawNet("CTRL"),
        ),
        sch_components=(
            RawSchComponent(
                designator="SW1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "0.05",
                    "PDN1_P_NET": "VIN_A",
                    "PDN1_N_NET": "VOUT_A",
                    "PDN2_P_NET": "VIN_B",
                    "PDN2_N_NET": "VOUT_B",
                },
                pin_designators=("1", "2", "3", "4", "5"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="SW1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="RELAY", source_designator="SW1",
            ),
        ),
        pads=(
            _pad(0, "1", 0),   # VIN_A
            _pad(0, "2", 1, 1),  # VOUT_A
            _pad(0, "3", 2, 2),  # VIN_B
            _pad(0, "4", 3, 3),  # VOUT_B
            _pad(0, "5", 4, 4),  # CTRL (ignored)
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    series = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(series) == 2
    by_ch = {d.channel_index: d for d in series}
    assert None not in by_ch
    assert by_ch[1].resistance == 0.05
    assert by_ch[2].resistance == 0.05
    assert {p.pad_designator for p in by_ch[1].p.pins} == {"1"}
    assert {p.pad_designator for p in by_ch[1].n.pins} == {"2"}
    assert {p.pad_designator for p in by_ch[2].p.pins} == {"3"}
    assert {p.pad_designator for p in by_ch[2].n.pins} == {"4"}


def test_series_template_r_override_on_one_channel():
    proj = _minimal_proj(
        nets=(RawNet("A"), RawNet("B"), RawNet("C"), RawNet("D")),
        sch_components=(
            RawSchComponent(
                designator="FB1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "0.1",
                    "PDN1_P_NET": "A", "PDN1_N_NET": "B",
                    "PDN2_R": "0.2",
                    "PDN2_P_NET": "C", "PDN2_N_NET": "D",
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
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    by_ch = {
        d.channel_index: d
        for d in result.directives if isinstance(d, ResistorSpec)
    }
    assert by_ch[1].resistance == 0.1
    assert by_ch[2].resistance == 0.2


def test_regulator_template_shared_in_and_voltage():
    """Shared PDN_V / IN_* / TYPE with per-channel OUT_* only — no legacy channel."""
    # Nets: 0=GND 1=VIN 2=VOUT_P 3=VOUT_N
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VIN"), RawNet("VOUT_P"), RawNet("VOUT_N"),
        ),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_REGULATOR_TYPE": "LDO",
                    "PDN_QUIESCENT": "390uA",
                    "PDN_IN_P_NET": "VIN",
                    "PDN_IN_N_NET": "GND",
                    "PDN1_OUT_P_NET": "VOUT_P",
                    "PDN1_OUT_N_NET": "GND",
                    "PDN2_OUT_P_NET": "GND",
                    "PDN2_OUT_N_NET": "VOUT_N",
                },
                pin_designators=("1", "2", "3", "4", "5"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="DFN", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "1", 1),   # VIN
            _pad(0, "2", 0, 1),  # GND
            _pad(0, "3", 2, 2),  # VOUT_P
            _pad(0, "4", 3, 3),  # VOUT_N
            _pad(0, "5", 0, 4),  # GND again
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    regs = [d for d in result.directives if isinstance(d, RegulatorSpec)]
    assert len(regs) == 2
    by_ch = {d.channel_index: d for d in regs}
    assert None not in by_ch
    assert by_ch[1].voltage == 3.3
    assert by_ch[2].voltage == 3.3
    assert by_ch[1].regulator_type == "LDO"
    assert by_ch[2].quiescent_current == pytest.approx(390e-6)
    assert by_ch[1].in_p.requested_net == "VIN"
    assert by_ch[2].in_p.requested_net == "VIN"
    assert by_ch[1].out_p.requested_net == "VOUT_P"
    assert by_ch[2].out_n.requested_net == "VOUT_N"


def test_sink_unindexed_plus_indexed_both_real_channels():
    """Legacy SINK with its own terminals stays a real channel beside PDN1_*."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN1_I": "250mA",
                    "PDN1_P_NET": "+1V8",
                    "PDN1_N_NET": "GND",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U7",
            ),
        ),
        pads=(
            _pad(0, "1", 1),
            _pad(0, "2", 2, 1),
            _pad(0, "3", 0, 2),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert {d.channel_index for d in sinks} == {None, 1}


def test_sink_template_shared_n_net():
    """Shared PDN_N_NET alone is template-only; each channel needs its P net."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_N_NET": "GND",
                    "PDN1_P_NET": "+3V3",
                    "PDN2_I": "50mA",
                    "PDN2_P_NET": "+1V8",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U7",
            ),
        ),
        pads=(
            _pad(0, "1", 1),
            _pad(0, "2", 2, 1),
            _pad(0, "3", 0, 2),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    by_ch = {d.channel_index: d for d in sinks}
    assert None not in by_ch
    assert by_ch[1].current == pytest.approx(0.1)
    assert by_ch[2].current == pytest.approx(0.05)
    assert by_ch[1].n.requested_net == "GND"
    assert by_ch[2].n.requested_net == "GND"


def test_series_template_shared_p_net():
    """Shared PDN_P_NET alone is template-only with per-channel N nets."""
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT_A"), RawNet("VOUT_B")),
        sch_components=(
            RawSchComponent(
                designator="SW1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "0.05",
                    "PDN_P_NET": "VIN",
                    "PDN1_N_NET": "VOUT_A",
                    "PDN2_N_NET": "VOUT_B",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="SW1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="RELAY", source_designator="SW1",
            ),
        ),
        pads=(
            _pad(0, "1", 0),
            _pad(0, "2", 1, 1),
            _pad(0, "3", 2, 2),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    by_ch = {
        d.channel_index: d
        for d in result.directives if isinstance(d, ResistorSpec)
    }
    assert None not in by_ch
    assert by_ch[1].p.requested_net == "VIN"
    assert by_ch[2].p.requested_net == "VIN"
    assert by_ch[1].n.requested_net == "VOUT_A"
    assert by_ch[2].n.requested_net == "VOUT_B"


def test_cross_role_value_suffix_does_not_create_phantom_channel():
    """Leftover PDN1_R alone on a SINK-only part must not invent a channel."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN1_R": "0.1",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U7",
            ),
        ),
        pads=(
            _pad(0, "1", 1),
            _pad(0, "2", 0, 1),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].channel_index is None
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)
    assert any("PDN1_R" in w for w in result.warnings)


def test_indexed_only_role_with_unindexed_value_template():
    """PDNn_ROLE without part-wide PDN_ROLE may still inherit PDN_R / PDN_V."""
    proj = _minimal_proj(
        nets=(RawNet("A"), RawNet("B")),
        sch_components=(
            RawSchComponent(
                designator="R1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN1_ROLE": "SERIES",
                    "PDN_R": "0.05",
                    "PDN1_P_NET": "A",
                    "PDN1_N_NET": "B",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="R1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="0402", source_designator="R1",
            ),
        ),
        pads=(_pad(0, "1", 0), _pad(0, "2", 1, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    series = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(series) == 1
    assert series[0].channel_index == 1
    assert series[0].resistance == 0.05


def test_template_does_not_cross_a_channel_role_override():
    """A PDNn_ROLE=SERIES channel must not inherit the part's SINK terminals.

    The unindexed template belongs to the part-wide PDN_ROLE. Letting the
    SERIES channel read the sink's PDN_P_NET / PDN_N_NET would silently drop
    a 0.1 ohm resistor straight across the rail the sink sits on — so the
    channel keeps its own "name the nets explicitly" error instead.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("VA"), RawNet("VB")),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN1_ROLE": "SERIES",
                    "PDN1_R": "0.1",
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
            _pad(0, "1", 1), _pad(0, "2", 0, 1), _pad(0, "3", 2, 2),
            _pad(0, "4", 3, 3), _pad(0, "5", 3, 4),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert any(
        "PDN1_P_NET and PDN1_N_NET are required" in e for e in result.errors
    )
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert [d.channel_index for d in sinks] == [None]


def test_indexed_only_role_still_inherits_without_part_wide_role():
    """No part-wide PDN_ROLE — unindexed params are pure templates for all.

    The role-scoping guard above must not break the indexed-only form: with
    no PDN_ROLE there is no unindexed channel whose role could conflict.
    """
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT_A"), RawNet("VOUT_B")),
        sch_components=(
            RawSchComponent(
                designator="SW3", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_R": "0.05",
                    "PDN_P_NET": "VIN",
                    "PDN1_ROLE": "SERIES", "PDN1_N_NET": "VOUT_A",
                    "PDN2_ROLE": "SERIES", "PDN2_N_NET": "VOUT_B",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="SW3", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="RELAY", source_designator="SW3",
            ),
        ),
        pads=(_pad(0, "1", 0), _pad(0, "2", 1, 1), _pad(0, "3", 2, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    by_ch = {
        d.channel_index: d
        for d in result.directives if isinstance(d, ResistorSpec)
    }
    assert set(by_ch) == {1, 2}
    assert by_ch[1].p.requested_net == "VIN"
    assert by_ch[2].p.requested_net == "VIN"


def test_stray_indexed_modifier_does_not_clone_a_channel():
    """PDN1_MIN_V is a modifier, not a channel — it must not clone the load.

    Marking a channel present off any known suffix would let one stray param
    inherit the whole unindexed directive, double-counting 100 mA as 200 mA.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN1_MIN_V": "3.0",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U7",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert [d.channel_index for d in sinks] == [None]
    assert sinks[0].current == pytest.approx(0.1)


def test_stray_indexed_quiescent_does_not_clone_a_regulator():
    """Same rule for REGULATOR modifiers — one regulator, not two."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U5", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_REGULATOR_TYPE": "LDO",
                    "PDN_IN_P_NET": "VIN", "PDN_IN_N_NET": "GND",
                    "PDN_OUT_P_NET": "+3V3", "PDN_OUT_N_NET": "GND",
                    "PDN1_QUIESCENT": "50uA",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U5", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT23", source_designator="U5",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1), _pad(0, "3", 2, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    regs = [d for d in result.directives if isinstance(d, RegulatorSpec)]
    assert [d.channel_index for d in regs] == [None]


def test_single_net_template_does_not_break_indexed_two_terminal():
    """Legacy PDN_NET beside a two-terminal PDN1_*: both stay real channels.

    The two terminal forms are mutually exclusive, so a channel that already
    picked one must not inherit the other and then fail _terminal_mode.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA", "PDN_NET": "+3V3",
                    "PDN1_I": "50mA",
                    "PDN1_P_NET": "+1V8", "PDN1_N_NET": "GND",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U7",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 2, 1), _pad(0, "3", 0, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    by_ch = {
        d.channel_index: d
        for d in result.directives if isinstance(d, SinkSpec)
    }
    assert set(by_ch) == {None, 1}
    assert by_ch[None].n is None            # single-net
    assert by_ch[1].p.requested_net == "+1V8"
    assert by_ch[1].n.requested_net == "GND"


def test_two_terminal_template_does_not_break_indexed_single_net():
    """The mirror case: legacy P/N pair beside an indexed PDN1_NET."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="U7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+3V3", "PDN_N_NET": "GND",
                    "PDN1_I": "50mA", "PDN1_NET": "+1V8",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U7", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U7",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 2, 1), _pad(0, "3", 0, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    by_ch = {
        d.channel_index: d
        for d in result.directives if isinstance(d, SinkSpec)
    }
    assert set(by_ch) == {None, 1}
    assert by_ch[None].p.requested_net == "+3V3"
    assert by_ch[1].n is None               # single-net


def test_series_shared_p_net_template_reaches_bridge_graph():
    """A side inherited from PDN_P_NET must still bridge in the SERIES graph.

    The directives alone are not enough: _iter_series_bridge_pairs feeds
    analysis-group unioning and regulator Vin inference, so a channel that
    resolves for the parser but not for the graph silently splits the rails.
    """
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT_A"), RawNet("VOUT_B")),
        sch_components=(
            RawSchComponent(
                designator="SW1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "0.05",
                    "PDN_P_NET": "VIN",
                    "PDN1_N_NET": "VOUT_A",
                    "PDN2_N_NET": "VOUT_B",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="SW1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="RELAY", source_designator="SW1",
            ),
        ),
        pads=(_pad(0, "1", 0), _pad(0, "2", 1, 1), _pad(0, "3", 2, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    pairs = list(_iter_series_bridge_pairs(
        list(_iter_pdn_parameter_sources(proj)), proj,
    ))
    assert {(p, n) for _pcb, p, n, _d in pairs} == {
        ("VIN", "VOUT_A"), ("VIN", "VOUT_B"),
    }


def test_series_shared_p_pins_template_reaches_bridge_graph():
    """Same for the pin form of a shared side (PDN_P_PINS)."""
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT_A"), RawNet("VOUT_B")),
        sch_components=(
            RawSchComponent(
                designator="SW2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "0.05",
                    "PDN_P_PINS": "1",
                    "PDN1_N_PINS": "2",
                    "PDN2_N_PINS": "3",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="SW2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="RELAY", source_designator="SW2",
            ),
        ),
        pads=(_pad(0, "1", 0), _pad(0, "2", 1, 1), _pad(0, "3", 2, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    pairs = list(_iter_series_bridge_pairs(
        list(_iter_pdn_parameter_sources(proj)), proj,
    ))
    assert {(p, n) for _pcb, p, n, _d in pairs} == {
        ("VIN", "VOUT_A"), ("VIN", "VOUT_B"),
    }


def test_shared_terminal_pair_with_indexed_values_only():
    """Complete shared pair + per-channel values: no phantom legacy channel.

    The unindexed form has both terminals but no PDN_I of its own, so it is a
    terminal template — keeping it would report "missing PDN_I" for a channel
    the user never declared.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+1V0")),
        sch_components=(
            RawSchComponent(
                designator="U8", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_P_NET": "+1V0", "PDN_N_NET": "GND",
                    "PDN1_I": "2A", "PDN2_I": "500mA",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U8", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U8",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    by_ch = {
        d.channel_index: d
        for d in result.directives if isinstance(d, SinkSpec)
    }
    assert set(by_ch) == {1, 2}
    assert by_ch[1].current == pytest.approx(2.0)
    assert by_ch[2].current == pytest.approx(0.5)
    assert by_ch[1].p.requested_net == "+1V0"
    assert by_ch[2].n.requested_net == "GND"


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


def test_bridge_pairs_indexed_series_nets():
    # A multi-channel SERIES part bridges a different net pair per channel;
    # iteration must yield every channel's pair, not stop at the first.
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
    proj = _minimal_proj(
        pcb_components=(
            RawPcbComponent(
                designator="FB1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="FB", source_designator="FB1",
            ),
        ),
    )
    pairs = {
        (p, n) for _idx, p, n, _directed
        in _iter_series_bridge_pairs([source], proj)
    }
    assert pairs == {("RAIL_A", "RAIL_B"), ("RAIL_C", "RAIL_D")}


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
    # Path-qualified annotation vs basename-only netlist provenance.
    assert _sheet_name_matches(
        "mod/Child.SchDoc",
        ["Child.SchDoc"],
    )
    assert _sheet_name_matches(
        "mod/Child.SchDoc",
        ["child.schdoc"],
    )


def _sheet_map(proj):
    from fypa.altium.annotations import _physical_sheet_file_map
    return _physical_sheet_file_map(proj)


def test_sheet_name_matches_physical_page_id_via_map():
    """altium_monkey ≥ 2026.7 emits physical page ids in source_sheets."""
    physical = (
        "physical:0:logical:0:main.SchDoc:child:1:sheet_symbol:U_PWR"
        ":sheet:power.SchDoc"
    )
    proj = _minimal_proj(
        physical_sheet_names=((physical, "Power.SchDoc"),),
    )
    smap = _sheet_map(proj)
    assert _sheet_name_matches("Power.SchDoc", [physical], sheet_map=smap)
    assert not _sheet_name_matches("Other.SchDoc", [physical], sheet_map=smap)


def test_sheet_map_keeps_same_named_sheets_in_different_directories_apart():
    """Harvesting the bare leaf would collapse these onto one name, which is
    the collision _sheet_name_matches exists to prevent."""
    page_a = "physical:0:logical:0:SubA/Power.SchDoc"
    page_b = "physical:1:logical:1:SubB/Power.SchDoc"
    proj = _minimal_proj(physical_sheet_names=(
        (page_a, "SubA/Power.SchDoc"),
        (page_b, "SubB/Power.SchDoc"),
    ))
    smap = _sheet_map(proj)
    assert _sheet_name_matches("SubA/Power.SchDoc", [page_a], sheet_map=smap)
    assert not _sheet_name_matches("SubA/Power.SchDoc", [page_b], sheet_map=smap)


def test_unmapped_physical_page_id_is_unknown_not_guessed():
    """A child page id embeds its PARENT's logical id, so scanning it for a
    ``*.SchDoc`` token yields the root sheet — which both hides real matches
    and invents false ones. An unmapped id must read as unknown provenance,
    degrading to the permissive path rather than binding to a guess."""
    from fypa.altium.annotations import _logical_schdoc_name

    child = (
        "physical:0:logical:0:main.SchDoc:child:logical:0:main.SchDoc"
        ":sheet_symbol:UID-ABC:1"
    )
    assert _logical_schdoc_name(child, {}) is None
    # Not "matches the root sheet", and not a hard reject either: with no
    # usable provenance the net is accepted the same way one with no
    # source_sheets at all is.
    assert _sheet_name_matches("Power.SchDoc", [child])
    assert _sheet_name_matches("main.SchDoc", [child])
    # …but a page the map DOES cover still discriminates.
    proj = _minimal_proj(physical_sheet_names=((child, "Power.SchDoc"),))
    smap = _sheet_map(proj)
    assert _sheet_name_matches("Power.SchDoc", [child], sheet_map=smap)
    assert not _sheet_name_matches("main.SchDoc", [child], sheet_map=smap)


def test_plain_sheet_names_are_unaffected_by_the_page_id_path():
    assert _sheet_name_matches("Power.SchDoc", ["Power.SchDoc"])
    assert _sheet_name_matches("Power.SchDoc", ["SubA/Power.SchDoc"])
    assert not _sheet_name_matches("SubA/Power.SchDoc", ["SubB/Power.SchDoc"])


def test_resolve_local_net_pins_physical_source_sheet():
    physical = (
        "physical:0:logical:0:main.SchDoc:child:1:sheet_symbol:U_PWR"
        ":sheet:power.SchDoc"
    )
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="Sheet1_+3V3",
            aliases=["+3V3"],
            source_sheets=[physical],
            terminals=[_FakeTerminal("U1", "14"), _FakeTerminal("C1", "1")],
        ),
    ])
    proj = _minimal_proj(
        physical_sheet_names=((physical, "Power.SchDoc"),),
        compiled_netlist=netlist,
    )
    pins, _tier = _resolve_local_net_pins(
        netlist, "U1", "Power.SchDoc", "+3V3", sheet_map=_sheet_map(proj),
    )
    assert pins == ["14"]


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
    pins, _tier = _resolve_local_net_pins(
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
    pins, _tier = _resolve_local_net_pins(
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


def test_schdoc_inference_flattened_terminal_designator():
    """Netlist terminals keyed as J3.4 must vote when lookup designator is J3."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="IOUT3",
            aliases=["S00A.4"],
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("J3.4", "29")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("IOUT3"),),
        pcb_components=(
            RawPcbComponent(
                designator="J3.4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J3",
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="J3", schdoc_name="Other.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("29",),
            ),
            RawSchComponent(
                designator="J3", schdoc_name="Child.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("29",),
            ),
        ),
        pads=(_pad(0, "29", 0, 0),),
        compiled_netlist=netlist,
    )
    assert _schdoc_for_pcb_instance(proj, 0, "J3") == "Child.SchDoc"


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
    spec, errors, _tier = _resolve_terminal(
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
        return_value=([], 2),
    ):
        spec, errors, _tier = _resolve_terminal(
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
        return_value=([], 2),
    ):
        spec, errors, _tier = _resolve_terminal(
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
    expanded = _instance_resolver(proj).expand_net_names(
        "VCC_EFUSE", pcb_index=0,
    )
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
    spec, errors, _tier = _resolve_terminal(
        proj, 0, "VCC_EFUSE.4", None, [1], "SERIES N",
    )
    assert not errors
    assert spec is not None
    assert spec.pins[0].net_index == 0


def test_resolve_terminal_degraded_suffix_guess_warns():
    """No netlist: VCC_EFUSE on R63.4 maps to VCC_EFUSE.4 — with a warning."""
    proj = _minimal_proj(
        nets=(RawNet("VCC_EFUSE.4"),),
        pcb_components=(
            RawPcbComponent(
                designator="R63.4", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R63",
            ),
        ),
        pads=(_pad(0, "2", 0, 0),),
        compiled_netlist=None,
    )
    warnings: list[str] = []
    spec, errors, _tier = _resolve_terminal(
        proj, 0, "VCC_EFUSE", None, [1], "SERIES N", warnings=warnings,
    )
    assert not errors
    assert spec is not None
    assert spec.pins[0].net_index == 0
    assert any("VCC_EFUSE.4" in w and "guessed" in w for w in warnings)


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
    spec, errors, _tier = _resolve_terminal(
        proj, 0, "+3V3", None, [1], "SINK P",
        sch_lookup_designator="U1", schdoc_name="Power.SchDoc",
    )
    assert not errors
    assert spec is not None
    assert len(spec.pins) == 1
    assert spec.pins[0].pad_designator == "2"
    assert spec.pins[0].net_index == 1


def test_local_net_label_matches_separator_agnostic_channel_token():
    """Net VIN_1 must match local VIN on designator R1.1 (._ separators differ)."""
    assert _local_net_label_matches("VIN_1", "VIN", {"R1", "R1.1"})
    assert _local_net_label_matches("VIN.1", "VIN", {"R1", "R1_1"})
    assert not _local_net_label_matches("VIN_L.1", "VIN", {"R1", "R1.1"})
    assert _local_net_label_matches("VIN_L.1", "VIN_L", {"R1", "R1.1"})


def test_resolve_local_net_pins_prefers_name_over_shared_alias():
    """Two nets sharing bare alias VIN: keep only the channel name-level match.

    Models the multi-channel case where both sides of a SERIES element carry
    the same local alias in the compiled netlist. The P label VIN must resolve
    to pin 2 (net VIN_1) and not also claim pin 1 (net VIN_L.1).
    """
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VIN_1",
            aliases=["VIN", "VIN.1"],
            source_sheets=["Supply.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "2")],
        ),
        _FakeNet(
            name="VIN_L.1",
            aliases=["VIN", "VIN.1", "VIN.2"],
            source_sheets=["Supply.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "1")],
        ),
    ])
    pins, tier = _resolve_local_net_pins(
        netlist, "R1", "Supply.SchDoc", "VIN",
        pcb_designator="R1.1",
        routed_pin_keys={"1", "2"},
    )
    assert pins == ["2"]
    assert tier == _LOCAL_NET_TIER_NAME

    n_pins, n_tier = _resolve_local_net_pins(
        netlist, "R1", "Supply.SchDoc", "VIN_L",
        pcb_designator="R1.1",
        routed_pin_keys={"1", "2"},
    )
    assert n_pins == ["1"]
    assert n_tier == _LOCAL_NET_TIER_NAME


def test_resolve_local_net_pins_pcb_confirmed_beats_name():
    """When PCB net confirms a row, prefer that tier over bare name matches."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="OTHER",
            aliases=["VIN"],
            source_sheets=["Supply.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "2")],
        ),
        _FakeNet(
            name="VIN_1",
            aliases=["VIN"],
            source_sheets=["Supply.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "1")],
        ),
    ])
    pins, tier = _resolve_local_net_pins(
        netlist, "R1", "Supply.SchDoc", "VIN",
        pcb_designator="R1.1",
        routed_pin_keys={"1", "2"},
        pcb_net_by_pin={"1": "VIN_1", "2": "VOUT"},
    )
    # Pin 1 is PCB-confirmed (VIN_1 on the row); pin 2 is alias-only → drop it.
    assert pins == ["1"]
    assert tier == _LOCAL_NET_TIER_PCB


def test_series_shared_alias_resolves_disjoint_p_n():
    """SERIES with local VIN / VIN_L must not short both pads of a 2-pin part."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VIN_1",
            aliases=["VIN", "VIN.1"],
            source_sheets=["Supply.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "2")],
        ),
        _FakeNet(
            name="VIN_L.1",
            aliases=["VIN", "VIN.1"],
            source_sheets=["Supply.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("VIN_1"), RawNet("VIN_L.1")),
        pcb_components=(
            RawPcbComponent(
                designator="R1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R1",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "10m",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "VIN_L",
                },
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),  # VIN_L.1
            _pad(0, "2", 0, 1),  # VIN_1
        ),
        compiled_netlist=netlist,
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    series = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(series) == 1
    r = series[0]
    p_nets = {p.net_index for p in r.p.pins}
    n_nets = {p.net_index for p in r.n.pins}
    assert p_nets == {0}
    assert n_nets == {1}
    assert p_nets.isdisjoint(n_nets)


def test_arbitrate_overlapping_terminals_drops_weaker_side():
    p = TerminalSpec(
        pins=(TerminalPin("1", 1, 0, Pt2D(0, 0)),
              TerminalPin("2", 1, 1, Pt2D(1, 0))),
        requested_net="VIN",
        resolved_via_local=True,
    )
    n = TerminalSpec(
        pins=(TerminalPin("1", 1, 0, Pt2D(0, 0)),),
        requested_net="VIN_L",
        resolved_via_local=True,
    )
    result = AnnotationResult()
    p2, n2 = _arbitrate_overlapping_terminals(
        p, n, _LOCAL_NET_TIER_ALIAS, _LOCAL_NET_TIER_NAME, "SERIES on R1.1", result,
    )
    assert p2 is not None and n2 is not None
    assert {pin.pad_designator for pin in p2.pins} == {"2"}
    assert {pin.pad_designator for pin in n2.pins} == {"1"}
    assert any("both matched pin(s)" in w for w in result.warnings)
    # The pad designator, not the internal "R1.1:1" arbitration key: the
    # message tells the user to set PDN_*_PINS, which matches on pad names.
    assert any("kept on N" in w and "dropped from P" in w
               for w in result.warnings)
    assert not result.errors


def test_arbitrate_overlapping_terminals_equal_tier_errors():
    p = TerminalSpec(
        pins=(TerminalPin("1", 1, 0, Pt2D(0, 0)),),
        requested_net="VIN",
    )
    n = TerminalSpec(
        pins=(TerminalPin("1", 1, 0, Pt2D(0, 0)),),
        requested_net="VIN_L",
    )
    result = AnnotationResult()
    p2, n2 = _arbitrate_overlapping_terminals(
        p, n, _LOCAL_NET_TIER_ALIAS, _LOCAL_NET_TIER_ALIAS, "SERIES on R1", result,
    )
    assert p2 is None and n2 is None
    assert any("PDN_P_PINS" in e for e in result.errors)


def test_arbitrate_overlapping_terminals_ignores_same_pad_on_other_parts():
    """Banana-jack style: pad '1' on J2 vs pad '1' on J3 is not an overlap."""
    p = TerminalSpec(
        pins=(TerminalPin(
            "1", 1, 0, Pt2D(0, 0), component_designator="J2",
        ),),
        requested_net="+5V",
    )
    n = TerminalSpec(
        pins=(TerminalPin(
            "1", 1, 1, Pt2D(10, 0), component_designator="J3",
        ),),
        requested_net="GND",
    )
    result = AnnotationResult()
    p2, n2 = _arbitrate_overlapping_terminals(
        p, n, _LOCAL_NET_TIER_ALIAS, _LOCAL_NET_TIER_ALIAS, "SOURCE on J2", result,
    )
    assert p2 is p and n2 is n
    assert not result.errors
    assert not result.warnings


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


def test_lookup_inferred_vin_exact_match_without_series_walk():
    """Without a SERIES graph, only direct supply_map hits resolve."""
    supply_map = {"VDD_48V": 48.0, "VDD_12V": 12.0}
    assert _lookup_inferred_vin("VDD_48V", supply_map) == (48.0, None, 0)
    assert _lookup_inferred_vin("VDD_12V", supply_map) == (12.0, None, 0)
    assert _lookup_inferred_vin("VDD_48V_RP", supply_map) == (None, None, 0)


def test_lookup_inferred_vin_walks_series_upstream():
    supply_map = {"VDD_48V_IN": 48.0}
    graph = _SeriesVinGraph(
        upstream={"VDD_48V_RP": "VDD_48V", "VDD_48V": "VDD_48V_IN"},
    )
    # Two hops from the named rail — the caller warns on any non-zero hop count.
    assert _lookup_inferred_vin(
        "VDD_48V_RP", supply_map, graph=graph,
    ) == (48.0, None, 2)


def test_lookup_inferred_vin_series_ambiguous_and_cycle():
    supply_map = {"VDD_48V_IN": 48.0}
    assert _lookup_inferred_vin(
        "VDD_48V_RP", supply_map,
        graph=_SeriesVinGraph(ambiguous=frozenset({"VDD_48V_RP"})),
    ) == (None, "ambiguous", 0)
    assert _lookup_inferred_vin(
        "VDD_48V_RP", supply_map,
        graph=_SeriesVinGraph(
            upstream={"VDD_48V_RP": "VDD_48V", "VDD_48V": "VDD_48V_RP"},
        ),
    ) == (None, "cycle", 1)


def test_lookup_inferred_vin_declared_rail_beats_series_ambiguity():
    """A rail whose voltage is declared resolves even when SERIES links to it
    disagree — parallel ferrites / ORing FETs / fuse+bypass all land here."""
    assert _lookup_inferred_vin(
        "VDD_12V", {"VDD_12V": 12.0},
        graph=_SeriesVinGraph(ambiguous=frozenset({"VDD_12V"})),
    ) == (12.0, None, 0)


def test_lookup_inferred_vin_crosses_single_undirected_link():
    """An auto-inferred 2-pin element has no P/N direction, so it is crossed
    only when it is the one unvisited way out of the net."""
    supply_map = {"VDD_48V_IN": 48.0}
    one_way = _SeriesVinGraph(
        undirected={
            "VDD_48V": frozenset({"VDD_48V_IN"}),
            "VDD_48V_IN": frozenset({"VDD_48V"}),
        },
    )
    assert _lookup_inferred_vin("VDD_48V", supply_map, graph=one_way) == (
        48.0, None, 1,
    )
    forked = _SeriesVinGraph(
        undirected={"VDD_48V": frozenset({"VDD_48V_IN", "VDD_SENSE"})},
    )
    assert _lookup_inferred_vin("VDD_48V", supply_map, graph=forked) == (
        None, "undirected", 0,
    )


def test_lookup_inferred_vin_bounds_chain_length():
    """A chain longer than the hop cap errors instead of walking on."""
    chain = {f"N{i}": f"N{i + 1}" for i in range(_SERIES_VIN_MAX_HOPS + 3)}
    vin, failure, _hops = _lookup_inferred_vin(
        "N0", {"N99": 48.0}, graph=_SeriesVinGraph(upstream=chain),
    )
    assert vin is None
    assert failure == "too_deep"


def test_regulator_smps_vin_through_series_chain():
    """VIP-style: SOURCE -> SERIES -> SERIES -> SMPS on downstream net."""
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VDD_48V_IN"), RawNet("VDD_48V"),
            RawNet("VDD_48V_RP"), RawNet("VDD_12V"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "48",
                    "PDN_P_NET": "VDD_48V_IN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J8", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "50m",
                    "PDN_P_NET": "VDD_48V_IN",
                    "PDN_N_NET": "VDD_48V",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="Q3", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "300m",
                    "PDN_P_NET": "VDD_48V",
                    "PDN_N_NET": "VDD_48V_RP",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U5", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.8",
                    "PDN_V": "12",
                    "PDN_OUT_P_NET": "VDD_12V",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VDD_48V_RP",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J7", center=Pt2D(-15, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J7",
            ),
            RawPcbComponent(
                designator="J8", center=Pt2D(-10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="FUSE", source_designator="J8",
            ),
            RawPcbComponent(
                designator="Q3", center=Pt2D(-5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="Q3",
            ),
            RawPcbComponent(
                designator="U5", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U5",
            ),
        ),
        pads=(
            _pad(0, "1", 1, -15), _pad(0, "2", 0, -14),
            _pad(1, "1", 1, -10), _pad(1, "2", 2, -9),
            _pad(2, "1", 2, -5), _pad(2, "2", 3, -4),
            _pad(3, "1", 4, 0), _pad(3, "2", 0, 1),
            _pad(3, "3", 3, 2), _pad(3, "4", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    reg = next(d for d in result.directives if d.designator == "U5")
    assert isinstance(reg, RegulatorSpec)
    assert reg.regulator_type == "SMPS"
    assert abs(reg.gain - (12.0 / (48.0 * 0.8))) < 1e-6
    assert not any("cannot infer input voltage" in e for e in result.errors)


def test_regulator_smps_vin_series_ambiguous_fork():
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VDD_48V_IN"), RawNet("VDD_48V"),
            RawNet("VDD_48V_RP"), RawNet("VDD_12V"), RawNet("ALT_48V"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J7", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE", "PDN_V": "48",
                    "PDN_P_NET": "VDD_48V_IN", "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J8", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES", "PDN_R": "50m",
                    "PDN_P_NET": "VDD_48V_IN", "PDN_N_NET": "VDD_48V",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="Q3a", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES", "PDN_R": "100m",
                    "PDN_P_NET": "VDD_48V", "PDN_N_NET": "VDD_48V_RP",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="Q3b", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES", "PDN_R": "200m",
                    "PDN_P_NET": "ALT_48V", "PDN_N_NET": "VDD_48V_RP",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U5", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.8",
                    "PDN_V": "12",
                    "PDN_OUT_P_NET": "VDD_12V",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VDD_48V_RP",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J7", center=Pt2D(-15, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J7",
            ),
            RawPcbComponent(
                designator="J8", center=Pt2D(-10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="FUSE", source_designator="J8",
            ),
            RawPcbComponent(
                designator="Q3a", center=Pt2D(-5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="Q3a",
            ),
            RawPcbComponent(
                designator="Q3b", center=Pt2D(-4, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="Q3b",
            ),
            RawPcbComponent(
                designator="U5", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U5",
            ),
        ),
        pads=(
            _pad(0, "1", 1, -15), _pad(0, "2", 0, -14),
            _pad(1, "1", 1, -10), _pad(1, "2", 2, -9),
            _pad(2, "1", 2, -5), _pad(2, "2", 3, -4),
            _pad(3, "1", 5, -4), _pad(3, "2", 3, -3),
            _pad(4, "1", 4, 0), _pad(4, "2", 0, 1),
            _pad(4, "3", 3, 2), _pad(4, "4", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any("ambiguous SERIES upstream" in e for e in result.errors)


# --- SERIES Vin graph construction --------------------------------------------

_SERIES_VIN_NETS = {
    "GND": 0, "VDD_48V_IN": 1, "VDD_48V": 2, "VDD_12V": 3, "VDD_48V_F": 4,
}


def _series_vin_proj(
    series_params: dict[str, str],
    *,
    in_p_net: str = "VDD_48V",
    in_p_pad_net: int | None = None,
    series_pads: tuple[tuple[str, int], ...] = (("1", 1), ("2", 2)),
    extra_series: dict[str, str] | None = None,
) -> ExtractedProject:
    """SOURCE(48 V on VDD_48V_IN) → one SERIES part → SMPS on ``in_p_net``.

    ``series_params`` is the SERIES component's parameter dict, so each test can
    vary only how that one part declares its two nets. ``series_pads`` is its
    ``(pin, net_index)`` list *in file order* — the order 2-pin auto-inference
    reads P/N off. ``in_p_pad_net`` overrides the net the regulator's IN_P pad
    sits on, modelling the state ``_apply_net_remap`` leaves behind: the pad
    carries the canonical index while the annotation still names the folded one.
    """
    sch = [
        RawSchComponent(
            designator="J7", schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SOURCE", "PDN_V": "48",
                "PDN_P_NET": "VDD_48V_IN", "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="F1", schdoc_name="Pwr.SchDoc",
            parameters=series_params,
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="U5", schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "REGULATOR", "PDN_REGULATOR_TYPE": "SMPS",
                "PDN_REGULATOR_EFFICIENCY": "0.8", "PDN_V": "12",
                "PDN_OUT_P_NET": "VDD_12V", "PDN_OUT_N_NET": "GND",
                "PDN_IN_P_NET": in_p_net, "PDN_IN_N_NET": "GND",
            },
            pin_designators=("1", "2", "3", "4"),
        ),
    ]
    pcb = [
        RawPcbComponent(
            designator="J7", center=Pt2D(-15, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="CONN", source_designator="J7",
        ),
        RawPcbComponent(
            designator="F1", center=Pt2D(-10, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="FUSE", source_designator="F1",
        ),
        RawPcbComponent(
            designator="U5", center=Pt2D(0, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="SOT", source_designator="U5",
        ),
    ]
    # J7 = pcb 0, F1 = pcb 1, [R9 = pcb 2], U5 last.
    pads = [_pad(0, "1", 1, -15), _pad(0, "2", 0, -14)]
    pads += [_pad(1, pin, net, -10) for pin, net in series_pads]
    u5_index = 2
    if extra_series is not None:
        sch.insert(2, RawSchComponent(
            designator="R9", schdoc_name="Pwr.SchDoc",
            parameters=extra_series, pin_designators=("1", "2"),
        ))
        pcb.insert(2, RawPcbComponent(
            designator="R9", center=Pt2D(-7, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="0402", source_designator="R9",
        ))
        pads += [_pad(2, "1", 2, -7), _pad(2, "2", 4, -6)]
        u5_index = 3
    pads += [
        _pad(u5_index, "1", 3, 0),                              # OUT_P
        _pad(u5_index, "2", 0, 1),                              # OUT_N
        _pad(
            u5_index, "3",
            _SERIES_VIN_NETS[in_p_net] if in_p_pad_net is None else in_p_pad_net,
            2,
        ),                                                      # IN_P
        _pad(u5_index, "4", 0, 3),                              # IN_N
    ]
    return _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VDD_48V_IN"), RawNet("VDD_48V"),
            RawNet("VDD_12V"), RawNet("VDD_48V_F"),
        ),
        sch_components=tuple(sch),
        pcb_components=tuple(pcb),
        pads=tuple(pads),
    )


def test_series_upstream_map_reads_indexed_role_only_channels():
    """A part whose SERIES channels come only from PDN<n>_ROLE overrides still
    contributes its links — _is_pdn_annotated admits it, so the graph must too."""
    proj = _series_vin_proj({
        "PDN1_ROLE": "SERIES", "PDN1_R": "50m",
        "PDN1_P_NET": "VDD_48V_IN", "PDN1_N_NET": "VDD_48V",
        "PDN2_ROLE": "SERIES", "PDN2_R": "50m",
        "PDN2_P_NET": "VDD_48V", "PDN2_N_NET": "VDD_12V",
    })
    graph = _collect_series_upstream_map(_iter_pdn_parameter_sources(proj), proj)
    assert graph.upstream == {
        "VDD_48V": "VDD_48V_IN", "VDD_12V": "VDD_48V",
    }


def test_series_upstream_map_reads_pin_specified_channels():
    """PDN_P_PINS / PDN_N_PINS is a supported way to state a SERIES element's
    two sides, so it must produce the same edge PDN_P_NET / PDN_N_NET would."""
    proj = _series_vin_proj(
        {
            "PDN_ROLE": "SERIES", "PDN_R": "50m",
            "PDN_P_PINS": "1", "PDN_N_PINS": "2,3",
        },
        # pin 1 on VDD_48V_IN, pins 2+3 both on VDD_48V.
        series_pads=(("1", 1), ("2", 2), ("3", 2)),
    )
    graph = _collect_series_upstream_map(_iter_pdn_parameter_sources(proj), proj)
    assert graph.upstream == {"VDD_48V": "VDD_48V_IN"}

    result = parse_annotations(proj, enabled_layers=[1])
    reg = next(d for d in result.directives if d.designator == "U5")
    assert abs(reg.gain - (12.0 / (48.0 * 0.8))) < 1e-6


def test_series_upstream_map_auto_inferred_link_is_direction_agnostic():
    """_autoinfer_2pin_nets takes P/N from raw pad order, so the Vin result must
    not change when the two RawPad entries are swapped."""
    params = {"PDN_ROLE": "SERIES", "PDN_R": "50m"}
    forward = _series_vin_proj(params, series_pads=(("1", 1), ("2", 2)))
    reversed_ = _series_vin_proj(params, series_pads=(("2", 2), ("1", 1)))
    expected = 12.0 / (48.0 * 0.8)
    for proj in (forward, reversed_):
        graph = _collect_series_upstream_map(
            _iter_pdn_parameter_sources(proj), proj,
        )
        # Recorded as an undirected pair — pad order states no power flow.
        assert not graph.upstream
        assert graph.undirected["VDD_48V"] == frozenset({"VDD_48V_IN"})
        result = parse_annotations(proj, enabled_layers=[1])
        reg = next(d for d in result.directives if d.designator == "U5")
        assert abs(reg.gain - expected) < 1e-6, result.errors


def test_series_upstream_map_skips_merged_short_self_edges():
    """A 0 Ω the merge pre-pass absorbed has both ends on one net now. Neither
    the skip list nor the self-edge guard may let it shadow the real edge."""
    proj = _series_vin_proj(
        {
            "PDN_ROLE": "SERIES", "PDN_R": "50m",
            "PDN_P_NET": "VDD_48V_IN", "PDN_N_NET": "VDD_48V",
        },
        extra_series={
            "PDN_ROLE": "SERIES", "PDN_R": "0.001",
            "PDN_P_NET": "VDD_48V", "PDN_N_NET": "VDD_48V_F",
        },
    )
    # VDD_48V_F (index 4) merged into VDD_48V (index 2).
    net_remap = {4: 2}
    graph = _collect_series_upstream_map(
        _iter_pdn_parameter_sources(proj), proj,
        net_remap=net_remap, skip_designators={"R9"},
    )
    assert graph.upstream == {"VDD_48V": "VDD_48V_IN"}
    assert not graph.ambiguous

    # Even without the skip list, the self-edge guard must hold the line.
    unskipped = _collect_series_upstream_map(
        _iter_pdn_parameter_sources(proj), proj, net_remap=net_remap,
    )
    assert unskipped.upstream == {"VDD_48V": "VDD_48V_IN"}
    assert not unskipped.ambiguous


def test_regulator_smps_vin_resolves_under_merged_net_name():
    """The user may write either merged name. Both the supply map and the
    lookup key are canonicalised, so IN_P_NET on the folded name still lands."""
    proj = _series_vin_proj(
        {
            "PDN_ROLE": "SERIES", "PDN_R": "50m",
            "PDN_P_NET": "VDD_48V_IN", "PDN_N_NET": "VDD_48V",
        },
        in_p_net="VDD_48V_F",   # the name the user wrote
        in_p_pad_net=2,         # ...folded into VDD_48V by the merge pre-pass
    )
    result = parse_annotations(proj, enabled_layers=[1], net_remap={4: 2})
    reg = next(d for d in result.directives if d.designator == "U5")
    assert abs(reg.gain - (12.0 / (48.0 * 0.8))) < 1e-6, result.errors


def test_regulator_smps_vin_warns_when_inferred_through_series_chain():
    """Vin taken from a walked chain is a guess about which leg is the power
    path — a sense or bleed resistor produces a silently wrong gain, so say so."""
    proj = _series_vin_proj({
        "PDN_ROLE": "SERIES", "PDN_R": "50m",
        "PDN_P_NET": "VDD_48V_IN", "PDN_N_NET": "VDD_48V",
    })
    result = parse_annotations(proj, enabled_layers=[1])
    assert any(
        "was inferred 1 SERIES hop(s) upstream" in w for w in result.warnings
    ), result.warnings


def test_regulator_smps_vin_does_not_warn_on_directly_declared_rail():
    proj = _series_vin_proj(
        {
            "PDN_ROLE": "SERIES", "PDN_R": "50m",
            "PDN_P_NET": "VDD_48V_IN", "PDN_N_NET": "VDD_48V",
        },
        in_p_net="VDD_48V_IN",
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not any("SERIES hop(s) upstream" in w for w in result.warnings)


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
    unions: list[tuple[int, int]] = []
    _union_series_bridge_net_indices(
        proj, _iter_pdn_parameter_sources(proj),
        lambda a, b: unions.append((a, b)),
    )
    assert (1, 2) in unions  # +5V ↔ 5V_SW


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


def test_indexed_roles_only_bridge_registers():
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
    unions: list[tuple[int, int]] = []
    _union_series_bridge_net_indices(
        proj, _iter_pdn_parameter_sources(proj),
        lambda a, b: unions.append((a, b)),
    )
    assert (1, 2) in unions  # VIN ↔ VOUT


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
    assert not result.infos


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

    supply_map, _ = _collect_supply_voltages_by_net(
        _iter_pdn_parameter_sources(proj), proj,
    )
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


# --- Multi-connector PDN_*_DES ------------------------------------------------

def _banana_source_proj(*, n_des: str | None = "J3,J5,J7",
                       extra_params: dict | None = None,
                       omit_j7_gnd: bool = False,
                       include_sink: bool = True):
    """SOURCE on J2 (VIN/GND) with optional PDN_N_DES listing return connectors.

    Nets: 0=GND, 1=VIN. Each connector has pin 1 on VIN and pin 2 on GND
    unless ``omit_j7_gnd`` drops J7's GND pad.
    """
    src_params = {
        "PDN_ROLE": "SOURCE",
        "PDN_V": "5",
        "PDN_P_NET": "VIN",
        "PDN_N_NET": "GND",
    }
    if n_des is not None:
        src_params["PDN_N_DES"] = n_des
    if extra_params:
        src_params.update(extra_params)

    sch = [
        RawSchComponent(
            designator="J2", schdoc_name="Pwr.SchDoc",
            parameters=src_params,
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="J3", schdoc_name="Pwr.SchDoc",
            parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="J5", schdoc_name="Pwr.SchDoc",
            parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="J7", schdoc_name="Pwr.SchDoc",
            parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
        ),
    ]
    pcb = [
        RawPcbComponent(
            designator="J2", center=Pt2D(0, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="CONN", source_designator="J2",
        ),
        RawPcbComponent(
            designator="J3", center=Pt2D(5, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="CONN", source_designator="J3",
        ),
        RawPcbComponent(
            designator="J5", center=Pt2D(10, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="CONN", source_designator="J5",
        ),
        RawPcbComponent(
            designator="J7", center=Pt2D(15, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="CONN", source_designator="J7",
        ),
    ]
    pads = [
        _pad(0, "1", 1, 0),   # J2 VIN
        _pad(0, "2", 0, 1),   # J2 GND
        _pad(1, "1", 1, 5),   # J3 VIN
        _pad(1, "2", 0, 6),   # J3 GND
        _pad(2, "1", 1, 10),  # J5 VIN
        _pad(2, "2", 0, 11),  # J5 GND
        _pad(3, "1", 1, 15),  # J7 VIN
    ]
    if not omit_j7_gnd:
        pads.append(_pad(3, "2", 0, 16))  # J7 GND

    if include_sink:
        sch.append(RawSchComponent(
            designator="U1", schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SINK",
                "PDN_I": "1A",
                "PDN_P_NET": "VIN",
                "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ))
        pcb.append(RawPcbComponent(
            designator="U1", center=Pt2D(20, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="QFN", source_designator="U1",
        ))
        pads.extend([_pad(4, "1", 1, 20), _pad(4, "2", 0, 21)])

    return _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN")),
        sch_components=tuple(sch),
        pcb_components=tuple(pcb),
        pads=tuple(pads),
    )


def test_source_n_des_multi_connector():
    """SOURCE on J2 with PDN_N_DES=J3,J5,J7 → P on J2, N from three connectors."""
    proj = _banana_source_proj()
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sources = [d for d in result.directives if isinstance(d, SourceSpec)]
    assert len(sources) == 1
    src = sources[0]
    assert src.designator == "J2"
    assert {p.pad_designator for p in src.p.pins} == {"1"}
    assert {p.component_designator for p in src.p.pins} == {"J2"}
    assert len(src.n.pins) == 3
    assert {p.component_designator for p in src.n.pins} == {"J3", "J5", "J7"}
    assert {p.pad_designator for p in src.n.pins} == {"2"}
    # Host J2 GND pad is NOT auto-included when N_DES is set.
    assert "J2" not in {p.component_designator for p in src.n.pins}


def test_source_n_des_missing_designator_errors():
    proj = _banana_source_proj(n_des="J3,J99,J5")
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any(
        "J99" in e and "not found" in e for e in result.errors
    ), result.errors


def test_source_n_des_no_pad_on_net_errors():
    proj = _banana_source_proj(n_des="J3,J5,J7", omit_j7_gnd=True)
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any(
        "J7" in e and "no pad" in e and "GND" in e for e in result.errors
    ), result.errors


def test_source_without_des_backward_compat():
    """Without *_DES, P and N pads come from the host only."""
    proj = _banana_source_proj(n_des=None)
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    src = next(d for d in result.directives if isinstance(d, SourceSpec))
    assert {p.component_designator for p in src.p.pins} == {"J2"}
    assert {p.component_designator for p in src.n.pins} == {"J2"}
    assert {p.pad_designator for p in src.p.pins} == {"1"}
    assert {p.pad_designator for p in src.n.pins} == {"2"}


def test_sink_p_des_multi_connector():
    """Analogous SINK: host J1 draws from +5V; P pads from J2,J3."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "2A",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                    "PDN_P_DES": "J2,J3",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J2", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J3", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J5", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "5",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
            RawPcbComponent(
                designator="J2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J2",
            ),
            RawPcbComponent(
                designator="J3", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J3",
            ),
            RawPcbComponent(
                designator="J5", center=Pt2D(15, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J5",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0),   # J1 +5V (host — NOT in P when P_DES set)
            _pad(0, "2", 0, 1),   # J1 GND
            _pad(1, "1", 1, 5),   # J2 +5V
            _pad(1, "2", 0, 6),
            _pad(2, "1", 1, 10),  # J3 +5V
            _pad(2, "2", 0, 11),
            _pad(3, "1", 1, 15),  # J5 SOURCE +5V
            _pad(3, "2", 0, 16),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    snk = sinks[0]
    assert snk.designator == "J1"
    assert len(snk.p.pins) == 2
    assert {p.component_designator for p in snk.p.pins} == {"J2", "J3"}
    assert {p.component_designator for p in snk.n.pins} == {"J1"}
    assert "J1" not in {p.component_designator for p in snk.p.pins}


def test_source_n_des_pins_filter_intersects_net():
    """``*_PINS`` narrows the ``*_DES`` net match; it never overrides it.

    Regression: the pin filter used to select pads by name alone, so naming a
    pin that sits on the *other* rail resolved the N terminal onto that rail —
    both terminals on VIN, a dead short across the source, reported ok.
    """
    proj = _banana_source_proj(
        n_des="J3,J5", extra_params={"PDN_N_PINS": "1"},
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok, [
        (d.n.requested_net,
         [(p.component_designator, p.pad_designator, p.net_index)
          for p in d.n.pins])
        for d in result.directives if isinstance(d, SourceSpec)
    ]
    assert any(
        "J3" in e and "GND" in e and "'1'" in e for e in result.errors
    ), result.errors


def test_source_n_des_pins_filter_selects_matching_pad():
    """The filter still works when the named pin IS on the requested net."""
    proj = _banana_source_proj(
        n_des="J3,J5", extra_params={"PDN_N_PINS": "2"},
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    src = next(d for d in result.directives if isinstance(d, SourceSpec))
    assert {p.component_designator for p in src.n.pins} == {"J3", "J5"}
    assert {p.pad_designator for p in src.n.pins} == {"2"}
    gnd = [i for i, n in enumerate(proj.nets) if n.name == "GND"][0]
    assert {p.net_index for p in src.n.pins} == {gnd}


def _multi_channel_des_proj():
    """SOURCE J2 placed twice (multi-channel) with PDN_N_DES=J3 (placed once)."""
    return _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN")),
        sch_components=(
            RawSchComponent(
                designator="J2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE", "PDN_V": "5",
                    "PDN_P_NET": "VIN", "PDN_N_NET": "GND",
                    "PDN_N_DES": "J3",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J3", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J2_CH1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J2",
            ),
            RawPcbComponent(
                designator="J2_CH2", center=Pt2D(30, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J2",
            ),
            RawPcbComponent(
                designator="J3", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J3",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 30), _pad(1, "2", 0, 31),
            _pad(2, "1", 1, 5), _pad(2, "2", 0, 6),
        ),
    )


def test_source_des_on_multi_channel_part_is_refused():
    """A designator list cannot be qualified per channel, so refuse it.

    Regression: every placement got the identical N pads, stacking one 5 V
    source per channel on the same return node and multiplying the injected
    current, with ok=True and no warning.
    """
    result = parse_annotations(_multi_channel_des_proj(), enabled_layers=[1])
    assert not result.ok
    assert any(
        "PDN_N_DES" in e and "multi-channel" in e for e in result.errors
    ), result.errors
    assert not [d for d in result.directives if isinstance(d, SourceSpec)]


def test_indexed_single_net_channel_does_not_inherit_des_template():
    """A PDNn_NET channel must not inherit an unindexed PDN_*_DES template.

    Regression: *_DES was missing from the two-terminal form group, so the
    single-net channel inherited PDN_N_DES and _terminal_mode then rejected
    the channel as a form conflict the user never wrote.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN")),
        sch_components=(
            RawSchComponent(
                designator="J2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_N_DES": "J3",          # unindexed template only
                    "PDN1_ROLE": "SOURCE", "PDN1_V": "5",
                    "PDN1_NET": "VIN",          # single-net channel
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J3", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J2",
            ),
            RawPcbComponent(
                designator="J3", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J3",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sources = [d for d in result.directives if isinstance(d, SourceSpec)]
    assert [s.channel_index for s in sources] == [1]


def test_channel_defined_only_by_des_is_discovered():
    """``PDNn_N_DES`` alone marks channel n present, like ``PDNn_N_PINS``.

    Regression: *_DES was not a channel-defining suffix, so both indexed
    channels were silently dropped and the part collapsed to the unindexed
    directive resolving on the host's own pad.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN")),
        sch_components=(
            RawSchComponent(
                designator="J2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN1_ROLE": "SOURCE", "PDN1_V": "5",
                    "PDN1_P_NET": "VIN", "PDN1_N_NET": "GND",
                    "PDN1_N_DES": "J3",
                    "PDN2_ROLE": "SOURCE", "PDN2_V": "5",
                    "PDN2_P_NET": "VIN", "PDN2_N_NET": "GND",
                    "PDN2_N_DES": "J5",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J3", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J5", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "CONN"}, pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J2",
            ),
            RawPcbComponent(
                designator="J3", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J3",
            ),
            RawPcbComponent(
                designator="J5", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J5",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
            _pad(2, "1", 1, 10), _pad(2, "2", 0, 11),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    by_channel = {
        d.channel_index: d for d in result.directives
        if isinstance(d, SourceSpec)
    }
    assert set(by_channel) == {1, 2}
    assert {p.component_designator for p in by_channel[1].n.pins} == {"J3"}
    assert {p.component_designator for p in by_channel[2].n.pins} == {"J5"}


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


# --- physical-page-map harvesting ---------------------------------------------


def test_harvest_prefers_source_path_over_bare_file_name():
    """file_name is the bare leaf, so harvesting it collapses same-named
    sheets in different directories onto one map value."""
    from types import SimpleNamespace

    from fypa.altium.extract import _harvest_physical_sheet_names

    compiled = SimpleNamespace(physical_documents=[
        SimpleNamespace(id="physical:0", source_path="SubA/Power.SchDoc",
                        file_name="Power.SchDoc"),
        SimpleNamespace(id="physical:1", source_path="SubB/Power.SchDoc",
                        file_name="Power.SchDoc"),
    ])
    assert _harvest_physical_sheet_names(compiled) == (
        ("physical:0", "SubA/Power.SchDoc"),
        ("physical:1", "SubB/Power.SchDoc"),
    )


def test_harvest_falls_back_to_file_name_when_source_path_is_absent():
    from types import SimpleNamespace

    from fypa.altium.extract import _harvest_physical_sheet_names

    compiled = SimpleNamespace(physical_documents=[
        SimpleNamespace(id="physical:0", file_name="Power.SchDoc"),
    ])
    assert _harvest_physical_sheet_names(compiled) == (
        ("physical:0", "Power.SchDoc"),
    )


def test_harvest_is_empty_on_a_release_without_physical_documents():
    """The installed library may predate the compiled model entirely; that
    must yield an empty map, not an exception."""
    from types import SimpleNamespace

    from fypa.altium.extract import _harvest_physical_sheet_names

    assert _harvest_physical_sheet_names(SimpleNamespace()) == ()
    assert _harvest_physical_sheet_names(
        SimpleNamespace(physical_documents=None)) == ()
    # Entries missing either half are skipped rather than half-recorded.
    assert _harvest_physical_sheet_names(SimpleNamespace(physical_documents=[
        SimpleNamespace(id=None, source_path="A.SchDoc"),
        SimpleNamespace(id="physical:0", source_path=None, file_name=None),
    ])) == ()


def test_compile_falls_back_to_to_netlist_with_the_designs_own_options():
    """Rebuilding NetlistOptions.from_prjpcb() drops the merged per-sheet
    parameters that net labels substitute — only AltiumDesign.from_prjpcb adds
    them — so a fallback that does so silently renames nets."""

    from fypa.altium.extract import _compile_schematic_netlist

    sentinel = object()
    calls = []

    class _Design:
        schdocs = ["a.SchDoc"]
        project = None

        def to_netlist(self):
            calls.append("to_netlist")
            return sentinel

    netlist, sheet_names = _compile_schematic_netlist(_Design())
    assert netlist is sentinel
    assert sheet_names == ()
    assert calls == ["to_netlist"]


def test_a_failed_page_map_does_not_discard_a_good_netlist():

    from fypa.altium.extract import _compile_schematic_netlist

    sentinel = object()

    class _Compiled:
        def to_netlist(self):
            return sentinel

        @property
        def physical_documents(self):
            raise RuntimeError("field renamed upstream")

    class _Design:
        schdocs = ["a.SchDoc"]
        project = None

        def compile(self):
            return _Compiled()

        def to_netlist(self):
            raise AssertionError("must not recompile after a good compile()")

    netlist, sheet_names = _compile_schematic_netlist(_Design())
    assert netlist is sentinel
    assert sheet_names == ()


# --- P/N arbitration follow-ups ------------------------------------------------


def test_alias_fallback_does_not_outrank_a_name_level_match():
    """"The pad's PCB net is on the row" is the criterion the PCB tier
    explicitly rejects, so an alias-only hit must not be stamped tier 0 and
    strip a genuine net.name match on the other terminal."""
    from fypa.altium.annotations import (
        _LOCAL_NET_TIER_ALIAS, _LOCAL_NET_TIER_NAME, _LOCAL_NET_TIER_PCB,
    )

    assert _LOCAL_NET_TIER_ALIAS > _LOCAL_NET_TIER_NAME > _LOCAL_NET_TIER_PCB


def test_explicit_pin_override_outranks_a_net_name_match():
    """The overlap error tells the user to set PDN_*_PINS. That advice only
    works if an explicit pin list actually breaks the tie."""
    from fypa.altium.annotations import (
        _LOCAL_NET_TIER_DIRECT, _LOCAL_NET_TIER_OVERRIDE,
    )

    assert _LOCAL_NET_TIER_OVERRIDE < _LOCAL_NET_TIER_DIRECT


def _pin(pad):
    return TerminalPin(pad, 1, 0, Pt2D(0, 0))


def test_overlap_messages_name_pads_not_arbitration_keys():
    """The keys are composite ("R5:1") but PDN_*_PINS matches bare pad names,
    so printing the key sent the user to an error about an unknown pin."""
    p = TerminalSpec(
        pins=(TerminalPin("1", 1, 0, Pt2D(0, 0), component_designator="R5"),
              TerminalPin("2", 1, 0, Pt2D(0, 0), component_designator="R5")),
        requested_net="VIN",
    )
    n = TerminalSpec(
        pins=(TerminalPin("1", 1, 0, Pt2D(0, 0), component_designator="R5"),),
        requested_net="VIN_L",
    )
    result = AnnotationResult()
    _arbitrate_overlapping_terminals(
        p, n, _LOCAL_NET_TIER_ALIAS, _LOCAL_NET_TIER_NAME,
        "SERIES on R5", result,
    )
    text = " ".join(result.warnings + result.errors)
    assert "R5:1" not in text
    assert "pin(s) 1" in text


def test_empty_side_errors_without_a_contradictory_warning():
    """Warning that pins were "dropped from N" and then that N is empty and
    the directive discarded reads as two contradictory log lines."""
    shared = TerminalPin("1", 1, 0, Pt2D(0, 0))
    p = TerminalSpec(pins=(shared,), requested_net="VIN")
    n = TerminalSpec(pins=(shared,), requested_net="VIN_L")
    result = AnnotationResult()
    p2, n2 = _arbitrate_overlapping_terminals(
        p, n, _LOCAL_NET_TIER_NAME, _LOCAL_NET_TIER_ALIAS,
        "SERIES on R5", result,
    )
    assert p2 is not None and n2 is None
    assert result.errors and "would be empty" in result.errors[0]
    assert result.warnings == []


def test_equal_tier_error_points_at_the_remedy_that_works():
    shared = TerminalPin("1", 1, 0, Pt2D(0, 0))
    p = TerminalSpec(pins=(shared,), requested_net="VIN")
    n = TerminalSpec(pins=(shared,), requested_net="VIN")
    result = AnnotationResult()
    p2, n2 = _arbitrate_overlapping_terminals(
        p, n, _LOCAL_NET_TIER_NAME, _LOCAL_NET_TIER_NAME,
        "SERIES on R5", result,
    )
    assert p2 is None and n2 is None
    assert "outranks a net-name match" in result.errors[0]


def test_channel_token_must_come_from_flattening():
    """A part merely NAMED FB_2 is not channel 2 of FB, so an unrelated
    repeated sheet's VIN.2 must not match it."""
    # Genuine channel instance: the placed designator is the schematic one
    # plus the channel token, so the separators may legitimately disagree.
    assert _local_net_label_matches("VIN_1", "VIN", {"R1", "R1.1"})
    assert _local_net_label_matches("VIN.1", "VIN", {"R1", "R1_1"})
    # Not a channel: nothing in the candidate set is "FB" + sep + "2".
    assert not _local_net_label_matches("VIN.2", "VIN", {"FB_2"})
    assert not _local_net_label_matches("VIN_3", "VIN", {"SW_3"})
    # An exact label still matches regardless.
    assert _local_net_label_matches("VIN", "VIN", {"FB_2"})

# --- Sheet-symbol PDN overrides (rebased) ---

def test_ambiguous_hierpath_loser_still_warns_missing_designator():
    """Name-loser skips unbound warn but still flags unknown PDN designator."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "1A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Other.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={
                    "PDN_J1_I": "2A",
                    "PDN_J99_I": "9A",  # no such PCB designator
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id="",
                source_hierarchical_path=r"main\CON-A",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert not any(
        "not bound to any PCB placement" in w and "SYMB" in w
        for w in result.warnings
    )
    assert any(
        "PDN_J99_I" in w
        and "no PCB component with source designator 'J99'" in w
        and "Other.SchDoc" in w
        for w in result.warnings
    )

def test_ambiguous_hierpath_sheet_name_warns():
    """Two symbols with the same sheet_name → one used, others ignored."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "1A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Other.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={"PDN_J1_I": "2A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id="",
                source_hierarchical_path=r"main\CON-A",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert any(
        "SOURCEHIERARCHICALPATH matches" in w
        and "using 'SYMA'" in w
        and "ignoring" in w
        for w in result.warnings
    )
    assert not any(
        "not bound to any PCB placement" in w and "SYMB" in w
        for w in result.warnings
    )
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(1.0)  # SYMA wins over SYMB

def test_autoinfer_series_propagates_supply_voltage_for_smps_vin():
    """2-pin SERIES without P/N nets still forwards Vin via pad autoinfer."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("VIN_L"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "12",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="L1", schdoc_name="Main.SchDoc",
                parameters={"PDN_ROLE": "SERIES", "PDN_R": "5m"},
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.9",
                    "PDN_OUT_P_NET": "VOUT",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VIN_L",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
            RawPcbComponent(
                designator="L1", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="IND", source_designator="L1",
            ),
            RawPcbComponent(
                designator="U1", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            # L1 autoinfer: pad1 VIN (upstream), pad2 VIN_L (downstream)
            _pad(1, "1", 1, 5), _pad(1, "2", 2, 6),
            _pad(2, "1", 3, 10), _pad(2, "2", 0, 11),
            _pad(2, "3", 2, 12), _pad(2, "4", 0, 13),
        ),
    )
    supply_map, _ = _collect_supply_voltages_by_net(
        _iter_pdn_parameter_sources(proj), proj,
    )
    assert supply_map.get("VIN") == pytest.approx(12.0)
    assert supply_map.get("VIN_L") == pytest.approx(12.0)

    ann = parse_annotations(proj, enabled_layers=[1])
    assert ann.ok, ann.errors
    reg = next(d for d in ann.directives if isinstance(d, RegulatorSpec))
    assert reg.gain == pytest.approx(3.3 / (12.0 * 0.9))

def test_autoinfer_series_vin_ignores_reversed_pad_order():
    """Autoinfer SERIES copies Vin even when pad0 is the downstream net."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("VIN_L"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "12",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="L1", schdoc_name="Main.SchDoc",
                parameters={"PDN_ROLE": "SERIES", "PDN_R": "5m"},
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.9",
                    "PDN_OUT_P_NET": "VOUT",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VIN_L",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
            RawPcbComponent(
                designator="L1", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="IND", source_designator="L1",
            ),
            RawPcbComponent(
                designator="U1", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            # Reversed vs power flow: pad1=VIN_L (downstream), pad2=VIN (upstream)
            _pad(1, "1", 2, 5), _pad(1, "2", 1, 6),
            _pad(2, "1", 3, 10), _pad(2, "2", 0, 11),
            _pad(2, "3", 2, 12), _pad(2, "4", 0, 13),
        ),
    )
    supply_map, _ = _collect_supply_voltages_by_net(
        _iter_pdn_parameter_sources(proj), proj,
    )
    assert supply_map.get("VIN") == pytest.approx(12.0)
    assert supply_map.get("VIN_L") == pytest.approx(12.0)

    ann = parse_annotations(proj, enabled_layers=[1])
    assert ann.ok, ann.errors
    reg = next(d for d in ann.directives if isinstance(d, RegulatorSpec))
    assert reg.gain == pytest.approx(3.3 / (12.0 * 0.9))

def test_duplicate_sch_designator_with_sheet_symbols_not_multiplied():
    """Two SchDocs with same J1 + sheet binding → one warning, no dup sinks."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="J1", schdoc_name="AltPort.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "9A",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "1.5A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert any(
        "appears in multiple schdocs with PDN_ROLE" in w
        for w in result.warnings
    )
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(1.5)

def test_hierpath_bound_override_no_orphan_warning():
    """Hierpath-only binding must not emit UniqueID orphan warnings."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "2.5A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id="",
                source_hierarchical_path=r"main\CON-A",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert not any("not bound to any PCB placement" in w for w in result.warnings)
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(2.5)

def test_hierpath_nested_sheet_symbol_first_segment():
    """Path CON-A\\INNER (first segment is a sheet symbol) binds both; deeper wins."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Mid.SchDoc",
                unique_id="OUTER",
                parameters={"PDN_J1_I": "1A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Mid.SchDoc",
                sheet_name="INNER",
                child_filename="Port.SchDoc",
                unique_id="INNER",
                parameters={"PDN_J1_I": "2.5A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id="",
                source_hierarchical_path=r"CON-A\INNER",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(2.5)

def test_hierpath_single_segment_binds():
    """Path with only the sheet-symbol name (no root) still binds."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "1.7A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id="",
                source_hierarchical_path="CON-A",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(1.7)

def test_hierpath_skips_root_sheet_segment():
    """A sheet symbol named like the root path segment must not bind."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Top.SchDoc",
                sheet_name="main",  # same as root segment
                child_filename="Other.SchDoc",
                unique_id="ROOTISH",
                parameters={"PDN_J1_I": "9A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "1.2A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id="",
                source_hierarchical_path=r"main\CON-A",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(1.2)

def test_indexed_sheet_override_does_not_force_sibling_channel():
    """PDN_J1_2_I on one placement must not invent channel 2 on siblings."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={
                    "PDN_J1_2_I": "2A",
                    "PDN_J1_2_P_NET": "+5V",
                    "PDN_J1_2_N_NET": "GND",
                },
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={"PDN_J1_I": "500mA"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    by_des = {}
    for s in sinks:
        by_des.setdefault(s.designator, []).append(s)
    # J1.1: unindexed child I + indexed channel 2 from sheet
    assert len(by_des["J1.1"]) == 2
    assert {s.channel_index for s in by_des["J1.1"]} == {None, 2}
    # J1.2: only unindexed (overridden to 500mA) — no channel 2
    assert len(by_des["J1.2"]) == 1
    assert by_des["J1.2"][0].channel_index is None
    assert by_des["J1.2"][0].current == pytest.approx(0.5)
    assert not any("missing" in e and "PDN2" in e for e in result.errors)

def test_lookup_inferred_vin_ignores_series_bridge_groups():
    """Sense paths through GND must not make Vin ambiguous (project_a / PDN5_R)."""
    supply_map = {"VDD_48V": 48.0, "VDD_12V": 12.0}
    assert _lookup_inferred_vin("VDD_48V", supply_map)[0] == 48.0
    assert _lookup_inferred_vin("VDD_12V", supply_map)[0] == 12.0

def test_multi_instance_lx_supply_map_infers_downstream_smps_vin():
    """Shared local OUT=LX with different PDN_V must not drop Vin for rails.

    Two regulator placements publish LX.1=5 V / LX.2=12 V; SERIES inductors
    map LX→VDD_OUT to VDD_5V0 / VDD_12V; a second SERIES (listed *before* the
    inductor in the source list) bridges VDD_12V→VDD_12V_S so multi-hop
    fixpoint propagation still reaches a downstream SMPS on VDD_12V_S.
    """
    from fypa.altium.annotations import _expand_sheet_bound_parameter_sources

    reg_params = {
        "PDN_ROLE": "REGULATOR",
        "PDN_REGULATOR_TYPE": "SMPS",
        "PDN_REGULATOR_EFFICIENCY": "0.8",
        "PDN_OUT_P_NET": "LX",
        "PDN_OUT_N_NET": "GND",
        "PDN_IN_P_NET": "VIN",
        "PDN_IN_N_NET": "GND",
    }
    # nets: 0 GND, 1 VIN, 2 LX.1, 3 LX.2, 4 VDD_5V0, 5 VDD_12V,
    #       6 VDD_12V_S, 7 VDD_3V3
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VIN"),
            RawNet("LX.1"), RawNet("LX.2"),
            RawNet("VDD_5V0"), RawNet("VDD_12V"),
            RawNet("VDD_12V_S"), RawNet("VDD_3V3"),
        ),
        sch_components=(
            # Q1 before L1: one-pass copy would miss VDD_12V_S; fixpoint must not.
            RawSchComponent(
                designator="Q1", schdoc_name="Load.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "10m",
                    "PDN_P_NET": "VDD_12V",
                    "PDN_N_NET": "VDD_12V_S",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={**reg_params, "PDN_V": "5V"},
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="L1", schdoc_name="Pwr.SchDoc",
                # Indexed-only SERIES (no part-wide PDN_ROLE) must still
                # propagate Vin across the filter inductor.
                parameters={
                    "PDN1_ROLE": "SERIES",
                    "PDN1_R": "6m",
                    "PDN1_P_NET": "LX",
                    "PDN1_N_NET": "VDD_OUT",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2", schdoc_name="Load.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.85",
                    "PDN_OUT_P_NET": "VDD_3V3",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VDD_12V_S",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="J1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "24",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="PWR_5V",
                child_filename="Pwr.SchDoc",
                unique_id="SYM5",
                parameters={"PDN_U1_V": "5V"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="PWR_12V",
                child_filename="Pwr.SchDoc",
                unique_id="SYM12",
                parameters={"PDN_U1_V": "12 V"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
                source_unique_id=r"\SYM5\COMP",
                parameters=dict(reg_params),
            ),
            RawPcbComponent(
                designator="U1.2", center=Pt2D(20, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
                source_unique_id=r"\SYM12\COMP",
                parameters=dict(reg_params),
            ),
            RawPcbComponent(
                designator="L1.1", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="IND", source_designator="L1",
                source_unique_id=r"\SYM5\L",
            ),
            RawPcbComponent(
                designator="L1.2", center=Pt2D(25, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="IND", source_designator="L1",
                source_unique_id=r"\SYM12\L",
            ),
            RawPcbComponent(
                designator="Q1", center=Pt2D(35, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="FET", source_designator="Q1",
            ),
            RawPcbComponent(
                designator="U2", center=Pt2D(40, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U2",
            ),
            RawPcbComponent(
                designator="J1", center=Pt2D(-10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
        ),
        pads=(
            # U1.1: OUT on LX.1 (net 2), IN on VIN (1), GND (0)
            _pad(0, "1", 2), _pad(0, "2", 0, 1),
            _pad(0, "3", 1, 2), _pad(0, "4", 0, 3),
            # U1.2: OUT on LX.2 (net 3)
            _pad(1, "1", 3, 20), _pad(1, "2", 0, 21),
            _pad(1, "3", 1, 22), _pad(1, "4", 0, 23),
            # L1.1: LX.1 — VDD_5V0
            _pad(2, "1", 2, 5), _pad(2, "2", 4, 6),
            # L1.2: LX.2 — VDD_12V
            _pad(3, "1", 3, 25), _pad(3, "2", 5, 26),
            # Q1: VDD_12V — VDD_12V_S
            _pad(4, "1", 5, 35), _pad(4, "2", 6, 36),
            # U2: OUT VDD_3V3, IN VDD_12V_S
            _pad(5, "1", 7, 40), _pad(5, "2", 0, 41),
            _pad(5, "3", 6, 42), _pad(5, "4", 0, 43),
            # J1 SOURCE
            _pad(6, "1", 1, -10), _pad(6, "2", 0, -9),
        ),
        compiled_netlist=_FakeNetlist(nets=[
            _FakeNet(
                name="LX.1", aliases=["LX"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("U1.1", "1"), _FakeTerminal("L1.1", "1")],
            ),
            _FakeNet(
                name="LX.2", aliases=["LX"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("U1.2", "1"), _FakeTerminal("L1.2", "1")],
            ),
            _FakeNet(
                name="VDD_5V0", aliases=["VDD_OUT"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("L1.1", "2")],
            ),
            _FakeNet(
                name="VDD_12V", aliases=["VDD_OUT"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("L1.2", "2")],
            ),
        ]),
    )
    result = AnnotationResult()
    sources = _expand_sheet_bound_parameter_sources(
        proj, _iter_pdn_parameter_sources(proj, result), result,
    )
    supply_map, _ = _collect_supply_voltages_by_net(sources, proj)
    assert "LX" not in supply_map  # ambiguous shared local dropped
    assert supply_map.get("LX.1") == pytest.approx(5.0)
    assert supply_map.get("LX.2") == pytest.approx(12.0)
    assert supply_map.get("VDD_5V0") == pytest.approx(5.0)
    assert supply_map.get("VDD_12V") == pytest.approx(12.0)
    assert supply_map.get("VDD_12V_S") == pytest.approx(12.0)

    ann = parse_annotations(proj, enabled_layers=[1])
    assert ann.ok, ann.errors
    assert not any("cannot infer input voltage" in e for e in ann.errors)
    regs = {
        d.designator: d
        for d in ann.directives if isinstance(d, RegulatorSpec)
    }
    assert "U2" in regs
    # Vin=12, Vout=3.3, eta=0.85 -> gain = 3.3/(12*0.85)
    assert regs["U2"].gain == pytest.approx(3.3 / (12.0 * 0.85))

def test_multi_instance_missing_net_reports_once():
    """Missing PDN_P_NET on every instance → one logical error."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "1A",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    missing = [e for e in result.errors if "missing PDN_P_NET" in e]
    assert len(missing) == 1
    assert "SINK on J1:" in missing[0]

def test_multi_instance_missing_value_reports_once():
    """Missing PDN_I after expand → one logical error, not one per instance.

    A bound sheet symbol carries only a REPEAT slot that matches no PCB
    designator (``.99``). Discovery still sees ``PDN_I``, but no instance
    receives the override.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I.99": "1A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
            RawPcbComponent(
                designator="J1.3", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
            _pad(2, "1", 1, 10), _pad(2, "2", 0, 11),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    missing = [e for e in result.errors if "missing PDN_I" in e]
    # Reported against the logical designator, never once per placement: three
    # placements must read exactly like one. (Two distinct messages is the
    # single-placement baseline — _resolve_channel_roles names the channel,
    # then the role parser names the indexed alternative — so what this guards
    # is that neither is repeated.)
    assert len(missing) == len(set(missing)), missing
    assert all("SINK on J1:" in m for m in missing), missing
    assert not any("J1.1" in m or "J1.2" in m or "J1.3" in m for m in missing)

def test_orphan_sheet_override_does_not_invent_discovery_channel():
    """Unbound sheet-symbol PDN_I must not make a child channel 'present'."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="ORPHAN",
                child_filename="Port.SchDoc",
                unique_id="NOTONPCB",
                parameters={"PDN_J1_I": "1A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\OTHER\COMP",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.ok
    assert any(
        "missing PDN_I" in e and "SINK on J1" in e
        for e in result.errors
    )
    assert not any("missing required parameter PDN_I" in e for e in result.errors)

def test_orphan_sheet_override_warnings_consolidated():
    """Multiple orphan PDN keys on one symbol → one warning listing them."""
    proj = _minimal_proj(
        nets=(RawNet("GND"),),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="ORPHAN",
                child_filename="Port.SchDoc",
                unique_id="NOSUCHUID",
                parameters={
                    "PDN_J1_I": "1A",
                    "PDN_J1_ROLE": "SINK",
                    "PDN_J1_P_NET": "+5V",
                },
            ),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    uid_warns = [
        w for w in result.warnings
        if "NOSUCHUID" in w and "not bound to any PCB placement" in w
    ]
    assert len(uid_warns) == 1
    assert "PDN_J1_I" in uid_warns[0]
    assert "PDN_J1_ROLE" in uid_warns[0]
    assert "PDN_J1_P_NET" in uid_warns[0]

def test_partial_coverage_warning_labels_pcb_eco_separately():
    """Mixed PCB-ECO + sheet-only partial coverage names sources explicitly."""
    pcb_pdn = {
        "PDN_ROLE": "SINK",
        "PDN_I": "1A",
        "PDN_P_NET": "+5V",
        "PDN_N_NET": "GND",
    }
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={"Comment": "USB"},
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={
                    "PDN_J1_ROLE": "SINK",
                    "PDN_J1_I": "2A",
                    "PDN_J1_P_NET": "+5V",
                    "PDN_J1_N_NET": "GND",
                },
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-C",
                child_filename="Port.SchDoc",
                unique_id="SYMC",
                parameters={},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
                parameters=dict(pcb_pdn),
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
            RawPcbComponent(
                designator="J1.3", center=Pt2D(20, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMC\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 10), _pad(1, "2", 0, 11),
            _pad(2, "1", 1, 20), _pad(2, "2", 0, 21),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert any(
        "J1.2 (sheet symbol)" in w
        and "J1.1 (PCB-ECO)" in w
        and "but not J1.3" in w
        for w in result.warnings
    )

def test_pcb_eco_and_full_sheet_directive_deduped():
    """PCB-ECO ROLE + full sheet-symbol ROLE must not double-parse one placement."""
    pcb_pdn = {
        "PDN_ROLE": "REGULATOR",
        "PDN_GAIN": "1",
        "PDN_OUT_P_NET": "VOUT",
        "PDN_OUT_N_NET": "GND",
        "PDN_IN_P_NET": "VIN",
        "PDN_IN_N_NET": "GND",
    }
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "buck"},
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="PWR",
                child_filename="Pwr.SchDoc",
                unique_id="SYM",
                parameters={
                    "PDN_U1_ROLE": "REGULATOR",
                    "PDN_U1_V": "5V",
                    "PDN_U1_GAIN": "1",
                    "PDN_U1_OUT_P_NET": "VOUT",
                    "PDN_U1_OUT_N_NET": "GND",
                    "PDN_U1_IN_P_NET": "VIN",
                    "PDN_U1_IN_N_NET": "GND",
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
                source_unique_id=r"\SYM\COMP",
                parameters=dict(pcb_pdn),
            ),
        ),
        pads=(
            _pad(0, "1", 2), _pad(0, "2", 0, 1),
            _pad(0, "3", 1, 2), _pad(0, "4", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    regs = [d for d in result.directives if isinstance(d, RegulatorSpec)]
    assert len(regs) == 1
    assert regs[0].designator == "U1.1"
    assert regs[0].voltage == pytest.approx(5.0)

def test_pcb_eco_counts_toward_sheet_only_partial_coverage():
    """Sibling covered by PCB-ECO is not flagged as missing sheet-only ROLE."""
    pcb_pdn = {
        "PDN_ROLE": "SINK",
        "PDN_I": "100mA",
        "PDN_P_NET": "+5V",
        "PDN_N_NET": "GND",
    }
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={"Comment": "USB"},
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={
                    # Value-only override on the PCB-ECO instance.
                    "PDN_J1_I": "1.5A",
                },
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={
                    "PDN_J1_ROLE": "SINK",
                    "PDN_J1_I": "2A",
                    "PDN_J1_P_NET": "+5V",
                    "PDN_J1_N_NET": "GND",
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
                parameters=dict(pcb_pdn),
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 10), _pad(1, "2", 0, 11),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert not any("PDN covers" in w and "but not" in w for w in result.warnings)
    sinks = {
        d.designator: d
        for d in result.directives if isinstance(d, SinkSpec)
    }
    assert set(sinks) == {"J1.1", "J1.2"}
    assert sinks["J1.1"].current == pytest.approx(1.5)
    assert sinks["J1.2"].current == pytest.approx(2.0)

def test_pcb_eco_value_override_no_false_partial_coverage_warning():
    """PCB-ECO + value-only sheet override must not warn about a bare sibling."""
    pcb_pdn = {
        "PDN_ROLE": "SINK",
        "PDN_I": "100mA",
        "PDN_P_NET": "+5V",
        "PDN_N_NET": "GND",
    }
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={"Comment": "USB"},
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "1.5A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
                parameters=dict(pcb_pdn),
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 10), _pad(1, "2", 0, 11),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert not any("PDN covers" in w and "but not" in w for w in result.warnings)
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].designator == "J1.1"
    assert sinks[0].current == pytest.approx(1.5)

def test_series_supply_flow_uses_primary_net_not_alias_cross_product():
    """SERIES edges pair one primary expand per side (no alias fan-out)."""
    from fypa.altium.annotations import _iter_series_supply_flow_edges

    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("LX.2"), RawNet("VDD_12V"),
            # Extra PCB net that also expands from local LX / VDD_OUT aliases
            # would create a spurious cross edge if all expansions were paired.
            RawNet("LX_ALT"), RawNet("VDD_ALT"),
        ),
        sch_components=(
            RawSchComponent(
                designator="L1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "6m",
                    "PDN_P_NET": "LX",
                    "PDN_N_NET": "VDD_OUT",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="L1.2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="IND", source_designator="L1",
                source_unique_id=r"\SYM12\L",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 2, 1)),
        compiled_netlist=_FakeNetlist(nets=[
            _FakeNet(
                name="LX.2", aliases=["LX", "LX_ALT"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("L1.2", "1")],
            ),
            _FakeNet(
                name="VDD_12V", aliases=["VDD_OUT", "VDD_ALT"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("L1.2", "2")],
            ),
        ]),
    )
    edges = list(_iter_series_supply_flow_edges(
        _iter_pdn_parameter_sources(proj), proj,
    ))
    assert edges == [(True, "LX.2", "VDD_12V")]

def test_sheet_override_designator_with_trailing_letter():
    """PDN_U12A_I targets designator U12A, not U12 + suffix A_I."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="U12A", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_U12A_I": "1.8A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U12A.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U12A",
                source_unique_id=r"\SYMA\COMP",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(1.8)

def test_sheet_override_pdn_v_on_pcb_eco_regulator():
    """PCB-ECO REGULATOR without PDN_V still takes PDN_<Des>_V from sheet symbol.

    Matches the common pattern where ROLE/nets were ECO'd to the PCB and only
    the per-instance output voltage lives on the parent sheet symbol.
    """
    pcb_pdn = {
        "PDN_ROLE": "REGULATOR",
        "PDN_GAIN": "1",
        "PDN_OUT_P_NET": "VOUT",
        "PDN_OUT_N_NET": "GND",
        "PDN_IN_P_NET": "VIN",
        "PDN_IN_N_NET": "GND",
    }
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("VOUT.1"), RawNet("VOUT.2")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "buck"},
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="PWR_5V",
                child_filename="Pwr.SchDoc",
                unique_id="SYM5V",
                parameters={"PDN_U1_V": "5V"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="PWR_12V",
                child_filename="Pwr.SchDoc",
                unique_id="SYM12V",
                parameters={"PDN_U1_V": "12 V"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
                source_unique_id=r"\SYM5V\COMP",
                parameters=dict(pcb_pdn),
            ),
            RawPcbComponent(
                designator="U1.2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
                source_unique_id=r"\SYM12V\COMP",
                parameters=dict(pcb_pdn),
            ),
        ),
        pads=(
            _pad(0, "1", 2), _pad(0, "2", 0, 1),
            _pad(0, "3", 1, 2), _pad(0, "4", 0, 3),
            _pad(1, "1", 3, 10), _pad(1, "2", 0, 11),
            _pad(1, "3", 1, 12), _pad(1, "4", 0, 13),
        ),
        compiled_netlist=_FakeNetlist(nets=[
            _FakeNet(
                name="VOUT.1", aliases=["VOUT"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("U1.1", "1")],
            ),
            _FakeNet(
                name="VOUT.2", aliases=["VOUT"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("U1.2", "1")],
            ),
            _FakeNet(
                name="VIN",
                source_sheets=["pwr.schdoc"],
                terminals=[
                    _FakeTerminal("U1.1", "3"),
                    _FakeTerminal("U1.2", "3"),
                ],
            ),
            _FakeNet(
                name="GND",
                source_sheets=["pwr.schdoc"],
                terminals=[
                    _FakeTerminal("U1.1", "2"),
                    _FakeTerminal("U1.1", "4"),
                    _FakeTerminal("U1.2", "2"),
                    _FakeTerminal("U1.2", "4"),
                ],
            ),
        ]),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    regs = {
        d.designator: d
        for d in result.directives if isinstance(d, RegulatorSpec)
    }
    assert set(regs) == {"U1.1", "U1.2"}
    assert regs["U1.1"].voltage == pytest.approx(5.0)
    assert regs["U1.2"].voltage == pytest.approx(12.0)

def test_sheet_override_role_changes_parser_per_instance():
    """Per-placement PDN_J1_ROLE must dispatch SOURCE vs SINK correctly."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "1A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={
                    "PDN_J1_ROLE": "SOURCE",
                    "PDN_J1_V": "5V",
                    "PDN_J1_P_NET": "+5V",
                    "PDN_J1_N_NET": "GND",
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert not any("unknown PDN parameter 'PDN_I'" in w for w in result.warnings)
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    sources = [d for d in result.directives if isinstance(d, SourceSpec)]
    assert len(sinks) == 1 and sinks[0].designator == "J1.1"
    assert sinks[0].current == pytest.approx(1.0)
    assert len(sources) == 1 and sources[0].designator == "J1.2"
    assert sources[0].voltage == pytest.approx(5.0)

def test_sheet_override_role_strips_regulator_terminal_keys():
    """Leaving REGULATOR via sheet ROLE drops OUT_/IN_ keys without noise."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V"), RawNet("VIN")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_OUT_P_NET": "+5V",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VIN",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="REG-A",
                child_filename="Pwr.SchDoc",
                unique_id="SYMA",
                parameters={
                    "PDN_U1_ROLE": "SINK",
                    "PDN_U1_I": "0.5A",
                    "PDN_U1_P_NET": "+5V",
                    "PDN_U1_N_NET": "GND",
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOIC", source_designator="U1",
                source_unique_id=r"\SYMA\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(0, "3", 2, 2), _pad(0, "4", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert not any("OUT_P_NET" in w or "IN_P_NET" in w for w in result.warnings)
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(0.5)

def test_sheet_symbol_extract_to_parse_round_trip():
    """OwnerIndex extract → RawSchSheetSymbol → parse_annotations."""
    from fypa.altium.extract import _extract_sch_sheet_symbol

    @dataclass
    class _Param:
        name: str
        text: str
        owner_index: int

    @dataclass
    class _Record:
        unique_id: str = "SYMA"
        index_in_sheet: int = 7
        children: tuple = ()

    @dataclass
    class _Info:
        record: _Record
        designator: str = "CON-A"
        file_name: str = "Port.SchDoc"
        unique_id: str = "SYMA"

    @dataclass
    class _SchDoc:
        parameters: list

    sym = _extract_sch_sheet_symbol(
        _Info(record=_Record()),
        "Main.SchDoc",
        schdoc=_SchDoc(parameters=[
            _Param("PDN_J1_I", "1.5A", 7),
            _Param("Manufacturer", "ACME", 7),
        ]),
    )
    assert sym is not None
    assert sym.parameters == {"PDN_J1_I": "1.5A"}

    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(sym,),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(1.5)

def test_sheet_symbol_indexed_channel_override():
    """``PDN_U1_1_I`` overrides only the indexed PDN channel on the child."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Chip.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "50mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN1_I": "10mA",
                    "PDN1_P_NET": "+1V8",
                    "PDN1_N_NET": "GND",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CH",
                child_filename="Chip.SchDoc",
                unique_id="CHIPSYM",
                parameters={"PDN_U1_1_I": "80mA"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
                source_unique_id=r"\CHIPSYM\U1UID",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 2, 1), _pad(0, "3", 0, 2),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = {
        d.channel_index: d
        for d in result.directives if isinstance(d, SinkSpec)
    }
    assert sinks[None].current == pytest.approx(0.05)
    assert sinks[1].current == pytest.approx(0.08)

def test_sheet_symbol_nested_uid_deepest_wins():
    """Deeper UniqueID in SOURCEUNIQUEID overrides a shallower symbol."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="OUTER",
                child_filename="Mid.SchDoc",
                unique_id="OUTERUID",
                parameters={"PDN_J1_I": "1A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Mid.SchDoc",
                sheet_name="INNER",
                child_filename="Port.SchDoc",
                unique_id="INNERUID",
                parameters={"PDN_J1_I": "2A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\OUTERUID\INNERUID\COMP",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(2.0)

def test_sheet_symbol_only_directive_without_child_pdn_role():
    """Full SINK directive can live entirely on the sheet symbol."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={"Comment": "USB"},
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={
                    "PDN_J1_ROLE": "SINK",
                    "PDN_J1_I": "1.2A",
                    "PDN_J1_P_NET": "+5V",
                    "PDN_J1_N_NET": "GND",
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].designator == "J1.1"
    assert sinks[0].current == pytest.approx(1.2)

def test_sheet_symbol_only_multi_instance_different_currents():
    """Full sheet-symbol-only directive with different I per placement."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={"Comment": "USB"},
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={
                    "PDN_J1_ROLE": "SINK",
                    "PDN_J1_I": "1.5A",
                    "PDN_J1_P_NET": "+5V",
                    "PDN_J1_N_NET": "GND",
                },
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={
                    "PDN_J1_ROLE": "SINK",
                    "PDN_J1_I": "3A",
                    "PDN_J1_P_NET": "+5V",
                    "PDN_J1_N_NET": "GND",
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = {d.designator: d for d in result.directives if isinstance(d, SinkSpec)}
    assert sinks["J1.1"].current == pytest.approx(1.5)
    assert sinks["J1.2"].current == pytest.approx(3.0)

def test_sheet_symbol_only_partial_coverage_warns():
    """Full sheet-only on one placement, sibling unbound → warning."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={"Comment": "USB"},
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={
                    "PDN_J1_ROLE": "SINK",
                    "PDN_J1_I": "1.5A",
                    "PDN_J1_P_NET": "+5V",
                    "PDN_J1_N_NET": "GND",
                },
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMB",
                parameters={},  # no PDN — sibling uncovered
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert any(
        "PDN covers" in w
        and "J1.1 (sheet symbol)" in w
        and "but not J1.2" in w
        for w in result.warnings
    )
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].designator == "J1.1"

def test_sheet_symbol_override_absent_keeps_shared_child_current():
    """Without sheet overrides, every instance inherits the child PDN_I."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMA\COMP",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMB\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 1, 5), _pad(1, "2", 0, 6),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 2
    assert all(d.current == pytest.approx(0.5) for d in sinks)

def test_sheet_symbol_override_different_currents_per_instance():
    """Two sheet symbols override PDN_J1_I differently for J1.1 / J1.2."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VBUS.1"), RawNet("VBUS.2")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA",
                    "PDN_P_NET": "VBUS",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("A4", "A1"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMAAAAA",
                parameters={"PDN_J1_I": "1.5A"},
            ),
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-B",
                child_filename="Port.SchDoc",
                unique_id="SYMBBBBB",
                parameters={"PDN_J1_I": "3A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMAAAAA\COMPUID1",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\SYMBBBBB\COMPUID1",
            ),
        ),
        pads=(
            _pad(0, "A4", 1), _pad(0, "A1", 0, 1),
            _pad(1, "A4", 2, 10), _pad(1, "A1", 0, 11),
        ),
        compiled_netlist=_FakeNetlist(nets=[
            _FakeNet(
                name="VBUS.1", aliases=["VBUS"],
                source_sheets=["port.schdoc"],
                terminals=[_FakeTerminal("J1.1", "A4")],
            ),
            _FakeNet(
                name="VBUS.2", aliases=["VBUS"],
                source_sheets=["port.schdoc"],
                terminals=[_FakeTerminal("J1.2", "A4")],
            ),
            _FakeNet(
                name="GND",
                source_sheets=["port.schdoc"],
                terminals=[
                    _FakeTerminal("J1.1", "A1"),
                    _FakeTerminal("J1.2", "A1"),
                ],
            ),
        ]),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = {d.designator: d for d in result.directives if isinstance(d, SinkSpec)}
    assert set(sinks) == {"J1.1", "J1.2"}
    assert sinks["J1.1"].current == pytest.approx(1.5)
    assert sinks["J1.2"].current == pytest.approx(3.0)

def test_sheet_symbol_override_via_hierarchical_path_fallback():
    """When SOURCEUNIQUEID is empty, match sheet symbols via hierarchy path."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="CON-A",
                child_filename="Port.SchDoc",
                unique_id="SYMA",
                parameters={"PDN_J1_I": "2.5A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id="",  # force fallback
                source_hierarchical_path=r"main\CON-A",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert sinks[0].current == pytest.approx(2.5)

def test_sheet_symbol_params_via_owner_index():
    """Sheet-symbol PDN_* must be read from schdoc.parameters OwnerIndex.

    altium_monkey does not put sheet-symbol parameters on ``symbol.children``.
    """
    from fypa.altium.extract import (
        _extract_sch_sheet_symbol,
        _parameters_owned_by_index,
    )

    @dataclass
    class _Param:
        name: str
        text: str
        owner_index: int

    @dataclass
    class _Record:
        unique_id: str = "SYMUID"
        index_in_sheet: int = 42
        children: tuple = ()
        sheet_name: object = None
        file_name: object = None

    @dataclass
    class _Info:
        record: _Record
        designator: str = "CON-A"
        file_name: str = "Port.SchDoc"
        unique_id: str = "SYMUID"

    @dataclass
    class _SchDoc:
        parameters: list

    schdoc = _SchDoc(parameters=[
        _Param("PDN_J1_I", "1.5A", 42),
        _Param("PDN_J1_ROLE", "SINK", 42),
        _Param("Design Item Id", "BOM-PART", 42),  # filtered out
        _Param("Other", "x", 99),  # different owner — ignored
    ])
    owned = _parameters_owned_by_index(schdoc, 42)
    assert owned["PDN_J1_I"] == "1.5A"
    assert "Design Item Id" in owned  # raw owner lookup keeps all

    info = _Info(record=_Record())
    sym = _extract_sch_sheet_symbol(info, "Main.SchDoc", schdoc=schdoc)
    assert sym is not None
    assert sym.parameters == {"PDN_J1_I": "1.5A", "PDN_J1_ROLE": "SINK"}
    assert "Design Item Id" not in sym.parameters
    assert "Other" not in sym.parameters

def test_sheet_symbol_repeat_slot_qualified_override():
    """One REPEAT sheet symbol: ``PDN_J1_I.1`` / ``PDN_J1_I.2`` per channel."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VBUS.1"), RawNet("VBUS.2")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "VBUS",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="Repeat(PORT,1,2)",
                child_filename="Port.SchDoc",
                unique_id="REPEATSYM",
                parameters={
                    "PDN_J1_I.1": "1A",
                    "PDN_J1_I.2": "2A",
                },
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\REPEATSYM\COMP",
            ),
            RawPcbComponent(
                designator="J1.2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\REPEATSYM\COMP",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 2, 10), _pad(1, "2", 0, 11),
        ),
        compiled_netlist=_FakeNetlist(nets=[
            _FakeNet(
                name="VBUS.1", aliases=["VBUS"],
                source_sheets=["port.schdoc"],
                terminals=[_FakeTerminal("J1.1", "1")],
            ),
            _FakeNet(
                name="VBUS.2", aliases=["VBUS"],
                source_sheets=["port.schdoc"],
                terminals=[_FakeTerminal("J1.2", "1")],
            ),
            _FakeNet(
                name="GND",
                source_sheets=["port.schdoc"],
                terminals=[
                    _FakeTerminal("J1.1", "2"),
                    _FakeTerminal("J1.2", "2"),
                ],
            ),
        ]),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = {d.designator: d for d in result.directives if isinstance(d, SinkSpec)}
    assert sinks["J1.1"].current == pytest.approx(1.0)
    assert sinks["J1.2"].current == pytest.approx(2.0)

def test_sheet_symbol_repeat_slot_skipped_for_non_numeric_designator():
    """Slot-qualified override warns when PCB designator has no .N suffix."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Port.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+5V",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="Repeat(PORT,1,2)",
                child_filename="Port.SchDoc",
                unique_id="REPEATSYM",
                parameters={"PDN_J1_I.1": "2A"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1_CH1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="USB", source_designator="J1",
                source_unique_id=r"\REPEATSYM\COMP",
            ),
        ),
        pads=(_pad(0, "1", 1), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    # Override not applied — falls back to child default.
    assert sinks[0].current == pytest.approx(0.1)
    assert any(
        "PDN_J1_I.1" in w and "no numeric channel suffix" in w
        for w in result.warnings
    )

def test_smps_vin_expands_local_in_p_net_per_instance():
    """Local PDN_IN_P_NET (VDD_OUT) must resolve via instance expansion to Vin."""

    reg_params = {
        "PDN_ROLE": "REGULATOR",
        "PDN_REGULATOR_TYPE": "SMPS",
        "PDN_REGULATOR_EFFICIENCY": "0.8",
        "PDN_OUT_P_NET": "LX",
        "PDN_OUT_N_NET": "GND",
        "PDN_IN_P_NET": "VIN",
        "PDN_IN_N_NET": "GND",
    }
    proj = _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VIN"),
            RawNet("LX.2"), RawNet("VDD_12V"), RawNet("VDD_3V3"),
        ),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={**reg_params, "PDN_V": "12V"},
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="L1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "6m",
                    "PDN_P_NET": "LX",
                    "PDN_N_NET": "VDD_OUT",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.85",
                    "PDN_OUT_P_NET": "VDD_3V3",
                    "PDN_OUT_N_NET": "GND",
                    # Local child-sheet label — must expand to VDD_12V for Vin.
                    "PDN_IN_P_NET": "VDD_OUT",
                    "PDN_IN_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="J1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "24",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
        ),
        sch_sheet_symbols=(
            RawSchSheetSymbol(
                parent_schdoc="Main.SchDoc",
                sheet_name="PWR_12V",
                child_filename="Pwr.SchDoc",
                unique_id="SYM12",
                parameters={"PDN_U1_V": "12 V"},
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1.2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
                source_unique_id=r"\SYM12\COMP",
                parameters=dict(reg_params),
            ),
            RawPcbComponent(
                designator="L1.2", center=Pt2D(5, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="IND", source_designator="L1",
                source_unique_id=r"\SYM12\L",
            ),
            RawPcbComponent(
                designator="U2.2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U2",
                source_unique_id=r"\SYM12\U2",
            ),
            RawPcbComponent(
                designator="J1", center=Pt2D(-10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
        ),
        pads=(
            _pad(0, "1", 2), _pad(0, "2", 0, 1),
            _pad(0, "3", 1, 2), _pad(0, "4", 0, 3),
            _pad(1, "1", 2, 5), _pad(1, "2", 3, 6),
            _pad(2, "1", 4, 10), _pad(2, "2", 0, 11),
            _pad(2, "3", 3, 12), _pad(2, "4", 0, 13),
            _pad(3, "1", 1, -10), _pad(3, "2", 0, -9),
        ),
        compiled_netlist=_FakeNetlist(nets=[
            _FakeNet(
                name="LX.2", aliases=["LX"],
                source_sheets=["pwr.schdoc"],
                terminals=[_FakeTerminal("U1.2", "1"), _FakeTerminal("L1.2", "1")],
            ),
            _FakeNet(
                name="VDD_12V", aliases=["VDD_OUT"],
                source_sheets=["pwr.schdoc"],
                terminals=[
                    _FakeTerminal("L1.2", "2"), _FakeTerminal("U2.2", "3"),
                ],
            ),
        ]),
    )
    ann = parse_annotations(proj, enabled_layers=[1])
    assert ann.ok, ann.errors
    assert not any("cannot infer input voltage" in e for e in ann.errors)
    regs = {
        d.designator: d
        for d in ann.directives if isinstance(d, RegulatorSpec)
    }
    assert "U2.2" in regs
    assert regs["U2.2"].gain == pytest.approx(3.3 / (12.0 * 0.85))

def test_unplaced_series_does_not_emit_global_supply_edges():
    """SERIES without a PCB instance must not copy voltages between rails."""
    from fypa.altium.annotations import _iter_series_supply_flow_edges

    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="J1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "12",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="L99", schdoc_name="Main.SchDoc",
                # Explicit nets, but no PCB placement for L99.
                parameters={
                    "PDN_ROLE": "SERIES",
                    "PDN_R": "5m",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "VOUT",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Main.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "1",
                    "PDN_OUT_P_NET": "VOUT",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VIN",
                    "PDN_IN_N_NET": "GND",
                    "PDN_GAIN": "0.5",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="J1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="CONN", source_designator="J1",
            ),
            RawPcbComponent(
                designator="U1", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1), _pad(0, "2", 0, 1),
            _pad(1, "1", 2, 10), _pad(1, "2", 0, 11),
            _pad(1, "3", 1, 12), _pad(1, "4", 0, 13),
        ),
    )
    sources = _iter_pdn_parameter_sources(proj)
    assert list(_iter_series_supply_flow_edges(sources, proj)) == []
    supply_map, _ = _collect_supply_voltages_by_net(sources, proj)
    assert supply_map.get("VIN") == pytest.approx(12.0)
    # Must not inherit 12 V from the unplaced SERIES VIN→VOUT edge.
    assert supply_map.get("VOUT") == pytest.approx(3.3)

def test_resolve_local_net_relative_schdoc_with_basename_source_sheets():
    """Nested relative schdoc_name must still hit project-netlist pins."""
    netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="RAIL",
            aliases=["VIN_LOCAL"],
            source_sheets=["Child.SchDoc"],  # basename only
            terminals=[_FakeTerminal("R1", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("VIN_GROUP"),),
        pcb_components=(
            RawPcbComponent(
                designator="R1.1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R1",
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="R1", schdoc_name="mod/Child.SchDoc",
                parameters={}, pin_designators=("1", "2"),
            ),
        ),
        pads=(_pad(0, "1", 0),),
        compiled_netlist=netlist,
        schdoc_paths={"mod/child.schdoc": r"C:\proj\mod\Child.SchDoc"},
        sheet_netlists={},
    )
    # Must resolve via project netlist (not require child-sheet compile).
    with patch(
        "fypa.altium.annotations._compile_sheet_netlist_at_path",
    ) as compile_mock:
        spec, err, _tier = _resolve_terminal(
            proj, 0, "VIN_LOCAL", None, [1], "SERIES P",
            sch_lookup_designator="R1", schdoc_name="mod/Child.SchDoc",
        )
    assert not err and spec is not None
    assert spec.pins[0].net_index == 0
    compile_mock.assert_not_called()


def test_netlist_options_for_project_does_not_cache_load_failure():
    from fypa.altium.annotations import (
        _netlist_options_for_project,
        clear_annotation_caches,
    )

    clear_annotation_caches()
    proj = _minimal_proj(prjpcb_path=Path("C:/proj/board.PrjPcb"))
    sentinel = object()
    with patch(
        "altium_monkey.altium_prjpcb.AltiumPrjPcb",
        side_effect=[RuntimeError("locked"), object()],
    ):
        with patch(
            "altium_monkey.altium_netlist_options.NetlistOptions.from_prjpcb",
            return_value=sentinel,
        ) as from_prj:
            first = _netlist_options_for_project(proj)
            second = _netlist_options_for_project(proj)
    assert first is not sentinel  # defaults after failure
    assert second is sentinel
    assert from_prj.call_count == 1


def test_resolve_terminal_child_sheet_fallback_when_project_netlist_incomplete():
    """Nested REPEAT: project netlist may omit R1.3 while R1.1 exists."""
    full_netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="RAIL_A",
            aliases=["VIN_LOCAL.1"],
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "1")],
        ),
        _FakeNet(
            name="RAIL_B",
            aliases=["VOUT.1"],
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "2")],
        ),
    ])
    child_netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VIN_LOCAL",
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1", "1")],
        ),
        _FakeNet(
            name="VOUT",
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1", "2")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("VIN_GROUP.2"), RawNet("VOUT.3")),
        pcb_components=(
            RawPcbComponent(
                designator="R1.3", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R1",
            ),
        ),
        pads=(
            _pad(0, "1", 0),
            _pad(0, "2", 1),
        ),
        compiled_netlist=full_netlist,
        sheet_netlists={"child.schdoc": child_netlist},
    )
    p_spec, p_err, _tier = _resolve_terminal(
        proj, 0, "VIN_LOCAL", None, [1], "SERIES P",
        sch_lookup_designator="R1", schdoc_name="Child.SchDoc",
    )
    n_spec, n_err, _tier = _resolve_terminal(
        proj, 0, "VOUT", None, [1], "SERIES N",
        sch_lookup_designator="R1", schdoc_name="Child.SchDoc",
    )
    assert not p_err and not n_err
    assert p_spec is not None and n_spec is not None
    assert p_spec.pins[0].net_index == 0
    assert n_spec.pins[0].net_index == 1


def test_expand_net_names_child_sheet_fallback():
    """expand_net_names must use sheet_netlists when project netlist omits the instance."""
    full_netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="RAIL_A",
            aliases=["VIN_LOCAL.1"],
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "1")],
        ),
    ])
    child_netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VIN_LOCAL",
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("VIN_GROUP.2"),),
        pcb_components=(
            RawPcbComponent(
                designator="R1.3", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R", source_designator="R1",
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="R1", schdoc_name="Child.SchDoc",
                parameters={}, pin_designators=("1", "2"),
            ),
        ),
        pads=(_pad(0, "1", 0),),
        compiled_netlist=full_netlist,
        sheet_netlists={"child.schdoc": child_netlist},
    )
    names = _instance_resolver(proj).expand_net_names("VIN_LOCAL", 0)
    assert "VIN_GROUP.2" in names
    assert "VIN_LOCAL" in names


def test_resolve_local_net_pins_child_sheet_direct():
    child_netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VIN_LOCAL",
            terminals=[_FakeTerminal("R1", "1")],
        ),
    ])
    proj = _minimal_proj(sheet_netlists={"child.schdoc": child_netlist})
    from fypa.altium.annotations import _resolve_local_net_pins_child_sheet
    pins = _resolve_local_net_pins_child_sheet(
        proj, "R1", "Child.SchDoc", "VIN_LOCAL",
        routed_pin_keys={"1"},
    )
    assert pins == ["1"]
    assert _resolve_local_net_pins_child_sheet(
        proj, "R1", "Child.SchDoc", "VIN_LOCAL",
        routed_pin_keys={"2"},
    ) == []


def test_base_sch_designator_strips_numeric_channel():
    from fypa.altium.annotations import _base_sch_designator
    assert _base_sch_designator("R1.3") == "R1"
    assert _base_sch_designator("R1") == "R1"
    assert _base_sch_designator("J3_SL8M7") == "J3_SL8M7"
    assert _base_sch_designator("U1.A") == "U1.A"


def test_child_sheet_resolves_channel_designator_without_source_designator():
    """PCB designator R1.3 with no source_designator still matches child R1."""
    full_netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="RAIL_A",
            aliases=["VIN_LOCAL.1"],
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1.1", "1")],
        ),
    ])
    child_netlist = _FakeNetlist(nets=[
        _FakeNet(
            name="VIN_LOCAL",
            source_sheets=["Child.SchDoc"],
            terminals=[_FakeTerminal("R1", "1")],
        ),
    ])
    proj = _minimal_proj(
        nets=(RawNet("VIN_GROUP.2"),),
        pcb_components=(
            RawPcbComponent(
                designator="R1.3", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="1005R",
                source_designator="",  # missing — lookup falls back to R1.3
            ),
        ),
        sch_components=(
            RawSchComponent(
                designator="R1", schdoc_name="Child.SchDoc",
                parameters={}, pin_designators=("1", "2"),
            ),
        ),
        pads=(_pad(0, "1", 0),),
        compiled_netlist=full_netlist,
        sheet_netlists={"child.schdoc": child_netlist},
    )
    spec, err, _tier = _resolve_terminal(
        proj, 0, "VIN_LOCAL", None, [1], "SERIES P",
        sch_lookup_designator="R1.3", schdoc_name="Child.SchDoc",
    )
    assert not err and spec is not None
    assert spec.pins[0].net_index == 0
    names = _instance_resolver(proj).expand_net_names("VIN_LOCAL", 0)
    assert "VIN_GROUP.2" in names


def test_schdoc_path_key_prefers_relative_path_on_basename_collision():
    from fypa.altium.annotations import _get_child_sheet_netlist, _schdoc_path_key

    nl_a = _FakeNetlist(nets=[
        _FakeNet(name="VIN", terminals=[_FakeTerminal("R1", "1")]),
    ])
    nl_b = _FakeNetlist(nets=[
        _FakeNet(name="VOUT", terminals=[_FakeTerminal("R1", "1")]),
    ])
    # Collision: same basename, no basename alias — only relative keys.
    proj = _minimal_proj(
        schdoc_paths={
            "mod_a/child.schdoc": r"C:\proj\mod_a\Child.SchDoc",
            "mod_b/child.schdoc": r"C:\proj\mod_b\Child.SchDoc",
        },
        sheet_netlists={
            "mod_a/child.schdoc": nl_a,
            "mod_b/child.schdoc": nl_b,
        },
    )
    assert _schdoc_path_key(proj, "mod_a/Child.SchDoc") == "mod_a/child.schdoc"
    assert _schdoc_path_key(proj, "mod_b/Child.SchDoc") == "mod_b/child.schdoc"
    assert _get_child_sheet_netlist(proj, "mod_a/Child.SchDoc") is nl_a
    assert _get_child_sheet_netlist(proj, "mod_b/Child.SchDoc") is nl_b


def test_schdoc_path_key_refuses_ambiguous_basename_only():
    """Basename-only lookup must not guess when two sheets share the name."""
    from fypa.altium.annotations import _get_child_sheet_netlist, _schdoc_path_key

    nl_a = _FakeNetlist(nets=[
        _FakeNet(name="VIN", terminals=[_FakeTerminal("R1", "1")]),
    ])
    proj = _minimal_proj(
        schdoc_paths={
            "mod_a/child.schdoc": r"C:\proj\mod_a\Child.SchDoc",
            "mod_b/child.schdoc": r"C:\proj\mod_b\Child.SchDoc",
        },
        sheet_netlists={
            "mod_a/child.schdoc": nl_a,
            "child.schdoc": nl_a,  # must not be used under ambiguity
        },
    )
    assert _schdoc_path_key(proj, "Child.SchDoc") is None
    assert _get_child_sheet_netlist(proj, "Child.SchDoc") is None


def test_get_child_sheet_netlist_lazy_compile_memoizes():
    from fypa.altium.annotations import _get_child_sheet_netlist

    child_nl = _FakeNetlist(nets=[
        _FakeNet(name="VIN", terminals=[_FakeTerminal("R1", "1")]),
    ])
    proj = _minimal_proj(
        schdoc_paths={"child.schdoc": r"C:\proj\Child.SchDoc"},
        sheet_netlists={},
    )
    with patch(
        "fypa.altium.annotations._compile_sheet_netlist_at_path",
        return_value=child_nl,
    ) as compile_mock:
        first = _get_child_sheet_netlist(proj, "Child.SchDoc")
        second = _get_child_sheet_netlist(proj, "Child.SchDoc")
    assert first is child_nl
    assert second is child_nl
    assert compile_mock.call_count == 1
    assert proj.sheet_netlists.get("child.schdoc") is child_nl


def test_get_child_sheet_netlist_does_not_cache_failed_compile():
    from fypa.altium.annotations import _get_child_sheet_netlist

    child_nl = _FakeNetlist(nets=[
        _FakeNet(name="VIN", terminals=[_FakeTerminal("R1", "1")]),
    ])
    proj = _minimal_proj(
        schdoc_paths={"child.schdoc": r"C:\proj\Child.SchDoc"},
        sheet_netlists={},
    )
    with patch(
        "fypa.altium.annotations._compile_sheet_netlist_at_path",
        side_effect=[None, child_nl],
    ) as compile_mock:
        assert _get_child_sheet_netlist(proj, "Child.SchDoc") is None
        assert "child.schdoc" not in proj.sheet_netlists
        assert _get_child_sheet_netlist(proj, "Child.SchDoc") is child_nl
    assert compile_mock.call_count == 2


def test_compile_sheet_netlist_uses_project_netlist_options():
    from fypa.altium.annotations import (
        _compile_sheet_netlist_at_path,
        clear_annotation_caches,
    )

    clear_annotation_caches()
    proj = _minimal_proj(prjpcb_path=Path("C:/proj/board.PrjPcb"))
    sentinel = object()
    with (
        patch(
            "fypa.altium.annotations._netlist_options_for_project",
            return_value=sentinel,
        ) as opts_mock,
        patch("altium_monkey.altium_schdoc.AltiumSchDoc") as sch_cls,
        patch(
            "altium_monkey.altium_netlist_compilation.compile_netlist",
            return_value="NL",
        ) as compile_mock,
    ):
        sch_cls.return_value = object()
        assert _compile_sheet_netlist_at_path(r"C:\proj\Child.SchDoc", proj) == "NL"
    opts_mock.assert_called_once_with(proj)
    assert compile_mock.call_args.args[2] is sentinel


def test_collect_schdoc_paths_unique_basename_alias_only():
    from fypa.altium.extract import _collect_schdoc_paths

    @dataclass
    class _Sch:
        filepath: str

    @dataclass
    class _Design:
        schdocs: list

    root = Path("C:/proj")
    design = _Design(schdocs=[
        _Sch(filepath=str(root / "Power.SchDoc")),
        _Sch(filepath=str(root / "mod_a" / "Child.SchDoc")),
        _Sch(filepath=str(root / "mod_b" / "Child.SchDoc")),
    ])
    paths = _collect_schdoc_paths(design, root / "board.PrjPcb")
    assert paths["power.schdoc"].lower().endswith("power.schdoc")
    assert "child.schdoc" not in paths  # basename collision — no alias
    assert "mod_a/child.schdoc" in paths
    assert "mod_b/child.schdoc" in paths


def test_collect_schdoc_paths_outside_root_uses_absolute_key():
    from fypa.altium.extract import (
        _collect_schdoc_paths,
        _schdoc_display_path,
        _schdoc_storage_key,
    )

    @dataclass
    class _Sch:
        filepath: str

    @dataclass
    class _Design:
        schdocs: list

    root = Path("C:/proj")
    outside_a = Path("D:/libs/Shared.SchDoc")
    outside_b = Path("E:/other/Shared.SchDoc")
    design = _Design(schdocs=[
        _Sch(filepath=str(outside_a)),
        _Sch(filepath=str(outside_b)),
    ])
    paths = _collect_schdoc_paths(design, root / "board.PrjPcb")
    key_a = _schdoc_storage_key(outside_a, root)
    key_b = _schdoc_storage_key(outside_b, root)
    assert key_a != key_b
    assert key_a == _schdoc_display_path(outside_a, root).lower()
    assert "shared.schdoc" not in paths  # colliding basenames — no alias
    assert paths[key_a].lower().endswith("shared.schdoc")
    assert paths[key_b].lower().endswith("shared.schdoc")


def test_schdoc_display_path_preserves_casing():
    from fypa.altium.extract import _schdoc_display_path, _schdoc_storage_key

    root = Path("C:/proj")
    abs_path = root / "mod" / "Child.SchDoc"
    display = _schdoc_display_path(abs_path, root)
    assert display == "mod/Child.SchDoc"
    assert _schdoc_storage_key(abs_path, root) == "mod/child.schdoc"


def test_pin_filter_sch_with_pcb_role_emits_no_info():
    # Expected Blanket/ECO workflow: SchLib carries PDN_PINS_ONLY only;
    # role+values live on the PCB. Suppress the pin-filter INFO.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={
                    "PDN_PINS_ONLY": "1,2,TP",
                    "PDN_EXTRA_PINS": "EP",
                    "PDN_IGNORE_PINS": "NC",
                },
                pin_designators=("1", "2", "TP", "EP", "NC"),
            ),
            # Multipart twin — same designator, still no INFO spam.
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={"PDN_PINS_ONLY": "1,2,TP"},
                pin_designators=("1", "2", "TP"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U2",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "10mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN_PINS_ONLY": "1,2,TP",
                },
            ),
        ),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not any("pin-filter parameter" in i for i in result.infos)
    assert not any("U2" in w and "no PDN_ROLE" in w for w in result.warnings)
    assert any(isinstance(d, SinkSpec) and d.designator == "U2"
               for d in result.directives)


def test_pin_filter_only_without_pcb_role_is_info_once():
    # Pin-filter on the symbol with no role anywhere → INFO (forgotten ECO),
    # but only once per designator even with multiple schematic parts.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={"PDN_PINS_ONLY": "1,2"},
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={"PDN_PINS_ONLY": "1,2"},
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U2",
            ),
        ),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 0, 1)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    pin_filter_infos = [
        i for i in result.infos if "pin-filter parameter" in i and "U2" in i
    ]
    assert len(pin_filter_infos) == 1
    assert not result.directives
    assert not any("no PDN_ROLE" in w for w in result.warnings)


# --- PDN_PINS_ONLY / PDN_EXTRA_PINS / PDN_IGNORE ------------------------------

def test_allow_pins_for_part_union_and_none():
    from fypa.altium.annotations import _allow_pins_for_part

    assert _allow_pins_for_part({}) is None
    assert _allow_pins_for_part({"PDN_PINS_ONLY": "1, EP"}) == frozenset({"1", "EP"})
    assert _allow_pins_for_part({"PDN_EXTRA_PINS": "SNS"}) == frozenset({"SNS"})
    assert _allow_pins_for_part({
        "PDN_PINS_ONLY": "1,2",
        "PDN_EXTRA_PINS": "EP",
    }) == frozenset({"1", "2", "EP"})


def test_resolve_terminal_honours_pins_only_allowlist():
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("GND")),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "EN", 0, 1.0),
            _pad(0, "GND", 1, 2.0),
        ),
    )
    warnings: list[str] = []
    spec, errs, _tier = _resolve_terminal(
        proj, 0, "VIN", None, [1], "SINK P",
        warnings=warnings, allow_pins=frozenset({"1", "GND"}),
    )
    assert not errs and spec is not None
    assert [p.pad_designator for p in spec.pins] == ["1"]


def test_resolve_terminal_extra_pins_extends_allowlist():
    from fypa.altium.annotations import _allow_pins_for_part

    allow = _allow_pins_for_part({
        "PDN_PINS_ONLY": "1",
        "PDN_EXTRA_PINS": "EP",
    })
    proj = _minimal_proj(
        nets=(RawNet("GND"),),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "EP", 0, 1.0),
            _pad(0, "EN", 0, 2.0),
        ),
    )
    spec, errs, _tier = _resolve_terminal(
        proj, 0, "GND", None, [1], "SINK N",
        allow_pins=allow,
    )
    assert not errs and spec is not None
    assert sorted(p.pad_designator for p in spec.pins) == ["1", "EP"]


def test_resolve_terminal_override_pins_bypass_allowlist():
    proj = _minimal_proj(
        nets=(RawNet("VIN"),),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "EN", 0, 1.0),
        ),
    )
    spec, errs, _tier = _resolve_terminal(
        proj, 0, "VIN", ["EN"], [1], "SINK P",
        allow_pins=frozenset({"1"}),
    )
    assert not errs and spec is not None
    assert [p.pad_designator for p in spec.pins] == ["EN"]


def test_parse_sink_pins_only_keeps_en_off_rail():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN_PINS_ONLY": "1,GND",
                },
                pin_designators=("1", "EN", "GND"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0.0),
            _pad(0, "EN", 1, 1.0),
            _pad(0, "GND", 0, 2.0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.errors
    sink = next(d for d in result.directives if isinstance(d, SinkSpec))
    assert sorted(p.pad_designator for p in sink.p.pins) == ["1"]
    assert sorted(p.pad_designator for p in sink.n.pins) == ["GND"]


def test_allowlist_empty_pool_errors_once_for_two_terminals():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN_PINS_ONLY": "NOSUCH",
                },
                pin_designators=("1", "GND"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0.0),
            _pad(0, "GND", 0, 1.0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    empty = [
        e for e in result.errors
        if "no pads match PDN_PINS_ONLY" in e
    ]
    assert len(empty) == 1
    assert empty[0].startswith("U1:")


def test_allowlist_missing_pin_warns_once_for_two_terminals():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "100mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN_PINS_ONLY": "1,GND,MISSING",
                },
                pin_designators=("1", "GND"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0.0),
            _pad(0, "GND", 0, 1.0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    miss = [
        w for w in result.warnings
        if "not on the PCB" in w and "MISSING" in w
    ]
    assert len(miss) == 1


def test_resolve_terminal_allowlist_error_names_excluded_net_pads():
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("GND")),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0.0),   # on GND — in allowlist
            _pad(0, "EN", 0, 1.0),  # on VIN — outside allowlist
        ),
    )
    spec, errs, _tier = _resolve_terminal(
        proj, 0, "VIN", None, [1], "SINK P",
        allow_pins=frozenset({"1"}),
    )
    assert spec is None
    assert errs
    assert "no allowlisted pad on net" in errs[0]
    assert "EN" in errs[0]
    assert "PDN_PINS_ONLY" in errs[0]


def test_parse_sink_pins_only_partitions_multi_channel_rails():
    # Nets: 0=GND, 1=+3V3, 2=+1V8 — EN hard-tied to +3V3 must stay off both.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3"), RawNet("+1V8")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN1_I": "250mA",
                    "PDN1_P_NET": "+1V8",
                    "PDN1_N_NET": "GND",
                    "PDN_PINS_ONLY": "A1,A2,GND",
                },
                pin_designators=("A1", "A2", "EN", "GND"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="BGA", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "A1", 1, 0.0),
            _pad(0, "A2", 2, 1.0),
            _pad(0, "EN", 1, 2.0),
            _pad(0, "GND", 0, 3.0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 2
    by_ch = {d.channel_index: d for d in sinks}
    assert sorted(p.pad_designator for p in by_ch[None].p.pins) == ["A1"]
    assert sorted(p.pad_designator for p in by_ch[1].p.pins) == ["A2"]
    for s in sinks:
        assert sorted(p.pad_designator for p in s.n.pins) == ["GND"]


def test_parse_regulator_honours_pins_only_on_in_and_out():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+5V"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "3.3",
                    "PDN_GAIN": "0.9",
                    "PDN_OUT_P_NET": "+3V3",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "+5V",
                    "PDN_IN_N_NET": "GND",
                    "PDN_PINS_ONLY": "IN,OUT,GND",
                },
                pin_designators=("IN", "OUT", "EN", "GND"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="SOT", source_designator="U2",
            ),
        ),
        pads=(
            _pad(0, "IN", 1, 0.0),
            _pad(0, "OUT", 2, 1.0),
            _pad(0, "EN", 1, 2.0),  # on +5V, must not join IN
            _pad(0, "GND", 0, 3.0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert not result.errors
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert sorted(p.pad_designator for p in reg.in_p.pins) == ["IN"]
    assert sorted(p.pad_designator for p in reg.out_p.pins) == ["OUT"]
    assert sorted(p.pad_designator for p in reg.in_n.pins) == ["GND"]
    assert sorted(p.pad_designator for p in reg.out_n.pins) == ["GND"]


def test_resolve_terminal_allowlist_then_ignore():
    proj = _minimal_proj(
        nets=(RawNet("VIN"),),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "2", 0, 1.0),
            _pad(0, "EN", 0, 2.0),
        ),
    )
    warnings: list[str] = []
    spec, errs, _tier = _resolve_terminal(
        proj, 0, "VIN", None, [1], "SINK P",
        warnings=warnings,
        allow_pins=frozenset({"1", "2"}),
        ignore_pins=frozenset({"2"}),
    )
    assert not errs and spec is not None
    assert [p.pad_designator for p in spec.pins] == ["1"]
    assert any("ignoring pin" in w and "2" in w for w in warnings)


def test_warn_unknown_flags_indexed_pins_only():
    result = AnnotationResult()
    comp = PdnParameterSource(
        designator="U1", schdoc_name="Pwr.SchDoc",
        parameters={
            "PDN_ROLE": "SINK",
            "PDN_I": "100mA",
            "PDN_P_NET": "+3V3",
            "PDN_N_NET": "GND",
            "PDN1_PINS_ONLY": "1,2",
        },
    )
    _warn_unknown_pdn_params(comp, "SINK", result)
    assert any("PDN1_PINS_ONLY" in w and "part-wide" in w for w in result.warnings)


def test_warn_unknown_does_not_flag_pins_only():
    result = AnnotationResult()
    comp = PdnParameterSource(
        designator="U1", schdoc_name="Pwr.SchDoc",
        parameters={
            "PDN_ROLE": "SINK",
            "PDN_I": "100mA",
            "PDN_P_NET": "+3V3",
            "PDN_N_NET": "GND",
            "PDN_PINS_ONLY": "1,GND",
            "PDN_EXTRA_PINS": "EP",
        },
    )
    _warn_unknown_pdn_params(comp, "SINK", result)
    assert not any("PINS_ONLY" in w or "EXTRA_PINS" in w for w in result.warnings)


def test_resolve_terminal_excludes_ignore_pins_from_net_match():
    from fypa.altium.annotations import _ignore_pins_for_channel

    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("GND")),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "2", 0, 1.0),   # enable hard-tied to VIN
            _pad(0, "3", 1, 2.0),
        ),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={"Comment": "IC"},
                pin_designators=("1", "2", "3"),
                ignored_pins=frozenset({"2"}),
            ),
        ),
    )
    warnings: list[str] = []
    ign = _ignore_pins_for_channel(
        {}, None, frozenset({"2"}),
    )
    spec, errs, _tier = _resolve_terminal(
        proj, 0, "VIN", None, [1], "SINK P",
        warnings=warnings, ignore_pins=ign,
    )
    assert not errs and spec is not None
    assert [p.pad_designator for p in spec.pins] == ["1"]
    assert any("ignoring pin" in w and "2" in w for w in warnings)


def test_resolve_terminal_ignore_wins_over_include_list():
    proj = _minimal_proj(
        nets=(RawNet("VIN"),),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "EN", 0, 1.0),
        ),
    )
    warnings: list[str] = []
    spec, errs, _tier = _resolve_terminal(
        proj, 0, "VIN", ["1", "EN"], [1], "SINK P",
        warnings=warnings, ignore_pins=frozenset({"EN"}),
    )
    assert not errs and spec is not None
    assert [p.pad_designator for p in spec.pins] == ["1"]


def test_parse_sink_honours_pdn_ignore_pins_list():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                    "PDN_IGNORE_PINS": "EN",
                },
                pin_designators=("1", "EN", "GND"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0.0),
            _pad(0, "EN", 1, 1.0),
            _pad(0, "GND", 0, 2.0),
        ),
    )
    result = parse_annotations(proj)
    assert not result.errors, result.errors
    assert len(result.directives) == 1
    sink = result.directives[0]
    assert isinstance(sink, SinkSpec)
    assert [p.pad_designator for p in sink.p.pins] == ["1"]
    assert any("IGNORE" in w for w in result.warnings)


def test_parse_sink_channel_ignore_pins():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("AVDD"), RawNet("DVDD")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "10mA",
                    "PDN_P_NET": "AVDD",
                    "PDN_N_NET": "GND",
                    "PDN1_I": "20mA",
                    "PDN1_P_NET": "DVDD",
                    "PDN1_N_NET": "GND",
                    "PDN1_IGNORE_PINS": "4",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0.0),  # AVDD
            _pad(0, "2", 0, 1.0),  # GND
            _pad(0, "3", 2, 2.0),  # DVDD
            _pad(0, "4", 2, 3.0),  # DVDD enable-like
        ),
    )
    result = parse_annotations(proj)
    assert not result.errors, result.errors
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 2
    by_ch = {d.channel_index: d for d in sinks}
    assert [p.pad_designator for p in by_ch[None].p.pins] == ["1"]
    assert [p.pad_designator for p in by_ch[1].p.pins] == ["3"]


def test_is_pdn_ignore_param_alias():
    from fypa.altium.extract import _is_pdn_ignore_param

    assert _is_pdn_ignore_param("PDN_IGNORE", "1")
    assert _is_pdn_ignore_param("PDN_IGNORE", "TRUE")
    assert _is_pdn_ignore_param("PDN", "IGNORE")
    assert not _is_pdn_ignore_param("PDN_IGNORE", "")
    assert not _is_pdn_ignore_param("PDN", "1")
    assert not _is_pdn_ignore_param("OTHER", "IGNORE")


def test_ignored_pins_from_sch_component_owner_index():
    from types import SimpleNamespace

    from fypa.altium.extract import _ignored_pins_from_sch_component

    pin = SimpleNamespace(
        designator="EN",
        _record_index=42,
        pin_parameters=[],
    )
    comp = SimpleNamespace(pins=[pin], children=[])
    param = SimpleNamespace(
        name="PDN_IGNORE", text="1", owner_index=42,
    )
    assert _ignored_pins_from_sch_component(comp, [param]) == frozenset({"EN"})


def test_warn_unknown_does_not_flag_ignore_pins():
    result = AnnotationResult()
    comp = PdnParameterSource(
        designator="U1", schdoc_name="Pwr.SchDoc",
        parameters={
            "PDN_ROLE": "SINK",
            "PDN_I": "100mA",
            "PDN_P_NET": "+3V3",
            "PDN_N_NET": "GND",
            "PDN_IGNORE_PINS": "EN",
        },
    )
    _warn_unknown_pdn_params(comp, "SINK", result)
    assert not any("IGNORE_PINS" in w for w in result.warnings)


def test_parse_annotations_honours_sch_ignored_pins():
    """End-to-end: pin-level ignored_pins on RawSchComponent reach the sink."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN_I": "500mA",
                    "PDN_P_NET": "+3V3",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "EN", "GND"),
                ignored_pins=frozenset({"EN"}),
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="QFN", source_designator="U1",
            ),
        ),
        pads=(
            _pad(0, "1", 1, 0.0),
            _pad(0, "EN", 1, 1.0),
            _pad(0, "GND", 0, 2.0),
        ),
    )
    result = parse_annotations(proj)
    assert not result.errors, result.errors
    sink = result.directives[0]
    assert isinstance(sink, SinkSpec)
    assert [p.pad_designator for p in sink.p.pins] == ["1"]
    assert any("IGNORE" in w for w in result.warnings)


def test_sch_ignored_pins_empty_sheet_match_does_not_import_other_sheet():
    """Matching sheet with no ignores must not fall back to another sheet."""
    from fypa.altium.annotations import _sch_ignored_pins

    proj = _minimal_proj(
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={}, pin_designators=("1", "EN"),
                ignored_pins=frozenset(),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Other.SchDoc",
                parameters={}, pin_designators=("1", "EN"),
                ignored_pins=frozenset({"EN"}),
            ),
        ),
    )
    assert _sch_ignored_pins(proj, "U1", "Pwr.SchDoc") == frozenset()


def test_sch_ignored_pins_fallback_when_sheet_missing():
    from fypa.altium.annotations import _sch_ignored_pins

    proj = _minimal_proj(
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={}, pin_designators=("1", "EN"),
                ignored_pins=frozenset({"EN"}),
            ),
        ),
    )
    assert _sch_ignored_pins(proj, "U1", "Missing.SchDoc") == frozenset({"EN"})


def test_sch_ignored_pins_blank_sheet_does_not_union_ambiguous():
    """Blank/None schdoc must not merge ignores across same designators."""
    from fypa.altium.annotations import _sch_ignored_pins

    proj = _minimal_proj(
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={}, pin_designators=("1", "EN"),
                ignored_pins=frozenset(),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Other.SchDoc",
                parameters={}, pin_designators=("1", "EN"),
                ignored_pins=frozenset({"EN"}),
            ),
        ),
    )
    assert _sch_ignored_pins(proj, "U1", "") == frozenset()
    assert _sch_ignored_pins(proj, "U1", None) == frozenset()


def test_sch_ignored_pins_blank_sheet_unique_placement_ok():
    from fypa.altium.annotations import _sch_ignored_pins

    proj = _minimal_proj(
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={}, pin_designators=("1", "EN"),
                ignored_pins=frozenset({"EN"}),
            ),
        ),
    )
    assert _sch_ignored_pins(proj, "U1", "") == frozenset({"EN"})
    assert _sch_ignored_pins(proj, "U1", None) == frozenset({"EN"})


def test_sch_ignored_pins_missing_sheet_ambiguous_no_fallback_union():
    """Mismatched sheet + multiple placements → empty, not a cross-sheet union."""
    from fypa.altium.annotations import _sch_ignored_pins

    proj = _minimal_proj(
        sch_components=(
            RawSchComponent(
                designator="U1", schdoc_name="Pwr.SchDoc",
                parameters={}, pin_designators=("1",),
                ignored_pins=frozenset({"1"}),
            ),
            RawSchComponent(
                designator="U1", schdoc_name="Other.SchDoc",
                parameters={}, pin_designators=("EN",),
                ignored_pins=frozenset({"EN"}),
            ),
        ),
    )
    assert _sch_ignored_pins(proj, "U1", "Missing.SchDoc") == frozenset()


# --- PR #36 review fixes ------------------------------------------------------
#
# Each test below pins one behaviour that was wrong (or silently absent) in the
# first cut of the pin-allowlist / local-net work. Names say what breaks.


# --- PR #36 review fixes ------------------------------------------------------
#
# Each test below pins one behaviour that was wrong (or silently absent) in the
# first cut of the pin-allowlist / local-net work. Names say what breaks.

def _u2_pcb(**params) -> RawPcbComponent:
    return RawPcbComponent(
        designator="U2", center=Pt2D(0, 0), rotation_deg=0.0,
        layer_name="TOP", footprint="QFN", source_designator="U2",
        parameters=params,
    )


def test_symbol_pins_only_applies_to_pcb_sourced_directive():
    # The documented Blanket/ECO workflow: PDN_PINS_ONLY stays on the SchLib
    # symbol while the ECO puts PDN_ROLE + values on the PCB instance. The PCB
    # source is the only one parsed, so the symbol's allowlist has to be
    # adopted or the hard-tied EN pad joins the +3V3 terminal and steals a
    # share of the sink current.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={"PDN_PINS_ONLY": "1,2,G1"},
                pin_designators=("1", "2", "EN", "G1"),
            ),
        ),
        pcb_components=(_u2_pcb(
            PDN_ROLE="SINK", PDN_I="500mA",
            PDN_P_NET="+3V3", PDN_N_NET="GND",
        ),),
        pads=(
            _pad(0, "1", 1, 0), _pad(0, "2", 1, 1),
            _pad(0, "EN", 1, 2), _pad(0, "G1", 0, 3),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert len(sinks) == 1
    assert {p.pad_designator for p in sinks[0].p.pins} == {"1", "2"}
    assert not result.errors


def test_symbol_ignore_pins_applies_to_pcb_sourced_directive():
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={"PDN_IGNORE_PINS": "EN"},
                pin_designators=("1", "EN", "G1"),
            ),
        ),
        pcb_components=(_u2_pcb(
            PDN_ROLE="SINK", PDN_I="500mA",
            PDN_P_NET="+3V3", PDN_N_NET="GND",
        ),),
        pads=(_pad(0, "1", 1, 0), _pad(0, "EN", 1, 1), _pad(0, "G1", 0, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert {p.pad_designator for p in sinks[0].p.pins} == {"1"}


def test_pcb_pin_filter_overrides_symbol_value():
    # The board-level statement is the more specific one and wins outright
    # (not a union) for the same key.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={"PDN_PINS_ONLY": "1,G1"},
                pin_designators=("1", "2", "G1"),
            ),
        ),
        pcb_components=(_u2_pcb(
            PDN_ROLE="SINK", PDN_I="500mA",
            PDN_P_NET="+3V3", PDN_N_NET="GND",
            PDN_PINS_ONLY="2,G1",
        ),),
        pads=(_pad(0, "1", 1, 0), _pad(0, "2", 1, 1), _pad(0, "G1", 0, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert {p.pad_designator for p in sinks[0].p.pins} == {"2"}


def test_pcb_extra_pins_unions_with_symbol_pins_only():
    # The documented per-board tweak: symbol owns PINS_ONLY, the placed part
    # adds a forgotten sense pin. Different keys, so both apply.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        sch_components=(
            RawSchComponent(
                designator="U2", schdoc_name="Mcu.SchDoc",
                parameters={"PDN_PINS_ONLY": "1,G1"},
                pin_designators=("1", "SNS", "G1"),
            ),
        ),
        pcb_components=(_u2_pcb(
            PDN_ROLE="SINK", PDN_I="500mA",
            PDN_P_NET="+3V3", PDN_N_NET="GND",
            PDN_EXTRA_PINS="SNS",
        ),),
        pads=(_pad(0, "1", 1, 0), _pad(0, "SNS", 1, 1), _pad(0, "G1", 0, 2)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    sinks = [d for d in result.directives if isinstance(d, SinkSpec)]
    assert {p.pad_designator for p in sinks[0].p.pins} == {"1", "SNS"}
    # Both keys are present on the merged part, so no lone-EXTRA_PINS warning.
    assert not any("EXTRA_PINS is set without" in w for w in result.warnings)


def test_extra_pins_without_pins_only_warns():
    # EXTRA_PINS alone IS the whole allowlist, so this quietly collapses a
    # multi-pin rail terminal to one pad. Nothing else flags it.
    comp = PdnParameterSource(
        designator="U9", schdoc_name="Mcu.SchDoc",
        parameters={
            "PDN_ROLE": "SINK", "PDN_I": "500mA",
            "PDN_P_NET": "+3V3", "PDN_N_NET": "GND",
            "PDN_EXTRA_PINS": "SNS",
        },
    )
    result = AnnotationResult()
    _warn_unknown_pdn_params(comp, "SINK", result)
    assert any("EXTRA_PINS is set without PDN_PINS_ONLY" in w
               for w in result.warnings)


def test_pin_ignores_do_not_leak_between_sheets_sharing_a_basename():
    from fypa.altium.annotations import _sch_ignored_pins

    proj = _minimal_proj(sch_components=(
        RawSchComponent(
            designator="U1", schdoc_name="mod_a/Child.SchDoc",
            parameters={}, pin_designators=("1", "EN"),
            ignored_pins=frozenset({"EN"}),
        ),
        RawSchComponent(
            designator="U1", schdoc_name="mod_b/Child.SchDoc",
            parameters={}, pin_designators=("1", "EN"),
        ),
    ))
    assert _sch_ignored_pins(proj, "U1", "mod_a/Child.SchDoc") == frozenset({"EN"})
    # mod_b's U1 has no ignores of its own and must not inherit mod_a's.
    assert _sch_ignored_pins(proj, "U1", "mod_b/Child.SchDoc") == frozenset()
    # A bare basename cannot pick between them - refuse rather than guess.
    assert _sch_ignored_pins(proj, "U1", "Child.SchDoc") == frozenset()


def test_pin_ignores_union_across_parts_of_one_multipart_symbol():
    from fypa.altium.annotations import _sch_ignored_pins

    proj = _minimal_proj(sch_components=(
        RawSchComponent(
            designator="U1", schdoc_name="Mcu.SchDoc", parameters={},
            pin_designators=("1",), ignored_pins=frozenset({"1"}),
        ),
        RawSchComponent(
            designator="U1", schdoc_name="Mcu.SchDoc", parameters={},
            pin_designators=("2",), ignored_pins=frozenset({"2"}),
        ),
    ))
    # Same physical part, two part records - both sets apply.
    assert _sch_ignored_pins(proj, "U1", "Mcu.SchDoc") == frozenset({"1", "2"})
    assert _sch_ignored_pins(proj, "U1", None) == frozenset({"1", "2"})


def test_infer_schdoc_resolves_multipart_symbol():
    # A dual-part symbol is two sch_components rows on one sheet; counting rows
    # instead of sheets made this return "" and killed the child-sheet
    # local-net fallback it exists for.
    proj = _minimal_proj(
        sch_components=(
            RawSchComponent(designator="R1", schdoc_name="Child.SchDoc",
                            parameters={}, pin_designators=("1",)),
            RawSchComponent(designator="R1", schdoc_name="Child.SchDoc",
                            parameters={}, pin_designators=("2",)),
        ),
        pcb_components=(RawPcbComponent(
            designator="R1.3", center=Pt2D(0, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="R", parameters={},
        ),),
    )
    resolver = _instance_resolver(proj)
    assert resolver.infer_schdoc(0, "R1.3") == "Child.SchDoc"


def test_clear_annotation_caches_drops_pad_and_net_indices():
    from fypa.altium import annotations as ann

    proj = _minimal_proj(nets=(RawNet("GND"),), pads=(_pad(0, "1", 0),))
    ann._net_indices_by_name(proj, "GND")
    ann._pads_by_component_all(proj)
    assert ann._net_indices_cache and ann._pads_by_comp_cache
    ann.clear_annotation_caches()
    # Left populated, these pin the *previous* project's whole extract in
    # memory for the entire duration of the next project's load.
    assert not ann._net_indices_cache
    assert not ann._pads_by_comp_cache


def test_missing_net_error_wins_over_empty_allowlist():
    # Both are wrong, but the absent net is the one that blocks everything
    # downstream and the one the user can act on; reporting only "no pads
    # match PDN_PINS_ONLY" sends them after the wrong problem.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("+3V3")),
        pcb_components=(_u2_pcb(),),
        pads=(_pad(0, "1", 1, 0), _pad(0, "G1", 0, 1)),
    )
    spec, errors, _tier = _resolve_terminal(
        proj, 0, None, None, [1], "SINK on U2",
        allow_pins=frozenset({"NOSUCH"}),
    )
    assert spec is None
    assert any("neither a net nor pin overrides supplied" in e for e in errors)
    assert not any("no pads match" in e for e in errors)


def test_child_sheet_compile_warning_is_deduped():
    from fypa.altium.annotations import (
        _compile_sheet_netlist_at_path,
        clear_annotation_caches,
    )

    clear_annotation_caches()
    proj = _minimal_proj()
    with patch("fypa.altium.annotations.log") as log_mock:
        for _ in range(5):
            # No such file, so the parse raises identically every time.
            assert _compile_sheet_netlist_at_path(
                "C:/nope/Child.SchDoc", proj,
            ) is None
        assert log_mock.warning.call_count == 1


def test_extracted_project_pickle_drops_sheet_netlists():
    import pickle

    proj = _minimal_proj(schdoc_paths={"child.schdoc": "C:/p/Child.SchDoc"})
    proj.sheet_netlists["child.schdoc"] = {"a": "compiled netlist stand-in"}
    revived = pickle.loads(pickle.dumps(proj))
    # Rebuildable cache - it must not ride along into the design-info pickle.
    assert revived.sheet_netlists == {}
    assert revived.schdoc_paths == {"child.schdoc": "C:/p/Child.SchDoc"}
    assert proj.sheet_netlists  # the live project keeps its cache


def test_degraded_net_guess_does_not_leak_into_allowlist_error():
    # No compiled netlist, so the resolver falls back to channel-suffix net
    # guesses ("VCC" -> "VCC.4"). A guess that fails to match must not be left
    # standing as the net whose pads get listed: the message names the user's
    # net, so listing pads from VCC.4 points them at the wrong pins.
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VCC"), RawNet("VCC.4")),
        pcb_components=(RawPcbComponent(
            designator="U2.4", center=Pt2D(0, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="QFN", parameters={},
        ),),
        pads=(
            _pad(0, "1", 0, 0),   # allowlisted, but sits on GND
            _pad(0, "2", 2, 1),   # on the *guessed* net VCC.4
            _pad(0, "3", 1, 2),   # on the net the user actually named
        ),
        compiled_netlist=None,
    )
    spec, errors, _tier = _resolve_terminal(
        proj, 0, "VCC", None, [1], "SINK on U2.4",
        allow_pins=frozenset({"1"}),
    )
    assert spec is None
    joined = " ".join(errors)
    assert "no allowlisted pad on net 'VCC'" in joined
    assert "'3'" in joined       # the pad actually on VCC
    assert "'2'" not in joined   # the pad on the failed guess VCC.4
