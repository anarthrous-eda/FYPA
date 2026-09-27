"""Design report: rules, verdicts, and an end-to-end run on the Sandbox board.

The rule tests build :class:`RailReport` objects by hand, so they pin the
pass / fail logic without a solve. The Sandbox tests solve the example design
once and check the report against numbers that can be read straight off the
solution — the report must agree with the viewer's Nodes / Vias tables, which
share :mod:`fypa.solution_sampling` with it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fypa.report import checks
from fypa.report.model import (
    FAIL,
    PASS,
    UNCHECKED,
    WARN,
    DecouplingResult,
    LoadResult,
    RailReport,
    Report,
    ReportSettings,
    ViaResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PRJPCB = REPO_ROOT / "ExampleDesigns" / "Sandbox" / "Sandbox.PrjPcb"


# --- rule tests (no solve) --------------------------------------------------------

def _load(v_load, min_v=None, kind="sink", label="U1"):
    return LoadResult(
        label=label, kind=kind, net="VDD", pin_count=1, current_a=1.0,
        v_load=v_load, drop_v=None if v_load is None else 1.0 - v_load,
        return_drop_v=0.0, min_v=min_v,
        margin_v=None if min_v is None or v_load is None else v_load - min_v,
        status=PASS, worst_pin="U1-1", worst_layer="Top")


def _rail(loads, **kw):
    rail = RailReport(name="VDD", kind="supply", members=["VDD"],
                      nominal_v=1.0, source_label="U9 (source)", load_a=1.0,
                      loads=loads)
    for k, v in kw.items():
        setattr(rail, k, v)
    return rail


def test_load_below_min_v_fails():
    rail = _rail([_load(0.94, min_v=0.95)])
    checks.rail_findings(rail, ReportSettings())
    assert rail.status == FAIL
    assert rail.loads[0].status == FAIL
    assert [f.code for f in rail.findings] == ["below-min-v"]


def test_small_margin_warns_at_the_configured_share_of_nominal():
    # 5 mV margin on a 1 V rail: under 1 % warns, under 0.4 % does not.
    rail = _rail([_load(0.955, min_v=0.95)])
    checks.rail_findings(rail, ReportSettings(margin_warn_pct=1.0))
    assert rail.status == WARN
    rail = _rail([_load(0.955, min_v=0.95)])
    checks.rail_findings(rail, ReportSettings(margin_warn_pct=0.4))
    assert rail.status == PASS


def test_no_limit_is_unchecked_not_a_pass():
    rail = _rail([_load(0.99)])
    checks.rail_findings(rail, ReportSettings())
    # The rail has nothing wrong with it, but its load was never compared
    # against anything — that must be visible, not folded into PASS.
    assert rail.status == PASS
    assert rail.loads[0].status == UNCHECKED
    assert [f.severity for f in rail.findings] == [UNCHECKED]


def test_drop_budget_fails_even_without_min_v():
    rail = _rail([_load(0.96)])
    checks.rail_findings(rail, ReportSettings(drop_budget_pct=3.0))
    assert rail.status == FAIL
    assert any(f.code == "drop-budget" for f in rail.findings)


def test_via_over_limit_fails():
    via = ViaResult(x_mm=1, y_mm=2, net="VDD", span="Top → Bottom",
                    drill_mm=0.2, fill="—", current_a=1.4, delta_v=1e-3,
                    power_w=1e-3, over_limit=True)
    rail = _rail([_load(0.99, min_v=0.9)], vias=[via], vias_over=1,
                 max_via_a=1.4)
    checks.rail_findings(rail, ReportSettings())
    assert rail.status == FAIL
    assert any(f.code == "via-current" for f in rail.findings)


def test_impedance_miss_fails_and_flagged_caps_warn():
    dec = DecouplingResult(analysed=True, target_ohm=0.012, f_max_hz=40e6,
                           meets_target=False, worst_z_ohm=0.021,
                           worst_f_hz=18e6, n_modelled=11, n_under_1nh=4,
                           flagged=[("C17", "single-via", 2.4)])
    rail = _rail([_load(0.99, min_v=0.9)], decoupling=dec)
    checks.rail_findings(rail, ReportSettings())
    codes = {f.code: f.severity for f in rail.findings}
    assert codes == {"impedance": FAIL, "cap-flags": WARN}


def test_decoupling_that_never_ran_raises_no_finding():
    dec = DecouplingResult(analysed=False, reason="Not run.")
    rail = _rail([_load(0.99, min_v=0.9)], decoupling=dec)
    checks.rail_findings(rail, ReportSettings())
    assert rail.findings == []
    dec = DecouplingResult(analysed=False, reason="None found.",
                           flag_missing=True)
    rail = _rail([_load(0.99, min_v=0.9)], decoupling=dec)
    checks.rail_findings(rail, ReportSettings())
    assert [f.code for f in rail.findings] == ["no-impedance"]


def _report(rails, design=()):
    return Report(project_name="P", prjpcb_path="", pcbdoc_path="",
                  generated="", fypa_version="", settings=ReportSettings(),
                  rails=rails, design_findings=list(design))


@pytest.mark.parametrize("loads, severity, headline", [
    ([_load(0.99, min_v=0.9)], PASS, "All 1 rail passed"),
    ([_load(0.99)], UNCHECKED, "1 rail assessed, 1 result not checked "
                               "against a limit"),
    ([_load(0.905, min_v=0.9)], WARN, "1 of 1 rail passed, 1 with warnings"),
    ([_load(0.85, min_v=0.9)], FAIL, "1 of 1 rail failed"),
])
def test_verdict_states_the_outcome_plainly(loads, severity, headline):
    rail = _rail(loads)
    checks.rail_findings(rail, ReportSettings())
    sev, head, _why = _report([rail]).verdict()
    assert (sev, head) == (severity, headline)


def test_settings_round_trip_drops_unknown_values():
    s = ReportSettings.from_dict({"detail": "bogus", "sections": ["dc", "x"],
                                  "via_limit_a": 2.0, "format": "pdf"})
    assert s.detail == "failing"
    assert s.sections == ["dc"]
    assert s.via_limit_a == 2.0
    assert ReportSettings.from_dict(s.to_dict()) == s


# --- end to end on Sandbox ---------------------------------------------------------

@pytest.fixture(scope="module")
def sandbox_solve(tmp_path_factory):
    pytest.importorskip("altium_monkey")
    if not PRJPCB.exists():
        pytest.skip(f"example design not present: {PRJPCB}")
    from fypa.cli import _load_solution_pickle, main

    out = tmp_path_factory.mktemp("report") / "sandbox.pkl"
    assert main(["solve", str(PRJPCB), str(out)]) == 0
    return _load_solution_pickle(out)


@pytest.fixture(scope="module")
def sandbox_report(sandbox_solve):
    from fypa.report import build_report
    solution, metadata = sandbox_solve
    return build_report(solution, metadata, ReportSettings(detail="all"))


def test_sandbox_verdict(sandbox_report):
    # P1V_ARC carries the only PDN_MIN_V (0.95 V) and misses it.
    sev, head, why = sandbox_report.verdict()
    assert sev == FAIL
    assert head == f"1 of {len(sandbox_report.rails)} rails failed"
    assert "P1V_ARC" in why
    assert sandbox_report.rails[0].name == "P1V_ARC"


def test_sandbox_rail_kinds(sandbox_report):
    kinds = {r.name: r.kind for r in sandbox_report.rails}
    assert kinds["GND"] == "return"
    assert kinds["P1V_1W100L"] == "supply"
    assert sandbox_report.rails[-1].kind == "return"


def test_load_voltage_matches_the_solved_pins(sandbox_solve, sandbox_report):
    """V at load = sink P pin minus sink N pin, as the Nodes tab samples them;
    drop = setpoint minus that."""
    from fypa.rail_groups import compute_rail_groups
    from fypa.solution_sampling import SolutionSampler, compute_node_rows

    solution, metadata = sandbox_solve
    _names, members = compute_rail_groups(metadata)
    rows = compute_node_rows(SolutionSampler(solution, metadata), members)
    by_term = {(r["designator"], r["terminal"]): r["voltage"] for r in rows}
    rail = next(r for r in sandbox_report.rails if r.name == "P1V_1W100L")
    (load,) = rail.loads
    expected = by_term[("J2", "P")] - by_term[("J2", "N")]
    assert load.v_load == pytest.approx(expected, abs=1e-12)
    assert load.drop_v == pytest.approx(1.0 - expected, abs=1e-12)
    # 100 squares of ~0.47 mΩ/□ copper at 1 A, plus the return path.
    assert 0.045 < load.drop_v < 0.06
    # Every bit of the rail's loss is on the one layer it's drawn on.
    assert sum(lr.share for lr in rail.layers) == pytest.approx(1.0)


def test_sandbox_single_via_at_exactly_the_limit_is_not_over(sandbox_report):
    # P1V_VIA pushes its whole 1 A load through one via: at the 1 A limit,
    # not above it.
    rail = next(r for r in sandbox_report.rails if r.name == "P1V_VIA")
    assert rail.max_via_a == pytest.approx(1.0, rel=1e-4)
    assert rail.vias_over == 0


def test_sandbox_writes_html_pdf_and_json(sandbox_report, tmp_path):
    from fypa.report import write_report

    html_path, json_path = write_report(sandbox_report, tmp_path / "r.html",
                                        json_sidecar=True)
    text = html_path.read_text(encoding="utf-8")
    assert "1 of 13 rails failed" in text
    assert 'id="rail-p1v-arc"' in text
    assert "data:image/png;base64," in text

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["verdict"]["severity"] == FAIL
    arc = next(r for r in data["rails"] if r["name"] == "P1V_ARC")
    assert arc["status"] == FAIL and "figures" not in arc

    (pdf_path,) = write_report(sandbox_report, tmp_path / "r.pdf")
    assert pdf_path.read_bytes()[:5] == b"%PDF-"


def test_cli_report_fail_on(sandbox_solve, tmp_path):
    import pickle

    from fypa.cli import main

    solution, metadata = sandbox_solve
    pkl = tmp_path / "s.pkl"
    with open(pkl, "wb") as f:
        pickle.dump({"solution": solution, "metadata": metadata}, f)
    out = tmp_path / "r.html"
    assert main(["report", str(pkl), str(out), "--detail", "summary"]) == 0
    assert out.exists()
    assert main(["report", str(pkl), str(out), "--detail", "summary",
                 "--fail-on", "fail"]) == 1


# --- viewer adapter: capacitor / impedance rows → DecouplingResult -------------------

def test_viewer_decoupling_maps_cap_rows_and_impedance():
    """The Capacitors / Impedance tab data becomes one DecouplingResult per
    rail: a single 100 nF cap can't hold a 1 mΩ target, so it misses."""
    import types

    from fypa.caploop.impedance import (
        CapBranch,
        RailTarget,
        VrmModel,
        log_freqs,
        rail_impedance,
    )
    from fypa.viewer.report_export import _ReportExportMixin

    rows = [
        {"rail": "VDD", "designator": "C1", "capacitance_f": 100e-9,
         "package": "0402", "l1_nh": 0.8, "l2_nh": None, "l3_nh": None,
         "flags": [], "included": True},
        {"rail": "VDD", "designator": "C2", "capacitance_f": 10e-6,
         "package": "0805", "l1_nh": 2.6, "l2_nh": None, "l3_nh": None,
         "flags": ["long-escape"], "included": True},
        {"rail": "VDD", "designator": "C3", "capacitance_f": 1e-6,
         "package": "0603", "l1_nh": 0.5, "l2_nh": None, "l3_nh": None,
         "flags": [], "included": False},
    ]

    def impedance(rail):
        branches = [CapBranch("C1", 100e-9, 0.02, 0.3e-9, 0.8e-9),
                    CapBranch("C2", 10e-6, 0.005, 0.5e-9, 2.6e-9)]
        target = RailTarget(rail, 1.0, ripple_pct=0.1,
                            transient_current_a=1.0, f_max_hz=40e6)
        return rail_impedance(rail, log_freqs(), branches, target,
                              VrmModel(), 0.0, [("C9", "no value")])

    fake = types.SimpleNamespace(
        _caps_rows_cache=rows, _caploop_rollup={},
        _impedance_rails=lambda: ["VDD"],
        _compute_rail_impedance=impedance,
        _cap_l_best_nh=lambda r: r["l3_nh"] or r["l2_nh"] or r["l1_nh"],
        _cap_row_is_warn=lambda r: bool(r["flags"]) or r["l1_nh"] >= 2.0,
    )
    results, note = _ReportExportMixin._report_decoupling(fake)
    assert note == ""
    dec = results["VDD"]
    assert dec.analysed and dec.meets_target is False
    assert dec.target_ohm == pytest.approx(1e-3)
    assert dec.worst_z_ohm > dec.target_ohm and dec.worst_f_hz <= 40e6
    assert (dec.n_caps, dec.n_modelled, dec.n_under_1nh) == (3, 2, 1)
    assert [d for d, _why, _l in dec.flagged] == ["C2"]
    assert dec.skipped == [("C9", "no value")]
    assert [c["tier"] for c in dec.cap_rows] == ["Tier 1", "Tier 1",
                                                 "excluded"]
    assert dec.z_png and dec.z_png[:4] == b"\x89PNG"
    assert "Tier 1" in dec.tier_note


def test_viewer_decoupling_without_caps_passes_the_reason_on():
    import types

    from fypa.viewer.report_export import _ReportExportMixin

    fake = types.SimpleNamespace(
        _caps_rows_cache=[],
        _caps_empty_reason="Gerber imports carry no component data.")
    assert _ReportExportMixin._report_decoupling(fake) == (
        None, "Gerber imports carry no component data.")
