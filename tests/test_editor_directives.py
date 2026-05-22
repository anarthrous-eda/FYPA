"""Editor-directive → solver-spec conversion tests.

``apply_editor_directives`` turns the ``.fypa`` project's hand-placed
editor directives into real ``SourceSpec`` / ``SinkSpec`` / ``ResistorSpec``
entries before a re-solve. These exercise the SERIES path with lightweight
stand-ins for the Altium extraction — a free marker anchors directly on a
named net, so no real board / pad geometry is needed.
"""
from __future__ import annotations

from types import SimpleNamespace

from fypa.altium_annotations import (
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
