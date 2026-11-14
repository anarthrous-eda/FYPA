"""Excel-style per-column filters for the viewer's QTableWidgets.

A :class:`TableColumnFilter` swaps the table's horizontal header for a
:class:`FilterHeaderView`, which draws a small funnel button at the right of
every filterable section. Clicking the section's label still sorts, exactly as
before; clicking the funnel (or right-clicking anywhere on the header) opens a
popup with:

* sort ascending / descending,
* a Min / Max range for numeric columns,
* a searchable checklist of the column's distinct values, with row counts.

The controller owns no rows and hides none itself. Each tab already has an
``_apply_*_filter`` that walks the rows and calls ``setRowHidden`` for its own
filter bar (rail combo, warnings-only, …); it ANDs in :meth:`accepts` and
connects :attr:`changed` back to itself, so there is one place that decides
whether a row is shown.

Filter state is keyed by column index and holds the values *excluded* rather
than the values allowed, so a repopulate that introduces a new value (a
re-solve, an override) shows it instead of silently hiding it.
"""
from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
)

from fypa.viewer.numeric import _numeric_validator, _parse_numeric_text
from fypa.viewer.theme import _T


# Width reserved at the right of each filterable header section for the
# funnel button. Added to the section's size hint so resizeColumnsToContents
# leaves room for it rather than drawing it over the label.
_BUTTON_W = 16

# Label shown in the checklist for an empty / "-" cell.
BLANK_LABEL = "(Blanks)"

# A numeric column with more distinct values than this lists none of them -
# a checklist of 3 000 via X coordinates is noise; Min / Max is the tool.
_NUMERIC_LIST_LIMIT = 60

_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass
class _ColumnState:
    excluded: set[str] = field(default_factory=set)
    lo: float | None = None
    hi: float | None = None

    def active(self) -> bool:
        return bool(self.excluded) or self.lo is not None or self.hi is not None


class FilterHeaderView(QHeaderView):
    """Horizontal header with a funnel button per section.

    A press on the funnel emits :attr:`filterRequested` and is swallowed, so
    it never reaches QHeaderView's click-to-sort. A right-click anywhere on a
    section opens the same popup.
    """

    filterRequested = Signal(int, QPoint)   # logical column, global anchor

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self._active: set[int] = set()
        self._skip: set[int] = set()
        self._swallow_release = False

    def set_skip_columns(self, cols) -> None:
        self._skip = set(cols)
        self.viewport().update()

    def set_active_columns(self, cols) -> None:
        self._active = set(cols)
        self.viewport().update()

    def _section_rect(self, logical: int) -> QRect:
        return QRect(self.sectionViewportPosition(logical), 0,
                     self.sectionSize(logical), self.height())

    @staticmethod
    def _button_rect(section: QRect) -> QRect:
        return QRect(section.right() - _BUTTON_W + 1, section.top(),
                     _BUTTON_W, section.height())

    def sectionSizeFromContents(self, logical: int) -> QSize:
        size = super().sectionSizeFromContents(logical)
        if logical not in self._skip:
            size.setWidth(size.width() + _BUTTON_W)
        return size

    def paintSection(self, painter: QPainter, rect: QRect, logical: int) -> None:
        painter.save()
        super().paintSection(painter, rect, logical)
        painter.restore()
        if logical in self._skip or not rect.isValid():
            return
        btn = self._button_rect(rect)
        cx, cy = btn.center().x() - 1, btn.center().y()
        # Funnel: a triangle over a short stem, ~8 px wide.
        poly = QPolygonF([QPointF(cx - 4, cy - 4), QPointF(cx + 4, cy - 4),
                          QPointF(cx + 1, cy), QPointF(cx + 1, cy + 4),
                          QPointF(cx - 1, cy + 4), QPointF(cx - 1, cy)])
        t = _T()
        active = logical in self._active
        color = QColor(t["accent"] if active else t["fg_muted"])
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(color, 1.0))
        painter.setBrush(color if active else Qt.NoBrush)
        painter.drawPolygon(poly)
        painter.restore()

    def _hit_button(self, pos: QPoint) -> int:
        logical = self.logicalIndexAt(pos)
        if logical < 0 or logical in self._skip:
            return -1
        if self._button_rect(self._section_rect(logical)).contains(pos):
            return logical
        return -1

    def _anchor(self, logical: int) -> QPoint:
        rect = self._section_rect(logical)
        return self.viewport().mapToGlobal(
            QPoint(max(rect.left(), rect.right() - 260), rect.bottom() + 1))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            logical = self._hit_button(event.position().toPoint())
            if logical >= 0:
                self._swallow_release = True
                event.accept()
                self.filterRequested.emit(logical, self._anchor(logical))
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        # The press opened the popup; letting the release through would make
        # QHeaderView treat it as a click and re-sort the table as well.
        if self._swallow_release:
            self._swallow_release = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        logical = self.logicalIndexAt(event.pos())
        if logical < 0 or logical in self._skip:
            super().contextMenuEvent(event)
            return
        event.accept()
        self.filterRequested.emit(logical, event.globalPos())


