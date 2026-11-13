"""Hover over merged copper names its original net: "Net = +12V (HS_SNS)".

The low-Ω merge folds a net into another before the solve, after which every
primitive reports the surviving name, so copper that was HS_SNS looks like
+12V's own. The loader records each moved primitive's original net, the solve
metadata carries that copper under its original name (``premerged_copper``),
and the hover looks the point up in it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fypa.altium.annotations import parse_annotations
from fypa.altium.extract import (
    ExtractedProject,
    Pt2D,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawStackupLayer,
)
from fypa.altium.loader import (
    LoadedProject,
    _apply_net_remap,
    _premerge_primitive_nets,
    build_premerged_copper_records,
    replace_loaded,
)

P12, SNS, GND = 0, 1, 2


def _layer(lid, name, *, plane_net=None):
    return RawStackupLayer(
        layer_id=lid, name=name, copper_thickness_mm=0.035,
        dielectric_thickness_mm=0.1, next_layer_id=0,
        is_plane=plane_net is not None, plane_net_name=plane_net,
        mech_enabled=True,
    )


def _pad(pin, net, x):
    return RawPad(
        center=Pt2D(x, 0), width_mm=1.0, height_mm=1.0, hole_mm=0,
        shape=2, rotation_deg=0, layer_id=1, net_index=net,
        designator=pin, component_index=0,
        is_through_hole=False, is_smt=True,
    )


def _proj(stackup=None) -> ExtractedProject:
    """Pad 1 on +12V at x=0, pad 2 on HS_SNS at x=5 (far apart)."""
    return ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(), stackup=stackup or (_layer(1, "Top"),),
        compiled_netlist=None,
        nets=(RawNet("+12V"), RawNet("HS_SNS"), RawNet("GND")),
        sch_components=(),
        pcb_components=(RawPcbComponent(
            designator="R80", center=Pt2D(2.5, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="2512", source_designator="R80"),),
        pads=(_pad("1", P12, 0.0), _pad("2", SNS, 5.0)),
    )


def _merged(proj, remap=None):
    remap = remap or {SNS: P12}
    pre = _premerge_primitive_nets(proj, remap)
    merged = _apply_net_remap(proj, remap)
    ann = parse_annotations(merged, enabled_layers=[1])
    return LoadedProject(extracted=merged, annotations=ann,
                         premerge_primitive_nets=pre)


# --- loader side -------------------------------------------------------------

def test_moved_primitives_are_recorded_with_their_original_net():
    pre = _premerge_primitive_nets(_proj(), {SNS: P12})
    assert pre == {"pads": {1: SNS}}


def test_nothing_recorded_without_a_merge():
    assert _premerge_primitive_nets(_proj(), {}) == {}


def test_replace_loaded_keeps_the_record():
    loaded = _merged(_proj())
    assert replace_loaded(loaded).premerge_primitive_nets == {"pads": {1: SNS}}


def test_premerged_copper_covers_the_original_nets_copper_only():
    import shapely.geometry as sg
    recs = build_premerged_copper_records(_merged(_proj()))
    assert len(recs) == 1
    rec = recs[0]
    assert (rec["net"], rec["canonical"], rec["layer_id"]) == (
        "HS_SNS", "+12V", 1)
    polys = [sg.Polygon(p["exterior"], list(p["holes"]))
             for p in rec["polygons"]]
    assert any(p.contains(sg.Point(5.0, 0.0)) for p in polys)
    assert not any(p.contains(sg.Point(0.0, 0.0)) for p in polys)


def test_a_plane_flooded_by_a_merged_net_is_a_whole_layer_record():
    stack = (_layer(1, "Top"), _layer(2, "PWR", plane_net="HS_SNS"))
    recs = build_premerged_copper_records(_merged(_proj(stack)))
    plane = [r for r in recs if r.get("whole_layer")]
    assert plane == [{"layer_id": 2, "net": "HS_SNS", "canonical": "+12V",
                      "whole_layer": True, "polygons": []}]


def test_no_records_without_a_merge_or_from_an_old_cache():
    proj = _proj()
    ann = parse_annotations(proj, enabled_layers=[1])
    assert build_premerged_copper_records(
        LoadedProject(extracted=proj, annotations=ann)) == []
    old = _merged(proj)
    del old.premerge_primitive_nets          # older design-info pickle
    assert build_premerged_copper_records(old) == []


# --- viewer side -------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V
    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    stack = (_layer(1, "Top"), _layer(2, "PWR", plane_net="HS_SNS"))
    v.metadata = {"premerged_copper":
                  build_premerged_copper_records(_merged(_proj(stack)))}
    v._phys_name_to_layer_id = {"Top": 1, "PWR": 2}
    return v


def _label(v, net, phys, x, y=0.0):
    import fypa.altium_viewer as V
    return V.PdnViewer._with_original_net(
        v, {"net": net, "physical": phys}, x, y)["net"]


def test_hover_on_merged_copper_names_the_original_net(viewer):
    assert _label(viewer, "+12V", "Top", 5.0) == "+12V (HS_SNS)"


def test_hover_on_the_canonical_nets_own_copper_is_unchanged(viewer):
    assert _label(viewer, "+12V", "Top", 0.0) == "+12V"


def test_hover_on_a_plane_flooded_by_the_merged_net(viewer):
    assert _label(viewer, "+12V", "PWR", 40.0) == "+12V (HS_SNS)"
    # Another net's copper in that plane layer is not the plane.
    assert _label(viewer, "GND", "PWR", 40.0) == "GND"


def test_bare_copper_hit_carries_its_layer_id(viewer):
    import fypa.altium_viewer as V
    out = V.PdnViewer._with_original_net(
        viewer, {"net": "+12V", "layer_id": 1}, 5.0, 0.0)
    assert out["net"] == "+12V (HS_SNS)"


def test_the_cached_probe_is_not_modified(viewer):
    import fypa.altium_viewer as V
    lp = {"net": "+12V", "physical": "Top"}
    out = V.PdnViewer._with_original_net(viewer, lp, 5.0, 0.0)
    assert lp["net"] == "+12V" and out is not lp


def test_metadata_without_the_records_leaves_names_alone(viewer):
    viewer.metadata = {}
    assert _label(viewer, "+12V", "Top", 5.0) == "+12V"
