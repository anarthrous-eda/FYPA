"""Auto-bridge Altium Net Tie components without PDN_ROLE=SERIES."""

from __future__ import annotations

from pathlib import Path

from fypa.altium.annotations import (
    COMPONENT_KIND_NET_TIE_BOM,
    AnnotationResult,
    COMPONENT_KIND_NET_TIE_NO_BOM,
    NET_TIE_BRIDGE_RESISTANCE_OHM,
    ResistorSpec,
    _nettie_net_names,
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
    _carry_absorbed_notes,
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


# --- post-review fixes (PR #48) ----------------------------------------------


def test_duplicate_net_names_do_not_self_pair():
    """Two channels' nets share a name; the bridge must not pair one with itself.

    Altium does not channel-qualify names in Nets6, so a placement's pads can
    sit on distinct indices carrying the same name. Deduping by index would
    build PDN_P_NET == PDN_N_NET, whose terminals resolve to the same pads and
    get rejected as a short instead of bridged.
    """
    proj = _minimal_proj(
        nets=(RawNet("GND"), RawNet("GND"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="NT1", schdoc_name="Pwr.SchDoc", parameters={},
                pin_designators=("1", "2", "3"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="NT1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie3",
                source_designator="NT1",
            ),
        ),
        pads=(
            _pad(0, "1", 0, 0.0),   # GND (net index 0)
            _pad(0, "2", 1, 1.0),   # GND (net index 1 - same name, other channel)
            _pad(0, "3", 2, 2.0),   # VOUT
        ),
    )
    assert _nettie_net_names(proj, 0) == ["GND", "VOUT"]

    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    # One bridge: the two GND-named nets collapse to a single chain entry.
    assert len(resistors) == 1
    p_pads = {pin.pad_designator for pin in resistors[0].p.pins}
    n_pads = {pin.pad_designator for pin in resistors[0].n.pins}
    assert not (p_pads & n_pads), "P and N resolved to the same pad"
    # Index-based dedup would chain GND to GND and land here instead.
    assert not any("equally strong evidence" in w for w in result.warnings),         result.warnings


def test_auto_bridge_resolution_failure_is_a_warning_not_an_error():
    """A Net Tie the bridge cannot resolve must not fail `fypa annotations`.

    Nobody authored this directive - it was inferred from ComponentKind - so a
    resolution failure is not the user's annotation mistake. Here a net_remap
    collapses the tie's two nets, so both terminals match the same pad and
    overlap arbitration rejects the pair. That complaint belongs in warnings:
    `do_annotations` exits non-zero on `not result.ok`, which would fail a CI
    gate and pop a load-time error telling the user to add PDN_N_PINS to a part
    they never touched.
    """
    proj = _nettie_proj()
    result = parse_annotations(proj, enabled_layers=[1], net_remap={1: 0})
    assert result.ok, result.errors
    assert not result.errors
    shorted = [w for w in result.warnings if "would be shorted" in w]
    assert len(shorted) == 1, result.warnings
    assert "auto-bridge skipped" in shorted[0]
    # The actionable advice from the original message is kept verbatim.
    assert "PDN_P_PINS" in shorted[0]
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)


def test_annotated_series_resolution_failure_is_still_an_error():
    """The downgrade must not leak to directives the user actually wrote."""
    proj = _nettie_proj(parameters={"PDN_ROLE": "SERIES", "PDN_R": "0.01"})
    result = parse_annotations(proj, enabled_layers=[1], net_remap={1: 0})
    assert not result.ok
    assert any("would be shorted" in e for e in result.errors), result.errors


def test_skip_reason_names_the_net_count_not_the_pad_count():
    """A 3-pad tie on one net must not be told its pad count is the problem."""
    proj = _minimal_proj(
        nets=(RawNet("GND"),),
        sch_components=(
            RawSchComponent(
                designator="NT1", schdoc_name="Pwr.SchDoc", parameters={},
                pin_designators=("1", "2", "3"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="NT1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie3",
                source_designator="NT1",
            ),
        ),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 0, 1.0), _pad(0, "3", 0, 2.0)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    skip = [w for w in result.warnings if "skipped auto-bridge" in w]
    assert len(skip) == 1
    assert "not 2" not in skip[0], skip[0]
    assert "one net" in skip[0] and "GND" in skip[0], skip[0]


def test_pcb_only_nettie_is_bridged():
    """A Net Tie known only to the PcbDoc (ECO, or no schematics) still bridges."""
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT")),
        sch_components=(),
        pcb_components=(
            RawPcbComponent(
                designator="NT9", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 1.0)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert [d.designator for d in resistors] == ["NT9"]
    assert resistors[0].resistance == NET_TIE_BRIDGE_RESISTANCE_OHM


def test_pcb_only_nettie_respects_pdn_override():
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT")),
        sch_components=(),
        pcb_components=(
            RawPcbComponent(
                designator="NT9", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
                parameters={"PDN_ROLE": "SERIES", "PDN_R": "0.02"},
            ),
        ),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 1.0)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(resistors) == 1
    assert resistors[0].resistance == 0.02


def test_pcb_only_nettie_respects_skip_designators():
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT")),
        sch_components=(),
        pcb_components=(
            RawPcbComponent(
                designator="NT9", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 1.0)),
    )
    result = parse_annotations(
        proj, enabled_layers=[1], skip_designators={"NT9"},
    )
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)


