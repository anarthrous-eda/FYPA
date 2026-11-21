"""Worst-case supply voltages: the range maths, the schematic parameters that
feed it, the report rules built on it, and the range's round trip through the
project file and Manual setup."""
from __future__ import annotations

from pathlib import Path

import pytest

from fypa import worst_case as W
from fypa.altium.annotations import parse_annotations, parse_voltage_tolerance
from fypa.altium.extract import (
    ExtractedProject,
    Pt2D,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawSchComponent,
    RawStackupLayer,
)
from fypa.autosetup import manual as M
from fypa.project_file import EditorDirective
from fypa.report import checks
from fypa.report.model import (
    FAIL,
    PASS,
    UNCHECKED,
    WARN,
    LoadResult,
    RailReport,
    ReportSettings,
)


def _term(net: str) -> dict:
    return {"pins": [{"net": net}]}


def _source(net, v, **kw):
    return {"role": "SOURCE", "value": v, "unit": "V",
            "terminals": {"P": _term(net), "N": _term("GND")}, **kw}


def _ldo(out_net, in_net, v, **kw):
    return {"role": "REGULATOR", "value": v, "unit": "V",
            "regulator_type": "LDO",
            "terminals": {"OUT_P": _term(out_net), "OUT_N": _term("GND"),
                          "IN_P": _term(in_net), "IN_N": _term("GND")}, **kw}


# --- the range ------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("5%", (3.135, 3.465)),
    ("±5%", (3.135, 3.465)),
    ("+/-50mV", (3.25, 3.35)),
    ("+3/-5%", (3.135, 3.399)),
    ("-100mV/+50mV", (3.2, 3.35)),
    ("-5%", (3.135, 3.3)),          # one-sided
])
def test_tolerance_forms(text, expected):
    assert parse_voltage_tolerance(text, 3.3) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["5", "abc", "+3/+5%", "", "1/2/3%"])
def test_tolerance_rejects_what_it_cannot_read(text):
    with pytest.raises(ValueError):
        parse_voltage_tolerance(text, 3.3)


def test_rail_range_takes_the_largest_shift_of_its_supplies():
    rails = {"+3V3": ["+3V3", "+3V3_FB"], "+5V": ["+5V"]}
    ranges = W.rail_ranges([
        _source("+3V3", 3.3, v_min=3.2, v_max=3.35),
        _source("+3V3_FB", 3.3, v_min=3.1),      # second supply, wider low side
        _source("+5V", 5.0),                     # no range given
    ], rails)
    assert ranges["+3V3"].low == pytest.approx(0.2)
    assert ranges["+3V3"].high == pytest.approx(0.05)
    assert ranges["+3V3"].toleranced
    assert (ranges["+5V"].low, ranges["+5V"].high) == (0.0, 0.0)
    assert not ranges["+5V"].toleranced


def test_lowest_shifts_down_and_highest_is_the_unloaded_rail():
    rng = W.RailRange(nominal=3.3, low=0.165, high=0.165, toleranced=True)
    # A load solved at 3.25 V (50 mV of drop).
    assert W.lowest_voltage(3.25, rng) == pytest.approx(3.085)
    # Highest: no drop at light load, so the setpoint, not the solved value.
    assert W.highest_voltage(3.25, rng) == pytest.approx(3.465)
    # Remote sense can lift a pin above the setpoint; that is kept.
    assert W.highest_voltage(3.34, rng) == pytest.approx(3.505)


def test_regulator_input_minimum_uses_the_output_maximum():
    assert W.regulator_input_minimum(_ldo("+3V3", "+5V", 3.3)) is None
    need = W.regulator_input_minimum(
        _ldo("+3V3", "+5V", 3.3, v_max=3.4, dropout=0.3))
    assert need[0] == pytest.approx(3.7)
    # The stricter of dropout and PDN_VIN_MIN wins.
    need = W.regulator_input_minimum(
        _ldo("+3V3", "+5V", 3.3, dropout=0.3, vin_min=4.5))
    assert need == (4.5, "PDN_VIN_MIN")
    # A boost's input sits below its output: dropout says nothing about it.
    boost = _ldo("+12V", "+5V", 12.0, dropout=0.3, smps_topology="BOOST")
    assert W.regulator_input_minimum(boost) is None


