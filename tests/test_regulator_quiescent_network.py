"""Quiescent-current lumped-element construction for REGULATOR directives.

Parsing of ``PDN_QUIESCENT`` is covered in ``test_annotations``; here we check
that the solver-level network actually gains a constant ``CurrentSource`` on
the regulator's input (sense) nodes, drawing current in the same sense as a
SINK on the input rail.
"""
from __future__ import annotations

import shapely.geometry

from pdnsolver import problem as _pp

from fypa.altium.annotations import RegulatorSpec, TerminalPin, TerminalSpec
from fypa.altium.extract import Pt2D
from fypa.altium.loader import _directive_to_network


def _unit_layer(name: str) -> _pp.Layer:
    poly = shapely.geometry.Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    return _pp.Layer(
        shape=shapely.geometry.MultiPolygon([poly]),
        name=name,
        conductance=1.0,
    )


def _one_pin_terminal(layer_id: int, net_index: int, x: float) -> TerminalSpec:
    return TerminalSpec(pins=(
        TerminalPin(
            pad_designator=str(net_index),
            layer_id=layer_id,
            net_index=net_index,
            point=Pt2D(x, 0.5),
        ),
    ))


def _regulator(quiescent: float) -> tuple[RegulatorSpec, dict]:
    # Four distinct (layer_id, net_index) pairs, each with its own padne Layer.
    layer_map = {
        (0, 10): _unit_layer("L|OUT_P"),
        (0, 11): _unit_layer("L|OUT_N"),
        (0, 12): _unit_layer("L|IN_P"),
        (0, 13): _unit_layer("L|IN_N"),
    }
    reg = RegulatorSpec(
        designator="U2",
        schdoc_name="Pwr.SchDoc",
        voltage=3.3,
        gain=1.0,
        out_p=_one_pin_terminal(0, 10, 0.2),
        out_n=_one_pin_terminal(0, 11, 0.4),
        in_p=_one_pin_terminal(0, 12, 0.6),
        in_n=_one_pin_terminal(0, 13, 0.8),
        regulator_type="LDO",
        efficiency=1.0,
        quiescent_current=quiescent,
    )
    return reg, layer_map


def test_quiescent_adds_current_source_on_input_sense_nodes():
    reg, layer_map = _regulator(quiescent=0.005)
    net = _directive_to_network(reg, layer_map)
    assert net is not None

    regulators = [e for e in net.elements if isinstance(e, _pp.VoltageRegulator)]
    sources = [e for e in net.elements if isinstance(e, _pp.CurrentSource)]
    assert len(regulators) == 1
    assert len(sources) == 1

    vreg = regulators[0]
    cs = sources[0]
    assert cs.current == 0.005
    # Drawn between the regulator's input sense nodes, same orientation as a
    # SINK (from the positive input toward the negative input).
    assert cs.f is vreg.s_f
    assert cs.t is vreg.s_t


def test_zero_quiescent_adds_no_current_source():
    reg, layer_map = _regulator(quiescent=0.0)
    net = _directive_to_network(reg, layer_map)
    assert net is not None
    assert not any(isinstance(e, _pp.CurrentSource) for e in net.elements)
    assert sum(isinstance(e, _pp.VoltageRegulator) for e in net.elements) == 1
