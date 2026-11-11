"""Rebuilding only the copper of the nets a Bridges-tab change moved.

Disabling one auto-bridge re-merges a couple of nets, which changes the
per-net geometry cache key — and used to rebuild every (layer, net) bucket
(20 s on Mangala) plus every layer's all-nets copper (26 s serially). The
cache now keeps the board's net state, so the next load rebuilds only the
changed nets. These tests hold that to a full rebuild: per-net copper must
be identical, byte for byte; per-layer copper must be the same copper.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "ExampleDesigns" / "Sandbox" / "Sandbox.PrjPcb"


@pytest.fixture(scope="module")
def base():
    from fypa.altium.loader import load_project
    return load_project(SANDBOX).extracted


def _idx(ext, name):
    return next(i for i, n in enumerate(ext.nets) if n.name == name)


def _merged(ext, frm, to):
    from fypa.altium.loader import _apply_net_remap
    return _apply_net_remap(ext, {_idx(ext, frm): _idx(ext, to)})


def _sig(layers):
    import shapely
    return {(g.layer_id, g.net_index): (g.name, g.conductance, g.is_plane,
                                        g.plane_net_index,
                                        shapely.to_wkb(g.shape))
            for g in layers}


def _full(ext):
    from fypa.altium_geometry import (
        build_layer_geometries_from_per_net,
        build_per_net_geometry_layers,
        clear_net_layer_bucket_cache,
    )
    clear_net_layer_bucket_cache()
    per_net = build_per_net_geometry_layers(ext)
    return per_net, build_layer_geometries_from_per_net(ext, per_net)


def _cache(tmp_path, ext, *, with_state=True):
    """Per-net and layer caches of ``ext`` as a load would write them."""
    from fypa.cli import _per_net_geometry_fingerprint, _save_per_net_geometry
    fp = _per_net_geometry_fingerprint(ext)
    per_net, layers = _full(ext)
    pn, ly = tmp_path / "per-net.pkl", tmp_path / "layers.pkl"
    _save_per_net_geometry(pn, fp, per_net, ext if with_state else None)
    _save_per_net_geometry(ly, fp, layers)
    return pn, ly, fp


def _incremental(ext, pn):
    from fypa.altium_geometry import clear_net_layer_bucket_cache
    from fypa.cli import _per_net_geometry_fingerprint, incremental_per_net_geometry
    clear_net_layer_bucket_cache()
    return incremental_per_net_geometry(ext, pn, _per_net_geometry_fingerprint(ext))


@pytest.mark.parametrize("frm,to", [
    ("P1V_VIAS", "P1V_VIA"),             # two ordinary nets
    ("P1V_PLANE_TEST", "P1V_TRACE"),     # a plane's net folded away
])
def test_rebuilt_nets_match_a_full_rebuild_exactly(base, tmp_path, frm, to):
    pn, _ly, _fp = _cache(tmp_path, base)
    after = _merged(base, frm, to)
    got = _incremental(after, pn)
    assert got is not None
    layers, changed, _old = got
    assert changed == {_idx(base, frm), _idx(base, to)}
    assert _sig(layers) == _sig(_full(after)[0])


def test_unchanged_nets_are_reused_not_rebuilt(base, tmp_path):
    from fypa.cli import _try_load_per_net_geometry
    pn, _ly, fp = _cache(tmp_path, base)
    cached = {(g.layer_id, g.net_index): g
              for g in _try_load_per_net_geometry(pn, fp)}
    layers, changed, _old = _incremental(_merged(base, "P1V_VIAS", "P1V_VIA"), pn)
    gnd = _idx(base, "GND")
    reused = [g for g in layers if g.net_index == gnd]
    assert reused and all(
        g.shape.equals_exact(cached[(g.layer_id, gnd)].shape, 0)
        for g in reused)
    assert gnd not in changed


def test_layer_copper_reused_and_plane_rebuilt(base, tmp_path):
    """Signal layers: same copper, whatever net it is labelled. A plane
    whose own net changed is rebuilt and matches a full rebuild exactly."""
    import shapely
    from fypa.cli import incremental_layer_geometry
    pn, ly, _fp = _cache(tmp_path, base)
    after = _merged(base, "P1V_PLANE_TEST", "P1V_TRACE")
    _layers, changed, old_fp = _incremental(after, pn)
    got = incremental_layer_geometry(after, ly, old_fp, changed)
    want = _full(after)[1]
    assert [g.layer_id for g in got] == [g.layer_id for g in want]
    for a, b in zip(got, want):
        if b.is_plane:
            assert shapely.to_wkb(a.shape) == shapely.to_wkb(b.shape), b.name
        else:
            assert a.shape.symmetric_difference(b.shape).area < 1e-9, b.name


def test_copper_gaining_a_net_rebuilds_layers(base, tmp_path):
    """No-net multilayer copper is not fanned out to every layer, so copper
    gaining a net changes a signal layer's copper: no layer reuse then."""
    from fypa.altium.extract import NO_NET
    from fypa.cli import incremental_layer_geometry
    _pn, ly, fp = _cache(tmp_path, base)
    assert incremental_layer_geometry(base, ly, fp, {NO_NET, 3}) is None


