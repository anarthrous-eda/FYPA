"""The board thermal model (pdnsolver.thermal) against closed forms."""
from __future__ import annotations

import math

import numpy as np
import pytest
import scipy.special
from shapely.geometry import Point, box

from pdnsolver import thermal as TH

H = 20.0                      # W/(m²·K), both faces combined
T_CU = 0.035


def _uniform_rise(board, layer, watts_per_mm2):
    heat = np.zeros(board.n_unknowns)
    u = board.unknown_of[layer][board.unknown_of[layer] >= 0]
    heat[u] = watts_per_mm2 * board.dx * board.dx
    return board.solve(heat)


def test_uniform_heating_of_one_sheet_is_q_over_h():
    outline = box(0, 0, 50, 40)
    board = TH.build_thermal_board(
        outline, [TH.ThermalLayer(T_CU, outline)], [], H, cell_mm=1.0)
    q = 1e-4                                   # W/mm² = 100 W/m²
    t = _uniform_rise(board, 0, q)
    assert np.allclose(t, q * 1e6 / H, rtol=1e-9)


def test_heat_on_the_top_crosses_the_laminate():
    """Uniform heat on the top of a two-layer board: half the coefficient on
    each face, and the laminate (t / k_z) in between. The bottom's share
    first crosses the laminate (flux h_face·T_bot = k_z/t·(T_top − T_bot)),
    and the two faces together shed q, so with β = h_face·t/k_z

        T_bot = q / (h_face·(2 + β)),  T_top = T_bot·(1 + β)."""
    outline = box(0, 0, 30, 30)
    t_lam = 1.5
    board = TH.build_thermal_board(
        outline, [TH.ThermalLayer(T_CU, outline, dielectric_below_mm=t_lam),
                  TH.ThermalLayer(T_CU, outline)], [], H, cell_mm=1.0)
    q = 1e-4
    t = _uniform_rise(board, 0, q)
    top = t[board.unknown_of[0][board.unknown_of[0] >= 0]]
    bot = t[board.unknown_of[1][board.unknown_of[1] >= 0]]
    h_face = 0.5 * H * 1e-6
    k_z = TH.K_LAMINATE_Z_W_PER_MK * 1e-3
    beta = h_face * t_lam / k_z
    assert np.allclose(bot, q / (h_face * (2 + beta)), rtol=1e-6)
    assert np.allclose(top, bot * (1 + beta), rtol=1e-6)


def test_point_source_spreads_like_a_cooling_fin():
    """A point source on one convecting copper sheet: the rise falls off as

        T(r) = P / (2π·G) · K0(r/λ),   λ = √(G / h),  G = k·t

    (a thin fin). Checked a few cells out, where the grid resolves it."""
    r_board = 120.0
    outline = Point(0, 0).buffer(r_board, quad_segs=64)
    t_cu = 0.070                                # 2 oz, so λ ≈ 37 mm
    board = TH.build_thermal_board(
        outline, [TH.ThermalLayer(t_cu, outline)], [], H, cell_mm=0.5)
    # Centre the source on a cell.
    row, col = board.cell_of(np.array([0.0]), np.array([0.0]))
    xs = board.x0 + (col + 0.5) * board.dx
    ys = board.y0 + (row + 0.5) * board.dx
    p = 1.0
    heat = np.zeros(board.n_unknowns)
    heat[board.unknown_at(np.array([0]), xs, ys)] = p
    t = board.solve(heat)
    g = TH.K_COPPER_W_PER_MK * 1e-3 * t_cu
    lam = math.sqrt(g / (H * 1e-6))
    for r in (5.0, 15.0, 40.0):
        u = board.unknown_at(np.array([0]), xs + r, ys)
        expected = p / (2 * math.pi * g) * scipy.special.k0(r / lam)
        assert t[u][0] == pytest.approx(expected, rel=0.03), r


def test_coverage_counts_any_net_and_holes_do_not_erase_other_copper():
    """A plane with an antipad, and another net's via pad inside it: the
    pad's copper must survive the plane's hole."""
    from shapely.geometry import MultiPolygon
    plane = box(0, 0, 10, 10).difference(Point(5, 5).buffer(1.0))
    pad = Point(5, 5).buffer(0.5)
    cov = TH._coverage(MultiPolygon([plane, pad]), 0, 0, 0.25, 40, 40)
    # Centre cell is the pad's; a cell inside the antipad ring is empty.
    assert cov[20, 20] == pytest.approx(1.0)
    assert cov[20, 23] == pytest.approx(0.0)
    assert cov[2, 2] == pytest.approx(1.0)


# ---------------------------------------------------------------- coupled solve

