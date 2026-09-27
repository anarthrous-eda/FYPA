"""Copper ROI: the fixes it suggests, checked against actually making them.

A 1 V source feeds a 1 mm trace on Top to a 5 A load, returning through a
GND plane. The analysis must point at the trace, and its estimates must
match what re-solving with the fix applied really gains.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, box

from fypa import copper_roi as R
from fypa.lean_solution import to_lean_solution
from pdnsolver import problem as P
from pdnsolver import solver as S

_CU = 5.95e4 * 0.035
_W = 1.0          # trace width, mm
_LEN = 40.0
_PAD = 0.8


def _pad(layer, x, y, node):
    return P.Connection(layer=layer, point=Point(x, y), node_id=node,
                        region=box(x - _PAD / 2, y - _PAD / 2,
                                   x + _PAD / 2, y + _PAD / 2))


def _solve(trace_top=_W, parallel=False):
    """Solve the board. ``trace_top`` moves the trace's upper edge (widening
    it on one side); ``parallel`` adds a stitched copy of the trace on Mid."""
    top = P.Layer(shape=MultiPolygon([box(0, 0, _LEN, trace_top)]),
                  name="Top|+1V", conductance=_CU)
    gnd = P.Layer(shape=MultiPolygon([box(0, -6, _LEN, 6)]),
                  name="Bottom|GND", conductance=_CU)
    layers = [top, gnd]
    nets = []
    sp, sn = P.NodeID(), P.NodeID()
    nets.append(P.Network(
        connections=[_pad(top, 0.5, _W / 2, sp), _pad(gnd, 0.5, -3, sn)],
        elements=[P.VoltageSource(p=sp, n=sn, voltage=1.0)]))
    f, t = P.NodeID(), P.NodeID()
    sink = P.CurrentSource(f=f, t=t, current=5.0)
    nets.append(P.Network(
        connections=[_pad(top, _LEN - 0.5, _W / 2, f),
                     _pad(gnd, _LEN - 0.5, -3, t)],
        elements=[sink]))
    if parallel:
        mid = P.Layer(shape=MultiPolygon([box(0, 0, _LEN, _W)]),
                      name="Mid|+1V", conductance=_CU)
        layers.append(mid)
        for x in (1.5, _LEN - 1.5):
            a, b = P.NodeID(), P.NodeID()
            nets.append(P.Network(
                connections=[P.Connection(layer=top, point=Point(x, _W / 2),
                                          node_id=a),
                             P.Connection(layer=mid, point=Point(x, _W / 2),
                                          node_id=b)],
                elements=[P.Resistor(a=a, b=b, resistance=1e-5)]))
    prob = P.Problem(layers=layers, networks=nets, project_name="roi",
                     sensitivity_targets=(("U1", sink),))
    return to_lean_solution(S.solve(prob))


_META = {"stackup": [{"name": "Top", "layer_id": 1},
                     {"name": "Mid", "layer_id": 2},
                     {"name": "Bottom", "layer_id": 3}]}


def _load_v(lean):
    return lean.sensitivity["U1"]["objective_v"]


@pytest.fixture(scope="module")
def base():
    lean = _solve()
    return lean, R.analyse(lean, _META, "U1")


def test_the_trace_is_the_top_suggestion(base):
    _, res = base
    top = res.opportunities[0]
    assert top.net == "+1V" and top.layer == "Top"


def test_widen_estimate_matches_actually_widening(base):
    lean, res = base
    widen = next(o for o in res.opportunities
                 if o.kind == "widen" and o.net == "+1V")
    actual = _load_v(_solve(trace_top=_W + R.WIDEN_STEP_MM)) - _load_v(lean)
    assert actual > 0
    assert widen.value_v == pytest.approx(actual, rel=0.2)


def test_parallel_estimate_matches_adding_the_layer(base):
    lean, res = base
    par = next(o for o in res.opportunities
               if o.kind == "parallel" and o.net == "+1V")
    actual = _load_v(_solve(parallel=True)) - _load_v(lean)
    assert actual > 0
    # The region is a grid approximation of where the copper pays off, so
    # this is looser than the widen check.
    assert par.value_v == pytest.approx(actual, rel=0.35)
    assert "Mid" in par.detail  # the free layer is named


def test_the_two_sides_of_a_trace_are_one_suggestion(base):
    _, res = base
    widen = [o for o in res.opportunities
             if o.kind == "widen" and o.net == "+1V"]
    assert len(widen) == 1


def test_widening_into_other_copper_is_blocked():
    lean = _solve()
    blocker = box(0, _W, _LEN, _W + 2.0)  # another net right against the trace
    copper = {"Top": [("+1V", lean.problem.layers[0].shape),
                      ("SIG", blocker)],
              "Bottom": [("GND", lean.problem.layers[1].shape)]}
    res = R.analyse(lean, _META, "U1", copper_by_layer=copper)
    widen = [o for o in res.opportunities if o.kind == "widen"
             and o.net == "+1V"]
    # The top edge is blocked; the bottom edge is free, so the unblocked
    # suggestion survives and sorts first.
    assert widen and not widen[0].blocked


# --- ranking ----------------------------------------------------------------

def _fake_report(loads, nominal=3.3):
    rail = SimpleNamespace(name="+3V3", kind="supply", nominal_v=nominal,
                           loads=loads)
    return SimpleNamespace(rails=[rail])


def _ld(label, drop, min_v=None):
    return SimpleNamespace(label=label, kind="sink", drop_v=drop, min_v=min_v)


def test_rank_uses_min_v_budget_and_5pct_default(monkeypatch):
    import fypa.report.collect as collect

    loads = [_ld("U1", 0.10),                  # 5 % of 3.3 = 165 mV → 61 %
             _ld("U2", 0.10, min_v=3.2),       # 100 mV budget → 100 %
             _ld("U3", 0.02)]                  # → 12 %
    monkeypatch.setattr(collect, "build_report",
                        lambda *a, **k: _fake_report(loads))
    sol = SimpleNamespace(sensitivity={"U1": {}, "U2": {}, "U3": {}})
    ranked = R.rank_loads(sol, {})
    assert [s.label for s in ranked] == ["U2", "U1", "U3"]
    assert ranked[0].basis == "PDN_MIN_V"
    assert ranked[0].used == pytest.approx(1.0)
    assert ranked[1].basis == "default"
    assert ranked[1].budget_v == pytest.approx(3.3 * 0.05)
    assert R.default_target(ranked).label == "U2"


def test_default_target_skips_loads_without_a_field(monkeypatch):
    import fypa.report.collect as collect

    monkeypatch.setattr(collect, "build_report", lambda *a, **k: _fake_report(
        [_ld("U1", 0.3), _ld("U2", 0.01)]))
    ranked = R.rank_loads(SimpleNamespace(sensitivity={"U2": {}}), {})
    assert ranked[0].label == "U1" and not ranked[0].has_field
    assert R.default_target(ranked).label == "U2"


# --- helpers ------------------------------------------------------------------

def test_short_cool_gaps_join_runs():
    vals = np.array([1, 1, 0, 1, 1, 0, 0, 0, 0, 1], dtype=float)
    lengths = np.ones_like(vals)
    # Gap of 1 mm joins; the 4 mm gap does not; the loop wraps 9 → 0.
    assert R._runs(vals, 0.5, lengths, 1.5) == [(-1, 5)]
    assert R._runs(vals, 0.5) == [(-1, 2), (3, 5)]


# --- viewer render hook -------------------------------------------------------

def test_viewer_values_follow_kept_meshes_and_clip_negative():
    """The heatmap reads the target's density per *kept* mesh (the viewer
    skips degenerate meshes), averaged onto vertices and clipped at 0."""
    from fypa.viewer.tabs.fixes import _FixesTabMixin

    tris = np.array([[0, 1, 2]])
    density = {7: [np.array([-1.0]), None, np.array([2.0])]}
    stub = SimpleNamespace(_roi_result=SimpleNamespace(density=density))
    geom = {"_kept": [(tris, None, None, 3), (tris, None, None, 3)],
            "_kept_mesh_idx": [0, 2]}
    out = _FixesTabMixin._roi_vertex_values(stub, 7, geom)
    assert [a.tolist() for a in out] == [[0.0, 0.0, 0.0], [2.0, 2.0, 2.0]]
    # No analysis yet: zeros, not an error.
    stub._roi_result = None
    assert all(not a.any() for a in
               _FixesTabMixin._roi_vertex_values(stub, 7, geom))


# --- solve cache --------------------------------------------------------------

class _OldLean:
    """A lean solution pickled before ``sensitivity`` existed: no attribute."""
    solver_info: dict = {}


def test_pre_feature_cache_with_sinks_is_a_miss():
    from fypa.cli import _cache_predates_copper_roi

    md = {"directives": [{"role": "SOURCE"}, {"role": "SINK"}]}
    assert _cache_predates_copper_roi(_OldLean(), md)


def test_pre_feature_cache_without_sinks_is_still_served():
    from fypa.cli import _cache_predates_copper_roi

    assert not _cache_predates_copper_roi(
        _OldLean(), {"directives": [{"role": "SOURCE"}]})


def test_new_cache_and_stubs_are_served():
    from fypa.cli import _cache_predates_copper_roi

    md = {"directives": [{"role": "SINK"}]}
    assert not _cache_predates_copper_roi(SimpleNamespace(sensitivity={}), md)
    stub = _OldLean()
    stub.solver_info = {"stub": True}
    assert not _cache_predates_copper_roi(stub, md)