def test_nettie_on_another_board_is_silent():
    """Sheets that placed nothing on this PcbDoc must not warn.

    extract_project reads every SchDoc but only one .PcbDoc, so a second
    board's Net Ties always resolve to no placement. Warning per tie floods a
    log the user cannot act on.
    """
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT")),
        sch_components=(
            # Board A: placed here.
            RawSchComponent(
                designator="NT1", schdoc_name="BoardA.SchDoc", parameters={},
                pin_designators=("1", "2"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
            # Board B: nothing from this sheet is on the loaded PcbDoc.
            RawSchComponent(
                designator="NT7", schdoc_name="BoardB.SchDoc", parameters={},
                pin_designators=("1", "2"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="NT1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                source_designator="NT1",
            ),
        ),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 1.0)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert result.ok, result.errors
    assert not any("NT7" in w for w in result.warnings), result.warnings


def test_missing_placement_on_a_populated_sheet_still_warns():
    """The same sheet placed something, so a missing tie is worth reporting."""
    proj = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="NT1", schdoc_name="Pwr.SchDoc", parameters={},
                pin_designators=("1", "2"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
            RawSchComponent(
                designator="NT2", schdoc_name="Pwr.SchDoc", parameters={},
                pin_designators=("1", "2"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="NT1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                source_designator="NT1",
            ),
        ),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 1.0)),
    )
    result = parse_annotations(proj, enabled_layers=[1])
    assert any(
        "NT2" in w and "no PCB placement" in w for w in result.warnings
    ), result.warnings


def test_auto_bridge_note_is_recorded_for_the_post_merge_reparse():
    """The user-visible log must record that two nets were shorted.

    load_project parses twice and throws pass 1 away whenever a merge happened
    - which is exactly when a Net Tie bridged something. absorbed_notes is what
    the loader carries across; without it a tie that wrongly shorted two rails
    leaves no trace in the annotation log or the load dialog.
    """
    proj = _nettie_proj()
    first = parse_annotations(proj, enabled_layers=[1])
    assert any("auto-bridged" in n for n in first.absorbed_notes)

    _, skipped, _ = _build_net_merge_map(first, proj)
    merged = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT"), RawNet("GND")),
        sch_components=proj.sch_components,
        pcb_components=proj.pcb_components,
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 0, 1.0)),
    )
    second = parse_annotations(
        merged, enabled_layers=[1], skip_designators=skipped,
    )
    # Pass 2 on its own loses the notice - the carry-forward is what saves it.
    assert not any("auto-bridged" in w for w in second.warnings)

    _carry_absorbed_notes(first, second)
    assert any("auto-bridged" in w for w in second.warnings)
    # Carried notes stay carryable, and re-running must not duplicate them.
    before = list(second.warnings)
    _carry_absorbed_notes(first, second)
    assert second.warnings == before


