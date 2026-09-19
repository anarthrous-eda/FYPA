"""External-FET SMPS (PATH + BUCKBOOST / INVERTER) annotation tests."""

from __future__ import annotations

import pytest
from pathlib import Path

from fypa.altium.annotations import (
    PathSpec,
    RegulatorSpec,
    ResistorSpec,
    SwitchPathLeg,
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


def _minimal_stackup() -> tuple[RawStackupLayer, ...]:
    return (
        RawStackupLayer(
            layer_id=1,
            name="Top",
            copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0,
            next_layer_id=0,
            is_plane=False,
            plane_net_name=None,
            mech_enabled=True,
        ),
    )


def _minimal_proj(**overrides) -> ExtractedProject:
    base: dict = {
        "prjpcb_path": Path("t.PrjPcb"),
        "pcbdoc_path": Path("t.PcbDoc"),
        "tracks": (),
        "arcs": (),
        "vias": (),
        "pads": (),
        "regions": (),
        "shape_based_regions": (),
        "fills": (),
        "pcb_components": (),
        "nets": (),
        "stackup": _minimal_stackup(),
        "sch_components": (),
        "compiled_netlist": None,
    }
    base.update(overrides)
    return ExtractedProject(**base)  # type: ignore[arg-type]


def _pad(comp_idx: int, pin: str, net_index: int, x: float = 0.0) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0),
        width_mm=1,
        height_mm=1,
        hole_mm=0,
        shape=2,
        rotation_deg=0,
        layer_id=1,
        net_index=net_index,
        designator=pin,
        component_index=comp_idx,
        is_through_hole=False,
        is_smt=True,
    )


def _pcb(des: str, x: float = 0.0) -> RawPcbComponent:
    return RawPcbComponent(
        designator=des,
        center=Pt2D(x, 0),
        rotation_deg=0.0,
        layer_name="TOP",
        footprint="X",
        source_designator=des,
    )


# Net indices for the BUCKBOOST fixture:
# 0=GND 1=VIN 2=SW1 3=MID (between shunt and L) 4=SW2 5=VOUT
_GND, _VIN, _SW1, _MID, _SW2, _VOUT = 0, 1, 2, 3, 4, 5


def _buckboost_proj(*, include_ls: bool = True):
    if include_ls:
        return _buckboost_proj_with_ls()
    # Same as with_ls but without Q_LS_OUT.
    base = _buckboost_proj_with_ls()
    sch = tuple(c for c in base.sch_components if c.designator != "Q_LS_OUT")
    # Remap pcb/pads: drop index 6 (Q_LS_OUT), shift LOAD from 7→6.
    pcb = (
        base.pcb_components[0],
        base.pcb_components[1],
        base.pcb_components[2],
        base.pcb_components[3],
        base.pcb_components[4],
        base.pcb_components[5],
        _pcb("LOAD", 6),
    )
    pads = tuple(p for p in base.pads if p.component_index not in (6, 7)) + (
        _pad(6, "1", _VOUT),
        _pad(6, "2", _GND),
    )
    return _minimal_proj(
        nets=base.nets,
        sch_components=sch,
        pcb_components=pcb,
        pads=pads,
    )


