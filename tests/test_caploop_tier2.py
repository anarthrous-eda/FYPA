"""Tier-2 FEM plane-pair spreading inductance (fypa.caploop.tier2_fem).

The key physics test: on an unbroken cavity the FEM must agree with the
two-port closed form. A slot between the ports must raise the answer
materially — the whole reason Tier 2 exists. Also covers the duality
constant, zero-current source acceptance (the inactive ports of an N-port
solve), matrix reciprocity, mesh-assembly cache reuse across the N solves,
and disconnected cavity islands.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
import shapely.geometry

import pdnsolver.problem as _pp
from pdnsolver import mesh as _mesh
from fypa.caploop.constants import MU0_H_PER_MM
from fypa.caploop.tier1 import spreading_closed_form_h
from fypa.caploop.tier2_fem import (
    Tier2Error,
    _Port,
    _make_port,
    build_cavity_problem,
    build_cavity_sheet,
    sheet_coefficient,
    solve_cavity_matrix,
)

H_CAV = 0.2      # mm
SEP = 10.0       # mm between the cap port and the IC port
R_PORT = 0.3     # mm
# A large plane so the closed form's infinite-sheet assumption holds; on a
# 20 mm plane the boundary pushes L up ~10 %.
PLANE = 60.0

MESHER = _mesh.Mesher.Config(maximum_size=0.8, minimum_angle=25)


def _cavity(shape) -> _pp.Layer:
    if shape.geom_type == "Polygon":
        shape = shapely.geometry.MultiPolygon([shape])
    return _pp.Layer(shape=shape, name="cavity",
                     conductance=sheet_coefficient(H_CAV))


def _solid_cavity(size=PLANE) -> _pp.Layer:
    return _cavity(shapely.geometry.box(-size / 2, -size / 2,
                                        size / 2, size / 2))


def _ports(sheet, cap_xys=((-SEP / 2, 0.0),), ic_xy=(SEP / 2, 0.0)):
    caps = [_make_port(f"C{i}", [xy], R_PORT, sheet.shape)
            for i, xy in enumerate(cap_xys, start=1)]
    ic = _make_port("U1", [ic_xy], R_PORT, sheet.shape)
    return caps, ic


def _solve_self_l(sheet, cap_xys=((-SEP / 2, 0.0),), ic_xy=(SEP / 2, 0.0)):
    caps, ic = _ports(sheet, cap_xys, ic_xy)
    m = solve_cavity_matrix(sheet, caps, ic, MESHER)
    return m, caps, ic


# --- the duality constant --------------------------------------------------


def test_sheet_coefficient_is_inverse_inductance_per_square():
    assert sheet_coefficient(0.2) == pytest.approx(1.0 / (MU0_H_PER_MM * 0.2))


def test_sheet_coefficient_rejects_nonphysical_height():
    with pytest.raises(Tier2Error):
        sheet_coefficient(0.0)


# --- the key physics test ----------------------------------------------------


def test_unbroken_cavity_matches_closed_form():
    """FEM vs (μ0·h/π)·ln(s/r) on a plane large enough to be ~infinite."""
    m, _, _ = _solve_self_l(_solid_cavity())
    fem_h = m[0, 0]
    closed_h = spreading_closed_form_h(H_CAV, R_PORT, SEP)
    assert fem_h == pytest.approx(closed_h, rel=0.20)
    # Sanity: a realistic cavity spreading term is a few hundred pH.
    assert 0.05e-9 < fem_h < 1e-9


def test_spreading_scales_linearly_with_cavity_height():
    m1, _, _ = _solve_self_l(_solid_cavity())
    tall = _pp.Layer(shape=_solid_cavity().shape, name="cavity",
                     conductance=sheet_coefficient(2 * H_CAV))
    m2, _, _ = _solve_self_l(tall)
    assert m2[0, 0] == pytest.approx(2.0 * m1[0, 0], rel=0.02)


def test_split_plane_raises_spreading_inductance():
    """A slot forcing the return current around it — where closed forms fail."""
    box = shapely.geometry.box(-PLANE / 2, -PLANE / 2, PLANE / 2, PLANE / 2)
    slot = shapely.geometry.box(-0.5, -PLANE / 2 + 2.0, 0.5, PLANE / 2)
    split = _cavity(box.difference(slot))
    m_split, _, _ = _solve_self_l(split)
    m_solid, _, _ = _solve_self_l(_solid_cavity())
    assert m_split[0, 0] > 1.5 * m_solid[0, 0]


# --- N-port behaviour ----------------------------------------------------------


def test_zero_current_ports_are_passive_observation_points():
    """The inactive ports of an N-port solve must not short the cavity: the
    active cap's self inductance barely moves when a second port is added."""
    sheet = _solid_cavity()
    alone, _, _ = _solve_self_l(sheet)
    together, _, _ = _solve_self_l(sheet, cap_xys=((-SEP / 2, 0.0), (0.0, 6.0)))
    assert together[0, 0] == pytest.approx(alone[0, 0], rel=0.05)


