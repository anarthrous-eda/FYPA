"""Hatched polygon pours import as copper on the parent polygon's net.

GitHub issue #41. Altium realises a *solid* pour as `Regions6` fill with the
perimeter kept as `is_polygon_outline` boundary artwork, and FYPA rightly drops
that artwork (otherwise a rounded-corner pour gains a spurious band of copper
along its border). A **hatched** — or "outlines only" — pour is realised the
other way round: its copper *is* `Tracks6`/`Arcs6`, perimeter included, and the
net assignment lives on the `Polygons6` record rather than on each primitive.

Applying the solid-pour rules to it produced exactly the two symptoms reported:

1. the outer perimeter vanished, because it carries the polygon-outline flag;
2. the surviving hatch lines landed on no net, because only the region
   extractors inherited the parent polygon's net.

A third hazard is pinned here too: altium_monkey parses a missing `NET` field
as ``int(record.get('NET', 0))``, so a net-less polygon is indistinguishable
from one genuinely on net index 0. Inheriting that blindly attaches pour copper
to whichever net happens to sit first in `Nets6` — 27 of the 134 polygons in
the Corvette example carry no `NET` field at all.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fypa.altium.extract import (
    NO_NET,
    NO_POLYGON,
    ExtractedProject,
    Pt2D,
    RawArc,
    RawNet,
    RawStackupLayer,
    RawTrack,
    _extract_arcs,
    _extract_tracks,
)
from fypa.altium_geometry import (
    _arc_is_copper,
    _track_is_copper,
    build_net_layer_shapes,
)

TOP = 1
BOTTOM = 32


# --------------------------------------------------------------------------
# altium_monkey stand-ins. `_extract_tracks` / `_extract_arcs` only ever touch
# `pcb.tracks`, `pcb.arcs` and `pcb.polygons`, so a duck-typed PcbDoc pins the
# extraction contract without needing a hatched .PcbDoc on disk (every design
# in ExampleDesigns/ is 100 % HATCHSTYLE=Solid).
# --------------------------------------------------------------------------
def _mk_polygon(net_field: str | None, hatch_style: str | None):
    """One `Polygons6` record. ``net_field`` is the raw record's NET string —
    ``None`` means the field is absent, which is how Altium writes a net-less
    polygon and how the Corvette example stores 27 of its pours."""
    return SimpleNamespace(
        net=int(net_field) if net_field not in (None, "") else 0,
        hatch_style=hatch_style,
        _raw_record={"NET": net_field, "HATCHSTYLE": hatch_style},
    )


def _mk_track(*, net_index=None, polygon_index=NO_POLYGON, is_outline=False):
    return SimpleNamespace(
        start_x_mils=0.0, start_y_mils=0.0,
        end_x_mils=100.0, end_y_mils=0.0,
        width_mils=10.0, layer=TOP,
        net_index=net_index, polygon_index=polygon_index,
        is_polygon_outline=is_outline, component_index=None, is_keepout=False,
    )


def _mk_arc(*, net_index=None, polygon_index=NO_POLYGON, is_outline=False):
    return SimpleNamespace(
        center_x_mils=0.0, center_y_mils=0.0, radius_mils=50.0,
        start_angle=0.0, end_angle=90.0, width_mils=10.0, layer=TOP,
        net_index=net_index, polygon_index=polygon_index,
        is_polygon_outline=is_outline, is_keepout=False,
    )


def _mk_pcb(polygons, tracks=(), arcs=()):
    return SimpleNamespace(polygons=list(polygons),
                           tracks=list(tracks), arcs=list(arcs))


# --------------------------------------------------------------------------
# Extraction: net inheritance + hatched-pour classification
# --------------------------------------------------------------------------
def test_hatched_pour_tracks_inherit_polygon_net():
    """Both the perimeter and the hatch lines of a hatched pour are unlinked in
    the file (net_index 0xFFFF → None); each must take the polygon's net."""
    pcb = _mk_pcb(
        polygons=[_mk_polygon("7", "45Degree")],
        tracks=[_mk_track(polygon_index=0, is_outline=True),   # perimeter
                _mk_track(polygon_index=0)],                   # hatch line
    )
    perimeter, hatch = _extract_tracks(pcb, 0.0, 0.0)

    assert perimeter.net_index == 7, "hatched pour perimeter lost its net"
    assert hatch.net_index == 7, "hatch line lost its net"
    assert perimeter.polygon_hatched and hatch.polygon_hatched


