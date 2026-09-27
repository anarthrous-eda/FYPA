"""The Capacitors tab and its Tier-2 / Tier-3 solves."""
from __future__ import annotations

import logging
import re
import time
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.numeric import _parse_capacitance_f
from fypa.viewer.session import _run_background_load
from fypa.viewer.solve_worker import _CapLoopWorker
from fypa.viewer.tabs.messages import _MessagesSortItem
from fypa.viewer.theme import _T


# Item-data role carrying the Capacitors table's original row index. It can't
# live on Qt.UserRole because :class:`_MessagesSortItem` reads that role as
# the numeric sort key — the Go / Use cells need both a sort key and a row
# identity that survives the user re-sorting the table.
_CAPS_TABLE_ROW_ROLE = int(Qt.UserRole) + 2



# Canonical imperial package key on the Impedance-tab package table's name
# cell — the visible text may be a metric label when that convention is on.
_PKG_CANONICAL_ROLE = int(Qt.UserRole) + 3



# Columns of the Capacitors-tab table: (display label, numeric?). Defined at
# module scope so the label→index map below can be built from it (a class-body
# comprehension can't see other class-body names).
_CAPS_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = (
    ("",            False),
    ("Use",         False),
    ("Designator",  False),
    ("Rail",        False),
    ("C (µF)",      True),
    # Part parasitics for the impedance model: the package sets the defaults,
    # a per-part override wins. Both are editable in place.
    ("Pkg",         False),
    ("ESL (nH)",    True),
    ("ESR (mΩ)",    True),
    ("Part V",      True),
    ("Design V",    True),
    ("Target",      False),
    ("Pins",        True),
    ("Vias",        False),
    ("s (mm)",      True),
    ("Escape (mm)", True),
    ("Cavity",      False),
    ("L1 (nH)",     True),
    ("L2 (nH)",     True),
    ("L3 (nH)",     True),
    ("Flags",       False),
)


_CAPS_COL: dict[str, int] = {
    label: i for i, (label, _numeric) in enumerate(_CAPS_TABLE_COLUMNS)
}


