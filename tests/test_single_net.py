"""Single-net (PDN_NET) analysis - solver-level tests.

A point-to-point check models one net with an ideal 0 Ω return: a SOURCE and
a SINK both land on the same copper and share one virtual return node, so the
current loop closes. The two directives are emitted as *separate* Networks
that share a NodeID - exactly what ``altium_loader`` builds for PDN_NET
directives. See ``altium_annotations.py`` for the annotation schema.
"""
from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Point

from pdnsolver import problem as P
from pdnsolver import solver as S

# Copper conductivity used throughout FYPA; 1 oz finished copper ~= 0.035 mm.
COPPER_CONDUCTIVITY_S_PER_MM = 5.95e4
DEFAULT_THICKNESS_MM = 0.035


def _strip_layer(length_mm: float, width_mm: float) -> P.Layer:
    rect = shapely.box(0.0, 0.0, length_mm, width_mm)
    conductance = COPPER_CONDUCTIVITY_S_PER_MM * DEFAULT_THICKNESS_MM
    return P.Layer(shape=MultiPolygon([rect]), name="strip",
                   conductance=conductance)


def _single_net_problem(length_mm: float = 40.0, width_mm: float = 6.0,
                        voltage: float = 5.0, current: float = 2.0,
                        inset_mm: float = 1.0) -> P.Problem:
    """A SOURCE and a SINK on one copper strip sharing an ideal return node.

    The SOURCE's N terminal and the SINK's return terminal are the *same*
    ``NodeID`` reused across two Networks - the way the loader closes a
    point-to-point loop for single-net directives.
    """
    layer = _strip_layer(length_mm, width_mm)
    ref = P.NodeID()  # shared ideal 0 Ω return - no copper connection

    src_p = P.NodeID()
    src_net = P.Network(
        connections=[P.Connection(
            layer=layer, point=Point(inset_mm, width_mm / 2.0),
            node_id=src_p)],
        elements=[P.VoltageSource(p=src_p, n=ref, voltage=voltage)],
    )

    snk_f = P.NodeID()
    snk_net = P.Network(
        connections=[P.Connection(
            layer=layer, point=Point(length_mm - inset_mm, width_mm / 2.0),
            node_id=snk_f)],
        elements=[P.CurrentSource(f=snk_f, t=ref, current=current)],
    )
    return P.Problem(layers=[layer], networks=[src_net, snk_net],
                     project_name="single-net-test")


def test_single_net_loop_closes_and_is_balanced():
    """A shared-return SOURCE+SINK pair solves cleanly: the loop closes
    through the ideal return, so the reference node sources ~0 net current."""
    solution = S.solve(_single_net_problem())
    info = solution.solver_info
    assert info.residual_norm < 1e-7
    assert abs(info.ground_node_current) < 1e-9


def test_single_net_produces_ir_drop():
    """Current from the SOURCE pad to the SINK pad develops a real, finite
    IR drop along the strip, and nowhere exceeds the source voltage."""
    voltage = 5.0
    solution = S.solve(_single_net_problem(voltage=voltage, current=2.0))
    pots = np.asarray(
        solution.layer_solutions[0].potentials[0].values, dtype=np.float64)
    assert np.all(np.isfinite(pots))
    spread = float(pots.max() - pots.min())
    # 2 A through a 40 x 6 mm strip - a few mV of drop.
    assert 1e-4 < spread < 5e-2, f"implausible drop: {spread:.4e} V"
    # The SOURCE holds its pad at `voltage` above the 0 V ideal return; every
    # copper vertex sits at or below it, and the source pad is the maximum.
    assert pots.max() <= voltage + 1e-6
    assert abs(pots.max() - voltage) < 1e-3


def test_single_net_coexists_with_a_normal_analysis():
    """A single-net analysis and an ordinary two-net analysis on electrically
    isolated copper solve together - each isolated subsystem gets its own
    voltage reference, so neither floats."""
    # Subsystem A: single-net strip (SOURCE + SINK sharing an ideal return).
    single = _single_net_problem(voltage=5.0, current=2.0)

    # Subsystem B: a normal rail/return pair - two separate copper strips,
    # a VoltageSource bridging them and a CurrentSource loading them.
    rail = _strip_layer(30.0, 5.0)
    gnd = P.Layer(shape=MultiPolygon([shapely.box(0.0, 10.0, 30.0, 15.0)]),
                  name="gnd", conductance=rail.conductance)
    vp, vn, sf, st = (P.NodeID() for _ in range(4))
    vsrc_net = P.Network(
        connections=[
            P.Connection(layer=rail, point=Point(1.0, 2.5), node_id=vp),
            P.Connection(layer=gnd, point=Point(1.0, 12.5), node_id=vn),
        ],
        elements=[P.VoltageSource(p=vp, n=vn, voltage=3.3)],
    )
    isink_net = P.Network(
        connections=[
            P.Connection(layer=rail, point=Point(29.0, 2.5), node_id=sf),
            P.Connection(layer=gnd, point=Point(29.0, 12.5), node_id=st),
        ],
        elements=[P.CurrentSource(f=sf, t=st, current=1.0)],
    )

    combined = P.Problem(
        layers=[*single.layers, rail, gnd],
        networks=[*single.networks, vsrc_net, isink_net],
        project_name="single-net-coexist",
    )
    solution = S.solve(combined)
    # Both subsystems are referenced, so the system is non-singular and
    # balanced - a floating island would blow up the residual.
    assert solution.solver_info.residual_norm < 1e-6
    assert abs(solution.solver_info.ground_node_current) < 1e-6
    for ls in solution.layer_solutions:
        for zf in ls.potentials:
            assert np.all(np.isfinite(np.asarray(zf.values, dtype=np.float64)))