def _hot_trace_solve(board_model: bool):
    """A 1 mm, 40 mm trace carrying 5 A on the top of a 50 x 50 mm board
    whose bottom layer is a solid plane of another net."""
    from shapely.geometry import MultiPolygon

    from pdnsolver import problem as P
    from pdnsolver import solver as S

    S.free_mesh_assembly_cache()
    g = 5.95e4 * T_CU
    trace = box(5, 24.5, 45, 25.5)
    lay = P.Layer(shape=MultiPolygon([trace]), name="Top|VCC", conductance=g)
    pa, pb = box(5, 24.5, 6, 25.5), box(44, 24.5, 45, 25.5)
    a, b = P.NodeID(), P.NodeID()
    net = P.Network(connections=[
        P.Connection(layer=lay, point=pa.centroid, node_id=a, region=pa),
        P.Connection(layer=lay, point=pb.centroid, node_id=b, region=pb),
    ], elements=[P.CurrentSource(f=b, t=a, current=5.0)])
    board = None
    if board_model:
        outline = box(0, 0, 50, 50)
        board = TH.build_thermal_board(
            outline,
            [TH.ThermalLayer(T_CU, trace, dielectric_below_mm=1.5),
             TH.ThermalLayer(T_CU, outline)],
            [], H, cell_mm=0.5)
        board.problem_layer_map = [0]
    cfg = S.ThermalConfig(enabled=True, heat_transfer_w_per_m2k=H,
                          max_iterations=20, board=board)
    sol = S.solve(P.Problem(layers=[lay], networks=[net]), thermal=cfg)
    S.free_mesh_assembly_cache()
    iso = S.solve(P.Problem(layers=[lay], networks=[net]))
    rise = np.concatenate([np.asarray(r.values) for r in
                           sol.layer_solutions[0].temperature_rises])

    def _drop(s):
        v = np.concatenate([np.asarray(p.values)
                            for p in s.layer_solutions[0].potentials])
        return v.max() - v.min()
    return sol, rise, _drop(sol), _drop(iso)


def test_board_model_spreads_the_heat_of_a_hot_trace():
    local, rise_local, drop_local, drop_iso = _hot_trace_solve(False)
    spread, rise_board, drop_board, _ = _hot_trace_solve(True)
    assert spread.solver_info.thermal_converged
    # The plane under the trace (and the laminate around it) carry the heat
    # away: the trace runs far cooler than shedding it on the spot.
    assert 0.0 < rise_board.max() < 0.5 * rise_local.max()
    # Still hotter than ambient, so still more resistive than isothermal.
    assert drop_iso < drop_board < drop_local


def test_lumped_heat_counts_real_parts_and_regulator_loss_only():
    """A heat-source resistor dissipates I²R at its pads, a star-coupling
    resistor nothing, and a regulator its input minus output power."""
    from types import SimpleNamespace

    from pdnsolver import problem as P
    from pdnsolver import solver as S

    lay = SimpleNamespace()
    a, b, c, d = (P.NodeID() for _ in range(4))
    vp, vn, sf, st = (P.NodeID() for _ in range(4))
    part = P.Resistor(a=a, b=b, resistance=0.5, heat_source=True)
    coupling = P.Resistor(a=c, b=d, resistance=0.1)
    reg = P.VoltageRegulator(v_p=vp, v_n=vn, s_f=sf, s_t=st,
                             voltage=1.0, gain=0.25)

    def _conn(node, x):
        return SimpleNamespace(layer=lay, point=Point(x, 0), node_id=node)
    nets = [
        SimpleNamespace(elements=[part, coupling],
                        connections=[_conn(a, 0), _conn(b, 1), _conn(c, 5),
                                     _conn(d, 6)]),
        SimpleNamespace(elements=[reg],
                        connections=[_conn(vp, 10), _conn(sf, 11)]),
    ]
    order = [a, b, c, d, vp, vn, sf, st]
    v = np.zeros(len(order) + 1)
    vals = {a: 2.0, b: 1.0, c: 1.0, d: 0.0, vp: 1.0, vn: 0.0, sf: 5.0, st: 0.0}
    for i, n in enumerate(order):
        v[i] = vals[n]
    v[-1] = 2.0                                    # regulator output current
    indexer = SimpleNamespace(
        node_to_global_index={n: i for i, n in enumerate(order)},
        extra_source_to_global_index={reg: len(order)})
    layers, xs, _ys, watts = S._lumped_heat_sources(
        nets, indexer, v, {id(lay): 0}, np.array([0]))
    by_x = dict(zip(xs.tolist(), watts.tolist()))
    # Part: (1 V)² / 0.5 Ω = 2 W, half at each pad; the coupling: nothing.
    assert by_x[0.0] == pytest.approx(1.0) and by_x[1.0] == pytest.approx(1.0)
    assert 5.0 not in by_x and 6.0 not in by_x
    # Regulator: in 5 V · 0.25 · 2 A = 2.5 W, out 1 V · 2 A = 2 W → 0.5 W.
    assert by_x[10.0] + by_x[11.0] == pytest.approx(0.5)
    assert set(layers.tolist()) == {0}


def test_coverage_reads_a_collection_of_per_net_multipolygons():
    """The loader hands each layer over as a GeometryCollection of per-net
    MultiPolygons; one level of unwrapping read that as no copper at all."""
    import shapely
    from shapely.geometry import MultiPolygon
    a = MultiPolygon([box(0, 0, 5, 10)])
    b = MultiPolygon([box(5, 0, 10, 10)])
    cov = TH._coverage(shapely.GeometryCollection([a, b]), 0, 0, 0.5, 20, 20)
    assert cov.mean() == pytest.approx(1.0)
