"""End-to-end golden-file regression tests.

Each test shells out to ``FYPA.py solve`` on a bundled example design, reduces
the solution to a compact numeric *fingerprint* (mesh counts + potential-field
statistics + solver diagnostics), and compares it against a committed golden
file under ``tests/golden/``.

The fingerprint — rather than the full multi-megabyte field — keeps the golden
files small enough to commit while still catching real drift: a mesher change
moves the vertex/triangle counts, and a solver change moves the field stats.

On the first run for a design (no golden yet) the fingerprint is written and
the test is skipped — commit the generated file to lock the baseline in.

``Sandbox`` runs by default (~5 s).
"""
from __future__ import annotations

import json
import math
import pickle
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# Potential-field statistics must match the golden to this relative tolerance.
FIELD_REL_TOL = 1e-6
# Near-zero solver diagnostics are compared against an absolute ceiling, not
# the golden value — their last digits are run-to-run noise.
GROUND_CURRENT_CEILING = 1e-3
RESIDUAL_CEILING = 1e-4


def _fingerprint(solution) -> dict:
    """Reduce a LeanSolution to a small, comparable, JSON-able dict."""
    all_potentials: list[np.ndarray] = []
    n_components = 0
    total_triangles = 0
    for layer_solution in solution.layer_solutions:
        for potentials in layer_solution.potentials:
            all_potentials.append(np.asarray(potentials, dtype=np.float64))
            n_components += 1
        for triangles in layer_solution.triangles:
            total_triangles += int(len(triangles))

    field = (
        np.concatenate(all_potentials) if all_potentials else np.zeros(0, dtype=np.float64)
    )
    assert np.all(np.isfinite(field)), "solution contains non-finite potentials"

    info = solution.solver_info or {}
    return {
        "n_layer_solutions": len(solution.layer_solutions),
        "n_components": n_components,
        "total_vertices": int(field.size),
        "total_triangles": total_triangles,
        "potential_min": float(field.min()) if field.size else 0.0,
        "potential_max": float(field.max()) if field.size else 0.0,
        "potential_mean": float(field.mean()) if field.size else 0.0,
        "potential_std": float(field.std()) if field.size else 0.0,
        "potential_abs_l2": float(np.linalg.norm(field)),
        "ground_node_current": float(info.get("ground_node_current", 0.0)),
        "residual_norm": float(info.get("residual_norm", 0.0)),
    }


def _solve_design(prjpcb: Path, out_path: Path) -> dict:
    """Run ``FYPA.py solve`` and return the fingerprint of the result."""
    result = subprocess.run(
        [sys.executable, "FYPA.py", "solve", str(prjpcb), str(out_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, (
        f"FYPA.py solve failed ({result.returncode}):\n{result.stderr[-2000:]}"
    )
    assert out_path.exists(), "solve reported success but wrote no output pickle"
    with open(out_path, "rb") as f:
        obj = pickle.load(f)
    return _fingerprint(obj["solution"])


def _compare_to_golden(design: str, current: dict) -> None:
    """Assert ``current`` matches the committed golden, or create it."""
    golden_path = GOLDEN_DIR / f"{design}.json"
    if not golden_path.exists():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(current, indent=2, sort_keys=True))
        pytest.skip(
            f"no golden for '{design}' — wrote {golden_path.name}; "
            "review and commit it to lock the baseline"
        )

    golden = json.loads(golden_path.read_text())

    # Integer mesh-topology counts must match exactly.
    for key in ("n_layer_solutions", "n_components", "total_vertices", "total_triangles"):
        assert current[key] == golden[key], (
            f"{design}: '{key}' changed {golden[key]} -> {current[key]}. "
            "If this is an intentional mesher change, delete "
            f"tests/golden/{design}.json and regenerate."
        )

    # Field statistics must match within tolerance.
    for key in (
        "potential_min",
        "potential_max",
        "potential_mean",
        "potential_std",
        "potential_abs_l2",
    ):
        assert math.isclose(
            current[key], golden[key], rel_tol=FIELD_REL_TOL, abs_tol=1e-12
        ), f"{design}: '{key}' drifted {golden[key]!r} -> {current[key]!r}"

    # Solver diagnostics are near zero; gate on a ceiling rather than equality.
    assert abs(current["ground_node_current"]) < GROUND_CURRENT_CEILING, (
        f"{design}: ground node current {current['ground_node_current']:.3e} A "
        "is far from zero"
    )
    assert current["residual_norm"] < RESIDUAL_CEILING, (
        f"{design}: residual norm {current['residual_norm']:.3e} is too large"
    )


@pytest.mark.parametrize("design", ["Sandbox"])
def test_example_design_solution_matches_golden(design, tmp_path):
    prjpcb = REPO_ROOT / "ExampleDesigns" / design / f"{design}.PrjPcb"
    if not prjpcb.exists():
        pytest.skip(f"example design not present: {prjpcb}")

    current = _solve_design(prjpcb, tmp_path / f"{design}.pkl")
    _compare_to_golden(design, current)


@pytest.mark.parametrize("design", ["KiCAD_Sandbox"])
def test_kicad_example_design_solution_matches_golden(design, tmp_path):
    # The KiCAD path is dispatched by file suffix, so `FYPA.py solve` on a
    # .kicad_pcb exercises the whole extract → annotations → solve pipeline.
    board = REPO_ROOT / "ExampleDesigns" / design / f"{design}.kicad_pcb"
    if not board.exists():
        pytest.skip(f"example design not present: {board}")

    current = _solve_design(board, tmp_path / f"{design}.pkl")
    _compare_to_golden(design, current)
