"""The out-of-process solve path (fypa.solve_subprocess).

Qt-free, so it runs headless in CI. Covers the opt-in flag, error marshaling
across the process boundary, and - when the Sandbox example is present - a full
child solve whose result must match an in-process solve.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fypa import solve_subprocess as S

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "ExampleDesigns" / "Sandbox" / "Sandbox.PrjPcb"


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("FYPA_SOLVE_SUBPROCESS", raising=False)
    assert S.subprocess_solve_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("nope", False),
])
def test_flag_env_parsing(monkeypatch, val, expected):
    monkeypatch.setenv("FYPA_SOLVE_SUBPROCESS", val)
    assert S.subprocess_solve_enabled() is expected


def test_child_failure_surfaces_as_error():
    """A job that blows up inside the child comes back as a
    SolveSubprocessError carrying the child's traceback - never a hang."""
    from pdnsolver import mesh as _pdn_mesh
    from fypa.altium.loader import SolveSettings
    # loaded=None makes solve_problem_adaptive raise in the child.
    job = S.SolveJob(
        loaded=None,
        mesher_config=_pdn_mesh.Mesher.Config(minimum_angle=20.0, maximum_size=2.0),
        settings=SolveSettings(),
    )
    with pytest.raises(S.SolveSubprocessError) as exc:
        S.run_solve_in_subprocess(
            job, on_stage=lambda m: None, on_substage=lambda m: None,
            is_cancelled=lambda: False,
        )
    # The message is the child traceback, so it names the failing call.
    assert "Traceback" in str(exc.value) or "Error" in str(exc.value)


def test_child_entry_mesh_failure_returns_stub(monkeypatch):
    """MeshingException in the child becomes an ok stub, not a fail message."""
    from types import SimpleNamespace

    from fypa.altium.loader import SolveSettings
    from fypa.lean_solution import LeanProblem, LeanSolution
    from pdnsolver import mesh as _pdn_mesh
    from pdnsolver.mesh import MeshingException

    stub = LeanSolution(
        problem=LeanProblem(layers=[], project_name="example"),
        layer_solutions=[],
        solver_info={"stub": True},
    )
    fail_md = {"mesh_failures": [{"summary": "bad copper"}]}

    def _boom(*_a, **_k):
        raise MeshingException("triangle aborted", layer_name="Top|VIN")

    monkeypatch.setattr(
        "fypa.altium.loader.solve_problem_adaptive", _boom,
    )
    monkeypatch.setattr(
        "fypa.altium.loader.package_mesh_failure",
        lambda *_a, **_k: (stub, fail_md),
    )

    msgs: list = []

    class _Q:
        def put(self, msg) -> None:
            msgs.append(msg)

    job = S.SolveJob(
        loaded=SimpleNamespace(),
        mesher_config=_pdn_mesh.Mesher.Config(
            minimum_angle=20.0, maximum_size=1.0,
        ),
        settings=SolveSettings(),
    )
    S._child_entry(job, _Q())
    kinds = [m[0] for m in msgs]
    assert "fail" not in kinds
    assert ("ok", stub, fail_md) in msgs
    assert any(
        m[0] == "stage" and "Meshing failed" in m[1] for m in msgs
    )



def test_cancel_returns_none_and_kills_child():
    """Cancelling mid-run terminates the child and returns None."""
    if not SANDBOX.exists():
        pytest.skip("Sandbox example not present")
    from pdnsolver import mesh as _pdn_mesh
    from fypa.altium.loader import load_project, SolveSettings
    settings = SolveSettings()
    settings.mesh_max_size_mm = 2.0
    settings.apply_to_modules()
    cfg = _pdn_mesh.Mesher.Config(minimum_angle=settings.mesh_min_angle_deg,
                                  maximum_size=settings.mesh_max_size_mm)
    loaded = load_project(SANDBOX)
    job = S.SolveJob(loaded=loaded, mesher_config=cfg, settings=settings)

    seen = [0]
    proc_ref = {}
    result = S.run_solve_in_subprocess(
        job,
        on_stage=lambda m: seen.__setitem__(0, seen[0] + 1),
        on_substage=lambda m: seen.__setitem__(0, seen[0] + 1),
        is_cancelled=lambda: seen[0] >= 2,   # cancel after 2 progress msgs
        register_process=lambda p: proc_ref.setdefault("p", p),
    )
    assert result is None
    assert proc_ref.get("p") is not None
    assert not proc_ref["p"].is_alive()


