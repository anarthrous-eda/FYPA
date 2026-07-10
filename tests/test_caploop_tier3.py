"""Tier-3 full cap→plane→IC loop assembly (fypa.caploop.tier3).

Checks the series assembly, the partial-total contract when the target's via
geometry is unknown, the degenerate-geometry fallback, and the per-rail
parallel rollup.
"""
from __future__ import annotations

import dataclasses
import math

import pytest

from fypa.caploop.constants import CapLoopSettings
from fypa.caploop.identify import EscapeVia
from fypa.caploop.tier1 import mounted_inductance, via_pair_loop_h
from fypa.caploop.tier3 import (
    IcGeometry,
    barrel_pair_loop_h,
    build_ic_geometry,
    ic_via_loop_h,
    rail_rollup,
    total_loop,
)
from tests.test_caploop_identify import (
    GND,
    PWR,
    _directives,
    _identify,
    _pad,
    _standard_cap_project,
    _via,
)

SPREAD_H = 0.3e-9


def _cap(**kwargs):
    cap = _identify(_standard_cap_project(),
                    metadata_directives=_directives())[0]
    return dataclasses.replace(cap, **kwargs) if kwargs else cap


def _ic_geometry(z_mount=0.0175):
    def ev(i, x, y):
        return EscapeVia(via_index=i, x_mm=x, y_mm=y, dist_mm=0.2,
                         escape_mm=0.0, drill_mm=0.3, span=(1, 39, 40, 32))
    return IcGeometry(
        vias_rail=(ev(10, 4.5, 5.0), ev(11, 4.6, 5.0)),
        vias_return=(ev(12, 5.5, 5.0), ev(13, 5.6, 5.0)),
        mount_layer_id=1, z_mount_mm=z_mount)


# --- barrel pair ------------------------------------------------------------


def test_barrel_pair_matches_via_pair_form():
    assert barrel_pair_loop_h(0.3, 1.0, 1.0) == \
        pytest.approx(via_pair_loop_h(1.0, 1.0, 0.15))


def test_barrel_pair_degenerate_geometry_uses_fallback():
    s = CapLoopSettings(fallback_via_loop_nh=1.5)
    assert barrel_pair_loop_h(0.0, 1.0, 1.0, s) == pytest.approx(1.5e-9)
    assert barrel_pair_loop_h(0.3, 0.0, 1.0, s) == pytest.approx(1.5e-9)
    assert barrel_pair_loop_h(0.3, 1.0, 0.0, s) == pytest.approx(1.5e-9)


def test_ic_via_loop_derates_by_pair_count():
    ic = _ic_geometry()
    # Cavity mid-z of the fixture stackup ≈ 0.42; IC mounts on Top (0.0175).
    l_h, pairs = ic_via_loop_h(ic, 0.42)
    assert pairs == 2
    assert l_h > 0.0
    single, _ = ic_via_loop_h(
        dataclasses.replace(ic, vias_rail=ic.vias_rail[:1],
                            vias_return=ic.vias_return[:1]), 0.42)
    assert single > l_h        # one pair carries more L than two in parallel


# --- total loop -----------------------------------------------------------------


def test_total_loop_is_the_series_sum():
    cap = _cap()
    t1 = mounted_inductance(cap)
    ic = _ic_geometry()
    t3 = total_loop(cap, t1, SPREAD_H, ic)
    assert not t3.is_partial
    assert t3.spread_h == SPREAD_H
    assert t3.total_h == pytest.approx(
        t3.escape_h + t3.via_loop_cap_h + t3.spread_h + t3.via_loop_ic_h)
    # Every term contributes; none silently zero.
    assert t3.escape_h > 0 and t3.via_loop_cap_h > 0 and t3.via_loop_ic_h > 0


