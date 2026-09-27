"""The Fixes tab: where extra copper would help each load most."""
from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa import copper_roi
from fypa.solution_sampling import face_to_vertex_average
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _qt_widget_alive

log = logging.getLogger(__name__)

# Fix kind -> the word the table shows.
_KIND_LABELS = {"widen": "Widen", "parallel": "Add layer", "via": "Add via"}

_LOAD_COLUMNS = ("Load", "Rail", "Drop", "Budget", "Used", "Budget basis")
_FIX_COLUMNS = ("", "#", "Fix", "Est. gain", "Where", "Notes")
_FIX_GO_COL = 0


class _FixesTabMixin:
    """The Fixes tab: loads ranked by drop budget used, and for the selected
    one, the copper changes that would raise its voltage most."""

    # Class-level defaults so the render path and the Nodes-tab entry work
    # before the tab is ever opened.
    _roi_key: str | None = None
    _roi_scores: list | None = None
    _roi_result = None
    _roi_selected: int = -1
    # The heatmap shows the selected load's value map instead of the Mode
    # combo's quantity (see _current_selection). Any mode pick clears it.
    _roi_value_map: bool = False
    # Load the Nodes-tab entry asked for, while the tab opens.
    _fixes_pending: str | None = None

    # --- build ------------------------------------------------------------------

    def _build_fixes_tab(self) -> QWidget:
        t = _T()
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        intro = QLabel(
            "Where extra copper would help each load most. Loads are listed "
            "by how much of their drop budget they use — the drop from the "
            "rail's setpoint over (nominal − PDN_MIN_V) where PDN_MIN_V is "
            f"set, otherwise over {copper_roi.DEFAULT_BUDGET_PCT:g} % of "
            "nominal. Pick a load to rank its fixes; click Go to see one "
            "on the heatmap.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"QLabel {{ color: {t['fg_muted']}; }}")
        outer.addWidget(intro)

        split = QSplitter(Qt.Horizontal, widget)
        self.fixes_loads_table = self._fixes_table(_LOAD_COLUMNS)
        self.fixes_loads_table.itemSelectionChanged.connect(
            self._on_fixes_load_selected)
        split.addWidget(self.fixes_loads_table)

        right = QWidget(split)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.fixes_summary = QLabel("")
        self.fixes_summary.setWordWrap(True)
        rl.addWidget(self.fixes_summary)
        self.fixes_table = self._fixes_table(_FIX_COLUMNS)
        self.fixes_table.setWordWrap(True)
        self.fixes_table.itemSelectionChanged.connect(
            self._on_fixes_fix_selected)
        self.fixes_table.cellClicked.connect(self._on_fixes_cell_clicked)
        self.fixes_table.setToolTip(
            "Widen: push that copper edge out. Add layer: a stitched parallel "
            "copy of the copper, same weight, on the named free layer. Add "
            "via: another via beside this one. Greyed fixes run into other "
            "copper.")
        rl.addWidget(self.fixes_table, 1)

        opts = QHBoxLayout()
        self.fixes_show_box = QCheckBox("Show fixes on the heatmap")
        self.fixes_show_box.setChecked(True)
        self.fixes_show_box.toggled.connect(
            lambda _on: self._refresh_roi_highlights())
        opts.addWidget(self.fixes_show_box)
        self.fixes_value_map_box = QCheckBox("Show value map")
        self.fixes_value_map_box.setToolTip(
            "Colour the heatmap by where parallel copper would raise the "
            "selected load's voltage most (V per mm²) instead of the Mode "
            "quantity. Choosing a mode switches it off.")
        self.fixes_value_map_box.toggled.connect(self._on_fixes_value_map)
        opts.addWidget(self.fixes_value_map_box)
        opts.addStretch(1)
        rl.addLayout(opts)

        note = QLabel(
            "Estimates are first order: good for modest changes, optimistic "
            "for large ones. Re-solve to confirm a fix.")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}")
        rl.addWidget(note)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        outer.addWidget(split, 1)

        self._fixes_stack = self._wrap_placeholder(widget)
        self._sync_placeholder_tabs()
        return self._fixes_stack

    def _fixes_table(self, columns) -> QTableWidget:
        t = _T()
        tbl = QTableWidget()
        tbl.setColumnCount(len(columns))
        tbl.setHorizontalHeaderLabels(list(columns))
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        tbl.setAlternatingRowColors(True)
        tbl.verticalHeader().setVisible(False)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        tbl.setStyleSheet(
            f"QTableWidget {{ background-color: {t['bg']}; color: {t['fg']};"
            f"               gridline-color: {t['gridline']};"
            f"               alternate-background-color: {t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {t['bg_header']};"
            f"  color: {t['fg_strong']}; padding: 4px;"
            f"  border: 1px solid {t['border']}; }}"
            f"QTableWidget::item:selected {{"
            f"  background-color: {t['bg_selection']}; }}")
        return tbl

    # --- availability / placeholder ---------------------------------------------

    def _roi_available(self) -> bool:
        return bool(getattr(getattr(self, "solution", None),
                            "sensitivity", None))

    def _needs_fixes_text(self, shows: str) -> str:
        text = self._needs_solve_text(shows)
        if text:
            return text
        if not self._roi_available():
            return ("This solution was saved before FYPA could rank copper "
                    f"fixes, or has no SINK loads. Re-solve to show {shows}.")
        return ""

    # --- populate ---------------------------------------------------------------

    def _populate_fixes_tab(self) -> None:
        """Rank the loads (once per solution) and select the worst."""
        if self._roi_scores is None:
            try:
                self._roi_scores = copper_roi.rank_loads(
                    self.solution, self.metadata or {})
            except Exception:
                log.exception("Fixes: ranking the loads failed")
                self._roi_scores = []
        tbl = self.fixes_loads_table
        tbl.blockSignals(True)
        rows = [s for s in self._roi_scores if s.has_field]
        tbl.setRowCount(len(rows))
        # Over-budget rows in the same red the Nodes tab uses for FAIL.
        t = _T()
        over_bg = QBrush(QColor(t["warn_bg"]))
        over_fg = QBrush(QColor(t["warn_fg"]))
        for r, s in enumerate(rows):
            cells = (s.label, s.rail, f"{s.drop_v * 1e3:.3g} mV",
                     f"{s.budget_v * 1e3:.3g} mV", f"{s.used:.0%}",
                     s.basis_text)
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.UserRole, s.label)
                if s.used > 1:
                    item.setBackground(over_bg)
                    item.setForeground(over_fg)
                tbl.setItem(r, c, item)
        tbl.resizeColumnsToContents()
        tbl.blockSignals(False)
        if not rows:
            self.fixes_summary.setText("No solved SINK loads to rank copper for.")
            self.fixes_table.setRowCount(0)
            return
        pending = getattr(self, "_fixes_pending", None)
        if pending and self._roi_score(pending) is None:
            # A Nodes row names the designator; a channel label adds "#n".
            pending = next((s.label for s in rows
                            if s.label.split("#")[0] == pending), None)
        key = (pending or self._roi_key
               or copper_roi.default_target(self._roi_scores).label)
        self._select_fixes_load(key)

    def _select_fixes_load(self, key: str) -> None:
        tbl = self.fixes_loads_table
        for r in range(tbl.rowCount()):
            item = tbl.item(r, 0)
            if item is not None and item.data(Qt.UserRole) == key:
                if tbl.currentRow() == r:
                    self._set_roi_target(key)
                else:
                    tbl.selectRow(r)  # -> _on_fixes_load_selected
                return

    def _on_fixes_load_selected(self) -> None:
        rows = self.fixes_loads_table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.fixes_loads_table.item(rows[0].row(), 0)
        key = item.data(Qt.UserRole) if item is not None else None
        if key and (key != self._roi_key or self._roi_result is None):
            self._set_roi_target(key)

    def _roi_score(self, key: str | None):
        return next((s for s in (self._roi_scores or []) if s.label == key),
                    None)

    def _set_roi_target(self, key: str) -> None:
        """Analyse the copper for one load and list its fixes."""
        self._roi_key = key
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._roi_result = copper_roi.analyse(
                self.solution, self.metadata or {}, key,
                copper_by_layer=self._roi_copper_by_layer())
        except Exception:
            log.exception("Fixes: analysing %s failed", key)
            self._roi_result = None
        finally:
            QApplication.restoreOverrideCursor()
        self._roi_selected = -1
        self._fill_fixes_summary()
        self._fill_fixes_table()
        self._refresh_roi_highlights()
        if self._roi_value_map:
            self._render_with_busy_popup()  # the map follows the load

    def _roi_copper_by_layer(self) -> dict | None:
        """Every net's copper per physical layer, for the blocked-widening and
        free-layer checks. None falls back to the solved nets only."""
        try:
            shape_by_key, net_by_key = self._all_copper_poly_maps()
        except Exception:
            log.debug("Fixes: no all-copper geometry", exc_info=True)
            return None
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        out: dict[str, list] = {}
        for key, shp in shape_by_key.items():
            phys = id_to_phys.get(key[0])
            if phys is not None and shp is not None:
                out.setdefault(phys, []).append(
                    (net_by_key.get(key) or "(none)", shp))
        return out or None

    def _fill_fixes_summary(self) -> None:
        s = self._roi_score(self._roi_key)
        if s is None:
            self.fixes_summary.setText("")
            return
        status = "over budget" if s.used > 1 else "within budget"
        self.fixes_summary.setText(
            f"<b>{s.label}</b> on {s.rail} sees {s.drop_v * 1e3:.3g} mV of "
            f"drop against a {s.budget_v * 1e3:.3g} mV budget "
            f"({s.basis_text}) — {s.used:.0%}, {status}.")

    def _fill_fixes_table(self) -> None:
        tbl = self.fixes_table
        tbl.blockSignals(True)
        opps = getattr(self._roi_result, "opportunities", None) or []
        tbl.setRowCount(len(opps))
        t = _T()
        muted = QBrush(QColor(t["fg_muted"]))
        go = QBrush(QColor(t["accent"]))
        for r, o in enumerate(opps):
            cells = ("Go", str(r + 1), _KIND_LABELS.get(o.kind, o.kind),
                     f"+{o.value_v * 1e3:.2g} mV", o.title, o.detail)
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(f"{o.title}\n{o.detail}")
                if c == _FIX_GO_COL:
                    item.setForeground(go)
                    item.setToolTip("Show this fix on the heatmap")
                elif o.blocked:
                    item.setForeground(muted)
                tbl.setItem(r, c, item)
        if not opps and self._roi_result is not None:
            tbl.setRowCount(1)
            tbl.setItem(0, 4, QTableWidgetItem(
                "No copper change would move this load's voltage noticeably."))
        tbl.resizeColumnsToContents()
        tbl.resizeRowsToContents()
        tbl.blockSignals(False)

    # --- fixes on the board -----------------------------------------------------

    def _on_fixes_fix_selected(self) -> None:
        rows = self.fixes_table.selectionModel().selectedRows()
        self._roi_selected = rows[0].row() if rows else -1
        self._refresh_roi_highlights()

    def _on_fixes_cell_clicked(self, row: int, col: int) -> None:
        if col == _FIX_GO_COL:
            self._go_to_fix(row)

    def _go_to_fix(self, row: int) -> None:
        """Switch to the Heatmap, make the fix's copper visible, zoom to it."""
        opps = getattr(self._roi_result, "opportunities", None) or []
        if not 0 <= row < len(opps):
            return
        self._roi_selected = row
        o = opps[row]
        self.fixes_show_box.setChecked(True)
        self._show_roi_copper(o)
        self.tabs.setCurrentIndex(self._heatmap_tab_index)
        self._refresh_roi_highlights()
        self._render()
        x0, y0, x1, y1 = o.bbox
        # Frame the fix with room to see what it connects to.
        pad = max(3.0, 0.3 * max(x1 - x0, y1 - y0))
        self._gl_viewer.fit_to_bounds(x0 - pad, x1 + pad, y0 - pad, y1 + pad)

    def _show_roi_copper(self, opp) -> None:
        """Make sure the fix's physical layer and rail are visible, without
        hiding anything else."""
        visible = set(self._visible_layers())
        if opp.layer and opp.layer not in visible \
                and opp.layer in self._phys_name_to_layer_id:
            self._set_layer_visible(opp.layer, True, emit=False)
        net_to_rail = {n: r for r, members in self._rail_to_members.items()
                       for n in members}
        want = {net_to_rail.get(opp.net)}
        score = self._roi_score(self._roi_key)
        if score is not None:
            want.add(score.rail)
        changed = False
        for name, eye in self._rail_eye_buttons:
            if name in want and _qt_widget_alive(eye) \
                    and not eye.isVisibleState():
                eye.setVisibleState(True, partial=False, emit=False)
                self._fan_out_rail_eye_to_subnets(name, True)
                self._sync_rail_tree_node_partials(name)
                changed = True
        if changed:
            self._sync_all_rails_eye()
            self._sync_rail_only_visibility()

    def _refresh_roi_highlights(self) -> None:
        gl = getattr(self, "_gl_viewer", None)
        if gl is None:
            return
        opps = getattr(self._roi_result, "opportunities", None) or []
        box = getattr(self, "fixes_show_box", None)
        if not opps or box is None or not box.isChecked():
            gl.set_roi_highlights(None)
            return
        gl.set_roi_highlights([
            {"kind": o.kind, "coords": o.coords, "rings": o.rings,
             "x": o.x_mm, "y": o.y_mm,
             "rank": i + 1, "selected": i == self._roi_selected,
             "blocked": o.blocked}
            for i, o in enumerate(opps)])

    # --- value map ----------------------------------------------------------------

    def _on_fixes_value_map(self, on: bool) -> None:
        self._roi_value_map = bool(on) and self._roi_result is not None
        self._render_with_busy_popup()

    def _clear_roi_value_map(self, *_args) -> None:
        """A Mode pick means the user wants that quantity back."""
        if self._roi_value_map:
            self._roi_value_map = False
            box = getattr(self, "fixes_value_map_box", None)
            if box is not None and _qt_widget_alive(box):
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)

    def _roi_vertex_values(self, layer_index: int, geom: dict) -> list:
        """Per kept mesh, the selected load's value density averaged onto
        vertices (V/mm², clipped at 0 — copper that would lower the voltage
        is rare and reads as no value). Zeros until a load is analysed."""
        per_mesh = (getattr(self._roi_result, "density", None) or {}
                    ).get(layer_index)
        out = []
        for (tris_local, _pot, _pd, n), mesh_i in zip(
                geom["_kept"], geom["_kept_mesh_idx"]):
            dens = (per_mesh[mesh_i]
                    if per_mesh is not None and mesh_i < len(per_mesh)
                    else None)
            if dens is None or not dens.size:
                out.append(np.zeros(n, dtype=np.float64))
                continue
            out.append(face_to_vertex_average(
                tris_local, np.maximum(dens, 0.0), n))
        return out

    # --- lifecycle ------------------------------------------------------------------

    def _reset_copper_roi(self) -> None:
        """Forget every result — a new solution invalidates them. If the tab
        is open, rebuild it once the solution swap is done."""
        self._roi_key = None
        self._roi_scores = None
        self._roi_result = None
        self._roi_selected = -1
        self._clear_roi_value_map()
        self._fixes_populated = False
        gl = getattr(self, "_gl_viewer", None)
        if gl is not None:
            gl.set_roi_highlights(None)
        tabs = getattr(self, "tabs", None)
        if (tabs is not None and _qt_widget_alive(tabs)
                and tabs.currentIndex() == getattr(
                    self, "_fixes_tab_index", -1)):
            QTimer.singleShot(
                0, lambda: self._on_tabs_current_changed(tabs.currentIndex()))

    def _show_fixes_for(self, label: str) -> None:
        """Nodes-tab entry point: open the Fixes tab on this load."""
        # Picked up by the populate a first open triggers, so it analyses
        # this load rather than the default one first.
        self._fixes_pending = label
        self.tabs.setCurrentIndex(self._fixes_tab_index)
        self._fixes_pending = None
        if self._roi_score(label) is None and self._roi_scores:
            # A pin row names the designator; a channel label adds "#n".
            label = next((s.label for s in self._roi_scores
                          if s.label.split("#")[0] == label), label)
        self._select_fixes_load(label)
