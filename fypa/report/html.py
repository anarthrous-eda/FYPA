"""Render a :class:`Report` as one self-contained HTML file.

Images are embedded as base64 PNGs and the topology diagram as inline SVG, so
the file can be mailed or archived on its own. Print styles start each rail
on a new page, so printing from a browser gives a usable paper copy too.

Status is always shown as a symbol plus a word, never colour alone, so the
report still reads in black and white.
"""
from __future__ import annotations

import base64
import html
import math

from fypa.report.model import (
    DETAIL_LEVELS,
    FAIL,
    PASS,
    SECTIONS,
    UNCHECKED,
    WARN,
    Finding,
    LoadResult,
    RailReport,
    Report,
)

_CHIP_CLASS = {FAIL: "fail", WARN: "warn", PASS: "pass", UNCHECKED: "na"}
_CHIP_WORD = {FAIL: "FAIL", WARN: "WARN", PASS: "PASS", UNCHECKED: "UNCHECKED"}


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


# --- number formatting ----------------------------------------------------------

def fmt_v(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f} V"


def fmt_mv(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    s = f"{v * 1e3:+.3g}" if signed else f"{v * 1e3:.3g}"
    return s.replace("-", "−") + " mV"


def fmt_a(a: float | None) -> str:
    if a is None:
        return "—"
    return f"{a * 1e3:.3g} mA" if abs(a) < 0.1 else f"{a:.3g} A"


def fmt_w(w: float | None) -> str:
    if w is None:
        return "—"
    return f"{w * 1e3:.3g} mW" if abs(w) < 1.0 else f"{w:.3g} W"


def fmt_ohm(r: float | None) -> str:
    if r is None:
        return "—"
    if r < 1.0:
        return f"{r * 1e3:.3g} mΩ"
    return f"{r:.3g} Ω"


def fmt_hz(f: float | None) -> str:
    if not f:
        return "—"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale:
            return f"{f / scale:.3g} {unit}"
    return f"{f:.3g} Hz"


def fmt_xy(xy) -> str:
    return "—" if not xy else f"({xy[0]:.2f}, {xy[1]:.2f})"


def fmt_j(j: float | None) -> str:
    return "—" if j is None else f"{j:.3g} A/mm"


def chip(status: str, text: str | None = None) -> str:
    return (f'<span class="chip {_CHIP_CLASS.get(status, "na")}">'
            f'{esc(text or _CHIP_WORD.get(status, status))}</span>')


def img(png: bytes | None, alt: str) -> str:
    if not png:
        return ""
    b64 = base64.b64encode(png).decode("ascii")
    return f'<img src="data:image/png;base64,{b64}" alt="{esc(alt)}">'


# --- page ------------------------------------------------------------------------

_CSS = """
:root{--bg:#fff;--bg-alt:#f6f7f9;--fg:#1d1d1f;--muted:#5f6670;--border:#dde1e6;
--accent:#1a5fbf;--fail:#b3261e;--fail-bg:#fdecea;--warn:#8a5a00;--warn-bg:#fff4dc;
--warn-edge:#e0a400;--pass:#1e6b3a;--pass-bg:#e7f5ec;--na:#5f6670;--na-bg:#eef0f3}
*{box-sizing:border-box}
body{margin:0;background:#e9ecf0;color:var(--fg);
font:14px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
.page{max-width:1000px;margin:24px auto;background:var(--bg);padding:40px 48px;
box-shadow:0 1px 4px rgba(0,0,0,.08)}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:19px;margin:36px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--fg)}
h3{font-size:15px;margin:22px 0 8px}
h3 .n{color:var(--muted);font-weight:500;margin-right:6px}
.muted{color:var(--muted)}
td.num,th.num,.num{font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.hdr{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;
border-bottom:1px solid var(--border);padding-bottom:16px}
.kicker{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.meta{display:grid;grid-template-columns:auto auto;gap:2px 14px;font-size:13px;margin:0}
.meta dt{color:var(--muted)}.meta dd{margin:0;overflow-wrap:anywhere}
.banner{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin:20px 0;
padding:14px 18px;border-radius:6px;border-left:6px solid}
.banner .big{font-size:20px;font-weight:700}
.banner.fail{border-color:var(--fail);background:var(--fail-bg)}.banner.fail .big{color:var(--fail)}
.banner.warn{border-color:var(--warn-edge);background:var(--warn-bg)}.banner.warn .big{color:var(--warn)}
.banner.pass{border-color:var(--pass);background:var(--pass-bg)}.banner.pass .big{color:var(--pass)}
.banner.na{border-color:var(--na);background:var(--na-bg)}.banner.na .big{color:var(--na)}
.counts{display:flex;gap:10px;margin-left:auto;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;
font-size:12px;font-weight:600;white-space:nowrap}
.chip.fail{color:var(--fail);background:var(--fail-bg)}
.chip.warn{color:var(--warn);background:var(--warn-bg)}
.chip.pass{color:var(--pass);background:var(--pass-bg)}
.chip.na{color:var(--na);background:var(--na-bg)}
.chip.fail::before{content:"\\2716"}.chip.warn::before{content:"\\25B2"}
.chip.pass::before{content:"\\2714"}.chip.na::before{content:"\\2013"}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:12px;
border-bottom:1px solid var(--fg);padding:6px 8px;vertical-align:bottom}
td{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top}
td.num,th.num{text-align:right;white-space:nowrap}
td .chip+.chip{margin-top:3px}
td.bad{color:var(--fail);font-weight:600;background:var(--fail-bg)}
td.caution{color:var(--warn);font-weight:600;background:var(--warn-bg)}
td.nolimit{color:var(--na)}
tr.row-fail td:first-child{box-shadow:inset 4px 0 0 var(--fail)}
tr.row-warn td:first-child{box-shadow:inset 4px 0 0 var(--warn-edge)}
.scroll{overflow-x:auto}
.findings{list-style:none;margin:0;padding:0}
.findings li{display:grid;grid-template-columns:104px 1fr;gap:10px;padding:8px 0;
border-bottom:1px solid var(--border)}
.findings .what{font-weight:600}.findings .why{color:var(--muted);font-size:13px}
.rail{margin-top:40px;border-top:4px solid var(--fg);padding-top:4px}
.rail.fail{border-top-color:var(--fail)}.rail.warn{border-top-color:var(--warn-edge)}
.rail.pass{border-top-color:var(--pass)}
.rail-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.rail-head h2{border:0;margin:10px 0 4px;padding:0;font-size:22px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.kpi{background:var(--bg-alt);border-radius:6px;padding:10px 12px}
.kpi .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.kpi .v{font-size:19px;font-weight:600}.kpi .s{font-size:12px;color:var(--muted)}
.kpi.fail{background:var(--fail-bg)}.kpi.fail .v{color:var(--fail)}
.kpi.warn{background:var(--warn-bg)}.kpi.warn .v{color:var(--warn)}
.figs{display:grid;grid-template-columns:1fr 1fr;gap:14px}
figure{margin:0;border:1px solid var(--border);border-radius:6px;padding:8px;background:#fff}
figure img,figure svg{display:block;max-width:100%;height:auto;margin:0 auto}
figcaption{font-size:12px;color:var(--muted);margin-top:4px}
.na-line{padding:8px 12px;background:var(--na-bg);color:var(--na);border-radius:4px;font-size:13px}
.collapsed{padding:10px 14px;background:var(--bg-alt);border-radius:6px;font-size:13px}
.signoff{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:14px}
.signoff div{border-bottom:1px solid var(--fg);padding-top:28px;font-size:12px;color:var(--muted)}
.note{font-size:12px;color:var(--muted);margin-top:6px}
details>summary{cursor:pointer;color:var(--accent);font-size:13px;margin:8px 0}
.topo{overflow-x:auto;text-align:center}
@media (max-width:700px){.page{padding:20px 16px}.figs{grid-template-columns:1fr}
.signoff{grid-template-columns:1fr}.findings li{grid-template-columns:1fr}}
@media print{body{background:#fff}.page{box-shadow:none;margin:0;max-width:none;padding:0}
.rail{break-before:page}table,figure,.kpis{break-inside:avoid}thead{display:table-header-group}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def render_html(report: Report) -> str:
    out: list[str] = [
        "<!doctype html>", '<html lang="en">', "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>PDN Report — {esc(report.project_name)}</title>",
        f"<style>{_CSS}</style>", "</head>", "<body>", '<div class="page">',
    ]
    out.append(_header(report))
    out.append(_summary(report))
    n = 2
    if report.topology_svg:
        out.append(f'<h2 id="power-tree">{n} · Power tree</h2>')
        out.append(f'<figure class="topo">{report.topology_svg}'
                   '<figcaption>Sources, regulators, series parts and loads as '
                   'annotated. The same diagram as the Topology tab.'
                   '</figcaption></figure>')
        n += 1
    if report.settings.detail != "summary":
        for rail in report.rails:
            full = (report.settings.detail == "all"
                    or rail.status in (FAIL, WARN))
            out.append(_rail(rail, n, report, full))
            n += 1
    if report.settings.include_appendix:
        out.append(_appendix(report))
    out.append(_signoff(report))
    out.append("</div></body></html>")
    return "\n".join(out)


def _header(r: Report) -> str:
    rows = [("Project", r.prjpcb_path or r.project_name)]
    if r.pcbdoc_path:
        rows.append(("PCB", r.pcbdoc_path))
    rows.append(("Generated", r.generated))
    rows.append(("FYPA", r.fypa_version))
    if r.settings.revision:
        rows.append(("Revision", r.settings.revision))
    rows.append(("Prepared by", r.settings.author or "—"))
    meta = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    sub = r.analyses[:1].upper() + r.analyses[1:] if r.analyses else ""
    rev = f"Rev {esc(r.settings.revision)} · " if r.settings.revision else ""
    return (f'<div class="hdr"><div><div class="kicker">FYPA · Power Delivery '
            f'Network Assessment</div><h1>{esc(r.project_name)}</h1>'
            f'<div class="muted">{rev}{esc(sub)}</div></div>'
            f'<dl class="meta">{meta}</dl></div>')


def _summary(r: Report) -> str:
    sev, head, why = r.verdict()
    cls = _CHIP_CLASS.get(sev, "na")
    glyph = {FAIL: "✖", WARN: "▲", PASS: "✔", UNCHECKED: "–"}[sev]
    counts = []
    for s, word in ((FAIL, "failure"), (WARN, "warning"),
                    (UNCHECKED, "unchecked")):
        c = r.count(s)
        if c:
            plural = "s" if c != 1 and word != "unchecked" else ""
            counts.append(chip(s, f"{c} {word}{plural}"))
    out = ['<h2 id="summary">1 · Executive summary</h2>']
    if r.solve_stale:
        out.append('<div class="banner warn"><div><div class="big">▲ Results '
                   'are out of date</div><div>The design was edited after the '
                   'last solve. Re-solve before relying on this report.</div>'
                   '</div></div>')
    out.append(f'<div class="banner {cls}"><div><div class="big">{glyph} '
               f'{esc(head)}</div><div>{esc(why)}</div></div>'
               f'<div class="counts">{"".join(counts)}</div></div>')
    out.append(_rail_table(r))
    out.append('<p class="note">Rails with failures come first, then warnings, '
               'then by nominal voltage. Grey “no limit” and “not analysed” '
               'cells were not checked against anything, so they are not '
               'passes. Drop is measured from the setpoint and includes the '
               'return path.</p>')
    out.append(_issue_list(r))
    return "\n".join(out)


def _rail_table(r: Report) -> str:
    rows = []
    for rail in r.rails:
        codes = {f.code for f in rail.findings}
        link = (f'<a href="#{rail.anchor}">{esc(rail.name)}</a>'
                if r.settings.detail != "summary" else esc(rail.name))
        tr_cls = {FAIL: "row-fail", WARN: "row-warn"}.get(rail.status, "")
        if rail.kind == "return":
            cells = [link, '<td class="num muted">return</td>',
                     '<td class="num">—</td>', '<td class="num">—</td>',
                     f'<td class="num">rise {fmt_mv(rail.ground_rise_v)}</td>',
                     '<td class="num">—</td>']
        else:
            margin_cls = ("bad" if "below-min-v" in codes else
                          "caution" if "low-margin" in codes else
                          "nolimit" if rail.min_margin_v is None else "")
            margin_txt = ("no limit" if rail.min_margin_v is None
                          else fmt_mv(rail.min_margin_v, signed=True))
            drop_txt = fmt_mv(rail.worst_drop_v)
            if rail.worst_drop_pct is not None:
                drop_txt += f" · {rail.worst_drop_pct:.2g} %"
            drop_cls = "bad" if "drop-budget" in codes else ""
            cells = [link, f'<td class="num">{fmt_v(rail.nominal_v)}</td>',
                     f'<td class="num">{fmt_a(rail.load_a)}</td>',
                     f'<td class="num">{fmt_v(rail.lowest_v)}</td>',
                     f'<td class="num {drop_cls}">{drop_txt}</td>',
                     f'<td class="num {margin_cls}">{margin_txt}</td>']
        via_cls = "bad" if rail.vias_over else ""
        j_cls = "caution" if "current-density" in codes else ""
        cells += [f'<td class="num {via_cls}">{fmt_a(rail.max_via_a)}</td>',
                  f'<td class="num {j_cls}">{fmt_j(rail.peak_j_a_per_mm)}</td>',
                  _impedance_cell(rail)]
        st = chip(rail.status)
        unchecked = rail.count(UNCHECKED)
        if unchecked:
            st += " " + chip(UNCHECKED, f"{unchecked} unchecked")
        cells.append(f"<td>{st}</td>")
        cells[0] = f"<td>{cells[0]}</td>"
        rows.append(f'<tr class="{tr_cls}">{"".join(cells)}</tr>')
    return ('<div class="scroll"><table><thead><tr><th>Rail</th>'
            '<th class="num">Nominal</th><th class="num">Load</th>'
            '<th class="num">Lowest load V</th><th class="num">Worst drop</th>'
            '<th class="num">Min margin</th><th class="num">Max via I</th>'
            '<th class="num">Peak |J|</th><th>Impedance</th><th>Status</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _impedance_cell(rail: RailReport) -> str:
    dec = rail.decoupling
    if rail.kind == "return" or dec is None:
        return '<td class="nolimit">—</td>'
    if not dec.analysed:
        return '<td class="nolimit">Not analysed</td>'
    if dec.meets_target:
        return "<td>Meets</td>"
    return f'<td class="bad">Misses @ {fmt_hz(dec.worst_f_hz)}</td>'


def _finding_li(f: Finding, link: bool = True) -> str:
    rail = ""
    if f.rail and link:
        anchor = RailReport(f.rail, "", [], None, "", None).anchor
        rail = f'<a href="#{anchor}">{esc(f.rail)}</a> · '
    elif f.rail is None and link:
        rail = "Design · "
    why = f'<div class="why">{esc(f.detail)}</div>' if f.detail else ""
    return (f'<li>{chip(f.severity)}<div><div class="what">{rail}'
            f'{esc(f.title)}</div>{why}</div></li>')


def _issue_list(r: Report) -> str:
    findings = r.all_findings
    main = [f for f in findings if f.severity in (FAIL, WARN)]
    grey = [f for f in findings if f.severity == UNCHECKED]
    out = []
    if main:
        out.append("<h3>Issues to resolve</h3>")
        out.append('<ul class="findings">'
                   + "".join(_finding_li(f) for f in main) + "</ul>")
    if grey:
        open_attr = " open" if len(grey) <= 5 else ""
        out.append(f"<details{open_attr}><summary>{len(grey)} result"
                   f"{'s' if len(grey) != 1 else ''} not checked against a "
                   "limit</summary>")
        out.append('<ul class="findings">'
                   + "".join(_finding_li(f) for f in grey) + "</ul></details>")
    if not main and not grey:
        out.append('<p class="muted">No issues found.</p>')
    return "\n".join(out)


# --- rail sections --------------------------------------------------------------

def _rail(rail: RailReport, n: int, r: Report, full: bool) -> str:
    cls = {FAIL: "fail", WARN: "warn"}.get(rail.status, "pass")
    layers = ", ".join(lr.layer for lr in rail.layers)
    members = ", ".join(rail.members)
    sub = [rail.source_label] if rail.kind == "supply" else ["Return path"]
    if len(rail.members) > 1:
        sub.append(f"nets {members}")
    if layers:
        sub.append(f"copper on {layers}")
    unchecked = rail.count(UNCHECKED)
    extra = (" " + chip(UNCHECKED, f"{unchecked} unchecked")) if unchecked else ""
    out = [f'<section class="rail {cls}" id="{rail.anchor}">',
           f'<div class="rail-head"><h2>{n} · {esc(rail.name)}</h2>'
           f'{chip(rail.status)}{extra}<span class="muted">'
           f'{esc(" · ".join(sub))}</span></div>']
    if not full:
        out.append(f'<div class="collapsed">{_condensed(rail)}</div>')
        out.append("</section>")
        return "\n".join(out)
    out.append(_kpis(rail))
    sections = set(r.settings.sections)
    sub_n = 1
    for key, title in SECTIONS:
        if key not in sections:
            continue
        body = _SECTION_RENDERERS[key](rail, r)
        if not body:
            continue
        out.append(f'<h3><span class="n">{n}.{sub_n}</span>{esc(title)}</h3>')
        out.append(body)
        sub_n += 1
    out.append("</section>")
    return "\n".join(out)


def _condensed(rail: RailReport) -> str:
    parts = []
    if rail.status == PASS:
        parts.append("All checks passed.")
    if rail.kind == "supply":
        lowest = lowest_load(rail)
        if lowest is not None:
            txt = (f"Lowest load {esc(lowest.label)} at "
                   f"{fmt_v(lowest.v_load)}")
            if lowest.margin_v is not None:
                txt += f" ({fmt_mv(lowest.margin_v, signed=True)} margin)"
            txt += (f", drop {fmt_mv(rail.worst_drop_v)}"
                    + (f" ({rail.worst_drop_pct:.2g} %)"
                       if rail.worst_drop_pct is not None else "") + ".")
            parts.append(txt)
    else:
        parts.append(f"Voltage rise across the return copper "
                     f"{fmt_mv(rail.ground_rise_v)}.")
    if rail.max_via_a is not None:
        parts.append(f"Max via current {fmt_a(rail.max_via_a)} "
                     f"({rail.vias_total} via"
                     f"{'s' if rail.vias_total != 1 else ''}).")
    dec = rail.decoupling
    if dec is not None and dec.analysed:
        parts.append("Impedance meets its "
                     f"{fmt_ohm(dec.target_ohm)} target." if dec.meets_target
                     else "Impedance misses its target.")
    grey = [f for f in rail.findings if f.severity == UNCHECKED]
    if grey:
        parts.append("<strong>Not checked:</strong> "
                     + esc("; ".join(f.title for f in grey)) + ".")
    return " ".join(parts)


def lowest_load(rail: RailReport) -> LoadResult | None:
    """The load with the lowest solved voltage, if any was solved."""
    best: LoadResult | None = None
    for ld in rail.loads:
        if ld.v_load is not None and (best is None or best.v_load is None
                                      or ld.v_load < best.v_load):
            best = ld
    return best


def kpi_tiles(rail: RailReport) -> list[tuple[str, str, str, str]]:
    """Headline numbers for a rail as ``(status, label, value, sub)``;
    ``status`` is FAIL / WARN to highlight a tile, else ``""``. Shared with
    the PDF renderer."""
    codes = {f.code for f in rail.findings}
    tiles: list[tuple[str, str, str, str]] = []
    if rail.kind == "supply":
        lowest = lowest_load(rail)
        if lowest is not None:
            sub = lowest.label + (f" · min {fmt_v(lowest.min_v)}"
                                  if lowest.min_v is not None else "")
            tiles.append((FAIL if "below-min-v" in codes else
                          WARN if "low-margin" in codes else "",
                          "Lowest load", fmt_v(lowest.v_load), sub))
        tiles.append((FAIL if "drop-budget" in codes else "", "Worst drop",
                      fmt_mv(rail.worst_drop_v),
                      f"{rail.worst_drop_pct:.2g} % of {fmt_v(rail.nominal_v)}"
                      if rail.worst_drop_pct is not None else ""))
        n_loads = len(rail.loads)
        tiles.append(("", "Load", fmt_a(rail.load_a),
                      f"{n_loads} load{'s' if n_loads != 1 else ''}"))
    else:
        tiles.append(("", "Return rise", fmt_mv(rail.ground_rise_v),
                      "across the return copper"))
    if rail.max_via_a is not None:
        tiles.append((FAIL if rail.vias_over else "", "Max via current",
                      fmt_a(rail.max_via_a),
                      f"{rail.vias_over} over the limit" if rail.vias_over
                      else f"{rail.vias_total} via"
                      f"{'s' if rail.vias_total != 1 else ''}"))
    if rail.loss_w is not None:
        top = sorted(rail.layers, key=lambda lr: -lr.loss_w)[:2]
        tiles.append(("", "Copper loss", fmt_w(rail.loss_w),
                      " · ".join(f"{lr.layer} {fmt_w(lr.loss_w)}"
                                 for lr in top)))
    dec = rail.decoupling
    if dec is not None and dec.analysed and dec.target_ohm is not None:
        tiles.append(("" if dec.meets_target else FAIL, "Impedance",
                      "Meets" if dec.meets_target else fmt_ohm(dec.worst_z_ohm),
                      f"target {fmt_ohm(dec.target_ohm)}"
                      + ("" if dec.meets_target
                         else f" · {fmt_hz(dec.worst_f_hz)}")))
    elif rail.peak_j_a_per_mm is not None:
        tiles.append((WARN if "current-density" in codes else "", "Peak |J|",
                      fmt_j(rail.peak_j_a_per_mm), "current density"))
    return tiles


def _kpis(rail: RailReport) -> str:
    return '<div class="kpis">' + "".join(
        f'<div class="kpi {_CHIP_CLASS.get(st, "") if st else ""}">'
        f'<div class="l">{esc(label)}</div><div class="v">{esc(v)}</div>'
        f'<div class="s">{esc(sub)}</div></div>'
        for st, label, v, sub in kpi_tiles(rail)) + "</div>"


def _sec_findings(rail: RailReport, r: Report) -> str:
    if not rail.findings:
        return '<p class="muted">No issues on this rail.</p>'
    return ('<ul class="findings">'
            + "".join(_finding_li(f, link=False) for f in rail.findings)
            + "</ul>")


def _figure(rail: RailReport, key: str, alt: str) -> str:
    png = rail.figures.get(key)
    if not png:
        return ""
    return (f"<figure>{img(png, alt)}<figcaption>"
            f"{esc(rail.captions.get(key, ''))}</figcaption></figure>")


def _sec_power_path(rail: RailReport, r: Report) -> str:
    return _figure(rail, "power_path", f"Power path for {rail.name}")


def _sec_dc(rail: RailReport, r: Report) -> str:
    if rail.kind != "supply":
        return ""
    if not rail.loads:
        return '<div class="na-line">No loads on this rail.</div>'
    rows = []
    for ld in rail.loads:
        tr = {FAIL: "row-fail", WARN: "row-warn"}.get(ld.status, "")
        m_cls = ("bad" if ld.status == FAIL and ld.margin_v is not None
                 and ld.margin_v < 0 else
                 "caution" if ld.status == WARN else
                 "nolimit" if ld.margin_v is None else "")
        kind = "" if ld.kind == "sink" else ' <span class="muted">(regulator input)</span>'
        rows.append(
            f'<tr class="{tr}"><td>{esc(ld.label)}{kind}</td><td>{esc(ld.net)}</td>'
            f'<td class="num">{ld.pin_count}</td>'
            f'<td class="num">{fmt_a(ld.current_a)}</td>'
            f'<td class="num">{fmt_v(ld.v_load)}</td>'
            f'<td class="num">{fmt_mv(ld.drop_v)}</td>'
            f'<td class="num">{fmt_mv(ld.return_drop_v)}</td>'
            f'<td class="num">{fmt_v(ld.min_v)}</td>'
            f'<td class="num {m_cls}">'
            f'{"no limit" if ld.margin_v is None else fmt_mv(ld.margin_v, True)}'
            f'</td><td>{chip(ld.status)}</td></tr>')
    return ('<div class="scroll"><table><thead><tr><th>Load</th><th>Net</th>'
            '<th class="num">Pins</th><th class="num">Current</th>'
            '<th class="num">V at load</th><th class="num">Drop</th>'
            '<th class="num">in return</th><th class="num">Min V</th>'
            '<th class="num">Margin</th><th>Status</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>"
            f'<p class="note">V at load is the lowest supply pin minus the '
            f'highest return pin. Drop is measured from the '
            f'{fmt_v(rail.nominal_v)} setpoint.'
            + (" Every pin is listed in the appendix."
               if r.settings.include_appendix else "") + "</p>")


def _sec_heatmaps(rail: RailReport, r: Report) -> str:
    figs = [_figure(rail, "drop_map", f"Voltage drop map for {rail.name}"),
            _figure(rail, "j_map", f"Current density map for {rail.name}")]
    figs = [f for f in figs if f]
    return f'<div class="figs">{"".join(figs)}</div>' if figs else ""


def _sec_layers(rail: RailReport, r: Report) -> str:
    if not rail.layers:
        return '<div class="na-line">No solved copper on this rail.</div>'
    rows = "".join(
        f'<tr><td>{esc(lr.layer)}</td><td>{esc(", ".join(lr.nets))}</td>'
        f'<td>{esc(lr.copper)}</td>'
        f'<td class="num">{fmt_mv(lr.drop_across_v)}</td>'
        f'<td class="num">{fmt_j(lr.peak_j_a_per_mm)}</td>'
        f'<td class="num">{fmt_xy(lr.peak_j_xy)}</td>'
        f'<td class="num">{fmt_w(lr.loss_w)}</td>'
        f'<td class="num">{lr.share:.0%}</td></tr>'
        for lr in rail.layers)
    return ('<div class="scroll"><table><thead><tr><th>Layer</th><th>Nets</th>'
            '<th>Copper</th><th class="num">V spread</th>'
            '<th class="num">Peak |J|</th><th class="num">at (mm)</th>'
            '<th class="num">Loss</th><th class="num">Share</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            '<p class="note">V spread is the highest minus the lowest voltage '
            'on the layer\'s rail copper. Peak |J| is the 99.9th percentile '
            'of the mesh, so single-vertex pin singularities are ignored.</p>')


def _sec_vias(rail: RailReport, r: Report) -> str:
    if not rail.vias:
        return '<div class="na-line">No vias carry this rail\'s current.</div>'
    rows = "".join(
        f'<tr class="{"row-fail" if v.over_limit else ""}">'
        f'<td class="num">{fmt_xy((v.x_mm, v.y_mm))}</td><td>{esc(v.span)}</td>'
        f'<td>{"—" if v.drill_mm is None else f"{v.drill_mm:.2f} mm"} · '
        f'{esc(v.fill)}</td>'
        f'<td class="num {"bad" if v.over_limit else ""}">{fmt_a(v.current_a)}</td>'
        f'<td class="num">{fmt_mv(abs(v.delta_v))}</td>'
        f'<td class="num">{fmt_w(v.power_w)}</td>'
        f'<td>{chip(FAIL if v.over_limit else PASS)}</td></tr>'
        for v in rail.vias)
    return ('<div class="scroll"><table><thead><tr><th class="num">Location (mm)</th>'
            '<th>Span</th><th>Drill · fill</th><th class="num">|I|</th>'
            '<th class="num">ΔV</th><th class="num">Power</th><th>Status</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>'
            f'<p class="note">Showing {len(rail.vias)} of {rail.vias_total} '
            f'vias, highest current first. Limit '
            f'{r.settings.via_limit_a:g} A per via.</p>')


def _sec_series(rail: RailReport, r: Report) -> str:
    if not rail.series and not rail.regulators:
        return ""
    out = []
    rows = []
    for s in rail.series:
        rows.append(f"<tr><td>{esc(s.designator)}</td><td>{esc(s.kind)}</td>"
                    f"<td>{esc(s.nets)}</td>"
                    f'<td class="num">{fmt_ohm(s.resistance_ohm)}</td>'
                    f'<td class="num">{fmt_a(s.current_a)}</td>'
                    f'<td class="num">{fmt_mv(s.delta_v)}</td>'
                    f'<td class="num">{fmt_w(s.loss_w)}</td></tr>')
    if rows:
        out.append('<div class="scroll"><table><thead><tr><th>Part</th>'
                   '<th>Kind</th><th>Nets</th><th class="num">R</th>'
                   '<th class="num">I</th><th class="num">ΔV</th>'
                   '<th class="num">Loss</th></tr></thead><tbody>'
                   + "".join(rows) + "</tbody></table></div>")
    rows = []
    for g in rail.regulators:
        rows.append(
            f"<tr><td>{esc(g.designator)}</td><td>{esc(g.kind)}</td>"
            f"<td>{esc(g.input_rail or '—')} → {esc(g.output_rail)}</td>"
            f'<td class="num">{fmt_v(g.v_out_set)}</td>'
            f'<td class="num">{fmt_v(g.v_in)}</td>'
            f'<td class="num">{fmt_a(g.i_out)}</td>'
            f'<td class="num">{fmt_a(g.i_in)}</td>'
            f'<td class="num">{fmt_w(g.loss_w)}</td></tr>')
    if rows:
        out.append('<div class="scroll" style="margin-top:10px"><table><thead>'
                   '<tr><th>Regulator</th><th>Kind</th><th>Rails</th>'
                   '<th class="num">V out</th><th class="num">V in</th>'
                   '<th class="num">I out</th><th class="num">I in (est.)</th>'
                   '<th class="num">Loss</th></tr></thead><tbody>'
                   + "".join(rows) + "</tbody></table></div>"
                   '<p class="note">V in is measured at the input pins. I in is '
                   'estimated as gain × I out + quiescent current.</p>')
    return "\n".join(out)


def _sec_decoupling(rail: RailReport, r: Report) -> str:
    dec = rail.decoupling
    if rail.kind != "supply" or dec is None:
        return ""
    if not dec.analysed:
        return f'<div class="na-line">Not analysed. {esc(dec.reason)}</div>'
    fig = ""
    if dec.z_png:
        cap = (f"|Z(f)| against the {fmt_ohm(dec.target_ohm)} target up to "
               f"{fmt_hz(dec.f_max_hz)}. The shaded band is where the target "
               "is breached.")
        fig = f"<figure>{img(dec.z_png, 'Impedance plot')}<figcaption>{esc(cap)}</figcaption></figure>"
    rows = [("Capacitors on the rail", str(dec.n_caps)),
            ("Modelled", str(dec.n_modelled)),
            ("Loop L below 1 nH", str(dec.n_under_1nh)),
            ("Flagged", str(len(dec.flagged))
             + (f" ({', '.join(d for d, _w, _l in dec.flagged[:4])})"
                if dec.flagged else "")),
            ("Left out", str(len(dec.skipped)))]
    if dec.parallel_l_nh is not None:
        rows.append(("Parallel loop L", f"{dec.parallel_l_nh:.3g} nH"))
    table = ("<table><tbody>" + "".join(
        f'<tr><td>{esc(k)}</td><td class="num '
        f'{"caution" if k == "Flagged" and dec.flagged else ""}">{esc(v)}</td></tr>'
        for k, v in rows) + "</tbody></table>")
    note = f'<p class="note">{esc(dec.tier_note)}</p>' if dec.tier_note else ""
    return f'<div class="figs">{fig}<div>{table}{note}</div></div>'


def _sec_thermal(rail: RailReport, r: Report) -> str:
    if rail.max_temp_rise_c is None:
        return f'<div class="na-line">{esc(rail.thermal_note)}</div>'
    return (f'<div class="kpis"><div class="kpi"><div class="l">Peak '
            f'temperature rise</div><div class="v">{rail.max_temp_rise_c:.3g} °C'
            f'</div><div class="s">99.9th percentile</div></div></div>'
            f'<p class="note">{esc(rail.thermal_note)}</p>')


_SECTION_RENDERERS = {
    "findings": _sec_findings,
    "power_path": _sec_power_path,
    "dc": _sec_dc,
    "heatmaps": _sec_heatmaps,
    "layers": _sec_layers,
    "vias": _sec_vias,
    "series": _sec_series,
    "decoupling": _sec_decoupling,
    "thermal": _sec_thermal,
}


# --- appendix -------------------------------------------------------------------

def _appendix(r: Report) -> str:
    out = ['<h2 id="appendix">A · Setup &amp; assumptions</h2>']
    detail = dict(DETAIL_LEVELS).get(r.settings.detail, "")
    rows = list(r.setup_rows) + [("Report detail", detail)]
    out.append("<table><tbody>" + "".join(
        f'<tr><td style="width:190px">{esc(k)}</td><td>{esc(v)}</td></tr>'
        for k, v in rows) + "</tbody></table>")
    if r.stackup_rows:
        out.append("<h3>Stackup</h3>")
        out.append('<div class="scroll"><table><thead><tr><th>Layer</th>'
                   '<th class="num">Copper</th><th class="num">Sheet R</th>'
                   '<th class="num">Dielectric below</th><th>Plane</th></tr>'
                   "</thead><tbody>" + "".join(
                       f"<tr><td>{esc(s['name'])}</td>"
                       f'<td class="num">{_num(s["copper_oz"], ".2g", " oz")}'
                       f' · {s["copper_um"]:.0f} µm</td>'
                       f'<td class="num">'
                       f'{_num(s["sheet_mohm_sq"], ".3g", " mΩ/□")}</td>'
                       f'<td class="num">'
                       f'{_num(s["dielectric_mm"] or None, ".3f", " mm")}</td>'
                       f"<td>{esc(s['plane'])}</td></tr>"
                       for s in r.stackup_rows) + "</tbody></table></div>")
    pin_rails = [rail for rail in r.rails if rail.pin_rows]
    if pin_rails:
        out.append("<h3>All load pins</h3>")
        for rail in pin_rails:
            body = "".join(
                f"<tr><td>{esc(p['load'])}</td><td>{esc(p['pad'])}</td>"
                f"<td>{esc(p['net'])}</td><td>{esc(p['layer'])}</td>"
                f'<td class="num">{fmt_xy((p["x_mm"], p["y_mm"])) if p["x_mm"] is not None else "—"}</td>'
                f'<td class="num">{fmt_v(p["v_load"])}</td></tr>'
                for p in rail.pin_rows)
            out.append(f"<details><summary>{esc(rail.name)} · "
                       f"{len(rail.pin_rows)} pins</summary>"
                       '<div class="scroll"><table><thead><tr><th>Load</th>'
                       '<th>Pin</th><th>Net</th><th>Layer</th>'
                       '<th class="num">Location (mm)</th>'
                       '<th class="num">V at pin</th></tr></thead><tbody>'
                       f"{body}</tbody></table></div></details>")
    cap_rails = [(rail, rail.decoupling) for rail in r.rails
                 if rail.decoupling and rail.decoupling.cap_rows]
    if cap_rails:
        out.append("<h3>Capacitors</h3>")
        for rail, dec in cap_rails:
            body = "".join(
                f"<tr><td>{esc(c.get('designator'))}</td>"
                f'<td class="num">{_fmt_cap(c.get("capacitance_f"))}</td>'
                f"<td>{esc(c.get('package') or '—')}</td>"
                f'<td class="num">{_num(c.get("l_nh"), ".3g", " nH")}</td>'
                f"<td>{esc(c.get('tier') or '')}</td>"
                f"<td>{esc(', '.join(c.get('flags') or []))}</td></tr>"
                for c in dec.cap_rows)
            out.append(f"<details><summary>{esc(rail.name)} · "
                       f"{len(dec.cap_rows)} capacitors</summary>"
                       '<div class="scroll"><table><thead><tr><th>Part</th>'
                       '<th class="num">C</th><th>Package</th>'
                       '<th class="num">Loop L</th><th>From</th><th>Flags</th>'
                       f"</tr></thead><tbody>{body}</tbody></table></div>"
                       "</details>")
    if r.messages:
        out.append("<h3>Warnings and errors from the Messages log</h3>")
        out.append("<table><tbody>" + "".join(
            f'<tr><td style="width:90px">{esc(level)}</td><td>{esc(text)}</td></tr>'
            for level, text in r.messages) + "</tbody></table>")
    return "\n".join(out)


def _num(v: float | None, spec: str, unit: str) -> str:
    return "—" if v is None else f"{v:{spec}}{unit}"


def _fmt_cap(c: float | None) -> str:
    if not c or not math.isfinite(c):
        return "—"
    for scale, unit in ((1e-6, "µF"), (1e-9, "nF"), (1e-12, "pF")):
        if c >= scale:
            return f"{c / scale:.3g} {unit}"
    return f"{c:.3g} F"


def _signoff(r: Report) -> str:
    return ('<h2>Review</h2><div class="signoff"><div>Prepared by / date</div>'
            '<div>Reviewed by / date</div><div>Approved by / date</div></div>'
            '<p class="note" style="margin-top:18px">FYPA is a design aid. '
            'Validate results against measurement.</p>')
