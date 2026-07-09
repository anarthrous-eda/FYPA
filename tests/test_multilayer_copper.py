"""Multilayer (layer id 74) copper primitive distribution.

Tracks, regions, and fills placed on Altium's Multi Layer with an assigned
net must appear on every enabled signal copper layer. Internal planes are
excluded. Unassigned (NO_NET) multilayer primitives stay excluded from the mesh.
"""
from __future__ import annotations

from pathlib import Path

from fypa.altium.extract import (
    NO_NET,
    NO_POLYGON,
    ExtractedProject,
    Pt2D,
    RawFill,
    RawNet,
    RawRegion,
    RawStackupLayer,
    RawTrack,
)
from fypa.altium_geometry import (
    MULTI_LAYER_PAD_LAYER_ID,
    build_layer_geometries,
    build_layer_geometry,
    build_net_layer_shapes,
)


def _two_layer_stackup() -> tuple[RawStackupLayer, ...]:
    return (
        RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.2, next_layer_id=32,
            is_plane=False, plane_net_name=None, mech_enabled=True,
        ),
        RawStackupLayer(
            layer_id=32, name="Bottom", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0,
            is_plane=False, plane_net_name=None, mech_enabled=True,
        ),
    )


def _stackup_with_internal_plane() -> tuple[RawStackupLayer, ...]:
    return (
        RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.2, next_layer_id=39,
            is_plane=False, plane_net_name=None, mech_enabled=True,
        ),
        RawStackupLayer(
            layer_id=39, name="GND Plane", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.2, next_layer_id=32,
            is_plane=True, plane_net_name="GND", mech_enabled=True,
        ),
        RawStackupLayer(
            layer_id=32, name="Bottom", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0,
            is_plane=False, plane_net_name=None, mech_enabled=True,
        ),
    )


def _proj(**overrides) -> ExtractedProject:
    base = {
        "prjpcb_path": Path("t.PrjPcb"),
        "pcbdoc_path": Path("t.PcbDoc"),
        "tracks": (), "arcs": (), "vias": (), "pads": (), "regions": (),
        "shape_based_regions": (), "fills": (),
        "pcb_components": (), "nets": (RawNet("+5V"),), "stackup": _two_layer_stackup(),
        "sch_components": (), "compiled_netlist": None,
    }
    base.update(overrides)
    return ExtractedProject(**base)


def _horizontal_track(layer_id: int, net_index: int) -> RawTrack:
    return RawTrack(
        a=Pt2D(0.0, 0.0),
        b=Pt2D(10.0, 0.0),
        width_mm=0.5,
        layer_id=layer_id,
        net_index=net_index,
        polygon_index=NO_POLYGON,
        is_polygon_outline=False,
        component_index=-1,
        is_keepout=False,
    )


def _square_region(layer_id: int, net_index: int) -> RawRegion:
    outline = (
        Pt2D(0.0, 0.0),
        Pt2D(5.0, 0.0),
        Pt2D(5.0, 5.0),
        Pt2D(0.0, 5.0),
    )
    return RawRegion(
        outline=outline,
        holes=(),
        layer_id=layer_id,
        net_index=net_index,
        kind=0,
        is_polygon_outline=False,
        is_keepout=False,
        is_board_cutout=False,
    )


def _axis_aligned_fill(layer_id: int, net_index: int) -> RawFill:
    return RawFill(
        x1_mm=0.0, y1_mm=0.0, x2_mm=4.0, y2_mm=3.0,
        rotation_deg=0.0,
        layer_id=layer_id,
        net_index=net_index,
        is_keepout=False,
    )


def test_multilayer_track_with_net_on_all_layers():
    proj = _proj(tracks=(_horizontal_track(MULTI_LAYER_PAD_LAYER_ID, 0),))
    enabled = [1, 32]
    shapes = build_net_layer_shapes(proj, enabled)

    assert (1, 0) in shapes and not shapes[(1, 0)].is_empty
    assert (32, 0) in shapes and not shapes[(32, 0)].is_empty


def test_multilayer_track_no_net_excluded():
    proj = _proj(tracks=(_horizontal_track(MULTI_LAYER_PAD_LAYER_ID, NO_NET),))
    enabled = [1, 32]
    shapes = build_net_layer_shapes(proj, enabled)

    assert (1, NO_NET) not in shapes
    assert (32, NO_NET) not in shapes


def test_multilayer_region_fill_with_net():
    proj = _proj(regions=(_square_region(MULTI_LAYER_PAD_LAYER_ID, 0),))
    enabled = [1, 32]
    region_shapes = build_net_layer_shapes(proj, enabled)
    assert (1, 0) in region_shapes and not region_shapes[(1, 0)].is_empty
    assert (32, 0) in region_shapes and not region_shapes[(32, 0)].is_empty

    proj = _proj(fills=(_axis_aligned_fill(MULTI_LAYER_PAD_LAYER_ID, 0),))
    fill_shapes = build_net_layer_shapes(proj, enabled)
    assert (1, 0) in fill_shapes and not fill_shapes[(1, 0)].is_empty
    assert (32, 0) in fill_shapes and not fill_shapes[(32, 0)].is_empty


def test_single_layer_track_unchanged():
    proj = _proj(tracks=(_horizontal_track(1, 0),))
    enabled = [1, 32]
    shapes = build_net_layer_shapes(proj, enabled)

    assert (1, 0) in shapes and not shapes[(1, 0)].is_empty
    assert (32, 0) not in shapes


def test_multilayer_track_not_on_internal_plane():
    proj = _proj(
        nets=(RawNet("SIG"), RawNet("GND")),
        stackup=_stackup_with_internal_plane(),
        tracks=(_horizontal_track(MULTI_LAYER_PAD_LAYER_ID, 0),),
    )
    enabled = [1, 39, 32]
    shapes = build_net_layer_shapes(proj, enabled)

    assert (1, 0) in shapes and not shapes[(1, 0)].is_empty
    assert (32, 0) in shapes and not shapes[(32, 0)].is_empty
    assert (39, 0) not in shapes


def test_multilayer_track_in_build_layer_geometries_signal_layers_only():
    proj = _proj(
        nets=(RawNet("SIG"), RawNet("GND")),
        stackup=_stackup_with_internal_plane(),
        tracks=(_horizontal_track(MULTI_LAYER_PAD_LAYER_ID, 0),),
    )
    layers = {g.layer_id: g for g in build_layer_geometries(proj)}

    assert not layers[1].shape.is_empty
    assert not layers[32].shape.is_empty
    assert layers[39].is_plane
    assert (39, 0) not in build_net_layer_shapes(proj, [1, 39, 32])


def test_build_layer_geometry_without_shared_cache_includes_multilayer():
    proj = _proj(tracks=(_horizontal_track(MULTI_LAYER_PAD_LAYER_ID, 0),))
    top = build_layer_geometry(proj, 1, [1, 32])
    bottom = build_layer_geometry(proj, 32, [1, 32])

    assert not top.shape.is_empty
    assert not bottom.shape.is_empty