def test_port_matrix_is_reciprocal_and_coupled():
    sheet = _solid_cavity()
    m, _, _ = _solve_self_l(sheet, cap_xys=((-SEP / 2, 0.0), (0.0, 6.0)))
    assert m.shape == (2, 2)
    # Reciprocity: L[i][j] == L[j][i].
    assert m[0, 1] == pytest.approx(m[1, 0], rel=0.05)
    # Mutual coupling is real, positive, and weaker than either self term.
    assert 0.0 < m[0, 1] < min(m[0, 0], m[1, 1])


def test_isolated_cavity_island_yields_nan_row():
    """A cap on a plane island with no path to the target: not zero coupling,
    *unknown* — the row is NaN and the caller reports a split plane."""
    main = shapely.geometry.box(-20.0, -10.0, 0.0, 10.0)
    island = shapely.geometry.box(2.0, -10.0, 20.0, 10.0)
    sheet = _cavity(shapely.geometry.MultiPolygon([main, island]))
    # Cap on the island, IC on the main polygon.
    caps = [_make_port("C1", [(10.0, 0.0)], R_PORT, sheet.shape)]
    ic = _make_port("U1", [(-10.0, 0.0)], R_PORT, sheet.shape)
    assert caps[0].geom_index != ic.geom_index
    m = solve_cavity_matrix(sheet, caps, ic, MESHER)
    assert np.isnan(m[0, 0])


# --- mesh reuse ---------------------------------------------------------------------


def test_value_only_resolve_reuses_mesh_assembly(caplog):
    """Solves 2…N of a cavity differ only in source magnitudes, so the mesh
    + Laplacian assembly cache must hit — that is what makes an N-cap cavity
    affordable."""
    import logging
    sheet = _solid_cavity(size=20.0)
    caps, ic = _ports(sheet, cap_xys=((-SEP / 2, 0.0), (0.0, 6.0)))
    with caplog.at_level(logging.INFO, logger="pdnsolver.solver"):
        solve_cavity_matrix(sheet, caps, ic,
                            _mesh.Mesher.Config(maximum_size=1.5,
                                                minimum_angle=25))
    reuses = sum(1 for r in caplog.records
                 if "value-only re-solve" in r.getMessage())
    assert reuses >= 1


# --- problem construction -------------------------------------------------------------


def test_build_cavity_problem_wires_one_source_per_cap():
    sheet = _solid_cavity()
    caps, ic = _ports(sheet, cap_xys=((-SEP / 2, 0.0), (0.0, 6.0)))
    prob = build_cavity_problem(sheet, caps, ic, [1.0, 0.0])
    (network,) = prob.networks
    assert len(network.connections) == 3          # 2 caps + shared IC
    assert len(network.elements) == 2
    assert [e.current for e in network.elements] == [1.0, 0.0]
    # Every source draws from the same IC node — the matrix's reference.
    assert len({e.f for e in network.elements}) == 1


def test_build_cavity_sheet_intersects_the_two_planes():
    rail = shapely.geometry.box(0.0, 0.0, 10.0, 10.0)
    ret = shapely.geometry.box(5.0, 0.0, 20.0, 10.0)
    shapes = {(1, 0): rail, (2, 1): ret}
    sheet = build_cavity_sheet(1, 2, {0}, {1}, shapes, H_CAV, "c")
    assert sheet.shape.area == pytest.approx(50.0)
    assert sheet.conductance == pytest.approx(sheet_coefficient(H_CAV))


def test_build_cavity_sheet_rejects_non_overlapping_planes():
    shapes = {
        (1, 0): shapely.geometry.box(0.0, 0.0, 4.0, 10.0),
        (2, 1): shapely.geometry.box(6.0, 0.0, 10.0, 10.0),
    }
    with pytest.raises(Tier2Error):
        build_cavity_sheet(1, 2, {0}, {1}, shapes, H_CAV, "c")