def test_a_stackup_change_is_not_reused(base, tmp_path):
    pn, _ly, _fp = _cache(tmp_path, base)
    s0 = base.stackup[0]
    thicker = dataclasses.replace(base, stackup=(dataclasses.replace(
        s0, copper_thickness_mm=(s0.copper_thickness_mm or 0) + 0.01),)
        + base.stackup[1:])
    assert _incremental(thicker, pn) is None


def test_another_tool_version_or_board_is_not_reused(base, tmp_path):
    from fypa.cli import _per_net_geometry_fingerprint, incremental_per_net_geometry
    pn, _ly, _fp = _cache(tmp_path, base)
    after = _merged(base, "P1V_VIAS", "P1V_VIA")
    fp = dict(_per_net_geometry_fingerprint(after))
    fp["tool_source_hashes"] = {"x": "y"}
    assert incremental_per_net_geometry(after, pn, fp) is None


def test_a_cache_without_net_state_is_not_reused_but_still_loads(base, tmp_path):
    """Files written before net states were stored: no partial reuse, but
    an exact-key hit still reads them as before."""
    from fypa.cli import _try_load_per_net_geometry
    pn, _ly, fp = _cache(tmp_path, base, with_state=False)
    assert _incremental(_merged(base, "P1V_VIAS", "P1V_VIA"), pn) is None
    assert _try_load_per_net_geometry(pn, fp) is not None


def test_a_cache_with_net_state_loads_on_an_exact_hit(base, tmp_path):
    from fypa.cli import _try_load_per_net_geometry
    pn, _ly, fp = _cache(tmp_path, base)
    assert _sig(_try_load_per_net_geometry(pn, fp)) == _sig(_full(base)[0])


def test_no_change_reuses_everything(base, tmp_path):
    pn, _ly, _fp = _cache(tmp_path, base)
    layers, changed, _old = _incremental(base, pn)
    assert changed == set()
    assert _sig(layers) == _sig(_full(base)[0])


# --- design-info cache saved off the critical path --------------------------

def test_design_info_cache_is_saved_in_the_background(tmp_path, monkeypatch):
    import fypa.cli as C
    from fypa.altium.loader import load_project

    target = tmp_path / "design-info.pkl"
    monkeypatch.setattr(C, "_design_info_cache_path", lambda *a, **k: target)
    loaded = load_project(SANDBOX)
    th = C.save_cached_design_info_in_background(SANDBOX, loaded)
    # The caller may extend the original meanwhile (a lazily built
    # attribute); the save works on its own copy.
    loaded.__dict__["_test_marker"] = 1
    th.join(timeout=120)
    assert target.exists()
    back = C._try_load_cached_design_info(SANDBOX, None)
    assert back is not None
    assert not hasattr(back, "_test_marker")
    assert len(back.extracted.pads) == len(loaded.extracted.pads)
    # The original is tagged with the file, for the solve subprocess.
    assert getattr(loaded, "_design_cache_ref", None) is not None
