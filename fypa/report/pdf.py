"""Render a :class:`Report` as a PDF with reportlab.

Same content and order as the HTML report, laid out for A4 paper: running
header and page numbers, table headers repeated across page breaks, each
fully-detailed rail starting on a new page. Fonts are the DejaVu Sans files
matplotlib already ships, which carry Ω, µ and the ✔ ✖ ▲ status symbols that
reportlab's built-in Helvetica lacks.
"""
from __future__ import annotations

import io
import logging
import os
import sys
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    CondPageBreak,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from fypa.report import html as H
from fypa.report.model import (
    DETAIL_LEVELS,
    FAIL,
    PASS,
    SECTIONS,
    UNCHECKED,
    WARN,
    Finding,
    RailReport,
    Report,
)

log = logging.getLogger(__name__)

INK = colors.HexColor("#1d1d1f")
MUTED = colors.HexColor("#5f6670")
BORDER = colors.HexColor("#dde1e6")
BG_ALT = colors.HexColor("#f6f7f9")
ACCENT = colors.HexColor("#1a5fbf")
STATUS = {
    FAIL: (colors.HexColor("#b3261e"), colors.HexColor("#fdecea")),
    WARN: (colors.HexColor("#8a5a00"), colors.HexColor("#fff4dc")),
    PASS: (colors.HexColor("#1e6b3a"), colors.HexColor("#e7f5ec")),
    UNCHECKED: (colors.HexColor("#5f6670"), colors.HexColor("#eef0f3")),
}
EDGE = {FAIL: colors.HexColor("#b3261e"), WARN: colors.HexColor("#e0a400"),
        PASS: colors.HexColor("#1e6b3a"), UNCHECKED: MUTED}
GLYPH = {FAIL: "✖", WARN: "▲", PASS: "✔", UNCHECKED: "–"}

PAGE_W, PAGE_H = A4
MARGIN = 16 * mm
FRAME_W = PAGE_W - 2 * MARGIN

_FONT = "FypaSans"
_FONT_BOLD = "FypaSans-Bold"


