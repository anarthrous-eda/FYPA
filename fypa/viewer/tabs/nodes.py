"""The Nodes tab."""
from __future__ import annotations

import logging
import math
import time
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.display import _power_density_per_vertex
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
        outer.addWidget(self.nodes_table, 1)
        # Deliberately NOT calling _populate_nodes_table() here — the row build
        # is deferred to first tab activation (see __init__ + _on_tabs_current_changed).
        self._nodes_stack = self._wrap_placeholder(widget)
        self._sync_placeholder_tabs()
        return self._nodes_stack

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

        Voltage and power-density are sampled from the per-
        (physical_layer, net) padne Layer's mesh using a
        ``scipy.spatial.cKDTree`` nearest-vertex lookup, batched across
        every pin. Padne adds directive-pin coupling sites as Steiner
        points to the Triangle mesher — so each pin's ``(x, y)`` IS a mesh
        vertex and the nearest-vertex value is the exact mesh-side
        potential at the pin. This replaces the original per-pin
        ``LinearTriInterpolator`` sampling, whose lazy
        ``TrapezoidMapTriFinder`` build dominated the runtime: ~3 s for
        175 pins → < 0.1 s after the refactor. ``drop`` is voltage minus
        the rail group's source voltage (max V at any directive pin on
        the same rail). Current density |J| = sqrt(power_density × sheet
        conductance).
        """
        if self.metadata is None:
            return []
        from scipy.spatial import cKDTree

        layer_index_by_pair = self._index_by_pair  # already keyed (phys, net)
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}

        # Per-(physical, net) cache of (cKDTree, potentials_1d,
        # pd_per_vertex_1d, sheet_conductance). Built lazily — only built
        # for (phys, net) combinations that actually have a pin sample.
        kdtree_cache: dict[tuple[str, str], tuple] = {}
        # Tolerance for "this pin xy matches a mesh vertex". Same value as
        # the Vias report — 0.01 mm is well below any Altium grid and
        # comfortably above float noise.
        _MATCH_TOL_MM = 0.01

        def _get_v_pd_kdtree(phys_name: str, net_name: str):
            """Return ``(tree, vs_arr, pds_arr, conductance)``, or
            ``(None, None, None, 0.0)`` if no mesh exists for this pair."""
            key = (phys_name, net_name)
            if key in kdtree_cache:
                return kdtree_cache[key]
            li = layer_index_by_pair.get(key)
            if li is None:
                kdtree_cache[key] = (None, None, None, 0.0)
                return kdtree_cache[key]
            ls = self.solution.layer_solutions[li]
            layer = self.solution.problem.layers[li]
            xs_parts, ys_parts, vs_parts, pd_parts = [], [], [], []
            for xys, tris_local, pot, pd in zip(
                ls.vertex_xys, ls.triangles,
                ls.potentials, ls.power_densities,
            ):
                n = xys.shape[0]
                if n == 0 or tris_local.size == 0:
                    continue
                # Power density is stored per-face; convert to per-vertex
                # for nearest-vertex lookup. Mirrors the original logic
                # used inside the LinearTriInterpolator path.
                if pd is not None:
                    pd_per_v = _power_density_per_vertex(
                        tris_local, pot, pd, layer.conductance, n,
                    )
                else:
                    pd_per_v = np.zeros(n, dtype=np.float64)
                # Drop orphan vertices — those not referenced by any
                # triangle. Padne pins them to V=0 to keep the linear
                # system non-singular; including them in the kdtree
                # would let a pin sample V=0 instead of the real
                # voltage when its (x,y) sits within the match
                # tolerance of an orphan.
                used = np.unique(tris_local.ravel())
                xs_parts.append(xys[used, 0])
                ys_parts.append(xys[used, 1])
                vs_parts.append(pot[used])
                pd_parts.append(pd_per_v[used])
            if not xs_parts:
                kdtree_cache[key] = (None, None, None, layer.conductance)
                return kdtree_cache[key]
            pts = np.column_stack([
                np.concatenate(xs_parts), np.concatenate(ys_parts),
            ])
            vs = np.concatenate(vs_parts)
            pds = np.concatenate(pd_parts)
            kdtree_cache[key] = (cKDTree(pts), vs, pds, layer.conductance)
            return kdtree_cache[key]

        # --- Pass 1: prep each pin + bucket sample requests by (phys, net) ---
        # preps[i] holds everything needed to assemble row i once voltages
        # and power densities for that pin are known.
        preps: list[dict] = []
        sample_requests: dict[tuple[str, str],
                              list[tuple[int, float, float]]] = {}
        for d in self.metadata.get("directives", []):
            role = d.get("role", "")
            # An auto-bridge is a synthetic record for the marker overlay,
            # not a terminal pair anyone can inspect: both of its pins
            # report the post-merge net and zero pad area.
            if role == "AUTO_BRIDGE":
                continue
            desig = d.get("designator", "?")
            # ``label`` disambiguates multi-channel SOURCE/SINK pins
            # ("U5" vs "U5#1") in the Nodes-tab table.
            display_desig = str(d.get("label") or desig)
            schdoc = d.get("schdoc", "")
            # PDN_MIN_V is a per-directive limit on the SINK's rail (P) side.
            # Carry it only onto SINK P-terminal pins so the per-pin margin
            # check doesn't fire on the return (N) terminal or on non-sinks.
            directive_min_v = (d.get("min_voltage")
                               if role == "SINK" else None)
            for term_name, term in (d.get("terminals") or {}).items():
                pin_min_v = (directive_min_v
                             if term_name == "P" else None)
                for pin in term.get("pins", []):
                    layer_id = pin.get("layer_id")
                    net = pin.get("net", "")
                    x = pin.get("x_mm")
                    y = pin.get("y_mm")
                    phys = id_to_phys.get(layer_id)
                    prep_idx = len(preps)
                    preps.append({
                        "role": role,
                        "designator": display_desig,
                        "schdoc": schdoc,
                        "terminal": term_name,
                        "pad": self._pin_display_pad(pin),
                        "net": net,
                        "layer_id": layer_id,
                        "x_mm": x,
                        "y_mm": y,
                        "phys": phys,
                        "min_voltage": pin_min_v,
                    })
                    if (phys is not None and x is not None and y is not None):
                        sample_requests.setdefault((phys, net), []).append(
                            (prep_idx, float(x), float(y))
                        )

        # --- Pass 2: batched nearest-vertex lookup per (phys, net) -------
        # samples[(prep_idx)] = (voltage_or_None, pd_or_None, conductance)
        samples: dict[int, tuple[float | None, float | None, float]] = {}
        for key, reqs in sample_requests.items():
            tree, vs_arr, pds_arr, cond = _get_v_pd_kdtree(*key)
            if tree is None:
                for (p, _x, _y) in reqs:
                    samples[p] = (None, None, cond)
                continue
            pts = np.empty((len(reqs), 2), dtype=np.float64)
            for i, r in enumerate(reqs):
                pts[i, 0] = r[1]
                pts[i, 1] = r[2]
            distances, indices = tree.query(pts)
            for i, (p, _x, _y) in enumerate(reqs):
                if distances[i] > _MATCH_TOL_MM:
                    samples[p] = (None, None, cond)
                else:
                    idx = indices[i]
                    v = float(vs_arr[idx])
                    pd_v = float(pds_arr[idx])
                    samples[p] = (
                        v if np.isfinite(v) else None,
                        pd_v if np.isfinite(pd_v) else None,
                        cond,
                    )

        # --- Pass 3: assemble rows from preps + samples ------------------
        rows: list[dict] = []
        for prep_idx, prep in enumerate(preps):
            voltage, pd_val, conductance = samples.get(
                prep_idx, (None, None, 0.0)
            )
            cd_val = (math.sqrt(max(pd_val * conductance, 0.0))
                      if pd_val is not None else None)
            min_v = prep["min_voltage"]
            # Margin = actual sample - declared minimum; ``None`` whenever
            # either side is missing (no PDN_MIN_V on the directive, or the
            # mesh sample failed and the row's voltage is None). Status is
            # "PASS" when the margin is >=0 and "FAIL" when negative; left
            # ``None`` so the table cell renders as "—" otherwise.
            if min_v is None or voltage is None:
                margin = None
                status = None
            else:
                margin = voltage - min_v
                status = "PASS" if margin >= 0 else "FAIL"
            rows.append({
                "role": prep["role"],
                "designator": prep["designator"],
                "schdoc": prep["schdoc"],
                "terminal": prep["terminal"],
                "pad": prep["pad"],
                "net": prep["net"],
                "layer_id": prep["layer_id"],
                "x_mm": prep["x_mm"],
                "y_mm": prep["y_mm"],
                "voltage": voltage,
                "power_density": pd_val,
                "current_density": cd_val,
                "min_voltage": min_v,
                "margin": margin,
                "status": status,
            })

        # Now compute Drop per row: V - max(V on the same rail group).
        # We use the rail-group lookup the heatmap already builds at init.
        net_to_rail: dict[str, str] = {}
        for rail, members in self._rail_to_members.items():
            for n in members:
                net_to_rail[n] = rail
        rail_max_v: dict[str, float] = {}
        for r in rows:
            rail = net_to_rail.get(r["net"])
            if rail is None or r["voltage"] is None:
                continue
            cur = rail_max_v.get(rail)
            if cur is None or r["voltage"] > cur:
                rail_max_v[rail] = r["voltage"]
        for r in rows:
            rail = net_to_rail.get(r["net"])
            if rail is None or r["voltage"] is None or rail not in rail_max_v:
                r["drop"] = None
            else:
                r["drop"] = r["voltage"] - rail_max_v[rail]

        return rows
