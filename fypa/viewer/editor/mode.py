"""Editor mode: entering / leaving it and its viewport buttons."""
from __future__ import annotations

import logging
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.assets import _load_editmode_icon, _triangle_icon
from fypa.viewer.prefs import save_adaptive_regulator_gain
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _ClickAbsorbingPanel


class _EditorModeMixin:
    """Editor mode: entering / leaving it and its viewport buttons."""

    # --- Editor mode ---------------------------------------------------------
    #
    # Editor mode lets the user place PDN sources / sinks directly on the
    # board (on a real component, or as a free marker on copper) without
    # editing the Altium schematic. Edits accumulate in the .fypa project
    # file; the resolve button re-runs the FEM with them applied.

    # Shared QSS for the square icon buttons pinned on the viewport.
    _EDITOR_OVERLAY_BTN_QSS = (
        "QToolButton { border: 1px solid %(border)s; border-radius: 6px;"
        "  background-color: %(bg)s; }"
        "QToolButton:hover { background-color: %(hover)s; }"
        "QToolButton:checked { background-color: %(accent)s;"
        "  border-color: %(accent)s; }"
    )

    def _build_editor_overlay_buttons(self) -> None:
        """Create the editor-mode toggle, the source / sink free-marker
        buttons, and the resolve button as overlay children of the GL
        viewer, pinned top-left by :meth:`_position_editor_overlays`. The
        source / sink buttons are shown only while editor mode is active.
        Per-widget colours come from :meth:`_restyle_editor_overlay_buttons`
        at the end so build-time and live-theme-toggle styling share one
        source of truth."""
        # Click-absorbing backdrop behind the edit toggle + free-marker
        # buttons. Created first so it sits below them in the GL viewer's
        # child stacking order. It extends a few px past the buttons so a
        # click that lands just outside a button's hit area is swallowed
        # here instead of falling through to the viewport and selecting /
        # deselecting copper. Shown only in editor mode (see
        # _position_editor_overlays).
        bg = _ClickAbsorbingPanel(
            self._gl_viewer, enable_left_edge_resize=False)
        bg.hide()
        self._editor_overlay_bg = bg

        btn = QToolButton(self._gl_viewer)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        icon = _load_editmode_icon()
        if icon is not None:
            btn.setIcon(icon)
            btn.setIconSize(QSize(20, 20))
        else:
            btn.setText("Edit")
        btn.setToolTip("Toggle editor mode (E) — place PDN sources / sinks")
        btn.setFixedSize(34, 34)
        btn.toggled.connect(self._on_editor_mode_toggled)
        self._editor_toggle_btn = btn

        # Free-marker palette — red up-triangle SOURCE / blue down-triangle
        # SINK, matching the solved-directive markers (_ROLE_MARKER_STYLE).
        # Hidden until editor mode is entered. SERIES has no free-marker
        # button: a free marker has a single anchor point, but a SERIES
        # element bridges two separate copper points, so SERIES is
        # component-bound only — assign it by selecting the part.
        # Tooltips come from _MARKER_TIPS so the build-time text and the
        # live text _sync_marker_buttons re-applies (enabled / disabled)
        # can't drift apart — notably over the S / L hotkey hints.
        for role, attr, up in (
            ("SOURCE", "_editor_add_source_btn", True),
            ("SINK", "_editor_add_sink_btn", False),
        ):
            style = self._ROLE_MARKER_STYLE[role]
            mbtn = QToolButton(self._gl_viewer)
            mbtn.setCheckable(True)
            mbtn.setCursor(Qt.PointingHandCursor)
            mbtn.setIcon(_triangle_icon(style["color"], up=up))
            mbtn.setIconSize(QSize(18, 18))
            mbtn.setFixedSize(34, 34)
            mbtn.setToolTip(self._MARKER_TIPS[role])
            mbtn.clicked.connect(
                lambda _checked=False, r=role: self._on_editor_add_marker(r)
            )
            mbtn.hide()
            setattr(self, attr, mbtn)

        # "Adaptive SMPS gain" lives in Settings > Solve parameters (built in
        # _build_settings_tab), not as a canvas overlay. Its attribute name and
        # handler are unchanged, so the solve-time reads and the resolve-enable
        # logic keep working regardless of where the widget is.

        rbtn = QPushButton("↻  Resolve", self._gl_viewer)
        rbtn.setCursor(Qt.PointingHandCursor)
        rbtn.setToolTip(
            "Re-run the solver with the current editor changes applied"
        )
        rbtn.clicked.connect(self._on_resolve_clicked)
        rbtn.hide()
        self._resolve_btn = rbtn
        self._restyle_editor_overlay_buttons()
        self._position_editor_overlays()

    def _restyle_editor_overlay_buttons(self) -> None:
        """Re-apply the active theme to the editor overlay buttons. Shared
        by :meth:`_build_editor_overlay_buttons` (build time) and
        :meth:`_refresh_inline_theme` (live theme toggle); safe before the
        widgets exist (each is guarded)."""
        t = _T()
        qss = self._EDITOR_OVERLAY_BTN_QSS % {
            "border": t["border"], "bg": t["bg"],
            "hover": t["bg_hover"], "accent": t["accent"],
        }
        bg = getattr(self, "_editor_overlay_bg", None)
        if bg is not None:
            bg.setStyleSheet(
                f"background-color: {t['bg']};"
                f" border: 1px solid {t['border']};"
                f" border-radius: 8px;"
            )
        for attr in ("_editor_toggle_btn", "_editor_add_source_btn",
                     "_editor_add_sink_btn"):
            b = getattr(self, attr, None)
            if b is not None:
                b.setStyleSheet(qss)
        rbtn = getattr(self, "_resolve_btn", None)
        if rbtn is not None:
            # Border + text use a fixed "go" green (not a theme token) so the
            # Resolve action reads as a positive call-to-action in both themes.
            rbtn.setStyleSheet(
                "QPushButton {{ border: 1px solid {green}; border-radius: 6px;"
                "  padding: 6px 12px; font-weight: bold;"
                "  background-color: {bg}; color: {green}; }}"
                "QPushButton:hover:enabled {{ background-color: {hover}; }}"
                "QPushButton:disabled {{ color: {dim}; border-color: {dim}; }}"
                .format(green="#2ca92c", bg=t["bg"], hover=t["bg_hover"],
                        dim=t["fg_dim"])
            )
        ag_chk = getattr(self, "_adaptive_gain_check", None)
        if ag_chk is not None:
            ag_chk.setStyleSheet(f"QCheckBox {{ color: {t['fg']}; }}")

    def _solve_overlay_visible(self) -> bool:
        if isinstance(getattr(self, "metadata", None), dict):
            if self.metadata.get("prjpcb_path"):
                return True
        if getattr(self, "_loaded_project", None) is not None:
            return True
        return False

    def _resolve_button_enabled(self) -> bool:
        chk = getattr(self, "_adaptive_gain_check", None)
        adaptive_on = (
            chk is not None and chk.isEnabled() and chk.isChecked()
        )
        wanted = bool(
            self._awaiting_first_solve or self._solve_stale or adaptive_on
        )
        # A pending / stale solve is only actionable if the solver would
        # accept it — otherwise the click just lands on the "Project is not
        # solveable" dialog, so keep the button greyed out with a tooltip
        # that says what's missing instead.
        return wanted and self._has_solveable_directives()

    _NOTHING_TO_SOLVE_TIP = (
        "Nothing to solve yet — the design needs a SOURCE and a SINK on the "
        "same rail. Switch on Edit to place them, then Solve."
    )

    def _has_solveable_directives(self) -> bool:
        """True when the schematic + editor directives form at least one
        closed rail (a source and a sink that share connected nets) — the
        same test :attr:`LoadedProject.is_solveable` applies once the editor
        directives are merged in. Answers ``True`` when there is no in-memory
        design to inspect (a viewer opened from a bare pickle) or the check
        itself fails, so the button never gets stuck disabled for a solve
        the worker could actually run."""
        loaded = getattr(self, "_loaded_project", None)
        if loaded is None:
            return True
        project = getattr(self, "_project", None)
        editor_directives = (
            list(project.editor_directives) if project is not None else []
        )

        def _named_copper_at(ed):
            # Free marker on copper the user has since named via a
            # CopperName rename — mirror _unnamed_copper_directives()'s
            # promotion, read-only.
            if (ed.kind != "free" or ed.anchor_xy is None
                    or ed.layer_id is None):
                return None
            c = self._copper_name_at(
                float(ed.anchor_xy[0]), float(ed.anchor_xy[1]),
                int(ed.layer_id),
            )
            return c.name if c is not None else None

        try:
            from fypa.editor_directives import has_closed_pdn_loop
            return has_closed_pdn_loop(
                loaded, editor_directives, p_net_resolver=_named_copper_at,
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "closed-loop check failed; enabling Solve", exc_info=True,
            )
            return True

    def _on_adaptive_gain_toggled(self, checked: bool) -> None:
        save_adaptive_regulator_gain(checked)
        self._refresh_solve_stale_overlay()

    def _position_editor_overlays(self) -> None:
        """Pin the editor toggle, source / sink, and resolve buttons in a
        row along the GL viewer's top-left corner. Safe before the widgets
        exist (no-op); wired to every GL-viewer resize via
        :meth:`eventFilter`."""
        gl = getattr(self, "_gl_viewer", None)
        btn = getattr(self, "_editor_toggle_btn", None)
        if gl is None or btn is None:
            return
        margin, gap = 12, 6
        x = margin
        btn.move(x, margin)
        btn.raise_()
        x += btn.width() + gap
        for attr in ("_editor_add_source_btn", "_editor_add_sink_btn"):
            b = getattr(self, attr, None)
            if b is not None and not b.isHidden():
                b.move(x, margin)
                b.raise_()
                x += b.width() + gap
        rbtn = getattr(self, "_resolve_btn", None)
        if rbtn is not None and rbtn.isVisible():
            rbtn.adjustSize()
            rbtn.move(x + 2, margin)
            rbtn.raise_()

        # Size the click-absorbing backdrop to wrap the edit toggle + the two
        # free-marker buttons (not the Resolve button, which sits apart), with
        # a few px of slop so a near-miss click is caught. Only shown in
        # editor mode, when the free-marker buttons are visible.
        bg = getattr(self, "_editor_overlay_bg", None)
        if bg is not None:
            rects = [
                b.geometry()
                for b in (btn,
                          getattr(self, "_editor_add_source_btn", None),
                          getattr(self, "_editor_add_sink_btn", None))
                if b is not None and not b.isHidden()
            ]
            if getattr(self, "_editor_mode", False) and rects:
                union = rects[0]
                for r in rects[1:]:
                    union = union.united(r)
                pad = 6
                bg.setGeometry(union.adjusted(-pad, -pad, pad, pad))
                bg.lower()   # keep it behind the buttons it wraps
                bg.show()
            else:
                bg.hide()

    def _apply_sidebar_scroll_theme(self) -> None:
        """Pin the side panel's scroll-area background to the active theme
        (also re-run on a theme switch by :meth:`_refresh_inline_theme`)."""
        self._sidebar_scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {_T()['bg']}; }}"
        )

    def _build_editor_panel(self) -> QWidget:
        """Right-hand panel — multi-purpose. In editor mode it hosts the
        PDN-role form (filled by :meth:`_populate_editor_form` on
        component / free-marker selection). In viewer mode it hosts the
        copper-properties form (filled by
        :meth:`_populate_copper_props_form` when the user clicks a
        copper primitive). The two contents live in sibling host
        widgets, swapped by :meth:`_show_pdn_editor_layout` /
        :meth:`_show_copper_props_layout`."""
        panel = _ClickAbsorbingPanel()
        panel.setObjectName("EditorSidePanel")
        panel.setFixedWidth(self._editor_panel_width)
        # User can drag the panel's left edge to resize it; the owner
        # clamps the proposed width against the live viewport size.
        panel.leftEdgeResized.connect(self._on_editor_panel_resized)
        self._editor_panel_widget = panel
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Title is swappable so the same widget can read as "PDN Editor"
        # in editor mode and "Copper Properties" in viewer mode.
        self._editor_panel_title = QLabel("<b>PDN Editor</b>")
        lay.addWidget(self._editor_panel_title)

        self._editor_hint = QLabel(self._EDITOR_DEFAULT_HINT)
        self._editor_hint.setWordWrap(True)
        lay.addWidget(self._editor_hint)

        # Net-focus release chip — visible for as long as a net focus is
        # held, whatever else the panel is showing. The net table (which owns
        # the crosshair that sets focus) is the panel's idle view and hides on
        # any selection, so without this the only way back to all-copper would
        # be to clear the selection first. See :meth:`_update_editor_focus_chip`.
        self._editor_focus_chip = QLabel("")
        self._editor_focus_chip.setWordWrap(True)
        self._editor_focus_chip.setTextFormat(Qt.RichText)
        self._editor_focus_chip.setOpenExternalLinks(False)
        self._editor_focus_chip.linkActivated.connect(
            self._on_editor_focus_chip_link)
        self._editor_focus_chip.hide()
        lay.addWidget(self._editor_focus_chip)

        # Attached-PDN summary — the sources / sinks / series elements that
        # couple into the selected copper's rail group, filled by
        # :meth:`_update_editor_panel` on a named-copper selection and
        # hidden for every other selection. Element names are links that
        # select the owning component / free marker.
        self._editor_net_summary = QLabel("")
        self._editor_net_summary.setWordWrap(True)
        self._editor_net_summary.setTextFormat(Qt.RichText)
        self._editor_net_summary.setOpenExternalLinks(False)
        self._editor_net_summary.linkActivated.connect(
            self._on_net_summary_link)
        self._editor_net_summary.hide()
        lay.addWidget(self._editor_net_summary)

        # Form host — (re)populated by _populate_editor_form on selection.
        self._editor_form_host = QWidget()
        self._editor_form_layout = QVBoxLayout(self._editor_form_host)
        self._editor_form_layout.setContentsMargins(0, 0, 0, 0)
        self._editor_form_layout.setSpacing(6)
        self._editor_form_host.hide()
        lay.addWidget(self._editor_form_host)

        # Sibling host for the marquee multi-selection form (several PDN
        # markers selected at once), built by
        # :meth:`_populate_multi_editor_form`.
        self._multi_form_host = QWidget()
        self._multi_form_layout = QVBoxLayout(self._multi_form_host)
        self._multi_form_layout.setContentsMargins(0, 0, 0, 0)
        self._multi_form_layout.setSpacing(6)
        self._multi_form_host.hide()
        lay.addWidget(self._multi_form_host)

        # Sibling host for the viewer-mode copper-properties form.
        self._copper_props_host = QWidget()
        self._copper_props_layout = QVBoxLayout(self._copper_props_host)
        self._copper_props_layout.setContentsMargins(0, 0, 0, 0)
        self._copper_props_layout.setSpacing(6)
        self._copper_props_host.hide()
        lay.addWidget(self._copper_props_host)

        # Net inventory table (editor mode only) — every net on the board
        # with its total copper area, ordered by area descending. Clicking
        # a row lights up that net's copper in the viewport; the crosshair
        # column shows only that net (see :meth:`_set_editor_focus`). See
        # :meth:`_refresh_net_table`.
        self._net_table_label = QLabel("<b>Nets</b>")
        # The focus release link is appended to this line while a focus is
        # held (see :meth:`_update_net_table_label`).
        self._net_table_label.setTextFormat(Qt.RichText)
        self._net_table_label.setOpenExternalLinks(False)
        self._net_table_label.linkActivated.connect(
            self._on_editor_focus_chip_link)
        lay.addWidget(self._net_table_label)
        # Live name filter — hides non-matching rows as the user types
        # (case-insensitive substring). Inherits the panel's themed
        # QLineEdit styling. See :meth:`_apply_net_table_filter`.
        self._net_table_filter = QLineEdit(panel)
        self._net_table_filter.setObjectName("NetTableFilter")
        self._net_table_filter.setPlaceholderText("Filter nets by name…")
        self._net_table_filter.setClearButtonEnabled(True)
        self._net_table_filter.textChanged.connect(
            self._on_net_table_filter_changed)
        self._net_table_filter.hide()
        lay.addWidget(self._net_table_filter)
        self._net_table = QTableWidget(0, 3, panel)
        self._net_table.setObjectName("NetTable")
        self._net_table.setHorizontalHeaderLabels(
            ["", "Net", "Area (mm²)"])
        self._net_table.horizontalHeaderItem(
            self._NET_COL_FOCUS).setToolTip(
                "Focus — show only one net's copper at a time")
        self._net_table.verticalHeader().setVisible(False)
        self._net_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._net_table.setSelectionMode(QAbstractItemView.SingleSelection)
        # Click a column header to re-sort. _refresh_net_table disables this
        # while it writes cells (so rows don't reshuffle mid-populate) and
        # re-applies the user's chosen sort afterwards.
        self._net_table.setSortingEnabled(True)
        self._net_table.horizontalHeader().setSortIndicator(
            self._NET_COL_AREA, Qt.DescendingOrder)
        self._net_table.setAlternatingRowColors(True)
        hh = self._net_table.horizontalHeader()
        # The focus crosshair is a fixed-width icon gutter; the name column
        # takes the slack. A QTableWidgetItem icon (not a cell widget) is
        # what carries the glyph, because setCellWidget widgets do NOT move
        # with sortItems — they would stay put while the rows beneath them
        # re-ordered, leaving every crosshair on the wrong net.
        hh.setSectionResizeMode(
            self._NET_COL_FOCUS, QHeaderView.Fixed)
        self._net_table.setColumnWidth(self._NET_COL_FOCUS, 26)
        hh.setSectionResizeMode(self._NET_COL_NAME, QHeaderView.Stretch)
        hh.setSectionResizeMode(
            self._NET_COL_AREA, QHeaderView.ResizeToContents)
        # A manual header-click re-sort rearranges items but the hidden-row
        # state is tracked per row position, so re-apply the filter after a
        # sort to keep the right rows masked.
        hh.sortIndicatorChanged.connect(
            lambda *_: self._apply_net_table_filter())
        # Re-built each time the editor opens; populated rows here keep the
        # per-row {name, area, poly_keys, renameable} payload so a click /
        # rename doesn't have to recompute the connectivity index.
        self._net_table_rows: list[dict] = []
        # Set while _refresh_net_table writes cells so the itemChanged
        # rename handler ignores its own programmatic edits.
        self._net_table_populating: bool = False
        self._net_table.itemSelectionChanged.connect(
            self._on_net_table_selection)
        self._net_table.itemChanged.connect(self._on_net_table_item_changed)
        # The crosshair cells are enabled but not selectable, so a click on
        # one reaches cellClicked without disturbing the row selection (and
        # so without hiding the table the crosshair lives in).
        self._net_table.cellClicked.connect(self._on_net_table_cell_clicked)
        self._net_table_label.hide()
        self._net_table.hide()
        lay.addWidget(self._net_table, 1)

        lay.addStretch(0)
        self._apply_editor_panel_theme()
        return panel

    def _apply_editor_panel_theme(self) -> None:
        """(Re)pin the active theme's colours onto the right-hand panel's
        inline-styled widgets. Called once at build time and again from
        :meth:`_refresh_inline_theme` on a theme switch — the panel lives
        on the GL canvas, so it is restyled in place rather than rebuilt."""
        panel = getattr(self, "_editor_panel_widget", None)
        if panel is None:
            return
        t = _T()
        # QLineEdit children (copper-name, loc X / Y) need explicit theming
        # or they fall back to the platform default (white bg, black text,
        # near-black placeholder) which is unreadable against the dark
        # panel background. The #id selector keeps the bg rule from
        # cascading to every descendant.
        panel.setStyleSheet(
            f"QWidget#EditorSidePanel {{ background-color: {t['bg']}; }}"
            f"QLineEdit {{ background-color: {t['bg_input']};"
            f"            color: {t['fg']};"
            f"            border: 1px solid {t['border']};"
            f"            padding: 2px 4px;"
            f"            selection-background-color: {t['bg_selection']}; }}"
            f"QLineEdit:focus {{ border: 1px solid {t['accent']}; }}"
            f"QLineEdit {{ placeholder-text-color: {t['fg_hint']}; }}"
        )
        self._editor_hint.setStyleSheet(f"color: {t['fg_muted']};")
        self._editor_focus_chip.setStyleSheet(
            f"QLabel {{ border: 1px solid {t['accent']}; border-radius: 4px;"
            f" padding: 4px 6px; background-color: {t['bg_alt']};"
            f" color: {t['fg']}; }}"
        )
        self._editor_net_summary.setStyleSheet(
            f"QLabel {{ border: 1px solid {t['border']}; border-radius: 4px;"
            f" padding: 6px; background-color: {t['bg_alt']};"
            f" color: {t['fg']}; }}"
        )
        # alternate-background-color is pinned here too: left to the app
        # palette it can disagree with the inline bg on a theme switch.
        self._net_table.setStyleSheet(
            f"QTableWidget#NetTable {{ background-color: {t['bg_input']};"
            f"            color: {t['fg']};"
            f"            alternate-background-color: {t['bg_alt']};"
            f"            gridline-color: {t['border']};"
            f"            border: 1px solid {t['border']}; }}"
            f"QTableWidget#NetTable::item:selected {{"
            f"            background-color: {t['bg_selection']};"
            f"            color: {t['fg']}; }}"
            f"QHeaderView::section {{ background-color: {t['bg']};"
            f"            color: {t['fg_muted']};"
            f"            border: 0px; border-bottom: 1px solid {t['border']};"
            f"            padding: 2px 4px; }}"
        )
        # Crosshair icons are cached per theme mode; repaint for the new one.
        self._sync_net_focus_column()

    def _show_pdn_editor_layout(self) -> None:
        """Switch the right panel to its editor-mode contents (title,
        hint, PDN-role form host). Visibility of the form host itself is
        still driven by :meth:`_update_editor_panel`."""
        if not hasattr(self, "_editor_panel_title"):
            return
        self._editor_panel_title.setText("<b>PDN Editor</b>")
        self._editor_hint.show()
        self._copper_props_host.hide()
        # _update_editor_panel decides whether _editor_form_host and the
        # net table are on (the table shows only with nothing selected).

    def _show_copper_props_layout(self) -> None:
        """Switch the right panel to its viewer-mode contents (title +
        copper-properties form host). The editor-mode form / hint hide."""
        if not hasattr(self, "_editor_panel_title"):
            return
        self._editor_panel_title.setText("<b>Copper Properties</b>")
        self._editor_hint.hide()
        self._editor_focus_chip.hide()
        self._editor_net_summary.hide()
        self._editor_form_host.hide()
        self._multi_form_host.hide()
        self._copper_props_host.show()
        if hasattr(self, "_net_table"):
            self._net_table_label.hide()
            self._net_table_filter.hide()
            self._net_table.hide()

    def _on_editor_mode_toggled(self, checked: bool) -> None:
        """Enter / leave editor mode — swaps the viewport look, shows /
        hides the right-hand panel, and clears transient selection state
        on the way out (placed directives persist in the project)."""
        self._editor_mode = bool(checked)
        self._gl_viewer.set_editor_mode(self._editor_mode)
        # The right-hand panel serves both modes; clear the other mode's
        # selection before swapping its contents.
        if self._editor_mode:
            # Entering editor mode: drop any viewer-mode copper selection
            # (its dashed outline and props form would conflict with the
            # PDN-editor form).
            self._clear_copper_selection()
            self._show_pdn_editor_layout()
        else:
            self._show_pdn_editor_layout()  # safe; visibility checked below
        self._editor_panel.setVisible(
            self._editor_mode or self._copper_selection is not None)
        # Always re-position so the legend inset gets cleared when the
        # panel becomes hidden (the position-helper is a no-op aside
        # from the inset when the panel is invisible).
        self._position_editor_panel()
        # Keep the toggle button in sync when entered via the E hotkey.
        if self._editor_toggle_btn.isChecked() != self._editor_mode:
            self._editor_toggle_btn.blockSignals(True)
            self._editor_toggle_btn.setChecked(self._editor_mode)
            self._editor_toggle_btn.blockSignals(False)
        if not self._editor_mode:
            self._editor_selection = None
            self._editor_multi = []
            self._editor_pending_marker = None
            self._marker_drag = None
            # Focus hides copper, and there is no control for it outside
            # editor mode — leaving must not strand the user with two thirds
            # of the board missing from the viewer.
            self._clear_editor_focus(render=False)
            self._clear_editor_highlight()
        else:
            self._update_pending_rails()
            # Warm the connected-copper index now so the first selection
            # click doesn't pay the one-time STRtree / union-find build
            # cost — on big boards that's seconds of work the user would
            # otherwise see as a click-to-highlight delay.
            try:
                self._connected_components_data()
            except Exception:
                pass
            # Mint a stable ``$NET_xxx`` name for each electrically-isolated
            # region of un-netted copper so the net table has rows to show
            # (and the rest of the editor has real net names to work with).
            # That's every region on a Gerber import (no nets at all) and the
            # no-net ``"(none)"`` pours / fills on an Altium board. Cheap +
            # idempotent: only unnamed regions get named.
            try:
                self._auto_name_isolated_regions()
            except Exception:
                logging.getLogger(__name__).exception(
                    "auto-naming isolated copper regions failed")
            self._refresh_net_table()
        # Source / sink free-marker buttons live in the viewport overlay
        # and are visible only while editor mode is active.
        for attr in ("_editor_add_source_btn", "_editor_add_sink_btn"):
            b = getattr(self, attr, None)
            if b is not None:
                b.setVisible(self._editor_mode)
        self._update_editor_panel()
        self._position_editor_overlays()
        # Refresh so editor-directive markers appear / disappear with the
        # mode (the grid + background are handled by the GL viewer itself).
        self._render()

    def _hotkey_toggle_editor(self) -> None:
        self._editor_toggle_btn.toggle()

    def _hotkey_arm_source_marker(self) -> None:
        self._hotkey_arm_marker("SOURCE")

    def _hotkey_arm_sink_marker(self) -> None:
        self._hotkey_arm_marker("SINK")

    def _hotkey_arm_marker(self, role: str) -> None:
        """S / L equivalent of clicking the viewport SOURCE / SINK button:
        arm "drop a free <role> on the next copper click", or disarm it
        when that role is already armed.

        A no-op outside editor mode, where the buttons are hidden anyway.
        Inside it the work goes through :meth:`_on_editor_add_marker`, so
        the keyboard hits the same named-copper guard and the same button
        sync as the mouse — the pressed key lights the button up.
        """
        if not getattr(self, "_editor_mode", False):
            return
        self._on_editor_add_marker(role)

    _EDITOR_DEFAULT_HINT = (
        "Click a component or a copper region in the viewport to assign a "
        "PDN role, or use the red / blue triangle buttons at the top-left "
        "of the viewport to drop a free source / sink."
    )

    # Right-hand panel width bounds (px). The default matches the historic
    # fixed width; the user can drag the panel's left edge between the min
    # and "viewport minus this" cap (see :meth:`_on_editor_panel_resized`).
    _EDITOR_PANEL_DEFAULT_W = 300
    _EDITOR_PANEL_MIN_W = 200
    _EDITOR_PANEL_VIEWPORT_RESERVE = 120

    def _on_editor_panel_resized(self, proposed_w: int) -> None:
        """Apply a user-dragged right-panel width, clamped to the min
        width and to leaving a reserve strip of the viewport visible."""
        gl = getattr(self, "_gl_viewer", None)
        panel = getattr(self, "_editor_panel", None)
        if gl is None or panel is None:
            return
        max_w = max(
            self._EDITOR_PANEL_MIN_W,
            gl.width() - self._EDITOR_PANEL_VIEWPORT_RESERVE,
        )
        w = max(self._EDITOR_PANEL_MIN_W, min(proposed_w, max_w))
        if w != panel.width():
            self._editor_panel_width = w
            panel.setFixedWidth(w)
            self._position_editor_panel()

    def _is_copper_name_selection(self, sel: dict | None) -> bool:
        """Whether the right-hand panel should display the copper-name
        form for ``sel``. True when a copper selection's net is the
        unnamed sentinel ``"(none)"`` (first-time naming) OR when its
        click point already has a :class:`CopperName` rename pinned to
        the same polygon (so re-selecting renamed copper still surfaces
        the form, with the user-entered name retained)."""
        if (not sel or sel.get("kind") != "copper"
                or sel.get("anchor_xy") is None
                or sel.get("layer_id") is None):
            return False
        if sel.get("net") == "(none)":
            return True
        ax, ay = sel["anchor_xy"]
        return self._copper_name_at(float(ax), float(ay),
                                    int(sel["layer_id"])) is not None
