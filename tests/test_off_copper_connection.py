"""Off-copper / mesh-less connection handling in ``NodeIndexer.create``.

Two failure modes used to lurk when a directive terminal did not land on
copper reachable from a source:

* If the terminal's *layer* had no meshed copper at all, ``NodeIndexer.create``
  indexed ``layer_to_kdtree[layer_i]`` directly and raised **KeyError**,
  aborting the entire solve with an unhelpful traceback.
* If the layer *was* meshed but the point sat well off the copper, the nearest
  vertex was accepted with no distance check, so the terminal's current was
  injected at a possibly-distant vertex — silently skewing IR-drop with no
  warning.

The solve now degrades gracefully: a mesh-less layer skips the connection with
a ``SolverWarning`` (the node is left unattached rather than crashing), and an
off-copper point still attaches but warns. These tests lock both in, and check
that an ordinary on-copper solve stays silent (no false positives).
"""
from __future__ import annotations

import warnings

import shapely
from shapely.geometry import MultiPolygon, Point

from pdnsolver import problem as P
from pdnsolver import solver as S

COPPER_CONDUCTIVITY_S_PER_MM = 5.95e4
DEFAULT_THICKNESS_MM = 0.035
_CU = COPPER_CONDUCTIVITY_S_PER_MM * DEFAULT_THICKNESS_MM


def _strip(x0: float, y0: float, x1: float, y1: float, name: str) -> P.Layer:
    return P.Layer(shape=MultiPolygon([shapely.box(x0, y0, x1, y1)]),
                   name=name, conductance=_CU)


def _driven_rail_gnd():
    """A normal, well-posed rail/return pair that meshes and solves cleanly."""
    rail = _strip(0.0, 0.0, 30.0, 5.0, "rail")
    gnd = _strip(0.0, 10.0, 30.0, 15.0, "gnd")
    vp, vn, sf, st = (P.NodeID() for _ in range(4))
    vsrc = P.Network(
        connections=[P.Connection(layer=rail, point=Point(1.0, 2.5), node_id=vp),
                     P.Connection(layer=gnd, point=Point(1.0, 12.5), node_id=vn)],
        elements=[P.VoltageSource(p=vp, n=vn, voltage=3.3)],
    )
    isink = P.Network(
        connections=[P.Connection(layer=rail, point=Point(29.0, 2.5), node_id=sf),
                     P.Connection(layer=gnd, point=Point(29.0, 12.5), node_id=st)],
        elements=[P.CurrentSource(f=sf, t=st, current=1.0)],
    )
    return rail, gnd, st, [vsrc, isink]


def _solver_warnings(prob: P.Problem) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Must not raise — that is the crash this fix prevents.
        S.solve(prob)
    return [str(w.message) for w in caught
            if issubclass(w.category, S.SolverWarning)]


def test_meshless_layer_connection_does_not_crash():
    """A terminal on a layer with no meshed copper warns instead of KeyError."""
    rail, gnd, st, nets = _driven_rail_gnd()
    # An isolated layer whose copper never connects to a source, with the
    # terminal placed off even its own copper — nothing meshes it, so it has
    # no KD-tree. This used to raise KeyError out of NodeIndexer.create.
    orphan = _strip(100.0, 100.0, 110.0, 105.0, "orphan")
    oa = P.NodeID()
    orphan_net = P.Network(
        connections=[P.Connection(layer=orphan, point=Point(500.0, 500.0),
                                  node_id=oa)],
        elements=[P.CurrentSource(f=oa, t=st, current=0.5)],
    )
    prob = P.Problem(layers=[rail, gnd, orphan],
                     networks=[*nets, orphan_net],
                     project_name="meshless-layer")

    msgs = _solver_warnings(prob)  # asserts no exception
    assert any("no meshed copper" in m for m in msgs), msgs


def test_off_copper_point_warns_but_solves():
    """A terminal far off its net's copper still attaches, but warns."""
    rail, gnd, st, nets = _driven_rail_gnd()
    sf2 = P.NodeID()
    # On the rail *layer* but ~35 mm off the rail copper (y in [0, 5]).
    extra = P.Network(
        connections=[P.Connection(layer=rail, point=Point(15.0, 40.0),
                                  node_id=sf2)],
        elements=[P.CurrentSource(f=sf2, t=st, current=0.2)],
    )
    prob = P.Problem(layers=[rail, gnd], networks=[*nets, extra],
                     project_name="off-copper-point")

    msgs = _solver_warnings(prob)
    assert any("from the nearest copper vertex" in m for m in msgs), msgs


def test_on_copper_solve_emits_no_off_copper_warning():
    """A well-posed, fully on-copper solve raises neither off-copper warning."""
    rail, gnd, st, nets = _driven_rail_gnd()
    prob = P.Problem(layers=[rail, gnd], networks=nets,
                     project_name="on-copper")

    msgs = _solver_warnings(prob)
    assert not any("no meshed copper" in m for m in msgs), msgs
    assert not any("from the nearest copper vertex" in m for m in msgs), msgs
