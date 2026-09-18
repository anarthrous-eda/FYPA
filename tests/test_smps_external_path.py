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


def test_buckboost_path_binds_and_cuts_at_inductor():
    result = parse_annotations(_buckboost_proj_with_ls(), enabled_layers=[1])
    assert result.ok, result.errors
    assert not any(isinstance(d, PathSpec) for d in result.directives)

    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.smps_topology == "BUCKBOOST"
    assert abs(reg.gain - (24.0 / (12.0 * 0.9))) < 1e-6
    assert reg.inductor_dcr == pytest.approx(0.007)
    # IN/OUT relocated to inductor pads (MID / SW2).
    in_nets = {p.net_index for p in reg.in_p.pins}
    out_nets = {p.net_index for p in reg.out_p.pins}
    assert in_nets == {_MID}
    assert out_nets == {_SW2}

    bridges = [d for d in result.directives if isinstance(d, ResistorSpec)]
    bridge_des = {d.designator for d in bridges}
    assert "Q_HS_IN" in bridge_des
    assert "Q_HS_OUT" in bridge_des
    assert "R_SHUNT" in bridge_des
    assert "L1" not in bridge_des  # inductor is the cut, not a bridge

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
    assert reg.inductor_dcr is not None
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PR #54 BOOST defect: _classify_path checks SW1<->SW2 before IN_P, so "
        "L1 (VIN<->SW1) becomes hs_in and Q_HS (SW1<->VOUT) becomes the "
        "inductor/cut. Q_LS then classifies as ls_in, but _compute_ls_coeffs "
        "gives BOOST ls_in_coeff=0, so the only current-carrying leg is "
        "dropped. Needs a topology-aware classifier and a coefficient table "
        "that agree on which leg a boost switches."
    ),
)
def test_boost_stage_classifies_inductor_and_ls_leg():
    """A boost must cut at L1, bridge through Q_HS, and switch through Q_LS."""
    result = parse_annotations(_boost_proj(), enabled_layers=[1])
    assert result.ok, result.errors

    reg = next(d for d in result.directives if isinstance(d, RegulatorSpec))
    assert reg.smps_topology == "BOOST"

    # The inductor is the cut, and carries its own DCR.
    assert reg.inductor_dcr == pytest.approx(0.007)

    # The high-side FET is a plain resistive bridge; the inductor is not.
    bridge_des = {
        d.designator for d in result.directives if isinstance(d, ResistorSpec)
    }
    assert "Q_HS" in bridge_des
    assert "L1" not in bridge_des

    # The low-side FET is the switch leg and carries real current.
    assert len(reg.switch_path_legs) == 1
    leg = reg.switch_path_legs[0]
    assert leg.designator == "Q_LS"
    assert leg.coeff == pytest.approx(reg.gain - 1.0)
