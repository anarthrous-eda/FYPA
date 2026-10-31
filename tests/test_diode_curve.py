# Copyright (c) 2026 Tomachie LLC.
"""A series diode solved on its datasheet's Vf-vs-If curve (fypa.altium.diode_curve)."""

import copy
from dataclasses import dataclass, field

import pytest

from fypa.altium import diode_curve as dc
from fypa.altium.annotations import AnnotationResult, ResistorSpec, _resistor_spec_factory
from pdnsolver import problem as pp
from test_tomachie_pdn import PDN, params

# An SK510A's 25 C curve: far from a straight line over its range.
CURVE = ((0.01, 0.3484), (0.05, 0.3968), (0.1, 0.4264), (0.5, 0.5341), (1.0, 0.6083),
         (2.0, 0.6923), (5.0, 0.7959), (10.0, 0.8936), (20.0, 1.038))


def test_curve_volts_and_its_tangent():
    assert dc.curve_volts(CURVE, 1.0) == pytest.approx(0.6083)
    vf, r = dc.tangent(CURVE, 0.08)
    assert vf + r * 0.08 == pytest.approx(dc.curve_volts(CURVE, 0.08))
    assert 0.1 < r < 2.0                       # the diode's own slope at 80 mA


@dataclass
class _Pin:
    name: str


@dataclass
class _Term:
    pins: list


@dataclass
class _Annotations:
    directives: list = field(default_factory=list)


@dataclass
class _Loaded:
    annotations: _Annotations


def _diode(vf=0.684, r=0.0184):
    return ResistorSpec(designator="D2", schdoc_name="", resistance=r,
                        p=_Term([_Pin("A")]), n=_Term([_Pin("K")]), forward_v=vf,
                        vf_curve=CURVE)


def _network(spec):
    a, b = pp.NodeID(), pp.NodeID()
    return pp.Network(connections=[], elements=[
        pp.Resistor(a=a, b=b, resistance=spec.resistance),
        pp.CurrentSource(f=b, t=a, current=spec.forward_v / spec.resistance)])


def test_the_line_moves_onto_the_curve_at_the_current_the_diode_carries():
    """80 mA through D2 (the load's current, whatever the drop): the fitted line from
    2 A to 20 A puts 0.686 V across it; on the curve it is 0.415 V."""
    amps = 0.08
    loaded = _Loaded(_Annotations([_diode()]))
    net = _network(loaded.annotations.directives[0])

    def sample(_solution, ld, pin):
        d = ld.annotations.directives[0]
        return 12.0 if pin.name == "A" else 12.0 - (d.forward_v + d.resistance * amps)

    gaps = []
    for _ in range(6):
        gap, solved = dc.retune(None, loaded, {"D2": net}, lambda d: d.designator, sample)
        gaps.append(gap)
        if gap < dc.CURVE_TOL_V:
            break
    d = loaded.annotations.directives[0]
    assert gaps[0] == pytest.approx(0.686 - 0.4155, abs=2e-3)
    assert gaps[-1] < dc.CURVE_TOL_V and len(gaps) <= 3
    assert d.forward_v + d.resistance * amps == pytest.approx(dc.curve_volts(CURVE, amps),
                                                              abs=1e-4)
    assert solved["D2"][0] == pytest.approx(amps)
    # The built network carries the new line: its resistor and Vf / R current source.
    assert net.elements[0].resistance == d.resistance
    assert net.elements[1].current == pytest.approx(d.forward_v / d.resistance)


def test_a_diode_carrying_nothing_is_left_as_it_is():
    loaded = _Loaded(_Annotations([_diode()]))
    before = loaded.annotations.directives[0]
    gap, solved = dc.retune(None, loaded, {"D2": _network(before)}, lambda d: d.designator,
                            lambda _s, _l, _p: 5.0)
    assert (gap, solved, loaded.annotations.directives[0]) == (0.0, {}, before)


def _factory(value_params):
    result = AnnotationResult()
    spec = _resistor_spec_factory(params=value_params, value=0.0184, result=result,
                                  diag="D2", designator="D2", schdoc_name="",
                                  p=_Term([]), n=_Term([]), channel_index=1)
    return spec, result


def test_the_series_directive_carries_the_curve():
    spec, result = _factory({"PDN1_VF": "0.684", "PDN1_VF_CURVE": "0.01:0.3484;1:0.6083"})
    assert spec.vf_curve == ((0.01, 0.3484), (1.0, 0.6083)) and not result.errors
    spec, result = _factory({"PDN1_VF": "0.684", "PDN1_VF_CURVE": "0.01:0.3484"})
    assert spec.vf_curve is None and result.errors


def test_the_reader_writes_the_curve_lifted_in_the_worst_case():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][2]["series_from_parent"].append({
        "refdes": "D1", "class": "diode", "net_in": "+3V3", "net_out": "+3V3A",
        "resistance_ohm": "unknown",
        "datasheet_figures": [{"spec": "v_forward", "max": "0.85", "unit": "V",
                               "if": "5A"}],
        "vf_curves": [{"label": "TJ=125°C", "points": [[0.1, 0.2], [5, 0.6]]},
                      {"label": "TJ=25°C", "points": [list(p) for p in CURVE]}]})
    p, notes = params(pdn=pdn)
    pts = [tuple(map(float, x.split(":"))) for x in p["D1"]["PDN1_VF_CURVE"].split(";")]
    assert pts == [pytest.approx(x) for x in CURVE]
    worst, _ = params("max", pdn=pdn)
    lift = float(worst["D1"]["PDN1_VF"]) - float(p["D1"]["PDN1_VF"])
    pts = [tuple(map(float, x.split(":")))
           for x in worst["D1"]["PDN1_VF_CURVE"].split(";")]
    assert lift > 0 and pts[4][1] == pytest.approx(CURVE[4][1] + lift, abs=1e-4)
    assert any("solved on its Vf-vs-If curve (TJ=25°C)" in n for n in notes)
