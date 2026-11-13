"""Editor-mode REGULATOR directives → RegulatorSpec (and round trip)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from fypa.altium.annotations import AnnotationResult, RegulatorSpec, SourceSpec
from fypa.altium.extract import Pt2D
from fypa.editor_directives import apply_editor_directives, has_closed_pdn_loop
from fypa.project_file import EditorDirective

NETS = ["GND", "+12V", "+3V3", "+1V8"]


def _pad(ci: int, net: str, pad: str, x: float):
    return SimpleNamespace(component_index=ci, net_index=NETS.index(net),
                           designator=pad, center=Pt2D(x, 0), layer_id=1,
                           is_through_hole=False)


def _loaded():
    """J1 brings in 12 V; U1 makes 3.3 V from it; U2 makes 1.8 V from 3.3 V."""
    comps = [SimpleNamespace(designator=d) for d in ("J1", "U1", "U2", "U3")]
    pads = [
        _pad(0, "+12V", "1", 0), _pad(0, "GND", "2", 1),
        _pad(1, "+12V", "1", 10), _pad(1, "+3V3", "2", 11), _pad(1, "GND", "3", 12),
        _pad(2, "+3V3", "1", 20), _pad(2, "+1V8", "2", 21), _pad(2, "GND", "3", 22),
        _pad(3, "+1V8", "1", 30), _pad(3, "GND", "2", 31),
    ]
    extracted = SimpleNamespace(
        nets=[SimpleNamespace(name=n) for n in NETS],
        pcb_components=comps, pads=pads,
        enabled_copper_layer_ids=lambda: [1],
    )
    return SimpleNamespace(extracted=extracted, annotations=AnnotationResult())


def _reg(des, out, inn, v, **kw) -> EditorDirective:
    return EditorDirective(kind="component", role="REGULATOR", designator=des,
                           single_net=False, p_net=out, n_net="GND",
                           in_p_net=inn, voltage=v, **kw)


def _source() -> EditorDirective:
    return EditorDirective(kind="component", role="SOURCE", designator="J1",
                           single_net=False, p_net="+12V", n_net="GND", voltage=12.0)


def _specs(loaded, cls):
    return [d for d in loaded.annotations.directives if isinstance(d, cls)]


def test_ldo_regulator_becomes_regulator_spec_with_unit_gain() -> None:
    loaded = _loaded()
    warn = apply_editor_directives(
        loaded, [_source(), _reg("U2", "+1V8", "+3V3", 1.8, regulator_type="LDO")])
    (reg,) = _specs(loaded, RegulatorSpec)
    assert reg.designator == "U2" and reg.voltage == 1.8
    assert reg.regulator_type == "LDO" and reg.gain == 1.0
    assert not reg.adaptive_gain_eligible
    assert {p.pad_designator for p in reg.out_p.pins} == {"2"}
    assert {p.pad_designator for p in reg.in_p.pins} == {"1"}
    assert {p.pad_designator for p in reg.in_n.pins} == {"3"}
    assert not warn


def test_smps_gain_uses_the_source_voltage_on_its_input() -> None:
    loaded = _loaded()
    apply_editor_directives(loaded, [
        _source(),
        _reg("U1", "+3V3", "+12V", 3.3, regulator_type="SMPS", efficiency=0.9),
    ])
    (reg,) = _specs(loaded, RegulatorSpec)
    assert reg.gain == pytest.approx(3.3 / (12.0 * 0.9))
    assert reg.efficiency == 0.9 and reg.adaptive_gain_eligible


def test_cascaded_editor_regulators_resolve_in_any_order() -> None:
    loaded = _loaded()
    # U2 (fed by U1's output) listed first.
    apply_editor_directives(loaded, [
        _reg("U2", "+1V8", "+3V3", 1.8, regulator_type="SMPS", efficiency=1.0),
        _reg("U1", "+3V3", "+12V", 3.3, regulator_type="SMPS", efficiency=1.0),
        _source(),
    ])
    gains = {r.designator: r.gain for r in _specs(loaded, RegulatorSpec)}
    assert gains["U2"] == pytest.approx(1.8 / 3.3)
    assert gains["U1"] == pytest.approx(3.3 / 12.0)


def test_smps_with_undriven_input_warns_and_starts_from_one_over_eta() -> None:
    loaded = _loaded()
    warn = apply_editor_directives(
        loaded, [_reg("U1", "+3V3", "+12V", 3.3, regulator_type="SMPS",
                      efficiency=0.8)])
    (reg,) = _specs(loaded, RegulatorSpec)
    assert reg.gain == pytest.approx(1 / 0.8)
    assert any("nothing drives the input" in w for w in warn)


@pytest.mark.parametrize("kw, msg", [
    ({"voltage": None}, "no output voltage"),
    ({"in_p_net": None}, "IN net is not set"),
    ({"in_p_net": "+3V3"}, "both on"),
    ({"kind": "free"}, "must be bound to a component"),
    ({"in_p_net": "NOPE"}, "not found on the board"),
])
def test_bad_regulators_are_skipped_with_a_reason(kw, msg) -> None:
    ed = _reg("U1", "+3V3", "+12V", 3.3, regulator_type="LDO")
    for k, v in kw.items():
        setattr(ed, k, v)
    loaded = _loaded()
    warn = apply_editor_directives(loaded, [ed])
    assert not _specs(loaded, RegulatorSpec)
    assert any(msg in w for w in warn), warn


def test_regulator_closes_the_loop_between_its_rails() -> None:
    sink = EditorDirective(kind="component", role="SINK", designator="U3",
                           p_net="+1V8", current=0.5)
    reg = _reg("U2", "+1V8", "+3V3", 1.8, regulator_type="LDO")
    assert has_closed_pdn_loop(_loaded(), [sink, reg]) is True
    assert has_closed_pdn_loop(_loaded(), [sink]) is False


def test_regulator_fields_round_trip_through_the_project_file() -> None:
    ed = _reg("U1", "+3V3", "+12V", 3.3, regulator_type="smps", efficiency=0.92,
              smps_topology="buck", in_p_pins=["1"], origin="autosetup")
    back = EditorDirective.from_dict(ed.to_dict())
    assert back.in_p_net == "+12V" and back.in_p_pins == ["1"]
    assert back.regulator_type == "SMPS" and back.smps_topology == "BUCK"
    assert back.efficiency == 0.92 and back.origin == "autosetup"


def test_editor_regulator_reaches_the_topology_preview() -> None:
    from fypa.topology.preview import _editor_directive_to_metadata_dict

    d = _editor_directive_to_metadata_dict(
        _reg("U1", "+3V3", "+12V", 3.3, regulator_type="SMPS", smps_topology="BUCK"))
    assert d["role"] == "REGULATOR"
    assert d["terminals"]["IN_P"]["requested_net"] == "+12V"
    assert d["terminals"]["OUT_P"]["requested_net"] == "+3V3"


def test_directives_sharing_a_part_become_its_channels() -> None:
    from fypa.altium.annotations import SinkSpec
    from fypa.altium.loader import directive_label

    loaded = _loaded()
    sinks = [EditorDirective(kind="component", role="SINK", designator="U2",
                             p_net=net, current=0.1) for net in ("+3V3", "+1V8")]
    apply_editor_directives(loaded, [_source(), *sinks])
    labels = sorted(directive_label(d) for d in _specs(loaded, SinkSpec))
    assert labels == ["U2#1", "U2#2"]
    # A part with one directive keeps its plain label.
    (src,) = _specs(loaded, SourceSpec)
    assert directive_label(src) == "J1"


def test_editor_directive_on_a_schematic_part_numbers_after_it() -> None:
    from fypa.altium.annotations import SinkSpec
    from fypa.altium.loader import directive_label

    loaded = _loaded()
    pin = SimpleNamespace(net_index=3, pad_designator="1")
    loaded.annotations.directives.append(SinkSpec(
        designator="U3", schdoc_name="a.SchDoc", current=1.0,
        p=SimpleNamespace(pins=(pin,), requested_net="+1V8"), n=None))
    extra = EditorDirective(kind="component", role="SINK", designator="U3",
                            p_net="GND", current=0.1)
    apply_editor_directives(loaded, [extra])
    labels = sorted(directive_label(d) for d in _specs(loaded, SinkSpec))
    assert labels == ["U3", "U3#1"]


def test_schematic_source_counts_as_supply_for_editor_smps() -> None:
    loaded = _loaded()
    pin = SimpleNamespace(net_index=1, pad_designator="1")
    term = SimpleNamespace(pins=(pin,), requested_net="+12V")
    loaded.annotations.directives.append(
        SourceSpec(designator="J9", schdoc_name="a.SchDoc", voltage=24.0,
                   p=term, n=None))
    apply_editor_directives(loaded, [
        _reg("U1", "+3V3", "+12V", 3.3, regulator_type="SMPS", efficiency=1.0)])
    (reg,) = _specs(loaded, RegulatorSpec)
    assert reg.gain == pytest.approx(3.3 / 24.0)
