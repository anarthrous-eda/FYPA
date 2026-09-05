"""Editor-directive → solver-spec conversion tests.

``apply_editor_directives`` turns the ``.fypa`` project's hand-placed
editor directives into real ``SourceSpec`` / ``SinkSpec`` / ``ResistorSpec``
entries before a re-solve. These exercise the SERIES path with lightweight
stand-ins for the Altium extraction — a free marker anchors directly on a
named net, so no real board / pad geometry is needed.
"""
from __future__ import annotations

from types import SimpleNamespace

from fypa.altium.annotations import (
    AnnotationResult,
    ResistorSpec,
    SinkSpec,
    SourceSpec,
)
from fypa.editor_directives import apply_editor_directives
from fypa.project_file import EditorDirective


def _loaded(net_names: list[str]):
    """A minimal LoadedProject stand-in: one enabled copper layer, the
    given nets, no components / pads (free markers anchor on copper)."""
    extracted = SimpleNamespace(
        nets=[SimpleNamespace(name=n) for n in net_names],
        pcb_components=[],
        pads=[],
        enabled_copper_layer_ids=lambda: [1],
    )
    return SimpleNamespace(extracted=extracted, annotations=AnnotationResult())


def _free(role: str, **kw) -> EditorDirective:
    return EditorDirective(kind="free", role=role, anchor_xy=(0.0, 0.0),
                           layer_id=1, **kw)


def test_series_directive_becomes_resistor_spec():
    loaded = _loaded(["+5V", "+3V3"])
    eds = [_free("SERIES", single_net=False, p_net="+5V", n_net="+3V3",
                 resistance=0.05)]
    warnings = apply_editor_directives(loaded, eds)
    assert warnings == []
    specs = loaded.annotations.directives
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, ResistorSpec)
    assert spec.resistance == 0.05
    assert spec.p.requested_net == "+5V"
    assert spec.n.requested_net == "+3V3"


def test_series_without_resistance_is_skipped():
    loaded = _loaded(["+5V", "+3V3"])
    eds = [_free("SERIES", single_net=False, p_net="+5V", n_net="+3V3",
                 resistance=None)]
    warnings = apply_editor_directives(loaded, eds)
    assert loaded.annotations.directives == []
    assert any("no resistance" in w for w in warnings)


def test_series_without_n_net_is_skipped():
    loaded = _loaded(["+5V"])
    eds = [_free("SERIES", single_net=False, p_net="+5V", n_net=None,
                 resistance=0.05)]
    warnings = apply_editor_directives(loaded, eds)
    assert loaded.annotations.directives == []
    assert any("P net and an N net" in w for w in warnings)


def test_series_non_positive_resistance_is_skipped():
    loaded = _loaded(["+5V", "+3V3"])
    eds = [_free("SERIES", single_net=False, p_net="+5V", n_net="+3V3",
                 resistance=0.0)]
    warnings = apply_editor_directives(loaded, eds)
    assert loaded.annotations.directives == []
    assert any("positive" in w for w in warnings)


def test_resolve_is_idempotent_via_clone(monkeypatch):
    """Round-2 finding #2: each Resolve must clone before applying editor
    directives so the pristine (viewer-retained) annotations are never
    mutated and directives don't accumulate across Resolves.

    Simulates the worker's shared-annotations hazard: ``_apply_stackup_overrides``
    returns a fresh LoadedProject that SHARES ``annotations`` with the pristine
    copy. The fix always routes through ``clone_loaded_for_edit`` before
    ``apply_editor_directives``; here we assert that composition is idempotent
    on the pristine object.
    """
    from types import SimpleNamespace

    from fypa.altium.loader import clone_loaded_for_edit

    base = _loaded(["+5V", "GND"])
    # Give the stand-in the fields clone_loaded_for_edit reads.
    pristine = SimpleNamespace(
        extracted=base.extracted, annotations=base.annotations,
        absorbed_bridges=[], __dict__={},
    )
    # Emulate a stackup-override result: a *different* wrapper that still
    # shares the pristine annotations object.
    shared = SimpleNamespace(
        extracted=base.extracted, annotations=pristine.annotations,
        absorbed_bridges=[],
    )
    eds = [_free("SINK", single_net=True, p_net="+5V", current=1.0)]

    def resolve():
        clone = clone_loaded_for_edit(shared)
        apply_editor_directives(clone, eds)
        return clone

    r1 = resolve()
    r2 = resolve()

    # Pristine annotations never mutated; each resolve sees exactly one SINK
    # (no doubling), which is the whole point of the clone.
    assert pristine.annotations.directives == []
    assert len(r1.annotations.directives) == 1
    assert len(r2.annotations.directives) == 1


