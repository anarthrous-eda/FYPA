"""Custom-OpenGL PDN solution viewer for FYPA.

This is the user-facing visualisation layer. The heatmap canvas is a
purpose-built :class:`gl_mesh_viewer.GLMeshViewer` (a ``QOpenGLWidget``
subclass) that renders the FEM triangle mesh directly on the GPU via
shaders — colours interpolate per-vertex through a 1-D LUT texture, and
pan / zoom are single matrix-uniform updates. No rasterise step, no
texture re-upload, always pixel-sharp at any zoom level.

Layout
------
* Side panel (left): **Physical layers** eye-icon list (Top/Bottom/...),
  **Rails** eye-icon list (active PDN nets only — tick one or more at once),
  **Mode** dropdown (Voltage / Voltage Drop / Current Density / Power Density),
  display checkboxes, summary stats, and a :class:`ScaleController`
  (colour scheme, linear/log, Min/Max entry boxes).
* Plot area (right): :class:`gl_mesh_viewer.GLMeshViewer` rendering the
  mesh natively in OpenGL plus QPainter overlays for the title chip,
  legend chip, and directive-pin markers, with the heatmap colour-scale
  strip (:class:`_GradientBar` — gradient, draggable Min/Max handles,
  value ticks) overlaid on the bottom-left corner. A probe label across
  the bottom shows x / y / value / net under cursor.

The viewer reads :class:`pdnsolver.solver.Solution` objects produced by
``FYPA.py solve``. Each padne ``Layer`` in the solution is expected
to be named ``"<physical>|<rail>"`` (the convention
:func:`altium_geometry.build_per_net_geometry_layers` follows). Layers
without a ``|`` still work — they appear with a ``(none)`` rail.
"""
from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple
import matplotlib
import matplotlib.cm as _mpl_cm
import matplotlib.colors
import numpy as np
import shapely.geometry as _sg
import shapely.prepared as _sp
from fypa import log_buffer
from fypa.rail_groups import resolve_rail_member_nets
from fypa.gl_mesh_viewer import (
    _install_default_surface_format,
    GLMeshViewer,
    LegendRow,
    MarkerGroup,
)
from fypa.spacemouse_nav import SpaceMouseController
from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QLocale,
    QMetaMethod,
    QObject,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QDoubleValidator,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from matplotlib.tri import Triangulation

