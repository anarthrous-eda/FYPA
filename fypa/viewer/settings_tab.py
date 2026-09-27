"""The Settings tab, shared by the launcher and the viewer."""
from __future__ import annotations

import os
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.display import _DISPLAY_PERCENTILE_HIGH, _VIA_CURRENT_WARN_A
from fypa.viewer.numeric import _numeric_validator, _parse_numeric_text
from fypa.viewer.prefs import (
    _apply_performance_prefs,
    _FUSE_BACKENDS_UI,
    load_adaptive_regulator_gain,
    load_auto_solve_on_import,
    load_fuse_backend,
    load_mesh_max_workers,
    load_minres_budget_s,
    load_recent_open_clean,
    load_solve_in_subprocess,
    load_supersampling_enabled,
    load_via_no_current_opacity,
    save_auto_solve_on_import,
    save_fuse_backend,
    save_mesh_max_workers,
    save_minres_budget_s,
    save_recent_open_clean,
    save_solve_in_subprocess,
    save_supersampling_enabled,
    save_via_no_current_opacity,
)
from fypa.viewer.session import _viewer_has_adaptive_smps
from fypa.viewer.theme import (
    _T,
    _THEME_PRESETS,
    apply_app_theme,
    current_theme_mode,
    save_theme_mode,
)
from fypa.viewer.widgets import _esc


