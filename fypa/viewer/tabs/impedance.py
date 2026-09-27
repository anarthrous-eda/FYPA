"""The Impedance tab: rail model and Z(f) plot."""
from __future__ import annotations

import math
from typing import NamedTuple
import matplotlib
import matplotlib.colors
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.numeric import _numeric_validator, _NumericCellDelegate, _parse_numeric_text
from fypa.viewer.tabs.capacitors import _PKG_CANONICAL_ROLE
from fypa.viewer.theme import _T
from fypa.viewer.widgets import (
    _esc,
    _make_floating_tooltip,
    _move_tooltip_to_cursor,
    _qt_widget_alive,
)


# Capacitor traces beyond this many carry no legend label. A rail with dozens
# of decoupling caps otherwise produces a legend taller than the axes, hiding
# the |Z| trace and the anti-resonance markers the tab exists to show; the
# unlabelled traces stay identifiable by colour and the hover tooltip.
_IMP_MAX_LEGEND_BRANCHES = 8




class _ImpBranch(NamedTuple):
    """One capacitor's |Z| trace, plus everything the hover hit-test needs.

    ``log_f`` / ``log_z`` are precomputed per replot: the hit-test runs on
    every mouse-move, and recomputing ``log10`` over the whole sweep there made
    each event O(branches x samples) on the GUI thread. ``color`` is kept so
    the highlight can be undone without re-deriving the palette.
    """

    line: object
    designator: str
    freqs: object
    z: object
    log_f: object
    log_z: object
    color: str




def _branch_colors(n: int) -> list[str]:
    """Distinct colours for the per-capacitor traces.

    They were all one muted grey, which made the per-designator legend N
    identical swatches — no way to map a designator to a trace, which is the
    whole point of the "Show individual capacitors" checkbox. tab20 reads
    acceptably on both the light and the dark theme.
    """
    cmap = matplotlib.colormaps["tab20"]
    return [matplotlib.colors.to_hex(cmap(i % cmap.N)) for i in range(n)]