class _CapacitorsTabMixin:
    """The Capacitors tab and its Tier-2 / Tier-3 solves."""

    # --- Capacitors tab ------------------------------------------------------

    # Columns of the Capacitors-tab table. (display label, numeric?)
    # Column 0 is the per-row "Go" jump cell, column 1 the include
    # checkbox; the rest are data. Same no-setCellWidget discipline as the
    # Vias tab — see _populate_vias_table for the perf rationale.
    _CAPS_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = _CAPS_TABLE_COLUMNS
    # Column indexes, derived from the labels above rather than hand-counted,
    # so inserting a column can't silently mis-address the click dispatcher.
    _CAPS_ACTION_COL = _CAPS_COL[""]
    _CAPS_USE_COL = _CAPS_COL["Use"]
    _CAPS_RAIL_COL = _CAPS_COL["Rail"]
    _CAPS_C_COL = _CAPS_COL["C (µF)"]
    _CAPS_PKG_COL = _CAPS_COL["Pkg"]
    _CAPS_ESL_COL = _CAPS_COL["ESL (nH)"]
    _CAPS_ESR_COL = _CAPS_COL["ESR (mΩ)"]
    _CAPS_TARGET_COL = _CAPS_COL["Target"]
    _CAPS_FLAGS_COL = _CAPS_COL["Flags"]

    def _caploop_settings(self):
        """The active :class:`~fypa.caploop.constants.CapLoopSettings` —
        project-file values when present, defaults otherwise. Cached;
        cleared by the Settings-tab apply (_on_rerun_cap_check)."""
        obj = getattr(self, "_caploop_settings_obj", None)
        if obj is None:
            from fypa.caploop.constants import CapLoopSettings
            stored = {}
            if getattr(self, "_project", None) is not None:
                stored = self._project.viewer_settings.get("caploop") or {}
            obj = CapLoopSettings.from_dict(stored)
            self._caploop_settings_obj = obj
        return obj

    def _footprint_convention(self) -> str:
        """Case-size naming convention for footprint parsing and display."""
        from fypa.caploop.packages import normalize_footprint_convention

        if getattr(self, "_project", None) is not None:
            stored = self._project.viewer_settings.get("footprint_convention")
            if stored is not None:
                return normalize_footprint_convention(str(stored))
        return "auto"

    def _set_footprint_convention(self, convention: str) -> None:
        from fypa.caploop.packages import normalize_footprint_convention

        conv = normalize_footprint_convention(convention)
        proj = self._ensure_project()
        proj.viewer_settings["footprint_convention"] = conv
        self._display_dirty = True

    def _build_footprint_convention_combo(self) -> QComboBox:
        """Case-size naming convention — shared by Capacitors and Impedance tabs."""
        combo = QComboBox()
        for value, label in (
            ("auto", "Auto"),
            ("metric", "Metric"),
            ("imperial", "Imperial"),
        ):
            combo.addItem(label, value)
        current = self._footprint_convention()
        idx = combo.findData(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.setToolTip(
            "How bare case-size codes in footprint names are read and "
            "shown. Auto recognises unambiguous metric codes (1005, 1608) "
            "and treats bare 0402 / 0603 as imperial.")
        combo.currentIndexChanged.connect(self._on_footprint_convention_changed)
        return combo

    def _sync_footprint_convention_ui(self) -> None:
        """Refresh convention combos and package labels from the active project."""
        current = self._footprint_convention()
        for attr in ("caps_footprint_conv_combo", "imp_footprint_conv_combo"):
            combo = getattr(self, attr, None)
            if combo is not None:
                combo.blockSignals(True)
                idx = combo.findData(current)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        if getattr(self, "imp_pkg_table", None) is not None:
            self._populate_package_table()

    def _build_capacitors_tab(self) -> QWidget:
        """Build the Capacitors tab — a sortable table of every decoupling
        capacitor with its Tier-1 mounted loop inductance, informational
        part values, and per-cap include / target overrides. Rows populate
        on first tab activation (identification walks the whole extracted
        design and builds per-net copper shapes — seconds on a big board)."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Rail:"))
        self.caps_rail_combo = QComboBox()
        self.caps_rail_combo.addItem("All rails")
        for r in self._rails:
            self.caps_rail_combo.addItem(r)
        self.caps_rail_combo.setToolTip(
            "Filter rows to capacitors whose power side is on the selected "
            "rail group."
        )
        self.caps_rail_combo.currentTextChanged.connect(self._apply_caps_filter)
        filter_row.addWidget(self.caps_rail_combo)

        filter_row.addSpacing(12)
        self.caps_flagged_only_box = QCheckBox("Show only flagged")
        self.caps_flagged_only_box.setToolTip(
            "Show only capacitors with a geometry flag (single via, long "
            "escape, far reference plane…) or a loop inductance at or above "
            "the warning threshold."
        )
        self.caps_flagged_only_box.toggled.connect(self._apply_caps_filter)
        filter_row.addWidget(self.caps_flagged_only_box)

        self.caps_included_only_box = QCheckBox("Included only")
        self.caps_included_only_box.setToolTip(
            "Hide capacitors excluded from the analysis via the Use column."
        )
        self.caps_included_only_box.toggled.connect(self._apply_caps_filter)
        filter_row.addWidget(self.caps_included_only_box)

        self.caps_overlay_box = QCheckBox("Show on heatmap")
        self.caps_overlay_box.setToolTip(
            "Colour each included capacitor's pads on the Heatmap tab by its "
            "loop inductance (best available tier), on a log scale from the "
            "lowest to the highest on the board."
        )
        self.caps_overlay_box.toggled.connect(self._on_caps_overlay_toggled)
        filter_row.addWidget(self.caps_overlay_box)

        filter_row.addSpacing(12)
        filter_row.addWidget(QLabel("Case size convention:"))
        self.caps_footprint_conv_combo = self._build_footprint_convention_combo()
        filter_row.addWidget(self.caps_footprint_conv_combo)

        filter_row.addSpacing(12)
        self.caps_tier23_btn = QPushButton("Compute Tier 2/3")
        self.caps_tier23_btn.setToolTip(
            "Solve the plane-pair spreading inductance for every included "
            "capacitor with the 2-D FEM (Tier 2), then assemble the full "
            "cap→plane→IC loop (Tier 3). Accurate on split and perforated "
            "planes, where the Tier-1 closed form is not."
        )
        self.caps_tier23_btn.clicked.connect(self._on_compute_cap_tier23)
        filter_row.addWidget(self.caps_tier23_btn)

        self.caps_progress_label = QLabel("")
        self.caps_progress_label.setStyleSheet(
            f"QLabel {{ color: {_T()['accent']}; }}"
        )
        filter_row.addWidget(self.caps_progress_label)

        filter_row.addStretch(1)
        self.caps_summary_label = QLabel("")
        self.caps_summary_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; }}"
        )
        filter_row.addWidget(self.caps_summary_label)
        outer.addLayout(filter_row)

        self.caps_table = QTableWidget()
        cols = self._CAPS_TABLE_COLUMNS
        self.caps_table.setColumnCount(len(cols))
        self.caps_table.setHorizontalHeaderLabels([c[0] for c in cols])
        self.caps_table.setSortingEnabled(True)
        self.caps_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.caps_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.caps_table.setAlternatingRowColors(True)
        self.caps_table.verticalHeader().setVisible(False)
        self.caps_table.horizontalHeader().setStretchLastSection(True)
        self.caps_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive)
        _t = _T()
        self.caps_table.setStyleSheet(
            f"QTableWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"               gridline-color: {_t['gridline']};"
            f"               alternate-background-color: {_t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
            f"                       padding: 4px; border: 1px solid {_t['border']}; }}"
            f"QTableWidget::item:selected {{ background-color: {_t['bg_selection']}; }}"
        )
        outer.addWidget(self.caps_table, 1)
        self._caps_stack = self._wrap_placeholder(widget)
        self._sync_placeholder_tabs()
        return self._caps_stack

    def _caploop_package_library(self):
        """The editable SMD package library (typical ESL / ESR per case size),
        seeded from the project file. Cached; rebuilt when the user edits it."""
        lib = getattr(self, "_caploop_package_lib", None)
        if lib is None:
            from fypa.caploop.packages import PackageLibrary
            stored = {}
            if getattr(self, "_project", None) is not None:
                stored = self._project.viewer_settings.get(
                    "caploop_packages") or {}
            lib = PackageLibrary.from_dict(stored)
            self._caploop_package_lib = lib
        return lib

    # Per-rail impedance-model inputs, defaulted so a rail the user has never
    # configured still plots something honest.
    _CAPLOOP_RAIL_DEFAULTS = {
        "ripple_pct": 5.0,
        "transient_current_a": 1.0,
        "f_max_hz": 40e6,
        "vrm_r_ohm": 0.002,
        "vrm_l_h": 5e-9,
    }

    def _caploop_rail_config(self, rail: str) -> dict:
        """Target-mask and VRM parameters for one rail, from the project file
        with the defaults filled in."""
        stored = {}
        if getattr(self, "_project", None) is not None:
            stored = (self._project.viewer_settings.get("caploop_rails")
                      or {}).get(rail) or {}
        cfg = dict(self._CAPLOOP_RAIL_DEFAULTS)
        for key in cfg:
            if key in stored:
                try:
                    cfg[key] = float(stored[key])
                except (TypeError, ValueError):
                    pass
        return cfg

    def _set_caploop_rail_config(self, rail: str, cfg: dict) -> None:
        proj = self._ensure_project()
        rails = proj.viewer_settings.setdefault("caploop_rails", {})
        rails[rail] = {k: float(v) for k, v in cfg.items()}
        self._display_dirty = True

    def _cap_net_layer_shapes(self, extracted):
        """Per-(layer, net) copper shapes for the capacitor analysis, cached
        for the lifetime of one extracted design.

        This union is the expensive part of the whole feature — 5–11 s on a
        real board — and both the Capacitors table and the Tier-2/3 solve need
        exactly the same shapes. Keying the cache on the extracted project
        object (held by reference, so its identity can't be recycled by the
        GC) means a Reload Design Info / re-solve naturally misses, while a
        settings or override change — which invalidates the *rows*, not the
        geometry — reuses it. Qt-free, so it is safe to call from a worker
        thread.
        """
        from fypa.altium_geometry import build_net_layer_shapes

        cached = getattr(self, "_caps_shapes_cache", None)
        if cached is not None and cached[0] is extracted:
            return cached[1]
        _t0 = time.monotonic()
        shapes = build_net_layer_shapes(
            extracted, extracted.enabled_copper_layer_ids())
        logging.getLogger(__name__).info(
            "Caps: net-layer shapes %.2fs (%d buckets)",
            time.monotonic() - _t0, len(shapes))
        self._caps_shapes_cache = (extracted, shapes)
        return shapes

    def _compute_cap_report(self) -> list[dict]:
        """One row dict per detected decoupling capacitor: identification
        bundle + Tier-1 mounted inductance. Pure geometry over the loaded
        design — no solve needed, so this works right after Reload Design
        Info. Returns [] (with an explanatory summary message stashed on
        ``self._caps_empty_reason``) when the design data isn't available.

        Touches no Qt widgets: :meth:`_ensure_cap_rows_async` runs it on a
        worker thread so the several seconds it takes don't freeze the GUI.
        """
        log = logging.getLogger(__name__)
        self._caps_empty_reason = ""
        loaded = getattr(self, "_loaded_project", None)
        extracted = getattr(loaded, "extracted", None)
        if extracted is None:
            self._caps_empty_reason = (
                "Capacitor analysis needs the design info — use "
                "Reload Design Info, then reopen this tab.")
            return []
        if not extracted.pcb_components:
            self._caps_empty_reason = (
                "Capacitor analysis needs component data, which Gerber "
                "imports don't carry — import the Altium design instead.")
            return []

        from fypa.caploop.identify import (
            apply_cap_overrides,
            identify_capacitors,
        )
        from fypa.caploop.tier1 import mounted_inductance

        settings = self._caploop_settings()
        includes, targets = ({}, {})
        esl_over, esr_over = ({}, {})
        cap_over, pkg_over = ({}, {})
        if getattr(self, "_project", None) is not None:
            includes, targets = self._project.cap_override_maps()
            esl_over, esr_over = self._project.cap_parasitic_overrides()
            cap_over, pkg_over = self._project.cap_value_overrides()
        library = self._caploop_package_library()
        directives = (self.metadata or {}).get("directives") or []

        shapes = self._cap_net_layer_shapes(extracted)

        # Identification is the slow half (copper-coverage tests per cap), and
        # it depends only on the board, the analysis settings, and which caps
        # are force-included — never on an exclude or a retargeting. Cache it
        # so toggling a checkbox is a millisecond, not a five-second freeze.
        _t1 = time.monotonic()
        forced = frozenset(d for d, v in includes.items() if v)
        convention = self._footprint_convention()
        cached = getattr(self, "_caps_identity_cache", None)
        if (cached is not None and cached[0] is extracted
                and cached[1] == settings and cached[2] == forced
                and cached[3] == convention):
            base = cached[4]
        else:
            base = identify_capacitors(
                extracted, self._rail_to_members,
                metadata_directives=directives,
                settings=settings,
                net_layer_shapes=shapes,
                include_overrides=dict.fromkeys(forced, True),
                footprint_convention=convention,
            )
            self._caps_identity_cache = (
                extracted, settings, forced, convention, base)
            log.info("Caps report: identify %.2fs (%d caps)",
                     time.monotonic() - _t1, len(base))
        caps = apply_cap_overrides(
            base, self._rail_to_members, directives, includes, targets)
        rows: list[dict] = []
        for cap in caps:
            t1 = mounted_inductance(cap, settings)
            # Case size: detected from the footprint name unless the user has
            # pinned one. The override is applied *here* rather than inside
            # identification because it only selects a library entry and a
            # label — it changes no geometry, so the identity cache (which a
            # package override does not key) stays valid.
            package = pkg_over.get(cap.designator, cap.package)
            # Part parasitics: the package library supplies the default, an
            # explicit per-part override wins. A part the library can't
            # classify has neither until the user supplies one.
            model = library.get(package)
            esl_h = esl_over.get(cap.designator,
                                 model.esl_h if model else None)
            esr_ohm = esr_over.get(cap.designator,
                                   model.esr_ohm if model else None)
            rows.append({
                "cap": cap,
                "designator": cap.designator,
                "rail": cap.rail_group,
                "rail_net": cap.rail_net,
                "return_net": cap.return_net,
                "capacitance_f": cap_over.get(cap.designator,
                                              cap.capacitance_f),
                "package": package,
                # What extraction found, kept beside the effective value so a
                # tooltip can say what the override is overriding — and so
                # picking the detected case size can be stored as "no
                # override" rather than as a redundant one.
                "capacitance_parsed_f": cap.capacitance_f,
                "package_detected": cap.package,
                "capacitance_is_override": cap.designator in cap_over,
                "package_is_override": cap.designator in pkg_over,
                "esl_h": esl_h,
                "esr_ohm": esr_ohm,
                "esl_is_override": cap.designator in esl_over,
                "esr_is_override": cap.designator in esr_over,
                "voltage_rating_v": cap.voltage_rating_v,
                "design_voltage_v": cap.design_voltage_v,
                "target_label": cap.target_label,
                "target_is_override": cap.target_is_override,
                "target_pin_count": len(cap.target_pins),
                "vias_str": f"{len(cap.vias_rail)}+{len(cap.vias_return)}",
                "s_mm": t1.s_mm if not t1.is_fallback else None,
                # The worse of the two sides' shortest pad-edge→via runs —
                # the same measure the long-escape flag uses, so the column
                # and the flag can never disagree.
                "escape_mm": max(
                    (min(e.escape_mm for e in side)
                     for side in (cap.vias_rail, cap.vias_return) if side),
                    default=None),
                "cavity_str": (
                    f"{cap.cavity.name_rail} ↔ {cap.cavity.name_return}"
                    if cap.cavity is not None else None),
                "tier1": t1,
                "l1_nh": t1.total_h * 1e9,
                "l1_is_fallback": t1.is_fallback,
                "l2_nh": None,   # filled by the Tier-2/3 worker (phase 4/5)
                "l3_nh": None,
                "flags": cap.flags,
                "included": cap.included,
                "auto_detected": cap.auto_detected,
                "x_mm": cap.center_xy[0],
                "y_mm": cap.center_xy[1],
                "layer_id": cap.mount_layer_id,
            })
        log.info("Caps report: rows built %.2fs (%d caps)",
                 time.monotonic() - _t1, len(rows))
        if not rows:
            self._caps_empty_reason = (
                "No decoupling capacitors found — detection needs "
                "C-designator two-pin parts across annotated power rails "
                "(GND counts as a rail).")
        return rows

    def _get_or_compute_cap_rows(self) -> list[dict]:
        """Run :meth:`_compute_cap_report` at most once per viewer and cache
        the result. Mirrors :meth:`_get_or_compute_via_rows`; invalidated by
        override edits and Settings-tab apply via
        :meth:`_invalidate_caps_cache`.

        Synchronous. Every *first* build goes through
        :meth:`_ensure_cap_rows_async` instead, so by the time this is called
        the cache is warm and it returns immediately. It stays synchronous for
        the recompute-after-an-override path, which reuses the cached copper
        shapes and takes milliseconds.
        """
        cached = getattr(self, "_caps_rows_cache", None)
        if cached is None:
            cached = self._compute_cap_report()
            self._caps_rows_cache = cached
        return cached

    def _ensure_cap_rows_async(self, then) -> None:
        """Make sure the capacitor rows exist, then call ``then()``.

        The first build unions every (layer, net) copper shape and walks every
        component — seconds on a real board, and previously all of it on the
        GUI thread, which is what made the tab look hung. Run it on a worker
        behind a modal busy dialog instead; the event loop keeps turning so
        the window repaints. Already-cached rows skip the thread entirely and
        call ``then()`` inline, so a re-entered tab stays instant.

        Callers that arrive while a build is in flight are *queued*, not
        dropped: the Capacitors tab, the heatmap overlay and the Impedance tab
        all want the same rows, and whichever asks second must still get its
        continuation run, or its view stays permanently blank.
        """
        if getattr(self, "_caps_rows_cache", None) is not None:
            then()
            return

        waiters = getattr(self, "_caps_rows_waiters", None)
        if waiters is None:
            waiters = self._caps_rows_waiters = []
        waiters.append(then)
        if getattr(self, "_caps_rows_pending", False):
            return
        self._caps_rows_pending = True

        def _work():
            # Worker thread: _compute_cap_report touches no Qt.
            return self._compute_cap_report()

        def _flush() -> None:
            self._caps_rows_pending = False
            pending, self._caps_rows_waiters = self._caps_rows_waiters, []
            for waiter in pending:
                waiter()

        def _ok(rows) -> None:
            self._caps_rows_cache = rows
            _flush()

        def _fail(exc_type: str, message: str) -> None:
            self._caps_rows_cache = []
            self._caps_empty_reason = (
                f"Capacitor analysis failed: {exc_type}: {message}")
            logging.getLogger(__name__).error(
                "Capacitor report failed: %s: %s", exc_type, message)
            _flush()

        _run_background_load(
            self, _work, _ok, _fail,
            title="Capacitors",
            label="Analysing decoupling capacitors…\n"
                  "Building copper geometry and finding escape vias.",
        )

    def _invalidate_caps_cache(self, *, repopulate: bool = True,
                              heavy: bool = False) -> None:
        """Drop the cached cap rows and re-populate the table if it was
        already built. Tier-2/3 results living in the rows are dropped too —
        they were computed against the old geometry/overrides and would be
        stale.

        ``heavy`` also drops the cached *identification* (and with it the
        copper shapes), which only a design reload or a settings change can
        invalidate. A plain override edit leaves both caches warm, so the
        rebuild is a millisecond and can run inline; a heavy rebuild is
        seconds and goes through the busy dialog.
        """
        self._caps_rows_cache = None
        self._caploop_matrices = []
        self._caploop_rollup = {}
        # The rebuilt rows carry no Tier-2/3 values, so the set of designators
        # the last solve was given no longer describes them.
        self._caploop_requested = None
        if heavy:
            self._caps_identity_cache = None
            # The plane-pair capacitance depends on the cavity geometry.
            self._imp_plane_cache = {}
        if hasattr(self, "caps_progress_label"):
            self.caps_progress_label.setText("")
        if not (repopulate and getattr(self, "_caps_table_populated", False)):
            return
        if heavy:
            self._ensure_cap_rows_async(self._populate_caps_table)
        else:
            self._populate_caps_table()

    def _on_rerun_cap_check(self) -> None:
        """Settings-tab apply for the capacitor loop-inductance knobs:
        gather the ``settings_edit_cl_*`` fields into a fresh
        CapLoopSettings, persist it with the viewer state, and recompute
        the Capacitors tab in place. No FEM re-solve."""
        import dataclasses as _dc
        from fypa.caploop.constants import CapLoopSettings
        values = {}
        for f in _dc.fields(CapLoopSettings):
            edit = getattr(self, f"settings_edit_cl_{f.name}", None)
            if edit is None:
                continue
            try:
                values[f.name] = self._parse_settings_value(edit.text())
            except ValueError:
                self._settings_status_label.setText(
                    f"<span style='color:{_T()['warn_fg']};'>"
                    f"Capacitor check: {f.name} is not a number "
                    f"({edit.text().strip()!r})</span>")
                return
        # Replace onto the *current* settings, not onto fresh defaults: the
        # fields with no Settings-tab editor (the plane-detection knobs) would
        # otherwise be silently reset to their defaults on every apply.
        self._caploop_settings_obj = _dc.replace(
            self._caploop_settings(), **values)
        # Persist alongside the other viewer state on next project save.
        if getattr(self, "_project", None) is not None:
            self._project.viewer_settings["caploop"] = \
                self._caploop_settings_obj.to_dict()
            self._display_dirty = True
        # Settings feed escape-via clustering and cavity selection, so the
        # identification itself has to be redone — that's the slow path.
        self._invalidate_caps_cache(heavy=True)
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Capacitor check "
            f"re-run with the new settings.</span>")

    # What each capacitor flag means, keyed by its base token. The
    # side-specific ones carry the offending pad's net in parentheses.
    _CAP_FLAG_HELP: dict[str, str] = {
        "no-escape-via":
            "No via within the search radius reaches this pad's own layer, "
            "so its current leaves the pad over surface copper. The named "
            "net is the pad with no via.",
        "single-via":
            "Only one escape via on the named pad. Halving the via count "
            "roughly doubles that side's loop inductance — usually the "
            "cheapest thing to fix.",
        "long-escape":
            "The run from the named pad's edge to its nearest via exceeds "
            "the warning distance. Via-in-pad escapes in 0 mm.",
        "far-plane":
            "The reference plane pair sits deeper below the mounting "
            "surface than the warning depth, so the loop reaches further "
            "into the stack than it needs to.",
        "no-cavity":
            "No reachable plane pair, so the closed forms can't model this "
            "capacitor. Its L1 is a fallback estimate (shown with a ~) and "
            "Tier 2 will skip it.",
        "no-target":
            "No SINK directive on this rail, so the loop has no far end to "
            "be measured to. Pick a device in the Target column.",
    }

    def _cap_flags_tooltip(self, row: dict) -> str:
        """Spell out this row's flags. Thresholds live in the Settings tab, so
        quote the ones that were actually applied rather than the defaults."""
        flags = row.get("flags", ())
        if not flags:
            return "No geometry concerns for this capacitor."
        settings = self._caploop_settings()
        limits = {
            "long-escape": f" (over {settings.long_escape_warn_mm:g} mm)",
            "far-plane": f" (deeper than {settings.far_plane_warn_mm:g} mm)",
            "no-escape-via": f" (within {settings.escape_via_search_mm:g} mm)",
        }
        lines = []
        for flag in flags:
            token = flag.split(" (", 1)[0]
            help_text = self._CAP_FLAG_HELP.get(token, "")
            lines.append(f"• {flag}{limits.get(token, '')}\n    {help_text}")
        return "\n".join(lines)

    def _cap_l_best_nh(self, row: dict) -> float | None:
        """Best-available loop inductance: Tier 3 > Tier 2 > Tier 1."""
        for key in ("l3_nh", "l2_nh", "l1_nh"):
            if row.get(key) is not None:
                return row[key]
        return None

    def _cap_row_is_warn(self, row: dict) -> bool:
        if not row.get("included", True):
            return False
        if row.get("flags"):
            return True
        l_best = self._cap_l_best_nh(row)
        return (l_best is not None
                and l_best >= self._caploop_settings().cap_l_warn_nh)

    def _populate_caps_table(self) -> None:
        """Fill the Capacitors table from the cached cap report. Same
        plain-cell + single click-dispatcher pattern as the Vias table."""
        from fypa.caploop.packages import format_package_label

        log = logging.getLogger(__name__)
        _t0 = time.monotonic()
        rows = self._get_or_compute_cap_rows()
        self._caps_rows = rows
        cols = self._CAPS_TABLE_COLUMNS
        _t = _T()
        warn_bg = QBrush(QColor(_t["warn_bg"]))
        warn_fg = QBrush(QColor(_t["warn_fg"]))
        action_fg = QBrush(QColor(_t["accent"]))
        muted_fg = QBrush(QColor(_t["fg_muted"]))
        footprint_convention = self._footprint_convention()

        # itemChanged fires for every setItem during populate — guard the
        # include-toggle handler with a populating flag.
        self._caps_populating = True
        self.caps_table.setSortingEnabled(False)
        self.caps_table.setRowCount(len(rows))
        warn_count = 0
        for r, row in enumerate(rows):
            is_warn = self._cap_row_is_warn(row)
            if is_warn:
                warn_count += 1
            included = row.get("included", True)

            # Sort keys go on Qt.UserRole (read by _MessagesSortItem.__lt__);
            # the display text and the row identity would both be destroyed by
            # the usual setData(Qt.EditRole, ...) pattern, because
            # QTableWidgetItem aliases EditRole to DisplayRole.
            action_item = _MessagesSortItem("Go ▶")
            action_item.setData(Qt.UserRole, float(r))
            action_item.setData(_CAPS_TABLE_ROW_ROLE, r)
            action_item.setForeground(action_fg)
            action_item.setTextAlignment(Qt.AlignCenter)
            action_item.setToolTip(
                "Click to jump to this capacitor in the Heatmap tab.")
            self.caps_table.setItem(r, self._CAPS_ACTION_COL, action_item)

            use_item = _MessagesSortItem("")
            use_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                              | Qt.ItemIsUserCheckable)
            use_item.setCheckState(Qt.Checked if included else Qt.Unchecked)
            use_item.setData(_CAPS_TABLE_ROW_ROLE, r)
            # Sort key: included rows first when sorting this column.
            use_item.setData(Qt.UserRole, 0.0 if included else 1.0)
            use_item.setToolTip(
                "Include this capacitor in the loop-inductance analysis. "
                "The choice persists in the .fypa project file.")
            self.caps_table.setItem(r, self._CAPS_USE_COL, use_item)

            t1 = row["tier1"]
            t1_tip = (
                "Tier-1 breakdown:\n"
                f"  escape (rail): {t1.escape_rail_h * 1e9:.3g} nH\n"
                f"  escape (return): {t1.escape_return_h * 1e9:.3g} nH\n"
                f"  via pair ×{t1.n_pairs}: {t1.via_loop_h * 1e9:.3g} nH\n"
                f"  spreading (closed form): {t1.spread_cf_h * 1e9:.3g} nH"
                if not t1.is_fallback else
                "Geometry too degenerate for the closed forms (no escape "
                "via or no reference cavity) — settings fallback value.")

            target_text = row.get("target_label") or "—"
            if row.get("target_is_override"):
                target_text += " ✎"

            package_label = format_package_label(
                row.get("package"), footprint_convention)

            cells = (
                None,  # action
                None,  # use checkbox — set above
                row["designator"],
                row["rail"],
                None if row.get("capacitance_f") is None
                else row["capacitance_f"] * 1e6,
                package_label,   # already "—" when the package is unknown
                None if row.get("esl_h") is None else row["esl_h"] * 1e9,
                None if row.get("esr_ohm") is None else row["esr_ohm"] * 1e3,
                row.get("voltage_rating_v"),
                row.get("design_voltage_v"),
                target_text,
                row.get("target_pin_count") or None,
                row.get("vias_str", ""),
                row.get("s_mm"),
                row.get("escape_mm"),
                row.get("cavity_str") or "—",
                row.get("l1_nh"),
                row.get("l2_nh"),
                row.get("l3_nh"),
                ", ".join(row.get("flags", ())) or "—",
            )
            for c, (col_label, is_numeric) in enumerate(cols):
                if c in (self._CAPS_ACTION_COL, self._CAPS_USE_COL):
                    continue
                value = cells[c]
                if value is None:
                    item = QTableWidgetItem("—")
                elif is_numeric and isinstance(value, (int, float)):
                    item = _MessagesSortItem(
                        f"{value:d}" if isinstance(value, int)
                        else f"{value:.4g}")
                    item.setData(Qt.UserRole, float(value))
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    item = QTableWidgetItem(str(value))
                if col_label == "L1 (nH)":
                    item.setToolTip(t1_tip)
                    if row.get("l1_is_fallback"):
                        item.setText(f"~{item.text()}")
                if col_label == "L2 (nH)":
                    if row.get("l2_nh") is None:
                        tip = (row.get("l2_reason")
                               or "Press Compute Tier 2/3 to solve.")
                    else:
                        tip = ("FEM plane-pair spreading inductance between "
                               "this capacitor's vias and its target's vias.")
                        # A solved value can still come with a caveat — e.g. a
                        # zero because the cap sits on the target's own via.
                        if row.get("l2_reason"):
                            tip += f"\n\n{row['l2_reason']}"
                    item.setToolTip(tip)
                if col_label == "L3 (nH)":
                    t3 = row.get("tier3")
                    if t3 is not None:
                        item.setToolTip(
                            "Full cap→plane→IC loop:\n"
                            f"  escape (both pads): {t3.escape_h * 1e9:.3g} nH\n"
                            f"  cap via pair: {t3.via_loop_cap_h * 1e9:.3g} nH\n"
                            f"  cavity spreading (FEM): {t3.spread_h * 1e9:.3g} nH\n"
                            f"  IC via pair ×{t3.ic_pairs}: "
                            f"{t3.via_loop_ic_h * 1e9:.3g} nH"
                            + (f"\n\nPartial: {t3.reason} — the total is a "
                               "lower bound." if t3.is_partial else ""))
                        if t3.is_partial:
                            item.setText(f"≥{item.text()}")
                    else:
                        # No total without a spreading term. Once a solve has
                        # run, the L2 reason *is* the reason there is no L3.
                        item.setToolTip(
                            f"No cap→plane→IC total: {row['l2_reason']}"
                            if row.get("l2_reason") else
                            "Needs a Tier-2 spreading term — "
                            "press Compute Tier 2/3.")
                if col_label == "Flags":
                    item.setToolTip(self._cap_flags_tooltip(row))
                if col_label == "C (µF)":
                    item.setToolTip(self._cap_value_tooltip(row))
                    if row.get("capacitance_is_override"):
                        item.setText(f"{item.text()} ✎")
                        item.setForeground(action_fg)
                    elif row.get("capacitance_f") is None:
                        item.setForeground(action_fg)
                if col_label == "Pkg":
                    item.setToolTip(self._cap_package_tooltip(
                        row, package_label, footprint_convention))
                    if row.get("package_is_override"):
                        item.setText(f"{package_label} ✎")
                        item.setForeground(action_fg)
                if col_label in ("ESL (nH)", "ESR (mΩ)"):
                    is_esl = col_label.startswith("ESL")
                    overridden = row.get(
                        "esl_is_override" if is_esl else "esr_is_override")
                    what = "inductance" if is_esl else "resistance"
                    if row.get("esl_h" if is_esl else "esr_ohm") is None:
                        item.setToolTip(
                            f"No equivalent series {what} — the package is "
                            "unrecognised. Double-click to set it, or "
                            "double-click the Pkg cell to name the case "
                            "size and take the library default.")
                    elif overridden and row.get("package"):
                        item.setToolTip(
                            f"Per-part override. Double-click to change, or "
                            f"clear the field to fall back to the "
                            f"{package_label} package default.")
                    elif overridden:
                        # No package, so there is nothing to fall back TO --
                        # clearing the field drops the part from the model.
                        item.setToolTip(
                            "Per-part override. Double-click to change. The "
                            "package is unrecognised, so clearing this field "
                            "removes the part from the impedance model.")
                        item.setForeground(action_fg)
                    else:
                        item.setToolTip(
                            f"Typical equivalent series {what} for a "
                            f"{package_label} package. Double-click to "
                            "override this part.")
                        item.setForeground(muted_fg)
                if col_label == "Target":
                    item.setToolTip(
                        "The loop-measurement endpoint (defaults to the "
                        "largest-current SINK on the rail). Click to pick a "
                        "different directive; persists in the .fypa.")
                    item.setForeground(action_fg)
                if is_warn and col_label in ("L1 (nH)", "L2 (nH)",
                                             "L3 (nH)", "Flags"):
                    if col_label == "Flags" and not row.get("flags"):
                        pass
                    else:
                        item.setBackground(warn_bg)
                        item.setForeground(warn_fg)
                if not included:
                    item.setForeground(muted_fg)
                self.caps_table.setItem(r, c, item)
        self.caps_table.setSortingEnabled(True)
        # Default sort: L1 descending — worst mounts on top.
        l1_col = next(i for i, (n, _) in enumerate(cols) if n == "L1 (nH)")
        self.caps_table.sortByColumn(l1_col, Qt.DescendingOrder)
        self.caps_table.resizeColumnsToContents()
        self._caps_populating = False
        if not getattr(self, "_caps_click_handler_wired", False):
            self.caps_table.cellClicked.connect(self._on_caps_cell_clicked)
            self.caps_table.cellDoubleClicked.connect(
                self._on_caps_cell_double_clicked)
            self.caps_table.itemChanged.connect(self._on_caps_item_changed)
            self._caps_click_handler_wired = True
        self._caps_warn_count = warn_count
        self._update_caps_tab_title(warn_count)
        self._apply_caps_filter()
        log.info("Caps populate: TOTAL %.2fs (%d rows)",
                 time.monotonic() - _t0, len(rows))

    def _cap_value_tooltip(self, row: dict) -> str:
        """Tooltip for the Capacitors-tab C column.

        The value is not decoration: :func:`fypa.caploop.impedance.cap_branch`
        feeds it straight into the branch impedance, and a capacitor without
        one is dropped from the model — so the cell has to say where the
        number came from and how to correct it.
        """
        parsed = row.get("capacitance_parsed_f")
        parsed_txt = ("—" if parsed is None
                      else f"{parsed * 1e6:.4g} µF")
        if row.get("capacitance_is_override"):
            return (
                "Per-part override. Double-click to change, or clear the "
                "field to fall back to the value read from the part "
                f"({parsed_txt}).")
        if parsed is None:
            return (
                "No capacitance could be read from this part's Altium "
                "parameters (Capacitance / Value / Comment), so it is "
                "excluded from the impedance model. Double-click to set it.")
        return (
            "Read from the part's Altium parameters. Double-click to "
            "override — for a value no heuristic can parse, or for the "
            "effective capacitance after DC-bias and temperature derating.")

    def _cap_package_tooltip(self, row: dict, package_label: str,
                             convention: str) -> str:
        """Tooltip for the Capacitors-tab Pkg column."""
        from fypa.caploop.packages import (
            format_package_label,
            package_detection_tooltip,
        )

        footprint = row["cap"].footprint
        if row.get("package_is_override"):
            detected = row.get("package_detected")
            was = (f"The footprint {footprint!r} reads as "
                   f"{format_package_label(detected, convention)}."
                   if detected else
                   f"The footprint {footprint!r} names no case size.")
            return (
                f"Per-part override: {package_label}. {was} It selects the "
                "default ESL / ESR from the package library. Double-click "
                "to change it, or choose (automatic) to clear it.")
        return (package_detection_tooltip(footprint, row.get("package"))
                + "\n\nDouble-click to pin a different case size.")

    def _on_caps_item_changed(self, item) -> None:
        """Include-checkbox edits → persist as a CapOverride and recompute.
        Guarded against populate-time setItem noise."""
        if getattr(self, "_caps_populating", False):
            return
        if item.column() != self._CAPS_USE_COL:
            return
        orig_idx = item.data(_CAPS_TABLE_ROW_ROLE)
        if not (isinstance(orig_idx, int)
                and 0 <= orig_idx < len(getattr(self, "_caps_rows", []))):
            return
        row = self._caps_rows[orig_idx]
        include = item.checkState() == Qt.Checked
        # Persist only a *deviation* from auto-detection: re-checking a cap
        # that detection found on its own clears the override. A cap that
        # only a force-include admitted must keep ``include=True`` — clearing
        # it there would drop the cap from the list entirely.
        if include:
            new_include = None if row.get("auto_detected", True) else True
        else:
            new_include = False
        self._set_cap_override(row["designator"], include=new_include)

    def _on_caps_cell_clicked(self, row: int, col: int) -> None:
        """Single dispatcher for the Go ▶ (jump) and Target (picker) cells."""
        item = self.caps_table.item(row, col)
        if item is None:
            return
        if col == self._CAPS_ACTION_COL:
            orig_idx = item.data(_CAPS_TABLE_ROW_ROLE)
            if (isinstance(orig_idx, int)
                    and 0 <= orig_idx < len(getattr(self, "_caps_rows", []))):
                r = self._caps_rows[orig_idx]
                self._jump_to_xy(r.get("x_mm"), r.get("y_mm"),
                                 [r.get("layer_id")])
        elif col == self._CAPS_TARGET_COL:
            # Row identity comes from the action cell (this cell carries text
            # only), and survives the user re-sorting the table.
            action_item = self.caps_table.item(row, self._CAPS_ACTION_COL)
            orig_idx = (action_item.data(_CAPS_TABLE_ROW_ROLE)
                        if action_item is not None else None)
            if (isinstance(orig_idx, int)
                    and 0 <= orig_idx < len(getattr(self, "_caps_rows", []))):
                self._show_cap_target_menu(self._caps_rows[orig_idx])

    def _caps_row_at(self, table_row: int) -> dict | None:
        """The row dict behind a visual table row — recovered through the
        action cell's stashed index, so it survives re-sorting."""
        action_item = self.caps_table.item(table_row, self._CAPS_ACTION_COL)
        orig_idx = (action_item.data(_CAPS_TABLE_ROW_ROLE)
                    if action_item is not None else None)
        rows = getattr(self, "_caps_rows", [])
        if isinstance(orig_idx, int) and 0 <= orig_idx < len(rows):
            return rows[orig_idx]
        return None

    def _on_caps_cell_double_clicked(self, row: int, col: int) -> None:
        """Edit the four per-part values the board geometry can't supply:
        the capacitance, the case size, and the part's own ESL / ESR.

        An empty input clears the override, falling back to the parsed part
        value or the package library — the only way back for a part the user
        has pinned.
        """
        if col not in (self._CAPS_C_COL, self._CAPS_PKG_COL,
                       self._CAPS_ESL_COL, self._CAPS_ESR_COL):
            return
        data = self._caps_row_at(row)
        if data is None:
            return
        if col == self._CAPS_C_COL:
            self._edit_cap_capacitance(data)
            return
        if col == self._CAPS_PKG_COL:
            self._show_cap_package_menu(data)
            return

        is_esl = col == self._CAPS_ESL_COL
        if is_esl:
            title, unit, scale = "Equivalent series inductance", "nH", 1e-9
            current, key = data.get("esl_h"), "esl_h"
        else:
            title, unit, scale = "Equivalent series resistance", "mΩ", 1e-3
            current, key = data.get("esr_ohm"), "esr_ohm"

        from fypa.caploop.packages import format_package_label
        package = data.get("package")
        # Same label the Pkg cell shows: naming the canonical "0402" while the
        # cell the user just double-clicked reads "1005" looks like two
        # different parts.
        package_label = format_package_label(
            package, self._footprint_convention())
        default_note = (
            f"Leave empty to use the {package_label} package default."
            if package else
            "This part's package is unrecognised, so there is no default to "
            "fall back on.")
        text, ok = QInputDialog.getText(
            self, f"{title} — {data['designator']}",
            f"{title} ({unit}):\n{default_note}",
            text="" if current is None else f"{current / scale:.4g}")
        if not ok:
            return

        text = text.strip()
        if not text:
            self._set_cap_override(data["designator"], **{key: None})
            return
        try:
            value = float(text)
        except ValueError:
            QMessageBox.warning(self, "Not a number",
                                f"{text!r} is not a number.")
            return
        if value < 0.0:
            QMessageBox.warning(self, "Out of range",
                                f"{title} must be zero or positive.")
            return
        self._set_cap_override(data["designator"], **{key: value * scale})

    def _edit_cap_capacitance(self, data: dict) -> None:
        """Override one capacitor's value. Empty input restores the value
        parsed from the part's Altium parameters."""
        parsed = data.get("capacitance_parsed_f")
        current = data.get("capacitance_f")
        note = (
            f"Leave empty to use the value read from the part "
            f"({parsed * 1e6:.4g} µF)." if parsed is not None else
            "Nothing could be read from this part's parameters, so leaving "
            "it empty keeps the capacitor out of the impedance model.")
        text, ok = QInputDialog.getText(
            self, f"Capacitance — {data['designator']}",
            f"Capacitance (µF, or a unit suffix such as 100n):\n{note}",
            text="" if current is None else f"{current * 1e6:.4g}")
        if not ok:
            return

        text = text.strip()
        if not text:
            self._set_cap_override(data["designator"], capacitance_f=None)
            return
        try:
            value = _parse_capacitance_f(text)
        except ValueError:
            QMessageBox.warning(
                self, "Not a capacitance",
                f"{text!r} is not a capacitance. Enter a number in µF "
                f"(0.1), or a number with a unit (100n, 4.7uF, 220pF). "
                f"Use a dot decimal separator.")
            return
        if value <= 0.0:
            QMessageBox.warning(self, "Out of range",
                                "The capacitance must be greater than zero.")
            return
        self._set_cap_override(data["designator"], capacitance_f=value)

    def _show_cap_package_menu(self, row: dict) -> None:
        """Popup listing every case size in the SMD package library plus an
        "(automatic)" reset. The chosen value persists as a CapOverride.

        Only library case sizes are offered: the package exists to select an
        ESL / ESR pair, so a name the library doesn't hold would select
        nothing. A part with no case size at all (a tantalum brick) still
        needs the ESL / ESR editors.
        """
        from fypa.caploop.packages import format_package_label

        convention = self._footprint_convention()
        detected = row.get("package_detected")
        menu = QMenu(self)
        auto = menu.addAction(
            "(automatic — from the footprint: "
            f"{format_package_label(detected, convention)})" if detected else
            "(automatic — the footprint names no case size)")
        auto.setCheckable(True)
        auto.setChecked(not row.get("package_is_override"))
        menu.addSeparator()
        by_action = {}
        for model in self._caploop_package_library():
            act = menu.addAction(format_package_label(model.name, convention))
            act.setCheckable(True)
            act.setChecked(bool(row.get("package_is_override"))
                           and row.get("package") == model.name)
            by_action[act] = model.name
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        package = None if chosen is auto else by_action[chosen]
        # Persist only a deviation from detection, exactly as the include
        # checkbox does: pinning the case size the footprint already names
        # would leave a no-op record in the .fypa that survives a footprint
        # rename and then silently contradicts it.
        self._set_cap_override(
            row["designator"],
            package=None if package == detected else package)

    def _show_cap_target_menu(self, row: dict) -> None:
        """Popup listing every eligible target directive for the cap's rail
        plus an "(automatic)" reset. Chosen value persists as a CapOverride."""
        from fypa.caploop.identify import eligible_target_labels
        directives = (self.metadata or {}).get("directives") or []
        members = set(self._rail_to_members.get(row["rail"], [row["rail"]]))
        labels = eligible_target_labels(members, directives)
        menu = QMenu(self)
        auto = menu.addAction("(automatic — largest-current sink)")
        auto.setCheckable(True)
        auto.setChecked(not row.get("target_is_override"))
        menu.addSeparator()
        for label in labels:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(bool(row.get("target_is_override"))
                           and row.get("target_label") == label)
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if chosen is auto:
            self._set_cap_override(row["designator"], target_label=None)
        else:
            self._set_cap_override(row["designator"],
                                   target_label=chosen.text())

    def _set_cap_override(self, designator: str, *, include=...,
                          target_label=..., esl_h=..., esr_ohm=...,
                          capacitance_f=..., package=...) -> None:
        """Write one override into the project file (created on first edit),
        mark the display dirty (no re-solve — analysis state only), and
        rebuild the cap rows so every derived value reflects the change."""
        proj = self._ensure_project()
        proj.upsert_cap_override(designator, include=include,
                                 target_label=target_label,
                                 esl_h=esl_h, esr_ohm=esr_ohm,
                                 capacitance_f=capacitance_f,
                                 package=package)
        # Display-dirty, not project-dirty: an include/target choice never
        # stales the FEM solve, it only changes this tab's analysis.
        self._display_dirty = True
        self._invalidate_caps_cache()
        # Every one of these overrides is an input to the impedance model --
        # C and the parasitics directly, include/target through which branches
        # exist and how long their mounting loop is — so a plot already on
        # screen is now stale. Same guard the package-library editor uses.
        if getattr(self, "_impedance_populated", False):
            self._replot_impedance()

    def _update_caps_tab_title(self, warn_count: int) -> None:
        idx = getattr(self, "_caps_tab_index", -1)
        if idx < 0:
            return
        title = ("Capacitors" if warn_count == 0
                 else f"Capacitors ⚠ {warn_count}")
        self.tabs.setTabText(idx, title)

    def _on_caps_overlay_toggled(self, checked: bool) -> None:
        """Re-render the heatmap so the cap overlay appears / clears. The
        rows must exist first — the overlay reads the same cache the table
        does, and the tab may never have been opened, so this can be the
        build that pays for the geometry."""
        if checked:
            self._ensure_cap_rows_async(self._render)
        else:
            self._render()

    # --- Capacitors tab: Tier-2 / Tier-3 solve -------------------------------

    def _on_compute_cap_tier23(self) -> None:
        """Kick off the background cavity solve for every included cap.

        The rows (and with them the cached copper geometry the solve needs)
        may not exist yet if the user pressed the button on a freshly-opened
        tab, so build them behind their own busy dialog first."""
        if getattr(self, "_caploop_worker", None) is not None:
            return
        self._ensure_cap_rows_async(self._start_cap_tier23)

    def _start_cap_tier23(self) -> None:
        """Launch the Tier-2/3 worker behind a cancellable progress dialog."""
        if getattr(self, "_caploop_worker", None) is not None:
            return
        rows = self._get_or_compute_cap_rows()
        caps = [r["cap"] for r in rows if r.get("included", True)]
        if not caps:
            QMessageBox.information(
                self, "Nothing to compute",
                "No capacitors are included in the analysis."
                if rows else
                (getattr(self, "_caps_empty_reason", "")
                 or "No decoupling capacitors were found."))
            return
        loaded = getattr(self, "_loaded_project", None)
        extracted = getattr(loaded, "extracted", None)
        if extracted is None:
            return

        from pdnsolver import mesh as _pdn_mesh

        # Already unioned by the row build (same cache), so this is a lookup,
        # not the 5–11 s geometry pass it used to be on the GUI thread.
        shapes = self._cap_net_layer_shapes(extracted)

        # Cavity domains are a single layer, so the board's mesh size is
        # affordable here. ``mesh_max_size_mm = 0`` means "no cap" on the
        # solve path; Config is frozen and takes a plain float, so fall back
        # to its own default rather than passing None through.
        _cfg_kwargs = {"minimum_angle": self._solve_settings.mesh_min_angle_deg}
        if self._solve_settings.mesh_max_size_mm > 0:
            _cfg_kwargs["maximum_size"] = self._solve_settings.mesh_max_size_mm
        mesher_config = _pdn_mesh.Mesher.Config(**_cfg_kwargs)

        self.caps_tier23_btn.setEnabled(False)
        self.caps_progress_label.setText("Starting cavity solve…")
        # Only the *included* capacitors are handed to the worker, so the
        # completion handler must not report the excluded ones as "skipped" —
        # they were never asked for.
        self._caploop_requested = {c.designator for c in caps}

        worker = _CapLoopWorker(
            extracted, caps, shapes, self._rail_to_members,
            self._caploop_settings(), mesher_config, parent=self)

        # One FEM solve per capacitor, so the bar can be determinate. The
        # dialog's Cancel sets the worker's flag; run_tier2 checks it between
        # solves and unwinds.
        dlg = QProgressDialog(
            f"Solving plane-pair spreading inductance for "
            f"{len(caps)} capacitor(s)…", "Cancel", 0, len(caps), self)
        dlg.setWindowTitle("Capacitor loop inductance")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        dlg.setValue(0)
        dlg.canceled.connect(worker.cancel)
        self._caploop_progress_dlg = dlg

        def _on_progress(message: str) -> None:
            self.caps_progress_label.setText(message)
            dlg.setLabelText(message)
            # Worker messages read "Cavity solve 3/12: C41" — advance the bar
            # on the cap index rather than inventing a second counter.
            m = re.match(r"Cavity solve (\d+)/", message)
            if m:
                dlg.setValue(int(m.group(1)) - 1)

        worker.progress.connect(_on_progress)
        worker.finished_ok.connect(self._on_cap_tier23_done)
        worker.failed.connect(self._on_cap_tier23_failed)
        worker.finished.connect(self._on_cap_tier23_cleanup)
        self._caploop_worker = worker
        worker.start()

    def _on_cap_tier23_done(self, results, matrices, tier3) -> None:
        """Merge the solved Tier-2/3 values into the cached rows.

        Every row ends up with an ``l2_reason``, because the progress line
        tells the user to hover the L2 cell for it. A row the worker was never
        given (an excluded capacitor) says so, rather than falling through to
        the pre-solve "press Compute Tier 2/3" hint — and is not counted as
        skipped, because nothing was skipped: it wasn't asked for.

        The matrices are kept whole (not just their diagonals): a later
        PDN-impedance analysis needs the cap↔cap mutual terms, and they
        cannot be recovered from the per-cap scalars."""
        rows = getattr(self, "_caps_rows_cache", None) or []
        requested = getattr(self, "_caploop_requested", None)
        self._caploop_matrices = matrices
        solved = skipped = 0
        skip_reasons: dict[str, int] = {}
        for row in rows:
            designator = row["designator"]
            if requested is not None and designator not in requested:
                row["l2_nh"] = row["l3_nh"] = None
                row["tier3"] = None
                row["l3_is_partial"] = False
                row["l2_reason"] = (
                    "Excluded from the analysis — tick Use to include this "
                    "capacitor, then compute again.")
                continue

            res = results.get(designator)
            row["l2_nh"] = (None if res is None or res.spread_h is None
                            else res.spread_h * 1e9)
            row["l2_reason"] = (
                res.reason if res is not None
                else "The cavity solve returned no result for this capacitor.")
            t3 = tier3.get(designator)
            row["l3_nh"] = None if t3 is None else t3.total_h * 1e9
            row["l3_is_partial"] = bool(t3 is not None and t3.is_partial)
            row["tier3"] = t3
            if row["l2_nh"] is not None:
                solved += 1
            else:
                skipped += 1
                reason = row["l2_reason"]
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        # Per-rail rollup: what each IC sees with its caps in parallel.
        from fypa.caploop.tier3 import rail_rollup
        self._caploop_rollup = rail_rollup([
            (r["rail"], r["designator"], (r["l3_nh"] or 0.0) * 1e-9)
            for r in rows
            if r.get("included", True) and r.get("l3_nh")
        ])

        msg = f"Tier 2/3 complete — {solved} capacitor(s) solved"
        if skipped:
            # Name the reasons here rather than only in a tooltip: a user
            # reading "N skipped" shouldn't have to go hunting for why.
            top = sorted(skip_reasons.items(), key=lambda kv: -kv[1])[:2]
            detail = "; ".join(f"{count}× {reason.rstrip('.')}"
                               for reason, count in top)
            if len(skip_reasons) > 2:
                detail += f"; +{len(skip_reasons) - 2} other reason(s)"
            msg += f", {skipped} skipped ({detail})"
        self.caps_progress_label.setText(msg)
        self.caps_progress_label.setToolTip(
            "Hover a capacitor's L2 cell for its individual reason."
            if skipped else "")
        if getattr(self, "_caps_table_populated", False):
            self._populate_caps_table()
        # The overlay colours by the best available tier, which just changed.
        if getattr(self, "caps_overlay_box", None) is not None \
                and self.caps_overlay_box.isChecked():
            self._render()
        # Every capacitor's series resonance moves with its mounted L, so the
        # impedance plot is now stale — redraw it if it has ever been built.
        if getattr(self, "_impedance_populated", False):
            self._replot_impedance()

    def _on_cap_tier23_failed(self, message: str) -> None:
        self.caps_progress_label.setText("")
        if message == "Cancelled.":
            return
        QMessageBox.warning(
            self, "Capacitor loop-inductance solve failed",
            f"The Tier-2/3 solve did not complete:\n\n{message}\n\n"
            "The Tier-1 estimates in the table are unaffected.")

    def _on_cap_tier23_cleanup(self) -> None:
        # QProgressDialog.close() routes through reject() → cancel(), which
        # would re-emit ``canceled`` into a worker that has already finished.
        # Disconnect first, then close, or a completed solve looks cancelled.
        dlg = getattr(self, "_caploop_progress_dlg", None)
        if dlg is not None:
            try:
                dlg.canceled.disconnect()
            except (RuntimeError, TypeError):
                pass
            dlg.close()
            dlg.deleteLater()
        self._caploop_progress_dlg = None
        worker = getattr(self, "_caploop_worker", None)
        if worker is not None:
            worker.deleteLater()
        self._caploop_worker = None
        if hasattr(self, "caps_tier23_btn"):
            self.caps_tier23_btn.setEnabled(True)

    def _apply_caps_filter(self, *_args) -> None:
        """Hide rows failing the Rail / flagged-only / included-only
        filters, and refresh the summary line (which doubles as the
        empty-state explanation when there are no rows at all)."""
        if not hasattr(self, "caps_table"):
            return
        rows = getattr(self, "_caps_rows", [])
        if not rows:
            self.caps_summary_label.setText(
                getattr(self, "_caps_empty_reason", "") or "")
            return
        rail_choice = (self.caps_rail_combo.currentText()
                       if hasattr(self, "caps_rail_combo") else "All rails")
        flagged_only = (self.caps_flagged_only_box.isChecked()
                        if hasattr(self, "caps_flagged_only_box") else False)
        included_only = (self.caps_included_only_box.isChecked()
                         if hasattr(self, "caps_included_only_box") else False)

        visible = 0
        warn_visible = 0
        for tr in range(self.caps_table.rowCount()):
            action_item = self.caps_table.item(tr, self._CAPS_ACTION_COL)
            orig_idx = (action_item.data(_CAPS_TABLE_ROW_ROLE)
                        if action_item is not None else None)
            row = (rows[orig_idx]
                   if isinstance(orig_idx, int) and 0 <= orig_idx < len(rows)
                   else {})
            rail_ok = (rail_choice == "All rails"
                       or row.get("rail") == rail_choice)
            is_warn = self._cap_row_is_warn(row)
            warn_ok = (not flagged_only) or is_warn
            incl_ok = (not included_only) or row.get("included", True)
            hide = not (rail_ok and warn_ok and incl_ok)
            self.caps_table.setRowHidden(tr, hide)
            if not hide:
                visible += 1
                if is_warn:
                    warn_visible += 1
        included_total = sum(1 for r in rows if r.get("included", True))
        text = (f"{visible} of {len(rows)} capacitor(s) shown — "
                f"{included_total} included, {warn_visible} flagged")
        # Once Tier 3 has run, append what the IC actually sees: every
        # included cap's loop, in parallel. This is the headline number, so
        # it must not hide behind the rail filter — with "All rails" selected
        # we list the rails compactly rather than showing nothing.
        rollup = getattr(self, "_caploop_rollup", None)
        if rollup:
            if rail_choice in rollup:
                shown = [rollup[rail_choice]]
                more = 0
            else:
                ordered = sorted(rollup.values(),
                                 key=lambda s: -s.parallel_h)
                shown, more = ordered[:3], max(0, len(ordered) - 3)
            parts = [
                f"{s.rail}: {s.cap_count} cap(s) in parallel = "
                f"{s.parallel_h * 1e9:.3g} nH (best {s.min_h * 1e9:.3g} nH)"
                for s in shown
            ]
            if more:
                parts.append(f"+{more} more rail(s)")
            text += " · " + " · ".join(parts)
        self.caps_summary_label.setText(text)