def _register_fonts() -> None:
    if _FONT in pdfmetrics.getRegisteredFontNames():
        return
    import matplotlib
    d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    pdfmetrics.registerFont(TTFont(_FONT, os.path.join(d, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(
        TTFont(_FONT_BOLD, os.path.join(d, "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(_FONT, normal=_FONT, bold=_FONT_BOLD,
                                  italic=_FONT, boldItalic=_FONT_BOLD)


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=_FONT, fontSize=8.5, leading=11.5,
                          textColor=INK)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.5,
                                leading=9.5),
        "muted": ParagraphStyle("muted", parent=base, fontSize=7.5,
                                leading=9.5, textColor=MUTED),
        "cell": ParagraphStyle("cell", parent=base, fontSize=7.5, leading=9.3),
        "cellr": ParagraphStyle("cellr", parent=base, fontSize=7.5,
                                leading=9.3, alignment=TA_RIGHT),
        "th": ParagraphStyle("th", parent=base, fontSize=7, leading=8.5,
                             textColor=MUTED, fontName=_FONT_BOLD),
        "thr": ParagraphStyle("thr", parent=base, fontSize=7, leading=8.5,
                              textColor=MUTED, fontName=_FONT_BOLD,
                              alignment=TA_RIGHT),
        "kicker": ParagraphStyle("kicker", parent=base, fontSize=7,
                                 textColor=MUTED),
        "h1": ParagraphStyle("h1", parent=base, fontName=_FONT_BOLD,
                             fontSize=20, leading=24, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base, fontName=_FONT_BOLD,
                             fontSize=13, leading=16, spaceBefore=12,
                             spaceAfter=6),
        "h3": ParagraphStyle("h3", parent=base, fontName=_FONT_BOLD,
                             fontSize=10, leading=13, spaceBefore=9,
                             spaceAfter=4),
        "big": ParagraphStyle("big", parent=base, fontName=_FONT_BOLD,
                              fontSize=13, leading=16),
        "kpi_l": ParagraphStyle("kpi_l", parent=base, fontSize=6.5,
                                leading=8, textColor=MUTED),
        "kpi_v": ParagraphStyle("kpi_v", parent=base, fontName=_FONT_BOLD,
                                fontSize=12, leading=15),
        "kpi_s": ParagraphStyle("kpi_s", parent=base, fontSize=6.5,
                                leading=8, textColor=MUTED),
    }


def e(x) -> str:
    return escape("" if x is None else str(x))


def _hex(c: colors.Color) -> str:
    return "#" + c.hexval()[2:]


def _status_text(status: str, text: str | None = None) -> str:
    fg, _bg = STATUS.get(status, STATUS[UNCHECKED])
    word = text or status
    return (f'<font color="{_hex(fg)}"><b>'
            f"{GLYPH.get(status, '')} {e(word)}</b></font>")


class _NumberedCanvas(rl_canvas.Canvas):
    """Canvas that knows the page count, for "Page n of N" footers."""

    def __init__(self, *args, report: Report | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []
        self._report = report

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total: int) -> None:
        r = self._report
        self.setFont(_FONT, 7)
        self.setFillColor(MUTED)
        if self._pageNumber > 1 and r is not None:
            self.drawString(MARGIN, PAGE_H - 10 * mm,
                            f"{r.project_name} · PDN assessment"
                            + (f" · Rev {r.settings.revision}"
                               if r.settings.revision else ""))
            self.drawRightString(PAGE_W - MARGIN, PAGE_H - 10 * mm,
                                 r.generated)
            self.setStrokeColor(BORDER)
            self.line(MARGIN, PAGE_H - 11.5 * mm, PAGE_W - MARGIN,
                      PAGE_H - 11.5 * mm)
        self.drawString(MARGIN, 9 * mm,
                        "FYPA is a design aid. Validate results against "
                        "measurement.")
        self.drawRightString(PAGE_W - MARGIN, 9 * mm,
                             f"Page {self._pageNumber} of {total}")


def render_pdf(report: Report) -> bytes:
    _register_fonts()
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"PDN Report — {report.project_name}", author=report.settings.author
        or "FYPA", subject="Power delivery network assessment")
    b = _Builder(report, st)
    story = b.build()
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(
        *a, report=report, **k))
    return buf.getvalue()