def test_subprocess_solve_matches_in_process():
    """A full child solve returns a lean solution + metadata whose potential
    field matches an in-process solve of the same problem."""
    if not SANDBOX.exists():
        pytest.skip("Sandbox example not present")
    from pdnsolver import mesh as _pdn_mesh
    from fypa.altium.loader import (
        load_project, solve_problem_adaptive, SolveSettings,
    )
    from fypa.lean_solution import to_lean_solution

    settings = SolveSettings()
    settings.mesh_max_size_mm = 2.0     # coarse -> fast
    settings.apply_to_modules()
    cfg = _pdn_mesh.Mesher.Config(minimum_angle=settings.mesh_min_angle_deg,
                                  maximum_size=settings.mesh_max_size_mm)
    loaded = load_project(SANDBOX)

    def _stats(sol):
        chunks = []
        for ls in sol.layer_solutions:
            for zf in ls.potentials:
                arr = getattr(zf, "values", zf)
                chunks.append(np.asarray(arr, np.float64).ravel())
        v = np.concatenate(chunks)
        return float(v.min()), float(v.max()), float(np.linalg.norm(v))

    padne, *_ = solve_problem_adaptive(loaded, cfg, adaptive_regulator_gain=False)
    ref = _stats(to_lean_solution(padne))

    stages = []
    job = S.SolveJob(loaded=loaded, mesher_config=cfg, settings=settings)
    result = S.run_solve_in_subprocess(
        job,
        on_stage=lambda m: stages.append(m),
        on_substage=lambda m: stages.append(m),
        is_cancelled=lambda: False,
    )
    assert result is not None
    sub_sol, sub_meta = result
    assert isinstance(sub_meta, dict) and sub_meta
    assert stages, "no progress messages were forwarded"
    assert np.allclose(ref, _stats(sub_sol), rtol=1e-6, atol=1e-12)


# --- cheap hand-over: design from its cache file, result through a file -------

def _write_design_cache(path, loaded):
    """A design-info cache file holding ``loaded``, which is tagged with it
    the way the real load / save does."""
    import pickle

    from fypa import cli
    with open(path, "wb") as f:
        pickle.dump({"format": cli._DESIGN_INFO_FORMAT, "fingerprint": {}}, f)
        pickle.dump(loaded, f)
    cli.tag_design_cache_ref(loaded, cli._design_cache_stat(path))


def _edited_clone(loaded):
    """A clone whose extract differs from ``loaded``'s the way copper names
    change it: one primitive re-netted, one net added."""
    import dataclasses

    from fypa.altium.loader import clone_loaded_for_edit, replace_loaded
    ext = loaded.extracted
    new_net = dataclasses.replace(ext.nets[0], name="$EDIT_NET")
    tracks = list(ext.tracks)
    tracks[3] = dataclasses.replace(tracks[3], net_index=len(ext.nets))
    edited = dataclasses.replace(
        ext, tracks=type(ext.tracks)(tracks),
        nets=type(ext.nets)(list(ext.nets) + [new_net]))
    return replace_loaded(clone_loaded_for_edit(loaded), extracted=edited)


def test_extracted_delta_round_trip(tmp_path):
    """The delta is small, and applying it to the design as read back from
    the cache file gives the edited design exactly."""
    if not SANDBOX.exists():
        pytest.skip("Sandbox example not present")
    import pickle

    from fypa.altium.loader import load_project
    loaded = load_project(SANDBOX)
    edited = _edited_clone(loaded)

    delta = S._extracted_delta(loaded.extracted, edited.extracted)
    assert set(delta) == {"tracks", "nets"}
    assert delta["tracks"][0] == S._PATCH and len(delta["tracks"][1]) == 1
    assert len(pickle.dumps(delta)) < len(pickle.dumps(edited.extracted)) / 10

    from_disk = pickle.loads(pickle.dumps(loaded.extracted))
    rebuilt = S._apply_extracted_delta(from_disk, delta)
    assert rebuilt == edited.extracted
    assert S._extracted_digest(rebuilt) == S._extracted_digest(edited.extracted)
    assert S._extracted_digest(rebuilt) != S._extracted_digest(loaded.extracted)
    # Nothing changed: nothing to send, and the base itself comes back.
    assert S._extracted_delta(loaded.extracted, loaded.extracted) == {}
    assert S._apply_extracted_delta(from_disk, {}) is from_disk


