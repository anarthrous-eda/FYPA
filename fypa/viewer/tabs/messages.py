"""The Messages tab."""
from __future__ import annotations

import logging
from fypa import log_buffer
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.theme import _T


class _MessagesSortItem(QTableWidgetItem):
    """QTableWidgetItem that decouples sort key from displayed text.

    QTableWidgetItem aliases ``Qt::EditRole`` to ``Qt::DisplayRole``
    internally (Qt source: ``role = (role == Qt::EditRole ? Qt::DisplayRole
    : role)`` inside ``setData``), so the usual pattern of
    ``setData(Qt.EditRole, sort_key)`` overwrites the cell's display text
    with the sort key — fine when the sort key happens to be the value
    you wanted to show, but in the Messages tab we need the cell to show
    an emoji or a formatted timestamp while still sorting by an int level
    or a float epoch.

    The fix is to stash the sort key on ``Qt::UserRole`` (which Qt leaves
    alone) and override ``__lt__`` so QTableWidget's sort comparison
    uses that key instead of falling back to the display string. If the
    other item happens to be a plain QTableWidgetItem (no UserRole set),
    we defer to the base comparison so the table doesn't crash on a
    mixed column."""

    def __lt__(self, other) -> bool:
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole) if isinstance(other, QTableWidgetItem) else None
        if a is not None and b is not None:
            try:
                return a < b
            except TypeError:
                pass
        return super().__lt__(other)