def test_tolerance_text_for_labels():
    assert W.tolerance_text(_source("A", 3.3, v_min=3.135, v_max=3.465)) == "±5%"
    assert W.tolerance_text(
        _source("A", 3.3, v_min=3.135, v_max=3.399)) == "+3/−5%"
    assert W.tolerance_text(_source("A", 3.3)) == ""


# --- schematic parameters ---------------------------------------------------------

def _proj(params: dict) -> ExtractedProject:
    """One part U1 with pads 1 = +5V, 2 = +3V3, 3 = GND carrying ``params``."""
    def pad(pin: str, net_index: int, x: float) -> RawPad:
        return RawPad(
            center=Pt2D(x, 0), width_mm=1, height_mm=1, hole_mm=0,
            shape=2, rotation_deg=0, layer_id=1, net_index=net_index,
            designator=pin, component_index=0,
            is_through_hole=False, is_smt=True)

    return ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(), compiled_netlist=None,
        stackup=(RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0, is_plane=False,
            plane_net_name=None, mech_enabled=True),),
        nets=(RawNet("GND"), RawNet("+5V"), RawNet("+3V3")),
        sch_components=(RawSchComponent(
            designator="U1", schdoc_name="Pwr.SchDoc", parameters=params,
            pin_designators=("1", "2", "3")),),
        pcb_components=(RawPcbComponent(
            designator="U1", center=Pt2D(0, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="QFN", source_designator="U1"),),
        pads=(pad("1", 1, 0), pad("2", 2, 1), pad("3", 0, 2)),
    )


_SOURCE_3V3 = {"PDN_ROLE": "SOURCE", "PDN_V": "3.3",
               "PDN_P_NET": "+3V3", "PDN_N_NET": "GND"}
_LDO_3V3 = {"PDN_ROLE": "REGULATOR", "PDN_V": "3.3",
            "PDN_REGULATOR_TYPE": "LDO",
            "PDN_OUT_P_NET": "+3V3", "PDN_OUT_N_NET": "GND",
            "PDN_IN_P_NET": "+5V", "PDN_IN_N_NET": "GND"}


@pytest.mark.parametrize("extra, v_min, v_max", [
    ({}, None, None),
    ({"PDN_V_TOL": "5%"}, 3.135, 3.465),
    ({"PDN_V_TOL": "+3/-5%"}, 3.135, 3.399),
    ({"PDN_V_TOL": "50mV"}, 3.25, 3.35),
    ({"PDN_V_MIN": "3.2", "PDN_V_MAX": "3.4"}, 3.2, 3.4),
    ({"PDN_V_MIN": "3V2"}, 3.2, None),
])
def test_source_output_range_from_tolerance_or_limits(extra, v_min, v_max):
    result = parse_annotations(_proj({**_SOURCE_3V3, **extra}),
                               enabled_layers=[1])
    assert result.ok, result.errors
    (src,) = result.directives
    assert src.voltage == 3.3          # the solve still runs at nominal
    assert src.v_min == (None if v_min is None else pytest.approx(v_min))
    assert src.v_max == (None if v_max is None else pytest.approx(v_max))


@pytest.mark.parametrize("extra, fragment", [
    ({"PDN_V_TOL": "5%", "PDN_V_MIN": "3.2"}, "not both"),
    ({"PDN_V_TOL": "5"}, "needs a unit"),
    ({"PDN_V_MIN": "3.4"}, "above the nominal"),
    ({"PDN_V_MAX": "3.2"}, "below the nominal"),
])
def test_unusable_output_range_is_an_error_not_a_silent_drop(extra, fragment):
    result = parse_annotations(_proj({**_SOURCE_3V3, **extra}),
                               enabled_layers=[1])
    assert not result.directives
    assert any(fragment in e for e in result.errors), result.errors


def test_output_range_modifier_does_not_clone_a_channel():
    # PDN1_V_TOL alone tunes nothing that exists: it must not invent channel 1.
    result = parse_annotations(
        _proj({**_SOURCE_3V3, "PDN1_V_TOL": "5%"}), enabled_layers=[1])
    assert [d.channel_index for d in result.directives] == [None]


