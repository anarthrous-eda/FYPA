"""Split internal planes (``(Multiple Nets)``): each region floods its own net.

Altium keeps a split plane's regions as ``Polygons6`` records of type
``Split Plane``, each with its net; the stackup's plane net name is the
literal ``(Multiple Nets)``, which is no net. Read as one net, the layer had
no copper at all, so every via that reached its net only through the plane
was an island (one board: GND on P1-GND / P2-GND in 35 pieces).
"""
from __future__ import annotations

from pathlib import Path

import shapely.geometry as sg

from fypa.altium.extract import (
    ExtractedProject,
    Pt2D,
    RawNet,
    RawSplitPlane,
    RawStackupLayer,
    RawVia,
)
from fypa.altium.loader import _apply_net_remap
from fypa.altium_geometry import (
    _build_net_layer_buckets,
    _shapes_to_geometry_layers,
    build_net_layer_shapes,
    clear_net_layer_bucket_cache,
)

GND, AGND, V5, AGND2 = 0, 1, 2, 3
PLANE = 39


def _rect(x0, y0, x1, y1):
    return tuple(Pt2D(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _proj(**overrides) -> ExtractedProject:
    stackup = (
        RawStackupLayer(layer_id=1, name="Top", copper_thickness_mm=0.035,
                        dielectric_thickness_mm=0.2, next_layer_id=PLANE,
                        is_plane=False, plane_net_name=None, mech_enabled=True),
        RawStackupLayer(layer_id=PLANE, name="P1", copper_thickness_mm=0.035,
                        dielectric_thickness_mm=0.2, next_layer_id=32,
                        is_plane=True, plane_net_name="(Multiple Nets)",
                        mech_enabled=True),
        RawStackupLayer(layer_id=32, name="Bottom", copper_thickness_mm=0.035,
                        dielectric_thickness_mm=0.0, next_layer_id=0,
                        is_plane=False, plane_net_name=None, mech_enabled=True),
    )
    base = {
        "prjpcb_path": Path("s.PrjPcb"), "pcbdoc_path": Path("s.PcbDoc"),
        "tracks": (), "arcs": (), "pads": (), "regions": (), "shape_based_regions": (),
        "fills": (), "pcb_components": (),
        "nets": (RawNet("GND"), RawNet("AGND"), RawNet("+5V"), RawNet("AGND2")),
        "stackup": stackup, "sch_components": (),
        "board_outline": _rect(0, 0, 20, 10),
        "vias": (
            RawVia(Pt2D(2, 5), 0.6, 0.3, 1, 32, GND),
            RawVia(Pt2D(8, 5), 0.6, 0.3, 1, 32, GND),
            RawVia(Pt2D(15, 5), 0.6, 0.3, 1, 32, AGND),
        ),
        "split_planes": (
            # GND on the left with a +5V island inside it, AGND on the right,
            # 0.25 mm split line between them.
            RawSplitPlane(PLANE, GND, _rect(0.5, 0.5, 9.875, 9.5)),
            RawSplitPlane(PLANE, V5, _rect(4, 3, 6, 7)),
            RawSplitPlane(PLANE, AGND, _rect(10.125, 0.5, 19.5, 9.5)),
        ),
        "plane_clearance_mm": 0.25, "plane_relief_air_gap_mm": 0.2,
        "plane_relief_conductor_width_mm": 0.25, "plane_relief_entries": 4,
    }
    base.update(overrides)
    return ExtractedProject(**base)


def _plane_shapes(proj):
    clear_net_layer_bucket_cache()
    shapes = build_net_layer_shapes(proj, proj.enabled_copper_layer_ids(),
                                    include_vias=True)
    return {net: geom for (lid, net), geom in shapes.items() if lid == PLANE}


def test_each_split_region_floods_its_own_net():
    plane = _plane_shapes(_proj())
    assert set(plane) == {GND, AGND, V5}
    # Both GND vias sit on one piece of GND plane copper: one island.
    gnd = plane[GND]
    assert gnd.geom_type == "Polygon" or len(gnd.geoms) == 1
    assert gnd.covers(sg.Point(2, 5)) and gnd.covers(sg.Point(8, 5))
    # The +5V island is cut out of GND, and nothing crosses the split line.
    assert not gnd.intersects(sg.Point(5, 5))
    assert plane[V5].covers(sg.Point(5, 5))
    assert gnd.intersection(plane[AGND]).area == 0
    assert plane[AGND].covers(sg.Point(15, 5))


def test_split_region_layers_are_tagged_as_plane():
    proj = _proj()
    clear_net_layer_bucket_cache()
    layers = _shapes_to_geometry_layers(
        proj, build_net_layer_shapes(proj, proj.enabled_copper_layer_ids(),
                                     include_vias=True))
    tagged = {(L.layer_id, L.plane_net_index) for L in layers if L.is_plane}
    assert tagged == {(PLANE, GND), (PLANE, AGND), (PLANE, V5)}


def test_net_filter_builds_only_the_wanted_regions():
    proj = _proj()
    clear_net_layer_bucket_cache()
    buckets = _build_net_layer_buckets(proj, proj.enabled_copper_layer_ids(),
                                       include_vias=True, only_nets={AGND})
    assert {net for (lid, net) in buckets if lid == PLANE} == {AGND}


def test_unsplit_plane_is_unchanged():
    proj = _proj(split_planes=(), stackup=tuple(
        s if s.layer_id != PLANE else RawStackupLayer(
            layer_id=PLANE, name="P1", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.2, next_layer_id=32, is_plane=True,
            plane_net_name="GND", mech_enabled=True)
        for s in _proj().stackup))
    plane = _plane_shapes(proj)
    assert set(plane) == {GND, AGND}          # the sheet + AGND's via disc
    assert plane[GND].area > 150


def test_net_merge_remaps_split_regions():
    remapped = _apply_net_remap(_proj(), {AGND: GND})
    assert {sp.net_index for sp in remapped.split_planes} == {GND, V5}


def test_plane_whose_only_region_is_its_own_net_is_flooded_whole():
    named = tuple(
        s if s.layer_id != PLANE else RawStackupLayer(
            layer_id=PLANE, name="P1", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.2, next_layer_id=32, is_plane=True,
            plane_net_name="GND", mech_enabled=True)
        for s in _proj().stackup)
    whole = _plane_shapes(_proj(stackup=named, split_planes=()))[GND]
    one = _plane_shapes(_proj(stackup=named, split_planes=(
        RawSplitPlane(PLANE, GND, _rect(0.5, 0.5, 19.5, 9.5)),)))[GND]
    assert one.equals(whole)
