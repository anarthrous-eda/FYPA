"""Validation boards: the solver against closed-form answers.

Each test builds a small board whose resistance is known exactly from
physics, solves it at the solver's **default** mesh settings, and checks the
answer. Together they cover the pieces every real rail is made of: sheet
resistance along a trace, current turning a corner, spreading in a plane,
current entering a via, and current shared by a row of vias between layers.

Vias are built through :func:`fypa.altium.loader._coupling_networks`, the
production path (drill-circle patches, per-type plating, hop lengths from
layer centres), not hand-made resistors, so a regression there shows up here.

Terminals are equipotential patches (a pad's ``region``), as directives are
in a real solve.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
import shapely
from shapely.geometry import MultiPolygon, Point, box

from fypa.altium import loader as L
from pdnsolver import mesh as M
from pdnsolver import problem as P
from pdnsolver import solver as S

SIGMA_S_PER_MM = 5.95e4        # FYPA's copper conductivity at 20 °C
T_1OZ_MM = 0.035
T_2OZ_MM = 0.070
G_1OZ = SIGMA_S_PER_MM * T_1OZ_MM   # sheet conductance, S
RS_1OZ = 1.0 / G_1OZ                # sheet resistance, Ω/square

# Squares added by one right-angle bend of a uniform strip (the corner
# square counted as 0.5587 squares, not 1): the classic conformal-map value.
CORNER_SQUARES = 0.5587


@pytest.fixture(autouse=True)
def _fresh_mesh_cache():
    """Each board is its own geometry; never reuse another test's mesh."""
    S.free_mesh_assembly_cache()
    yield
    S.free_mesh_assembly_cache()


def _solve(layers, networks, mesh_size: float | None = None):
    prob = P.Problem(layers=layers, networks=networks, project_name="validation")
    cfg = M.Mesher.Config(maximum_size=mesh_size) if mesh_size else None
    return S.solve(prob, mesher_config=cfg)


def _v_at(solution, layer_index: int, x: float, y: float) -> float:
    """Potential at the mesh vertex nearest (x, y) on one layer."""
    ls = solution.layer_solutions[layer_index]
    best_d, best_v = math.inf, math.nan
    for msh, pot in zip(ls.meshes, ls.potentials):
        xy = np.asarray(msh._source_xys, dtype=np.float64)
        d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        i = int(np.argmin(d))
        if d[i] < best_d:
            best_d, best_v = float(d[i]), float(np.asarray(pot.values)[i])
    return best_v


def _drive(layer_a, region_a, layer_b, region_b, current=1.0):
    """A 1 A current source between two pad patches; returns the network."""
    a, b = P.NodeID(), P.NodeID()
    conns = [
        P.Connection(layer=layer_a, point=region_a.centroid, node_id=a,
                     region=region_a),
        P.Connection(layer=layer_b, point=region_b.centroid, node_id=b,
                     region=region_b),
    ]
    return P.Network(connections=conns,
                     elements=[P.CurrentSource(f=b, t=a, current=current)])


# ---------------------------------------------------------------- traces

def _strip_squares(length, width, pad=1.0, mesh_size=None) -> float:
    """Solved resistance, in squares, of a straight strip between two
    full-width end pads."""
    lay = P.Layer(shape=MultiPolygon([box(0, 0, length, width)]),
                  name="F.Cu", conductance=G_1OZ)
    pa, pb = box(0, 0, pad, width), box(length - pad, 0, length, width)
    sol = _solve([lay], [_drive(lay, pa, lay, pb)], mesh_size=mesh_size)
    drop = _v_at(sol, 0, length - pad / 2, width / 2) - _v_at(
        sol, 0, pad / 2, width / 2)
    return abs(drop) / RS_1OZ


