"""The Design issues panel: the design's setup problems, on every tab.

A load or a solve can leave things to act on - a directive that was skipped,
a rail left out of the solve, a source and sink on copper that is not
connected. They used to arrive as a run of pop-ups that were gone once
dismissed. They are now listed in a panel along the bottom of the window,
under whichever tab is open::

    > Design issues   (x) 1 Error   (!) 2 Warnings   (i) 14 Notes

* The strip is there only while the design has an error or a warning, so a
  clean design gives the board its full height.
* Collapsed by default; a click on the strip opens the list. The three
  counts are also filters: Errors and Warnings start on, Notes (what FYPA
  worked out by itself) start off.
* Double-click an issue to go to the part or rail it is about - on the
  Topology diagram when that tab is open, else on the Board. Right-click
  to copy.
* One pop-up after a load, or a solve that turned up something new, says
  how many there are and where to find them.

The records come from :mod:`fypa.design_issues`; the same ones put the
warning icons on the Topology blocks.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSpacerItem,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fypa.design_issues import (
    ERROR,
    NOTE,
    WARNING,
    DesignIssue,
    collect_design_issues,
    count_by_severity,
    issues_by_designator,
    summary_text,
)
from fypa.viewer.theme import _T

STRIP_HEIGHT_PX = 28
_DEFAULT_OPEN_PX = 210      # the list's height the first time it is opened
_MIN_OPEN_PX = 110
_QWIDGETSIZE_MAX = 16777215
# Width of the pop-up. A QMessageBox sizes itself to a narrow column of
# text; this keeps the two sentences to a couple of lines.
POPUP_WIDTH_PX = 720

_SEVERITY_WORD = {ERROR: "Error", WARNING: "Warning", NOTE: "Note"}
# (severity, singular, plural) in strip order.
_FILTERS = ((ERROR, "Error", "Errors"), (WARNING, "Warning", "Warnings"),
            (NOTE, "Note", "Notes"))

_COL_ICON, _COL_KIND, _COL_WHERE, _COL_TEXT = range(4)


def severity_color(severity: str) -> str:
    t = _T()
    return {ERROR: t["err"], WARNING: t["warn"]}.get(severity, t["accent"])


def severity_icon(severity: str, size: int = 16) -> QIcon:
    """Drawn, so it never depends on a font having the glyph: a disc with a
    cross (error), a triangle with a bar (warning), a disc with an i (note)."""
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.setDevicePixelRatio(scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    color = QColor(severity_color(severity))
    mark = QColor(_T()["bg"])
    s = float(size)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    pen = QPen(mark, max(1.4, s * 0.11))
    pen.setCapStyle(Qt.RoundCap)
    if severity == WARNING:
        p.drawPolygon(QPolygonF([QPointF(s / 2, s * 0.08),
                                 QPointF(s * 0.96, s * 0.92),
                                 QPointF(s * 0.04, s * 0.92)]))
        p.setPen(pen)
        p.drawLine(QPointF(s / 2, s * 0.40), QPointF(s / 2, s * 0.62))
        p.drawPoint(QPointF(s / 2, s * 0.78))
    else:
        p.drawEllipse(QPointF(s / 2, s / 2), s * 0.45, s * 0.45)
        p.setPen(pen)
        if severity == ERROR:
            a = s * 0.19
            p.drawLine(QPointF(s / 2 - a, s / 2 - a),
                       QPointF(s / 2 + a, s / 2 + a))
            p.drawLine(QPointF(s / 2 - a, s / 2 + a),
                       QPointF(s / 2 + a, s / 2 - a))
        else:
            p.drawPoint(QPointF(s / 2, s * 0.30))
            p.drawLine(QPointF(s / 2, s * 0.47), QPointF(s / 2, s * 0.73))
    p.end()
    return QIcon(pm)


class _Strip(QFrame):
    """The always-visible row; a click anywhere but a count toggles the
    list."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt name
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class DesignIssuesPanel(QWidget):
    """The strip and, when open, the list under it. Hidden altogether while
    the design has neither an error nor a warning."""

    jump_requested = Signal(object)         # the DesignIssue
    expanded_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._issues: list[DesignIssue] = []
        self._rows: list[DesignIssue] = []      # what the table shows
        self._expanded = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        strip = _Strip(self)
        strip.setObjectName("fypaIssuesStrip")
        strip.setFixedHeight(STRIP_HEIGHT_PX)
        strip.setCursor(Qt.PointingHandCursor)
        strip.clicked.connect(self.toggle)
        row = QHBoxLayout(strip)
        row.setContentsMargins(8, 0, 10, 0)
        row.setSpacing(8)
        self._chevron = QLabel(strip)
        self._chevron.setFixedWidth(12)
        self._title = QLabel("Design issues", strip)
        title_font = QFont(self._title.font())
        title_font.setBold(True)
        self._title.setFont(title_font)
        for w in (self._chevron, self._title):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            row.addWidget(w)
        row.addSpacing(6)
        self._filter_btns: dict[str, QToolButton] = {}
        for severity, _one, _many in _FILTERS:
            btn = QToolButton(strip)
            btn.setCheckable(True)
            btn.setChecked(severity != NOTE)
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            btn.setIconSize(QSize(14, 14))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(self._on_filter_clicked)
            self._filter_btns[severity] = btn
            row.addWidget(btn)
        row.addStretch(1)
        self._hint = QLabel(
            "Double-click an issue to go to it. Right-click to copy.", strip)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.addWidget(self._hint)
        self._strip = strip
        outer.addWidget(strip)

        table = QTableWidget(0, 4, self)
        table.setHorizontalHeaderLabels(["", "Issue", "Where", "Description"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.setContextMenuPolicy(Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(self._on_context_menu)
        table.cellDoubleClicked.connect(self._on_double_clicked)
        header = table.horizontalHeader()
        header.setSectionResizeMode(_COL_ICON, QHeaderView.Fixed)
        header.setSectionResizeMode(_COL_KIND, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_WHERE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_TEXT, QHeaderView.Stretch)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        table.setColumnWidth(_COL_ICON, 28)
        # Wrapped rows are as tall as their text at the current width.
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_rows)
        header.sectionResized.connect(lambda *_a: self._fit_timer.start())
        copy = QShortcut(QKeySequence.Copy, table)
        copy.setContext(Qt.WidgetWithChildrenShortcut)
        copy.activated.connect(self.copy_selected)
        self.table = table
        outer.addWidget(table, 1)

        self.restyle()
        self._apply_expanded()
        self.setVisible(False)

    # --- content ------------------------------------------------------------

    def issues(self) -> list[DesignIssue]:
        return list(self._issues)

    def shown_issues(self) -> list[DesignIssue]:
        """The rows the list holds, in order (the filters applied)."""
        return list(self._rows)

    def set_issues(self, issues) -> None:
        """Take the design's issues. The panel shows itself while there is
        an error or a warning and goes away (closed) when there is none."""
        self._issues = list(issues)
        counts = count_by_severity(self._issues)
        for severity, one, many in _FILTERS:
            n = counts[severity]
            btn = self._filter_btns[severity]
            btn.setText(f"{n} {one if n == 1 else many}")
            btn.setEnabled(n > 0)
        has_problem = bool(counts[ERROR] or counts[WARNING])
        if not has_problem and self._expanded:
            self.set_expanded(False)
        self._rebuild()
        self.setVisible(has_problem)

    def _wanted(self) -> set[str]:
        return {s for s, b in self._filter_btns.items() if b.isChecked()}

    def _rebuild(self) -> None:
        wanted = self._wanted()
        self._rows = [i for i in self._issues if i.severity in wanted]
        table = self.table
        table.setRowCount(len(self._rows))
        for r, issue in enumerate(self._rows):
            icon = QTableWidgetItem()
            icon.setIcon(severity_icon(issue.severity))
            icon.setToolTip(_SEVERITY_WORD.get(issue.severity, ""))
            kind = QTableWidgetItem(issue.label)
            kind.setForeground(QBrush(QColor(severity_color(issue.severity))))
            where = QTableWidgetItem(issue.where)
            if issue.designators or issue.nets:
                where.setToolTip(", ".join(issue.designators + issue.nets))
            text = QTableWidgetItem(issue.message)
            for item in (icon, kind, where, text):
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignTop)
            table.setItem(r, _COL_ICON, icon)
            table.setItem(r, _COL_KIND, kind)
            table.setItem(r, _COL_WHERE, where)
            table.setItem(r, _COL_TEXT, text)
        self._fit_rows()

    def _fit_rows(self) -> None:
        if self._expanded:
            self.table.resizeRowsToContents()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt name
        super().resizeEvent(event)
        self._fit_timer.start()

    # --- open / closed ------------------------------------------------------

    def is_expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, on: bool) -> None:
        on = bool(on)
        if on == self._expanded:
            return
        self._expanded = on
        self._apply_expanded()
        self.expanded_changed.emit(on)

    def _apply_expanded(self) -> None:
        on = self._expanded
        self.table.setVisible(on)
        self._hint.setVisible(on)
        # Painted, not a glyph: a right- or down-pointing triangle.
        self._chevron.setPixmap(_chevron_pixmap(on, _T()["fg_muted"]))
        self._strip.setToolTip(
            "Hide the list" if on else
            "Show the design's setup problems - what was skipped, left out "
            "of the solve or can't be trusted, and where")
        if on:
            self.setMinimumHeight(STRIP_HEIGHT_PX + _MIN_OPEN_PX)
            self.setMaximumHeight(_QWIDGETSIZE_MAX)
            self._fit_timer.start()
        else:
            self.setFixedHeight(STRIP_HEIGHT_PX)

    def _on_filter_clicked(self) -> None:
        self._rebuild()
        # A count is also the way in: clicking one opens the list.
        if not self._expanded:
            self.set_expanded(True)

    # --- actions ------------------------------------------------------------

    def _selected(self) -> list[DesignIssue]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._rows[r] for r in rows if 0 <= r < len(self._rows)]

    def _on_double_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._rows):
            self.jump_requested.emit(self._rows[row])

    def copy_selected(self, *, whole_rows: bool = False) -> str:
        """Put the selected issues on the clipboard: their descriptions, or
        tab-separated rows (severity, issue, where, description). Returns
        the text."""
        chosen = self._selected()
        if whole_rows:
            lines = ["\t".join((_SEVERITY_WORD.get(i.severity, ""), i.label,
                                i.where, i.message)) for i in chosen]
        else:
            lines = [i.message for i in chosen]
        text = "\n".join(lines)
        if text:
            QApplication.clipboard().setText(text)
        return text

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        if not self.table.item(row, _COL_TEXT).isSelected():
            self.table.selectRow(row)
        menu = QMenu(self.table)
        go = menu.addAction("Go To")
        go.setEnabled(len(self._selected()) == 1)
        menu.addSeparator()
        copy = menu.addAction("Copy")
        copy.setShortcut(QKeySequence.Copy)
        copy_row = menu.addAction("Copy Row")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is go:
            self.jump_requested.emit(self._rows[row])
        elif chosen is copy:
            self.copy_selected()
        elif chosen is copy_row:
            self.copy_selected(whole_rows=True)

    # --- look ---------------------------------------------------------------

    def restyle(self) -> None:
        t = _T()
        self._strip.setStyleSheet(
            f"QFrame#fypaIssuesStrip {{ background: {t['bg_header']};"
            f" border-top: 1px solid {t['border']}; }}"
            f"QFrame#fypaIssuesStrip:hover {{ background: {t['bg_hover']}; }}"
            f"QLabel {{ color: {t['fg_strong']}; background: transparent; }}"
            "QToolButton { border: 1px solid transparent; border-radius: 3px;"
            f" padding: 1px 6px; color: {t['fg']}; background: transparent; }}"
            f"QToolButton:checked {{ border-color: {t['border']};"
            f" background: {t['bg']}; }}"
            f"QToolButton:hover:enabled {{ border-color: {t['accent']}; }}"
            f"QToolButton:disabled {{ color: {t['fg_dim']}; }}")
        self._hint.setStyleSheet(
            f"QLabel {{ color: {t['fg_muted']}; background: transparent; }}")
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {t['bg']}; color: {t['fg']};"
            f" alternate-background-color: {t['bg_alt']}; border: none; }}"
            f"QHeaderView::section {{ background-color: {t['bg_header']};"
            f" color: {t['fg_strong']}; padding: 3px 6px;"
            f" border: none; border-bottom: 1px solid {t['border']}; }}"
            f"QTableWidget::item {{ padding: 3px 6px; }}"
            f"QTableWidget::item:selected {{"
            f" background-color: {t['bg_selection']}; }}")
        for severity, btn in self._filter_btns.items():
            btn.setIcon(severity_icon(severity, 14))
        self._chevron.setPixmap(
            _chevron_pixmap(self._expanded, t["fg_muted"]))
        if self._issues:
            self._rebuild()


