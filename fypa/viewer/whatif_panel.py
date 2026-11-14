"""The What-If panel: try a change, re-solve it, compare against the base.

The panel sits on the right of the heatmap, shown / hidden by the What-If
button beside Resolve (or W). It is viewer-mode only: entering editor mode
closes it and hides the button. It lists the saved what-ifs, runs one, and switches the viewer between

* **Base** - the design's own result (what every tab normally shows),
* **What-if** - the selected what-if's result; every tab follows it and a
  banner on the heatmap says so, and
* **Difference** - the base geometry coloured by what-if minus base for the
  current heatmap mode (red = worse, blue = better).

A what-if is only ever compared with the base. Its result lives in its own
pickle, so the base solve is never replaced. The scenario model and the
comparison maths are Qt-free in :mod:`fypa.whatif`.

While a what-if is shown, ``self.solution`` / ``self.metadata`` hold the
what-if's result and the base pair is parked in ``self._whatif_base``.
Anything that must act on the base (a solve, a save, editor mode) calls
:meth:`_whatif_ensure_base` first.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fypa import whatif as W
from fypa.viewer.editor.pending import RESOLVE_DIALOG_TEXT
from fypa.viewer.numeric import _numeric_validator, _parse_numeric_text
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _ClickAbsorbingPanel, _esc

_log = logging.getLogger(__name__)

# Heatmap modes that can be drawn as a difference.
_DIFF_MODES = frozenset(
    {"Voltage", "Voltage Drop", "Current Density", "Power Density"})
# Modes where a HIGHER value is better (so red must mean "went down").
_HIGHER_IS_BETTER_MODES = frozenset({"Voltage", "Voltage Drop"})


def _fmt(v, scale: float = 1.0, unit: str = "") -> str:
    if v is None:
        return "-"
    try:
        f = float(v) * scale
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return "-"
    txt = f"{f:.4g}"
    return f"{txt} {unit}".rstrip() if unit else txt


def _fmt_delta(v, scale: float = 1.0) -> str:
    if v is None:
        return "-"
    f = float(v) * scale
    if not math.isfinite(f):
        return "-"
    return f"{f:+.3g}"


# ---------------------------------------------------------------------------
# Scenario editor dialog
# ---------------------------------------------------------------------------

class WhatIfDialog(QDialog):
    """Create / edit one what-if. Every field shows the base value as its
    placeholder; a blank field means "same as base"."""

    def __init__(self, parent, scenario: W.WhatIfScenario, base_settings,
                 stackup_rows: list[dict], existing_names: list[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle("What-if")
        self._scenario = scenario
        self._base = base_settings
        self._rows = stackup_rows
        self._existing = [n for n in existing_names if n != scenario.name]
        t = _T()
        root = QVBoxLayout(self)

        top = QFormLayout()
        self.name_edit = QLineEdit(scenario.name)
        top.addRow("Name", self.name_edit)
        self.notes_edit = QPlainTextEdit(scenario.notes)
        self.notes_edit.setPlaceholderText(
            "Why: e.g. \"cost-down: 1 oz inner layers\" or \"hot enclosure\"")
        self.notes_edit.setFixedHeight(52)
        top.addRow("Notes", self.notes_edit)
        root.addLayout(top)

        hint = QLabel(
            "Change only what you want to try - blank fields keep the base "
            "design's value (shown greyed). The what-if is solved with the "
            "same design, directives and mesh as the base.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"QLabel {{ color: {t['fg_label']}; }}")
        root.addWidget(hint)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)

        # ----- Physics -----
        phys_box = QGroupBox("Physics and vias")
        phys = QFormLayout(phys_box)
        self._edits: dict[str, object] = {}
        ov = scenario.settings_overrides
        for key, label, unit, kind in W.TUNABLE_FIELDS:
            base_val = getattr(base_settings, key, None)
            if kind == "float":
                w = QLineEdit(
                    "" if key not in ov else f"{float(ov[key]):.6g}")
                w.setValidator(_numeric_validator(self))
                w.setPlaceholderText(f"{float(base_val):.6g}"
                                     if base_val is not None else "")
                w.setMaximumWidth(140)
                row = QHBoxLayout()
                row.addWidget(w)
                row.addWidget(QLabel(unit))
                row.addStretch(1)
                holder = QWidget()
                holder.setLayout(row)
                phys.addRow(label, holder)
            else:
                w = QComboBox()
                base_txt = (W.FILL_MODE_LABELS.get(str(base_val), str(base_val))
                            if kind == "fill_mode"
                            else ("on" if base_val else "off"))
                w.addItem(f"Same as base ({base_txt})", None)
                if kind == "fill_mode":
                    for code, txt in W.FILL_MODE_LABELS.items():
                        w.addItem(txt, code)
                else:
                    w.addItem("On", True)
                    w.addItem("Off", False)
                if key in ov:
                    idx = w.findData(ov[key])
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                phys.addRow(label, w)
            self._edits[key] = w
        body_layout.addWidget(phys_box)

        # ----- Stackup -----
        stack_box = QGroupBox("Stackup - copper thickness and resistivity")
        stack = QFormLayout(stack_box)
        self._thk_edits: dict[int, QLineEdit] = {}
        self._rho_edits: dict[int, QLineEdit] = {}
        base_rho = dict(getattr(
            base_settings, "layer_copper_resistivity_20c_microohm_cm",
            None) or {})
        board_rho = float(getattr(
            base_settings, "copper_resistivity_20c_microohm_cm", 1.68))
        if not stackup_rows:
            stack.addRow(QLabel("No copper layers in this design's stackup."))
        all_row = QHBoxLayout()
        all_combo = QComboBox()
        all_combo.addItem("Set every layer to…", None)
        for txt, um in W.COPPER_WEIGHT_PRESETS_UM:
            all_combo.addItem(txt, um)
        all_combo.activated.connect(
            lambda i, c=all_combo: self._set_all_thickness(c.itemData(i)))
        if stackup_rows:
            all_row.addWidget(all_combo)
            all_row.addStretch(1)
            holder = QWidget()
            holder.setLayout(all_row)
            stack.addRow("", holder)
        for row in stackup_rows:
            try:
                lid = int(row.get("layer_id"))
            except (TypeError, ValueError):
                continue
            name = str(row.get("name") or f"L{lid}")
            base_um = float(row.get("copper_thickness_mm") or 0.0) * 1000.0
            thk = QLineEdit(
                "" if lid not in scenario.stackup_thickness_mm
                else f"{scenario.stackup_thickness_mm[lid] * 1000.0:.6g}")
            thk.setValidator(_numeric_validator(self))
            thk.setPlaceholderText(f"{base_um:g}")
            thk.setMaximumWidth(90)
            thk.setToolTip(
                f"Copper thickness of {name} in µm (base {base_um:g} µm ≈ "
                f"{base_um / 34.8:.2g} oz). Blank = same as base.")
            preset = QComboBox()
            preset.addItem("oz…", None)
            for txt, um in W.COPPER_WEIGHT_PRESETS_UM:
                preset.addItem(txt, um)
            preset.activated.connect(
                lambda i, c=preset, e=thk: (
                    e.setText(f"{c.itemData(i):g}")
                    if c.itemData(i) is not None else None,
                    c.setCurrentIndex(0)))
            rho = QLineEdit(
                "" if lid not in scenario.layer_resistivity
                else f"{scenario.layer_resistivity[lid]:.6g}")
            rho.setValidator(_numeric_validator(self))
            rho.setPlaceholderText(f"{base_rho.get(lid, board_rho):g}")
            rho.setMaximumWidth(80)
            rho.setToolTip(
                f"Resistivity of {name} at 20 °C in µΩ·cm. Blank = same as "
                "base.")
            line = QHBoxLayout()
            line.addWidget(thk)
            line.addWidget(QLabel("µm"))
            line.addWidget(preset)
            line.addSpacing(10)
            line.addWidget(QLabel("ρ"))
            line.addWidget(rho)
            line.addWidget(QLabel("µΩ·cm"))
            line.addStretch(1)
            holder = QWidget()
            holder.setLayout(line)
            stack.addRow(f"L{lid}  {name}", holder)
            self._thk_edits[lid] = thk
            self._rho_edits[lid] = rho
        body_layout.addWidget(stack_box)
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._error = QLabel("")
        self._error.setStyleSheet(f"QLabel {{ color: {t['err']}; }}")
        self._error.setWordWrap(True)
        root.addWidget(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._run_btn = buttons.addButton(
            "Save and Run", QDialogButtonBox.AcceptRole)
        self.run_requested = False
        self._run_btn.clicked.connect(
            lambda: setattr(self, "run_requested", True))
        buttons.button(QDialogButtonBox.Ok).setText("Save")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.resize(640, 680)

        # Changed fields get the same outline as an edited field on the
        # Properties tab (the ``dirty`` property, styled below).
        self.setStyleSheet(
            f"QLineEdit[dirty=\"true\"] {{ border: 1px solid {t['warn_fg']}; }}"
            f"QLineEdit[dirty=\"true\"]:focus {{ border: 1px solid "
            f"{t['warn_fg']}; }}"
            f"QComboBox[dirty=\"true\"] {{ border: 1px solid {t['warn_fg']}; }}"
        )
        self._base_thk_mm = {}
        for row in stackup_rows:
            try:
                self._base_thk_mm[int(row["layer_id"])] = float(
                    row.get("copper_thickness_mm") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
        self._base_rho = {lid: base_rho.get(lid, board_rho)
                          for lid in self._rho_edits}
        for w in self._edits.values():
            if isinstance(w, QLineEdit):
                w.textChanged.connect(self._update_dirty)
            else:
                w.currentIndexChanged.connect(self._update_dirty)
        for edit in (*self._thk_edits.values(), *self._rho_edits.values()):
            edit.textChanged.connect(self._update_dirty)
        self._update_dirty()

    @staticmethod
    def _mark(widget, dirty: bool) -> None:
        val = "true" if dirty else "false"
        if widget.property("dirty") == val:
            return
        widget.setProperty("dirty", val)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    @staticmethod
    def _text_differs(text: str, base, tol: float) -> bool:
        """Non-blank and not the base value (bad input counts as changed,
        as on the Properties tab, so the outline stays up mid-edit)."""
        text = text.strip()
        if not text:
            return False
        try:
            val = _parse_numeric_text(text)
        except ValueError:
            return True
        if base is None:
            return True
        return abs(val - float(base)) > tol * max(1.0, abs(float(base)))

    def _update_dirty(self, *_args) -> None:
        """Outline every field whose value differs from the base."""
        for key, _label, _unit, kind in W.TUNABLE_FIELDS:
            w = self._edits[key]
            base = getattr(self._base, key, None)
            if kind == "float":
                self._mark(w, self._text_differs(w.text(), base, 1e-12))
            else:
                data = w.currentData()
                self._mark(w, data is not None and data != base)
        for lid, edit in self._thk_edits.items():
            base_mm = self._base_thk_mm.get(lid)
            base_um = None if base_mm is None else base_mm * 1000.0
            self._mark(edit, self._text_differs(edit.text(), base_um, 1e-7))
        for lid, edit in self._rho_edits.items():
            self._mark(edit, self._text_differs(
                edit.text(), self._base_rho.get(lid), 1e-12))

    def _set_all_thickness(self, um) -> None:
        if um is None:
            return
        for edit in self._thk_edits.values():
            edit.setText(f"{float(um):g}")

    def _on_accept(self) -> None:
        try:
            self._apply()
        except ValueError as e:
            self.run_requested = False
            self._error.setText(str(e))
            return
        self.accept()

    def _apply(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("Give the what-if a name.")
        if name in self._existing:
            raise ValueError(f"There is already a what-if called {name!r}.")
        overrides: dict[str, object] = {}
        for key, label, _unit, kind in W.TUNABLE_FIELDS:
            w = self._edits[key]
            if kind == "float":
                text = w.text().strip()
                if not text:
                    continue
                try:
                    val = _parse_numeric_text(text)
                except ValueError:
                    raise ValueError(f"{label}: not a number ({text!r}).")
                if key != "temperature_c" and val < 0:
                    raise ValueError(f"{label} must not be negative.")
                if key in ("copper_resistivity_20c_microohm_cm",
                           "conductive_fill_resistivity_ohm_mm",
                           "heat_transfer_w_per_m2k") and val <= 0:
                    raise ValueError(f"{label} must be greater than 0.")
                base = getattr(self._base, key, None)
                if base is not None and abs(val - float(base)) <= 1e-12:
                    continue
                overrides[key] = float(val)
            else:
                data = w.currentData()
                if data is None:
                    continue
                if data == getattr(self._base, key, None):
                    continue
                overrides[key] = data
        thick: dict[int, float] = {}
        base_thk = {int(r["layer_id"]): float(r.get("copper_thickness_mm")
                                              or 0.0)
                    for r in self._rows if r.get("layer_id") is not None}
        for lid, edit in self._thk_edits.items():
            text = edit.text().strip()
            if not text:
                continue
            try:
                um = _parse_numeric_text(text)
            except ValueError:
                raise ValueError(
                    f"Layer {lid} thickness: not a number ({text!r}).")
            if um <= 0:
                raise ValueError(f"Layer {lid} thickness must be > 0 µm.")
            if abs(um / 1000.0 - base_thk.get(lid, -1.0)) > 1e-7:
                thick[lid] = um / 1000.0
        rho: dict[int, float] = {}
        base_rho = dict(getattr(
            self._base, "layer_copper_resistivity_20c_microohm_cm",
            None) or {})
        board_rho = float(getattr(
            self._base, "copper_resistivity_20c_microohm_cm", 1.68))
        for lid, edit in self._rho_edits.items():
            text = edit.text().strip()
            if not text:
                continue
            try:
                val = _parse_numeric_text(text)
            except ValueError:
                raise ValueError(
                    f"Layer {lid} resistivity: not a number ({text!r}).")
            if val <= 0:
                raise ValueError(f"Layer {lid} resistivity must be > 0.")
            if abs(val - base_rho.get(lid, board_rho)) > 1e-12:
                rho[lid] = val
        if not (overrides or thick or rho):
            raise ValueError(
                "Nothing is changed from the base yet - set at least one "
                "value.")
        s = self._scenario
        changed = (overrides != s.settings_overrides
                   or thick != s.stackup_thickness_mm
                   or rho != s.layer_resistivity)
        s.name = name
        s.notes = self.notes_edit.toPlainText().strip()
        s.settings_overrides = overrides
        s.stackup_thickness_mm = thick
        s.layer_resistivity = rho
        if changed and s.status != W.STATUS_NOT_RUN:
            # Different inputs: the stored result no longer belongs to it.
            s.status = W.STATUS_NOT_RUN
            s.summary = {}
            s.base_fingerprint = None
        self.inputs_changed = changed


# ---------------------------------------------------------------------------
# Viewer mixin
# ---------------------------------------------------------------------------

class _WhatIfMixin:
    """What-If panel, runner and view switching for :class:`PdnViewer`."""

    # Default width (px); the user can drag the panel's left edge between
    # the min and "heatmap row minus this reserve" (see
    # _on_whatif_panel_resized). Not remembered between sessions.
    _WHATIF_PANEL_W = 360
    _WHATIF_PANEL_MIN_W = 280
    _WHATIF_PANEL_PLOT_RESERVE = 200

    # --- state ---------------------------------------------------------------

    def _init_whatif_state(self) -> None:
        if getattr(self, "_whatif_state_ready", False):
            return
        self._whatif_state_ready = True
        self._whatifs: list[W.WhatIfScenario] = []
        self._whatif_selected_id: str | None = None
        self._whatif_view = "base"            # "base" | "whatif" | "diff"
        # (base_solution, base_metadata) while a what-if is swapped in.
        self._whatif_base: tuple | None = None
        # Live difference context (dict) or None - see _whatif_set_view.
        self._whatif_diff: dict | None = None
        # The same, but only while the current heatmap mode supports it;
        # set at the start of every render (render.py reads this one).
        self._whatif_diff_live: dict | None = None
        # One what-if result kept loaded: (id, solution, metadata).
        self._whatif_loaded: tuple | None = None
        self._whatif_base_fp: str | None = None
        self._whatif_rows_cache: dict = {}
        self._whatif_running_id: str | None = None

    # --- panel ---------------------------------------------------------------

    def _build_whatif_panel(self, outer_layout) -> None:
        """Add the panel to the heatmap tab's outer row, right of the plot.
        Shown / hidden by the What-If button beside Resolve (or W)."""
        self._init_whatif_state()
        t = _T()
        # Left edge is a drag-to-resize grip (same as the editor panel).
        panel = _ClickAbsorbingPanel()
        self._whatif_panel = panel
        panel.setFixedWidth(self._WHATIF_PANEL_W)
        panel.leftEdgeResized.connect(self._on_whatif_panel_resized)
        panel.setStyleSheet(
            f"QWidget {{ background-color: {t['bg']}; color: {t['fg']}; }}")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("<b>What-Ifs</b>")
        title.setStyleSheet(f"QLabel {{ color: {t['fg_strong']}; }}")
        head.addWidget(title)
        head.addStretch(1)
        self._whatif_new_btn = QPushButton("New…")
        self._whatif_new_btn.setToolTip(
            "Make a what-if: change copper weight, temperature, via plating "
            "or fill, then solve it alongside the base.")
        self._whatif_new_btn.clicked.connect(self._on_whatif_new)
        head.addWidget(self._whatif_new_btn)
        lay.addLayout(head)

        self._whatif_hint = QLabel("")
        self._whatif_hint.setWordWrap(True)
        self._whatif_hint.setStyleSheet(
            f"QLabel {{ color: {t['fg_label']}; }}")
        lay.addWidget(self._whatif_hint)

        self._whatif_list = QListWidget()
        self._whatif_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._whatif_list.setMinimumHeight(90)
        self._whatif_list.setMaximumHeight(180)
        self._whatif_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._whatif_list.customContextMenuRequested.connect(
            self._on_whatif_context_menu)
        self._whatif_list.currentItemChanged.connect(
            lambda *_a: self._on_whatif_selection_changed())
        self._whatif_list.itemDoubleClicked.connect(
            lambda _it: self._on_whatif_edit())
        lay.addWidget(self._whatif_list)

        btns = QHBoxLayout()
        self._whatif_run_btn = QPushButton("Run")
        self._whatif_run_btn.setToolTip(
            "Solve the selected what-if. The base result is kept.")
        self._whatif_run_btn.clicked.connect(self._on_whatif_run)
        self._whatif_edit_btn = QPushButton("Edit…")
        self._whatif_edit_btn.clicked.connect(self._on_whatif_edit)
        more = QToolButton()
        more.setText("More")
        more.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(more)
        more_menu.addAction("Duplicate", self._on_whatif_duplicate)
        more_menu.addAction("Delete", self._on_whatif_delete)
        more_menu.addSeparator()
        more_menu.addAction("Make this the base…", self._on_whatif_apply)
        more.setMenu(more_menu)
        self._whatif_more_btn = more
        for b in (self._whatif_run_btn, self._whatif_edit_btn):
            btns.addWidget(b)
        btns.addWidget(more)
        btns.addStretch(1)
        lay.addLayout(btns)

        self._whatif_changes = QLabel("")
        self._whatif_changes.setWordWrap(True)
        self._whatif_changes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._whatif_changes.setStyleSheet(
            f"QLabel {{ color: {t['fg']}; background-color: {t['bg_alt']};"
            f" border: 1px solid {t['border']}; padding: 4px; }}")
        lay.addWidget(self._whatif_changes)

        # Show: Base | What-if | Difference
        show_row = QHBoxLayout()
        show_row.addWidget(QLabel("Show"))
        self._whatif_view_group = QButtonGroup(panel)
        self._whatif_view_buttons: dict[str, QPushButton] = {}
        for key, text, tip in (
            ("base", "Base", "The design's own result."),
            ("whatif", "What-if",
             "The selected what-if's result - every tab follows it."),
            ("diff", "Difference",
             "What-if minus base for the current heatmap mode: red where "
             "it got worse, blue where it got better."),
        ):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setStyleSheet(
                f"QPushButton {{ padding: 3px 8px; border: 1px solid "
                f"{t['border']}; background-color: {t['bg_hover']}; }}"
                f"QPushButton:checked {{ background-color: {t['accent_btn']};"
                f" color: {t['fg_strong']}; font-weight: 600; }}"
                f"QPushButton:disabled {{ color: {t['fg_hint']}; }}")
            b.clicked.connect(lambda _c=False, k=key: self._whatif_set_view(k))
            self._whatif_view_group.addButton(b)
            self._whatif_view_buttons[key] = b
            show_row.addWidget(b)
        show_row.addStretch(1)
        lay.addLayout(show_row)

        self._whatif_summary_table = QTableWidget(0, 5)
        self._whatif_summary_table.setHorizontalHeaderLabels(
            ["Headline", "Rail", "Base", "What-if", "Δ"])
        self._setup_compare_table(self._whatif_summary_table)
        self._whatif_summary_table.setMaximumHeight(170)
        lay.addWidget(self._whatif_summary_table)

        metric_row = QHBoxLayout()
        metric_row.addWidget(QLabel("Nodes by"))
        self._whatif_metric_combo = QComboBox()
        for key, label, unit, _scale, _d in W.COMPARE_METRICS:
            self._whatif_metric_combo.addItem(
                f"{label} ({unit})" if unit else label, key)
        self._whatif_metric_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_whatif_compare())
        metric_row.addWidget(self._whatif_metric_combo, 1)
        self._whatif_changed_only = QCheckBox("Changed only")
        self._whatif_changed_only.setChecked(True)
        self._whatif_changed_only.toggled.connect(
            lambda _c: self._refresh_whatif_compare())
        metric_row.addWidget(self._whatif_changed_only)
        lay.addLayout(metric_row)

        self._whatif_table = QTableWidget(0, 5)
        self._whatif_table.setHorizontalHeaderLabels(
            ["Node", "Net", "Base", "What-if", "Δ"])
        self._setup_compare_table(self._whatif_table)
        lay.addWidget(self._whatif_table, 1)

        self._whatif_status = QLabel("")
        self._whatif_status.setWordWrap(True)
        self._whatif_status.setStyleSheet(
            f"QLabel {{ color: {t['fg_hint']}; }}")
        lay.addWidget(self._whatif_status)
        outer_layout.addWidget(panel)

        # Banner on the heatmap while a what-if (or the difference) shows.
        self._whatif_banner = QLabel("", self._gl_viewer)
        self._whatif_banner.setStyleSheet(
            "QLabel { background-color: rgba(214, 120, 20, 225);"
            " color: white; font-weight: 600; padding: 4px 12px;"
            " border-radius: 4px; }")
        self._whatif_banner.hide()

        # Always starts hidden when a design loads (not remembered); opened
        # only by the What-If button / W. Never open in editor mode.
        panel.setVisible(False)
        self._refresh_whatif_panel()

    @staticmethod
    def _setup_compare_table(table: QTableWidget) -> None:
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setWordWrap(False)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setStretchLastSection(True)
        table.verticalHeader().setDefaultSectionSize(20)

    def _on_whatif_panel_resized(self, proposed_w: int) -> None:
        """Apply a user-dragged What-If panel width, clamped to the min
        width and to leaving a reserve of the heatmap row for the plot."""
        panel = getattr(self, "_whatif_panel", None)
        if panel is None:
            return
        row = panel.parentWidget()
        row_w = row.width() if row is not None else proposed_w
        max_w = max(self._WHATIF_PANEL_MIN_W,
                    row_w - self._WHATIF_PANEL_PLOT_RESERVE)
        w = max(self._WHATIF_PANEL_MIN_W, min(int(proposed_w), max_w))
        if w != panel.width():
            panel.setFixedWidth(w)

    def _toggle_whatif_panel_visible(self) -> None:
        """W hotkey: same as clicking the What-If button."""
        panel = getattr(self, "_whatif_panel", None)
        if panel is None:
            return
        self._set_whatif_panel_visible(not panel.isVisible())

    def _set_whatif_panel_visible(self, visible: bool) -> None:
        """Show / hide the What-If panel and keep its button in step. The
        panel is viewer-mode only, so a show request in editor mode is
        refused."""
        panel = getattr(self, "_whatif_panel", None)
        if panel is None:
            return
        visible = bool(visible) and not bool(getattr(self, "_editor_mode", False))
        if panel.isVisible() != visible:
            panel.setVisible(visible)
            if visible:
                self._refresh_whatif_compare()
        btn = getattr(self, "_whatif_btn", None)
        if btn is not None and btn.isChecked() != visible:
            btn.blockSignals(True)
            btn.setChecked(visible)
            btn.blockSignals(False)

    def _position_whatif_banner(self) -> None:
        banner = getattr(self, "_whatif_banner", None)
        gl = getattr(self, "_gl_viewer", None)
        if banner is None or gl is None or not banner.isVisible():
            return
        banner.adjustSize()
        banner.move(max(8, (gl.width() - banner.width()) // 2), 8)
        banner.raise_()

    # --- helpers -------------------------------------------------------------

    def _whatif_by_id(self, sid: str | None) -> W.WhatIfScenario | None:
        for s in getattr(self, "_whatifs", []):
            if s.id == sid:
                return s
        return None

    def _whatif_selected(self) -> W.WhatIfScenario | None:
        return self._whatif_by_id(getattr(self, "_whatif_selected_id", None))

    def _whatif_base_pair(self) -> tuple:
        """``(solution, metadata)`` of the base, whatever is on screen."""
        if getattr(self, "_whatif_base", None) is not None:
            return self._whatif_base
        return self.solution, self.metadata

    def _whatif_base_is_solved(self) -> bool:
        sol, meta = self._whatif_base_pair()
        if sol is None or not meta:
            return False
        return not bool(getattr(sol, "solver_info", {}).get("stub"))

    def _whatif_compute_base_fp(self) -> str | None:
        """Fingerprint of the base the current result was solved from.

        Uses the settings the metadata records (not the live form), so the
        same base gives the same fingerprint after a save / reopen."""
        _sol, meta = self._whatif_base_pair()
        if not meta:
            return None
        from fypa.altium.loader import SolveSettings
        proj = getattr(self, "_project", None)
        extra = {
            "prjpcb": str(meta.get("prjpcb_path") or ""),
            "pcbdoc": str(meta.get("pcbdoc_path") or ""),
            "editor_directives": [d.to_dict() for d in (
                getattr(proj, "editor_directives", None) or [])],
            "copper_names": [c.to_dict() for c in (
                getattr(proj, "copper_names", None) or [])],
            "no_auto_bridge": sorted(
                str(d).upper() for d in (
                    getattr(proj, "no_auto_bridge", None) or [])),
            "pdn_json": str(getattr(proj, "pdn_json", "") or ""),
            "pdn_corner": str(getattr(proj, "pdn_corner", "typ") or "typ"),
            "auto_series": getattr(proj, "auto_series_max_ohm", None),
        }
        return W.base_fingerprint(
            SolveSettings.from_metadata(meta),
            meta.get("stackup_overrides") or {}, extra)

    def _whatif_results_dir(self) -> Path:
        path = getattr(self, "_project_path", None)
        if path is not None:
            p = Path(path)
            return p.with_name(p.stem + "_whatifs")
        cache = self._project_cache_dir_str()
        if cache:
            return Path(cache) / "whatifs"
        import tempfile
        return Path(tempfile.gettempdir()) / "fypa_whatifs"

    def _whatif_mark_unsaved(self) -> None:
        # Cosmetic for the solve (so no Resolve prompt) but an unsaved change.
        self._display_dirty = True

    # --- base changes / project load ------------------------------------------

    def _whatif_drop_view_state(self) -> None:
        """Forget any swapped-in what-if without re-rendering - called at the
        start of a base result swap, which re-renders itself."""
        self._init_whatif_state()
        base = self._whatif_base
        if base is not None:
            self.solution, self.metadata = base
        self._whatif_base = None
        self._whatif_diff = None
        self._whatif_diff_live = None
        self._whatif_view = "base"
        banner = getattr(self, "_whatif_banner", None)
        if banner is not None:
            banner.hide()

    def _whatif_after_base_solve(self, *, project=None,
                                 is_import: bool = False) -> None:
        """A new base result landed: reload the scenarios when a project file
        was opened, drop them on an import of another design, and re-check
        which ones are out of date."""
        self._init_whatif_state()
        if project is not None:
            self._whatifs = [
                W.WhatIfScenario.from_dict(d)
                for d in (getattr(project, "what_ifs", None) or [])
                if isinstance(d, dict)]
            self._whatif_selected_id = (
                self._whatifs[0].id if self._whatifs else None)
            self._whatif_loaded = None
        elif is_import:
            self._whatifs = []
            self._whatif_selected_id = None
            self._whatif_loaded = None
        self._whatif_rows_cache = {}
        self._whatif_base_fp = self._whatif_compute_base_fp()
        self._refresh_whatif_panel()

    def _whatifs_for_save(self, target_fypa: Path) -> list[dict]:
        """Scenario dicts for the ``.fypa``, copying each result pickle into
        ``<stem>_whatifs/`` beside it first (so the folder stays portable).
        Runs on the save worker thread: file work only."""
        import shutil
        out: list[dict] = []
        dest_dir = target_fypa.with_name(target_fypa.stem + "_whatifs")
        for s in list(getattr(self, "_whatifs", [])):
            d = s.to_dict()
            src = s.result_pickle
            if src and Path(src).exists():
                dest = dest_dir / f"{s.id}.pkl"
                try:
                    if Path(src).resolve() != dest.resolve():
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dest)
                    d["result_pickle"] = str(dest)
                except OSError:
                    _log.warning("Couldn't copy what-if result %s", src,
                                 exc_info=True)
            out.append(d)
        return out

    def _whatif_adopt_saved_paths(self, saved: list[dict]) -> None:
        by_id = {d.get("id"): d for d in saved}
        for s in getattr(self, "_whatifs", []):
            d = by_id.get(s.id)
            if d and d.get("result_pickle"):
                s.result_pickle = d["result_pickle"]

    def _whatif_ensure_base(self) -> None:
        """Put the base back on screen before acting on it."""
        if getattr(self, "_whatif_view", "base") != "base":
            self._whatif_set_view("base")

    # --- list / buttons -------------------------------------------------------

    def _refresh_whatif_panel(self) -> None:
        lst = getattr(self, "_whatif_list", None)
        if lst is None:
            return
        t = _T()
        lst.blockSignals(True)
        lst.clear()
        sel_row = -1
        for i, s in enumerate(self._whatifs):
            if s.id == self._whatif_running_id:
                badge, color = "running…", t["accent"]
            elif s.status == W.STATUS_FAILED:
                badge, color = "failed", t["err"]
            elif s.status != W.STATUS_OK:
                badge, color = "not run", t["fg_hint"]
            elif s.is_stale(self._whatif_base_fp):
                badge, color = "out of date", t["warn_fg"]
            else:
                badge, color = "✓", t["ok"]
            item = QListWidgetItem(f"{s.name}    [{badge}]")
            item.setData(Qt.UserRole, s.id)
            item.setForeground(_qcolor(color))
            item.setToolTip("\n".join(W.describe_changes(
                s, self._solve_settings,
                (self._whatif_base_pair()[1] or {}).get("stackup")))
                or "(no changes)")
            lst.addItem(item)
            if s.id == self._whatif_selected_id:
                sel_row = i
        if sel_row < 0 and self._whatifs:
            sel_row = 0
            self._whatif_selected_id = self._whatifs[0].id
        if sel_row >= 0:
            lst.setCurrentRow(sel_row)
        lst.blockSignals(False)

        solved = self._whatif_base_is_solved()
        if not solved:
            hint = ("Solve the design first - a what-if is always compared "
                    "with the base result.")
        elif not self._whatifs:
            hint = ("Try a change without losing this result: press "
                    "<b>New…</b>, e.g. thinner copper to save cost or a "
                    "hotter enclosure.")
        else:
            hint = ""
        self._whatif_hint.setText(hint)
        self._whatif_hint.setVisible(bool(hint))
        self._whatif_new_btn.setEnabled(solved)

        s = self._whatif_selected()
        busy = self._whatif_running_id is not None
        self._whatif_run_btn.setEnabled(s is not None and solved and not busy)
        self._whatif_run_btn.setText(
            "Run again" if s is not None and s.status == W.STATUS_OK
            else "Run")
        self._whatif_edit_btn.setEnabled(s is not None and not busy)
        self._whatif_more_btn.setEnabled(s is not None and not busy)
        if s is None:
            self._whatif_changes.setText(
                f"<span style='color:{t['fg_hint']};'>No what-if selected."
                "</span>")
        else:
            lines = W.describe_changes(
                s, self._solve_settings,
                (self._whatif_base_pair()[1] or {}).get("stackup"))
            parts = [_esc(x) for x in lines] or ["(no changes)"]
            text = "<br>".join(parts)
            if s.notes:
                text = (f"<i style='color:{t['fg_label']};'>"
                        f"{_esc(s.notes)}</i><br>" + text)
            if s.status == W.STATUS_FAILED and s.error:
                text += (f"<br><span style='color:{t['err']};'>Failed: "
                         f"{_esc(s.error.splitlines()[0])}</span>")
            elif s.is_stale(self._whatif_base_fp):
                text += (f"<br><span style='color:{t['warn_fg']};'>Out of "
                         "date: the base changed since this ran. Run it "
                         "again to compare against the current base.</span>")
            self._whatif_changes.setText(text)
        has_result = s is not None and self._whatif_has_result(s)
        for key, b in self._whatif_view_buttons.items():
            b.setEnabled(key == "base" or (has_result and not busy))
            b.blockSignals(True)
            b.setChecked(key == self._whatif_view)
            b.blockSignals(False)
        self._refresh_whatif_compare()

    def _whatif_has_result(self, s: W.WhatIfScenario) -> bool:
        if s.status != W.STATUS_OK:
            return False
        loaded = self._whatif_loaded
        if loaded is not None and loaded[0] == s.id:
            return True
        return bool(s.result_pickle) and Path(s.result_pickle).exists()

    def _on_whatif_selection_changed(self) -> None:
        item = self._whatif_list.currentItem()
        sid = item.data(Qt.UserRole) if item is not None else None
        if sid == self._whatif_selected_id:
            return
        self._whatif_selected_id = sid
        if self._whatif_view != "base":
            s = self._whatif_selected()
            if s is not None and s.status == W.STATUS_OK:
                # Keep the same kind of view, on the newly selected what-if.
                self._whatif_set_view(self._whatif_view, force=True)
            else:
                self._whatif_set_view("base")
        self._refresh_whatif_panel()

    def _on_whatif_context_menu(self, pos) -> None:
        item = self._whatif_list.itemAt(pos)
        if item is None:
            return
        self._whatif_list.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Run", self._on_whatif_run)
        menu.addAction("Edit…", self._on_whatif_edit)
        menu.addAction("Duplicate", self._on_whatif_duplicate)
        menu.addAction("Delete", self._on_whatif_delete)
        menu.addSeparator()
        menu.addAction("Make this the base…", self._on_whatif_apply)
        menu.exec(self._whatif_list.viewport().mapToGlobal(pos))

    def _whatif_edit_dialog(self, scenario: W.WhatIfScenario) -> QDialog:
        _sol, meta = self._whatif_base_pair()
        return WhatIfDialog(
            self, scenario, self._solve_settings,
            list((meta or {}).get("stackup") or []),
            [s.name for s in self._whatifs])

    def _on_whatif_new(self) -> None:
        if not self._whatif_base_is_solved():
            return
        scen = W.WhatIfScenario(
            name=W.unique_name([s.name for s in self._whatifs]))
        dlg = self._whatif_edit_dialog(scen)
        if dlg.exec() != QDialog.Accepted:
            return
        self._whatifs.append(scen)
        self._whatif_selected_id = scen.id
        self._whatif_mark_unsaved()
        self._refresh_whatif_panel()
        if dlg.run_requested:
            QTimer.singleShot(0, self._on_whatif_run)

    def _on_whatif_edit(self) -> None:
        s = self._whatif_selected()
        if s is None:
            return
        dlg = self._whatif_edit_dialog(s)
        if dlg.exec() != QDialog.Accepted:
            return
        if getattr(dlg, "inputs_changed", False):
            if self._whatif_view != "base":
                self._whatif_set_view("base")
            if self._whatif_loaded and self._whatif_loaded[0] == s.id:
                self._whatif_loaded = None
            self._whatif_rows_cache = {}
        self._whatif_mark_unsaved()
        self._refresh_whatif_panel()
        if dlg.run_requested:
            QTimer.singleShot(0, self._on_whatif_run)

    def _on_whatif_duplicate(self) -> None:
        s = self._whatif_selected()
        if s is None:
            return
        copy = s.copy_as(W.unique_name([x.name for x in self._whatifs],
                                       stem=f"{s.name} copy"))
        self._whatifs.append(copy)
        self._whatif_selected_id = copy.id
        self._whatif_mark_unsaved()
        self._refresh_whatif_panel()

    def _on_whatif_delete(self) -> None:
        s = self._whatif_selected()
        if s is None:
            return
        if QMessageBox.question(
                self, "Delete what-if",
                f"Delete the what-if “{s.name}” and its result?",
        ) != QMessageBox.Yes:
            return
        if self._whatif_view != "base":
            self._whatif_set_view("base")
        self._whatifs = [x for x in self._whatifs if x.id != s.id]
        if self._whatif_loaded and self._whatif_loaded[0] == s.id:
            self._whatif_loaded = None
        # The result file is removed only if it is our own scratch copy; a
        # copy saved beside a .fypa is left for the next Save to drop.
        try:
            if s.result_pickle and Path(s.result_pickle).parent == (
                    self._whatif_results_dir()) and getattr(
                    self, "_project_path", None) is None:
                Path(s.result_pickle).unlink(missing_ok=True)
        except OSError:
            pass
        self._whatif_selected_id = (self._whatifs[0].id if self._whatifs
                                    else None)
        self._whatif_mark_unsaved()
        self._refresh_whatif_panel()

    def _on_whatif_apply(self) -> None:
        """Make the selected what-if the base: copy its changes into the
        Properties tab's Base properties, then either adopt its result as the
        base result (when it is current - no re-solve) or re-solve."""
        s = self._whatif_selected()
        if s is None:
            return
        fresh = (self._whatif_has_result(s)
                 and not s.is_stale(self._whatif_base_fp))
        how = ("Its result becomes the base result (no re-solve)."
               if fresh else
               "The design is then re-solved with them.")
        if QMessageBox.question(
                self, "Make this the base",
                f"Make “{s.name}” the base?\n\nIts changes are "
                f"copied into the Properties tab's Base properties. {how}\n\n"
                "The current base result is replaced.",
        ) != QMessageBox.Yes:
            return
        self._whatif_ensure_base()
        new_settings = W.scenario_settings(self._solve_settings, s)
        fmt = self._fmt_settings_value
        for key, value in s.settings_overrides.items():
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is not None:
                edit.setText(fmt(value))
            elif key == "conductive_fill_mode":
                combo = getattr(self, "_fill_mode_combo", None)
                if combo is not None:
                    idx = combo.findData(value)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
            else:
                chk = {
                    "electrothermal": "_settings_electrothermal_check",
                    "thermal_spreading": "_settings_thermal_spreading_check",
                }.get(key)
                box = getattr(self, chk, None) if chk else None
                if box is not None:
                    box.setChecked(bool(value))
        for lid, mm in s.stackup_thickness_mm.items():
            entry = getattr(self, "_stackup_thickness_edits", {}).get(lid)
            if entry is not None:
                entry[0].setText(fmt(mm * 1000.0))
        for lid, rho in s.layer_resistivity.items():
            edit = getattr(self, "_stackup_resistivity_edits", {}).get(lid)
            if edit is not None:
                edit.setText(fmt(rho))
        self._on_settings_field_changed()
        pair = self._whatif_load(s) if fresh else None
        if pair is not None:
            # Same design, directives and mesh as the base, so the what-if's
            # result IS the base result for these properties.
            new_settings.apply_to_modules()
            self._apply_solve_result(
                pair[0], pair[1], getattr(self, "_loaded_project", None),
                new_settings, self._via_current_warn_a,
                self._display_percentile_high,
                mark_solved_since_save=True,
                status_done=f"“{s.name}” is now the base.")
            self.statusBar().showMessage(
                f"“{s.name}” is now the base.", 5000)
        else:
            self._on_resolve_clicked()

    # --- running --------------------------------------------------------------

    def _on_whatif_run(self) -> None:
        s = self._whatif_selected()
        if s is None or self._whatif_running_id is not None:
            return
        if getattr(self, "_solve_worker", None) is not None:
            QMessageBox.information(
                self, "Solver busy", "Another solve is running - wait for "
                "it to finish.")
            return
        if not self._whatif_base_is_solved():
            QMessageBox.information(
                self, "Solve the base first",
                "A what-if is compared with the base result, so the design "
                "needs a solve first.")
            return
        if getattr(self, "_project_dirty", False):
            QMessageBox.information(
                self, "Unsolved changes",
                "The design has editor changes that aren't solved yet. "
                "Press Resolve first, so the what-if and the base are "
                "solved from the same design.")
            return
        self._whatif_ensure_base()
        meta = self.metadata or {}
        prjpcb = meta.get("prjpcb_path")
        if not prjpcb or not Path(prjpcb).exists():
            QMessageBox.warning(
                self, "Can't run what-if",
                "This result isn't linked to an Altium project on disk, so "
                "the design can't be re-solved.")
            return
        settings = W.scenario_settings(self._solve_settings, s)
        design_thk: dict[int, float] = {}
        for row in meta.get("stackup") or []:
            try:
                lid = int(row.get("layer_id"))
            except (TypeError, ValueError):
                continue
            val = row.get("extracted_copper_thickness_mm")
            if val is None:
                val = row.get("copper_thickness_mm")
            if val is not None:
                design_thk[lid] = float(val)
        base_overrides = {int(k): float(v) for k, v in (
            meta.get("stackup_overrides") or {}).items()}
        stack = W.scenario_stackup_overrides(base_overrides, s, design_thk)
        proj = getattr(self, "_project", None)
        editor_directives = list(getattr(proj, "editor_directives", None)
                                 or [])
        copper_names = list(getattr(proj, "copper_names", None) or [])
        adaptive = (getattr(self, "_adaptive_gain_check", None) is not None
                    and self._adaptive_gain_check.isEnabled()
                    and self._adaptive_gain_check.isChecked())
        pcbdoc = meta.get("pcbdoc_path")
        fp = self._whatif_base_fp or self._whatif_compute_base_fp()
        settings.apply_to_modules()
        self._whatif_running_id = s.id
        self._refresh_whatif_panel()
        sid = s.id
        self._start_solve_worker(
            Path(prjpcb), settings,
            self._via_current_warn_a, self._display_percentile_high,
            stackup_overrides=stack,
            pcbdoc_selector=str(pcbdoc) if pcbdoc else None,
            use_design_cache=True,
            editor_directives=editor_directives,
            copper_names=copper_names,
            loaded_project=getattr(self, "_loaded_project", None),
            is_resolve=True,
            adaptive_regulator_gain=adaptive,
            dialog_title="Running what-if",
            dialog_text=(f"Solving what-if “{s.name}”…\n"
                         "The base result is kept."),
            # Same width as the Resolve pop-up (see _on_resolve_clicked), so
            # the per-stage status lines aren't cut off.
            dialog_width_scale=3.199392,
            dialog_width_text=RESOLVE_DIALOG_TEXT,
            on_finished=lambda sol, md, _loaded: self._on_whatif_finished(
                sid, fp, sol, md),
            on_failed=lambda msg: self._on_whatif_failed(sid, msg),
            on_cancelled=lambda: self._on_whatif_cancelled(sid),
            write_solve_cache=False,
        )
        # _start_solve_worker may refuse (another solve running).
        if getattr(self, "_solve_worker", None) is None:
            self._whatif_running_id = None
            self._solve_settings.apply_to_modules()
            self._refresh_whatif_panel()

    def _whatif_after_worker(self, fn, tries: int = 200) -> None:
        """Run ``fn`` once no solve worker is attached (polls every 50 ms,
        up to ~10 s)."""
        if getattr(self, "_solve_worker", None) is None:
            fn()
        elif tries > 0:
            QTimer.singleShot(
                50, lambda: self._whatif_after_worker(fn, tries - 1))

    def _whatif_restore_base_modules(self) -> None:
        # The what-if pushed its physics into the module globals; put the
        # base's back so nothing downstream reads the what-if's values.
        try:
            self._solve_settings.apply_to_modules()
        except Exception:
            _log.warning("Couldn't restore base solve settings",
                         exc_info=True)

    def _on_whatif_finished(self, sid: str, fp, solution, metadata) -> None:
        self._whatif_running_id = None
        self._whatif_restore_base_modules()
        s = self._whatif_by_id(sid)
        if s is None:
            return
        if bool(getattr(solution, "solver_info", {}).get("stub")):
            self._on_whatif_failed(
                sid, "The what-if solve produced no result (meshing failed "
                "or no closed rail).")
            return
        target = self._whatif_results_dir() / f"{s.id}.pkl"
        from fypa.cli import save_solution_file
        from fypa.viewer.save_progress import run_save_with_progress
        _, err = run_save_with_progress(
            self, "Saving what-if result",
            lambda status: save_solution_file(target, solution, metadata,
                                              status=status))
        if err is not None:
            _log.error("Couldn't save what-if result %s: %s", target, err)
            QMessageBox.warning(
                self, "What-if result not saved",
                f"The what-if solved but its result couldn't be written to "
                f"{target}:\n\n{err}\n\nIt is shown for this session only.")
            s.result_pickle = None
        else:
            s.result_pickle = str(target)
        s.status = W.STATUS_OK
        s.error = ""
        s.base_fingerprint = fp
        s.run_at = W.now()
        self._whatif_loaded = (s.id, solution, metadata)
        self._whatif_rows_cache = {
            k: v for k, v in self._whatif_rows_cache.items() if k[0] != s.id}
        try:
            rows = self._whatif_node_rows_for(solution, metadata)
            s.summary = W.summarise(rows, solution)
        except Exception:
            _log.warning("What-if summary failed", exc_info=True)
            s.summary = {}
        self._whatif_mark_unsaved()
        self._whatif_selected_id = s.id
        self._refresh_whatif_panel()
        # The finishing worker is only released on QThread.finished, a beat
        # after this slot; show the difference once it has gone.
        self._whatif_after_worker(lambda: self._whatif_set_view("diff"))
        self.statusBar().showMessage(f"What-if “{s.name}” solved.",
                                     5000)

    def _on_whatif_failed(self, sid: str, message: str) -> None:
        self._whatif_running_id = None
        self._whatif_restore_base_modules()
        s = self._whatif_by_id(sid)
        if s is not None:
            s.status = W.STATUS_FAILED
            s.error = message or "Solve failed"
        self._refresh_whatif_panel()
        first = (message or "Solve failed").splitlines()[0]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("What-if failed")
        box.setText(f"The what-if couldn't be solved: {first}")
        if message and message != first:
            box.setDetailedText(message)
        box.exec()

    def _on_whatif_cancelled(self, sid: str) -> None:
        from fypa.viewer.solve_worker import _abort_solve_worker
        _abort_solve_worker(self)
        self._whatif_running_id = None
        self._whatif_restore_base_modules()
        self._refresh_whatif_panel()
        self.statusBar().showMessage("What-if cancelled; base kept.", 5000)

    # --- loading results ---------------------------------------------------

    def _whatif_load(self, s: W.WhatIfScenario) -> tuple | None:
        """``(solution, metadata)`` of a what-if's result, or None."""
        if self._whatif_loaded and self._whatif_loaded[0] == s.id:
            return self._whatif_loaded[1], self._whatif_loaded[2]
        if not s.result_pickle or not Path(s.result_pickle).exists():
            return None
        from fypa.cli import _load_solution_pickle
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            sol, meta = _load_solution_pickle(Path(s.result_pickle))
        except Exception as e:
            _log.error("Couldn't load what-if result %s: %s",
                       s.result_pickle, e)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        self._whatif_loaded = (s.id, sol, meta)
        return sol, meta

    # --- view switching -----------------------------------------------------

    def _whatif_set_view(self, view: str, *, force: bool = False) -> None:
        """Show the base, the selected what-if, or their difference."""
        self._init_whatif_state()
        if view == self._whatif_view and not force:
            self._refresh_whatif_panel()
            return
        s = self._whatif_selected()
        pair = None
        if view != "base":
            if getattr(self, "_solve_worker", None) is not None:
                return
            pair = self._whatif_load(s) if s is not None else None
            if pair is None:
                QMessageBox.information(
                    self, "No result",
                    "This what-if has no result to show - press Run.")
                view = "base"
        if view != "base" and self._editor_mode:
            self._on_editor_mode_toggled(False)
            btn = getattr(self, "_editor_toggle_btn", None)
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)
        # Undo any current swap.
        if self._whatif_base is not None:
            self.solution, self.metadata = self._whatif_base
            self._whatif_base = None
        self._whatif_diff = None
        if view == "whatif":
            self._whatif_base = (self.solution, self.metadata)
            self.solution, self.metadata = pair
        elif view == "diff":
            from fypa.solution_sampling import SolutionSampler
            b_sampler = SolutionSampler(self.solution, self.metadata)
            s_sampler = SolutionSampler(pair[0], pair[1])
            b_off = W.source_return_offsets(b_sampler)
            s_off = W.source_return_offsets(s_sampler)
            self._whatif_diff = {
                "id": s.id,
                "name": s.name,
                "solution": pair[0],
                "metadata": pair[1],
                "index_by_name": W.layer_index_by_name(pair[0]),
                "offsets": {n: s_off.get(n, 0.0) - b_off.get(n, 0.0)
                            for n in set(b_off) | set(s_off)},
                "methods": set(),
            }
        self._whatif_view = view
        self._whatif_refresh_derived()
        self._refresh_whatif_panel()

    def _whatif_refresh_derived(self) -> None:
        """Re-point every solution-derived view at ``self.solution`` /
        ``self.metadata`` (the subset of _apply_solve_result that doesn't
        touch the project, settings or editor state)."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._init_solution_indices()
            self._clear_solution_derived_caches()
            self._rebuild_layer_rail_lists(preserve_visibility=True)
            if getattr(self, "properties_browser", None) is not None:
                self._refresh_properties_html()
            if getattr(self, "_topology_view", None) is not None:
                self._topology_populated = False
            for flag in ("_nodes_table_populated", "_vias_table_populated",
                         "_bridges_table_populated", "_caps_table_populated",
                         "_impedance_populated", "_fixes_populated"):
                setattr(self, flag, False)
            try:
                self._invalidate_caps_cache(repopulate=False, heavy=True)
            except Exception:
                pass
            self._caps_shapes_cache = None
            self._sync_placeholder_tabs()
            self._update_bridges_tab_title()
            # Tab-title warning counts: deferred like after a solve (the via
            # rows take seconds on a big board).
            QTimer.singleShot(0, self._init_nodes_warn_count)
            QTimer.singleShot(0, self._init_vias_warn_count)
        finally:
            QApplication.restoreOverrideCursor()
        self._render()
        idx = self.tabs.currentIndex()
        if idx != getattr(self, "_heatmap_tab_index", 0):
            if idx == getattr(self, "_topology_tab_index", -1):
                self._populate_topology()
            self._on_tabs_current_changed(idx)
        self._update_whatif_banner()

    def _update_whatif_banner(self) -> None:
        banner = getattr(self, "_whatif_banner", None)
        if banner is None:
            return
        view = self._whatif_view
        s = self._whatif_selected()
        name = s.name if s is not None else "?"
        if view == "whatif":
            text = f"Viewing what-if: {name}  -  every tab shows this result"
        elif view == "diff":
            mode = self.mode_combo.currentText()
            if mode in _DIFF_MODES:
                text = (f"Difference: {name} − base  ·  {mode}  ·  "
                        "red = worse, blue = better")
                methods = (self._whatif_diff or {}).get("methods") or set()
                if methods - {"identical"}:
                    text += "  (meshes differ: interpolated)"
            else:
                text = (f"Difference: {name} − base  ·  not available in "
                        f"{mode} mode - showing base")
        else:
            banner.hide()
            return
        banner.setText(text)
        banner.show()
        self._position_whatif_banner()

    # --- render hooks (called from render.py) -----------------------------

    def _whatif_begin_render(self, mode: str) -> dict | None:
        """Set ``_whatif_diff_live`` for this render and return it."""
        diff = getattr(self, "_whatif_diff", None)
        live = diff if (diff is not None and mode in _DIFF_MODES) else None
        self._whatif_diff_live = live
        return live

    def _whatif_diff_values(self, layer_index: int, derive_fn,
                            base_vs: np.ndarray) -> np.ndarray:
        """What-if minus base, per vertex of the base layer's mesh."""
        d = self._whatif_diff_live
        name = self.solution.problem.layers[layer_index].name
        j = d["index_by_name"].get(name)
        if j is None or base_vs.size == 0:
            return np.zeros_like(base_vs)
        from fypa.altium_geometry import solve_copper_conductivity
        sigma = solve_copper_conductivity(d["metadata"], name)
        xys, tris, vals = W.layer_vertex_field(
            d["solution"], j, derive_fn, sigma)
        geom = self._layer_geometry(layer_index)
        mapped, method = W.map_field_to_points(
            xys, tris, vals, geom["xs"], geom["ys"])
        d["methods"].add(method)
        out = mapped - base_vs
        out[~np.isfinite(out)] = 0.0
        return out

    def _whatif_diff_cmap(self, mode: str) -> str:
        # Red always means "worse": for voltages a fall is worse.
        return "RdBu" if mode in _HIGHER_IS_BETTER_MODES else "RdBu_r"

    @staticmethod
    def _whatif_half_cmap(name: str, half: str) -> str:
        """Name of one half of a diverging colormap, registered on first
        use: ``"low"`` runs from the low end to the white centre, ``"high"``
        from the centre to the high end. Used when every change has the
        same sign, so 0 (white) sits at one end of the key."""
        import matplotlib
        reg_name = f"fypa_{name}_{half}"
        if reg_name not in matplotlib.colormaps:
            from matplotlib.colors import LinearSegmentedColormap
            base = matplotlib.colormaps[name]
            span = (np.linspace(0.0, 0.5, 256) if half == "low"
                    else np.linspace(0.5, 1.0, 256))
            matplotlib.colormaps.register(
                LinearSegmentedColormap.from_list(reg_name, base(span), N=256),
                name=reg_name)
        return reg_name

    # --- comparison table ---------------------------------------------------

    def _whatif_node_rows_for(self, solution, metadata) -> list[dict]:
        from fypa.solution_sampling import SolutionSampler, compute_node_rows
        _names, rail_to_members = self._compute_rail_groups(metadata)
        rows = compute_node_rows(SolutionSampler(solution, metadata),
                                 rail_to_members)
        net_to_rail = {n: rail for rail, members in rail_to_members.items()
                       for n in members}
        for r in rows:
            r["rail"] = net_to_rail.get(r.get("net"))
        return rows

    def _whatif_rows(self, s: W.WhatIfScenario) -> tuple | None:
        key = (s.id, "rows")
        cached = self._whatif_rows_cache.get(key)
        if cached is not None:
            return cached
        pair = self._whatif_load(s)
        if pair is None:
            return None
        base_sol, base_meta = self._whatif_base_pair()
        b_key = ("__base__", id(base_sol))
        base_rows = self._whatif_rows_cache.get(b_key)
        if base_rows is None:
            base_rows = self._whatif_node_rows_for(base_sol, base_meta)
            self._whatif_rows_cache[b_key] = base_rows
            self._whatif_rows_cache[("__base_summary__", id(base_sol))] = (
                W.summarise(base_rows, base_sol))
        scen_rows = self._whatif_node_rows_for(*pair)
        out = (base_rows, scen_rows)
        self._whatif_rows_cache[key] = out
        return out

    def _refresh_whatif_compare(self) -> None:
        table = getattr(self, "_whatif_table", None)
        if table is None:
            return
        t = _T()
        s = self._whatif_selected()
        summ = self._whatif_summary_table
        table.setRowCount(0)
        summ.setRowCount(0)
        status = ""
        if s is None or s.status != W.STATUS_OK or not self._whatif_base_is_solved():
            if s is not None and s.status == W.STATUS_NOT_RUN:
                status = "Run the what-if to compare it with the base."
            self._whatif_status.setText(status)
            return
        if getattr(self, "_whatif_panel", None) is not None and not (
                self._whatif_panel.isVisible()):
            # Comparing samples every node; skip while nobody can see it.
            return
        try:
            rows = self._whatif_rows(s)
        except Exception:
            _log.warning("What-if comparison failed", exc_info=True)
            rows = None
        if rows is None:
            self._whatif_status.setText(
                "The result file for this what-if is missing - press Run.")
            return
        base_rows, scen_rows = rows
        base_sol, _bm = self._whatif_base_pair()
        base_summary = self._whatif_rows_cache.get(
            ("__base_summary__", id(base_sol))) or {}
        # The stored summary has the copper loss (needs the full solution);
        # where each worst case sits is re-read from the rows, so results
        # saved before rails were recorded still name them.
        scen_summary = {**(s.summary or {}), **{
            k: v for k, v in W.summarise(scen_rows).items()
            if k.endswith(("_rail", "_node")) or k == "fails_rails"}}
        colors = {"better": t["ok"], "worse": t["err"], "same": t["fg_hint"]}

        def _cell(text, verdict="", align=Qt.AlignRight, sort=None):
            # Rows are ordered worst change first; no click-to-sort (a
            # Python __lt__ on table items crashes PySide6).
            it = QTableWidgetItem(text)
            it.setTextAlignment(int(align | Qt.AlignVCenter))
            if verdict in colors:
                it.setForeground(_qcolor(colors[verdict]))
            return it

        heads = W.compare_summaries(base_summary, scen_summary)
        summ.setRowCount(len(heads))
        for r, h in enumerate(heads):
            summ.setItem(r, 0, _cell(h["label"], align=Qt.AlignLeft))
            rail_item = _cell(h["rail"], align=Qt.AlignLeft)
            if h["rail_tip"]:
                rail_item.setToolTip(h["rail_tip"])
            summ.setItem(r, 1, rail_item)
            summ.setItem(r, 2, _cell(_fmt(h["base"], 1.0, h["unit"])))
            summ.setItem(r, 3, _cell(_fmt(h["whatif"], 1.0, h["unit"]),
                                     h["verdict"]))
            arrow = {"better": " ▲", "worse": " ▼"}.get(h["verdict"], "")
            summ.setItem(r, 4, _cell(_fmt_delta(h["delta"]) + arrow,
                                     h["verdict"]))

        metric = self._whatif_metric_combo.currentData() or "drop"
        _key, _label, unit, scale, _d = next(
            m for m in W.COMPARE_METRICS if m[0] == metric)
        cmp_rows = W.compare_node_rows(base_rows, scen_rows, metric)
        if self._whatif_changed_only.isChecked():
            cmp_rows = [r for r in cmp_rows if r["verdict"] in
                        ("better", "worse") or (r["base"] is None)
                        != (r["whatif"] is None)]
        # Worst change first.
        cmp_rows.sort(key=lambda r: (
            0 if r["verdict"] == "worse" else 1 if r["verdict"] == "better"
            else 2, -abs(r["delta"] or 0.0)))
        table.setRowCount(len(cmp_rows))
        hdr = table.horizontalHeaderItem(2)
        if hdr is not None:
            table.setHorizontalHeaderLabels(
                ["Node", "Net", f"Base ({unit})" if unit else "Base",
                 f"What-if ({unit})" if unit else "What-if", "Δ"])
        n_better = n_worse = 0
        for r, row in enumerate(cmp_rows):
            v = row["verdict"]
            n_better += v == "better"
            n_worse += v == "worse"
            node = f"{row['designator']}.{row['pad'] or '?'}"
            if row.get("terminal") and row["terminal"] != "P":
                node += f" ({row['terminal']})"
            table.setItem(r, 0, _cell(node, align=Qt.AlignLeft))
            table.setItem(r, 1, _cell(str(row.get("net") or ""),
                                      align=Qt.AlignLeft))
            table.setItem(r, 2, _cell(_fmt(row["base"], scale),
                                      sort=(row["base"] or 0.0) * scale))
            table.setItem(r, 3, _cell(_fmt(row["whatif"], scale), v,
                                      sort=(row["whatif"] or 0.0) * scale))
            arrow = {"better": " ▲", "worse": " ▼"}.get(v, "")
            table.setItem(r, 4, _cell(_fmt_delta(row["delta"], scale) + arrow,
                                      v, sort=(row["delta"] or 0.0) * scale))
        stale = s.is_stale(self._whatif_base_fp)
        status = (f"{n_better} node(s) better, {n_worse} worse "
                  f"(of {len(base_rows)}).")
        if stale:
            status += " Out of date - run again for the current base."
        self._whatif_status.setText(status)


def _qcolor(hex_or_name: str):
    from PySide6.QtGui import QColor
    return QColor(hex_or_name)