def test_total_loop_uses_fem_spread_not_the_closed_form():
    cap = _cap()
    t1 = mounted_inductance(cap)
    low = total_loop(cap, t1, 0.1e-9, _ic_geometry())
    high = total_loop(cap, t1, 0.9e-9, _ic_geometry())
    assert high.total_h - low.total_h == pytest.approx(0.8e-9)


def test_total_loop_without_ic_geometry_is_partial_lower_bound():
    cap = _cap()
    t1 = mounted_inductance(cap)
    t3 = total_loop(cap, t1, SPREAD_H, None)
    assert t3.is_partial and t3.reason
    assert t3.via_loop_ic_h == 0.0 and t3.ic_pairs == 0
    full = total_loop(cap, t1, SPREAD_H, _ic_geometry())
    assert t3.total_h < full.total_h


def test_total_loop_none_without_a_spreading_term():
    cap = _cap()
    assert total_loop(cap, mounted_inductance(cap), None,
                      _ic_geometry()) is None


def test_total_loop_none_when_tier1_fell_back():
    # No cavity → Tier-1 is a fallback estimate; a "total" built on it would
    # imply a precision the geometry doesn't support.
    cap = _identify(_standard_cap_project(), net_layer_shapes=None)[0]
    t1 = mounted_inductance(cap)
    assert t1.is_fallback
    assert total_loop(cap, t1, SPREAD_H, _ic_geometry()) is None


# --- IC geometry association -------------------------------------------------------


def test_build_ic_geometry_finds_target_vias():
    # Put the sink's pads + escape vias on the board at the pin coordinates
    # the _directives() fixture reports (5.0, 5.0).
    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, PWR), _pad(0.5, 0.0, GND),
              _pad(5.0, 5.0, PWR, comp=1), _pad(5.0, 5.0, GND, comp=1)),
        vias=(_via(-0.9, 0.0, PWR), _via(-1.05, 0.0, PWR),
              _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
              _via(5.2, 5.0, PWR), _via(4.8, 5.0, GND)),
    )
    cap = _identify(proj, metadata_directives=_directives())[0]
    z = {1: 0.0175, 39: 0.2525, 40: 0.5875, 32: 0.8225}
    ic = build_ic_geometry(proj, cap, {PWR}, {GND}, [1, 39, 40, 32], z)
    assert ic is not None
    assert len(ic.vias_rail) == 1 and len(ic.vias_return) == 1
    assert ic.mount_layer_id == 1


def test_build_ic_geometry_none_without_return_pins():
    cap = _cap(target_pins_n=())
    assert build_ic_geometry(_standard_cap_project(), cap, {PWR}, {GND},
                             [1, 39, 40, 32], {1: 0.0}) is None


def test_build_ic_geometry_none_without_target():
    cap = _identify(_standard_cap_project())[0]   # no directives → no target
    assert build_ic_geometry(_standard_cap_project(), cap, {PWR}, {GND},
                             [1, 39, 40, 32], {1: 0.0}) is None


# --- rail rollup ---------------------------------------------------------------------


def test_rail_rollup_combines_caps_in_parallel():
    rows = [("+3V3", "C1", 2e-9), ("+3V3", "C2", 2e-9),
            ("+1V8", "C3", 1e-9)]
    out = rail_rollup(rows)
    assert out["+3V3"].cap_count == 2
    assert out["+3V3"].parallel_h == pytest.approx(1e-9)   # two 2 nH ‖
    assert out["+3V3"].min_h == pytest.approx(2e-9)
    assert out["+1V8"].parallel_h == pytest.approx(1e-9)


def test_rail_rollup_median_and_skips_nonpositive():
    rows = [("R", "C1", 1e-9), ("R", "C2", 3e-9), ("R", "C3", 5e-9),
            ("R", "C4", 0.0)]
    out = rail_rollup(rows)
    assert out["R"].cap_count == 3
    assert out["R"].median_h == pytest.approx(3e-9)


def test_rail_rollup_empty():
    assert rail_rollup([]) == {}