def _buckboost_proj_with_ls():
    sch = (
        RawSchComponent(
            designator="J1",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SOURCE",
                "PDN_V": "12",
                "PDN_P_NET": "VIN",
                "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="U2",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "REGULATOR",
                "PDN_REGULATOR_TYPE": "SMPS",
                "PDN_REGULATOR_EFFICIENCY": "0.9",
                "PDN_V": "24",
                "PDN_SMPS_TOPOLOGY": "BUCKBOOST",
                "PDN_IN_P_NET": "VIN",
                "PDN_IN_N_NET": "GND",
                "PDN_OUT_P_NET": "VOUT",
                "PDN_OUT_N_NET": "GND",
                "PDN_SW1_NET": "SW1",
                "PDN_SW2_NET": "SW2",
                "PDN_IN_P_PINS": "1",
                "PDN_IN_N_PINS": "2",
                "PDN_OUT_P_PINS": "3",
                "PDN_OUT_N_PINS": "4",
            },
            pin_designators=("1", "2", "3", "4"),
        ),
        RawSchComponent(
            designator="Q_HS_IN",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH",
                "PDN_R": "10m",
                "PDN_P_PINS": "3",
                "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="R_SHUNT",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH",
                "PDN_R": "1m",
                "PDN_P_PINS": "1",
                "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="L1",
            schdoc_name="Pwr.SchDoc",
            parameters={"PDN_ROLE": "PATH", "PDN_R": "7m"},
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="Q_HS_OUT",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH",
                "PDN_R": "12m",
                "PDN_P_PINS": "3",
                "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="Q_LS_OUT",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH",
                "PDN_R": "8m",
                "PDN_P_PINS": "3",
                "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="LOAD",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SINK",
                "PDN_I": "2A",
                "PDN_P_NET": "VOUT",
                "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
    )
    pcb = tuple(
        _pcb(d, i)
        for i, d in enumerate(
            ["J1", "U2", "Q_HS_IN", "R_SHUNT", "L1", "Q_HS_OUT", "Q_LS_OUT", "LOAD"]
        )
    )
    pads = (
        _pad(0, "1", _VIN),
        _pad(0, "2", _GND),
        _pad(1, "1", _VIN),
        _pad(1, "2", _GND),
        _pad(1, "3", _VOUT),
        _pad(1, "4", _GND),
        _pad(2, "1", _GND),
        _pad(2, "2", _SW1),
        _pad(2, "3", _VIN),
        _pad(3, "1", _SW1),
        _pad(3, "2", _MID),
        _pad(4, "1", _MID),
        _pad(4, "2", _SW2),
        _pad(5, "1", _GND),
        _pad(5, "2", _SW2),
        _pad(5, "3", _VOUT),
        _pad(6, "1", _GND),
        _pad(6, "2", _GND),
        _pad(6, "3", _SW2),
        _pad(7, "1", _VOUT),
        _pad(7, "2", _GND),
    )
    return _minimal_proj(
        nets=(
            RawNet("GND"),
            RawNet("VIN"),
            RawNet("SW1"),
            RawNet("MID"),
            RawNet("SW2"),
            RawNet("VOUT"),
        ),
        sch_components=sch,
        pcb_components=pcb,
        pads=pads,
    )


def test_buckboost_path_binds_and_cuts_at_output_fet():
    """The cut is the part carrying i_v: the output-side HS FET, not L1.

    The inductor of a boosting stage carries gain*i_v, so cutting there would
    make the element draw the wrong current through its own DCR.
    """
    result = parse_annotations(_buckboost_proj_with_ls(), enabled_layers=[1])
    assert result.ok, result.errors
    assert not any(isinstance(d, PathSpec) for d in result.directives)

    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.smps_topology == "BUCKBOOST"
    assert abs(reg.gain - (24.0 / (12.0 * 0.9))) < 1e-6
    assert reg.cut_resistance == pytest.approx(0.012)  # Q_HS_OUT RDSon
    # IN/OUT relocated to the cut part's pads (SW2 / VOUT).
    in_nets = {p.net_index for p in reg.in_p.pins}
    out_nets = {p.net_index for p in reg.out_p.pins}
    assert in_nets == {_SW2}
    assert out_nets == {_VOUT}

    bridges = [d for d in result.directives if isinstance(d, ResistorSpec)]
    bridge_des = {d.designator for d in bridges}
    assert "Q_HS_IN" in bridge_des
    assert "R_SHUNT" in bridge_des
    assert "L1" in bridge_des  # inductor is a plain conductor here
    assert "Q_HS_OUT" not in bridge_des  # it is the cut

    assert len(reg.switch_path_legs) == 1
    leg = reg.switch_path_legs[0]
    assert leg.kind == "ls_out"
    assert leg.designator == "Q_LS_OUT"
    assert leg.coeff == pytest.approx(reg.gain - 1.0)


