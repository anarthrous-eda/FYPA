"""Adjoint copper sensitivity, checked against finite differences.

The solver returns, per target load, the adjoint field λ with Lᵀλ = e_f - e_t.
``pdnsolver.sensitivity`` turns (v, λ) into per-triangle values
σ_t·dJ/dσ_t. Summed over a layer they must equal dJ/d(ln σ_layer), which a
central difference on the layer's conductance measures directly; a via
resistor's value I·(λ_a - λ_b) must equal R·(-dJ/dR) likewise.
"""
from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import problem as P
from pdnsolver import sensitivity as SENS
from pdnsolver import solver as S

_CU = 5.95e4 * 0.035  # 1 oz sheet conductance, S
_PAD = 0.6


def _pad(layer, x, y, node):
    return P.Connection(layer=layer, point=Point(x, y), node_id=node,
                        region=box(x - _PAD / 2, y - _PAD / 2,
                                   x + _PAD / 2, y + _PAD / 2))


def _board(cond_a=_CU, cond_b=_CU, cond_gnd=_CU, via_r=2e-3,
           regulator=False):
    """A rail split over two layers joined by a via, a GND plane, a source at
    the left and two sinks. With ``regulator`` the rail is fed by a regulator
    from a separate 5 V input rail (an unsymmetric system)."""
    top_a = P.Layer(shape=MultiPolygon([box(0, 0, 20, 3)]), name="top_a",
                    conductance=cond_a)
    top_b = P.Layer(shape=MultiPolygon([box(19, 0, 40, 3)]), name="top_b",
                    conductance=cond_b)
    gnd = P.Layer(shape=MultiPolygon([box(0, -6, 40, 9)]), name="gnd",
                  conductance=cond_gnd)
    layers = [top_a, top_b, gnd]
    nets = []

    # Via between the two rail layers where they overlap.
    va, vb = P.NodeID(), P.NodeID()
    via = P.Resistor(a=va, b=vb, resistance=via_r)
    nets.append(P.Network(
        connections=[P.Connection(layer=top_a, point=Point(19.5, 1.5),
                                  node_id=va),
                     P.Connection(layer=top_b, point=Point(19.6, 1.5),
                                  node_id=vb)],
        elements=[via]))

    sp, sn = P.NodeID(), P.NodeID()
    if regulator:
        vin = P.Layer(shape=MultiPolygon([box(0, 12, 10, 14)]), name="vin",
                      conductance=_CU)
        layers.append(vin)
        ip, i_n = P.NodeID(), P.NodeID()
        nets.append(P.Network(
            connections=[_pad(vin, 0.5, 13, ip), _pad(gnd, 0.5, 8, i_n)],
            elements=[P.VoltageSource(p=ip, n=i_n, voltage=5.0)]))
        s_f, s_t = P.NodeID(), P.NodeID()
        nets.append(P.Network(
            connections=[_pad(top_a, 1.0, 1.5, sp), _pad(gnd, 1.0, -3, sn),
                         _pad(vin, 9.0, 13, s_f), _pad(gnd, 9.0, 8, s_t)],
            elements=[P.VoltageRegulator(v_p=sp, v_n=sn, s_f=s_f, s_t=s_t,
                                         voltage=1.0, gain=0.25)]))
    else:
        nets.append(P.Network(
            connections=[_pad(top_a, 1.0, 1.5, sp), _pad(gnd, 1.0, -3, sn)],
            elements=[P.VoltageSource(p=sp, n=sn, voltage=1.0)]))

    targets = []
    for key, layer, x, amps in (("U1", top_b, 38.5, 3.0),
                                ("U2", top_a, 10.0, 1.0)):
        f, t = P.NodeID(), P.NodeID()
        sink = P.CurrentSource(f=f, t=t, current=amps)
        nets.append(P.Network(
            connections=[_pad(layer, x, 1.5, f), _pad(gnd, x, -3, t)],
            elements=[sink]))
        targets.append((key, sink))
    prob = P.Problem(layers=layers, networks=nets, project_name="sens",
                     sensitivity_targets=tuple(targets))
    return prob, via


def _solve(**kw):
    prob, via = _board(**kw)
    return S.solve(prob), prob, via


def _layer_value_sum(sol, prob, layer_name, key):
    li = next(i for i, L in enumerate(prob.layers) if L.name == layer_name)
    ls = sol.layer_solutions[li]
    total = 0.0
    for mi, (m, zf) in enumerate(zip(ls.meshes, ls.potentials)):
        xys, tris = S._mesh_source_arrays(m)
        lam = SENS.mesh_adjoint(ls, key, mi)
        total += float(SENS.triangle_values(
            xys, tris, np.asarray(zf.values), lam,
            prob.layers[li].conductance).sum())
    return total


def _objective(sol, key):
    return sol.sensitivity[key]["objective_v"]