def test_port_seeds_on_copper_when_via_sits_in_an_antipad():
    """The cap's vias sit in anti-pad holes; the port must still land on
    copper or the solver would drop the network as an off-copper terminal."""
    box = shapely.geometry.box(-10.0, -10.0, 10.0, 10.0)
    antipad = shapely.geometry.Point(0.0, 0.0).buffer(0.5)
    sheet = _cavity(box.difference(antipad))
    port = _make_port("C1", [(0.0, 0.0)], 0.2, sheet.shape)
    assert len(port.points) == 1
    assert sheet.shape.covers(port.points[0])
    assert port.geom_index == 0


def test_multi_via_port_is_a_disc_union_not_a_hull():
    """A hull over a device's spread-out pins swallows any capacitor port
    inside its footprint: the solver claims each mesh vertex for the first
    connection covering it, so those caps read the device's own potential and
    their spreading inductance comes out as exactly zero."""
    sheet = _solid_cavity()
    port = _make_port("U1", [(-3.0, 0.0), (3.0, 0.0)], R_PORT, sheet.shape)
    assert len(port.points) == 2 and len(port.discs) == 2
    # The gap between the two vias belongs to nobody — a hull would cover it.
    assert not port.region.covers(shapely.geometry.Point(0.0, 0.0))
    assert port.region.area == pytest.approx(sum(d.area for d in port.discs))


def test_ports_inside_a_device_footprint_keep_their_own_potential():
    """Regression for the hull bug, end to end: a cap port between two IC
    pins must report a real self inductance. With a hull it read exactly 0 —
    the cap's mesh vertices had been claimed by the IC's own node.
    (Tolerances are in henries: 0.01 nH = 1e-11.)"""
    sheet = _solid_cavity()
    ic = _make_port("U1", [(-3.0, 8.0), (3.0, 8.0)], R_PORT, sheet.shape)
    cap = _make_port("C1", [(0.0, 8.0)], R_PORT, sheet.shape)   # between them
    m = solve_cavity_matrix(sheet, [cap], ic, MESHER)
    assert m[0, 0] > 1e-11


def test_each_port_is_exactly_one_connection():
    """pdnsolver maps one node to one representative vertex, so a multi-via
    port must be a single Connection whose region is the disc union — two
    Connections sharing a node raise "Duplicate connection vertices"."""
    sheet = _solid_cavity()
    caps, _ = _ports(sheet, cap_xys=((-SEP / 2, 0.0),))
    ic_multi = _make_port("U1", [(4.0, 0.0), (6.0, 0.0)], R_PORT, sheet.shape)
    prob = build_cavity_problem(sheet, caps, ic_multi, [1.0])
    (network,) = prob.networks
    assert len(network.connections) == 2                      # 1 cap + 1 IC
    assert len({c.node_id for c in network.connections}) == 2
    ic_conn = network.connections[0]
    assert ic_conn.region.geom_type == "MultiPolygon"
    assert len(ic_conn.region.geoms) == 2


# --- end to end: real board geometry through all three tiers -------------------------


def test_run_tier2_and_tier3_on_a_flooded_plane_board():
    """The whole chain on a synthetic board whose planes are really flooded
    (with anti-pads punched around every via) — the path the GUI takes."""
    from fypa.altium.loader import _layer_z_centers_mm
    from fypa.altium_geometry import build_net_layer_shapes
    from fypa.caploop.identify import identify_capacitors
    from fypa.caploop.tier1 import mounted_inductance
    from fypa.caploop.tier2_fem import run_tier2
    from fypa.caploop.tier3 import build_ic_geometry, total_loop
    from tests.test_caploop_identify import (
        GND, PWR, RAILS, _directives, _pad, _standard_cap_project, _via,
    )

    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, PWR), _pad(0.5, 0.0, GND),
              _pad(5.0, 5.0, PWR, comp=1), _pad(5.0, 5.0, GND, comp=1)),
        vias=(_via(-0.9, 0.0, PWR), _via(-1.05, 0.0, PWR),
              _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
              _via(5.2, 5.0, PWR), _via(4.8, 5.0, GND)),
    )
    enabled = proj.enabled_copper_layer_ids()
    shapes = build_net_layer_shapes(proj, enabled)
    caps = identify_capacitors(proj, RAILS, metadata_directives=_directives(),
                               net_layer_shapes=shapes)
    assert len(caps) == 1 and caps[0].cavity is not None

    results, matrices = run_tier2(proj, caps, shapes, RAILS,
                                  mesher_config=MESHER)
    res = results["C1"]
    assert res.reason == "" and res.spread_h is not None
    assert 0.0 < res.spread_h < 5e-9
    assert len(matrices) == 1
    assert matrices[0].labels == ("C1",)
    assert matrices[0].matrix.shape == (1, 1)
    assert matrices[0].h_cav_mm == pytest.approx(caps[0].cavity.h_cav_mm)

    z = _layer_z_centers_mm(proj, enabled)
    ic = build_ic_geometry(proj, caps[0], {PWR}, {GND}, enabled, z)
    t3 = total_loop(caps[0], mounted_inductance(caps[0]), res.spread_h, ic)
    assert t3 is not None and not t3.is_partial
    assert t3.total_h > res.spread_h        # the loop adds the via/escape terms
    assert 0.1e-9 < t3.total_h < 20e-9