class _Builder:
    def __init__(self, report: Report, st: dict[str, ParagraphStyle]):
        self.r = report
        self.st = st

    def P(self, text: str, style: str = "base") -> Paragraph:
        return Paragraph(text, self.st[style])

    # --- generic table ------------------------------------------------------

    def table(self, header: list[str], rows: list[list], widths: list[float],
              right: frozenset[int] | set[int] = frozenset(),
              cell_styles=None,
              row_status: list[str | None] | None = None,
              compact: bool = False) -> Table:
        """A data table in the report's style. ``rows`` hold strings (mini
        markup) or flowables. ``cell_styles``: ``[(col, row, status), …]``
        for highlighted cells; ``row_status`` puts a coloured edge on rows."""
        th, thr = self.st["th"], self.st["thr"]
        if compact:
            th = ParagraphStyle("thc", parent=th, fontSize=6.2, leading=7.6)
            thr = ParagraphStyle("thcr", parent=thr, fontSize=6.2,
                                 leading=7.6)
        data = [[Paragraph(h, thr if i in right else th)
                 for i, h in enumerate(header)]]
        left_st, right_st = self.st["cell"], self.st["cellr"]
        if compact:
            left_st = ParagraphStyle("cc", parent=left_st, fontSize=6.8,
                                     leading=8.4)
            right_st = ParagraphStyle("ccr", parent=right_st, fontSize=6.8,
                                      leading=8.4)
        for row in rows:
            data.append([
                c if not isinstance(c, str)
                else Paragraph(_nowrap(c) if i in right else c,
                               right_st if i in right else left_st)
                for i, c in enumerate(row)])
        scale = FRAME_W / sum(widths)
        t = Table(data, colWidths=[w * scale for w in widths], repeatRows=1)
        cmds = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]
        for col, row, status in cell_styles or []:
            _fg, bg = STATUS[status]
            cmds.append(("BACKGROUND", (col, row + 1), (col, row + 1), bg))
        for i, status in enumerate(row_status or []):
            if status in (FAIL, WARN):
                cmds.append(("LINEBEFORE", (0, i + 1), (0, i + 1), 2.5,
                             EDGE[status]))
        t.setStyle(TableStyle(cmds))
        return t

    def kv_table(self, rows: list[tuple[str, str]], key_w: float = 38 * mm
                 ) -> Table:
        data = [[self.P(e(k), "cell"), self.P(e(v), "cell")] for k, v in rows]
        t = Table(data, colWidths=[key_w, FRAME_W - key_w])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
            ("TEXTCOLOR", (0, 0), (0, -1), MUTED),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        return t

    def image(self, png: bytes | None, max_w: float = FRAME_W,
              max_h: float = 120 * mm) -> Image | None:
        if not png:
            return None
        from reportlab.lib.utils import ImageReader
        iw, ih = ImageReader(io.BytesIO(png)).getSize()
        scale = min(max_w / iw, max_h / ih)
        return Image(io.BytesIO(png), width=iw * scale, height=ih * scale)

    def na(self, text: str) -> Table:
        t = Table([[self.P(e(text), "small")]], colWidths=[FRAME_W])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), STATUS[UNCHECKED][1]),
            ("TEXTCOLOR", (0, 0), (-1, -1), MUTED),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    def note(self, text: str) -> Paragraph:
        return self.P(e(text), "muted")

    # --- document --------------------------------------------------------------

    def build(self) -> list:
        r = self.r
        story: list = []
        story += self.header()
        story += self.summary()
        n = 2
        if r.topology_svg:
            png = _svg_to_png(r.topology_svg)
            if png:
                story.append(CondPageBreak(90 * mm))
                story.append(self.P(f"{n} · Power tree", "h2"))
                story.append(self.image(png, max_h=215 * mm))
                story.append(self.note(
                    "Sources, regulators, series parts and loads as "
                    "annotated. The same diagram as the Topology tab."))
                n += 1
        if r.settings.detail != "summary":
            for rail in r.rails:
                full = r.settings.detail == "all" or rail.status in (FAIL, WARN)
                story += self.rail(rail, n, full)
                n += 1
        if r.settings.include_appendix:
            story.append(PageBreak())
            story += self.appendix()
        story += self.signoff()
        return story

    def header(self) -> list:
        r = self.r
        rows = [("Project", r.prjpcb_path or r.project_name)]
        if r.pcbdoc_path:
            rows.append(("PCB", r.pcbdoc_path))
        rows += [("Generated", r.generated), ("FYPA", r.fypa_version)]
        if r.settings.revision:
            rows.append(("Revision", r.settings.revision))
        rows.append(("Prepared by", r.settings.author or "—"))
        sub = r.analyses[:1].upper() + r.analyses[1:] if r.analyses else ""
        out = [self.P("FYPA · POWER DELIVERY NETWORK ASSESSMENT", "kicker"),
               self.P(e(r.project_name), "h1"),
               self.P(e(sub), "muted"), Spacer(1, 4 * mm),
               self.kv_table(rows, 26 * mm)]
        return out

    def banner(self, status: str, head: str, why: str) -> Table:
        fg, bg = STATUS[status]
        head_p = Paragraph(
            f"{GLYPH[status]} {e(head)}",
            ParagraphStyle("b", parent=self.st["big"], textColor=fg))
        t = Table([[[head_p, self.P(e(why), "base")]]], colWidths=[FRAME_W])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("LINEBEFORE", (0, 0), (0, -1), 4, EDGE[status]),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return t

    def summary(self) -> list:
        r = self.r
        out: list = [self.P("1 · Executive summary", "h2")]
        if r.solve_stale:
            out.append(self.banner(
                WARN, "Results are out of date",
                "The design was edited after the last solve. Re-solve before "
                "relying on this report."))
            out.append(Spacer(1, 3 * mm))
        sev, head, why = r.verdict()
        counts = []
        for s, word in ((FAIL, "failure"), (WARN, "warning"),
                        (UNCHECKED, "unchecked")):
            c = r.count(s)
            if c:
                plural = "s" if c != 1 and word != "unchecked" else ""
                counts.append(f"{c} {word}{plural}")
        out.append(self.banner(sev, head, why + (
            "  [" + " · ".join(counts) + "]" if counts else "")))
        out.append(Spacer(1, 4 * mm))
        out.append(self.rail_table())
        out.append(Spacer(1, 1.5 * mm))
        out.append(self.note(
            "Rails with failures come first, then warnings, then by nominal "
            "voltage. Grey “no limit” and “not analysed” cells were not "
            "checked against anything, so they are not passes. Drop is "
            "measured from the setpoint and includes the return path."))
        findings = r.all_findings
        main = [f for f in findings if f.severity in (FAIL, WARN)]
        grey = [f for f in findings if f.severity == UNCHECKED]
        if main:
            out.append(self.P("Issues to resolve", "h3"))
            out.append(self.findings(main, link=True))
        if grey:
            out.append(self.P(f"Not checked against a limit ({len(grey)})",
                              "h3"))
            out.append(self.findings(grey, link=True))
        if not main and not grey:
            out.append(self.P("No issues found.", "muted"))
        return out

    def rail_table(self) -> Table:
        rows, cells, row_status = [], [], []
        for i, rail in enumerate(self.r.rails):
            codes = {f.code for f in rail.findings}
            if rail.kind == "return":
                row = [e(rail.name), "return", "—", "—",
                       f"rise {H.fmt_mv(rail.ground_rise_v)}", "—"]
            else:
                drop = H.fmt_mv(rail.worst_drop_v)
                if rail.worst_drop_pct is not None:
                    drop += f" · {rail.worst_drop_pct:.2g} %"
                margin = ("no limit" if rail.min_margin_v is None
                          else H.fmt_mv(rail.min_margin_v, signed=True))
                row = [e(rail.name), H.fmt_v(rail.nominal_v),
                       H.fmt_a(rail.load_a), H.fmt_v(rail.lowest_v), drop,
                       margin]
                if "below-min-v" in codes:
                    cells.append((5, i, FAIL))
                elif "low-margin" in codes:
                    cells.append((5, i, WARN))
                if "drop-budget" in codes:
                    cells.append((4, i, FAIL))
            dec = rail.decoupling
            if rail.kind == "return" or dec is None:
                imp = "—"
            elif not dec.analysed:
                imp = "Not analysed"
            elif dec.meets_target:
                imp = "Meets"
            else:
                imp = f"Misses @ {H.fmt_hz(dec.worst_f_hz)}"
                cells.append((8, i, FAIL))
            if rail.vias_over:
                cells.append((6, i, FAIL))
            if "current-density" in codes:
                cells.append((7, i, WARN))
            status = _status_text(rail.status)
            unchecked = rail.count(UNCHECKED)
            if unchecked:
                status += (f'<br/><font color="#5f6670">– {unchecked}'
                           ' unchecked</font>')
            row += [H.fmt_a(rail.max_via_a), H.fmt_j(rail.peak_j_a_per_mm),
                    e(imp), status]
            rows.append(row)
            row_status.append(rail.status)
        return self.table(
            ["Rail", "Nominal", "Load", "Lowest load V", "Worst drop",
             "Min margin", "Max via I", "Peak |J|", "Impedance", "Status"],
            rows, [25, 13, 10, 13, 27, 15, 15, 19, 17, 24],
            right={1, 2, 3, 4, 5, 6, 7}, cell_styles=cells,
            row_status=row_status, compact=True)

    def findings(self, findings: list[Finding], link: bool) -> Table:
        data = []
        for f in findings:
            where = ""
            if link:
                where = (f"{e(f.rail)} · " if f.rail else "Design · ")
            text = f"<b>{where}{e(f.title)}</b>"
            if f.detail:
                text += f'<br/><font color="#5f6670" size="7.5">{e(f.detail)}</font>'
            data.append([self.P(_status_text(f.severity), "small"),
                         self.P(text, "base")])
        t = Table(data, colWidths=[24 * mm, FRAME_W - 24 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    # --- rails -------------------------------------------------------------------

    def rail(self, rail: RailReport, n: int, full: bool) -> list:
        out: list = [PageBreak() if full else CondPageBreak(40 * mm)]
        edge = EDGE.get(rail.status, PASS)
        sub = [rail.source_label] if rail.kind == "supply" else ["Return path"]
        if len(rail.members) > 1:
            sub.append("nets " + ", ".join(rail.members))
        if rail.layers:
            sub.append("copper on " + ", ".join(lr.layer for lr in rail.layers))
        unchecked = rail.count(UNCHECKED)
        head = (f"{n} · {e(rail.name)}   " + _status_text(rail.status)
                + (f'  <font color="#5f6670" size="8">– {unchecked} '
                   "unchecked</font>" if unchecked else ""))
        t = Table([[self.P(head, "h2")], [self.P(e(" · ".join(sub)), "muted")]],
                  colWidths=[FRAME_W])
        t.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (-1, 0), 3, edge),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, 0), 0),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        ]))
        out.append(t)
        if not full:
            out.append(self.P(_strip_html(H._condensed(rail)), "base"))
            out.append(Spacer(1, 4 * mm))
            return out
        out.append(self.kpis(rail))
        sections = set(self.r.settings.sections)
        sub_n = 1
        for key, title in SECTIONS:
            if key not in sections:
                continue
            body = getattr(self, f"sec_{key}")(rail)
            if not body:
                continue
            heading = self.P(
                f'<font color="#5f6670">{n}.{sub_n}</font>  {e(title)}', "h3")
            out.append(KeepTogether([heading, body[0]]))
            out.extend(body[1:])
            sub_n += 1
        return out

    def kpis(self, rail: RailReport) -> Table:
        tiles = H.kpi_tiles(rail)
        cols = 3
        cells, cmds = [], []
        for i, (status, label, value, sub) in enumerate(tiles):
            fg, bg = STATUS[status] if status else (INK, BG_ALT)
            cells.append([
                self.P(e(label.upper()), "kpi_l"),
                Paragraph(e(value), ParagraphStyle(
                    "v", parent=self.st["kpi_v"], textColor=fg)),
                self.P(e(sub), "kpi_s")])
            r, c = divmod(i, cols)
            cmds.append(("BACKGROUND", (c, r), (c, r), bg))
        while len(cells) % cols:
            cells.append("")
        grid = [cells[i:i + cols] for i in range(0, len(cells), cols)]
        t = Table(grid, colWidths=[FRAME_W / cols] * cols)
        t.setStyle(TableStyle(cmds + [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEAFTER", (0, 0), (-2, -1), 3, colors.white),
            ("LINEBELOW", (0, 0), (-1, -2), 3, colors.white),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    def captioned(self, png: bytes | None, caption: str, max_w=FRAME_W,
                  max_h=110 * mm) -> Table | None:
        """An image and its caption as one unsplittable flowable. A table
        rather than a KeepTogether: reportlab can't measure a KeepTogether
        nested in another, and the section heading already wraps its body
        in one."""
        im = self.image(png, max_w, max_h)
        if im is None:
            return None
        t = Table([[im], [self.note(caption)]], colWidths=[max_w])
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
        ]))
        return t

    def figure(self, rail: RailReport, key: str, max_w=FRAME_W,
               max_h=110 * mm) -> list:
        t = self.captioned(rail.figures.get(key),
                           rail.captions.get(key, ""), max_w, max_h)
        return [t] if t is not None else []

    def sec_findings(self, rail: RailReport) -> list:
        if not rail.findings:
            return [self.P("No issues on this rail.", "muted")]
        return [self.findings(rail.findings, link=False)]

    def sec_power_path(self, rail: RailReport) -> list:
        return self.figure(rail, "power_path", max_h=90 * mm)

    def sec_dc(self, rail: RailReport) -> list:
        if rail.kind != "supply":
            return []
        if not rail.loads:
            return [self.na("No loads on this rail.")]
        rows, cells, row_status = [], [], []
        for i, ld in enumerate(rail.loads):
            label = e(ld.label) + ("" if ld.kind == "sink" else
                                   ' <font color="#5f6670">(regulator input)</font>')
            margin = ("no limit" if ld.margin_v is None
                      else H.fmt_mv(ld.margin_v, signed=True))
            rows.append([label, e(ld.net), str(ld.pin_count),
                         H.fmt_a(ld.current_a), H.fmt_v(ld.v_load),
                         H.fmt_mv(ld.drop_v), H.fmt_mv(ld.return_drop_v),
                         H.fmt_v(ld.min_v), margin, _status_text(ld.status)])
            if ld.status in (FAIL, WARN) and ld.margin_v is not None:
                cells.append((8, i, ld.status))
            row_status.append(ld.status)
        return [
            self.table(["Load", "Net", "Pins", "Current", "V at load", "Drop",
                        "in return", "Min V", "Margin", "Status"], rows,
                       [22, 20, 7, 11, 12, 11, 11, 11, 12, 15],
                       right={2, 3, 4, 5, 6, 7, 8}, cell_styles=cells,
                       row_status=row_status),
            self.note(f"V at load is the lowest supply pin minus the highest "
                      f"return pin. Drop is measured from the "
                      f"{H.fmt_v(rail.nominal_v)} setpoint."),
        ]

    def sec_heatmaps(self, rail: RailReport) -> list:
        half = FRAME_W / 2 - 2 * mm
        figs = [self.captioned(rail.figures.get(k), rail.captions.get(k, ""),
                               half, 95 * mm) for k in ("drop_map", "j_map")]
        figs = [f for f in figs if f is not None]
        if not figs:
            return []
        t = Table([figs], colWidths=[FRAME_W / 2] * len(figs))
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
        ]))
        return [t]

    def sec_layers(self, rail: RailReport) -> list:
        if not rail.layers:
            return [self.na("No solved copper on this rail.")]
        rows = [[e(lr.layer), e(", ".join(lr.nets)), e(lr.copper),
                 H.fmt_mv(lr.drop_across_v), H.fmt_j(lr.peak_j_a_per_mm),
                 H.fmt_xy(lr.peak_j_xy), H.fmt_w(lr.loss_w), f"{lr.share:.0%}"]
                for lr in rail.layers]
        return [self.table(["Layer", "Nets", "Copper", "V spread", "Peak |J|",
                            "at (mm)", "Loss", "Share"], rows,
                           [20, 24, 10, 12, 13, 17, 11, 9],
                           right={3, 4, 5, 6, 7}),
                self.note("Peak |J| is the 99.9th percentile of the mesh, so "
                          "single-vertex pin singularities are ignored.")]

    def sec_vias(self, rail: RailReport) -> list:
        if not rail.vias:
            return [self.na("No vias carry this rail's current.")]
        rows, cells, row_status = [], [], []
        for i, v in enumerate(rail.vias):
            drill = "—" if v.drill_mm is None else f"{v.drill_mm:.2f} mm"
            rows.append([H.fmt_xy((v.x_mm, v.y_mm)), e(v.span),
                         f"{drill} · {e(v.fill)}", H.fmt_a(v.current_a),
                         H.fmt_mv(abs(v.delta_v)), H.fmt_w(v.power_w),
                         _status_text(FAIL if v.over_limit else PASS)])
            if v.over_limit:
                cells.append((3, i, FAIL))
            row_status.append(FAIL if v.over_limit else PASS)
        return [self.table(["Location (mm)", "Span", "Drill · fill", "|I|",
                            "ΔV", "Power", "Status"], rows,
                           [18, 26, 16, 10, 10, 10, 12], right={0, 3, 4, 5},
                           cell_styles=cells, row_status=row_status),
                self.note(f"Showing {len(rail.vias)} of {rail.vias_total} "
                          f"vias, highest current first. Limit "
                          f"{self.r.settings.via_limit_a:g} A per via.")]

    def sec_series(self, rail: RailReport) -> list:
        out = []
        if rail.series:
            out.append(self.table(
                ["Part", "Kind", "Nets", "R", "I", "ΔV", "Loss"],
                [[e(s.designator), e(s.kind), e(s.nets),
                  H.fmt_ohm(s.resistance_ohm), H.fmt_a(s.current_a),
                  H.fmt_mv(s.delta_v), H.fmt_w(s.loss_w)] for s in rail.series],
                [12, 12, 30, 10, 10, 10, 10], right={3, 4, 5, 6}))
        if rail.regulators:
            out.append(Spacer(1, 2 * mm))
            out.append(self.table(
                ["Regulator", "Kind", "Rails", "V out", "V in", "I out",
                 "I in (est.)", "Loss"],
                [[e(g.designator), e(g.kind),
                  f"{e(g.input_rail or '—')} → {e(g.output_rail)}",
                  H.fmt_v(g.v_out_set), H.fmt_v(g.v_in), H.fmt_a(g.i_out),
                  H.fmt_a(g.i_in), H.fmt_w(g.loss_w)] for g in rail.regulators],
                [12, 12, 22, 10, 10, 10, 11, 10], right={3, 4, 5, 6, 7}))
            out.append(self.note("V in is measured at the input pins. I in is "
                                 "estimated as gain × I out + quiescent "
                                 "current."))
        return out

    def sec_decoupling(self, rail: RailReport) -> list:
        dec = rail.decoupling
        if rail.kind != "supply" or dec is None:
            return []
        if not dec.analysed:
            return [self.na(f"Not analysed. {dec.reason}")]
        out = []
        fig = self.captioned(
            dec.z_png,
            f"|Z(f)| against the {H.fmt_ohm(dec.target_ohm)} target up to "
            f"{H.fmt_hz(dec.f_max_hz)}. The shaded band is where the target "
            "is breached.", FRAME_W * 0.8, 90 * mm)
        if fig is not None:
            out.append(fig)
        rows = [("Capacitors on the rail", str(dec.n_caps)),
                ("Modelled", str(dec.n_modelled)),
                ("Loop L below 1 nH", str(dec.n_under_1nh)),
                ("Flagged", str(len(dec.flagged))
                 + (f" ({', '.join(d for d, _w, _l in dec.flagged[:6])})"
                    if dec.flagged else "")),
                ("Left out", str(len(dec.skipped)))]
        if dec.parallel_l_nh is not None:
            rows.append(("Parallel loop L", f"{dec.parallel_l_nh:.3g} nH"))
        out.append(self.kv_table(rows, 45 * mm))
        if dec.tier_note:
            out.append(self.note(dec.tier_note))
        return out

    def sec_thermal(self, rail: RailReport) -> list:
        if rail.max_temp_rise_c is None:
            return [self.na(rail.thermal_note)]
        return [self.kv_table([("Peak temperature rise",
                                f"{rail.max_temp_rise_c:.3g} °C (99.9th "
                                "percentile)")]),
                self.note(rail.thermal_note)]

    # --- appendix -------------------------------------------------------------

    def appendix(self) -> list:
        r = self.r
        out: list = [self.P("A · Setup &amp; assumptions", "h2")]
        detail = dict(DETAIL_LEVELS).get(r.settings.detail, "")
        out.append(self.kv_table(list(r.setup_rows) + [("Report detail",
                                                        detail)]))
        if r.stackup_rows:
            out.append(self.P("Stackup", "h3"))
            out.append(self.table(
                ["Layer", "Copper", "Sheet R", "Dielectric below", "Plane"],
                [[e(s["name"]),
                  (H._num(s["copper_oz"], ".2g", " oz")
                   + f" · {s['copper_um']:.0f} µm"),
                  H._num(s["sheet_mohm_sq"], ".3g", " mΩ/□"),
                  H._num(s["dielectric_mm"] or None, ".3f", " mm"),
                  e(s["plane"])] for s in r.stackup_rows],
                [24, 18, 14, 16, 20], right={1, 2, 3}))
        for rail in r.rails:
            if not rail.pin_rows:
                continue
            out.append(self.P(f"Load pins · {e(rail.name)}", "h3"))
            out.append(self.table(
                ["Load", "Pin", "Net", "Layer", "Location (mm)", "V at pin"],
                [[e(p["load"]), e(p["pad"]), e(p["net"]), e(p["layer"]),
                  (H.fmt_xy((p["x_mm"], p["y_mm"]))
                   if p["x_mm"] is not None else "—"),
                  H.fmt_v(p["v_load"])] for p in rail.pin_rows],
                [20, 14, 20, 16, 18, 12], right={4, 5}))
        for rail in r.rails:
            dec = rail.decoupling
            if not (dec and dec.cap_rows):
                continue
            out.append(self.P(f"Capacitors · {e(rail.name)}", "h3"))
            out.append(self.table(
                ["Part", "C", "Package", "Loop L", "From", "Flags"],
                [[e(c.get("designator")), H._fmt_cap(c.get("capacitance_f")),
                  e(c.get("package") or "—"), H._num(c.get("l_nh"), ".3g",
                                                     " nH"),
                  e(c.get("tier") or ""), e(", ".join(c.get("flags") or []))]
                 for c in dec.cap_rows],
                [14, 12, 14, 12, 12, 30], right={1, 3}))
        if r.messages:
            out.append(self.P("Warnings and errors from the Messages log",
                              "h3"))
            out.append(self.table(["Level", "Message"],
                                  [[e(lv), e(t)] for lv, t in r.messages],
                                  [14, 86]))
        return out

    def signoff(self) -> list:
        cells = [[self.P(t, "muted") for t in
                  ("Prepared by / date", "Reviewed by / date",
                   "Approved by / date")]]
        t = Table(cells, colWidths=[FRAME_W / 3] * 3, rowHeights=[16 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.8, INK),
            ("LINEAFTER", (0, 0), (-2, -1), 8, colors.white),
        ]))
        return [CondPageBreak(45 * mm), self.P("Review", "h2"), t]