class TableColumnFilter(QObject):
    """Per-column filters for one QTableWidget.

    ``numeric_cols`` get a Min / Max range and sort their checklist by value.
    ``skip_cols`` (action cells such as "Go ▶") get no funnel and no filter.
    Checkable cells are keyed by their tick state, not their (empty) text.
    """

    changed = Signal()

    def __init__(self, table: QTableWidget, *, numeric_cols=(),
                 skip_cols=()) -> None:
        super().__init__(table)
        self._table = table
        self._numeric = set(numeric_cols)
        self._skip = set(skip_cols)
        self._states: dict[int, _ColumnState] = {}
        self._header = FilterHeaderView(table)
        self._header.set_skip_columns(self._skip)
        table.setHorizontalHeader(self._header)
        self._header.filterRequested.connect(self._open_popup)
        self._clear_buttons: list[QPushButton] = []

    # --- cell values -------------------------------------------------------

    def cell_key(self, row: int, col: int) -> str:
        item = self._table.item(row, col)
        if item is None:
            return BLANK_LABEL
        # A check state is only present once setCheckState was called - the
        # ItemIsUserCheckable flag is on for every QTableWidgetItem by default.
        if item.data(Qt.CheckStateRole) is not None:
            return "Ticked" if item.checkState() == Qt.Checked else "Not ticked"
        text = item.text().strip()
        return text if text and text != "-" else BLANK_LABEL

    def cell_number(self, row: int, col: int) -> float | None:
        item = self._table.item(row, col)
        if item is None:
            return None
        # Sort keys live on UserRole (_MessagesSortItem) or DisplayRole
        # (setData(EditRole, float)); fall back to the first number in the
        # text, which skips the "~" / "≥" / "✎" decorations.
        for role in (Qt.UserRole, Qt.DisplayRole):
            v = item.data(role)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v) if math.isfinite(v) else None
        m = _NUMBER_RE.search(item.text())
        return float(m.group()) if m else None

    # --- filtering ---------------------------------------------------------

    def is_active(self) -> bool:
        return any(s.active() for s in self._states.values())

    def accepts(self, row: int, *, ignore_col: int | None = None) -> bool:
        """Whether ``row`` passes every column filter (bar ``ignore_col``)."""
        for col, st in self._states.items():
            if col == ignore_col or not st.active():
                continue
            if st.excluded and self.cell_key(row, col) in st.excluded:
                return False
            if st.lo is not None or st.hi is not None:
                v = self.cell_number(row, col)
                if v is None:
                    return False
                if st.lo is not None and v < st.lo:
                    return False
                if st.hi is not None and v > st.hi:
                    return False
        return True

    def clear_all(self) -> None:
        if not self._states:
            return
        self._states.clear()
        self._emit_changed()

    def clear_column(self, col: int) -> None:
        if self._states.pop(col, None) is not None:
            self._emit_changed()

    def set_state(self, col: int, *, excluded=None, lo=None, hi=None) -> None:
        st = _ColumnState(set(excluded or ()), lo, hi)
        if st.active():
            self._states[col] = st
        else:
            self._states.pop(col, None)
        self._emit_changed()

    def make_clear_button(self, text: str = "Clear column filters") -> QPushButton:
        """A button that clears every column filter, enabled only while one
        is set - so the filter bar says when rows are hidden by the header."""
        btn = QPushButton(text)
        btn.setToolTip("Remove every filter set from the column headers. "
                       "Click a header's funnel (or right-click the header) "
                       "to filter that column.")
        btn.clicked.connect(self.clear_all)
        btn.setEnabled(self.is_active())
        self._clear_buttons.append(btn)
        return btn

    def _emit_changed(self) -> None:
        active = {c for c, s in self._states.items() if s.active()}
        self._header.set_active_columns(active)
        for btn in self._clear_buttons:
            btn.setEnabled(bool(active))
        self.changed.emit()

    # --- popup -------------------------------------------------------------

    def _open_popup(self, col: int, anchor: QPoint) -> None:
        if col in self._skip:
            return
        popup = _FilterPopup(self, col, self._table)
        popup.adjustSize()
        screen = (QGuiApplication.screenAt(anchor)
                  or QGuiApplication.primaryScreen())
        pos = QPoint(anchor)
        if screen is not None:
            avail = screen.availableGeometry()
            pos.setX(max(avail.left(),
                         min(pos.x(), avail.right() - popup.width())))
            pos.setY(max(avail.top(),
                         min(pos.y(), avail.bottom() - popup.height())))
        popup.move(pos)
        popup.show()
        popup.focus_default()