def test_slim_job_only_when_the_cache_file_is_provably_the_design(tmp_path):
    if not SANDBOX.exists():
        pytest.skip("Sandbox example not present")
    from pdnsolver import mesh as _pdn_mesh
    from fypa.altium.loader import SolveSettings, load_project

    loaded = load_project(SANDBOX)
    edited = _edited_clone(loaded)
    job = S.SolveJob(loaded=edited, mesher_config=_pdn_mesh.Mesher.Config(),
                     settings=SolveSettings())
    # Untagged base: the design is sent whole.
    assert S._slim_job(job, loaded) is job

    cache = tmp_path / "design-info.pkl"
    _write_design_cache(cache, loaded)
    slim = S._slim_job(job, loaded)
    assert slim is not job and slim.loaded.extracted is None
    assert edited.extracted is not None          # the caller's object is intact
    assert slim.loaded.annotations is edited.annotations

    # The child rebuilds the same extract from the file...
    S._materialise_loaded(slim)
    assert slim.loaded.extracted == edited.extracted
    # ...and refuses a design that is not the one the parent described.
    wrong = S._slim_job(job, loaded)
    wrong.extracted_digest = "0" * 40
    with pytest.raises(S._StaleDesign):
        S._materialise_loaded(wrong)

    # A cache file that changed after the load no longer qualifies.
    with open(cache, "ab") as f:
        f.write(b"x")
    assert S._slim_job(job, loaded) is job
    stale = slim
    stale.loaded.extracted = None
    with pytest.raises(S._StaleDesign):
        S._materialise_loaded(stale)


def test_subprocess_solve_via_cache_file_matches_full_pickle(tmp_path):
    """The same solve, once with the design pickled across and once read by
    the child from the design-info cache: identical results, and the result
    file is cleaned up."""
    if not SANDBOX.exists():
        pytest.skip("Sandbox example not present")
    import glob
    import os
    import tempfile

    from pdnsolver import mesh as _pdn_mesh
    from fypa.altium.loader import SolveSettings, load_project

    settings = SolveSettings()
    settings.mesh_max_size_mm = 2.0
    settings.apply_to_modules()
    cfg = _pdn_mesh.Mesher.Config(minimum_angle=settings.mesh_min_angle_deg,
                                  maximum_size=settings.mesh_max_size_mm)
    loaded = load_project(SANDBOX)
    _write_design_cache(tmp_path / "design-info.pkl", loaded)
    from fypa.altium.loader import clone_loaded_for_edit
    edited = clone_loaded_for_edit(loaded)

    def _run(base):
        stages: list = []
        job = S.SolveJob(loaded=edited, mesher_config=cfg, settings=settings)
        sol, meta = S.run_solve_in_subprocess(
            job, on_stage=stages.append, on_substage=lambda m: None,
            is_cancelled=lambda: False, base_loaded=base)
        return sol, meta, stages

    def _potentials(sol):
        return [np.asarray(p) for ls in sol.layer_solutions
                for p in ls.potentials]

    full_sol, full_meta, full_stages = _run(None)
    slim_sol, slim_meta, slim_stages = _run(loaded)
    assert not any("Reading the design" in s for s in full_stages)
    assert any("Reading the design" in s for s in slim_stages)
    a, b = _potentials(full_sol), _potentials(slim_sol)
    assert len(a) == len(b) and a
    for x, y in zip(a, b):
        assert np.allclose(x, y, rtol=1e-9, atol=1e-12)
    assert sorted(full_meta) == sorted(slim_meta)
    assert edited.extracted is loaded.extracted      # parent objects untouched
    assert not glob.glob(os.path.join(tempfile.gettempdir(), "fypa-solve-*"))
