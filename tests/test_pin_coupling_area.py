"""Area-weighted multi-pin star coupling resistances."""
from __future__ import annotations

import pytest

from fypa.altium.loader import (
    AREA_WEIGHTED_PIN_COUPLING,
    SolveSettings,
    _pin_coupling_resistances,
)


def test_equal_areas_keep_base_r():
    rs = _pin_coupling_resistances([1.0, 1.0, 1.0], 0.1, area_weighted=True)
    assert rs == [0.1, 0.1, 0.1]


def test_double_area_halves_r():
    rs = _pin_coupling_resistances([1.0, 2.0], 0.1, area_weighted=True)
    # A_mean = 1.5 → R = 0.1 * 1.5/1 = 0.15, 0.1 * 1.5/2 = 0.075
    assert rs[0] == pytest.approx(0.15)
    assert rs[1] == pytest.approx(0.075)
    # Conductance ratio matches area ratio
    assert (1.0 / rs[1]) / (1.0 / rs[0]) == pytest.approx(2.0)


def test_area_weighted_off_equal_r():
    rs = _pin_coupling_resistances([1.0, 10.0], 0.2, area_weighted=False)
    assert rs == [0.2, 0.2]


def test_degenerate_areas_fall_back_to_equal_r():
    rs = _pin_coupling_resistances([0.0, 0.0], 0.1, area_weighted=True)
    assert rs == [0.1, 0.1]


def test_mixed_zero_uses_mean_for_missing():
    rs = _pin_coupling_resistances([2.0, 0.0], 0.1, area_weighted=True)
    # only valid area is 2.0 → mean = 2.0; missing uses mean → both R = 0.1
    assert rs == [0.1, 0.1]


def test_solve_settings_round_trip_area_weighted_flag():
    assert AREA_WEIGHTED_PIN_COUPLING is False
    s = SolveSettings(area_weighted_pin_coupling=True)
    s.apply_to_modules()
    try:
        from fypa.altium import loader as L
        assert L.AREA_WEIGHTED_PIN_COUPLING is True
        restored = SolveSettings.from_metadata({
            "physics_constants": {"area_weighted_pin_coupling": True},
        })
        assert restored.area_weighted_pin_coupling is True
    finally:
        SolveSettings().apply_to_modules()


def test_terminal_connections_emits_unequal_resistors_when_weighted():
    """``_terminal_connections`` must wire area-scaled star resistors."""
    from types import SimpleNamespace

    import shapely.geometry
    from pdnsolver import problem as pp

    from fypa.altium.annotations import TerminalPin, TerminalSpec
    from fypa.altium.extract import Pt2D
    from fypa.altium.loader import _terminal_connections

    poly_small = shapely.geometry.box(0, 0, 1, 1)   # area 1
    poly_large = shapely.geometry.box(0, 0, 2, 2)   # area 4
    layer = SimpleNamespace(shape=shapely.geometry.box(-10, -10, 10, 10))
    term = TerminalSpec(pins=(
        TerminalPin(
            pad_designator="1", layer_id=1, net_index=0,
            point=Pt2D(0.5, 0.5), pad_polygon=poly_small,
        ),
        TerminalPin(
            pad_designator="2", layer_id=1, net_index=0,
            point=Pt2D(1.0, 1.0), pad_polygon=poly_large,
        ),
    ))
    layers = {(1, 0): layer}
    SolveSettings(area_weighted_pin_coupling=True).apply_to_modules()
    try:
        _conns, aux = _terminal_connections(
            term, pp.NodeID(), layers, coupling_resistance_ohm=0.1,
        )
        assert len(aux) == 2
        rs = sorted(float(r.resistance) for r in aux)
        # A_mean = 2.5 → R = 0.1*2.5/1 = 0.25, 0.1*2.5/4 = 0.0625
        assert rs[0] == pytest.approx(0.0625)
        assert rs[1] == pytest.approx(0.25)
    finally:
        SolveSettings().apply_to_modules()
