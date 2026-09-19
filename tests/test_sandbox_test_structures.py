"""IR-drop checks against the known test structures in ``ExampleDesigns/Sandbox``.

The Sandbox board carries deliberately simple copper pours whose resistance is
known from geometry alone. Each is a rectangular polygon on the top layer fed
from a 1 V source at one end and loaded by a 1 A sink at the other, so the
potential span across the pour *is* its resistance in ohms.

    R = R_sq * L / W        R_sq = 1 / layer_conductance

``R_sq`` comes from the solver's own physics constants rather than a literal,
so a deliberate change to copper resistivity or plated thickness moves the
expectation with it instead of failing these tests.

Two kinds of check live here, for the same reason ``test_analytical.py``
splits them:

* **Absolute** — measured span against ``R_sq * L / W``. The ideal formula
  assumes current crosses the full width uniformly, which is untrue near the
  pads, so each structure carries its own tolerance and a note on why it
  deviates.
* **Differential** — the *difference* between two structures of equal width.
  The end effects are then an additive constant common to both and cancel
  exactly, leaving pure sheet resistance. These hold to a few parts in 10^5
  and are the real regression gate on the solver's bulk physics.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRJPCB = REPO_ROOT / "ExampleDesigns" / "Sandbox" / "Sandbox.PrjPcb"

LOAD_CURRENT_A = 1.0

# net name, length (mm), width (mm), absolute tolerance as a fraction
STRUCTURES = [
    # Long and narrow: the 1 mm pads nearly span the width, so there is very
    # little room for current to spread and the ideal formula is close.
    ("P1V_1W100L", 100.0, 1.0, 0.03),
    ("P1V_1W50L", 50.0, 1.0, 0.03),
    # Only 10 squares long, so the pads occupy a large fraction of the run.
    # Pad copper is an equipotential patch, which shortens the resistive path
    # and pulls the measured drop *below* the ideal.
    ("P1V_1W10L", 10.0, 1.0, 0.06),
    # 10 mm wide fed from a ~1 mm pad: current has to spread out at one end
    # and converge at the other, and that constriction resistance is real
    # copper physics the L/W formula does not model. Reads ~10 % high.
    ("P1V_10W100L", 100.0, 10.0, 0.15),
]


@pytest.fixture(scope="module")
def sandbox():
    """Solve the Sandbox design once and reduce it to per-net facts.

    Returns ``(drops_mv, r_sq_mohm)`` where *drops_mv* maps a net name to the
    potential span over all of its copper.
    """
    pytest.importorskip("altium_monkey")
    if not PRJPCB.exists():
        pytest.skip(f"example design not present: {PRJPCB}")

    from pdnsolver import mesh as _pdn_mesh

    from fypa.altium.loader import load_project, solve_problem_adaptive

    loaded = load_project(PRJPCB)
    assert loaded.is_solveable, loaded.diagnostic_summary()
    solution = solve_problem_adaptive(loaded, _pdn_mesh.Mesher.Config())[0]

    drops: dict[str, float] = {}
    conductances: dict[str, float] = {}
    for layer, layer_solution in zip(solution.problem.layers,
                                     solution.layer_solutions):
        if "|" not in layer.name:
            continue
        net = layer.name.split("|", 1)[1]
        # padne hands back ZeroForms (mesh + values); the lean pickle hands
        # back bare arrays. Accept either.
        pots = [
            np.asarray(getattr(p, "values", p), dtype=float)
            for p in layer_solution.potentials
        ]
        if not pots:
            continue
        field = np.concatenate(pots)
        span_mv = float(field.max() - field.min()) * 1e3
        drops[net] = max(drops.get(net, 0.0), span_mv)
        conductances[net] = layer.conductance

    # Every structure under test is single-layer top copper, so one sheet
    # resistance covers them all.
    r_sq_mohm = 1e3 / conductances["P1V_1W100L"]
    return drops, r_sq_mohm


def test_sheet_resistance_is_one_ounce_copper(sandbox):
    """Sanity-check the constant the expectations are built on.

    ~0.473 mOhm/square is 1 oz copper at 20 C. A hand calculation using
    sigma = 6.0e7 S/m gives 0.4762; the solver uses the slightly more
    resistive annealed-copper figure, 0.8 % apart.
    """
    _drops, r_sq = sandbox
    assert r_sq == pytest.approx(0.4726, rel=0.02), (
        f"sheet resistance {r_sq:.5f} mOhm/sq is not 1 oz copper — the "
        "structure expectations below are derived from it"
    )


@pytest.mark.parametrize(("net", "length_mm", "width_mm", "tol"), STRUCTURES)
def test_structure_drop_matches_geometry(sandbox, net, length_mm, width_mm, tol):
    drops, r_sq = sandbox
    assert net in drops, f"{net} not in the solution: {sorted(drops)}"

    squares = length_mm / width_mm
    expected_mv = r_sq * squares * LOAD_CURRENT_A
    measured_mv = drops[net]
    rel_err = (measured_mv - expected_mv) / expected_mv

    assert abs(rel_err) < tol, (
        f"{net} ({width_mm:g} mm x {length_mm:g} mm, {squares:g} squares): "
        f"measured {measured_mv:.4f} mV vs ideal {expected_mv:.4f} mV "
        f"({rel_err:+.1%}, tolerance {tol:.0%})"
    )


@pytest.mark.parametrize(
    ("longer", "shorter", "extra_squares"),
    [
        ("P1V_1W100L", "P1V_1W50L", 50.0),
        ("P1V_1W50L", "P1V_1W10L", 40.0),
        ("P1V_1W100L", "P1V_1W10L", 90.0),
    ],
)
def test_incremental_length_is_pure_sheet_resistance(
    sandbox, longer, shorter, extra_squares,
):
    """Same width, different length: the difference is exactly the extra copper.

    Both structures have identical pads and identical current spreading, so
    whatever end effect inflates or deflates their absolute drop is the same
    additive term in each and cancels here. What remains is the sheet
    resistance of the extra squares, which the solver gets right to a few
    parts in 10^5 — far tighter than any absolute check can be.
    """
    drops, r_sq = sandbox
    measured_mv = drops[longer] - drops[shorter]
    expected_mv = r_sq * extra_squares * LOAD_CURRENT_A
    rel_err = (measured_mv - expected_mv) / expected_mv

    assert abs(rel_err) < 0.01, (
        f"{longer} - {shorter} = {measured_mv:.4f} mV, but the {extra_squares:g} "
        f"squares between them are worth {expected_mv:.4f} mV ({rel_err:+.3%})"
    )


def test_wide_pour_reads_above_the_ideal_sheet_value(sandbox):
    """A wide pour fed from a small pad costs more than L/W says.

    P1V_1W10L and P1V_10W100L are both 10 squares, so the ideal formula gives
    them the same resistance. They do not measure the same: the 10 mm pour
    makes the current spread out from the pad and converge again, and that
    constriction resistance is real. A regression that erased it — say, by
    treating a pad as spanning the full pour width — would show up as these
    two converging.
    """
    drops, r_sq = sandbox
    ideal_mv = r_sq * 10.0 * LOAD_CURRENT_A
    narrow = drops["P1V_1W10L"]
    wide = drops["P1V_10W100L"]

    assert narrow < ideal_mv < wide, (
        f"expected narrow ({narrow:.4f} mV) < ideal ({ideal_mv:.4f} mV) "
        f"< wide ({wide:.4f} mV)"
    )
    assert wide / narrow == pytest.approx(1.15, rel=0.10), (
        f"spreading penalty changed: wide/narrow = {wide / narrow:.4f}"
    )
