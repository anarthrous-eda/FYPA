"""Gerber drill hits only connect layers whose copper reaches the drill wall.

A drill file carries no pad data, so the importer gives every plated hit a
heuristic pad disc (drill + 0.3 mm) on every layer of its span. Where a
layer's copper is cleared back from the hole, that disc used to reach
across the clearance and short the hole to the surrounding pour (Hermes
Mezz: a 2.7 mm no-net mounting hole with 0.1 mm clearances tied GND to the
+3V3D pour on an inner layer).
"""
from __future__ import annotations

import shapely.geometry as sg

from fypa.altium.extract import NO_NET, Pt2D, RawVia, via_connected_layers
from fypa.gerber.extract import _mark_unconnected_via_layers

TOP, INNER, BOT = 1, 2, 32
ORDER = [TOP, INNER, BOT]


def _via(x, y, hole):
    return RawVia(center=Pt2D(x, y), diameter_mm=hole + 0.3,
                  hole_diameter_mm=hole, layer_start=TOP, layer_end=BOT,
                  net_index=NO_NET)


def _layers():
    hole = sg.Point(0, 0)
    pour = sg.box(-10, -10, 10, 10)
    # TOP / BOTTOM: a 2.7 mm pad flashed at drill size inside a 0.1 mm
    # clearance. INNER: the pour with only the clearance void, no pad.
    void = pour.difference(hole.buffer(1.45))
    pad = hole.buffer(1.35)
    return {TOP: void.union(pad), INNER: void, BOT: void.union(pad)}


def test_layer_cleared_back_from_hole_is_unconnected():
    (v,) = _mark_unconnected_via_layers([_via(0, 0, 2.7)], _layers(), ORDER)
    assert v.unconnected_layer_ids == (INNER,)
    assert via_connected_layers(v, ORDER) == [TOP, BOT]


def test_copper_at_drill_wall_connects_every_layer():
    solid = sg.box(-10, -10, 10, 10)
    layers = {TOP: solid, INNER: solid, BOT: solid}
    (v,) = _mark_unconnected_via_layers([_via(0, 0, 0.3)], layers, ORDER)
    assert v.unconnected_layer_ids == ()


def test_unconnected_layer_gets_no_pad_disc():
    from fypa.altium_geometry import _via_on_layer
    (v,) = _mark_unconnected_via_layers([_via(0, 0, 2.7)], _layers(), ORDER)
    assert _via_on_layer(v, TOP, ORDER)
    assert not _via_on_layer(v, INNER, ORDER)
    assert _via_on_layer(v, BOT, ORDER)


def test_metadata_dict_form():
    rec = {"unconnected_layer_ids": [INNER]}
    assert via_connected_layers(rec, ORDER) == [TOP, BOT]
    assert via_connected_layers({}, ORDER) == ORDER


def test_old_pickles_without_the_field_still_load():
    # Extract caches pickle RawVia by constructor; a record written before
    # the field existed has one fewer argument.
    v = RawVia(Pt2D(0, 0), 0.5, 0.2, TOP, BOT, NO_NET, 0, "")
    assert v.unconnected_layer_ids == ()
