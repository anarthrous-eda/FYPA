"""The PdnViewer main window, assembled from per-feature mixins."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
import numpy as np
from fypa.gl_mesh_viewer import _install_default_surface_format, GLMeshViewer
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from fypa.viewer.assets import _load_app_icon
from fypa.viewer.copper_pick import _CopperPickMixin
from fypa.viewer.diagnostics import (
    _activate_mesh_failure_layer,
    _maybe_show_annotation_errors,
    _maybe_show_mesh_failures,
    _maybe_warn_connectivity_breaks,
    _maybe_warn_needs_directives,
    _maybe_warn_open_loop_rails,
)
from fypa.viewer.display import (
    _DEFAULT_CMAP_NAME,
    _DISPLAY_PERCENTILE_HIGH,
    _split_composite_name,
    _VIA_CURRENT_WARN_A,
)
from fypa.viewer.editor.attached import _AttachedPdnMixin
from fypa.viewer.editor.form import _EditorFormMixin
from fypa.viewer.editor.marquee import _MarqueeMixin
from fypa.viewer.editor.mode import _EditorModeMixin
from fypa.viewer.editor.nets import _EditorNetsMixin
from fypa.viewer.editor.pending import _PendingRailsMixin
from fypa.viewer.editor.selection import _EditorSelectionMixin
from fypa.viewer.file_menu import _FileMenuMixin
from fypa.viewer.markers import _MarkerOverlayMixin
from fypa.viewer.panels.layers import _LayerPanelMixin
from fypa.viewer.panels.overlays import _OverlayPanelMixin
from fypa.viewer.prefs import load_via_no_current_opacity
from fypa.viewer.probes import _ProbeMixin
from fypa.viewer.render import _RenderMixin
from fypa.viewer.report_export import _ReportExportMixin
from fypa.viewer.session import _viewer_has_adaptive_smps
from fypa.viewer.settings_tab import _SettingsTabMixin
from fypa.viewer.tabs.bridges import _BridgesTabMixin
from fypa.viewer.tabs.capacitors import _CapacitorsTabMixin
from fypa.viewer.tabs.impedance import _ImpedanceTabMixin
from fypa.viewer.tabs.messages import _MessagesTabMixin
from fypa.viewer.tabs.nodes import _NodesTabMixin
from fypa.viewer.tabs.setup import _SetupTabMixin
from fypa.viewer.tabs.topology import _TopologyTabMixin
from fypa.viewer.tabs.vias import _ViasTabMixin
from fypa.viewer.theme import _T
from fypa.viewer.ui_build import _UiBuildMixin
from fypa.viewer.vias import _ViaRenderMixin
from fypa.viewer.viewport import _ViewportMixin
from fypa.viewer.tabs.fixes import _FixesTabMixin
from fypa.viewer.widgets import _esc


# Install the default QSurfaceFormat (OpenGL 3.3 core, vsync on) BEFORE
# any QOpenGLWidget is constructed. Idempotent — calling it from this
# module-level scope ensures the format is set before the GLMeshViewer
# in the Heatmap tab gets created.
_install_default_surface_format()




class PdnViewer(
    _UiBuildMixin,
    _LayerPanelMixin,
    _OverlayPanelMixin,
    _RenderMixin,
    _MarkerOverlayMixin,
    _ViaRenderMixin,
    _ViewportMixin,
    _EditorModeMixin,
    _AttachedPdnMixin,
    _EditorSelectionMixin,
    _CopperPickMixin,
    _EditorNetsMixin,
    _EditorFormMixin,
    _MarqueeMixin,
    _PendingRailsMixin,
    _ProbeMixin,
    _FileMenuMixin,
    _ReportExportMixin,
    _SetupTabMixin,
    _TopologyTabMixin,
    _NodesTabMixin,
    _ViasTabMixin,
    _BridgesTabMixin,
    _CapacitorsTabMixin,
    _ImpedanceTabMixin,
    _MessagesTabMixin,
    _FixesTabMixin,
    _SettingsTabMixin,
    QMainWindow,
):
    """Main window — composes the side panel and the matplotlib plot area.

    ``metadata`` (if supplied) is the dict bundled into the solve pickle
    by :func:`fypa.altium.loader.build_solve_metadata` — used to populate the
    Setup tab with stackup / physics constants / directive details /
    solver stats. If ``None`` (e.g. loading a legacy pickle), the Setup
    tab shows a note explaining why metadata isn't available.
    """

    def __init__(self, solution, metadata: dict | None = None,
                  initial_settings: object | None = None,
                  via_current_warn_a: float | None = None,
                  display_percentile_high: float | None = None,
                  project: object | None = None,
                  project_path: object | None = None,
                  loaded_project: object | None = None):
        super().__init__()
        # Stashed on self so _build_ui (called below) can log per-tab
        # timings against the same start point.
        self._init_t0 = time.monotonic()
        self._init_log = logging.getLogger(__name__)
        self._init_log.info("PdnViewer init: START")
        self.solution = solution
        self.metadata = metadata
        # Backward compat: older pickles pre-date the per-primitive
        # ``primitives`` block. Default to empty buckets so the viewer-mode
        # copper-click feature simply produces no hits instead of crashing.
        if isinstance(self.metadata, dict):
            self.metadata.setdefault("primitives", {
                "tracks": [], "arcs": [], "regions": [],
                "shape_based_regions": [], "fills": [],
            })

        # --- Editor mode + project-file state ---
        # The .fypa project (links the two cache pickles + editor
        # directives), set when the viewer is opened from / saved to a
        # project file, or carried across a resolve. See
        # :mod:`fypa.project_file`.
        self._project = project           # ProjectFile | None
        self._project_path = project_path  # Path | None
        # In-memory LoadedProject (pristine design extract) from the worker
        # that produced this viewer's solution. The editor 'Resolve' hands
        # it straight back to the next worker, so a resolve never re-reads
        # the design-info cache or re-stats the Altium project files. None
        # when opened from a bare pickle — a resolve then falls back to the
        # design-info cache.
        self._loaded_project = loaded_project
        # _project_dirty: editor edits not yet written to the .fypa that also
        #                 invalidate the solve (directives, copper names, …).
        # _display_dirty: cosmetic edits not yet written to the .fypa that do
        #                 NOT invalidate the solve (overlay colours). Kept
        #                 separate so a display-only colour pick can't light
        #                 up the Resolve button (see _on_overlay_color).
        # _settings_dirty: Settings-tab fields differ from the values
        #                  the current solve was run against.
        # _initial_solve_stale: viewer loaded with edits not yet
        #                       reflected by the solve (e.g. .fypa with
        #                       editor directives + a stale solve pickle).
        # _solve_stale: derived = OR of _project_dirty / _settings_dirty /
        #               _initial_solve_stale (NOT _display_dirty); drives the
        #               Resolve overlay button.
        self._project_dirty: bool = False
        self._display_dirty: bool = False
        self._settings_dirty: bool = False
        self._initial_solve_stale: bool = False
        self._solve_stale: bool = False
        # True while the viewer holds a stub and the user has not run the
        # FEM yet — drives the overlay button label ("Solve" vs "Resolve").
        self._awaiting_first_solve: bool = False
        self._has_adaptive_smps: bool = _viewer_has_adaptive_smps(
            getattr(self, "metadata", None),
            getattr(self, "_loaded_project", None),
        )
        # Whether a solver run has happened that isn't yet persisted to a
        # pickle — gates the "Save project + latest solver run" option in
        # the Ctrl+S popup. Set True by the resolve handler; a viewer
        # opened from disk starts False (its solution is already saved).
        self._solved_since_save: bool = False
        self._editor_mode: bool = False
        # Current editor selection: a dict carrying 'kind' plus
        # selection-specific keys, or None when nothing is selected.
        self._editor_selection: dict | None = None
        # Marquee (rubber-band) multi-selection: one entry per selected PDN
        # marker, ``{"kind": "directive"|"schematic", "id", "role", "label"}``
        # where ``id`` is the editor directive's id or, for a still-locked
        # Altium schematic directive, its component designator. Empty
        # whenever the selection is single (``_editor_selection``) or
        # nothing: the two are mutually exclusive, and a one-object marquee
        # collapses to the ordinary single selection so every pre-existing
        # code path still sees the shape it expects.
        self._editor_multi: list[dict] = []
        # Field names the multi-sink form has had user input on since it was
        # built. Apply writes only these, so a row still showing ``*`` (or an
        # untouched shared value) is left alone on every selected sink.
        self._mf_dirty: set[str] = set()
        # Multi-sink form widgets, (re)assigned by
        # :meth:`_populate_multi_editor_form`. Declared here so nothing can
        # reach them before the first multi-selection builds the form.
        self._mf_current = None
        self._mf_min_v = None
        self._mf_single = None
        self._mf_two = None
        self._mf_btngroup = None
        self._mf_pnet = None
        self._mf_nnet = None
        self._mf_nnet_label = None
        self._mf_status = None
        # Viewer-mode copper-primitive click selection. ``None`` when
        # nothing is selected; otherwise a dict ``{"kind", "record",
        # "layer_id", "net"}`` returned by :meth:`_primitive_at_point`.
        # Drives the dashed-yellow GL overlay and the right-panel form.
        self._copper_selection: dict | None = None
        # Lazy ``(layer_id, net) -> [primitive dict]`` index over
        # ``metadata['primitives']``. Built on first click.
        self._primitives_by_layer_net: dict[
            tuple[int, str], list[dict]] | None = None
        # Net names currently highlighted (connected copper); everything
        # else renders dimmed. Empty = no highlight active.
        self._editor_highlight_nets: set[str] = set()
        # Polygon identities currently highlighted as part of the same
        # electrical net as the editor-mode selection. Keyed by
        # ``(layer_id, id(poly_dict))`` — the poly dicts come from
        # ``metadata['all_copper']`` and stay stable across renders.
        # Used when the click lands on copper with no usable net name
        # (e.g. Gerber-sourced ``"(none)"`` copper) so the dim mask can
        # still distinguish the picked rail from disjoint unnamed pieces.
        self._editor_highlight_polys: set[tuple[int, int]] = set()
        # Net-focus ("show only this net", the net table's crosshair
        # column). Unlike the highlight above — which is a by-product of
        # whatever is selected and dies with it — focus is sticky: only
        # the crosshair, the F hotkey or leaving editor mode releases it,
        # so a stray click in the canvas can't cost the user the view they
        # set up. While it is on, copper outside the focused net is not
        # drawn at all (not merely dimmed): ``_editor_focus_nets`` holds
        # the net names that survive and ``_editor_focus_polys`` the
        # ``(layer_id, id(poly_dict))`` identities, the latter carrying
        # unnamed / synthetic copper whose "(none)" net name can't
        # identify it. ``_editor_focus_net`` is the table row's display
        # name, kept for the release chip and the icon sync.
        self._editor_focus_net: str | None = None
        self._editor_focus_nets: set[str] = set()
        self._editor_focus_polys: set[tuple[int, int]] = set()
        # "drop a free marker of this role on the next viewport click" —
        # None, or "SOURCE" / "SINK".
        self._editor_pending_marker: str | None = None
        # In-progress free-marker drag — a dict carrying the directive id,
        # its layer, the drag-start anchor / net, and the last on-copper
        # position; None when no drag is active. See _on_marker_drag_*.
        self._marker_drag: dict | None = None
        # Re-entrancy guard for the X / Y text-box on-copper check so the
        # focus shuffle of the revert warning can't recurse.
        self._suppress_coord_check: bool = False
        # Undo / redo stacks for free-marker edits — moves and deletes.
        # Each record carries an "op": a "move" record is
        # {"op": "move", "id", "old_xy", "new_xy", "old_p_net", "new_p_net"};
        # a "delete" record is {"op": "delete", "id", "directive", "index"}
        # where ``directive`` is the removed EditorDirective and ``index``
        # is its position in editor_directives so undo preserves z-order.
        self._marker_undo: list[dict] = []
        self._marker_redo: list[dict] = []
        # Cached non-editor marker groups, so a free-marker drag can do a
        # marker-only refresh without re-walking every pin (see
        # _refresh_editor_markers).
        self._non_editor_marker_groups: list | None = None
        # Pending (unsolved) editor rails surfaced in the Rails list.
        self._pending_rails: dict[str, list[str]] = {}
        self._pending_rail_items: list = []
        # Solve-time + display-time settings exposed in the Settings tab.
        # ``initial_settings`` is a :class:`fypa.altium.loader.SolveSettings` —
        # passed by the Re-run handler so the new viewer pre-populates the
        # fields with whatever the user just submitted. Falls back to the
        # values recorded in the pickle (which match the FEM that produced
        # this solution) so opening a fresh pickle always shows the truth.
        from fypa.altium.loader import SolveSettings as _SolveSettings
        if initial_settings is None:
            initial_settings = _SolveSettings.from_metadata(metadata)
        self._solve_settings = initial_settings
        # Display-only knobs (no re-solve needed to apply, but settable
        # from the same Settings tab so users have one place for tuning).
        self._via_current_warn_a: float = (
            float(via_current_warn_a) if via_current_warn_a is not None
            else _VIA_CURRENT_WARN_A
        )
        self._display_percentile_high: float = (
            float(display_percentile_high) if display_percentile_high is not None
            else _DISPLAY_PERCENTILE_HIGH
        )
        # Opacity of no-current via-barrel sections (3D cylinder fade), from
        # the persisted "No-current via opacity" Settings knob. Read live by
        # :meth:`_solid_via_chunks` / :meth:`_heatmap_via_chunks`.
        self._via_dead_section_alpha: float = load_via_no_current_opacity()
        project_name = getattr(solution.problem, "project_name", None) or "unknown"
        self.setWindowTitle(f"FYPA -- {project_name}")
        icon = _load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        # Open maximised so the plot has the most canvas to work with. With
        # aspect='equal', the plot's height grows proportionally with its
        # width — so a bigger window = a bigger board view, no stretching.
        self.resize(1400, 900)
        # Defer the maximise until after show() — calling showMaximized()
        # during __init__ doesn't always stick on Windows.
        self._pending_maximize: bool = True

        # Re-entrancy guard for _run_with_busy_popup so a signal fired by
        # processEvents() during the popup-paint can't stack a second
        # dialog on top of the first.
        self._render_busy_active: bool = False
        # Latest work callable that re-entered _run_with_busy_popup while it
        # was busy. Rather than silently drop it (which leaves an eye button
        # flipped but its copper never pushed), we run exactly one trailing
        # pass with the most recent work once the current one returns.
        self._busy_popup_pending_work = None
        # Re-entrancy guard for _render itself. Some signals (the outline
        # toggle, the value-scale range change) connect directly to
        # _render, bypassing _run_with_busy_popup's guard. _render pumps
        # QApplication.processEvents() at every stage boundary, so such a
        # signal can re-enter mid-render (in the window before the modal
        # busy dialog appears) and push half-built meshes into GL state.
        # _render defers a re-entrant call and coalesces it into one
        # trailing pass once the in-flight render returns.
        self._render_in_progress: bool = False
        self._render_rerender_pending: bool = False
        # Throttle timestamp for _pump_busy_ui — caps the marquee-bar
        # repaint rate at ~33 Hz so the pump can be sprinkled in hot
        # inner loops (every polygon, every per-(phys, net) tile) without
        # blowing up overhead. Reset to 0.0 when the popup opens.
        self._last_pump_time: float = 0.0

        self._init_solution_indices()

        # Designators of directives currently expanded in the Setup tab.
        # Empty by default → all directives collapsed; user click toggles.
        self._expanded_directives: set[str] = set()

        # Cached state for the hover probe. Updated on every _render(),
        # reused by _on_gl_mouse_hovered.
        self._probe_unit: str = ""
        self._probe_label: str = ""
        self._last_probe_at: float = 0.0
        # Per-(physical_layer, net) probe descriptors, top-first. Each
        # dict has: 'physical', 'net', 'layer_id', 'triangulation',
        # 'interpolator', 'values', 'prepared_shape'. Built by
        # :meth:`_build_rail_arrays`.
        self._layer_probes: list[dict] = []
        # Per-(layer_index, derive_fn) cache of the assembled mesh arrays
        # + Triangulation + _FastTriSampler + prepared shapely shape.
        # Solution is immutable for the session, so entries live forever
        # — this makes layer-toggle (re-render) re-use the already-built
        # voltage sampler instead of rebuilding it.
        self._layer_cache: dict[tuple[int, int], dict] = {}
        # Per-layer GEOMETRY cache, keyed by layer_index alone — the
        # coordinates / triangulation / prepared shape / outline are identical
        # across heatmap modes (only the per-vertex values differ). Splitting it
        # out of ``_layer_cache`` (which is keyed by (layer, derive_fn)) means a
        # mode switch reuses the layer's Triangulation and arrays instead of
        # rebuilding them, and stores one copy of xs/ys/tris per layer rather
        # than one per (layer, mode).
        self._layer_geom_cache: dict[int, dict] = {}
        # Combined GPU geometry-batch cache: (signature, (xs, ys, zs, tris,
        # no_current)). Built by :meth:`_build_rail_arrays`; reused wholesale
        # (same array objects) on a geometry-preserving re-render so the
        # set_mesh GPU upload is skipped. Scoped to the current solve.
        self._rail_geom_cache: tuple | None = None
        # Signature of the inputs the last overlay-fill build read (see
        # :meth:`_overlay_geom_signature`). When an incoming refresh matches it,
        # the previously-built overlay batch is still valid and the heavy
        # rebuild + GPU re-upload is skipped. Reset on any solution swap.
        self._overlay_geom_sig: tuple | None = None
        # Lazily-computed set of (layer_index, mesh_index) whose solved mesh
        # carries no current — i.e. a dead-end copper island whose only tie
        # to the rest of the net is a single via, so KCL forces zero current
        # through it and it sits at a uniform potential. Used by the "Grey
        # no current copper" toggle to grey these the same way it greys
        # FEM-excluded stubs. ``None`` until first built.
        self._no_current_meshes: set[tuple[int, int]] | None = None
        # Per-layer cache of current-density vectors (J = -sigma * grad V).
        # Mode-independent — keyed by layer_index only — because the
        # current arrows are derived from the raw potentials regardless
        # of which scalar mode the heatmap is showing.
        self._layer_vec_cache: dict[int, dict] = {}
        # Data bounds, colour-scale clamp, and the current colormap name.
        # All consumed by either the GLMeshViewer (rendering) or the
        # CPU-side hover probe (mode / Voltage Drop reference).
        self._data_bounds: tuple[float, float, float, float] | None = None
        self._vmin: float = 0.0
        self._vmax: float = 1.0
        self._cmap_name: str = _DEFAULT_CMAP_NAME
        # Which LUT is currently uploaded to the GL viewer's copper-mesh
        # cmap texture: "data" = the viridis ramp keyed on per-vertex
        # values; "neutral" = a flat grey LUT used in Via Current mode
        # so the copper drops out as context behind the heatmapped vias.
        # ``self._cmap_name`` is unchanged — it still tracks the data
        # ramp, which the via cylinders / scale controller use directly.
        self._gl_cmap_kind: str = "data"
        # Linear vs logarithmic colour scale. ``_log_scale`` is the user's
        # dropdown choice (persists across modes); ``_log_active`` is the
        # effective state for the current render — True only when the
        # mode is log-eligible and the data range is positive. The GL
        # values/levels and the baked via LUTs are pushed through
        # :meth:`_gl_scale`, which is log10 (floored at ``_log_floor``)
        # exactly when ``_log_active`` is True.
        self._log_scale: bool = False
        self._log_active: bool = False
        self._log_floor: float = 1e-12
        # (net, x_mm, y_mm) -> max-|segment-current| for every via on
        # the rail set rendered last. Populated in Via Current mode by
        # :meth:`_render`; empty for every other mode. Used by both the
        # 3D via cylinder coloring path and the 2D marker overlay.
        self._via_current_lookup: dict[tuple[str, float, float], float] = {}
        # Hit-test index for SOURCE/SINK marker hover. Rebuilt by
        # :meth:`_update_markers_and_legend` from the same pin walk that
        # populates the marker overlay, so it matches exactly what's drawn.
        self._marker_hover_index_cache: dict | None = None
        # Hover rows for the solved (schematic) directive markers, kept so
        # a free-marker drag can recombine them with freshly rebuilt
        # editor-directive rows without re-walking every pin.
        self._metadata_marker_hover_rows: list[dict] = []
        # (visible_layers, rail, mode) signature of the previous render's
        # scale-controller push. Used so a render that doesn't change the
        # heatmap selection (e.g. a 2D/3D toggle) leaves the user's clamp
        # alone — only a real layer/rail/mode change resets it.
        self._last_scale_selection: tuple[tuple[str, ...], str, str] | None = None

        # CAD-style fixed-scale viewport state. When the widget is
        # resized we preserve mm-per-pixel and grow / shrink the visible
        # area, rather than letting the view auto-fit the board.
        self._mm_per_pixel: float = 0.0
        # ``_need_initial_fit`` is True until the very first time the
        # view is successfully fit to the data. Stays False afterwards
        # so subsequent renders (layer toggle, rail change, mode change)
        # do NOT reset the user's pan / zoom — they just swap the mesh
        # in place and leave the viewport alone.
        self._need_initial_fit: bool = True
        # Re-entrancy guard for the GL viewer's synchronous viewChanged
        # signal — see :meth:`_fit_board_to_canvas` for why this exists.
        self._suppress_view_changed: bool = False

        # The OpenGL canvas — assigned in _build_ui.
        self._gl_viewer: GLMeshViewer | None = None
        # Legend rows whose markers the user has hidden by clicking the
        # row in the top-right chip. The row stays visible (slashed) so
        # the toggle is reversible; the matching marker groups are
        # skipped in :meth:`_update_markers_and_legend`. Keyed by the
        # legend label (same value used as the LegendRow ``key``).
        self._hidden_legend_keys: set[str] = set()
        # Currently highlighted via location (world mm). When non-None,
        # a yellow ring is drawn on the GL viewer at this point. Cleared
        # by another jump or by :meth:`_clear_via_highlight`.
        self._highlight_via_xy: tuple[float, float] | None = None

        # Per-(physical, net) nearest-vertex voltage lookups, built lazily
        # the first time a rail's stubs / series bars / heatmap-vias need a
        # voltage sample. Cached for the lifetime of the viewer (the
        # solution doesn't change), so flipping rails / vias on and off
        # never rebuilds them. Each entry is a ``(cKDTree, potentials)``
        # pair or ``None`` — see :meth:`_via_voltage_kdtree`.
        self._via_voltage_kdtree_cache: dict[
            tuple[str, str], tuple | None
        ] = {}
        # Updated each _render() so :meth:`_push_via_cylinders` can apply
        # the same Voltage-Drop offset that the layer heatmap uses.
        self._last_drop_reference: float | None = None

        # Single-entry cache of the stub triangle geometry — see
        # :meth:`_push_stubs`. Stub positions depend only on the visible
        # layer/rail set and the 2D/3D z, never on the colour scheme or
        # scale, so a recolour reuses this and only re-bakes the colours.
        # ``(geom_key, positions, spans)`` or ``None``.
        self._stub_geom_cache: tuple | None = None
        # Per-layer merged-and-triangulated all-copper geometry, keyed by
        # ``(layer_name, frozenset(rail_members))``. Used when a layer's
        # all-copper is drawn as a SOLID fill with alpha < 1.0 so that
        # overlapping polygons in the source data can't cumulatively blend
        # into opacity. See :meth:`_merged_solid_all_copper_tris`.
        self._merged_all_copper_cache: dict[
            tuple[str, frozenset], np.ndarray] = {}

        # Voltage-difference measurement tool. Set when the user presses
        # Shift while hovering copper that has a voltage value in either
        # Voltage or Voltage Drop mode. ``_measure_anchor_xy`` is the
        # world-mm point at shift-press time and ``_measure_anchor_voltage``
        # is the probed voltage there; the live readout subtracts the
        # current cursor's voltage from this anchor.
        self._measure_anchor_xy: tuple[float, float] | None = None
        self._measure_anchor_voltage: float | None = None

        self._build_ui()
        self._install_hotkeys()
        # Application-wide event filter for the Shift-drag voltage-
        # difference tool. Installing it on QApplication (rather than
        # the GL viewer or this window) means Shift key events are
        # picked up regardless of which child widget currently has
        # keyboard focus — without this, the user has to click the
        # viewport first before the tool responds.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        # If this viewer was opened bound to a project (e.g. via a resolve
        # or File > Open Project File), surface any still-pending rails.
        if self._project is not None:
            self._update_pending_rails()
        if (self.metadata or {}).get("mesh_failures"):
            _activate_mesh_failure_layer(self)
        self._render()

    def _init_solution_indices(self) -> None:
        """(Re)build layer / rail indices from :attr:`solution` and
        :attr:`metadata`. Called from ``__init__`` and after an in-place
        solve reload."""
        solution = self.solution
        metadata = self.metadata
        self._index_by_pair = {}
        physicals: set[str] = set()
        for i, layer in enumerate(solution.problem.layers):
            phys, net = _split_composite_name(layer.name)
            self._index_by_pair[(phys, net)] = i
            physicals.add(phys)
        if metadata:
            for row in metadata.get("stackup", []):
                nm = row.get("name")
                if nm and nm not in physicals:
                    physicals.add(nm)

        self._rail_names, self._rail_to_members = self._compute_rail_groups(
            metadata,
        )
        from fypa.rail_groups import build_rail_trees
        self._rail_to_trees = build_rail_trees(
            metadata, self._rail_to_members,
        )

        _stackup_pos = {
            row["name"]: i
            for i, row in enumerate(
                metadata.get("stackup", []) if metadata else []
            )
        }
        _ln_re = re.compile(r"^L(\d+)", re.IGNORECASE)

        def _layer_sort_key(name: str) -> tuple[int, int, str]:
            pos = _stackup_pos.get(name)
            if pos is not None:
                return (0, pos, name)
            lower = name.lower()
            if "top" in lower:
                return (1, 0, name)
            if "bottom" in lower:
                return (3, 0, name)
            m = _ln_re.match(name.strip())
            ln = int(m.group(1)) if m else (1 << 30)
            return (2, ln, name)

        self._physicals = sorted(physicals, key=_layer_sort_key)
        self._phys_stackup_rank = {
            name: rank for rank, name in enumerate(self._physicals)
        }
        self._phys_name_to_layer_id = {}
        for row in (metadata.get("stackup", []) if metadata else []):
            self._phys_name_to_layer_id[row["name"]] = row["layer_id"]

        self._phys_z_mm = {}
        z_accum = 0.0
        stack_rows = (metadata.get("stackup", []) if metadata else []) or []
        for i, row in enumerate(stack_rows):
            name = row.get("name")
            t_cu = float(row.get("copper_thickness_mm") or 0.0)
            if name is not None:
                self._phys_z_mm[name] = -(z_accum + 0.5 * t_cu)
            if i + 1 < len(stack_rows):
                z_accum += t_cu + float(
                    row.get("dielectric_thickness_mm") or 0.0,
                )
        self._rails = self._rail_names

    def _clear_solution_derived_caches(self) -> None:
        """Drop render / report caches tied to the previous solution."""
        self._layer_cache.clear()
        self._layer_geom_cache.clear()
        self._reset_copper_roi()
        self._layer_vec_cache.clear()
        self._rail_geom_cache = None  # combined batch is solve-specific
        self._overlay_geom_sig = None  # overlay batch is solve-specific too
        self._last_mesh_upload = None  # release retained GPU-upload arrays
        self._no_current_meshes = None
        self._marker_hover_index_cache = None
        # Both of these hold values sampled from the previous solution
        # (per-vertex return offsets; per-via current/voltage rows). They must
        # be dropped on re-solve or Voltage-mode readouts and via hovers report
        # the old solve's numbers against the new field.
        self._src_return_offset_cache = None
        self._via_hover_index_cache = None
        self._metadata_marker_hover_rows.clear()
        self._via_current_lookup.clear()
        self._via_voltage_kdtree_cache.clear()
        self._stub_geom_cache = None
        self._merged_all_copper_cache.clear()
        self._primitives_by_layer_net = None
        self._non_editor_marker_groups = None
        self._last_scale_selection = None
        self._layer_probes.clear()
        self._data_bounds = None
        # Copper hit-test / connectivity indices are built from ``self.metadata``
        # and keyed on ``id(poly_dict)``; ``_apply_solve_result`` replaces
        # ``self.metadata`` wholesale, so a re-solve (or File > Import of a
        # different board) leaves these STRtrees / union-find indexing the old
        # board's polygons and their ``id()``-keyed lookups can never match the
        # new metadata. Drop them so they rebuild against the new board.
        self._all_copper_bridges_cache = None
        self._layer_strtrees_cache = None
        self._cc_cache = None
        # Editor selection / connectivity highlight also reference the previous
        # board (``_editor_highlight_polys`` and ``_copper_selection`` hold
        # ``id(poly)`` keys into the stale metadata). Reset them so a re-solve
        # doesn't highlight / rename the wrong copper.
        self._editor_selection = None
        self._editor_highlight_polys = set()
        self._clear_editor_focus(render=False)
        self._copper_selection = None
        for attr in ("_nodes_rows_cache", "_vias_rows_cache"):
            if hasattr(self, attr):
                delattr(self, attr)

    def _clear_gl_mesh(self) -> None:
        """Drop the GL heatmap mesh and the host-side upload cache.

        ``GLMeshViewer.clear_mesh`` zeroes the GPU vertex count; if
        ``_last_mesh_upload`` is left pointing at the previous arrays the
        next render can skip :meth:`set_mesh` (identity match) while still
        calling :meth:`set_values` — which then raises length mismatch."""
        self._gl_viewer.clear_mesh()
        self._last_mesh_upload = None

    def _no_pdn_visibility(self) -> bool:
        """Whether to use the 'blank board' layer / rail visibility defaults:
        no rails shown, and the top physical layer's all-copper turned on.

        True when the design has no PDN rails at all (a Gerber import, or a
        project with no SOURCE/REGULATOR directives) OR while an unsolved
        stub is loaded (Import Altium Design with auto-solve off). A stub's
        rails carry no solved data yet, so auto-showing them is misleading —
        mirror the no-PDN defaults until the user runs the solver."""
        if not self._rail_names:
            return True
        return bool(getattr(self.solution, "solver_info", {}).get("stub"))

    def _rebuild_layer_rail_lists(self, *, preserve_visibility: bool = True) -> None:
        """Refresh the physical-layer and rail eye lists after a new solve.

        Preserves per-layer / per-rail visibility where the name still
        exists so a re-solve doesn't reset the user's layer toggles."""
        saved_layers: dict[str, bool] = {}
        saved_layers2: dict[str, bool] = {}
        saved_rails: dict[str, bool] = {}
        saved_subnets: dict[tuple[str, str], bool] = {}
        saved_expanded: dict[str, bool] = {}
        saved_subnet_expanded: dict[tuple[str, str], bool] = {}
        if preserve_visibility:
            saved_layers = {
                p: e.isVisibleState() for p, e in self._layer_eye_buttons
            }
            saved_layers2 = {
                p: e.isVisibleState() for p, e in self._layer_eye2_buttons
            }
            saved_rails = {
                r: e.isVisibleState() for r, e in self._rail_eye_buttons
            }
            for rail, nets in getattr(self, "_subnet_eye_buttons", {}).items():
                for net, eye in nets.items():
                    saved_subnets[(rail, net)] = eye.isVisibleState()
            saved_expanded = dict(getattr(self, "_rail_expanded", {}))
            saved_subnet_expanded = dict(
                getattr(self, "_subnet_node_expanded", {}),
            )

        while self.layer_list.count() > 1:
            self.layer_list.takeItem(self.layer_list.count() - 1)
        self._layer_eye_buttons.clear()
        self._layer_eye2_buttons.clear()
        self._layer_fill_buttons.clear()
        self._layer_transparency_buttons.clear()
        self._layer_list_items.clear()
        self._selected_layer = None

        for phys in self._physicals:
            eye, eye2 = self._add_layer_row(phys)
            if preserve_visibility and phys in saved_layers:
                eye.setVisibleState(saved_layers[phys], emit=False)
            if preserve_visibility and phys in saved_layers2:
                eye2.setVisibleState(saved_layers2[phys], emit=False)

        self._ensure_default_copper_visibility()
        self._sync_all_layers_eye()
        self._sync_all_layers_eye2()
        approx_row_h = self.layer_list.sizeHintForRow(0) or 22
        self.layer_list.setFixedHeight(
            (len(self._physicals) + 1) * approx_row_h + 6,
        )

        self._populate_rail_list(
            preserve_visibility=preserve_visibility,
            saved_rails=saved_rails,
            saved_subnets=saved_subnets,
            saved_expanded=saved_expanded,
            saved_subnet_expanded=saved_subnet_expanded,
        )

        has_rails = bool(self._rails)
        self._mode_label.setVisible(has_rails)
        self.mode_combo.setVisible(has_rails)
        # __init__ hides the colour-scale controls + bottom-left gradient
        # strip for a rail-less stub; a stub → solved transition must bring
        # them back so the user can read / adjust the heatmap scale.
        self.scale_controller.setVisible(has_rails)
        self._scale_overlay.setVisible(has_rails)
        if has_rails:
            self._position_scale_overlay()

        if (self.metadata or {}).get("mesh_failures"):
            _activate_mesh_failure_layer(self)

    def _apply_solve_result(
        self,
        new_solution,
        metadata: dict,
        loaded_project,
        new_settings,
        warn_a: float,
        pct: float,
        *,
        mark_solved_since_save: bool = False,
        is_import: bool = False,
        project: object | None = None,
        project_path: object | None = None,
        status_done: str | None = None,
    ) -> bool:
        """Swap in a freshly-solved result without replacing the window.

        Rebuilds solution-derived indices, clears render caches, refreshes
        the layer / rail lists and Setup tab, then re-renders in place —
        preserving the user's pan / zoom.

        ``is_import`` marks the File > Import Altium Design path, which can
        load a *different* project into this window: it resets the previous
        project's editor session, refreshes the title, and re-fits the view
        instead of inheriting the old pan / zoom.

        When ``project`` / ``project_path`` are supplied (File > Open Project
        File), the viewer binds to that ``.fypa`` in place instead of opening
        a new window. Returns ``True`` when the in-place refresh succeeded."""
        log = logging.getLogger(__name__)
        try:
            self.solution = new_solution
            self.metadata = metadata
            if isinstance(self.metadata, dict):
                self.metadata.setdefault("primitives", {
                    "tracks": [], "arcs": [], "regions": [],
                    "shape_based_regions": [], "fills": [],
                })
            self._loaded_project = loaded_project
            self._solve_settings = new_settings
            self._via_current_warn_a = warn_a
            self._display_percentile_high = pct
            self._has_adaptive_smps = _viewer_has_adaptive_smps(
                metadata, loaded_project,
            )

            # A returned stub (load-only import, or a project still missing
            # SOURCE/REGULATOR directives) hasn't been through the FEM yet —
            # keep the ↻ Solve overlay up rather than clearing it. Derive the
            # state from the solution so every caller stays consistent instead
            # of each one having to remember _mark_awaiting_first_solve().
            was_awaiting = self._awaiting_first_solve
            is_stub = bool(
                getattr(new_solution, "solver_info", {}).get("stub")
            )
            self._awaiting_first_solve = is_stub
            self._initial_solve_stale = is_stub
            self._project_dirty = False
            self._settings_dirty = False
            self._solve_stale = is_stub
            if mark_solved_since_save:
                self._solved_since_save = True

            project_swap = is_import or project is not None
            if project_swap:
                if self._editor_mode:
                    self._on_editor_mode_toggled(False)
                if project is not None:
                    self._project = project
                    self._project_path = project_path
                    self._loaded_project = None
                    self._solved_since_save = False
                    self._display_dirty = False
                    self._init_overlay_state()
                    self._load_sidebar_width_from_project()
                    title = (
                        Path(project_path).stem if project_path is not None
                        else getattr(new_solution.problem, "project_name", None)
                        or "unknown"
                    )
                    if getattr(project, "editor_directives", None):
                        self._initial_solve_stale = True
                else:
                    # Importing a different project must not leave the previous
                    # design's editor session / path bound to the new solution,
                    # or its name in the title bar. _project is rebuilt lazily
                    # from the new metadata when the user next edits.
                    self._project = None
                    self._project_path = None
                    self._display_dirty = False
                    self._init_overlay_state()
                    title = (
                        getattr(new_solution.problem, "project_name", None)
                        or "unknown"
                    )
                self.setWindowTitle(f"FYPA -- {title}")
                # New board → re-fit rather than inherit the old framing.
                self._need_initial_fit = True
                # The Overlays (Board Features) list is filtered by source kind
                # (Gerber imports hide silkscreen/pads/designator rows). An
                # import can swap the source kind of this window, so rebuild the
                # list — otherwise an Altium design imported into a Gerber-opened
                # viewer permanently hides those rows (and vice-versa leaves dead
                # rows). Safe to call synchronously; metadata is already set.
                self._build_overlay_list()

            self._init_solution_indices()
            self._clear_solution_derived_caches()
            # Preserve the user's layer / rail toggles on a same-project
            # re-solve, but fall back to fresh defaults when importing a
            # different project or when leaving the unsolved-stub state
            # (whose rails were all hidden) so the solved rails actually show.
            preserve = (not project_swap and not (was_awaiting and not is_stub))
            self._rebuild_layer_rail_lists(preserve_visibility=preserve)

            if getattr(self, "setup_browser", None) is not None:
                self._refresh_setup_html()
            if getattr(self, "_topology_view", None) is not None:
                self._topology_populated = False
                if self.tabs.currentIndex() == getattr(
                        self, "_topology_tab_index", -1):
                    self._populate_topology()

            self._nodes_table_populated = False
            self._vias_table_populated = False
            self._vias_warn_init_scheduled = False
            self._nodes_warn_init_scheduled = False
            self._sync_placeholder_tabs()
            # bridge_candidates came in with the new metadata, so the table
            # and the tab's warning count are both stale.
            self._bridges_table_populated = False
            self._update_bridges_tab_title()
            # Capacitor rows derive from the (re)loaded extracted design +
            # directives — drop the identification and copper shapes with
            # them, and recompute on next tab activation.
            self._caps_table_populated = False
            self._invalidate_caps_cache(repopulate=False, heavy=True)
            self._caps_shapes_cache = None
            self._impedance_populated = False
            self._sync_footprint_convention_ui()
            if self._project is not None:
                self._update_pending_rails()

            cur_tab = self.tabs.currentIndex()
            heatmap_idx = getattr(self, "_heatmap_tab_index", 0)
            self.tabs.setCurrentIndex(heatmap_idx)

            self._settings_rerun_btn.setEnabled(True)
            reload_btn = getattr(self, "_settings_reload_design_btn", None)
            if reload_btn is not None:
                reload_btn.setEnabled(True)

            self._refresh_solve_stale_overlay()
            # For a re-solve / resolve _need_initial_fit stays False so pan /
            # zoom survive the swap; the import path above flips it back on so
            # a different board re-fits.
            self._render()

            _maybe_warn_needs_directives(self, new_solution)
            _maybe_show_annotation_errors(self, metadata)
            _maybe_show_mesh_failures(self, metadata)
            _maybe_warn_open_loop_rails(self, metadata)
            _maybe_warn_connectivity_breaks(self, metadata)

            if not getattr(self, "_vias_warn_init_scheduled", False):
                self._vias_warn_init_scheduled = True
                QTimer.singleShot(0, self._init_vias_warn_count)
            if not getattr(self, "_nodes_warn_init_scheduled", False):
                self._nodes_warn_init_scheduled = True
                QTimer.singleShot(0, self._init_nodes_warn_count)
            if cur_tab in (
                getattr(self, "_nodes_tab_index", -1),
                getattr(self, "_vias_tab_index", -1),
                getattr(self, "_caps_tab_index", -1),
            ):
                self._on_tabs_current_changed(cur_tab)

            if status_done is None:
                if project is not None:
                    status_done = "Project loaded."
                elif is_import:
                    status_done = "Import complete."
                else:
                    status_done = "Solve complete."
            self._settings_status_label.setText(
                f"<span style='color:{_T()['ok']};'>"
                f"{_esc(status_done)}"
                "</span>"
            )
            return True
        except Exception as e:
            log.exception("Failed to apply solve result in place")
            _t = _T()
            self._settings_status_label.setText(
                f"<span style='color:{_t['err']};'>Solve succeeded but the "
                f"viewer failed to refresh: {_esc(str(e))}</span>"
            )
            self._settings_rerun_btn.setEnabled(True)
            reload_btn = getattr(self, "_settings_reload_design_btn", None)
            if reload_btn is not None:
                reload_btn.setEnabled(True)
            QMessageBox.critical(
                self, "Couldn't refresh viewer",
                f"The solve finished but updating this window failed:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return False

    # --- Rail-group computation ---------------------------------------------

    def _compute_rail_groups(
        self, metadata: dict | None,
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Group nets into rails based on RESISTOR bridges.

        Walks the metadata's directive list:

        * **RESISTOR** directives bridge their two terminal nets → union them.
        * **SOURCE / SINK / REGULATOR** directives mark their terminal's
          *named* net (the ``PDN_*_NET`` value) as a "primary candidate" —
          any group containing a primary is a rail worth showing in the
          dropdown; groups that don't (signal nets, unused bridges) are
          dropped.

        The group's **display name** is a primary in it — i.e. a net a
        directive explicitly named, never a net that was only pulled into
        the group by a SERIES bridge. So a sink whose ``PDN_N_NET = GND``
        resolved (via the bridge) onto ``+DM_SW1`` still gives a rail named
        ``GND``, not ``+DM_SW1``. Returns
        ``(rail_names_sorted, {primary_name: [all member nets]})``.
        """
        from fypa.rail_groups import compute_rail_groups
        return compute_rail_groups(metadata)

    def _rail_tree_metadata(self) -> dict:
        """Metadata for :func:`build_rail_trees` on pending / editor rails."""
        from fypa.rail_groups import merge_rail_tree_metadata
        meta = self.metadata if isinstance(self.metadata, dict) else {}
        editor_series: list[tuple[str, str]] = []
        project = getattr(self, "_project", None)
        if project is not None:
            for ed in getattr(project, "editor_directives", []) or []:
                if (
                    getattr(ed, "role", None) == "SERIES"
                    and getattr(ed, "p_net", None)
                    and getattr(ed, "n_net", None)
                ):
                    editor_series.append((ed.p_net, ed.n_net))
        return merge_rail_tree_metadata(meta, editor_series)

    def _subnet_rows_for_rail(
        self,
        rail: str,
        members: list[str],
        trees: dict | None,
        *,
        node_expanded: dict | None = None,
    ) -> list[tuple[str, int, bool]]:
        """Visible subnet rows as ``(net, depth, has_children)``."""
        from fypa.rail_groups import visible_rail_tree_rows
        tree = (trees or {}).get(rail)
        return visible_rail_tree_rows(
            rail, members, tree, node_expanded=node_expanded,
        )

    def showEvent(self, event) -> None:
        """Apply the deferred ``showMaximized`` once Qt has actually shown the
        window. Doing this in ``__init__`` is unreliable on Windows — the
        platform window doesn't exist yet so the maximise request gets lost.
        """
        super().showEvent(event)
        if getattr(self, "_pending_maximize", False):
            self._pending_maximize = False
            self.showMaximized()
        # Authoritative one-time board fit. The GL resize from show /
        # maximise normally drives this via _on_gl_view_changed, but
        # schedule it here too (deferred a tick so the maximised geometry
        # is applied first) so designs that push no FEM mesh — gerber
        # imports — still frame on first show. _fit_board_to_canvas
        # self-guards on _need_initial_fit, so whichever path runs first
        # wins and the rest are cheap no-ops.
        if self._need_initial_fit:
            QTimer.singleShot(0, self._fit_board_to_canvas)
        # Once the viewer is visible, compute the Vias warning count so the
        # tab title shows "Vias ⚠ N" without the user having to click in.
        # Deferred via singleShot(0) so the first paint happens before this
        # ~0.3 s compute runs (it's still on the GUI thread, so it briefly
        # stutters mouse handling, but the window is already visible by
        # then). Guarded so it runs only once per viewer instance.
        if not getattr(self, "_vias_warn_init_scheduled", False):
            self._vias_warn_init_scheduled = True
            QTimer.singleShot(0, self._init_vias_warn_count)
        # Same pattern for Nodes — show "Nodes ⚠ N" when any sink with a
        # PDN_MIN_V annotation has a pin below its declared minimum.
        if not getattr(self, "_nodes_warn_init_scheduled", False):
            self._nodes_warn_init_scheduled = True
            QTimer.singleShot(0, self._init_nodes_warn_count)
