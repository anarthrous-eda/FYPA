"""The Nodes tab."""
from __future__ import annotations

import logging
import time
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.theme import _T
from fypa.viewer.widgets import _qt_widget_alive


class _NodesTabMixin:
    """The Nodes tab."""

    # --- Nodes tab ------------------------------------------------------------

    # Columns of the Nodes-tab table. (display label, numeric? — used for
    # tab-stop alignment and sort key.)
    # Column 0 is the per-row "Go" jump cell (a clickable text cell, same
    # as the Vias tab); the rest are normal text/numeric cells.
    _NODES_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = (
        ("",           False),
        ("Role",       False),
        ("Designator", False),
        ("Pad",        False),
        ("Net",        False),
        ("Layer",      True),
        ("X (mm)",     True),
        ("Y (mm)",     True),
        ("Voltage (V)",        True),
        ("Drop (V)",           True),
        ("|J| (A/mm)",         True),
        ("Power (W/mm^2)",     True),
        # PDN_MIN_V check (SINK only): the declared minimum acceptable rail
        # voltage, the measured-vs-declared margin, and the per-pin pass/fail
        # verdict. Cells are blank ("—") for non-SINK rows and for the SINK
        # N-terminal (the return side); the margin and status are highlighted
        # red on FAIL so a long table can be skim-audited at a glance.
        ("Min V (V)",          True),
        ("Margin (V)",         True),
        ("Status",             False),
    )

    def _build_nodes_tab(self) -> QWidget:
        """Build the Nodes tab — a sortable table of every directive node
        and its computed metrics. The filter combo lets users narrow down
        to a single role (e.g. just SINKs) when there are hundreds of nodes."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        # Filter bar — role + rail combos. Both filters apply; rows must
        # satisfy BOTH selections to be visible.
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Role:"))
        self.nodes_filter_combo = QComboBox()
        self.nodes_filter_combo.addItem("All roles")
        for role in ("SOURCE", "SINK", "RESISTOR", "REGULATOR"):
            self.nodes_filter_combo.addItem(role)
        self.nodes_filter_combo.currentTextChanged.connect(self._apply_nodes_filter)
        filter_row.addWidget(self.nodes_filter_combo)

        filter_row.addSpacing(12)
        filter_row.addWidget(QLabel("Rail:"))
        self.nodes_rail_combo = QComboBox()
        self.nodes_rail_combo.addItem("All rails")
        for r in self._rails:
            self.nodes_rail_combo.addItem(r)
        self.nodes_rail_combo.setToolTip(
            "Filter rows to nodes on the selected rail group (a primary net "
            "plus any nets bridged to it via a SERIES directive)."
        )
        self.nodes_rail_combo.currentTextChanged.connect(self._apply_nodes_filter)
        filter_row.addWidget(self.nodes_rail_combo)

        filter_row.addStretch(1)
        self.nodes_summary_label = QLabel("")
        self.nodes_summary_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; }}"
        )
        filter_row.addWidget(self.nodes_summary_label)
        outer.addLayout(filter_row)

        # Table.
        self.nodes_table = QTableWidget()
        cols = self._NODES_TABLE_COLUMNS
        self.nodes_table.setColumnCount(len(cols))
        self.nodes_table.setHorizontalHeaderLabels([c[0] for c in cols])
        self.nodes_table.setSortingEnabled(True)
        self.nodes_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.nodes_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.nodes_table.setAlternatingRowColors(True)
        self.nodes_table.verticalHeader().setVisible(False)
        self.nodes_table.horizontalHeader().setStretchLastSection(True)
        # Interactive (user-draggable). See _build_vias_tab for why
        # ResizeToContents during populate is a perf disaster — the same
        # one-shot ``resizeColumnsToContents`` after populate applies here.
        self.nodes_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive)
        # Theme-driven styling to match the rest of the viewer.
        _t = _T()
        self.nodes_table.setStyleSheet(
            f"QTableWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"               gridline-color: {_t['gridline']};"
            f"               alternate-background-color: {_t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
            f"                       padding: 4px; border: 1px solid {_t['border']}; }}"
            f"QTableWidget::item:selected {{ background-color: {_t['bg_selection']}; }}"
        )
        # Right-click a SINK row to see where copper would help that load.
        self.nodes_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.nodes_table.customContextMenuRequested.connect(
            self._on_nodes_context_menu)
        outer.addWidget(self.nodes_table, 1)
        # Deliberately NOT calling _populate_nodes_table() here — the row build
        # is deferred to first tab activation (see __init__ + _on_tabs_current_changed).
        self._nodes_stack = self._wrap_placeholder(widget)
        self._sync_placeholder_tabs()
        return self._nodes_stack

    def _on_nodes_context_menu(self, pos) -> None:
        """Offer the Copper ROI view for a SINK row's load."""
        row = self.nodes_table.rowAt(pos.y())
        if row < 0:
            return
        role_item = self.nodes_table.item(row, 1)
        des_item = self.nodes_table.item(row, 2)
        if role_item is None or des_item is None or role_item.text() != "SINK":
            return
        designator = des_item.text()
        menu = QMenu(self.nodes_table)
        act = menu.addAction(f"Show fixes for {designator}")
        if not self._roi_available():
            act.setEnabled(False)
            act.setText(f"Show fixes for {designator} (re-solve first)")
        chosen = menu.exec(self.nodes_table.viewport().mapToGlobal(pos))
        if chosen is act and act.isEnabled():
            self._show_fixes_for(designator)

    def _has_solve_results(self) -> bool:
        """False while the viewer holds a stub: a load-only import, a design
        still missing directives, or a solve that failed to mesh. A stub's
        layers carry no mesh, so anything sampled from the FEM is blank."""
        solution = getattr(self, "solution", None)
        if solution is None:
            return False
        return not getattr(solution, "solver_info", {}).get("stub")

    def _needs_solve_text(self, shows: str) -> str:
        """Placeholder for a tab whose every value comes from the FEM, or
        "" once there is a solve. ``shows`` ends the sentence."""
        if self._has_solve_results():
            return ""
        if (getattr(self, "metadata", None) or {}).get("mesh_failed"):
            return ("The last solve failed to mesh — see the Messages tab. "
                    f"Fix the reported copper and re-solve to show {shows}.")
        return f"Run a solve first to show {shows}."

    def _has_editor_sources_or_sinks(self) -> bool:
        project = getattr(self, "_project", None)
        return any(
            getattr(ed, "role", None) in ("SOURCE", "SINK")
            for ed in (getattr(project, "editor_directives", None) or [])
        )

    def _needs_rails_text(self, shows: str) -> str:
        """Placeholder for a tab grouped by rail, or "" when rails exist.

        Rails come from SOURCE / SINK / REGULATOR directives in the metadata,
        not from the solve — a schematic-annotated design has them before any
        solve. Sources and sinks placed in the PDN editor only reach the
        metadata on the next solve, so those need one first."""
        if getattr(self, "_rails", None):
            return ""
        extracted = getattr(getattr(self, "_loaded_project", None),
                            "extracted", None)
        if extracted is not None and not extracted.pcb_components:
            # No rail would help — say the thing that actually would.
            return ("Capacitor analysis needs component data, which Gerber "
                    "imports don't carry — import the Altium design instead.")
        if self._has_editor_sources_or_sinks():
            return (f"Run a solve first to show {shows} — capacitors are "
                    "grouped by rail, and the sources and sinks placed in "
                    "the PDN editor only define rails once solved.")
        return ("No sources or sinks set up yet. Capacitors are grouped by "
                "the rails they define — place SOURCE and SINK directives in "
                f"the PDN editor, then run a solve to show {shows}.")

    # (stack attribute, placeholder-text method, end of its sentence)
    _PLACEHOLDER_TABS: tuple[tuple[str, str, str], ...] = (
        ("_nodes_stack", "_needs_solve_text",
         "the voltage, drop and current density at each directive pin"),
        ("_vias_stack", "_needs_solve_text",
         "the current and power dissipated in each via"),
        ("_fixes_stack", "_needs_fixes_text",
         "which copper changes would help each load most"),
        ("_caps_stack", "_needs_rails_text",
         "each decoupling capacitor's loop inductance"),
        ("_impedance_stack", "_needs_rails_text",
         "each rail's impedance against its target"),
    )

    def _wrap_placeholder(self, content: QWidget) -> QStackedWidget:
        """Stack *content* over a centred placeholder sentence — a table of
        blank cells says less than a sentence saying why they're blank.
        :meth:`_sync_placeholder_tabs` picks which one shows."""
        stack = QStackedWidget(self.tabs)
        stack.addWidget(content)
        label = QLabel("")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(f"QLabel {{ color: {_T()['fg_muted']}; }}")
        stack.addWidget(label)
        return stack

    def _placeholder_text(self, stack_attr: str) -> str:
        """The placeholder a tab is showing, or "" when it shows its content.
        Also what the lazy populate checks, so a placeholder tab isn't built."""
        for attr, method, shows in self._PLACEHOLDER_TABS:
            if attr == stack_attr:
                return getattr(self, method)(shows)
        return ""

    def _sync_placeholder_tabs(self) -> None:
        """Show or hide each tab's placeholder. Run when the tab is built,
        when a solve lands, and on every tab switch (editor edits can change
        the rail-less wording)."""
        for attr, _method, _shows in self._PLACEHOLDER_TABS:
            stack = getattr(self, attr, None)
            if stack is None or not _qt_widget_alive(stack):
                continue
            text = self._placeholder_text(attr)
            stack.widget(1).setText(text)
            stack.setCurrentIndex(1 if text else 0)

    def _update_bridges_tab_title(self) -> None:
        """Badge the tab with the number of parts that affect a solved rail.

        Those are the rows that change an answer — a part joining a solved
        rail to copper no directive touches means that copper is missing
        from the FEM. Putting the count on the tab is what stops the whole
        feature being something the user has to think to go and look at.
        """
        idx = getattr(self, "_bridges_tab_index", -1)
        if idx < 0:
            return
        try:
            n = sum(1 for r in self._bridge_rows() if r.get("impact"))
        except Exception:
            n = 0
        self.tabs.setTabText(idx, f"Bridges \u26a0 {n}" if n else "Bridges")

    def _on_tabs_current_changed(self, index: int) -> None:
        """Lazy-populate the Nodes / Vias tables the first time the user
        opens them. On a 7 000-via board the Vias populate alone takes
        ~35 s of blocked GUI thread; doing it on initial viewer open was
        the freeze users were seeing under the "saving cache" label.
        Done once per tab — the populated flags guard against re-runs.
        A tab showing its placeholder stays unpopulated — there's nothing
        to fill it with yet, and its populated flag stays False so it
        builds once the placeholder clears."""
        self._sync_placeholder_tabs()
        if (index == getattr(self, "_nodes_tab_index", -1)
                and not self._placeholder_text("_nodes_stack")
                and not getattr(self, "_nodes_table_populated", True)):
            self._nodes_table_populated = True
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._populate_nodes_table()
            finally:
                QApplication.restoreOverrideCursor()
        elif (index == getattr(self, "_vias_tab_index", -1)
                and not self._placeholder_text("_vias_stack")
                and not getattr(self, "_vias_table_populated", True)):
            self._vias_table_populated = True
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._populate_vias_table()
            finally:
                QApplication.restoreOverrideCursor()
        elif (index == getattr(self, "_fixes_tab_index", -1)
                and not self._placeholder_text("_fixes_stack")
                and not getattr(self, "_fixes_populated", True)):
            self._fixes_populated = True
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._populate_fixes_tab()
            finally:
                QApplication.restoreOverrideCursor()
        elif (index == getattr(self, "_bridges_tab_index", -1)
                and not getattr(self, "_bridges_table_populated", True)):
            self._bridges_table_populated = True
            self._populate_bridges_table()
        elif (index == getattr(self, "_caps_tab_index", -1)
                and not self._placeholder_text("_caps_stack")
                and not getattr(self, "_caps_table_populated", True)):
            # The row build is seconds of geometry work — run it behind a busy
            # dialog rather than freezing on the GUI thread. The populated flag
            # is set by the continuation, so a build that fails or is re-entered
            # doesn't leave the tab permanently blank.
            def _populate() -> None:
                self._caps_table_populated = True
                self._populate_caps_table()
            self._ensure_cap_rows_async(_populate)
        elif (index == getattr(self, "_impedance_tab_index", -1)
                and not self._placeholder_text("_impedance_stack")
                and not getattr(self, "_impedance_populated", True)):
            self._impedance_populated = True
            self._populate_impedance_tab()
        elif index == getattr(self, "_topology_tab_index", -1):
            if not getattr(self, "_topology_populated", True):
                self._topology_populated = True
                self._populate_topology()
            view = getattr(self, "_topology_view", None)
            if view is not None:
                view.setFocus()

    def _get_or_compute_node_rows(self) -> list[dict]:
        """Run :meth:`_compute_node_report` at most once per viewer and cache
        the result. Mirrors :meth:`_get_or_compute_via_rows` — used both by
        the deferred PDN_MIN_V warning-count initialiser (so the tab title
        shows "Nodes ⚠ N" before the user ever opens the tab) and by
        :meth:`_populate_nodes_table` (so the first populate skips the
        compute cost — it's already done)."""
        cached = getattr(self, "_nodes_rows_cache", None)
        if cached is None:
            cached = self._compute_node_report()
            self._nodes_rows_cache = cached
        return cached

    def _init_nodes_warn_count(self) -> None:
        """Compute the Nodes warning count once the viewer is visible, so the
        tab title shows the alert badge before the user navigates to the tab.
        Mirrors :meth:`_init_vias_warn_count`. The warning count is the
        number of pins on a SINK with a ``PDN_MIN_V`` annotation whose
        measured voltage is below that minimum."""
        if not self._has_solve_results():
            self._nodes_warn_count = 0
            self._update_nodes_tab_title(0)
            return
        rows = self._get_or_compute_node_rows()
        warn_count = sum(1 for r in rows if r.get("status") == "FAIL")
        self._nodes_warn_count = warn_count
        self._update_nodes_tab_title(warn_count)

    def _update_nodes_tab_title(self, warn_count: int) -> None:
        """Append the warning count to the Nodes tab label so users see it
        without having to open the tab. Mirrors :meth:`_update_vias_tab_title`."""
        idx = getattr(self, "_nodes_tab_index", -1)
        if idx < 0:
            return
        title = "Nodes" if warn_count == 0 else f"Nodes ⚠ {warn_count}"
        self.tabs.setTabText(idx, title)

    def _populate_nodes_table(self) -> None:
        """Fill the Nodes table from the cached node report."""
        log = logging.getLogger(__name__)
        _t0 = time.monotonic()
        rows = self._get_or_compute_node_rows()
        log.info("Nodes populate: _compute_node_report %.2fs (%d rows)",
                 time.monotonic() - _t0, len(rows))
        _t1 = time.monotonic()
        # Sidecar in original row order so the cellClicked handler can
        # find the node dict even after the user sorts the table.
        self._nodes_rows = rows
        cols = self._NODES_TABLE_COLUMNS
        # Action-column styling, fetched once. Mirrors the Vias tab.
        _t = _T()
        action_fg = QBrush(QColor(_t["accent"]))
        # Red highlight for FAIL rows on the PDN_MIN_V check — same palette
        # the Vias tab uses for over-current cells. PASS gets a green
        # counterpart so a row that was deliberately limit-checked stands
        # out from rows where no PDN_MIN_V was declared (those stay neutral).
        fail_bg = QBrush(QColor(_t["warn_bg"]))
        fail_fg = QBrush(QColor(_t["warn_fg"]))
        pass_bg = QBrush(QColor(_t["pass_bg"]))
        pass_fg = QBrush(QColor(_t["pass_fg"]))
        status_cols = {"Min V (V)", "Margin (V)", "Status"}

        # Column 0 is the action ("Go") cell; everything else is data in
        # columns 1..N. Stays aligned with _NODES_TABLE_COLUMNS and the
        # ROLE_COL / NET_COL constants in _apply_nodes_filter.
        ACTION_COL = 0

        self.nodes_table.setSortingEnabled(False)  # disable while loading
        self.nodes_table.setRowCount(len(rows))
        fail_count = 0
        for r, row in enumerate(rows):
            # Clickable action cell. The original row index is stashed on
            # UserRole so the cellClicked handler can recover the row dict
            # even after the user re-sorts the table.
            action_item = QTableWidgetItem("Go ▶")
            action_item.setData(Qt.EditRole, float(r))
            action_item.setData(Qt.UserRole, r)
            action_item.setForeground(action_fg)
            action_item.setTextAlignment(Qt.AlignCenter)
            action_item.setToolTip(
                "Click to jump to this node in the Heatmap tab — zooms "
                "in, enables the node's layer if needed, and drops a "
                "yellow highlight ring."
            )
            self.nodes_table.setItem(r, ACTION_COL, action_item)

            cells = (
                None,  # action column placeholder; we skip it below
                row.get("role", ""),
                row.get("designator", ""),
                row.get("pad", ""),
                row.get("net", ""),
                row.get("layer_id", ""),
                row.get("x_mm"),
                row.get("y_mm"),
                row.get("voltage"),
                row.get("drop"),
                row.get("current_density"),
                row.get("power_density"),
                row.get("min_voltage"),
                row.get("margin"),
                row.get("status"),
            )
            status = row.get("status")
            is_fail = status == "FAIL"
            is_pass = status == "PASS"
            if is_fail:
                fail_count += 1
            for c, (col_label, is_numeric) in enumerate(cols):
                if c == ACTION_COL:
                    continue
                value = cells[c]
                if value is None:
                    item = QTableWidgetItem("—")
                elif is_numeric and isinstance(value, (int, float)):
                    if isinstance(value, int):
                        text = f"{value:d}"
                    else:
                        text = f"{value:.4g}"
                    item = QTableWidgetItem(text)
                    item.setData(Qt.EditRole, float(value))  # numeric sort
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    item = QTableWidgetItem(str(value))
                if col_label in status_cols:
                    if is_fail:
                        item.setBackground(fail_bg)
                        item.setForeground(fail_fg)
                    elif is_pass:
                        item.setBackground(pass_bg)
                        item.setForeground(pass_fg)
                self.nodes_table.setItem(r, c, item)
        log.info("Nodes populate: items %.2fs", time.monotonic() - _t1)
        _t2 = time.monotonic()
        self.nodes_table.setSortingEnabled(True)
        # Default sort: Role ascending (column 1 — column 0 is the "Go"
        # action cell).
        self.nodes_table.sortByColumn(1, Qt.AscendingOrder)
        log.info("Nodes populate: sort %.2fs", time.monotonic() - _t2)
        _t3 = time.monotonic()
        self.nodes_table.resizeColumnsToContents()
        log.info("Nodes populate: resizeColumnsToContents %.2fs",
                 time.monotonic() - _t3)
        # Wire the table-level click dispatcher once. See the matching
        # guard in _populate_vias_table for why disconnect() isn't used.
        if not getattr(self, "_nodes_click_handler_wired", False):
            self.nodes_table.cellClicked.connect(self._on_nodes_cell_clicked)
            self._nodes_click_handler_wired = True
        # Update the tab-title badge from the actual fail count we just
        # rendered, so it stays in sync if the deferred _init pass hasn't
        # run yet (or if the row cache was invalidated).
        self._nodes_warn_count = fail_count
        self._update_nodes_tab_title(fail_count)
        self._apply_nodes_filter()  # respect current filter
        log.info("Nodes populate: TOTAL %.2fs", time.monotonic() - _t0)

    def _on_nodes_cell_clicked(self, row: int, col: int) -> None:
        """Single-click on the action column (col 0) → jump to that node
        in the Heatmap tab. Mirrors :meth:`_on_vias_cell_clicked`."""
        if col != 0:
            return
        item = self.nodes_table.item(row, 0)
        if item is None:
            return
        orig_idx = item.data(Qt.UserRole)
        if (isinstance(orig_idx, int)
                and 0 <= orig_idx < len(getattr(self, "_nodes_rows", []))):
            self._jump_to_node(self._nodes_rows[orig_idx])

    def _apply_nodes_filter(self, *_args) -> None:
        """Hide rows that fail either the Role filter or the Rail filter.
        Both must pass for a row to remain visible."""
        role_choice = (self.nodes_filter_combo.currentText()
                       if hasattr(self, "nodes_filter_combo") else "All roles")
        rail_choice = (self.nodes_rail_combo.currentText()
                       if hasattr(self, "nodes_rail_combo") else "All rails")
        # Resolve the rail choice into the set of member net names. "All
        # rails" → no filtering; an explicit rail → the rail group's nets.
        if rail_choice == "All rails":
            allowed_nets: set[str] | None = None
        else:
            allowed_nets = set(self._rail_to_members.get(rail_choice, [rail_choice]))

        # Column indexes (must stay aligned with _NODES_TABLE_COLUMNS).
        # Column 0 is the "Go" action cell, so Role + Net shift up by one.
        ROLE_COL, NET_COL = 1, 4

        visible = 0
        for r in range(self.nodes_table.rowCount()):
            role = self.nodes_table.item(r, ROLE_COL).text() if self.nodes_table.item(r, ROLE_COL) else ""
            net = self.nodes_table.item(r, NET_COL).text() if self.nodes_table.item(r, NET_COL) else ""
            role_ok = role_choice == "All roles" or role == role_choice
            rail_ok = allowed_nets is None or net in allowed_nets
            hide = not (role_ok and rail_ok)
            self.nodes_table.setRowHidden(r, hide)
            if not hide:
                visible += 1
        self.nodes_summary_label.setText(
            f"{visible} node(s) shown out of {self.nodes_table.rowCount()} total"
        )

    def _compute_node_report(self) -> list[dict]:
        """Build one row per directive-terminal node with V / drop / |J| / P.

        The sampling lives in :func:`fypa.solution_sampling.compute_node_rows`
        so the design report reads exactly the numbers this table shows.
        """
        if self.metadata is None:
            return []
        from fypa.solution_sampling import SolutionSampler, compute_node_rows
        return compute_node_rows(
            SolutionSampler(self.solution, self.metadata),
            self._rail_to_members,
        )