def test_carry_absorbed_notes_keeps_order_and_skips_duplicates():
    first = AnnotationResult()
    first.note_absorbed("a")
    first.note_absorbed("b")
    first.note_absorbed("c")
    second = AnnotationResult()
    second.warnings.append("b")

    _carry_absorbed_notes(first, second)
    assert second.warnings == ["b", "a", "c"]
    assert second.absorbed_notes == ["a", "c"]


def _nettie_proj_kind_on_both_sides(
    sch_parameters: dict[str, str] | None = None,
) -> ExtractedProject:
    """Altium stamps ComponentKind on the footprint as well as the symbol."""
    return _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT")),
        sch_components=(
            RawSchComponent(
                designator="NT1", schdoc_name="Pwr.SchDoc",
                parameters=dict(sch_parameters or {}),
                pin_designators=("1", "2"),
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pcb_components=(
            RawPcbComponent(
                designator="NT1", center=Pt2D(0, 0), rotation_deg=0.0,
                layer_name="TOP", footprint="NetTie2",
                source_designator="NT1",
                component_kind=COMPONENT_KIND_NET_TIE_NO_BOM,
            ),
        ),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 1.0)),
    )


def test_kind_on_both_sides_bridges_exactly_once():
    result = parse_annotations(
        _nettie_proj_kind_on_both_sides(), enabled_layers=[1])
    assert result.ok, result.errors
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(resistors) == 1
    assert sum("auto-bridged" in w for w in result.warnings) == 1


def test_schematic_pdn_optout_survives_the_pcb_side_sweep():
    """A symbol-level PDN_* must win even though the footprint says NetTie.

    The PCB sweep exists for ties with no symbol; it must not reach a tie the
    schematic loop deliberately left alone, or an explicit SERIES resistance
    would be shadowed by a 0.5 mOhm short.
    """
    result = parse_annotations(
        _nettie_proj_kind_on_both_sides(
            sch_parameters={"PDN_ROLE": "SERIES", "PDN_R": "0.01"}),
        enabled_layers=[1],
    )
    assert result.ok, result.errors
    resistors = [d for d in result.directives if isinstance(d, ResistorSpec)]
    assert len(resistors) == 1
    assert resistors[0].resistance == 0.01
    assert not any("auto-bridged" in w for w in result.warnings), result.warnings


def test_partial_schematic_pdn_optout_survives_the_pcb_side_sweep():
    """Half-finished annotations opt out too, and must stay opted out."""
    result = parse_annotations(
        _nettie_proj_kind_on_both_sides(sch_parameters={"PDN_ROLE": "SERIES"}),
        enabled_layers=[1],
    )
    assert not any(isinstance(d, ResistorSpec) for d in result.directives)
    assert not any("auto-bridged" in w for w in result.warnings), result.warnings


def test_skip_designators_survives_the_pcb_side_sweep():
    """Pass 2 skips absorbed designators; the PCB sweep must honour that.

    Otherwise the post-merge re-parse re-synthesises the very short the merge
    just absorbed, on pads that now share one net.
    """
    proj = _nettie_proj_kind_on_both_sides()
    first = parse_annotations(proj, enabled_layers=[1])
    _, skipped, _ = _build_net_merge_map(first, proj)
    merged = _minimal_proj(
        nets=(RawNet("VIN"), RawNet("VOUT")),
        sch_components=proj.sch_components,
        pcb_components=proj.pcb_components,
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 0, 1.0)),
    )
    second = parse_annotations(
        merged, enabled_layers=[1], skip_designators=skipped,
    )
    assert not any(isinstance(d, ResistorSpec) for d in second.directives)
    assert not any("skipped auto-bridge" in w for w in second.warnings), \
        second.warnings
