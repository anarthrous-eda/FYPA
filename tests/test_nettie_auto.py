"""Auto-bridge Altium Net Tie components without PDN_ROLE=SERIES."""

from __future__ import annotations

from pathlib import Path

from fypa.altium.annotations import (
    COMPONENT_KIND_NET_TIE_BOM,
    COMPONENT_KIND_NET_TIE_NO_BOM,
    NET_TIE_BRIDGE_RESISTANCE_OHM,
    ResistorSpec,
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
from fypa.altium.loader import (
    NET_MERGE_RESISTANCE_THRESHOLD_OHM,
    _build_net_merge_map,
)


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


def _pad(comp_idx: int, pin: str, net_index: int, x: float = 0.0) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0), width_mm=1, height_mm=1, hole_mm=0,
        shape=2, rotation_deg=0, layer_id=1, net_index=net_index,
        designator=pin, component_index=comp_idx,
        is_through_hole=False, is_smt=True,
    )


def _nettie_proj(
    *,
    kind: int = COMPONENT_KIND_NET_TIE_NO_BOM,
    parameters: dict[str, str] | None = None,
    designator: str = "NT1",
) -> ExtractedProject:
    return _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT"), RawNet("GND")),
        sch_components=(
            RawSchComponent(
                designator=designator,
                schdoc_name="Pwr.SchDoc",
                parameters=dict(parameters or {}),
                pin_designators=("1", "2"),
                component_kind=kind,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator=designator, center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                source_designator=designator,
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),  # VIN
            _pad(0, "2", 1, 1.0),  # VOUT
        ),
    )


def test_nettie_bridge_resistance_below_merge_threshold():
    assert NET_TIE_BRIDGE_RESISTANCE_OHM < NET_MERGE_RESISTANCE_THRESHOLD_OHM


def test_nettie_auto_emits_low_r_series():
    result = parse_annotations(_nettie_proj(), enabled_layers=[1])
    assert result.ok
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(resistors) == 1
    assert resistors[0].designator == "NT1"
    assert resistors[0].resistance == NET_TIE_BRIDGE_RESISTANCE_OHM
    nets = {
        pin.net_index
        for term in (resistors[0].p, resistors[0].n)
        for pin in term.pins
    }
    assert nets == {0, 1}
    assert any("auto-bridged" in w for w in result.warnings)


def test_nettie_bom_kind_also_auto_bridges():
    result = parse_annotations(
        _nettie_proj(kind=COMPONENT_KIND_NET_TIE_BOM),
        enabled_layers=[1],
    )
    assert result.ok
    assert sum(1 for d in result.directives if isinstance(d, ResistorSpec)) == 1


def test_explicit_series_on_nettie_skips_auto():
    result = parse_annotations(
        _nettie_proj(parameters={
            "PDN_ROLE": "SERIES",
            "PDN_R": "0.01",
        }),
        enabled_layers=[1],
    )
    assert result.ok
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(resistors) == 1
    assert resistors[0].resistance == 0.01
    assert not any("auto-bridged" in w for w in result.warnings)


def test_pcb_eco_series_on_nettie_skips_auto():
    """Blanket/ECO PDN_ROLE on the PCB placement must override auto-bridge."""
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT"), RawNet("GND")),
        sch_components=(
            RawSchComponent(
                designator="NT1",
                schdoc_name="Pwr.SchDoc",
                parameters={},
                pin_designators=("1", "2"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="NT1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                source_designator="NT1",
                parameters={"PDN_ROLE": "SERIES", "PDN_R": "0.02"},
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "2", 1, 1.0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(resistors) == 1
    assert resistors[0].resistance == 0.02
    assert not any("auto-bridged" in w for w in result.warnings)


def test_partial_pdn_params_on_nettie_skip_auto():
    """Stray PDN_R without ROLE must not still get a synthetic merge short."""
    result = parse_annotations(
        _nettie_proj(parameters={"PDN_R": "0.01"}),
        enabled_layers=[1],
    )
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)
    assert not any("auto-bridged" in w for w in result.warnings)


def test_indexed_pdn_params_on_nettie_skip_auto():
    result = parse_annotations(
        _nettie_proj(parameters={"PDN1_R": "0.01"}),
        enabled_layers=[1],
    )
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)
    assert not any("auto-bridged" in w for w in result.warnings)


def test_standard_component_without_pdn_is_not_bridged():
    proj = _nettie_proj(kind=0)  # ComponentKind.STANDARD
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)


def test_nettie_absorbed_by_net_merge_map():
    proj = _nettie_proj()
    annotations = parse_annotations(proj, enabled_layers=[1])
    remap, skipped, bridges = _build_net_merge_map(annotations, proj)
    assert remap  # VOUT → VIN (or vice versa)
    assert "NT1" in {d.upper() for d in skipped}
    assert len(bridges) == 1
    assert bridges[0].designator == "NT1"


def test_nettie_multi_pcb_instance():
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT"), RawNet("VIN_B"), RawNet("VOUT_B")),
        sch_components=(
            RawSchComponent(
                designator="NT1",
                schdoc_name="Pwr.SchDoc",
                parameters={},
                pin_designators=("1", "2"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="NT1_CH1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                source_designator="NT1",
            ),
            RawPcbComponent(
                designator="NT1_CH2", center=Pt2D(10, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                source_designator="NT1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "2", 1, 1.0),
            _pad(1, "1", 2, 10.0),
            _pad(1, "2", 3, 11.0),
        ),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert {d.designator for d in resistors} == {"NT1_CH1", "NT1_CH2"}


def test_skip_designators_suppresses_nettie_resynth():
    """After merge, pass-2 must not re-emit NetTie shorts on same-net pads."""
    proj = _nettie_proj()
    first = parse_annotations(proj, enabled_layers=[1])
    _, skipped, _ = _build_net_merge_map(first, proj)
    # Simulate merged pads: both on net 0.
    merged = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT"), RawNet("GND")),
        sch_components=proj.sch_components,
        pcb_components=proj.pcb_components,
        pads=(
            _pad(0, "1", 0, 0.0),
            _pad(0, "2", 0, 1.0),
        ),
    )
    second = parse_annotations(
        merged, enabled_layers=[1], skip_designators=skipped,
    )
    assert not any(isinstance(d, ResistorSpec) for d in second.directives)