def _nowrap(text: str) -> str:
    """Keep a number and its unit on one line (plain text only)."""
    return text if "<" in text else text.replace(" ", " ")


def _strip_html(text: str) -> str:
    """The condensed-rail paragraph is HTML with only <strong>; map it to
    reportlab's mini-markup."""
    return (text.replace("<strong>", "<b>").replace("</strong>", "</b>")
            .replace("&#x27;", "'").replace("&quot;", '"'))


# A QGuiApplication the CLI creates for SVG text rendering, kept alive here.
_QT_APP: list = []


def _svg_to_png(svg: str) -> bytes | None:
    """Rasterise the topology SVG with QtSvg (reportlab can't draw SVG).
    Needs a QGuiApplication for text; the CLI gets an offscreen one."""
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import (
            QFont,
            QFontDatabase,
            QGuiApplication,
            QImage,
            QPainter,
        )
        from PySide6.QtSvg import QSvgRenderer
        if QGuiApplication.instance() is None:
            # Windows' own platform plugin runs windowless and has the system
            # fonts; elsewhere a headless CLI run needs the offscreen one.
            if sys.platform != "win32":
                os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            _QT_APP.append(QGuiApplication([]))  # keep alive
        if "Segoe UI" not in QFontDatabase.families():
            # No system fonts (offscreen platform): the diagram's text would
            # draw as empty boxes. Fall back to matplotlib's DejaVu files.
            import matplotlib
            d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
            for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
                         "DejaVuSansMono.ttf"):
                QFontDatabase.addApplicationFont(os.path.join(d, name))
            QGuiApplication.setFont(QFont("DejaVu Sans"))
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not renderer.isValid():
            return None
        size = renderer.defaultSize()
        scale = 2.0
        image = QImage(int(size.width() * scale), int(size.height() * scale),
                       QImage.Format.Format_ARGB32)
        image.fill(0xFFFFFFFF)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        ba = QByteArray()
        qbuf = QBuffer(ba)
        qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
        # PySide6's stubs miss the (QIODevice, str) overload.
        image.save(qbuf, "PNG")  # ty: ignore[no-matching-overload]
        return bytes(ba.data())
    except Exception:
        log.warning("Couldn't rasterise the topology diagram for the PDF",
                    exc_info=True)
        return None