def test_sink_max_v_is_parsed_and_must_not_sit_below_min_v():
    sink = {"PDN_ROLE": "SINK", "PDN_I": "1", "PDN_P_NET": "+3V3",
            "PDN_N_NET": "GND", "PDN_MIN_V": "3.0", "PDN_MAX_V": "3.6"}
    result = parse_annotations(_proj(sink), enabled_layers=[1])
    assert result.ok, result.errors
    assert (result.directives[0].min_voltage,
            result.directives[0].max_voltage) == (3.0, 3.6)
    result = parse_annotations(_proj({**sink, "PDN_MAX_V": "2.9"}),
                               enabled_layers=[1])
    assert any("PDN_MAX_V" in e for e in result.errors)


def test_regulator_range_and_input_limits_are_parsed():
    result = parse_annotations(
        _proj({**_LDO_3V3, "PDN_V_TOL": "2%", "PDN_DROPOUT": "300mV",
               "PDN_VIN_MIN": "4.5", "PDN_VIN_MAX": "6"}),
        enabled_layers=[1])
    assert result.ok, result.errors
    (reg,) = result.directives
    assert (reg.v_min, reg.v_max) == pytest.approx((3.234, 3.366))
    assert (reg.dropout, reg.vin_min, reg.vin_max) == (0.3, 4.5, 6.0)


def test_dropout_on_a_boost_converter_is_rejected():
    # An explicit gain: nothing here drives +5V for the SMPS to derive one.
    boost = {**_LDO_3V3, "PDN_REGULATOR_TYPE": "SMPS", "PDN_GAIN": "0.7",
             "PDN_SMPS_TOPOLOGY": "BOOST", "PDN_DROPOUT": "0.3"}
    result = parse_annotations(_proj(boost), enabled_layers=[1])
    assert any("PDN_DROPOUT" in e and "BOOST" in e for e in result.errors)


# --- report rules -----------------------------------------------------------------

def _load(v_load, *, min_v=None, max_v=None, low=0.0, high=0.0, nominal=1.0):
    """A load on a 1 V rail whose supply can sit ``low`` below / ``high``
    above nominal - filled in the way fypa.report.collect does."""
    ld = LoadResult(
        label="U1", kind="sink", net="VDD", pin_count=1, current_a=1.0,
        v_load=v_load, drop_v=nominal - v_load, return_drop_v=0.0,
        min_v=min_v, margin_v=None if min_v is None else v_load - min_v,
        status=PASS, worst_pin="U1-1", worst_layer="Top", max_v=max_v)
    if low:
        ld.v_low = v_load - low
        if min_v is not None:
            ld.margin_low_v = ld.v_low - min_v
    if max_v is not None or high:
        ld.v_high = max(v_load, nominal) + high
        if max_v is not None:
            ld.margin_high_v = max_v - ld.v_high
    return ld


def _rail(loads, low=0.0, high=0.0):
    rail = RailReport(name="VDD", kind="supply", members=["VDD"],
                      nominal_v=1.0, source_label="U9 (source)", load_a=1.0,
                      loads=loads)
    if low or high:
        rail.source_min_v, rail.source_max_v = 1.0 - low, 1.0 + high
    return rail


def test_passing_at_nominal_but_not_at_the_supply_minimum_fails():
    rail = _rail([_load(0.97, min_v=0.95, low=0.05)], low=0.05)
    checks.rail_findings(rail, ReportSettings())
    assert rail.status == FAIL
    (f,) = rail.findings
    assert f.code == "below-min-v-worst"
    assert "0.920 V" in f.title and "0.95 V minimum" in f.title
    assert "Passes at the 1 V nominal" in f.detail


def test_failing_at_nominal_reports_the_nominal_failure_once():
    rail = _rail([_load(0.94, min_v=0.95, low=0.05)], low=0.05)
    checks.rail_findings(rail, ReportSettings())
    assert [f.code for f in rail.findings] == ["below-min-v"]


def test_low_margin_warning_uses_the_worst_case_margin():
    # 30 mV at nominal is comfortable; 5 mV at the supply minimum is not.
    rail = _rail([_load(0.98, min_v=0.95, low=0.025)], low=0.025)
    checks.rail_findings(rail, ReportSettings(margin_warn_pct=1.0))
    assert rail.status == WARN
    assert rail.loads[0].limiting_margin_v == pytest.approx(0.005)


