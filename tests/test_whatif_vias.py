"""What-If punched vias: vias a scenario adds to the board for its own solve.

Covers the scenario data (round trip through the .fypa dicts), adding the
vias to a copy of the extract, the per-net geometry cache rebuilding only
the nets a punched via touches (and never being overwritten by a what-if),
the solve subprocess's slim design delta, and the physics: an extra via in
parallel lowers the drop.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pytest

from fypa import whatif as W
from fypa.whatif_vias import (
    METADATA_KEY,
    AddedVia,
    added_via_report,
    apply_added_vias,
    describe,
    vias_from_dicts,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "ExampleDesigns" / "Sandbox" / "Sandbox.PrjPcb"

# Sandbox: P1V_VIAS runs Top -> Bottom -> Top through two 0.2 / 0.6 mm
# through vias, one at (207, 19). Top = 1, Bottom = 32; L2 (P1V_PLANE_TEST)
# and L3 (GND) are planes the barrels pass through.
TOP, BOTTOM = 1, 32


def _via(x=207.4, y=19.0, net="P1V_VIAS", start=TOP, end=BOTTOM,
         dia=0.6, hole=0.2) -> AddedVia:
    return AddedVia(x_mm=x, y_mm=y, net=net, layer_start=start,
                    layer_end=end, diameter_mm=dia, hole_diameter_mm=hole)


# --- scenario data -----------------------------------------------------------

def test_added_via_round_trips():
    v = _via()
    assert AddedVia.from_dict(v.to_dict()) == v


@pytest.mark.parametrize("bad", [
    None, "via", {},
    {**_via().to_dict(), "net": ""},
    {**_via().to_dict(), "hole_diameter_mm": 0.0},
    {**_via().to_dict(), "diameter_mm": 0.2},          # pad == drill
    {**_via().to_dict(), "x_mm": float("nan")},
    {**_via().to_dict(), "layer_start": "top"},
])
def test_malformed_vias_are_dropped(bad):
    assert AddedVia.from_dict(bad) is None
    assert vias_from_dicts([bad, _via().to_dict()]) == [_via()]


def test_scenario_keeps_its_vias_through_the_project_file():
    s = W.WhatIfScenario(name="More vias",
                         added_vias=[_via(), _via(x=213.4, y=17.0)])
    assert s.has_changes()
    back = W.WhatIfScenario.from_dict(s.to_dict())
    assert back.added_vias == s.added_vias
    # Files written before vias existed load with none.
    d = s.to_dict()
    d.pop("added_vias")
    assert W.WhatIfScenario.from_dict(d).added_vias == []


def test_a_copy_gets_its_own_via_list():
    s = W.WhatIfScenario(name="A", added_vias=[_via()])
    c = s.copy_as("B")
    c.added_vias.append(_via(x=1.0))
    assert len(s.added_vias) == 1 and len(c.added_vias) == 2


def test_vias_only_scenario_counts_as_changed_and_is_described():
    s = W.WhatIfScenario(name="A", added_vias=[
        _via(), _via(x=208.0), _via(net="GND")])
    assert s.has_changes()
    assert W.describe_changes(s, None) == [
        "Added vias: 2 on P1V_VIAS, 1 on GND"]
    assert describe([]) == []


def test_added_via_report_counts_the_tail():
    md = {"vias": [{"segments": [1]}, {"segments": [1]}, {"segments": []}]}
    assert added_via_report(md, 2) == {"count": 2, "unconnected": 1}
    assert added_via_report({}, 2) == {"count": 2, "unconnected": 0}


# --- on the board -------------------------------------------------------------

@pytest.fixture(scope="module")
def loaded():
    pytest.importorskip("altium_monkey")
    if not SANDBOX.exists():
        pytest.skip(f"example design not present: {SANDBOX}")
    from fypa.altium.loader import load_project
    return load_project(SANDBOX)


def _idx(ext, name):
    return next(i for i, n in enumerate(ext.nets) if n.name == name)


def test_vias_are_appended_to_a_copy(loaded):
    before = loaded.extracted.vias
    out, warnings, n = apply_added_vias(loaded, [_via()])
    assert (n, warnings) == (1, [])
    assert loaded.extracted.vias is before          # pristine untouched
    assert out.extracted.vias[:len(before)] == before
    new = out.extracted.vias[-1]
    assert new.net_index == _idx(loaded.extracted, "P1V_VIAS")
    assert (new.center.x, new.center.y) == (207.4, 19.0)
    assert (new.layer_start, new.layer_end) == (TOP, BOTTOM)
    # Everything else about the design carries across.
    assert out.annotations is loaded.annotations


def test_unusable_vias_are_skipped_with_a_warning(loaded):
    out, warnings, n = apply_added_vias(loaded, [
        _via(net="NO_SUCH_NET"),
        _via(start=TOP, end=7),           # Mid-Layer 6: not enabled
        _via(start=TOP, end=TOP),
        _via(),
    ])
    assert n == 1 and len(out.extracted.vias) == len(loaded.extracted.vias) + 1
    assert len(warnings) == 3
    assert "not on the board" in warnings[0]
    assert "not an enabled copper layer" in warnings[1]
    assert "same layer" in warnings[2]


def test_no_usable_vias_returns_the_design_itself(loaded):
    out, warnings, n = apply_added_vias(loaded, [_via(net="NO_SUCH_NET")])
    assert out is loaded and n == 0 and len(warnings) == 1


# --- per-net geometry cache ---------------------------------------------------

def _sig(layers):
    import shapely
    return {(g.layer_id, g.net_index): (g.name, g.conductance, g.is_plane,
                                        g.plane_net_index,
                                        shapely.to_wkb(g.shape))
            for g in layers}


def _full(ext):
    from fypa.altium_geometry import (
        build_per_net_geometry_layers,
        clear_net_layer_bucket_cache,
    )
    clear_net_layer_bucket_cache()
    return build_per_net_geometry_layers(ext)


def test_punched_via_rebuilds_only_its_net_and_the_planes_it_crosses(
        loaded, tmp_path):
    from fypa.altium_geometry import clear_net_layer_bucket_cache
    from fypa.cli import (
        _per_net_geometry_fingerprint,
        _save_per_net_geometry,
        incremental_per_net_geometry,
    )
    base = loaded.extracted
    pn = tmp_path / "per-net.pkl"
    _save_per_net_geometry(pn, _per_net_geometry_fingerprint(base),
                           _full(base), base)

    after = apply_added_vias(loaded, [_via()])[0].extracted
    clear_net_layer_bucket_cache()
    got = incremental_per_net_geometry(
        after, pn, _per_net_geometry_fingerprint(after))
    assert got is not None, "an appended via must not force a full rebuild"
    layers, changed, _old = got
    # Its own net, plus the two planes whose sheets gain an anti-pad.
    assert changed == {_idx(base, "P1V_VIAS"), _idx(base, "P1V_PLANE_TEST"),
                       _idx(base, "GND")}
    assert _sig(layers) == _sig(_full(after))


def test_removing_vias_still_forces_a_full_rebuild(loaded, tmp_path):
    from fypa.cli import (
        _per_net_geometry_fingerprint,
        _save_per_net_geometry,
        incremental_per_net_geometry,
    )
    base = loaded.extracted
    pn = tmp_path / "per-net.pkl"
    _save_per_net_geometry(pn, _per_net_geometry_fingerprint(base),
                           _full(base), base)
    fewer = dataclasses.replace(base, vias=base.vias[:-1])
    assert incremental_per_net_geometry(
        fewer, pn, _per_net_geometry_fingerprint(fewer)) is None


def test_a_whatif_solve_never_writes_the_geometry_cache(
        loaded, tmp_path, monkeypatch):
    import fypa.altium_geometry as G
    import fypa.cli as C
    # Sandbox is small enough to skip the build; pretend it is not.
    monkeypatch.setattr(G, "parallel_geometry_workers", lambda _e: 2)
    saves = []
    monkeypatch.setattr(C, "_save_per_net_geometry",
                        lambda *a, **k: saves.append(a))
    after = apply_added_vias(loaded, [_via()])[0].extracted
    cache = tmp_path / "per-net.pkl"
    layers = C.per_net_geometry_for_solve(
        after, cache, C._per_net_geometry_fingerprint(after),
        write_cache=False)
    assert layers and saves == [] and not cache.exists()


# --- solve subprocess: the slim design delta ----------------------------------

def test_appended_vias_travel_as_an_append_delta(loaded):
    from fypa.solve_subprocess import (
        _apply_extracted_delta,
        _extracted_delta,
        _extracted_digest,
    )
    base = loaded.extracted
    after = apply_added_vias(loaded, [_via(), _via(x=213.4, y=17.0)])[0].extracted
    delta = _extracted_delta(base, after)
    assert set(delta) == {"vias"}
    kind, payload = delta["vias"]
    assert kind == "append" and len(payload) == 2
    rebuilt = _apply_extracted_delta(base, delta)
    assert rebuilt.vias == after.vias
    assert _extracted_digest(rebuilt) == _extracted_digest(after)


# --- physics ------------------------------------------------------------------

def _span(solution, net):
    out = 0.0
    for layer, ls in zip(solution.problem.layers, solution.layer_solutions):
        if layer.name.split("|", 1)[-1] != net:
            continue
        pots = [np.asarray(getattr(p, "values", p), float)
                for p in ls.potentials]
        if pots:
            f = np.concatenate(pots)
            out = max(out, float(f.max() - f.min()))
    return out


def test_a_parallel_via_lowers_the_drop(loaded):
    from pdnsolver import mesh as _pdn_mesh

    from fypa.altium.loader import (
        SolveSettings,
        build_solve_metadata,
        solve_problem_adaptive,
    )
    SolveSettings().apply_to_modules()
    cfg = _pdn_mesh.Mesher.Config()
    base = solve_problem_adaptive(loaded, cfg)[0]

    punched, _w, n = apply_added_vias(loaded, [_via()])
    sol, problem, segs, *_ = solve_problem_adaptive(punched, cfg)
    before, after = _span(base, "P1V_VIAS"), _span(sol, "P1V_VIAS")
    assert 0.0 < after < before, (before, after)
    # Nets the via doesn't touch keep their drop.
    assert _span(sol, "P1V_1W100L") == pytest.approx(
        _span(base, "P1V_1W100L"), rel=1e-6)

    md = build_solve_metadata(punched, problem, via_segment_records=segs)
    report = added_via_report(md, n)
    assert report == {"count": 1, "unconnected": 0}
    assert md["vias"][-1]["net"] == "P1V_VIAS"


def test_metadata_key_is_stable():
    # Read back from saved what-if results; renaming it orphans them.
    assert METADATA_KEY == "whatif_added_vias"
