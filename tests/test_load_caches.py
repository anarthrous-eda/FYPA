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


_SCH_FIELDS = ("sch_components", "sch_sheet_symbols", "schdoc_paths",
               "physical_sheet_names")


@pytest.fixture
def parse_guard(monkeypatch):
    """Make board / schematic parsing raise, so a test can prove a cache hit
    did not parse. ``guard.board`` / ``guard.sch`` toggle each side."""
    from altium_monkey import AltiumDesign
    from altium_monkey.altium_pcbdoc import AltiumPcbDoc

    real_pcb = AltiumPcbDoc.from_file.__func__
    real_sch = AltiumDesign.from_prjpcb.__func__

    class Guard:
        board = False
        sch = False

    def pcb_from_file(cls, *a, **k):
        if Guard.board:
            raise AssertionError("PcbDoc parsed despite a valid cache")
        return real_pcb(cls, *a, **k)

    def sch_from_prjpcb(cls, *a, **k):
        if Guard.sch:
            raise AssertionError("SchDocs parsed despite a valid cache")
        return real_sch(cls, *a, **k)

    monkeypatch.setattr(AltiumPcbDoc, "from_file", classmethod(pcb_from_file))
    monkeypatch.setattr(AltiumDesign, "from_prjpcb", classmethod(sch_from_prjpcb))
    return Guard


def _netlist_json(proj) -> str:
    import json
    return json.dumps(proj.compiled_netlist.to_json(), sort_keys=True,
                      default=repr)


def test_extract_caches_round_trip_and_invalidation(sandbox, tmp_path,
                                                    parse_guard):
    from fypa.altium import extract as ex

    cache_dir = tmp_path / "cache"
    cold = ex.extract_project(sandbox, cache_dir=cache_dir)
    assert (cache_dir / ex.PCB_EXTRACT_CACHE_NAME).exists()
    assert (cache_dir / ex.SCH_EXTRACT_CACHE_NAME).exists()

    # Both halves cached: nothing is parsed, and the result is identical.
    parse_guard.board = parse_guard.sch = True
    warm = ex.extract_project(sandbox, cache_dir=cache_dir)
    for f in _PCB_FIELDS + _SCH_FIELDS:
        assert getattr(warm, f) == getattr(cold, f), f
    assert _netlist_json(warm) == _netlist_json(cold)

    # A schematic save re-extracts the schematics only...
    _bump_mtime(sandbox.parent / "Main.SchDoc")
    with pytest.raises(AssertionError, match="SchDocs parsed"):
        ex.extract_project(sandbox, cache_dir=cache_dir)
    parse_guard.sch = False
    ex.extract_project(sandbox, cache_dir=cache_dir)       # board stays cached

    # ...and a board save re-parses the board only.
    _bump_mtime(sandbox.parent / "Sandbox.PcbDoc")
    parse_guard.sch = True
    with pytest.raises(AssertionError, match="PcbDoc parsed"):
        ex.extract_project(sandbox, cache_dir=cache_dir)
    parse_guard.board = False
    ex.extract_project(sandbox, cache_dir=cache_dir)       # schematics cached

    # read_cache=False (a clean load) re-parses both even when valid.
    parse_guard.board = True
    with pytest.raises(AssertionError, match="parsed"):
        ex.extract_project(sandbox, cache_dir=cache_dir, read_cache=False)


def test_schematic_cache_notices_structure_and_annotation_files(
        sandbox, tmp_path, parse_guard):
    """altium_monkey reads <project>.PrjPcbStructure / .Annotation (channel
    designators); creating or editing either must invalidate."""
    from fypa.altium import extract as ex

    cache_dir = tmp_path / "cache"
    ex.extract_project(sandbox, cache_dir=cache_dir)
    parse_guard.sch = True
    ex.extract_project(sandbox, cache_dir=cache_dir)       # hit
    sandbox.with_suffix(".Annotation").write_text("x")
    with pytest.raises(AssertionError, match="SchDocs parsed"):
        ex.extract_project(sandbox, cache_dir=cache_dir)