def test_coincident_ports_are_clustered():
    from fypa.caploop.tier2_fem import _cluster_coincident

    # Ports 0 and 2 share a via (26 µm apart); port 1 is 1 mm away.
    reps = _cluster_coincident(
        [[(0.0, 0.0)], [(1.0, 0.0)], [(0.026, 0.0)]], 0.25)
    assert reps[0] == reps[2] != reps[1]


def test_disjoint_radii_shrink_to_keep_discs_apart():
    from fypa.caploop.tier2_fem import _disjoint_radii

    radii = _disjoint_radii([[(0.0, 0.0)], [(1.0, 0.0)]], [0.5, 0.5])
    # 0.45 × 1.0 mm separation, so the two discs cannot touch.
    assert radii == [pytest.approx(0.45), pytest.approx(0.45)]
    assert sum(radii) < 1.0
    # A lone port keeps its requested radius.
    assert _disjoint_radii([[(0.0, 0.0)]], [0.5]) == [0.5]


def test_a_cap_on_the_targets_own_via_reports_zero_with_a_reason():
    """A capacitor mounted on the IC's via-in-pad is electrically the same
    cavity port: there is no plane spreading between them. That must be
    stated, not emitted as a bare zero that reads like a computed value —
    and it must not enter the port matrix, which has to stay reciprocal."""
    from fypa.altium_geometry import build_net_layer_shapes
    from fypa.caploop.identify import identify_capacitors
    from fypa.caploop.tier2_fem import run_tier2
    from tests.test_caploop_identify import (
        GND, PWR, RAILS, _directives, _pad, _standard_cap_project, _via,
    )

    # The sink's pins sit on the capacitor's own escape vias.
    directives = _directives()
    for d in directives:
        for name, term in d["terminals"].items():
            xy = (-0.9, 0.0) if name in ("P",) else (0.9, 0.0)
            for pin in term["pins"]:
                pin["x_mm"], pin["y_mm"] = xy

    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, PWR), _pad(0.5, 0.0, GND),
              _pad(-0.9, 0.0, PWR, comp=1), _pad(0.9, 0.0, GND, comp=1)),
    )
    shapes = build_net_layer_shapes(proj, proj.enabled_copper_layer_ids())
    caps = identify_capacitors(proj, RAILS, metadata_directives=directives,
                               net_layer_shapes=shapes)
    results, matrices = run_tier2(proj, caps, shapes, RAILS,
                                  mesher_config=MESHER)
    res = results["C1"]
    assert res.spread_h == 0.0
    assert "shares the target's via" in res.reason
    # No independent port left, so no matrix for this cavity.
    assert matrices == []


def test_port_matrix_is_exactly_reciprocal_once_ports_are_independent():
    """With coincident ports merged out and discs kept disjoint, the matrix
    obeys reciprocity to numerical precision — the property a downstream
    Z(f) solve relies on."""
    sheet = _solid_cavity()
    m, _, _ = _solve_self_l(sheet, cap_xys=((-SEP / 2, 0.0), (0.0, 6.0)))
    assert abs(m[0, 1] - m[1, 0]) < 1e-15


def test_run_tier2_skips_caps_without_a_target():
    from fypa.altium_geometry import build_net_layer_shapes
    from fypa.caploop.identify import identify_capacitors
    from fypa.caploop.tier2_fem import run_tier2
    from tests.test_caploop_identify import RAILS, _standard_cap_project

    proj = _standard_cap_project()
    shapes = build_net_layer_shapes(proj, proj.enabled_copper_layer_ids())
    caps = identify_capacitors(proj, RAILS, net_layer_shapes=shapes)
    results, matrices = run_tier2(proj, caps, shapes, RAILS,
                                  mesher_config=MESHER)
    assert matrices == []
    assert results["C1"].spread_h is None
    assert results["C1"].reason == "no target device"
