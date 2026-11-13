"""Auto setup wizard (Topology tab): propose PDN directives, review, apply.

Three steps in one dialog:

1. **Setup** — optional datasheet folder. Without one the wizard still runs
   on the design alone (pin names, net names, part descriptions, power-port
   symbols), with weaker type / efficiency / current values.
2. **Analysing** — :func:`fypa.autosetup.run_autosetup` on a worker thread,
   with progress and Cancel.
3. **Review** — every suggestion with a tick box, its value editable in
   place, and the reasoning (including the datasheet sentence a value came
   from) for the selected row. Apply returns the ticked suggestions; the
   caller turns them into editor directives.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from fypa.autosetup.classify import AutoSetupResult, Suggestion
from fypa.viewer.theme import _file_dialog_options, _T
from fypa.viewer.widgets import _esc

log = logging.getLogger(__name__)

_COLS = ("", "Role", "Part", "Part number", "Nets", "Value", "Type", "η / topology",
         "Confidence")
C_CHECK, C_ROLE, C_PART, C_PN, C_NETS, C_VALUE, C_TYPE, C_EFF, C_CONF = range(len(_COLS))

_ROLE_COLORS = {"SOURCE": "#ff3030", "SINK": "#3aa8ff", "SERIES": "#1a7a3c",
                "REGULATOR": "#ff66ff"}
_UNITS = {"voltage": "V", "current": "A", "resistance": "Ω"}
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
       "": 1.0, "k": 1e3}


def format_value(value: float | None, field: str) -> str:
    """``0.00064`` current → ``640 µA``; ``1.8`` voltage → ``1.8 V``."""
    if value is None:
        return "—"
    unit = _UNITS[field]
    a = abs(value)
    for mult, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ")):
        if a >= mult or mult == 1e-6:
            return f"{value / mult:.4g} {prefix}{unit}"
    return f"{value:g} {unit}"


def parse_value(text: str) -> float | None:
    """``640u``, ``640 µA``, ``0.64m``, ``1.8``, ``1.8 V``, ``10 mΩ`` → float.

    Lower-case ``m`` is milli; anything unparseable is ``None``."""
    t = (text or "").strip().replace(",", ".")
    t = re.sub(r"(?i)(ohms?|Ω|v|a)$", "", t).strip()
    m = re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([pnuµμmk]?)", t)
    if not m:
        return None
    return float(m.group(1)) * _SI[m.group(2)]


class _Worker(QObject):
    progress = Signal(float, str)
    finished = Signal(object, object)
    failed = Signal(str)

    def __init__(self, loaded, editor_directives, datasheet_dir) -> None:
        super().__init__()
        self._loaded = loaded
        self._eds = list(editor_directives)
        self._dir = datasheet_dir
        self.cancel = False

    def run(self) -> None:
        from fypa.autosetup import run_autosetup

        try:
            result, view = run_autosetup(
                self._loaded, editor_directives=self._eds,
                datasheet_dir=self._dir,
                progress=lambda f, m: self.progress.emit(f, m),
                cancelled=lambda: self.cancel,
            )
        except InterruptedError:
            self.failed.emit("")
            return
        except Exception as e:  # noqa: BLE001 - shown to the user, not swallowed
            log.exception("Auto setup failed")
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.finished.emit(result, view)


class AutoSetupWizard(QDialog):
    """Datasheet folder → analysis → review. ``chosen()`` after ``exec()``."""

    def __init__(self, parent, *, loaded, editor_directives, datasheet_dir: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto setup — PDN sources, regulators and sinks")
        self.resize(1080, 680)
        self._loaded = loaded
        self._eds = list(editor_directives)
        self.result: AutoSetupResult | None = None
        self.view = None
        self._rows: list[Suggestion] = []
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._filling = False

        t = _T()
        self._stack = QStackedWidget(self)
        outer = QVBoxLayout(self)
        outer.addWidget(self._stack, 1)
        self._stack.addWidget(self._build_setup_page(datasheet_dir, t))
        self._stack.addWidget(self._build_progress_page(t))
        self._stack.addWidget(self._build_review_page(t))

    # --- pages ---------------------------------------------------------------

    def _build_setup_page(self, datasheet_dir: str, t) -> QWidget:
        from fypa.autosetup import pdf_support_available

        page = QWidget()
        lay = QVBoxLayout(page)
        intro = QLabel(
            "<h3>Auto setup</h3>"
            "Proposes how to set up the PDN: which connectors bring power in "
            "(<b>sources</b>), which parts convert it (<b>regulators</b>), which "
            "ICs draw from each rail (<b>sinks</b>) and which sense resistors / "
            "ferrites sit in the path (<b>series</b>). It reads the design's pin "
            "names, net names, part parameters and power-port symbols.<br><br>"
            "Point it at a folder of <b>datasheets</b> (PDF) to identify "
            "regulators reliably and take efficiencies, supply currents and "
            "resistances from them. Files are matched to parts by part number "
            "(file name, or the first page). Without datasheets those values "
            "are estimates you will need to check.<br><br>"
            "Nothing changes until you review the suggestions and press "
            "<b>Apply</b>; applied suggestions become ordinary FYPA editor "
            "directives you can edit or remove. Parts already set up in the "
            "schematic or the editor are left alone.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Datasheet folder:"))
        self._dir_edit = QLineEdit(datasheet_dir)
        self._dir_edit.setPlaceholderText("optional — leave blank to use the design data only")
        self._dir_edit.setClearButtonEnabled(True)
        self._dir_edit.textChanged.connect(self._update_dir_hint)
        row.addWidget(self._dir_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        lay.addLayout(row)
        self._dir_hint = QLabel("")
        self._dir_hint.setWordWrap(True)
        self._dir_hint.setStyleSheet(f"color: {t['fg_muted']};")
        lay.addWidget(self._dir_hint)
        if not pdf_support_available():
            warn = QLabel("PDF reading is unavailable in this build (pypdf is "
                          "missing) — datasheets will be ignored.")
            warn.setStyleSheet(f"color: {t['warn']};")
            lay.addWidget(warn)
        lay.addStretch(1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self._analyse_btn = QPushButton("Analyse  ▶")
        self._analyse_btn.setDefault(True)
        self._analyse_btn.clicked.connect(self._start)
        btns.addWidget(cancel)
        btns.addWidget(self._analyse_btn)
        lay.addLayout(btns)
        self._update_dir_hint()
        return page

    def _build_progress_page(self, t) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addStretch(1)
        self._progress_label = QLabel("Starting…")
        self._progress_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._progress_label)
        self._progress = QProgressBar()
        self._progress.setRange(0, 1000)
        lay.addWidget(self._progress)
        lay.addStretch(1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self._cancel_run_btn = QPushButton("Cancel")
        self._cancel_run_btn.clicked.connect(self._cancel_run)
        btns.addWidget(self._cancel_run_btn)
        lay.addLayout(btns)
        return page

    def _build_review_page(self, t) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setTextFormat(Qt.RichText)
        lay.addWidget(self._summary)

        tools = QHBoxLayout()
        for text, slot in (("Tick all", lambda: self._tick(lambda s: True)),
                           ("Tick none", lambda: self._tick(lambda s: False)),
                           ("Tick confident (≥ 70 %)",
                            lambda: self._tick(lambda s: s.confidence >= 0.7))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            tools.addWidget(b)
        tools.addSpacing(12)
        tools.addWidget(QLabel("Show:"))
        self._role_filter = QComboBox()
        self._role_filter.addItems(["All roles", "SOURCE", "REGULATOR", "SERIES", "SINK"])
        self._role_filter.currentTextChanged.connect(lambda _t: self._apply_filter())
        tools.addWidget(self._role_filter)
        self._hide_estimates = QCheckBox("Only rows needing attention")
        self._hide_estimates.setToolTip("Rows with an estimated or missing value, "
                                        "or below 70 % confidence")
        self._hide_estimates.toggled.connect(lambda _c: self._apply_filter())
        tools.addWidget(self._hide_estimates)
        tools.addStretch(1)
        lay.addLayout(tools)

        split = QSplitter(Qt.Vertical)
        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.DoubleClicked
                                    | QAbstractItemView.EditKeyPressed
                                    | QAbstractItemView.SelectedClicked)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(C_NETS, QHeaderView.Stretch)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.currentCellChanged.connect(lambda r, *_: self._show_detail(r))
        split.addWidget(self._table)
        self._detail = QTextBrowser()
        self._detail.setOpenLinks(False)
        split.addWidget(self._detail)
        split.setSizes([440, 160])
        lay.addWidget(split, 1)

        btns = QHBoxLayout()
        back = QPushButton("◀  Back")
        back.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        btns.addWidget(back)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setDefault(True)
        self._apply_btn.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(self._apply_btn)
        lay.addLayout(btns)
        return page

    # --- setup page ---------------------------------------------------------------

    def _browse(self) -> None:
        start = self._dir_edit.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(
            self, "Datasheet folder", start,
            options=_file_dialog_options() | QFileDialog.Option.ShowDirsOnly)
        if d:
            self._dir_edit.setText(d)

    def _update_dir_hint(self) -> None:
        d = self._dir_edit.text().strip()
        if not d:
            self._dir_hint.setText(
                "No folder: suggestions use the design data only — regulator "
                "types may be guessed and sink currents will be estimates.")
            return
        p = Path(d)
        if not p.is_dir():
            self._dir_hint.setText("That folder doesn't exist.")
            return
        n = sum(1 for f in p.rglob("*") if f.suffix.lower() == ".pdf")
        self._dir_hint.setText(f"{n} PDF file(s) found (searched recursively).")

    def datasheet_dir(self) -> str:
        return self._dir_edit.text().strip()

    # --- run --------------------------------------------------------------------

    def _start(self) -> None:
        d = self.datasheet_dir()
        if d and not Path(d).is_dir():
            self._dir_hint.setText("That folder doesn't exist — clear it to run "
                                   "without datasheets.")
            return
        self._stack.setCurrentIndex(1)
        self._progress.setValue(0)
        self._progress_label.setText("Reading the design…")
        self._cancel_run_btn.setEnabled(True)
        self._thread = QThread(self)
        self._worker = _Worker(self._loaded, self._eds, d or None)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def _cancel_run(self) -> None:
        if self._worker is not None:
            self._worker.cancel = True
            self._cancel_run_btn.setEnabled(False)
            self._progress_label.setText("Cancelling…")

    def _on_progress(self, frac: float, msg: str) -> None:
        self._progress.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self._progress_label.setText(msg)

    def _on_failed(self, msg: str) -> None:
        self._stack.setCurrentIndex(0)
        if msg:
            self._dir_hint.setText(f"<span style='color:{_T()['err']};'>Auto setup "
                                   f"failed: {_esc(msg)}</span>")

    def _on_finished(self, result: AutoSetupResult, view) -> None:
        self.result = result
        self.view = view
        self._rows = list(result.suggestions)
        self._fill_table()
        self._fill_summary()
        self._stack.setCurrentIndex(2)
        if self._rows:
            self._table.selectRow(0)

    def done(self, r: int) -> None:  # noqa: D401 - Qt override
        if self._worker is not None:
            self._worker.cancel = True
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(5000)
        super().done(r)

    # --- review -------------------------------------------------------------------

    def _fill_summary(self) -> None:
        res = self.result
        t = _T()
        counts: dict[str, int] = {}
        for s in res.suggestions:
            counts[s.role] = counts.get(s.role, 0) + 1
        parts = [f"{n} {role.lower()}{'s' if n != 1 else ''}"
                 for role, n in sorted(counts.items())]
        html = (f"<b>{len(res.suggestions)} suggestion(s)</b>: "
                f"{', '.join(parts) or 'none'}.")
        if res.already_configured:
            names = ", ".join(d for d, _w in res.already_configured[:12])
            more = "…" if len(res.already_configured) > 12 else ""
            html += (f" <span style='color:{t['fg_muted']};'>Already set up, "
                     f"left alone: {_esc(names)}{more}.</span>")
        if res.datasheets_used:
            html += (f"<br>Datasheets matched for {len(res.datasheet_matches)} part(s)"
                     + (f"; none found for {_esc(', '.join(res.without_datasheet[:10]))}"
                        if res.without_datasheet else "") + ".")
        for note in res.notes:
            html += f"<br><span style='color:{t['warn']};'>{_esc(note)}</span>"
        html += (f"<br><span style='color:{t['fg_muted']};'>Tick what to apply. "
                 "Double-click a value to change it. <span style='color:#e0a030;'>"
                 "Amber</span> values are estimates; rows missing a value can't "
                 "be ticked until it is filled.</span>")
        self._summary.setText(html)

    def _fill_table(self) -> None:
        self._filling = True
        try:
            self._table.setRowCount(len(self._rows))
            for r, s in enumerate(self._rows):
                self._fill_row(r, s)
        finally:
            self._filling = False
        self._apply_filter()
        self._update_apply_button()

    def _fill_row(self, r: int, s: Suggestion) -> None:
        t = _T()
        check = QTableWidgetItem()
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if s.complete:
            flags |= Qt.ItemIsUserCheckable
        check.setFlags(flags)
        check.setCheckState(Qt.Checked if (s.accepted and s.complete) else Qt.Unchecked)
        if not s.complete:
            check.setToolTip("Missing: " + ", ".join(s.missing()))
        self._table.setItem(r, C_CHECK, check)

        role = QTableWidgetItem(s.role)
        role.setForeground(QColor(_ROLE_COLORS.get(s.role, t["fg"])))
        self._ro(r, C_ROLE, role)
        self._ro(r, C_PART, QTableWidgetItem(s.designator))
        self._ro(r, C_PN, QTableWidgetItem(s.part_number))
        self._ro(r, C_NETS, QTableWidgetItem(s.nets_text()))

        value = QTableWidgetItem(format_value(s.value, s.value_field))
        value.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
        if s.value is None:
            value.setForeground(QColor(t["err"]))
            value.setToolTip("Missing — double-click to enter")
        elif s.value_field in s.estimated:
            value.setForeground(QColor("#e0a030"))
            value.setToolTip("Estimate — double-click to change")
        self._table.setItem(r, C_VALUE, value)

        if s.role == "REGULATOR":
            combo = QComboBox()
            combo.addItems(["LDO", "SMPS"])
            combo.setCurrentText(s.regulator_type or "LDO")
            combo.currentTextChanged.connect(
                lambda text, sug=s, row=r: self._on_type_changed(sug, row, text))
            self._table.setCellWidget(r, C_TYPE, combo)
            self._ro(r, C_TYPE, QTableWidgetItem(""))
            eff = QTableWidgetItem(self._type_text(s))
            eff.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled
                         | (Qt.ItemIsEditable if s.regulator_type == "SMPS" else Qt.NoItemFlags))
            eff.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if "efficiency" in s.estimated:
                eff.setForeground(QColor("#e0a030"))
            self._table.setItem(r, C_EFF, eff)
        else:
            self._table.removeCellWidget(r, C_TYPE)
            self._ro(r, C_TYPE, QTableWidgetItem(""))
            self._ro(r, C_EFF, QTableWidgetItem(""))

        conf = QTableWidgetItem(f"{s.confidence:.0%}")
        conf.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if s.confidence < 0.5:
            conf.setForeground(QColor(t["warn"]))
        self._ro(r, C_CONF, conf)

    @staticmethod
    def _type_text(s: Suggestion) -> str:
        if s.regulator_type == "SMPS":
            eff = f"η {s.efficiency * 100:.0f} %" if s.efficiency else "η —"
            return f"{eff}  {s.smps_topology or ''}".rstrip()
        return ""

    def _ro(self, r: int, c: int, item: QTableWidgetItem) -> None:
        item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self._table.setItem(r, c, item)

    def _on_type_changed(self, s: Suggestion, row: int, text: str) -> None:
        s.regulator_type = text
        s.estimated.discard("regulator_type")
        if text == "SMPS" and s.efficiency is None:
            from fypa.autosetup.classify import DEFAULT_SMPS_EFFICIENCY
            s.efficiency = DEFAULT_SMPS_EFFICIENCY
            s.estimated.add("efficiency")
        # Only the η cell changes. Rebuilding the row would replace the combo
        # box whose signal is running this handler.
        self._filling = True
        try:
            eff = self._table.item(row, C_EFF)
            if eff is not None:
                eff.setText(self._type_text(s))
                eff.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled
                             | (Qt.ItemIsEditable if text == "SMPS" else Qt.NoItemFlags))
                eff.setForeground(QColor("#e0a030" if "efficiency" in s.estimated
                                         else _T()["fg"]))
        finally:
            self._filling = False
        self._show_detail(row)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        r = item.row()
        if not (0 <= r < len(self._rows)):
            return
        s = self._rows[r]
        c = item.column()
        if c == C_CHECK:
            s.accepted = item.checkState() == Qt.Checked
            self._update_apply_button()
            return
        if c == C_VALUE:
            v = parse_value(item.text())
            if v is not None and (v > 0 or s.role == "SOURCE" and v != 0):
                setattr(s, s.value_field, v)
                s.estimated.discard(s.value_field)
                if s.complete:
                    s.accepted = True
        elif c == C_EFF and s.role == "REGULATOR":
            m = re.search(r"(\d+(?:\.\d+)?)", item.text())
            if m:
                eff = float(m.group(1))
                eff = eff / 100.0 if eff > 1 else eff
                if 0 < eff <= 1:
                    s.efficiency = eff
                    s.estimated.discard("efficiency")
        self._filling = True
        try:
            self._fill_row(r, s)
        finally:
            self._filling = False
        self._update_apply_button()
        self._show_detail(r)

    def _tick(self, pred) -> None:
        for s in self._rows:
            s.accepted = s.complete and pred(s)
        self._fill_table()

    def _apply_filter(self) -> None:
        role = self._role_filter.currentText()
        attention = self._hide_estimates.isChecked()
        for r, s in enumerate(self._rows):
            show = role in ("All roles", s.role)
            if attention:
                show = show and (bool(s.estimated) or not s.complete or s.confidence < 0.7)
            self._table.setRowHidden(r, not show)

    def _update_apply_button(self) -> None:
        n = len(self.chosen())
        self._apply_btn.setText(f"Apply {n} suggestion{'s' if n != 1 else ''}")
        self._apply_btn.setEnabled(n > 0)

    def _show_detail(self, r: int) -> None:
        if not (0 <= r < len(self._rows)):
            self._detail.clear()
            return
        s = self._rows[r]
        t = _T()
        lines = [f"<b>{_esc(s.role)} {_esc(s.designator)}</b> "
                 f"<span style='color:{t['fg_muted']};'>{_esc(s.part_number)}</span>"
                 f" — {_esc(s.nets_text())}"]
        if s.role == "REGULATOR" and s.n_net:
            lines[0] += f", return {_esc(s.n_net)}"
        elif s.role in ("SOURCE", "SINK"):
            lines[0] += (f", return {_esc(s.n_net)}" if s.n_net and not s.single_net
                         else " (single net, ideal return)")
        items = "".join(f"<li>{_esc(reason)}</li>" for reason in s.reasons)
        lines.append(f"<ul style='margin-top:2px;'>{items}</ul>")
        if s.missing():
            lines.append(f"<span style='color:{t['err']};'>Missing: "
                         f"{_esc(', '.join(s.missing()))}</span>")
        if s.datasheet is not None:
            lines.append(f"<span style='color:{t['fg_muted']};'>Datasheet: "
                         f"{_esc(str(s.datasheet))}</span>")
        self._detail.setHtml("<br>".join(lines))

    def chosen(self) -> list[Suggestion]:
        return [s for s in self._rows if s.accepted and s.complete]
