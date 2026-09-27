"""Pass / fail rules for the design report.

Every rule turns a computed result into a :class:`Finding`. A result with no
limit to check against becomes UNCHECKED, never PASS, so a rail nobody set
limits on can't read as green.
"""
from __future__ import annotations

import math
import re

from fypa.report.model import (
    FAIL,
    PASS,
    UNCHECKED,
    WARN,
    Finding,
    RailReport,
    ReportSettings,
    worst_severity,
)


def fmt_mv(v: float) -> str:
    return f"{v * 1e3:.3g} mV" if abs(v) < 1.0 else f"{v:.3f} V"


def fmt_xy(xy) -> str:
    return f"({xy[0]:.2f}, {xy[1]:.2f}) mm"


def rail_findings(rail: RailReport, s: ReportSettings) -> None:
    """Set each load's status and the rail's findings and status."""
    out: list[Finding] = []
    nominal = rail.nominal_v or 0.0

    if rail.kind == "supply":
        no_limit = []
        for ld in rail.loads:
            if ld.v_load is None:
                ld.status = UNCHECKED
                out.append(Finding(
                    UNCHECKED, f"{ld.label}: no solved voltage at its pins",
                    "The pins are off the solved copper, so this load could "
                    "not be assessed.", rail.name, "load not solved",
                    "load-unsampled"))
                continue
            status = PASS
            if ld.min_v is not None and ld.margin_v is not None:
                where = (f"Drop {fmt_mv(ld.drop_v or 0.0)} from the "
                         f"{nominal:.4g} V setpoint"
                         + (f" ({fmt_mv(ld.return_drop_v)} of it in the "
                            "return path)" if ld.return_drop_v else "")
                         + f". Lowest pin {ld.worst_pin} on {ld.worst_layer}.")
                if ld.margin_v < 0:
                    status = FAIL
                    out.append(Finding(
                        FAIL,
                        f"{ld.label} at {ld.v_load:.3f} V, "
                        f"{fmt_mv(-ld.margin_v)} below PDN_MIN_V "
                        f"{ld.min_v:.3f} V", where, rail.name,
                        "load below minimum voltage", "below-min-v"))
                elif nominal and ld.margin_v < nominal * s.margin_warn_pct / 100:
                    status = WARN
                    out.append(Finding(
                        WARN,
                        f"{ld.label}: only {fmt_mv(ld.margin_v)} above "
                        f"PDN_MIN_V {ld.min_v:.3f} V",
                        f"Margin is under {s.margin_warn_pct:g} % of the "
                        f"{nominal:.4g} V nominal. " + where, rail.name,
                        "low voltage margin", "low-margin"))
            elif ld.kind == "sink":
                no_limit.append(ld.label)
            if (s.drop_budget_pct is not None and nominal
                    and ld.drop_v is not None
                    and ld.drop_v > nominal * s.drop_budget_pct / 100):
                status = FAIL
                out.append(Finding(
                    FAIL,
                    f"{ld.label}: drop {fmt_mv(ld.drop_v)} "
                    f"({100 * ld.drop_v / nominal:.2g} %) exceeds the "
                    f"{s.drop_budget_pct:g} % budget",
                    f"Lowest pin {ld.worst_pin} on {ld.worst_layer}.",
                    rail.name, "drop over budget", "drop-budget"))
            elif (status == PASS and ld.min_v is None
                  and s.drop_budget_pct is None):
                status = UNCHECKED
            ld.status = status
        if no_limit:
            n_sinks = sum(1 for ld in rail.loads if ld.kind == "sink")
            who = ", ".join(no_limit[:4]) + (
                f" and {len(no_limit) - 4} more" if len(no_limit) > 4 else "")
            out.append(Finding(
                UNCHECKED,
                (f"No PDN_MIN_V on {len(no_limit)} of {n_sinks} sinks"
                 if len(no_limit) < n_sinks else "No PDN_MIN_V on any sink"),
                f"The drop at {who} was calculated but not compared against "
                "a limit.", rail.name, "no voltage limit", "no-min-v"))

    if rail.vias_over:
        over = [v for v in rail.vias if v.over_limit]
        worst = over[0] if over else None
        share = ""
        if rail.load_a and over:
            share = (f" Together they carry "
                     f"{sum(v.current_a for v in over) / rail.load_a:.0%} "
                     "of the rail current.")
        out.append(Finding(
            FAIL,
            f"{rail.vias_over} via{'s' if rail.vias_over != 1 else ''} above "
            f"the {s.via_limit_a:g} A limit (max {rail.max_via_a:.3g} A)",
            (f"Worst at {fmt_xy((worst.x_mm, worst.y_mm))}, {worst.span}."
             if worst else "") + share,
            rail.name, "via current over limit", "via-current"))

    if (s.j_limit_a_per_mm is not None and rail.peak_j_a_per_mm is not None
            and rail.peak_j_a_per_mm > s.j_limit_a_per_mm):
        top = max(rail.layers, key=lambda lr: lr.peak_j_a_per_mm)
        out.append(Finding(
            WARN,
            f"Peak current density {rail.peak_j_a_per_mm:.3g} A/mm, limit "
            f"{s.j_limit_a_per_mm:g} A/mm",
            f"On {top.layer} at {fmt_xy(top.peak_j_xy)}."
            if top.peak_j_xy else "", rail.name,
            "current density over limit", "current-density"))

    dec = rail.decoupling
    if dec is not None and dec.analysed:
        if dec.meets_target is False:
            out.append(Finding(
                FAIL,
                f"Impedance {dec.worst_z_ohm * 1e3:.3g} mΩ at "
                f"{_fmt_hz(dec.worst_f_hz)}, target "
                f"{dec.target_ohm * 1e3:.3g} mΩ"
                if dec.worst_z_ohm is not None and dec.worst_f_hz
                and dec.target_ohm is not None
                else "Impedance misses its target",
                (f"The target holds up to {_fmt_hz(dec.reached_hz)}. "
                 if dec.reached_hz else "")
                + f"{dec.n_under_1nh} of {dec.n_modelled} modelled "
                "capacitors reach below 1 nH loop inductance.",
                rail.name, "impedance misses target", "impedance"))
        if dec.flagged:
            names = ", ".join(f"{d} ({why})" for d, why, _l in dec.flagged[:4])
            more = (f" and {len(dec.flagged) - 4} more"
                    if len(dec.flagged) > 4 else "")
            out.append(Finding(
                WARN,
                f"{len(dec.flagged)} capacitor"
                f"{'s' if len(dec.flagged) != 1 else ''} flagged",
                names + more + ".", rail.name, "capacitors flagged",
                "cap-flags"))
        if dec.skipped:
            out.append(Finding(
                UNCHECKED,
                f"{len(dec.skipped)} capacitor"
                f"{'s' if len(dec.skipped) != 1 else ''} left out of the "
                "impedance model",
                "; ".join(f"{d}: {why}" for d, why in dec.skipped[:3]) + ".",
                rail.name, "capacitors not modelled", "cap-skipped"))
    elif dec is not None and dec.flag_missing:
        out.append(Finding(
            UNCHECKED, "Decoupling impedance not analysed", dec.reason,
            rail.name, "impedance not analysed", "no-impedance"))

    rail.findings = out
    rail.status = worst_severity(
        [f.severity for f in out if f.severity in (FAIL, WARN)]) \
        if any(f.severity in (FAIL, WARN) for f in out) else PASS


