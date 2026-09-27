"""File > Export > Report… — the design report dialog and its export flow.

The report itself is built headless by :mod:`fypa.report`; this mixin adds
what only the viewer holds: the capacitor / impedance results, the Messages
log, whether the solve is stale, and the project file the choices are
remembered in (``viewer_settings["report"]``).
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from fypa.report.model import DETAIL_LEVELS, SECTIONS, ReportSettings
from fypa.viewer.session import _run_background_load
from fypa.viewer.theme import _T, _file_dialog_options
from fypa.viewer.widgets import _esc

log = logging.getLogger(__name__)

# Messages-log entries at or above this level go into the report appendix.
_MESSAGE_LEVEL = logging.WARNING
_MAX_MESSAGES = 200


class ReportDialog(QDialog):
    """Choose format, content and limits for one report export."""

    def __init__(self, parent: QWidget, settings: ReportSettings, fmt: str,
                 json_sidecar: bool, open_after: bool):
        super().__init__(parent)
        self.setWindowTitle("Generate Report")
        self.setWindowFlags(self.windowFlags()
                            & ~Qt.WindowContextHelpButtonHint)
        root = QVBoxLayout(self)

        fmt_box = QGroupBox("Format")
        fmt_row = QHBoxLayout(fmt_box)
        self.html_radio = QRadioButton("HTML")
        self.html_radio.setToolTip(
            "One self-contained file with links between the summary and the "
            "rail sections. Opens in any browser.")
        self.pdf_radio = QRadioButton("PDF")
        self.pdf_radio.setToolTip(
            "Paginated A4 document with page numbers, for design reviews "
            "and archiving.")
        (self.pdf_radio if fmt == "pdf" else self.html_radio).setChecked(True)
        self.json_check = QCheckBox("Also save the numbers as JSON")
        self.json_check.setToolTip(
            "Writes <name>.json next to the report, for comparing board "
            "revisions or checking results in a script.")
        self.json_check.setChecked(json_sidecar)
        fmt_row.addWidget(self.html_radio)
        fmt_row.addWidget(self.pdf_radio)
        fmt_row.addStretch(1)
        fmt_row.addWidget(self.json_check)
        root.addWidget(fmt_box)

        content = QGroupBox("Content")
        grid = QGridLayout(content)
        grid.addWidget(QLabel("Detail"), 0, 0)
        self.detail_combo = QComboBox()
        for key, label in DETAIL_LEVELS:
            self.detail_combo.addItem(label, key)
        self.detail_combo.setCurrentIndex(
            max(0, self.detail_combo.findData(settings.detail)))
        grid.addWidget(self.detail_combo, 0, 1, 1, 2)
        grid.addWidget(QLabel("Per-rail sections"), 1, 0, Qt.AlignTop)
        self.section_checks: dict[str, QCheckBox] = {}
        for i, (key, label) in enumerate(SECTIONS):
            cb = QCheckBox(label)
            cb.setChecked(key in settings.sections)
            self.section_checks[key] = cb
            grid.addWidget(cb, 1 + i // 2, 1 + i % 2)
        row = 1 + (len(SECTIONS) + 1) // 2
        self.topology_check = QCheckBox("Power tree diagram")
        self.topology_check.setChecked(settings.include_topology)
        self.appendix_check = QCheckBox(
            "Appendix (setup, stackup, every pin, capacitors, messages)")
        self.appendix_check.setChecked(settings.include_appendix)
        grid.addWidget(self.topology_check, row, 1)
        grid.addWidget(self.appendix_check, row + 1, 1, 1, 2)
        root.addWidget(content)

        limits = QGroupBox("Pass / fail limits")
        form = QFormLayout(limits)
        self.via_edit = QLineEdit(f"{settings.via_limit_a:g}")
        self.via_edit.setToolTip(
            "A via carrying more than this is a failure. Starts from the via "
            "warning current in the Settings tab.")
        form.addRow("Via current limit (A)", self.via_edit)
        self.margin_edit = QLineEdit(f"{settings.margin_warn_pct:g}")
        self.margin_edit.setToolTip(
            "A load that meets its PDN_MIN_V by less than this share of the "
            "rail's nominal voltage is a warning.")
        form.addRow("Warn below margin (% of nominal)", self.margin_edit)
        self.drop_edit = QLineEdit(_opt(settings.drop_budget_pct))
        self.drop_edit.setPlaceholderText("none")
        self.drop_edit.setToolTip(
            "Optional. Any load whose drop from the setpoint exceeds this "
            "share of nominal fails. Leave blank to check only PDN_MIN_V.")
        form.addRow("Drop budget (% of nominal)", self.drop_edit)
        self.j_edit = QLineEdit(_opt(settings.j_limit_a_per_mm))
        self.j_edit.setPlaceholderText("none")
        self.j_edit.setToolTip(
            "Optional. Copper whose peak current density exceeds this is a "
            "warning. Leave blank to report the peak without checking it.")
        form.addRow("Current density limit (A/mm)", self.j_edit)
        root.addWidget(limits)

        who = QGroupBox("Document")
        wform = QFormLayout(who)
        self.author_edit = QLineEdit(settings.author)
        self.revision_edit = QLineEdit(settings.revision)
        self.revision_edit.setPlaceholderText("e.g. B")
        wform.addRow("Prepared by", self.author_edit)
        wform.addRow("Board revision", self.revision_edit)
        self.open_check = QCheckBox("Open the report when it's saved")
        self.open_check.setChecked(open_after)
        wform.addRow("", self.open_check)
        root.addWidget(who)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok_btn = buttons.addButton("Generate…",
                                        QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.result_settings: ReportSettings | None = None

    def _on_accept(self) -> None:
        try:
            via = _parse(self.via_edit.text(), "Via current limit")
            margin = _parse(self.margin_edit.text(), "Margin warning")
            drop = _parse(self.drop_edit.text(), "Drop budget", optional=True)
            j = _parse(self.j_edit.text(), "Current density limit",
                       optional=True)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid value", str(e))
            return
        if via is None or via <= 0:
            QMessageBox.warning(self, "Invalid value",
                                "Via current limit must be above zero.")
            return
        self.result_settings = ReportSettings(
            via_limit_a=via, margin_warn_pct=margin or 0.0,
            drop_budget_pct=drop, j_limit_a_per_mm=j,
            detail=self.detail_combo.currentData(),
            sections=[k for k, cb in self.section_checks.items()
                      if cb.isChecked()],
            include_appendix=self.appendix_check.isChecked(),
            include_topology=self.topology_check.isChecked(),
            author=self.author_edit.text().strip(),
            revision=self.revision_edit.text().strip(),
        )
        self.accept()

    @property
    def fmt(self) -> str:
        return "pdf" if self.pdf_radio.isChecked() else "html"


def _opt(v: float | None) -> str:
    return "" if v is None else f"{v:g}"


def _parse(text: str, name: str, optional: bool = False) -> float | None:
    text = text.strip()
    if not text:
        if optional:
            return None
        raise ValueError(f"{name} is required.")
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{name}: {text!r} is not a number.") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be zero or positive.")
    return value


class _ReportExportMixin:
    """File > Export > Report… on :class:`PdnViewer`."""

    def _on_menu_export_report(self) -> None:
        if self.solution is None or not self._has_solve_results():
            QMessageBox.information(
                self, "Nothing to report",
                "Solve the design first — the report documents the solved "
                "results.")
            return
        stored = {}
        if getattr(self, "_project", None) is not None:
            stored = dict(self._project.viewer_settings.get("report") or {})
        settings = ReportSettings.from_dict(stored)
        # The via limit follows the Settings tab rather than being a second,
        # report-only number that could drift from what the Vias tab flags.
        settings.via_limit_a = float(self._via_current_warn_a)
        dlg = ReportDialog(self, settings, stored.get("format", "html"),
                           bool(stored.get("json", False)),
                           bool(stored.get("open_after", True)))
        if dlg.exec() != QDialog.Accepted or dlg.result_settings is None:
            return
        settings, fmt = dlg.result_settings, dlg.fmt
        json_sidecar, open_after = (dlg.json_check.isChecked(),
                                    dlg.open_check.isChecked())

        name = (self.metadata or {}).get("project_name") or "design"
        suffix = f"_Rev{settings.revision}" if settings.revision else ""
        start = Path(self._menu_start_dir()) / f"{name}{suffix}_PDN_report.{fmt}"
        filt = ("PDF document (*.pdf)" if fmt == "pdf"
                else "HTML page (*.html *.htm)")
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save Report", str(start), filt,
            options=_file_dialog_options())
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() not in (".pdf", ".html", ".htm"):
            path = path.with_suffix(f".{fmt}")

        proj = self._ensure_project()
        saved = settings.to_dict()
        saved.update(format=fmt, json=json_sidecar, open_after=open_after)
        proj.viewer_settings["report"] = saved
        self._display_dirty = True

        def _with_decoupling(decoupling, note) -> None:
            self._write_report(path, fmt, settings, decoupling, note,
                               json_sidecar, open_after)

        if "decoupling" in settings.sections \
                and getattr(self, "_loaded_project", None) is not None:
            self._ensure_cap_rows_async(
                lambda: _with_decoupling(*self._report_decoupling()))
        else:
            _with_decoupling(None, "Capacitor analysis needs the design info "
                             "(Reload Design Info) and was not run.")

    def _report_decoupling(self):
        """``(results by rail, note)`` from the Capacitors / Impedance tabs'
        data. GUI thread: the impedance model reads the Impedance tab's
        plane-capacitance checkbox."""
        from fypa.report.figures import impedance_png
        from fypa.report.model import DecouplingResult

        rows = getattr(self, "_caps_rows_cache", None) or []
        if not rows:
            return None, (getattr(self, "_caps_empty_reason", "")
                          or "No decoupling capacitors were identified.")
        rollup = getattr(self, "_caploop_rollup", None) or {}
        any_tier3 = any(r.get("l3_nh") is not None for r in rows)
        tier_note = (
            "Loop inductance from the Tier 3 FEM solve where available, the "
            "Tier 1 estimate otherwise." if any_tier3 else
            "Loop inductance from the Tier 1 closed-form estimate. Run Tier "
            "2/3 in the Capacitors tab for FEM values.")
        out: dict[str, DecouplingResult] = {}
        for rail in self._impedance_rails():
            rail_rows = [r for r in rows if r["rail"] == rail]
            try:
                res = self._compute_rail_impedance(rail)
            except Exception:
                log.warning("Impedance for %s failed in the report", rail,
                            exc_info=True)
                continue
            f = res.freqs_hz
            z = res.z_mag
            f_max = res.target.f_max_hz
            band = f <= f_max
            worst = None
            if band.any():
                i = int(np.argmax(np.where(band, z, -np.inf)))
                worst = (float(f[i]), float(z[i]))
            meets = res.meets_target()
            target = res.target.z_target_ohm
            peak = res.worst_peak
            shown = worst if not meets else (
                (peak.freq_hz, peak.z_ohm) if peak else None)
            cap_rows, flagged, under = [], [], 0
            for r in rail_rows:
                l_best = self._cap_l_best_nh(r)
                tier = ("Tier 3" if r.get("l3_nh") is not None else
                        "Tier 2" if r.get("l2_nh") is not None else "Tier 1")
                cap_rows.append({
                    "designator": r["designator"],
                    "capacitance_f": r.get("capacitance_f"),
                    "package": r.get("package"), "l_nh": l_best,
                    "tier": tier if r.get("included", True) else "excluded",
                    "flags": list(r.get("flags") or []),
                })
                if not r.get("included", True):
                    continue
                if l_best is not None and l_best < 1.0:
                    under += 1
                if self._cap_row_is_warn(r):
                    why = ", ".join(r.get("flags") or []) or (
                        f"loop L {l_best:.2g} nH")
                    flagged.append((r["designator"], why, l_best))
            summary = rollup.get(rail)
            out[rail] = DecouplingResult(
                analysed=True,
                target_ohm=target if math.isfinite(target) else None,
                f_max_hz=f_max, meets_target=meets,
                worst_z_ohm=worst[1] if worst else None,
                worst_f_hz=worst[0] if worst else None,
                reached_hz=None if meets else res.reached_frequency_hz(),
                n_caps=len(rail_rows), n_modelled=len(res.branches),
                n_under_1nh=under,
                parallel_l_nh=(summary.parallel_h * 1e9
                               if summary is not None
                               and getattr(summary, "parallel_h", None)
                               else None),
                tier_note=tier_note, flagged=flagged,
                skipped=list(res.skipped), cap_rows=cap_rows,
                z_png=impedance_png(f, z, target if math.isfinite(target)
                                    else None, f_max, shown),
            )
        return out, ""

    def _write_report(self, path: Path, fmt: str, settings: ReportSettings,
                      decoupling, decoupling_note: str, json_sidecar: bool,
                      open_after: bool) -> None:
        from fypa import log_buffer
        from fypa.report import build_report, write_report

        messages = [(m.level_name, m.message) for m in log_buffer.records()
                    if m.level >= _MESSAGE_LEVEL][-_MAX_MESSAGES:]
        solution, metadata = self.solution, self.metadata
        stale = bool(getattr(self, "_solve_stale", False))
        source_kind = getattr(getattr(self, "_project", None), "source_kind",
                              None) or "altium"

        def _work():
            # Worker thread: build_report and the renderers touch no widgets.
            try:
                report = build_report(
                    solution, metadata, settings, decoupling=decoupling,
                    decoupling_note=decoupling_note, solve_stale=stale,
                    messages=messages, source_kind=source_kind)
                return write_report(report, path, fmt,
                                    json_sidecar=json_sidecar)
            except Exception:
                # The background runner only forwards the message; keep the
                # traceback in the Messages tab / log file.
                log.exception("Report export to %s failed", path)
                raise

        def _ok(written) -> None:
            log.info("Report written to %s", ", ".join(map(str, written)))
            label = getattr(self, "_settings_status_label", None)
            if label is not None:
                label.setText(f"<span style='color:{_T()['ok']};'>Saved "
                              f"report to {_esc(str(path))}</span>")
            self.statusBar().showMessage(f"Saved report to {path}", 5000)
            if open_after:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

        def _fail(exc_type: str, message: str) -> None:
            log.error("Report export to %s failed: %s: %s", path, exc_type,
                      message)
            QMessageBox.critical(
                self, "Report Failed",
                f"Couldn't write the report:\n\n{exc_type}: {message}\n\n"
                "See the Messages tab for details.")

        _run_background_load(
            self, _work, _ok, _fail, title="Generate Report",
            label="Building the design report…\n"
                  "Sampling the solution and drawing the figures.")
