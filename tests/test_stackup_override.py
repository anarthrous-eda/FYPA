"""Stackup thickness override must keep every LoadedProject field.

``_SolveWorker._apply_stackup_overrides`` used to rebuild the LoadedProject
with a hand-picked field list that left out ``absorbed_bridges``. Those are
the low-Ω SERIES parts whose nets were merged at load time; ``build_problem``
re-emits them as resistors to keep the merged copper joined. Dropped, that
copper floated, the solve went near-singular and a 9 mV drop read 45 MV,
for ANY override, even one equal to the original thickness.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import fypa.altium_geometry as _geom
from fypa.altium.annotations import AnnotationResult
from fypa.altium.loader import LoadedProject, clone_loaded_for_edit, replace_loaded
from fypa.viewer.solve_worker import _SolveWorker


@dataclass(frozen=True)
class _Layer:
    layer_id: int
    copper_thickness_mm: float


@dataclass(frozen=True)
class _Extracted:
    stackup: tuple


def _loaded() -> LoadedProject:
    return LoadedProject(
        extracted=_Extracted(stackup=(_Layer(1, 0.035), _Layer(6, 0.035))),
        annotations=AnnotationResult(),
        geometry=["old-geometry"],
        absorbed_bridges=[SimpleNamespace(designator="R1"),
                          SimpleNamespace(designator="R2")],
        merged_net_names=frozenset({"VDD_CON"}),
        no_auto_bridge_applied=frozenset({"R9"}),
        auto_series_max_ohm_applied=0.1,
    )


def _assert_fields_carried(new: LoadedProject, old: LoadedProject) -> None:
    assert [b.designator for b in new.absorbed_bridges] == ["R1", "R2"]
    assert new.absorbed_bridges is not old.absorbed_bridges
    assert new.merged_net_names == frozenset({"VDD_CON"})
    assert new.no_auto_bridge_applied == frozenset({"R9"})
    assert new.auto_series_max_ohm_applied == 0.1


def test_stackup_override_keeps_absorbed_bridges(monkeypatch):
    monkeypatch.setattr(_geom, "build_layer_geometries",
                        lambda extracted: ["new-geometry"])
    old = _loaded()
    worker = _SolveWorker.__new__(_SolveWorker)
    worker._stackup_overrides = {6: 0.350012}

    new = worker._apply_stackup_overrides(old)

    _assert_fields_carried(new, old)
    assert new.annotations is old.annotations
    assert new.geometry == ["new-geometry"]
    thk = {s.layer_id: s.copper_thickness_mm for s in new.extracted.stackup}
    assert thk == {1: 0.035, 6: 0.350012}
    # The pristine project is untouched.
    assert old.extracted.stackup[1].copper_thickness_mm == 0.035


def test_clone_for_edit_keeps_every_field():
    old = _loaded()
    new = clone_loaded_for_edit(old)
    _assert_fields_carried(new, old)
    assert new.extracted is old.extracted
    assert new.geometry == ["old-geometry"]
    assert new.annotations is not old.annotations


def test_replace_loaded_drops_stale_geometry_with_new_extracted():
    old = _loaded()
    new = replace_loaded(old, extracted=_Extracted(stackup=()))
    _assert_fields_carried(new, old)
    assert "geometry" not in new.__dict__   # rebuilt lazily from new extract


def test_replace_loaded_tolerates_old_pickles_missing_fields():
    old = _loaded()
    del old.__dict__["merged_net_names"]
    del old.__dict__["no_auto_bridge_applied"]
    new = replace_loaded(old)
    assert new.merged_net_names == frozenset()
    assert new.no_auto_bridge_applied == frozenset()
