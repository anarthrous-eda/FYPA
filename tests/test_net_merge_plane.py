"""Plane net-merge canonicalization (round-2 finding #3).

After a low-Ω SERIES auto-merge, `_apply_net_remap` rewrites every primitive's
net_index onto the canonical class representative. Internal-plane sheets are
flooded by NAME (`plane_net_name`) resolved against `proj.nets` at geometry
time — a path the primitive rewrite never touched — so a plane whose net was
the *non-canonical* member kept resolving to its pre-merge index, landed in a
bucket filtered out of the active-net set, and the whole plane silently
vanished from the FEM. `_apply_net_remap` must rewrite plane_net_name to the
canonical net's name.
"""
from __future__ import annotations

from pathlib import Path

from fypa.altium.extract import ExtractedProject, RawNet, RawStackupLayer
from fypa.altium.loader import _apply_net_remap


def _proj(nets, stackup) -> ExtractedProject:
    return ExtractedProject(
        prjpcb_path=Path("x.PrjPcb"), pcbdoc_path=Path("x.PcbDoc"),
        tracks=(), arcs=(), vias=(), pads=(), regions=(),
        shape_based_regions=(), fills=(), pcb_components=(),
        nets=tuple(nets), stackup=tuple(stackup), sch_components=(),
    )


def _plane(layer_id: int, net_name: str) -> RawStackupLayer:
    return RawStackupLayer(
        layer_id=layer_id, name=f"Plane{layer_id}", copper_thickness_mm=0.035,
        dielectric_thickness_mm=0.2, next_layer_id=0, is_plane=True,
        plane_net_name=net_name, mech_enabled=True,
    )


def test_plane_net_name_canonicalized_when_plane_net_is_non_canonical():
    # nets: 0=GND (canonical), 1=PGND (non-canonical, merged into GND).
    nets = [RawNet("GND"), RawNet("PGND")]
    proj = _proj(nets, [_plane(39, "PGND")])
    remapped = _apply_net_remap(proj, {1: 0})
    # The plane now floods under the canonical name so its (layer, net) bucket
    # matches the merged rail and is not filtered out of the active set.
    assert remapped.stackup[0].plane_net_name == "GND"


def test_plane_net_name_untouched_when_canonical():
    nets = [RawNet("GND"), RawNet("PGND")]
    proj = _proj(nets, [_plane(39, "GND")])
    # GND (index 0) is the canonical target; nothing to rewrite.
    remapped = _apply_net_remap(proj, {1: 0})
    assert remapped.stackup[0].plane_net_name == "GND"


def test_no_remap_is_identity():
    nets = [RawNet("GND")]
    proj = _proj(nets, [_plane(39, "GND")])
    assert _apply_net_remap(proj, {}) is proj