def test_raw_records_pickle_round_trip():
    """The fast ``__reduce__`` must rebuild equal records (and stay hashable
    / frozen) for every record type in a real extract."""
    import pickle

    from fypa.altium import extract as ex

    proj = ex.extract_project(SANDBOX / "Sandbox.PrjPcb")
    for f in _PCB_FIELDS + _SCH_FIELDS:
        value = getattr(proj, f)
        assert pickle.loads(pickle.dumps(value, protocol=5)) == value, f


def test_corrupt_pcb_extract_cache_is_a_miss(sandbox, tmp_path):
    from fypa.altium import extract as ex

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / ex.PCB_EXTRACT_CACHE_NAME).write_bytes(b"not a pickle")
    proj = ex.extract_project(sandbox, cache_dir=cache_dir)
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


def test_stub_layer_geometry_from_per_net_matches_legacy():
    """The viewer stub's per-layer copper, fused from the per-net shapes, is
    the legacy all-primitives union to within edge rounding: same layers and
    metadata, and any difference vanishes when shrunk by 1 um (it is only the
    per-net path's 1 um grid snapping along edges)."""
    import shapely

    from fypa.altium.loader import load_project
    from fypa.altium_geometry import (
        build_layer_geometries,
        build_layer_geometries_from_per_net,
        build_per_net_geometry_layers,
    )

    ext = load_project(SANDBOX / "Sandbox.PrjPcb").extracted
    legacy = build_layer_geometries(ext)
    derived = build_layer_geometries_from_per_net(
        ext, build_per_net_geometry_layers(ext))
    meta = lambda L: (L.layer_id, L.name, L.conductance, L.is_plane,  # noqa: E731
                      L.plane_net_index)
    assert [meta(L) for L in derived] == [meta(L) for L in legacy]
    for a, b in zip(legacy, derived):
        xor = shapely.symmetric_difference(a.shape, b.shape, grid_size=1e-4)
        assert xor.buffer(-1e-3).area < 1e-9, a.name


def test_vectorised_all_copper_records_match_reference():
    """The bulk-shapely overlay records equal the per-polygon reference:
    same records, order and float32 arrays."""
    import numpy as np

    from fypa.altium import loader as L
    from fypa.altium_geometry import build_per_net_geometry_layers

    ext = L.load_project(SANDBOX / "Sandbox.PrjPcb").extracted
    per_net = build_per_net_geometry_layers(ext)
    ref = L._build_all_copper_records_reference(per_net, str)
    new = L._build_all_copper_records_vectorised(per_net, str)
    assert len(new) == len(ref) > 0
    for a, b in zip(ref, new):
        assert (a["layer_id"], a["net"]) == (b["layer_id"], b["net"])
        assert len(a["polygons"]) == len(b["polygons"])
        for pa, pb in zip(a["polygons"], b["polygons"]):
            assert pb["exterior"].dtype == np.float32
            assert np.array_equal(pa["exterior"], pb["exterior"])
            assert len(pa["holes"]) == len(pb["holes"])
            for ha, hb in zip(pa["holes"], pb["holes"]):
                assert np.array_equal(ha, hb)


def test_parallel_copper_geometry_is_identical_to_serial(monkeypatch):
    """The multi-process union build returns exactly the serial result:
    same per-net layers in the same order, byte-identical shapes."""
    import shapely

    from fypa.altium.loader import load_project
    from fypa.altium_geometry import (
        build_layer_geometries_from_per_net,
        build_per_net_and_layer_geometry,
        build_per_net_geometry_layers,
        clear_net_layer_bucket_cache,
    )

    def sig(layers):
        return [(L.layer_id, L.net_index, L.name, L.conductance, L.is_plane,
                 L.plane_net_index, shapely.to_wkb(L.shape)) for L in layers]

    ext = load_project(SANDBOX / "Sandbox.PrjPcb").extracted
    clear_net_layer_bucket_cache()
    serial_net = build_per_net_geometry_layers(ext)
    serial_layers = build_layer_geometries_from_per_net(ext, serial_net)
    clear_net_layer_bucket_cache()
    monkeypatch.setenv("FYPA_GEOMETRY_WORKERS", "2")
    par_net, par_layers = build_per_net_and_layer_geometry(ext)
    assert sig(par_net) == sig(serial_net)
    assert sig(par_layers) == sig(serial_layers)
