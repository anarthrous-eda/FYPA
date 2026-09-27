"""Data model for the design report.

Everything a renderer needs is on :class:`Report`; the HTML and PDF renderers
only format it, and :func:`report_to_json` dumps it (minus the figures) so two
board revisions can be compared or a CI job can fail on a regression.

Units are SI throughout (V, A, W, Ω, mm); renderers pick display prefixes.
"""
from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field

# Severity, most severe first. UNCHECKED is not a pass: it marks a result that
# was computed but had no limit to be compared against.
FAIL = "FAIL"
WARN = "WARN"
UNCHECKED = "UNCHECKED"
PASS = "PASS"
SEVERITY_ORDER = (FAIL, WARN, UNCHECKED, PASS)

# Report sections, in the order each rail presents them. The key is what the
# export dialog and ``ReportSettings.sections`` use.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("findings", "Findings"),
    ("power_path", "Power path"),
    ("dc", "DC voltage at loads"),
    ("heatmaps", "Heatmaps"),
    ("layers", "Copper by layer"),
    ("vias", "Vias"),
    ("series", "Series elements & regulators"),
    ("decoupling", "Decoupling & PDN impedance"),
    ("thermal", "Thermal"),
)

# How much of each rail to print. "failing": rails with a FAIL or WARN in
# full, the rest as a one-paragraph summary; "all": every rail in full;
# "summary": the executive summary and appendix only.
DETAIL_LEVELS: tuple[tuple[str, str], ...] = (
    ("failing", "Full detail for rails with issues, passing rails condensed"),
    ("all", "Full detail for every rail"),
    ("summary", "Executive summary only"),
)


@dataclass
class ReportSettings:
    """User choices and pass/fail limits. Persisted per project under
    ``viewer_settings["report"]``."""
    via_limit_a: float = 1.0
    # A sink that meets PDN_MIN_V by less than this share of the rail's
    # nominal voltage is a warning.
    margin_warn_pct: float = 1.0
    # Optional: the most drop, as a share of nominal, any load may see.
    drop_budget_pct: float | None = None
    # Optional: current-density limit for copper (A per mm of width).
    j_limit_a_per_mm: float | None = None
    detail: str = "failing"
    sections: list[str] = field(
        default_factory=lambda: [k for k, _ in SECTIONS])
    include_appendix: bool = True
    include_topology: bool = True
    author: str = ""
    revision: str = ""
    top_vias: int = 5

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> ReportSettings:
        out = cls()
        for f in dataclasses.fields(cls):
            if data and f.name in data:
                setattr(out, f.name, data[f.name])
        valid = {k for k, _ in SECTIONS}
        out.sections = [s for s in (out.sections or []) if s in valid]
        if out.detail not in {k for k, _ in DETAIL_LEVELS}:
            out.detail = "failing"
        return out


@dataclass
class Finding:
    severity: str
    title: str
    detail: str = ""
    rail: str | None = None
    # Short phrase for the summary banner ("below minimum voltage").
    short: str = ""
    code: str = ""


@dataclass
class LoadResult:
    """A load on a rail: a SINK, or a regulator drawing its input."""
    label: str
    kind: str                     # "sink" | "regulator input"
    net: str
    pin_count: int
    current_a: float | None
    v_load: float | None          # worst P pin minus worst N pin
    drop_v: float | None          # setpoint - v_load
    return_drop_v: float | None   # share of drop_v lost in the return path
    min_v: float | None
    margin_v: float | None
    status: str                   # PASS | FAIL | WARN | UNCHECKED
    worst_pin: str = ""
    worst_layer: str = ""
    worst_xy: tuple[float, float] | None = None


@dataclass
class LayerResult:
    layer: str
    nets: list[str]
    copper: str                   # "1 oz"
    drop_across_v: float          # max V - min V on this layer's rail copper
    peak_j_a_per_mm: float
    peak_j_xy: tuple[float, float] | None
    loss_w: float
    share: float                  # of the rail's total copper loss


@dataclass
class ViaResult:
    x_mm: float
    y_mm: float
    net: str
    span: str
    drill_mm: float | None
    fill: str
    current_a: float
    delta_v: float
    power_w: float
    over_limit: bool


@dataclass
class SeriesResult:
    designator: str
    kind: str
    resistance_ohm: float
    nets: str
    current_a: float | None
    delta_v: float | None
    loss_w: float | None


@dataclass
class RegulatorResult:
    designator: str
    kind: str                     # "LDO" / "SMPS (buck)" / "Regulator"
    input_rail: str | None
    output_rail: str | None
    v_out_set: float
    v_in: float | None            # measured across IN_P / IN_N
    i_out: float | None
    i_in: float | None            # estimated: gain * i_out + Iq
    loss_w: float | None
    gain: float | None