@pytest.mark.parametrize("width_mm", [0.15, 1.0, 5.0])
def test_strip_end_to_end_is_rho_l_over_wt(width_mm):
    """Pads spanning the full width at each end: R = Rs · (length − 2·pad)
    / width. P1 elements are exact for the linear field between the pads;
    what is left (a few 1e-4) is the pad patch's inner edge, which follows
    the mesh vertices rather than a straight line."""
    length, pad = 20.0, 1.0
    squares = _strip_squares(length, width_mm, pad)
    assert squares == pytest.approx((length - 2 * pad) / width_mm, rel=1e-3)


@pytest.mark.parametrize(("mesh_size", "tol"), [(None, 0.03), (0.1, 0.005)])
def test_right_angle_bend_adds_0_559_squares(mesh_size, tol):
    """An L route: two straight legs plus one corner worth 0.5587 squares.
    The re-entrant corner is singular, so this is what a mesh gets wrong
    first: at the default mesh FYPA reads about 0.026 squares low per bend
    (~0.1 % of a typical route), and converges as the mesh is refined."""
    w, la, lb, pad = 1.0, 15.0, 12.0, 1.0
    shape = shapely.union(box(0, 0, la, w), box(la - w, 0, la, lb))
    lay = P.Layer(shape=MultiPolygon([shape]), name="F.Cu",
                  conductance=G_1OZ)
    pa, pb = box(0, 0, pad, w), box(la - w, lb - pad, la, lb)
    sol = _solve([lay], [_drive(lay, pa, lay, pb)], mesh_size=mesh_size)
    squares = abs(_v_at(sol, 0, la - w / 2, lb - pad / 2)
                  - _v_at(sol, 0, pad / 2, w / 2)) / RS_1OZ
    straight = (la - pad - w) / w + (lb - pad - w) / w
    assert squares - straight == pytest.approx(CORNER_SQUARES, abs=tol)


# ---------------------------------------------------------------- planes

@pytest.mark.parametrize(("mesh_size", "tol"), [(None, 0.01), (0.2, 0.0025)])
def test_two_pads_on_a_disc_plane_matches_method_of_images(mesh_size, tol):
    """Two round pads (radius a) at ±D on an insulated disc of radius R0.
    Each pad's image sits at R0²/D (same sign, for an insulating edge), so

        R = (Rs/π) · ln( (2D/a) · (R0² + D²) / (R0² − D²) )   (a ≪ D).

    The grading rings round each pad take the default-mesh error from
    about -1.0 % to about -0.5 %."""
    r0, d, a = 25.0, 10.0, 0.5
    disc = Point(0, 0).buffer(r0, quad_segs=64)
    lay = P.Layer(shape=MultiPolygon([disc]), name="In1.Cu",
                  conductance=G_1OZ)
    pa = Point(-d, 0).buffer(a, quad_segs=16)
    pb = Point(d, 0).buffer(a, quad_segs=16)
    sol = _solve([lay], [_drive(lay, pa, lay, pb)], mesh_size=mesh_size)
    drop = abs(_v_at(sol, 0, d, 0) - _v_at(sol, 0, -d, 0))
    expected = RS_1OZ / math.pi * math.log(
        2 * d / a * (r0 ** 2 + d ** 2) / (r0 ** 2 - d ** 2))
    assert drop == pytest.approx(expected, rel=tol)


# ---------------------------------------------------------------- vias

def _via_sites(xy, drill, kind="through", span=(1, 32), net=0):
    return [L._ViaSite(x_mm=x, y_mm=y, span=span, net_index=net,
                       drill_diameter_mm=drill, kind=kind) for x, y in xy]


def _two_layer_board(shape_top, shape_bot, t_top=T_1OZ_MM, t_bot=T_1OZ_MM,
                     dielectric_mm=1.5):
    """Layers 1 (top) and 32 (bottom) of net 0, and their centre z."""
    top = P.Layer(shape=MultiPolygon([shape_top]), name="Top",
                  conductance=SIGMA_S_PER_MM * t_top)
    bot = P.Layer(shape=MultiPolygon([shape_bot]), name="Bottom",
                  conductance=SIGMA_S_PER_MM * t_bot)
    layers = {(1, 0): top, (32, 0): bot}
    z = {1: 0.5 * t_top, 32: t_top + dielectric_mm + 0.5 * t_bot}
    return top, bot, layers, z


