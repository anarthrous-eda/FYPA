"""Remote sense on SOURCE / REGULATOR outputs (PDN_SENSE_DES and friends).

A remote-sensed supply holds its setpoint across the sense pads - usually at
the load - and raises its own output pins by whatever the copper drops on
the way. These tests cover the directive parsing; the solve itself is
checked against closed form in ``test_validation.py``.
"""
from __future__ import annotations

from pathlib import Path

from fypa.altium.annotations import RegulatorSpec, SourceSpec, parse_annotations
from fypa.altium.extract import (
    ExtractedProject,
    Pt2D,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawSchComponent,
    RawStackupLayer,
)

NETS = (RawNet("GND"), RawNet("+5V"), RawNet("+3V3"))
GND, P5V, P3V3 = 0, 1, 2


def _pad(comp_idx: int, pin: str, net_index: int, x: float) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0), width_mm=1, height_mm=1, hole_mm=0,
        shape=2, rotation_deg=0, layer_id=1, net_index=net_index,
        designator=pin, component_index=comp_idx,
        is_through_hole=False, is_smt=True,
    )


def _proj(parts: list[tuple[str, dict, list[tuple[str, int]]]]) -> ExtractedProject:
    """One schematic + PCB component per ``(designator, params, pads)``,
    pads given as ``(pin, net)`` and laid out along x."""
    sch, pcb, pads = [], [], []
    for i, (des, params, pad_list) in enumerate(parts):
        sch.append(RawSchComponent(
            designator=des, schdoc_name="Pwr.SchDoc",
            parameters=params or {"Comment": "X"},
            pin_designators=tuple(p for p, _n in pad_list)))
        pcb.append(RawPcbComponent(
            designator=des, center=Pt2D(10.0 * i, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="X", source_designator=des))
        pads.extend(_pad(i, pin, net, 10.0 * i + j)
                    for j, (pin, net) in enumerate(pad_list))
    return ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), pads=tuple(pads), regions=(),
        shape_based_regions=(), fills=(),
        pcb_components=tuple(pcb), nets=NETS,
        stackup=(RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0, is_plane=False,
            plane_net_name=None, mech_enabled=True),),
        sch_components=tuple(sch), compiled_netlist=None,
    )


LOAD = ("U1", {"PDN_ROLE": "SINK", "PDN_I": "2A", "PDN_P_NET": "+5V",
               "PDN_N_NET": "GND"},
        [("1", P5V), ("2", GND), ("3", P5V), ("4", GND)])


def _source(**extra) -> tuple[str, dict, list]:
    params = {"PDN_ROLE": "SOURCE", "PDN_V": "5", "PDN_P_NET": "+5V",
              "PDN_N_NET": "GND", **extra}
    return ("J1", params, [("1", P5V), ("2", GND)])


def test_source_senses_at_the_load_pads_on_its_own_net():
    proj = _proj([_source(PDN_SENSE_DES="U1"), LOAD])
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    src = next(d for d in result.directives if isinstance(d, SourceSpec))
    # + sense: U1's +5V pads only (not its GND pads); − sense stays local.
    assert {(p.component_designator, p.pad_designator)
            for p in src.sense_p.pins} == {("U1", "1"), ("U1", "3")}
    assert src.sense_n is None


def test_sense_pins_narrow_the_pads_and_both_sides_can_sense():
    proj = _proj([_source(PDN_SENSE_DES="U1", PDN_SENSE_PINS="3",
                          PDN_SENSE_N_DES="U1", PDN_SENSE_N_PINS="4"), LOAD])
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    src = next(d for d in result.directives if isinstance(d, SourceSpec))
    assert [p.pad_designator for p in src.sense_p.pins] == ["3"]
    assert [p.pad_designator for p in src.sense_n.pins] == ["4"]


def test_no_sense_parameters_means_local_sense():
    result = parse_annotations(_proj([_source(), LOAD]), enabled_layers=[1])
    src = next(d for d in result.directives if isinstance(d, SourceSpec))
    assert src.sense_p is None and src.sense_n is None


def test_sense_pins_without_a_designator_is_an_error():
    result = parse_annotations(_proj([_source(PDN_SENSE_PINS="3"), LOAD]),
                               enabled_layers=[1])
    assert any("PDN_SENSE_PINS needs PDN_SENSE_DES" in e for e in result.errors)
    assert not any(isinstance(d, SourceSpec) for d in result.directives)


def test_sense_on_a_part_with_no_pad_on_the_net_is_an_error():
    other = ("U2", {}, [("1", P3V3), ("2", GND)])
    result = parse_annotations(
        _proj([_source(PDN_SENSE_DES="U2"), LOAD, other]), enabled_layers=[1])
    assert result.errors
    assert not any(isinstance(d, SourceSpec) for d in result.directives)


def test_regulator_output_senses_remotely():
    reg = ("U5", {
        "PDN_ROLE": "REGULATOR", "PDN_V": "3.3", "PDN_REGULATOR_TYPE": "LDO",
        "PDN_OUT_P_NET": "+3V3", "PDN_OUT_N_NET": "GND",
        "PDN_IN_P_NET": "+5V", "PDN_IN_N_NET": "GND",
        "PDN_SENSE_DES": "U6",
    }, [("1", P5V), ("2", GND), ("3", P3V3)])
    load = ("U6", {"PDN_ROLE": "SINK", "PDN_I": "1A", "PDN_P_NET": "+3V3",
                   "PDN_N_NET": "GND"}, [("1", P3V3), ("2", GND)])
    result = parse_annotations(
        _proj([_source(), reg, load]), enabled_layers=[1])
    assert result.ok, result.errors
    r = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert [(p.component_designator, p.pad_designator)
            for p in r.out_sense_p.pins] == [("U6", "1")]
    assert r.out_sense_n is None
