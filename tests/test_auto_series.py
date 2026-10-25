"""Auto-SERIES: unannotated low-value resistors modelled at their own value.

A current-sense shunt with no PDN_* parameters used to be left open at DC,
which split a rail at the shunt and broke regulator Vin inference across it.
Resistors at or below ``auto_series_max_ohm`` are now modelled as SERIES with
their own value — without merging the nets, unless the value is also under
the loader's 0.9 mohm merge threshold.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from fypa.altium.annotations import (
    DEFAULT_AUTO_SERIES_MAX_OHM,
    NET_TIE_BRIDGE_RESISTANCE_OHM,
    RegulatorSpec,
    ResistorSpec,
    _low_ohm_resistor_value,
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
from fypa.project_file import ProjectFile


# ---------------------------------------------------------------------------
# Value recognition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("params,designator,ohms", [
    ({"Resistance": "5mR"}, "R18", 0.005),
    ({"Resistance": "5 mΩ"}, "R18", 0.005),
    ({"Resistance": "0R005"}, "R18", 0.005),
    ({"Resistance": "10 mOhm"}, "R1", 0.01),
    ({"Resistance": "5E-3"}, "R1", 0.005),
    ({"Value": "0.1"}, "R24", 0.1),
    ({"Value": "20mR, 1%, 1W"}, "R31", 0.02),
    ({"Comment": "4k7"}, "R2", 4700.0),
    # A dedicated Resistance parameter is trusted whatever the designator.
    ({"Resistance": "2m"}, "SH1", 0.002),
])
def test_resistor_values_are_read(params, designator, ohms):
    found = _low_ohm_resistor_value(params, designator)
    assert found is not None
    assert found[0] == pytest.approx(ohms)


@pytest.mark.parametrize("params,designator", [
    ({"Value": "10m"}, "C4"),             # generic value on a non-R part
    ({"Value": "500mA"}, "F1"),           # fuse rating
    ({"Resistance": "500mA"}, "R1"),      # wrong unit even on a resistor
    ({"Value": "600R@100MHz"}, "R1"),     # extra text
    ({"Value": "10uF"}, "R1"),
    ({"Comment": "resistor_iec"}, "R1"),
    ({}, "R1"),
])
def test_non_resistor_values_are_refused(params, designator):
    assert _low_ohm_resistor_value(params, designator) is None


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def _stackup():
    return (RawStackupLayer(
        layer_id=1, name="Top", copper_thickness_mm=0.035,
        dielectric_thickness_mm=0.0, next_layer_id=0,
        is_plane=False, plane_net_name=None, mech_enabled=True,
    ),)


def _pad(comp_idx: int, pin: str, net_index: int, x: float) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0), width_mm=1, height_mm=1, hole_mm=0,
        shape=2, rotation_deg=0, layer_id=1, net_index=net_index,
        designator=pin, component_index=comp_idx,
        is_through_hole=False, is_smt=True,
    )


def _shunt_proj(value: str = "5mR", designator: str = "R18",
                extra_params: dict | None = None) -> ExtractedProject:
    """One two-pin part between 12P0V and +12V."""
    params = {"Resistance": value}
    params.update(extra_params or {})
    return ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(), stackup=_stackup(), compiled_netlist=None,
        nets=(RawNet("12P0V"), RawNet("+12V")),
        sch_components=(RawSchComponent(
            designator=designator, schdoc_name="Pwr.SchDoc",
            parameters=params, pin_designators=("1", "2"),
        ),),
        pcb_components=(RawPcbComponent(
            designator=designator, center=Pt2D(0, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="R2512",
            source_designator=designator,
        ),),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 5.0)),
    )


def _resistors(result):
    return [d for d in result.directives if isinstance(d, ResistorSpec)]


def test_default_limit_is_100_milliohm():
    assert DEFAULT_AUTO_SERIES_MAX_OHM == pytest.approx(0.1)


def test_shunt_is_modelled_at_its_own_value():
    result = parse_annotations(_shunt_proj(), enabled_layers=[1])
    specs = _resistors(result)
    assert len(specs) == 1
    assert specs[0].resistance == pytest.approx(0.005)
    assert specs[0].auto_reason
    assert not result.errors
    assert any("auto-modelled as SERIES" in w for w in result.warnings)
    # The synthesis note names the nets; the parser's own auto-infer warning
    # would only repeat it.
    assert not any(": auto-inferred " in w for w in result.warnings)


def test_value_above_the_limit_is_left_alone():
    assert _resistors(parse_annotations(
        _shunt_proj("0.15"), enabled_layers=[1])) == []


def test_value_at_the_limit_is_included():
    specs = _resistors(parse_annotations(
        _shunt_proj("100m"), enabled_layers=[1]))
    assert len(specs) == 1


def test_limit_is_configurable_and_zero_turns_it_off():
    assert _resistors(parse_annotations(
        _shunt_proj("0.15"), enabled_layers=[1], auto_series_max_ohm=0.2))
    assert _resistors(parse_annotations(
        _shunt_proj(), enabled_layers=[1], auto_series_max_ohm=0)) == []
    assert _resistors(parse_annotations(
        _shunt_proj(), enabled_layers=[1], auto_series_max_ohm=None)) == []


def test_zero_ohm_link_keeps_the_merge_bridge():
    """0 ohm values belong to the 0 ohm bridge, not to auto-SERIES."""
    specs = _resistors(parse_annotations(_shunt_proj("0R"), enabled_layers=[1]))
    assert len(specs) == 1
    assert specs[0].resistance == NET_TIE_BRIDGE_RESISTANCE_OHM
    assert specs[0].auto_reason is None


def test_explicit_annotation_wins():
    specs = _resistors(parse_annotations(_shunt_proj(extra_params={
        "PDN_ROLE": "SERIES", "PDN_R": "7m",
    }), enabled_layers=[1]))
    assert len(specs) == 1
    assert specs[0].resistance == pytest.approx(0.007)
    assert specs[0].auto_reason is None


def test_bridges_tab_opt_out_vetoes_it():
    result = parse_annotations(
        _shunt_proj(), enabled_layers=[1], no_auto_bridge={"r18"})
    assert _resistors(result) == []
    assert any("disabled by the user" in w for w in result.warnings)


def test_part_on_one_net_is_reported_not_modelled():
    proj = dataclasses.replace(_shunt_proj(), pads=(
        _pad(0, "1", 1, 0.0), _pad(0, "2", 1, 5.0)))
    result = parse_annotations(proj, enabled_layers=[1])
    assert _resistors(result) == []
    assert any("not modelled" in w for w in result.warnings)
    assert not result.errors


def test_regulator_finds_vin_across_an_auto_series_shunt():
    """The reason the feature exists: J2 sources 12 V on 12P0V, a 5 mohm
    shunt feeds +12V, and an SMPS on +12V needs Vin for its gain."""
    nets = (RawNet("12P0V"), RawNet("+12V"), RawNet("GND"), RawNet("+1V"))
    sch = (
        RawSchComponent(
            designator="R18", schdoc_name="Pwr.SchDoc",
            parameters={"Resistance": "5mR"}, pin_designators=("1", "2")),
        RawSchComponent(
            designator="J2", schdoc_name="Pwr.SchDoc",
            parameters={"PDN_ROLE": "SOURCE", "PDN_V": "12",
                        "PDN_P_NET": "12P0V", "PDN_N_NET": "GND"},
            pin_designators=("1", "2")),
        RawSchComponent(
            designator="U6", schdoc_name="Pwr.SchDoc",
            parameters={"PDN_ROLE": "REGULATOR", "PDN_V": "1",
                        "PDN_IN_P_NET": "+12V", "PDN_IN_N_NET": "GND",
                        "PDN_OUT_P_NET": "+1V", "PDN_OUT_N_NET": "GND",
                        "PDN_REGULATOR_TYPE": "SMPS",
                        "PDN_REGULATOR_EFFICIENCY": "0.85"},
            pin_designators=("1", "2", "3")),
    )
    pcb = tuple(
        RawPcbComponent(designator=d, center=Pt2D(0, 0), rotation_deg=0.0,
                        layer_name="TOP", footprint="X", source_designator=d)
        for d in ("R18", "J2", "U6")
    )
    pads = (
        _pad(0, "1", 0, 0.0), _pad(0, "2", 1, 5.0),          # R18
        _pad(1, "1", 0, 10.0), _pad(1, "2", 2, 12.0),        # J2
        _pad(2, "1", 1, 20.0), _pad(2, "2", 2, 22.0),        # U6 in
        _pad(2, "3", 3, 24.0),                               # U6 out
    )
    proj = ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(), stackup=_stackup(), compiled_netlist=None,
        nets=nets, sch_components=sch, pcb_components=pcb, pads=pads,
    )

    result = parse_annotations(proj, enabled_layers=[1])
    regs = [d for d in result.directives if isinstance(d, RegulatorSpec)]
    assert len(regs) == 1, result.errors
    assert regs[0].gain == pytest.approx(1 / (12 * 0.85))

    off = parse_annotations(proj, enabled_layers=[1], auto_series_max_ohm=0)
    assert not [d for d in off.directives if isinstance(d, RegulatorSpec)]
    assert any("cannot infer input voltage" in e for e in off.errors)


# ---------------------------------------------------------------------------
# Project file
# ---------------------------------------------------------------------------

def test_project_file_round_trips_the_limit(tmp_path):
    pf = ProjectFile(auto_series_max_ohm=0.25)
    path = tmp_path / "p.fypa"
    pf.save(path)
    assert ProjectFile.load(path).auto_series_max_ohm == pytest.approx(0.25)


def test_older_project_file_gets_the_default(tmp_path):
    path = tmp_path / "old.fypa"
    path.write_text('{"schema_version": 1}', encoding="utf-8")
    assert ProjectFile.load(path).auto_series_max_ohm == pytest.approx(0.1)
