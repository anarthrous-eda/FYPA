"""Tier-1 closed-form mounted-inductance model (fypa.caploop.tier1).

Closed forms checked against hand-computed values, clamp/fallback paths, and
an end-to-end sanity band on the shared identify fixture (typical 0402 cap
geometry must land in the 0.1–10 nH decade or the units are wrong).
"""
from __future__ import annotations

import math

import pytest

from fypa.caploop.constants import MU0_H_PER_MM, CapLoopSettings
from fypa.caploop.tier1 import (
    escape_h,
    mounted_inductance,
    parallel_via_reduction,
    spreading_closed_form_h,
    via_pair_loop_h,
)
from tests.test_caploop_identify import (
    _directives,
    _identify,
    _standard_cap_project,
)


def test_via_pair_hand_value():
    # h=1 mm, s=1 mm, r=0.15 mm → (μ0/π)·acosh(3.333) ≈ 0.7495 nH
    l = via_pair_loop_h(1.0, 1.0, 0.15)
    expected = (MU0_H_PER_MM / math.pi) * math.acosh(1.0 / 0.3)
    assert l == pytest.approx(expected)
    assert l == pytest.approx(0.7495e-9, rel=1e-3)


def test_via_pair_scales_linearly_with_height():
    assert via_pair_loop_h(2.0, 1.0, 0.15) == \
        pytest.approx(2.0 * via_pair_loop_h(1.0, 1.0, 0.15))


def test_via_pair_clamps_overlapping_barrels():
    # s < 2r would NaN through acosh — clamped to the minimum argument.
    l = via_pair_loop_h(1.0, 0.2, 0.15)
    assert math.isfinite(l) and l > 0.0
    assert l == pytest.approx(
        (MU0_H_PER_MM / math.pi) * math.acosh(1.02))


def test_via_pair_degenerate_geometry_is_zero():
    assert via_pair_loop_h(0.0, 1.0, 0.15) == 0.0
    assert via_pair_loop_h(1.0, 1.0, 0.0) == 0.0


def test_parallel_reduction():
    assert parallel_via_reduction(1e-9, 2, 0.8) == pytest.approx(0.4e-9)
    assert parallel_via_reduction(1e-9, 1, 0.8) == pytest.approx(0.8e-9)
    assert parallel_via_reduction(1e-9, 0, 0.8) == pytest.approx(0.8e-9)


def test_escape_hand_value():
    # len=1 mm, w=0.5 mm, h_d=0.2 mm → μ0·0.2·1/0.5 ≈ 0.503 nH
    assert escape_h(1.0, 0.5, 0.2) == \
        pytest.approx(MU0_H_PER_MM * 0.4, rel=1e-9)
    assert escape_h(1.0, 0.5, 0.2) == pytest.approx(0.5027e-9, rel=1e-3)


def test_escape_zero_length_or_depth():
    assert escape_h(0.0, 0.5, 0.2) == 0.0
    assert escape_h(1.0, 0.5, 0.0) == 0.0


def test_spreading_hand_value():
    # Two-port form: h=0.3 mm, r_port=0.5, r_far=5 → (μ0·0.3/π)·ln(10)
    l = spreading_closed_form_h(0.3, 0.5, 5.0)
    assert l == pytest.approx(
        (MU0_H_PER_MM * 0.3 / math.pi) * math.log(10.0))
    assert l == pytest.approx(0.2763e-9, rel=1e-3)


def test_spreading_matches_via_pair_form():
    # Both are the same 2-D Laplace problem with two line sources, so for
    # s >> r the cavity spreading term and a via pair of length h agree.
    h, s, r = 0.3, 10.0, 0.3
    assert spreading_closed_form_h(h, r, s) == pytest.approx(
        via_pair_loop_h(h, s, r), rel=0.01)


def test_spreading_degenerate_radii():
    assert spreading_closed_form_h(0.3, 0.5, 0.5) == 0.0
    assert spreading_closed_form_h(0.3, 2.0, 1.0) == 0.0
    assert spreading_closed_form_h(0.0, 0.5, 5.0) == 0.0


# --- end-to-end on the identify fixture -----------------------------------


def test_mounted_inductance_typical_geometry_in_nh_band():
    cap = _identify(_standard_cap_project(),
                    metadata_directives=_directives())[0]
    res = mounted_inductance(cap)
    assert not res.is_fallback
    assert 0.1e-9 <= res.total_h <= 10e-9
    assert res.total_h == pytest.approx(
        res.escape_rail_h + res.escape_return_h
        + res.via_loop_h + res.spread_cf_h)
    # Fixture geometry: clusters at x≈∓0.975 → s≈1.95 mm; 4 escapes of
    # 0.3 mm drill → r=0.15; cavity GND(z=.2525)/PWR(z=.5875), h_cav=0.3,
    # depth=0.2525 → h_via = 0.4025.
    assert res.s_mm == pytest.approx(1.95)
    assert res.r_eff_mm == pytest.approx(0.15)
    assert res.h_via_mm == pytest.approx(0.4025)
    assert res.n_pairs == 2


def test_mounted_inductance_single_via_is_larger():
    import dataclasses
    cap = _identify(_standard_cap_project())[0]
    both = mounted_inductance(cap)
    single = mounted_inductance(
        dataclasses.replace(cap, vias_rail=cap.vias_rail[:1],
                            vias_return=cap.vias_return[:1]))
    assert single.via_loop_h > both.via_loop_h


def test_mounted_inductance_fallback_without_cavity():
    cap = _identify(_standard_cap_project(), net_layer_shapes=None)[0]
    settings = CapLoopSettings()
    res = mounted_inductance(cap, settings)
    assert res.is_fallback
    assert res.total_h == pytest.approx(settings.fallback_via_loop_nh * 1e-9)


def test_mounted_inductance_target_shrinks_spreading():
    # The fixture SINK sits 5√2 mm away ≈ default 5 mm — move it closer via
    # a nearer directive and the closed-form spreading term must shrink.
    near = _directives()
    for d in near:
        for t in d["terminals"].values():
            for p in t["pins"]:
                p["x_mm"], p["y_mm"] = 2.0, 0.0
    cap_near = _identify(_standard_cap_project(),
                         metadata_directives=near)[0]
    cap_far = _identify(_standard_cap_project())[0]  # default r_far = 5 mm
    assert mounted_inductance(cap_near).spread_cf_h < \
        mounted_inductance(cap_far).spread_cf_h