class _MessagesTabMixin:
    """The Messages tab."""

    # --- Messages tab ------------------------------------------------------

    # Columns of the Messages-tab table. (display label, numeric?) — the
    # numeric flag is informational; sort keys are carried on the cell's
    # Qt.UserRole via :class:`_MessagesSortItem` so the Time column sorts
    # by epoch and the Level column by logging level number rather than
    # by the displayed string. (Setting Qt.EditRole on a QTableWidgetItem
    # would clobber the DisplayRole text — Qt aliases EditRole to
    # DisplayRole for this class — which is why we don't go that route.)
    _MESSAGES_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = (
        ("Time",    True),
        ("Level",   True),
        ("Source",  False),
        ("Message", False),
    )

    # Which level checkboxes the filter row exposes, in display order.
    # Each entry is (label, level_no). DEBUG is included so that runs
    # launched with ``FYPA -d`` can still narrow the table down. Labels
    # carry the same glyph the Level cell uses so users can match a row
    # to its filter at a glance.
    _MESSAGES_FILTER_LEVELS: tuple[tuple[str, int], ...] = (
        ("❌ Errors",   logging.ERROR),
        ("⚠ Warnings", logging.WARNING),
        ("ℹ Info",     logging.INFO),
        ("🐛 Debug",    logging.DEBUG),
    )

    @staticmethod
    def _messages_level_glyph(level: int) -> str:
        """Glyph shown in the Level cell instead of the raw level name.
        Bands rather than per-int lookups so CRITICAL (>= ERROR) and any
        custom levels in between still pick up sensible icons. The full
        level name remains accessible via the cell's tooltip."""
        if level >= logging.CRITICAL:
            return "🛑"
        if level >= logging.ERROR:
            return "❌"
        if level >= logging.WARNING:
            return "⚠"
        if level >= logging.INFO:
            return "ℹ"
        return "🐛"

    def _build_messages_tab(self) -> QWidget:
        """Build the Messages tab — a sortable, filterable table of every
        log record captured since the process started (see
        :mod:`fypa.log_buffer`). Errors, warnings, and info messages are
        all routed here so the user can audit what the loader / solver /
        editor reported without having to open the log file.

        New records arrive on whichever thread emitted them; the bridge
        :class:`QObject` re-emits via a queued signal so the row append
        always lands on the GUI thread."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        # Filter bar — one checkbox per level, plus an Auto-scroll toggle
        # and a Clear button. The Clear button drops every buffered
        # record (process-wide) so the table reflects the current state
        # rather than the cumulative session log.
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self._messages_level_boxes: dict[int, QCheckBox] = {}
        for label, level_no in self._MESSAGES_FILTER_LEVELS:
            box = QCheckBox(label)
            box.setChecked(True)
            box.toggled.connect(self._apply_messages_filter)
            filter_row.addWidget(box)
            self._messages_level_boxes[level_no] = box

        filter_row.addSpacing(12)
        self._messages_autoscroll_box = QCheckBox("Auto-scroll")
        self._messages_autoscroll_box.setChecked(True)
        self._messages_autoscroll_box.setToolTip(
            "Scroll the table to the newest message whenever a new record "
            "arrives. Disable if you want to inspect older rows without "
            "the view jumping under you."
        )
        filter_row.addWidget(self._messages_autoscroll_box)

        filter_row.addStretch(1)
        self.messages_summary_label = QLabel("")
        self.messages_summary_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; }}"
        )
        filter_row.addWidget(self.messages_summary_label)

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip(
            "Drop every buffered log record. The file log (fypa.log) is "
            "not affected."
        )
        clear_btn.clicked.connect(self._on_messages_clear)
        filter_row.addWidget(clear_btn)
        outer.addLayout(filter_row)

        # Table.
        self.messages_table = QTableWidget()
        cols = self._MESSAGES_TABLE_COLUMNS
        self.messages_table.setColumnCount(len(cols))
        self.messages_table.setHorizontalHeaderLabels([c[0] for c in cols])
        self.messages_table.setSortingEnabled(True)
        self.messages_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.messages_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.messages_table.setAlternatingRowColors(True)
        self.messages_table.verticalHeader().setVisible(False)
        # Stretch the Message column so long lines use the remaining width.
        header = self.messages_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        # Theme-driven styling — matches the Nodes / Vias tables.
        _t = _T()
        self.messages_table.setStyleSheet(
            f"QTableWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"               gridline-color: {_t['gridline']};"
            f"               alternate-background-color: {_t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
            f"                       padding: 4px; border: 1px solid {_t['border']}; }}"
            f"QTableWidget::item:selected {{ background-color: {_t['bg_selection']}; }}"
        )
        outer.addWidget(self.messages_table, 1)

        # Seed the table with everything already in the buffer (e.g. solve
        # warnings emitted before the viewer was constructed) and wire up
        # the listener for future records. Listener uses a queued signal
        # because log records may come from worker threads.
        self._install_messages_listener()
        self._populate_messages_table()
        return widget

    def _install_messages_signaller(self) -> None:
        """Create the QObject bridge that re-emits log records on the GUI
        thread. Idempotent — called both from :meth:`_install_messages_listener`
        and during the theme-driven tab rebuild so a fresh tab inherits
        the existing buffer feed."""
        if getattr(self, "_messages_signaller", None) is not None:
            return

        class _MessagesSignaller(QObject):
            recordReady = Signal(object)

        self._messages_signaller = _MessagesSignaller(self)
        # Qt.QueuedConnection: log records may fire on worker threads
        # (solve worker, mesher pool callbacks). Queueing marshals the
        # slot onto the GUI thread so QTableWidget mutations are safe.
        self._messages_signaller.recordReady.connect(
            self._on_messages_record, Qt.QueuedConnection,
        )

    def _install_messages_listener(self) -> None:
        """Register the per-viewer listener with the global log buffer.
        Safe to call repeatedly — the buffer dedupes by callable identity."""
        self._install_messages_signaller()
        signaller = self._messages_signaller
        if getattr(self, "_messages_listener", None) is None:
            # Bound closure that the log buffer can call from any thread.
            def _listener(rec: log_buffer.MessageRecord) -> None:
                # signaller may have been GC'd if the viewer is closing.
                try:
                    signaller.recordReady.emit(rec)
                except RuntimeError:
                    # Underlying QObject destroyed — listener is stale;
                    # remove ourselves so the buffer stops calling us.
                    log_buffer.remove_listener(_listener)
            self._messages_listener = _listener
        log_buffer.add_listener(self._messages_listener)

    def _populate_messages_table(self) -> None:
        """Rebuild the table from the global log buffer + the current
        filter. Called once at construction and again whenever the filter
        or the underlying buffer changes wholesale (e.g. after Clear)."""
        table = getattr(self, "messages_table", None)
        if table is None:
            return
        records = log_buffer.records()
        # Snapshot allowed levels from the checkboxes so the loop doesn't
        # re-check the widgets per row.
        allowed = self._messages_allowed_levels()

        table.setSortingEnabled(False)
        table.setRowCount(0)
        visible = 0
        for rec in records:
            if rec.level not in allowed:
                continue
            row = table.rowCount()
            table.setRowCount(row + 1)
            self._fill_messages_row(row, rec)
            visible += 1
        table.setSortingEnabled(True)
        # Default sort: newest first (Time descending). The user can
        # click any header to override.
        table.sortByColumn(0, Qt.DescendingOrder)
        # One-shot column-resize so timestamps + level + source fit
        # without truncating; the message column has stretch turned on
        # so it absorbs whatever's left.
        table.resizeColumnToContents(0)
        table.resizeColumnToContents(1)
        table.resizeColumnToContents(2)
        self._update_messages_summary(visible, len(records))

    def _on_messages_record(self, rec: log_buffer.MessageRecord) -> None:
        """Slot for the signaller — runs on the GUI thread thanks to the
        QueuedConnection. Appends one row (if the filter accepts it) and
        updates the summary label."""
        table = getattr(self, "messages_table", None)
        if table is None:
            return
        total = len(log_buffer.records())
        allowed = self._messages_allowed_levels()
        if rec.level in allowed:
            table.setSortingEnabled(False)
            row = table.rowCount()
            table.setRowCount(row + 1)
            self._fill_messages_row(row, rec)
            table.setSortingEnabled(True)
            # Preserve the user's chosen sort order by re-sorting after
            # the append. Without this, the new row stays at the bottom
            # regardless of the current sort column.
            header = table.horizontalHeader()
            table.sortByColumn(header.sortIndicatorSection(),
                               header.sortIndicatorOrder())
            if (getattr(self, "_messages_autoscroll_box", None) is not None
                    and self._messages_autoscroll_box.isChecked()):
                table.scrollToBottom()
            visible = sum(
                1 for r in range(table.rowCount())
                if not table.isRowHidden(r)
            )
            self._update_messages_summary(visible, total)
        else:
            # Record didn't pass the filter — only the totals change.
            visible = sum(
                1 for r in range(table.rowCount())
                if not table.isRowHidden(r)
            )
            self._update_messages_summary(visible, total)

    def _fill_messages_row(self, row: int,
                           rec: log_buffer.MessageRecord) -> None:
        """Populate one row's four cells from a :class:`MessageRecord`.
        Adds per-level colour to the Level cell so errors/warnings pop
        without needing to read the column text."""
        _t = _T()
        # Time cell — displays the full local-time stamp, but sort key
        # is the raw epoch so a roll past midnight (or a re-ordering
        # after Clear) still sorts chronologically. Sort key lives on
        # UserRole rather than EditRole because QTableWidgetItem aliases
        # EditRole to DisplayRole and would clobber the formatted text;
        # _MessagesSortItem.__lt__ reads UserRole instead.
        time_item = _MessagesSortItem(log_buffer.format_timestamp(rec.ts))
        time_item.setData(Qt.UserRole, float(rec.ts))
        time_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.messages_table.setItem(row, 0, time_item)

        level_item = _MessagesSortItem(self._messages_level_glyph(rec.level))
        # Numeric sort key so ERROR sits above WARNING above INFO when
        # the column is sorted descending — without it the alphabetical
        # order of the emoji codepoints leaks through. See the
        # _MessagesSortItem docstring for the EditRole-vs-DisplayRole
        # trap that motivates using UserRole here.
        level_item.setData(Qt.UserRole, int(rec.level))
        # Cell only shows the glyph; full level name lives in the tooltip
        # so users on a screen reader or hovering for clarity still get
        # the textual label.
        level_item.setToolTip(rec.level_name)
        level_item.setTextAlignment(Qt.AlignCenter)
        if rec.level >= logging.ERROR:
            level_item.setForeground(QBrush(QColor(_t["err"])))
        elif rec.level >= logging.WARNING:
            level_item.setForeground(QBrush(QColor(_t["warn"])))
        elif rec.level <= logging.DEBUG:
            level_item.setForeground(QBrush(QColor(_t["fg_dim"])))
        self.messages_table.setItem(row, 1, level_item)

        source_item = QTableWidgetItem(rec.name)
        source_item.setToolTip(rec.name)
        self.messages_table.setItem(row, 2, source_item)

        # Strip embedded newlines from the message so long multi-line
        # records (e.g. tracebacks via logging.exception) stay on one
        # row in the table. The full text is preserved in the tooltip.
        oneline = rec.message.replace("\r\n", " | ").replace("\n", " | ")
        msg_item = QTableWidgetItem(oneline)
        msg_item.setToolTip(rec.message)
        self.messages_table.setItem(row, 3, msg_item)

    def _messages_allowed_levels(self) -> set[int]:
        """Return the set of ``logging.*`` level numbers currently
        ticked in the filter row. A record passes the filter if its
        ``levelno`` is in this set."""
        boxes = getattr(self, "_messages_level_boxes", None)
        if not boxes:
            return {lvl for _, lvl in self._MESSAGES_FILTER_LEVELS}
        return {lvl for lvl, box in boxes.items() if box.isChecked()}

    def _apply_messages_filter(self, *_args) -> None:
        """Wholesale rebuild on filter change. The buffer is small
        (capped at 5 000 records), so a full rebuild stays well under
        a frame — simpler than tracking per-row visibility deltas."""
        self._populate_messages_table()

    def _on_messages_clear(self) -> None:
        """Wipe both the underlying buffer and the table. The file log
        is untouched; new records start flowing in immediately."""
        log_buffer.clear()
        table = getattr(self, "messages_table", None)
        if table is not None:
            table.setSortingEnabled(False)
            table.setRowCount(0)
            table.setSortingEnabled(True)
        self._update_messages_summary(0, 0)

    def _update_messages_summary(self, visible: int, total: int) -> None:
        """Footer counter — "N messages shown out of M total" — kept in
        sync with the table state."""
        label = getattr(self, "messages_summary_label", None)
        if label is None:
            return
        label.setText(
            f"{visible} message(s) shown out of {total} total"
        )
