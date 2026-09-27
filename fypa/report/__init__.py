"""Design report: an HTML or PDF record of a PDN assessment.

:func:`build_report` gathers the numbers into a :class:`Report`;
:func:`write_report` renders it. The viewer's File > Export > Report… and the
``FYPA report`` CLI command are both thin wrappers over these.
"""
from __future__ import annotations

from pathlib import Path

from fypa.report.collect import build_report
from fypa.report.model import Report, ReportSettings, report_to_json

__all__ = ["Report", "ReportSettings", "build_report", "report_to_json",
           "write_report"]


def write_report(report: Report, path: str | Path, fmt: str | None = None,
                 *, json_sidecar: bool = False) -> list[Path]:
    """Render ``report`` to ``path`` as ``"html"`` or ``"pdf"`` (from the
    suffix when ``fmt`` is omitted). With ``json_sidecar`` the numbers are
    also written next to it as ``<name>.json``. Returns the files written."""
    path = Path(path)
    fmt = (fmt or path.suffix.lstrip(".") or "html").lower()
    if fmt == "html":
        from fypa.report.html import render_html
        path.write_text(render_html(report), encoding="utf-8")
    elif fmt == "pdf":
        from fypa.report.pdf import render_pdf
        path.write_bytes(render_pdf(report))
    else:
        raise ValueError(f"Unknown report format {fmt!r} (use html or pdf)")
    written = [path]
    if json_sidecar:
        jpath = path.with_suffix(".json")
        jpath.write_text(report_to_json(report), encoding="utf-8")
        written.append(jpath)
    return written