def test_no_range_behaves_as_before():
    rail = _rail([_load(0.97, min_v=0.95)])
    checks.rail_findings(rail, ReportSettings())
    assert rail.status == PASS and rail.findings == []


def test_max_v_is_checked_against_the_unloaded_rail_at_supply_maximum():
    # Solved at 0.97 V, well under the 1.03 V limit - but with no load the
    # rail sits at its setpoint, and the supply can be 5 % high.
    rail = _rail([_load(0.97, min_v=0.9, max_v=1.03, high=0.05)], high=0.05)
    checks.rail_findings(rail, ReportSettings())
    assert rail.status == FAIL
    (f,) = rail.findings
    assert f.code == "above-max-v" and "can reach 1.050 V" in f.title


def test_max_v_without_a_supply_range_is_checked_at_the_setpoint():
    rail = _rail([_load(0.97, min_v=0.9, max_v=0.99)])
    checks.rail_findings(rail, ReportSettings())
    assert [f.code for f in rail.findings] == ["above-max-v"]
    rail = _rail([_load(0.97, min_v=0.9, max_v=1.0)])
    checks.rail_findings(rail, ReportSettings())
    assert rail.status == PASS and rail.findings == []


def test_regulator_input_below_its_dropout_fails_and_says_why():
    ld = _load(0.97, min_v=0.99)
    ld.kind, ld.min_name = "regulator input", "input minimum"
    ld.min_basis = "0.9 V output + 90 mV dropout"
    ld.regulator_type = "LDO"
    rail = _rail([ld])
    checks.rail_findings(rail, ReportSettings())
    (f,) = rail.findings
    assert f.severity == FAIL and "below input minimum" in f.title
    assert "0.9 V output + 90 mV dropout" in f.detail


def test_ldo_without_a_dropout_is_unchecked_but_an_smps_stays_quiet():
    ldo = _load(0.97)
    ldo.kind, ldo.regulator_type = "regulator input", "LDO"
    smps = _load(0.97)
    smps.kind, smps.regulator_type, smps.label = "regulator input", "SMPS", "U2"
    rail = _rail([ldo, smps])
    checks.rail_findings(rail, ReportSettings())
    assert [(f.severity, f.code) for f in rail.findings] == [
        (UNCHECKED, "no-dropout")]
    assert "1 LDO " in rail.findings[0].title


# --- project file and Manual setup ---------------------------------------------------

def test_editor_directive_round_trips_the_new_fields():
    d = EditorDirective(role="REGULATOR", voltage=3.3, v_tol="+3/-5%",
                        dropout=0.3, vin_min=4.5, vin_max=6.0)
    back = EditorDirective.from_dict(d.to_dict())
    assert (back.v_tol, back.dropout, back.vin_min, back.vin_max) == (
        "+3/-5%", 0.3, 4.5, 6.0)
    sink = EditorDirective.from_dict(
        EditorDirective(role="SINK", current=1.0, max_voltage=3.6).to_dict())
    assert sink.max_voltage == 3.6
    # A project saved before these fields existed still loads.
    old = EditorDirective.from_dict({"role": "SOURCE", "voltage": 5.0})
    assert (old.v_tol, old.v_min, old.v_max, old.max_voltage) == (
        None, None, None, None)


def _parse(text):
    try:
        return float(text.replace("V", "").strip())
    except ValueError:
        return None


@pytest.mark.parametrize("text, tol, lo, hi", [
    ("5%", "5%", None, None),
    ("+3/-5%", "+3/-5%", None, None),
    ("3.1 to 3.5 V", None, 3.1, 3.5),
    ("3.5 .. 3.1", None, 3.1, 3.5),      # either order
    ("", None, None, None),
])
def test_manual_setup_reads_one_line_as_tolerance_or_range(text, tol, lo, hi):
    d = M.new_supply()
    d.voltage = 3.3
    M.set_output_range(d, text, _parse)
    assert (d.v_tol, d.v_min, d.v_max) == (tol, lo, hi)
    # What is shown back parses to the same thing.
    again = M.new_supply()
    again.voltage = 3.3
    M.set_output_range(again, M.output_range_text(d), _parse)
    assert (again.v_tol, again.v_min, again.v_max) == (tol, lo, hi)