def test_buckboost_auto_host_without_smps_host():
    """PATH parts bind via shared nets — no PDN_SMPS_HOST required."""
    result = parse_annotations(_buckboost_proj(include_ls=False), enabled_layers=[1])
    assert result.ok, result.errors
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.cut_resistance is not None
    assert not reg.switch_path_legs


def test_internal_fet_regulator_unchanged_without_path():
    """Regression: REGULATOR without PATH stays on controller pads."""
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("VIN"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="J1",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "5",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.9",
                    "PDN_V": "3.3",
                    "PDN_IN_P_NET": "VIN",
                    "PDN_IN_N_NET": "GND",
                    "PDN_OUT_P_NET": "VOUT",
                    "PDN_OUT_N_NET": "GND",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
        ),
        pcb_components=(_pcb("J1"), _pcb("U2", 1)),
        pads=(
            _pad(0, "1", 1),
            _pad(0, "2", 0),
            _pad(1, "1", 2),
            _pad(1, "2", 0),
            _pad(1, "3", 1),
            _pad(1, "4", 0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.smps_topology is None
    assert reg.switch_path_legs == ()
    assert {p.net_index for p in reg.in_p.pins} == {1}
    assert {p.net_index for p in reg.out_p.pins} == {2}


def test_inverter_topology_gain_without_upstream_vin():
    """INVERTER: gain = 1/eff, no Vin inference, phases stay separate rails."""
    proj = _minimal_proj(
        nets=(
            RawNet("GND"),
            RawNet("VBUS"),
            RawNet("PHASE_A"),
            RawNet("PHASE_B"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J1",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "24",
                    "PDN_P_NET": "VBUS",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U5",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "0.85",
                    "PDN_SMPS_TOPOLOGY": "INVERTER",
                    "PDN_V": "24",
                    "PDN_IN_P_NET": "VBUS",
                    "PDN_IN_N_NET": "GND",
                    "PDN1_OUT_P_NET": "PHASE_A",
                    "PDN1_OUT_N_NET": "GND",
                    "PDN1_V": "24",
                    "PDN2_OUT_P_NET": "PHASE_B",
                    "PDN2_OUT_N_NET": "GND",
                    "PDN2_V": "24",
                },
                pin_designators=("1", "2", "3", "4", "5"),
            ),
            RawSchComponent(
                designator="J9",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SINK",
                    "PDN1_I": "1A",
                    "PDN1_P_NET": "PHASE_A",
                    "PDN1_N_NET": "GND",
                    "PDN2_I": "1A",
                    "PDN2_P_NET": "PHASE_B",
                    "PDN2_N_NET": "GND",
                },
                pin_designators=("1", "2", "4"),
            ),
        ),
        pcb_components=(_pcb("J1"), _pcb("U5", 1), _pcb("J9", 2)),
        pads=(
            _pad(0, "1", 1),
            _pad(0, "2", 0),
            _pad(1, "1", 1),
            _pad(1, "2", 0),
            _pad(1, "3", 2),
            _pad(1, "4", 3),
            _pad(1, "5", 0),
            _pad(2, "1", 2),
            _pad(2, "2", 3),
            _pad(2, "4", 0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    regs = [d for d in result.directives if isinstance(d, RegulatorSpec)]
    assert len(regs) >= 1
    for reg in regs:
        assert reg.smps_topology == "INVERTER"
        assert reg.gain == pytest.approx(1.0 / 0.85)
        assert not reg.adaptive_gain_eligible
    # No SERIES bridge from bus to phases → VIN and phase nets stay distinct
    # in directive terminals (REGULATOR does not union rails).
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)


def test_path_ambiguous_host_requires_smps_host():
    """Two BUCKBOOST stages sharing a net name force PDN_SMPS_HOST."""
    # Minimal: two regulators both claiming SW1=SWA — PATH on SWA is ambiguous.
    proj = _minimal_proj(
        nets=(
            RawNet("GND"),
            RawNet("VIN"),
            RawNet("SWA"),
            RawNet("SWB1"),
            RawNet("SWB2"),
            RawNet("VOUT1"),
            RawNet("VOUT2"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J1",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "12",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U1",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "24",
                    "PDN_GAIN": "2",
                    "PDN_SMPS_TOPOLOGY": "BUCKBOOST",
                    "PDN_IN_P_NET": "VIN",
                    "PDN_IN_N_NET": "GND",
                    "PDN_OUT_P_NET": "VOUT1",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_SW1_NET": "SWA",
                    "PDN_SW2_NET": "SWB1",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="U2",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_V": "24",
                    "PDN_GAIN": "2",
                    "PDN_SMPS_TOPOLOGY": "BUCKBOOST",
                    "PDN_IN_P_NET": "VIN",
                    "PDN_IN_N_NET": "GND",
                    "PDN_OUT_P_NET": "VOUT2",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_SW1_NET": "SWA",
                    "PDN_SW2_NET": "SWB2",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="L1",
                schdoc_name="Pwr.SchDoc",
                parameters={"PDN_ROLE": "PATH", "PDN_R": "5m"},
                pin_designators=("1", "2"),
            ),
        ),
        pcb_components=(_pcb("J1"), _pcb("U1", 1), _pcb("U2", 2), _pcb("L1", 3)),
        pads=(
            _pad(0, "1", 1),
            _pad(0, "2", 0),
            _pad(1, "1", 1),
            _pad(1, "2", 0),
            _pad(1, "3", 5),
            _pad(1, "4", 0),
            _pad(2, "1", 1),
            _pad(2, "2", 0),
            _pad(2, "3", 6),
            _pad(2, "4", 0),
            _pad(3, "1", 2),
            _pad(3, "2", 3),  # SWA ↔ SWB1 — still touches SWA
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert any("ambiguous host" in e for e in result.errors)


def test_shunt_kelvin_pads_without_filter_warn_or_fail_cleanly():
    """4-pin shunt: Kelvin on GND ignored when P/N nets name the power path."""
    base = _buckboost_proj(include_ls=False)
    sch = list(base.sch_components)
    sch[3] = RawSchComponent(
        designator="R_SHUNT",
        schdoc_name="Pwr.SchDoc",
        parameters={
            "PDN_ROLE": "PATH",
            "PDN_R": "1m",
            "PDN_P_NET": "SW1",
            "PDN_N_NET": "MID",
        },
        pin_designators=("1", "2", "1a", "2a"),
    )
    pads = list(base.pads) + [
        _pad(3, "1a", _GND, 3.2),
        _pad(3, "2a", _GND, 3.3),
    ]
    proj = _minimal_proj(
        nets=base.nets,
        sch_components=tuple(sch),
        pcb_components=base.pcb_components,
        pads=tuple(pads),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    shunt = next(
        d for d in result.directives if isinstance(d, ResistorSpec) and d.designator == "R_SHUNT"
    )
    nets = {p.net_index for t in (shunt.p, shunt.n) for p in t.pins}
    assert nets == {_SW1, _MID}


def test_loader_stamps_switch_legs():
    """LS coeff on SwitchPathLeg matches boost (G-1)."""
    result = parse_annotations(_buckboost_proj_with_ls(), enabled_layers=[1])
    assert result.ok, result.errors
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.switch_path_legs
    assert reg.switch_path_legs[0].coeff == pytest.approx(reg.gain - 1.0)
    assert isinstance(reg.switch_path_legs[0], SwitchPathLeg)


def test_buck_topology_ls_in_coeff():
    """BUCK with G<1 → ls_in coeff = 1-G."""
    proj = _minimal_proj(
        nets=(
            RawNet("GND"),
            RawNet("VIN"),
            RawNet("SW1"),
            RawNet("VOUT"),
        ),
        sch_components=(
            RawSchComponent(
                designator="J1",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "SOURCE",
                    "PDN_V": "12",
                    "PDN_P_NET": "VIN",
                    "PDN_N_NET": "GND",
                },
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="U2",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "REGULATOR",
                    "PDN_REGULATOR_TYPE": "SMPS",
                    "PDN_REGULATOR_EFFICIENCY": "1",
                    "PDN_V": "5",
                    "PDN_SMPS_TOPOLOGY": "BUCK",
                    "PDN_IN_P_NET": "VIN",
                    "PDN_IN_N_NET": "GND",
                    "PDN_OUT_P_NET": "VOUT",
                    "PDN_OUT_N_NET": "GND",
                    "PDN_SW1_NET": "SW1",
                    "PDN_SW2_NET": "VOUT",
                },
                pin_designators=("1", "2", "3", "4"),
            ),
            RawSchComponent(
                designator="Q_HS",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "PATH",
                    "PDN_R": "10m",
                    "PDN_P_PINS": "3",
                    "PDN_N_PINS": "2",
                },
                pin_designators=("1", "2", "3"),
            ),
            RawSchComponent(
                designator="L1",
                schdoc_name="Pwr.SchDoc",
                parameters={"PDN_ROLE": "PATH", "PDN_R": "5m"},
                pin_designators=("1", "2"),
            ),
            RawSchComponent(
                designator="Q_LS",
                schdoc_name="Pwr.SchDoc",
                parameters={
                    "PDN_ROLE": "PATH",
                    "PDN_R": "8m",
                    "PDN_P_PINS": "3",
                    "PDN_N_PINS": "2",
                },
                pin_designators=("1", "2", "3"),
            ),
        ),
        pcb_components=tuple(_pcb(d, i) for i, d in enumerate(["J1", "U2", "Q_HS", "L1", "Q_LS"])),
        pads=(
            _pad(0, "1", 1),
            _pad(0, "2", 0),
            _pad(1, "1", 1),
            _pad(1, "2", 0),
            _pad(1, "3", 3),
            _pad(1, "4", 0),
            _pad(2, "1", 0),
            _pad(2, "2", 2),
            _pad(2, "3", 1),  # HS VIN↔SW1
            _pad(3, "1", 2),
            _pad(3, "2", 3),  # L SW1↔VOUT(=SW2)
            _pad(4, "1", 0),
            _pad(4, "2", 0),
            _pad(4, "3", 2),  # LS SW1↔GND
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.smps_topology == "BUCK"
    assert reg.gain == pytest.approx(5.0 / 12.0)
    assert len(reg.switch_path_legs) == 1
    assert reg.switch_path_legs[0].kind == "ls_in"
    assert reg.switch_path_legs[0].coeff == pytest.approx(1.0 - reg.gain)


# ---------------------------------------------------------------------------
# BOOST regression (see review of PR #54)
# ---------------------------------------------------------------------------


def _boost_proj():
    """Textbook boost: VIN-L1-SW1, HS FET SW1->VOUT, LS FET SW1->GND.

    Nets: GND=0, VIN=1, SW1=2, VOUT=3.
    """
    sch = (
        RawSchComponent(
            designator="J1",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SOURCE",
                "PDN_V": "12",
                "PDN_P_NET": "VIN",
                "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="U2",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "REGULATOR",
                "PDN_REGULATOR_TYPE": "SMPS",
                "PDN_REGULATOR_EFFICIENCY": "0.9",
                "PDN_V": "24",
                "PDN_SMPS_TOPOLOGY": "BOOST",
                "PDN_IN_P_NET": "VIN",
                "PDN_IN_N_NET": "GND",
                "PDN_OUT_P_NET": "VOUT",
                "PDN_OUT_N_NET": "GND",
                "PDN_SW1_NET": "SW1",
                "PDN_SW2_NET": "VOUT",
                "PDN_IN_P_PINS": "1",
                "PDN_IN_N_PINS": "2",
                "PDN_OUT_P_PINS": "3",
                "PDN_OUT_N_PINS": "4",
            },
            pin_designators=("1", "2", "3", "4"),
        ),
        RawSchComponent(
            designator="L1",
            schdoc_name="Pwr.SchDoc",
            parameters={"PDN_ROLE": "PATH", "PDN_R": "7m"},
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="Q_HS",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH",
                "PDN_R": "12m",
                "PDN_P_PINS": "3",
                "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="Q_LS",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH",
                "PDN_R": "8m",
                "PDN_P_PINS": "3",
                "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="LOAD",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SINK",
                "PDN_I": "2A",
                "PDN_P_NET": "VOUT",
                "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
    )
    pcb = tuple(
        _pcb(d, i)
        for i, d in enumerate(["J1", "U2", "L1", "Q_HS", "Q_LS", "LOAD"])
    )
    _B_VOUT = 3
    pads = (
        _pad(0, "1", _VIN),
        _pad(0, "2", _GND),
        _pad(1, "1", _VIN),
        _pad(1, "2", _GND),
        _pad(1, "3", _B_VOUT),
        _pad(1, "4", _GND),
        _pad(2, "1", _VIN),
        _pad(2, "2", _SW1),
        _pad(3, "1", _GND),
        _pad(3, "2", _SW1),
        _pad(3, "3", _B_VOUT),
        _pad(4, "1", _GND),
        _pad(4, "2", _GND),
        _pad(4, "3", _SW1),
        _pad(5, "1", _B_VOUT),
        _pad(5, "2", _GND),
    )
    return _minimal_proj(
        nets=(
            RawNet("GND"),
            RawNet("VIN"),
            RawNet("SW1"),
            RawNet("VOUT"),
        ),
        sch_components=sch,
        pcb_components=pcb,
        pads=pads,
    )


def test_boost_stage_cuts_at_hs_fet_and_switches_through_ls():
    """A boost cuts at Q_HS, bridges through L1, and switches through Q_LS.

    The boost inductor carries the *input* current (gain*i_v), so the part
    carrying i_v -- and therefore the cut -- is the high-side FET.
    """
    result = parse_annotations(_boost_proj(), enabled_layers=[1])
    assert result.ok, result.errors

    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.smps_topology == "BOOST"

    # The high-side FET is the cut and contributes its RDSon.
    assert reg.cut_resistance == pytest.approx(0.012)
    assert {p.net_index for p in reg.in_p.pins} == {_SW1}
    assert {p.net_index for p in reg.out_p.pins} == {3}  # VOUT

    # The inductor is a plain resistive bridge; the HS FET is not a bridge.
    bridge_des = {
        d.designator for d in result.directives if isinstance(d, ResistorSpec)
    }
    assert "L1" in bridge_des
    assert "Q_HS" not in bridge_des

    # The low-side FET pulls (gain-1)*i_v down into the ground plane.
    assert len(reg.switch_path_legs) == 1
    leg = reg.switch_path_legs[0]
    assert leg.designator == "Q_LS"
    assert leg.kind == "ls_out"
    assert leg.coeff == pytest.approx(reg.gain - 1.0)
    # Current flows switch node -> ground, so p is the SW1 pad.
    assert {p.net_index for p in leg.p.pins} == {_SW1}
    assert {p.net_index for p in leg.n.pins} == {_GND}


def test_switch_leg_stamp_is_balanced_and_draws_i_v():
    """A leg must draw exactly what it injects, and the input must draw i_v.

    COO assembly *sums* duplicate (row, col) entries rather than keeping them
    separate, so a stamp that writes -coeff and +coeff at the same node
    cancels itself: the switch node then contributes nothing and the leg's
    other end injects current no one draws.
    """
    import numpy as np

    from pdnsolver import problem as pp
    from pdnsolver.solver import stamp_network_into_system

    v_p, v_n, s_f, s_t = pp.NodeID(), pp.NodeID(), pp.NodeID(), pp.NodeID()
    leg_f, leg_t = pp.NodeID(), pp.NodeID()
    reg = pp.VoltageRegulator(
        v_p=v_p, v_n=v_n, s_f=s_f, s_t=s_t,
        voltage=3.3, gain=0.4, input_gain=1.0,
        switch_legs=((leg_f, leg_t, 0.6),),
    )
    nodes = [v_p, v_n, s_f, s_t, leg_f, leg_t]
    idx = {n: i for i, n in enumerate(nodes)}
    i_v = len(nodes)

    class _Indexer:
        node_to_global_index = idx
        extra_source_to_global_index = {reg: i_v}

    rows: list = []
    cols: list = []
    vals: list = []
    r = np.zeros(i_v + 1)
    stamp_network_into_system(
        pp.Network(connections=[], elements=[reg]), _Indexer(),
        rows, cols, vals, r,
    )

    # Collapse the i_v column the way coo_matrix.tocsc() would.
    col: dict[int, float] = {}
    for rr, cc, vv in zip(rows, cols, vals):
        if cc == i_v:
            col[rr] = col.get(rr, 0.0) + vv

    # The leg: f loses coeff*i_v, t receives it.
    assert col[idx[leg_f]] == pytest.approx(-0.6)
    assert col[idx[leg_t]] == pytest.approx(0.6)

    # input_gain=1.0 means the cut element is a conductor: it draws i_v, not
    # gain*i_v, because the conversion ratio lives in the legs.
    assert col[idx[s_f]] == pytest.approx(-1.0)
    assert col[idx[s_t]] == pytest.approx(1.0)

    # Nothing is injected into the network overall.
    assert sum(col.values()) == pytest.approx(0.0)


def test_internal_fet_regulator_still_draws_gain_i_v():
    """With no external cut, input_gain is None and the draw stays gain*i_v."""
    import numpy as np

    from pdnsolver import problem as pp
    from pdnsolver.solver import stamp_network_into_system

    v_p, v_n, s_f, s_t = pp.NodeID(), pp.NodeID(), pp.NodeID(), pp.NodeID()
    reg = pp.VoltageRegulator(
        v_p=v_p, v_n=v_n, s_f=s_f, s_t=s_t, voltage=3.3, gain=0.4,
    )
    nodes = [v_p, v_n, s_f, s_t]
    idx = {n: i for i, n in enumerate(nodes)}
    i_v = len(nodes)

    class _Indexer:
        node_to_global_index = idx
        extra_source_to_global_index = {reg: i_v}

    rows: list = []
    cols: list = []
    vals: list = []
    r = np.zeros(i_v + 1)
    stamp_network_into_system(
        pp.Network(connections=[], elements=[reg]), _Indexer(),
        rows, cols, vals, r,
    )
    col: dict[int, float] = {}
    for rr, cc, vv in zip(rows, cols, vals):
        if cc == i_v:
            col[rr] = col.get(rr, 0.0) + vv

    assert col[idx[s_f]] == pytest.approx(-0.4)
    assert col[idx[s_t]] == pytest.approx(0.4)


def _buck_with_unrelated_ldo_proj():
    """External-FET buck plus an ordinary LDO sharing the VIN rail.

    Nets: GND=0, VIN=1, SW1=2, VOUT=3, V1V8=4.
    """
    _B_VOUT, _V1V8 = 3, 4
    sch = (
        RawSchComponent(
            designator="J1",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SOURCE", "PDN_V": "12",
                "PDN_P_NET": "VIN", "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="U2",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "REGULATOR",
                "PDN_REGULATOR_TYPE": "SMPS",
                "PDN_REGULATOR_EFFICIENCY": "0.9",
                "PDN_V": "5",
                "PDN_SMPS_TOPOLOGY": "BUCK",
                "PDN_IN_P_NET": "VIN", "PDN_IN_N_NET": "GND",
                "PDN_OUT_P_NET": "VOUT", "PDN_OUT_N_NET": "GND",
                "PDN_SW1_NET": "SW1",
                "PDN_IN_P_PINS": "1", "PDN_IN_N_PINS": "2",
                "PDN_OUT_P_PINS": "3", "PDN_OUT_N_PINS": "4",
            },
            pin_designators=("1", "2", "3", "4"),
        ),
        # Plain internal-FET LDO on the same input rail. It declares no
        # switch stage, so it must never be considered a PATH host.
        RawSchComponent(
            designator="U3",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "REGULATOR",
                "PDN_REGULATOR_TYPE": "LDO",
                "PDN_V": "1.8",
                "PDN_IN_P_NET": "VIN", "PDN_IN_N_NET": "GND",
                "PDN_OUT_P_NET": "V1V8", "PDN_OUT_N_NET": "GND",
                "PDN_IN_P_PINS": "1", "PDN_IN_N_PINS": "2",
                "PDN_OUT_P_PINS": "3", "PDN_OUT_N_PINS": "4",
            },
            pin_designators=("1", "2", "3", "4"),
        ),
        RawSchComponent(
            designator="Q_HS",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH", "PDN_R": "10m",
                "PDN_P_PINS": "3", "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="Q_LS",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "PATH", "PDN_R": "8m",
                "PDN_P_PINS": "3", "PDN_N_PINS": "2",
            },
            pin_designators=("1", "2", "3"),
        ),
        RawSchComponent(
            designator="L1",
            schdoc_name="Pwr.SchDoc",
            parameters={"PDN_ROLE": "PATH", "PDN_R": "7m"},
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="LOAD",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SINK", "PDN_I": "2A",
                "PDN_P_NET": "VOUT", "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
        RawSchComponent(
            designator="LOAD2",
            schdoc_name="Pwr.SchDoc",
            parameters={
                "PDN_ROLE": "SINK", "PDN_I": "100mA",
                "PDN_P_NET": "V1V8", "PDN_N_NET": "GND",
            },
            pin_designators=("1", "2"),
        ),
    )
    pcb = tuple(
        _pcb(d, i)
        for i, d in enumerate(
            ["J1", "U2", "U3", "Q_HS", "Q_LS", "L1", "LOAD", "LOAD2"]
        )
    )
    pads = (
        _pad(0, "1", _VIN), _pad(0, "2", _GND),
        _pad(1, "1", _VIN), _pad(1, "2", _GND),
        _pad(1, "3", _B_VOUT), _pad(1, "4", _GND),
        _pad(2, "1", _VIN), _pad(2, "2", _GND),
        _pad(2, "3", _V1V8), _pad(2, "4", _GND),
        _pad(3, "1", _GND), _pad(3, "2", _SW1), _pad(3, "3", _VIN),
        _pad(4, "1", _GND), _pad(4, "2", _GND), _pad(4, "3", _SW1),
        _pad(5, "1", _SW1), _pad(5, "2", _B_VOUT),
        _pad(6, "1", _B_VOUT), _pad(6, "2", _GND),
        _pad(7, "1", _V1V8), _pad(7, "2", _GND),
    )
    return _minimal_proj(
        nets=(
            RawNet("GND"), RawNet("VIN"), RawNet("SW1"),
            RawNet("VOUT"), RawNet("V1V8"),
        ),
        sch_components=sch,
        pcb_components=pcb,
        pads=pads,
    )


def test_second_regulator_on_the_input_rail_is_not_a_path_host():
    """An LDO sharing VIN must not make every high-side part ambiguous.

    Host candidates are restricted to regulators that declare a switch
    stage, and matching prefers the switch nets over the shared rails.
    """
    result = parse_annotations(_buck_with_unrelated_ldo_proj(), enabled_layers=[1])
    assert result.ok, result.errors
    assert not any("ambiguous host" in e for e in result.errors)

    regs = {d.designator: d for d in result.directives
            if isinstance(d, RegulatorSpec)}
    assert set(regs) == {"U2", "U3"}
    # The LDO is untouched: no legs, no relocated cut.
    assert not regs["U3"].switch_path_legs
    assert regs["U3"].cut_resistance is None

    # The buck bound its parts: L1 is the cut, Q_HS a bridge, Q_LS the leg.
    u2 = regs["U2"]
    assert u2.cut_resistance == pytest.approx(0.007)
    assert len(u2.switch_path_legs) == 1
    leg = u2.switch_path_legs[0]
    assert leg.designator == "Q_LS"
    assert leg.kind == "ls_in"
    assert leg.coeff == pytest.approx(1.0 - u2.gain)
    # Buck freewheel: current flows out of the ground plane into SW1.
    assert {p.net_index for p in leg.p.pins} == {_GND}
    assert {p.net_index for p in leg.n.pins} == {_SW1}

    bridge_des = {d.designator for d in result.directives
                  if isinstance(d, ResistorSpec)}
    assert "Q_HS" in bridge_des
    assert "L1" not in bridge_des