def design_findings(metadata: dict, solution, solve_stale: bool
                    ) -> list[Finding]:
    """Findings that belong to the whole design rather than one rail."""
    out: list[Finding] = []
    if solve_stale:
        out.append(Finding(
            WARN, "The design was edited after this solve",
            "The results below reflect the last solve. Re-solve before "
            "relying on this report.", None, "solve is out of date",
            "stale"))
    for mf in metadata.get("mesh_failures") or []:
        net = mf.get("net", "?") if isinstance(mf, dict) else "?"
        summary = mf.get("summary", "") if isinstance(mf, dict) else str(mf)
        out.append(Finding(
            FAIL, f"Mesh failed for {net}; its results are missing",
            summary, None, "mesh failed", "mesh-failed"))
    for msg in metadata.get("annotation_errors") or []:
        out.append(_msg_finding(FAIL, msg, "annotation error"))
    for msg in metadata.get("connectivity_breaks") or []:
        out.append(_msg_finding(FAIL, msg, "copper connectivity break"))
    special = set(metadata.get("open_loop_rails") or []) | set(
        metadata.get("unannotated_bridges") or [])
    for msg in metadata.get("open_loop_rails") or []:
        out.append(_msg_finding(WARN, msg, "rail not solved"))
    for msg in metadata.get("unannotated_bridges") or []:
        out.append(_msg_finding(WARN, msg, "unannotated bridge"))
    for msg in metadata.get("annotation_warnings") or []:
        if msg not in special:
            out.append(_msg_finding(WARN, msg, "annotation warning"))

    stats = metadata.get("solver_stats") or {}
    res = stats.get("residual_norm")
    gnd = stats.get("ground_node_current_A")
    # Same thresholds the Setup tab colours by.
    if isinstance(res, (int, float)) and math.isfinite(res) and res > 1e-3:
        out.append(Finding(
            FAIL if res > 1.0 else WARN,
            f"Solver residual {res:.3g} is high",
            "The linear solve did not converge cleanly; results may be "
            "inaccurate.", None, "solver residual high", "residual"))
    if isinstance(gnd, (int, float)) and math.isfinite(gnd) and abs(gnd) > 1e-3:
        out.append(Finding(
            FAIL if abs(gnd) > 0.1 else WARN,
            f"Ground-node current {gnd * 1e3:.3g} mA",
            "Should be close to zero for a well-posed problem; a large value "
            "usually means a current loop does not close through copper.",
            None, "current loop not closed", "ground-current"))
    info = getattr(solution, "solver_info", None) or {}
    if info.get("thermal_iterations") and info.get("thermal_converged") is False:
        out.append(Finding(
            WARN, "Electrothermal iteration did not converge",
            "Temperature-dependent resistance may be off.", None,
            "electrothermal not converged", "thermal"))
    return out


def _msg_finding(severity: str, msg: str, short: str) -> Finding:
    """Split a loader message into a headline (first clause) and detail."""
    text = str(msg).strip()
    m = re.search(r"(\s[—–-]\s|\.\s)", text)
    if m and m.start() <= 160:
        title, detail = text[:m.start()], text[m.end():]
        detail = detail[:1].upper() + detail[1:]
    elif len(text) > 160:
        title, detail = text[:157].rstrip() + "…", text
    else:
        title, detail = text, ""
    return Finding(severity, title, detail, None, short, short.replace(" ", "-"))


def _fmt_hz(f: float | None) -> str:
    if not f:
        return "—"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale:
            return f"{f / scale:.3g} {unit}"
    return f"{f:.3g} Hz"