from fypa.viewer.prefs import (
    _ADAPTIVE_REGULATOR_GAIN_QS_KEY,
    _apply_performance_prefs,
    _AUTO_SOLVE_IMPORT_QS_KEY,
    _FUSE_BACKEND_QS_KEY,
    _FUSE_BACKENDS,
    _FUSE_BACKENDS_UI,
    _MAX_RECENT_PROJECTS,
    _MESH_WORKERS_QS_KEY,
    _MINRES_BUDGET_QS_KEY,
    _normalize_recent_entry,
    _normalize_recent_path,
    _recent_entry_exists,
    _recent_entry_key,
    _RECENT_OPEN_CLEAN_QS_KEY,
    _RECENT_PROJECTS_QS_KEY,
    _recent_settings,
    _record_recent_altium_from_metadata,
    _remove_recent_project_entry,
    _save_recent_projects,
    _SOLVE_SUBPROCESS_QS_KEY,
    _SSAA_QS_KEY,
    _THEME_QS_APP,
    _THEME_QS_KEY,
    _THEME_QS_ORG,
    _VIA_NO_CURRENT_OPACITY_DEFAULT,
    _VIA_NO_CURRENT_OPACITY_QS_KEY,
    clear_recent_projects,
    load_adaptive_regulator_gain,
    load_auto_solve_on_import,
    load_fuse_backend,
    load_mesh_max_workers,
    load_minres_budget_s,
    load_recent_open_clean,
    load_recent_projects,
    load_solve_in_subprocess,
    load_supersampling_enabled,
    load_via_no_current_opacity,
    prune_missing_recent_projects,
    recent_project_label,
    recent_project_tooltip,
    record_recent_project,
    save_adaptive_regulator_gain,
    save_auto_solve_on_import,
    save_fuse_backend,
    save_mesh_max_workers,
    save_minres_budget_s,
    save_recent_open_clean,
    save_solve_in_subprocess,
    save_supersampling_enabled,
    save_via_no_current_opacity,
)
from fypa.viewer.assets import (
    _AUMID,
    _EDITMODE_ICON_CACHE,
    _EDITMODE_ICON_CHECKED_COLOR,
    _force_native_window_icon,
    _FYPA_FANGS_CACHE,
    _FYPA_FANGS_PATH_PNG,
    _FYPA_TEXT_CACHE,
    _FYPA_TEXT_PATH_PNG,
    _ICON_CACHE,
    _ICON_DIR,
    _ICON_PATH_ICO,
    _ICON_PATH_ICO_TITLE,
    _ICON_PATH_SVG,
    _load_app_icon,
    _load_editmode_icon,
    _load_fypa_fangs_pixmap,
    _load_fypa_text_pixmap,
    _set_window_aumid,
    _set_windows_app_user_model_id,
    _triangle_icon,
)
from fypa.viewer.theme import (
    _build_app_palette,
    _build_app_stylesheet,
    _current_theme_mode,
    _file_dialog_options,
    _native_file_dialog_on_theme,
    _os_color_scheme,
    _T,
    _THEME_PRESETS,
    apply_app_theme,
    current_theme,
    current_theme_mode,
    load_saved_theme_mode,
    save_theme_mode,
)
from fypa.viewer.net_names import _GROUND_NET_TOKENS, _looks_like_supply_net, _supply_net_key
from fypa.viewer.numeric import (
    _CAPACITANCE_INPUT_RE,
    _CAPACITANCE_UNIT_F,
    _CLocaleDoubleValidator,
    _numeric_validator,
    _NumericCellDelegate,
    _parse_capacitance_f,
    _parse_numeric_text,
    _SETTINGS_VALUE_DECIMALS,
)
from fypa.viewer.display import (
    _build_cmap_lut,
    _build_neutral_cmap_lut,
    _current_density_per_vertex,
    _CUSTOM_CMAP_ANCHORS,
    _DEFAULT_CMAP_NAME,
    _DISPLAY_PERCENTILE_HIGH,
    _face_to_vertex_average,
    _FILL_MODE_REPORT_LABELS,
    _HEATMAP_COLORMAPS,
    _LOG_ELIGIBLE_MODES,
    _LOG_SCALE_DECADES,
    _MODES,
    _power_density_per_vertex,
    _register_custom_colormaps,
    _SLIDER_CAP_PERCENTILE,
    _slider_data_max,
    _SPIKE_PRONE_MODES,
    _split_composite_name,
    _VIA_CURRENT_COPPER_RGBA,
    _VIA_CURRENT_MODE,
    _VIA_CURRENT_WARN_A,
    _VOLTAGE_DROP_MODE,
    _voltage_per_vertex,
)
from fypa.viewer.mesh_geometry import (
    _apply_alpha,
    _drill_footprint_xy,
    _extrude_to_prism,
    _FastTriSampler,
    _generate_disk_cap,
    _generate_via_cylinder,
    _generate_via_cylinder_gradient,
    _sample_cmap_lut,
    _shape_outline_segments,
    _triangulate_polygon_for_stub,
)
from fypa.viewer.overlays import (
    _EDITOR_MARKER_EDGE_W,
    _EDITOR_MARKER_HIT_PX,
    _EDITOR_MARKER_SELECT_SCALE,
    _EDITOR_OVERLAY_DIM_ALPHA,
    _EDITOR_OVERLAY_DIM_BG,
    _EDITOR_SCHDOC,
    _MARKER_LAYER_RING_W,
    _N_NET_MARKER_EDGE,
    _N_SIDE_TERMINALS,
    _overlay_box_ring,
    _overlay_cap_arc,
    _overlay_circle_ring,
    _OVERLAY_CIRCLE_SEGMENTS,
    _OVERLAY_DEFAULT_BOTTOM_COLORS,
    _OVERLAY_DEFAULT_COLORS,
    _OVERLAY_DEFAULT_SOLID,
    _overlay_default_solid,
    _overlay_fan_tris,
    _overlay_hole_ring,
    _OVERLAY_LAYERS,
    _overlay_obround_ring,
    _overlay_outline_tris,
    _overlay_rect_ring,
    _overlay_ribbon_offsets,
    _overlay_ribbon_outline_tris,
    _OVERLAY_WIRE_HALF_MM,
    _OVERLAY_WIREMESH_BY_DEFAULT,
    _rec_slot_tuple,
)
from fypa.viewer.widgets import (
    _ClickAbsorbingPanel,
    _contrasting_text_color,
    _esc,
    _eye_icon_color,
    _EYE_PARTIAL_BLEND,
    _eye_pixmap,
    _EYE_PIXMAP_CACHE,
    _fill_pixmap,
    _FILL_PIXMAP_CACHE,
    _floating_tooltip_qss,
    _focus_pixmap,
    _FOCUS_PIXMAP_CACHE,
    _make_eye_pixmap,
    _make_fill_pixmap,
    _make_floating_tooltip,
    _make_focus_pixmap,
    _make_outline_pixmap,
    _make_split_pixmap,
    _make_swatch_pixmap,
    _make_transparency_pixmap,
    _move_tooltip_to_cursor,
    _outline_pixmap,
    _OUTLINE_PIXMAP_CACHE,
    _OVERLAY_SWATCH_PX,
    _qt_widget_alive,
    _split_pixmap,
    _SPLIT_PIXMAP_CACHE,
    _transparency_alpha,
    _TRANSPARENCY_COARSE_DELTA,
    _TRANSPARENCY_FINE_DELTA,
    _transparency_percent,
    _transparency_pixmap,
    _TRANSPARENCY_PIXMAP_CACHE,
    _TRANSPARENCY_STEPS,
    CURSOR_TOOLTIP_THROTTLE_S,
    EyeButton,
    FillToggleButton,
    HOVER_THROTTLE_S,
    OutlineToggleButton,
    OverlayColorButton,
    SidebarToggleButton,
    SplitButton,
    TransparencyButton,
)
from fypa.viewer.scale_controls import _GradientBar, ScaleController
from fypa.viewer.session import (
    _any_gerber_import_running,
    _any_solve_running,
    _BACKGROUND_LOADERS,
    _BackgroundLoadWorker,
    _clear_pending_altium_recent,
    _confirm_replace_project,
    _drop_failed_fypa_recent_entry,
    _drop_pending_altium_recent,
    _LIVE_VIEWERS,
    _metadata_has_adaptive_smps,
    _ORPHANED_THREADS,
    _register_viewer,
    _reject_if_background_load_running,
    _reject_if_solve_running,
    _restore_bundled_design_info,
    _retire_thread,
    _retire_viewer,
    _run_background_load,
    _stash_pending_altium_recent,
    _viewer_has_adaptive_smps,
    _viewer_has_unsaved_changes,
)
from fypa.viewer.solve_worker import (
    _abort_solve_worker,
    _build_stub_lean_solution_from_loaded,
    _cache_serves_adaptive_request,
    _CapLoopWorker,
    _choose_pcbdoc,
    _SolveProgressUpdater,
    _SolveWorker,
    _StageTimer,
    _try_solve_cache,
)
from fypa.viewer.diagnostics import (
    _activate_mesh_failure_layer,
    _apply_mesh_failure_highlights,
    _maybe_show_annotation_errors,
    _maybe_show_mesh_failures,
    _maybe_warn_connectivity_breaks,
    _maybe_warn_needs_directives,
    _maybe_warn_open_loop_rails,
    _maybe_warn_unannotated_bridges,
    _MESH_FAILURE_LOCAL_OUTLINE_MAX_SPAN_MM,
    _MESH_FAILURE_MARKER_RADIUS_MM,
    _mesh_failure_marker_xy,
    _mesh_failure_outline_rings,
    _MESH_FAILURE_RING_RADIUS_MM,
    _phys_name_for_layer_id,
    _primary_mesh_failure_layer_id,
)
from fypa.viewer.tabs.help import _HELP_SECTIONS, _help_tab_html, _help_tab_style
from fypa.viewer.tabs.messages import _MessagesSortItem, _MessagesTabMixin
from fypa.viewer.tabs.topology import (
    _TOPOLOGY_MAX_SCALE,
    _TOPOLOGY_MIN_SCALE,
    _TOPOLOGY_PAN_STEP,
    _TOPOLOGY_ZOOM_STEP,
    _TopologyTabMixin,
    _TopologyView,
)
from fypa.viewer.tabs.capacitors import (
    _CapacitorsTabMixin,
    _CAPS_COL,
    _CAPS_TABLE_COLUMNS,
    _CAPS_TABLE_ROW_ROLE,
    _PKG_CANONICAL_ROLE,
)
from fypa.viewer.tabs.impedance import (
    _branch_colors,
    _IMP_MAX_LEGEND_BRANCHES,
    _ImpBranch,
    _ImpedanceTabMixin,
)
from fypa.viewer.tabs.setup import _format_setup_html, _SetupTabMixin
from fypa.viewer.tabs.nodes import _NodesTabMixin
from fypa.viewer.tabs.vias import _ViasTabMixin
from fypa.viewer.tabs.bridges import _BridgesTabMixin
from fypa.viewer.editor.nets import _EditorNetsMixin, _NET_TABLE_ROW_ROLE
from fypa.viewer.editor.mode import _EditorModeMixin
from fypa.viewer.editor.attached import _AttachedPdnMixin
from fypa.viewer.editor.selection import _EditorSelectionMixin
from fypa.viewer.editor.form import _EditorFormMixin
from fypa.viewer.editor.marquee import _MarqueeMixin
from fypa.viewer.editor.pending import _PendingRailsMixin
from fypa.viewer.panels.layers import _LayerPanelMixin
from fypa.viewer.panels.overlays import _OverlayPanelMixin
from fypa.viewer.render import _RenderMixin
from fypa.viewer.markers import _MarkerOverlayMixin
from fypa.viewer.vias import _ViaRenderMixin
from fypa.viewer.viewport import _ViewportMixin
from fypa.viewer.copper_pick import _CopperPickMixin
from fypa.viewer.probes import _ProbeMixin
from fypa.viewer.file_menu import _FileMenuMixin, _ProjectSaveDialog
from fypa.viewer.ui_build import _UiBuildMixin
from fypa.viewer.settings_tab import _SettingsTabMixin
from fypa.viewer.window import PdnViewer
from fypa.viewer.project_open import (
    _altium_import_auto_solve_status_tip,
    _altium_import_clean_status_tip,
    _altium_import_worker_options,
    _consume_cli_adaptive_flag,
    _headless_platform,
    _open_altium_project_at,
    _open_project_file_at,
    _open_recent_project,
    _open_solution_at,
    _resolve_project_solution_path,
    _resync_settings_fields,
    _schedule_cli_altium_import,
    _start_launcher_altium_solve,
)
from fypa.viewer.gerber_import import (
    _finish_gerber_import,
    _GerberImportCancelled,
    _GerberImportWorker,
    _perform_gerber_import,
    _pick_gerber_inputs,
)
from fypa.viewer.app_menus import (
    _build_help_menu,
    _build_recent_projects_menu,
    _GITHUB_URL,
    _open_log_file,
    _show_about_dialog,
)
from fypa.viewer.launcher import LauncherWindow
from fypa.viewer.app import main