class _SettingsTabMixin:
    """Shared construction of the Settings tab.

    Inherited by :class:`PdnViewer` (full tab, design loaded) and by
    :class:`LauncherWindow` (no design — a subset built in launcher mode).
    Launcher mode is signalled by ``self._settings_launcher_mode`` being
    truthy; the design-only sections (Project/PcbDoc labels, stackup, the
    Re-run Solver / Reload Design Info actions) are skipped in that mode.
    """

    # --- Settings tab --------------------------------------------------------

    # Form-field schema: each entry is
    #   (attr_name, label, unit, getter_from_settings, default_text, tooltip)
    # The attr_name doubles as the QLineEdit's instance-attribute name on
    # the viewer (``self.settings_edit_<attr_name>``) and as a key in the
    # SolveSettings dataclass. ``getter_from_settings`` extracts the
    # current value from a SolveSettings instance, formatted for display.
    # ``default_text`` is the static "(default: …)" hint shown beside the
    # field so users always know the unmodified value.
    _SETTINGS_FIELDS: tuple[tuple[str, str, str, str], ...] = (
        # (key, label, unit, tooltip)
        ("temperature_c",
         "Board temperature",
         "°C",
         "Operating temperature of the copper. Drives the temperature-"
         "corrected sheet conductivity used by every layer in the FEM."),
        ("copper_resistivity_20c_microohm_cm",
         "Copper resistivity (at 20 °C)",
         "µΩ·cm",
         "Bulk copper resistivity at the 20 °C reference. Default 1.68 "
         "µΩ·cm matches annealed (IACS 100 %) copper. Lower for rolled / "
         "ED copper, higher for thin plated foil."),
        ("copper_temp_coefficient_per_c",
         "Copper temperature coefficient α",
         "1/°C",
         "Linear temperature coefficient of resistivity. Default 0.00393 "
         "/°C is the standard value for annealed copper."),
        ("heat_transfer_w_per_m2k",
         "Board heat transfer coefficient",
         "W/(m²·K)",
         "Only used when 'Coupled electro-thermal solve' is ticked. How "
         "readily the board sheds heat to ambient, counting both faces. "
         "~20 is a bare board in still air; 50–100 with forced air or a "
         "chassis heatsink; lower for a conformally coated board in a "
         "sealed box. Lower values mean hotter copper and a larger IR "
         "drop."),
        ("plating_thickness_mm",
         "Via plating thickness",
         "mm",
         "Plated-through-hole copper wall thickness. Default 0.025 mm "
         "(~1 mil) matches IPC-A-600 Class 2; bump to 0.030–0.050 mm for "
         "Class 3 / heavy-copper builds."),
        ("coupling_resistance_ohm",
         "Multi-pin coupling resistance",
         "Ω",
         "Small star-topology resistor used to tie each pin of a multi-pin "
         "terminal back to its main NodeID. Should stay << any real trace "
         "resistance — change only if you know why."),
        ("fallback_via_resistance_ohm",
         "Fallback via resistance",
         "Ω",
         "Per-hop resistance assigned to vias whose drill geometry is "
         "missing or degenerate. Most boards never hit this fallback."),
        ("conductive_fill_resistivity_ohm_mm",
         "Conductive fill resistivity",
         "Ω·mm",
         "Bulk resistivity of IPC-4761 conductive via-fill (copper / silver "
         "paste). Default 5e-3 Ω·mm matches typical silver-loaded epoxy. "
         "Lower for pure electroplated-copper fills. Only used when Altium "
         "marks the via as filled AND the FILLING row's material classifies "
         "as conductive — non-conductive fills leave the resistance "
         "unchanged."),
        ("mesh_min_angle_deg",
         "Mesh minimum angle",
         "°",
         "Triangle quality constraint passed to the Triangle mesher (0–34 "
         "is safe; higher values can stall on tight features). Smaller "
         "values mesh faster but yield poorer-conditioned FEM matrices."),
        ("mesh_max_size_mm",
         "Mesh maximum edge size",
         "mm",
         "Cap on triangle edge length (and area). Smaller = denser mesh, "
         "slower solve, finer-resolution voltage maps. 0 disables the cap."),
    )

    # Display-only knobs (no re-solve needed; applied immediately).
    _SETTINGS_DISPLAY_FIELDS: tuple[tuple[str, str, str, str], ...] = (
        ("via_current_warn_a",
         "Via current warning level |I|",
         "A",
         "Vias whose worst-segment current is at or above this threshold "
         "are highlighted red in the Vias tab and contribute to the tab-title "
         "warning count."),
        ("display_percentile_high",
         "Heatmap colour-scale clip percentile",
         "%",
         "Default upper clamp for Current Density / Power Density modes. "
         "Set to e.g. 99 to suppress single-vertex FEM singularity spikes "
         "and let the rest of the board use the full colour scale."),
    )

    # Capacitor loop-inductance knobs (Capacitors tab; no re-solve —
    # applied by the group's "Re-run Capacitor Check" button). Keys map
    # 1:1 onto fypa.caploop.constants.CapLoopSettings fields.
    _SETTINGS_CAPLOOP_FIELDS: tuple[tuple[str, str, str, str], ...] = (
        ("escape_via_search_mm",
         "Escape-via search radius",
         "mm",
         "Radius around a capacitor pad searched for candidate same-net "
         "escape vias."),
        ("escape_via_max_dist_mm",
         "Escape-via cluster limit",
         "mm",
         "Vias beyond this distance don't join the local escape cluster; "
         "if nothing is closer, the single nearest via (within the search "
         "radius) is used and the cap is flagged long-escape."),
        ("escape_cluster_slack",
         "Escape-cluster slack factor",
         "×",
         "Cluster membership window as a multiple of the nearest "
         "candidate's distance — rejects stitching fields that fall inside "
         "the search radius."),
        ("mutual_coupling_factor",
         "Via mutual-coupling factor k",
         "",
         "Derating for N parallel via pairs: L = L_single · k / N. "
         "k = 1 would mean fully independent pairs; real adjacent pairs "
         "share flux (≈ 0.8)."),
        ("tier1_r_far_default_mm",
         "Default spreading radius",
         "mm",
         "Closed-form spreading-term outer radius used when a capacitor "
         "has no target device."),
        ("fallback_via_loop_nh",
         "Fallback loop inductance",
         "nH",
         "Assigned when geometry is too degenerate for the closed forms "
         "(no escape via / no reference cavity). Shown with a ~ prefix."),
        ("long_escape_warn_mm",
         "Long-escape warning distance",
         "mm",
         "Pad-to-via escape runs longer than this raise the long-escape "
         "flag."),
        ("far_plane_warn_mm",
         "Far-plane warning depth",
         "mm",
         "Mounting-surface → nearest-reference-plane depth beyond which "
         "the far-plane flag is raised."),
        ("cap_l_warn_nh",
         "Loop inductance warning level",
         "nH",
         "Capacitors whose best-available loop inductance is at or above "
         "this are highlighted red and counted in the tab-title badge."),
        ("plane_antipad_clearance_mm",
         "Cavity anti-pad clearance",
         "mm",
         "Extra clearance punched around via bores in the Tier-2 cavity "
         "sheet when the extraction didn't already perforate the plane."),
    )

    def _build_settings_tab(self) -> QWidget:
        """Build the Settings tab — tunable physics + mesh + display knobs
        and a Re-run Solver button.

        Editing a field does NOT immediately re-solve; the user must press
        "Re-run Solver" to spawn a fresh solve with the new parameters.
        Display-only fields (warning threshold, percentile) are also
        applied by the same button, but those are cheap and never trigger
        an FEM rebuild on their own.

        In launcher mode (``self._settings_launcher_mode`` truthy, no design
        loaded) the design-only sections are skipped: the Project/PcbDoc
        labels, the stackup copper-thickness group, and the Re-run Solver /
        Reload Design Info actions. The remaining physics / fill / display
        fields act as session defaults only — they are not persisted, so
        editing them in the launcher does not carry into a later import."""
        launcher = bool(getattr(self, "_settings_launcher_mode", False))
        widget = QWidget(self.tabs)
        scroll = QScrollArea(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        t = _T()
        scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {t['bg']}; }}"
        )

        inner = QWidget()
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # ----- Intro -----
        intro = QLabel(
            "Application preferences below persist across launches. The "
            "solve, via-fill and display fields shown for reference take "
            "effect once a design is loaded and re-solved."
            if launcher else
            "Adjust the physics and meshing parameters below, then click "
            "<b>Re-run Solver</b> to re-solve the current project with the "
            "new values. The viewer will reload with the fresh result."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"QLabel {{ color: {t['fg_label']}; }}")
        outer.addWidget(intro)

        # Project / PcbDoc labels only make sense once a design is loaded.
        if not launcher:
            prjpcb = ""
            pcbdoc = ""
            if self.metadata:
                prjpcb = str(self.metadata.get("prjpcb_path") or "")
                pcbdoc = str(self.metadata.get("pcbdoc_path") or "")
            project_lbl = QLabel(
                f"<span style='color:{t['fg_dim']};'>Project:</span> "
                f"<code style='color:{t['code']};'>{_esc(prjpcb) or '(not in metadata)'}</code>"
            )
            project_lbl.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")
            project_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            project_lbl.setWordWrap(True)
            outer.addWidget(project_lbl)

            pcbdoc_lbl = QLabel(
                f"<span style='color:{t['fg_dim']};'>PcbDoc:</span> "
                f"<code style='color:{t['code']};'>{_esc(pcbdoc) or '(not in metadata)'}</code>"
            )
            pcbdoc_lbl.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")
            pcbdoc_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            pcbdoc_lbl.setWordWrap(True)
            outer.addWidget(pcbdoc_lbl)

        # ----- Appearance group -----
        outer.addWidget(self._build_appearance_settings_box())

        # ----- Solve parameters group -----
        solve_box = self._make_settings_group(
            "Solve parameters (require re-solve)",
            self._SETTINGS_FIELDS,
            source=self._solve_settings,
            attr_prefix="settings_edit_",
            # The conductive-fill resistivity field is rebuilt in its own
            # "Conductive via fill" group below (alongside the mode + material
            # pickers) — keep it out of the generic solve box here.
            skip_keys={"conductive_fill_resistivity_ohm_mm"},
        )
        # Adaptive-mesh toggle — a boolean, so not part of the QLineEdit-
        # based _SETTINGS_FIELDS schema; add it into the same group box.
        self._settings_adaptive_check = QCheckBox(
            "Adaptive (variable-density) mesh")
        self._settings_adaptive_check.setChecked(
            bool(getattr(self._solve_settings, "adaptive_mesh", False)))
        self._settings_adaptive_check.setToolTip(
            "Variable-density meshing — a faster approximation. Fine near "
            "pins, vias and copper edges; coarser elsewhere, which slightly "
            "reduces accuracy on current-carrying copper away from "
            "terminals. Best for boards with large low-current pours; "
            "little benefit on densely via-stitched planes. "
            "Off = uniform mesh (most accurate — the default)."
        )
        self._settings_adaptive_check.toggled.connect(
            self._on_settings_field_changed)
        self._settings_area_weighted_check = QCheckBox(
            "Weight multi-pin coupling by pad area")
        self._settings_area_weighted_check.setChecked(
            bool(getattr(
                self._solve_settings, "area_weighted_pin_coupling", False)))
        self._settings_area_weighted_check.setToolTip(
            "Scale each multi-pin star coupling resistor inversely with that "
            "pin's pad area (R ∝ 1/A), so larger pads — including QFN thermal "
            "pads on GND — take a larger share of supply and return current "
            "when copper access is similar. Off = equal R per pin (default)."
        )
        self._settings_area_weighted_check.toggled.connect(
            self._on_settings_field_changed)
        # Coupled electro-thermal solve. Off by default: with it off the
        # solve is bit-identical to an isothermal run, so every existing
        # result is reproduced exactly.
        self._settings_electrothermal_check = QCheckBox(
            "Coupled electro-thermal solve (self-heating)")
        self._settings_electrothermal_check.setChecked(
            bool(getattr(self._solve_settings, "electrothermal", False)))
        self._settings_electrothermal_check.setToolTip(
            "Iterate self-heating: copper that dissipates power gets hotter, "
            "so more resistive, so it drops more and heats further. Copper "
            "rises ~0.39 %/K, so a heavily loaded rail can read 10–20 % "
            "optimistic without this.\n\n"
            "Heat is shed locally to ambient using the 'Board heat transfer "
            "coefficient' above — there is no lateral heat spreading, so a "
            "small hot feature reads as an upper bound while a broad one is "
            "about right. Costs one extra factorisation per iteration "
            "(typically 3–8).\n\n"
            "Off = isothermal at the board temperature (the default)."
        )
        self._settings_electrothermal_check.toggled.connect(
            self._on_settings_field_changed)
        self._settings_electrothermal_check.toggled.connect(
            self._on_electrothermal_toggled)
        _solve_layout = solve_box.layout()
        if _solve_layout is not None:
            _solve_layout.addRow(self._settings_adaptive_check)
            _solve_layout.addRow(self._settings_area_weighted_check)
            _solve_layout.addRow(self._settings_electrothermal_check)
        self._apply_electrothermal_enabled()
        # Adaptive SMPS regulator-gain iteration — moved here from the editor
        # canvas overlay. Same attribute name + handler as before, so the
        # solve-time reads and resolve-enable logic are unchanged. Enabled only
        # for designs with an auto-gain SMPS regulator; disabled (with an
        # explanatory tooltip) otherwise — _refresh_solve_stale_overlay keeps
        # the enabled state in sync as the design changes. Skipped in launcher
        # mode: it is design-specific and its toggle handler lives on the
        # viewer, not the mixin.
        if not launcher:
            self._adaptive_gain_check = QCheckBox("Adaptive SMPS gain")
            self._adaptive_gain_check.setChecked(load_adaptive_regulator_gain())
            _ag_has_smps = _viewer_has_adaptive_smps(
                getattr(self, "metadata", None),
                getattr(self, "_loaded_project", None),
            )
            self._adaptive_gain_check.setEnabled(_ag_has_smps)
            self._adaptive_gain_check.setToolTip(
                "When checked, re-solve iterates SMPS regulator gain using the "
                "solved input voltage (includes SERIES and copper IR drop). LDO "
                "regulators are unaffected. Adds extra solve time."
                if _ag_has_smps else
                "Requires a REGULATOR with PDN_REGULATOR_TYPE=SMPS and no PDN_GAIN "
                "(auto-gain). LDO regulators and manual PDN_GAIN are not iterated."
            )
            self._adaptive_gain_check.toggled.connect(self._on_adaptive_gain_toggled)
            if _solve_layout is not None:
                _solve_layout.addRow(self._adaptive_gain_check)
        outer.addWidget(solve_box)

        # ----- Conductive via fill group -----
        outer.addWidget(self._build_conductive_fill_box())

        # ----- Stackup copper-thickness group (design-specific) -----
        if not launcher:
            outer.addWidget(self._build_stackup_settings_box())

        # ----- Display parameters group -----
        # Build a tiny ad-hoc object exposing the same attribute names as
        # the field schema so the same _make_settings_group helper works.
        class _DisplayValues:
            pass
        display_src = _DisplayValues()
        display_src.via_current_warn_a = self._via_current_warn_a
        display_src.display_percentile_high = self._display_percentile_high
        display_box = self._make_settings_group(
            "Display options"
            if launcher else
            "Display options (applied immediately on Re-run)",
            self._SETTINGS_DISPLAY_FIELDS,
            source=display_src,
            attr_prefix="settings_edit_",
        )
        outer.addWidget(display_box)

        # ----- Capacitor loop-inductance group (design-specific) -----
        # Analysis knobs for the Capacitors tab — no FEM re-solve; the
        # group's own button re-runs identification + Tier 1 in place.
        if not launcher:
            caploop_box = self._make_settings_group(
                "Capacitor loop inductance (applied by Re-run "
                "Capacitor Check)",
                self._SETTINGS_CAPLOOP_FIELDS,
                source=self._caploop_settings(),
                attr_prefix="settings_edit_cl_",
            )
            _cl_layout = caploop_box.layout()
            if _cl_layout is not None:
                cl_btn = QPushButton("Re-run Capacitor Check")
                cl_btn.setToolTip(
                    "Apply the values above to the Capacitors tab — "
                    "re-identifies escape vias / cavities and recomputes "
                    "the Tier-1 inductances. No FEM re-solve.")
                cl_btn.clicked.connect(self._on_rerun_cap_check)
                cl_row = QHBoxLayout()
                cl_row.addWidget(cl_btn)
                cl_row.addStretch(1)
                _cl_layout.addRow(cl_row)
            outer.addWidget(caploop_box)

        # ----- General options group -----
        # App-behaviour preferences, persisted immediately (not part of the
        # re-solve field schema).
        gen_box = QGroupBox("General options")
        gen_layout = QVBoxLayout(gen_box)
        self._settings_auto_solve_check = QCheckBox(
            "Solve automatically on Altium import")
        self._settings_auto_solve_check.setChecked(load_auto_solve_on_import())
        self._settings_auto_solve_check.setToolTip(
            "When checked, Import Altium Design runs the FEM solver immediately "
            "(reusing the solve cache when possible). When unchecked, only "
            "design info is loaded — press ↻ Solve in the viewer. Persists "
            "across launches."
        )
        self._settings_auto_solve_check.toggled.connect(
            lambda checked: save_auto_solve_on_import(bool(checked)))
        gen_layout.addWidget(self._settings_auto_solve_check)

        self._settings_recent_clean_check = QCheckBox(
            "Load recent projects clean")
        self._settings_recent_clean_check.setChecked(load_recent_open_clean())
        self._settings_recent_clean_check.setToolTip(
            "When checked, re-opening an Altium project from File > Recent "
            "Projects ignores the design and solve caches and re-extracts "
            "from the Altium files (slower, but picks up edits made in "
            "Altium). When unchecked, the caches are reused — same as "
            "File > Import Altium Design. Persists across launches."
        )
        self._settings_recent_clean_check.toggled.connect(
            lambda checked: save_recent_open_clean(bool(checked)))
        gen_layout.addWidget(self._settings_recent_clean_check)

        # No-current via opacity — fade level (3D) of via-barrel sections that
        # carry no current on the selected rail. Persisted immediately and
        # live-applied to an open viewer (no re-solve).
        self._settings_via_opacity_spin = QSpinBox()
        self._settings_via_opacity_spin.setRange(0, 100)
        self._settings_via_opacity_spin.setSuffix(" %")
        self._settings_via_opacity_spin.setValue(
            int(round(load_via_no_current_opacity() * 100)))
        self._settings_via_opacity_spin.setToolTip(
            "Opacity of via-barrel sections carrying no current on the "
            "selected rail — the 3D cylinder stub ends outside the via's "
            "current-carrying copper. 100% = fully solid; lower fades them so "
            "the current-carrying span stands out. 3D view only; persists "
            "across launches."
        )

        def _on_via_opacity_changed(pct: int) -> None:
            alpha = int(pct) / 100.0
            save_via_no_current_opacity(alpha)
            apply = getattr(self, "_apply_via_no_current_opacity", None)
            if callable(apply):
                apply(alpha)

        self._settings_via_opacity_spin.valueChanged.connect(
            _on_via_opacity_changed)

        via_op_row = QHBoxLayout()
        via_op_row.addWidget(QLabel("No-current via opacity"))
        via_op_row.addStretch(1)
        via_op_row.addWidget(self._settings_via_opacity_spin)
        gen_layout.addLayout(via_op_row)

        outer.addWidget(gen_box)

        # ----- Performance group -----
        # Tunables previously reachable only via env vars. Persisted and applied
        # (to os.environ + pdnsolver) immediately; they affect the NEXT solve /
        # load, not the current result — no re-solve needed to persist them.
        perf_box = QGroupBox("Performance")
        perf_form = QFormLayout(perf_box)

        # Display label per backend; the stored value stays the bare backend
        # id (kept in item data) so ``save_fuse_backend`` still sees "clipper".
        _fuse_labels = {"clipper": "clipper (recommended)", "shapely": "shapely"}
        self._settings_fuse_combo = QComboBox()
        for _fb in _FUSE_BACKENDS_UI:
            self._settings_fuse_combo.addItem(_fuse_labels.get(_fb, _fb), _fb)
        _cur_fb = load_fuse_backend()
        self._settings_fuse_combo.setCurrentIndex(
            _FUSE_BACKENDS_UI.index(_cur_fb) if _cur_fb in _FUSE_BACKENDS_UI
            else 0)
        self._settings_fuse_combo.setToolTip(
            "Geometry-fusion backend used when building copper polygons.\n"
            "• clipper — Clipper2, the fast default (recommended).\n"
            "• shapely — the legacy GEOS path (slower; reference)."
        )
        self._fit_combo_width(self._settings_fuse_combo)

        self._settings_mesh_workers_spin = QSpinBox()
        self._settings_mesh_workers_spin.setRange(1, max(1, os.cpu_count() or 1))
        self._settings_mesh_workers_spin.setValue(load_mesh_max_workers())
        self._settings_mesh_workers_spin.setToolTip(
            "Number of worker processes used for meshing. Higher can be faster "
            "on many-core machines but adds memory + spawn overhead; the "
            "default is a physical-core estimate."
        )
        self._fit_field_width(self._settings_mesh_workers_spin)

        self._settings_minres_spin = QSpinBox()
        self._settings_minres_spin.setRange(10, 3600)
        self._settings_minres_spin.setSingleStep(30)
        self._settings_minres_spin.setSuffix(" s")
        self._settings_minres_spin.setValue(load_minres_budget_s())
        self._settings_minres_spin.setToolTip(
            "Wall-clock budget for the iterative fallback solver (only reached "
            "when the direct solve can't be used). On timeout the best iterate "
            "so far is kept. Raise this for very large boards."
        )
        self._fit_field_width(self._settings_minres_spin)

        # Connect AFTER setting initial values so seeding them doesn't fire a
        # spurious save/apply.
        def _on_fuse_changed(_idx: int) -> None:
            save_fuse_backend(self._settings_fuse_combo.currentData())
            _apply_performance_prefs()

        def _on_workers_changed(val: int) -> None:
            save_mesh_max_workers(int(val))
            _apply_performance_prefs()

        def _on_minres_changed(val: int) -> None:
            save_minres_budget_s(int(val))
            _apply_performance_prefs()

        self._settings_fuse_combo.currentIndexChanged.connect(_on_fuse_changed)
        self._settings_mesh_workers_spin.valueChanged.connect(_on_workers_changed)
        self._settings_minres_spin.valueChanged.connect(_on_minres_changed)

        perf_form.addRow("Fusion backend", self._settings_fuse_combo)
        perf_form.addRow("Mesh worker processes", self._settings_mesh_workers_spin)
        perf_form.addRow("Iterative-solver timeout", self._settings_minres_spin)
        outer.addWidget(perf_box)

        # ----- Experimental group -----
        # Settings here are persisted immediately (not part of the re-solve
        # field schema) and take effect on the NEXT solve.
        exp_box = QGroupBox("Experimental")
        exp_layout = QVBoxLayout(exp_box)
        self._settings_subprocess_check = QCheckBox("Run solve in a subprocess")
        self._settings_subprocess_check.setChecked(load_solve_in_subprocess())
        self._settings_subprocess_check.setToolTip(
            "Run the mesh + solve in a separate process, so Cancel becomes a "
            "clean kill of that process (no risk of a stuck solve leaving the "
            "app locked). Trade-off: a fresh process per solve does not reuse "
            "the warm mesh / factorisation caches, so repeated re-solves are "
            "slower. Applies to the next solve; persists across launches. "
            "Off by default."
        )
        self._settings_subprocess_check.toggled.connect(
            lambda checked: save_solve_in_subprocess(bool(checked)))
        exp_layout.addWidget(self._settings_subprocess_check)
        outer.addWidget(exp_box)

        # ----- Status line + buttons -----
        self._settings_status_label = QLabel("")
        self._settings_status_label.setWordWrap(True)
        self._settings_status_label.setStyleSheet(
            f"QLabel {{ color: {t['accent']}; padding: 4px 0; }}"
        )
        outer.addWidget(self._settings_status_label)

        button_row = QHBoxLayout()
        # Re-run Solver / Reload Design Info act on a loaded design, so they
        # are omitted in launcher mode — only Reset to defaults is shown there.
        if not launcher:
            self._settings_rerun_btn = QPushButton("Re-run Solver")
            self._settings_rerun_btn.setToolTip(
                "Re-solve the current project with the parameters above. "
                "Updates the heatmap in this window when the solve finishes."
            )
            self._settings_rerun_btn.setStyleSheet(
                f"QPushButton {{ background-color: {t['accent_btn']}; color: {t['fg_strong']};"
                f"              border: 1px solid {t['accent_btn_hov']}; padding: 6px 14px;"
                f"              font-weight: 600; }}"
                f"QPushButton:hover {{ background-color: {t['accent_btn_hov']}; }}"
                f"QPushButton:disabled {{ background-color: {t['bg_hover']}; color: {t['fg_hint']}; }}"
            )
            self._settings_rerun_btn.clicked.connect(self._on_rerun_solver)
            button_row.addWidget(self._settings_rerun_btn)

            self._settings_reload_design_btn = QPushButton("Reload Design Info")
            self._settings_reload_design_btn.setToolTip(
                "Re-extract the design (geometry + annotations) from the on-disk "
                "Altium project, ignoring the design-info cache. Then re-solve "
                "with the current settings. Use this when you've edited the "
                ".PrjPcb / .PcbDoc and want to be sure FYPA picks up the change."
            )
            self._settings_reload_design_btn.setStyleSheet(
                f"QPushButton {{ background-color: {t['bg_hover']}; color: {t['fg']};"
                f"              border: 1px solid {t['border']}; padding: 6px 14px; }}"
                f"QPushButton:hover {{ background-color: {t['bg_hover_strong']}; }}"
                f"QPushButton:disabled {{ background-color: {t['bg_hover']}; color: {t['fg_hint']}; }}"
            )
            self._settings_reload_design_btn.clicked.connect(self._on_reload_design_info)
            button_row.addWidget(self._settings_reload_design_btn)

        reset_btn = QPushButton("Reset to defaults")
        reset_btn.setToolTip(
            "Restore every field above to its built-in default value."
            if launcher else
            "Restore every field above to its built-in default value "
            "(does NOT re-solve — press Re-run Solver to commit)."
        )
        reset_btn.setStyleSheet(
            f"QPushButton {{ background-color: {t['bg_hover']}; color: {t['fg']};"
            f"              border: 1px solid {t['border']}; padding: 6px 14px; }}"
            f"QPushButton:hover {{ background-color: {t['bg_hover_strong']}; }}"
        )
        reset_btn.clicked.connect(self._on_settings_reset)
        button_row.addWidget(reset_btn)
        button_row.addStretch(1)
        outer.addLayout(button_row)

        outer.addStretch(1)

        scroll.setWidget(inner)
        # Wrap the scroll area in the returned widget so addTab gets a
        # plain QWidget with the same background as the others.
        wrap_layout = QVBoxLayout(widget)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.addWidget(scroll)
        widget.setStyleSheet(
            f"QWidget {{ background-color: {t['bg']}; color: {t['fg']}; }}"
            f"QGroupBox {{ border: 1px solid {t['border']}; border-radius: 4px;"
            f"            margin-top: 14px; padding: 12px;"
            f"            background-color: {t['bg']}; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px;"
            f"                   padding: 0 6px; color: {t['fg_strong']};"
            f"                   font-weight: 600; }}"
            f"QLineEdit {{ background-color: {t['bg_input']}; color: {t['fg']};"
            f"            border: 1px solid {t['border']}; padding: 3px 6px;"
            f"            selection-background-color: {t['bg_selection']}; }}"
            f"QLineEdit:focus {{ border: 1px solid {t['accent']}; }}"
            # Out-of-date field outline / colour: the dynamic ``dirty``
            # property is toggled by _update_settings_field_styles when
            # the field diverges from / matches the current solve.
            f"QLineEdit[dirty=\"true\"] {{ border: 1px solid {t['warn_fg']}; }}"
            f"QLineEdit[dirty=\"true\"]:focus {{ border: 1px solid {t['warn_fg']}; }}"
            f"QCheckBox[dirty=\"true\"] {{ color: {t['warn_fg']}; }}"
            f"QComboBox[dirty=\"true\"] {{ border: 1px solid {t['warn_fg']}; }}"
        )
        return widget

    @staticmethod
    def _fit_field_width(widget: QWidget, margin: float = 0.1) -> None:
        """Cap a Settings-tab field to its content width (+ ~``margin``)
        rather than letting the QFormLayout stretch it the full panel width.

        A full-width combo/spin box is an easy accidental target while
        scrolling the Settings tab — a stray wheel tick lands on it and
        silently changes the value. Sizing to content keeps the hit area
        small. Populate/configure the widget (items, range, suffix) before
        calling this — the width comes from its ``sizeHint``."""
        width = int(widget.sizeHint().width() * (1.0 + margin))
        widget.setMaximumWidth(width)
        # Maximum (not Expanding) so the form layout can't grow it past the
        # hint; it may still shrink on a very narrow window.
        widget.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

    @classmethod
    def _fit_combo_width(cls, combo: QComboBox, margin: float = 0.1) -> None:
        """As ``_fit_field_width`` but first makes the combo size to its
        widest item (``AdjustToContents``) instead of a fixed default."""
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        cls._fit_field_width(combo, margin)

    def _make_settings_group(
        self, title: str,
        fields: tuple[tuple[str, str, str, str], ...],
        source: object,
        attr_prefix: str,
        skip_keys: frozenset[str] | set[str] | None = None,
    ) -> QGroupBox:
        """Build a QGroupBox containing one labelled QLineEdit per field
        in ``fields``. The current value is pulled from ``source`` via
        ``getattr``; default-value hints come from a fresh SolveSettings.

        ``skip_keys`` lets a caller omit specific fields from this box (so
        they can be rebuilt elsewhere) while keeping them in the shared
        ``_SETTINGS_FIELDS`` schema that reset / dirty / gather iterate."""
        from fypa.altium.loader import SolveSettings as _SolveSettings
        from dataclasses import fields as _dc_fields
        defaults = _SolveSettings()
        # The display-only fields aren't in SolveSettings — fall back to
        # the module-level constants for their default-value hint. The
        # caploop fields likewise hint from their own dataclass defaults.
        from fypa.caploop.constants import CapLoopSettings as _CapLoopSettings
        import dataclasses as _dc
        display_defaults = {
            "via_current_warn_a": _VIA_CURRENT_WARN_A,
            "display_percentile_high": _DISPLAY_PERCENTILE_HIGH,
            **_dc.asdict(_CapLoopSettings()),
        }
        defaults_attrs = {f.name for f in _dc_fields(defaults)}

        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        for key, label, unit, tooltip in fields:
            if skip_keys and key in skip_keys:
                continue
            current = getattr(source, key, None)
            if current is None:
                continue
            default_val = (
                getattr(defaults, key) if key in defaults_attrs
                else display_defaults.get(key)
            )

            edit = QLineEdit(self._fmt_settings_value(current))
            edit.setValidator(_numeric_validator(self))
            edit.setMinimumWidth(110)
            edit.setMaximumWidth(160)
            edit.setToolTip(tooltip)
            setattr(self, f"{attr_prefix}{key}", edit)
            edit.textChanged.connect(self._on_settings_field_changed)

            _t = _T()
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(edit)
            unit_lbl = QLabel(unit)
            unit_lbl.setStyleSheet(f"QLabel {{ color: {_t['fg_muted']}; }}")
            row.addWidget(unit_lbl)
            row.addSpacing(8)
            hint = QLabel(f"<span style='color:{_t['fg_hint']};'>"
                          f"(default: {self._fmt_settings_value(default_val)})</span>"
                          if default_val is not None else "")
            row.addWidget(hint)
            if key == "via_current_warn_a" and not getattr(
                    self, "_settings_launcher_mode", False):
                # In-place re-check button: applies the new threshold to
                # the Vias tab without a full re-solve. Hidden until the
                # field diverges from the currently-applied level. Omitted in
                # launcher mode — it re-checks a loaded design's Vias tab, and
                # its handlers live on the viewer, not the mixin.
                row.addSpacing(12)
                btn = QPushButton("Re-run Via Check")
                btn.setToolTip(
                    "Re-check vias against the new warning level (no "
                    "re-solve). Refreshes the Vias tab title, summary, "
                    "and warning highlights in place."
                )
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {_t['accent_btn']};"
                    f"              color: {_t['fg_strong']};"
                    f"              border: 1px solid {_t['accent_btn_hov']};"
                    f"              padding: 3px 10px; font-weight: 600; }}"
                    f"QPushButton:hover {{ background-color: {_t['accent_btn_hov']}; }}"
                )
                btn.clicked.connect(self._on_rerun_via_check)
                btn.setVisible(False)
                self._settings_rerun_via_check_btn = btn
                row.addWidget(btn)
                edit.textChanged.connect(
                    lambda _t=None: self._update_via_check_btn_visibility()
                )
            row.addStretch(1)
            row_widget = QWidget()
            row_widget.setLayout(row)

            label_widget = QLabel(label)
            label_widget.setToolTip(tooltip)
            label_widget.setStyleSheet(f"QLabel {{ color: {_t['fg']}; }}")
            form.addRow(label_widget, row_widget)

        return box

    # Fill-material presets for the Conductive via fill picker. Each entry is
    # (label, resistivity_ohm_mm | None, tooltip). ``None`` marks the Custom
    # entry, which leaves the resistivity field free for manual entry. Values
    # are bulk resistivities converted to Ω·mm (1 Ω·cm = 10 Ω·mm):
    #   silver-loaded epoxy paste ~5e-4 Ω·cm, copper-loaded paste ~5e-3 Ω·cm,
    #   solid electroplated copper ~ bulk Cu (1.68 µΩ·cm).
    _FILL_MATERIAL_PRESETS: tuple[tuple[str, float | None, str], ...] = (
        ("Silver-filled epoxy paste", 5.0e-3,
         "Silver-loaded thermoset paste — the most common IPC-4761 "
         "conductive via fill (~5×10⁻⁴ Ω·cm bulk resistivity)."),
        ("Copper-filled paste / epoxy", 5.0e-2,
         "Copper-loaded paste/epoxy fill (~5×10⁻³ Ω·cm) — cheaper than "
         "silver but ~10× more resistive."),
        ("Solid electroplated copper", 1.7e-5,
         "Type-VII copper-filled via — the centre void is closed with "
         "electroplated copper, approaching bulk copper resistivity "
         "(~1.7×10⁻⁶ Ω·cm). The lowest-resistance fill."),
        ("Custom (enter value)", None,
         "Enter the fill resistivity directly in the field below — e.g. "
         "from the fab's via-fill paste data sheet."),
    )

    def _build_conductive_fill_box(self) -> QGroupBox:
        """Build the "Conductive via fill" group: an Auto/All/None override
        mode, a fill-material preset picker, and the fill-resistivity field.

        The resistivity QLineEdit is built here (instead of in the generic
        solve box) but keeps its ``settings_edit_conductive_fill_resistivity_
        ohm_mm`` attribute name, so the shared reset / dirty / gather loops
        — which walk ``_SETTINGS_FIELDS`` and resolve widgets by attribute —
        still pick it up unchanged."""
        fill_fields = tuple(
            f for f in self._SETTINGS_FIELDS
            if f[0] == "conductive_fill_resistivity_ohm_mm"
        )
        box = self._make_settings_group(
            "Conductive via fill",
            fill_fields,
            source=self._solve_settings,
            attr_prefix="settings_edit_",
        )
        form = box.layout()   # QFormLayout: row 0 is the resistivity field
        t = _T()

        # ----- Mode combo (Auto / force All / force None) -----
        self._fill_mode_combo = QComboBox()
        self._fill_mode_combo.addItem("Auto (from Altium IPC-4761 data)", "auto")
        self._fill_mode_combo.addItem("Force: treat all vias as filled", "all")
        self._fill_mode_combo.addItem("Force: treat no vias as filled", "none")
        self._fill_mode_combo.setToolTip(
            "Whether a via's centre is modelled as a conductive fill-rod in "
            "parallel with the plated barrel wall (lowering its resistance).\n"
            "• Auto — decide per via from the design's IPC-4761 FILLING "
            "material string (the default).\n"
            "• Force all — treat every coupled via/through-hole as filled "
            "(use the fab's fill spec when Altium has no IPC-4761 data).\n"
            "• Force none — ignore any fill; the plated wall is the only path."
        )
        cur_mode = getattr(self._solve_settings, "conductive_fill_mode", "auto")
        idx = self._fill_mode_combo.findData(cur_mode)
        if idx >= 0:
            self._fill_mode_combo.setCurrentIndex(idx)
        self._fit_combo_width(self._fill_mode_combo)
        self._fill_mode_combo.currentIndexChanged.connect(
            self._on_fill_mode_changed)
        self._fill_mode_combo.currentIndexChanged.connect(
            self._on_settings_field_changed)
        mode_label = QLabel("Fill mode")
        mode_label.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")

        # ----- Material preset combo -----
        self._fill_material_combo = QComboBox()
        for label, _rho, tip in self._FILL_MATERIAL_PRESETS:
            self._fill_material_combo.addItem(label)
            self._fill_material_combo.setItemData(
                self._fill_material_combo.count() - 1, tip, Qt.ToolTipRole)
        self._fill_material_combo.setToolTip(
            "Pick a fill material to populate the resistivity field below "
            "with a typical value, or choose Custom to type your own."
        )
        self._fit_combo_width(self._fill_material_combo)
        self._fill_material_combo.currentIndexChanged.connect(
            self._on_fill_material_changed)
        mat_label = QLabel("Fill material")
        mat_label.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")

        # Insert mode (row 0) and material (row 1) above the resistivity row.
        form.insertRow(0, mode_label, self._fill_mode_combo)
        form.insertRow(1, mat_label, self._fill_material_combo)

        # Initialise the material combo from the current resistivity, and
        # keep the two in sync when the user edits the resistivity directly.
        edit = getattr(self, "settings_edit_conductive_fill_resistivity_ohm_mm",
                       None)
        if edit is not None:
            edit.textChanged.connect(self._sync_material_combo_from_resistivity)
        self._sync_material_combo_from_resistivity()
        self._apply_fill_mode_enabled()
        return box

    def _apply_fill_mode_enabled(self) -> None:
        """Grey out the material + resistivity controls when the mode is
        "Force none" (no fill, so the value is irrelevant)."""
        mode = self._fill_mode_combo.currentData()
        enabled = mode != "none"
        self._fill_material_combo.setEnabled(enabled)
        edit = getattr(self, "settings_edit_conductive_fill_resistivity_ohm_mm",
                       None)
        if edit is not None:
            edit.setEnabled(enabled)

    def _on_fill_mode_changed(self, _idx: int) -> None:
        self._apply_fill_mode_enabled()

    def _on_fill_material_changed(self, idx: int) -> None:
        """Material picker → write the preset resistivity into the field.
        The Custom entry (resistivity None) leaves the field untouched."""
        if idx < 0 or idx >= len(self._FILL_MATERIAL_PRESETS):
            return
        _label, rho, _tip = self._FILL_MATERIAL_PRESETS[idx]
        if rho is None:
            return
        edit = getattr(self, "settings_edit_conductive_fill_resistivity_ohm_mm",
                       None)
        if edit is None:
            return
        new_text = self._fmt_settings_value(rho)
        if edit.text().strip() != new_text:
            # Guard against the textChanged → _sync → material-combo loop:
            # setText fires _sync, which would re-select this same preset.
            edit.setText(new_text)

    def _sync_material_combo_from_resistivity(self, *_args) -> None:
        """Resistivity field → select the matching material preset, or fall
        back to Custom when the value matches none of them."""
        combo = getattr(self, "_fill_material_combo", None)
        edit = getattr(self, "settings_edit_conductive_fill_resistivity_ohm_mm",
                       None)
        if combo is None or edit is None:
            return
        try:
            val = self._parse_settings_value(edit.text())
        except ValueError:
            return
        target = len(self._FILL_MATERIAL_PRESETS) - 1   # Custom by default
        for i, (_label, rho, _tip) in enumerate(self._FILL_MATERIAL_PRESETS):
            if rho is not None and abs(val - rho) <= abs(rho) * 1e-6:
                target = i
                break
        if combo.currentIndex() != target:
            combo.blockSignals(True)
            combo.setCurrentIndex(target)
            combo.blockSignals(False)

    def _build_appearance_settings_box(self) -> QWidget:
        """Theme picker (Dark / Light). The choice is persisted via
        :func:`save_theme_mode` and applied to the running QApplication
        immediately — the viewer window is rebuilt so the inline-styled
        widgets (layer list, tables, side panel) pick up the new colours.
        """
        t = _T()
        box = QGroupBox("Appearance")
        box.setStyleSheet(
            f"QGroupBox {{ border: 1px solid {t['border']}; border-radius: 4px;"
            f"            margin-top: 14px; padding: 12px;"
            f"            background-color: {t['bg']}; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px;"
            f"                   padding: 0 6px; color: {t['fg_strong']};"
            f"                   font-weight: 600; }}"
        )

        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self._theme_combo = QComboBox()
        self._theme_combo.addItem("Dark", "dark")
        self._theme_combo.addItem("Light", "light")
        current_mode = current_theme_mode()
        for i in range(self._theme_combo.count()):
            if self._theme_combo.itemData(i) == current_mode:
                self._theme_combo.setCurrentIndex(i)
                break
        self._theme_combo.setToolTip(
            "Switch the viewer colour theme. Dark is the default and "
            "matches the rest of the tooling; Light is friendlier in "
            "bright rooms. The choice is remembered for the next launch."
        )
        self._fit_combo_width(self._theme_combo)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_combo_changed)

        label_widget = QLabel("Colour theme")
        label_widget.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")
        form.addRow(label_widget, self._theme_combo)

        # High-quality zoom (supersampling / SSAA). When on, the heatmap
        # canvas renders oversized and downsamples, so thin copper and
        # outlines stay visible when zoomed far out instead of breaking
        # up or vanishing. Costs GPU memory + fill while a viewer is open,
        # hence a toggle; defaults on. Applied to the live canvas right
        # here (the GL viewer already exists by the time this tab builds)
        # so the setting takes effect without waiting for the next launch.
        self._ssaa_check = QCheckBox("High-quality zoom (supersampling)")
        self._ssaa_check.setChecked(load_supersampling_enabled())
        self._ssaa_check.setToolTip(
            "Render the heatmap canvas oversized and downsample it so thin "
            "copper and outlines stay visible when zoomed far out, instead "
            "of breaking up or vanishing. Uses more GPU memory while a "
            "viewer window is open. The choice is remembered for the next "
            "launch."
        )
        self._ssaa_check.toggled.connect(self._on_ssaa_toggled)
        if self._gl_viewer is not None:
            self._gl_viewer.set_supersampling(self._ssaa_check.isChecked())
        ssaa_label = QLabel("High-quality zoom")
        ssaa_label.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")
        form.addRow(ssaa_label, self._ssaa_check)

        # Status / hint line. Starts with the static usage hint and is
        # overwritten by :meth:`_on_theme_combo_changed` to confirm a
        # toggle took effect and prompt the user to restart.
        self._theme_status_label = QLabel(
            f"<span style='color:{t['fg_hint']};'>"
            "Dark by default. Switching here saves the choice and updates "
            "the menubar and dialogs immediately; restart FYPA for the "
            "side panel and tables to repaint."
            "</span>"
        )
        self._theme_status_label.setWordWrap(True)
        form.addRow(QLabel(""), self._theme_status_label)
        return box

    def _on_ssaa_toggled(self, checked: bool) -> None:
        """Persist and apply the High-quality-zoom (supersampling) toggle.
        Takes effect on the live canvas immediately — no relaunch."""
        save_supersampling_enabled(bool(checked))
        if self._gl_viewer is not None:
            self._gl_viewer.set_supersampling(bool(checked))

    def _on_theme_combo_changed(self, _idx: int) -> None:
        """Handle the user picking a different theme from the combobox.

        Persists the choice and updates the QApplication-level palette
        and base stylesheet immediately. The heavy widget refresh (re-
        styling the side panel + rebuilding the non-heatmap tabs) is
        deferred to the next event-loop tick so we're not destroying
        our own QComboBox in the middle of its currentIndexChanged
        emission. The Heatmap tab's OpenGL viewer can't be rebuilt
        safely — its context tear-down kills the process — so we re-
        style the side-panel widgets in place instead and leave the
        canvas alone.
        """
        mode = self._theme_combo.currentData()
        if not isinstance(mode, str) or mode not in _THEME_PRESETS:
            return
        if mode == current_theme_mode():
            return
        save_theme_mode(mode)
        app = QApplication.instance()
        if app is not None:
            apply_app_theme(app, mode)
        label = getattr(self, "_theme_status_label", None)
        if label is not None:
            t = _T()
            label.setText(
                f"<span style='color:{t['accent']};'>"
                f"Theme set to <b>{_esc(mode.capitalize())}</b>. "
                "Refreshing widgets…</span>"
            )
        QTimer.singleShot(0, self._refresh_inline_theme)

    @staticmethod
    def _fmt_settings_value(v) -> str:
        """Format a numeric setting for display in a QLineEdit. Uses %g
        to drop trailing zeros without losing precision for the long-
        tail values (1e-3, 0.00393, etc.)."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return ""
        if f == 0.0:
            return "0"
        # %g picks fixed or scientific automatically; clamp to 6 sig figs.
        return f"{f:.6g}"

    _parse_settings_value = staticmethod(_parse_numeric_text)

    def _on_settings_reset(self) -> None:
        """Restore every Settings-tab field to its built-in default. The
        user still has to press Re-run Solver to commit."""
        from fypa.altium.loader import SolveSettings as _SolveSettings
        defaults = _SolveSettings()
        for key, *_ in self._SETTINGS_FIELDS:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is not None:
                edit.setText(self._fmt_settings_value(getattr(defaults, key)))
        chk = getattr(self, "_settings_adaptive_check", None)
        if chk is not None:
            chk.setChecked(bool(defaults.adaptive_mesh))
        et = getattr(self, "_settings_electrothermal_check", None)
        if et is not None:
            et.setChecked(bool(defaults.electrothermal))
            self._apply_electrothermal_enabled()
        aw = getattr(self, "_settings_area_weighted_check", None)
        if aw is not None:
            aw.setChecked(bool(defaults.area_weighted_pin_coupling))
        mode_combo = getattr(self, "_fill_mode_combo", None)
        if mode_combo is not None:
            idx = mode_combo.findData(defaults.conductive_fill_mode)
            if idx >= 0:
                mode_combo.setCurrentIndex(idx)
            self._apply_fill_mode_enabled()
        display_defaults = {
            "via_current_warn_a": _VIA_CURRENT_WARN_A,
            "display_percentile_high": _DISPLAY_PERCENTILE_HIGH,
        }
        for key, *_ in self._SETTINGS_DISPLAY_FIELDS:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is not None:
                edit.setText(self._fmt_settings_value(display_defaults[key]))
        # Restore stackup copper-thickness fields to metadata defaults.
        for (edit, original_mm) in getattr(
                self, "_stackup_thickness_edits", {}).values():
            edit.setText(self._fmt_settings_value(original_mm * 1000.0))
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Fields reset — press "
            "<b>Re-run Solver</b> to commit.</span>"
        )

    @staticmethod
    def _mark_field_dirty(widget, dirty: bool) -> None:
        """Set / clear the ``dirty`` dynamic property on a Settings-tab
        widget and re-polish so the parent stylesheet's
        ``[dirty="true"]`` selector kicks in (red outline / text)."""
        new_val = "true" if dirty else "false"
        if widget.property("dirty") == new_val:
            return
        widget.setProperty("dirty", new_val)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _update_settings_field_styles(self) -> bool:
        """Walk every Settings-tab field, mark each one's ``dirty``
        property True when it differs from the current solve baseline,
        False otherwise. Bad input counts as dirty so the red outline
        stays up while the user is mid-edit. Returns True iff any
        field is dirty."""
        any_dirty = False
        mark = self._mark_field_dirty

        for key, *_ in self._SETTINGS_FIELDS:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is None:
                continue
            baseline = getattr(self._solve_settings, key, None)
            if baseline is None:
                mark(edit, False)
                continue
            try:
                val = self._parse_settings_value(edit.text())
                dirty = abs(val - float(baseline)) > 1e-12
            except ValueError:
                dirty = True
            mark(edit, dirty)
            any_dirty = any_dirty or dirty

        chk = getattr(self, "_settings_adaptive_check", None)
        if chk is not None:
            dirty = bool(chk.isChecked()) != bool(
                getattr(self._solve_settings, "adaptive_mesh", False))
            mark(chk, dirty)
            any_dirty = any_dirty or dirty

        aw = getattr(self, "_settings_area_weighted_check", None)
        if aw is not None:
            dirty = bool(aw.isChecked()) != bool(
                getattr(
                    self._solve_settings, "area_weighted_pin_coupling", False))
            mark(aw, dirty)
            any_dirty = any_dirty or dirty

        et = getattr(self, "_settings_electrothermal_check", None)
        if et is not None:
            dirty = bool(et.isChecked()) != bool(
                getattr(self._solve_settings, "electrothermal", False))
            mark(et, dirty)
            any_dirty = any_dirty or dirty

        mode_combo = getattr(self, "_fill_mode_combo", None)
        if mode_combo is not None:
            dirty = mode_combo.currentData() != getattr(
                self._solve_settings, "conductive_fill_mode", "auto")
            mark(mode_combo, dirty)
            any_dirty = any_dirty or dirty

        for key, baseline in (
            ("via_current_warn_a", self._via_current_warn_a),
            ("display_percentile_high", self._display_percentile_high),
        ):
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is None:
                continue
            try:
                val = self._parse_settings_value(edit.text())
                dirty = abs(val - float(baseline)) > 1e-12
            except ValueError:
                dirty = True
            mark(edit, dirty)
            any_dirty = any_dirty or dirty

        for _lid, (edit, original_mm) in getattr(
                self, "_stackup_thickness_edits", {}).items():
            text = edit.text().strip()
            if not text:
                mark(edit, False)
                continue
            try:
                new_um = self._parse_settings_value(text)
                dirty = abs(new_um / 1000.0 - original_mm) > 1.0e-6
            except ValueError:
                dirty = True
            mark(edit, dirty)
            any_dirty = any_dirty or dirty

        return any_dirty

    def _on_electrothermal_toggled(self, _checked: bool) -> None:
        """Grey the heat-transfer field when the coupled solve is off — it
        has no effect there, and a live-looking field that does nothing is
        worse than a disabled one."""
        self._apply_electrothermal_enabled()

    def _apply_electrothermal_enabled(self) -> None:
        chk = getattr(self, "_settings_electrothermal_check", None)
        edit = getattr(self, "settings_edit_heat_transfer_w_per_m2k", None)
        if edit is None:
            return
        on = bool(chk.isChecked()) if chk is not None else False
        edit.setEnabled(on)
        edit.setToolTip(
            "How readily the board sheds heat to ambient, both faces "
            "combined. Lower = hotter copper = larger IR drop."
            if on else
            "Enabled by 'Coupled electro-thermal solve (self-heating)'."
        )

    def _on_settings_field_changed(self, *_args) -> None:
        """Slot wired to every Settings-tab editor's change signal:
        refresh per-field dirty outlines and update ``_settings_dirty``
        against the current solve baseline, then refresh the Resolve
        overlay button.

        In launcher mode there is no committed solve to be stale against and
        no editor overlay, so only the per-field dirty outlines are updated."""
        new_dirty = self._update_settings_field_styles()
        if getattr(self, "_settings_launcher_mode", False):
            return
        if new_dirty == self._settings_dirty:
            return
        self._settings_dirty = new_dirty
        self._refresh_solve_stale_overlay()