def _chevron_pixmap(open_: bool, color: str, size: int = 10) -> QPixmap:
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.setDevicePixelRatio(scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    s = float(size)
    if open_:
        tri = [QPointF(s * 0.1, s * 0.25), QPointF(s * 0.9, s * 0.25),
               QPointF(s * 0.5, s * 0.8)]
    else:
        tri = [QPointF(s * 0.25, s * 0.1), QPointF(s * 0.8, s * 0.5),
               QPointF(s * 0.25, s * 0.9)]
    p.drawPolygon(QPolygonF(tri))
    p.end()
    return pm


# --- the pop-up ----------------------------------------------------------------

def issue_summary_text(issues) -> tuple[str, str]:
    """``(headline, detail)`` of the one pop-up that says the design has
    issues and where they are listed."""
    counts = count_by_severity(issues)
    headline = f"This design has {summary_text(issues)}."
    detail = ("They are listed in the Design issues panel along the bottom "
              "of the window, on every tab. Double-click one there to go to "
              "the part or rail it is about.")
    if counts[ERROR]:
        detail = ("An error means part of the design was left out of the "
                  "solve, or a result can't be trusted. ") + detail
    return headline, detail


def build_issue_summary_box(window, issues):
    """``(box, show button)``: the pop-up, built but not shown."""
    counts = count_by_severity(issues)
    headline, detail = issue_summary_text(issues)
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Critical if counts[ERROR]
                else QMessageBox.Icon.Warning)
    box.setWindowTitle("Design issues")
    box.setText(headline)
    box.setInformativeText(detail)
    show = box.addButton("Show Issues", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.setDefaultButton(show)
    # A message box ignores a minimum width of its own; a spacer across the
    # bottom of its grid is what widens it.
    lay = box.layout()
    if isinstance(lay, QGridLayout):
        lay.addItem(QSpacerItem(POPUP_WIDTH_PX, 0, QSizePolicy.Minimum,
                                QSizePolicy.Minimum),
                    lay.rowCount(), 0, 1, lay.columnCount())
    return box, show


def show_issue_summary(window, issues) -> bool:
    """The pop-up itself. True when the user chose to open the list. Does
    nothing on a display nobody can click (a script, a CI run), where a
    modal would never be dismissed."""
    from fypa.viewer.project_open import _headless_platform
    if _headless_platform():
        return False
    box, show = build_issue_summary_box(window, issues)
    box.exec()
    return box.clickedButton() is show


# --- the window's side ---------------------------------------------------------

class _DesignIssuesMixin:
    """Keeps the panel in step with the design on screen, shows the pop-up,
    and takes the user to what an issue is about. See the module doc."""

    _issues_panel: DesignIssuesPanel | None = None
    _issues_split = None
    _design_issues: tuple[DesignIssue, ...] = ()
    # Errors and warnings the pop-up has already told the user about.
    _issues_announced: frozenset = frozenset()
    _issues_open_px: int = _DEFAULT_OPEN_PX

    def _build_issues_panel(self, parent) -> DesignIssuesPanel:
        panel = DesignIssuesPanel(parent)
        panel.jump_requested.connect(self._on_issue_jump)
        panel.expanded_changed.connect(self._on_issues_expanded)
        self._issues_panel = panel
        return panel

    def _refresh_design_issues(self, *, announce: bool = False,
                               new_design: bool = False) -> None:
        """Re-read the issues from the metadata on screen.

        ``announce``: a load or a solve just finished - pop the summary up
        if it brought an error or a warning the user has not been told
        about. ``new_design``: forget what was announced for the previous
        design."""
        from fypa.viewer.prefs import load_show_bridge_notice

        md = getattr(self, "metadata", None)
        try:
            bridges = bool(load_show_bridge_notice())
        except Exception:  # noqa: BLE001 - a preference, never a failure
            bridges = True
        issues = collect_design_issues(
            md if isinstance(md, dict) else None,
            bridges_as_warnings=bridges)
        changed = tuple(issues) != self._design_issues
        self._design_issues = tuple(issues)
        if changed:
            # The Topology blocks carry the same issues as icons.
            redraw = getattr(self, "_invalidate_topology", None)
            if redraw is not None:
                redraw()
        panel = self._issues_panel
        if panel is not None:
            panel.set_issues(issues)
        problems = frozenset(i.key for i in issues if i.severity != NOTE)
        if new_design:
            self._issues_announced = frozenset()
        if not announce:
            # Fixed ones can be announced again if they come back.
            self._issues_announced = self._issues_announced & problems
            return
        fresh = problems - self._issues_announced
        self._issues_announced = problems
        if fresh and panel is not None:
            if show_issue_summary(self, issues):
                panel.set_expanded(True)

    def _on_issues_expanded(self, on: bool) -> None:
        """Give the opened list its height (the one the user last left it
        at), and remember it when it closes."""
        split, panel = self._issues_split, self._issues_panel
        if split is None or panel is None:
            return
        sizes = split.sizes()
        if len(sizes) != 2:
            return
        total = sum(sizes)
        if on:
            want = STRIP_HEIGHT_PX + max(_MIN_OPEN_PX, self._issues_open_px)
            want = min(want, max(STRIP_HEIGHT_PX + _MIN_OPEN_PX,
                                 int(total * 0.6)))
            split.setSizes([max(0, total - want), want])
        else:
            if sizes[1] > STRIP_HEIGHT_PX + _MIN_OPEN_PX // 2:
                self._issues_open_px = sizes[1] - STRIP_HEIGHT_PX
            split.setSizes([max(0, total - STRIP_HEIGHT_PX),
                            STRIP_HEIGHT_PX])

    # --- going to an issue --------------------------------------------------

    def _issue_target_designators(self, issue: DesignIssue) -> list[str]:
        """Parts to show for ``issue``: the ones it names, else the sources,
        sinks and regulators on the rail it names."""
        if issue.designators:
            return list(issue.designators)
        md = self.metadata if isinstance(self.metadata, dict) else {}
        by_des = issues_by_designator([issue], md.get("directives") or [])
        return list(by_des)

    def _on_issue_jump(self, issue: DesignIssue) -> None:
        if issue.kind == "mesh-failed":
            from fypa.viewer.diagnostics import _apply_mesh_failure_highlights
            self.tabs.setCurrentIndex(self._heatmap_tab_index)
            _apply_mesh_failure_highlights(self)
            return
        if issue.kind == "setup-held":
            from fypa.viewer.setup_restore import open_setup_review
            open_setup_review(self)
            return
        targets = [d.upper() for d in self._issue_target_designators(issue)]
        if targets and self._issue_jump_topology(targets):
            return
        if targets and self._issue_jump_board(targets):
            return
        self.statusBar().showMessage(
            "This issue names no part or rail on the board to go to.", 6000)

    def _issue_jump_topology(self, targets: list[str]) -> bool:
        """On the Topology tab, pan to the block. False when another tab is
        open or the diagram has no such block."""
        if self.tabs.currentIndex() != getattr(self, "_topology_tab_index", -1):
            return False
        model = getattr(self, "_topology_model", None)
        view = getattr(self, "_topology_view", None)
        if model is None or view is None:
            return False
        for des in targets:
            for node in model.nodes:
                if str(node.designator or "").upper() == des:
                    view.go_to((node.x + node.width / 2.0,
                                node.y + node.height / 2.0))
                    return True
        return False

    def _issue_jump_board(self, targets: list[str]) -> bool:
        """Switch to the Board and zoom to the part: a pin of its directive,
        else the middle of its footprint."""
        from fypa.topology.metadata.specs import jump_row_for_directive

        md = self.metadata if isinstance(self.metadata, dict) else {}
        directives = md.get("directives") or []
        for des in targets:
            for d in directives:
                names = {str(d.get("designator") or "").upper(),
                         str(d.get("label") or "").upper()}
                if des in names:
                    row = jump_row_for_directive(d)
                    if row is not None:
                        self._jump_to_node(row)
                        return True
        for des in targets:
            for comp in md.get("components") or []:
                if (isinstance(comp, dict)
                        and str(comp.get("designator") or "").upper() == des):
                    box = comp.get("bbox") or []
                    if len(box) == 4:
                        self._jump_to_xy((box[0] + box[2]) / 2.0,
                                         (box[1] + box[3]) / 2.0, [])
                        return True
        return False


__all__ = [
    "DesignIssuesPanel", "STRIP_HEIGHT_PX", "issue_summary_text",
    "severity_icon", "show_issue_summary",
]