class _FilterPopup(QFrame):
    """The drop-down for one column: sort, range, searchable value list."""

    def __init__(self, ctrl: TableColumnFilter, col: int,
                 table: QTableWidget) -> None:
        super().__init__(table, Qt.Popup)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFrameShape(QFrame.StyledPanel)
        self._ctrl = ctrl
        self._col = col
        self._table = table
        self._numeric = col in ctrl._numeric
        self._syncing = False
        state = ctrl._states.get(col, _ColumnState())

        t = _T()
        self.setObjectName("filterPopup")
        self.setStyleSheet(
            f"QFrame#filterPopup {{ background-color: {t['bg']}; "
            f"color: {t['fg']}; border: 1px solid {t['border']}; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        header_item = table.horizontalHeaderItem(col)
        title = QLabel(
            f"<b>{html.escape(header_item.text() if header_item else '')}</b>")
        lay.addWidget(title)

        sort_row = QHBoxLayout()
        asc = QPushButton("Sort ascending" if self._numeric else "Sort A → Z")
        desc = QPushButton("Sort descending" if self._numeric else "Sort Z → A")
        asc.clicked.connect(lambda: self._sort(Qt.AscendingOrder))
        desc.clicked.connect(lambda: self._sort(Qt.DescendingOrder))
        sort_row.addWidget(asc)
        sort_row.addWidget(desc)
        lay.addLayout(sort_row)

        # Values present in rows that pass every *other* column filter - so,
        # as in Excel, filtering Net first narrows what the next column lists.
        counts: dict[str, int] = {}
        numbers: dict[str, float | None] = {}
        for r in range(table.rowCount()):
            if not ctrl.accepts(r, ignore_col=col):
                continue
            key = ctrl.cell_key(r, col)
            counts[key] = counts.get(key, 0) + 1
            if self._numeric and key not in numbers:
                numbers[key] = ctrl.cell_number(r, col)
        self._listed = set(counts)

        self._lo_edit = self._hi_edit = None
        if self._numeric:
            range_row = QHBoxLayout()
            range_row.addWidget(QLabel("Min"))
            self._lo_edit = QLineEdit(
                "" if state.lo is None else f"{state.lo:g}")
            range_row.addWidget(self._lo_edit)
            range_row.addWidget(QLabel("Max"))
            self._hi_edit = QLineEdit(
                "" if state.hi is None else f"{state.hi:g}")
            range_row.addWidget(self._hi_edit)
            for edit in (self._lo_edit, self._hi_edit):
                edit.setValidator(_numeric_validator(edit, bottom=None))
                edit.setPlaceholderText("any")
                edit.setFixedWidth(80)
                edit.returnPressed.connect(self._accept)
            lay.addLayout(range_row)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search values…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.returnPressed.connect(self._accept)

        self._list = QListWidget()
        self._list.setMinimumWidth(240)
        # The Bridges "Why / impact" values are whole sentences; scroll them
        # rather than open a popup as wide as the screen.
        self.setMaximumWidth(480)
        self._list.setMinimumHeight(180)
        self._list.setUniformItemSizes(True)
        self._all_item: QListWidgetItem | None = None

        show_list = not (self._numeric and len(counts) > _NUMERIC_LIST_LIMIT)
        if show_list:
            lay.addWidget(self._search)

            def _order(key: str):
                if key == BLANK_LABEL:
                    return (2, 0.0, "")
                if self._numeric:
                    v = numbers.get(key)
                    return (0 if v is not None else 1, v or 0.0, key.lower())
                return (0, 0.0, key.lower())

            self._syncing = True
            self._all_item = QListWidgetItem("(Select all)")
            self._all_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            self._list.addItem(self._all_item)
            for key in sorted(counts, key=_order):
                it = QListWidgetItem(f"{key}    ({counts[key]})")
                it.setData(Qt.UserRole, key)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked if key in state.excluded
                                 else Qt.Checked)
                self._list.addItem(it)
            self._syncing = False
            self._sync_all_item()
            self._list.itemChanged.connect(self._on_item_changed)
            lay.addWidget(self._list, 1)
        else:
            note = QLabel(f"{len(counts)} distinct values - use Min / Max.")
            note.setStyleSheet(f"QLabel {{ color: {t['fg_muted']}; }}")
            lay.addWidget(note)

        btn_row = QHBoxLayout()
        clear = QPushButton("Clear filter")
        clear.setEnabled(state.active())
        clear.clicked.connect(self._clear)
        btn_row.addWidget(clear)
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        lay.addLayout(btn_row)

    def focus_default(self) -> None:
        if self._lo_edit is not None:
            self._lo_edit.setFocus()
        elif self._all_item is not None:
            self._search.setFocus()

    def _value_items(self):
        for i in range(1, self._list.count()):
            yield self._list.item(i)

    def _sync_all_item(self) -> None:
        if self._all_item is None:
            return
        visible = [it for it in self._value_items() if not it.isHidden()]
        n_checked = sum(it.checkState() == Qt.Checked for it in visible)
        self._syncing = True
        if visible and n_checked == len(visible):
            self._all_item.setCheckState(Qt.Checked)
        elif n_checked == 0:
            self._all_item.setCheckState(Qt.Unchecked)
        else:
            self._all_item.setCheckState(Qt.PartiallyChecked)
        self._syncing = False

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._syncing:
            return
        if item is self._all_item:
            # Tri-state clicks cycle through PartiallyChecked; treat anything
            # but a clear as "tick every visible value".
            target = (Qt.Unchecked if item.checkState() == Qt.Unchecked
                      else Qt.Checked)
            self._syncing = True
            for it in self._value_items():
                if not it.isHidden():
                    it.setCheckState(target)
            self._syncing = False
        self._sync_all_item()

    def _on_search(self, text: str) -> None:
        needle = text.strip().lower()
        for it in self._value_items():
            it.setHidden(bool(needle)
                         and needle not in str(it.data(Qt.UserRole)).lower())
        self._sync_all_item()

    def _sort(self, order) -> None:
        self._table.sortByColumn(self._col, order)
        self.close()

    def _clear(self) -> None:
        self._ctrl.clear_column(self._col)
        self.close()

    @staticmethod
    def _bound(edit: QLineEdit | None) -> float | None:
        if edit is None or not edit.text().strip():
            return None
        try:
            return _parse_numeric_text(edit.text())
        except ValueError:
            return None

    def _accept(self) -> None:
        old = self._ctrl._states.get(self._col, _ColumnState())
        # Values not listed (hidden by another column's filter) keep whatever
        # exclusion they had; listed values take the checklist's state.
        excluded = set(old.excluded) - self._listed
        if self._all_item is not None:
            searching = bool(self._search.text().strip())
            for it in self._value_items():
                key = it.data(Qt.UserRole)
                # With a search typed, Excel semantics: keep only the
                # matching values that are ticked.
                if it.checkState() != Qt.Checked or (searching and it.isHidden()):
                    excluded.add(key)
        lo, hi = self._bound(self._lo_edit), self._bound(self._hi_edit)
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        self._ctrl.set_state(self._col, excluded=excluded, lo=lo, hi=hi)
        self.close()
