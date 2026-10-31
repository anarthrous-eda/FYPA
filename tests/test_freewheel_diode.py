# Copyright (c) 2026 Tomachie LLC.
"""Non-synchronous buck: the freewheel diode (PDN_ROLE=FREEWHEEL).

An internal-switch buck whose low side is a diode, not a FET: the switch pin
conducts D = Vout / Vin of the period and the diode the rest, from its anode
(ground) into the switch node. Averaged, the switch pins deliver D x I_out and
the diode (1 - D) x I_out, while the input still draws gain x I_out.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fypa.altium.annotations import (
    FreewheelSpec,
    RegulatorSpec,
    parse_annotations,
    regulator_input_gain,
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

# Nets: 0=GND 1=VIN 2=SW 3=VOUT
_GND, _VIN, _SW, _VOUT = 0, 1, 2, 3


def _pad(comp_idx: int, pin: str, net_index: int, x: float = 0.0) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0), width_mm=1, height_mm=1, hole_mm=0, shape=2,
        rotation_deg=0, layer_id=1, net_index=net_index, designator=pin,
        component_index=comp_idx, is_through_hole=False, is_smt=True,
    )


def _pcb(des: str, x: float = 0.0) -> RawPcbComponent:
    return RawPcbComponent(
        designator=des, center=Pt2D(x, 0), rotation_deg=0.0, layer_name="TOP",
        footprint="X", source_designator=des,
    )


def _sch(des: str, params: dict, pins: tuple) -> RawSchComponent:
    return RawSchComponent(designator=des, schdoc_name="Pwr.SchDoc",
                           parameters=params, pin_designators=pins)


def _buck_proj(diode_params: dict | None):
    """J1 48 V -> U1 buck (VIN pin 2, SW pin 8, GND pin 1) -> L1 -> 5 V,
    D1 anode pads 1,2 on GND and cathode pad 3 on SW."""
    sch = [
        _sch("J1", {"PDN_ROLE": "SOURCE", "PDN_V": "48", "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND"}, ("1", "2")),
        _sch("U1", {"PDN_ROLE": "REGULATOR", "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.9", "PDN_V": "5",
                    "PDN_OUT_P_NET": "SW", "PDN_OUT_N_NET": "GND",
                    "PDN_IN_P_NET": "VIN", "PDN_IN_N_NET": "GND"},
             ("1", "2", "8")),
        _sch("L1", {"PDN_ROLE": "SERIES", "PDN_R": "1m"}, ("1", "2")),
        _sch("U5", {"PDN_ROLE": "SINK", "PDN_I": "2", "PDN_P_NET": "VOUT",
                    "PDN_N_NET": "GND"}, ("1", "2")),
        _sch("D1", diode_params or {}, ("1", "2", "3")),
    ]
    pcb = tuple(_pcb(d, i) for i, d in enumerate(["J1", "U1", "L1", "U5", "D1"]))
    pads = (
        _pad(0, "1", _VIN), _pad(0, "2", _GND),
        _pad(1, "1", _GND), _pad(1, "2", _VIN), _pad(1, "8", _SW),
        _pad(2, "1", _SW), _pad(2, "2", _VOUT),
        _pad(3, "1", _VOUT), _pad(3, "2", _GND),
        _pad(4, "1", _GND), _pad(4, "2", _GND), _pad(4, "3", _SW),
    )
    return ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), pads=pads, regions=(),
        shape_based_regions=(), fills=(), pcb_components=pcb,
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("SW"), RawNet("VOUT")),
        stackup=(RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0, is_plane=False,
            plane_net_name=None, mech_enabled=True),),
        sch_components=tuple(sch), compiled_netlist=None,
    )


D = 5.0 / 48.0
DIODE = {"PDN_ROLE": "FREEWHEEL", "PDN_DUTY": f"{D:.6g}", "PDN_SMPS_HOST": "U1",
         "PDN_P_NET": "GND", "PDN_N_NET": "SW", "PDN_P_PINS": "1,2",
         "PDN_N_PINS": "3"}


def _regulator(diode_params):
    result = parse_annotations(_buck_proj(diode_params), enabled_layers=[1])
    assert not any(isinstance(d, FreewheelSpec) for d in result.directives)
    return result, next(d for d in result.directives if isinstance(d, RegulatorSpec))


def test_freewheel_diode_becomes_a_leg_on_its_regulator():
    result, reg = _regulator(DIODE)
    assert result.ok, result.errors
    assert reg.freewheel_duty == pytest.approx(D, rel=1e-5)
    (leg,) = reg.switch_path_legs
    assert leg.kind == "freewheel" and leg.designator == "D1"
    # Drawn from the anode pads (ground), delivered at the cathode (switch node).
    assert {p.pad_designator for p in leg.p.pins} == {"1", "2"}
    assert {p.net_index for p in leg.p.pins} == {_GND}
    assert [(p.pad_designator, p.net_index) for p in leg.n.pins] == [("3", _SW)]
    assert leg.coeff == pytest.approx((1 - D) / D, rel=1e-5)
    # The element keeps its switch-pin terminal and draws gain x I_out at VIN.
    assert [p.pad_designator for p in reg.out_p.pins] == ["8"]
    assert regulator_input_gain(reg) == pytest.approx(reg.gain / D, rel=1e-5)


def test_currents_split_d_at_the_switch_pin_and_one_minus_d_at_the_diode():
    """Stamp the element and read the currents per ampere of output: the
    switch pin delivers D, the diode 1 - D, the input draws gain."""
    from pdnsolver import problem as pp
    from pdnsolver.solver import stamp_network_into_system

    _result, spec = _regulator(DIODE)
    v_p, v_n, s_f, s_t, a, k = (pp.NodeID() for _ in range(6))
    leg = spec.switch_path_legs[0]
    reg = pp.VoltageRegulator(
        v_p=v_p, v_n=v_n, s_f=s_f, s_t=s_t, voltage=5.0, gain=spec.gain,
        input_gain=regulator_input_gain(spec),
        switch_legs=((a, k, leg.coeff),),
    )
    nodes = [v_p, v_n, s_f, s_t, a, k]
    idx = {n: i for i, n in enumerate(nodes)}
    i_v = len(nodes)

    class _Indexer:
        node_to_global_index = idx
        extra_source_to_global_index = {reg: i_v}

    rows: list = []
    cols: list = []
    vals: list = []
    stamp_network_into_system(pp.Network(connections=[], elements=[reg]),
                              _Indexer(), rows, cols, vals, np.zeros(i_v + 1))
    col: dict[int, float] = {}
    for rr, cc, vv in zip(rows, cols, vals):
        if cc == i_v:
            col[rr] = col.get(rr, 0.0) + vv

    i_out = 1.0
    sw_pin = D * i_out                       # the element's own current
    into_switch_node = col[idx[v_p]] * sw_pin + col[idx[k]] * sw_pin
    assert into_switch_node == pytest.approx(i_out, rel=1e-5)
    assert col[idx[k]] * sw_pin == pytest.approx(1 - D, rel=1e-5)        # diode
    assert -col[idx[a]] * sw_pin == pytest.approx(1 - D, rel=1e-5)       # from ground
    assert -col[idx[s_f]] * sw_pin == pytest.approx(spec.gain * i_out, rel=1e-5)
    assert sum(col.values()) == pytest.approx(0.0, abs=1e-12)


def test_without_a_diode_the_regulator_is_unchanged():
    result, reg = _regulator(None)
    assert result.ok, result.errors
    assert reg.freewheel_duty is None and reg.switch_path_legs == ()
    assert regulator_input_gain(reg) is None


def test_diode_off_the_switch_node_is_refused_and_dropped():
    off = dict(DIODE, PDN_N_NET="VOUT", PDN_N_PINS="")
    off.pop("PDN_N_PINS")
    proj = _buck_proj(off)
    # Move D1's cathode pad onto VOUT, so it is not on U1's switch node.
    pads = tuple(p if not (p.component_index == 4 and p.designator == "3")
                 else _pad(4, "3", _VOUT) for p in proj.pads)
    from dataclasses import replace
    result = parse_annotations(replace(proj, pads=pads), enabled_layers=[1])
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.freewheel_duty is None and reg.switch_path_legs == ()
    assert any("FREEWHEEL on D1" in e and "switch node" in e for e in result.errors)


def test_duty_out_of_range_is_an_error():
    result = parse_annotations(_buck_proj(dict(DIODE, PDN_DUTY="1.2")),
                               enabled_layers=[1])
    assert any("PDN_DUTY must be between 0 and 1" in e for e in result.errors)
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.freewheel_duty is None
