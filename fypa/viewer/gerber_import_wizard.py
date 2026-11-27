"""The Import Gerber Files window.

One window with the steps listed down the left and the current step on the
right. The user can move back and forward freely; Next only goes on once the
current step has no problems, and the list only lets you click steps you
have already reached.

1. **Files & roles** - :class:`fypa.gerber.import_ui.GerberFilesPage`
2. **Stackup** - :class:`fypa.gerber.import_ui.GerberStackupPage`, rebuilt
   from step 1's copper layers each time it is entered (values typed for
   layers that are still there are kept)
3. **Review & import** - a summary, then the import itself runs here with
   its progress on the page. A failed or cancelled import leaves the window
   open with everything as it was, so the user can go back and fix it.

On success the window closes and :attr:`GerberImportWizard.import_output`
holds ``(res, result, pseudo)`` for the caller to open the viewer with.
"""
from __future__ import annotations

import html
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from fypa.gerber.import_ui import (
    GerberFilesPage,
    GerberStackupPage,
    _order_copper_layer_ids,
    _role_label,
    build_import_result,
)
from fypa.viewer.gerber_import import GerberImportRun, _pseudo_project_path
from fypa.viewer.theme import _T

STEP_FILES = 0
STEP_STACKUP = 1
STEP_REVIEW = 2

_STEPS = (
    ("Files & roles",
     "Pick the board's Gerber and drill files and check what each one is."),
    ("Stackup",
     "Set the copper and dielectric thickness of each copper layer."),
    ("Review & import",
     "Check the summary, then import. You can still go back."),
)


def _bullets(items: list[str], colour: str) -> str:
    rows = "".join(f"<li>{html.escape(s)}</li>" for s in items)
    return (f"<ul style='margin:0; -qt-list-indent:1; color:{colour};'>"
            f"{rows}</ul>")