def _sample_adjoint(sol, prob, layer_name, key, x, y):
    """λ at the mesh vertex nearest (x, y) on a layer."""
    li = next(i for i, L in enumerate(prob.layers) if L.name == layer_name)
    ls = sol.layer_solutions[li]
    best = (np.inf, 0.0, 0.0)
    for mi, (m, zf) in enumerate(zip(ls.meshes, ls.potentials)):
        xys, _ = S._mesh_source_arrays(m)
        d = np.hypot(xys[:, 0] - x, xys[:, 1] - y)
        k = int(np.argmin(d))
        if d[k] < best[0]:
            best = (d[k], SENS.mesh_adjoint(ls, key, mi)[k],
                    float(np.asarray(zf.values)[k]))
    return best[1], best[2]


@pytest.mark.parametrize("regulator", [False, True])
@pytest.mark.parametrize("key", ["U1", "U2"])
@pytest.mark.parametrize("layer, kwarg", [
    ("top_a", "cond_a"), ("top_b", "cond_b"), ("gnd", "cond_gnd")])
def test_layer_sum_matches_central_difference(regulator, key, layer, kwarg):
    sol, prob, _ = _solve(regulator=regulator)
    predicted = _layer_value_sum(sol, prob, layer, key)

    eps = 1e-3
    hi, _, _ = _solve(regulator=regulator, **{kwarg: _CU * (1 + eps)})
    lo, _, _ = _solve(regulator=regulator, **{kwarg: _CU * (1 - eps)})
    measured = (_objective(hi, key) - _objective(lo, key)) / (2 * eps)

    # Absolute floor: a layer that doesn't matter to the load predicts ~0,
    # and the difference then only measures solver round-off / eps (~1e-11).
    assert predicted == pytest.approx(measured, rel=1e-3, abs=1e-8)


def test_more_copper_on_the_loads_own_path_helps():
    """Doubling copper on the rail between source and load can only raise
    the voltage the load receives."""
    sol, prob, _ = _solve()
    assert _layer_value_sum(sol, prob, "top_b", "U1") > 0
    assert _layer_value_sum(sol, prob, "gnd", "U1") > 0


def test_copper_beyond_a_load_does_not_matter_to_it():
    """U2 sits on top_a; top_b only feeds U1, so its copper is worth ~nothing
    to U2 while it is worth a lot to U1."""
    sol, prob, _ = _solve()
    to_u1 = _layer_value_sum(sol, prob, "top_b", "U1")
    to_u2 = _layer_value_sum(sol, prob, "top_b", "U2")
    assert abs(to_u2) < 1e-3 * abs(to_u1)


@pytest.mark.parametrize("regulator", [False, True])
def test_via_value_matches_central_difference(regulator):
    r = 2e-3
    sol, prob, via = _solve(regulator=regulator, via_r=r)
    # Current through the via from the solved potentials at its two ends.
    lam_a, v_a = _sample_adjoint(sol, prob, "top_a", "U1", 19.5, 1.5)
    lam_b, v_b = _sample_adjoint(sol, prob, "top_b", "U1", 19.6, 1.5)
    predicted = SENS.resistor_value((v_a - v_b) / r, lam_a, lam_b)

    eps = 1e-3
    hi, _, _ = _solve(regulator=regulator, via_r=r * (1 + eps))
    lo, _, _ = _solve(regulator=regulator, via_r=r * (1 - eps))
    # Doubling G is ΔG = G: G·dJ/dG = -R·dJ/dR.
    measured = -(_objective(hi, "U1") - _objective(lo, "U1")) / (2 * eps)
    assert predicted == pytest.approx(measured, rel=2e-2)


def test_no_targets_means_no_adjoint_work():
    prob, _ = _board()
    sol = S.solve(dataclasses.replace(prob, sensitivity_targets=()))
    assert sol.sensitivity == {}
    assert all(ls.adjoints == {} for ls in sol.layer_solutions)


def test_objective_is_the_load_voltage():
    sol, _, _ = _solve()
    for key in ("U1", "U2"):
        j = _objective(sol, key)
        assert 0.8 < j < 1.0  # below the 1 V source, above a silly drop


@pytest.mark.parametrize("regulator", [False, True])
def test_superlu_fallback_gives_the_same_adjoint(regulator, monkeypatch):
    """Without PARDISO the adjoints come from SuperLU; they must agree."""
    sol, prob, _ = _solve(regulator=regulator)
    monkeypatch.setattr(S, "_HAVE_PARDISO", False)
    alt, _, _ = _solve(regulator=regulator)
    for key in ("U1", "U2"):
        assert (_layer_value_sum(alt, prob, "gnd", key)
                == pytest.approx(_layer_value_sum(sol, prob, "gnd", key),
                                 rel=1e-6))