def test_hatched_pour_arcs_inherit_polygon_net():
    """A hatched pour's curved perimeter arrives as polygon-owned arcs with no
    net of their own — same inheritance as the tracks."""
    pcb = _mk_pcb(polygons=[_mk_polygon("7", "45Degree")],
                  arcs=[_mk_arc(polygon_index=0, is_outline=True)])
    (arc,) = _extract_arcs(pcb, 0.0, 0.0)

    assert arc.net_index == 7
    assert arc.polygon_hatched


def test_primitive_net_wins_over_parent_polygon():
    """Inheritance only fills a gap — a primitive that carries its own net
    (a routed track crossing a pour, say) keeps it."""
    pcb = _mk_pcb(polygons=[_mk_polygon("7", "45Degree")],
                  tracks=[_mk_track(net_index=3, polygon_index=0)])
    (track,) = _extract_tracks(pcb, 0.0, 0.0)

    assert track.net_index == 3


def test_netless_polygon_does_not_donate_phantom_net_zero():
    """A polygon whose record has no NET field must leave its primitives
    unassigned rather than pulling them onto net index 0."""
    pcb = _mk_pcb(polygons=[_mk_polygon(None, "45Degree")],
                  tracks=[_mk_track(polygon_index=0)],
                  arcs=[_mk_arc(polygon_index=0)])
    (track,) = _extract_tracks(pcb, 0.0, 0.0)
    (arc,) = _extract_arcs(pcb, 0.0, 0.0)

    assert track.net_index == NO_NET
    assert arc.net_index == NO_NET


def test_polygon_on_net_zero_still_inherits():
    """The flip side: an explicit ``NET=0`` is a real net index, not a
    sentinel, and must still be inherited."""
    pcb = _mk_pcb(polygons=[_mk_polygon("0", "45Degree")],
                  tracks=[_mk_track(polygon_index=0)])
    (track,) = _extract_tracks(pcb, 0.0, 0.0)

    assert track.net_index == 0


def test_polygon_index_sentinels_resolve_to_no_net():
    """65535 is the documented "no polygon" sentinel; split-plane and
    board-outline tracks carry 65534. Neither may index the polygon list."""
    pcb = _mk_pcb(polygons=[_mk_polygon("7", "45Degree")],
                  tracks=[_mk_track(polygon_index=NO_POLYGON),
                          _mk_track(polygon_index=65534)])
    no_polygon, split_plane = _extract_tracks(pcb, 0.0, 0.0)

    assert no_polygon.net_index == NO_NET and not no_polygon.polygon_hatched
    assert split_plane.net_index == NO_NET and not split_plane.polygon_hatched


def test_fill_style_classification():
    """Every Altium HATCHSTYLE, plus the two distinct "None"s: a *missing*
    hatch_style attribute is altium_monkey's own default and means solid,
    whereas the *string* 'None' is Altium's "outlines only" fill, whose copper
    really is just the perimeter tracks."""
    styles = ["Solid", "45Degree", "90Degree", "Horizontal", "Vertical",
              "None", None]
    pcb = _mk_pcb(
        polygons=[_mk_polygon("7", s) for s in styles],
        tracks=[_mk_track(polygon_index=i) for i in range(len(styles))],
    )
    hatched = [t.polygon_hatched for t in _extract_tracks(pcb, 0.0, 0.0)]

    assert hatched == [False, True, True, True, True, True, False], (
        f"fill-style classification wrong for {styles}: {hatched}")


# --------------------------------------------------------------------------
# Geometry: which outline primitives count as copper
# --------------------------------------------------------------------------
def _outline_track(*, hatched: bool, net_index: int = 0) -> RawTrack:
    return RawTrack(
        a=Pt2D(0.0, 0.0), b=Pt2D(10.0, 0.0), width_mm=0.5,
        layer_id=TOP, net_index=net_index, polygon_index=0,
        is_polygon_outline=True, component_index=-1, is_keepout=False,
        polygon_hatched=hatched,
    )