@dataclass
class DecouplingResult:
    """PDN impedance and capacitor summary for one rail.

    ``analysed`` False means the analysis never ran for this rail; ``reason``
    says why. The figure is a PNG of |Z(f)| against the target.
    """
    analysed: bool
    reason: str = ""
    # True when the capacitor analysis ran but found nothing usable for this
    # rail — worth an UNCHECKED finding. False when it never ran at all.
    flag_missing: bool = False
    target_ohm: float | None = None
    f_max_hz: float | None = None
    meets_target: bool | None = None
    worst_z_ohm: float | None = None
    worst_f_hz: float | None = None
    reached_hz: float | None = None
    n_caps: int = 0
    n_modelled: int = 0
    n_under_1nh: int = 0
    parallel_l_nh: float | None = None
    tier_note: str = ""
    flagged: list[tuple[str, str, float | None]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # Appendix rows: designator, C (F), package, L best (nH), flags.
    cap_rows: list[dict] = field(default_factory=list)
    z_png: bytes | None = None


@dataclass
class RailReport:
    name: str
    kind: str                     # "supply" | "return"
    members: list[str]
    nominal_v: float | None
    source_label: str             # "U4 (SMPS buck, from 5V0)"
    load_a: float | None
    loads: list[LoadResult] = field(default_factory=list)
    lowest_v: float | None = None
    worst_drop_v: float | None = None
    worst_drop_pct: float | None = None
    min_margin_v: float | None = None
    ground_rise_v: float | None = None   # return rails only
    layers: list[LayerResult] = field(default_factory=list)
    loss_w: float | None = None
    peak_j_a_per_mm: float | None = None
    worst_point: tuple[str, float, float] | None = None  # layer, x, y
    vias_total: int = 0
    vias: list[ViaResult] = field(default_factory=list)   # top N by current
    vias_over: int = 0
    max_via_a: float | None = None
    series: list[SeriesResult] = field(default_factory=list)
    regulators: list[RegulatorResult] = field(default_factory=list)
    decoupling: DecouplingResult | None = None
    thermal_note: str = ""
    max_temp_rise_c: float | None = None
    findings: list[Finding] = field(default_factory=list)
    status: str = PASS
    # PNGs keyed "power_path", "drop_map", "j_map"; captions alongside.
    figures: dict[str, bytes] = field(default_factory=dict)
    captions: dict[str, str] = field(default_factory=dict)
    # Full appendix listing: every load pin.
    pin_rows: list[dict] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return "rail-" + "".join(
            c if c.isalnum() else "-" for c in self.name).strip("-").lower()

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)


@dataclass
class Report:
    project_name: str
    prjpcb_path: str
    pcbdoc_path: str
    generated: str                # ISO local time, minutes
    fypa_version: str
    settings: ReportSettings
    rails: list[RailReport]
    design_findings: list[Finding]
    solve_stale: bool = False
    source_kind: str = "altium"
    analyses: str = ""            # "DC IR drop, via current, …"
    # Appendix
    setup_rows: list[tuple[str, str]] = field(default_factory=list)
    stackup_rows: list[dict] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)  # level, text
    topology_svg: str | None = None
    topology_png: bytes | None = None

    @property
    def all_findings(self) -> list[Finding]:
        out = list(self.design_findings)
        for r in self.rails:
            out.extend(r.findings)
        order = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        return sorted(out, key=lambda f: order.get(f.severity, 9))

    def count(self, severity: str) -> int:
        return sum(1 for f in self.all_findings if f.severity == severity)

    def verdict(self) -> tuple[str, str, str]:
        """``(severity, headline, explanation)`` for the summary banner."""
        n = len(self.rails)
        failed = [r for r in self.rails if r.status == FAIL]
        warned = [r for r in self.rails if r.status == WARN]
        design_fail = [f for f in self.design_findings if f.severity == FAIL]
        unchecked = self.count(UNCHECKED)
        rails_word = "rail" if n == 1 else "rails"
        if failed or design_fail:
            if failed:
                head = f"{len(failed)} of {n} {rails_word} failed"
            else:
                head = (f"{len(design_fail)} design check"
                        f"{'s' if len(design_fail) != 1 else ''} failed")
            parts = []
            for r in failed[:3]:
                shorts = _unique(f.short or f.title for f in r.findings
                                 if f.severity == FAIL)
                parts.append(f"{r.name}: {', '.join(shorts)}.")
            if len(failed) > 3:
                parts.append(f"And {len(failed) - 3} more rails.")
            for f in design_fail[:2]:
                parts.append(f"{f.short or f.title}.")
            return FAIL, head, " ".join(parts)
        if warned or self.count(WARN):
            head = (f"{n} of {n} {rails_word} passed, "
                    f"{len(warned)} with warnings" if warned
                    else f"All {n} {rails_word} passed, with design warnings")
            shorts = _unique(f.short or f.title for f in self.all_findings
                             if f.severity == WARN)
            return WARN, head, "Warnings: " + "; ".join(shorts[:4]) + "."
        if unchecked:
            return (UNCHECKED,
                    f"{n} {rails_word} assessed, {unchecked} result"
                    f"{'s' if unchecked != 1 else ''} not checked against a "
                    "limit",
                    "Nothing failed, but some results had no limit to be "
                    "checked against. See the grey items below.")
        return PASS, f"All {n} {rails_word} passed", \
            "Every checked result is within its limit."


def _unique(items) -> list[str]:
    seen: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.append(it)
    return seen


def worst_severity(severities) -> str:
    order = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    best = PASS
    for s in severities:
        if order.get(s, 9) < order[best]:
            best = s
    return best


def report_to_json(report: Report) -> str:
    """The report's numbers as JSON — figures and the topology SVG left out."""
    def strip(obj):
        if isinstance(obj, bytes):
            return None
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items()
                    if k not in ("figures", "z_png", "topology_svg",
                                 "topology_png")}
        if isinstance(obj, (list, tuple)):
            return [strip(v) for v in obj]
        return obj

    data = strip(dataclasses.asdict(report))
    sev, head, why = report.verdict()
    data["verdict"] = {"severity": sev, "headline": head, "explanation": why}
    data["format_version"] = 1
    return json.dumps(data, indent=2, ensure_ascii=False)