class _ImpedanceTabMixin:
    """The Impedance tab: rail model and Z(f) plot."""

    # --- Impedance tab -----------------------------------------------------
    #
    # Per-rail PDN impedance Z(f) against a target mask (TI SWPA222A §4). The
    # capacitor loop inductances the Capacitors tab extracts are what make this
    # more than a spreadsheet: every branch's series resonance sits where its
    # *mounted* L puts it, not where its datasheet ESL alone would.

    def _build_impedance_tab(self) -> QWidget:
        """Setup panel (rail, target mask, VRM, package library) beside an
        embedded matplotlib canvas showing |Z(f)|."""
        from matplotlib.backends.backend_qtagg import (
            FigureCanvasQTAgg,
            NavigationToolbar2QT,
        )
        from matplotlib.figure import Figure

        widget = QWidget(self.tabs)
        outer = QHBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)
        t = _T()

        # ----- left: setup -----
        side = QVBoxLayout()
        side.setSpacing(8)

        rail_row = QHBoxLayout()
        rail_row.addWidget(QLabel("Rail:"))
        self.imp_rail_combo = QComboBox()
        self.imp_rail_combo.setToolTip(
            "Rails carrying at least one included decoupling capacitor.")
        self.imp_rail_combo.currentTextChanged.connect(
            self._on_impedance_rail_changed)
        rail_row.addWidget(self.imp_rail_combo, 1)
        side.addLayout(rail_row)

        # Target mask — Z_target = V · ripple% / I_transient (TI §4).
        mask_box = QGroupBox("Target impedance (TI SWPA222A §4)")
        mask_form = QFormLayout(mask_box)
        self.imp_ripple_edit = QLineEdit()
        self.imp_ripple_edit.setToolTip(
            "Allowed rail ripple as a percentage of the nominal voltage.")
        self.imp_itran_edit = QLineEdit()
        self.imp_itran_edit.setToolTip(
            "Transient (step) current the device draws. Z_target = "
            "V · ripple% / I_transient.")
        self.imp_fmax_edit = QLineEdit()
        self.imp_fmax_edit.setToolTip(
            "F_MAX — the frequency up to which the mask must be met. Beyond "
            "it, plane spreading and package inductance dominate and adding "
            "capacitors no longer helps.")
        for e in (self.imp_ripple_edit, self.imp_itran_edit,
                  self.imp_fmax_edit):
            e.setValidator(_numeric_validator(self, top=1e12))
            e.setMaximumWidth(120)
        mask_form.addRow("Ripple (%)", self.imp_ripple_edit)
        mask_form.addRow("Transient current (A)", self.imp_itran_edit)
        mask_form.addRow("F_MAX (MHz)", self.imp_fmax_edit)
        self.imp_ztarget_label = QLabel("—")
        self.imp_ztarget_label.setStyleSheet(
            f"QLabel {{ color: {t['fg_muted']}; }}")
        mask_form.addRow("Z_target", self.imp_ztarget_label)
        side.addWidget(mask_box)

        # VRM — sets the low-frequency floor.
        vrm_box = QGroupBox("Voltage regulator")
        vrm_form = QFormLayout(vrm_box)
        self.imp_vrm_r_edit = QLineEdit()
        self.imp_vrm_r_edit.setToolTip(
            "Closed-loop output resistance of the regulator. This is the "
            "floor |Z| settles to at DC.")
        self.imp_vrm_l_edit = QLineEdit()
        self.imp_vrm_l_edit.setToolTip(
            "Output inductance of the regulator including its path to the "
            "plane. It makes the VRM branch give up above its bandwidth.")
        for e in (self.imp_vrm_r_edit, self.imp_vrm_l_edit):
            e.setValidator(_numeric_validator(self, top=1e12))
            e.setMaximumWidth(120)
        vrm_form.addRow("R (mΩ)", self.imp_vrm_r_edit)
        vrm_form.addRow("L (nH)", self.imp_vrm_l_edit)
        side.addWidget(vrm_box)

        # Plane-pair capacitance — the only thing holding the rail down above
        # the last capacitor's self-resonance.
        plane_box = QGroupBox("Plane-pair capacitance")
        plane_layout = QVBoxLayout(plane_box)
        self.imp_plane_check = QCheckBox("Include in the model")
        self.imp_plane_check.setChecked(True)
        self.imp_plane_check.setToolTip(
            "ε0·εr·A/h over the rail's reference cavity, using the stackup's "
            "extracted Dk. Small, but it sets the high-frequency tail.")
        self.imp_plane_check.toggled.connect(self._replot_impedance)
        plane_layout.addWidget(self.imp_plane_check)
        self.imp_plane_label = QLabel("—")
        self.imp_plane_label.setStyleSheet(
            f"QLabel {{ color: {t['fg_muted']}; }}")
        self.imp_plane_label.setWordWrap(True)
        plane_layout.addWidget(self.imp_plane_label)
        side.addWidget(plane_box)

        self.imp_show_branches = QCheckBox("Show individual capacitors")
        self.imp_show_branches.setToolTip(
            "Draw each capacitor's own |Z| faintly. Hover a trace to "
            "highlight it and show its designator; the legend lists all "
            "included parts.")
        self.imp_show_branches.toggled.connect(self._replot_impedance)
        side.addWidget(self.imp_show_branches)

        apply_btn = QPushButton("Apply && Recompute")
        apply_btn.setToolTip(
            "Apply the mask and VRM values above and redraw. They persist in "
            "the .fypa project file, per rail.")
        apply_btn.clicked.connect(self._on_impedance_apply)
        side.addWidget(apply_btn)

        side.addWidget(self._build_package_library_box())
        side.addStretch(1)

        side_wrap = QWidget()
        side_wrap.setLayout(side)
        side_wrap.setMaximumWidth(380)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(side_wrap)
        scroll.setMaximumWidth(400)
        outer.addWidget(scroll)

        # ----- right: the plot -----
        right = QVBoxLayout()
        self._imp_figure = Figure(figsize=(7, 5), layout="constrained")
        self._imp_canvas = FigureCanvasQTAgg(self._imp_figure)
        self._imp_axes = self._imp_figure.add_subplot(111)
        self._imp_branch_artists: list[_ImpBranch] = []
        self._imp_branch_highlighted = None
        # This tab is destroyed and rebuilt on every theme change, taking the
        # canvas and the tooltip parented to it. Drop the stale reference here
        # or the next hover resolves a deleted C++ object.
        self._imp_branch_tooltip = None
        self._imp_canvas.mpl_connect(
            "motion_notify_event", self._on_imp_branch_hover)
        # Leaving delivers a leave event, not a final motion event.
        self._imp_canvas.mpl_connect(
            "figure_leave_event", self._on_imp_branch_leave)
        self._imp_canvas.mpl_connect(
            "axes_leave_event", self._on_imp_branch_leave)
        right.addWidget(NavigationToolbar2QT(self._imp_canvas, widget))
        right.addWidget(self._imp_canvas, 1)

        self.imp_summary_label = QLabel("")
        self.imp_summary_label.setWordWrap(True)
        self.imp_summary_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        right.addWidget(self.imp_summary_label)
        outer.addLayout(right, 1)
        self._impedance_stack = self._wrap_placeholder(widget)
        self._sync_placeholder_tabs()
        return self._impedance_stack

    def _build_package_library_box(self) -> QWidget:
        """The editable SMD case-size table: typical ESL / ESR per package.

        Read by every capacitor whose footprint carries a recognisable case
        code; a per-part override on the Capacitors tab beats it. Only SMD chip
        packages are listed, because they are the only ones whose parasitics a
        case size predicts."""
        box = QGroupBox("SMD package library (typical values)")
        layout = QVBoxLayout(box)
        note = QLabel(
            "Defaults for X5R/X7R MLCCs. Edit a cell to change every "
            "capacitor of that case size; override a single part on the "
            "Capacitors tab. Non-SMD parts have no entry here and need a "
            "per-part override to be modelled at all.")
        note.setWordWrap(True)
        note.setStyleSheet(f"QLabel {{ color: {_T()['fg_muted']}; }}")
        layout.addWidget(note)

        conv_row = QHBoxLayout()
        conv_row.addWidget(QLabel("Case size convention:"))
        self.imp_footprint_conv_combo = self._build_footprint_convention_combo()
        conv_row.addWidget(self.imp_footprint_conv_combo, 1)
        layout.addLayout(conv_row)

        self.imp_pkg_table = QTableWidget()
        self.imp_pkg_table.setColumnCount(3)
        self.imp_pkg_table.setHorizontalHeaderLabels(
            ["Package", "ESL (nH)", "ESR (mΩ)"])
        self.imp_pkg_table.verticalHeader().setVisible(False)
        self.imp_pkg_table.setAlternatingRowColors(True)
        self.imp_pkg_table.horizontalHeader().setStretchLastSection(True)
        self.imp_pkg_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.imp_pkg_table.setMinimumHeight(220)
        self._populate_package_table()
        self.imp_pkg_table.setItemDelegate(_NumericCellDelegate(self.imp_pkg_table))
        self.imp_pkg_table.itemChanged.connect(self._on_package_item_changed)
        layout.addWidget(self.imp_pkg_table)

        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._on_package_reset)
        layout.addWidget(reset)
        return box

    def _populate_package_table(self) -> None:
        lib = self._caploop_package_library()
        table = self.imp_pkg_table
        convention = self._footprint_convention()
        self._imp_pkg_populating = True
        try:
            self._fill_package_rows(table, lib, convention)
        finally:
            # Without this, an exception mid-fill leaves the flag set and
            # _on_package_item_changed silently discards EVERY later ESL/ESR
            # edit for the rest of the session.
            self._imp_pkg_populating = False
        table.resizeColumnsToContents()

    def _fill_package_rows(self, table, lib, convention: str) -> None:
        from fypa.caploop.packages import format_package_label

        table.setRowCount(len(lib))
        for r, model in enumerate(lib):
            name = QTableWidgetItem(
                format_package_label(model.name, convention))
            name.setData(_PKG_CANONICAL_ROLE, model.name)
            name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            table.setItem(r, 0, name)
            for c, value in ((1, model.esl_nh), (2, model.esr_mohm)):
                item = QTableWidgetItem(f"{value:.4g}")
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, item)

    def _on_footprint_convention_changed(self, _index: int = 0) -> None:
        sender = self.sender()
        if isinstance(sender, QComboBox):
            value = sender.currentData()
        else:
            combo = getattr(self, "imp_footprint_conv_combo", None)
            value = combo.currentData() if combo else None
        if not value:
            return
        self._set_footprint_convention(str(value))
        for attr in ("caps_footprint_conv_combo", "imp_footprint_conv_combo"):
            combo = getattr(self, attr, None)
            if combo is not None and combo is not sender:
                combo.blockSignals(True)
                idx = combo.findData(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        if getattr(self, "imp_pkg_table", None) is not None:
            self._populate_package_table()
        # heavy=False: the convention only remaps a footprint STRING to a
        # package key. Copper geometry, escape-via clustering and the
        # plane-pair cavity are untouched, so discarding those caches cost a
        # multi-second re-identification per toggle for no change in result.
        self._invalidate_caps_cache(heavy=False)
        # Every cap's package -- and so its library ESL/ESR and the
        # anti-resonance -- just changed, leaving the plotted curve, the
        # summary and the skipped list stale. Both sibling handlers do this.
        if getattr(self, "_impedance_populated", False):
            self._replot_impedance()

    def _on_package_item_changed(self, item) -> None:
        """Commit an edited ESL / ESR back to the library, persist it, and
        recompute every capacitor that uses that package."""
        if getattr(self, "_imp_pkg_populating", False) or item.column() == 0:
            return
        table = self.imp_pkg_table
        name_item = table.item(item.row(), 0)
        package = name_item.data(_PKG_CANONICAL_ROLE) if name_item else None
        if not package:
            # Falling back to the cell TEXT is precisely wrong here: under the
            # metric convention that text is a display label ("1005"), never a
            # library key, so lib.get() returns None and every branch below
            # raises AttributeError or KeyError. A row without the canonical
            # role is not editable.
            return
        lib = self._caploop_package_library()
        model = lib.get(package)
        if model is None:
            return
        try:
            value = _parse_numeric_text(item.text())
            if value < 0.0:
                raise ValueError
        except ValueError:
            bad = item.text().strip()
            self._imp_pkg_populating = True
            item.setText(f"{(model.esl_nh if item.column() == 1 else model.esr_mohm):.4g}")
            self._imp_pkg_populating = False
            # Reverting in silence looks like the edit simply vanished. Say
            # what was rejected — a comma decimal is the likely cause on a
            # locale that formats numbers that way.
            QMessageBox.warning(
                self, "Invalid value",
                f"{'ESL' if item.column() == 1 else 'ESR'} for {package}: "
                f"{bad!r} is not a non-negative number. Use a dot decimal "
                f"separator (0.5, not 0,5)."
            )
            return
        esl_h = value * 1e-9 if item.column() == 1 else model.esl_h
        esr_ohm = value * 1e-3 if item.column() == 2 else model.esr_ohm
        lib.set_values(package, esl_h, esr_ohm)

        proj = self._ensure_project()
        proj.viewer_settings["caploop_packages"] = lib.to_dict()
        self._display_dirty = True
        # Package parasitics are per-part inputs, not geometry: the cheap
        # invalidation is enough.
        self._invalidate_caps_cache()
        self._replot_impedance()

    def _on_package_reset(self) -> None:
        lib = self._caploop_package_library()
        lib.reset()
        proj = self._ensure_project()
        proj.viewer_settings["caploop_packages"] = lib.to_dict()
        self._display_dirty = True
        self._populate_package_table()
        self._invalidate_caps_cache()
        self._replot_impedance()

    # --- rail model ---------------------------------------------------------

    def _impedance_rails(self) -> list[str]:
        rows = getattr(self, "_caps_rows_cache", None) or []
        seen: list[str] = []
        for row in rows:
            if row.get("included", True) and row["rail"] not in seen:
                seen.append(row["rail"])
        return seen

    def _rail_plane_capacitance_f(self, rail: str) -> tuple[float, str]:
        """(C_plane, explanation) for the rail's dominant reference cavity.

        Cached per rail — it needs one shapely intersection of two plane
        sheets, which is not free on a big board.
        """
        cache = getattr(self, "_imp_plane_cache", None)
        if cache is None:
            cache = self._imp_plane_cache = {}
        if rail in cache:
            return cache[rail]

        from fypa.caploop.impedance import DEFAULT_DK, plane_capacitance_f
        from fypa.caploop.tier2_fem import Tier2Error, build_cavity_sheet

        rows = [r for r in (getattr(self, "_caps_rows_cache", None) or [])
                if r["rail"] == rail and r.get("included", True)
                and r["cap"].cavity is not None]
        extracted = getattr(getattr(self, "_loaded_project", None),
                            "extracted", None)
        if not rows or extracted is None:
            cache[rail] = (0.0, "No reference cavity on this rail.")
            return cache[rail]

        # The cavity most of the rail's capacitors actually reference.
        counts: dict[tuple[int, int], int] = {}
        for r in rows:
            cav = r["cap"].cavity
            key = (cav.layer_rail, cav.layer_return)
            counts[key] = counts.get(key, 0) + 1
        (layer_rail, layer_return) = max(counts, key=counts.get)
        cap = next(r["cap"] for r in rows
                   if (r["cap"].cavity.layer_rail,
                       r["cap"].cavity.layer_return) == (layer_rail,
                                                         layer_return))
        cav = cap.cavity

        net_index = {n.name: i for i, n in enumerate(extracted.nets)}
        members = self._rail_to_members.get(rail, [rail])
        rail_idx = {net_index[m] for m in members if m in net_index}
        return_idx = ({net_index[cap.return_net]}
                      if cap.return_net in net_index else set())
        try:
            sheet = build_cavity_sheet(
                layer_rail, layer_return, rail_idx, return_idx,
                self._cap_net_layer_shapes(extracted), cav.h_cav_mm, "plane")
        except Tier2Error as e:
            cache[rail] = (0.0, f"No plane-pair capacitance: {e}.")
            return cache[rail]

        # Dk of the gap: it belongs to the upper of the two copper layers.
        stackup = {s.layer_id: s for s in extracted.stackup}
        upper = (layer_rail if cav.z_rail_mm <= cav.z_return_mm
                 else layer_return)
        dk = getattr(stackup.get(upper), "dielectric_dk", None)
        area = sheet.shape.area
        c = plane_capacitance_f(area, cav.h_cav_mm, dk)
        note = (
            f"{c * 1e9:.3g} nF — {cav.name_rail} ↔ {cav.name_return}, "
            f"{area:.0f} mm² at {cav.h_cav_mm:.3f} mm, "
            f"Dk {dk:.2f}" if dk else
            f"{c * 1e9:.3g} nF — {cav.name_rail} ↔ {cav.name_return}, "
            f"{area:.0f} mm² at {cav.h_cav_mm:.3f} mm, "
            f"Dk {DEFAULT_DK} (not in the stackup)")
        cache[rail] = (c, note)
        return cache[rail]

    def _compute_rail_impedance(self, rail: str):
        """Assemble and solve Z(f) for one rail from the capacitor rows."""
        from fypa.caploop.impedance import (
            RailTarget,
            VrmModel,
            cap_branch,
            log_freqs,
            rail_impedance,
        )

        rows = [r for r in (getattr(self, "_caps_rows_cache", None) or [])
                if r["rail"] == rail and r.get("included", True)]
        library = self._caploop_package_library()
        cfg = self._caploop_rail_config(rail)

        voltage = next((r["design_voltage_v"] for r in rows
                        if r.get("design_voltage_v")), None)
        branches, skipped = [], []
        for row in rows:
            l_mount_nh = self._cap_l_best_nh(row)
            branch, reason = cap_branch(
                row["designator"], row.get("capacitance_f"),
                None if l_mount_nh is None else l_mount_nh * 1e-9,
                row.get("package"), library,
                esr_override=(row["esr_ohm"] if row.get("esr_is_override")
                              else None),
                esl_override=(row["esl_h"] if row.get("esl_is_override")
                              else None),
            )
            if branch is None:
                skipped.append((row["designator"], reason))
            else:
                branches.append(branch)

        target = RailTarget(
            rail=rail, voltage_v=voltage or 0.0,
            ripple_pct=cfg["ripple_pct"],
            transient_current_a=cfg["transient_current_a"],
            f_max_hz=cfg["f_max_hz"])
        vrm = VrmModel(r_ohm=cfg["vrm_r_ohm"], l_h=cfg["vrm_l_h"])

        c_plane = 0.0
        if getattr(self, "imp_plane_check", None) is None \
                or self.imp_plane_check.isChecked():
            c_plane = self._rail_plane_capacitance_f(rail)[0]

        return rail_impedance(rail, log_freqs(), branches, target, vrm,
                              c_plane, skipped)

    # --- tab lifecycle ------------------------------------------------------

    def _populate_impedance_tab(self) -> None:
        """First activation: make sure the capacitor rows exist (they carry
        the mounted inductances), then fill the rail list and plot."""
        def _ready() -> None:
            rails = self._impedance_rails()
            combo = self.imp_rail_combo
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(rails)
            combo.blockSignals(False)
            if rails:
                self._load_impedance_rail_config(rails[0])
            self._sync_footprint_convention_ui()
            self._replot_impedance()

        self._ensure_cap_rows_async(_ready)

    def _load_impedance_rail_config(self, rail: str) -> None:
        cfg = self._caploop_rail_config(rail)
        self.imp_ripple_edit.setText(
            self._fmt_settings_value(cfg["ripple_pct"]))
        self.imp_itran_edit.setText(
            self._fmt_settings_value(cfg["transient_current_a"]))
        self.imp_fmax_edit.setText(
            self._fmt_settings_value(cfg["f_max_hz"] / 1e6))
        self.imp_vrm_r_edit.setText(
            self._fmt_settings_value(cfg["vrm_r_ohm"] * 1e3))
        self.imp_vrm_l_edit.setText(
            self._fmt_settings_value(cfg["vrm_l_h"] * 1e9))

    def _on_impedance_rail_changed(self, rail: str) -> None:
        if not rail:
            return
        self._load_impedance_rail_config(rail)
        self._replot_impedance()

    def _on_impedance_apply(self) -> None:
        """Read the setup fields, persist them for this rail, and redraw."""
        rail = self.imp_rail_combo.currentText()
        if not rail:
            return

        def _f(edit, name, scale=1.0):
            # Normalisation belongs to _parse_settings_value; keep the raw
            # text only to quote back what the user actually typed.
            text = edit.text()
            try:
                value = self._parse_settings_value(text)
            except ValueError:
                raise ValueError(f"{name}: {text.strip()!r} is not a number")
            if value < 0.0:
                raise ValueError(f"{name} must be zero or positive")
            return value * scale

        try:
            cfg = {
                "ripple_pct": _f(self.imp_ripple_edit, "Ripple"),
                "transient_current_a": _f(self.imp_itran_edit,
                                          "Transient current"),
                "f_max_hz": _f(self.imp_fmax_edit, "F_MAX", 1e6),
                "vrm_r_ohm": _f(self.imp_vrm_r_edit, "VRM R", 1e-3),
                "vrm_l_h": _f(self.imp_vrm_l_edit, "VRM L", 1e-9),
            }
        except ValueError as e:
            QMessageBox.warning(self, "Invalid value", str(e))
            return
        self._set_caploop_rail_config(rail, cfg)
        self._replot_impedance()

    def _impedance_empty_reason(self) -> str:
        """Why there is no rail to plot.

        The rail list is derived from the capacitor rows, so an empty list has
        three quite different causes and only one of them is "no rail". Saying
        "no rail carries an included decoupling capacitor" to someone who
        imported Gerbers — which carry no component data at all — sends them
        looking for a capacitor that was never going to be found.
        """
        rows = getattr(self, "_caps_rows_cache", None)
        if rows is None:
            return ("Open the Capacitors tab to analyse this design, then "
                    "come back to plot its impedance.")
        if not rows:
            # The Capacitors tab already worked out exactly why (no design
            # info, a Gerber import, no capacitors detected) — reuse it rather
            # than guess.
            return (getattr(self, "_caps_empty_reason", "")
                    or "No decoupling capacitors were found on this design.")
        return (f"All {len(rows)} detected capacitor(s) are excluded from the "
                f"analysis.\nTick Use on the Capacitors tab to include one.")

    def _ensure_imp_branch_tooltip(self) -> QLabel:
        """Floating label for the capacitor trace under the cursor."""
        label = getattr(self, "_imp_branch_tooltip", None)
        # Aliveness, not just presence: _refresh_inline_theme destroys and
        # rebuilds the Impedance tab, taking the canvas and this child label
        # with it, and a cached dead wrapper raises RuntimeError out of a slot.
        if label is not None and _qt_widget_alive(label):
            return label
        label = _make_floating_tooltip(
            self._imp_canvas, font_size="8pt", padding="3px 6px")
        self._imp_branch_tooltip = label
        return label

    def _hide_imp_branch_tooltip(self) -> None:
        label = getattr(self, "_imp_branch_tooltip", None)
        # Unconditional hide: isVisible() is False whenever the Impedance page
        # is not the current tab, so guarding on it left a stale tooltip to
        # reappear with the tab after a background replot.
        if label is not None and _qt_widget_alive(label):
            label.hide()

    def _reset_imp_branch_highlight(self) -> None:
        for entry in self._imp_branch_artists:
            entry.line.set_linewidth(0.9)
            entry.line.set_alpha(0.55)
            entry.line.set_color(entry.color)
            entry.line.set_zorder(2)
        self._imp_branch_highlighted = None
        self._hide_imp_branch_tooltip()

    def _pick_impedance_branch(self, event):
        """Return the branch trace nearest the cursor, in log-log space."""
        if (event.inaxes is not self._imp_axes
                or event.xdata is None or event.ydata is None
                or not self._imp_branch_artists):
            return None
        try:
            ex = math.log10(event.xdata)
            ey = math.log10(event.ydata)
        except ValueError:
            return None
        best = None
        best_d2 = float("inf")
        for entry in self._imp_branch_artists:
            hit, info = entry.line.contains(event)
            if not hit:
                continue
            # The nearest index within the pick radius, not the first one.
            # Near a capacitor's SRF the trace is close to vertical, so
            # contains() returns a long run of indices and ind[0] can be a
            # decade of |Z| away — far enough for a genuinely more distant but
            # flatter neighbour to win and the tooltip to name the wrong part.
            for i in info.get("ind", ()):
                if not 0 <= i < len(entry.log_f):
                    continue
                d2 = ((entry.log_f[i] - ex) ** 2
                      + (entry.log_z[i] - ey) ** 2)
                if d2 < best_d2:
                    best_d2 = d2
                    best = entry
        return best

    def _highlight_impedance_branch(self, entry, event) -> None:
        # Keyed on the artist, not the designator: a duplicate refdes would
        # otherwise short-circuit the restyle and leave the highlight on the
        # first trace while the tooltip reported the second's values.
        restyle = self._imp_branch_highlighted is not entry.line
        if restyle:
            self._reset_imp_branch_highlight()
            # Keep the branch's own colour. Recolouring to the accent made the
            # highlight indistinguishable from the |Z| total trace (also
            # accent) exactly where the two run together, and left the legend
            # swatch — a copy taken at ax.legend() time — disagreeing with the
            # line. Weight, opacity and z-order carry the highlight instead.
            entry.line.set_linewidth(2.4)
            entry.line.set_alpha(1.0)
            entry.line.set_zorder(6)
            self._imp_branch_highlighted = entry.line

        idx = int(np.argmin(np.abs(entry.log_f - math.log10(event.xdata))))
        label = self._ensure_imp_branch_tooltip()
        label.setText(
            f"{entry.designator}  |Z|={entry.z[idx] * 1e3:.3g} mΩ @ "
            f"{entry.freqs[idx] / 1e6:.3g} MHz")
        label.adjustSize()
        _move_tooltip_to_cursor(label)
        label.show()
        label.raise_()
        if restyle:
            self._imp_canvas.draw_idle()

    def _on_imp_branch_leave(self, _event=None) -> None:
        """Reset when the cursor leaves the axes or the canvas.

        Leaving delivers a leave event, not another motion event, so without
        this a cursor that exits over a trace — into the navigation toolbar, or
        straight off the window — leaves the branch highlighted and the tooltip
        floating indefinitely.
        """
        if self._imp_branch_highlighted is not None:
            self._reset_imp_branch_highlight()
            self._imp_canvas.draw_idle()
        else:
            self._hide_imp_branch_tooltip()

    def _on_imp_branch_hover(self, event) -> None:
        if not getattr(self, "imp_show_branches", None):
            return
        if not self.imp_show_branches.isChecked():
            if self._imp_branch_highlighted is not None:
                self._reset_imp_branch_highlight()
                self._imp_canvas.draw_idle()
            return
        entry = self._pick_impedance_branch(event)
        if entry is None:
            if self._imp_branch_highlighted is not None:
                self._reset_imp_branch_highlight()
                self._imp_canvas.draw_idle()
            return
        self._highlight_impedance_branch(entry, event)

    def _replot_impedance(self, *_args) -> None:
        """Redraw |Z(f)| for the selected rail."""
        if getattr(self, "_imp_axes", None) is None:
            return
        rail = self.imp_rail_combo.currentText()
        ax = self._imp_axes
        ax.clear()
        self._imp_branch_artists = []
        self._imp_branch_highlighted = None
        self._hide_imp_branch_tooltip()
        t = _T()

        if not rail:
            ax.set_axis_off()
            # Style before drawing: an unstyled axes is white with black text,
            # which is invisible-adjacent on the dark theme.
            self._style_impedance_axes(ax)
            ax.text(0.5, 0.5, self._impedance_empty_reason(),
                    ha="center", va="center", transform=ax.transAxes,
                    color=t["fg"], fontsize=9, wrap=True)
            self.imp_summary_label.setText("")
            self.imp_ztarget_label.setText("—")
            self.imp_plane_label.setText("—")
            self._imp_canvas.draw_idle()
            return

        result = self._compute_rail_impedance(rail)
        self.imp_ztarget_label.setText(
            "—" if not math.isfinite(result.target.z_target_ohm)
            else f"{result.target.z_target_ohm * 1e3:.4g} mΩ")
        self.imp_plane_label.setText(self._rail_plane_capacitance_f(rail)[1])

        freqs = result.freqs_hz
        unlabelled_branches = 0
        if self.imp_show_branches.isChecked():
            from fypa.caploop.impedance import branch_impedance
            omega = 2.0 * math.pi * freqs
            log_f = np.log10(freqs)
            colors = _branch_colors(len(result.branches))
            for i, branch in enumerate(result.branches):
                z_branch = np.abs(branch_impedance(branch, omega))
                labelled = i < _IMP_MAX_LEGEND_BRANCHES
                (line,) = ax.loglog(
                    freqs, z_branch, lw=0.9, alpha=0.55, color=colors[i],
                    label=branch.designator if labelled else "_nolegend_",
                    zorder=2)
                line.set_picker(8)
                self._imp_branch_artists.append(_ImpBranch(
                    line, branch.designator, freqs, z_branch,
                    log_f, np.log10(z_branch), colors[i]))
            unlabelled_branches = max(
                0, len(result.branches) - _IMP_MAX_LEGEND_BRANCHES)

        ax.loglog(freqs, result.z_mag, lw=1.8, color=t["accent"],
                  label=f"|Z| — {rail}", zorder=3)

        z_t = result.target.z_target_ohm
        if math.isfinite(z_t):
            f_max = result.target.f_max_hz
            ax.hlines(z_t, freqs[0], f_max, colors=t["err"], linestyles="--",
                      lw=1.4, zorder=4,
                      label=f"Z_target {z_t * 1e3:.3g} mΩ to "
                            f"{f_max / 1e6:.3g} MHz")
            ax.axvline(f_max, color=t["err"], ls=":", lw=1.0, alpha=0.6)

        peaks = [a for a in result.antiresonances
                 if a.freq_hz <= result.target.f_max_hz]
        # Only the worst few, or a busy rail buries the plot in markers.
        for peak in sorted(peaks, key=lambda a: -a.z_ohm)[:3]:
            ax.plot([peak.freq_hz], [peak.z_ohm], "v", ms=7, zorder=5,
                    color=t["err"] if peak.exceeds_target else t["warn_fg"])
            ax.annotate(f"{peak.freq_hz / 1e6:.3g} MHz\n"
                        f"{peak.z_ohm * 1e3:.3g} mΩ",
                        (peak.freq_hz, peak.z_ohm),
                        textcoords="offset points", xytext=(6, 6),
                        fontsize=7, color=t["fg"])

        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("|Z| (Ω)")
        ax.set_title(f"PDN impedance — {rail}")
        # The F_MAX rule and the target mask both stop short of the sweep's
        # ends, and matplotlib would autoscale to their extents — pin the axis
        # to the swept band instead of leaving dead space beside the trace.
        ax.set_xlim(freqs[0], freqs[-1])
        ax.grid(True, which="both", alpha=0.25)
        legend_ncol = 1
        if self.imp_show_branches.isChecked() and len(result.branches) > 5:
            legend_ncol = 2
        ax.legend(loc="upper left", fontsize=8, ncol=legend_ncol)
        self._style_impedance_axes(ax)
        self._imp_canvas.draw_idle()
        summary = self._impedance_summary_html(result)
        if unlabelled_branches:
            summary += (
                f"<br><span style='color:{t['fg_muted']};'>Legend names the "
                f"first {_IMP_MAX_LEGEND_BRANCHES} capacitors; "
                f"{unlabelled_branches} more are drawn unlabelled — hover any "
                f"trace to identify it.</span>"
            )
        self.imp_summary_label.setText(summary)

    def _style_impedance_axes(self, ax) -> None:
        """Match the plot to the app theme (matplotlib defaults are light)."""
        t = _T()
        self._imp_figure.set_facecolor(t["bg"])
        ax.set_facecolor(t["bg"])
        for spine in ax.spines.values():
            spine.set_color(t["border"])
        ax.tick_params(colors=t["fg"], which="both")
        ax.xaxis.label.set_color(t["fg"])
        ax.yaxis.label.set_color(t["fg"])
        ax.title.set_color(t["fg_strong"])

    def _impedance_summary_html(self, result) -> str:
        t = _T()
        parts: list[str] = []
        if not result.branches:
            parts.append(
                f"<span style='color:{t['warn_fg']};'>No capacitor on this "
                f"rail could be modelled.</span>")
        elif not math.isfinite(result.target.z_target_ohm):
            parts.append(
                f"<span style='color:{t['fg_muted']};'>Set a transient "
                f"current to get a target mask.</span>")
        elif result.meets_target():
            parts.append(
                f"<span style='color:{t['ok']};'><b>Meets target</b></span> — "
                f"|Z| stays below {result.target.z_target_ohm * 1e3:.3g} mΩ "
                f"to {result.target.f_max_hz / 1e6:.3g} MHz.")
        else:
            reached = result.reached_frequency_hz()
            parts.append(
                f"<span style='color:{t['err']};'><b>Misses target</b></span> "
                f"— |Z| first breaches "
                f"{result.target.z_target_ohm * 1e3:.3g} mΩ at "
                f"{reached / 1e6:.3g} MHz "
                f"(needed to {result.target.f_max_hz / 1e6:.3g} MHz).")

        peak = result.worst_peak
        if peak is not None:
            parts.append(
                f"Worst anti-resonance {peak.z_ohm * 1e3:.3g} mΩ at "
                f"{peak.freq_hz / 1e6:.3g} MHz.")
        parts.append(f"{len(result.branches)} capacitor(s) modelled"
                     + (f", plane-pair {result.c_plane_f * 1e9:.3g} nF"
                        if result.c_plane_f > 0 else ""))
        if result.skipped:
            # Collapse capacitors sharing a reason into one comma-delimited
            # group so a whole footprint family reads as a single clause.
            grouped: dict[str, list[str]] = {}
            for des, reason in result.skipped:
                grouped.setdefault(reason, []).append(des)
            groups = list(grouped.items())
            shown = "; ".join(f"{', '.join(d)} ({r})" for r, d in groups[:3])
            hidden = sum(len(d) for _, d in groups[3:])
            more = f" +{hidden} more" if hidden else ""
            parts.append(
                f"<span style='color:{t['warn_fg']};'>Excluded: "
                f"{_esc(shown)}{more}.</span>")
        return "<br>".join(parts)