def _outline_arc(*, hatched: bool, net_index: int = 0) -> RawArc:
    return RawArc(
        center=Pt2D(0.0, 0.0), radius_mm=5.0,
        start_angle_deg=0.0, end_angle_deg=90.0, width_mm=0.5,
        layer_id=TOP, net_index=net_index, is_keepout=False,
        is_polygon_outline=True, polygon_index=0, polygon_hatched=hatched,
    )


def test_solid_pour_outline_stays_artwork():
    """Unchanged behaviour: a solid pour's outline is boundary artwork over the
    region fill and must stay out of the copper geometry."""
    assert not _track_is_copper(_outline_track(hatched=False), set())
    assert not _arc_is_copper(_outline_arc(hatched=False), set())


def test_hatched_pour_outline_is_copper():
    """The fix: a hatched pour's perimeter is the pour's outer conductor."""
    assert _track_is_copper(_outline_track(hatched=True), set())
    assert _arc_is_copper(_outline_arc(hatched=True), set())


def test_hatched_pour_outline_still_obeys_the_other_exclusions():
    """Being hatched only lifts the polygon-outline exclusion — keepout, zero
    width and plane layers still disqualify a primitive."""
    import dataclasses

    hatched = _outline_track(hatched=True)
    assert not _track_is_copper(dataclasses.replace(hatched, is_keepout=True), set())
    assert not _track_is_copper(dataclasses.replace(hatched, width_mm=0.0), set())
    assert not _track_is_copper(hatched, {TOP})


# --------------------------------------------------------------------------
# Geometry: the perimeter reaches the per-net copper shapes
# --------------------------------------------------------------------------
def _stackup() -> tuple[RawStackupLayer, ...]:
    return (
        RawStackupLayer(
            layer_id=TOP, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.2, next_layer_id=BOTTOM,
            is_plane=False, plane_net_name=None, mech_enabled=True,
        ),
        RawStackupLayer(
            layer_id=BOTTOM, name="Bottom", copper_thickness_mm=0.035,
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
        "pcb_components": (), "nets": (RawNet("GND"), RawNet("+5V")),
        "stackup": _stackup(), "sch_components": (), "compiled_netlist": None,
    }
    base.update(overrides)
    return ExtractedProject(**base)


def _hatch_line(y_mm: float, net_index: int) -> RawTrack:
    return RawTrack(
        a=Pt2D(0.0, y_mm), b=Pt2D(10.0, y_mm), width_mm=0.25,
        layer_id=TOP, net_index=net_index, polygon_index=0,
        is_polygon_outline=False, component_index=-1, is_keepout=False,
        polygon_hatched=True,
    )


def test_hatched_pour_perimeter_reaches_the_net_copper_shape():
    """The whole pour — perimeter plus hatch lines — unions into the +5V
    bucket on Top. Before the fix the perimeter was missing from that shape and
    the hatch lines sat in the NO_NET bucket instead."""
    plus5v = 1
    perimeter = _outline_track(hatched=True, net_index=plus5v)
    hatch = _hatch_line(2.0, plus5v)
    proj = _proj(tracks=(perimeter, hatch))

    shapes = build_net_layer_shapes(proj, [TOP, BOTTOM])

    assert (TOP, plus5v) in shapes, "hatched pour produced no +5V copper on Top"
    pour = shapes[(TOP, plus5v)]
    assert (TOP, NO_NET) not in shapes, "pour copper leaked into the NO_NET bucket"

    # The perimeter runs along y=0 and the hatch line along y=2; both must be
    # inside the unioned shape, and its bbox must span them.
    assert pour.intersects(_shapely_point(5.0, 0.0)), "perimeter missing"
    assert pour.intersects(_shapely_point(5.0, 2.0)), "hatch line missing"
    miny, maxy = pour.bounds[1], pour.bounds[3]
    assert miny < 0.0 and maxy > 2.0


def test_solid_pour_outline_absent_from_net_copper_shape():
    """Control: the same geometry marked as a solid pour contributes nothing —
    its copper would come from the Regions6 fill instead."""
    plus5v = 1
    proj = _proj(tracks=(_outline_track(hatched=False, net_index=plus5v),))

    shapes = build_net_layer_shapes(proj, [TOP, BOTTOM])

    assert not shapes.get((TOP, plus5v)), (
        "solid-pour outline artwork must not become copper")


def _shapely_point(x: float, y: float):
    import shapely.geometry
    return shapely.geometry.Point(x, y)
