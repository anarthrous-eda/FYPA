"""The out-of-process solve path (fypa.solve_subprocess).

Qt-free, so it runs headless in CI. Covers the opt-in flag, error marshaling
across the process boundary, and — when the Sandbox example is present — a full
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
    SolveSubprocessError carrying the child's traceback — never a hang."""
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
