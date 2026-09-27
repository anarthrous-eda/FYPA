"""Builds the viewer's widget tree: sidebar, tab pages and the heatmap canvas."""
from __future__ import annotations

import time
from fypa.gl_mesh_viewer import GLMeshViewer
from fypa.spacemouse_nav import SpaceMouseController
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.display import _build_cmap_lut, _MODES
from fypa.viewer.scale_controls import ScaleController
from fypa.viewer.theme import _T
from fypa.viewer.widgets import (
    EyeButton,
    FillToggleButton,
    OutlineToggleButton,
    SidebarToggleButton,
    TransparencyButton,
)


class _UiBuildMixin:
    """Builds the viewer's widget tree: sidebar, tab pages and the heatmap canvas."""

    # --- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        # Top-level: a menubar over a tab widget. Tab 0 is the heatmap view
        # (everything that used to be the whole window); Tab 1 is the Setup
        # tab populated from the metadata dict so users can verify what the
        # FEM was given.
        self._build_menubar()
        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)

        heatmap_tab = QWidget(self.tabs)
        self._heatmap_tab_index = self.tabs.addTab(heatmap_tab, "Heatmap")

        central = heatmap_tab
        outer = QHBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)

        # Side panel.
        side = QVBoxLayout()
        side.setSpacing(6)

        side.addWidget(QLabel("<b>Physical layers</b>"))
        # Altium-style layer list. Each row has a clickable eye icon (open
        # = visible, slashed grey = hidden), a colour swatch, and the layer
        # name. The first row is an "All Layers" toggle that mirrors
        # Altium's behaviour: clicking its eye shows or hides every layer.
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.layer_list.setFocusPolicy(Qt.NoFocus)
        _t = _T()
        self.layer_list.setStyleSheet(
            f"QListWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"              border: 1px solid {_t['border']}; padding: 2px;"
            f"              alternate-background-color: {_t['bg_alt']}; }}"
            f"QListWidget::item:hover {{ background-color: {_t['bg_hover']}; }}"
        )
        self.layer_list.setAlternatingRowColors(True)

        # Each physical-layer row carries four controls: the primary eye
        # (layer in the heatmap), a second eye that shows ALL the copper on
        # that layer — every net, not just the analysed rails — a
        # transparency cycler and a wire-mesh / solid fill toggle for that
        # all-copper view.
        self._layer_eye_buttons: list[tuple[str, EyeButton]] = []
        self._layer_eye2_buttons: list[tuple[str, EyeButton]] = []
        self._layer_fill_buttons: list[tuple[str, FillToggleButton]] = []
        self._layer_transparency_buttons: list[
            tuple[str, TransparencyButton]] = []
        # phys-name → QListWidgetItem, used by the selected-layer click
        # handler and the row-background highlight.
        self._layer_list_items: dict[str, QListWidgetItem] = {}
        self._selected_layer: str | None = None
        # What the last Shift+click isolated, so a second Shift+click on the
        # same item inverts. "Is it the only thing visible?" cannot stand in
        # for this: the default rail visibility already leaves exactly one rail
        # showing, and narrowing to one layer by ordinary clicks is routine —
        # in both cases the FIRST Shift+click would invert instead of isolate.
        self._isolated_key: object | None = None

        self._all_layers_eye = EyeButton(
            visible=True,
            tip_show=(
                "Show the analysed rails on every layer.\n"
                "This first column controls rail copper only (the PDN "
                "heatmap) — use the second eye to show all copper."
            ),
            tip_hide=(
                "Hide the analysed rails on every layer.\n"
                "This first column controls rail copper only (the PDN "
                "heatmap) — use the second eye to show all copper."
            ),
        )
        self._all_layers_eye2 = EyeButton(
            visible=False,
            tip_show="Show all copper on every layer",
            tip_hide="Hide all copper on every layer",
        )
        self._all_layers_fill = FillToggleButton(solid=True)
        self._all_layers_transparency = TransparencyButton(step=0)
        # Layer-outline toggle — only meaningful for the visible rails'
        # copper (that's what the outline pass traces), so it lives on the
        # "All Rails" row below. Replaces the old "Show layer outlines (O)"
        # side-panel checkbox.
        self._outlines_btn = OutlineToggleButton(on=True)
        all_row = self._build_layer_row_widget(
            self._all_layers_eye, swatch_color=None,
            label_text="All Layers", bold=True,
            second_eye=self._all_layers_eye2, fill_btn=self._all_layers_fill,
            transparency_btn=self._all_layers_transparency,
        )
        all_item = QListWidgetItem()
        all_item.setFlags(Qt.ItemIsEnabled)
        self.layer_list.addItem(all_item)
        all_item.setSizeHint(all_row.sizeHint())
        self.layer_list.setItemWidget(all_item, all_row)
        self._all_layers_eye.toggled_visible.connect(self._on_all_layers_toggled)
        self._all_layers_eye2.toggled_visible.connect(
            self._on_all_layers_eye2_toggled)
        self._all_layers_fill.toggled_fill.connect(
            self._on_all_layers_fill_toggled)
        self._all_layers_transparency.toggled_transparency.connect(
            self._on_all_layers_transparency_toggled)
        self._outlines_btn.toggled_outline.connect(self._render)

        for phys in self._physicals:
            self._add_layer_row(phys)

        # Clicking a layer row (anywhere outside its eye/fill/transparency
        # buttons — those consume the click before itemClicked fires)
        # toggles that layer as the selected layer; clicking the already-
        # selected layer clears the selection back to none.
        self.layer_list.itemClicked.connect(self._on_layer_item_clicked)

        self._sync_all_layers_eye()
        self._ensure_default_copper_visibility()
        self._sync_all_layers_eye2()

        # Size the list to show every physical layer (plus the "All Layers"
        # header). When the full side panel ends up too tall for the window,
        # the outer side QScrollArea below scrolls the whole panel.
        approx_row_h = self.layer_list.sizeHintForRow(0) or 22
        self.layer_list.setFixedHeight(
            (len(self._physicals) + 1) * approx_row_h + 6
        )
        side.addWidget(self.layer_list)

        side.addSpacing(8)
        side.addWidget(QLabel("<b>Rails</b>"))
        # Altium-style rail list — mirrors the physical-layer control above
        # so users can show any combination of rails at once. The first row
        # is an "All Rails" toggle that mirrors the same UX as "All Layers".
        # Rails are the PRIMARY names of bridge groups (e.g. "+3V3"), not
        # raw net names: ticking "+3V3" displays both +3V3 and any net
        # bridged to it (e.g. 3V3_SW via L2's RESISTOR directive).
        self.rail_list = QListWidget()
        self.rail_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.rail_list.setFocusPolicy(Qt.NoFocus)
        self.rail_list.setStyleSheet(
            f"QListWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"              border: 1px solid {_t['border']}; padding: 2px;"
            f"              alternate-background-color: {_t['bg_alt']}; }}"
            f"QListWidget::item:hover {{ background-color: {_t['bg_hover']}; }}"
        )
        self.rail_list.setAlternatingRowColors(True)
        self.rail_list.setToolTip(
            "PDN rails — tick one or more to show their copper. Each rail "
            "groups together nets bridged by a RESISTOR directive (e.g. "
            "selecting '+3V3' shows both +3V3 and 3V3_SW copper if L2 "
            "bridges them). Expand a rail (▶) to toggle individual subnet "
            "nets. 'All Rails' toggles every rail at once."
        )

        self._init_rail_list_state()

        self._all_rails_eye = EyeButton(visible=False)
        all_rails_row = self._build_layer_row_widget(
            self._all_rails_eye, swatch_color=None,
            label_text="All Rails", bold=True,
            outline_btn=self._outlines_btn,
        )
        all_rails_item = QListWidgetItem()
        all_rails_item.setFlags(Qt.ItemIsEnabled)
        self.rail_list.addItem(all_rails_item)
        all_rails_item.setSizeHint(all_rails_row.sizeHint())
        self.rail_list.setItemWidget(all_rails_item, all_rails_row)
        self._all_rails_eye.toggled_visible.connect(self._on_all_rails_toggled)

        self._populate_rail_list()
        side.addWidget(self.rail_list)

        side.addSpacing(8)
        self._mode_label = QLabel("<b>Mode</b>")
        side.addWidget(self._mode_label)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([m[0] for m in _MODES])
        side.addWidget(self.mode_combo)
        # No rails → no PDN data, so the metric picker is meaningless.
        # Hide it alongside the colour-scale controls (see below).
        if not self._rails:
            self._mode_label.setVisible(False)
            self.mode_combo.setVisible(False)

        side.addSpacing(8)
        side.addWidget(QLabel("<b>Board Features</b>"))
        # Non-copper display layers — silkscreen, pads, vias, components and
        # designators. Same QListWidget-of-rows control as the Physical
        # layers / Rails lists above, but each row carries two eyes (visible
        # on the selected rails only vs. visible everywhere — a mutually
        # exclusive pair) before the label, a wire-mesh / solid fill toggle
        # at the far right, and — for the layers that exist on both board
        # sides — a split toggle that breaks the row into Top / Bottom rows.
        self.overlay_list = QListWidget()
        self.overlay_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.overlay_list.setFocusPolicy(Qt.NoFocus)
        self.overlay_list.setStyleSheet(
            f"QListWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"              border: 1px solid {_t['border']}; padding: 2px;"
            f"              alternate-background-color: {_t['bg_alt']}; }}"
            f"QListWidget::item:hover {{ background-color: {_t['bg_hover']}; }}"
        )
        self.overlay_list.setAlternatingRowColors(True)
        self.overlay_list.setToolTip(
            "Non-copper overlays. Each row has two eyes — show on the "
            "selected rails only (left) or show everywhere (right) — a "
            "colour swatch that opens a picker for the overlay's colour, "
            "and a wire-mesh / solid fill toggle on the right. Overlay, "
            "pads, components and designators can be split into separate "
            "Top / Bottom rows with the split button."
        )
        self._init_overlay_state()
        self._build_overlay_list()
        side.addWidget(self.overlay_list)

        side.addSpacing(8)
        # Colour-scale controller: colour scheme + linear/log pickers and
        # Min/Max text-box entry. Editing the range clamps the heatmap
        # colours interactively without re-rasterising the mesh. Sits
        # directly under the Mode combo so the scale tracks the metric.
        # Its gradient strip is not shown here — it is reparented onto the
        # GL viewer as a bottom-left overlay (see below).
        self.scale_controller = ScaleController()
        self.scale_controller.rangeChanged.connect(self._on_scale_range_changed)
        self.scale_controller.colormapChanged.connect(self._on_colormap_changed)
        self.scale_controller.scaleTypeChanged.connect(self._on_scale_type_changed)
        side.addWidget(self.scale_controller, 0)
        # No rails → no PDN analysis to colour-map, so hide the side-panel
        # controls (Scheme / Scale / Min / Max). The bottom-left gradient
        # strip is hidden alongside it where the overlay is set up below.
        if not self._rails:
            self.scale_controller.setVisible(False)

        side.addSpacing(8)

        # The 2D/3D view, via-heatmap, no-current-copper-colour and
        # FEM-mesh-overlay toggles moved to the View menu (see
        # _build_view_menu). Their QCheckBox objects are kept as the
        # canonical state holders — so every isChecked() / setChecked() /
        # toggle() call site and the existing toggled-signal wiring stay
        # unchanged — but they are never added to a layout, so they remain
        # hidden. The View menu actions drive them and mirror their state
        # in the menu item labels via _refresh_view_menu_labels.
        self.show_mesh_box = QCheckBox("Show copper mesh")
        self.show_mesh_box.setChecked(False)
        self.colour_stubs_box = QCheckBox("Grey no current copper")
        self.colour_stubs_box.setChecked(False)
        self.view_3d_box = QCheckBox("3D view (3)")
        self.view_3d_box.setChecked(False)
        self.heatmap_vias_box = QCheckBox("Heatmap vias/PTH (V)")
        self.heatmap_vias_box.setChecked(False)

        self.rail_only_box = QCheckBox("Show only rail net (R)")
        self.rail_only_box.setToolTip(
            "When on, only the copper of each selected rail's own primary "
            "net is shown — any nets joined to it via SERIES bridges (e.g. "
            "3V3_SW bridged to +3V3 by L2) are hidden. Markers, the probe, "
            "and the net-name lookup all follow the same filter."
        )
        side.addWidget(self.rail_only_box)
        # The control only does anything when at least one visible rail is
        # bridged to siblings; hide it when no current selection would be
        # affected by toggling it.
        self._sync_rail_only_visibility()

        self.src_ref_box = QCheckBox("Reference to source return")
        self.src_ref_box.setChecked(True)
        self.src_ref_box.setToolTip(
            "Voltage mode only. On: each net's heatmap is referenced to its "
            "own SOURCE return pin, so it reads the net's true differential "
            "(≤ rail voltage). Off: shows absolute potential referenced "
            "to the single global 0 V point — which can read a fraction of a "
            "mV ABOVE the rail, because a source's return pin doesn't sit "
            "exactly at that global 0 V."
        )
        side.addWidget(self.src_ref_box)

        self.via_span_box = QCheckBox("Show via span")
        self.via_span_box.setChecked(False)
        self.via_span_box.setToolTip(
            "When on, each visible via is labelled with its copper-layer "
            "span (e.g. 1:8 = layer 1 → layer 8, 1:2 = blind/buried L1→L2). "
            "The label is drawn centred inside the via."
        )
        side.addWidget(self.via_span_box)

        self.cursor_tooltip_box = QCheckBox("Show cursor tooltip (T)")
        self.cursor_tooltip_box.setChecked(False)
        self.cursor_tooltip_box.setToolTip(
            "When on, a small tooltip follows the cursor showing the "
            "value of the current mode (Voltage / Voltage Drop / Current "
            "Density / Power Density) at that point, plus the net and "
            "layer. Same information as the bar at the bottom of the plot."
        )
        side.addWidget(self.cursor_tooltip_box)

        self.show_arrows_box = QCheckBox("Show current arrows (A)")
        self.show_arrows_box.setChecked(False)
        self.show_arrows_box.setToolTip(
            "Overlay white arrows showing the direction (and relative "
            "magnitude) of current flow at a regular grid of sample points. "
            "The arrow shaft length scales with sqrt(|J|) so weak and strong "
            "currents are both visible. Press A to toggle. Works in both "
            "2D and 3D — in 3D each arrow sits on its layer's copper top."
        )
        side.addWidget(self.show_arrows_box)

        self.arrow_spacing_label = QLabel("Arrow density: 30")
        self.arrow_spacing_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; font-size: 8pt; }}"
        )
        side.addWidget(self.arrow_spacing_label)
        self.arrow_spacing_slider = QSlider(Qt.Horizontal)
        self.arrow_spacing_slider.setRange(5, 300)
        self.arrow_spacing_slider.setValue(30)
        self.arrow_spacing_slider.setToolTip(
            "Approximate number of arrows along the shorter side of each "
            "layer. Arrows are placed at fixed world-space positions, so "
            "zooming pans through them instead of resampling — keeps the "
            "count bounded on very large designs."
        )
        self.arrow_spacing_slider.valueChanged.connect(
            self._on_arrow_density_changed,
        )
        side.addWidget(self.arrow_spacing_slider)
        # The density control only matters while arrows are shown — keep
        # the label + slider hidden until "Show current arrows" is ticked.
        self.arrow_spacing_label.setVisible(False)
        self.arrow_spacing_slider.setVisible(False)

        # Layer-spacing slider — drives both the per-layer z separation
        # and the via cylinder length in 3D mode (they share the same
        # vertical-exaggeration uniform, so dragging is instant — no
        # mesh rebuild). No-op in 2D mode.
        side.addSpacing(8)
        self.layer_spacing_label = QLabel("Layer spacing: 10×")
        self.layer_spacing_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; font-size: 8pt; }}"
        )
        side.addWidget(self.layer_spacing_label)
        self.layer_spacing_slider = QSlider(Qt.Horizontal)
        self.layer_spacing_slider.setRange(1, 100)
        self.layer_spacing_slider.setValue(10)
        self.layer_spacing_slider.setToolTip(
            "3D mode only. Higher = more visual separation between "
            "layers and longer via cylinders; 1× ≈ physical thickness."
        )
        self.layer_spacing_slider.valueChanged.connect(
            self._on_layer_spacing_changed,
        )
        side.addWidget(self.layer_spacing_slider)

        side.addSpacing(12)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        side.addWidget(self.summary_label)

        side.addStretch(1)

        # Wrap side layout in a width-adjustable container, then put that
        # inside a QScrollArea so the panel scrolls vertically when its
        # contents exceed the window height (e.g. many copper layers).
        # Width is user-draggable via the SidebarToggleButton splitter.
        self._SIDEBAR_DEFAULT_W = 260
        self._SIDEBAR_MIN_W = 180
        self._SIDEBAR_MAX_W = 520
        self._SIDEBAR_SCROLLBAR_W = 18
        self._sidebar_content_w = self._SIDEBAR_DEFAULT_W
        # What the user asked for, before clamping to the current window —
        # see _apply_sidebar_content_width.
        self._sidebar_requested_w = self._SIDEBAR_DEFAULT_W
        self._sidebar_drag_start_w = self._sidebar_content_w

        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(self._sidebar_content_w)
        self._sidebar_widget = side_widget

        side_scroll = QScrollArea()
        side_scroll.setWidget(side_widget)
        # Resizable=True is essential: with =False, the inner widget uses its
        # width-agnostic sizeHint() for height, which under-estimates the
        # height of word-wrapped labels (e.g. summary_label) at the actual
        # panel width, and the controls below it overlap. =True makes the
        # layout reflow at the real width.
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Always-on vertical scrollbar so the inner viewport width is
        # constant regardless of whether scrolling is needed; without this,
        # adding/removing the scrollbar would shift content widths around.
        side_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        side_scroll.setFixedWidth(
            self._sidebar_content_w + self._SIDEBAR_SCROLLBAR_W,
        )
        outer.addWidget(side_scroll)
        self._sidebar_scroll = side_scroll
        self._apply_sidebar_scroll_theme()

        # Slim vertical splitter: click toggles collapse; drag resizes width.
        # Hotkey "B" mirrors the click.
        self._sidebar_toggle_btn = SidebarToggleButton()
        self._sidebar_toggle_btn.setToolTip(
            "Drag to resize the side panel · Click to collapse / expand (B)"
        )
        self._sidebar_toggle_btn.clicked.connect(self._toggle_sidebar)
        self._sidebar_toggle_btn.resizedBy.connect(self._on_sidebar_resized_by)
        self._sidebar_toggle_btn.pressed.connect(self._on_sidebar_resize_press)
        outer.addWidget(self._sidebar_toggle_btn)
        self._load_sidebar_width_from_project()

        # Plot area — custom QOpenGLWidget rendering the FEM mesh directly
        # via shaders (per-vertex colour interpolation, MVP transform on
        # GPU). Pan and zoom become single matrix uniforms; no rasterise,
        # no texture upload, always pixel-sharp.
        plot_layout = QVBoxLayout()
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(0)

        self._gl_viewer = GLMeshViewer()
        # Push the colormap LUT once — every render reuses it via uniform.
        self._gl_viewer.set_colormap(_build_cmap_lut(self._cmap_name))
        # Resize / pan / zoom all flow through this signal — we use it to
        # keep our cached mm-per-pixel in sync (so window resizes preserve
        # the user's zoom level CAD-style) and to schedule a re-fit on
        # the first render once Qt has settled the widget size.
        self._gl_viewer.viewChanged.connect(self._on_gl_view_changed)
        self._gl_viewer.mouseHoveredAt.connect(self._on_gl_mouse_hovered)
        self._gl_viewer.clicked.connect(self._on_gl_clicked)
        # Top-right legend chip: clicking a row toggles that marker
        # category's visibility (and slashes the row to mark it hidden).
        self._gl_viewer.legendRowClicked.connect(
            self._on_legend_row_clicked)
        # Editor-mode free-marker dragging: the hit-test lets the viewer
        # claim a press over a marker, and the editorDrag* signals drive
        # the constrained move (see _on_marker_drag_*).
        self._gl_viewer.set_editor_drag_hit_test(self._marker_drag_hit_test)
        self._gl_viewer.editorDragStarted.connect(self._on_marker_drag_started)
        self._gl_viewer.editorDragMoved.connect(self._on_marker_drag_moved)
        self._gl_viewer.editorDragReleased.connect(
            self._on_marker_drag_released)
        # Editor-mode rubber-band select: a left drag on empty 2D viewport.
        self._gl_viewer.editorMarqueeSelected.connect(self._on_editor_marquee)

        self._spacemouse = SpaceMouseController(
            self._gl_viewer,
            self._on_spacemouse_fit,
            self,
        )

        plot_layout.addWidget(self._gl_viewer, 1)

        # Heatmap colour-scale strip — overlaid on the GL viewer's
        # bottom-left corner rather than living in the side panel. It's a
        # live child widget (not a paintGL overlay) so the Min/Max drag
        # handles stay interactive. The ScaleController still owns it and
        # drives its colormap / range / title; only the parent + on-screen
        # position change here. _position_scale_overlay (called on every
        # GL-viewer resize via eventFilter) keeps it pinned bottom-left.
        self._scale_overlay = self.scale_controller.bar
        self._scale_overlay.setParent(self._gl_viewer)
        # Hidden when there are no rails — the heatmap is empty so the
        # scale strip has nothing meaningful to display.
        self._scale_overlay.setVisible(bool(self._rails))
        self._position_scale_overlay()

        # Probe label: updated on every mouse move over the plot. Colours
        # pinned so the text stays readable under any system / Qt theme
        # (otherwise white-on-light-grey from the inherited dark theme).
        self.probe_label_widget = QLabel("Hover the plot to probe values")
        _t = _T()
        self.probe_label_widget.setStyleSheet(
            f"QLabel {{ font-family: Consolas, monospace; padding: 6px 10px;"
            f" color: {_t['fg']}; background-color: {_t['bg']};"
            f" border-top: 1px solid {_t['border']}; }}"
        )
        plot_layout.addWidget(self.probe_label_widget)

        # Editor-mode overlay buttons — live children of the GL viewer,
        # pinned top-left (the scale strip owns bottom-left). The editor
        # toggle is always visible; the resolve button only shows while
        # there are unsolved editor edits. _position_editor_overlays
        # (wired to GL-viewer resize via eventFilter) keeps them pinned.
        self._build_editor_overlay_buttons()
        self._refresh_solve_stale_overlay()

        plot_widget = QWidget()
        plot_widget.setLayout(plot_layout)
        outer.addWidget(plot_widget, 1)

        # Right-hand panel — overlaid on the GL viewer's right edge
        # rather than added to the outer layout. Floating it as a child
        # widget (same pattern as ``_scale_overlay``) keeps the viewport's
        # pixel size and pan / zoom centre invariant when the panel
        # appears, so the visible copper doesn't shift sideways. Carries
        # the PDN editor form in editor mode and the copper-properties
        # form in viewer mode. Width is user-adjustable (drag its left
        # edge); seed it with the default before the panel is built.
        self._editor_panel_width = self._EDITOR_PANEL_DEFAULT_W
        self._editor_panel = self._build_editor_panel()
        self._editor_panel.setParent(self._gl_viewer)
        self._editor_panel.hide()
        self._position_editor_panel()

        self._init_log.info(
            "PdnViewer init: pre-tabs done (%.2fs)",
            time.monotonic() - self._init_t0,
        )
        # Setup tab — populated from the metadata bundle that ships with the
        # solve pickle. Built once at construction time; doesn't react to
        # heatmap selection changes.
        _t = time.monotonic()
        self.tabs.addTab(self._build_setup_tab(), "Setup")
        self._init_log.info("PdnViewer init: Setup tab (%.2fs)", time.monotonic() - _t)

        _t = time.monotonic()
        self._topology_populated = False
        self._topology_tab_index = self.tabs.addTab(
            self._build_topology_tab(), "Topology",
        )
        self._init_log.info("PdnViewer init: Topology tab (%.2fs)", time.monotonic() - _t)

        # Nodes tab — sortable, filterable table of every directive node's
        # voltage / drop / current density / power density. The empty
        # table structure is built up-front (cheap); the actual row
        # population (one QTableWidgetItem per node × N columns + a voltage
        # interpolator per (layer, net)) is deferred to the first time the
        # user navigates to this tab — see :meth:`_on_tabs_current_changed`.
        # Without this, opening a viewer on a board with thousands of nodes
        # blocks the GUI thread for several seconds.
        _t = time.monotonic()
        self._nodes_table_populated = False
        self._nodes_tab_index = self.tabs.addTab(self._build_nodes_tab(), "Nodes")
        self._init_log.info("PdnViewer init: Nodes tab (%.2fs)", time.monotonic() - _t)

        # Fixes — where extra copper would help each load most. Ranking the
        # loads samples every pin, so it's populated on first open.
        self._fixes_populated = False
        self._fixes_tab_index = self.tabs.addTab(self._build_fixes_tab(), "Fixes")

        # Vias tab — sortable, filterable table of every via's worst-segment
        # current + power dissipation. Same lazy-populate treatment as the
        # Nodes tab; on a 7 000-via board the populate step alone took
        # 35 seconds of blocked GUI thread. The empty structure plus a
        # placeholder row are built up-front so the tab isn't visually
        # empty if the user immediately clicks it.
        _t = time.monotonic()
        self._vias_table_populated = False
        self._vias_tab_index = self.tabs.addTab(self._build_vias_tab(), "Vias")
        self._init_log.info("PdnViewer init: Vias tab (%.2fs)", time.monotonic() - _t)

        # Bridges — every part that joins two nets, and what FYPA did with
        # it. Cheap to build (the candidate scan already ran during the
        # solve), but populated lazily like its neighbours for consistency.
        _t = time.monotonic()
        self._bridges_table_populated = False
        self._bridges_tab_index = self.tabs.addTab(
            self._build_bridges_tab(), "Bridges")
        self._update_bridges_tab_title()
        self._init_log.info("PdnViewer init: Bridges tab (%.2fs)",
                            time.monotonic() - _t)

        # Capacitors tab — decoupling-cap loop-inductance analysis. Same
        # lazy-populate treatment; unlike Nodes/Vias there's no deferred
        # warning-count init either, because the row build includes a
        # per-net copper-shape union (seconds on a big board) that we don't
        # want to pay invisibly at startup — the ⚠ badge appears after the
        # first tab open.
        _t = time.monotonic()
        self._caps_table_populated = False
        self._caps_tab_index = self.tabs.addTab(
            self._build_capacitors_tab(), "Capacitors")
        self._init_log.info("PdnViewer init: Capacitors tab (%.2fs)",
                            time.monotonic() - _t)

        # Impedance tab — per-rail Z(f) against a target mask, built on the
        # loop inductances the Capacitors tab extracts. Same lazy populate:
        # the plot needs those rows.
        _t = time.monotonic()
        self._impedance_populated = False
        self._impedance_tab_index = self.tabs.addTab(
            self._build_impedance_tab(), "Impedance")
        self._init_log.info("PdnViewer init: Impedance tab (%.2fs)",
                            time.monotonic() - _t)

        # Messages tab — sortable, filterable view of every log record
        # captured since the process started (see fypa.log_buffer). Lets
        # the user audit loader / solver / editor warnings without
        # opening fypa.log directly.
        _t = time.monotonic()
        self._messages_tab_index = self.tabs.addTab(
            self._build_messages_tab(), "Messages",
        )
        self._init_log.info("PdnViewer init: Messages tab (%.2fs)",
                            time.monotonic() - _t)

        # Settings tab — tunable physics + meshing + display knobs and a
        # Re-run button. Changes apply on the next solve (Re-run opens a
        # fresh viewer with the new solution).
        _t = time.monotonic()
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self._init_log.info("PdnViewer init: Settings tab (%.2fs)", time.monotonic() - _t)

        # Help tab — static reference for hotkeys + mouse controls. Built
        # once at construction; never updates.
        _t = time.monotonic()
        self.tabs.addTab(self._build_help_tab(), "Help")
        self._init_log.info("PdnViewer init: Help tab (%.2fs)", time.monotonic() - _t)
        self._init_log.info(
            "PdnViewer init: all tabs done (total %.2fs)",
            time.monotonic() - self._init_t0,
        )

        # Lazy-populate the Nodes/Vias tables on first activation. Done
        # this way (rather than on construction) so opening a viewer on a
        # large board is instant — the user lands on the Heatmap tab and
        # only pays for the row builds if they actually navigate to the
        # table tabs.
        self.tabs.currentChanged.connect(self._on_tabs_current_changed)

        # Wire up signals.
        # layer_list.itemChanged is wired in _build_ui via
        # _on_layer_visibility_changed so we can pause-and-resume during
        # programmatic checks without spamming renders.
        # A mode pick turns the Fixes tab's value map off before re-rendering.
        self.mode_combo.currentTextChanged.connect(self._clear_roi_value_map)
        self.mode_combo.currentTextChanged.connect(self._render_with_busy_popup)
        self.rail_only_box.toggled.connect(self._render_with_busy_popup)
        self.src_ref_box.toggled.connect(self._render_with_busy_popup)
        self.via_span_box.toggled.connect(
            lambda _checked: self._refresh_overlay_geometry(
                self._visible_rails()))
        self.show_mesh_box.toggled.connect(
            lambda checked: self._gl_viewer.set_show_mesh_edges(checked)
        )
        self.colour_stubs_box.toggled.connect(self._render_with_busy_popup)
        self.cursor_tooltip_box.toggled.connect(self._on_cursor_tooltip_toggled)
        self.view_3d_box.toggled.connect(self._on_view_3d_toggled)
        self.heatmap_vias_box.toggled.connect(self._render_with_busy_popup)
        self.show_arrows_box.toggled.connect(self._on_arrows_toggled)
        # Keep the View-menu item labels in step whenever one of their
        # backing checkboxes flips — whether from the menu action, a
        # keyboard hotkey, or a restored project setting.
        for _box in (self.view_3d_box, self.heatmap_vias_box,
                     self.colour_stubs_box, self.show_mesh_box):
            _box.toggled.connect(self._refresh_view_menu_labels)
        self._refresh_view_menu_labels()
        # Pan, wheel zoom, and resize handlers are wired to the GL viewer
        # via signals connected when the viewer was created.
