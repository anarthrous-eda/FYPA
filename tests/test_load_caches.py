"""Load-time caches: the PCB-side extract cache, the per-net geometry cache,
and the geometry-free diagnostic summary.

A large board (a 238 MB PcbDoc) spent ~5 min parsing the PcbDoc, ~2 min on a
per-layer union built only for a log line, and ~1.5 min on per-net unions —
redone on every schematic save, because the design-info cache is keyed on
every project file. These tests pin the caches that avoid that work.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "ExampleDesigns" / "Sandbox"

_PCB_FIELDS = ("pcbdoc_path", "tracks", "arcs", "vias", "pads", "texts",
               "regions", "shape_based_regions", "fills", "pcb_components",
               "nets", "stackup", "board_origin_mm", "board_outline",
               "split_planes")


@pytest.fixture
def sandbox(tmp_path):
    """A private copy of the Sandbox project, so tests can touch its files."""
    dst = tmp_path / "Sandbox"
    shutil.copytree(SANDBOX, dst, ignore=shutil.ignore_patterns(
        "History", "Project Logs*", "Project Outputs*", "__Previews"))
    return dst / "Sandbox.PrjPcb"


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def test_pcb_extract_cache_round_trip_and_invalidation(sandbox, tmp_path,
                                                       monkeypatch):
    from altium_monkey import AltiumDesign
    from fypa.altium import extract as ex

    cache_dir = tmp_path / "cache"
    cold = ex.extract_project(sandbox, pcb_cache_dir=cache_dir)
    assert (cache_dir / ex.PCB_EXTRACT_CACHE_NAME).exists()

    # A warm extract must not parse the PcbDoc at all.
    real_load = AltiumDesign.load_pcbdoc

    def _no_parse(self, *a, **k):
        raise AssertionError("PcbDoc parsed despite a valid cache")

    monkeypatch.setattr(AltiumDesign, "load_pcbdoc", _no_parse)
    warm = ex.extract_project(sandbox, pcb_cache_dir=cache_dir)
    for f in _PCB_FIELDS:
        assert getattr(warm, f) == getattr(cold, f), f
    # Schematic side is still read fresh.
    assert warm.sch_components == cold.sch_components

    # A schematic save leaves the board cache valid...
    _bump_mtime(sandbox.parent / "Main.SchDoc")
    ex.extract_project(sandbox, pcb_cache_dir=cache_dir)

    # ...but a PcbDoc save forces a re-parse.
    _bump_mtime(sandbox.parent / "Sandbox.PcbDoc")
    with pytest.raises(AssertionError, match="PcbDoc parsed"):
        ex.extract_project(sandbox, pcb_cache_dir=cache_dir)

    # read_pcb_cache=False (a clean load) re-parses even when valid.
    monkeypatch.setattr(AltiumDesign, "load_pcbdoc", real_load)
    ex.extract_project(sandbox, pcb_cache_dir=cache_dir)   # refresh
    monkeypatch.setattr(AltiumDesign, "load_pcbdoc", _no_parse)
    with pytest.raises(AssertionError, match="PcbDoc parsed"):
        ex.extract_project(sandbox, pcb_cache_dir=cache_dir,
                           read_pcb_cache=False)


def test_corrupt_pcb_extract_cache_is_a_miss(sandbox, tmp_path):
    from fypa.altium import extract as ex

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / ex.PCB_EXTRACT_CACHE_NAME).write_bytes(b"not a pickle")
    proj = ex.extract_project(sandbox, pcb_cache_dir=cache_dir)
    assert proj.tracks or proj.regions or proj.shape_based_regions


def test_per_net_geometry_cache(sandbox, tmp_path):
    import dataclasses

    from fypa.altium.loader import load_project
    from fypa.altium_geometry import build_per_net_geometry_layers
    from fypa.cli import (
        _per_net_geometry_fingerprint,
        _save_per_net_geometry,
        _try_load_per_net_geometry,
    )

    loaded = load_project(sandbox)
    fp = _per_net_geometry_fingerprint(loaded.extracted)
    assert fp is not None
    assert _per_net_geometry_fingerprint(load_project(sandbox).extracted) == fp

    layers = build_per_net_geometry_layers(loaded.extracted)
    path = tmp_path / "per-net-geometry.pkl"
    _save_per_net_geometry(path, fp, layers)
    back = _try_load_per_net_geometry(path, fp)
    assert [(L.layer_id, L.net_index, L.name, L.conductance) for L in back] \
        == [(L.layer_id, L.net_index, L.name, L.conductance) for L in layers]
    assert all(a.shape.equals_exact(b.shape, 0) for a, b in zip(layers, back))

    # Re-tagging one primitive's net (a merge / editor copper name) changes
    # the key, as does a stackup change (thickness override).
    ext = loaded.extracted
    t0 = ext.tracks[0]
    retagged = dataclasses.replace(
        ext, tracks=(dataclasses.replace(t0, net_index=t0.net_index + 1),)
        + ext.tracks[1:])
    assert _per_net_geometry_fingerprint(retagged) != fp
    s0 = ext.stackup[0]
    restacked = dataclasses.replace(
        ext, stackup=(dataclasses.replace(
            s0, copper_thickness_mm=(s0.copper_thickness_mm or 0) + 0.01),)
        + ext.stackup[1:])
    assert _per_net_geometry_fingerprint(restacked) != fp
    assert _try_load_per_net_geometry(
        path, _per_net_geometry_fingerprint(retagged)) is None

    # A schematic save alone does not change it.
    _bump_mtime(sandbox.parent / "Main.SchDoc")
    assert _per_net_geometry_fingerprint(load_project(sandbox).extracted) == fp


def test_diagnostic_summary_can_skip_geometry(sandbox):
    from fypa.altium.loader import load_project

    loaded = load_project(sandbox)
    text = loaded.diagnostic_summary(include_geometry=False)
    assert "geometry" not in loaded.__dict__
    assert "per-layer union not built" in text
    # Once built, the geometry-free summary still reports it.
    _ = loaded.geometry
    assert "polys" in loaded.diagnostic_summary(include_geometry=False)