def test_series_bridge_unions_single_net_source_and_sink_return_groups():
    """A single-net SOURCE on +5V and a single-net SINK on +3V3 normally
    land on separate rails (distinct return groups, both open loops). An
    editor SERIES bridging the two nets must union them, so the two
    single-net directives share one return group and the loop closes."""
    loaded = _loaded(["+5V", "+3V3"])
    eds = [
        _free("SOURCE", single_net=True, p_net="+5V", voltage=5.0),
        _free("SINK", single_net=True, p_net="+3V3", current=1.0),
        _free("SERIES", single_net=False, p_net="+5V", n_net="+3V3",
              resistance=0.05),
    ]
    warnings = apply_editor_directives(loaded, eds)
    specs = loaded.annotations.directives
    src = next(s for s in specs if isinstance(s, SourceSpec))
    snk = next(s for s in specs if isinstance(s, SinkSpec))
    assert src.return_group is not None
    assert src.return_group == snk.return_group
    # The bridge closed the loop — no open-loop warning for either rail.
    assert not any("open loop" in w for w in warnings)


def test_editor_series_shorting_one_net_is_refused():
    """The annotation path errors on a P/N short; the editor path built the
    spec silently, giving a lumped element with both terminals on one node."""
    loaded = _loaded(["+5V", "+3V3"])
    eds = [_free("SERIES", single_net=False, p_net="+5V", n_net="+5V",
                 resistance=0.05)]
    warnings = apply_editor_directives(loaded, eds)
    assert warnings and "short the element" in warnings[0]
    assert loaded.annotations.directives == []


def test_editor_series_on_two_nets_is_still_accepted():
    """The guard must not fire on a legitimate free marker, whose terminals
    both carry the anchor placeholder rather than a pad name."""
    loaded = _loaded(["+5V", "+3V3"])
    eds = [_free("SERIES", single_net=False, p_net="+5V", n_net="+3V3",
                 resistance=0.05)]
    assert apply_editor_directives(loaded, eds) == []
    assert len(loaded.annotations.directives) == 1


# --- Multi-connector p_des / n_des -------------------------------------------