@pytest.mark.parametrize("mesh_size", [None, 0.3])
def test_via_spreading_into_a_plane(mesh_size):
    """Current leaving a via into a plane spreads from the drill wall:
    R = Rs/(2π) · ln(R_out / r_drill). A point contact at the via centre
    over-states this by ~80% and grows without bound as the mesh is refined;
    the drill-circle patch (plus the grading rings) lands within 1%."""
    r_plane, drill = 10.0, 0.3
    disc = Point(0, 0).buffer(r_plane, quad_segs=64)
    top, bot, layers, z = _two_layer_board(disc, disc)
    nets, _ = L._coupling_networks(_via_sites([(0.0, 0.0)], drill), layers, z,
                                   plating_thickness_mm=0.025)
    # Drive from an outer ring on the top plane to an outer ring on the
    # bottom plane, so the current crosses at the via.
    ring = (Point(0, 0).buffer(r_plane - 0.3, quad_segs=64)
            .difference(Point(0, 0).buffer(r_plane - 0.6, quad_segs=64)))
    sol = _solve([top, bot], nets + [_drive(top, ring, bot, ring)],
                 mesh_size=mesh_size)
    v_ring_top = _v_at(sol, 0, r_plane - 0.45, 0)
    v_via_top = _v_at(sol, 0, 0, 0)
    expected = RS_1OZ / (2 * math.pi) * math.log((r_plane - 0.6) / (drill / 2))
    assert abs(v_ring_top - v_via_top) == pytest.approx(expected, rel=0.01)


def test_layer_change_through_a_row_of_vias():
    """A strip on Top drops to a 2 oz strip on Bottom through a row of N
    vias at pitch p across its width (W = N·p). The drop is

        R = Rs_top·(x_via − pad)/W + Rs_bot·(L − pad − x_via)/W   (strips)
          + R_barrel / N                                          (barrels)
          + (Rs_top + Rs_bot) · ln(p / (2π r)) / (2π N)           (crowding)

    the last term being the constriction into a periodic row of holes of
    radius r. The row is symmetric, so every via carries I/N."""
    n, pitch, drill = 5, 1.0, 0.3
    w = n * pitch
    length, pad, x_via = 30.0, 1.0, 15.0
    stub = 3.0 * pitch          # past the row, where the field has died out
    top_shape = box(0, 0, x_via + stub, w)
    bot_shape = box(x_via - stub, 0, length, w)
    top, bot, layers, z = _two_layer_board(top_shape, bot_shape,
                                           t_bot=T_2OZ_MM)
    xy = [(x_via, pitch * (i + 0.5)) for i in range(n)]
    nets, records = L._coupling_networks(_via_sites(xy, drill), layers, z,
                                         plating_thickness_mm=0.025)
    assert len(records) == n
    sol = _solve([top, bot], nets + [_drive(top, box(0, 0, pad, w),
                                            bot, box(length - pad, 0,
                                                     length, w))])
    drop = abs(_v_at(sol, 1, length - pad / 2, w / 2)
               - _v_at(sol, 0, pad / 2, w / 2))

    rs_top, rs_bot = 1 / (SIGMA_S_PER_MM * T_1OZ_MM), 1 / (
        SIGMA_S_PER_MM * T_2OZ_MM)
    r_strips = rs_top * (x_via - pad) / w + rs_bot * (length - pad - x_via) / w
    r_barrel = records[0]["resistance_ohm"] / n
    r_crowd = ((rs_top + rs_bot) * math.log(pitch / (2 * math.pi * drill / 2))
               / (2 * math.pi * n))
    assert drop == pytest.approx(r_strips + r_barrel + r_crowd, rel=5e-3)

    # Equal sharing: each hop's current from the potentials across it.
    for (x, y), rec in zip(xy, records):
        i_via = (_v_at(sol, 0, x, y) - _v_at(sol, 1, x, y)) / rec["resistance_ohm"]
        assert abs(i_via) == pytest.approx(1.0 / n, rel=0.02)


