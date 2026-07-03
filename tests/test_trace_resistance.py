"""Analytic trace-resistance validation of the cotangent-Laplacian solver.

A rectangular copper strip of width ``W`` and length ``L`` with full-width
equipotential contacts at both ends carrying current ``I`` has the exact
resistance

    R = rho * L_eff / (W * t) = L_eff / (sigma * W * t) = Rs * L_eff / W

(the standard sheet-resistance formula, e.g. the omnicalculator PCB-trace-
resistance tool), where ``L_eff = L - 2*pad`` is the copper length between the
two ideal-conductor end pads. Because the pads are equipotential patches, they
short out their own length, so the resistive span is ``L_eff``, not ``L``.

The finite-element solve reproduces this to well under 1% — but ONLY with the
correct *signed* cotangent stiffness weight. Taking ``|cot|`` (the historical
bug) over-conducts obtuse triangles, so the solved resistance came out 6-12%
low and, tellingly, drifted *further* from the analytic value as the mesh was
refined (an inconsistent discretisation). These tests pin the corrected
behaviour: they pass comfortably for the signed weight and fail loudly for the
absolute-value one.
"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import problem as P
from pdnsolver import solver as S

SIGMA_S_PER_MM = 5.95e4      # copper conductivity
THICKNESS_MM = 0.035         # 1 oz finished copper
RS = 1.0 / (SIGMA_S_PER_MM * THICKNESS_MM)   # sheet resistance, ohm/sq
_CU = SIGMA_S_PER_MM * THICKNESS_MM          # conductance S


def _strip_resistance(width_mm: float, length_mm: float,
                      pad_mm: float = 0.5, current_a: float = 1.0) -> float:
    """Solve a W x L strip with full-width end pads; return R = dV / I."""
    layer = P.Layer(shape=MultiPolygon([box(0.0, 0.0, length_mm, width_mm)]),
                    name="strip", conductance=_CU)
    src, snk = P.NodeID(), P.NodeID()
    net = P.Network(
        connections=[
            P.Connection(layer=layer, point=Point(0.01, width_mm / 2),
                         node_id=src, region=box(0.0, 0.0, pad_mm, width_mm)),
            P.Connection(layer=layer, point=Point(length_mm - 0.01, width_mm / 2),
                         node_id=snk,
                         region=box(length_mm - pad_mm, 0.0, length_mm, width_mm)),
        ],
        elements=[P.CurrentSource(f=src, t=snk, current=current_a)],
    )
    sol = S.solve(P.Problem(layers=[layer], networks=[net],
                            project_name="trace-r"))
    assert sol.solver_info.residual_norm < 1e-6
    vals = np.concatenate([np.asarray(zf.values, np.float64)
                           for ls in sol.layer_solutions for zf in ls.potentials])
    return float(vals.max() - vals.min()) / current_a


# (width, length, pad) in mm — the example-design trace geometries plus a wide one.
@pytest.mark.parametrize("width,length,pad", [
    (1.0, 100.0, 0.5),   # P1V_1W100L
    (1.0, 50.0, 0.5),    # P1V_1W50L
    (1.0, 10.0, 0.2),    # P1V_1W10L
    (10.0, 100.0, 0.5),  # P1V_10W100L (full-width contact -> no spreading R)
])
def test_strip_resistance_matches_analytic(width, length, pad):
    r_fem = _strip_resistance(width, length, pad_mm=pad)
    l_eff = length - 2.0 * pad
    r_exact = RS * l_eff / width
    rel_err = abs(r_fem - r_exact) / r_exact
    # Signed cotangent lands <0.5% off; the |cot| bug missed by 6-12%.
    assert rel_err < 0.01, (
        f"{width}x{length} mm: R_fem={r_fem*1e3:.4f} mohm vs "
        f"analytic {r_exact*1e3:.4f} mohm (L_eff={l_eff}); rel err {rel_err:.4%}"
    )


def test_resistance_is_mesh_convergent():
    """A correct (consistent) discretisation must not drift as the mesh
    refines. The |cot| bug got *worse* with refinement; the signed weight
    stays put."""
    from pdnsolver import mesh as M

    def solve_at(max_size):
        layer = P.Layer(shape=MultiPolygon([box(0.0, 0.0, 100.0, 1.0)]),
                        name="strip", conductance=_CU)
        src, snk = P.NodeID(), P.NodeID()
        net = P.Network(
            connections=[
                P.Connection(layer=layer, point=Point(0.01, 0.5), node_id=src,
                             region=box(0.0, 0.0, 0.5, 1.0)),
                P.Connection(layer=layer, point=Point(99.99, 0.5), node_id=snk,
                             region=box(99.5, 0.0, 100.0, 1.0)),
            ],
            elements=[P.CurrentSource(f=src, t=snk, current=1.0)],
        )
        sol = S.solve(P.Problem(layers=[layer], networks=[net],
                                project_name="conv"),
                      mesher_config=M.Mesher.Config(maximum_size=max_size))
        vals = np.concatenate([np.asarray(zf.values, np.float64)
                               for ls in sol.layer_solutions
                               for zf in ls.potentials])
        return float(vals.max() - vals.min())

    coarse = solve_at(0.4)
    fine = solve_at(0.15)
    # Both within 1% of each other — refinement doesn't move the answer.
    assert abs(fine - coarse) / coarse < 0.01, (
        f"resistance drifted with refinement: {coarse*1e3:.4f} -> "
        f"{fine*1e3:.4f} mohm"
    )
