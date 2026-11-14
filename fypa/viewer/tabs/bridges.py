"""The Bridges tab: seeing and controlling net shorts."""
from __future__ import annotations

import logging
import time
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.net_names import _looks_like_supply_net, _supply_net_key
from fypa.viewer.numeric import _numeric_validator, _parse_numeric_text
from fypa.viewer.table_filter import TableColumnFilter
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _esc


class _BridgesTabMixin:
    """The Bridges tab: seeing and controlling net shorts."""

    # --- Bridges tab -------------------------------------------------------
    #
    # Every part that joins two nets, and what FYPA did with it. Bridging was
    # previously spread across four mechanisms with no single view of the
    # result - Altium PDN_ROLE=SERIES, the Net Tie / 0 Ω auto-bridge, the
    # low-Ω net merge that absorbs those, and editor-mode SERIES - which is
    # why a board could be silently shorted in five places with nothing on
    # screen to say so. This tab is that single view, and the only place an
    # auto-bridge can be turned off.

    _BRIDGES_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
        ("Part", "Designator. Click it to jump to the part in the Heatmap "
                 "tab; click elsewhere in the row to just select it."),
        ("Kind", "Inferred from the designator prefix."),
        ("Value", "The part's Altium value / comment."),
        ("Pins", "Pad count. The filter below keys on distinct NETS, not "
                 "pins - a 4-pad Kelvin shunt bridges two nets and matters, "
                 "a 2-pin part with both pads on one net does not."),
        ("Nets", "The two nets this part joins."),
        ("State", "series = you annotated it · auto = FYPA shorted it on "
                  "its own · off = auto-bridge disabled · not modelled = "
                  "open at DC, its far-side copper is absent from the FEM · "
                  "ignored (high Ω) = 100 Ω or more, too little current to "
                  "matter."),
        ("R", "DC resistance in ohms. Editable. Blank = not modelled."),
        ("Why / impact", "Why FYPA treated it this way, and what it costs "
                         "the result if it is wrong."),
    )

    # Below this, FYPA merges the two nets into one rail instead of keeping a
    # lumped resistor - mirrors loader.NET_MERGE_RESISTANCE_THRESHOLD_OHM.
    # Surfaced in the UI because the merge renames nets, which is startling
    # if you typed a small number and watched a rail disappear.
    _BRIDGE_MERGE_THRESHOLD_OHM: float = 0.9e-3

    def _build_bridges_tab(self) -> QWidget:
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        blurb = QLabel(
            "Parts that electrically join two nets. Set a resistance to model "
            "one as a SERIES element, or switch off an automatic short. "
            "Changes are saved to the project file and applied on the next "
            "Resolve."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"QLabel {{ color: {_T()['fg_muted']}; }}")
        outer.addWidget(blurb)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self.bridges_state_combo = QComboBox()
        self.bridges_state_combo.addItems([
            "All parts", "Modelled as SERIES", "Auto-bridged",
            "Not modelled", "Affects a solved rail", "Transistors",
        ])
        self.bridges_state_combo.setToolTip(
            "'Affects a solved rail' is the one to watch: those parts join a "
            "rail being solved to copper that is not in the solve, and are "
            "below 100 Ω (or of unknown value), so they can carry current "
            "the solve misses and the rail's resistance can read high. "
            "Pull-ups, dividers and other parts of 100 Ω or more are ignored."
        )
        self.bridges_state_combo.currentTextChanged.connect(
            self._apply_bridges_filter)
        filter_row.addWidget(self.bridges_state_combo)

        filter_row.addSpacing(16)
        filter_row.addWidget(QLabel("Auto-SERIES resistors up to"))
        self.bridges_auto_series_edit = QLineEdit()
        self.bridges_auto_series_edit.setValidator(
            _numeric_validator(self, top=1e6))
        self.bridges_auto_series_edit.setFixedWidth(80)
        self.bridges_auto_series_edit.setToolTip(
            "Resistors with no PDN_* parameters whose value is at or below "
            "this (ohms) are modelled as SERIES with their own value - "
            "current-sense shunts, mostly. 0 turns it off. Values under "
            f"{self._BRIDGE_MERGE_THRESHOLD_OHM * 1e3:g} mΩ merge the two "
            "nets like a 0 Ω link. Applied on the next Resolve.")
        self.bridges_auto_series_edit.editingFinished.connect(
            self._on_bridge_auto_series_changed)
        filter_row.addWidget(self.bridges_auto_series_edit)
        filter_row.addWidget(QLabel("Ω"))

        filter_row.addStretch(1)
        self.bridges_summary_label = QLabel("")
        self.bridges_summary_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; }}")
        filter_row.addWidget(self.bridges_summary_label)
        outer.addLayout(filter_row)

        self.bridges_table = QTableWidget()
        cols = self._BRIDGES_TABLE_COLUMNS
        # Excel-style header filters; installed before the header is
        # configured below, since it replaces the header widget.
        self.bridges_col_filter = TableColumnFilter(
            self.bridges_table,
            numeric_cols=[i for i, (n, _) in enumerate(cols)
                          if n in ("Pins", "R")])
        self.bridges_col_filter.changed.connect(self._apply_bridges_filter)
        filter_row.insertWidget(
            filter_row.indexOf(self.bridges_summary_label),
            self.bridges_col_filter.make_clear_button())
        self.bridges_table.setColumnCount(len(cols))
        self.bridges_table.setHorizontalHeaderLabels([c[0] for c in cols])
        for i, (_name, tip) in enumerate(cols):
            item = self.bridges_table.horizontalHeaderItem(i)
            if item is not None:
                item.setToolTip(tip)
        self.bridges_table.setSortingEnabled(True)
        self.bridges_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.bridges_table.setAlternatingRowColors(True)
        self.bridges_table.verticalHeader().setVisible(False)
        self.bridges_table.horizontalHeader().setStretchLastSection(True)
        self.bridges_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive)
        _t = _T()
        self.bridges_table.setStyleSheet(
            f"QTableWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"               gridline-color: {_t['gridline']};"
            f"               alternate-background-color: {_t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {_t['bg_header']};"
            f"                       color: {_t['fg_strong']}; padding: 4px;"
            f"                       border: 1px solid {_t['border']}; }}"
            f"QTableWidget::item:selected {{"
            f"    background-color: {_t['bg_selection']}; }}"
        )
        self.bridges_table.cellClicked.connect(self._on_bridges_cell_clicked)
        # cellClicked does not fire for arrow-key navigation, and a
        # repopulate clears the selection entirely; without this the action
        # buttons stay enabled for a row that is no longer selected.
        self.bridges_table.itemSelectionChanged.connect(
            self._on_bridges_selection_changed)
        self.bridges_table.itemChanged.connect(self._on_bridges_item_changed)
        outer.addWidget(self.bridges_table, 1)

        # Transistor pin picker: shown only while a transistor row is
        # selected. A power FET's drain and source are usually several pads
        # each, so the user says which pads form each side of the switch.
        self.bridges_pin_box = QWidget()
        pin_layout = QVBoxLayout(self.bridges_pin_box)
        pin_layout.setContentsMargins(0, 4, 0, 0)
        self.bridges_pin_label = QLabel("")
        self.bridges_pin_label.setWordWrap(True)
        pin_layout.addWidget(self.bridges_pin_label)
        self.bridges_pin_table = QTableWidget(0, 3)
        self.bridges_pin_table.setHorizontalHeaderLabels(["Pad", "Net", "Side"])
        for i, tip in enumerate((
                "Pad designator on the footprint.",
                "The pad's net. A net the low-Ω merge folded into another is "
                "shown as 'original → now'.",
                "A or B: the two sides of the switch (drain and source - the "
                "order does not matter for a resistance). Leave the gate, and "
                "anything else, as -.")):
            hdr = self.bridges_pin_table.horizontalHeaderItem(i)
            if hdr is not None:
                hdr.setToolTip(tip)
        self.bridges_pin_table.verticalHeader().setVisible(False)
        self.bridges_pin_table.horizontalHeader().setStretchLastSection(True)
        self.bridges_pin_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.bridges_pin_table.setMaximumHeight(200)
        self.bridges_pin_table.setStyleSheet(self.bridges_table.styleSheet())
        pin_layout.addWidget(self.bridges_pin_table)
        self.bridges_pin_status = QLabel("")
        self.bridges_pin_status.setWordWrap(True)
        pin_layout.addWidget(self.bridges_pin_status)
        self.bridges_pin_box.setVisible(False)
        self._bridge_pin_rec: dict | None = None
        self._bridge_pin_sides: dict[str, str] = {}
        outer.addWidget(self.bridges_pin_box)

        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("Selected part:"))
        self.bridges_r_edit = QLineEdit()
        self.bridges_r_edit.setPlaceholderText("resistance in ohms, e.g. 0.05")
        self.bridges_r_edit.setValidator(_numeric_validator(self, top=1e12))
        self.bridges_r_edit.setFixedWidth(180)
        self.bridges_r_edit.setToolTip(
            "Use the part's real DC resistance, not its nominal value - a "
            "ferrite's datasheet '0 Ω' is 20-200 mΩ of DCR, a fuse 10-100 mΩ."
        )
        self.bridges_r_edit.textChanged.connect(self._on_bridge_r_text_changed)
        edit_row.addWidget(self.bridges_r_edit)

        self.bridges_apply_btn = QPushButton("Model as SERIES")
        self.bridges_apply_btn.setToolTip(
            "Add an editor SERIES directive for the selected part with this "
            "resistance. Saved to the .fypa; press Resolve to apply it.")
        self.bridges_apply_btn.clicked.connect(self._on_bridge_apply)
        edit_row.addWidget(self.bridges_apply_btn)

        self.bridges_clear_btn = QPushButton("Remove")
        self.bridges_clear_btn.setToolTip(
            "Drop the editor SERIES directive for this part, or re-enable an "
            "auto-bridge that was switched off.")
        self.bridges_clear_btn.clicked.connect(self._on_bridge_clear)
        edit_row.addWidget(self.bridges_clear_btn)

        self.bridges_disable_btn = QPushButton("Disable auto-bridge")
        self.bridges_disable_btn.setToolTip(
            "Stop FYPA shorting this part automatically - it is left open at "
            "DC. The only way to veto an auto-bridge: the short (and the net "
            "merge that absorbs it) happens while annotations are parsed, "
            "long before editor directives are applied.")
        self.bridges_disable_btn.clicked.connect(self._on_bridge_disable)
        edit_row.addWidget(self.bridges_disable_btn)

        edit_row.addStretch(1)
        self.bridges_hint_label = QLabel("")
        self.bridges_hint_label.setWordWrap(True)
        edit_row.addWidget(self.bridges_hint_label, 1)
        outer.addLayout(edit_row)
        self._refresh_bridge_buttons()
        return widget

    # --- Bridges tab: data -------------------------------------------------

    def _bridge_rows(self) -> list[dict]:
        """Candidate records from solve metadata, with the user's own edits
        folded in so the table shows the *pending* state rather than the last
        solve's."""
        meta = self.metadata if isinstance(self.metadata, dict) else {}
        rows = [dict(r) for r in (meta.get("bridge_candidates") or [])]
        project = getattr(self, "_project", None)
        if project is None:
            return rows

        opted_out = {d.strip().upper()
                     for d in getattr(project, "no_auto_bridge", []) or []}
        editor_series = {
            (ed.designator or "").strip().upper(): ed
            for ed in getattr(project, "editor_directives", []) or []
            if ed.role == "SERIES" and ed.designator
        }
        for r in rows:
            key = r["designator"].strip().upper()
            ed = editor_series.get(key)
            if ed is not None and ed.resistance is not None:
                r["state"] = "series"
                r["resistance_ohm"] = float(ed.resistance)
                r["why"] = "set here (editor SERIES)"
                r["impact"] = ""
                if r.get("fet"):
                    r["a_pins"] = list(ed.p_pins or [])
                    r["b_pins"] = list(ed.n_pins or [])
                    r["net_a"] = ed.p_net or r.get("net_a")
                    r["net_b"] = ed.n_net or r.get("net_b")
                    r["why"] = "set here: modelled as switched on"
            elif key in opted_out:
                r["state"] = "off"
                r["resistance_ohm"] = None
                r["why"] = "auto-bridge disabled here"
        return rows

    def _selected_bridge_row(self) -> dict | None:
        table = getattr(self, "bridges_table", None)
        if table is None:
            return None
        items = table.selectedItems()
        if not items:
            return None
        rec = table.item(items[0].row(), 0)
        return rec.data(Qt.UserRole) if rec is not None else None


    _BRIDGE_STATE_LABEL = {
        "series": "series",
        "auto": "auto",
        "off": "off",
        "unmodelled": "not modelled",
        "high_ohm": "ignored (high Ω)",
        "annotated": "annotated",
        "open": "open (off)",
    }

    def _populate_bridges_table(self) -> None:
        table = getattr(self, "bridges_table", None)
        if table is None:
            return
        self._sync_bridge_auto_series_edit()
        rows = self._bridge_rows()
        # Sorting and itemChanged both fire during a populate; suppress them
        # or every setItem re-sorts the model out from under the loop and the
        # R column's edit handler fires on rows the user never touched.
        table.setSortingEnabled(False)
        self._bridges_populating = True
        try:
            table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                state = r.get("state", "unmodelled")
                r_ohm = r.get("resistance_ohm")
                nets = f"{r.get('net_a', '?')} \u2194 {r.get('net_b', '?')}"
                others = [n for n in (r.get("nets") or [])
                          if n not in (r.get("net_a"), r.get("net_b"))]
                if others:
                    # The gate (and any other net a transistor touches) -
                    # shown so the row is recognisable, never bridged.
                    nets += f"  (+ {', '.join(others)})"
                why = r.get("impact") or r.get("why") or ""
                cells = [
                    r.get("designator", "?"),
                    r.get("kind", ""),
                    r.get("value", ""),
                    str(r.get("pin_count", "")),
                    nets,
                    self._BRIDGE_STATE_LABEL.get(state, state),
                    "" if r_ohm is None else f"{r_ohm:g}",
                    why,
                ]
                for c, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    # Only the R column is editable - everything else is
                    # extracted fact, not a user choice.
                    if c == 6:
                        item.setFlags(item.flags() | Qt.ItemIsEditable)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if c == 0:
                        item.setData(Qt.UserRole, r)
                        # Link-styled: a click jumps to the part on the
                        # Heatmap. Underlined rather than recoloured, since
                        # the colour already carries the row's state.
                        font = item.font()
                        font.setUnderline(True)
                        item.setFont(font)
                        item.setToolTip(
                            "Click to jump to this part in the Heatmap tab.")
                    if r.get("impact"):
                        item.setForeground(QColor("#ffb300"))
                    elif state == "auto":
                        item.setForeground(QColor("#1f9c5a"))
                    table.setItem(i, c, item)
        finally:
            self._bridges_populating = False
            table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        self._apply_bridges_filter()
        self._refresh_bridge_buttons()

    def _apply_bridges_filter(self) -> None:
        table = getattr(self, "bridges_table", None)
        if table is None:
            return
        mode = self.bridges_state_combo.currentText()
        col_filter = getattr(self, "bridges_col_filter", None)
        shown = 0
        impacted = 0
        for i in range(table.rowCount()):
            cell = table.item(i, 0)
            r = cell.data(Qt.UserRole) if cell is not None else None
            if r is None:
                continue
            state = r.get("state", "unmodelled")
            if r.get("impact"):
                impacted += 1
            keep = True
            if mode == "Modelled as SERIES":
                keep = state == "series"
            elif mode == "Auto-bridged":
                keep = state == "auto"
            elif mode == "Not modelled":
                keep = state in ("unmodelled", "high_ohm", "off")
            elif mode == "Affects a solved rail":
                keep = bool(r.get("impact"))
            elif mode == "Transistors":
                keep = bool(r.get("fet"))
            if keep and col_filter is not None:
                keep = col_filter.accepts(i)
            table.setRowHidden(i, not keep)
            shown += int(keep)
        total = table.rowCount()
        msg = f"{shown} of {total} part(s)"
        if impacted:
            msg += f"  \u2022  {impacted} affecting a solved rail"
        self.bridges_summary_label.setText(msg)

    def _on_bridges_cell_clicked(self, row: int, col: int) -> None:
        """Locate the part on the board and load its resistance into the
        editor field. A click on the Part cell also jumps to the part on
        the Heatmap tab, like the Vias tab's Go cell."""
        cell = self.bridges_table.item(row, 0)
        r = cell.data(Qt.UserRole) if cell is not None else None
        if not r:
            return
        r_ohm = r.get("resistance_ohm")
        if r_ohm is None and r.get("fet"):
            # Pre-fill from the part's own Rds(on) parameter; nothing is
            # saved until the user presses "Model as SERIES".
            r_ohm = r.get("rds_on_ohm")
        self.bridges_r_edit.setText("" if r_ohm is None else f"{r_ohm:g}")
        self._refresh_bridge_buttons()
        self._sync_bridge_pin_picker()
        if col == 0:
            lid = r.get("layer_id")
            self._jump_to_xy(r.get("x_mm"), r.get("y_mm"),
                             [lid] if lid is not None else [])
            return
        # Reuse the Vias tab's highlight so a row click centres the part.
        try:
            self._highlight_via_xy = (float(r["x_mm"]), float(r["y_mm"]))
            self._render()
        except Exception:
            logging.getLogger(__name__).debug("bridge row highlight failed", exc_info=True)

    def _on_bridges_item_changed(self, item) -> None:
        """In-place edit of the R column commits the same way the button
        does, so typing a value and pressing Enter just works."""
        if getattr(self, "_bridges_populating", False) or item.column() != 6:
            return
        cell = self.bridges_table.item(item.row(), 0)
        r = cell.data(Qt.UserRole) if cell is not None else None
        if not r:
            return
        text = item.text().strip()
        if not text:
            self._apply_bridge_series(r, None)
            return
        try:
            value = _parse_numeric_text(text)
        except ValueError:
            QMessageBox.warning(
                self, "Not a number",
                f"{text!r} is not a resistance. Enter ohms, e.g. 0.05.")
            self._populate_bridges_table()
            return
        self._apply_bridge_series(r, value)

    def _on_bridge_r_text_changed(self, text: str) -> None:
        """Live warning about the merge cliff - below the threshold the two
        nets stop existing separately, which renames rails."""
        label = getattr(self, "bridges_hint_label", None)
        if label is None:
            return
        text = (text or "").strip()
        if not text:
            label.setText("")
            return
        try:
            value = _parse_numeric_text(text)
        except ValueError:
            label.setText("")
            return
        if value < self._BRIDGE_MERGE_THRESHOLD_OHM:
            # Values set here are editor directives, applied after the
            # load-time merge pass, so they are never merged, but a lumped
            # element this small between two large copper bodies is exactly
            # what the merge exists to avoid in the solver.
            label.setText(
                f"<span style='color:#ffb300;'>Below "
                f"{self._BRIDGE_MERGE_THRESHOLD_OHM * 1e3:g} m\u03a9. Values "
                f"set here are never merged, so both nets stay separate, "
                f"joined by {value * 1e3:g} m\u03a9. A series element this "
                f"small can make the solve ill-conditioned; if the result "
                f"shows large balancing currents, check this part "
                f"first.</span>")
        else:
            label.setText(
                f"<span style='color:{_T()['fg_muted']};'>Modelled as a "
                f"{value * 1e3:g} m\u03a9 series element; both nets stay "
                f"separate.</span>")

    def _on_bridges_selection_changed(self) -> None:
        self._refresh_bridge_buttons()
        self._sync_bridge_pin_picker()

    # --- Bridges tab: transistor pin picker ---------------------------------

    _PIN_SIDES = ("-", "A", "B")

    def _sync_bridge_pin_picker(self) -> None:
        """Show the picker for a selected transistor row, hide it otherwise.

        Re-filled only when the selected part changes, so a selection the
        user is part-way through is not reset by an unrelated refresh.
        """
        box = getattr(self, "bridges_pin_box", None)
        if box is None:
            return
        rec = self._selected_bridge_row()
        if not rec or not rec.get("fet"):
            self._bridge_pin_rec = None
            box.setVisible(False)
            return
        current = getattr(self, "_bridge_pin_rec", None)
        if current is not None and current.get("designator") == rec.get("designator"):
            box.setVisible(True)
            return
        self._load_bridge_pin_picker(rec)

    def _load_bridge_pin_picker(self, rec: dict) -> None:
        self._bridge_pin_rec = rec
        a = rec.get("a_pins") or rec.get("default_a_pins") or []
        b = rec.get("b_pins") or rec.get("default_b_pins") or []
        a_u = {p.upper() for p in a}
        b_u = {p.upper() for p in b}
        self._bridge_pin_sides = {}
        table = self.bridges_pin_table
        pads = rec.get("pads") or []
        table.setRowCount(len(pads))
        for i, p in enumerate(pads):
            pad = p["pad"]
            side = ("A" if pad.upper() in a_u
                    else "B" if pad.upper() in b_u else "")
            self._bridge_pin_sides[pad] = side
            pad_item = QTableWidgetItem(pad)
            pad_item.setFlags(pad_item.flags() & ~Qt.ItemIsEditable)
            table.setItem(i, 0, pad_item)
            net_text = p.get("net") or "(no net)"
            if p.get("orig_net") and p["orig_net"] != p.get("net"):
                net_text = (f"{p['orig_net']} → {p.get('net')}  "
                            f"({self._merge_chain_text(p)})")
            net_item = QTableWidgetItem(net_text)
            net_item.setFlags(net_item.flags() & ~Qt.ItemIsEditable)
            if p.get("orig_net") and p["orig_net"] != p.get("net"):
                net_item.setForeground(QColor("#ffb300"))
            table.setItem(i, 1, net_item)
            combo = QComboBox()
            combo.addItems(list(self._PIN_SIDES))
            combo.setCurrentText(side or self._PIN_SIDES[0])
            combo.setEnabled(bool(p.get("net")))
            combo.currentTextChanged.connect(
                lambda text, _pad=pad: self._on_bridge_pin_combo(_pad, text))
            table.setCellWidget(i, 2, combo)
        table.resizeColumnsToContents()
        self.bridges_pin_label.setText(
            f"<b>{_esc(rec.get('designator', '?'))}</b> pins: choose which "
            f"pads form each side of the switch (drain and source). Every pad "
            f"on a side must be on the same net; leave the gate as "
            f"-. Then enter its on-resistance and press "
            f"<i>Model as SERIES</i>.")
        self.bridges_pin_box.setVisible(True)
        self._refresh_bridge_pin_status()

    @staticmethod
    def _merge_chain_text(pad: dict) -> str:
        """``HS_SNS ─R80→ +12V_IN ─R1→ +12V_MEZZ ─R2→ +12V``: every link the
        merge went through, so a multi-hop merge is not blamed on whichever
        part happens to touch the original net."""
        path = pad.get("merge_path") or []
        if path:
            text = pad.get("orig_net", "?")
            for step in path:
                text += f" ─{step['via']}→ {step['net']}"
            return text
        # A record from before merge paths were kept: the links are known,
        # the order and intermediate nets are not.
        by = ", ".join(pad.get("merged_by") or [])
        return f"merged by {by}" if by else "merged by a low-Ω link"

    def _on_bridge_pin_combo(self, pad: str, text: str) -> None:
        self._bridge_pin_sides[pad] = text if text in ("A", "B") else ""
        self._refresh_bridge_pin_status()

    def _set_bridge_pin_side(self, pad: str, side: str) -> None:
        """Set one pad's side ("A", "B" or "") - the combo and the model."""
        table = self.bridges_pin_table
        for i in range(table.rowCount()):
            item = table.item(i, 0)
            if item is not None and item.text().upper() == pad.upper():
                combo = table.cellWidget(i, 2)
                if combo is not None:
                    combo.setCurrentText(side or self._PIN_SIDES[0])
                self._bridge_pin_sides[item.text()] = side
        self._refresh_bridge_pin_status()

    def _bridge_pin_selection(self) -> tuple[list[str], list[str]]:
        rec = getattr(self, "_bridge_pin_rec", None) or {}
        order = [p["pad"] for p in rec.get("pads") or []]
        sides = getattr(self, "_bridge_pin_sides", {})
        return ([p for p in order if sides.get(p) == "A"],
                [p for p in order if sides.get(p) == "B"])

    def _fet_pins_for(self, rec: dict) -> tuple[list[str], list[str]]:
        """The pins to save for ``rec``: the picker's if it is showing this
        part, else the saved directive's, else the default guess."""
        shown = getattr(self, "_bridge_pin_rec", None)
        if shown is not None and shown.get("designator") == rec.get("designator"):
            return self._bridge_pin_selection()
        return (list(rec.get("a_pins") or rec.get("default_a_pins") or []),
                list(rec.get("b_pins") or rec.get("default_b_pins") or []))

    def _check_fet_selection(
        self, rec: dict, a: list[str], b: list[str],
    ) -> tuple[str | None, str | None, str | None]:
        """``(net_a, net_b, None)`` for a usable pin choice, else
        ``(None, None, reason)``.

        Nets are the pads' *current* nets - the names the solve resolves.
        A net the merge folded away still has a name but owns no copper, so
        a directive naming it would quietly attach to nothing.
        """
        des = rec.get("designator", "?")
        pads = {p["pad"].upper(): p for p in rec.get("pads") or []}
        if not a or not b:
            return None, None, ("Pick at least one pad for side A and one "
                                "for side B.")
        both = sorted({x.upper() for x in a} & {x.upper() for x in b})
        if both:
            return None, None, f"Pad(s) {', '.join(both)} are on both sides."

        def _side_net(pins: list[str], label: str):
            unknown = [p for p in pins if p.upper() not in pads]
            if unknown:
                return None, (f"{des} has no pad(s) {', '.join(unknown)}.")
            nets = sorted({pads[p.upper()].get("net") or "" for p in pins})
            if "" in nets:
                return None, f"Side {label} includes a pad with no net."
            if len(nets) != 1:
                return None, (
                    f"Side {label} spans {', '.join(nets)}. Each side of the "
                    f"switch is a single net - all the drain pads on one, "
                    f"all the source pads on the other.")
            return nets[0], None

        net_a, err = _side_net(a, "A")
        if err:
            return None, None, err
        net_b, err = _side_net(b, "B")
        if err:
            return None, None, err
        if net_a == net_b:
            merged = [pads[p.upper()] for p in a + b
                      if pads[p.upper()].get("orig_net")
                      and pads[p.upper()]["orig_net"] != pads[p.upper()].get("net")]
            msg = (f"Side A and side B are both on {net_a}, so {des} would "
                   f"be shorted to itself.")
            if merged:
                p = merged[0]
                orig = p["orig_net"]
                links = p.get("merged_by") or []
                msg += (
                    f" {orig} was merged into {net_a} through "
                    f"{self._merge_chain_text(p)} (links auto-bridged below "
                    f"{self._BRIDGE_MERGE_THRESHOLD_OHM * 1e3:g} mΩ).")
                pending = self._pending_bridge_changes(links)
                if pending:
                    # The user already broke the chain here; the rows above
                    # are from the last solve, before that change applied.
                    msg += (
                        f" You have already changed {', '.join(pending)} in "
                        f"this tab, which breaks that chain, but it only takes "
                        f"effect when the design is rebuilt: press Resolve, "
                        f"then set up {des}.")
                elif len(links) > 1:
                    later = p["merge_path"][0]["net"]
                    msg += (
                        f" Break any link in that chain: give it its real "
                        f"resistance in this tab, or disable its auto-bridge, "
                        f"then Resolve. Either way {des} can then be set up. "
                        f"{links[0]} is the one that keeps {orig} on its own; "
                        f"breaking a later link leaves {orig} joined to "
                        f"{later}.")
                else:
                    first = links[0] if links else "the link"
                    msg += (
                        f" Give {first} its real resistance in this tab, or "
                        f"disable its auto-bridge, and Resolve; {orig} is then "
                        f"a separate net again and {des} can be set up.")
            else:
                msg += " Pick pads on two different nets."
            return None, None, msg
        return net_a, net_b, None

    def _pending_bridge_changes(self, links: list[str]) -> list[str]:
        """Which of ``links`` the user has opted out of, or given its own
        resistance, in this tab - changes that are saved in the project but
        only reach the nets on the next rebuild of the design."""
        project = getattr(self, "_project", None)
        if project is None or not links:
            return []
        changed = {d.strip().upper()
                   for d in (getattr(project, "no_auto_bridge", None) or [])}
        changed |= {(ed.designator or "").strip().upper()
                    for ed in (getattr(project, "editor_directives", None) or [])
                    if ed.role == "SERIES" and ed.designator}
        return [d for d in links if d.strip().upper() in changed]

    def _refresh_bridge_pin_status(self) -> None:
        rec = getattr(self, "_bridge_pin_rec", None)
        label = getattr(self, "bridges_pin_status", None)
        if rec is None or label is None:
            return
        a, b = self._bridge_pin_selection()
        net_a, net_b, err = self._check_fet_selection(rec, a, b)
        if err:
            label.setText(f"<span style='color:#ffb300;'>{_esc(err)}</span>")
        else:
            label.setText(
                f"<span style='color:{_T()['fg_muted']};'>A: {_esc(net_a)} "
                f"(pad {_esc(', '.join(a))}) ↔ B: {_esc(net_b)} "
                f"(pad {_esc(', '.join(b))})</span>")

    def _refresh_bridge_buttons(self) -> None:
        r = self._selected_bridge_row()
        for name in ("bridges_apply_btn", "bridges_clear_btn",
                     "bridges_disable_btn"):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.setEnabled(bool(r))
        btn = getattr(self, "bridges_disable_btn", None)
        if btn is not None:
            # Only meaningful for a part FYPA shorted on its own.
            btn.setEnabled(bool(r) and r.get("state") == "auto")

    # --- Bridges tab: actions ----------------------------------------------

    def _warn_if_shorting_power_rails(self, rec: dict) -> bool:
        """Confirm before joining two nets that both look like supplies.

        Tying AGND to GND is routine; tying +5V to +3V3 is almost always a
        mistake, and it is one the solver will happily accept and quietly
        give a wrong answer for.
        """
        a = str(rec.get("net_a", ""))
        b = str(rec.get("net_b", ""))
        if not (_looks_like_supply_net(a) and _looks_like_supply_net(b)):
            return True
        if _supply_net_key(a) == _supply_net_key(b):
            return True   # +3V3 and +3V3_SW are the same supply
        return QMessageBox.warning(
            self, "Shorting two supplies?",
            f"{rec.get('designator', '?')} joins {a} and {b}, which "
            f"both look like supply rails rather than a ground pair."
            f"\n\nTying two grounds together is routine; tying two "
            f"different supplies is almost always a mistake, and the "
            f"solver will accept it and quietly return a wrong answer."
            f"\n\nBridge them anyway?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) == QMessageBox.Yes

    def _on_bridge_apply(self) -> None:
        rec = self._selected_bridge_row()
        if not rec:
            return
        text = self.bridges_r_edit.text().strip()
        if not text:
            QMessageBox.information(
                self, "No resistance",
                "Enter the part's DC resistance in ohms first.")
            return
        try:
            value = _parse_numeric_text(text)
        except ValueError:
            QMessageBox.warning(self, "Not a number",
                                f"{text!r} is not a resistance.")
            return
        self._apply_bridge_series(rec, value)

    def _on_bridge_clear(self) -> None:
        rec = self._selected_bridge_row()
        if rec:
            self._apply_bridge_series(rec, None)

    def _sync_bridge_auto_series_edit(self) -> None:
        """Show the project's auto-SERIES limit, unless the user is typing."""
        edit = getattr(self, "bridges_auto_series_edit", None)
        if edit is None or edit.hasFocus():
            return
        project = getattr(self, "_project", None)
        if project is None:
            from fypa.altium.annotations import DEFAULT_AUTO_SERIES_MAX_OHM
            value = DEFAULT_AUTO_SERIES_MAX_OHM
        else:
            value = float(getattr(project, "auto_series_max_ohm", 0.1))
        edit.setText(f"{value:g}")

    def _on_bridge_auto_series_changed(self) -> None:
        edit = self.bridges_auto_series_edit
        text = edit.text().strip()
        try:
            value = _parse_numeric_text(text) if text else 0.0
        except ValueError:
            QMessageBox.warning(self, "Not a number",
                                f"{text!r} is not a resistance in ohms.")
            self._sync_bridge_auto_series_edit()
            return
        if value < 0:
            value = 0.0
        project = self._ensure_project()
        if float(getattr(project, "auto_series_max_ohm", 0.1)) == value:
            return
        project.auto_series_max_ohm = value
        if value == 0:
            msg = "Auto-SERIES for low-value resistors turned off."
        else:
            msg = (f"Unannotated resistors up to {value * 1e3:g} mΩ will be "
                   f"modelled as SERIES with their own value.")
        edit.clearFocus()
        self._after_bridge_edit(msg)

    def _on_bridge_disable(self) -> None:
        rec = self._selected_bridge_row()
        if not rec:
            return
        project = self._ensure_project()
        des = rec["designator"]
        existing = {d.strip().upper()
                    for d in getattr(project, "no_auto_bridge", []) or []}
        if des.strip().upper() in existing:
            return
        project.no_auto_bridge = list(
            getattr(project, "no_auto_bridge", []) or []) + [des]
        self._after_bridge_edit(
            f"{des}: auto-bridge disabled. It will be left open at DC on the "
            f"next Resolve.")

    def _apply_bridge_series(self, rec: dict, resistance: float | None) -> None:
        """Write (or drop) an editor SERIES directive for this part.

        Edits go into the ``.fypa`` as ordinary editor directives - the same
        store and the same undo path as Edit mode - so there is one source of
        truth rather than a parallel one owned by this tab.
        """
        from fypa.project_file import EditorDirective

        project = self._ensure_project()
        des = rec["designator"]
        key = des.strip().upper()

        def _drop_existing_series() -> None:
            """Remove this part's editor SERIES so edits replace, not stack."""
            for ed in list(getattr(project, "editor_directives", []) or []):
                if (ed.role == "SERIES"
                        and (ed.designator or "").strip().upper() == key):
                    project.remove_directive(ed.id)

        def _set_opt_out(enabled: bool) -> None:
            opted = [d for d in (getattr(project, "no_auto_bridge", []) or [])
                     if d.strip().upper() != key]
            if enabled:
                opted.append(des)
            project.no_auto_bridge = opted

        if resistance is None:
            # "Remove" also lifts an opt-out, so one button undoes either.
            _drop_existing_series()
            _set_opt_out(False)
            self._after_bridge_edit(f"{des}: reverted to FYPA's default.")
            return

        if rec.get("fet"):
            self._apply_fet_series(rec, float(resistance), _drop_existing_series)
            return

        # Validate and confirm BEFORE touching the project. Dropping the old
        # directive first means a cancelled confirmation silently destroys
        # the resistance the user had already saved.
        if resistance <= 0.0:
            QMessageBox.warning(
                self, "Resistance must be positive",
                "A zero or negative resistance would short the pads through "
                "an ideal wire. For a true wire link, leave it to the "
                "automatic bridge.")
            self._populate_bridges_table()
            return
        if not self._warn_if_shorting_power_rails(rec):
            self._populate_bridges_table()
            return

        _drop_existing_series()
        project.upsert_directive(EditorDirective(
            kind="component", role="SERIES", designator=des,
            single_net=False,
            p_net=rec.get("net_a"), n_net=rec.get("net_b"),
            resistance=float(resistance),
            overrides_designator=des,
        ))
        # The explicit directive has to SUPPRESS the automatic short, not sit
        # alongside it. The auto-bridge fires while annotations are parsed and
        # merges the two nets there and then; this directive is applied after
        # that and would name a net pair which no longer exists, so the
        # resistance would be silently ignored.
        _set_opt_out(True)
        note = (f"{des}: modelled as a {resistance * 1e3:g} m\u03a9 SERIES "
                f"element.")
        self._after_bridge_edit(note)

    def _apply_fet_series(self, rec: dict, resistance: float,
                          drop_existing) -> None:
        """Model a transistor as switched on: a SERIES element between the
        pads the user put on side A and those on side B.

        Validated and confirmed before the project is touched, like the
        two-pin path. No auto-bridge opt-out is written - transistors are
        never auto-bridged, so there is nothing to suppress.
        """
        from fypa.project_file import EditorDirective

        des = rec["designator"]
        if resistance <= 0.0:
            QMessageBox.warning(
                self, "Resistance must be positive",
                "Enter the transistor's on-resistance, Rds(on), in ohms.")
            return
        a, b = self._fet_pins_for(rec)
        net_a, net_b, err = self._check_fet_selection(rec, a, b)
        if err:
            QMessageBox.warning(self, f"Can't set up {des}", err)
            return
        if not self._warn_if_shorting_power_rails(
                dict(rec, net_a=net_a, net_b=net_b)):
            return

        drop_existing()
        self._ensure_project().upsert_directive(EditorDirective(
            kind="component", role="SERIES", designator=des,
            single_net=False,
            p_net=net_a, n_net=net_b,
            p_pins=list(a), n_pins=list(b),
            resistance=float(resistance),
            overrides_designator=des,
        ))
        self._after_bridge_edit(
            f"{des}: modelled as switched on, {resistance * 1e3:g} m\u03a9 "
            f"between {net_a} (pad {', '.join(a)}) and {net_b} "
            f"(pad {', '.join(b)}).")

    def _after_bridge_edit(self, message: str) -> None:
        """Persist, refresh the table, and mark the solve stale."""
        # Editor mode marks dirty and lets the user save explicitly; do the
        # same rather than writing the .fypa behind their back. The edit is
        # already in the in-memory project either way.
        self._mark_project_dirty()
        self._update_pending_rails()
        selected = (self._selected_bridge_row() or {}).get("designator")
        self._populate_bridges_table()
        if selected:
            # Repopulating clears the selection; put it back so the part the
            # user just edited (and its pin picker) stays in front of them.
            table = self.bridges_table
            for i in range(table.rowCount()):
                item = table.item(i, 0)
                if item is not None and item.text() == selected:
                    table.selectRow(i)
                    break
        self._update_bridges_tab_title()
        # A part just set up (or removed) as SERIES draws (or drops) its
        # unsolved link on the canvas straight away, without a re-render.
        refresh = getattr(self, "_refresh_pending_links", None)
        if refresh is not None:
            refresh()
        self.bridges_hint_label.setText(
            f"<span style='color:{_T()['fg_muted']};'>{_esc(message)} "
            f"Press Resolve to apply.</span>")


    def _build_vias_tab(self) -> QWidget:
        """Build the Vias tab - a sortable table of every via's worst-segment
        current + power dissipation. Rows over the warning threshold get a
        red highlight on the current and power columns so high-current vias
        jump out even on a 100+ row table."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        # Filter bar - rail combo + "warnings only" toggle + summary.
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Rail:"))
        self.vias_rail_combo = QComboBox()
        self.vias_rail_combo.addItem("All rails")
        for r in self._rails:
            self.vias_rail_combo.addItem(r)
        self.vias_rail_combo.setToolTip(
            "Filter rows to vias on the selected rail group (a primary net "
            "plus any nets bridged to it via a SERIES directive)."
        )
        self.vias_rail_combo.currentTextChanged.connect(self._apply_vias_filter)
        filter_row.addWidget(self.vias_rail_combo)

        filter_row.addSpacing(12)
        self.vias_warn_only_box = QCheckBox(
            f"Show only warnings (|I| ≥ {self._via_current_warn_a:g} A)"
        )
        self.vias_warn_only_box.toggled.connect(self._apply_vias_filter)
        filter_row.addWidget(self.vias_warn_only_box)

        filter_row.addStretch(1)
        self.vias_summary_label = QLabel("")
        self.vias_summary_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; }}"
        )
        filter_row.addWidget(self.vias_summary_label)
        outer.addLayout(filter_row)

        # Table.
        self.vias_table = QTableWidget()
        cols = self._VIAS_TABLE_COLUMNS
        # Excel-style header filters (column 0 is the Go action cell).
        self.vias_col_filter = TableColumnFilter(
            self.vias_table,
            numeric_cols=[i for i, (_n, num) in enumerate(cols) if num],
            skip_cols=[0])
        self.vias_col_filter.changed.connect(self._apply_vias_filter)
        filter_row.insertWidget(
            filter_row.indexOf(self.vias_summary_label),
            self.vias_col_filter.make_clear_button())
        self.vias_table.setColumnCount(len(cols))
        self.vias_table.setHorizontalHeaderLabels([c[0] for c in cols])
        self.vias_table.setSortingEnabled(True)
        self.vias_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.vias_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.vias_table.setAlternatingRowColors(True)
        self.vias_table.verticalHeader().setVisible(False)
        self.vias_table.horizontalHeader().setStretchLastSection(True)
        # Interactive (user-draggable) resize. ``ResizeToContents`` triggers
        # a column re-measurement on every setItem call during populate,
        # which on a 3 000-row × 11-column table runs to tens of millions
        # of font-metric calls and dominates the populate time. We do ONE
        # measurement pass at the end of _populate_vias_table instead via
        # ``resizeColumnsToContents``.
        self.vias_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive)
        # Theme-driven styling - matches the Nodes table.
        _t = _T()
        self.vias_table.setStyleSheet(
            f"QTableWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"               gridline-color: {_t['gridline']};"
            f"               alternate-background-color: {_t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
            f"                       padding: 4px; border: 1px solid {_t['border']}; }}"
            f"QTableWidget::item:selected {{ background-color: {_t['bg_selection']}; }}"
        )
        outer.addWidget(self.vias_table, 1)
        # Deliberately NOT calling _populate_vias_table() here - the row build
        # is deferred to first tab activation (see __init__ + _on_tabs_current_changed).
        # _compute_via_report + the ~7 000 QTableWidgetItem creations took 35 s
        # on a big board, blocking the whole viewer open. Tab title's warning
        # count will appear once the user first opens the tab.
        self._vias_stack = self._wrap_placeholder(widget)
        self._sync_placeholder_tabs()
        return self._vias_stack

    def _get_or_compute_via_rows(self) -> list[dict]:
        """Run :meth:`_compute_via_report` at most once per viewer and
        cache the result. Used both by the deferred warning-count
        initialiser (so the tab title shows "Vias ⚠ N" before the user
        ever opens the tab) and by :meth:`_populate_vias_table` (so the
        first table populate skips the compute cost - it's already done).
        """
        cached = getattr(self, "_vias_rows_cache", None)
        if cached is None:
            cached = self._compute_via_report()
            self._vias_rows_cache = cached
        return cached

    def _init_vias_warn_count(self) -> None:
        """Compute the Vias warning count once the viewer is visible, so
        the tab title shows the alert badge before the user navigates to
        the tab. The row dicts are cached for the eventual table populate
        so we never pay the compute cost twice."""
        if not self._has_solve_results():
            self._vias_warn_count = 0
            self._update_vias_tab_title(0)
            return
        rows = self._get_or_compute_via_rows()
        warn_count = sum(
            1 for r in rows
            if r.get("current") is not None
            and abs(r["current"]) >= self._via_current_warn_a
        )
        self._vias_warn_count = warn_count
        self._update_vias_tab_title(warn_count)

    def _populate_vias_table(self) -> None:
        """Fill the Vias table from the cached via report.

        The action column is a plain clickable text cell ("Go ▶"), not an
        embedded QPushButton - embedded widgets via ``setCellWidget`` cost
        ~5-10 ms each in Qt due to event-loop wiring, hover handling, and
        layout participation, and on a 3 000-row table that alone was ~25 s
        of the original 35 s tab-build freeze. Clicks are dispatched
        through a single table-level ``cellClicked`` signal instead."""
        log = logging.getLogger(__name__)
        _t0 = time.monotonic()
        rows = self._get_or_compute_via_rows()
        log.info("Vias populate: _compute_via_report %.2fs (%d rows)",
                 time.monotonic() - _t0, len(rows))
        _t1 = time.monotonic()
        # Sidecar in original row order so the cellClicked handler can
        # find the via dict even after the user sorts the table.
        self._vias_rows = rows
        cols = self._VIAS_TABLE_COLUMNS
        _t = _T()
        warn_bg = QBrush(QColor(_t["warn_bg"]))
        warn_fg = QBrush(QColor(_t["warn_fg"]))
        # Action-column styling colours, fetched once.
        action_fg = QBrush(QColor(_t["accent"]))
        action_align = Qt.AlignCenter

        # Column 0 is the action ("Go") cell; everything else is data in
        # columns 1..N. Stays aligned with _VIAS_TABLE_COLUMNS and the
        # NET_COL / CURRENT_COL constants in _apply_vias_filter.
        ACTION_COL = 0

        self.vias_table.setSortingEnabled(False)
        self.vias_table.setRowCount(len(rows))
        warn_count = 0
        for r, row in enumerate(rows):
            is_warn = (row.get("current") is not None
                       and abs(row["current"]) >= self._via_current_warn_a)
            if is_warn:
                warn_count += 1
            # Clickable action cell. We stash the original row index on
            # UserRole so the cellClicked handler can recover the row dict
            # even after the user re-sorts the table.
            action_item = QTableWidgetItem("Go ▶")
            action_item.setData(Qt.EditRole, float(r))
            action_item.setData(Qt.UserRole, r)
            action_item.setForeground(action_fg)
            action_item.setTextAlignment(action_align)
            action_item.setToolTip(
                "Click to jump to this via in the Heatmap tab - zooms "
                "in, enables the via's physical layer if needed, and "
                "drops a yellow highlight ring."
            )
            self.vias_table.setItem(r, ACTION_COL, action_item)

            cells = (
                None,  # action column placeholder; we skip it below
                row.get("net", ""),
                row.get("layer_span", ""),
                row.get("x_mm"),
                row.get("y_mm"),
                row.get("diameter_mm"),
                row.get("ipc4761_label", "-") or "-",
                row.get("v_top"),
                row.get("v_bottom"),
                # delta_v is V → display in mV for readability
                None if row.get("delta_v") is None else row["delta_v"] * 1000.0,
                row.get("current"),
                # power is W → display in mW
                None if row.get("power") is None else row["power"] * 1000.0,
            )
            for c, (col_label, is_numeric) in enumerate(cols):
                if c == ACTION_COL:
                    continue
                value = cells[c]
                if value is None:
                    item = QTableWidgetItem("-")
                elif is_numeric and isinstance(value, (int, float)):
                    if isinstance(value, int):
                        text = f"{value:d}"
                    else:
                        text = f"{value:.4g}"
                    item = QTableWidgetItem(text)
                    item.setData(Qt.EditRole, float(value))
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    item = QTableWidgetItem(str(value))
                # Red-highlight the current + power cells when above
                # the warning threshold so the user can scan a long list
                # and spot trouble at a glance.
                if is_warn and col_label in ("|I| max (A)", "Power (mW)"):
                    item.setBackground(warn_bg)
                    item.setForeground(warn_fg)
                self.vias_table.setItem(r, c, item)
        log.info("Vias populate: items+styling %.2fs", time.monotonic() - _t1)
        _t2 = time.monotonic()
        self.vias_table.setSortingEnabled(True)
        # Default sort: |I| descending so the worst vias surface at the top.
        current_col = next((i for i, (n, _) in enumerate(cols)
                            if n == "|I| max (A)"), 0)
        self.vias_table.sortByColumn(current_col, Qt.DescendingOrder)
        log.info("Vias populate: sort %.2fs", time.monotonic() - _t2)
        _t3 = time.monotonic()
        # One-shot column sizing now that every cell is set. See the
        # Interactive-mode rationale in _build_vias_tab.
        self.vias_table.resizeColumnsToContents()
        log.info("Vias populate: resizeColumnsToContents %.2fs",
                 time.monotonic() - _t3)
        _t4 = time.monotonic()
        # Wire the table-level click dispatcher once on first populate.
        # _vias_click_handler_wired guards against re-wiring (which would
        # cause the handler to fire N times per click after N populates);
        # disconnect() can't be used because PySide6 emits a noisy
        # RuntimeWarning when there's nothing to disconnect.
        if not getattr(self, "_vias_click_handler_wired", False):
            self.vias_table.cellClicked.connect(self._on_vias_cell_clicked)
            self._vias_click_handler_wired = True
        self._vias_warn_count = warn_count
        self._update_vias_tab_title(warn_count)
        self._apply_vias_filter()
        log.info("Vias populate: tail (filter etc) %.2fs",
                 time.monotonic() - _t4)
        log.info("Vias populate: TOTAL %.2fs", time.monotonic() - _t0)

    def _on_vias_cell_clicked(self, row: int, col: int) -> None:
        """Single-click on the action column (col 0) → jump to that via in
        the Heatmap tab. Replaces the old per-row QPushButton - see
        :meth:`_populate_vias_table` for the perf motivation."""
        if col != 0:
            return
        item = self.vias_table.item(row, 0)
        if item is None:
            return
        orig_idx = item.data(Qt.UserRole)
        if (isinstance(orig_idx, int)
                and 0 <= orig_idx < len(getattr(self, "_vias_rows", []))):
            self._jump_to_via(self._vias_rows[orig_idx])

    def _update_vias_tab_title(self, warn_count: int) -> None:
        """Append the warning count to the Vias tab label so users see it
        without having to open the tab."""
        idx = getattr(self, "_vias_tab_index", -1)
        if idx < 0:
            return
        title = "Vias" if warn_count == 0 else f"Vias ⚠ {warn_count}"
        self.tabs.setTabText(idx, title)

    def _apply_vias_filter(self, *_args) -> None:
        """Hide rows that fail the Rail filter or the warnings-only toggle."""
        rail_choice = (self.vias_rail_combo.currentText()
                       if hasattr(self, "vias_rail_combo") else "All rails")
        warn_only = (self.vias_warn_only_box.isChecked()
                     if hasattr(self, "vias_warn_only_box") else False)
        if rail_choice == "All rails":
            allowed_nets: set[str] | None = None
        else:
            allowed_nets = set(self._rail_to_members.get(rail_choice, [rail_choice]))

        # Column indexes (must stay aligned with _VIAS_TABLE_COLUMNS).
        # Column 0 is the "Go" action button. The "IPC-4761 fill" column at
        # index 6 pushes |I| max from 9 → 10.
        NET_COL, CURRENT_COL = 1, 10
        col_filter = getattr(self, "vias_col_filter", None)

        visible = 0
        warn_visible = 0
        for r in range(self.vias_table.rowCount()):
            net_item = self.vias_table.item(r, NET_COL)
            cur_item = self.vias_table.item(r, CURRENT_COL)
            net = net_item.text() if net_item else ""
            try:
                cur = float(cur_item.text())
            except (ValueError, AttributeError):
                cur = 0.0
            rail_ok = allowed_nets is None or net in allowed_nets
            warn_ok = (not warn_only) or abs(cur) >= self._via_current_warn_a
            hide = not (rail_ok and warn_ok
                        and (col_filter is None or col_filter.accepts(r)))
            self.vias_table.setRowHidden(r, hide)
            if not hide:
                visible += 1
                if abs(cur) >= self._via_current_warn_a:
                    warn_visible += 1
        self.vias_summary_label.setText(
            f"{visible} via(s) shown of {self.vias_table.rowCount()} total - "
            f"{warn_visible} at or above {self._via_current_warn_a:g} A"
        )

    def _compute_via_report(self) -> list[dict]:
        """One row per via with per-segment current + total power dissipation.

        The sampling lives in :func:`fypa.solution_sampling.compute_via_rows`
        so the design report reads exactly the numbers this table shows.
        """
        if self.metadata is None:
            return []
        from fypa.solution_sampling import SolutionSampler, compute_via_rows
        return compute_via_rows(
            SolutionSampler(self.solution, self.metadata),
            self._layer_id_to_name,
        )

    def _layer_id_to_name(self, layer_id: int) -> str:
        """Look up a stackup layer's human-readable name (e.g. 'Top') by id.
        Falls back to ``"L<id>"`` for unknown ids."""
        for row in (self.metadata.get("stackup", []) if self.metadata else []):
            if row.get("layer_id") == layer_id:
                return row.get("name") or f"L{layer_id}"
        return f"L{layer_id}"