# ---------------------------------------------------------------- via plating

def test_barrel_resistance_uses_the_plating_for_its_via_type():
    """R_hop = ρ·L / (π·(d·t − t²)) with t taken from the via's type."""
    d, hop = 0.3, 1.5
    disc = Point(0, 0).buffer(3.0)
    _top, _bot, layers, z = _two_layer_board(disc, disc, dielectric_mm=hop)
    for kind, t in (("through", L.PLATING_THICKNESS_MM),
                    ("pth", L.PLATING_THICKNESS_MM),
                    ("blind", L.PLATING_BLIND_MM),
                    ("buried", L.PLATING_BURIED_MM),
                    ("micro", L.PLATING_MICROVIA_MM)):
        _nets, rec = L._coupling_networks(
            _via_sites([(0.0, 0.0)], d, kind=kind), layers, z)
        length = z[32] - z[1]
        expected = (L.COPPER_RESISTIVITY_OHM_MM * length
                    / (math.pi * (d * t - t * t)))
        assert rec[0]["resistance_ohm"] == pytest.approx(expected, rel=1e-9), kind
        assert rec[0]["via_kind"] == kind
        assert rec[0]["plating_mm"] == t


@pytest.mark.parametrize(
    ("start", "end", "span", "drill", "kind"),
    [
        (1, 32, (1, 2, 3, 32), 0.30, "through"),
        (1, 3, (1, 2, 3), 0.30, "blind"),
        (32, 3, (3, 32), 0.30, "blind"),       # 1 dielectric, but a big drill
        (1, 2, (1, 2), 0.10, "micro"),         # laser via, one dielectric
        (2, 3, (2, 3), 0.10, "micro"),         # buried microvia
        (2, 3, (2, 3), 0.25, "buried"),
    ],
)
def test_via_classification(start, end, span, drill, kind):
    enabled = [1, 2, 3, 32]
    assert L._classify_via(start, end, span, enabled, drill) == kind


def test_old_pickles_keep_their_single_plating_for_every_type():
    """A solve from before per-type plating used one value for every via."""
    s = L.SolveSettings.from_metadata(
        {"physics_constants": {"plating_thickness_mm": 0.030}})
    assert (s.plating_thickness_mm, s.plating_blind_mm, s.plating_buried_mm,
            s.plating_microvia_mm) == (0.030, 0.030, 0.030, 0.030)
    s = L.SolveSettings.from_metadata({"physics_constants": {
        "plating_thickness_mm": 0.020, "plating_blind_mm": 0.020,
        "plating_buried_mm": 0.013, "plating_microvia_mm": 0.012}})
    assert s.plating_buried_mm == 0.013


# ---------------------------------------------------------------- remote sense

def _supply_and_return(length=20.0, w=1.0, gap=2.0):
    """A supply strip and, beside it, its return strip (separate nets)."""
    sup = P.Layer(shape=MultiPolygon([box(0, 0, length, w)]), name="VCC",
                  conductance=G_1OZ)
    ret = P.Layer(shape=MultiPolygon([box(0, w + gap, length, 2 * w + gap)]),
                  name="GND", conductance=G_1OZ)
    return sup, ret, (box(0, 0, 1, w), box(length - 1, 0, length, w),
                      box(0, w + gap, 1, 2 * w + gap),
                      box(length - 1, w + gap, length, 2 * w + gap))