def _loaded_with_connectors():
    """Board stand-in: J2 (VIN+GND), J3/J5/J7 (GND return bananas), U1 load."""
    from fypa.altium.extract import Pt2D

    nets = [SimpleNamespace(name="GND"), SimpleNamespace(name="VIN")]
    comps = [
        SimpleNamespace(designator="J2"),
        SimpleNamespace(designator="J3"),
        SimpleNamespace(designator="J5"),
        SimpleNamespace(designator="J7"),
        SimpleNamespace(designator="U1"),
    ]
    pads = [
        SimpleNamespace(component_index=0, net_index=1, designator="1",
                        center=Pt2D(0, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=0, net_index=0, designator="2",
                        center=Pt2D(1, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=1, net_index=0, designator="2",
                        center=Pt2D(5, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=2, net_index=0, designator="2",
                        center=Pt2D(10, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=3, net_index=0, designator="2",
                        center=Pt2D(15, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=4, net_index=1, designator="1",
                        center=Pt2D(20, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=4, net_index=0, designator="2",
                        center=Pt2D(21, 0), layer_id=1, is_through_hole=False),
    ]
    extracted = SimpleNamespace(
        nets=nets, pcb_components=comps, pads=pads,
        enabled_copper_layer_ids=lambda: [1],
    )
    return SimpleNamespace(extracted=extracted, annotations=AnnotationResult())


def test_editor_source_n_des_multi_connector():
    loaded = _loaded_with_connectors()
    eds = [
        EditorDirective(
            kind="component", role="SOURCE", designator="J2",
            single_net=False, p_net="VIN", n_net="GND",
            n_des=["J3", "J5", "J7"], voltage=5.0,
        ),
        EditorDirective(
            kind="component", role="SINK", designator="U1",
            single_net=False, p_net="VIN", n_net="GND", current=1.0,
        ),
    ]
    warnings = apply_editor_directives(loaded, eds)
    assert warnings == [], warnings
    src = next(s for s in loaded.annotations.directives
               if isinstance(s, SourceSpec))
    assert src.designator == "J2"
    assert {p.component_designator for p in src.p.pins} == {"J2"}
    assert {p.component_designator for p in src.n.pins} == {"J3", "J5", "J7"}
    assert "J2" not in {p.component_designator for p in src.n.pins}


def _loaded_with_single_pin_jacks():
    """Lab bench stand-in: banana jacks whose one and only pad is named "1".

    J2 is the source jack (VIN), J3/J5 the return jacks (GND), U1 the load.
    Every jack reuses pad designator "1" — the shape that made the short
    check compare P's "1" against N's "1" and refuse the directive.
    """
    from fypa.altium.extract import Pt2D

    nets = [SimpleNamespace(name="GND"), SimpleNamespace(name="VIN")]
    comps = [SimpleNamespace(designator=d) for d in ("J2", "J3", "J5", "U1")]
    pads = [
        SimpleNamespace(component_index=0, net_index=1, designator="1",
                        center=Pt2D(0, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=1, net_index=0, designator="1",
                        center=Pt2D(5, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=2, net_index=0, designator="1",
                        center=Pt2D(10, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=3, net_index=1, designator="1",
                        center=Pt2D(20, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=3, net_index=0, designator="2",
                        center=Pt2D(21, 0), layer_id=1, is_through_hole=False),
    ]
    extracted = SimpleNamespace(
        nets=nets, pcb_components=comps, pads=pads,
        enabled_copper_layer_ids=lambda: [1],
    )
    return SimpleNamespace(extracted=extracted, annotations=AnnotationResult())


def test_editor_n_des_single_pin_jacks_is_not_a_short():
    """Same pad name on different jacks is not a short.

    Regression: the overlap check compared bare pad designators, so a source
    on J2 pin 1 returning through J3/J5 pin 1 — the feature's whole point —
    was refused as "both resolve to pad(s) 1".
    """
    loaded = _loaded_with_single_pin_jacks()
    eds = [EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="GND",
        n_des=["J3", "J5"], voltage=5.0,
    )]
    warnings = apply_editor_directives(loaded, eds)
    assert warnings == [], warnings
    src = next(s for s in loaded.annotations.directives
               if isinstance(s, SourceSpec))
    assert {p.component_designator for p in src.p.pins} == {"J2"}
    assert {p.component_designator for p in src.n.pins} == {"J3", "J5"}
    assert {p.pad_designator for p in src.n.pins} == {"1"}


def test_editor_n_des_onto_the_p_net_is_still_a_short():
    """The overlap guard still fires when *_DES lands N back on P's net."""
    loaded = _loaded_with_single_pin_jacks()
    eds = [EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="VIN",
        n_des=["U1"], voltage=5.0,
    )]
    warnings = apply_editor_directives(loaded, eds)
    assert any("short" in w for w in warnings), warnings
    assert loaded.annotations.directives == []


def test_editor_n_des_missing_designator_skipped():
    loaded = _loaded_with_connectors()
    eds = [EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="GND",
        n_des=["J3", "J99"], voltage=5.0,
    )]
    warnings = apply_editor_directives(loaded, eds)
    assert loaded.annotations.directives == []
    assert any("J99" in w and "not found" in w for w in warnings)


def test_editor_n_des_no_pad_on_net_skipped():
    loaded = _loaded_with_connectors()
    # J7 has only a GND pad in the fixture; point N_DES at a designator
    # whose pads are all on VIN instead — drop J3's GND by renaming net.
    loaded.extracted.pads[2].net_index = 1  # J3 pad now on VIN, not GND
    eds = [EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="GND",
        n_des=["J3"], voltage=5.0,
    )]
    warnings = apply_editor_directives(loaded, eds)
    assert loaded.annotations.directives == []
    assert any("J3" in w and "no pad" in w for w in warnings)


def test_editor_without_des_backward_compat():
    loaded = _loaded_with_connectors()
    eds = [EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="GND", voltage=5.0,
    )]
    warnings = apply_editor_directives(loaded, eds)
    assert warnings == []
    src = loaded.annotations.directives[0]
    assert isinstance(src, SourceSpec)
    assert {p.component_designator for p in src.p.pins} == {"J2"}
    assert {p.component_designator for p in src.n.pins} == {"J2"}


def test_editor_directive_p_des_n_des_round_trip():
    d = EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="GND",
        p_des=["J1"], n_des=["J3", "J5"], voltage=5.0,
    )
    restored = EditorDirective.from_dict(d.to_dict())
    assert restored.p_des == ["J1"]
    assert restored.n_des == ["J3", "J5"]
    # Absent keys stay None (backward compatible .fypa files).
    bare = EditorDirective.from_dict({"role": "SINK", "p_net": "+5V"})
    assert bare.p_des is None
    assert bare.n_des is None


def test_editor_n_des_missing_pin_override_skipped():
    """Every *_PINS entry must appear on at least one listed designator."""
    loaded = _loaded_with_connectors()
    eds = [EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="GND",
        n_des=["J3", "J5"], n_pins=["2", "99"], voltage=5.0,
    )]
    warnings = apply_editor_directives(loaded, eds)
    assert loaded.annotations.directives == []
    assert any(
        "pin overrides not found" in w and "99" in w for w in warnings
    ), warnings


def test_editor_n_des_multi_instance_merges_pads():
    """Multi-channel: one logical DES matches several physical placements."""
    from fypa.altium.extract import Pt2D

    nets = [SimpleNamespace(name="GND"), SimpleNamespace(name="VIN")]
    comps = [
        SimpleNamespace(designator="J2", source_designator="J2"),
        SimpleNamespace(designator="J3_CH1", source_designator="J3"),
        SimpleNamespace(designator="J3_CH2", source_designator="J3"),
    ]
    pads = [
        SimpleNamespace(component_index=0, net_index=1, designator="1",
                        center=Pt2D(0, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=0, net_index=0, designator="2",
                        center=Pt2D(1, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=1, net_index=0, designator="2",
                        center=Pt2D(5, 0), layer_id=1, is_through_hole=False),
        SimpleNamespace(component_index=2, net_index=0, designator="2",
                        center=Pt2D(10, 0), layer_id=1, is_through_hole=False),
    ]
    extracted = SimpleNamespace(
        nets=nets, pcb_components=comps, pads=pads,
        enabled_copper_layer_ids=lambda: [1],
    )
    loaded = SimpleNamespace(
        extracted=extracted, annotations=AnnotationResult(),
    )
    eds = [EditorDirective(
        kind="component", role="SOURCE", designator="J2",
        single_net=False, p_net="VIN", n_net="GND",
        n_des=["J3"], voltage=5.0,
    )]
    warnings = apply_editor_directives(loaded, eds)
    assert warnings == [], warnings
    src = loaded.annotations.directives[0]
    assert isinstance(src, SourceSpec)
    assert {p.component_designator for p in src.n.pins} == {
        "J3_CH1", "J3_CH2",
    }
    assert len(src.n.pins) == 2


# --- Unlock seeding helpers (no GUI) -----------------------------------------

def test_terminal_summary_pad_is_raw_not_compound():
    from fypa.altium.annotations import TerminalPin, TerminalSpec
    from fypa.altium.extract import Pt2D
    from fypa.altium.loader import _terminal_summary

    nets = [SimpleNamespace(name="GND"), SimpleNamespace(name="VIN")]
    term = TerminalSpec(pins=(
        TerminalPin(
            pad_designator="1", layer_id=1, net_index=1,
            point=Pt2D(0, 0), component_designator="J2",
        ),
        TerminalPin(
            pad_designator="2", layer_id=1, net_index=0,
            point=Pt2D(1, 0), component_designator="J3",
        ),
    ), requested_net="VIN")
    summary = _terminal_summary(term, nets)
    pads = [p["pad"] for p in summary["pins"]]
    assert pads == ["1", "2"]
    assert summary["pins"][0]["component"] == "J2"
    assert summary["pins"][0]["pad_label"] == "J2-1"
    assert summary["pins"][1]["pad_label"] == "J3-2"


def test_unlock_seeds_raw_pads_and_des_lists():
    """Unlock helpers: raw pads + DES from pin components (not host-only)."""
    from fypa.altium_viewer import PdnViewer

    host = "J2"
    p_term = {
        "pins": [
            {"pad": "1", "component": "J2", "pad_label": "J2-1", "net": "VIN"},
        ],
    }
    n_term = {
        "pins": [
            {"pad": "2", "component": "J3", "pad_label": "J3-2", "net": "GND"},
            {"pad": "2", "component": "J5", "pad_label": "J5-2", "net": "GND"},
            {"pad": "2", "component": "J7", "pad_label": "J7-2", "net": "GND"},
        ],
    }
    assert PdnViewer._terminal_pin_pads(p_term) == ["1"]
    assert PdnViewer._terminal_pin_pads(n_term) == ["2"]
    assert PdnViewer._terminal_des_list(p_term, host) == []  # host-only
    assert PdnViewer._terminal_des_list(n_term, host) == ["J3", "J5", "J7"]


def test_unlock_strips_legacy_compound_pad():
    from fypa.altium_viewer import PdnViewer

    term = {
        "pins": [
            {"pad": "J2-1", "component": "J2", "net": "VIN"},
        ],
    }
    assert PdnViewer._terminal_pin_pads(term) == ["1"]

# ---------------------------------------------------------------------------
# has_closed_pdn_loop — the read-only "would the solver accept this?" check
# behind the viewer's greyed-out ↻ Solve button.
# ---------------------------------------------------------------------------

from fypa.altium.annotations import TerminalPin, TerminalSpec  # noqa: E402
from fypa.editor_directives import has_closed_pdn_loop  # noqa: E402


def _term(net_index: int) -> TerminalSpec:
    pin = TerminalPin(pad_designator="1", layer_id=1, net_index=net_index,
                      point=(0.0, 0.0))
    return TerminalSpec(pins=(pin,))


def _sch_source(net_index: int, designator: str = "J1") -> SourceSpec:
    return SourceSpec(designator=designator, schdoc_name="a.SchDoc",
                      voltage=5.0, p=_term(net_index), n=None)


def _sch_sink(net_index: int, designator: str = "U1") -> SinkSpec:
    return SinkSpec(designator=designator, schdoc_name="a.SchDoc",
                    current=1.0, p=_term(net_index), n=None)


def test_closed_loop_false_with_no_directives():
    assert has_closed_pdn_loop(_loaded(["+5V"]), []) is False


def test_closed_loop_false_with_no_copper_layers():
    loaded = _loaded(["+5V"])
    loaded.extracted.enabled_copper_layer_ids = list
    eds = [_free("SOURCE", p_net="+5V", voltage=5.0),
           _free("SINK", p_net="+5V", current=1.0)]
    assert has_closed_pdn_loop(loaded, eds) is False


def test_closed_loop_false_with_source_only():
    eds = [_free("SOURCE", p_net="+5V", voltage=5.0)]
    assert has_closed_pdn_loop(_loaded(["+5V"]), eds) is False


def test_closed_loop_true_with_editor_source_and_sink_on_same_net():
    eds = [_free("SOURCE", p_net="+5V", voltage=5.0),
           _free("SINK", p_net="+5v", current=1.0)]   # case-insensitive
    assert has_closed_pdn_loop(_loaded(["+5V"]), eds) is True


def test_closed_loop_false_when_source_and_sink_are_on_different_rails():
    eds = [_free("SOURCE", p_net="+5V", voltage=5.0),
           _free("SINK", p_net="+3V3", current=1.0)]
    assert has_closed_pdn_loop(_loaded(["+5V", "+3V3"]), eds) is False


def test_series_bridge_closes_the_loop_across_two_nets():
    eds = [_free("SOURCE", p_net="+5V", voltage=5.0),
           _free("SINK", p_net="+3V3", current=1.0),
           _free("SERIES", single_net=False, p_net="+5V", n_net="+3V3",
                 resistance=0.05)]
    assert has_closed_pdn_loop(_loaded(["+5V", "+3V3"]), eds) is True


def test_series_without_positive_resistance_does_not_bridge():
    for r in (None, 0.0, -1.0):
        eds = [_free("SOURCE", p_net="+5V", voltage=5.0),
               _free("SINK", p_net="+3V3", current=1.0),
               _free("SERIES", single_net=False, p_net="+5V", n_net="+3V3",
                     resistance=r)]
        assert has_closed_pdn_loop(_loaded(["+5V", "+3V3"]), eds) is False


def test_editor_directives_missing_their_value_are_ignored():
    loaded = _loaded(["+5V"])
    assert has_closed_pdn_loop(loaded, [
        _free("SOURCE", p_net="+5V", voltage=None),
        _free("SINK", p_net="+5V", current=1.0),
    ]) is False
    assert has_closed_pdn_loop(loaded, [
        _free("SOURCE", p_net="+5V", voltage=5.0),
        _free("SINK", p_net="+5V", current=None),
    ]) is False


def test_editor_regulator_role_is_not_counted_as_a_source():
    # apply_editor_directives skips REGULATOR, so the check must too.
    eds = [_free("REGULATOR", p_net="+5V", voltage=5.0),
           _free("SINK", p_net="+5V", current=1.0)]
    assert has_closed_pdn_loop(_loaded(["+5V"]), eds) is False


def test_schematic_source_plus_editor_sink_closes_the_loop():
    loaded = _loaded(["+5V"])
    loaded.annotations.directives.append(_sch_source(0))
    eds = [_free("SINK", p_net="+5V", current=1.0)]
    assert has_closed_pdn_loop(loaded, eds) is True


def test_schematic_source_and_sink_alone_close_the_loop():
    loaded = _loaded(["+5V"])
    loaded.annotations.directives += [_sch_source(0), _sch_sink(0)]
    assert has_closed_pdn_loop(loaded, []) is True


def test_schematic_directive_overridden_by_editor_is_dropped():
    loaded = _loaded(["+5V"])
    loaded.annotations.directives += [_sch_source(0, "J1"), _sch_sink(0)]
    # Editor SINK takes over J1 — the schematic SOURCE on J1 no longer counts,
    # leaving two sinks and no source.
    eds = [EditorDirective(kind="component", role="SINK", designator="J1",
                           p_net="+5V", current=1.0,
                           overrides_designator="J1")]
    assert has_closed_pdn_loop(loaded, eds) is False


def test_unnamed_copper_terminal_is_unresolved_until_named():
    loaded = _loaded(["+5V"])
    eds = [_free("SOURCE", p_net="(none)", voltage=5.0),
           _free("SINK", p_net="+5V", current=1.0)]
    assert has_closed_pdn_loop(loaded, eds) is False
    # A CopperName-style resolver can promote the free marker.
    assert has_closed_pdn_loop(
        loaded, eds, p_net_resolver=lambda ed: "+5V") is True
    # A failing resolver is best-effort, not fatal.
    def _boom(ed):
        raise RuntimeError("no geometry")
    assert has_closed_pdn_loop(loaded, eds, p_net_resolver=_boom) is False


def test_two_net_editor_directive_with_unnamed_n_net_is_unresolved():
    loaded = _loaded(["+5V", "GND"])
    eds = [_free("SOURCE", single_net=False, p_net="+5V", n_net="(none)",
                 voltage=5.0),
           _free("SINK", p_net="+5V", current=1.0)]
    assert has_closed_pdn_loop(loaded, eds) is False