class GerberImportWizard(QDialog):
    def __init__(self, parent=None, *, files=None, initial_assignments=None,
                 initial_stackup=None, dialog_options=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Gerber Files")
        self.resize(900, 560)
        self.import_output: tuple | None = None
        self._current = STEP_FILES
        self._furthest = STEP_FILES
        self._run: GerberImportRun | None = None
        self._failed = False
        self._review_msg = ""
        self._stage_text = ""
        self._substage_text = ""
        self._stage_count = 0
        self._stage_start = self._total_start = time.monotonic()
        t = _T()
        self._t = t

        # --- left: step list ---------------------------------------------
        self.step_list = QListWidget(self)
        self.step_list.setFixedWidth(190)
        self.step_list.setFocusPolicy(Qt.NoFocus)
        self.step_list.setStyleSheet(
            f"QListWidget {{ background-color: {t['bg_alt']};"
            f" border: 1px solid {t['border']}; border-radius: 6px;"
            " padding: 6px; }"
            "QListWidget::item { padding: 8px 6px; border-radius: 4px; }"
            f"QListWidget::item:selected {{ background-color:"
            f" {t['bg_selection']}; color: {t['fg_strong']}; }}")
        for _title, _sub in _STEPS:
            self.step_list.addItem(QListWidgetItem())
        self.step_list.itemClicked.connect(
            lambda item: self._on_step_clicked(self.step_list.row(item)))

        # --- right: header, pages, messages ------------------------------
        self.title_label = QLabel(self)
        self.title_label.setTextFormat(Qt.RichText)
        self.subtitle_label = QLabel(self)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet(f"color: {t['fg_muted']};")

        self.files_page = GerberFilesPage(
            files, initial_assignments=initial_assignments,
            dialog_options=dialog_options, parent=self)
        self.files_page.notice.setStyleSheet(f"color: {t['fg_muted']};")
        self.files_page.empty_label.setStyleSheet(
            f"color: {t['fg_dim']}; border: 1px dashed {t['border']};"
            " border-radius: 6px; padding: 24px;")
        self.stackup_page = GerberStackupPage(initial_stackup, parent=self)
        self.review_page = self._build_review_page()

        self.pages = QStackedWidget(self)
        for page in (self.files_page, self.stackup_page, self.review_page):
            self.pages.addWidget(page)

        self.message_label = QLabel(self)
        self.message_label.setTextFormat(Qt.RichText)
        self.message_label.setWordWrap(True)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self.title_label)
        right.addWidget(self.subtitle_label)
        right.addWidget(self.pages, 1)
        right.addWidget(self.message_label)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self.step_list)
        body.addLayout(right, 1)

        # --- bottom: buttons ---------------------------------------------
        self.cancel_btn = QPushButton("Cancel", self)
        self.back_btn = QPushButton("< Back", self)
        self.next_btn = QPushButton("Next >", self)
        # No default button: Enter in a spin box or combo must not jump to
        # the next step.
        for b in (self.cancel_btn, self.back_btn, self.next_btn):
            b.setAutoDefault(False)
            b.setDefault(False)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        self.back_btn.clicked.connect(lambda: self._go_to(self._current - 1))
        self.next_btn.clicked.connect(self._on_next)
        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.back_btn)
        buttons.addWidget(self.next_btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.addLayout(body, 1)
        outer.addLayout(buttons)

        self.files_page.changed.connect(self._refresh)
        self.stackup_page.changed.connect(self._refresh)

        self._stage_timer = QTimer(self)
        self._stage_timer.setInterval(1000)
        self._stage_timer.timeout.connect(self._render_progress)

        self._show_step(STEP_FILES)

    # --- review page -----------------------------------------------------

    def _build_review_page(self) -> QWidget:
        t = self._t
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.summary_label = QLabel(page)
        self.summary_label.setTextFormat(Qt.RichText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lay.addWidget(self.summary_label)
        lay.addStretch(1)

        self.progress_box = QWidget(page)
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 0, 0, 0)
        self.stage_label = QLabel(self.progress_box)
        self.substage_label = QLabel(self.progress_box)
        self.substage_label.setStyleSheet(f"color: {t['fg_muted']};")
        self.progress_bar = QProgressBar(self.progress_box)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.elapsed_label = QLabel(self.progress_box)
        self.elapsed_label.setStyleSheet(f"color: {t['fg_muted']};")
        for w in (self.stage_label, self.substage_label, self.progress_bar,
                  self.elapsed_label):
            pl.addWidget(w)
        self.progress_box.hide()
        lay.addWidget(self.progress_box)
        return page

    def _summary_html(self) -> str:
        t = self._t
        chk = self.files_page.check()
        stack = {L.layer_id: L for L in self.stackup_page.values()}
        rows = []
        for lid in _order_copper_layer_ids(list(chk.copper)):
            L = stack.get(lid)
            thick = (f"{L.copper_thickness_mm:.4g} mm copper"
                     if L is not None else "")
            rows.append(
                f"<tr><td style='padding:1px 14px 1px 0;'>"
                f"{html.escape(_role_label(lid))}</td>"
                f"<td style='padding:1px 14px 1px 0;'>"
                f"{html.escape(chk.copper[lid].name)}</td>"
                f"<td style='color:{t['fg_muted']};'>{thick}</td></tr>")

        def names(paths) -> str:
            return (html.escape(", ".join(p.name for p in paths))
                    if paths else f"<span style='color:{t['fg_dim']};'>"
                                  "none</span>")

        total = sum(L.copper_thickness_mm + L.dielectric_thickness_mm
                    for L in stack.values())
        parts = [
            f"<b>{len(chk.copper)} copper layers</b>",
            f"<table style='margin:4px 0 10px 12px;'>{''.join(rows)}</table>",
            f"<b>Drill:</b> {names(chk.drills)}<br>",
            f"<b>Outline:</b> "
            f"{names([chk.outline] if chk.outline else [])}<br>",
            f"<b>Ignored:</b> {len(chk.ignored)} file(s)<br>",
            f"<b>Board thickness:</b> {total:.3f} mm "
            f"(stated {self.stackup_page.total_thickness_mm():.3f} mm)",
        ]
        warnings = list(chk.notes)
        sw = self.stackup_page.sum_warning()
        if sw:
            warnings.append(sw)
        if warnings:
            parts.append("<p style='margin:10px 0 2px 0;'><b>Check:</b></p>"
                         + _bullets(warnings, t["warn"]))
        return "".join(parts)

    # --- step state ------------------------------------------------------

    def _step_problems(self, step: int) -> list[str]:
        if step == STEP_FILES:
            return self.files_page.check().problems
        if step == STEP_STACKUP:
            return self.stackup_page.problems()
        return []

    def _step_ok(self, step: int) -> bool:
        return not self._step_problems(step)

    def _can_reach(self, step: int) -> bool:
        if step > self._furthest:
            return False
        return all(self._step_ok(s) for s in range(step))

    def _sync_stackup_layers(self) -> None:
        chk = self.files_page.check()
        if chk.ok:
            self.stackup_page.set_layers(
                _order_copper_layer_ids(list(chk.copper)))

    def _go_to(self, step: int) -> None:
        """Back is always allowed; forward only to a step already reached
        (or the next new one), and only when every step before it is OK."""
        if self._run is not None or step == self._current:
            return
        if step < 0 or step >= len(_STEPS):
            return
        if step > self._current and (
                step > self._furthest + 1
                or not all(self._step_ok(s) for s in range(step))):
            return
        self._show_step(step)

    def _show_step(self, step: int) -> None:
        if step >= STEP_STACKUP:
            self._sync_stackup_layers()
        self._current = step
        self._furthest = max(self._furthest, step)
        if step != STEP_REVIEW:
            self._failed = False
            self._review_msg = ""
        self.pages.setCurrentIndex(step)
        title, sub = _STEPS[step]
        self.title_label.setText(
            f"<h3 style='margin:0;'>Step {step + 1} of {len(_STEPS)}: "
            f"{html.escape(title)}</h3>")
        self.subtitle_label.setText(sub)
        self._refresh()

    def _refresh(self) -> None:
        t = self._t
        running = self._run is not None
        step = self._current

        # Step list.
        for i, (title, _sub) in enumerate(_STEPS):
            item = self.step_list.item(i)
            visited = i <= self._furthest
            ok = self._step_ok(i)
            if i == step:
                mark = "▶"
            elif visited and i < STEP_REVIEW:
                mark = "✓" if ok else "!"
            else:
                mark = " "
            item.setText(f"{mark}  {i + 1}. {title}")
            enabled = (not running) and (i == step or self._can_reach(i))
            flags = Qt.ItemIsSelectable
            if enabled:
                flags |= Qt.ItemIsEnabled
            item.setFlags(flags)
            if visited and not ok and i != step:
                item.setForeground(QColor(t["warn"]))
            elif not enabled and i != step:
                item.setForeground(QColor(t["fg_dim"]))
            else:
                item.setForeground(QColor(t["fg"]))
            font = QFont(item.font())
            font.setBold(i == step)
            item.setFont(font)
        self.step_list.setCurrentRow(step)

        # Messages under the page.
        problems = self._step_problems(step)
        if step == STEP_FILES:
            chk = self.files_page.check()
            msg = ""
            # An empty list already says so on the page itself.
            if chk.problems and self.files_page.files():
                msg += _bullets(chk.problems, t["err"])
            if chk.notes:
                msg += _bullets(chk.notes, t["warn"])
        elif step == STEP_STACKUP:
            msg = _bullets(problems, t["err"]) if problems else ""
            sw = self.stackup_page.sum_warning()
            if sw:
                msg += _bullets([sw], t["warn"])
        else:
            msg = self._review_msg
            if not running:
                self.summary_label.setText(self._summary_html())
        self.message_label.setText(msg)
        self.message_label.setVisible(bool(msg))

        # Buttons.
        self.back_btn.setEnabled(step > 0 and not running)
        if step == STEP_REVIEW:
            self.next_btn.setText("Try again" if self._failed else "Import")
            self.next_btn.setEnabled(not running and self._can_reach(step))
        else:
            self.next_btn.setText("Next >")
            self.next_btn.setEnabled(not running and not problems)
        self.cancel_btn.setText("Cancel import" if running else "Cancel")
        self.files_page.setEnabled(not running)
        self.stackup_page.setEnabled(not running)

    def _on_step_clicked(self, step: int) -> None:
        if step != self._current:
            self._go_to(step)
        # Keep the highlight on the real current step if the jump was
        # refused.
        self.step_list.setCurrentRow(self._current)

    def _on_next(self) -> None:
        if self._current == STEP_REVIEW:
            self._start_import()
        else:
            self._go_to(self._current + 1)

    # --- import ----------------------------------------------------------

    def _set_message(self, text: str, colour: str) -> None:
        """Message under the Review page (rich text; escape it first)."""
        self._review_msg = (f"<span style='color:{colour};'>{text}</span>"
                            if text else "")

    def _start_import(self) -> None:
        if self._run is not None:
            return
        from fypa.viewer.session import _any_gerber_import_running
        if _any_gerber_import_running():
            self._failed = True
            self._set_message(
                "A previous Gerber import is still finishing in the "
                "background. Wait a moment, then try again.",
                self._t["warn"])
            self._refresh()
            return
        chk = self.files_page.check()
        if not chk.ok:
            return
        result = build_import_result(chk, self.stackup_page.values())
        pseudo = _pseudo_project_path(self.files_page.files()[0])
        run = GerberImportRun(result, pseudo, parent=self)
        run.stage_changed.connect(self._on_stage)
        run.substage_changed.connect(self._on_substage)
        run.total_stages.connect(self._on_total_stages)
        run.succeeded.connect(self._on_import_ok)
        run.failed.connect(self._on_import_failed)
        self._run = run
        self._failed = False
        self._stage_text = "Starting Gerber import…"
        self._substage_text = ""
        self._stage_count = 0
        self._stage_start = self._total_start = time.monotonic()
        self.progress_bar.setRange(0, 0)
        self.progress_box.show()
        self._set_message("", self._t["fg"])
        self._stage_timer.start()
        self._render_progress()
        self._refresh()
        run.start()

    def _on_total_stages(self, n: int) -> None:
        if n > 0:
            self.progress_bar.setRange(0, n)
            self.progress_bar.setValue(min(self._stage_count, n))

    def _on_stage(self, text: str) -> None:
        self._stage_text = text
        self._substage_text = ""
        self._stage_start = time.monotonic()
        self._stage_count += 1
        if self.progress_bar.maximum() > 0:
            if self._stage_count > self.progress_bar.maximum():
                self.progress_bar.setMaximum(self._stage_count)
            self.progress_bar.setValue(self._stage_count)
        self._render_progress()

    def _on_substage(self, text: str) -> None:
        self._substage_text = text
        self._render_progress()

    def _render_progress(self) -> None:
        now = time.monotonic()
        stage_s = int(now - self._stage_start)
        self.stage_label.setText(
            f"{self._stage_text}  ({stage_s}s)" if stage_s > 0
            else self._stage_text)
        self.substage_label.setText(self._substage_text)
        self.substage_label.setVisible(bool(self._substage_text))
        self.elapsed_label.setText(
            f"Elapsed: {int(now - self._total_start)}s")

    def _end_run(self) -> None:
        self._stage_timer.stop()
        self.progress_box.hide()
        run, self._run = self._run, None
        if run is not None:
            run.deleteLater()

    def _on_import_ok(self, res) -> None:
        run = self._run
        self._end_run()
        self.import_output = (res, run.result, run.pseudo)
        self.accept()

    def _on_import_failed(self, msg: str) -> None:
        self._end_run()
        self._failed = True
        self._set_message(
            "<b>The import failed:</b> " + html.escape(msg)
            + "<br>Go back to change the files or stackup, or try again.",
            self._t["err"])
        self._refresh()

    def cancel_import(self) -> None:
        if self._run is None:
            return
        self._run.cancel()
        self._end_run()
        self._failed = False
        self._set_message("Import cancelled.", self._t["fg_muted"])
        self._refresh()

    # --- closing ---------------------------------------------------------

    def _on_cancel_clicked(self) -> None:
        if self._run is not None:
            self.cancel_import()
        else:
            self.reject()

    def reject(self) -> None:
        """Cancel, Esc and the window's close button all come here. Asks
        before throwing away a file list; closing while an import runs also
        cancels it."""
        if self.files_page.files():
            ans = QMessageBox.question(
                self, "Discard this import?",
                "Close the import window? The files and settings you "
                "entered will not be kept.")
            if ans != QMessageBox.Yes:
                return
        if self._run is not None:
            self._run.cancel()
            self._end_run()
        super().reject()


def run_gerber_import_wizard(parent) -> tuple | None:
    """Show the import window over ``parent``. Returns
    ``(res, result, pseudo)`` after a successful import, or None."""
    from fypa.viewer.theme import _file_dialog_options
    dlg = GerberImportWizard(parent, dialog_options=_file_dialog_options())
    try:
        if dlg.exec() == QDialog.Accepted:
            return dlg.import_output
        return None
    finally:
        dlg.deleteLater()


__all__ = ["GerberImportWizard", "run_gerber_import_wizard",
           "STEP_FILES", "STEP_STACKUP", "STEP_REVIEW"]