def _remote_sense_board(make_source, current=2.0):
    """Source at x = 0, load at x = L, sensing at the load on both sides.
    Returns (solution, V across the load, V across the source pins, R_loop)."""
    length, w = 20.0, 1.0
    sup, ret, (src_p, load_p, src_n, load_n) = _supply_and_return(length, w)
    p, n, sp, sn = P.NodeID(), P.NodeID(), P.NodeID(), P.NodeID()
    f, t = P.NodeID(), P.NodeID()
    src_conns = [
        P.Connection(layer=sup, point=src_p.centroid, node_id=p, region=src_p),
        P.Connection(layer=ret, point=src_n.centroid, node_id=n, region=src_n),
        # The sense pads are the load's own pads: patches that overlap the
        # load's, so they join its groups.
        P.Connection(layer=sup, point=load_p.centroid, node_id=sp,
                     region=load_p),
        P.Connection(layer=ret, point=load_n.centroid, node_id=sn,
                     region=load_n),
    ]
    src_elems = make_source(p, n, sp, sn)
    # A locally sensed source uses no sense node: attach only what it uses.
    used = {node for e in src_elems for node in e.terminals}
    src_conns = [c for c in src_conns if c.node_id in used]
    load = P.Network(connections=[
        P.Connection(layer=sup, point=load_p.centroid, node_id=f,
                     region=load_p),
        P.Connection(layer=ret, point=load_n.centroid, node_id=t,
                     region=load_n),
    ], elements=[P.CurrentSource(f=f, t=t, current=current)])
    sol = _solve([sup, ret], [P.Network(connections=src_conns,
                                        elements=src_elems), load])
    v_load = (_v_at(sol, 0, length - 0.5, w / 2)
              - _v_at(sol, 1, length - 0.5, 1.5 * w + 2.0))
    v_src = _v_at(sol, 0, 0.5, w / 2) - _v_at(sol, 1, 0.5, 1.5 * w + 2.0)
    r_loop = 2 * RS_1OZ * (length - 2.0) / w
    return sol, v_load, v_src, r_loop


def test_remote_sensed_source_holds_its_setpoint_at_the_load():
    """The source regulates across the load's pads: the load sees exactly
    5 V and the source pins rise by I·R of the supply and return copper."""
    _sol, v_load, v_src, r_loop = _remote_sense_board(
        lambda p, n, sp, sn: [P.VoltageSource(p=p, n=n, voltage=5.0,
                                              sense_p=sp, sense_n=sn)])
    assert v_load == pytest.approx(5.0, abs=1e-9)
    assert v_src - 5.0 == pytest.approx(2.0 * r_loop, rel=2e-3)


def test_locally_sensed_source_drops_at_the_load():
    """The same board without remote sense: the source pins hold 5 V and
    the load sees 5 V − I·R."""
    _sol, v_load, v_src, r_loop = _remote_sense_board(
        lambda p, n, sp, sn: [P.VoltageSource(p=p, n=n, voltage=5.0)])
    assert v_src == pytest.approx(5.0, abs=1e-9)
    assert 5.0 - v_load == pytest.approx(2.0 * r_loop, rel=2e-3)


def test_remote_sensed_regulator_holds_its_setpoint_at_the_load():
    """A regulator (ideal 12 V input, gain 0.5) sensing at the load: the
    load sees exactly its setpoint, and the input still draws gain·I_out."""
    vin = {}

    def _reg(p, n, sp, sn):
        s_f, s_t = P.NodeID(), P.NodeID()
        vin["src"] = P.VoltageSource(p=s_f, n=s_t, voltage=12.0)
        return [P.VoltageRegulator(v_p=p, v_n=n, s_f=s_f, s_t=s_t,
                                   voltage=3.3, gain=0.5,
                                   sense_p=sp, sense_n=sn), vin["src"]]

    _sol, v_load, v_src, r_loop = _remote_sense_board(_reg)
    assert v_load == pytest.approx(3.3, abs=1e-9)
    assert v_src - 3.3 == pytest.approx(2.0 * r_loop, rel=2e-3)
