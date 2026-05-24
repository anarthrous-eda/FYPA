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
import time
from pathlib import Path

import matplotlib
import matplotlib.cm as _mpl_cm
import numpy as np
import shapely.geometry as _sg
import shapely.prepared as _sp

from fypa import log_buffer
from fypa.gl_mesh_viewer import (
    GLMeshViewer,
    LegendRow,
    MarkerGroup,
    _install_default_surface_format,
)


# Application icon — shown in the title bar AND Windows taskbar.
#
# Two source files live in ``assets/``: ``icon.svg`` (master, scalable)
# and ``icon.ico`` (pre-rendered multi-resolution Windows-native, built
# from the SVG by ``tools/build_icon_ico.py``). We prefer the .ico on
# Windows because Qt's setWindowIcon → WM_SETICON path uses native
# Windows icon resources to populate the taskbar; an SVG-derived
# QPixmap doesn't always make that round-trip reliably.
# assets/ sits at the repo root — and at the bundle root when frozen by
# PyInstaller (the spec maps it there) — i.e. one level up from this fypa/
# package, hence ``.parent.parent``.
_ICON_DIR: Path = Path(__file__).resolve().parent.parent / "assets"
_ICON_PATH_ICO: Path = _ICON_DIR / "icon.ico"
_ICON_PATH_SVG: Path = _ICON_DIR / "icon.svg"
# Title-bar variant — text-only "FYPA" wordmark. Used as ICON_SMALL so the
# title bar shows the wordmark while the taskbar (ICON_BIG) keeps the
# full fang logo. Optional: if missing we fall back to icon.ico for both.
_ICON_PATH_ICO_TITLE: Path = _ICON_DIR / "icon_titlebar.ico"
# Cache the multi-resolution QIcon once we've built it.
_ICON_CACHE: QIcon | None = None


def _load_app_icon() -> QIcon | None:
    """Return a multi-resolution :class:`QIcon` for use in
    ``QApplication.setWindowIcon`` / ``QMainWindow.setWindowIcon``.

    Prefers ``assets/icon_titlebar.ico`` (text-only "FYPA" wordmark) so
    that QDialog / QMessageBox popups — which aren't routed through
    :func:`_force_native_window_icon` — inherit the wordmark in their
    title bar instead of the fang logo. Falls back to ``icon.ico`` if
    the title-bar variant is missing, then to rendering ``icon.svg``
    directly when running an unbuilt working tree.
    """
    global _ICON_CACHE
    if _ICON_CACHE is not None:
        return _ICON_CACHE
    # Preferred: text-only wordmark — covers all dialogs and the main
    # window's Qt-side icon. Main windows additionally call
    # _force_native_window_icon to set ICON_BIG to the full fang logo.
    for candidate in (_ICON_PATH_ICO_TITLE, _ICON_PATH_ICO):
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                _ICON_CACHE = icon
                return icon
    # Fallback: render the SVG into a multi-resolution QIcon ourselves.
    if not _ICON_PATH_SVG.is_file():
        return None
    try:
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    renderer = QSvgRenderer(str(_ICON_PATH_SVG))
    if not renderer.isValid():
        return None
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pm)
    _ICON_CACHE = icon
    return icon


_EDITMODE_ICON_CACHE: dict[str, QIcon] = {}

# Tint for the editor-mode toggle icon when the button is CHECKED — the
# checked state shows a light accent background that washes a light glyph
# out, so the checked (On) state uses a near-black glyph instead.
_EDITMODE_ICON_CHECKED_COLOR = "#101010"


def _load_editmode_icon(color: str = "#d8dee9") -> QIcon | None:
    """Return the editor-mode toggle icon (``assets/icon_editmode.svg``).

    The icon carries two states: ``color`` tints the unchecked (Off) glyph
    so it reads against the dark / bluish viewport; the checked (On) glyph
    is tinted near-black so it stays legible against the light accent
    background the button shows while editor mode is active. Qt swaps the
    two automatically with the QToolButton's checked state. Cached per
    ``color``."""
    if color in _EDITMODE_ICON_CACHE:
        return _EDITMODE_ICON_CACHE[color]
    svg_path = _ICON_DIR / "icon_editmode.svg"
    if not svg_path.is_file():
        return None
    try:
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        return None
    def _tinted(size: int, tint: str) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        # Tint: paint the colour through the rendered glyph's alpha mask.
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(pm.rect(), QColor(tint))
        painter.end()
        return pm

    # Two states: ``color`` for the unchecked (Off) button on the dark
    # viewport; a near-black glyph for the checked (On) state, whose light
    # accent background would otherwise wash a light glyph out. Qt swaps
    # them automatically with the QToolButton's checked state.
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        icon.addPixmap(_tinted(size, color), QIcon.Normal, QIcon.Off)
        icon.addPixmap(_tinted(size, _EDITMODE_ICON_CHECKED_COLOR),
                       QIcon.Normal, QIcon.On)
    _EDITMODE_ICON_CACHE[color] = icon
    return icon


def _triangle_icon(color: str, up: bool = True) -> QIcon:
    """A small filled-triangle :class:`QIcon` — an up triangle for SOURCE,
    a down triangle for SINK — tinted to ``color`` to match the viewport's
    directive markers (see ``PdnViewer._ROLE_MARKER_STYLE``)."""
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QColor(color))
    p.setPen(QColor("#101010"))
    if up:
        pts = [QPointF(10, 3), QPointF(17.5, 16.5), QPointF(2.5, 16.5)]
    else:
        pts = [QPointF(2.5, 3.5), QPointF(17.5, 3.5), QPointF(10, 17)]
    p.drawPolygon(QPolygonF(pts))
    p.end()
    return QIcon(pm)


# FYPA branding assets — stacked above the H2 on the welcome window.
# Both are pre-rendered PNGs because Qt's QSvgRenderer can't handle the
# clipPaths in the master SVGs (the red/blue arrow triangles are
# supposed to be clipped to inside the FYPA letters). The PNGs are
# produced by tools/build_icon_ico.py via Inkscape.
_FYPA_FANGS_PATH_PNG: Path = _ICON_DIR / "fypa_fangs_only.png"
_FYPA_TEXT_PATH_PNG: Path = _ICON_DIR / "fypa_text_only_no_triangles.png"
_FYPA_FANGS_CACHE: dict[int, QPixmap] = {}
_FYPA_TEXT_CACHE: dict[int, QPixmap] = {}


def _load_fypa_fangs_pixmap(height: int) -> QPixmap | None:
    """Return ``assets/fypa_fangs_only.png`` scaled to ``height`` px,
    aspect-preserved. Cached by height. ``None`` if the file is missing.
    """
    cached = _FYPA_FANGS_CACHE.get(height)
    if cached is not None:
        return cached
    if not _FYPA_FANGS_PATH_PNG.is_file():
        return None
    pm = QPixmap(str(_FYPA_FANGS_PATH_PNG))
    if pm.isNull():
        return None
    pm = pm.scaledToHeight(height, Qt.SmoothTransformation)
    _FYPA_FANGS_CACHE[height] = pm
    return pm


def _load_fypa_text_pixmap(height: int) -> QPixmap | None:
    """Return ``assets/fypa_text_only_no_triangles.png`` scaled to
    ``height`` px, aspect-preserved. Cached by height. ``None`` if the
    file is missing.

    The PNG is the Inkscape-rendered, alpha-cropped form of
    ``fypa_text only_no_triangles.svg`` — see ``tools/build_icon_ico.py``.
    We don't render the SVG at runtime because Qt's QSvgRenderer
    silently drops clipPaths.
    """
    cached = _FYPA_TEXT_CACHE.get(height)
    if cached is not None:
        return cached
    if not _FYPA_TEXT_PATH_PNG.is_file():
        return None
    pm = QPixmap(str(_FYPA_TEXT_PATH_PNG))
    if pm.isNull():
        return None
    pm = pm.scaledToHeight(height, Qt.SmoothTransformation)
    _FYPA_TEXT_CACHE[height] = pm
    return pm


from PySide6.QtCore import (
    QEvent, QObject, QPointF, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal,
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
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from matplotlib.tri import Triangulation


# Install the default QSurfaceFormat (OpenGL 3.3 core, vsync on) BEFORE
# any QOpenGLWidget is constructed. Idempotent — calling it from this
# module-level scope ensures the format is set before the GLMeshViewer
# in the Heatmap tab gets created.
_install_default_surface_format()


# --- Theme (dark / light) --------------------------------------------------
#
# The viewer defaults to dark so it looks the same on every machine,
# regardless of the OS-level Qt palette. The Appearance group in the
# Settings tab lets the user toggle to a light palette; the choice is
# persisted via QSettings and re-applied on the next launch.
#
# Every styled widget pulls its colours from ``current_theme()`` via the
# ``_T()`` shortcut, so swapping the active theme just needs the active
# window to be rebuilt (handled by :meth:`PdnViewer._on_theme_changed`).

_THEME_PRESETS: dict[str, dict[str, str]] = {
    "dark": {
        # Core surfaces
        "bg":              "#2b2b2b",   # main panel background
        "bg_alt":          "#333333",   # alternating rows, secondary surface
        "bg_input":        "#1f1f1f",   # text inputs, code blocks
        "bg_hover":        "#3a3a3a",   # button/section-header hover
        "bg_hover_strong": "#4a4a4a",   # secondary hover
        "bg_selection":    "#4a6080",   # selected rows / focused selection
        "bg_header":       "#3a3a3a",   # table headers, group titles
        # Text
        "fg":              "#e6e6e6",   # primary text
        "fg_strong":       "#ffffff",   # emphatic text / headers
        "fg_muted":        "#b0b0b0",   # secondary text
        "fg_dim":          "#909090",   # tertiary/disabled-looking text
        "fg_hint":         "#888888",   # subtle hints / parentheticals
        "fg_label":        "#cccccc",   # body labels / intro paragraphs
        # Accents
        "accent":          "#b8d4ff",   # links / status messages / h3
        "accent_btn":      "#3a6080",   # primary button bg
        "accent_btn_hov":  "#4a80a0",   # primary button hover
        "code":            "#f0c674",   # inline <code> / code-like spans
        "warn":            "#ffb84d",   # warning text
        "err":             "#ff7070",   # error text
        "ok":              "#7fdc7f",   # success flash
        "warn_bg":         "#5a1a1a",   # warning cell background (table)
        "warn_fg":         "#ff9696",   # warning cell foreground (table)
        "pass_bg":         "#1a4a1a",   # pass cell background (table)
        "pass_fg":         "#96ff96",   # pass cell foreground (table)
        # Decoration
        "border":          "#555555",   # widget borders / table grid
        "gridline":        "#444444",   # subtle grid
        "dielectric":      "#9090c0",   # stackup dielectric label
        "dielectric_dim":  "#a0a0a0",
        "separator":       "#707070",
        "eye_open":        "#f0f0f0",
        "eye_closed":      "#7a7a7a",
    },
    "light": {
        # Core surfaces
        "bg":              "#f5f5f5",
        "bg_alt":          "#eeeeee",
        "bg_input":        "#ffffff",
        "bg_hover":        "#e0e0e0",
        "bg_hover_strong": "#cfcfcf",
        "bg_selection":    "#aac7ff",
        "bg_header":       "#dcdcdc",
        # Text
        "fg":              "#1d1d1d",
        "fg_strong":       "#000000",
        "fg_muted":        "#4a4a4a",
        "fg_dim":          "#6b6b6b",
        "fg_hint":         "#7a7a7a",
        "fg_label":        "#2d2d2d",
        # Accents
        "accent":          "#1a5fbf",
        "accent_btn":      "#3a80c0",
        "accent_btn_hov":  "#5aa0e0",
        "code":            "#8a5a00",
        "warn":            "#c47a00",
        "err":             "#c0392b",
        "ok":              "#2e8b57",
        "warn_bg":         "#ffd6d6",
        "warn_fg":         "#a31010",
        "pass_bg":         "#d6ffd6",
        "pass_fg":         "#106a10",
        # Decoration
        "border":          "#b8b8b8",
        "gridline":        "#cccccc",
        "dielectric":      "#5a5aa0",
        "dielectric_dim":  "#666688",
        "separator":       "#9a9a9a",
        "eye_open":        "#2d2d2d",
        "eye_closed":      "#9a9a9a",
    },
}

# QSettings keys. The org/app pair is also used by anything else in the
# project that wants a persistent preference.
_THEME_QS_ORG = "CopperTree"
_THEME_QS_APP = "FYPA"
_THEME_QS_KEY = "ui/theme"
# High-quality-zoom (supersampling / SSAA) preference for the heatmap
# canvas. Persisted so the choice survives a relaunch; defaults to on.
_SSAA_QS_KEY = "ui/supersampling"

_current_theme_mode: str = "dark"


def current_theme() -> dict[str, str]:
    """Return the active theme's colour-token dict."""
    return _THEME_PRESETS.get(_current_theme_mode, _THEME_PRESETS["dark"])


def _T() -> dict[str, str]:
    """Shortcut alias for :func:`current_theme` — handy in stylesheet f-strings."""
    return current_theme()


def current_theme_mode() -> str:
    return _current_theme_mode


def load_saved_theme_mode() -> str:
    """Read the persisted theme choice (defaults to ``"dark"``)."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        mode = qs.value(_THEME_QS_KEY, "dark")
        if isinstance(mode, str) and mode in _THEME_PRESETS:
            return mode
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read saved theme preference (%s); using default.", e,
        )
    return "dark"


def save_theme_mode(mode: str) -> None:
    """Persist the chosen theme so the next launch picks it up."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_THEME_QS_KEY, mode)
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist theme preference (%s); ignoring.", e,
        )


def load_supersampling_enabled() -> bool:
    """Read the persisted high-quality-zoom (SSAA) preference.

    Defaults to ``True`` — the supersampled canvas is the better
    experience and its cost is only paid while a viewer window is open.
    QSettings round-trips a bool as a native string on some backends, so
    accept ``"true"`` / ``1`` etc. as well as a real bool."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_SSAA_QS_KEY, True)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read saved supersampling preference (%s); using "
            "default.", e,
        )
    return True


def save_supersampling_enabled(enabled: bool) -> None:
    """Persist the high-quality-zoom (SSAA) preference for next launch."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_SSAA_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist supersampling preference (%s); ignoring.", e,
        )


def _build_app_palette(mode: str):
    """Build a QPalette matching the given theme. Used for native widgets
    (menubar, scrollbars, file dialogs, dropdowns) that read from the
    application palette rather than our QSS."""
    from PySide6.QtGui import QPalette, QColor
    t = _THEME_PRESETS[mode]
    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(t["bg"]))
    pal.setColor(QPalette.WindowText,      QColor(t["fg"]))
    pal.setColor(QPalette.Base,            QColor(t["bg_input"]))
    pal.setColor(QPalette.AlternateBase,   QColor(t["bg_alt"]))
    pal.setColor(QPalette.ToolTipBase,     QColor(t["bg"]))
    pal.setColor(QPalette.ToolTipText,     QColor(t["fg"]))
    pal.setColor(QPalette.Text,            QColor(t["fg"]))
    pal.setColor(QPalette.Button,          QColor(t["bg_hover"]))
    pal.setColor(QPalette.ButtonText,      QColor(t["fg"]))
    pal.setColor(QPalette.BrightText,      QColor(t["err"]))
    pal.setColor(QPalette.Highlight,       QColor(t["bg_selection"]))
    pal.setColor(QPalette.HighlightedText, QColor(t["fg_strong"]))
    pal.setColor(QPalette.Link,            QColor(t["accent"]))
    pal.setColor(QPalette.LinkVisited,     QColor(t["accent"]))
    # Disabled state — derived from the active colours so disabled
    # buttons / menu items stay readable in both themes.
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t["fg_dim"]))
    pal.setColor(QPalette.Disabled, QPalette.Text,       QColor(t["fg_dim"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t["fg_dim"]))
    return pal


def _build_app_stylesheet(mode: str) -> str:
    """Application-wide QSS, applied via ``app.setStyleSheet``. Targets
    widgets that aren't covered by an inline stylesheet — menubar,
    scrollbars, file dialogs, the central widget background — so they
    follow the active theme."""
    t = _THEME_PRESETS[mode]
    return (
        f"QMainWindow, QDialog, QWidget#qt_central_widget "
        f"{{ background-color: {t['bg']}; color: {t['fg']}; }}"
        f"QMenuBar {{ background-color: {t['bg']}; color: {t['fg']}; }}"
        f"QMenuBar::item {{ background-color: transparent; padding: 4px 10px; }}"
        f"QMenuBar::item:selected {{ background-color: {t['bg_hover']}; }}"
        f"QMenu {{ background-color: {t['bg']}; color: {t['fg']};"
        f"         border: 1px solid {t['border']}; }}"
        f"QMenu::item:selected {{ background-color: {t['bg_selection']};"
        f"                        color: {t['fg_strong']}; }}"
        f"QMenu::separator {{ height: 1px; background: {t['border']};"
        f"                    margin: 4px 6px; }}"
        f"QTabWidget::pane {{ border: 1px solid {t['border']};"
        f"                    background-color: {t['bg']}; }}"
        f"QTabBar::tab {{ background-color: {t['bg_alt']}; color: {t['fg']};"
        f"                padding: 6px 12px; border: 1px solid {t['border']};"
        f"                border-bottom: none; }}"
        f"QTabBar::tab:selected {{ background-color: {t['bg']};"
        f"                         color: {t['fg_strong']}; }}"
        f"QTabBar::tab:hover {{ background-color: {t['bg_hover']}; }}"
        f"QComboBox {{ background-color: {t['bg_input']}; color: {t['fg']};"
        f"             border: 1px solid {t['border']}; padding: 3px 6px; }}"
        f"QComboBox QAbstractItemView {{ background-color: {t['bg_input']};"
        f"                               color: {t['fg']};"
        f"                               selection-background-color: {t['bg_selection']};"
        f"                               selection-color: {t['fg_strong']}; }}"
        f"QCheckBox {{ color: {t['fg']}; }}"
        f"QGroupBox {{ color: {t['fg']}; }}"
        f"QSlider::groove:horizontal {{ background: {t['bg_input']};"
        f"                              height: 6px; border-radius: 3px; }}"
        f"QSlider::handle:horizontal {{ background: {t['accent_btn']};"
        f"                              width: 14px; margin: -5px 0;"
        f"                              border-radius: 7px; }}"
        f"QScrollBar:vertical {{ background: {t['bg']};"
        f"                       width: 12px; margin: 0; }}"
        f"QScrollBar::handle:vertical {{ background: {t['bg_hover']};"
        f"                               min-height: 24px; border-radius: 4px; }}"
        f"QScrollBar::handle:vertical:hover {{ background: {t['bg_hover_strong']}; }}"
        f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{"
        f"   height: 0; background: none; }}"
        f"QScrollBar:horizontal {{ background: {t['bg']};"
        f"                         height: 12px; margin: 0; }}"
        f"QScrollBar::handle:horizontal {{ background: {t['bg_hover']};"
        f"                                 min-width: 24px; border-radius: 4px; }}"
        f"QScrollBar::handle:horizontal:hover {{ background: {t['bg_hover_strong']}; }}"
        f"QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{"
        f"   width: 0; background: none; }}"
        f"QToolTip {{ background-color: {t['bg']}; color: {t['fg']};"
        f"            border: 1px solid {t['border']}; padding: 3px 6px; }}"
    )


def apply_app_theme(app, mode: str | None = None) -> None:
    """Apply ``mode`` (or the currently-active theme) to ``app``.

    Sets the global QApplication style to Fusion (so the QPalette has
    consistent effect across platforms), installs the matching palette
    and our base stylesheet. Inline-styled widgets pick up the theme
    when their owning window is rebuilt."""
    global _current_theme_mode
    if mode is not None and mode in _THEME_PRESETS:
        _current_theme_mode = mode
    try:
        app.setStyle("Fusion")
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not set the Fusion style (%s); keeping the platform default.", e,
        )
    app.setPalette(_build_app_palette(_current_theme_mode))
    app.setStyleSheet(_build_app_stylesheet(_current_theme_mode))

# Hover-probe throttle. Mouse-move callbacks fire up to ~125 Hz on a
# high-DPI mouse; we don't need to recompute the probe more often than
# the eye can track. 33 ms ≈ 30 Hz feels live and removes a chunky
# fraction of mouse-event CPU during slow pans / hover-over moves.
HOVER_THROTTLE_S: float = 0.033
# Faster throttle used ONLY while the "Show cursor tooltip" option is
# on — the tooltip follows the cursor visibly, so the eye notices any
# stutter the bottom probe-label hides. Lower = smoother but more CPU
# spent on shapely.contains + _FastTriSampler query per move event.
# Set to 0.0 to disable throttling entirely (re-probe on every event).
CURSOR_TOOLTIP_THROTTLE_S: float = 0.008


# --- Altium-style "eye" visibility icons -----------------------------------

_EYE_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}


def _make_eye_pixmap(open_: bool, *, size: int = 16) -> QPixmap:
    """Draw an Altium-style eye icon.

    ``open_`` = True  → white eye (layer visible).
    ``open_`` = False → muted grey eye with a diagonal slash (layer hidden).
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    t = current_theme()
    color = QColor(t["eye_open"] if open_ else t["eye_closed"])
    pen = QPen(color)
    pen.setWidthF(max(1.0, size * 0.09))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    pad = size * 0.10
    cy = size / 2.0
    bulge = size * 0.42
    path = QPainterPath()
    path.moveTo(pad, cy)
    path.quadTo(size / 2.0, cy - bulge, size - pad, cy)
    path.quadTo(size / 2.0, cy + bulge, pad, cy)
    p.drawPath(path)

    p.setPen(Qt.NoPen)
    p.setBrush(color)
    r = size * 0.17
    p.drawEllipse(QPointF(size / 2.0, cy), r, r)

    if not open_:
        slash = QPen(color)
        slash.setWidthF(max(1.2, size * 0.11))
        slash.setCapStyle(Qt.RoundCap)
        p.setPen(slash)
        p.setBrush(Qt.NoBrush)
        m = size * 0.12
        p.drawLine(QPointF(m, size - m), QPointF(size - m, m))

    p.end()
    return px


def _eye_pixmap(open_: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), open_, size)
    cached = _EYE_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_eye_pixmap(open_, size=size)
        _EYE_PIXMAP_CACHE[key] = cached
    return cached


class EyeButton(QToolButton):
    """Altium-style eye-icon toggle for layer visibility."""

    toggled_visible = Signal(bool)

    def __init__(self, parent=None, *, visible: bool = True,
                 icon_size: int = 16,
                 tip_show: str = "Show layer",
                 tip_hide: str = "Hide layer") -> None:
        super().__init__(parent)
        self._visible = bool(visible)
        self._icon_size = icon_size
        self._tip_show = tip_show
        self._tip_hide = tip_hide
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isVisibleState(self) -> bool:
        return self._visible

    def setVisibleState(self, on: bool, *, emit: bool = True) -> None:
        on = bool(on)
        if on == self._visible:
            return
        self._visible = on
        self._apply_icon()
        if emit:
            self.toggled_visible.emit(self._visible)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_eye_pixmap(self._visible, self._icon_size)))
        self.setToolTip(self._tip_hide if self._visible else self._tip_show)

    def _on_clicked(self) -> None:
        self.setVisibleState(not self._visible)


# --- Wire-mesh / solid fill toggle icons -----------------------------------

_FILL_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}


def _make_fill_pixmap(solid: bool, *, size: int = 16) -> QPixmap:
    """Draw a fill-style icon for an overlay row.

    ``solid`` = True  → a solid-filled square (overlay drawn as solid fill).
    ``solid`` = False → an outlined square (overlay drawn as a wire mesh).
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    color = QColor(current_theme()["eye_open"])
    inset = size * 0.20
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    pen = QPen(color)
    pen.setWidthF(max(1.2, size * 0.11))
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(color if solid else Qt.NoBrush)
    p.drawRect(rect)

    p.end()
    return px


def _fill_pixmap(solid: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), solid, size)
    cached = _FILL_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_fill_pixmap(solid, size=size)
        _FILL_PIXMAP_CACHE[key] = cached
    return cached


class FillToggleButton(QToolButton):
    """Outline-vs-solid fill toggle for an overlay row.

    Mirrors :class:`EyeButton` — a small auto-raise tool button whose icon
    flips between an outlined square (wire mesh) and a solid square."""

    toggled_fill = Signal(bool)  # True == solid fill

    def __init__(self, parent=None, *, solid: bool = True,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._solid = bool(solid)
        self._icon_size = icon_size
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isSolid(self) -> bool:
        return self._solid

    def setSolid(self, on: bool, *, emit: bool = True) -> None:
        on = bool(on)
        if on == self._solid:
            return
        self._solid = on
        self._apply_icon()
        if emit:
            self.toggled_fill.emit(self._solid)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_fill_pixmap(self._solid, self._icon_size)))
        self.setToolTip(
            "Solid fill — click for wire mesh" if self._solid
            else "Wire mesh — click for solid fill"
        )

    def _on_clicked(self) -> None:
        self.setSolid(not self._solid)


# --- Layer-outline toggle icons --------------------------------------------

_OUTLINE_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}


def _make_outline_pixmap(on: bool, *, size: int = 16) -> QPixmap:
    """Draw a layer-outline toggle icon.

    ``on``  = True  → a muted square wrapped by a bright traced contour
                      (layer outlines shown).
    ``on``  = False → just the muted square with a faint dotted contour
                      (layer outlines hidden).
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    t = current_theme()
    body = QColor(t["eye_closed"])
    trace = QColor(t["eye_open"] if on else t["eye_closed"])

    # Inner filled square — the copper polygon being traced.
    inset = size * 0.32
    inner = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    p.setPen(Qt.NoPen)
    p.setBrush(body)
    p.drawRect(inner)

    # Outer square outline — the traced layer boundary.
    o = size * 0.14
    outer = QRectF(o, o, size - 2 * o, size - 2 * o)
    pen = QPen(trace)
    pen.setJoinStyle(Qt.MiterJoin)
    if on:
        pen.setWidthF(max(1.2, size * 0.11))
    else:
        pen.setWidthF(max(1.0, size * 0.075))
        pen.setStyle(Qt.DotLine)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRect(outer)

    p.end()
    return px


def _outline_pixmap(on: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), on, size)
    cached = _OUTLINE_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_outline_pixmap(on, size=size)
        _OUTLINE_PIXMAP_CACHE[key] = cached
    return cached


class OutlineToggleButton(QToolButton):
    """Layer-outline visibility toggle for the "All Rails" row.

    Mirrors :class:`FillToggleButton` — a small auto-raise tool button,
    the same size as the wire-mesh / solid toggle. Its icon flips between
    a bright traced contour (outlines shown) and a faint dotted one
    (outlines hidden). Drives the same state the old "Show layer
    outlines" checkbox used to. Lives on the rails row because only the
    currently-visible rails' copper gets traced."""

    toggled_outline = Signal(bool)  # True == layer outlines shown

    def __init__(self, parent=None, *, on: bool = False,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._on = bool(on)
        self._icon_size = icon_size
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isOn(self) -> bool:
        return self._on

    def setOn(self, on: bool, *, emit: bool = True) -> None:
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        self._apply_icon()
        if emit:
            self.toggled_outline.emit(self._on)

    def toggle(self) -> None:
        self.setOn(not self._on)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_outline_pixmap(self._on, self._icon_size)))
        self.setToolTip(
            "Layer outlines shown (O) — click to hide" if self._on
            else "Layer outlines hidden (O) — click to show"
        )

    def _on_clicked(self) -> None:
        self.setOn(not self._on)


# --- Split / merge toggle icons --------------------------------------------

_SPLIT_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}


def _make_split_pixmap(split: bool, *, size: int = 16) -> QPixmap:
    """Draw a split / merge icon for overlay rows with a top + bottom side.

    ``split`` = False → one square with a dashed mid-line ("click to split").
    ``split`` = True  → two stacked squares with a gap ("click to merge").
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    color = QColor(current_theme()["eye_open"])
    pen = QPen(color)
    pen.setWidthF(max(1.0, size * 0.09))
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    inset = size * 0.20
    w = size - 2 * inset
    if split:
        # Two separate halves with a clear gap — the result of a split.
        gap = size * 0.14
        h = (w - gap) / 2.0
        p.drawRect(QRectF(inset, inset, w, h))
        p.drawRect(QRectF(inset, inset + h + gap, w, h))
    else:
        # One box with a dashed line marking where it would be cut.
        p.drawRect(QRectF(inset, inset, w, w))
        dash = QPen(color)
        dash.setWidthF(max(1.0, size * 0.09))
        dash.setStyle(Qt.DashLine)
        p.setPen(dash)
        cy = size / 2.0
        p.drawLine(QPointF(inset, cy), QPointF(inset + w, cy))

    p.end()
    return px


def _split_pixmap(split: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), split, size)
    cached = _SPLIT_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_split_pixmap(split, size=size)
        _SPLIT_PIXMAP_CACHE[key] = cached
    return cached


class SplitButton(QToolButton):
    """Toggle that splits an overlay row into separate Top / Bottom rows."""

    toggled_split = Signal(bool)

    def __init__(self, parent=None, *, split: bool = False,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._split = bool(split)
        self._icon_size = icon_size
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isSplit(self) -> bool:
        return self._split

    def setSplit(self, on: bool, *, emit: bool = True) -> None:
        on = bool(on)
        if on == self._split:
            return
        self._split = on
        self._apply_icon()
        if emit:
            self.toggled_split.emit(self._split)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_split_pixmap(self._split, self._icon_size)))
        self.setToolTip(
            "Merge the Top / Bottom rows back into one" if self._split
            else "Split into separate Top / Bottom rows"
        )

    def _on_clicked(self) -> None:
        self.setSplit(not self._split)


# --- Transparency control --------------------------------------------------

# Nine-step transparency grid (12.5% per step): 0, 12.5, 25, 37.5, 50, 62.5,
# 75, 87.5, 100 %. Stored as an integer step count [0..8] on the button —
# the alpha applied to the row's geometry is (8 - step) / 8 (0%
# transparency == fully opaque == step 0). A plain click moves by the
# coarse delta (two steps == 25 %) and wraps around; an alt-click moves by
# the fine delta (one step == 12.5 %) and clamps at 0 / 100 %. Shift
# reverses the direction (alone or with alt).
_TRANSPARENCY_STEPS = 9
_TRANSPARENCY_COARSE_DELTA = 2
_TRANSPARENCY_FINE_DELTA = 1
_TRANSPARENCY_PIXMAP_CACHE: dict[tuple[str, int, int], QPixmap] = {}


def _make_transparency_pixmap(step: int, *, size: int = 16) -> QPixmap:
    """Pie-chart icon for the per-row transparency control.

    ``step`` is the number of eighth-slices removed from a white disc;
    coarse clicks remove two eighths (a quarter) at a time, alt-clicks
    remove one eighth (12.5 %). 0 = full disc (0% transparent), 8 = empty
    (100% transparent — only the faint reference outline remains so the
    button stays visible)."""
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    t = current_theme()
    # Faint reference circle so the button still reads as a control at
    # 100% transparency (when the white pie has fully vanished).
    inset = size * 0.16
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    ring = QPen(QColor(t["eye_closed"]))
    ring.setWidthF(max(1.0, size * 0.08))
    p.setPen(ring)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(rect)

    remaining = max(0, _TRANSPARENCY_STEPS - 1 - int(step))
    if remaining > 0:
        # Start the pie at 12 o'clock (90°) and sweep counter-clockwise.
        # As ``remaining`` decreases the trailing end retracts back toward
        # 12 along the clockwise direction, so each click visually removes
        # the next eighth slice clockwise (upper-right first, then sweeping
        # round to the upper-left as transparency rises).
        span_deg = remaining * (360.0 / (_TRANSPARENCY_STEPS - 1))
        path = QPainterPath()
        path.moveTo(rect.center())
        path.arcTo(rect, 90.0, float(span_deg))
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawPath(path)
        # Re-stroke the reference outline so the white pie doesn't bleed
        # over the edge after antialiasing.
        p.setPen(ring)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)

    p.end()
    return px


def _transparency_pixmap(step: int, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), int(step), size)
    cached = _TRANSPARENCY_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_transparency_pixmap(step, size=size)
        _TRANSPARENCY_PIXMAP_CACHE[key] = cached
    return cached


def _transparency_percent(step: int) -> float:
    """Percentage the icon currently advertises for a given step count."""
    return 100.0 * step / (_TRANSPARENCY_STEPS - 1)


def _transparency_alpha(step: int) -> float:
    """Alpha (0..1) that the row's geometry should be drawn at."""
    return (_TRANSPARENCY_STEPS - 1 - int(step)) / float(_TRANSPARENCY_STEPS - 1)


class TransparencyButton(QToolButton):
    """Cycles a row's transparency through 0..100 % in 12.5 % increments.

    Click behaviour
    ---------------
    * Plain click       — +25 % (coarse), wraps 100 % → 0 %.
    * Shift+click       — -25 % (coarse), wraps 0 % → 100 %.
    * Right-click       — -25 % (coarse), wraps 0 % → 100 %.
    * Alt+click         — +12.5 % (fine), clamped at 100 %.
    * Alt+Shift+click   — -12.5 % (fine), clamped at 0 %.
    * Alt+Right-click   — -12.5 % (fine), clamped at 0 %.
    """

    toggled_transparency = Signal(int)  # 0..(_TRANSPARENCY_STEPS-1)

    def __init__(self, parent=None, *, step: int = 0,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._step = int(step) % _TRANSPARENCY_STEPS
        self._icon_size = icon_size
        self._press_mods: Qt.KeyboardModifiers = Qt.NoModifier
        self._press_button: Qt.MouseButton = Qt.NoButton
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def step(self) -> int:
        return self._step

    def alpha(self) -> float:
        return _transparency_alpha(self._step)

    def setStep(self, step: int, *, emit: bool = True) -> None:
        s = int(step) % _TRANSPARENCY_STEPS
        if s == self._step:
            return
        self._step = s
        self._apply_icon()
        if emit:
            self.toggled_transparency.emit(self._step)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_transparency_pixmap(self._step, self._icon_size)))
        pct = _transparency_percent(self._step)
        # Drop the trailing zero on the 25 %-grid values so the common
        # multiples-of-25 read short; fine-grid (12.5 / 37.5 / ...) keeps
        # the half.
        pct_text = (f"{pct:.1f}".rstrip("0").rstrip(".")) + "%"
        self.setToolTip(
            f"Transparency: {pct_text}\n"
            "Click: +25 %    Shift+Click / Right-Click: -25 %    (wrap)\n"
            "Alt+Click: +12.5 %    Alt+Shift+Click / Alt+Right-Click: "
            "-12.5 %    (stop at 0 % / 100 %)"
        )

    def mousePressEvent(self, event) -> None:
        # Capture the modifiers at press time — QToolButton.clicked fires
        # on release, and QApplication.keyboardModifiers() at that moment
        # would race the user lifting Alt / Shift before the mouse button.
        self._press_mods = event.modifiers()
        self._press_button = event.button()
        if event.button() == Qt.RightButton:
            # QAbstractButton.clicked only fires for the left button, so
            # handle the right-button press directly without forwarding.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            mods = self._press_mods
            pressed_right = self._press_button == Qt.RightButton
            self._press_mods = Qt.NoModifier
            self._press_button = Qt.NoButton
            # Treat as a click only if the press also started on this
            # widget and the release is still inside its bounds.
            if pressed_right and self.rect().contains(event.position().toPoint()):
                self._apply_step_delta(reverse=True,
                                       fine=bool(mods & Qt.AltModifier))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _on_clicked(self) -> None:
        mods = self._press_mods
        self._press_mods = Qt.NoModifier
        self._press_button = Qt.NoButton
        self._apply_step_delta(reverse=bool(mods & Qt.ShiftModifier),
                               fine=bool(mods & Qt.AltModifier))

    def _apply_step_delta(self, *, reverse: bool, fine: bool) -> None:
        delta = (_TRANSPARENCY_FINE_DELTA if fine
                 else _TRANSPARENCY_COARSE_DELTA)
        if reverse:
            delta = -delta
        max_step = _TRANSPARENCY_STEPS - 1
        if fine:
            # Fine clicks stop at the boundaries so the user can hold Alt
            # and nudge precisely without overshooting past 0 % / 100 %.
            new_step = max(0, min(max_step, self._step + delta))
        else:
            # Coarse clicks "snap, then wrap": land on the boundary first,
            # and only the next click *past* the boundary jumps to the
            # opposite end. Preserves the original "click at 100 % returns
            # to 0 %" feel even when the current step is off the coarse
            # grid (e.g. after the user has nudged with Alt).
            if reverse:
                if self._step == 0:
                    new_step = max_step
                else:
                    new_step = max(0, self._step + delta)
            else:
                if self._step == max_step:
                    new_step = 0
                else:
                    new_step = min(max_step, self._step + delta)
        if new_step == self._step:
            return
        self.setStep(new_step)


# --- Board-feature colour swatch -------------------------------------------

# Side (px) of the colour swatch. Matches the physical-layer list swatch
# (the flat square in _build_layer_row) so the two controls look identical.
_OVERLAY_SWATCH_PX = 14


def _make_swatch_pixmap(rgb: tuple[float, float, float], *,
                        size: int = _OVERLAY_SWATCH_PX) -> QPixmap:
    """A flat, solid-filled colour square — drawn exactly like the physical-
    layer list's swatch (a plain ``QPixmap.fill``: no border, no rounding,
    no antialiasing) so a Board Features row reads identically."""
    px = QPixmap(size, size)
    px.fill(QColor.fromRgbF(*[min(1.0, max(0.0, c)) for c in rgb]))
    return px


class OverlayColorButton(QToolButton):
    """Colour-swatch button for a Board Features row.

    Looks identical to the physical-layer list's colour box — a flat solid
    square, borderless and the same size — but is clickable: clicking opens
    a colour picker; choosing a colour repaints the swatch and emits
    :attr:`colorChanged` so the viewer can recolour the overlay and mark
    the project dirty."""

    colorChanged = Signal(object)  # the new (r, g, b) tuple, channels 0..1

    def __init__(self, parent=None, *,
                 rgb: tuple[float, float, float] = (1.0, 1.0, 1.0),
                 size: int = _OVERLAY_SWATCH_PX) -> None:
        super().__init__(parent)
        self._rgb = tuple(float(c) for c in rgb)
        self._size = size
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setIconSize(QSize(size, size))
        self.setFixedSize(size, size)
        # Borderless / transparent so only the solid swatch shows — no
        # tool-button frame or hover tint, matching the plain QLabel swatch.
        self.setStyleSheet(
            "QToolButton { border: none; padding: 0px; margin: 0px;"
            " background: transparent; }"
        )
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def colorRgb(self) -> tuple[float, float, float]:
        return self._rgb

    def setColorRgb(self, rgb: tuple[float, float, float], *,
                    emit: bool = True) -> None:
        rgb = tuple(float(c) for c in rgb)
        if rgb == self._rgb:
            return
        self._rgb = rgb
        self._apply_icon()
        if emit:
            self.colorChanged.emit(self._rgb)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_make_swatch_pixmap(self._rgb, size=self._size)))
        self.setToolTip("Pick this overlay's colour")

    def _on_clicked(self) -> None:
        chosen = QColorDialog.getColor(
            QColor.fromRgbF(*[min(1.0, max(0.0, c)) for c in self._rgb]),
            self, "Overlay colour",
        )
        if chosen.isValid():
            self.setColorRgb((chosen.redF(), chosen.greenF(), chosen.blueF()))


# Overlay layers shown in the Heatmap tab's "Board Features" control, in
# top-to-bottom row order. Each is (key, label, has_sides). ``has_sides``
# rows (everything but vias) carry a split button that breaks the row into
# separate Top / Bottom rows. This list drives only the UI row order — the
# draw order is fixed independently in _refresh_overlay_geometry.
_OVERLAY_LAYERS: list[tuple[str, str, bool]] = [
    ("components", "Components", True),
    ("pads", "Pads", True),
    # "silkscreen" key kept internally; label is "Overlay" to match
    # Altium's layer name (splits into Top Overlay / Bottom Overlay).
    ("silkscreen", "Overlay", True),
    ("designators", "Designators", True),
    ("vias", "Vias", False),
    # The mechanical board outline. It has no per-net association, so its
    # row carries only the "show everywhere" eye (no rails-only eye) and
    # no fill toggle — see _build_overlay_row_widget.
    ("board_outline", "Board outline", False),
]

# Fill style a freshly-built overlay row starts in (True == solid square,
# False == wire-mesh outline square). _OVERLAY_DEFAULT_SOLID is the
# fallback; keys in _OVERLAY_WIREMESH_BY_DEFAULT start as wire-mesh
# instead — Components defaults to wire-mesh so it reads as outlines and
# doesn't paint over the heatmap underneath.
_OVERLAY_DEFAULT_SOLID = True
_OVERLAY_WIREMESH_BY_DEFAULT: frozenset[str] = frozenset({"components"})


def _overlay_default_solid(key: str) -> bool:
    """Initial fill style for the Board Features row ``key`` — solid
    unless the key opts into wire-mesh via _OVERLAY_WIREMESH_BY_DEFAULT."""
    return key not in _OVERLAY_WIREMESH_BY_DEFAULT

# ── Board Features default colours ──────────────────────────────────────────
# Default RGB (each channel 0.0–1.0) every Board Features row is drawn in,
# picked to stand out against the viridis heatmap and from each other.
#
# This table is the single place to change a *default* — edit a tuple here
# and rebuild. At runtime each viewer copies it into ``self._overlay_colors``
# (the primary colour — used by a merged row and the Top side of a split
# one); the per-row colour-swatch button edits that copy. A split layer's
# Bottom row may be given its own colour, kept in ``self._overlay_bottom_
# colors`` (``None`` = follow the primary). Both are persisted to the .fypa
# project file (``viewer_settings["overlay_colors"]``) so a reopened project
# keeps its colours.
_OVERLAY_DEFAULT_COLORS: dict[str, tuple[float, float, float]] = {
    "silkscreen":    (1.00, 1.00, 0.00),   # yellow (#ffff00)
    "pads":          (1.00, 0.82, 0.29),   # amber
    "vias":          (1.00, 0.55, 0.23),   # orange (matches via cylinders)
    "components":    (0.00, 0.50, 0.00),   # green (#008000)
    "designators":   (1.00, 1.00, 0.00),   # yellow (#ffff00)
    "board_outline": (0.50, 0.00, 0.00),   # dark red / maroon (#800000)
}

# Default Bottom-side colour for a Board Features layer. Applied to the
# layer's bottom side — both the Bottom row of a split layer and the
# bottom-side geometry of a merged row — so the two board faces stay
# visually distinct. A layer absent here defaults to None: its Bottom
# side simply follows the primary colour above. The colour-swatch button
# overrides this per project.
_OVERLAY_DEFAULT_BOTTOM_COLORS: dict[str, tuple[float, float, float]] = {
    "silkscreen": (0.50, 0.50, 0.00),   # olive (#808000)
}

# World-space half-width (mm) of an overlay wire-mesh outline ribbon. The
# Overlays control renders every outline as a thin filled ribbon (rather
# than GL_LINES) so wire-mesh and solid fill share one GL triangle batch.
# Coder-tunable — raise for a heavier wire-mesh, lower for a finer one.
_OVERLAY_WIRE_HALF_MM = 0.025

# Segments a via circle is tessellated into.
_OVERLAY_CIRCLE_SEGMENTS = 20

# Editor-mode connectivity dimming for the all-copper overlay. When a
# selection highlight is active, copper whose net is NOT connected to the
# selection recedes. The overlay-fill batch has no per-vertex alpha (only
# the heatmap mesh does — see _editor_alpha_array), so we fake the same
# "0.1 alpha over background" look by pre-blending the colour toward the
# editor-mode clear colour. Keep _EDITOR_OVERLAY_DIM_BG in sync with
# gl_mesh_viewer._EDITOR_BG_HEX.
_EDITOR_OVERLAY_DIM_BG = (0x27 / 255.0, 0x27 / 255.0, 0x35 / 255.0)
_EDITOR_OVERLAY_DIM_ALPHA = 0.10

# Click hit-radius (screen pixels) for picking a placed free source /
# sink marker in editor mode. Converted to world mm with the current
# view zoom so it tracks the on-screen marker size at any scale.
_EDITOR_MARKER_HIT_PX = 14.0

# Glyph size + outline-width multiplier for the editor marker matching
# the current selection — it draws this much larger / thicker than the
# unselected source / sink markers.
_EDITOR_MARKER_SELECT_SCALE = 1.3

# Base outline width (px) of an editor source / sink marker. Wide enough
# that the P-side black / N-side green edge reads clearly against the
# marker fill; the selected marker scales this by _EDITOR_MARKER_SELECT_
# SCALE, and the yellow selection box reuses the selected width.
_EDITOR_MARKER_EDGE_W = 2.8

# Directive terminals carrying the return ("N-side") net. Their pin
# markers are drawn with a green outline (vs black for the P side) and
# skip the rail-visibility filter, so an N net — typically ground, and
# rarely the rail being viewed — always shows its source / sink marker.
_N_SIDE_TERMINALS = frozenset({"N", "OUT_N", "IN_N"})

# Outline colour for an N-side directive marker — green, to set the
# return-net markers apart from the black-outlined P-side ones.
_N_NET_MARKER_EDGE = "#008000"


def _overlay_ribbon_offsets(polyline, half_w: float, *,
                            closed: bool):
    """Mitered outer / inner edge points of a constant-width ribbon
    centred on ``polyline``.

    Returns ``(pts, outer, inner)`` — the cleaned centre-line vertices and
    the two offset edges, each a float64 ``(n, 2)`` array — or ``None`` when
    the polyline is too short to form a ribbon. The offset at each vertex
    bisects its two adjacent edge normals, so the band keeps a clean
    constant width even when the polyline has many short segments. A naive
    per-segment rectangle ribbon splays those into a radial sunburst once
    the width exceeds the segment length (e.g. a tessellated circular pad).
    ``closed`` joins the last vertex back to the first.

    Shared by :func:`_overlay_outline_tris` (which fills the band) and
    :func:`_overlay_ribbon_outline_tris` (which traces its perimeter for
    the wire-mesh fill style)."""
    pts = np.asarray(polyline, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2:
        return None
    if closed and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    n = pts.shape[0]
    if n < 2:
        return None
    half_w = max(float(half_w), 1e-4)

    # Outgoing-edge unit direction + right-hand normal at each vertex.
    if closed:
        edges = np.roll(pts, -1, axis=0) - pts
    else:
        edges = np.empty_like(pts)
        edges[:-1] = pts[1:] - pts[:-1]
        edges[-1] = edges[-2]          # last vertex reuses the final edge
    elen = np.linalg.norm(edges, axis=1)
    elen[elen == 0.0] = 1.0
    edir = edges / elen[:, None]
    enorm = np.column_stack([edir[:, 1], -edir[:, 0]])

    # Per-vertex offset = bisector of the incoming + outgoing edge normals.
    prev_norm = np.roll(enorm, 1, axis=0)
    if not closed:
        prev_norm[0] = enorm[0]        # open start has no incoming edge
    bis = enorm + prev_norm
    blen = np.linalg.norm(bis, axis=1)
    flat = blen < 1e-9
    bis[flat] = enorm[flat]
    blen[flat] = 1.0
    bis /= blen[:, None]
    # Miter length = half_w / cos(theta/2); clamp so a sharp corner can't
    # spike the offset out toward infinity.
    dot = np.einsum("ij,ij->i", bis, enorm)
    dot = np.where(np.abs(dot) < 0.25,
                   np.where(dot < 0.0, -0.25, 0.25), dot)
    off = bis * (half_w / dot)[:, None]
    return pts, pts + off, pts - off


def _overlay_outline_tris(polyline, half_w: float, *,
                          closed: bool) -> np.ndarray:
    """Triangulate a thin, uniform-width outline ribbon along a polyline.

    Returns an ``(3k, 2)`` float32 array of triangle vertices filling the
    band between the polyline's two mitered offset edges (see
    :func:`_overlay_ribbon_offsets`). ``closed`` joins the last vertex back
    to the first (pad / via / component outlines); open polylines
    (silkscreen tracks) get square ends."""
    res = _overlay_ribbon_offsets(polyline, half_w, closed=closed)
    if res is None:
        return np.empty((0, 2), dtype=np.float32)
    pts, outer, inner = res
    n = pts.shape[0]
    seg = n if closed else n - 1
    cur = np.arange(seg)
    nxt = (cur + 1) % n
    out = np.empty((seg * 6, 2), dtype=np.float32)
    out[0::6] = outer[cur]
    out[1::6] = inner[cur]
    out[2::6] = outer[nxt]
    out[3::6] = outer[nxt]
    out[4::6] = inner[cur]
    out[5::6] = inner[nxt]
    return out


def _overlay_cap_arc(center, p_from, outward,
                     segments: int = 8) -> np.ndarray:
    """Interior points of a ribbon end's semicircular round cap.

    Sweeps a half-circle of radius ``|p_from - center|`` starting at
    ``p_from`` and bulging toward ``outward`` (the ribbon's travel
    direction at that end), ending at the antipode of ``p_from``. Returns
    ``segments - 1`` interior points as a float64 ``(k, 2)`` array — the
    two diametric endpoints are supplied by the caller from the outer /
    inner offset edges, so they are omitted to avoid duplicate vertices."""
    center = np.asarray(center, dtype=np.float64)
    v = np.asarray(p_from, dtype=np.float64) - center
    r = float(np.linalg.norm(v))
    if r < 1e-9 or segments < 2:
        return np.empty((0, 2), dtype=np.float64)
    a0 = math.atan2(v[1], v[0])
    # Rotating v by +90° gives (-v.y, v.x); pick the sweep sign whose
    # mid-arc direction points the same way as `outward`.
    rot90 = np.array([-v[1], v[0]], dtype=np.float64)
    sign = (1.0 if float(rot90 @ np.asarray(outward, dtype=np.float64)) >= 0.0
            else -1.0)
    k = np.arange(1, segments)
    ang = a0 + sign * math.pi * k / segments
    return np.column_stack([center[0] + r * np.cos(ang),
                            center[1] + r * np.sin(ang)])


def _overlay_ribbon_outline_tris(polyline, half_w: float, wire_w: float, *,
                                 closed: bool) -> np.ndarray:
    """Wire-mesh of a constant-width ribbon — its hollow perimeter.

    Traces the boundary of the band :func:`_overlay_outline_tris` would
    fill and renders that boundary as its own thin ribbon of half-width
    ``wire_w``. ``half_w`` is the source ribbon's half-width. Used by the
    Overlay and Designators rows' wire-mesh fill style, whose geometry is
    open stroke polylines rather than closed shapes.

    For an open polyline the perimeter is one closed loop: out along the
    outer edge, a round end cap, back along the inner edge, a round start
    cap. For a closed polyline it is two concentric loops (the band's
    outer and inner rims)."""
    res = _overlay_ribbon_offsets(polyline, half_w, closed=closed)
    if res is None:
        return np.empty((0, 2), dtype=np.float32)
    pts, outer, inner = res
    if closed:
        return np.concatenate([
            _overlay_outline_tris(outer, wire_w, closed=True),
            _overlay_outline_tris(inner, wire_w, closed=True),
        ], axis=0)
    # Open ribbon: round end + start caps bridge the outer and inner edges
    # into a single closed perimeter (matching the round caps the solid
    # fill style draws as separate circles). At an open end the offset is
    # exactly ±half_w along the edge normal, so outer[-1] / inner[-1] are
    # antipodal on the cap circle — the arc joins them cleanly.
    end_cap = _overlay_cap_arc(pts[-1], outer[-1], pts[-1] - pts[-2])
    start_cap = _overlay_cap_arc(pts[0], inner[0], pts[0] - pts[1])
    ring = np.concatenate([outer, end_cap, inner[::-1], start_cap], axis=0)
    return _overlay_outline_tris(ring, wire_w, closed=True)


def _overlay_fan_tris(ring) -> np.ndarray:
    """Triangulate a convex polygon ring as a centroid fan.

    Returns an ``(3k, 2)`` float32 array. A trailing closing vertex (last
    point equal to the first) is dropped. Pad / via / component-box rings
    are convex, so a centroid fan fills them exactly."""
    pts = np.asarray(ring, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 3:
        return np.empty((0, 2), dtype=np.float32)
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    n = pts.shape[0]
    if n < 3:
        return np.empty((0, 2), dtype=np.float32)
    cx, cy = pts.mean(axis=0)
    out: list[tuple[float, float]] = []
    for i in range(n):
        j = (i + 1) % n
        out += [(cx, cy), (pts[i, 0], pts[i, 1]), (pts[j, 0], pts[j, 1])]
    return np.asarray(out, dtype=np.float32)


def _overlay_circle_ring(cx: float, cy: float, r: float,
                         n: int = _OVERLAY_CIRCLE_SEGMENTS) -> np.ndarray:
    """A closed ``(n+1, 2)`` ring approximating a circle."""
    ang = np.linspace(0.0, 2.0 * math.pi, n + 1)
    return np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang)])


def _overlay_box_ring(x0: float, y0: float,
                      x1: float, y1: float) -> np.ndarray:
    """A closed ``(5, 2)`` ring tracing an axis-aligned rectangle."""
    return np.asarray([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]],
                      dtype=np.float64)


class SidebarToggleButton(QToolButton):
    """Slim vertical splitter handle that collapses / expands the heatmap
    side panel. Paints a crisp anti-aliased triangle pointing in the
    direction the panel will travel on the next click, instead of relying
    on Unicode arrow glyphs (which render fuzzy at this size on Windows).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._collapsed = False
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(14)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def setCollapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.update()

    def isCollapsed(self) -> bool:
        return self._collapsed

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        t = _T()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect()
        # Track background. underMouse() drives the hover tint; QToolButton's
        # default styled-look fights the custom paint, so we draw it ourselves.
        bg = QColor(t["bg_hover"] if self.underMouse() else t["bg_alt"])
        p.fillRect(rect, bg)
        p.setPen(QColor(t["border"]))
        p.drawLine(rect.topLeft(), rect.bottomLeft())
        p.drawLine(rect.topRight(), rect.bottomRight())
        # Triangle: points RIGHT (▶) when collapsed (click expands the panel
        # outward); points LEFT (◀) when expanded (click pulls it back in).
        cx = rect.width() / 2.0
        cy = rect.height() / 2.0
        half_w = 3.0
        half_h = 5.0
        if self._collapsed:
            tri = QPolygonF([
                QPointF(cx - half_w, cy - half_h),
                QPointF(cx - half_w, cy + half_h),
                QPointF(cx + half_w, cy),
            ])
        else:
            tri = QPolygonF([
                QPointF(cx + half_w, cy - half_h),
                QPointF(cx + half_w, cy + half_h),
                QPointF(cx - half_w, cy),
            ])
        fill = QColor(t["fg"] if self.underMouse() else t["fg_muted"])
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawPolygon(tri)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)


# Modes the viewer offers. Each is (label, unit, derive_fn). The derive_fn
# takes (tris, potentials, power_density, conductance, n_verts) and
# returns a numpy array of values per vertex.
#   tris            — (M, 3) int32, vertex indices into the mesh
#   potentials      — (N,) float64, per-vertex voltage (V)
#   power_density   — (M,) float64 or None, per-face power density (W/mm²)
#   conductance     — sheet conductance (S) of the layer
#   n_verts         — vertex count of the mesh (== potentials.size)
def _voltage_per_vertex(tris, potentials, power_density, conductance, n_verts):
    return potentials.copy()


def _power_density_per_vertex(tris, potentials, power_density, conductance, n_verts):
    if power_density is None:
        return np.zeros(n_verts)
    return _face_to_vertex_average(tris, power_density, n_verts)


def _current_density_per_vertex(tris, potentials, power_density, conductance, n_verts):
    # |J| = sqrt(power_density * sheet_conductance). power_density is W/mm^2;
    # sheet_conductance is S (= A/V); product is A^2/mm^2; sqrt → A/mm.
    p = _power_density_per_vertex(tris, potentials, power_density, conductance, n_verts)
    return np.sqrt(np.maximum(p * conductance, 0.0))


_MODES = [
    ("Voltage",         "V",     _voltage_per_vertex),
    ("Voltage Drop",    "V",     _voltage_per_vertex),  # values are shifted in _render
    ("Current Density", "A/mm",  _current_density_per_vertex),
    ("Power Density",   "W/mm^2", _power_density_per_vertex),
    # Via Current: copper renders neutral grey (the derive_fn output is
    # ignored — see :meth:`PdnViewer._render`), and the heatmap belongs
    # to the via cylinders / markers. Scale range = (min, max) of every
    # visible via's max-segment |I| on the selected rails.
    ("Via Current",     "A",     _voltage_per_vertex),
]

# Mode label that triggers the "shift values so the worst SINK on the rail
# reads 0 V" post-processing in _render. Kept as a constant so the same
# string is used in both the mode list and the special-case check.
_VOLTAGE_DROP_MODE: str = "Voltage Drop"

# Mode label that disables the per-vertex copper heatmap and uses via
# currents as the heatmapped quantity instead. Same string is used in
# the mode list and every special-case branch.
_VIA_CURRENT_MODE: str = "Via Current"

# Modes whose values blow up at point constraints (the FEM has a
# logarithmic gradient singularity at any pinned-voltage vertex such as a
# SOURCE/SINK pin). The default colour-scale clamp uses a percentile rather
# than the absolute max so the rest of the board isn't crushed to black.
# The full data range is still exposed on the scale controller so the user
# can drag/type up to the real max if they want to see the spike.
_SPIKE_PRONE_MODES: frozenset[str] = frozenset({"Current Density", "Power Density"})

# Heatmap modes whose data spans many decades, so a logarithmic colour
# scale earns its keep: current density (J spikes hard at copper
# constrictions), power density (~ J²·ρ — roughly the square of that
# dynamic range), and via current (long-tailed: most vias near 0 A, a
# handful near a regulator). The linear/log toggle is enabled only for
# these. Voltage sits in a narrow band and Voltage Drop is signed, so a
# log scale is useless or undefined for them.
_LOG_ELIGIBLE_MODES: frozenset[str] = frozenset(
    {"Current Density", "Power Density", _VIA_CURRENT_MODE}
)

# A log colour scale spans at most this many decades below the window
# maximum. Values under the resulting floor — including the exact zeros
# of no-current copper — clamp to the bottom of the LUT instead of
# diverging to log(0) = -inf.
_LOG_SCALE_DECADES: float = 6.0

# Per-via segment current that earns a warning highlight in the Vias tab.
# 1 A through a single 0.6 mm plated through-hole on 1 oz copper is roughly
# where IPC-2152 derating starts to bite (~30°C rise depending on plating
# thickness). Tune to taste.
_VIA_CURRENT_WARN_A: float = 1.0

# Percentile used to clip outliers in the default display range. P99 means
# the worst 1% of vertices are above the auto-clamp top. Most FEM spikes
# affect << 1% of vertices, so this gives a useful default scale on the
# rest of the board.
_DISPLAY_PERCENTILE_HIGH: float = 99.0

# Percentile used to cap the *slider's full extent* (data_max) on spike-prone
# modes. The raw vertex maximum can be 10–100× the physically meaningful range
# because of the FEM logarithmic gradient singularity at pinned-voltage
# vertices (SOURCE/SINK pins), which makes the slider's "Max" label
# misleading. P99.9 still sits well above the P99 default-selection clamp so
# users can drag up to inspect outliers, just not the singularity itself.
_SLIDER_CAP_PERCENTILE: float = 99.9


# Strong-ref list of every viewer window we hand off to the user. Without
# this, freshly-created QMainWindows can be garbage-collected the moment
# the spawning function returns — PySide6's ``QApplication.setProperty``
# does NOT hold a reliable Python reference across signal boundaries, so
# we anchor at module scope instead. Dead entries are pruned each append.
_LIVE_VIEWERS: list[QMainWindow] = []


def _register_viewer(win: QMainWindow) -> None:
    """Append ``win`` to the module-level strong-ref list, pruning any
    entries whose underlying QObject has already been destroyed."""
    global _LIVE_VIEWERS
    pruned: list[QMainWindow] = []
    for w in _LIVE_VIEWERS:
        try:
            w.objectName()      # raises if the C++ object is gone
        except RuntimeError:
            continue
        pruned.append(w)
    pruned.append(win)
    _LIVE_VIEWERS = pruned


def _retire_viewer(win: QMainWindow) -> None:
    """Close, unregister and destroy a viewer that's being replaced on a
    reload, releasing its Solution / mesh data right away.

    Without this every reload leaks: ``_register_viewer`` only prunes
    viewers whose C++ object is *already* destroyed, and a plain
    ``close()`` merely hides the window — so the previous PdnViewer stays
    pinned in ``_LIVE_VIEWERS`` with its full (gigabyte-scale, on a large
    board) ``Solution`` still in RAM. A long session then accumulates one
    Solution per load until the next solve runs short of memory and PARDISO
    pages to disk — which is what turns an ~8 s linear solve into ~50 s.
    """
    global _LIVE_VIEWERS
    _LIVE_VIEWERS = [w for w in _LIVE_VIEWERS if w is not win]
    try:
        win.close()
    except RuntimeError:
        return  # C++ object already gone — nothing left to release.
    # Drop the heavy payload explicitly so it's freed now, rather than
    # whenever the window wrapper happens to be garbage-collected.
    for attr in ("solution", "metadata"):
        try:
            setattr(win, attr, None)
        except Exception:
            pass
    win.deleteLater()


def _slider_data_max(vs_arr: np.ndarray, mode: str, raw_max: float) -> float:
    """Effective ``data_max`` for the scale-controller slider.

    On spike-prone modes (Current Density / Power Density), clip the raw
    maximum to P99.9 so the FEM singularity at pinned-voltage vertices
    doesn't blow up the slider's upper bound. On all other modes (and on
    very small meshes) returns ``raw_max`` unchanged.
    """
    if mode not in _SPIKE_PRONE_MODES or len(vs_arr) < 100:
        return raw_max
    capped = float(np.percentile(vs_arr, _SLIDER_CAP_PERCENTILE))
    return min(raw_max, capped) if math.isfinite(capped) else raw_max


def _face_to_vertex_average(tris: np.ndarray, face_values: np.ndarray,
                            n_verts: int) -> np.ndarray:
    """Average face-defined values onto vertices (each vertex gets the mean
    of the values of its incident faces). Vectorised via ``np.add.at`` —
    same result as a Python loop, ~50× faster on large meshes."""
    totals = np.zeros(n_verts, dtype=np.float64)
    counts = np.zeros(n_verts, dtype=np.float64)
    if tris.size == 0:
        return totals
    np.add.at(totals, tris[:, 0], face_values)
    np.add.at(totals, tris[:, 1], face_values)
    np.add.at(totals, tris[:, 2], face_values)
    np.add.at(counts, tris[:, 0], 1.0)
    np.add.at(counts, tris[:, 1], 1.0)
    np.add.at(counts, tris[:, 2], 1.0)
    counts[counts == 0] = 1.0
    return totals / counts


def _split_composite_name(name: str) -> tuple[str, str]:
    if "|" in name:
        phys, rail = name.split("|", 1)
        return phys, rail
    return name, ""


def _build_cmap_lut(name: str = "viridis") -> np.ndarray:
    """Sample a matplotlib colormap into a (256, 4) uint8 RGBA LUT for
    upload to the GLMeshViewer's 1-D texture."""
    cmap = _mpl_cm.get_cmap(name)
    samples = cmap(np.linspace(0.0, 1.0, 256))
    return (samples * 255.0).astype(np.uint8)


# Flat grey the copper renders as in Via Current mode. Kept dark so the
# orange / light-grey via cylinders (which own the heatmap in that mode)
# stand out clearly against the context copper. Tweak here to taste.
_VIA_CURRENT_COPPER_RGBA: tuple[int, int, int, int] = (110, 110, 110, 255)


def _build_neutral_cmap_lut(
    rgba: tuple[int, int, int, int] = _VIA_CURRENT_COPPER_RGBA,
) -> np.ndarray:
    """Flat-grey 256-entry RGBA LUT. Pushed to the GL viewer in
    Via Current mode so the copper renders as a single neutral shade
    regardless of the per-vertex values: the heatmap "belongs" to the
    via cylinders in that mode, and the copper is just context."""
    return np.tile(np.asarray(rgba, dtype=np.uint8), (256, 1))


# --- Heatmap colour schemes -------------------------------------------------
#
# The heatmap looks up per-vertex colours through a 256-entry RGBA LUT
# (see :func:`_build_cmap_lut`). The scheme is user-selectable from a
# dropdown next to the colour-scale bar — :data:`_HEATMAP_COLORMAPS` is the
# ordered ``(display name, matplotlib colormap name)`` menu; index 0 is the
# default applied on first render.
#
# "Acton" and "Bam" are Scientific Colour Maps (Crameri — the same schemes
# JuliaPlots ships). matplotlib doesn't bundle them, so
# :func:`_register_custom_colormaps` builds them from anchor colours and
# registers them under ``fypa_``-prefixed names that ``get_cmap`` resolves
# like any built-in. Everything downstream (LUT build, gradient strip)
# only ever sees a colormap name string, so custom and built-in schemes
# travel the exact same path.

# Anchor colours (evenly spaced, RGB 0..1) for the schemes matplotlib
# doesn't ship. Sampled to follow the published Scientific Colour Map ramps.
_CUSTOM_CMAP_ANCHORS: dict[str, list[tuple[float, float, float]]] = {
    # Sequential: dark indigo -> mauve -> light pink.
    "fypa_acton": [
        (0.176, 0.125, 0.302),
        (0.318, 0.235, 0.408),
        (0.486, 0.318, 0.494),
        (0.682, 0.404, 0.557),
        (0.812, 0.541, 0.643),
        (0.871, 0.671, 0.733),
        (0.902, 0.792, 0.804),
    ],
    # Diverging: magenta <-> green through a near-white centre.
    "fypa_bam": [
        (0.396, 0.078, 0.353),
        (0.620, 0.247, 0.541),
        (0.812, 0.510, 0.706),
        (0.937, 0.776, 0.886),
        (0.969, 0.969, 0.969),
        (0.737, 0.871, 0.722),
        (0.435, 0.682, 0.435),
        (0.184, 0.451, 0.243),
    ],
}


def _register_custom_colormaps() -> None:
    """Register the non-matplotlib colour schemes so :func:`get_cmap`
    resolves them by name like any built-in. ``force=True`` makes a
    repeat call (e.g. after a module reload) harmless."""
    from matplotlib.colors import LinearSegmentedColormap
    for name, anchors in _CUSTOM_CMAP_ANCHORS.items():
        cmap = LinearSegmentedColormap.from_list(name, anchors, N=256)
        matplotlib.colormaps.register(cmap, name=name, force=True)


_register_custom_colormaps()


# Ordered ``(display name, matplotlib colormap name)`` for the colour-scale
# dropdown. Index 0 is the default. To add a scheme, append a row — the
# combo, gradient strip and LUT build all read this tuple.
_HEATMAP_COLORMAPS: tuple[tuple[str, str], ...] = (
    ("Viridis (default)", "viridis"),
    ("Blue → Red",   "RdBu_r"),
    ("Heat",              "YlOrRd"),
    ("Bam",               "fypa_bam"),
    ("Acton",             "fypa_acton"),
    ("Inferno",           "inferno"),
    ("Turbo",             "turbo"),
    ("Grayscale",         "gray"),
)
_DEFAULT_CMAP_NAME: str = _HEATMAP_COLORMAPS[0][1]


def _generate_disk_cap(
    x: float, y: float, z: float, radius: float,
    color_rgb: tuple[float, float, float],
    n_segments: int = 10,
) -> tuple[np.ndarray, np.ndarray]:
    """Filled circular disk at ``(x, y, z)`` — triangle fan from centre to rim.

    Returns ``(positions, colors)`` as (3*n_segments, 3) float32 arrays
    ready for GL_TRIANGLES. Used to cap via cylinders at each copper-layer
    junction so the endpoint colour is visible from any camera angle.
    """
    angles = np.linspace(0.0, 2.0 * np.pi, n_segments, endpoint=False)
    cos_a = np.cos(angles, dtype=np.float64)
    sin_a = np.sin(angles, dtype=np.float64)
    cos_b = np.roll(cos_a, -1)
    sin_b = np.roll(sin_a, -1)
    rim_a = np.column_stack((x + radius * cos_a, y + radius * sin_a,
                             np.full(n_segments, z)))
    rim_b = np.column_stack((x + radius * cos_b, y + radius * sin_b,
                             np.full(n_segments, z)))
    out = np.empty((n_segments * 3, 3), dtype=np.float32)
    out[0::3] = [x, y, z]
    out[1::3] = rim_a
    out[2::3] = rim_b
    col = np.broadcast_to(np.asarray(color_rgb, dtype=np.float32),
                          (out.shape[0], 3)).copy()
    return out, col


def _generate_via_cylinder(x: float, y: float, z_top: float, z_bottom: float,
                            radius: float, color_rgb: tuple[float, float, float],
                            n_segments: int = 10,
                            ) -> tuple[np.ndarray, np.ndarray]:
    """Generate the side-wall triangles of an open cylinder centred at
    ``(x, y)`` extending from ``z_top`` to ``z_bottom``.

    Returns ``(positions, colors)`` as (6*n_segments, 3) float32 arrays
    suitable for :meth:`gl_mesh_viewer.GLMeshViewer.set_cylinders`. Each
    side facet contributes two triangles (six vertices) drawn via
    ``GL_TRIANGLES``. Caps are omitted — the user's view only ever sees
    the side surface in PDN inspection.
    """
    angles = np.linspace(0.0, 2.0 * np.pi, n_segments, endpoint=False)
    cos_a = np.cos(angles, dtype=np.float64)
    sin_a = np.sin(angles, dtype=np.float64)
    cos_b = np.roll(cos_a, -1)
    sin_b = np.roll(sin_a, -1)
    # Per-side-facet quad corners.
    top1 = np.column_stack((x + radius * cos_a, y + radius * sin_a,
                            np.full(n_segments, z_top)))
    top2 = np.column_stack((x + radius * cos_b, y + radius * sin_b,
                            np.full(n_segments, z_top)))
    bot1 = np.column_stack((x + radius * cos_a, y + radius * sin_a,
                            np.full(n_segments, z_bottom)))
    bot2 = np.column_stack((x + radius * cos_b, y + radius * sin_b,
                            np.full(n_segments, z_bottom)))
    # Interleave as triangle pairs per facet:
    #   T1 = top1, bot1, top2     T2 = top2, bot1, bot2
    out = np.empty((n_segments * 6, 3), dtype=np.float32)
    out[0::6] = top1
    out[1::6] = bot1
    out[2::6] = top2
    out[3::6] = top2
    out[4::6] = bot1
    out[5::6] = bot2
    col = np.broadcast_to(np.asarray(color_rgb, dtype=np.float32),
                          (out.shape[0], 3)).copy()
    return out, col


def _generate_via_cylinder_gradient(
    x: float, y: float, z_top: float, z_bottom: float,
    radius: float,
    color_top_rgb: tuple[float, float, float],
    color_bottom_rgb: tuple[float, float, float],
    n_segments: int = 10,
) -> tuple[np.ndarray, np.ndarray]:
    """Same geometry as :func:`_generate_via_cylinder`, but each side facet's
    top vertices use ``color_top_rgb`` and its bottom vertices use
    ``color_bottom_rgb`` so the GPU interpolates the colour smoothly along
    the cylinder's axis."""
    angles = np.linspace(0.0, 2.0 * np.pi, n_segments, endpoint=False)
    cos_a = np.cos(angles, dtype=np.float64)
    sin_a = np.sin(angles, dtype=np.float64)
    cos_b = np.roll(cos_a, -1)
    sin_b = np.roll(sin_a, -1)
    top1 = np.column_stack((x + radius * cos_a, y + radius * sin_a,
                            np.full(n_segments, z_top)))
    top2 = np.column_stack((x + radius * cos_b, y + radius * sin_b,
                            np.full(n_segments, z_top)))
    bot1 = np.column_stack((x + radius * cos_a, y + radius * sin_a,
                            np.full(n_segments, z_bottom)))
    bot2 = np.column_stack((x + radius * cos_b, y + radius * sin_b,
                            np.full(n_segments, z_bottom)))
    out = np.empty((n_segments * 6, 3), dtype=np.float32)
    out[0::6] = top1
    out[1::6] = bot1
    out[2::6] = top2
    out[3::6] = top2
    out[4::6] = bot1
    out[5::6] = bot2
    ct = np.asarray(color_top_rgb, dtype=np.float32)
    cb = np.asarray(color_bottom_rgb, dtype=np.float32)
    col = np.empty((n_segments * 6, 3), dtype=np.float32)
    col[0::6] = ct
    col[1::6] = cb
    col[2::6] = ct
    col[3::6] = ct
    col[4::6] = cb
    col[5::6] = cb
    return out, col


def _sample_cmap_lut(lut: np.ndarray, value: float,
                     vmin: float, vmax: float,
                     ) -> tuple[float, float, float]:
    """Pick the LUT row matching ``value`` clamped to ``[vmin, vmax]``.
    ``lut`` is the (256, 4) uint8 RGBA array built by
    :func:`_build_cmap_lut`. Returns an (R, G, B) triple in 0..1."""
    if not math.isfinite(value):
        return (0.5, 0.5, 0.5)
    span = vmax - vmin
    if span <= 0:
        t = 0.0
    else:
        t = (value - vmin) / span
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    n = lut.shape[0]
    idx = int(round(t * (n - 1)))
    return (lut[idx, 0] / 255.0,
            lut[idx, 1] / 255.0,
            lut[idx, 2] / 255.0)


def _extrude_to_prism(
    xs: np.ndarray, ys: np.ndarray, vs: np.ndarray, tris: np.ndarray,
    z_center: float, thickness: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Extrude a flat 2D triangle mesh into a thin closed prism.

    The input is the per-layer copper mesh (xs, ys, vs at a single z =
    ``z_center``). The output has:

    * **Top face**: original triangles at ``z = z_center + thickness/2``
    * **Bottom face**: original triangles at ``z = z_center - thickness/2``
      with reversed winding (outward normal points down)
    * **Side walls**: two triangles per *boundary* edge (edges incident
      to exactly one input triangle); interior edges are hidden inside
      the prism so we don't waste triangles on them.

    Per-vertex value (used by the colormap shader) is duplicated top↔bottom
    so the copper is uniformly coloured through its thickness — matches the
    FEM's sheet-conductor assumption (no in-z variation).

    Returns ``(xs, ys, zs, vs, tris)`` ready to feed into the GL viewer's
    combined batch.
    """
    n = xs.size
    if n == 0 or tris.size == 0:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty, empty, empty, np.empty((0, 3), dtype=np.int32)

    # Extrude DOWNWARD only: top face sits at z_center (the layer's
    # plane) so it stays coplanar with the outline overlay and the via
    # cylinders meet the copper flush. Bottom face is pushed down by
    # ``thickness``.
    z_top = z_center
    z_bot = z_center - thickness

    new_xs = np.concatenate([xs, xs])
    new_ys = np.concatenate([ys, ys])
    new_zs = np.empty(2 * n, dtype=np.float64)
    new_zs[:n] = z_top
    new_zs[n:] = z_bot
    new_vs = np.concatenate([vs, vs])

    top_tris = tris.astype(np.int64, copy=False)
    bot_tris = tris[:, [0, 2, 1]].astype(np.int64) + n

    # Boundary edges = those incident to exactly one triangle.
    m = tris.shape[0]
    edges = np.empty((3 * m, 2), dtype=np.int64)
    edges[0::3] = np.sort(tris[:, [0, 1]], axis=1)
    edges[1::3] = np.sort(tris[:, [1, 2]], axis=1)
    edges[2::3] = np.sort(tris[:, [2, 0]], axis=1)
    _, inverse, counts = np.unique(edges, axis=0,
                                    return_inverse=True, return_counts=True)
    boundary_mask = counts[inverse] == 1
    bedges = edges[boundary_mask]

    # Each boundary edge (a, b) → two triangles forming the side-wall
    # quad: (a_top, b_top, b_bot) and (a_top, b_bot, a_bot).
    a = bedges[:, 0]
    b = bedges[:, 1]
    wall_1 = np.column_stack([a, b, b + n])
    wall_2 = np.column_stack([a, b + n, a + n])

    all_tris = np.concatenate([top_tris, bot_tris, wall_1, wall_2],
                              axis=0).astype(np.int32, copy=False)
    return new_xs, new_ys, new_zs, new_vs, all_tris


def _shape_outline_segments(shape) -> np.ndarray:
    """Convert a shapely Polygon / MultiPolygon's outline (exterior +
    interior rings) into an (N, 2) float32 array of GL_LINES vertices.

    Consecutive vertex pairs form one segment, so N == 2 * num_segments.
    Returns an empty array when the shape is empty / has no rings. Used
    by the per-layer outline overlay in :class:`GLMeshViewer`.
    """
    if shape is None or shape.is_empty:
        return np.empty((0, 2), dtype=np.float32)
    if hasattr(shape, "geoms"):
        polys = list(shape.geoms)
    else:
        polys = [shape]
    rings: list[np.ndarray] = []
    for poly in polys:
        ext = poly.exterior
        if ext is not None and not ext.is_empty:
            rings.append(np.asarray(ext.coords, dtype=np.float32))
        for hole in poly.interiors:
            if hole is not None and not hole.is_empty:
                rings.append(np.asarray(hole.coords, dtype=np.float32))
    segs: list[np.ndarray] = []
    for ring in rings:
        # Each ring already includes the closing-vertex duplicate
        # (ring[-1] == ring[0]); turning it into GL_LINES pairs gives
        # ``len(ring) - 1`` segments, each two consecutive points.
        if ring.shape[0] < 2:
            continue
        pairs = np.empty((2 * (ring.shape[0] - 1), 2), dtype=np.float32)
        pairs[0::2] = ring[:-1]
        pairs[1::2] = ring[1:]
        segs.append(pairs)
    if not segs:
        return np.empty((0, 2), dtype=np.float32)
    return np.concatenate(segs, axis=0)


class _FastTriSampler:
    """Point-samples a triangulated linear scalar field — a drop-in
    replacement for :class:`matplotlib.tri.LinearTriInterpolator` that
    skips its ``TrapezoidMapTriFinder``.

    Matplotlib builds that trapezoid map eagerly inside the interpolator
    constructor. On a large copper plane (a GND pour reaches 300k+
    triangles) that takes 3–10 s and froze the GUI for several seconds
    the first time the hover probe touched the plane — the per-layer
    interpolator cache is why only the *first* hover stalled.

    This sampler instead builds a ``cKDTree`` over triangle centroids
    (pure C, ~50–80x faster to construct) and, at query time,
    barycentric-interpolates the nearest triangle that contains the
    point. The interpolation is mathematically identical — linear over
    each triangle — so results match ``LinearTriInterpolator`` to
    floating-point precision (verified <1e-16 V across every GND plane
    in the example projects). Same pure-C-spatial-index swap the Vias /
    Nodes report tables already use (see :meth:`PdnViewer._compute_via_report`).
    """

    # Candidate triangles examined per query. For any quality mesh the
    # containing triangle's centroid is among the nearest few; 32 is a
    # wide safety margin — verified to give zero strict-inside misses
    # across every GND plane in the bundled example designs.
    _K: int = 32

    def __init__(self, triangulation, values) -> None:
        x = np.ascontiguousarray(triangulation.x, dtype=np.float64)
        y = np.ascontiguousarray(triangulation.y, dtype=np.float64)
        tris = np.ascontiguousarray(triangulation.triangles, dtype=np.intp)
        z = np.ascontiguousarray(values, dtype=np.float64)
        i0, i1, i2 = tris[:, 0], tris[:, 1], tris[:, 2]
        # Per-triangle anchor vertex + the two edge vectors, so a query
        # is just a point offset and a 2x2 solve for the barycentrics.
        self._ax, self._ay = x[i0], y[i0]
        self._v0x, self._v0y = x[i1] - self._ax, y[i1] - self._ay
        self._v1x, self._v1y = x[i2] - self._ax, y[i2] - self._ay
        det = self._v0x * self._v1y - self._v1x * self._v0y
        # 1/det, zeroed on degenerate (zero-area) triangles so they can
        # never win the argmax below.
        self._inv_det = np.where(np.abs(det) > 1e-30, 1.0 / det, 0.0)
        self._va, self._vb, self._vc = z[i0], z[i1], z[i2]
        centroids = np.column_stack([
            (x[i0] + x[i1] + x[i2]) / 3.0,
            (y[i0] + y[i1] + y[i2]) / 3.0,
        ])
        from scipy.spatial import cKDTree
        # balanced_tree / compact_nodes off → ~3x faster build; queries
        # stay in the microsecond range, and we build once per layer.
        self._tree = cKDTree(centroids, balanced_tree=False,
                             compact_nodes=False)
        self._k = int(min(self._K, max(1, tris.shape[0])))

    def __call__(self, x: float, y: float):
        """Sample the field at world coords (x, y). Returns a 0-d
        ``numpy`` masked array — the same type ``LinearTriInterpolator``
        returns — masked when the point is off the mesh."""
        _d, idx = self._tree.query((x, y), k=self._k)
        idx = np.atleast_1d(idx)
        px = x - self._ax[idx]
        py = y - self._ay[idx]
        v0x, v0y = self._v0x[idx], self._v0y[idx]
        v1x, v1y = self._v1x[idx], self._v1y[idx]
        inv = self._inv_det[idx]
        # Barycentric weights of (x, y) for each candidate triangle:
        # p - a = u*(b - a) + w*(c - a); the third weight is 1 - u - w.
        u = (px * v1y - v1x * py) * inv
        w = (v0x * py - px * v0y) * inv
        t = 1.0 - u - w
        min_w = np.minimum(np.minimum(t, u), w)
        min_w[inv == 0.0] = -np.inf          # never pick a degenerate tri
        j = int(np.argmax(min_w))            # triangle the point is "most inside"
        if min_w[j] < -0.5:
            # Well outside every candidate triangle — a genuine off-mesh
            # point (e.g. a meshing gap inside the copper outline). Match
            # LinearTriInterpolator's masked off-mesh result.
            return np.ma.masked_array(0.0, mask=True)
        val = (t[j] * self._va[idx[j]]
               + u[j] * self._vb[idx[j]]
               + w[j] * self._vc[idx[j]])
        return np.ma.masked_array(float(val), mask=False)


class _GradientBar(QWidget):
    """Horizontal colormap strip with two draggable handles for vmin/vmax,
    designed to sit as an overlay on the heatmap viewer's bottom-left
    corner.

    Paints its own dark chip background, a title, the gradient strip with
    draggable handles, and a row of value tick labels along the bottom.
    Emits :attr:`rangeChanged(low, high)` whenever the user drags a
    handle. The handles render as small triangle pointers above the
    strip; clicking on the strip near a handle starts a drag. ``low`` is
    always kept ≤ ``high`` with at least a tiny gap so the heatmap
    doesn't collapse.

    Colours are fixed rather than theme-driven — the chip sits on the
    heatmap (not in the themed side panel), so it always uses a dark
    background with light text, matching the GL viewer's legend chip.
    """

    rangeChanged = Signal(float, float)

    # --- Fixed overlay colours (theme-independent — chip sits on the GL
    # viewer, like the legend chip) ---
    _CHIP_BG = QColor(30, 30, 30)
    _CHIP_BORDER = QColor("#666666")
    _STRIP_BORDER = QColor("#888888")
    _MASK = QColor(0, 0, 0, 150)        # darkens the strip outside [low, high]
    _TEXT = QColor("#e6e6e6")
    _TICK = QColor("#aaaaaa")
    _HANDLE_FILL = QColor("#f0f0f0")
    _HANDLE_EDGE = QColor("#1a1a1a")

    # --- Pixel geometry ---
    _CHIP_W: int = 300
    _CHIP_PAD: int = 8       # padding between chip edge and content
    _MARGIN_X: int = 8       # extra inset so handle triangles aren't clipped
    _TITLE_H: int = 15       # title text band
    _MARGIN_TOP: int = 9     # handle-triangle space above the strip
    _STRIP_HEIGHT: int = 15
    _TICK_GAP: int = 3       # strip bottom → tick mark
    _TICK_MARK: int = 4      # tick mark length
    _TICK_LABEL_H: int = 13  # tick label text band
    _HANDLE_HALF_W: int = 6
    _N_TICKS: int = 5

    # Engineering SI prefixes (powers of 1000), exponent → symbol. One
    # prefix is picked per render from the data magnitude and folded into
    # the axis title so the tick labels can stay plain numbers.
    _SI_PREFIXES: dict[int, str] = {
        -15: "f", -12: "p", -9: "n", -6: "µ", -3: "m",
        0: "", 3: "k", 6: "M", 9: "G", 12: "T",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Data bounds (the absolute extents the slider spans) and user-
        # selected clamp values. Kept separately so the user can clamp
        # tighter than the data range without losing the auto-detected
        # extents.
        self._data_min: float = 0.0
        self._data_max: float = 1.0
        self._low: float = 0.0
        self._high: float = 1.0
        self._cmap_name: str = "viridis"
        self._cmap_gradient: QLinearGradient | None = None
        self._dragging: str | None = None  # 'low', 'high', or None
        # Metric name + unit shown above the strip, e.g. "Voltage" / "V".
        # Kept separate so an engineering SI prefix (chosen per render
        # from the data range) can be folded into the unit at paint time
        # — e.g. a ~1 mV axis renders its title as "Voltage [mV]".
        self._label: str = ""
        self._unit: str = ""
        # Log-spaced value axis. When True the handle ↔ value mapping is
        # logarithmic; the data range must be strictly positive for it to
        # take effect (callers floor it — see PdnViewer._render).
        self._log: bool = False
        height = (self._CHIP_PAD + self._TITLE_H + self._MARGIN_TOP
                  + self._STRIP_HEIGHT + self._TICK_GAP + self._TICK_MARK
                  + self._TICK_LABEL_H + self._CHIP_PAD)
        self.setFixedSize(self._CHIP_W, height)
        self.setMouseTracking(True)
        self._rebuild_gradient()

    def setTitle(self, label: str, unit: str) -> None:
        """Set the heading shown above the strip, e.g. ``Voltage`` / ``V``.

        The unit gets an SI prefix folded in at paint time (see
        :meth:`_display_title` / :meth:`_si_scale`), so the rendered
        heading may read ``Voltage [mV]`` even though ``unit`` is ``V``.
        """
        self._label = label or ""
        self._unit = unit or ""
        self.update()

    def setColormap(self, cmap_name: str) -> None:
        self._cmap_name = cmap_name
        self._rebuild_gradient()
        self.update()

    def setLogScale(self, on: bool) -> None:
        """Switch the strip between a linear and a log-spaced value axis.
        Log needs a strictly positive data range; :meth:`_value_to_t`
        falls back to linear if that doesn't hold."""
        on = bool(on)
        if on != self._log:
            self._log = on
            self.update()

    def setDataRange(self, data_min: float, data_max: float) -> None:
        """Set the absolute data extents (full slider range)."""
        if not math.isfinite(data_min) or not math.isfinite(data_max):
            return
        if data_max <= data_min:
            data_max = data_min + 1e-12
        self._data_min = float(data_min)
        self._data_max = float(data_max)
        # Clamp the current selection to the new bounds.
        self._low = max(self._data_min, min(self._low, self._data_max))
        self._high = min(self._data_max, max(self._high, self._data_min))
        if self._high <= self._low:
            self._high = self._low + (self._data_max - self._data_min) * 1e-6
        self.update()

    def setSelectedRange(self, low: float, high: float,
                         emit: bool = False) -> None:
        """Programmatically set the clamp values. Used to reset on a fresh
        render so the next mode starts with full-range coverage."""
        if not (math.isfinite(low) and math.isfinite(high)):
            return
        if high <= low:
            high = low + (self._data_max - self._data_min) * 1e-6
        self._low = float(low)
        self._high = float(high)
        self.update()
        if emit:
            self.rangeChanged.emit(self._low, self._high)

    def selectedRange(self) -> tuple[float, float]:
        return self._low, self._high

    def dataRange(self) -> tuple[float, float]:
        return self._data_min, self._data_max

    # --- Painting -----------------------------------------------------------

    def _rebuild_gradient(self) -> None:
        """Pre-compute a QLinearGradient with stops sampled from the active
        matplotlib colormap. Cached so paintEvent doesn't re-sample on every
        repaint (drags fire many repaints per second)."""
        cmap = _mpl_cm.get_cmap(self._cmap_name)
        grad = QLinearGradient(0, 0, 1, 0)
        grad.setCoordinateMode(QLinearGradient.ObjectBoundingMode)
        n_stops = 32
        for i in range(n_stops):
            t = i / (n_stops - 1)
            r, g, b, a = cmap(t)
            grad.setColorAt(t, QColor.fromRgbF(r, g, b, a))
        self._cmap_gradient = grad

    def _strip_rect(self) -> QRectF:
        left = self._CHIP_PAD + self._MARGIN_X
        top = self._CHIP_PAD + self._TITLE_H + self._MARGIN_TOP
        return QRectF(
            left,
            top,
            max(1.0, self.width() - 2.0 * left),
            self._STRIP_HEIGHT,
        )

    def _log_ok(self) -> bool:
        """True when the log axis can actually be used — it needs a
        strictly positive, non-degenerate data range."""
        return (self._log and self._data_min > 0.0
                and self._data_max > self._data_min)

    def _value_to_t(self, value: float) -> float:
        """Normalised 0..1 position of ``value`` along the strip — log-
        spaced when the log axis is active, linear otherwise."""
        if self._log_ok():
            lo = math.log10(self._data_min)
            hi = math.log10(self._data_max)
            v = math.log10(max(value, self._data_min))
            t = (v - lo) / max(hi - lo, 1e-30)
        else:
            span = max(self._data_max - self._data_min, 1e-30)
            t = (value - self._data_min) / span
        return min(1.0, max(0.0, t))

    def _value_to_x(self, value: float) -> float:
        rect = self._strip_rect()
        return rect.left() + self._value_to_t(value) * rect.width()

    def _x_to_value(self, x: float) -> float:
        rect = self._strip_rect()
        if rect.width() <= 0:
            return self._data_min
        t = min(1.0, max(0.0, (x - rect.left()) / rect.width()))
        if self._log_ok():
            lo = math.log10(self._data_min)
            hi = math.log10(self._data_max)
            return 10.0 ** (lo + t * (hi - lo))
        return self._data_min + t * (self._data_max - self._data_min)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # Chip background — an opaque dark rect + light border, matching
        # the GL viewer's legend chip so the strip reads on top of the
        # heatmap regardless of the colours underneath.
        p.fillRect(self.rect(), self._CHIP_BG)
        p.setPen(self._CHIP_BORDER)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))

        # Title ("Voltage [mV]" etc. — SI prefix folded in per render).
        title = self._display_title()
        if title:
            f = p.font()
            f.setPointSizeF(9.0)
            f.setBold(True)
            p.setFont(f)
            p.setPen(self._TEXT)
            p.drawText(
                QRectF(self._CHIP_PAD, self._CHIP_PAD,
                       self.width() - 2 * self._CHIP_PAD, self._TITLE_H),
                Qt.AlignLeft | Qt.AlignVCenter, title,
            )

        # Gradient strip + border.
        strip = self._strip_rect()
        if self._cmap_gradient is not None:
            p.fillRect(strip, self._cmap_gradient)
        p.setPen(self._STRIP_BORDER)
        p.setBrush(Qt.NoBrush)
        p.drawRect(strip)

        # Darken the strip outside [low, high] so the active clamp window
        # is visually obvious.
        low_x = self._value_to_x(self._low)
        high_x = self._value_to_x(self._high)
        p.fillRect(QRectF(strip.left(), strip.top(),
                          low_x - strip.left(), strip.height()), self._MASK)
        p.fillRect(QRectF(high_x, strip.top(),
                          strip.right() - high_x, strip.height()), self._MASK)

        # Tick marks + value labels along the bottom edge.
        self._draw_ticks(p, strip)

        # Handle triangles (one pointing down at each clamp position).
        self._draw_handle(p, low_x)
        self._draw_handle(p, high_x)

    def _draw_handle(self, p: QPainter, x: float) -> None:
        top_y = self._strip_rect().top() - 1
        tri = QPolygonF([
            QPointF(x, top_y + 8),
            QPointF(x - self._HANDLE_HALF_W, top_y - 2),
            QPointF(x + self._HANDLE_HALF_W, top_y - 2),
        ])
        p.setPen(self._HANDLE_EDGE)
        p.setBrush(self._HANDLE_FILL)
        p.drawPolygon(tri)

    def _draw_ticks(self, p: QPainter, strip: QRectF) -> None:
        """Draw ``_N_TICKS`` evenly-spaced tick marks under the strip,
        each labelled with the data value at that position. Values are
        divided by the axis SI prefix's divisor so the labels stay plain
        numbers (the prefix lives in the title — see :meth:`_si_scale`)."""
        divisor, _prefix = self._si_scale(self._data_min, self._data_max,
                                          self._unit)
        f = p.font()
        f.setPointSizeF(8.0)
        f.setBold(False)
        p.setFont(f)
        n = self._N_TICKS
        mark_top = strip.bottom() + self._TICK_GAP
        mark_bot = mark_top + self._TICK_MARK
        label_top = mark_bot
        # Pre-compute every tick value so each label can be formatted
        # against its local spacing — constant in linear mode, but
        # varies along the axis in log mode.
        values: list[float] = []
        for i in range(n):
            t = i / (n - 1)
            x = strip.left() + t * strip.width()
            values.append(self._x_to_value(x) / divisor)
        for i in range(n):
            t = i / (n - 1)
            x = strip.left() + t * strip.width()
            value = values[i]
            if n >= 2:
                if i == 0:
                    step = abs(values[1] - values[0])
                elif i == n - 1:
                    step = abs(values[-1] - values[-2])
                else:
                    step = min(abs(values[i + 1] - values[i]),
                               abs(values[i] - values[i - 1]))
            else:
                step = 0.0
            p.setPen(self._TICK)
            p.drawLine(QPointF(x, mark_top), QPointF(x, mark_bot))
            text = self._fmt_tick(value, step)
            # Keep the end labels inside the chip — left-align the first,
            # right-align the last, centre the rest.
            if i == 0:
                box = QRectF(x, label_top, 90.0, self._TICK_LABEL_H)
                align = Qt.AlignLeft | Qt.AlignVCenter
            elif i == n - 1:
                box = QRectF(x - 90.0, label_top, 90.0, self._TICK_LABEL_H)
                align = Qt.AlignRight | Qt.AlignVCenter
            else:
                box = QRectF(x - 45.0, label_top, 90.0, self._TICK_LABEL_H)
                align = Qt.AlignHCenter | Qt.AlignVCenter
            p.setPen(self._TEXT)
            p.drawText(box, align, text)

    @classmethod
    def _si_scale(cls, data_min: float, data_max: float,
                  unit: str) -> tuple[float, str]:
        """Pick one engineering SI prefix for the whole axis.

        Returns ``(divisor, prefix_symbol)``: a tick value divided by
        ``divisor`` prints as a plain number, and ``prefix_symbol`` is
        prepended to the unit in the title (e.g. ``V`` → ``mV``). Falls
        back to ``(1.0, "")`` when there is no unit or the data
        magnitude is zero / non-finite.
        """
        mag = max(abs(data_min), abs(data_max))
        if not unit or not math.isfinite(mag) or mag <= 0.0:
            return 1.0, ""
        exp = int(math.floor(math.log10(mag) / 3.0) * 3)
        exp = max(min(cls._SI_PREFIXES), min(max(cls._SI_PREFIXES), exp))
        return 10.0 ** exp, cls._SI_PREFIXES[exp]

    def _display_title(self) -> str:
        """Heading shown above the strip, with the active SI prefix
        folded into the unit — e.g. ``Voltage [mV]``."""
        if not self._unit:
            return self._label
        _divisor, prefix = self._si_scale(
            self._data_min, self._data_max, self._unit)
        return f"{self._label} [{prefix}{self._unit}]"

    @staticmethod
    def _fmt_tick(value: float, step: float = 0.0) -> str:
        """Format an already-SI-scaled tick value as a plain number.

        No scientific notation — the SI prefix in the axis title
        carries the magnitude. Precision comes from ``step`` (the
        spacing to the neighbouring tick): each label shows ~2 sig
        figs of the step, so the whole axis reads at a consistent
        scale and values smaller than the step's resolution collapse
        to ``"0"`` instead of printing noise digits (e.g. -0.000331
        on a 0..2.66 axis → ``0``). When ``step`` is unavailable, falls
        back to ~3 sig figs of the value itself."""
        if not math.isfinite(value):
            return "—"
        if step > 0.0 and math.isfinite(step):
            digits = max(0, 1 - int(math.floor(math.log10(step))))
        elif value == 0.0:
            return "0"
        else:
            digits = max(0, 2 - int(math.floor(math.log10(abs(value)))))
        digits = min(6, digits)
        # Round first so values within half a ULP of zero render as
        # plain "0" rather than "-0.00".
        rounded = round(value, digits)
        if rounded == 0.0:
            return "0"
        s = f"{rounded:.{digits}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    # --- Mouse input --------------------------------------------------------

    def _hit_handle(self, x: float) -> str | None:
        """Pick whichever handle is closest to ``x`` within the grip radius."""
        low_x = self._value_to_x(self._low)
        high_x = self._value_to_x(self._high)
        grip = self._HANDLE_HALF_W + 4
        d_low = abs(x - low_x)
        d_high = abs(x - high_x)
        if d_low <= grip and d_low <= d_high:
            return "low"
        if d_high <= grip:
            return "high"
        return None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        strip = self._strip_rect()
        # Only the strip and the handle band just above it are draggable —
        # clicks on the title or the tick labels must not move a handle.
        if not (strip.top() - 11 <= pos.y() <= strip.bottom() + 2):
            return
        self._dragging = self._hit_handle(pos.x())
        if self._dragging is None:
            # Click on bare strip → move the nearest handle to that point.
            v = self._x_to_value(pos.x())
            if abs(v - self._low) <= abs(v - self._high):
                self._dragging = "low"
            else:
                self._dragging = "high"
            self._update_from_drag(pos.x())

    def mouseMoveEvent(self, event) -> None:
        if self._dragging is not None:
            self._update_from_drag(event.position().x())

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = None

    def _update_from_drag(self, x_pixel: float) -> None:
        v = self._x_to_value(x_pixel)
        # Don't let the handles cross — leave at least 1e-6 of the data span
        # between them so the heatmap retains a tiny colour range.
        gap = max((self._data_max - self._data_min) * 1e-6, 1e-30)
        if self._dragging == "low":
            self._low = min(v, self._high - gap)
        elif self._dragging == "high":
            self._high = max(v, self._low + gap)
        self.update()
        self.rangeChanged.emit(self._low, self._high)


class ScaleController(QWidget):
    """Compound widget: title + gradient bar + Min/Max textboxes + Reset.

    Wraps :class:`_GradientBar` with editable text fields so users can
    enter precise clamp values OR drag the handles. Mirrors the
    "Scope Controller" panel commonly seen in CAD post-processors.
    """

    rangeChanged = Signal(float, float)
    # Emitted (with the matplotlib colormap name) when the user picks a
    # different colour scheme from the dropdown. Programmatic
    # :meth:`setColormap` calls are signal-blocked and do NOT emit this.
    colormapChanged = Signal(str)
    # Emitted (True = logarithmic) when the user changes the Scale
    # dropdown. Programmatic :meth:`setLogActive` does NOT emit it.
    scaleTypeChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._unit: str = ""
        self._label: str = "Value"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.title_label = QLabel("<b>Color scale</b>")
        layout.addWidget(self.title_label)

        # Colour-scheme picker — recolours the heatmap live. Sits directly
        # under the title so it reads as part of the colour-scale panel.
        cmap_row = QHBoxLayout()
        cmap_row.setContentsMargins(0, 0, 0, 0)
        cmap_row.setSpacing(6)
        self.cmap_label = QLabel("Scheme")
        self.cmap_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; font-size: 8pt; }}"
        )
        self.cmap_combo = QComboBox()
        for display_name, _mpl_name in _HEATMAP_COLORMAPS:
            self.cmap_combo.addItem(display_name)
        self.cmap_combo.setToolTip(
            "Colour scheme for the heatmap. Changing it recolours the "
            "viewport immediately — no re-solve needed."
        )
        self.cmap_combo.currentIndexChanged.connect(self._on_cmap_combo_changed)
        cmap_row.addWidget(self.cmap_label, 0)
        cmap_row.addWidget(self.cmap_combo, 1)
        layout.addLayout(cmap_row)

        # Linear / logarithmic value axis. Hidden by the viewer for
        # modes where a log scale is meaningless (Voltage) or undefined
        # (the signed Voltage Drop) — see PdnViewer._render.
        scale_row = QHBoxLayout()
        scale_row.setContentsMargins(0, 0, 0, 0)
        scale_row.setSpacing(6)
        self.scale_label = QLabel("Scale")
        self.scale_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; font-size: 8pt; }}"
        )
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["Linear", "Logarithmic"])
        self.scale_combo.setToolTip(
            "Linear or logarithmic colour scale. A log scale spreads the "
            "low end so the bulk of the board and the spikes are both "
            "readable at once — available for Current Density, Power "
            "Density and Via Current (those span many decades). Hidden "
            "for Voltage / Voltage Drop."
        )
        self.scale_combo.currentIndexChanged.connect(self._on_scale_combo_changed)
        scale_row.addWidget(self.scale_label, 0)
        scale_row.addWidget(self.scale_combo, 1)
        layout.addLayout(scale_row)

        # The gradient strip itself is NOT placed in this side panel — it
        # lives as an overlay on the heatmap viewer's bottom-left corner
        # (PdnViewer._build_ui reparents it onto the GL viewer). It is
        # created here, parentless, so this controller can keep driving
        # its colormap / range / title and relay its drag events; the
        # data-extent tick labels are drawn on the strip itself. This
        # panel keeps only the Scheme / Scale pickers + Min/Max editors.
        _t = _T()
        self.bar = _GradientBar()
        self.bar.rangeChanged.connect(self._on_bar_range_changed)

        edits_row = QHBoxLayout()
        edits_row.setContentsMargins(0, 0, 0, 0)
        edits_row.setSpacing(6)

        min_col = QVBoxLayout()
        min_col.setSpacing(2)
        min_lbl = QLabel("Min")
        min_lbl.setStyleSheet(
            f"QLabel {{ color: {_t['fg_muted']}; font-size: 8pt; }}"
        )
        self.min_edit = QLineEdit()
        self.min_edit.setValidator(QDoubleValidator())
        self.min_edit.editingFinished.connect(self._on_edits_committed)
        min_col.addWidget(min_lbl)
        min_col.addWidget(self.min_edit)
        edits_row.addLayout(min_col, 1)

        max_col = QVBoxLayout()
        max_col.setSpacing(2)
        max_lbl = QLabel("Max")
        max_lbl.setStyleSheet(
            f"QLabel {{ color: {_t['fg_muted']}; font-size: 8pt; }}"
        )
        self.max_edit = QLineEdit()
        self.max_edit.setValidator(QDoubleValidator())
        self.max_edit.editingFinished.connect(self._on_edits_committed)
        max_col.addWidget(max_lbl)
        max_col.addWidget(self.max_edit)
        edits_row.addLayout(max_col, 1)

        # Reset-to-data-range button — a small square button sitting to
        # the right of the Max box. The Min/Max columns carry a label
        # above the edit, so AlignBottom drops the button level with the
        # input boxes (not the taller label+edit column). The "↺" glyph
        # is the button's *text*, so it's painted in the themed text
        # colour and tracks light/dark mode — a QStyle standard icon
        # would not. Min/Max stay at equal stretch (1 each) so they keep
        # the same width; the button column is fixed-width (stretch 0).
        self.reset_button = QPushButton("↺")
        self.reset_button.setToolTip(
            "Reset to data range — restore Min / Max to the auto-detected "
            "data range for the current layer, rail, and mode."
        )
        _reset_font = self.reset_button.font()
        if _reset_font.pointSize() > 0:
            _reset_font.setPointSize(_reset_font.pointSize() + 2)
            self.reset_button.setFont(_reset_font)
        self.reset_button.clicked.connect(self._on_reset_clicked)
        edits_row.addWidget(self.reset_button, 0, Qt.AlignBottom)

        layout.addLayout(edits_row)

        # Theme-driven styling for the inputs so they don't fight the rest
        # of the UI. Colours come from the active theme dict so both dark
        # and light modes look right.
        self.setStyleSheet(
            f"QLineEdit {{ background-color: {_t['bg_input']}; color: {_t['fg']};"
            f"            border: 1px solid {_t['border']}; padding: 2px 4px; }}"
            f"QPushButton {{ background-color: {_t['bg_hover']}; color: {_t['fg']};"
            f"              border: 1px solid {_t['border']}; padding: 3px; }}"
            f"QPushButton:hover {{ background-color: {_t['bg_hover_strong']}; }}"
        )

        # Square the reset button to the Min/Max input height. Done after
        # the stylesheet is set so the line edit's size hint already
        # includes the themed padding + border.
        self.min_edit.ensurePolished()
        _edit_h = self.min_edit.sizeHint().height()
        self.reset_button.setFixedSize(_edit_h, _edit_h)

    def apply_theme(self) -> None:
        """Re-style the controller and its children to match the active
        theme. Called by :meth:`PdnViewer._refresh_inline_theme` after the
        user picks a new theme in the Settings tab."""
        t = current_theme()
        # The "Min" / "Max" / "Scheme" / "Scale" header labels weren't
        # captured as instance attributes; find them by their plain text
        # so we can re-style. (The gradient strip itself is a fixed-colour
        # overlay on the GL viewer — it has no theme to follow.)
        for lbl in self.findChildren(QLabel):
            if lbl is self.title_label:
                continue
            if lbl.text().strip() in ("Min", "Max", "Scheme", "Scale"):
                lbl.setStyleSheet(
                    f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}"
                )
        self.setStyleSheet(
            f"QLineEdit {{ background-color: {t['bg_input']}; color: {t['fg']};"
            f"            border: 1px solid {t['border']}; padding: 2px 4px; }}"
            f"QPushButton {{ background-color: {t['bg_hover']}; color: {t['fg']};"
            f"              border: 1px solid {t['border']}; padding: 3px; }}"
            f"QPushButton:hover {{ background-color: {t['bg_hover_strong']}; }}"
        )

    def setColormap(self, cmap_name: str) -> None:
        """Set the active colour scheme (matplotlib colormap name). Syncs
        both the gradient strip and the dropdown selection WITHOUT emitting
        :attr:`colormapChanged` — used for programmatic sync from the
        viewer (the dropdown itself is the only thing that emits)."""
        self.bar.setColormap(cmap_name)
        for i, (_display, mpl_name) in enumerate(_HEATMAP_COLORMAPS):
            if mpl_name == cmap_name and self.cmap_combo.currentIndex() != i:
                self.cmap_combo.blockSignals(True)
                self.cmap_combo.setCurrentIndex(i)
                self.cmap_combo.blockSignals(False)
                break

    def _on_cmap_combo_changed(self, index: int) -> None:
        """User picked a scheme from the dropdown — repaint the gradient
        strip and let the viewer recolour the heatmap."""
        if 0 <= index < len(_HEATMAP_COLORMAPS):
            mpl_name = _HEATMAP_COLORMAPS[index][1]
            self.bar.setColormap(mpl_name)
            self.colormapChanged.emit(mpl_name)

    def setLogActive(self, on: bool) -> None:
        """Set whether the gradient strip uses a log-spaced value axis.
        This is the *effective* state (the viewer forces linear for
        ineligible modes); the dropdown selection is left untouched so
        the user's preference survives a trip through such a mode."""
        self.bar.setLogScale(on)

    def setLogVisible(self, visible: bool) -> None:
        """Show or hide the Scale (Linear / Logarithmic) dropdown for the
        current mode. Hidden — not merely greyed — for modes where a log
        axis is meaningless (Voltage) or undefined (the signed Voltage
        Drop), so the panel only ever shows controls that do something."""
        self.scale_combo.setVisible(visible)
        self.scale_label.setVisible(visible)

    def _on_scale_combo_changed(self, index: int) -> None:
        """User switched the Linear / Logarithmic dropdown."""
        self.scaleTypeChanged.emit(index == 1)

    def setLabelUnit(self, label: str, unit: str) -> None:
        # The metric name + unit are shown as the title on the overlaid
        # strip; the side-panel header stays the static "Color scale".
        self._label = label
        self._unit = unit
        self.bar.setTitle(label, unit)

    def setRange(self, data_min: float, data_max: float,
                 sel_min: float | None = None,
                 sel_max: float | None = None,
                 reset_selection: bool = True) -> None:
        """Set the data extents (full slider range) and optionally the
        initial Min/Max selection.

        ``sel_min`` / ``sel_max`` default to the data extents — pass a
        narrower window when the auto-detected data range contains
        outliers (e.g. FEM spikes at pinned-voltage vertices) so the
        default heatmap isn't crushed to one corner of the colour scale.
        The slider can still be dragged out to ``data_max`` to inspect
        the outlier.
        """
        self.bar.setDataRange(data_min, data_max)
        if reset_selection:
            initial_min = data_min if sel_min is None else sel_min
            initial_max = data_max if sel_max is None else sel_max
            self.bar.setSelectedRange(initial_min, initial_max, emit=False)
        # Always refresh the text boxes so they reflect the (possibly
        # re-clamped) selection.
        low, high = self.bar.selectedRange()
        self._set_edit_values(low, high)

    def selectedRange(self) -> tuple[float, float]:
        return self.bar.selectedRange()

    # --- Internal slots -----------------------------------------------------

    def _on_bar_range_changed(self, low: float, high: float) -> None:
        self._set_edit_values(low, high)
        self.rangeChanged.emit(low, high)

    def _on_edits_committed(self) -> None:
        try:
            low = float(self.min_edit.text())
            high = float(self.max_edit.text())
        except ValueError:
            return
        if high <= low:
            return
        self.bar.setSelectedRange(low, high, emit=False)
        self.rangeChanged.emit(low, high)

    def _on_reset_clicked(self) -> None:
        d_min, d_max = self.bar.dataRange()
        self.bar.setSelectedRange(d_min, d_max, emit=False)
        self._set_edit_values(d_min, d_max)
        self.rangeChanged.emit(d_min, d_max)

    def _set_edit_values(self, low: float, high: float) -> None:
        # ``blockSignals`` is the simple way to suppress editingFinished
        # while we update the text — otherwise typing in one box would
        # trigger a re-render mid-edit when focus moves.
        self.min_edit.blockSignals(True)
        self.max_edit.blockSignals(True)
        self.min_edit.setText(self._fmt(low))
        self.max_edit.setText(self._fmt(high))
        self.min_edit.blockSignals(False)
        self.max_edit.blockSignals(False)

    @staticmethod
    def _fmt(value: float) -> str:
        if not math.isfinite(value):
            return "—"
        if value == 0:
            return "0"
        if abs(value) < 1e-3 or abs(value) >= 1e5:
            return f"{value:.3e}"
        return f"{value:.4g}"


class _SolveProgressUpdater(QObject):
    """Wires a :class:`_SolveWorker`'s ``stage_changed`` / ``substage_changed``
    signals to a :class:`QProgressDialog`, with a live elapsed-time
    counter for the current stage so the user can see that long opaque
    steps (e.g. the ~20 s "Meshing + solving") are still making progress.

    The dialog's label text is rendered as up to two lines:

      | Meshing + solving (21 (layer, net) slabs, 67 networks)…  (12s)
      | Currently: Constructing the Laplace operators

    A second, independent counter — the total wall-clock time since the
    load started — ticks in the dialog's bottom-left corner ("Elapsed:
    Ns"). Unlike the per-stage counter it never resets between stages.

    Both counters tick once a second via a QTimer parented to ``self``
    (so it dies when the updater is deleted). Call :meth:`stop` from the
    worker's cleanup path to stop ticking before the dialog closes.
    """

    def __init__(self, dlg: QProgressDialog, worker: _SolveWorker,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dlg = dlg
        self._stage_text: str = ""
        self._substage_text: str = ""
        self._stage_start: float = 0.0
        # Wall-clock start of the whole load, for the bottom-left total
        # counter. Set now (the dialog is already shown) so it counts
        # from the moment the dialog appears, not from the first stage.
        self._total_start: float = time.monotonic()
        self._elapsed_label: QLabel = self._make_elapsed_label(dlg)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._render)
        # Tick immediately — the total counter must run even before the
        # first stage_changed arrives (e.g. during the solve-cache probe).
        self._timer.start()
        worker.stage_changed.connect(self._on_stage)
        worker.substage_changed.connect(self._on_substage)
        self._render()

    @staticmethod
    def _make_elapsed_label(dlg: QProgressDialog) -> QLabel:
        """Build the total-elapsed label pinned to the dialog's
        bottom-left, level with the Cancel button.

        The dialog is fixed-size by the time this runs, and the Cancel
        button's vertical position depends only on the dialog height
        (unchanged by the width-only stretch in the caller) — so a
        one-shot move() holds for the dialog's whole lifetime.
        """
        label = QLabel(dlg)
        label.setObjectName("_elapsed_timer_label")
        # Size for the widest text we'll ever show: the label isn't
        # layout-managed, so a later setText() won't grow it and a
        # too-narrow label would clip the seconds count.
        label.setText("Elapsed: 0000s")
        label.adjustSize()
        label.setText("Elapsed: 0s")
        cancel_btn = dlg.findChild(QPushButton)
        if cancel_btn is not None and cancel_btn.height() > 0:
            y = cancel_btn.y() + (cancel_btn.height() - label.height()) // 2
        else:
            y = dlg.height() - label.height() - 9
        label.move(11, y)
        label.show()
        label.raise_()
        return label

    def _on_stage(self, text: str) -> None:
        self._stage_text = text
        self._substage_text = ""
        self._stage_start = time.monotonic()
        self._render()

    def _on_substage(self, text: str) -> None:
        self._substage_text = text
        self._render()

    def _render(self) -> None:
        if self._dlg is None:
            return
        # Total wall-clock counter (bottom-left) — updated every tick,
        # independent of which stage is currently running.
        total = int(time.monotonic() - self._total_start)
        self._elapsed_label.setText(f"Elapsed: {total}s")
        if not self._stage_text:
            return
        elapsed = int(time.monotonic() - self._stage_start)
        first = f"{self._stage_text}  ({elapsed}s)" if elapsed > 0 \
            else self._stage_text
        if self._substage_text:
            self._dlg.setLabelText(f"{first}\nCurrently: {self._substage_text}")
        else:
            self._dlg.setLabelText(first)

    def stop(self) -> None:
        """Stop the elapsed-time timer. Safe to call multiple times."""
        if self._timer.isActive():
            self._timer.stop()


class _StageTimer:
    """Accumulates wall-clock durations of named load-pipeline stages and
    logs a ranked breakdown when the load finishes.

    The whole-load counterpart of pdnsolver.solver's per-stage timing: it
    lets a clean load self-report where its time went (extract, geometry,
    solve, packaging, cache) so the slow stages are obvious without scraping
    timestamps out of the log by hand.
    """

    def __init__(self, log_: logging.Logger) -> None:
        self._log = log_
        self._stages: list[tuple[str, float]] = []
        self._t0 = time.monotonic()

    @contextlib.contextmanager
    def stage(self, label: str):
        """Time a ``with``-wrapped pipeline stage and record its duration."""
        t = time.monotonic()
        try:
            yield
        finally:
            dt = time.monotonic() - t
            self._stages.append((label, dt))
            self._log.info("Stage '%s' done in %.2fs", label, dt)

    def log_breakdown(self) -> None:
        """Log every recorded stage, slowest first, with its share of the
        total wall-clock time since this timer was created. An
        ``(other / untimed)`` row catches whatever ran outside a stage."""
        total = time.monotonic() - self._t0
        self._log.info("=== Load timing breakdown (slowest stage first) ===")
        accounted = 0.0
        for label, dt in sorted(self._stages, key=lambda kv: kv[1], reverse=True):
            accounted += dt
            pct = 100.0 * dt / total if total > 0 else 0.0
            self._log.info("  %8.2fs  %5.1f%%  %s", dt, pct, label)
        other = total - accounted
        self._log.info("  %8.2fs  %5.1f%%  (other / untimed)", other,
                       100.0 * other / total if total > 0 else 0.0)
        self._log.info("  %8.2fs  100.0%%  TOTAL", total)


class _SolveWorker(QThread):
    """Background worker that re-runs the FEM solve off the GUI thread.

    The solve takes 10–60 s on typical boards; running it on the main
    thread freezes the UI (Windows shows "Not Responding") and the
    Settings-tab status label can't update mid-solve. Punting it to a
    QThread lets the progress dialog spin and stage messages stream in
    via :attr:`stage_changed`.

    Note: the worker does NOT call ``settings.apply_to_modules()`` itself;
    the caller does that on the main thread before ``start()`` so the
    module-level monkey-patch ordering is unambiguous.
    """

    stage_changed = Signal(str)           # "Building geometry…" etc.
    substage_changed = Signal(str)        # finer-grained detail line shown
                                          # under the main stage label (e.g.
                                          # pdnsolver's per-step log records
                                          # during meshing + solving)
    finished_ok = Signal(object, object, object)  # (LeanSolution, metadata,
                                          # pristine LoadedProject | None)
    failed = Signal(str)                  # error message for the UI

    def __init__(self, prjpcb_path: Path, settings,
                  sink_overrides: dict[tuple[str, str, int | None], float] | None = None,
                  stackup_overrides: dict[int, float] | None = None,
                  pcbdoc_selector: str | Path | None = None,
                  use_design_cache: bool = True,
                  try_solve_cache_first: bool = False,
                  editor_directives: list | None = None,
                  copper_names: list | None = None,
                  loaded_project: object | None = None,
                  parent=None) -> None:
        super().__init__(parent)
        self._prjpcb_path = prjpcb_path
        self._settings = settings
        # ``{(designator, schdoc, channel_index): current_amperes}`` —
        # substituted into the parsed AnnotationResult before build_problem
        # so the FEM sees the new currents. ``channel_index`` is None for
        # the legacy unindexed SINK channel and an int for indexed channels
        # (PDN1_I / PDN2_I / …).
        self._sink_overrides = dict(sink_overrides or {})
        # ``{layer_id: copper_thickness_mm}`` — substituted into the
        # ExtractedProject's stackup so both per-layer conductance and
        # via-barrel hop lengths reflect the new thicknesses.
        self._stackup_overrides = dict(stackup_overrides or {})
        # Selects one of several .PcbDoc files in a multi-PCB project.
        # None = altium_monkey default (first PcbDoc in project order).
        self._pcbdoc_selector = pcbdoc_selector
        # False = "Import Altium Design (Clean)" / "Reload Design Info" path:
        # always run extract+geometry+annotations from disk. True = try the
        # FYPA design-info cache first and fall back to a fresh load on miss.
        self._use_design_cache = bool(use_design_cache)
        # True = try the FYPA SOLVE cache first; on hit, emit finished_ok
        # immediately with the cached (solution, metadata) and skip
        # extract/mesh/solve entirely. The pickle.load can take 5–10 s for
        # large boards, so doing it on this worker thread (rather than the
        # main thread before starting the worker) keeps the progress
        # dialog responsive instead of freezing the UI.
        self._try_solve_cache_first = bool(try_solve_cache_first)
        # FYPA editor-mode directives (project-file sources / sinks). When
        # present they're converted to synthetic SourceSpec / SinkSpec and
        # appended to the loaded AnnotationResult before build_problem —
        # and, like the override paths, they disable the solve cache (the
        # result diverges from the on-disk project).
        self._editor_directives = list(editor_directives or [])
        # FYPA editor-mode copper renames (user-given names for unnamed
        # copper pieces). Applied to ``loaded.extracted`` before
        # ``apply_editor_directives`` runs so the new nets exist by the
        # time directives are matched, and so the FEM bucketer sees the
        # renamed copper as part of those new nets instead of dropping it.
        self._copper_names = list(copper_names or [])
        # In-memory LoadedProject handed in by the editor 'Resolve' path:
        # the pristine design info the viewer already holds. When set, the
        # worker re-solves against it directly — no design-info cache read,
        # no project-file stat, no re-extract. None for every other flow
        # (normal load / Re-run / clean), which load design info as before.
        self._loaded_project = loaded_project

    def run(self) -> None:  # type: ignore[override]
        # Bias the OS scheduler toward the GUI thread. The packaging phase
        # (build_solve_metadata + to_lean_solution + cache pickle) is pure
        # Python and holds the GIL continuously; without this the main
        # thread starves, the QProgressDialog barber-pole pauses, and
        # Windows flags the window "Not Responding" on large boards.
        self.setPriority(QThread.LowPriority)
        try:
            from fypa.altium_loader import (
                build_problem,
                build_solve_metadata,
                load_project,
            )
            from fypa.lean_solution import to_lean_solution
            from pdnsolver import mesh as _pdn_mesh
            from pdnsolver import solver as _pdn_solver

            # Resolve the PcbDoc up-front so the cache key is stable.
            pcbdoc_resolved: Path | None = None
            try:
                from fypa.cli import _resolve_pcbdoc
                pcbdoc_resolved = _resolve_pcbdoc(
                    self._prjpcb_path,
                    str(self._pcbdoc_selector) if self._pcbdoc_selector else None,
                )
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Could not resolve PcbDoc for cache key (%s: %s); "
                    "solve cache will NOT be written this run.",
                    type(e).__name__, e,
                )
                pcbdoc_resolved = None

            # Solve-cache fast path: pickle.load can take 5–10 s on large
            # boards and blocks until the file is fully read, so doing it
            # here (off the GUI thread) keeps the progress dialog responsive.
            # On hit, finish immediately and skip extract/mesh/solve.
            # Skipped when overrides are active — the cached solve was
            # computed against the on-disk project and would be wrong.
            if (self._try_solve_cache_first
                    and pcbdoc_resolved is not None
                    and not self._stackup_overrides
                    and not self._sink_overrides
                    and not self._editor_directives):
                self.stage_changed.emit(
                    f"Checking solve cache for {self._prjpcb_path.name}…"
                )
                try:
                    cached = _try_solve_cache(self._prjpcb_path, pcbdoc_resolved)
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "Solve-cache check failed (%s: %s); will re-solve.",
                        type(e).__name__, e,
                    )
                    cached = None
                if cached is not None:
                    logging.getLogger(__name__).info(
                        "Solve cache hit for %s — skipping extract + solve.",
                        self._prjpcb_path.name,
                    )
                    self.stage_changed.emit(
                        "Solve cache hit — opening viewer…"
                    )
                    # Solve-cache hit skips the extract entirely, so there's
                    # no LoadedProject to hand on — a later resolve from this
                    # viewer falls back to the design-info cache.
                    self.finished_ok.emit(cached[0], cached[1], None)
                    return

            # Whole-load stage timer — logs a breakdown just before the
            # viewer opens so a clean load self-reports where its time went.
            _timer = _StageTimer(logging.getLogger(__name__))

            # A resolve (editor directives present) always re-solves against
            # the design info that's already loaded — it must never re-stat
            # the Altium project files to revalidate it.
            _is_resolve = bool(self._editor_directives)

            loaded = None
            if self._loaded_project is not None:
                # In-memory reuse — the editor 'Resolve' path hands us the
                # LoadedProject the viewer already holds. No cache read, no
                # project-file stat, no re-extract: take the object as-is.
                self.stage_changed.emit("Reusing loaded design info…")
                loaded = self._loaded_project
            elif self._use_design_cache and pcbdoc_resolved is not None:
                # No in-memory LoadedProject — e.g. the load hit the solve
                # cache, so nothing was extracted. Fall back to the
                # design-info cache. On a resolve, reuse it as-is:
                # current_fp=None skips the fingerprint compare and the
                # project-file stat that the "Checking…" stage performs.
                self.stage_changed.emit(
                    "Reusing loaded design info…" if _is_resolve
                    else "Checking design-info cache…"
                )
                try:
                    from fypa.cli import (
                        _design_info_fingerprint,
                        _try_load_cached_design_info,
                    )
                    # Unpickling the cached LoadedProject ("reusing the
                    # design extract") runs many seconds on a big board.
                    # Time it so the load breakdown reports the cost — it
                    # parallels the "Save design-info cache" stage.
                    with _timer.stage("Load design-info cache"):
                        design_fp = (
                            None if _is_resolve
                            else _design_info_fingerprint(
                                self._prjpcb_path, pcbdoc_resolved,
                            )
                        )
                        loaded = _try_load_cached_design_info(
                            self._prjpcb_path, design_fp,
                            pcbdoc_path=pcbdoc_resolved,
                        )
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "Design-info cache check failed (%s); re-extracting.",
                        e,
                    )
                    loaded = None

            if loaded is None:
                self.stage_changed.emit("Loading project from disk…")
                with _timer.stage("Extract + load project"):
                    loaded = load_project(self._prjpcb_path,
                                          pcbdoc_selector=self._pcbdoc_selector)
                # Persist the freshly-loaded design info so the next run
                # (e.g. a Re-run that only changes physics) can skip the
                # extract step. Failures are non-fatal.
                #
                # Skipped on a clean load (``use_design_cache`` False): a
                # clean load won't read a design-info cache anyway, so
                # writing one — ~5 s of pickling the ExtractedProject on a
                # big board — is pure dead weight on the critical path. The
                # first subsequent *non-clean* load repopulates it.
                if self._use_design_cache and pcbdoc_resolved is not None:
                    try:
                        from fypa.cli import (
                            _design_info_fingerprint,
                            _save_cached_design_info,
                        )
                        # Pickling the whole LoadedProject runs several
                        # seconds on a big board. Give it its own progress
                        # label + timing-log stage so the wait isn't
                        # mistaken for a hung "Loading project from disk".
                        self.stage_changed.emit("Saving design-info cache…")
                        with _timer.stage("Save design-info cache"):
                            design_fp = _design_info_fingerprint(
                                self._prjpcb_path, pcbdoc_resolved,
                            )
                            _save_cached_design_info(
                                self._prjpcb_path, design_fp, loaded,
                                pcbdoc_path=pcbdoc_resolved,
                            )
                    except Exception as e:
                        logging.getLogger(__name__).warning(
                            "Couldn't write design-info cache (%s); ignoring.",
                            e,
                        )
            else:
                self.stage_changed.emit(
                    "Reusing loaded design info…" if _is_resolve
                    else "Design-info cache hit — reusing extract."
                )

            # The design info exactly as loaded — before any stackup / sink
            # override or editor directive is applied. Emitted to the new
            # viewer so it can hand this same object straight back as the
            # in-memory source for the next resolve (see _loaded_project).
            pristine_loaded = loaded

            if not loaded.is_solveable:
                _log = logging.getLogger(__name__)
                from fypa.cli import _LOG_FILE as _log_file
                try:
                    # diagnostic_summary() touches loaded.geometry, which
                    # lazily runs the heavy per-layer polygon union. Give
                    # it an honest progress label + a timed stage so the
                    # wait shows in the dialog and log instead of looking
                    # like a hang on the "cache hit" message.
                    self.stage_changed.emit("Building diagnostic summary…")
                    with _timer.stage("Build diagnostic summary"):
                        _summary = loaded.diagnostic_summary()
                    _log.error("Project is not solveable:\n%s", _summary)
                except Exception as _exc:
                    _log.error("Project is not solveable (diagnostic failed: %s)", _exc)
                self.failed.emit(
                    f"Project is not solveable.\n\n"
                    f"See the log file for details:\n{_log_file}"
                )
                return

            if self._stackup_overrides:
                self.stage_changed.emit(
                    f"Applying {len(self._stackup_overrides)} stackup "
                    "thickness override(s)…"
                )
                loaded = self._apply_stackup_overrides(loaded)

            # Sink overrides and editor directives mutate ``loaded`` in
            # place. When ``loaded`` is still the pristine object — in
            # particular the in-memory LoadedProject the viewer also holds —
            # clone it first so that shared copy (reused by the next
            # resolve) is never touched. A stackup override above already
            # returned a fresh private object, so no clone is needed then.
            if ((self._sink_overrides or self._editor_directives
                    or self._copper_names)
                    and loaded is pristine_loaded):
                loaded = self._clone_loaded_for_edit(loaded)

            if self._sink_overrides:
                self.stage_changed.emit(
                    f"Applying {len(self._sink_overrides)} sink-current "
                    "override(s)…"
                )
                self._apply_sink_overrides(loaded)

            if self._copper_names:
                self.stage_changed.emit(
                    f"Naming {len(self._copper_names)} unnamed copper "
                    "piece(s)…"
                )
                from fypa.editor_directives import apply_copper_names
                cn_warnings = apply_copper_names(
                    loaded, self._copper_names,
                )
                for w in cn_warnings:
                    logging.getLogger(__name__).warning(
                        "Copper name not applied: %s", w,
                    )

            if self._editor_directives:
                self.stage_changed.emit(
                    f"Applying {len(self._editor_directives)} editor "
                    "directive(s)…"
                )
                from fypa.editor_directives import apply_editor_directives
                ed_warnings = apply_editor_directives(
                    loaded, self._editor_directives,
                )
                for w in ed_warnings:
                    logging.getLogger(__name__).warning(
                        "Editor directive not applied: %s", w,
                    )

            self.stage_changed.emit("Assembling FEM problem…")
            with _timer.stage("Build FEM problem"):
                problem, via_segment_records, stub_pieces_by_pair, per_net_layers = (
                    build_problem(loaded)
                )

            # variable_size_maximum_factor 1.0 == uniform mesh; > 1 enables
            # adaptive variable-density meshing (coarse plane interiors).
            mesher_config = _pdn_mesh.Mesher.Config(
                minimum_angle=self._settings.mesh_min_angle_deg,
                maximum_size=self._settings.mesh_max_size_mm,
                variable_size_maximum_factor=(
                    3.0 if getattr(self._settings, "adaptive_mesh", False)
                    else 1.0
                ),
            )

            self.stage_changed.emit(
                f"Meshing + solving ({len(problem.layers)} (layer, net) "
                f"slabs, {len(problem.networks)} networks)…"
            )
            # Capture Python warnings.warn() (e.g. padne's SolverWarning
            # about ground node current) into the log file. Without this,
            # warnings only go to stderr and are invisible from the GUI.
            logging.captureWarnings(True)
            # Forward pdnsolver's per-step INFO log records to the GUI as
            # substage updates so the user can see what the solver is
            # currently doing during the ~20 s opaque "Meshing + solving"
            # stage ("Meshing the connected components", "Constructing the
            # Laplace operators", "Solving the system of equations", …).
            sub_emit = self.substage_changed.emit

            class _SubstageForwarder(logging.Handler):
                def emit(self_h, record: logging.LogRecord) -> None:
                    try:
                        sub_emit(record.getMessage())
                    except Exception:
                        pass

            _substage_handler = _SubstageForwarder(level=logging.INFO)
            _solver_log = logging.getLogger("pdnsolver.solver")
            _mesh_log = logging.getLogger("pdnsolver.mesh")
            _solver_log.addHandler(_substage_handler)
            _mesh_log.addHandler(_substage_handler)
            with _timer.stage("Mesh + solve"):
                try:
                    padne_solution = _pdn_solver.solve(
                        problem, mesher_config=mesher_config,
                    )
                finally:
                    _solver_log.removeHandler(_substage_handler)
                    _mesh_log.removeHandler(_substage_handler)
            si = padne_solution.solver_info
            _log_post = logging.getLogger(__name__)
            _log_post.info(
                "Solver stats: ground_node_current=%.4g A, residual_norm=%.4g",
                si.ground_node_current, si.residual_norm,
            )
            if abs(si.ground_node_current) > 1e-3:
                _log_post.warning(
                    "Ground node current is %.4g A — far from zero. The FEM "
                    "is injecting this current at the reference vertex to "
                    "balance the system. Absolute voltages are unreliable.",
                    si.ground_node_current,
                )

            self.stage_changed.emit("Packaging solution: building metadata…")
            with _timer.stage("Build solve metadata"):
                metadata = build_solve_metadata(
                    loaded, problem,
                    mesher_config=mesher_config,
                    solver_info=padne_solution.solver_info,
                    via_segment_records=via_segment_records,
                    settings=self._settings,
                    stub_pieces_by_pair=stub_pieces_by_pair,
                    per_net_layers=per_net_layers,
                )
            self.stage_changed.emit("Packaging solution: converting result…")
            with _timer.stage("Convert to lean solution"):
                new_solution = to_lean_solution(padne_solution)

            # Persist the solve to the FYPA solve cache so the next
            # "Import Altium Design" can skip both extract and solve.
            # Skip when stackup_overrides / sink_overrides are in play —
            # the resulting solve diverges from the on-disk project, so
            # caching it would poison the next plain load. Failures are
            # non-fatal: the viewer still opens.
            _cache_log = logging.getLogger(__name__)
            if pcbdoc_resolved is None:
                _cache_log.warning(
                    "Solve cache NOT written: pcbdoc_resolved is None "
                    "(see earlier warning from _resolve_pcbdoc).",
                )
            elif (self._stackup_overrides or self._sink_overrides
                    or self._editor_directives):
                _cache_log.info(
                    "Solve cache NOT written: stackup/sink overrides or "
                    "editor directives are active; cached solve would "
                    "diverge from the on-disk project.",
                )
            else:
                try:
                    from fypa.cli import (
                        _project_fingerprint,
                        _save_cached_solution,
                        _solve_cache_path,
                    )
                    self.stage_changed.emit("Packaging solution: saving cache…")
                    solve_fp = _project_fingerprint(
                        self._prjpcb_path, pcbdoc_resolved,
                    )
                    cache_path = _solve_cache_path(
                        self._prjpcb_path, pcbdoc_resolved,
                    )
                    with _timer.stage("Write solve cache"):
                        wrote = _save_cached_solution(
                            self._prjpcb_path, solve_fp,
                            new_solution, metadata,
                            pcbdoc_path=pcbdoc_resolved,
                        )
                    if wrote:
                        _cache_log.info(
                            "Solve cache written to %s "
                            "(%d files in fingerprint).",
                            cache_path,
                            len(solve_fp.get("files") or {}),
                        )
                except Exception as e:
                    _cache_log.warning(
                        "Couldn't write solve cache (%s: %s); ignoring.",
                        type(e).__name__, e,
                    )

            # Final stage before handing off — the new PdnViewer construction
            # in the GUI thread's _on_solve_finished slot is heavy on big
            # boards (tabs, GL widgets, setup HTML for thousands of
            # components) and blocks the dialog from repainting until it
            # finishes. Update the label so the user sees what's actually
            # happening instead of a stale "saving cache" message.
            self.stage_changed.emit("Opening viewer…")
            _timer.log_breakdown()
            self.finished_ok.emit(new_solution, metadata, pristine_loaded)
        except Exception as e:
            import traceback
            self.failed.emit(
                f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            )

    def _apply_stackup_overrides(self, loaded):
        """Return a new :class:`LoadedProject` whose extracted stackup
        has the user-supplied copper thicknesses substituted in.

        ExtractedProject + RawStackupLayer are both frozen dataclasses,
        so we rebuild the stackup tuple via :func:`dataclasses.replace`.
        ``loaded.geometry`` is recomputed too so the displayed per-layer
        conductance reflects the new thickness — the FEM itself reads
        ``loaded.extracted.stackup`` directly via
        :func:`altium_loader.build_per_net_geometry_layers`, so the
        solve correctness only depends on the new ExtractedProject.
        Skipped overrides (layer not in stackup) are silently ignored.
        """
        from dataclasses import replace as _dc_replace
        from fypa.altium_geometry import build_layer_geometries
        from fypa.altium_loader import LoadedProject

        new_stackup = []
        for s in loaded.extracted.stackup:
            if s.layer_id in self._stackup_overrides:
                new_thk = float(self._stackup_overrides[s.layer_id])
                s = _dc_replace(s, copper_thickness_mm=new_thk)
            new_stackup.append(s)

        new_extracted = _dc_replace(
            loaded.extracted, stackup=tuple(new_stackup),
        )
        # Rebuild the legacy single-union geometry — cheap relative to
        # the solve, and keeps the diagnostic_summary honest about the
        # new sheet conductance. LoadedProject is no longer a frozen
        # dataclass (geometry was made lazy to skip ~0.8 s on every
        # solve), so we construct a fresh one explicitly instead of
        # going through dataclasses.replace.
        new_geometry = build_layer_geometries(new_extracted)
        return LoadedProject(
            extracted=new_extracted,
            annotations=loaded.annotations,
            geometry=new_geometry,
        )

    def _apply_sink_overrides(self, loaded) -> None:
        """Mutate ``loaded.annotations.directives`` in-place so every
        SinkSpec whose ``(designator, schdoc, channel_index)`` matches an
        override gets replaced with a copy carrying the new ``current``.
        Unmatched overrides are silently ignored — typically because the
        user deleted a SINK directive (or removed an indexed channel) in
        Altium between solves."""
        from dataclasses import replace as _dc_replace
        from fypa.altium_annotations import SinkSpec
        directives = loaded.annotations.directives
        for i, d in enumerate(directives):
            if not isinstance(d, SinkSpec):
                continue
            key = (d.designator, d.schdoc_name, d.channel_index)
            if key in self._sink_overrides:
                directives[i] = _dc_replace(
                    d, current=float(self._sink_overrides[key]),
                )

    def _clone_loaded_for_edit(self, loaded):
        """Return a copy of ``loaded`` that the sink-override / editor-
        directive passes can mutate without touching the original.

        Only ``annotations`` is copied (its ``directives`` list is what
        those passes rewrite). The heavy ``extracted`` / ``geometry`` are
        read-only from here on — :func:`build_problem` never mutates them —
        so they're shared: the copy costs a few KB, not the tens of MB a
        full clone would. This is what lets the viewer hand the worker its
        own in-memory LoadedProject for a resolve without it being
        corrupted for the next one."""
        import copy as _copy
        from fypa.altium_loader import LoadedProject
        new_annotations = _copy.copy(loaded.annotations)
        new_annotations.directives = list(loaded.annotations.directives)
        return LoadedProject(
            extracted=loaded.extracted,
            annotations=new_annotations,
            geometry=loaded.__dict__.get("geometry"),
            absorbed_bridges=list(loaded.absorbed_bridges),
        )


def _try_solve_cache(prjpcb_path: Path,
                     pcbdoc_path: Path | None) -> tuple[object, dict] | None:
    """Return ``(solution, metadata)`` from the FYPA solve cache if the
    fingerprint matches, else ``None``. Wraps the FYPA helpers so menu
    handlers don't have to know about fingerprints; cache misses + import
    failures are silently treated as ``None``."""
    try:
        from fypa.cli import (
            _project_fingerprint,
            _try_load_cached_solution,
        )
        fp = _project_fingerprint(prjpcb_path, pcbdoc_path)
        cached = _try_load_cached_solution(prjpcb_path, fp,
                                            pcbdoc_path=pcbdoc_path)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Solve-cache check failed (%s); will re-solve.", e,
        )
        return None
    if cached is None or cached[0] is None:
        return None
    return cached


def _choose_pcbdoc(parent: QWidget | None, prjpcb_path: Path,
                   default: Path | None = None) -> tuple[bool, Path | None]:
    """Resolve which .PcbDoc to use for ``prjpcb_path``.

    Returns ``(proceed, selected_path)``:

    * ``(True, path)``  — caller should solve against ``path``.
    * ``(True, None)``  — failed to enumerate, but caller may still
      attempt with altium_monkey's default. Used as a soft fallback.
    * ``(False, None)`` — user cancelled the chooser; caller should
      abort silently.

    A single-PCB project returns the only path without prompting.
    Multi-PCB projects open a modal :class:`QInputDialog.getItem` so the
    user can pick. ``default``, when supplied and present in the list, is
    pre-selected (handy for re-runs of a previously-chosen board).
    """
    try:
        from fypa.altium_extract import list_pcbdoc_paths
        paths = list_pcbdoc_paths(prjpcb_path)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Couldn't enumerate PcbDocs in %s (%s); falling back to default.",
            prjpcb_path, e,
        )
        return True, None
    if not paths:
        QMessageBox.critical(
            parent, "No PcbDoc in project",
            f"{prjpcb_path.name} does not reference any .PcbDoc — FYPA "
            "needs a PCB document for power analysis.",
        )
        return False, None
    if len(paths) == 1:
        return True, paths[0]
    names = [p.name for p in paths]
    default_idx = 0
    if default is not None:
        default_resolved = default.resolve()
        for i, p in enumerate(paths):
            if p.resolve() == default_resolved:
                default_idx = i
                break
    chosen, ok = QInputDialog.getItem(
        parent, "Select PcbDoc",
        f"{prjpcb_path.name} contains multiple PCB documents.\n"
        "Choose which one to solve:",
        names, default_idx, False,
    )
    if not ok or not chosen:
        return False, None
    return True, paths[names.index(chosen)]


def _abort_solve_worker(owner) -> None:
    """Tear down an in-flight ``_SolveWorker`` owned by ``owner``.

    Detaches the worker's result signals (so a late finish can't pop open
    a viewer we no longer want), requests cooperative interruption, then
    falls back to ``QThread.terminate()`` for when the worker is mid-solve
    in a long-running scipy/Triangle C call we can't interrupt politely.
    Forcible termination may leak some Triangle / scipy arena memory until
    process exit, but the interpreter itself stays usable — the user can
    open another project without restarting.

    Also closes the progress dialog and clears ``owner``'s solve refs so
    the late ``QThread.finished`` → ``_cleanup_solve_worker`` chain is a
    no-op. Safe to call when nothing is in flight."""
    worker = getattr(owner, "_solve_worker", None)
    if worker is not None:
        # Detach BOTH the result signals (finished_ok/failed/stage_changed)
        # and QThread.finished. The latter normally fires
        # ``_cleanup_solve_worker``, but we do its work inline below — and
        # leaving it connected would race with a fresh solve started right
        # after cancel: the OLD worker's late ``finished`` would wipe
        # ``owner._solve_worker``, which by then points at the NEW worker.
        for sig_name in ("finished_ok", "failed", "stage_changed",
                         "substage_changed", "finished"):
            sig = getattr(worker, sig_name, None)
            if sig is not None:
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        # If the solver is mid-meshing, tear down the worker-process pool
        # so the children stop after their current Triangle call instead
        # of running to completion as orphans. Safe no-op when no pool is
        # active. Must happen BEFORE terminate(), so the queue close
        # propagates before the thread holding the pool dies.
        try:
            from pdnsolver.solver import cancel_active_mesh_pool
            cancel_active_mesh_pool()
        except Exception as _exc:
            logging.getLogger(__name__).warning(
                "cancel_active_mesh_pool raised %s; carrying on.", _exc,
            )
        worker.requestInterruption()
        worker.terminate()
        # 2 s is plenty for the OS to reap the thread; we don't want to
        # block the GUI indefinitely if something pathological happens.
        worker.wait(2000)
    updater = getattr(owner, "_solve_progress_updater", None)
    if updater is not None:
        updater.stop()
        updater.deleteLater()
        owner._solve_progress_updater = None
    dlg = getattr(owner, "_solve_progress_dlg", None)
    if dlg is not None:
        dlg.close()
        owner._solve_progress_dlg = None
    if worker is not None:
        worker.deleteLater()
        owner._solve_worker = None


# --- Help menu: shared handlers --------------------------------------------
#
# The launcher and the main viewer both expose a Help menu (Open Log /
# About) next to their File menu. _build_help_menu builds it; the two
# module-level handlers below back its actions so the behaviour is
# identical from either window.

# Project home page, shown as a clickable link in the About dialog.
_GITHUB_URL: str = "https://github.com/anarthrous-eda/FYPA"


def _open_log_file(parent: QWidget) -> None:
    """Help > Open Log — open FYPA's log file with the OS default handler.

    The path comes from :data:`fypa.cli._LOG_FILE`, which is anchored next to
    FYPA.exe in a PyInstaller build and in the source tree's ``log/`` folder
    in a dev checkout — so this works identically either way."""
    try:
        from fypa.cli import _LOG_FILE
        log_path = Path(_LOG_FILE)
    except Exception as e:  # pragma: no cover - defensive
        QMessageBox.warning(parent, "Open Log",
                            f"Couldn't locate the log file ({e}).")
        return
    if not log_path.is_file():
        QMessageBox.information(
            parent, "Open Log",
            "No log file has been written yet.\n\n"
            f"Expected location:\n{log_path}",
        )
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path))):
        QMessageBox.warning(
            parent, "Open Log",
            "Couldn't open the log file in the default application.\n\n"
            f"It is located at:\n{log_path}",
        )


def _show_about_dialog(parent: QWidget) -> None:
    """Help > About — a small themed dialog showing the version and a
    clickable link to the project's GitHub page."""
    try:
        from fypa.cli import __version__ as fypa_version
    except Exception:
        fypa_version = "unknown"
    t = _T()
    # QDialog background + text colour come from the app-wide stylesheet;
    # the Close button follows the Fusion palette like every other dialog.
    dlg = QDialog(parent)
    dlg.setWindowTitle("About FYPA")
    icon = _load_app_icon()
    if icon is not None:
        dlg.setWindowIcon(icon)

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(36, 28, 36, 24)
    layout.setSpacing(8)

    fangs = _load_fypa_fangs_pixmap(96)
    if fangs is not None:
        logo = QLabel()
        logo.setPixmap(fangs)
        logo.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo)

    body = QLabel(
        "<div style='text-align:center;'>"
        f"<h2 style='color:{t['fg']}; margin:6px 0 0 0;'>FYPA</h2>"
        f"<p style='color:{t['accent']}; margin:2px 0;'>Altium PDN Analyser</p>"
        f"<p style='color:{t['fg_muted']}; margin:2px 0;'>Version {fypa_version}</p>"
        f"<p style='margin:12px 0 2px 0;'>"
        f"<a href='{_GITHUB_URL}' style='color:{t['accent']};'>{_GITHUB_URL}</a>"
        "</p>"
        "</div>"
    )
    body.setTextFormat(Qt.RichText)
    body.setAlignment(Qt.AlignCenter)
    body.setOpenExternalLinks(True)
    layout.addWidget(body)

    layout.addSpacing(6)
    buttons = QHBoxLayout()
    buttons.addStretch(1)
    close_btn = QPushButton("Close")
    close_btn.setDefault(True)
    close_btn.clicked.connect(dlg.accept)
    buttons.addWidget(close_btn)
    buttons.addStretch(1)
    layout.addLayout(buttons)

    # Widen the dialog to roughly double its natural (content-driven) width
    # so the layout has more breathing room around the logo and text.
    hint = dlg.sizeHint()
    dlg.resize(hint.width() * 2, hint.height())
    dlg.exec()


def _build_help_menu(window) -> None:
    """Add a Help menu (Open Log / About) to *window*'s menu bar.

    Shared by :class:`LauncherWindow` and :class:`PdnViewer` so both show
    the same Help entries immediately after their File menu."""
    help_menu = window.menuBar().addMenu("&Help")

    open_log = QAction("Open &Log", window)
    open_log.setStatusTip(
        "Open the FYPA log file in the system's default application."
    )
    open_log.triggered.connect(lambda: _open_log_file(window))
    help_menu.addAction(open_log)

    about = QAction("&About", window)
    about.setStatusTip("Version information and a link to the project page.")
    about.triggered.connect(lambda: _show_about_dialog(window))
    help_menu.addAction(about)


class LauncherWindow(QMainWindow):
    """Minimal launcher window shown when FYPA is invoked with no project.

    Has just a File menu (Import Altium Design / Open Project File /
    Load Solution / Exit) and a centred
    welcome label. Picking a project runs the same solve worker the main
    viewer uses; on success, a real :class:`PdnViewer` opens and this
    launcher closes itself.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FYPA")
        icon = _load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(640, 360)

        centre = QWidget(self)
        layout = QVBoxLayout(centre)
        layout.setContentsMargins(40, 40, 40, 40)
        t = _T()
        # Use clickable <a> links (with linkActivated) so the user can
        # launch each File-menu action from this welcome screen too.
        link_style = (
            f"color:{t['accent']}; text-decoration:underline; font-weight:600;"
        )
        label = QLabel(
            "<div style='text-align:center;'>"
            f"<h2 style='color:{t['fg']}; margin-top:2px; margin-bottom:0;'>Altium PDN Analyser</h2>"
            f"<p style='color:{t['accent']};'>No project loaded.</p>"
            f"<p style='color:{t['fg_dim']};'>"
            f"<a href='open-project' style='{link_style}'>Import Altium Design&hellip;</a>"
            " (Ctrl+O) to pick a <code>.PrjPcb</code>,<br>"
            f"<a href='open-project-clean' style='{link_style}'>Import Altium Design (Clean)&hellip;</a>"
            " (Ctrl+Shift+L) to force a fresh extract + solve,<br>"
            "or "
            f"<a href='open-solution' style='{link_style}'>Load Solution&hellip;</a>"
            " (Ctrl+Shift+O) to load a saved <code>.pkl</code>."
            "</p>"
            f"<p style='color:{t['fg_dim']}; font-size:smaller; font-style:italic;'>"
            "FYPA is a design-aid tool. Treat its results as guidance "
            "and validate against measurement."
            "</p>"
            "</div>"
        )
        label.setTextFormat(Qt.RichText)
        label.setAlignment(Qt.AlignCenter)
        # Handle the link clicks ourselves and dispatch to the existing
        # menu handlers — don't bounce out to a system browser.
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(
            Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard
        )
        label.linkActivated.connect(self._on_welcome_link)
        layout.setSpacing(0)
        layout.addStretch(1)
        fangs_pm = _load_fypa_fangs_pixmap(256)
        if fangs_pm is not None:
            fangs_label = QLabel()
            fangs_label.setPixmap(fangs_pm)
            fangs_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(fangs_label)
        text_pm = _load_fypa_text_pixmap(50)
        if text_pm is not None:
            text_label = QLabel()
            text_label.setPixmap(text_pm)
            text_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(text_label)
        layout.addWidget(label)
        layout.addStretch(1)
        centre.setStyleSheet(f"background-color: {t['bg']};")
        self.setCentralWidget(centre)

        self._build_menubar()

        # Held across the async solve so Qt + Python keep them alive.
        self._solve_worker: _SolveWorker | None = None
        self._solve_progress_dlg: QProgressDialog | None = None
        self._solve_progress_updater: _SolveProgressUpdater | None = None

    def _on_welcome_link(self, href: str) -> None:
        """Dispatch a click on one of the welcome-label hyperlinks to the
        matching File-menu handler."""
        if href == "open-project":
            self._on_menu_open_project(clean=False)
        elif href == "open-project-clean":
            self._on_menu_open_project(clean=True)
        elif href == "open-solution":
            self._on_menu_open_solution()

    def _build_menubar(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")

        open_proj = QAction("&Import Altium Design…", self)
        open_proj.setShortcut(QKeySequence.Open)  # Ctrl+O
        open_proj.setStatusTip(
            "Pick a .PrjPcb; reuse the cached solution if the project is "
            "unchanged, otherwise extract + solve."
        )
        open_proj.triggered.connect(self._on_menu_open_project)
        file_menu.addAction(open_proj)

        open_proj_clean = QAction("Import Altium Design (&Clean)…", self)
        open_proj_clean.setShortcut("Ctrl+Shift+L")
        open_proj_clean.setStatusTip(
            "Pick a .PrjPcb; ignore any cached design info or solution and "
            "re-extract + re-solve from scratch."
        )
        open_proj_clean.triggered.connect(
            lambda: self._on_menu_open_project(clean=True)
        )
        file_menu.addAction(open_proj_clean)

        open_projfile = QAction("Open &Project File…", self)
        open_projfile.setShortcut("Ctrl+Shift+P")
        open_projfile.setStatusTip(
            "Open a .fypa project file — its linked solution plus any "
            "editor-mode changes."
        )
        open_projfile.triggered.connect(self._on_menu_open_project_file)
        file_menu.addAction(open_projfile)

        file_menu.addSeparator()

        open_sol = QAction("&Load Solution…", self)
        open_sol.setShortcut("Ctrl+Shift+O")
        open_sol.setStatusTip(
            "Open a previously-saved solution pickle (no re-solve)."
        )
        open_sol.triggered.connect(self._on_menu_open_solution)
        file_menu.addAction(open_sol)

        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut(QKeySequence.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        _build_help_menu(self)

    def _on_menu_open_project(self, *, clean: bool = False) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import Altium project (clean)" if clean else "Import Altium project",
            "",
            "Altium project (*.PrjPcb);;All files (*)",
        )
        if not path_str:
            return
        prjpcb_path = Path(path_str)
        proceed, pcbdoc_path = _choose_pcbdoc(self, prjpcb_path)
        if not proceed:
            return
        from fypa.altium_loader import SolveSettings
        settings = SolveSettings()
        settings.apply_to_modules()

        # The solve-cache check (potentially 5–10 s of pickle.load on
        # large boards) is delegated to the worker so the dialog below
        # stays responsive instead of freezing the UI.
        initial_text = (
            f"Checking solve cache for {prjpcb_path.name}…\n"
            "On a cache miss this falls through to a full extract + solve "
            "(10–60 s depending on board size)."
            if not clean else
            f"Loading {prjpcb_path.name} and solving…\n"
            "This can take 10–60 s depending on board size and mesh density."
        )
        dlg = QProgressDialog(initial_text, "Cancel", 0, 0, self)
        dlg.setWindowTitle(
            "Loading project (clean)" if clean else "Loading project"
        )
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        dlg.canceled.connect(self._on_solve_cancelled)
        dlg.show()
        QApplication.processEvents()
        # 44% wider than Qt's auto-sized width so the longer per-stage
        # status messages ("Packaging solution: building metadata…",
        # "Opening viewer…", etc.) aren't truncated.
        _sz = dlg.size()
        dlg.setFixedSize(int(_sz.width() * 1.44), _sz.height())

        worker = _SolveWorker(
            prjpcb_path, settings,
            pcbdoc_selector=str(pcbdoc_path) if pcbdoc_path else None,
            use_design_cache=not clean,
            try_solve_cache_first=not clean,
            parent=self,
        )
        self._solve_worker = worker
        self._solve_progress_dlg = dlg
        # Wires stage_changed / substage_changed to the dialog, with a
        # live elapsed-time counter for the current stage.
        self._solve_progress_updater = _SolveProgressUpdater(dlg, worker, self)
        worker.finished_ok.connect(
            lambda sol, meta, loaded: self._on_solve_finished(
                sol, meta, loaded, settings,
            )
        )
        worker.failed.connect(self._on_solve_failed)
        worker.finished.connect(self._cleanup_solve_worker)
        worker.start()

    def _on_solve_cancelled(self) -> None:
        """User clicked Cancel on the solve progress dialog. Forcibly kill
        the worker — the launcher is already the "home" state, so just stay
        here once the dialog is gone."""
        _abort_solve_worker(self)

    def _cleanup_solve_worker(self) -> None:
        if self._solve_progress_updater is not None:
            self._solve_progress_updater.stop()
            self._solve_progress_updater.deleteLater()
            self._solve_progress_updater = None
        if self._solve_progress_dlg is not None:
            # QProgressDialog.close() routes through reject() → cancel(),
            # which emits canceled — and our canceled handler tears down
            # the in-flight solve. Drop the connection first so closing a
            # dialog whose worker finished naturally doesn't get treated
            # as a user cancel.
            try:
                self._solve_progress_dlg.canceled.disconnect(self._on_solve_cancelled)
            except (RuntimeError, TypeError):
                pass
            self._solve_progress_dlg.close()
            self._solve_progress_dlg = None
        if self._solve_worker is not None:
            self._solve_worker.deleteLater()
            self._solve_worker = None

    def _on_solve_failed(self, message: str) -> None:
        logging.getLogger(__name__).error("Solve failed: %s", message)
        QMessageBox.critical(self, "Solve failed", message)

    def _on_solve_finished(self, new_solution, metadata: dict,
                           loaded_project, new_settings) -> None:
        self._open_viewer_and_close(new_solution, metadata,
                                    initial_settings=new_settings,
                                    loaded_project=loaded_project)

    def _on_menu_open_solution(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open solution pickle", "",
            "Solution pickle (*.pkl);;All files (*)",
        )
        if not path_str:
            return
        try:
            from fypa.cli import _load_solution_pickle
            solution, metadata = _load_solution_pickle(Path(path_str))
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open solution",
                f"Failed to load {path_str}:\n\n{type(e).__name__}: {e}",
            )
            return
        self._open_viewer_and_close(solution, metadata)

    def _on_menu_open_project_file(self) -> None:
        """File > Open Project File… → load a ``.fypa`` and open a viewer
        bound to it (its linked solution + editor directives)."""
        from fypa.project_file import ProjectFile
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open project file", "",
            "FYPA project (*.fypa);;All files (*)",
        )
        if not path_str:
            return
        try:
            proj = ProjectFile.load(Path(path_str))
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open project",
                f"Failed to load {path_str}:\n\n{type(e).__name__}: {e}",
            )
            return
        sol_path = proj.solve_pickle
        if not sol_path or not Path(sol_path).exists():
            sol_path = None
            if proj.prjpcb_path:
                try:
                    from fypa.cli import _solve_cache_path
                    cand = _solve_cache_path(
                        Path(proj.prjpcb_path),
                        Path(proj.pcbdoc_path) if proj.pcbdoc_path else None,
                    )
                    if cand.exists():
                        sol_path = str(cand)
                except Exception:
                    sol_path = None
        if not sol_path:
            QMessageBox.critical(
                self, "Couldn't open project",
                "The project file doesn't point to a readable solution "
                "pickle, and no cached solve was found.",
            )
            return
        try:
            from fypa.cli import _load_solution_pickle
            solution, metadata = _load_solution_pickle(Path(sol_path))
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open project",
                f"Failed to load the linked solution {sol_path}:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return
        self._open_viewer_and_close(
            solution, metadata, project=proj, project_path=Path(path_str),
        )

    def _open_viewer_and_close(self, solution, metadata: dict | None,
                               *, initial_settings=None,
                               loaded_project=None,
                               project=None,
                               project_path=None) -> None:
        try:
            kwargs = {"metadata": metadata}
            if initial_settings is not None:
                kwargs["initial_settings"] = initial_settings
            if loaded_project is not None:
                kwargs["loaded_project"] = loaded_project
            if project is not None:
                kwargs["project"] = project
            if project_path is not None:
                kwargs["project_path"] = project_path
            _t = time.monotonic()
            new_win = PdnViewer(solution, **kwargs)
            logging.getLogger(__name__).info(
                "PdnViewer __init__ took %.2fs", time.monotonic() - _t,
            )
        except Exception as e:
            logging.getLogger(__name__).exception("Failed to open viewer")
            QMessageBox.critical(
                self, "Couldn't open viewer",
                f"Solution loaded but the viewer failed to open:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return
        if project is not None and getattr(project, "editor_directives", None):
            new_win._set_solve_stale(True)
        _register_viewer(new_win)
        app = QApplication.instance()
        # The new viewer's GL widget show() is processed asynchronously,
        # so there's a one-tick window where visible_windows == 0 if we
        # hide/close the launcher synchronously — that trips
        # quitOnLastWindowClosed and the whole process exits. Disable
        # the auto-quit, hide the launcher, then restore the flag on the
        # next event-loop tick (by which time new_win is fully shown).
        prev_quit = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        new_win.show()
        _force_native_window_icon(new_win)
        _set_window_aumid(new_win)
        self.hide()
        QTimer.singleShot(
            0, lambda: app.setQuitOnLastWindowClosed(prev_quit)
        )


class _ProjectSaveDialog(QDialog):
    """The Ctrl+S popup. Two choices: save the project file only, or save
    the project file plus the latest solver run. ``choice`` is ``"project"``,
    ``"all"``, or ``None`` (cancelled) after :meth:`exec`.

    Keyboard: ``S`` (or a second ``Ctrl+S``) picks project-only; ``A`` picks
    save-all when it's available, falling back to project-only otherwise.
    """

    def __init__(self, parent, allow_all: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save project")
        self.setModal(True)
        self.choice: str | None = None
        self._allow_all = bool(allow_all)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Save the FYPA project?"))

        self._save_btn = QPushButton("Save project  (S)")
        self._save_btn.clicked.connect(lambda: self._pick("project"))
        lay.addWidget(self._save_btn)

        self._all_btn = QPushButton("Save project + latest solver run  (A)")
        self._all_btn.setEnabled(self._allow_all)
        if not self._allow_all:
            self._all_btn.setToolTip(
                "No solver run since the last save."
            )
        self._all_btn.clicked.connect(lambda: self._pick("all"))
        lay.addWidget(self._all_btn)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        lay.addWidget(cancel)
        self._save_btn.setDefault(True)

    def _pick(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key_S:          # S or Ctrl+S → project-only save
            self._pick("project")
        elif key == Qt.Key_A:        # A → save-all (or project-only if N/A)
            self._pick("all" if self._allow_all else "project")
        else:
            super().keyPressEvent(event)


class _MessagesSortItem(QTableWidgetItem):
    """QTableWidgetItem that decouples sort key from displayed text.

    QTableWidgetItem aliases ``Qt::EditRole`` to ``Qt::DisplayRole``
    internally (Qt source: ``role = (role == Qt::EditRole ? Qt::DisplayRole
    : role)`` inside ``setData``), so the usual pattern of
    ``setData(Qt.EditRole, sort_key)`` overwrites the cell's display text
    with the sort key — fine when the sort key happens to be the value
    you wanted to show, but in the Messages tab we need the cell to show
    an emoji or a formatted timestamp while still sorting by an int level
    or a float epoch.

    The fix is to stash the sort key on ``Qt::UserRole`` (which Qt leaves
    alone) and override ``__lt__`` so QTableWidget's sort comparison
    uses that key instead of falling back to the display string. If the
    other item happens to be a plain QTableWidgetItem (no UserRole set),
    we defer to the base comparison so the table doesn't crash on a
    mixed column."""

    def __lt__(self, other) -> bool:
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole) if isinstance(other, QTableWidgetItem) else None
        if a is not None and b is not None:
            try:
                return a < b
            except TypeError:
                pass
        return super().__lt__(other)


class PdnViewer(QMainWindow):
    """Main window — composes the side panel and the matplotlib plot area.

    ``metadata`` (if supplied) is the dict bundled into the solve pickle
    by :func:`altium_loader.build_solve_metadata` — used to populate the
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
        # _project_dirty: editor edits not yet written to the .fypa.
        # _settings_dirty: Settings-tab fields differ from the values
        #                  the current solve was run against.
        # _initial_solve_stale: viewer loaded with edits not yet
        #                       reflected by the solve (e.g. .fypa with
        #                       editor directives + a stale solve pickle).
        # _solve_stale: derived = OR of the three above; drives the
        #               Resolve overlay button.
        self._project_dirty: bool = False
        self._settings_dirty: bool = False
        self._initial_solve_stale: bool = False
        self._solve_stale: bool = False
        # Whether a solver run has happened that isn't yet persisted to a
        # pickle — gates the "Save project + latest solver run" option in
        # the Ctrl+S popup. Set True by the resolve handler; a viewer
        # opened from disk starts False (its solution is already saved).
        self._solved_since_save: bool = False
        self._editor_mode: bool = False
        # Current editor selection: a dict carrying 'kind' plus
        # selection-specific keys, or None when nothing is selected.
        self._editor_selection: dict | None = None
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
        # ``initial_settings`` is a :class:`altium_loader.SolveSettings` —
        # passed by the Re-run handler so the new viewer pre-populates the
        # fields with whatever the user just submitted. Falls back to the
        # values recorded in the pickle (which match the FEM that produced
        # this solution) so opening a fresh pickle always shows the truth.
        from fypa.altium_loader import SolveSettings as _SolveSettings
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
        # Throttle timestamp for _pump_busy_ui — caps the marquee-bar
        # repaint rate at ~33 Hz so the pump can be sprinkled in hot
        # inner loops (every polygon, every per-(phys, net) tile) without
        # blowing up overhead. Reset to 0.0 when the popup opens.
        self._last_pump_time: float = 0.0

        # Index every padne Layer by (physical, net). The net here is what we
        # used to call "rail" in the naming convention — but now we group
        # bridged nets into rails, so the underlying per-net layer index
        # still keys on the literal net name.
        self._index_by_pair: dict[tuple[str, str], int] = {}
        physicals: set[str] = set()
        for i, layer in enumerate(solution.problem.layers):
            phys, net = _split_composite_name(layer.name)
            self._index_by_pair[(phys, net)] = i
            physicals.add(phys)

        # Build rail groups from RESISTOR bridges + SOURCE/SINK/REGULATOR
        # terminals (the metadata bundle). The Rail dropdown shows the
        # PRIMARY net per group ("+3V3", not "3V3_SW"); the renderer combines
        # every per-net layer in the group into one heatmap so the user sees
        # both +3V3 and 3V3_SW copper when they pick "+3V3".
        self._rail_names: list[str]
        self._rail_to_members: dict[str, list[str]]
        self._rail_names, self._rail_to_members = self._compute_rail_groups(metadata)

        # Sort layers into physical stackup order (Top → inner → Bottom).
        # The stackup metadata is the authority: its rows are already in
        # physical order (top first), independent of layer name or id —
        # which matters for boards like Corvette whose layers are named
        # "L1".."L16" rather than "Top Layer"/"Bottom Layer". The name
        # heuristic below is only a fallback for layers absent from the
        # metadata (or when no metadata was bundled): inner-layer names
        # start with "L" + a number, parsed so L10 sorts after L9.
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
        # Rank in the physical stackup (0 = topmost). Used to decide draw
        # order when multiple layers are visible — we want the topmost
        # checked layer to render last so it sits visually on top.
        self._phys_stackup_rank: dict[str, int] = {}
        for rank, name in enumerate(self._physicals):
            self._phys_stackup_rank[name] = rank

        # Map physical-layer display name → Altium layer id. We need this so
        # the directive-pin overlay can filter pins to the layer currently
        # being viewed (pins carry a layer_id, the dropdown carries a name).
        self._phys_name_to_layer_id: dict[str, int] = {}
        for row in (metadata.get("stackup", []) if metadata else []):
            self._phys_name_to_layer_id[row["name"]] = row["layer_id"]

        # World-z (mm) at the centre of each physical copper layer, derived
        # from the cumulative copper + dielectric thicknesses in the stackup
        # metadata. z=0 is the topmost layer's centreline; lower layers are
        # negative. Using real distances (instead of a uniform rank spacing)
        # makes 3D via cylinders honour the actual stackup — adjacent inner
        # planes look thin, top-to-bottom vias look long.
        self._phys_z_mm: dict[str, float] = {}
        z_accum = 0.0
        rows = (metadata.get("stackup", []) if metadata else []) or []
        for i, row in enumerate(rows):
            name = row.get("name")
            t_cu = float(row.get("copper_thickness_mm") or 0.0)
            if name is not None:
                self._phys_z_mm[name] = -(z_accum + 0.5 * t_cu)
            if i + 1 < len(rows):
                z_accum += t_cu + float(row.get("dielectric_thickness_mm") or 0.0)
        # ``self._rails`` kept as an alias of the rail-group names so the
        # rest of the UI code (rail combo population, mode helpers) doesn't
        # need to know the rails came from group reduction.
        self._rails = self._rail_names

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
        # a yellow ring is drawn on the GL viewer at this point — always
        # shown, even if "Show pin markers" is off. Cleared by another
        # jump or by :meth:`_clear_via_highlight`.
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
        self._render()

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
        if metadata is None:
            return [], {}

        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]  # path compression
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        primary_candidates: set[str] = set()

        for d in metadata.get("directives", []):
            role = d.get("role", "")
            terms = d.get("terminals") or {}
            nets_per_term: list[set[str]] = []
            for _tname, t in terms.items():
                nets = {p.get("net") for p in t.get("pins", []) if p.get("net")}
                req = t.get("requested_net")
                for n in nets:
                    find(n)  # ensure presence in union-find
                if nets:
                    nets_per_term.append(nets)
                # The directive names this terminal's net (PDN_*_NET). When
                # its pins resolved onto a SERIES-bridged net instead, tie the
                # named net into the group so the rail keeps the user's name.
                if req:
                    find(req)
                    for n in nets:
                        union(req, n)
                # The rail's display name is what the directive ASKED for —
                # the named net — not the bridged net its pins landed on.
                # Fall back to the pin nets only for a *_PINS-override
                # terminal, which has no named net.
                if role in ("SOURCE", "SINK", "REGULATOR"):
                    primary_candidates.update({req} if req else nets)
            if role == "RESISTOR" and len(nets_per_term) == 2:
                # Bridge every net in one terminal with every net in the other.
                for a in nets_per_term[0]:
                    for b in nets_per_term[1]:
                        union(a, b)

        # Materialise groups and pick a primary for each.
        groups: dict[str, set[str]] = {}
        for net in list(parent.keys()):
            groups.setdefault(find(net), set()).add(net)

        rail_to_members: dict[str, list[str]] = {}
        for root, members in groups.items():
            primaries = members & primary_candidates
            if not primaries:
                continue
            # Pick the "most rail-looking" primary name: prefer leading '+',
            # then ground-ish names, then alphabetical.
            def _primary_rank(n: str) -> tuple[int, str]:
                if n.startswith("+"):
                    return (0, n)
                if n.lower() in {"0v", "gnd", "ground", "vss"}:
                    return (1, n)
                return (2, n)
            primary = sorted(primaries, key=_primary_rank)[0]
            rail_to_members[primary] = sorted(members)

        def _rail_sort_key(rail: str) -> tuple[int, str]:
            if rail.lower() in {"0v", "gnd", "ground", "vss"}:
                return (2, rail)
            if rail.startswith("+"):
                return (0, rail)
            return (1, rail)
        rail_names = sorted(rail_to_members.keys(), key=_rail_sort_key)
        return rail_names, rail_to_members

    def showEvent(self, event) -> None:
        """Apply the deferred ``showMaximized`` once Qt has actually shown the
        window. Doing this in ``__init__`` is unreliable on Windows — the
        platform window doesn't exist yet so the maximise request gets lost.
        """
        super().showEvent(event)
        if getattr(self, "_pending_maximize", False):
            self._pending_maximize = False
            self.showMaximized()
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

        self._all_layers_eye = EyeButton(visible=True)
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
        self._outlines_btn = OutlineToggleButton(on=False)
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
            eye = EyeButton(visible=True)
            eye.toggled_visible.connect(self._on_layer_eye_toggled)
            eye2 = EyeButton(
                visible=False,
                tip_show="Show all copper on this layer",
                tip_hide="Hide all copper on this layer",
            )
            eye2.toggled_visible.connect(self._on_layer_eye2_toggled)
            fill = FillToggleButton(solid=True)
            fill.toggled_fill.connect(self._on_layer_fill_toggled)
            transp = TransparencyButton(step=0)
            transp.toggled_transparency.connect(
                self._on_layer_transparency_toggled)
            row = self._build_layer_row_widget(
                eye, swatch_color=self._layer_color_for(phys),
                label_text=phys, bold=False,
                second_eye=eye2, fill_btn=fill, transparency_btn=transp,
            )
            item = QListWidgetItem()
            item.setFlags(Qt.ItemIsEnabled)
            self.layer_list.addItem(item)
            item.setSizeHint(row.sizeHint())
            self.layer_list.setItemWidget(item, row)
            self._layer_eye_buttons.append((phys, eye))
            self._layer_eye2_buttons.append((phys, eye2))
            self._layer_fill_buttons.append((phys, fill))
            self._layer_transparency_buttons.append((phys, transp))

        self._sync_all_layers_eye()
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
            "bridges them). 'All Rails' toggles every rail at once."
        )

        self._rail_eye_buttons: list[tuple[str, EyeButton]] = []

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

        for rail in self._rails:
            eye = EyeButton(visible=False)
            eye.toggled_visible.connect(self._on_rail_eye_toggled)
            row = self._build_layer_row_widget(
                eye, swatch_color=None,
                label_text=rail, bold=False,
            )
            item = QListWidgetItem()
            item.setFlags(Qt.ItemIsEnabled)
            self.rail_list.addItem(item)
            item.setSizeHint(row.sizeHint())
            self.rail_list.setItemWidget(item, row)
            self._rail_eye_buttons.append((rail, eye))

        # Default: first rail visible, others hidden — matches the previous
        # combo's behaviour of starting at index 0.
        if self._rail_eye_buttons:
            self._rail_eye_buttons[0][1].setVisibleState(True, emit=False)
        self._sync_all_rails_eye()

        approx_rail_h = self.rail_list.sizeHintForRow(0) or 22
        self.rail_list.setFixedHeight(
            (len(self._rails) + 1) * approx_rail_h + 6
        )
        side.addWidget(self.rail_list)

        side.addSpacing(8)
        side.addWidget(QLabel("<b>Mode</b>"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([m[0] for m in _MODES])
        side.addWidget(self.mode_combo)

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

        self.show_markers_box = QCheckBox("Show pin markers (I)")
        self.show_markers_box.setChecked(True)
        self.show_markers_box.setToolTip(
            "When on, the directive pin markers (SOURCE / SINK / SERIES / "
            "REGULATOR / VIA) and their legend are drawn on top of the "
            "heatmap. Turn off for an unobstructed view of the copper."
        )
        side.addWidget(self.show_markers_box)

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

        # Wrap side layout in a fixed-width container, then put that inside a
        # QScrollArea so the panel scrolls vertically when its contents exceed
        # the window height (e.g. on boards with many copper layers).
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(260)

        side_scroll = QScrollArea()
        side_scroll.setWidget(side_widget)
        # Resizable=True is essential: with =False, the inner widget uses its
        # width-agnostic sizeHint() for height, which under-estimates the
        # height of word-wrapped labels (e.g. summary_label) at the actual
        # 260px width, and the controls below it overlap. =True makes the
        # layout reflow at the real width.
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Always-on vertical scrollbar so the inner viewport width is
        # constant regardless of whether scrolling is needed; without this,
        # adding/removing the scrollbar would shift content widths around.
        side_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        # Reserve room for the vertical scrollbar so its appearance doesn't
        # crop the 260px-wide content. 18px covers the default Fusion/Win
        # scrollbar extent with a hair of margin.
        side_scroll.setFixedWidth(260 + 18)
        side_scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {_T()['bg']}; }}"
        )
        outer.addWidget(side_scroll)
        self._sidebar_scroll = side_scroll

        # Slim vertical splitter handle that toggles the sidebar's visibility
        # so the user can give the viewport extra real estate. Custom-painted
        # triangle stays crisp at 14px; Unicode arrow glyphs were fuzzy.
        # Hotkey "B" mirrors the click.
        self._sidebar_toggle_btn = SidebarToggleButton()
        self._sidebar_toggle_btn.setToolTip(
            "Collapse / expand the side panel (B)"
        )
        self._sidebar_toggle_btn.clicked.connect(self._toggle_sidebar)
        outer.addWidget(self._sidebar_toggle_btn)

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
        self._scale_overlay.show()
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

        plot_widget = QWidget()
        plot_widget.setLayout(plot_layout)
        outer.addWidget(plot_widget, 1)

        # Right-hand panel — overlaid on the GL viewer's right edge
        # rather than added to the outer layout. Floating it as a child
        # widget (same pattern as ``_scale_overlay``) keeps the viewport's
        # pixel size and pan / zoom centre invariant when the panel
        # appears, so the visible copper doesn't shift sideways. Carries
        # the PDN editor form in editor mode and the copper-properties
        # form in viewer mode.
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
        self.mode_combo.currentTextChanged.connect(self._render_with_busy_popup)
        self.rail_only_box.toggled.connect(self._render_with_busy_popup)
        self.show_markers_box.toggled.connect(self._render)
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

    # --- Layer-list helpers -------------------------------------------------

    # Fixed colour palette for the layer-list swatches. Matches Altium's
    # default "Signal And Plane Layers" palette so the swatches in this
    # viewer line up with what the user sees in Altium's Layers dialog.
    # Top/Bottom get dedicated colours; inner layers index into a cycle
    # by their position in the stackup (1st inner = cycle[0], etc.).
    _LAYER_SWATCH_COLOURS: dict[str, str] = {
        "top":    "#ff0000",
        "bottom": "#0000ff",
    }
    _INNER_LAYER_CYCLE: tuple[str, ...] = (
        "#bc8e00",  # 1st inner (L2)
        "#70dbfa",  # 2nd inner (L3)
        "#00cc66",  # 3rd inner (L4)
        "#9966ff",  # 4th inner (L5)
        "#00ffff",  # 5th inner (L6)
        "#800080",  # 6th inner (L7)
        "#ff00ff",  # 7th inner (L8)
        "#808000",  # 8th inner (L9)
        "#ffff00",  # 9th inner (L10)
        "#808080",  # 10th inner (L11)
        "#ffffff",  # 11th inner (L12)
        "#800080",  # 12th inner (L13)
        "#008080",  # 13th inner (L14)
        "#c0c0c0",  # 14th inner (L15)
    )

    def _layer_color_for(self, phys: str) -> str:
        # Colour is keyed purely on the layer's position in the stackup
        # ordering — not its name or id. Board layer names vary ("Top Layer"
        # vs "L1"), so a name check mis-colours designs like Corvette whose
        # layers are "L1".."L16". Ordering is reliable: the first layer in
        # the stackup is always red, the last always blue.
        rank = self._phys_stackup_rank.get(phys, 0)
        if rank == 0:
            return self._LAYER_SWATCH_COLOURS["top"]
        if rank == len(self._physicals) - 1:
            return self._LAYER_SWATCH_COLOURS["bottom"]
        # Inner layer — pick deterministically by stackup position so the
        # same layer keeps the same colour across re-renders. The 1st inner
        # layer has rank 1; subtract 1 to map it to cycle[0] (the cyan that
        # Altium uses for Mid Layer 1).
        idx = max(0, rank - 1)
        return self._INNER_LAYER_CYCLE[idx % len(self._INNER_LAYER_CYCLE)]

    def _build_layer_row_widget(self, eye: EyeButton, *,
                                  swatch_color: str | None,
                                  label_text: str, bold: bool,
                                  second_eye: EyeButton | None = None,
                                  fill_btn: FillToggleButton | None = None,
                                  outline_btn: OutlineToggleButton | None = None,
                                  transparency_btn: TransparencyButton | None = None,
                                  ) -> QWidget:
        """Build a single row for the layer list: eye(s) + swatch + name.

        ``second_eye``, when given, sits immediately right of the primary
        eye — the Physical layers control uses it for the per-layer "show
        all copper on this layer" toggle. ``fill_btn``, when given, is
        pinned to the far right — the wire-mesh / solid fill style for that
        all-copper view. ``transparency_btn``, when given, sits immediately
        left of ``fill_btn`` — the per-row transparency cycler that fades
        the all-copper overlay. ``outline_btn``, when given, sits just left
        of ``transparency_btn`` — the layer-outline toggle on the "All
        Rails" row (outlines only trace rail copper, so it belongs there).
        Per-rail and per-layer rows pass none of these and keep the
        original single-eye layout."""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(2, 1, 6, 1)
        layout.setSpacing(6)
        layout.addWidget(eye)
        if second_eye is not None:
            layout.addWidget(second_eye)
        if swatch_color is not None:
            swatch = QLabel()
            pix = QPixmap(14, 14)
            pix.fill(QColor(swatch_color))
            swatch.setPixmap(pix)
            swatch.setFixedSize(14, 14)
            layout.addWidget(swatch)
        else:
            # Keep the name label aligned with rows that have a swatch.
            spacer = QLabel()
            spacer.setFixedSize(14, 14)
            layout.addWidget(spacer)
        name = QLabel(label_text)
        _fg = _T()["fg"]
        name.setStyleSheet(
            f"QLabel {{ color: {_fg}; font-weight: bold; }}"
            if bold else f"QLabel {{ color: {_fg}; }}"
        )
        layout.addWidget(name)
        layout.addStretch(1)
        if outline_btn is not None:
            layout.addWidget(outline_btn)
        if transparency_btn is not None:
            layout.addWidget(transparency_btn)
        if fill_btn is not None:
            layout.addWidget(fill_btn)
        return w

    def _on_layer_eye_toggled(self, _on: bool) -> None:
        """An individual layer's eye was clicked."""
        self._sync_all_layers_eye()
        self._on_layer_visibility_changed()

    def _on_all_layers_toggled(self, on: bool) -> None:
        """The "All Layers" eye was clicked — show or hide every layer."""
        for _name, eye in self._layer_eye_buttons:
            eye.setVisibleState(on, emit=False)
        self._on_layer_visibility_changed()

    def _sync_all_layers_eye(self) -> None:
        """Reflect "any layer visible" in the All Layers eye state."""
        any_visible = any(eye.isVisibleState()
                          for _n, eye in self._layer_eye_buttons)
        self._all_layers_eye.setVisibleState(any_visible, emit=False)

    def _set_layer_visible(self, name: str, on: bool, *, emit: bool = True) -> None:
        """Programmatically toggle a named layer's visibility."""
        for nm, eye in self._layer_eye_buttons:
            if nm == name:
                eye.setVisibleState(on, emit=emit)
                break
        self._sync_all_layers_eye()

    def _on_layer_visibility_changed(self, _item: object = None) -> None:
        """Layer eye toggled in the layer list → re-render."""
        self._render_with_busy_popup()

    def _visible_layers(self) -> list[str]:
        """Names of the physical layers currently visible (eye open),
        in stackup order (top first)."""
        return [name for name, eye in self._layer_eye_buttons
                if eye.isVisibleState()]

    # --- Physical-layer "all copper" (second eye + fill toggle) -------------

    def _on_layer_eye2_toggled(self, _on: bool) -> None:
        """A layer's 'all copper' eye was clicked. The all-copper view is
        independent of the FEM heatmap, so only the overlay geometry needs
        rebuilding — no full re-render."""
        self._sync_all_layers_eye2()
        rails = self._visible_rails()
        self._run_with_busy_popup(lambda: self._refresh_overlay_geometry(rails))

    def _on_all_layers_eye2_toggled(self, on: bool) -> None:
        """The "All Layers" all-copper eye — show/hide all copper on every
        layer at once."""
        for _name, eye2 in self._layer_eye2_buttons:
            eye2.setVisibleState(on, emit=False)
        rails = self._visible_rails()
        self._run_with_busy_popup(lambda: self._refresh_overlay_geometry(rails))

    def _sync_all_layers_eye2(self) -> None:
        """Reflect "any layer's all-copper shown" in the All Layers eye."""
        any_on = any(eye2.isVisibleState()
                     for _n, eye2 in self._layer_eye2_buttons)
        self._all_layers_eye2.setVisibleState(any_on, emit=False)

    def _on_layer_fill_toggled(self, _solid: bool) -> None:
        """A layer's all-copper wire-mesh / solid fill toggle flipped."""
        self._refresh_overlay_geometry(self._visible_rails())

    def _on_all_layers_fill_toggled(self, solid: bool) -> None:
        """The "All Layers" fill toggle — set every layer's all-copper fill
        style at once."""
        for _name, fill in self._layer_fill_buttons:
            fill.setSolid(solid, emit=False)
        self._refresh_overlay_geometry(self._visible_rails())

    def _on_layer_transparency_toggled(self, _step: int) -> None:
        """A layer's transparency was cycled. Fades both the heatmap mesh
        (per-vertex alpha) and the all-copper overlay for that layer."""
        self._push_mesh_alpha()
        self._refresh_overlay_geometry(self._visible_rails())

    def _on_all_layers_transparency_toggled(self, step: int) -> None:
        """The "All Layers" transparency cycler — fan out to every per-
        layer button. Updates the mesh alpha + all-copper overlay once
        after the fan-out so the GPU only sees a single re-push."""
        for _name, transp in self._layer_transparency_buttons:
            transp.setStep(step, emit=False)
        self._push_mesh_alpha()
        self._refresh_overlay_geometry(self._visible_rails())

    def _on_rail_eye_toggled(self, _on: bool) -> None:
        """An individual rail's eye was clicked."""
        self._sync_all_rails_eye()
        self._sync_rail_only_visibility()
        self._render_with_busy_popup()

    def _on_all_rails_toggled(self, on: bool) -> None:
        """The "All Rails" eye was clicked — show or hide every rail."""
        for _name, eye in self._rail_eye_buttons:
            eye.setVisibleState(on, emit=False)
        self._sync_rail_only_visibility()
        self._render_with_busy_popup()

    def _sync_all_rails_eye(self) -> None:
        """Reflect "any rail visible" in the All Rails eye state."""
        any_visible = any(eye.isVisibleState()
                          for _n, eye in self._rail_eye_buttons)
        self._all_rails_eye.setVisibleState(any_visible, emit=False)

    def _visible_rails(self) -> list[str]:
        """Names of the rails currently visible (eye open), in the
        sort order they were registered (matches the rail list UI)."""
        return [name for name, eye in self._rail_eye_buttons
                if eye.isVisibleState()]

    def _rail_only_meaningful(self) -> bool:
        """True iff any currently-visible rail has SERIES-bridged sibling
        nets — i.e. the "Show only rail net" filter would actually hide
        something. When no visible rail has siblings, toggling the box
        changes nothing, so the control is hidden by
        :meth:`_sync_rail_only_visibility`."""
        for name in self._visible_rails():
            if len(self._rail_to_members.get(name, [name])) > 1:
                return True
        return False

    def _sync_rail_only_visibility(self) -> None:
        """Show / hide the "Show only rail net" checkbox so it only appears
        when at least one visible rail is bridged to siblings via a
        RESISTOR / SERIES directive. The box's checked state is preserved
        across hides, so toggling a bridged rail back on restores the
        previous filter."""
        box = getattr(self, "rail_only_box", None)
        if box is None:
            return
        box.setVisible(self._rail_only_meaningful())

    # --- Overlays control ----------------------------------------------------

    def _init_overlay_state(self) -> None:
        """Build the Overlays control's state dict — the source of truth
        that :meth:`_build_overlay_list` renders rows from.

        One entry per overlay layer. ``split`` says whether the layer is
        shown as one merged row or separate Top / Bottom rows; each row
        variant (``both`` / ``top`` / ``bottom``) tracks ``vis`` — one of
        ``None`` (hidden), ``"rails"`` (visible on the selected rails only)
        or ``"all"`` (visible everywhere) — and ``solid`` (the fill style).

        Draw colour lives separately. ``self._overlay_colors`` holds the
        primary colour per layer — used by a merged row and the Top side of
        a split one. ``self._overlay_bottom_colors`` holds the distinct
        colour for the layer's Bottom side (``None`` == follow the primary);
        it applies whether the layer is split or merged. Both are seeded
        from :data:`_OVERLAY_DEFAULT_COLORS` /
        :data:`_OVERLAY_DEFAULT_BOTTOM_COLORS` and then overridden by
        anything the open .fypa project saved.
        """
        self._overlay_state: dict[str, dict] = {}
        for key, _label, has_sides in _OVERLAY_LAYERS:
            solid0 = _overlay_default_solid(key)
            # ``alpha_step`` is the TransparencyButton's step count (0..4);
            # 0 == 0% transparent == fully opaque. New rows start opaque.
            st: dict = {
                "split": False,
                "both": {"vis": None, "solid": solid0, "alpha_step": 0},
            }
            if has_sides:
                st["top"] = {"vis": None, "solid": solid0, "alpha_step": 0}
                st["bottom"] = {"vis": None, "solid": solid0, "alpha_step": 0}
            self._overlay_state[key] = st

        self._overlay_colors: dict[str, tuple[float, float, float]] = dict(
            _OVERLAY_DEFAULT_COLORS)
        # None == the Bottom side follows the primary colour; a tuple == a
        # distinct Bottom colour (a built-in default from
        # _OVERLAY_DEFAULT_BOTTOM_COLORS, or one the user later pins). It is
        # applied to the Bottom side whether the layer is split or merged.
        self._overlay_bottom_colors: dict[
            str, tuple[float, float, float] | None] = {
            key: _OVERLAY_DEFAULT_BOTTOM_COLORS.get(key)
            for key in _OVERLAY_DEFAULT_COLORS
        }
        self._load_overlay_colors_from_project()

    def _load_overlay_colors_from_project(self) -> None:
        """Override the built-in overlay colours with any saved in the open
        .fypa project (``viewer_settings["overlay_colors"]``).

        Each entry is ``{"primary": [r, g, b], "bottom": [r, g, b]}`` —
        ``"bottom"`` present only when the user pinned a distinct Bottom
        colour. A bare ``[r, g, b]`` list is also accepted (the colours
        were stored that way before split colours existed). Unknown keys
        and malformed values are skipped so an older or hand-edited project
        still loads cleanly."""
        proj = getattr(self, "_project", None)
        if proj is None:
            return
        saved = (getattr(proj, "viewer_settings", None) or {}).get(
            "overlay_colors") or {}

        clamp = lambda c: min(1.0, max(0.0, c))

        def _rgb(v):
            try:
                return (clamp(float(v[0])), clamp(float(v[1])),
                        clamp(float(v[2])))
            except (TypeError, ValueError, IndexError):
                return None

        for key, val in saved.items():
            if key not in self._overlay_colors:
                continue
            if isinstance(val, dict):
                primary, bottom = _rgb(val.get("primary")), _rgb(
                    val.get("bottom"))
            else:
                primary, bottom = _rgb(val), None   # legacy flat form
            if primary is not None:
                self._overlay_colors[key] = primary
            if bottom is not None:
                self._overlay_bottom_colors[key] = bottom

    def _build_overlay_list(self) -> None:
        """(Re)populate the Overlays QListWidget from ``self._overlay_state``.

        A split layer contributes two rows (Top / Bottom); a merged layer
        one. Called once at construction and again whenever a split toggle
        flips, so the list re-sizes to whatever the current state needs."""
        self.overlay_list.clear()
        n_rows = 0
        for key, label, has_sides in _OVERLAY_LAYERS:
            if has_sides and self._overlay_state[key]["split"]:
                self._add_overlay_row(key, "top", f"Top {label}",
                                      splittable=True)
                self._add_overlay_row(key, "bottom", f"Bottom {label}",
                                      splittable=False)
                n_rows += 2
            else:
                self._add_overlay_row(key, "both", label,
                                      splittable=has_sides)
                n_rows += 1
        row_h = self.overlay_list.sizeHintForRow(0) or 24
        self.overlay_list.setFixedHeight(n_rows * row_h + 6)

    def _add_overlay_row(self, key: str, variant: str, label_text: str, *,
                         splittable: bool) -> None:
        """Append one overlay row widget to the list."""
        row = self._build_overlay_row_widget(key, variant, label_text,
                                             splittable=splittable)
        item = QListWidgetItem()
        item.setFlags(Qt.ItemIsEnabled)
        self.overlay_list.addItem(item)
        item.setSizeHint(row.sizeHint())
        self.overlay_list.setItemWidget(item, row)

    def _build_overlay_row_widget(self, key: str, variant: str,
                                  label_text: str, *,
                                  splittable: bool) -> QWidget:
        """Build one row of the Overlays control.

        Layout: ``[rails eye][all eye][colour] <label> … [split] [fill]``.
        The two eyes are a mutually-exclusive pair (selected-rails-only vs.
        everywhere), so the row is hidden / rails / all. The colour swatch
        opens a picker for the overlay's draw colour. ``splittable`` rows
        carry the split toggle just left of the fill toggle; on an
        already-split layer the Top row keeps it (acting as a merge button)
        while the Bottom row gets an aligning spacer in its place."""
        sub = self._overlay_state[key][variant]
        is_board = key == "board_outline"
        # Overlays with no per-net association get only the "show
        # everywhere" eye — a "show on the selected rails only" eye is
        # meaningless when nothing ties the overlay to a rail. That covers
        # the mechanical board outline and the silkscreen "Overlay" layer
        # (graphics-only). Every other overlay keeps the rails-only eye.
        has_rails_eye = key not in ("board_outline", "silkscreen")
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(2, 1, 6, 1)
        layout.setSpacing(4)

        # The "show everywhere" eye is on every row. Its companion
        # "show on the selected rails only" eye sits to its left — except
        # on rows with no per-net association (see ``has_rails_eye``), which
        # get an equal-width spacer instead, keeping the "show everywhere"
        # eye in the same column as every other row. The two eyes (when both
        # present) are mutually exclusive: the row is hidden / rails /
        # everywhere.
        all_eye = EyeButton(
            visible=(sub["vis"] == "all"),
            tip_show="Show everywhere",
            tip_hide="Hide (shown everywhere)",
        )
        if not has_rails_eye:
            rails_eye = None
            rails_slot: QWidget = QLabel()
            rails_slot.setFixedSize(all_eye.width(), all_eye.height())
        else:
            rails_eye = EyeButton(
                visible=(sub["vis"] == "rails"),
                tip_show="Show on the selected rails only",
                tip_hide="Hide (shown on the selected rails only)",
            )
            rails_eye.toggled_visible.connect(
                lambda on: self._on_overlay_eye(key, variant, "rails", on,
                                                rails_eye, all_eye)
            )
            rails_slot = rails_eye
        all_eye.toggled_visible.connect(
            lambda on: self._on_overlay_eye(key, variant, "all", on,
                                            rails_eye, all_eye)
        )
        layout.addWidget(rails_slot)
        layout.addWidget(all_eye)

        # Colour swatch — opens a picker for this overlay's draw colour.
        # A merged row and the Top row share the layer's primary colour;
        # the Bottom row of a split layer carries its own swatch (its own
        # colour, or the primary while it is still following — see
        # _overlay_swatch_rgb / _on_overlay_color).
        colour_btn = OverlayColorButton(
            rgb=self._overlay_swatch_rgb(key, variant))
        colour_btn.colorChanged.connect(
            lambda rgb, k=key, v=variant: self._on_overlay_color(k, v, rgb)
        )
        layout.addWidget(colour_btn)

        name = QLabel(label_text)
        name.setStyleSheet(f"QLabel {{ color: {_T()['fg']}; }}")
        layout.addWidget(name)
        layout.addStretch(1)

        # Split / merge toggle — only on rows that own a Top + Bottom pair.
        # Sits just left of the fill toggle at the right of the row. Other
        # rows (vias, and the Bottom side of a split layer) get a spacer the
        # same width so the fill toggle still lines up across every row.
        if splittable:
            split_btn = SplitButton(split=self._overlay_state[key]["split"])
            split_btn.toggled_split.connect(
                lambda on: self._on_overlay_split(key, on)
            )
            layout.addWidget(split_btn)
        else:
            # Size off all_eye, not rails_eye — the board-outline row has
            # no rails_eye (it is None there).
            spacer = QLabel()
            spacer.setFixedSize(all_eye.width(), all_eye.height())
            layout.addWidget(spacer)

        # Transparency control — sits immediately left of the wire-mesh /
        # solid fill toggle on every row (including the board outline, which
        # still benefits from being able to fade its outline ribbon).
        transp_btn = TransparencyButton(step=int(sub.get("alpha_step", 0) or 0))
        transp_btn.toggled_transparency.connect(
            lambda step: self._on_overlay_transparency(key, variant, step)
        )
        layout.addWidget(transp_btn)

        # Wire-mesh / solid fill toggle, pinned to the far right of the row.
        # The board outline is always a fixed-width ribbon — solid vs
        # wire-mesh is meaningless — so that row gets an aligning spacer in
        # the fill toggle's place instead.
        if is_board:
            fill_slot: QWidget = QLabel()
            fill_slot.setFixedSize(all_eye.width(), all_eye.height())
            layout.addWidget(fill_slot)
        else:
            fill_btn = FillToggleButton(solid=sub["solid"])
            fill_btn.toggled_fill.connect(
                lambda on: self._on_overlay_fill(key, variant, on)
            )
            layout.addWidget(fill_btn)
        return w

    def _on_overlay_eye(self, key: str, variant: str, scope: str, on: bool,
                        rails_eye: EyeButton | None,
                        all_eye: EyeButton) -> None:
        """One of an overlay row's eyes toggled. When the row has both
        eyes they are a mutually-exclusive pair, so turning one on turns
        the other off — the row ends up hidden / shown-on-rails /
        shown-everywhere. The board-outline row has only the "everywhere"
        eye (``rails_eye`` is None), so there is no companion to clear."""
        sub = self._overlay_state[key][variant]
        if on:
            sub["vis"] = scope
            other = all_eye if scope == "rails" else rails_eye
            if other is not None:
                other.setVisibleState(False, emit=False)
        elif sub["vis"] == scope:
            sub["vis"] = None
        self._on_overlay_changed()

    def _on_overlay_fill(self, key: str, variant: str, solid: bool) -> None:
        """An overlay row's wire-mesh / solid fill toggle flipped."""
        self._overlay_state[key][variant]["solid"] = bool(solid)
        self._on_overlay_changed()

    def _on_overlay_transparency(self, key: str, variant: str,
                                  step: int) -> None:
        """An overlay row's transparency control cycled."""
        self._overlay_state[key][variant]["alpha_step"] = int(step)
        self._on_overlay_changed()

    def _overlay_swatch_rgb(self, key: str,
                            variant: str) -> tuple[float, float, float]:
        """Colour the swatch button on a ``(key, variant)`` row should show.

        The Bottom row of a split layer shows its own pinned colour, or the
        primary colour while it is still following it. Every other row (a
        merged ``both`` row, or the Top row) shows the primary colour."""
        if variant == "bottom":
            bot = self._overlay_bottom_colors.get(key)
            if bot is not None:
                return bot
        return self._overlay_colors[key]

    def _overlay_color_for(self, key: str,
                           side: str) -> tuple[float, float, float]:
        """RGB an overlay's geometry on ``side`` ("top" / "bottom") is drawn
        in. The Bottom side uses its own distinct colour whenever one is set
        — whether the layer is split or merged — so a merged row still draws
        its bottom-side geometry in the Bottom colour. The Top side, and the
        Bottom side when it has no distinct colour, use the primary."""
        if side == "bottom":
            bot = self._overlay_bottom_colors.get(key)
            if bot is not None:
                return bot
        return self._overlay_colors[key]

    def _on_overlay_color(self, key: str, variant: str,
                          rgb: tuple[float, float, float]) -> None:
        """A Board Features row's colour swatch picked a new colour.

        The Bottom row of a split layer pins its own colour
        (``self._overlay_bottom_colors``); a merged or Top row sets the
        layer's primary colour. The list is rebuilt (deferred a tick, as the
        picked swatch is mid-emit) so a Bottom swatch still following the
        primary repaints too. The overlay geometry is recoloured
        immediately, and the project is marked dirty so the new colour is
        written to the .fypa on the next save."""
        rgb = tuple(float(c) for c in rgb)
        if variant == "bottom":
            self._overlay_bottom_colors[key] = rgb
        else:
            self._overlay_colors[key] = rgb
        self._project_dirty = True
        QTimer.singleShot(0, self._build_overlay_list)
        self._on_overlay_changed()

    def _on_overlay_split(self, key: str, split: bool) -> None:
        """An overlay row's split toggle flipped. Splitting seeds the fresh
        Top / Bottom rows from the merged row; merging keeps the Top row's
        state on the recombined row. The list is rebuilt so rows appear /
        disappear — deferred to the next event-loop tick because the rebuild
        deletes the SplitButton that is mid-emit right now."""
        st = self._overlay_state[key]
        if split == st["split"]:
            return
        st["split"] = split
        if split:
            for side in ("top", "bottom"):
                st[side]["vis"] = st["both"]["vis"]
                st[side]["solid"] = st["both"]["solid"]
                st[side]["alpha_step"] = st["both"].get("alpha_step", 0)
        else:
            st["both"]["vis"] = st["top"]["vis"]
            st["both"]["solid"] = st["top"]["solid"]
            st["both"]["alpha_step"] = st["top"].get("alpha_step", 0)
        QTimer.singleShot(0, self._build_overlay_list)
        self._on_overlay_changed()

    def _overlay_visibility(self) -> dict[str, dict]:
        """Current Overlays selection, keyed by layer then row variant —
        e.g. ``{"pads": {"top": {"vis": "rails", "solid": True}, …}}``.

        ``vis`` is ``None`` / ``"rails"`` / ``"all"``. For a merged layer
        only the ``"both"`` variant is populated; for a split layer the
        ``"top"`` and ``"bottom"`` variants are."""
        out: dict[str, dict] = {}
        for key, _label, has_sides in _OVERLAY_LAYERS:
            st = self._overlay_state[key]
            variants = (("top", "bottom") if has_sides and st["split"]
                        else ("both",))
            out[key] = {v: dict(st[v]) for v in variants}
        return out

    def _on_overlay_changed(self) -> None:
        """An overlay row's visibility, fill style or split state changed.

        Overlays are independent of the FEM heatmap, so this rebuilds just
        the overlay geometry rather than triggering a full :meth:`_render`.
        The board outline lives in its own GL batch, so it is refreshed
        alongside (its row is part of the same Overlays control)."""
        self._refresh_overlay_geometry(self._visible_rails())
        self._refresh_board_outline()

    def _overlay_side_states(self, key: str) -> dict:
        """Return ``{"top": (vis, solid), "bottom": (vis, solid)}`` for an
        overlay layer. When the layer is not split, the merged row's state
        applies to both sides."""
        st = self._overlay_state[key]
        if st.get("split"):
            return {"top": (st["top"]["vis"], st["top"]["solid"]),
                    "bottom": (st["bottom"]["vis"], st["bottom"]["solid"])}
        b = st["both"]
        return {"top": (b["vis"], b["solid"]),
                "bottom": (b["vis"], b["solid"])}

    def _overlay_side_alpha(self, key: str) -> dict[str, float]:
        """Per-side draw alpha (0..1) for an overlay layer, mirroring
        :meth:`_overlay_side_states`. Reads each row's TransparencyButton
        step (0 = opaque, 4 = invisible). A merged row's step applies to
        both sides."""
        st = self._overlay_state[key]
        if st.get("split"):
            return {
                "top": _transparency_alpha(st["top"].get("alpha_step", 0)),
                "bottom": _transparency_alpha(
                    st["bottom"].get("alpha_step", 0)),
            }
        a = _transparency_alpha(st["both"].get("alpha_step", 0))
        return {"top": a, "bottom": a}

    def _refresh_overlay_geometry(self, rails) -> None:
        """Rebuild and push the Overlays control's GL geometry.

        Every visible overlay row contributes flat-shaded triangles — a
        solid fill, or a thin ribbon for wire-mesh outlines — to the GL
        viewer's overlay-fill batch; visible designator rows contribute
        text labels. Independent of the FEM heatmap, so it is safe to call
        regardless of the rail / layer selection.

        ``rails`` is the list of currently-visible rail names; it drives
        the per-row "show on the selected rails only" eye. Silkscreen has
        no per-net association, so either eye simply shows it."""
        gv = getattr(self, "_gl_viewer", None)
        if gv is None:
            return
        md = self.metadata
        if md is None:
            gv.clear_overlay_fills()
            gv.clear_overlay_labels()
            return

        rail_members = (set(self._effective_rail_members(rails))
                        if rails else set())
        # Per-side z for 3D placement. ``side_z`` is the outer copper plane
        # on each side — used by vias, which span the whole board. z=0 is
        # the top of the stackup; lower layers are negative.
        side_z = {"top": 0.0, "bottom": 0.0}
        if self._physicals:
            side_z["top"] = self._layer_z_for(self._physicals[0])
            side_z["bottom"] = self._layer_z_for(self._physicals[-1])
        # Board features (silkscreen / pads / components / designators)
        # physically sit on the board's outer surfaces, so top-side items
        # are lifted ABOVE the topmost physical layer and bottom-side items
        # dropped BELOW the lowest. The offset is one mean layer pitch so
        # they read as clearly separate from the copper in 3D (fixed-gap
        # fallback on a single-layer board).
        n_phys = len(self._physicals)
        if n_phys > 1:
            pitch = (side_z["top"] - side_z["bottom"]) / (n_phys - 1)
        else:
            pitch = self._LAYER_Z_SPACING_MM
        if pitch <= 0.0:
            pitch = self._LAYER_Z_SPACING_MM
        feature_z = {"top": side_z["top"] + pitch,
                     "bottom": side_z["bottom"] - pitch}

        # Triangles split into three batches: ``under_*`` draw BEFORE the
        # heatmap mesh (2D only — bottom-side board features sit behind the
        # bottom copper), ``over_*`` draw after the mesh (vias, the per-
        # layer all-copper overlay), and ``top_*`` draw last (2D only —
        # top-side board features sit in front of all top copper / rails,
        # the all-copper overlay, and every other overlay batch). In 3D the
        # per-side feature z + depth test handle ordering, so everything
        # goes to ``over_*``.
        in_2d = not self.view_3d_box.isChecked()
        under_tri: list[np.ndarray] = []
        under_col: list[np.ndarray] = []
        over_tri: list[np.ndarray] = []
        over_col: list[np.ndarray] = []
        top_tri: list[np.ndarray] = []
        top_col: list[np.ndarray] = []
        labels: list[dict] = []

        def _emit(verts_xy: np.ndarray, z: float,
                  rgb: tuple[float, float, float], *,
                  under: bool = False, top: bool = False,
                  alpha: float = 1.0) -> None:
            if verts_xy.size == 0:
                return
            if under and top:
                raise ValueError("under and top are mutually exclusive")
            p = np.empty((verts_xy.shape[0], 3), dtype=np.float32)
            p[:, :2] = verts_xy
            p[:, 2] = z
            if under:
                tri, col = under_tri, under_col
            elif top:
                tri, col = top_tri, top_col
            else:
                tri, col = over_tri, over_col
            tri.append(p)
            rgba = np.empty((p.shape[0], 4), dtype=np.float32)
            rgba[:, 0] = float(rgb[0])
            rgba[:, 1] = float(rgb[1])
            rgba[:, 2] = float(rgb[2])
            rgba[:, 3] = float(alpha)
            col.append(rgba)

        # In 2D the top-side board features are routed to ``top_*`` so they
        # paint on top of the mesh AND the all-copper overlay. In 3D the
        # depth test handles ordering, so they stay in ``over_*``.
        def _side_buckets(side: str) -> dict[str, bool]:
            if not in_2d:
                return {}
            if side == "top":
                return {"top": True}
            if side == "bottom":
                return {"under": True}
            return {}

        def _shape_tris(ring, solid: bool) -> np.ndarray:
            # Solid = filled polygon; wire-mesh = thin mitered outline of
            # the closed ring (pad / via / component box).
            return (_overlay_fan_tris(ring) if solid
                    else _overlay_outline_tris(ring, _OVERLAY_WIRE_HALF_MM,
                                               closed=True))

        def _net_match(nets) -> bool:
            return bool(rail_members) and bool(set(nets) & rail_members)

        self._pump_busy_ui()
        # Overlay graphics — silkscreen tracks / arcs. Each is drawn at its
        # real track width with round end caps, so it matches Altium's
        # round-capped tracks and consecutive segments join smoothly. No
        # per-net data, so either eye ("selected rails" / "all") shows it.
        sides = self._overlay_side_states("silkscreen")
        alphas = self._overlay_side_alpha("silkscreen")
        for rec in md.get("silkscreen", []):
            self._pump_busy_ui()
            if rec.get("kind") == "text":
                continue  # legacy pickles only — text isn't an Overlay item
            side = rec.get("side", "top")
            vis, solid = sides.get(side, (None, False))
            if vis is None:
                continue
            a = alphas.get(side, 1.0)
            if a <= 0.0:
                continue
            rgb = self._overlay_color_for("silkscreen", side)
            poly = rec.get("polyline") or []
            if len(poly) < 2:
                continue
            # Real track width (Altium silkscreen is thin); a small floor
            # keeps a zero-width track from collapsing to nothing.
            half = max(0.5 * float(rec.get("width_mm", 0.0) or 0.0), 0.01)
            z = feature_z.get(side, 0.0)
            bucket = _side_buckets(side)
            if solid:
                _emit(_overlay_outline_tris(poly, half, closed=False), z,
                      rgb, alpha=a, **bucket)
                # Round end caps — interior joins are already mitered.
                for end in (poly[0], poly[-1]):
                    _emit(_overlay_fan_tris(_overlay_circle_ring(
                        float(end[0]), float(end[1]), half, 12)), z, rgb,
                        alpha=a, **bucket)
            else:
                # Wire-mesh: the track's hollow perimeter. The round caps
                # are part of the traced outline — no separate cap fills.
                _emit(_overlay_ribbon_outline_tris(
                    poly, half, _OVERLAY_WIRE_HALF_MM, closed=False),
                    z, rgb, alpha=a, **bucket)

        self._pump_busy_ui()
        # Vias — one circle per via (the Vias row has no top/bottom split).
        vsub = self._overlay_state["vias"]["both"]
        if vsub["vis"] is not None:
            rgb = self._overlay_colors["vias"]
            solid = vsub["solid"]
            via_alpha = _transparency_alpha(vsub.get("alpha_step", 0))
            rails_only = vsub["vis"] == "rails"
            if via_alpha > 0.0:
                for rec in md.get("vias", []):
                    self._pump_busy_ui()
                    if rails_only and not _net_match([rec.get("net")]):
                        continue
                    r = 0.5 * float(rec.get("diameter_mm", 0.0) or 0.0)
                    if r <= 0.0:
                        continue
                    ring = _overlay_circle_ring(float(rec["x_mm"]),
                                                float(rec["y_mm"]), r)
                    _emit(_shape_tris(ring, solid), side_z["top"], rgb,
                          alpha=via_alpha)

        self._pump_busy_ui()
        # Components — axis-aligned bounding box per component.
        sides = self._overlay_side_states("components")
        alphas = self._overlay_side_alpha("components")
        for rec in md.get("components", []):
            self._pump_busy_ui()
            side = rec.get("side", "top")
            vis, solid = sides.get(side, (None, False))
            if vis is None:
                continue
            if vis == "rails" and not _net_match(rec.get("nets", [])):
                continue
            a = alphas.get(side, 1.0)
            if a <= 0.0:
                continue
            rgb = self._overlay_color_for("components", side)
            bbox = rec.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            _emit(_shape_tris(_overlay_box_ring(*bbox), solid),
                  feature_z[side], rgb, alpha=a, **_side_buckets(side))

        self._pump_busy_ui()
        # Pads — outline ring per pad, drawn on whichever board side(s)
        # the pad's copper layers include (through-hole pads → both).
        # Emitted AFTER components: the 2D overlay batch has no depth test,
        # so the last-emitted geometry wins — this puts pads visually on
        # top of the component bodies they sit on.
        enabled = md.get("enabled_copper_layer_ids") or []
        top_lid = enabled[0] if enabled else None
        bot_lid = enabled[-1] if enabled else None
        sides = self._overlay_side_states("pads")
        alphas = self._overlay_side_alpha("pads")
        for rec in md.get("pads", []):
            self._pump_busy_ui()
            ring = rec.get("outline")
            if not ring:
                continue
            lids = rec.get("layer_ids") or []
            for side, lid in (("top", top_lid), ("bottom", bot_lid)):
                if lid is None or lid not in lids:
                    continue
                vis, solid = sides[side]
                if vis is None:
                    continue
                if vis == "rails" and not _net_match([rec.get("net")]):
                    continue
                a = alphas.get(side, 1.0)
                if a <= 0.0:
                    continue
                rgb = self._overlay_color_for("pads", side)
                _emit(_shape_tris(ring, solid), feature_z[side], rgb,
                      alpha=a, **_side_buckets(side))

        self._pump_busy_ui()
        # Designators — drawn in Altium's actual single-stroke font: the
        # loader lays each one out into stroke polylines, which render as
        # thin round-capped ribbons (same path as silkscreen). TrueType-
        # font designators carry no polylines and fall back to a label.
        sides = self._overlay_side_states("designators")
        alphas = self._overlay_side_alpha("designators")
        for rec in md.get("designators", []):
            self._pump_busy_ui()
            side = rec.get("side", "top")
            vis, solid = sides.get(side, (None, False))
            if vis is None:
                continue
            if vis == "rails" and not _net_match(rec.get("nets", [])):
                continue
            a = alphas.get(side, 1.0)
            if a <= 0.0:
                continue
            rgb = self._overlay_color_for("designators", side)
            if rec.get("polylines"):
                _emit(self._designator_stroke_tris(rec, solid=solid),
                      feature_z[side], rgb, alpha=a, **_side_buckets(side))
            else:
                # Text labels honour the row's transparency by riding the
                # colour's alpha channel through #rrggbbaa.
                qc = QColor.fromRgbF(rgb[0], rgb[1], rgb[2], a)
                labels.append({
                    "x": float(rec.get("x_mm", 0.0)),
                    "y": float(rec.get("y_mm", 0.0)),
                    "z": feature_z[side],
                    "text": rec.get("text", ""),
                    "color": qc.name(QColor.HexArgb),
                    "height_mm": float(rec.get("height_mm", 1.0) or 1.0),
                    "rotation_deg": float(rec.get("rotation_deg", 0.0) or 0.0),
                })

        self._pump_busy_ui()
        # Physical-layer "all copper" — the per-layer second eye. For every
        # layer whose all-copper eye is on, draw all of that layer's copper
        # that ISN'T part of a selected rail (the rails are already the
        # heatmap; this is the rest), in the layer's swatch colour, as a
        # wire-mesh outline or a solid fill per that row's fill toggle.
        # The row's TransparencyButton fades that geometry without affecting
        # the heatmap itself.
        eye2_buttons = getattr(self, "_layer_eye2_buttons", [])
        if eye2_buttons and md.get("all_copper"):
            ac_by_layer: dict[int, list] = {}
            for rec in md["all_copper"]:
                ac_by_layer.setdefault(rec.get("layer_id"), []).append(rec)
            fill_by_name = dict(getattr(self, "_layer_fill_buttons", []))
            transp_by_name = dict(
                getattr(self, "_layer_transparency_buttons", []))
            # Emit bottom-up: ``_layer_eye2_buttons`` is in panel order
            # (topmost first); sort by stackup rank descending so the
            # topmost layer is emitted last, mirroring the heatmap mesh's
            # phys_draw_order — that matches the GL_LEQUAL depth test the
            # under-mesh batch uses in 2D (same z → later draw wins, so
            # topmost-emitted-last still wins).
            # In 2D opaque all-copper rides in ``under_*`` so it paints
            # BEFORE the heatmap mesh with depth on — the rail mesh
            # (drawn next, same per-layer z) paints over its own layer's
            # all-copper, and bottom rail triangles are depth-rejected
            # where a higher layer's all-copper has already written a
            # closer z. That cross-layer z stacking only works because
            # the under-mesh pass also WRITES depth, which would equally
            # depth-reject the rail mesh underneath a partially-
            # transparent layer — turning the transparent layer into a
            # tinted hole instead of letting the heatmap show through.
            # So a layer at alpha < 1.0 is routed to ``over_*`` instead:
            # drawn AFTER the mesh, no depth interference, free to blend
            # with the mesh (or other opaque all-copper) below. 3D
            # always uses ``over_*`` — global per-vertex z + depth
            # handles ordering there.
            for name, eye2 in sorted(
                eye2_buttons,
                key=lambda ne: self._phys_stackup_rank.get(ne[0], 1 << 30),
                reverse=True,
            ):
                if not eye2.isVisibleState():
                    continue
                recs = ac_by_layer.get(self._phys_name_to_layer_id.get(name))
                if not recs:
                    continue
                # Per-layer pump — for a 16-layer board with all-copper on,
                # this gives ~16 marquee-bar ticks during the heaviest
                # section of the overlay refresh.
                self._pump_busy_ui()
                fb = fill_by_name.get(name)
                solid = bool(fb.isSolid()) if fb is not None else False
                tb = transp_by_name.get(name)
                layer_alpha = tb.alpha() if tb is not None else 1.0
                if layer_alpha <= 0.0:
                    continue
                # Opaque layers stay in the under-mesh batch to keep the
                # cross-layer z stacking; transparent layers fall to
                # ``over_*`` so the mesh underneath isn't depth-rejected.
                ac_bucket = ({"under": True}
                             if (in_2d and layer_alpha >= 1.0)
                             else {})
                qc = QColor(self._layer_color_for(name))
                lrgb = (qc.redF(), qc.greenF(), qc.blueF())
                z = self._layer_z_for(name)
                # Solid + partial transparency: route through a per-layer
                # merged triangulation so overlapping source polygons
                # blend the layer's alpha exactly once per pixel (rather
                # than once per overlapping polygon, which cumulatively
                # pushes the area to opacity). The merge collapses
                # per-net distinction, so editor-mode connectivity dim
                # falls back to a per-rec emit on this path — the wire-
                # mesh fill keeps the precise per-net dim anyway.
                if (solid and layer_alpha < 1.0
                        and not (self._editor_mode
                                  and self._editor_highlight_nets)):
                    merged = self._merged_solid_all_copper_tris(
                        name, recs, rail_members)
                    if merged is not None:
                        _emit(merged, z, lrgb, alpha=layer_alpha,
                              **ac_bucket)
                    continue
                for rec in recs:
                    net = rec.get("net")
                    if net in rail_members:
                        continue
                    # Per-rec pump — a single layer on Corvette can have
                    # hundreds of all-copper records; without a pump in
                    # this loop the marquee freezes for the duration of
                    # one layer's emit. Throttled, so overhead stays
                    # near-zero.
                    self._pump_busy_ui()
                    # Editor-mode connectivity focus: copper not connected
                    # to the current selection recedes toward the
                    # background (mirrors the heatmap mesh dimming).
                    rgb = self._editor_dim_rgb(lrgb, net)
                    for poly in rec.get("polygons", []):
                        if solid:
                            _emit(self._triangulate_stub(poly), z, rgb,
                                  alpha=layer_alpha, **ac_bucket)
                        else:
                            _emit(self._copper_poly_wire(poly), z, rgb,
                                  alpha=layer_alpha, **ac_bucket)

        # Concatenate under-mesh chunks first so the GL viewer can draw
        # that leading slice before the heatmap mesh.
        # ``under_*`` (drawn before the mesh, 2D only): bottom-side board
        # features + every layer's all-copper geometry — having all-copper
        # paint before the mesh lets the rail mesh sit visually on top of
        # both its own layer's other-net copper and any lower-layer
        # all-copper. ``over_*`` (drawn after the mesh): vias and, in 3D,
        # every feature (depth test handles ordering there). ``top_*`` (2D
        # only): top-side board features so they sit on top of all copper.
        all_tri = under_tri + over_tri + top_tri
        if all_tri:
            under_count = sum(c.shape[0] for c in under_tri)
            gv.set_overlay_fills(
                np.concatenate(all_tri, axis=0),
                np.concatenate(under_col + over_col + top_col, axis=0),
                under_mesh_count=under_count)
        else:
            gv.clear_overlay_fills()
        if labels:
            gv.set_overlay_labels(labels)
        else:
            gv.clear_overlay_labels()

    def _copper_poly_wire(self, poly: dict) -> np.ndarray:
        """Mitered wire-mesh outline triangles for one all-copper polygon
        (its exterior ring plus any holes). Cached on the polygon dict so
        repeated renders just re-upload the prebuilt array."""
        cached = poly.get("_wire_cache")
        if cached is not None:
            return cached
        chunks: list[np.ndarray] = []
        ext = poly.get("exterior")
        if ext is not None and len(ext) >= 2:
            chunks.append(_overlay_outline_tris(
                ext, _OVERLAY_WIRE_HALF_MM, closed=True))
        for hole in poly.get("holes", []) or []:
            if hole is not None and len(hole) >= 2:
                chunks.append(_overlay_outline_tris(
                    hole, _OVERLAY_WIRE_HALF_MM, closed=True))
        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        poly["_wire_cache"] = arr
        return arr

    def _designator_stroke_tris(self, rec: dict, *,
                                solid: bool = True) -> np.ndarray:
        """Triangles for one designator's stroke-font geometry — each glyph
        stroke as a thin round-capped ribbon at the text's stroke width.

        ``solid`` draws each stroke as a filled ribbon; otherwise each
        stroke is traced as its hollow perimeter (the wire-mesh fill
        style). Both variants are cached separately on the record dict
        (the layout is static for the session)."""
        cache_key = "_des_tris" if solid else "_des_tris_wire"
        cached = rec.get(cache_key)
        if cached is not None:
            return cached
        half = max(0.5 * float(rec.get("stroke_width_mm", 0.0) or 0.0), 0.01)
        chunks: list[np.ndarray] = []
        for pl in rec.get("polylines") or []:
            if len(pl) < 2:
                continue
            if solid:
                chunks.append(_overlay_outline_tris(pl, half, closed=False))
                for end in (pl[0], pl[-1]):
                    chunks.append(_overlay_fan_tris(_overlay_circle_ring(
                        float(end[0]), float(end[1]), half, 8)))
            else:
                chunks.append(_overlay_ribbon_outline_tris(
                    pl, half, _OVERLAY_WIRE_HALF_MM, closed=False))
        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        rec[cache_key] = arr
        return arr

    # --- Rendering -----------------------------------------------------------

    def _current_selection(self) -> tuple[list[str], list[str], str]:
        """Current heatmap selection: ``(visible_layers, visible_rails, mode)``."""
        layers = self._visible_layers()
        rails = self._visible_rails()
        mode = self.mode_combo.currentText()
        return layers, rails, mode

    def _mode_derive_fn(self, mode: str):
        for label, unit, fn in _MODES:
            if label == mode:
                return label, unit, fn
        raise KeyError(mode)

    def _effective_rail_members(self, rail_names) -> list[str]:
        """The list of net names whose copper should appear when the given
        rails are selected, honouring the "Show only rail net" checkbox.

        Off → union of full rail groups (e.g. ``[+3V3, 3V3_SW]`` for +3V3).
        On  → just each selected rail's primary name (bridged nets hidden).

        Accepts either a single rail name (legacy single-rail call sites)
        or a list of rail names (multi-rail control). An empty list / empty
        string returns ``[]`` — caller code treats that as "no rail filter
        currently selected", which in practice means the heatmap mesh has
        no nets to draw.
        """
        if isinstance(rail_names, str):
            rail_names = [rail_names] if rail_names else []
        if not rail_names:
            return []
        rail_only = self.rail_only_box.isChecked()
        members: list[str] = []
        seen: set[str] = set()
        for rail_name in rail_names:
            full = self._rail_to_members.get(rail_name, [rail_name])
            if rail_only and rail_name in full:
                picks: list[str] = [rail_name]
            else:
                picks = list(full)
            for m in picks:
                if m not in seen:
                    seen.add(m)
                    members.append(m)
        return members

    def _build_rail_arrays(
        self, phys_list: list[str], rail_names: list[str], derive_fn,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray,
               np.ndarray, list[dict]]:
        """Combine every per-(layer, net) padne Layer for the listed physical
        layers + the selected rail groups' nets into one big mesh batch for
        the GPU, and ALSO build per-(physical_layer, net) CPU-side
        interpolators for the hover probe and Voltage Drop reference.

        Drawing order is BOTTOM-to-TOP of the stackup, so the topmost
        checked layer is rendered last and sits visually on top of any
        lower layers it overlaps with.

        Returns ``(xs, ys, zs, vs, triangles, layer_probes)``. ``zs`` is
        per-vertex z (pre-exaggeration mm; the GLMeshViewer applies its
        own vertical scaling in 3D mode). In 2D mode the GL viewer ignores
        z, but we still build the array so the data path stays uniform.
        ``layer_probes`` is a list of dicts (one per visible (phys, net))
        with keys ``physical``, ``net``, ``layer_id``, ``triangulation``,
        ``interpolator``, ``values`` (numpy float64), ``prepared_shape``.
        The list is in TOP-FIRST order so the probe can walk it and
        report the topmost layer whose copper sits under the cursor.
        """
        members = self._effective_rail_members(rail_names)
        # BOTTOM-first for GPU draw order (topmost rendered last → on top).
        phys_draw_order = sorted(
            phys_list,
            key=lambda p: self._phys_stackup_rank.get(p, 1 << 30),
            reverse=True,
        )
        xs_parts: list[np.ndarray] = []
        ys_parts: list[np.ndarray] = []
        zs_parts: list[np.ndarray] = []
        vs_parts: list[np.ndarray] = []
        tris_parts: list[np.ndarray] = []
        # layer_probes is ordered TOP-FIRST so the hover probe naturally
        # reports the visually-topmost layer when copper overlaps.
        layer_probes: list[dict] = []
        # In 3D mode extrude each layer's flat mesh into a thin prism so
        # the copper has visible thickness from oblique angles. 2D mode
        # keeps the flat mesh as-is (no perf hit on the common path).
        extrude_3d = (self.view_3d_box.isChecked()
                      and self._COPPER_THICKNESS_MM > 0.0)
        offset = 0
        for phys in phys_draw_order:
            phys_z = self._layer_z_for(phys)
            for net in members:
                layer_index = self._index_by_pair.get((phys, net))
                if layer_index is None:
                    continue
                # Per-(phys, net) pump — this loop is the bulk of
                # _build_rail_arrays cost on multi-rail, multi-layer
                # selections. The throttle keeps the overhead negligible.
                self._pump_busy_ui()
                entry = self._layer_arrays(layer_index, derive_fn)
                n_in = entry["xs"].size
                if n_in == 0 or entry["tris"].size == 0:
                    continue
                if extrude_3d:
                    lxs, lys, lzs, lvs, ltris = _extrude_to_prism(
                        entry["xs"], entry["ys"], entry["vs"], entry["tris"],
                        z_center=phys_z,
                        thickness=self._COPPER_THICKNESS_MM,
                    )
                else:
                    lxs = entry["xs"]
                    lys = entry["ys"]
                    lzs = np.full(n_in, phys_z, dtype=np.float64)
                    lvs = entry["vs"]
                    ltris = entry["tris"]
                n = lxs.size
                xs_parts.append(lxs)
                ys_parts.append(lys)
                zs_parts.append(lzs)
                vs_parts.append(lvs)
                # Vectorised offset add — re-base local indices into the
                # combined GPU batch.
                tris_parts.append(ltris + offset)
                offset += n
                layer_probes.append({
                    "physical": phys,
                    "net": net,
                    "layer_id": self._phys_name_to_layer_id.get(phys),
                    "layer_index": layer_index,
                    # Vertex count this (phys, net) contributed to the
                    # combined mesh — used to build the per-vertex alpha
                    # array (see :meth:`_combined_mesh_alpha_array`),
                    # which folds in both the per-layer Transparency
                    # setting and the editor-mode connectivity dim.
                    "n_vertices": n,
                    "triangulation": entry["triangulation"],
                    "interpolator": entry["interpolator"],
                    # Cache key stored for lazy interpolator writeback — when
                    # the hover probe builds the interpolator on first cursor
                    # move, it writes back to the cache entry so subsequent
                    # renders of the same (layer, mode) reuse it.
                    "_cache_key": (layer_index, id(derive_fn)),
                    "values": entry["vs"],
                    "prepared_shape": entry["prepared_shape"],
                    "outline_segments": entry["outline_segments"],
                    "z": phys_z,
                })
        # Reverse to put topmost layer first in the probe walk order.
        layer_probes.reverse()

        if xs_parts:
            xs = np.concatenate(xs_parts)
            ys = np.concatenate(ys_parts)
            zs = np.concatenate(zs_parts)
            vs = np.concatenate(vs_parts)
            tris = np.concatenate(tris_parts, axis=0)
        else:
            xs = np.empty(0, dtype=np.float64)
            ys = np.empty(0, dtype=np.float64)
            zs = np.empty(0, dtype=np.float64)
            vs = np.empty(0, dtype=np.float64)
            tris = np.empty((0, 3), dtype=np.int32)
        return xs, ys, zs, vs, tris, layer_probes

    def _layer_arrays(self, layer_index: int, derive_fn) -> dict:
        """Assemble (and cache) per-layer arrays + Triangulation +
        _FastTriSampler + prepared shapely shape for one
        (layer_index, derive_fn) pair.

        First call walks the LayerSolution meshes once and builds
        everything; subsequent calls are dict lookups. Solution data is
        immutable so the cache lives for the session.

        Keying on ``id(derive_fn)`` (rather than the mode label) means
        modes that share a derive function — e.g. 'Voltage' and
        'Voltage Drop', which both use ``_voltage_per_vertex`` (Voltage
        Drop's shift is applied later, downstream in ``_render``) —
        share a single cache entry.
        """
        key = (layer_index, id(derive_fn))
        cached = self._layer_cache.get(key)
        if cached is not None:
            return cached

        ls = self.solution.layer_solutions[layer_index]
        layer = self.solution.problem.layers[layer_index]
        # Lean format: per-mesh-component numpy arrays — no half-edge
        # iteration, no per-vertex Python attribute access.
        xys_meshes = ls.vertex_xys
        tris_meshes = ls.triangles
        pots_meshes = ls.potentials
        pds_meshes = ls.power_densities

        mxs_parts: list[np.ndarray] = []
        mys_parts: list[np.ndarray] = []
        mvs_parts: list[np.ndarray] = []
        mtris_parts: list[np.ndarray] = []
        offset = 0
        for xys, tris_local, pot, pd in zip(
            xys_meshes, tris_meshes, pots_meshes, pds_meshes,
        ):
            n = xys.shape[0]
            if n < 3 or tris_local.size == 0:
                continue
            values = derive_fn(tris_local, pot, pd, layer.conductance, n)
            mxs_parts.append(xys[:, 0])
            mys_parts.append(xys[:, 1])
            mvs_parts.append(np.asarray(values, dtype=np.float64))
            # Re-base local indices into the per-layer combined batch.
            mtris_parts.append(tris_local + offset)
            offset += n

        if mxs_parts:
            xs = np.concatenate(mxs_parts)
            ys = np.concatenate(mys_parts)
            vs = np.concatenate(mvs_parts)
        else:
            xs = np.empty(0, dtype=np.float64)
            ys = np.empty(0, dtype=np.float64)
            vs = np.empty(0, dtype=np.float64)
        if mtris_parts:
            tris = np.concatenate(mtris_parts, axis=0)
        else:
            tris = np.empty((0, 3), dtype=np.int32)

        if xs.size >= 3 and tris.size > 0:
            triangulation = Triangulation(xs, ys, tris)
            # The voltage sampler (_FastTriSampler) is built lazily on
            # first cursor hover via _ensure_interpolator() — there's no
            # point building it for a layer the user never probes. The
            # cache entry is updated at hover time so re-renders of the
            # same (layer, mode) always reuse a previously-built sampler.
            interpolator = None
        else:
            triangulation = None
            interpolator = None

        shape = layer.shape
        prepared_shape = (_sp.prep(shape)
                          if shape is not None and not shape.is_empty else None)
        outline_segments = _shape_outline_segments(shape)

        # Vertex indices that actually appear in a triangle. The padne
        # solver's orphan-vertex guards pin "no-triangle" vertices to
        # V=0 to keep the linear system non-singular; they're invisible
        # in the heatmap (no triangle = no painted pixel) but they
        # otherwise dominate vs.min()/max() and skew the scale-controller
        # range — especially after the Voltage Drop subtract shifts them
        # by the source voltage. Callers compute stats via vs[used_indices].
        if tris.size > 0:
            used_indices = np.unique(tris.ravel().astype(np.int64))
        else:
            used_indices = np.empty(0, dtype=np.int64)

        entry = {
            "xs": xs,
            "ys": ys,
            "vs": vs,
            "tris": tris,
            "triangulation": triangulation,
            "interpolator": interpolator,
            "prepared_shape": prepared_shape,
            # Outline segments: (N, 2) float32 with vertices in GL_LINES
            # pairs (consecutive entries are one segment). Pre-built here
            # so toggling the outline overlay is just a buffer upload.
            "outline_segments": outline_segments,
            "used_indices": used_indices,
        }
        self._layer_cache[key] = entry
        return entry

    def _layer_vectors(self, layer_index: int) -> dict | None:
        """Build (and cache) the per-triangle current-density vector
        ``J = -sigma * grad V`` for one padne layer.

        The result is independent of the current heatmap mode (it always
        works off the raw potentials), so the cache key is just the
        layer index. ``None`` is returned when the layer has no usable
        triangles.

        Returned dict has:

        * ``xs, ys`` — ``(N,)`` float64 vertex coordinates (mm) packed
          across all mesh components of the layer (same packing scheme
          as :meth:`_layer_arrays`).
        * ``tris`` — ``(M, 3)`` int32 triangle indices.
        * ``cx, cy`` — ``(M,)`` triangle centroid coordinates (mm).
        * ``Jx, Jy`` — ``(M,)`` per-triangle current-density components
          (A/mm). Zero on degenerate (zero-area) triangles.
        * ``trifinder`` — matplotlib ``TriFinder`` for point-in-triangle
          lookups during arrow grid sampling.
        * ``bounds`` — ``(x_min, x_max, y_min, y_max)`` of the layer.
        """
        cached = self._layer_vec_cache.get(layer_index)
        if cached is not None:
            return cached

        ls = self.solution.layer_solutions[layer_index]
        layer = self.solution.problem.layers[layer_index]
        sigma = float(layer.conductance)

        xs_parts: list[np.ndarray] = []
        ys_parts: list[np.ndarray] = []
        pot_parts: list[np.ndarray] = []
        tris_parts: list[np.ndarray] = []
        offset = 0
        for xys, tris_local, pot in zip(ls.vertex_xys, ls.triangles, ls.potentials):
            n = xys.shape[0]
            if n < 3 or tris_local.size == 0:
                continue
            xs_parts.append(xys[:, 0])
            ys_parts.append(xys[:, 1])
            pot_parts.append(np.asarray(pot, dtype=np.float64))
            tris_parts.append(tris_local.astype(np.int32, copy=False) + offset)
            offset += n
        if not xs_parts:
            return None

        xs = np.concatenate(xs_parts)
        ys = np.concatenate(ys_parts)
        pots = np.concatenate(pot_parts)
        tris = np.concatenate(tris_parts, axis=0)

        # Per-triangle linear gradient of potential. Solve
        #   [[x1-x0, y1-y0], [x2-x0, y2-y0]] @ [dV/dx, dV/dy]^T
        #   = [V1-V0, V2-V0]^T
        # vectorised across all triangles.
        tx = xs[tris]
        ty = ys[tris]
        tv = pots[tris]
        ax_ = tx[:, 1] - tx[:, 0]
        ay_ = ty[:, 1] - ty[:, 0]
        bx_ = tx[:, 2] - tx[:, 0]
        by_ = ty[:, 2] - ty[:, 0]
        det = ax_ * by_ - bx_ * ay_
        # Avoid division by zero on degenerate (collinear) triangles.
        bad = np.abs(det) < 1e-30
        safe_det = np.where(bad, 1.0, det)
        dV1 = tv[:, 1] - tv[:, 0]
        dV2 = tv[:, 2] - tv[:, 0]
        dVdx = (by_ * dV1 - ay_ * dV2) / safe_det
        dVdy = (ax_ * dV2 - bx_ * dV1) / safe_det
        Jx = -sigma * dVdx
        Jy = -sigma * dVdy
        if bad.any():
            Jx[bad] = 0.0
            Jy[bad] = 0.0

        cx = tx.mean(axis=1)
        cy = ty.mean(axis=1)

        triangulation = Triangulation(xs, ys, tris)
        trifinder = triangulation.get_trifinder()

        entry = {
            "xs": xs, "ys": ys, "tris": tris,
            "cx": cx, "cy": cy,
            "Jx": Jx, "Jy": Jy,
            "trifinder": trifinder,
            "bounds": (float(xs.min()), float(xs.max()),
                       float(ys.min()), float(ys.max())),
        }
        self._layer_vec_cache[layer_index] = entry
        return entry

    # --- Current-arrow overlay --------------------------------------------

    # Pre-exaggeration mm offset applied to arrow z in 3D mode so the
    # arrow sits above the copper top face instead of z-fighting with
    # it. Scaled by the GL viewer's vertical-exaggeration uniform, so a
    # 0.0025 mm lift becomes 0.125 mm at the default 50× — enough to
    # clear z-fight without the arrow visibly floating above the copper.
    # Manual-adjust knob: bump this up if you still see z-fighting,
    # drop it if the arrows look detached from the layer.
    _ARROW_Z_LIFT_MM: float = 0.0015

    # Hard cap on grid cells per layer. Even with a pathological density
    # request and a very elongated layer, this keeps the meshgrid
    # allocation bounded (≤ ~3 MB of float64) instead of letting it blow
    # out to hundreds of GiB like the old screen-px sampling did on
    # large designs at deep zoom.
    _ARROW_MAX_GRID_CELLS: int = 200_000

    def _build_arrow_segments(self, layer_probes: list[dict],
                              density: float) -> np.ndarray:
        """Sample current vectors on a regular grid anchored to each
        layer's *world-space* bounds and return the GL_LINES vertex
        buffer needed to draw an arrow at each sample. In 2D the result
        is ``(K, 2)`` (z=0 broadcast in the GL viewer); in 3D it's
        ``(K, 3)`` with each arrow lifted to its layer's stackup z so it
        sits on top of the copper. Consecutive vertex pairs are one
        segment; each arrow contributes three segments (shaft + two
        head wings = 6 vertices).

        ``density`` is the approximate number of arrows along the
        shorter side of the *combined* visible-layer bounds. Spacing is
        shared across every layer in this pass so a small island layer
        and a full-board plane get the same arrow size — and is derived
        from world bounds (not the viewport), so zoom / pan / 3D dolly
        don't change the sample positions or trigger a rebuild.
        """
        if (not layer_probes or density <= 0
                or self._gl_viewer is None):
            return np.empty((0, 2), dtype=np.float32)
        in_3d = self._gl_viewer.view_mode() == "3d"

        head_angle_rad = math.radians(25.0)
        cos_a = math.cos(head_angle_rad)
        sin_a = math.sin(head_angle_rad)
        head_frac = 0.30           # head length / shaft length
        max_len_frac = 0.85        # longest shaft / spacing_mm
        min_len_frac = 0.20        # shortest shaft (for non-zero |J|)
        cols = 3 if in_3d else 2

        # Pre-pass: resolve per-layer vector caches once and take the
        # union of their bounds. The arrow spacing is derived from this
        # union (not each layer's own bounds), so every layer in the
        # current selection gets the same arrow size — otherwise a tiny
        # island layer would render thousands of tiny arrows next to a
        # plane layer's coarse grid.
        resolved: list[tuple[dict, dict]] = []
        ux_min = math.inf; ux_max = -math.inf
        uy_min = math.inf; uy_max = -math.inf
        for lp in layer_probes:
            layer_index = lp.get("layer_index")
            if layer_index is None:
                continue
            vec = self._layer_vectors(layer_index)
            if vec is None:
                continue
            lx_min, lx_max, ly_min, ly_max = vec["bounds"]
            if lx_max <= lx_min or ly_max <= ly_min:
                continue
            resolved.append((lp, vec))
            if lx_min < ux_min: ux_min = lx_min
            if lx_max > ux_max: ux_max = lx_max
            if ly_min < uy_min: uy_min = ly_min
            if ly_max > uy_max: uy_max = ly_max
        if not resolved:
            return np.empty((0, cols), dtype=np.float32)
        union_w = ux_max - ux_min
        union_h = uy_max - uy_min
        spacing_mm = min(union_w, union_h) / float(density)
        if spacing_mm <= 0:
            return np.empty((0, cols), dtype=np.float32)

        all_segs: list[np.ndarray] = []
        for lp, vec in resolved:
            lx_min, lx_max, ly_min, ly_max = vec["bounds"]
            gx_min, gx_max = lx_min, lx_max
            gy_min, gy_max = ly_min, ly_max
            layer_w = gx_max - gx_min
            layer_h = gy_max - gy_min
            cell = spacing_mm
            nx = max(1, int(math.floor(layer_w / cell)))
            ny = max(1, int(math.floor(layer_h / cell)))
            # Safety net: if a freak aspect ratio still pushes nx*ny
            # over the cap, scale spacing up *for this layer only* so
            # the grid fits.
            if nx * ny > self._ARROW_MAX_GRID_CELLS:
                scale = math.sqrt(nx * ny / self._ARROW_MAX_GRID_CELLS)
                cell *= scale
                nx = max(1, int(math.floor(layer_w / cell)))
                ny = max(1, int(math.floor(layer_h / cell)))
            gx_axis = gx_min + cell * (np.arange(nx) + 0.5)
            gy_axis = gy_min + cell * (np.arange(ny) + 0.5)
            gx_grid, gy_grid = np.meshgrid(gx_axis, gy_axis)
            gx_flat = gx_grid.ravel()
            gy_flat = gy_grid.ravel()
            tri_idx = vec["trifinder"](gx_flat, gy_flat)
            mask = tri_idx >= 0
            if not mask.any():
                continue
            px = gx_flat[mask]
            py = gy_flat[mask]
            ti = tri_idx[mask]
            Jx = vec["Jx"][ti]
            Jy = vec["Jy"][ti]
            mag = np.hypot(Jx, Jy)
            if mag.max() <= 0:
                continue
            # 95th-percentile reference so a single FEM-singularity spike
            # near a SOURCE/SINK pin doesn't shrink every other arrow.
            ref = float(np.percentile(mag, 95.0))
            if ref <= 0:
                ref = float(mag.max())
            nmag = np.clip(mag / ref, 0.0, 1.0)
            length = spacing_mm * (
                min_len_frac + (max_len_frac - min_len_frac) * np.sqrt(nmag)
            )
            inv_mag = np.divide(1.0, mag,
                                out=np.zeros_like(mag), where=mag > 0)
            dirx = Jx * inv_mag
            diry = Jy * inv_mag

            half_len = length * 0.5
            tail_x = px - half_len * dirx
            tail_y = py - half_len * diry
            tip_x = px + half_len * dirx
            tip_y = py + half_len * diry

            head_len = length * head_frac
            wl_x = tip_x + head_len * (-dirx * cos_a + diry * sin_a)
            wl_y = tip_y + head_len * (-dirx * sin_a - diry * cos_a)
            wr_x = tip_x + head_len * (-dirx * cos_a - diry * sin_a)
            wr_y = tip_y + head_len * (dirx * sin_a - diry * cos_a)

            n_arrows = px.size
            segs = np.empty((n_arrows * 6, cols), dtype=np.float32)
            segs[0::6, 0] = tail_x; segs[0::6, 1] = tail_y
            segs[1::6, 0] = tip_x;  segs[1::6, 1] = tip_y
            segs[2::6, 0] = tip_x;  segs[2::6, 1] = tip_y
            segs[3::6, 0] = wl_x;   segs[3::6, 1] = wl_y
            segs[4::6, 0] = tip_x;  segs[4::6, 1] = tip_y
            segs[5::6, 0] = wr_x;   segs[5::6, 1] = wr_y
            if in_3d:
                # Lift arrow vertices slightly above the layer top so
                # GL_DEPTH_TEST doesn't fight the copper mesh under them.
                # ``z`` is pre-exaggeration mm; vertical-exaggeration in
                # the model matrix scales both layer z and this lift
                # together, so the offset stays proportional.
                z_lift = float(lp.get("z", 0.0)) + self._ARROW_Z_LIFT_MM
                segs[:, 2] = z_lift
            all_segs.append(segs)

        if not all_segs:
            return np.empty((0, cols), dtype=np.float32)
        return np.concatenate(all_segs, axis=0)

    def _refresh_arrows(self) -> None:
        """Push the current-arrow overlay (or clear it) based on the
        side-panel state. Works in both 2D and 3D — in 3D each arrow is
        lifted to its layer's stackup z so it floats on the copper top
        face."""
        if (not hasattr(self, "show_arrows_box")
                or self._gl_viewer is None):
            return
        if (not self.show_arrows_box.isChecked()
                or not self._layer_probes):
            self._gl_viewer.clear_arrows()
            return
        density = float(self.arrow_spacing_slider.value())
        segs = self._build_arrow_segments(self._layer_probes, density)
        if segs.size == 0:
            self._gl_viewer.clear_arrows()
            return
        self._gl_viewer.set_arrows(segs, color=(1.0, 1.0, 1.0))

    # Runs an arbitrary display-update callable. If it takes longer than
    # ``_BUSY_POPUP_DELAY_MS`` the modal "Updating display…" popup
    # appears mid-flight; if it finishes faster, no popup is ever shown.
    # The deferred-show pattern means we don't have to predict slowness
    # up front — fast cached operations cost nothing, slow ones get the
    # dialog without flashing it on quick toggles. Used by both the
    # render path and the overlay-refresh path (all-copper toggles on
    # big boards run through :meth:`_refresh_overlay_geometry`, not
    # :meth:`_render`).
    _BUSY_POPUP_DELAY_MS = 400

    def _run_with_busy_popup(self, work) -> None:
        # Re-entrancy: the inner work pumps QApplication.processEvents()
        # (via :meth:`_pump_busy_ui`) so the deferred-show timer can fire
        # and the marquee can repaint. If a queued signal re-enters this
        # wrapper mid-flight, drop it — running the work twice would
        # corrupt GL state (half-pushed meshes, mis-ordered overlay
        # batches). The popup itself is ApplicationModal so it blocks
        # user input on other windows; in practice this guard only fires
        # on stray internal signals.
        if self._render_busy_active:
            return
        self._render_busy_active = True
        self._last_pump_time = 0.0
        # Mutable box so the timer slot below can publish the created
        # dialog back to the finally block for cleanup.
        dlg_box: list[QProgressDialog | None] = [None]

        def _show_popup() -> None:
            # The timer slot can theoretically run after the finally block
            # has cleared the busy flag (if it fires between work
            # returning and timer.stop() in finally). Guard against that.
            if not self._render_busy_active or dlg_box[0] is not None:
                return
            d = QProgressDialog("Updating display…", "", 0, 0, self)
            d.setWindowTitle("Working")
            # The work isn't cancellable — it's one synchronous call.
            # Strip the cancel button so the dialog is unambiguously a
            # busy indicator.
            d.setCancelButton(None)
            d.setWindowModality(Qt.ApplicationModal)
            d.setMinimumDuration(0)
            d.setAutoClose(False)
            d.setAutoReset(False)
            d.setWindowFlags(d.windowFlags() & ~Qt.WindowContextHelpButtonHint)
            d.show()
            # Let Qt paint the dialog frame in this same processEvents
            # tick so the marquee shows up immediately rather than after
            # the next pump.
            QApplication.processEvents()
            dlg_box[0] = d

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(_show_popup)
        timer.start(self._BUSY_POPUP_DELAY_MS)
        try:
            work()
        finally:
            timer.stop()
            timer.deleteLater()
            if dlg_box[0] is not None:
                dlg_box[0].close()
                dlg_box[0].deleteLater()
            self._render_busy_active = False

    # Backwards-compat thin wrapper: existing toggle signals connect to
    # this name. New callers should prefer :meth:`_run_with_busy_popup`
    # directly with their specific work function.
    def _render_with_busy_popup(self) -> None:
        self._run_with_busy_popup(self._render)

    def _pump_busy_ui(self) -> None:
        """Yield briefly to Qt so the busy popup's deferred-show timer
        can fire AND the marquee bar can repaint mid-work. Cheap when no
        popup is up (single bool check) and throttled to ~33 Hz when one
        is, so callers can sprinkle it in hot inner loops (every polygon,
        every per-(phys, net) tile) without blowing up overhead. Qt's
        marquee animates by an internal timer at ~30 Hz; pumping faster
        than that wastes CPU."""
        if not self._render_busy_active:
            return
        now = time.perf_counter()
        if now - self._last_pump_time < 0.03:
            return
        self._last_pump_time = now
        QApplication.processEvents()

    def _render(self) -> None:
        # Optional per-stage timing. When ``self._render_profile`` is a
        # list (set by tools/bench_recolor.py) each stage appends a
        # ``(name, seconds)`` pair; when it's None ``_mark`` is a cheap
        # no-op so production renders pay nothing.
        _prof = getattr(self, "_render_profile", None)
        _t0 = time.perf_counter()
        _tprev = _t0

        def _mark(_name: str) -> None:
            nonlocal _tprev
            if _prof is not None:
                _now = time.perf_counter()
                _prof.append((_name, _now - _tprev))
                _tprev = _now
            # Pump the Qt event loop at every stage boundary so the busy
            # popup's marquee bar keeps animating during long renders.
            # No-op when no popup is up; ~microseconds when one is.
            self._pump_busy_ui()

        phys_list, rails, mode = self._current_selection()
        label, unit, derive_fn = self._mode_derive_fn(mode)
        is_via_current = (mode == _VIA_CURRENT_MODE)

        # Overlays (silkscreen / pads / vias / components / designators)
        # are independent of the FEM heatmap. Refresh them up-front so a
        # rail or 2D/3D change keeps them in sync — and so they still draw
        # when the early-outs below fire (no layer / rail selected).
        self._refresh_overlay_geometry(rails)
        # Same up-front treatment for the editor-mode component selection
        # box — it must survive the no-layer / no-rail early-outs below.
        self._refresh_editor_selection()

        # Copper-mesh cmap follows the active mode: every other mode
        # paints the copper from its per-vertex scalar field, so we
        # need the viridis ramp. Via Current paints the heatmap onto
        # the vias instead — the copper drops out as context, so we
        # push a flat-grey LUT and ignore the per-vertex values.
        self._ensure_gl_cmap("neutral" if is_via_current else "data")

        # Drop cached probe state until we build new layer probes.
        # In Via Current mode the per-vertex copper values are blanked
        # (vs_arr is zeroed below), so reporting them as "Via Current"
        # would mislead — fall back to the underlying voltage label /
        # unit so the hover bar still reads a meaningful number for
        # the copper underneath the cursor. The via overlay (the
        # ``_via_hover_info`` suffix) is what surfaces the actual
        # current reading when the cursor sits on a via.
        if is_via_current:
            self._probe_label = "Voltage"
            self._probe_unit = "V"
        else:
            self._probe_label = label
            self._probe_unit = unit
        self._layer_probes: list[dict] = []

        if not phys_list or not rails:
            self._gl_viewer.clear_mesh()
            self._gl_viewer.clear_outlines()
            self._gl_viewer.clear_cylinders()
            self._gl_viewer.clear_arrows()
            self._gl_viewer.clear_series_bars()
            self._gl_viewer.clear_stub_triangles()
            self._gl_viewer.set_overlay_top_right("")
            self._gl_viewer.clear_markers()
            self._refresh_board_outline()
            if not phys_list:
                self.summary_label.setText("(no layers selected)")
            else:
                self.summary_label.setText("(no rails selected)")
            return

        # Combine every per-net layer in the selected rail groups across
        # every visible physical layer into one GPU batch. Per-layer
        # interpolators come back alongside for the CPU-side probe + the
        # Voltage Drop reference lookup.
        xs, ys, zs, vs, tris, layer_probes = self._build_rail_arrays(
            phys_list, rails, derive_fn,
        )
        self._layer_probes = layer_probes
        _mark("build_rail_arrays")
        if xs.size == 0 or tris.size == 0:
            self._gl_viewer.clear_mesh()
            self._gl_viewer.clear_outlines()
            self._gl_viewer.clear_cylinders()
            self._gl_viewer.clear_arrows()
            self._gl_viewer.clear_series_bars()
            self._gl_viewer.clear_stub_triangles()
            self._gl_viewer.set_overlay_top_right("")
            self._gl_viewer.clear_markers()
            self._refresh_board_outline()
            self.summary_label.setText("(no mesh — selected layers have no copper on these rails)")
            return

        # Vertices referenced by at least one triangle. Orphan vertices
        # (pinned to V=0 by the FEM solver to keep the linear system
        # well-conditioned) are invisible in the heatmap but would
        # otherwise dominate min/max stats — especially in Voltage Drop
        # mode where they'd appear as a fake -V_source drop.
        used_idx = np.unique(tris.ravel())
        vs_used = vs[used_idx] if used_idx.size > 0 else vs

        # _build_rail_arrays returns numpy arrays directly; alias them
        # so the downstream code (which used to wrap lists) keeps reading.
        xs_arr = xs
        ys_arr = ys
        vs_arr = vs
        tris_arr = tris
        drop_reference: float | None = None
        drop_reference_at: str = ""

        if is_via_current:
            # Range comes from the per-via |I| report, restricted to vias
            # on the selected rails (independent of which physical layers
            # are toggled visible — flipping layers shouldn't rescale the
            # colour bar). Per-vertex copper values aren't meaningful in
            # this mode, so we zero them out; the neutral LUT collapses
            # every entry to the same grey anyway.
            vmin, vmax, self._via_current_lookup = (
                self._via_current_lookup_and_range(rails)
            )
            logging.getLogger(__name__).debug(
                "Via Current: %d vias on rails=%s, raw range=[%.6g, %.6g] A",
                len(self._via_current_lookup), rails, vmin, vmax,
            )
            vs_arr = np.zeros_like(vs_arr, dtype=np.float64)
        else:
            self._via_current_lookup = {}
            vmin, vmax = float(vs_used.min()), float(vs_used.max())
            vmax = _slider_data_max(vs_used, mode, vmax)
            if vmax <= vmin:
                vmax = vmin + 1e-12

        # Voltage Drop mode: anchor the reference at the SOURCE (highest
        # voltage on the rail across any visible layer) and subtract it
        # so the heatmap reads 0 V at the source and goes NEGATIVE
        # toward the sinks. Pin voltages are sampled from the
        # *per-layer* interpolator matching each pin's layer_id, so the
        # reference is correct even when multiple physical layers are on.
        if mode == _VOLTAGE_DROP_MODE:
            target_layer_ids = {self._phys_name_to_layer_id.get(p)
                                for p in phys_list}
            target_layer_ids.discard(None)
            rail_members = set(self._effective_rail_members(rails))
            # layer_id → list of layer_probes (could be >1 if multiple
            # nets in the rail group are on the same physical layer).
            probes_by_layer: dict[int, list[dict]] = {}
            for lp in layer_probes:
                probes_by_layer.setdefault(lp["layer_id"], []).append(lp)

            def _sample_pin_voltage(layer_id: int, x_mm: float,
                                     y_mm: float, net: str) -> float | None:
                # Prefer the probe whose net matches the pin's net; fall
                # back to whichever probe on the same layer reports a
                # non-masked sample.
                candidates = probes_by_layer.get(layer_id, [])
                ordered = sorted(candidates,
                                  key=lambda lp: 0 if lp["net"] == net else 1)
                for lp in ordered:
                    interp = self._ensure_interpolator(lp)
                    if interp is None:
                        continue
                    s = interp(x_mm, y_mm)
                    try:
                        if hasattr(s, "mask") and \
                                bool(np.ma.getmaskarray(s).item()):
                            continue
                        v = float(s)
                    except (TypeError, ValueError):
                        continue
                    if np.isfinite(v):
                        return v
                return None

            def _collect_candidates(role_filter: str | None
                                    ) -> list[tuple[float, str]]:
                out: list[tuple[float, str]] = []
                for d in (self.metadata or {}).get("directives", []):
                    if role_filter is not None and d.get("role") != role_filter:
                        continue
                    for term in (d.get("terminals") or {}).values():
                        for pin in term.get("pins", []):
                            lid = pin.get("layer_id")
                            if lid not in target_layer_ids:
                                continue
                            pnet = pin.get("net", "")
                            if rail_members and pnet not in rail_members:
                                continue
                            v_at = _sample_pin_voltage(
                                lid, pin.get("x_mm"), pin.get("y_mm"), pnet,
                            )
                            if v_at is None:
                                continue
                            out.append(
                                (v_at, f"{d.get('label') or d.get('designator', '?')}"
                                       f".{pin.get('pad', '?')}")
                            )
                return out

            candidates = _collect_candidates("SOURCE")
            if not candidates:
                candidates = _collect_candidates(None)

            if candidates:
                drop_reference, drop_reference_at = max(candidates,
                                                         key=lambda t: t[0])
                vs_arr = vs_arr - drop_reference
                # Recompute range against the used (non-orphan) subset
                # only, otherwise orphan pins shifted by -drop_reference
                # would set a fake floor at -V_source.
                vs_used = vs_arr[used_idx] if used_idx.size > 0 else vs_arr
                vmin, vmax = float(vs_used.min()), float(vs_used.max())
                vmax = _slider_data_max(vs_used, mode, vmax)
                if vmax <= vmin:
                    vmax = vmin + 1e-12
                # Re-shift each layer probe's values + rebuild samplers
                # so the hover probe reports the shifted (drop) values too.
                # Pre-setting ``interpolator`` here (rather than clearing it
                # to None) is deliberate: Voltage and Voltage Drop share a
                # _layer_cache entry, so a lazily-built sampler would write
                # the *shifted* field back into the cache and corrupt plain
                # Voltage mode. _FastTriSampler builds in ~50-150 ms, so the
                # eager rebuild no longer stalls the way LinearTriInterpolator
                # did.
                for lp in layer_probes:
                    shifted = lp["values"] - drop_reference
                    lp["values"] = shifted
                    lp["interpolator"] = _FastTriSampler(
                        lp["triangulation"], shifted,
                    )

        x_min, x_max = float(xs_arr.min()), float(xs_arr.max())
        y_min, y_max = float(ys_arr.min()), float(ys_arr.max())
        self._data_bounds = (x_min, x_max, y_min, y_max)

        # Default colour-scale window (percentile-clipped for spike-prone
        # modes so FEM singularities don't crush the visible range).
        # Use the used (non-orphan) subset for the percentile calc too.
        # Via Current applies a similar clip — real boards have a
        # long-tail distribution (lots of low-current stitching vias,
        # a handful of high-current vias near regulators), so a raw
        # min..max map crushes 99% of the vias into the bottom of the
        # LUT. Clipping at P99 keeps the bulk of the vias spanning the
        # ramp while leaving outliers visible (clamped to the top).
        if is_via_current:
            display_min, display_max = self._via_current_display_range(
                self._via_current_lookup, vmin, vmax,
            )
        else:
            vs_used = vs_arr[used_idx] if used_idx.size > 0 else vs_arr
            display_min, display_max = self._useful_display_range(
                vs_used, mode, vmin, vmax,
            )

        # Resolve the linear vs log colour scale for this render. Log is
        # only meaningful for the wide-dynamic-range modes and needs a
        # strictly positive window, so it's decided here — once — and the
        # GL push, the scale bar and the baked via overlays all key off
        # ``self._log_active`` / ``self._log_floor`` for a consistent map.
        log_eligible = mode in _LOG_ELIGIBLE_MODES
        self._log_active = (self._log_scale and log_eligible
                            and math.isfinite(vmax) and vmax > 0.0)
        if self._log_active:
            self._log_floor = max(vmax * 10.0 ** (-_LOG_SCALE_DECADES),
                                   1e-300)
            # A log axis can't show zero / negatives — floor the window.
            # It also keeps the bulk readable AND the spikes on-scale by
            # itself, so the default window spans the full (floored) data
            # range rather than the linear-scale percentile clip.
            vmin = max(vmin, self._log_floor)
            display_min, display_max = vmin, vmax
        else:
            self._log_floor = 1e-12

        # If the heatmap selection (layers/rails/mode/rail-only-filter/scale)
        # hasn't changed since the last render — e.g. a 2D ↔ 3D toggle, which
        # only rebuilds the mesh geometry — keep the user's current clamp
        # instead of snapping back to the auto-detected default. A linear↔log
        # switch counts as a change so the window resets to the new default.
        selection_sig = (tuple(phys_list), tuple(rails), mode,
                         self.rail_only_box.isChecked(), self._log_active)
        preserve_scale = (self._last_scale_selection == selection_sig)
        self._last_scale_selection = selection_sig
        if preserve_scale:
            levels_min, levels_max = self._vmin, self._vmax
        else:
            self._vmin, self._vmax = display_min, display_max
            levels_min, levels_max = display_min, display_max

        # Push everything to the GPU. set_mesh is the heavy upload; on
        # mode-only switches it's a no-op (xs/ys/tris unchanged) — TODO
        # could detect and skip, but the upload is cheap (<5ms typical).
        # ``zs`` is per-vertex z (mm, pre-exaggeration); only used by the
        # 3D mode's perspective MVP — ignored by the 2D ortho path.
        _mark("prep")
        self._gl_viewer.set_mesh(xs_arr, ys_arr, tris_arr,
                                  data_bounds=self._data_bounds,
                                  zs=zs.astype(np.float32))
        # Values + levels are pushed through _gl_scale: identity on a
        # linear scale, log10 (floored) on a log scale. Pushing both in
        # the same space means the GL viewer's linear normalisation
        # shader produces the correct log mapping with no shader change.
        self._gl_viewer.set_values(self._gl_scale(vs_arr).astype(np.float32))
        self._gl_viewer.set_levels(float(self._gl_scale(levels_min)),
                                    float(self._gl_scale(levels_max)))
        # Per-vertex mesh alpha: combines the per-layer Transparency
        # control with editor-mode connectivity dimming. Pushed after
        # set_mesh (which invalidates any previous alpha array).
        self._gl_viewer.set_vertex_alpha(
            self._combined_mesh_alpha_array(layer_probes, xs_arr.size)
        )
        _mark("gl_mesh_upload")

        # Carry the user's chosen colour scheme through the re-render
        # (mode / layer / rail switches must not snap it back to default).
        self.scale_controller.setLogVisible(log_eligible)
        self._update_scale_controller(vmin, vmax, display_min, display_max,
                                       self._cmap_name, label, unit,
                                       reset_selection=not preserve_scale,
                                       log_active=self._log_active)
        _mark("scale_controller")

        # Markers + legend (also overlaid via the GLMeshViewer's QPainter
        # layer, so they don't trigger Qt's raster-fallback compositor).
        # The Vias-tab "Go" highlight is appended regardless of the
        # show_markers checkbox so the user can still find the via they
        # just jumped to.
        self._update_markers_and_legend(phys_list, rails)
        _mark("markers_legend")

        # Layer + pad outlines (GL_LINES). Cheap toggle — segments are
        # cached at first use, so flipping either checkbox just uploads /
        # clears one VBO pair.
        self._refresh_outlines(layer_probes, phys_list, rails)
        _mark("outlines")

        # Stub-copper overlay — polygons of copper the FEM excluded.
        # Always pushed so users see what's there even when arrows /
        # outlines / heatmap-vias are off. Default is flat grey; if the
        # user opts into colour-by-V we sample the same-net solved
        # layer at each stub's centroid using ``mode`` + ``drop_reference``.
        self._push_stubs(phys_list, rails, mode=mode,
                          drop_reference=drop_reference)
        _mark("stubs")

        # Series-component bars — gradient rectangles between the two
        # terminal pin positions of each RESISTOR directive.
        self._push_series_bars(phys_list, rails, mode,
                               drop_reference=drop_reference)
        _mark("series_bars")

        # Via cylinders — only meaningful in 3D mode (in 2D they'd
        # collapse to overlapping circles at z=0). The heatmap-vias path
        # needs the same Voltage-Drop reference the layer heatmap used.
        # Via Current mode always pushes cylinders in 3D; in 2D the
        # per-via colours are emitted via the marker overlay path inside
        # :meth:`_update_markers_and_legend`.
        self._last_drop_reference = drop_reference
        if self.view_3d_box.isChecked():
            self._push_via_cylinders(phys_list, rails, mode=mode)
        else:
            self._gl_viewer.clear_cylinders()
        _mark("via_cylinders")

        # Current-flow arrow overlay (2D only). Cheap if disabled.
        self._refresh_arrows()
        _mark("arrows")

        # Board outline overlay — independent of layer/rail selection.
        self._refresh_board_outline()
        _mark("board_outline")

        # Fit to data ONLY on the very first render (or while we're
        # still waiting for the deferred initial fit after Qt sizes the
        # widget). Subsequent renders — layer toggles, mode/rail
        # changes — leave the user's pan/zoom alone.
        if self._need_initial_fit:
            self._fit_board_to_canvas(x_min, x_max, y_min, y_max)

        # Summary stats over the mesh's actual values — orphan vertices
        # excluded (same reason as the scale-range filtering above). In
        # Via Current mode the per-vertex copper values are blanked, so
        # we report stats over the per-via |I| set instead.
        if is_via_current:
            via_vals = list(self._via_current_lookup.values())
            if via_vals:
                arr = np.asarray(via_vals, dtype=np.float64)
                self.summary_label.setText(
                    f"<b>{label}</b><br>"
                    f"min = {arr.min():.4g} {unit}<br>"
                    f"max = {arr.max():.4g} {unit}<br>"
                    f"mean = {arr.mean():.4g} {unit}<br>"
                    f"vias: {arr.size:,}"
                )
            else:
                self.summary_label.setText(
                    f"<b>{label}</b><br>"
                    "(no vias on the selected rails)"
                )
        else:
            vs_used = vs_arr[used_idx] if used_idx.size > 0 else vs_arr
            self.summary_label.setText(
                f"<b>{label}</b><br>"
                f"min = {vs_used.min():.4g} {unit}<br>"
                f"max = {vs_used.max():.4g} {unit}<br>"
                f"mean = {vs_used.mean():.4g} {unit}<br>"
                f"vertices: {len(vs_used):,}"
            )
        _mark("summary")
        if _prof is not None:
            _prof.append(("TOTAL", time.perf_counter() - _t0))

    # --- Side-panel scale controller ----------------------------------------

    def _update_scale_controller(self, data_min: float, data_max: float,
                                 sel_min: float, sel_max: float,
                                 cmap_name: str, label: str, unit: str,
                                 reset_selection: bool = True,
                                 log_active: bool = False
                                 ) -> None:
        """Push fresh data bounds + label/unit into the scale controller.

        With ``reset_selection=True`` (default) the user clamp snaps back
        to (sel_min, sel_max) — that's the right behaviour across layer /
        rail / mode changes (e.g. a Voltage Drop clamp of -0.05..-0.01 is
        nonsense when you switch to Current Density). Callers re-rendering
        the same selection (e.g. a 2D/3D toggle) pass ``False`` so the
        user's existing clamp is preserved.

        ``log_active`` is the effective scale type — the gradient strip
        needs it set before :meth:`ScaleController.setRange` so the
        handles land at the right (log-spaced) positions.
        """
        self._cmap_name = cmap_name
        self.scale_controller.setColormap(cmap_name)
        self.scale_controller.setLogActive(log_active)
        self.scale_controller.setLabelUnit(label, unit)
        self.scale_controller.setRange(data_min, data_max,
                                         sel_min=sel_min, sel_max=sel_max,
                                         reset_selection=reset_selection)

    def _via_current_display_range(
        self,
        lookup: dict[tuple[str, float, float], float],
        data_min: float, data_max: float,
    ) -> tuple[float, float]:
        """Pick the default Via Current colour-scale window.

        Long-tail distributions (most vias near 0 A, a few outliers
        near a regulator) crush the bulk of the vias into the bottom
        of the LUT under a raw min..max map. Two adjustments:

        1. When there are enough samples for a percentile to be
           meaningful (>= 8 vias), clip the top to
           :data:`_DISPLAY_PERCENTILE_HIGH` of the data so the bulk
           of the vias span the ramp.
        2. When every via reports the same current (or the range is
           numerically degenerate), widen the window to ±10% around
           the value so all vias don't collapse to LUT entry 0.
        """
        if not lookup:
            return data_min, data_max
        arr = np.asarray(list(lookup.values()), dtype=np.float64)
        span = data_max - data_min
        # Degenerate-range case: all vias carry essentially the same
        # current. Without this widening, ``t = (cur - vmin) / span``
        # collapses to 0 for every via and the whole batch renders at
        # LUT index 0 (the deep-purple end of viridis).
        if span <= max(1e-9, abs(data_min) * 1e-6):
            anchor = float(arr.mean()) if arr.size else data_min
            half = max(abs(anchor) * 0.1, 1e-6)
            return anchor - half, anchor + half
        if arr.size < 8:
            return data_min, data_max
        sel_max = float(np.percentile(arr, self._display_percentile_high))
        # Only clip when the percentile is meaningfully below the raw
        # max — otherwise the slider's drag-to-real-max range
        # collapses and the heatmap reads the same as the unclipped
        # case anyway.
        if sel_max >= data_max * 0.95:
            return data_min, data_max
        # Guard against a clip that collapses the range (happens when
        # the bulk of the vias share a single value and only a few
        # outliers stretch the raw max). Fall back to the unclipped
        # range and let the user drag the scale if they care.
        if sel_max - data_min <= max(1e-9, abs(data_min) * 1e-6):
            return data_min, data_max
        return data_min, sel_max

    def _useful_display_range(self, vs_arr: np.ndarray, mode: str,
                              data_min: float, data_max: float,
                              ) -> tuple[float, float]:
        """Pick the default colour-scale window.

        For modes prone to FEM singularities at pinned-voltage vertices
        (Current Density, Power Density), the top end is clipped to
        :data:`_DISPLAY_PERCENTILE_HIGH` of the data so a handful of
        outlier vertices near a SOURCE/SINK pin don't crush the rest of
        the board to the bottom of the colour scale. Returns
        ``(sel_min, sel_max)`` ready to hand to the rasterisation step
        and the scale controller.
        """
        if mode not in _SPIKE_PRONE_MODES or len(vs_arr) < 100:
            return data_min, data_max
        sel_max = float(np.percentile(vs_arr, self._display_percentile_high))
        # Only clip if the percentile is meaningfully below the actual
        # max — otherwise the slider's drag-to-real-max range collapses.
        if sel_max >= data_max * 0.95:
            return data_min, data_max
        if sel_max <= data_min:
            sel_max = data_min + (data_max - data_min) * 1e-3
        return data_min, sel_max

    # --- Directive-pin overlay -----------------------------------------------

    # Marker style per role — colours stand out against the viridis heatmap
    # AND on the dark viewbox background. ``symbol`` is a pyqtgraph marker
    # name (one of 'o', 's', 't', 'd', 'star', '+', 'x', 'p', 'h', 't1' …).
    # NOTE: keys match the role string produced by
    # altium_loader.build_solve_metadata() — that strips the trailing "Spec"
    # off the dataclass name and uppercases it (so ResistorSpec → RESISTOR).
    # Keyed by the role string seen in the relevant context: solved
    # directives carry the Spec class name (``RESISTOR`` for a SERIES
    # element — see altium_loader), while editor directives carry the
    # ``EDITOR_ROLES`` name (``SERIES``). Both keys map to the same style.
    _ROLE_MARKER_STYLE: dict[str, dict] = {
        "SOURCE":    {"symbol": "tri_up",   "color": "#ff3030", "size": 18, "label": "SOURCE"},
        "SINK":      {"symbol": "tri_down", "color": "#3aa8ff", "size": 16, "label": "SINK"},
        "RESISTOR":  {"symbol": "s",        "color": "#3aff8a", "size": 12, "label": "SERIES"},
        "SERIES":    {"symbol": "s",        "color": "#3aff8a", "size": 12, "label": "SERIES"},
        "REGULATOR": {"symbol": "d",        "color": "#ff66ff", "size": 14, "label": "REGULATOR"},
    }

    # --- Directive-pin marker + legend overlay -----------------------------

    # Glyph used in the legend swatch for each marker symbol name. Drawn
    # in the swatch column of the legend chip (right-side overlay).
    _LEGEND_GLYPHS: dict[str, str] = {
        "star":     "★",
        "o":        "●",
        "s":        "■",
        "d":        "◆",
        "bolt":     "⚡",
        "target":   "◎",
        "tri_up":   "▲",
        "tri_down": "▼",
    }

    def _refresh_outlines(self, layer_probes: list[dict],
                           phys_list: list[str],
                           rail_names: list[str] | str | None = None) -> None:
        """Push the combined layer-outline + pad-outline overlay to the GL
        viewer as one GL_LINES batch.

        Layer outlines (gated by ``_outlines_btn``) trace each visible
        (layer, net) shape in the physical layer's swatch colour. Stub
        outlines (same gate, same colour) trace the no-current copper
        pieces that the FEM filter excluded — these aren't in
        ``layer_probes`` because there's no FEM solution for them, so we
        walk ``metadata['stubs']`` separately.

        Each segment is promoted to 3D with z = the layer's stackup z, so
        the outline sits flush with the copper's top face. In 3D mode a
        second copy is emitted at the BOTTOM of the extruded copper prism
        (``z - copper_thickness``) so the polygon is traced on both faces
        of the plate. Empty result clears the overlay.
        """
        in_3d = self.view_3d_box.isChecked()
        thickness = self._COPPER_THICKNESS_MM if in_3d else 0.0
        pos_chunks: list[np.ndarray] = []
        col_chunks: list[np.ndarray] = []

        def _emit(segs_xy: np.ndarray, z_top: float,
                  rgb: np.ndarray) -> None:
            if segs_xy.size == 0:
                return
            segs_top = np.empty((segs_xy.shape[0], 3), dtype=np.float32)
            segs_top[:, :2] = segs_xy
            segs_top[:, 2] = z_top
            pos_chunks.append(segs_top)
            col_chunks.append(np.broadcast_to(rgb,
                                              (segs_xy.shape[0], 3)).copy())
            if thickness > 0.0:
                segs_bot = np.empty((segs_xy.shape[0], 3), dtype=np.float32)
                segs_bot[:, :2] = segs_xy
                segs_bot[:, 2] = z_top - thickness
                pos_chunks.append(segs_bot)
                col_chunks.append(
                    np.broadcast_to(rgb, (segs_xy.shape[0], 3)).copy()
                )

        if self._outlines_btn.isOn():
            for lp in layer_probes:
                segs = lp.get("outline_segments")
                if segs is None or segs.size == 0:
                    continue
                phys = lp.get("physical", "")
                qc = QColor(self._layer_color_for(phys))
                rgb = np.array([qc.redF(), qc.greenF(), qc.blueF()],
                               dtype=np.float32)
                _emit(segs, self._layer_z_for(phys), rgb)

            # Stub copper has no FEM solution and is absent from
            # layer_probes, but the user still expects to see the same
            # layer-coloured outline around it (it's real copper). Walk
            # metadata['stubs'] using the same visible-layer + rail-net
            # filter that _push_stubs applies, so the outline overlay
            # tracks exactly what the grey fill is showing.
            if self.metadata is not None:
                stubs = self.metadata.get("stubs") or []
                if stubs:
                    visible_layer_ids: dict[int, str] = {}
                    for phys in phys_list:
                        lid = self._phys_name_to_layer_id.get(phys)
                        if lid is not None:
                            visible_layer_ids[lid] = phys
                    rail_members = (set(self._effective_rail_members(rail_names))
                                    if rail_names is not None else set())
                    for stub in stubs:
                        lid = stub.get("layer_id")
                        phys = visible_layer_ids.get(lid)
                        if phys is None:
                            continue
                        net = stub.get("net")
                        if rail_members and net not in rail_members:
                            continue
                        segs = self._stub_outline_segments(stub)
                        if segs.size == 0:
                            continue
                        qc = QColor(self._layer_color_for(phys))
                        rgb = np.array([qc.redF(), qc.greenF(), qc.blueF()],
                                       dtype=np.float32)
                        _emit(segs, self._layer_z_for(phys), rgb)

        if pos_chunks:
            self._gl_viewer.set_outlines(
                np.concatenate(pos_chunks, axis=0),
                np.concatenate(col_chunks, axis=0),
            )
        else:
            self._gl_viewer.clear_outlines()

    # Half-width of the board-outline ribbon in mm. 0.2 mm reads as a
    # bold accent at typical board zooms without obscuring fine copper
    # features near the edge.
    _BOARD_OUTLINE_HALF_WIDTH_MM: float = 0.2

    def _refresh_board_outline(self) -> None:
        """Push or clear the board-outline ribbon overlay.

        The metadata ``board_outline`` is a closed polyline of [x, y] in
        mm (origin-corrected). It's triangulated into a fixed-mm-wide
        ribbon with mitered interior joins so the line thickness is
        uniform across drivers and reads boldly at any zoom (a raw
        GL_LINES pass would be clamped to 1 px on most Core profile
        drivers). Drawn on top of the heatmap in both 2D and 3D modes.
        """
        bo_state = self._overlay_state.get("board_outline", {}).get("both", {})
        if bo_state.get("vis") is None:
            self._gl_viewer.clear_board_outline()
            return
        points = (self.metadata or {}).get("board_outline") or []
        if len(points) < 3:
            self._gl_viewer.clear_board_outline()
            return

        ring = np.asarray(points, dtype=np.float64)
        n = ring.shape[0]
        # Per-vertex mitered offset normal: average of the incoming and
        # outgoing edge normals, scaled so the projected offset matches
        # the requested half-width along each adjacent edge. Clamped to
        # 4× half-width to keep sharp corners from spiking out.
        edges = np.roll(ring, -1, axis=0) - ring
        edge_len = np.linalg.norm(edges, axis=1)
        edge_len[edge_len == 0.0] = 1.0
        edge_dir = edges / edge_len[:, None]
        # Right-hand normal in 2D: (dx, dy) -> (dy, -dx).
        edge_normal = np.empty_like(edge_dir)
        edge_normal[:, 0] = edge_dir[:, 1]
        edge_normal[:, 1] = -edge_dir[:, 0]
        prev_normal = np.roll(edge_normal, 1, axis=0)
        bisector = edge_normal + prev_normal
        bis_len = np.linalg.norm(bisector, axis=1)
        # Degenerate (collinear reversal) — fall back to the outgoing normal.
        flat = bis_len < 1e-9
        bisector[flat] = edge_normal[flat]
        bis_len[flat] = 1.0
        bisector /= bis_len[:, None]
        # Miter length: half-width / cos(theta/2) = half-width / dot(bisector, normal).
        hw = self._BOARD_OUTLINE_HALF_WIDTH_MM
        dot = np.einsum("ij,ij->i", bisector, edge_normal)
        dot = np.where(np.abs(dot) < 0.25, np.sign(dot) * 0.25, dot)
        miter_len = hw / dot
        offset = bisector * miter_len[:, None]
        outer = ring + offset
        inner = ring - offset

        # Build GL_TRIANGLES (one quad per edge = 2 triangles = 6 verts).
        idx = np.arange(n)
        nxt = (idx + 1) % n
        o_a = outer[idx]
        i_a = inner[idx]
        o_b = outer[nxt]
        i_b = inner[nxt]
        # tri 1: o_a, i_a, o_b ;  tri 2: o_b, i_a, i_b
        positions_2d = np.empty((n * 6, 2), dtype=np.float32)
        positions_2d[0::6] = o_a
        positions_2d[1::6] = i_a
        positions_2d[2::6] = o_b
        positions_2d[3::6] = o_b
        positions_2d[4::6] = i_a
        positions_2d[5::6] = i_b
        positions = np.zeros((positions_2d.shape[0], 3), dtype=np.float32)
        positions[:, :2] = positions_2d
        # Lift slightly above z=0 in 3D so the ribbon doesn't z-fight
        # with anything drawn at the same plane.
        if self.view_3d_box.isChecked():
            positions[:, 2] = 0.01

        # User-set colour for the board-outline row (defaults to a bold
        # warm orange — reads on both viridis and the standard dark/light
        # themes without being mistaken for any of the layer swatches).
        # Alpha follows the row's TransparencyButton.
        colour = np.asarray(self._overlay_colors["board_outline"],
                            dtype=np.float32)
        alpha = _transparency_alpha(bo_state.get("alpha_step", 0))
        colors = np.empty((positions.shape[0], 4), dtype=np.float32)
        colors[:, :3] = colour
        colors[:, 3] = alpha
        self._gl_viewer.set_board_outline(positions, colors)

    # --- Stub-copper overlay -----------------------------------------------

    # RGB of the stub-copper polygons — dim grey so they're visibly
    # present but obviously distinct from the heatmap LUT colours.
    # The viewer's clear-colour is black, so this sits clearly above the
    # background without competing with the viridis-coloured rails.
    _STUB_COLOR_RGB: tuple[float, float, float] = (
        0x60 / 255.0, 0x60 / 255.0, 0x60 / 255.0,
    )

    def _stubs_coloured_by_voltage(self, mode: str | None) -> bool:
        """True when stub copper is shaded from the active colour scheme
        rather than flat grey.

        Stub voltage colouring is opt-in (the ``colour_stubs_box`` toggle)
        and only meaningful for the Voltage / Voltage Drop modes — current
        and power are zero inside a stub by definition. When this returns
        False a colour-scheme change can't affect the stubs, so the fast
        recolour path (:meth:`_recolor_overlays`) can skip re-pushing them.
        """
        return (
            getattr(self, "colour_stubs_box", None) is not None
            and not self.colour_stubs_box.isChecked()
            and mode in ("Voltage", _VOLTAGE_DROP_MODE)
        )

    def _push_stubs(self, phys_list: list[str],
                    rail_names: list[str] | str,
                    *, mode: str | None = None,
                    drop_reference: float | None = None) -> None:
        """Build triangle geometry for every stub copper piece on the
        visible layers + selected rail groups, and push it as one batch
        to the GL viewer.

        Stubs are copper pieces the FEM filter excluded (no current
        path through them). We render them anyway so the user can SEE
        the copper exists; otherwise the heatmap looks like the copper
        vanished. By default each stub is flat dim grey (obviously
        "no data"); when the user toggles ``colour_stubs_box`` AND the
        active mode is Voltage / Voltage Drop, each stub is instead
        coloured by its approximate voltage — sampled from the same-net
        solved layer at the stub's centroid. Voltage is constant across
        each stub (no current flows through it).

        The triangle *positions* depend only on the visible layer/rail
        set and the 2D/3D z — never on the colour scheme or scale — so
        they're cached (single entry, keyed on exactly those inputs).
        A colour-scheme / scale change reuses the cached positions and
        only re-bakes the per-vertex colour array, which is what makes
        the :meth:`_recolor_overlays` fast path cheap. Per-stub
        triangulation is itself cached on the stub dict, so even a cache
        miss just re-aggregates cached triangles.
        """
        if self.metadata is None or not phys_list:
            self._gl_viewer.clear_stub_triangles()
            return
        stubs = self.metadata.get("stubs") or []
        if not stubs:
            self._gl_viewer.clear_stub_triangles()
            return
        visible_layer_ids: dict[int, str] = {}
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                visible_layer_ids[lid] = phys
        if not visible_layer_ids:
            self._gl_viewer.clear_stub_triangles()
            return
        rail_members = set(self._effective_rail_members(rail_names))
        in_3d = self._gl_viewer.view_mode() == "3d"

        # Reuse cached positions when the visible stub set + z mode are
        # unchanged (e.g. this call arrived via a colour-scheme toggle).
        geom_key = (frozenset(visible_layer_ids), frozenset(rail_members),
                    in_3d)
        cache = self._stub_geom_cache
        if cache is not None and cache[0] == geom_key:
            positions, spans = cache[1], cache[2]
        else:
            positions, spans = self._build_stub_geometry(
                stubs, visible_layer_ids, rail_members, in_3d)
            self._stub_geom_cache = (geom_key, positions, spans)

        if positions is None:
            self._gl_viewer.clear_stub_triangles()
            return
        colors = self._bake_stub_colors(spans, mode, drop_reference)
        self._gl_viewer.set_stub_triangles(positions, colors)

    def _build_stub_geometry(
        self, stubs: list[dict], visible_layer_ids: dict[int, str],
        rail_members: set[str], in_3d: bool,
    ) -> tuple[np.ndarray | None, list[tuple[dict, str | None, int]]]:
        """Aggregate the stub triangle positions for the visible stub set.

        Returns ``(positions, spans)`` — ``positions`` is the concatenated
        ``(M, 3)`` float32 vertex array (or ``None`` when no stub is
        visible) and ``spans`` is a parallel list of
        ``(stub, net, vertex_count)`` so :meth:`_bake_stub_colors` can
        rebuild the colour array without re-triangulating. Triangulation
        itself is cached per stub by :meth:`_triangulate_stub`.
        """
        pos_chunks: list[np.ndarray] = []
        spans: list[tuple[dict, str | None, int]] = []
        for stub in stubs:
            lid = stub.get("layer_id")
            if lid not in visible_layer_ids:
                continue
            net = stub.get("net")
            if rail_members and net not in rail_members:
                continue
            tris = self._triangulate_stub(stub)
            if tris.size == 0:
                continue
            z = (self._layer_z_for(visible_layer_ids[lid])
                 if in_3d else 0.0)
            xyz = np.empty((tris.shape[0], 3), dtype=np.float32)
            xyz[:, :2] = tris
            xyz[:, 2] = z
            pos_chunks.append(xyz)
            spans.append((stub, net, xyz.shape[0]))
        if not pos_chunks:
            return None, []
        return np.concatenate(pos_chunks, axis=0), spans

    def _bake_stub_colors(
        self, spans: list[tuple[dict, str | None, int]],
        mode: str | None, drop_reference: float | None,
    ) -> np.ndarray:
        """Build the per-vertex stub colour array for cached geometry.

        Each stub is flat dim grey unless stub voltage-colouring is active
        (:meth:`_stubs_coloured_by_voltage`), in which case it takes the
        LUT colour of its sampled centroid voltage. ``spans`` is the list
        :meth:`_build_stub_geometry` returned alongside the positions.
        """
        total = sum(n for _stub, _net, n in spans)
        colors = np.empty((total, 3), dtype=np.float32)
        default_color = np.asarray(self._STUB_COLOR_RGB, dtype=np.float32)

        # Voltage-coloured mode only makes sense for Voltage / Voltage
        # Drop modes. Current density and power density are zero in a
        # stub by definition — colouring stubs with the lowest LUT entry
        # would be visually misleading, so fall back to grey.
        colour_by_v = self._stubs_coloured_by_voltage(mode)
        lut: np.ndarray | None = None
        vmin = vmax = 0.0
        if colour_by_v:
            lut = _build_cmap_lut(self._cmap_name)
            vmin = float(self._vmin)
            vmax = float(self._vmax)
            if vmax <= vmin:
                vmax = vmin + 1e-30
        use_drop = colour_by_v and mode == _VOLTAGE_DROP_MODE
        drop_ref_f = (float(drop_reference)
                      if (use_drop and drop_reference is not None) else 0.0)

        offset = 0
        for stub, net, n in spans:
            piece_color = default_color
            if colour_by_v and lut is not None:
                v_sample = self._sample_stub_voltage(stub, net)
                if v_sample is not None:
                    val = v_sample - drop_ref_f if use_drop else v_sample
                    piece_color = self._lut_lookup(lut, val, vmin, vmax)
            colors[offset:offset + n] = piece_color
            offset += n
        return colors

    def _sample_stub_voltage(self, stub: dict,
                             net_name: str | None) -> float | None:
        """Estimate the voltage of a stub piece by sampling the same-net
        FEM solution at the stub's centroid.

        Tries every physical layer that has a solved (layer, net) pair.
        Caches the answer on the stub dict — voltages are constant per
        solve so the lookup only needs to happen once per stub.
        """
        if net_name is None:
            return None
        cached = stub.get("_v_sample_cache")
        if cached is not None:
            return cached if cached != "missing" else None
        cx, cy = stub.get("_centroid", (None, None))
        if cx is None:
            from shapely.geometry import Polygon as _Polygon
            ext = stub.get("exterior")
            # Accept numpy (N, 2) array or legacy nested-list; shapely's
            # Polygon takes either via its sequence-of-coordinates ctor.
            if ext is None or (hasattr(ext, "size") and ext.size == 0):
                stub["_v_sample_cache"] = "missing"
                return None
            poly = _Polygon(ext, holes=stub.get("holes") or [])
            if poly.is_empty:
                stub["_v_sample_cache"] = "missing"
                return None
            c = poly.centroid
            cx, cy = float(c.x), float(c.y)
            stub["_centroid"] = (cx, cy)
        for phys in self._physicals:
            v = self._sample_via_voltage(phys, net_name, cx, cy)
            if v is not None:
                stub["_v_sample_cache"] = v
                return v
        # Centroid lies outside all solved meshes for this net (the stub is
        # geometrically disconnected from the solved copper, which is exactly
        # why it's a stub). Fall back to the nearest solved vertex so that
        # colour-by-V can still assign a meaningful voltage.
        v = self._nearest_vertex_voltage(net_name, cx, cy)
        if v is not None:
            stub["_v_sample_cache"] = v
            return v
        stub["_v_sample_cache"] = "missing"
        return None

    def _nearest_vertex_voltage(self, net_name: str,
                                cx: float, cy: float) -> float | None:
        """Return the voltage of the nearest solved vertex for *net_name*.

        Scans every (physical layer, net_name) pair in the solution and
        returns the potential of the vertex closest to (cx, cy). Used as a
        fallback when centroid interpolation fails because the stub centroid
        lies outside the triangulated solved-copper region.
        """
        best_dist_sq = float("inf")
        best_v: float | None = None
        for phys in self._physicals:
            li = self._index_by_pair.get((phys, net_name))
            if li is None:
                continue
            ls = self.solution.layer_solutions[li]
            for xys, pot in zip(ls.vertex_xys, ls.potentials):
                if xys.shape[0] == 0:
                    continue
                dx = xys[:, 0] - cx
                dy = xys[:, 1] - cy
                d2 = dx * dx + dy * dy
                idx = int(np.argmin(d2))
                if d2[idx] < best_dist_sq:
                    best_dist_sq = d2[idx]
                    best_v = float(pot[idx])
        return best_v

    # World-space half-width (mm) of the colored part of the series-component bar.
    # Total colored width = 2 × this = 0.2 mm.
    _SERIES_BAR_HALF_WIDTH_MM: float = 0.2
    # Extra half-width (mm) added on each side for the black outline border.
    # Total bar width including outline = 2 × (hw + border) = 0.3 mm.
    _SERIES_BAR_BORDER_MM: float = 0.05
    # How far (mm) the bar's z sits proud of the copper centroid z on the
    # top and bottom copper layers, so it visually rests on the surface.
    _SERIES_BAR_Z_LIFT_MM: float = 0.001
    # Extrusion height (mm) of the bar above its mounting surface in 3D mode.
    # Set to 0 to revert to a flat rectangle.  Tunable — increase for more
    # visual prominence, decrease if it obscures nearby copper detail.
    _SERIES_BAR_HEIGHT_MM: float = 0.005

    def _push_series_bars(self, phys_list: list[str],
                          rail_names: list[str] | str,
                          mode: str,
                          drop_reference: float | None = None) -> None:
        """Build gradient-filled rectangle geometry for every RESISTOR
        (series) directive visible on the current layers + selected rails,
        and push it as one triangle batch to the GL viewer.

        Each bar runs between the centroid of terminal "P" pins and the
        centroid of terminal "N" pins. The rectangle is ``_SERIES_BAR_HALF_WIDTH_MM``
        wide (world-space mm) and is coloured by the active heatmap mode:

        * **Voltage / Voltage Drop** — smooth gradient from the P-side
          heatmap colour to the N-side colour.
        * **Current Density** — uniform colour computed from I = ΔV / R.
        * **Power Density** — uniform colour from P = I² · R.
        """
        if self.metadata is None or not phys_list:
            self._gl_viewer.clear_series_bars()
            return

        directives = self.metadata.get("directives") or []
        rail_members = set(self._effective_rail_members(rail_names))
        target_layer_ids: set[int] = {
            self._phys_name_to_layer_id[p]
            for p in phys_list
            if p in self._phys_name_to_layer_id
        }
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        in_3d = self.view_3d_box.isChecked()

        lut = _build_cmap_lut(self._cmap_name)
        vmin = float(self._vmin)
        vmax = float(self._vmax)
        if vmax <= vmin:
            vmax = vmin + 1e-30
        drop_ref = float(drop_reference) if drop_reference is not None else 0.0

        # Two chunk lists. ``under_*`` rects are drawn BEFORE the heatmap
        # mesh so a bottom-side bar sits visually beneath the bottom
        # copper in 2D (depth test is off in 2D, so order = layer order).
        # ``over_*`` rects are drawn after the mesh, on top. In 3D the
        # extruded bar's z handles ordering, so everything goes over.
        under_pos_chunks: list[np.ndarray] = []
        under_col_chunks: list[np.ndarray] = []
        over_pos_chunks: list[np.ndarray] = []
        over_col_chunks: list[np.ndarray] = []
        hw = self._SERIES_BAR_HALF_WIDTH_MM
        hw_out = hw + self._SERIES_BAR_BORDER_MM
        z_lift = self._SERIES_BAR_Z_LIFT_MM
        max_rank = len(self._physicals) - 1
        half_cu = self._COPPER_THICKNESS_MM / 2.0
        black = (0.0, 0.0, 0.0)

        def _bar_is_on_bottom(phys1: str | None,
                              phys2: str | None) -> bool:
            r1 = self._phys_stackup_rank.get(phys1, 0) if phys1 else 0
            r2 = self._phys_stackup_rank.get(phys2, 0) if phys2 else 0
            return min(r1, r2) == max_rank

        def _z_for_bar(phys1: str | None,
                       phys2: str | None) -> tuple[float, float]:
            """Return ``(z_base, extrude_sign)`` for the bar.

            z_base: the z the bar sits on (copper surface + tiny lift).
            extrude_sign: +1 when the bar extrudes away from the viewer
            in the +z direction (top layer / inner layers); −1 when it
            extrudes toward −z (bottom copper layer faces downward).
            """
            z1 = self._layer_z_for(phys1) if phys1 else 0.0
            z2 = self._layer_z_for(phys2) if phys2 else 0.0
            z_mid = (z1 + z2) * 0.5
            if _bar_is_on_bottom(phys1, phys2):
                return z_mid - half_cu - z_lift, -1.0
            return z_mid + half_cu + z_lift, 1.0

        for d in directives:
            if d.get("role") != "RESISTOR":
                continue
            terminals = d.get("terminals") or {}
            p_term = terminals.get("P") or {}
            n_term = terminals.get("N") or {}
            p_pins = p_term.get("pins") or []
            n_pins = n_term.get("pins") or []
            if not p_pins or not n_pins:
                continue

            # Find the first pin on a visible layer for each terminal.
            def _pick_pin(pins: list[dict]) -> dict | None:
                for pin in pins:
                    if pin.get("layer_id") in target_layer_ids:
                        return pin
                # fall back to any pin that has an associated visible phys
                return pins[0] if pins else None

            p_pin = _pick_pin(p_pins)
            n_pin = _pick_pin(n_pins)
            if p_pin is None or n_pin is None:
                continue

            # Skip if neither terminal pin is on the rail.
            if rail_members:
                p_net = p_pin.get("net", "")
                n_net = n_pin.get("net", "")
                if p_net not in rail_members and n_net not in rail_members:
                    continue

            x1 = float(p_pin.get("x_mm", 0.0))
            y1 = float(p_pin.get("y_mm", 0.0))
            x2 = float(n_pin.get("x_mm", 0.0))
            y2 = float(n_pin.get("y_mm", 0.0))

            phys1 = id_to_phys.get(p_pin.get("layer_id"))
            phys2 = id_to_phys.get(n_pin.get("layer_id"))
            if in_3d:
                z, extrude_sign = _z_for_bar(phys1, phys2)
            else:
                z, extrude_sign = 0.0, 1.0
            # Route this bar's triangles to the right batch. In 2D the
            # bottom-side bar must draw under the mesh so the bottom
            # copper covers it; all others go over the mesh as usual.
            if not in_3d and _bar_is_on_bottom(phys1, phys2):
                pos_chunks = under_pos_chunks
                col_chunks = under_col_chunks
            else:
                pos_chunks = over_pos_chunks
                col_chunks = over_col_chunks

            # Sample voltage at each pin; fall back to the opposite pin's
            # net if the pin's own net has no solution on this layer.
            def _sample(pin: dict) -> float | None:
                lid = pin.get("layer_id")
                phys = id_to_phys.get(lid)
                if phys is None:
                    phys = phys_list[0]
                net = pin.get("net", "")
                v = self._sample_via_voltage(phys, net,
                                             float(pin.get("x_mm", 0.0)),
                                             float(pin.get("y_mm", 0.0)))
                if v is None:
                    # try any visible layer for this net
                    for pl in phys_list:
                        v = self._sample_via_voltage(
                            pl, net,
                            float(pin.get("x_mm", 0.0)),
                            float(pin.get("y_mm", 0.0)),
                        )
                        if v is not None:
                            break
                return v

            v1 = _sample(p_pin)
            v2 = _sample(n_pin)
            if v1 is None and v2 is None:
                continue
            if v1 is None:
                v1 = v2
            if v2 is None:
                v2 = v1

            # Convert raw voltages to the display value for this mode.
            if mode in ("Voltage", _VOLTAGE_DROP_MODE):
                val1 = v1 - drop_ref
                val2 = v2 - drop_ref
                c1 = self._shade(lut, val1, vmin, vmax)
                c2 = self._shade(lut, val2, vmin, vmax)
            elif mode == _VIA_CURRENT_MODE:
                # Via Current mode doesn't define a value for series
                # resistors — colour them with the same neutral shade
                # the copper uses so they read as context, not data.
                neutral_rgb = tuple(c / 255.0 for c in (160, 160, 160))
                c1 = c2 = neutral_rgb
            else:
                r_ohm = float(d.get("value") or 0.0)
                if r_ohm <= 0.0:
                    r_ohm = 1e-3
                i_abs = abs(v1 - v2) / r_ohm
                if mode == "Current Density":
                    val = i_abs
                else:  # Power Density
                    val = i_abs * i_abs * r_ohm
                c = self._shade(lut, val, vmin, vmax)
                c1 = c2 = c

            # Perpendicular unit vector for bar width.
            ddx, ddy = x2 - x1, y2 - y1
            length = math.sqrt(ddx * ddx + ddy * ddy)
            if length < 1e-6:
                continue
            nx, ny = -ddy / length, ddx / length

            # Outer black box corners (at hw_out).
            a_ox, a_oy = x1 + nx * hw_out, y1 + ny * hw_out  # P end, +side
            b_ox, b_oy = x1 - nx * hw_out, y1 - ny * hw_out  # P end, -side
            c_ox, c_oy = x2 - nx * hw_out, y2 - ny * hw_out  # N end, -side
            d_ox, d_oy = x2 + nx * hw_out, y2 + ny * hw_out  # N end, +side

            # Helper: horizontal quad (2 tris) at a constant z, colored
            # gradient from ca (P end) to cb (N end).
            def _hquad(hw_: float, z_: float, ca, cb) -> tuple:
                ax_, ay_ = x1 + nx * hw_, y1 + ny * hw_
                bx_, by_ = x1 - nx * hw_, y1 - ny * hw_
                cx__, cy__ = x2 - nx * hw_, y2 - ny * hw_
                dx__, dy__ = x2 + nx * hw_, y2 + ny * hw_
                pos_ = np.array([
                    [ax_, ay_, z_], [bx_, by_, z_], [cx__, cy__, z_],
                    [ax_, ay_, z_], [cx__, cy__, z_], [dx__, dy__, z_],
                ], dtype=np.float32)
                col_ = np.array([ca, ca, cb, ca, cb, cb], dtype=np.float32)
                return pos_, col_

            # Helper: vertical quad (2 tris) connecting edge (p1→p2) from
            # z_bot to z_top, colored ca at p1 end and cb at p2 end.
            def _vquad(p1x, p1y, p2x, p2y, z_bot, z_top, ca, cb) -> tuple:
                pos_ = np.array([
                    [p1x, p1y, z_bot], [p2x, p2y, z_bot], [p2x, p2y, z_top],
                    [p1x, p1y, z_bot], [p2x, p2y, z_top], [p1x, p1y, z_top],
                ], dtype=np.float32)
                col_ = np.array([ca, cb, cb, ca, cb, ca], dtype=np.float32)
                return pos_, col_

            # Helper: two-strip black outline frame around a coloured cap,
            # at the cap's own z. Sitting at the same z guarantees the
            # outline reads next to the cap regardless of how the box's
            # far black face depth-resolves on the viewer's GPU.
            def _hframe(z_: float) -> tuple:
                # +perp strip: from hw to hw_out on the +nx,+ny side
                ai_x, ai_y = x1 + nx * hw, y1 + ny * hw
                ao_x, ao_y = x1 + nx * hw_out, y1 + ny * hw_out
                di_x, di_y = x2 + nx * hw, y2 + ny * hw
                do_x, do_y = x2 + nx * hw_out, y2 + ny * hw_out
                # -perp strip
                bi_x, bi_y = x1 - nx * hw, y1 - ny * hw
                bo_x, bo_y = x1 - nx * hw_out, y1 - ny * hw_out
                ci_x, ci_y = x2 - nx * hw, y2 - ny * hw
                co_x, co_y = x2 - nx * hw_out, y2 - ny * hw_out
                pos_ = np.array([
                    # +perp strip: ai, ao, do | ai, do, di
                    [ai_x, ai_y, z_], [ao_x, ao_y, z_], [do_x, do_y, z_],
                    [ai_x, ai_y, z_], [do_x, do_y, z_], [di_x, di_y, z_],
                    # -perp strip: bi, ci, co | bi, co, bo
                    [bi_x, bi_y, z_], [ci_x, ci_y, z_], [co_x, co_y, z_],
                    [bi_x, bi_y, z_], [co_x, co_y, z_], [bo_x, bo_y, z_],
                ], dtype=np.float32)
                col_ = np.tile(np.array(black, dtype=np.float32), (12, 1))
                return pos_, col_

            height = self._SERIES_BAR_HEIGHT_MM
            if in_3d and height > 0.0:
                # 3D extruded box with colored caps on both ends so the
                # bar reads the same from above and below. Each cap is
                # paired with a coplanar black outline frame at the same
                # z, so the outline is anchored to the cap and can't be
                # lost when the far black face is occluded by copper or
                # falls outside depth-buffer precision.
                z_top = z + extrude_sign * height
                z_cap = z_top + extrude_sign * 5e-4  # outer cap, away from board
                z_cap_inner = z - extrude_sign * 5e-4  # inner cap, board side

                # Black box walls (4 sides). No top/base face needed —
                # the cap-coplanar frames close the silhouette.
                p, c = _vquad(a_ox, a_oy, d_ox, d_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _vquad(b_ox, b_oy, c_ox, c_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _vquad(a_ox, a_oy, b_ox, b_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _vquad(d_ox, d_oy, c_ox, c_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)

                # Colored heatmap caps + coplanar black outline frames.
                p, c = _hquad(hw, z_cap, c1, c2)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hframe(z_cap)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hquad(hw, z_cap_inner, c1, c2)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hframe(z_cap_inner)
                pos_chunks.append(p); col_chunks.append(c)
            else:
                # 2D flat (or height == 0): black outline rect + colored rect.
                z_fg = z + 5e-4
                p, c = _hquad(hw_out, z, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hquad(hw, z_fg, c1, c2)
                pos_chunks.append(p); col_chunks.append(c)

        if not under_pos_chunks and not over_pos_chunks:
            self._gl_viewer.clear_series_bars()
            return
        all_pos = under_pos_chunks + over_pos_chunks
        all_col = under_col_chunks + over_col_chunks
        under_count = sum(c.shape[0] for c in under_pos_chunks)
        self._gl_viewer.set_series_bars(
            np.concatenate(all_pos, axis=0),
            np.concatenate(all_col, axis=0),
            under_mesh_count=under_count,
        )

    @staticmethod
    def _lut_lookup(lut: np.ndarray, value: float,
                    vmin: float, vmax: float) -> np.ndarray:
        """Look up an RGB triplet (float32, [0..1]) from the LUT for
        ``value`` normalised into ``[vmin, vmax]``."""
        if not np.isfinite(value):
            return np.asarray((0.5, 0.5, 0.5), dtype=np.float32)
        t = (value - vmin) / (vmax - vmin)
        t = max(0.0, min(1.0, t))
        idx = int(round(t * (lut.shape[0] - 1)))
        rgba = lut[idx]
        # LUT entries are uint8 RGBA; convert to float RGB.
        return np.asarray(rgba[:3], dtype=np.float32) / 255.0

    def _triangulate_stub(self, stub: dict) -> np.ndarray:
        """Triangulate a stub polygon, caching the result on the metadata dict.

        Returns an (N*3, 2) float32 array of vertex coords; consecutive
        triples form one GL_TRIANGLES triangle.

        Fast path: ``altium_loader._build_stub_record`` pre-triangulates
        every stub at solve time and ships the result in the pickle as
        ``stub['triangles_xy']`` — already in the (N*3, 2) float32 layout
        the GL stub batch wants, so the viewer just uploads it. Old
        pickles (or stubs where the loader's triangle call failed) fall
        through to the lazy Triangle-library path below.

        Lazy path: Shewchuk's Triangle (via the ``triangle`` PyPI
        package) does a constrained Delaunay of the exterior + interior
        rings with one hole marker per interior — fills non-convex shapes
        (including those with holes) exactly. The earlier
        ``shapely.ops.triangulate`` + ``poly.contains(centroid)``
        approach was a vertex-set Delaunay bounded by the convex hull:
        it left gaps in concavities and produced wrong fills for complex
        stub shapes (e.g. the expansion-board copper island with cutouts).
        """
        cached = stub.get("_tris_cache")
        if cached is not None:
            return cached
        prebuilt = stub.get("triangles_xy")
        if prebuilt is not None:
            arr = np.asarray(prebuilt, dtype=np.float32)
            stub["_tris_cache"] = arr
            return arr
        from shapely.geometry import Polygon as _Polygon
        ext = stub.get("exterior")
        if ext is None or (hasattr(ext, "size") and ext.size == 0) or (
                not hasattr(ext, "size") and not ext):
            exterior = []
        else:
            # Accept either a numpy (N, 2) array (new format) or a nested
            # list (legacy format) — shapely's Polygon takes both.
            exterior = ext
        holes = stub.get("holes") or []
        try:
            poly = _Polygon(exterior, holes=holes) if len(exterior) >= 3 else None
        except Exception:
            poly = None
        if poly is None or poly.is_empty:
            empty = np.empty((0, 2), dtype=np.float32)
            stub["_tris_cache"] = empty
            return empty

        # Shewchuk's Triangle (the C core behind the ``triangle`` package)
        # aborts the whole process — uncatchably, with no Python traceback
        # — on a degenerate PSLG: self-touching rings, zero-length edges or
        # duplicate vertices. ``all_copper`` rings are stored as float32,
        # which on a dense real-board copper pour routinely rounds
        # near-coincident vertices onto each other. Repair the polygon to
        # strict OGC validity before meshing it. This is exactly why
        # solid-fill "show all copper" crashed where wire-mesh — a
        # pure-numpy path that never touches Triangle — did not.
        if not poly.is_valid:
            try:
                repaired = poly.buffer(0)
            except Exception:
                repaired = None
            if repaired is not None and not repaired.is_empty:
                poly = repaired

        # buffer(0) can split an invalid polygon into several pieces; mesh
        # each component polygon on its own PSLG and concatenate (a single
        # shared PSLG would let Triangle fill the gaps between them).
        if poly.geom_type == "Polygon":
            parts = [poly]
        elif hasattr(poly, "geoms"):
            parts = [g for g in poly.geoms if g.geom_type == "Polygon"]
        else:
            parts = []
        chunks: list[np.ndarray] = []
        for part in parts:
            if part.is_empty:
                continue
            piece = self._triangulate_simple_polygon(part)
            if piece.size:
                chunks.append(piece)
        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        stub["_tris_cache"] = arr
        return arr

    def _merged_solid_all_copper_tris(
        self, layer_name: str, recs: list[dict],
        rail_members: set[str],
    ) -> np.ndarray | None:
        """Pre-merge a layer's all-copper polygons via shapely's
        :func:`unary_union` and triangulate the result, so a transparent
        solid fill blends each pixel exactly once.

        The source data sometimes carries overlapping polygons for the
        same layer (a GND plane plus smaller copper objects nominally
        sitting in its clearances, plus pad / via-clearance fills) — each
        overlap adds another blend pass at the layer's transparency
        alpha, cumulatively pushing the area to opacity even when the per-
        vertex alpha is low. Merging into one non-overlapping triangulation
        bypasses that. Cached per ``(layer_name, frozenset(rail_members))``
        — the metadata is fixed for the viewer's lifetime, so no other
        invalidation is needed.

        Returns ``None`` when the layer has no non-rail-member geometry.
        Wire-mesh fill draws thin outlines that don't cumulate noticeably,
        so the caller only routes through this for solid fill with
        ``layer_alpha < 1.0``.
        """
        key = (layer_name, frozenset(rail_members))
        cached = self._merged_all_copper_cache.get(key)
        if cached is not None:
            return cached if cached.size > 0 else None

        from shapely.geometry import Polygon as _Polygon
        from shapely.ops import unary_union

        polys = []
        for rec in recs:
            net = rec.get("net")
            if net in rail_members:
                continue
            for poly_dict in rec.get("polygons", []):
                ext = poly_dict.get("exterior")
                if ext is None:
                    continue
                if hasattr(ext, "size"):
                    if ext.size == 0:
                        continue
                elif not ext:
                    continue
                holes = poly_dict.get("holes") or []
                try:
                    p = _Polygon(ext, holes=holes)
                except Exception:
                    continue
                if p.is_empty:
                    continue
                if not p.is_valid:
                    try:
                        from shapely.validation import make_valid
                        p = make_valid(p)
                    except Exception:
                        continue
                    if p.is_empty:
                        continue
                polys.append(p)

        if not polys:
            self._merged_all_copper_cache[key] = np.empty(
                (0, 2), dtype=np.float32)
            return None

        try:
            merged = unary_union(polys)
        except Exception:
            merged = None

        if merged is None or merged.is_empty:
            self._merged_all_copper_cache[key] = np.empty(
                (0, 2), dtype=np.float32)
            return None

        # Strip the union down to its Polygon components — a
        # GeometryCollection may carry lower-dim leftovers we don't want
        # to triangulate.
        if merged.geom_type == "Polygon":
            components = [merged]
        elif merged.geom_type == "MultiPolygon":
            components = list(merged.geoms)
        else:
            components = [g for g in getattr(merged, "geoms", [])
                          if g.geom_type == "Polygon"]

        chunks: list[np.ndarray] = []
        for poly in components:
            if poly.is_empty:
                continue
            tris = self._triangulate_simple_polygon(poly)
            if tris.size > 0:
                chunks.append(tris)

        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        self._merged_all_copper_cache[key] = arr
        return arr if arr.size > 0 else None

    def _triangulate_simple_polygon(self, poly) -> np.ndarray:
        """Constrained-Delaunay triangulate one OGC-valid shapely Polygon
        into a flat ``(N*3, 2)`` float32 GL_TRIANGLES vertex soup.

        Returns an empty array if Triangle declines the polygon. The
        caller must hand in geometry that is already valid — see
        :meth:`_triangulate_stub` for the validity repair.
        """
        verts, segs, hole_markers = self._poly_to_triangle_input(poly)
        if not verts or not segs:
            return np.empty((0, 2), dtype=np.float32)
        try:
            import triangle as _triangle
            tri_input: dict = {"vertices": verts, "segments": segs}
            if hole_markers:
                tri_input["holes"] = hole_markers
            # ``p`` = constrained planar straight-line graph triangulation;
            # ``Q`` silences Triangle's stdout. No ``q`` / ``a`` quality
            # switches — we just want a geometrically faithful fill, not
            # a FEM-quality mesh.
            out = _triangle.triangulate(tri_input, "pQ")
        except Exception:
            out = None

        out_verts = out.get("vertices") if out else None
        out_tris = out.get("triangles") if out else None
        if out_verts is None or out_tris is None or len(out_tris) == 0:
            return np.empty((0, 2), dtype=np.float32)
        v_arr = np.asarray(out_verts, dtype=np.float32)
        t_arr = np.asarray(out_tris, dtype=np.int32)
        # Expand the index list into a flat (N*3, 2) GL_TRIANGLES vertex
        # soup — matches the buffer layout the GL stub/overlay batch wants.
        return v_arr[t_arr.ravel()].astype(np.float32, copy=False)

    @staticmethod
    def _poly_to_triangle_input(poly) -> tuple[
        list[tuple[float, float]],
        list[tuple[int, int]],
        list[tuple[float, float]],
    ]:
        """Convert a shapely Polygon into Triangle-library input.

        Returns ``(vertices, segments, hole_markers)``. Each ring (the
        exterior + every interior) contributes its vertices once (the
        duplicated closing vertex is dropped) and a closed loop of
        segment indices. Hole markers are representative points inside
        each interior ring, which tells Triangle to leave the hole
        region un-meshed. Shared with the stub-outline builder so the
        outline and the triangulation see exactly the same geometry.
        """
        from shapely.geometry import Polygon as _Polygon
        verts: list[tuple[float, float]] = []
        segs: list[tuple[int, int]] = []
        hole_markers: list[tuple[float, float]] = []

        # Float32-rounded pour rings can carry coincident vertices that
        # survive as zero-length PSLG edges; Triangle's C core aborts on
        # those, so merge anything closer than this (0.1 µm — far below
        # the finest real copper feature).
        eps = 1e-4

        def _add_ring(ring) -> None:
            if ring is None or ring.is_empty:
                return
            coords = list(ring.coords)
            if len(coords) >= 2 and coords[0] == coords[-1]:
                coords = coords[:-1]
            # Drop consecutive coincident vertices (and the wrap-around
            # duplicate) so every PSLG segment has non-zero length.
            cleaned: list[tuple[float, float]] = []
            for x, y in coords:
                fx, fy = float(x), float(y)
                if cleaned and abs(fx - cleaned[-1][0]) <= eps \
                        and abs(fy - cleaned[-1][1]) <= eps:
                    continue
                cleaned.append((fx, fy))
            while len(cleaned) >= 2 \
                    and abs(cleaned[0][0] - cleaned[-1][0]) <= eps \
                    and abs(cleaned[0][1] - cleaned[-1][1]) <= eps:
                cleaned.pop()
            if len(cleaned) < 3:
                return
            i_first = len(verts)
            verts.extend(cleaned)
            n = len(cleaned)
            for i in range(n):
                segs.append((i_first + i, i_first + (i + 1) % n))

        _add_ring(poly.exterior)
        for hole_ring in poly.interiors:
            _add_ring(hole_ring)
            try:
                hp = _Polygon(hole_ring).representative_point()
                hole_markers.append((float(hp.x), float(hp.y)))
            except Exception:
                continue

        # Weld globally-coincident vertices. The per-ring pass above only
        # drops *consecutive* duplicates; float32-stored pour rings also
        # round genuinely-distinct, non-adjacent vertices onto the exact
        # same coordinate (within and across rings). Shewchuk's Triangle
        # segfaults — silently, no traceback, taking the GUI with it — the
        # instant its vertex list holds a duplicate or any non-finite
        # coordinate, so collapse exact duplicates and bail on NaN/Inf
        # before the PSLG ever reaches the C core.
        if verts:
            welded: list[tuple[float, float]] = []
            index_of: dict[tuple[float, float], int] = {}
            remap: list[int] = []
            for vx, vy in verts:
                if not (math.isfinite(vx) and math.isfinite(vy)):
                    return [], [], []   # Triangle would segfault on NaN/Inf
                key = (vx, vy)
                idx = index_of.get(key)
                if idx is None:
                    idx = len(welded)
                    index_of[key] = idx
                    welded.append(key)
                remap.append(idx)
            clean_segs: list[tuple[int, int]] = []
            seen_segs: set[tuple[int, int]] = set()
            for a, b in segs:
                ra, rb = remap[a], remap[b]
                if ra == rb:
                    continue                       # collapsed to zero length
                ordered = (ra, rb) if ra < rb else (rb, ra)
                if ordered in seen_segs:
                    continue                       # duplicate constraint edge
                seen_segs.add(ordered)
                clean_segs.append((ra, rb))
            verts, segs = welded, clean_segs
        return verts, segs, hole_markers

    def _stub_outline_segments(self, stub: dict) -> np.ndarray:
        """Build (and cache) GL_LINES outline segments for a stub piece.

        Returns an ``(N, 2)`` float32 array where consecutive vertex
        pairs form one segment. Traces the exterior + every hole; result
        is cached on the stub dict so toggling the outline overlay is a
        pure buffer upload.

        Accepts both the new pickle format (numpy ``(N, 2)`` float32 arrays
        for exterior + each hole) and the legacy format (nested Python
        lists). Both go through ``np.asarray`` so the downstream code is
        identical.
        """
        cached = stub.get("_outline_cache")
        if cached is not None:
            return cached
        exterior = stub.get("exterior")
        if exterior is None:
            exterior_arr = np.empty((0, 2), dtype=np.float32)
        else:
            exterior_arr = np.asarray(exterior, dtype=np.float32)
        holes = stub.get("holes") or []
        rings: list[np.ndarray] = []
        if exterior_arr.shape[0] >= 3:
            rings.append(exterior_arr)
        for hole in holes:
            hole_arr = np.asarray(hole, dtype=np.float32)
            if hole_arr.shape[0] >= 3:
                rings.append(hole_arr)
        pairs_chunks: list[np.ndarray] = []
        for ring in rings:
            # Close the ring if the source data didn't (extraction stores
            # exterior/holes as open rings — first vertex != last).
            if not np.allclose(ring[0], ring[-1]):
                ring = np.vstack([ring, ring[:1]])
            if ring.shape[0] < 2:
                continue
            pairs = np.empty((2 * (ring.shape[0] - 1), 2), dtype=np.float32)
            pairs[0::2] = ring[:-1]
            pairs[1::2] = ring[1:]
            pairs_chunks.append(pairs)
        if not pairs_chunks:
            arr = np.empty((0, 2), dtype=np.float32)
        else:
            arr = np.concatenate(pairs_chunks, axis=0)
        stub["_outline_cache"] = arr
        return arr

    def _ensure_gl_cmap(self, kind: str) -> None:
        """Make sure the LUT uploaded to the GL viewer's copper-mesh
        cmap texture matches ``kind``.

        ``kind`` is ``"data"`` (the viridis ramp keyed on per-vertex
        values — every mode except Via Current) or ``"neutral"`` (a
        flat-grey LUT used in Via Current mode so the copper renders
        as context behind the heatmapped vias). The viewer caches the
        active kind so this is a no-op when the LUT hasn't changed.
        """
        if self._gl_cmap_kind == kind:
            return
        if kind == "neutral":
            self._gl_viewer.set_colormap(_build_neutral_cmap_lut())
        else:
            self._gl_viewer.set_colormap(_build_cmap_lut(self._cmap_name))
        self._gl_cmap_kind = kind

    def _gl_scale(self, v):
        """Map real heatmap value(s) into the space the colour lookup
        runs in: identity on a linear scale, ``log10`` (floored at
        ``_log_floor``) on a log scale. Accepts a scalar or an ndarray
        and never mutates its input.

        Pushing both the per-vertex values and the level clamps through
        this keeps the GL viewer's linear normalisation shader correct
        for the log scale with no shader change — see :meth:`_render`."""
        if not self._log_active:
            return v
        return np.log10(np.maximum(v, self._log_floor))

    def _shade(self, lut: np.ndarray, value: float,
               vmin: float, vmax: float) -> tuple[float, float, float]:
        """LUT colour for one scalar value, honouring the active linear /
        log scale. Wraps :func:`_sample_cmap_lut` so the CPU-baked
        overlays (via cylinders, series bars, 2D via markers) pick up the
        log scale the same way the GPU-shaded copper mesh does."""
        return _sample_cmap_lut(
            lut, self._gl_scale(value),
            self._gl_scale(vmin), self._gl_scale(vmax))

    def _on_scale_type_changed(self, is_log: bool) -> None:
        """The Linear / Logarithmic dropdown changed — re-render so the
        copper mesh, the via overlays and the gradient strip all switch
        scale together. ``_render`` re-derives ``_log_active`` (the
        dropdown is disabled for ineligible modes, but guard anyway)."""
        if is_log == self._log_scale:
            return
        self._log_scale = is_log
        self._render()

    def _via_current_lookup_and_range(
        self, rail_names: list[str],
    ) -> tuple[float, float, dict[tuple[str, float, float], float]]:
        """For Via Current mode: return ``(vmin, vmax, lookup)``.

        ``lookup`` maps ``(net, x_mm, y_mm)`` to the via's
        max-|segment-current| (matches the Vias-tab ``current`` column)
        for every via whose net is in the effective rail set. The
        range is taken across that whole set — independent of which
        physical layers are toggled visible — so flipping layers
        doesn't move the colormap. Falls back to ``(0.0, 1.0)`` when
        no via matches the rail filter.
        """
        rail_members = set(self._effective_rail_members(rail_names))
        rows = self._get_or_compute_via_rows()
        lookup: dict[tuple[str, float, float], float] = {}
        for r in rows:
            net = r.get("net", "")
            if rail_members and net not in rail_members:
                continue
            cur = r.get("current")
            if cur is None:
                continue
            cur_f = float(cur)
            if not math.isfinite(cur_f):
                continue
            key = (net,
                   float(r.get("x_mm", 0.0)),
                   float(r.get("y_mm", 0.0)))
            lookup[key] = cur_f
        if not lookup:
            return 0.0, 1.0, lookup
        vals = list(lookup.values())
        vmin = min(vals)
        vmax = max(vals)
        if vmax <= vmin:
            vmax = vmin + 1e-12
        return vmin, vmax, lookup

    # --- Via cylinders (3D-mode only) --------------------------------------

    # Cylinder tessellation — 10 sides keeps the silhouette smooth at
    # typical zoom without flooding the GPU when there are 100+ vias.
    _VIA_CYL_SEGMENTS: int = 10
    # Minimum world-mm radius for vias whose ``diameter_mm`` is missing
    # or oddly small (we still want them to be visible cylinders).
    _VIA_CYL_MIN_RADIUS_MM: float = 0.2
    # RGB colour of the via cylinders (matches the 2D orange via marker).
    _VIA_CYL_COLOR_RGB: tuple[float, float, float] = (
        0xff / 255.0, 0x8c / 255.0, 0x00 / 255.0,
    )
    # Plated-through-hole pad styling — light grey so PTHs are visually
    # distinct from the orange vias in both the 2D marker overlay and the
    # 3D cylinder view. PTHs span every enabled copper layer (Altium pads
    # have no blind/buried span) so their cylinders run the full stack.
    _PTH_COLOR_HEX: str = "#b8b8b8"
    _PTH_CYL_COLOR_RGB: tuple[float, float, float] = (
        0xb8 / 255.0, 0xb8 / 255.0, 0xb8 / 255.0,
    )

    def _push_via_cylinders(self, phys_list: list[str],
                            rail_names: list[str] | str,
                            *, mode: str | None = None) -> None:
        """Build cylinder triangle geometry for every via that touches
        the visible physical layers + the selected rail groups, and push
        it as one batch to the GLMeshViewer. Empty input clears the
        cylinders.

        When the "Heatmap vias" toggle is on, each via is split into one
        cylinder per inter-layer segment, coloured by the active mode's
        heatmap (voltage / drop interpolate top↔bottom along the via;
        current and power are constant per segment). Otherwise vias are
        drawn solid orange to match the 2D marker."""
        if self.metadata is None or not phys_list:
            self._gl_viewer.clear_cylinders()
            return
        # Map layer_id → physical name → rank → z. Use the same rank-
        # based z as the heatmap meshes so cylinders connect cleanly.
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        visible_ids = {self._phys_name_to_layer_id.get(p)
                       for p in phys_list}
        visible_ids.discard(None)
        rail_members = set(self._effective_rail_members(rail_names))

        # Via Current mode supersedes the "Heatmap vias" toggle — the
        # mode IS the heatmap, so we always colour every visible via /
        # PTH by its max-|segment-current| (looked up from
        # :attr:`_via_current_lookup`, populated in :meth:`_render`).
        via_current_on = (mode == _VIA_CURRENT_MODE)
        heatmap_on = via_current_on or (
            mode is not None
            and getattr(self, "heatmap_vias_box", None) is not None
            and self.heatmap_vias_box.isChecked()
        )
        lut: np.ndarray | None = None
        vmin = vmax = 0.0
        fallback_r_seg = 1.0e-3
        drop_ref = 0.0
        stackup_ids: list[int] = []
        if heatmap_on:
            lut = _build_cmap_lut(self._cmap_name)
            vmin = float(self._vmin)
            vmax = float(self._vmax)
            if vmax <= vmin:
                vmax = vmin + 1e-30
            # Each via dict now carries its own segments list with per-hop R;
            # this fallback only applies if that's missing (e.g. legacy pickle).
            fallback_r_seg = float(
                (self.metadata.get("physics_constants", {}) or {})
                .get("fallback_via_resistance_ohm", 1.0e-3)
            )
            if (mode == _VOLTAGE_DROP_MODE
                    and self._last_drop_reference is not None):
                drop_ref = float(self._last_drop_reference)
            stackup_ids = [row["layer_id"]
                           for row in self.metadata.get("stackup", [])]

        pos_chunks: list[np.ndarray] = []
        col_chunks: list[np.ndarray] = []
        for v in self.metadata.get("vias", []):
            ls_id = v.get("layer_start")
            le_id = v.get("layer_end")
            if ls_id is None or le_id is None:
                continue
            # Skip vias that don't touch any visible layer.
            lo_id = min(ls_id, le_id)
            hi_id = max(ls_id, le_id)
            spanned = [lid for lid in (lo_id, hi_id) if lid in id_to_phys]
            if not spanned:
                continue
            if not any(visible_ids & set(spanned)):
                continue
            # Skip vias not on the rail (when a rail is selected).
            if rail_members and v.get("net") not in rail_members:
                continue
            phys_top = id_to_phys.get(lo_id)
            phys_bot = id_to_phys.get(hi_id)
            if phys_top is None or phys_bot is None:
                continue
            z_top = self._layer_z_for(phys_top)
            z_bot = self._layer_z_for(phys_bot)
            if z_top == z_bot:
                continue
            x = float(v.get("x_mm", 0.0))
            y = float(v.get("y_mm", 0.0))
            # Draw at drill diameter — that's the actual barrel that carries
            # current between layers (and what the FEM uses for R). The
            # outer pad diameter belongs to the per-layer copper plane,
            # which is already drawn as part of the layer mesh. Fall back
            # to pad diameter if drill data is missing.
            drill_mm = float(v.get("hole_diameter_mm") or 0.0)
            outer_mm = float(v.get("diameter_mm") or 0.0)
            radius = (drill_mm if drill_mm > 0.0 else outer_mm) * 0.5
            radius = max(radius, self._VIA_CYL_MIN_RADIUS_MM)

            # Heatmap path: per-segment cylinders coloured by mode. Falls
            # back to the solid-orange branch below when we can't sample
            # voltages on at least two of the via's spanned layers.
            heatmap_chunks: list[tuple[np.ndarray, np.ndarray]] = []
            if via_current_on and lut is not None:
                # Via Current mode: one cylinder per via, coloured by
                # the via's max-|segment-current| (matches the Vias-tab
                # value and the scale-controller range).
                key = (v.get("net", ""), x, y)
                cur = self._via_current_lookup.get(key)
                if cur is not None:
                    c = self._shade(lut, cur, vmin, vmax)
                    pos, col = _generate_via_cylinder(
                        x, y, z_top, z_bot, radius, c,
                        n_segments=self._VIA_CYL_SEGMENTS,
                    )
                    heatmap_chunks = [(pos, col)]
            elif heatmap_on and lut is not None and mode is not None:
                heatmap_chunks = self._heatmap_via_chunks(
                    v, x, y, radius, lo_id, hi_id,
                    stackup_ids, id_to_phys,
                    mode, lut, vmin, vmax, fallback_r_seg, drop_ref,
                )

            if heatmap_chunks:
                for pos, col in heatmap_chunks:
                    pos_chunks.append(pos)
                    col_chunks.append(col)
            else:
                pos, col = _generate_via_cylinder(
                    x, y, z_top, z_bot, radius,
                    self._VIA_CYL_COLOR_RGB,
                    n_segments=self._VIA_CYL_SEGMENTS,
                )
                pos_chunks.append(pos)
                col_chunks.append(col)

        # Plated through-hole pads — same per-site gating as vias (visible
        # layer ∩ rail). Default to light grey; when the heatmap-vias/PTH
        # toggle is on they're coloured the same way as via cylinders via
        # the shared :meth:`_heatmap_via_chunks` helper (PTH dicts carry
        # the same ``net`` + ``segments`` keys vias do).
        for p in self.metadata.get("pths", []):
            ls_id = p.get("layer_start")
            le_id = p.get("layer_end")
            if ls_id is None or le_id is None:
                continue
            lo_id = min(ls_id, le_id)
            hi_id = max(ls_id, le_id)
            spanned = [lid for lid in (lo_id, hi_id) if lid in id_to_phys]
            if not spanned:
                continue
            if not any(visible_ids & set(spanned)):
                continue
            if rail_members and p.get("net") not in rail_members:
                continue
            phys_top = id_to_phys.get(lo_id)
            phys_bot = id_to_phys.get(hi_id)
            if phys_top is None or phys_bot is None:
                continue
            z_top = self._layer_z_for(phys_top)
            z_bot = self._layer_z_for(phys_bot)
            if z_top == z_bot:
                continue
            x = float(p.get("x_mm", 0.0))
            y = float(p.get("y_mm", 0.0))
            # Drill diameter, same reasoning as the via path above.
            drill_mm = float(p.get("hole_diameter_mm") or 0.0)
            outer_mm = float(p.get("diameter_mm") or 0.0)
            radius = (drill_mm if drill_mm > 0.0 else outer_mm) * 0.5
            radius = max(radius, self._VIA_CYL_MIN_RADIUS_MM)

            heatmap_chunks: list[tuple[np.ndarray, np.ndarray]] = []
            if via_current_on:
                # Via Current mode: PTH pads aren't part of the cached
                # Vias-tab report (it iterates ``metadata['vias']``
                # only), so they have no entry in
                # ``_via_current_lookup``. Render them as the default
                # light-grey PTH colour to keep them visible without
                # implying a current reading we don't have.
                pass
            elif heatmap_on and lut is not None and mode is not None:
                heatmap_chunks = self._heatmap_via_chunks(
                    p, x, y, radius, lo_id, hi_id,
                    stackup_ids, id_to_phys,
                    mode, lut, vmin, vmax, fallback_r_seg, drop_ref,
                )

            if heatmap_chunks:
                for pos, col in heatmap_chunks:
                    pos_chunks.append(pos)
                    col_chunks.append(col)
            else:
                pos, col = _generate_via_cylinder(
                    x, y, z_top, z_bot, radius,
                    self._PTH_CYL_COLOR_RGB,
                    n_segments=self._VIA_CYL_SEGMENTS,
                )
                pos_chunks.append(pos)
                col_chunks.append(col)

        if pos_chunks:
            self._gl_viewer.set_cylinders(
                np.concatenate(pos_chunks, axis=0),
                np.concatenate(col_chunks, axis=0),
            )
        else:
            self._gl_viewer.clear_cylinders()

    def _heatmap_via_chunks(
        self, via: dict, x: float, y: float, radius: float,
        lo_id: int, hi_id: int,
        stackup_ids: list[int], id_to_phys: dict[int, str],
        mode: str, lut: np.ndarray, vmin: float, vmax: float,
        fallback_r_seg: float, drop_ref: float,
    ) -> list[tuple[np.ndarray, np.ndarray]]:
        """Per-segment cylinder geometry for one via, coloured by mode.

        Voltage / Voltage Drop produce a smooth top↔bottom gradient on
        each segment; Current Density / Power Density produce a single
        colour per segment (the value is constant along the resistor).
        Returns an empty list when the via has fewer than two sampleable
        layers — caller falls back to the solid-orange path.
        """
        net = via.get("net", "")
        if not net or net in ("?", "NO_NET"):
            return []
        try:
            i_start = stackup_ids.index(lo_id)
            i_end = stackup_ids.index(hi_id)
        except ValueError:
            return []
        span_ids = stackup_ids[i_start:i_end + 1]
        if len(span_ids) < 2:
            return []
        # (layer_id, z, raw_voltage) for every layer in the span where copper
        # for this net exists. Layers without a sampleable voltage are dropped
        # — the resulting segment list matches the FEM coupling network.
        sampled: list[tuple[int, float, float]] = []
        for lid in span_ids:
            phys = id_to_phys.get(lid)
            if phys is None:
                continue
            v_at = self._sample_via_voltage(phys, net, x, y)
            if v_at is None:
                continue
            sampled.append((lid, self._layer_z_for(phys), v_at))
        if len(sampled) < 2:
            # Only one layer was sampleable. For Voltage / Voltage Drop we can
            # still colour the whole cylinder barrel with that one voltage —
            # via resistance is negligible so a solid shade is a good
            # approximation. Current / Power need at least two voltage samples
            # to compute a meaningful value, so those modes fall back.
            if len(sampled) == 1 and mode in ("Voltage", _VOLTAGE_DROP_MODE):
                phys_lo = id_to_phys.get(lo_id)
                phys_hi = id_to_phys.get(hi_id)
                if phys_lo is None or phys_hi is None:
                    return []
                z_lo = self._layer_z_for(phys_lo)
                z_hi = self._layer_z_for(phys_hi)
                if z_lo == z_hi:
                    return []
                val = sampled[0][2] - drop_ref
                c = self._shade(lut, val, vmin, vmax)
                pos, col = _generate_via_cylinder(
                    x, y, z_lo, z_hi, radius, c,
                    n_segments=self._VIA_CYL_SEGMENTS,
                )
                # Single cap at the one layer we could sample.
                _, z_cap, v_cap = sampled[0]
                c_cap = self._shade(lut, v_cap - drop_ref, vmin, vmax)
                cap_pos, cap_col = _generate_disk_cap(
                    x, y, z_cap, radius, c_cap,
                    n_segments=self._VIA_CYL_SEGMENTS,
                )
                return [(pos, col), (cap_pos, cap_col)]
            return []

        # Per-hop R lookup, keyed unordered so (a,b) and (b,a) both match.
        r_by_pair: dict[frozenset[int], float] = {}
        for seg in via.get("segments") or []:
            r_by_pair[frozenset((seg["layer_a"], seg["layer_b"]))] = (
                float(seg["resistance_ohm"])
            )

        chunks: list[tuple[np.ndarray, np.ndarray]] = []
        for (lid_a, z_a, v_a), (lid_b, z_b, v_b) in zip(sampled, sampled[1:]):
            if z_a == z_b:
                continue
            if mode in ("Voltage", _VOLTAGE_DROP_MODE):
                top_val = v_a - drop_ref
                bot_val = v_b - drop_ref
                ct = self._shade(lut, top_val, vmin, vmax)
                cb = self._shade(lut, bot_val, vmin, vmax)
                pos, col = _generate_via_cylinder_gradient(
                    x, y, z_a, z_b, radius, ct, cb,
                    n_segments=self._VIA_CYL_SEGMENTS,
                )
            else:
                # Current / power are constant along each inter-layer barrel
                # resistor — colour the whole segment one shade. R is the
                # actual per-hop value the FEM used (varies with drill,
                # plating, and dielectric thickness traversed).
                r_seg = r_by_pair.get(
                    frozenset((lid_a, lid_b)), fallback_r_seg,
                )
                i_seg = (v_a - v_b) / r_seg if r_seg > 0 else 0.0
                if mode == "Current Density":
                    val = abs(i_seg)
                else:  # Power Density (and any unexpected mode → power)
                    val = i_seg * i_seg * r_seg
                c = self._shade(lut, val, vmin, vmax)
                pos, col = _generate_via_cylinder(
                    x, y, z_a, z_b, radius, c,
                    n_segments=self._VIA_CYL_SEGMENTS,
                )
            chunks.append((pos, col))

        # For Voltage / Voltage Drop add a filled disk cap at the top and
        # bottom of the via so the endpoint colour is clearly visible at each
        # copper-layer junction. The cap colour matches the sampled voltage at
        # that layer, which is the same value the copper-mesh shader draws at
        # (x, y) — so any z-fighting with the copper surface is invisible
        # (both surfaces are the same colour). Caps are omitted for
        # Current / Power Density because those modes assign per-segment values
        # rather than per-layer endpoint values.
        if chunks and mode in ("Voltage", _VOLTAGE_DROP_MODE):
            _, z_top_cap, v_top_cap = sampled[0]
            _, z_bot_cap, v_bot_cap = sampled[-1]
            ct_cap = self._shade(lut, v_top_cap - drop_ref, vmin, vmax)
            cb_cap = self._shade(lut, v_bot_cap - drop_ref, vmin, vmax)
            pos_t, col_t = _generate_disk_cap(
                x, y, z_top_cap, radius, ct_cap,
                n_segments=self._VIA_CYL_SEGMENTS,
            )
            pos_b, col_b = _generate_disk_cap(
                x, y, z_bot_cap, radius, cb_cap,
                n_segments=self._VIA_CYL_SEGMENTS,
            )
            chunks = [(pos_t, col_t)] + chunks + [(pos_b, col_b)]

        return chunks

    def _via_voltage_kdtree(
        self, phys_name: str, net_name: str,
    ) -> tuple | None:
        """Lazily build (and cache for the viewer's lifetime) a nearest-
        vertex voltage lookup for the (physical layer, net) solution
        layer. Returns ``(cKDTree, potentials)`` — a kd-tree of mesh
        vertex (x, y) positions plus the matching per-vertex potentials —
        or ``None`` if no such layer exists / its mesh is empty.

        Padne adds via + directive-pin coupling sites to the Triangle
        mesher as Steiner points, so every point this samples (via
        centres, resistor pins) IS a mesh vertex — the nearest-vertex
        potential is then the exact mesh-side voltage there.

        This used to build a matplotlib ``LinearTriInterpolator``, whose
        implicit ``TrapezoidMapTriFinder`` build is O(seconds) on a large
        plane (e.g. GND) and froze the GUI for several seconds the first
        time a heavy rail was shown — the per-(phys, net) cache is why
        only the *first* toggle stalled. A ``cKDTree`` build is pure C and
        ~100x faster — the same swap the Vias / Nodes report tables
        already made (see :meth:`_compute_via_report`).

        Orphan vertices — those not referenced by any triangle, which the
        FEM pins to V=0 to keep the linear system non-singular — are
        excluded so an off-copper sample never snaps to a fake 0 V node.
        """
        key = (phys_name, net_name)
        if key in self._via_voltage_kdtree_cache:
            return self._via_voltage_kdtree_cache[key]
        li = self._index_by_pair.get(key)
        if li is None:
            self._via_voltage_kdtree_cache[key] = None
            return None
        ls = self.solution.layer_solutions[li]
        xs_parts: list[np.ndarray] = []
        ys_parts: list[np.ndarray] = []
        vs_parts: list[np.ndarray] = []
        for xys, tris_local, pot in zip(
            ls.vertex_xys, ls.triangles, ls.potentials,
        ):
            if xys.shape[0] == 0 or tris_local.size == 0:
                continue
            used = np.unique(tris_local.ravel())
            xs_parts.append(xys[used, 0])
            ys_parts.append(xys[used, 1])
            vs_parts.append(pot[used])
        if not xs_parts:
            self._via_voltage_kdtree_cache[key] = None
            return None
        from scipy.spatial import cKDTree
        pts = np.column_stack([
            np.concatenate(xs_parts), np.concatenate(ys_parts),
        ])
        entry = (cKDTree(pts), np.concatenate(vs_parts))
        self._via_voltage_kdtree_cache[key] = entry
        return entry

    def _sample_via_voltage(
        self, phys_name: str, net_name: str, x: float, y: float,
    ) -> float | None:
        """Sample the (phys, net) voltage at (x, y) via a nearest mesh-
        vertex lookup. ``None`` only if the (phys, net) layer doesn't
        exist or has no mesh.

        For via centres and directive pins the (x, y) IS a mesh vertex
        (a padne Steiner point), so the nearest vertex is an exact hit.
        For a point off the solved copper — a stub centroid, or a via in
        a clearance gap — the nearest mesh vertex on the same net is
        still the best estimate: the coupling node is always close by,
        and voltage is near-constant over copper carrying little current.
        """
        entry = self._via_voltage_kdtree(phys_name, net_name)
        if entry is None:
            return None
        tree, vs = entry
        _dist, idx = tree.query((x, y))
        f = float(vs[int(idx)])
        return f if np.isfinite(f) else None

    # Colour buckets used by the 2D Via Current marker overlay. 64 is
    # fine enough that adjacent buckets are visually indistinguishable
    # at typical zoom, while keeping the number of MarkerGroup draw
    # calls bounded no matter how many vias the board has.
    _VIA_CURRENT_BUCKETS: int = 128
    # Minimum pixel diameter for 2D Via Current markers. When the user
    # zooms out so far that a via's physical footprint would be
    # sub-pixel, the marker stays at this floor so vias never shrink
    # to invisible dots. At those zoom levels markers will overlap —
    # the user explicitly asked for this trade-off so the heatmap
    # remains readable.
    _VIA_MARKER_MIN_PX: float = 6.0
    # Fallback diameter (mm) for vias with no diameter / hole metadata.
    # 0.4 mm matches the conservative default used by the cylinder
    # path's ``_VIA_CYL_MIN_RADIUS_MM``.
    _VIA_MARKER_FALLBACK_DIAM_MM: float = 0.4

    def _build_via_current_marker_groups(
        self, target_layer_ids: set[int], rail_members: set[str],
    ) -> list[MarkerGroup]:
        """Build a list of MarkerGroups for the 2D Via Current overlay.

        Vias whose span includes a visible layer AND whose net is on
        the selected rails are looked up in :attr:`_via_current_lookup`
        for their max-|segment-current|, then bucketed into
        :data:`_VIA_CURRENT_BUCKETS` colour bins. One MarkerGroup per
        non-empty bucket gives a heatmap-coloured marker per via with
        a bounded number of draw calls. Each marker's drawn pixel
        diameter scales with the via's physical pad diameter (so
        zooming in reveals the real footprint), floored at
        :data:`_VIA_MARKER_MIN_PX` (so zooming out doesn't collapse
        vias into invisible dots).
        """
        if not self._via_current_lookup or self.metadata is None:
            return []
        lut = _build_cmap_lut(self._cmap_name)
        vmin = float(self._vmin)
        vmax = float(self._vmax)
        if vmax <= vmin:
            vmax = vmin + 1e-12
        n_buckets = max(2, int(self._VIA_CURRENT_BUCKETS))
        # bucket -> list of (x, y, diameter_mm)
        buckets: dict[int, list[tuple[float, float, float]]] = {}
        for v in self.metadata.get("vias", []):
            net = v.get("net", "")
            if rail_members and net not in rail_members:
                continue
            ls_id = v.get("layer_start")
            le_id = v.get("layer_end")
            if ls_id is None or le_id is None:
                continue
            lo, hi = (ls_id, le_id) if ls_id <= le_id else (le_id, ls_id)
            if not any(lo <= lid <= hi for lid in target_layer_ids):
                continue
            x = float(v.get("x_mm", 0.0))
            y = float(v.get("y_mm", 0.0))
            cur = self._via_current_lookup.get((net, x, y))
            if cur is None:
                continue
            t = (cur - vmin) / (vmax - vmin)
            t = max(0.0, min(1.0, t))
            bucket = int(round(t * (n_buckets - 1)))
            # Match the 3D cylinder convention: drill diameter is the
            # actual current-carrying barrel and the value the FEM
            # solved with, so the 2D marker tracks the same metric.
            # Pad diameter (the annular ring) varies more across via
            # classes — using it makes power vs signal vias look
            # noticeably different even when their drill bores are
            # similar. Fall back to pad diameter, then to a sane
            # default, if drill data is missing.
            drill = float(v.get("hole_diameter_mm") or 0.0)
            outer = float(v.get("diameter_mm") or 0.0)
            diameter_mm = drill if drill > 0.0 else (
                outer if outer > 0.0 else self._VIA_MARKER_FALLBACK_DIAM_MM
            )
            buckets.setdefault(bucket, []).append((x, y, diameter_mm))
        groups: list[MarkerGroup] = []
        lut_max = lut.shape[0] - 1
        bucket_div = max(1, n_buckets - 1)
        for bucket, items in buckets.items():
            lut_idx = int(round(bucket / bucket_div * lut_max))
            r, g, b = int(lut[lut_idx, 0]), int(lut[lut_idx, 1]), int(lut[lut_idx, 2])
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            n = len(items)
            xs_arr = np.fromiter((it[0] for it in items), dtype=np.float64,
                                  count=n)
            ys_arr = np.fromiter((it[1] for it in items), dtype=np.float64,
                                  count=n)
            diam_arr = np.fromiter((it[2] for it in items), dtype=np.float64,
                                    count=n)
            groups.append(MarkerGroup(
                xs=xs_arr,
                ys=ys_arr,
                color=hex_color,
                symbol="o",
                size=8,  # ignored when world_diameters_mm is set
                edge_color="#000000",
                edge_width=0.4,
                world_diameters_mm=diam_arr,
                min_pixel_diameter=self._VIA_MARKER_MIN_PX,
            ))
        return groups

    def _update_markers_and_legend(self, phys_list: list[str],
                                   rail_names: list[str] | str) -> None:
        """Build the marker overlay + legend for the current view and
        push both to the GL viewer.

        Role markers (SOURCE / SINK / SERIES / REGULATOR) and the orange
        via dots are gated behind the "Show pin markers" checkbox. The
        Vias-tab "Go" highlight (a yellow ring at
        :attr:`_highlight_via_xy`) is ALWAYS drawn regardless of that
        checkbox so the user can still find a via they just jumped to.

        Each legend row is also a per-category visibility toggle: clicking
        a row sets :attr:`_hidden_legend_keys` and the matching marker
        groups are skipped on the next render (the row stays in the
        chip, slashed).
        """
        groups: list[MarkerGroup] = []
        # Structured legend rows pushed to the GL viewer's top-right
        # chip. A row marked hidden suppresses its MarkerGroup above but
        # still appears in the chip (slashed) so the toggle is
        # discoverable + reversible.
        legend_rows: list[LegendRow] = []
        legend_seen_keys: set[str] = set()

        def _add_legend(key: str, glyph_symbol: str, color: str,
                        label: str | None = None) -> None:
            if key in legend_seen_keys:
                return
            legend_seen_keys.add(key)
            legend_rows.append(LegendRow(
                key=key,
                label=label or key,
                glyph=self._LEGEND_GLYPHS.get(glyph_symbol, "●"),
                color=color,
                hidden=key in self._hidden_legend_keys,
            ))

        # Hover-index for SOURCE/SINK markers is rebuilt from the same
        # pin walk below, so drop the stale cache up-front. ``hover_rows``
        # collects the solved-directive entries; editor-directive rows are
        # appended once the editor markers are built (see below).
        self._marker_hover_index_cache = None
        hover_rows: list[dict] = []

        show_role_markers = self.show_markers_box.isChecked()
        target_layer_ids: set[int] = set()
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                target_layer_ids.add(lid)

        # In 3D mode, each marker needs its layer's z so projection
        # through the MVP places it on the correct copper plane.
        in_3d = self.view_3d_box.isChecked()
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        mode = self.mode_combo.currentText()
        is_via_current = (mode == _VIA_CURRENT_MODE)

        if (show_role_markers and self.metadata is not None
                and phys_list and target_layer_ids):
            rail_members = set(self._effective_rail_members(rail_names))

            # Keyed (role, is_n_side): the P side and N side of a
            # directive draw as separate groups so the N side can carry
            # the green outline.
            per_role: dict[tuple[str, bool],
                           tuple[list[float], list[float],
                                 list[float]]] = {}
            # Designators whose schematic directive an editor directive
            # overrides — their hover rows are suppressed so the bottom
            # bar reports the (pending) editor value, not the stale one.
            overridden = self._overridden_designators()
            for d in self.metadata.get("directives", []):
                role = d.get("role")
                if role not in self._ROLE_MARKER_STYLE:
                    continue
                directive_overridden = d.get("designator") in overridden
                directive_current = self._directive_current_for_hover(d)
                directive_label = str(d.get("label") or d.get("designator")
                                      or "")
                for term_name, term in (d.get("terminals") or {}).items():
                    term_pins = term.get("pins") or []
                    is_n_side = term_name in _N_SIDE_TERMINALS
                    # Per-pin current = directive total / pins in THIS
                    # terminal. The lumped element couples to each pin in
                    # a terminal through equal-valued star resistors, so
                    # with similar copper potentials at the pins the
                    # current splits evenly. A terminal with no pins
                    # (multi-channel directives can have empty terminals)
                    # contributes no markers, so the divisor is never 0.
                    n_pins = len(term_pins)
                    if (directive_current is not None
                            and np.isfinite(directive_current)
                            and n_pins > 0):
                        per_pin_current: float | None = (
                            directive_current / n_pins
                        )
                    else:
                        per_pin_current = None
                    for pin in term_pins:
                        lid = pin.get("layer_id")
                        if lid not in target_layer_ids:
                            continue
                        # In editor mode N-side markers skip the rail
                        # filter so the return net (rarely the viewed
                        # rail) always shows; in normal viewing they
                        # follow the rail filter like the P side, so only
                        # markers on the selected rails appear.
                        rail_exempt = is_n_side and self._editor_mode
                        if (not rail_exempt and rail_members
                                and pin.get("net") not in rail_members):
                            continue
                        xs, ys, zs = per_role.setdefault(
                            (role, is_n_side), ([], [], []))
                        px = pin.get("x_mm", 0.0)
                        py = pin.get("y_mm", 0.0)
                        xs.append(px)
                        ys.append(py)
                        phys_for_pin = id_to_phys.get(lid)
                        zs.append(self._layer_z_for(phys_for_pin)
                                   if phys_for_pin else 0.0)
                        if role in ("SOURCE", "SINK") and not directive_overridden:
                            hover_rows.append({
                                "x_mm": float(px),
                                "y_mm": float(py),
                                "role": role,
                                "label": directive_label,
                                "terminal": term_name,
                                "net": pin.get("net", ""),
                                "physical": phys_for_pin or "",
                                "current_a": per_pin_current,
                                "directive_current_a": directive_current,
                                "terminal_pin_count": n_pins,
                                "size_px": int(
                                    self._ROLE_MARKER_STYLE[role]["size"]
                                ),
                            })

            for (role, is_n_side), (xs, ys, zs) in per_role.items():
                if not xs:
                    continue
                style = self._ROLE_MARKER_STYLE[role]
                role_key = style["label"]
                # One legend row per role — the P / N split is a marker
                # detail, not a separate legend entry. Add the row even
                # when the user has hidden it (so the toggle stays
                # reachable) but skip emitting the MarkerGroup so the
                # markers themselves disappear from the canvas.
                _add_legend(role_key, style["symbol"], style["color"])
                if role_key in self._hidden_legend_keys:
                    continue
                groups.append(MarkerGroup(
                    xs=np.asarray(xs, dtype=np.float64),
                    ys=np.asarray(ys, dtype=np.float64),
                    zs=np.asarray(zs, dtype=np.float64),
                    color=style["color"],
                    symbol=style["symbol"],
                    size=int(style["size"]),
                    edge_color=(_N_NET_MARKER_EDGE if is_n_side
                                else "#000000"),
                    edge_width=1.6,
                ))

            # Via *markers* (orange dots) — skipped in 3D where the via
            # *cylinders* (drawn natively in GL) take over the same role.
            # Also skipped in Via Current mode — those vias come in via
            # the per-bucket coloured marker batch below so the 2D view
            # shows the same heatmap the cylinders show in 3D.
            if not in_3d and not is_via_current:
                # (x, y) -> diameter_mm: dedup across visible layers while
                # keeping each via's physical diameter so the orange dot
                # is sized to the real footprint. The GL viewer floors the
                # drawn size at _VIA_MARKER_MIN_PX so vias stay visible
                # when zoomed out.
                via_pts: dict[tuple[float, float], float] = {}
                for lid in target_layer_ids:
                    vxs, vys, vds = self._collect_via_positions(
                        lid, rail_members)
                    for vx, vy, vd in zip(vxs, vys, vds):
                        via_pts[(vx, vy)] = vd
                if via_pts:
                    _add_legend("VIA", "o", "#ff8c00")
                    if "VIA" not in self._hidden_legend_keys:
                        n_via = len(via_pts)
                        via_xs = np.fromiter((p[0] for p in via_pts),
                                             dtype=np.float64, count=n_via)
                        via_ys = np.fromiter((p[1] for p in via_pts),
                                             dtype=np.float64, count=n_via)
                        via_ds = np.fromiter(via_pts.values(),
                                             dtype=np.float64, count=n_via)
                        groups.append(MarkerGroup(
                            xs=via_xs,
                            ys=via_ys,
                            color="#ff8c00",
                            symbol="o",
                            size=6,  # ignored when world_diameters_mm set
                            edge_color="#000000",
                            edge_width=0.4,
                            world_diameters_mm=via_ds,
                            min_pixel_diameter=self._VIA_MARKER_MIN_PX,
                        ))

                # PTH (plated through-hole) markers — same gating, light
                # grey so they don't compete with the orange via dots.
                # 3D path uses the cylinder batch instead.
                pth_pts: dict[tuple[float, float], float] = {}
                for lid in target_layer_ids:
                    pxs, pys, pds = self._collect_pth_positions(
                        lid, rail_members)
                    for px, py, pd in zip(pxs, pys, pds):
                        pth_pts[(px, py)] = pd
                if pth_pts:
                    _add_legend("PTH", "o", self._PTH_COLOR_HEX)
                    if "PTH" not in self._hidden_legend_keys:
                        n_pth = len(pth_pts)
                        pth_xs = np.fromiter((p[0] for p in pth_pts),
                                             dtype=np.float64, count=n_pth)
                        pth_ys = np.fromiter((p[1] for p in pth_pts),
                                             dtype=np.float64, count=n_pth)
                        pth_ds = np.fromiter(pth_pts.values(),
                                             dtype=np.float64, count=n_pth)
                        groups.append(MarkerGroup(
                            xs=pth_xs,
                            ys=pth_ys,
                            color=self._PTH_COLOR_HEX,
                            symbol="o",
                            size=6,  # ignored when world_diameters_mm set
                            edge_color="#000000",
                            edge_width=0.4,
                            world_diameters_mm=pth_ds,
                            min_pixel_diameter=self._VIA_MARKER_MIN_PX,
                        ))

        # Via Current mode (2D fallback): emit one MarkerGroup per
        # colour bucket so each via shows its current value. The
        # scale controller already explains the ramp, so we don't add
        # legend rows for the bucketed groups.
        if (is_via_current and not in_3d
                and target_layer_ids and self.metadata is not None):
            rail_members = set(self._effective_rail_members(rail_names))
            groups.extend(self._build_via_current_marker_groups(
                target_layer_ids, rail_members,
            ))

        # Jump-highlight ring — always shown, drawn last so it sits on
        # top of every other marker. In 3D place it on the top of the
        # stackup (z=0) so it's clearly visible above the via cylinder.
        if self._highlight_via_xy is not None:
            hx, hy = self._highlight_via_xy
            groups.append(MarkerGroup(
                xs=np.array([hx], dtype=np.float64),
                ys=np.array([hy], dtype=np.float64),
                zs=np.array([0.0], dtype=np.float64),
                color="#ffff00",
                symbol="o",
                size=28,
                edge_color="#000000",
                edge_width=2.5,
            ))

        # Editor-mode directive markers (placed sources / sinks) — only
        # populated while editor mode is active. The non-editor groups are
        # cached so a free-marker drag can refresh just the markers via
        # _refresh_editor_markers without re-walking every pin.
        self._non_editor_marker_groups = list(groups)
        groups.extend(self._editor_marker_groups())

        self._gl_viewer.set_markers(groups)

        # Hover index = solved-directive rows + editor-directive rows, so
        # the bottom bar reports pending editor edits before a Resolve.
        self._metadata_marker_hover_rows = hover_rows
        self._set_marker_hover_rows(
            hover_rows + self._editor_marker_hover_rows())

        self._gl_viewer.set_overlay_top_right_legend(legend_rows)

    # --- CAD-style fixed-scale viewport (via GLMeshViewer) -----------------

    def _fit_board_to_canvas(self, x_min: float, x_max: float,
                             y_min: float, y_max: float) -> None:
        """Pick the largest mm-per-pixel that still fits the board into the
        current GL canvas, with a small margin, and apply it centred on the
        board. Guarded against re-entry from the synchronous ``viewChanged``
        signal that ``fit_to_data`` emits.
        """
        if self._gl_viewer is None:
            return
        self._suppress_view_changed = True
        try:
            self._gl_viewer.fit_to_data(padding=1.05)
            _, _, self._mm_per_pixel = self._gl_viewer.view_center_scale()
        finally:
            self._suppress_view_changed = False
        self._need_initial_fit = False

    def _on_gl_view_changed(self) -> None:
        """GLMeshViewer fired a view-change (pan/zoom/resize) signal.

        Two cases:

        * **Deferred initial fit**: ``_render`` ran before Qt had sized
          the widget, so the initial ``_fit_board_to_canvas`` used a
          stale size. The first real resize triggers this signal — we
          re-fit now that ``self.width()/height()`` are correct.
        * **User pan / zoom / window resize**: just cache the new
          mm-per-pixel so future code (e.g. window-resize handlers) can
          preserve the user's chosen zoom CAD-style.
        """
        if self._suppress_view_changed or self._gl_viewer is None:
            return
        if self._need_initial_fit and self._data_bounds is not None:
            x_min, x_max, y_min, y_max = self._data_bounds
            self._fit_board_to_canvas(x_min, x_max, y_min, y_max)
            return
        _, _, mpp = self._gl_viewer.view_center_scale()
        if mpp > 0:
            self._mm_per_pixel = mpp
        # Arrows are anchored to layer bounds (not screen pixels), so
        # pan / zoom / 3D dolly don't change their world-space positions
        # — no rebuild needed on view change.

    def _on_arrows_toggled(self, checked: bool) -> None:
        """Arrow checkbox flipped — show/hide the density control and
        push the overlay (or clear it). Cheap; doesn't trigger a full
        _render."""
        self.arrow_spacing_label.setVisible(checked)
        self.arrow_spacing_slider.setVisible(checked)
        self._refresh_arrows()

    def _on_arrow_density_changed(self, value: int) -> None:
        """Live update of the arrow-density label, with a debounced
        rebuild so dragging the slider doesn't trigger a full meshgrid
        + trifinder pass on every intermediate value."""
        self.arrow_spacing_label.setText(f"Arrow density: {value}")
        if (not self.show_arrows_box.isChecked()
                or self._gl_viewer is None):
            return
        from PySide6.QtCore import QTimer
        timer = getattr(self, "_arrow_density_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._refresh_arrows)
            self._arrow_density_timer = timer
        timer.start(80)

    def _via_marker_diameter_mm(self, entry: dict) -> float:
        """Physical diameter (mm) used to size a via / PTH marker dot.

        Drill diameter preferred — the same convention as the 3D via
        cylinders (:meth:`_push_via_cylinders`) and the Via Current
        overlay (the drill bore is the current-carrying barrel). Falls
        back to the outer pad diameter, then to
        :data:`_VIA_MARKER_FALLBACK_DIAM_MM` when neither is present."""
        drill = float(entry.get("hole_diameter_mm") or 0.0)
        if drill > 0.0:
            return drill
        outer = float(entry.get("diameter_mm") or 0.0)
        if outer > 0.0:
            return outer
        return self._VIA_MARKER_FALLBACK_DIAM_MM

    def _collect_via_positions(self, target_layer_id: int | None,
                               rail_members: set[str],
                               ) -> tuple[list[float], list[float],
                                          list[float]]:
        """Vias whose span includes ``target_layer_id`` AND whose net is in
        ``rail_members``. Returns parallel xs/ys lists plus each via's
        physical diameter (mm) so the marker overlay can size the orange
        dot to the real via footprint."""
        if target_layer_id is None or self.metadata is None:
            return [], [], []
        xs: list[float] = []
        ys: list[float] = []
        diams: list[float] = []
        for v in self.metadata.get("vias", []):
            ls = v.get("layer_start")
            le = v.get("layer_end")
            if ls is None or le is None:
                continue
            lo, hi = (ls, le) if ls <= le else (le, ls)
            if not (lo <= target_layer_id <= hi):
                continue
            if rail_members and v.get("net") not in rail_members:
                continue
            xs.append(v.get("x_mm", 0.0))
            ys.append(v.get("y_mm", 0.0))
            diams.append(self._via_marker_diameter_mm(v))
        return xs, ys, diams

    def _collect_pth_positions(self, target_layer_id: int | None,
                               rail_members: set[str],
                               ) -> tuple[list[float], list[float],
                                          list[float]]:
        """Plated-through-hole pads whose span includes ``target_layer_id``
        AND whose net is in ``rail_members``. Mirrors
        :meth:`_collect_via_positions` (xs/ys + physical diameter mm); PTHs
        span the full enabled stack so every visible copper layer hits the
        same set of pads (the marker-builder dedups across layers)."""
        if target_layer_id is None or self.metadata is None:
            return [], [], []
        xs: list[float] = []
        ys: list[float] = []
        diams: list[float] = []
        for p in self.metadata.get("pths", []):
            ls = p.get("layer_start")
            le = p.get("layer_end")
            if ls is None or le is None:
                continue
            lo, hi = (ls, le) if ls <= le else (le, ls)
            if not (lo <= target_layer_id <= hi):
                continue
            if rail_members and p.get("net") not in rail_members:
                continue
            xs.append(p.get("x_mm", 0.0))
            ys.append(p.get("y_mm", 0.0))
            diams.append(self._via_marker_diameter_mm(p))
        return xs, ys, diams

    def _on_colormap_changed(self, cmap_name: str) -> None:
        """The colour-scale dropdown picked a new scheme. Recolour every
        heatmapped surface — *without* the full :meth:`_render` rebuild.

        A scheme change touches no geometry and no scalar values: the
        copper mesh recolours on the GPU straight from the 1-D LUT texture
        (the ``set_colormap`` push below), and the gradient strip is
        repainted by the ScaleController itself. The only CPU-side work
        left is re-baking the overlays that carry baked LUT colours — stub
        copper, series bars and (in 3D / Via Current) the via cylinders —
        which :meth:`_recolor_overlays` handles. Skipping ``_render`` here
        avoids rebuilding the rail mesh batch, re-uploading the vertex
        buffers and recomputing the colour-scale range on every toggle.

        In Via Current mode the copper keeps its flat-grey LUT
        (``_gl_cmap_kind == "neutral"``) — only the via overlays recolour.
        """
        if cmap_name == self._cmap_name:
            return
        self._cmap_name = cmap_name
        # Re-push the copper-mesh LUT when the data ramp is the live one.
        # _ensure_gl_cmap would no-op here (kind unchanged), so push direct.
        if self._gl_cmap_kind == "data" and self._gl_viewer is not None:
            self._gl_viewer.set_colormap(_build_cmap_lut(self._cmap_name))
        self._recolor_overlays()

    def _recolor_overlays(self) -> None:
        """Re-push only the overlays whose colours are baked CPU-side from
        the active colour scheme: stub copper, series-component bars and
        (in 3D / Via Current) the via cylinders.

        This is the colour-scheme counterpart of
        :meth:`_reshade_baked_via_overlays` (used by the scale-slider
        path); it additionally covers the stub and series-bar overlays,
        which also carry baked LUT colours. The copper mesh is *not*
        touched here — it recolours from the GPU LUT texture alone.
        """
        if self._gl_viewer is None:
            return
        phys_list, rails, mode = self._current_selection()
        if not phys_list or not rails:
            return
        is_via_current = (mode == _VIA_CURRENT_MODE)
        # 2D Via Current bakes its per-via colours into the marker
        # overlay, which only refreshes cleanly through the full render
        # path (same reason _reshade_baked_via_overlays falls back here).
        if is_via_current and not self.view_3d_box.isChecked():
            self._render()
            return
        drop_reference = self._last_drop_reference
        # Stub copper carries baked LUT colours only when "colour by V"
        # is active; otherwise it's flat grey and the scheme change can't
        # touch it — skip the (geometry-rebuilding) re-push in that case.
        if self._stubs_coloured_by_voltage(mode):
            self._push_stubs(phys_list, rails, mode=mode,
                             drop_reference=drop_reference)
        # Series-component bars always carry a baked LUT gradient.
        self._push_series_bars(phys_list, rails, mode,
                               drop_reference=drop_reference)
        # Via cylinders (3D) carry baked LUT colours only when the
        # heatmap is painted onto them — Via Current mode, or any mode
        # with the Heatmap-vias toggle on. Otherwise they're solid orange.
        heatmap_vias = (
            getattr(self, "heatmap_vias_box", None) is not None
            and self.heatmap_vias_box.isChecked()
        )
        if self.view_3d_box.isChecked() and (is_via_current or heatmap_vias):
            self._push_via_cylinders(phys_list, rails, mode=mode)

    def _on_scale_range_changed(self, vmin: float, vmax: float) -> None:
        """ScaleController emitted a new clamp — push it to the GL viewer
        as a uniform update. Instant for the copper mesh (just a uniform);
        the via overlays in Via Current mode and the cylinder heatmap in
        other modes hold per-vertex baked colours that *don't* react to
        the uniform, so when one of those is active we also re-bake the
        affected overlay against the new range."""
        if vmax <= vmin:
            vmax = vmin + 1e-12
        self._vmin, self._vmax = vmin, vmax
        if self._gl_viewer is not None:
            # GL values were uploaded in _gl_scale space — the level
            # clamp must travel through the same transform to match.
            self._gl_viewer.set_levels(float(self._gl_scale(vmin)),
                                        float(self._gl_scale(vmax)))
        self._reshade_baked_via_overlays()

    def _reshade_baked_via_overlays(self) -> None:
        """Re-push the via cylinders / 2D markers when their colours
        come from a baked LUT lookup (Via Current mode, or any other
        mode with the Heatmap-vias toggle on). Called after the user
        manipulates the scale slider, since the GL viewer's levels
        uniform only re-shades the copper mesh.

        2D Via Current goes through the full :meth:`_render` so the
        marker overlay refreshes via the same path the 2D↔3D toggle
        uses — calling just :meth:`_update_markers_and_legend` in
        isolation wasn't enough on some Qt builds (the paint event
        coalesced behind the GL viewer's pending state and the
        markers stayed at their previous colours until something
        heavier kicked a full repaint).
        """
        mode = self.mode_combo.currentText()
        is_via_current = (mode == _VIA_CURRENT_MODE)
        heatmap_vias = (getattr(self, "heatmap_vias_box", None) is not None
                        and self.heatmap_vias_box.isChecked())
        if not (is_via_current or heatmap_vias):
            return
        phys_list, rails, _ = self._current_selection()
        if not phys_list or not rails:
            return
        if self.view_3d_box.isChecked():
            self._push_via_cylinders(phys_list, rails, mode=mode)
        elif is_via_current:
            self._render()

    # --- Mouse hover (GLMeshViewer signal) ----------------------------------

    # --- 3D view toggle ------------------------------------------------------

    # Fallback uniform spacing (mm) when the stackup metadata doesn't
    # carry per-layer thicknesses. Used only when ``_phys_z_mm`` lacks
    # an entry for the requested layer.
    _LAYER_Z_SPACING_MM: float = 0.4
    # Copper "plate" thickness in 3D mode (pre-exaggeration mm). Picked
    # at ~25% of the layer spacing so the copper looks like a visible
    # plate from oblique angles without dominating the layer gaps.
    # Each layer's flat mesh is extruded into a prism of this thickness
    # in 3D; 2D mode renders the flat mesh untouched.
    _COPPER_THICKNESS_MM: float = 0.0025

    def _layer_z_for(self, phys: str) -> float:
        """World-z (in mm, pre-exaggeration) for a given physical layer.
        Top of the stackup is z=0; lower layers go negative. Prefers the
        cumulative copper + dielectric centreline from the stackup
        metadata; falls back to rank × ``_LAYER_Z_SPACING_MM`` only when
        the layer is missing from the stackup."""
        z = self._phys_z_mm.get(phys)
        if z is not None:
            return z
        rank = self._phys_stackup_rank.get(phys, 0)
        return -rank * self._LAYER_Z_SPACING_MM

    def _on_view_3d_toggled(self, checked: bool) -> None:
        """Switch the GL viewer between 2D and 3D modes and re-render so
        the per-vertex z values get re-built for the new mode (zeros in
        2D, layer-rank-derived in 3D).

        The ``_preserve_view_on_toggle`` flag (set by the Ctrl+Alt+2/3
        hotkeys) tells us the GL viewer's mode has already been switched
        via :meth:`gl_mesh_viewer.set_view_mode_preserving` — skip the
        standard re-fit path so the preserved camera survives.
        """
        if not getattr(self, "_preserve_view_on_toggle", False):
            self._gl_viewer.set_view_mode("3d" if checked else "2d")
        # _render rebuilds arrows internally so they get re-emitted with
        # (N, 3) z-lifted vertices in 3D or flat (N, 2) in 2D.
        self._render_with_busy_popup()

    def _on_layer_spacing_changed(self, value: int) -> None:
        """Live update of the 3D vertical-exaggeration uniform — affects
        both the mesh-layer separation and the via cylinder length.
        Cheap (one uniform), no mesh rebuild, so dragging stays smooth."""
        self._gl_viewer.set_vertical_exaggeration(float(value))
        self.layer_spacing_label.setText(f"Layer spacing: {value}×")

    # --- Keyboard hotkeys ---------------------------------------------------

    def _install_hotkeys(self) -> None:
        """Window-scoped keyboard shortcuts. ``Qt.WindowShortcut`` so they
        fire whenever the viewer window has focus but defer to the text
        boxes (Min/Max scale, etc.) when one of them has focus."""
        bindings = (
            ("2", self._hotkey_2d_mode),
            ("3", self._hotkey_3d_mode),
            ("Ctrl+Alt+2", self._hotkey_2d_mode_preserving),
            ("Ctrl+Alt+3", self._hotkey_3d_mode_preserving),
            ("0", self._hotkey_reset_3d_view),
            ("O", self._hotkey_toggle_outlines),
            ("I", self._hotkey_toggle_markers),
            ("R", self._hotkey_toggle_rail_only),
            ("T", self._hotkey_toggle_cursor_tooltip),
            ("A", self._hotkey_toggle_arrows),
            ("V", self._hotkey_toggle_heatmap_vias),
            ("M", self._hotkey_cycle_mode),
            ("Shift+M", self._hotkey_cycle_mode_reverse),
            ("H", self._hotkey_cycle_colormap),
            ("Shift+H", self._hotkey_cycle_colormap_reverse),
            ("B", self._toggle_sidebar),
            ("E", self._hotkey_toggle_editor),
            # Free-marker edit undo / redo (move + delete) — no-ops outside
            # editor mode.
            ("Ctrl+Z", self._undo_marker_action),
            ("Ctrl+Shift+Z", self._redo_marker_action),
            ("Ctrl+Y", self._redo_marker_action),
            # Delete the selected free marker (Ctrl+Z to restore).
            ("Delete", self._delete_selected_free_marker),
            ("Backspace", self._delete_selected_free_marker),
        )
        # Hold references so the shortcuts don't get garbage-collected.
        self._hotkey_shortcuts = []
        for key, slot in bindings:
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(slot)
            self._hotkey_shortcuts.append(sc)

    def _toggle_sidebar(self) -> None:
        """Show / hide the heatmap-tab side panel so the user can give the
        viewport the panel's 260px of horizontal real estate. The slim
        toggle button between the panel and the plot stays visible either
        way, and its triangle flips ▶ / ◀ to mirror the new state."""
        was_visible = self._sidebar_scroll.isVisible()
        self._sidebar_scroll.setVisible(not was_visible)
        self._sidebar_toggle_btn.setCollapsed(was_visible)

    def _hotkey_2d_mode(self) -> None:
        self.view_3d_box.setChecked(False)

    def _hotkey_3d_mode(self) -> None:
        self.view_3d_box.setChecked(True)

    def _hotkey_2d_mode_preserving(self) -> None:
        """Switch to 2D while keeping the same world region framed —
        the 3D look-at point becomes the 2D centre, camera distance
        becomes mm-per-pixel via the perspective FOV."""
        if not self.view_3d_box.isChecked():
            return
        self._gl_viewer.set_view_mode_preserving("2d")
        self._preserve_view_on_toggle = True
        try:
            self.view_3d_box.setChecked(False)
        finally:
            self._preserve_view_on_toggle = False

    def _hotkey_3d_mode_preserving(self) -> None:
        """Switch to 3D while keeping the same world region framed —
        the 2D centre becomes the look-at point and the camera enters
        top-down at a distance that matches the 2D mm-per-pixel."""
        if self.view_3d_box.isChecked():
            return
        self._gl_viewer.set_view_mode_preserving("3d")
        self._preserve_view_on_toggle = True
        try:
            self.view_3d_box.setChecked(True)
        finally:
            self._preserve_view_on_toggle = False

    def _hotkey_reset_3d_view(self) -> None:
        """No-op in 2D — '0' only resets the view when the user is
        actually looking at the 3D model."""
        if self.view_3d_box.isChecked():
            self._gl_viewer.reset_3d_view()

    def _hotkey_toggle_outlines(self) -> None:
        self._outlines_btn.toggle()

    def _hotkey_toggle_markers(self) -> None:
        self.show_markers_box.toggle()

    def _hotkey_toggle_rail_only(self) -> None:
        # No-op when the box is hidden — it's only hidden while toggling it
        # wouldn't change anything (no visible rail has SERIES siblings).
        if not self.rail_only_box.isVisible():
            return
        self.rail_only_box.toggle()

    def _hotkey_toggle_cursor_tooltip(self) -> None:
        self.cursor_tooltip_box.toggle()

    def _hotkey_toggle_arrows(self) -> None:
        self.show_arrows_box.toggle()

    def _hotkey_toggle_heatmap_vias(self) -> None:
        self.heatmap_vias_box.toggle()

    def _hotkey_cycle_mode(self) -> None:
        self._cycle_mode(+1)

    def _hotkey_cycle_mode_reverse(self) -> None:
        self._cycle_mode(-1)

    def _cycle_mode(self, step: int) -> None:
        count = self.mode_combo.count()
        if count == 0:
            return
        idx = (self.mode_combo.currentIndex() + step) % count
        self.mode_combo.setCurrentIndex(idx)

    def _hotkey_cycle_colormap(self) -> None:
        self._cycle_colormap(+1)

    def _hotkey_cycle_colormap_reverse(self) -> None:
        self._cycle_colormap(-1)

    def _cycle_colormap(self, step: int) -> None:
        """Step the heatmap colour-scheme dropdown forward / backward,
        wrapping at the ends. Setting the index drives the same
        currentIndexChanged path a dropdown click uses, so the gradient
        strip and viewport recolour automatically."""
        combo = self.scale_controller.cmap_combo
        count = combo.count()
        if count == 0:
            return
        idx = (combo.currentIndex() + step) % count
        combo.setCurrentIndex(idx)

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
        source / sink buttons are shown only while editor mode is active."""
        t = _T()
        qss = self._EDITOR_OVERLAY_BTN_QSS % {
            "border": t["border"], "bg": t["bg"],
            "hover": t["bg_hover"], "accent": t["accent"],
        }
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
        btn.setStyleSheet(qss)
        btn.toggled.connect(self._on_editor_mode_toggled)
        self._editor_toggle_btn = btn

        # Free-marker palette — red up-triangle SOURCE / blue down-triangle
        # SINK, matching the solved-directive markers (_ROLE_MARKER_STYLE).
        # Hidden until editor mode is entered. SERIES has no free-marker
        # button: a free marker has a single anchor point, but a SERIES
        # element bridges two separate copper points, so SERIES is
        # component-bound only — assign it by selecting the part.
        for role, attr, up, tip in (
            ("SOURCE", "_editor_add_source_btn", True,
             "Drop a free SOURCE — click, then click copper"),
            ("SINK", "_editor_add_sink_btn", False,
             "Drop a free SINK — click, then click copper"),
        ):
            style = self._ROLE_MARKER_STYLE[role]
            mbtn = QToolButton(self._gl_viewer)
            mbtn.setCheckable(True)
            mbtn.setCursor(Qt.PointingHandCursor)
            mbtn.setIcon(_triangle_icon(style["color"], up=up))
            mbtn.setIconSize(QSize(18, 18))
            mbtn.setFixedSize(34, 34)
            mbtn.setToolTip(tip)
            mbtn.setStyleSheet(qss)
            mbtn.clicked.connect(
                lambda _checked=False, r=role: self._on_editor_add_marker(r)
            )
            mbtn.hide()
            setattr(self, attr, mbtn)

        rbtn = QPushButton("↻  Resolve", self._gl_viewer)
        rbtn.setCursor(Qt.PointingHandCursor)
        rbtn.setToolTip(
            "Re-run the solver with the current editor changes applied"
        )
        # Border + text use a fixed "go" green (not a theme token) so the
        # Resolve action reads as a positive call-to-action in both themes.
        rbtn.setStyleSheet(
            "QPushButton {{ border: 1px solid {green}; border-radius: 6px;"
            "  padding: 6px 12px; font-weight: bold;"
            "  background-color: {bg}; color: {green}; }}"
            "QPushButton:hover {{ background-color: {hover}; }}"
            .format(green="#2ca92c", bg=t["bg"], hover=t["bg_hover"])
        )
        rbtn.clicked.connect(self._on_resolve_clicked)
        rbtn.hide()
        self._resolve_btn = rbtn
        self._position_editor_overlays()

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
        if rbtn is not None and not rbtn.isHidden():
            rbtn.adjustSize()
            rbtn.move(x + 2, margin)
            rbtn.raise_()

    def _build_editor_panel(self) -> QWidget:
        """Right-hand panel — multi-purpose. In editor mode it hosts the
        PDN-role form (filled by :meth:`_populate_editor_form` on
        component / free-marker selection). In viewer mode it hosts the
        copper-properties form (filled by
        :meth:`_populate_copper_props_form` when the user clicks a
        copper primitive). The two contents live in sibling host
        widgets, swapped by :meth:`_show_pdn_editor_layout` /
        :meth:`_show_copper_props_layout`."""
        t = _T()
        panel = QWidget()
        panel.setFixedWidth(300)
        panel.setStyleSheet(f"background-color: {t['bg']};")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Title is swappable so the same widget can read as "PDN Editor"
        # in editor mode and "Copper Properties" in viewer mode.
        self._editor_panel_title = QLabel("<b>PDN Editor</b>")
        lay.addWidget(self._editor_panel_title)

        self._editor_hint = QLabel(self._EDITOR_DEFAULT_HINT)
        self._editor_hint.setWordWrap(True)
        self._editor_hint.setStyleSheet(f"color: {t['fg_muted']};")
        lay.addWidget(self._editor_hint)

        # Form host — (re)populated by _populate_editor_form on selection.
        self._editor_form_host = QWidget()
        self._editor_form_layout = QVBoxLayout(self._editor_form_host)
        self._editor_form_layout.setContentsMargins(0, 0, 0, 0)
        self._editor_form_layout.setSpacing(6)
        self._editor_form_host.hide()
        lay.addWidget(self._editor_form_host)

        # Sibling host for the viewer-mode copper-properties form.
        self._copper_props_host = QWidget()
        self._copper_props_layout = QVBoxLayout(self._copper_props_host)
        self._copper_props_layout.setContentsMargins(0, 0, 0, 0)
        self._copper_props_layout.setSpacing(6)
        self._copper_props_host.hide()
        lay.addWidget(self._copper_props_host)

        lay.addStretch(1)
        return panel

    def _show_pdn_editor_layout(self) -> None:
        """Switch the right panel to its editor-mode contents (title,
        hint, PDN-role form host). Visibility of the form host itself is
        still driven by :meth:`_update_editor_panel`."""
        if not hasattr(self, "_editor_panel_title"):
            return
        self._editor_panel_title.setText("<b>PDN Editor</b>")
        self._editor_hint.show()
        self._copper_props_host.hide()
        # _update_editor_panel decides whether _editor_form_host is on.

    def _show_copper_props_layout(self) -> None:
        """Switch the right panel to its viewer-mode contents (title +
        copper-properties form host). The editor-mode form / hint hide."""
        if not hasattr(self, "_editor_panel_title"):
            return
        self._editor_panel_title.setText("<b>Copper Properties</b>")
        self._editor_hint.hide()
        self._editor_form_host.hide()
        self._copper_props_host.show()

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
        if self._editor_panel.isVisible():
            self._position_editor_panel()
        # Keep the toggle button in sync when entered via the E hotkey.
        if self._editor_toggle_btn.isChecked() != self._editor_mode:
            self._editor_toggle_btn.blockSignals(True)
            self._editor_toggle_btn.setChecked(self._editor_mode)
            self._editor_toggle_btn.blockSignals(False)
        if not self._editor_mode:
            self._editor_selection = None
            self._editor_pending_marker = None
            self._marker_drag = None
            self._clear_editor_highlight()
        else:
            self._update_pending_rails()
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

    _EDITOR_DEFAULT_HINT = (
        "Click a component or a copper region in the viewport to assign a "
        "PDN role, or use the red / blue triangle buttons at the top-left "
        "of the viewport to drop a free source / sink."
    )

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

    def _update_editor_panel(self) -> None:
        """Sync the right-hand panel widgets to the current selection /
        pending-marker state. The PDN form shows for component / free-marker
        selections (and for unnamed copper, where it carries the
        copper-naming form); a plain named-copper pick just updates the
        hint text."""
        if not hasattr(self, "_editor_hint"):
            return
        sel = self._editor_selection
        kind = sel.get("kind") if sel else None
        unnamed_copper = self._is_copper_name_selection(sel)
        has_form = kind in ("component", "free") or unnamed_copper
        self._editor_form_host.setVisible(has_form)
        self._editor_hint.setVisible(not has_form)
        if kind == "copper" and sel and not unnamed_copper:
            self._editor_hint.setText(
                f"Copper net <b>{_esc(sel.get('net', '') or '')}</b> "
                "selected — connected copper is highlighted. Select a "
                "component or drop a free marker to add a source / sink."
            )
        elif not has_form:
            self._editor_hint.setText(self._EDITOR_DEFAULT_HINT)
        self._sync_marker_buttons()

    def _sync_marker_buttons(self) -> None:
        """Reflect the armed free-marker role on the viewport source / sink
        buttons' checked state."""
        pend = self._editor_pending_marker
        for role, attr in (("SOURCE", "_editor_add_source_btn"),
                           ("SINK", "_editor_add_sink_btn")):
            b = getattr(self, attr, None)
            if b is not None and b.isChecked() != (pend == role):
                b.setChecked(pend == role)

    def _on_editor_add_marker(self, role: str) -> None:
        """Arm 'drop a free <role> marker on the next viewport click'.
        Clicking the same button again disarms it."""
        self._editor_pending_marker = (
            None if self._editor_pending_marker == role else role
        )
        self._sync_marker_buttons()
        if self._editor_pending_marker:
            self.statusBar().showMessage(
                f"Click copper to drop a free {role.lower()}.", 4000)
        else:
            self.statusBar().clearMessage()

    def _set_solve_stale(self, stale: bool) -> None:
        """Force-flag the displayed solve as stale (used at viewer load
        when the loaded solve pre-dates the project's editor directives).
        Editor / settings dirty flags can independently extend
        staleness — see :meth:`_refresh_solve_stale_overlay`."""
        self._initial_solve_stale = bool(stale)
        self._refresh_solve_stale_overlay()

    def _refresh_solve_stale_overlay(self) -> None:
        """Recompute ``_solve_stale`` from the editor / settings / initial
        dirty flags, then show or hide the Resolve overlay button."""
        self._solve_stale = (bool(self._project_dirty)
                             or bool(self._settings_dirty)
                             or bool(self._initial_solve_stale))
        rbtn = getattr(self, "_resolve_btn", None)
        if rbtn is not None:
            rbtn.setVisible(self._solve_stale)
            self._position_editor_overlays()

    def _mark_project_dirty(self) -> None:
        """Record that an editor edit happened: the project file is now
        out of date and a re-solve is needed."""
        self._project_dirty = True
        self._refresh_solve_stale_overlay()

    # --- Editor mode: selection + connectivity highlight --------------------

    def _editor_alpha_array(self, layer_probes: list[dict],
                            total_vertices: int) -> np.ndarray | None:
        """Per-vertex alpha for editor-mode copper dimming: 1.0 for copper
        in the connectivity highlight, 0.1 for the rest. Returns ``None``
        when no highlight is active (the mesh then draws fully opaque)."""
        if not self._editor_mode or not self._editor_highlight_nets:
            return None
        hi = self._editor_highlight_nets
        parts: list[np.ndarray] = []
        # layer_probes is top-first; the combined mesh was built
        # bottom-first, so iterate reversed to match the xs/ys/tris order.
        for p in reversed(layer_probes):
            n = int(p.get("n_vertices", 0))
            if n <= 0:
                continue
            a = 1.0 if p.get("net") in hi else 0.1
            parts.append(np.full(n, a, dtype=np.float32))
        if not parts:
            return None
        arr = np.concatenate(parts)
        if arr.size != total_vertices:
            return None   # ordering mismatch — skip dimming, never crash
        return arr

    def _combined_mesh_alpha_array(self, layer_probes: list[dict],
                                    total_vertices: int) -> np.ndarray | None:
        """Per-vertex alpha for the heatmap mesh, combining the per-layer
        TransparencyButton setting with editor-mode connectivity dimming.

        For each (phys, net) slice in the combined mesh, the alpha is
        ``layer_transparency_alpha * editor_dim_alpha``. Returns ``None``
        when no adjustment is needed (every layer fully opaque AND no
        editor highlight active) so the mesh draws on the GPU fast path."""
        if not layer_probes:
            return None
        transp_by_name = dict(
            getattr(self, "_layer_transparency_buttons", []))
        editor_hi = (self._editor_highlight_nets
                      if (self._editor_mode
                          and self._editor_highlight_nets) else None)

        # Cheap pre-check — bail out to the fast path when nothing wants a
        # non-1.0 alpha. The mesh shader's constant attribute is faster
        # than uploading + blending a per-vertex array that's all 1s.
        layer_dim = any(
            (transp_by_name.get(lp.get("physical")) is not None
             and transp_by_name[lp.get("physical")].alpha() < 1.0)
            for lp in layer_probes
        )
        if not layer_dim and editor_hi is None:
            return None

        parts: list[np.ndarray] = []
        # layer_probes is top-first; the combined mesh was built
        # bottom-first, so iterate reversed to match the xs/ys/tris order.
        for lp in reversed(layer_probes):
            n = int(lp.get("n_vertices", 0))
            if n <= 0:
                continue
            tb = transp_by_name.get(lp.get("physical"))
            layer_a = tb.alpha() if tb is not None else 1.0
            if editor_hi is not None:
                editor_a = 1.0 if lp.get("net") in editor_hi else 0.1
            else:
                editor_a = 1.0
            parts.append(np.full(n, float(layer_a * editor_a),
                                  dtype=np.float32))
        if not parts:
            return None
        arr = np.concatenate(parts)
        if arr.size != total_vertices:
            return None   # ordering mismatch — skip dimming, never crash
        return arr

    def _push_mesh_alpha(self) -> None:
        """Re-push the heatmap mesh's per-vertex alpha array from the
        cached ``self._layer_probes``. Used by the layer Transparency
        toggle to update the mesh shading without rebuilding geometry."""
        gv = getattr(self, "_gl_viewer", None)
        if gv is None or not self._layer_probes:
            return
        total = sum(int(lp.get("n_vertices", 0)) for lp in self._layer_probes)
        if total <= 0:
            return
        gv.set_vertex_alpha(
            self._combined_mesh_alpha_array(self._layer_probes, total)
        )

    def _editor_dim_rgb(self, rgb: tuple[float, float, float],
                        net: str | None) -> tuple[float, float, float]:
        """Dim an all-copper overlay colour for editor-mode connectivity
        focus. With a selection highlight active, copper whose ``net`` is
        not in the highlight is blended toward the editor background — the
        same recede the heatmap mesh gets from :meth:`_editor_alpha_array`.
        Highlighted copper, and any time no highlight is active, is
        returned unchanged."""
        if not self._editor_mode or not self._editor_highlight_nets:
            return rgb
        if net in self._editor_highlight_nets:
            return rgb
        f = _EDITOR_OVERLAY_DIM_ALPHA
        bg = _EDITOR_OVERLAY_DIM_BG
        return (rgb[0] * f + bg[0] * (1.0 - f),
                rgb[1] * f + bg[1] * (1.0 - f),
                rgb[2] * f + bg[2] * (1.0 - f))

    def _connected_nets(self, net: str | None) -> set[str]:
        """Nets electrically connected to ``net`` — the net itself plus
        anything bridged to it by a SERIES directive. Same-net copper is
        connected by definition, including across layers through vias.

        Solved SERIES bridges are folded in via the rail-group union-find
        (``_rail_to_members``); editor SERIES directives the user has
        placed but not yet resolved are folded in here as well, so the
        connectivity highlight spans them before a re-solve."""
        if not net:
            return set()
        group: set[str] = {net}
        for members in self._rail_to_members.values():
            if net in members:
                group = set(members)
                break
        # Each editor SERIES directive shorts its P and N nets together.
        # Iterate to closure so a chain of bridges resolves transitively.
        bridges = [
            (d.p_net, d.n_net)
            for d in (self._project.editor_directives
                      if self._project is not None else [])
            if d.role == "SERIES" and d.p_net and d.n_net
        ]
        changed = True
        while changed:
            changed = False
            for a, b in bridges:
                if a in group and b not in group:
                    group.add(b)
                    changed = True
                elif b in group and a not in group:
                    group.add(a)
                    changed = True
        return group

    def _component_side_visible(self, comp: dict) -> bool:
        """Whether the component's side is currently drawn — i.e. the
        Components overlay (Board Features) is showing that side.

        Editor-mode selection skips components on a hidden side: you
        can't select what you can't see. This mirrors the exact
        visibility test in :meth:`_refresh_overlay_geometry` (a side is
        drawn when its overlay ``vis`` is not ``None``), so selection and
        the on-screen component boxes always agree. Components with no /
        unknown side info, or before the overlay state is built, are
        never blocked."""
        side = comp.get("side")
        if side not in ("top", "bottom"):
            return True
        try:
            vis, _solid = self._overlay_side_states("components").get(
                side, (None, None))
        except (AttributeError, KeyError):
            return True
        return vis is not None

    def _component_at(self, world_x: float, world_y: float, *,
                      visible_sides_only: bool = True) -> dict | None:
        """Metadata component record whose bounding box covers the point,
        or ``None``. Smallest box wins when component boxes nest.

        With ``visible_sides_only`` (the default, used for editor-mode
        selection) a component whose side's copper layer is hidden is
        skipped, so the pick can never land on an invisible component —
        and still finds the smallest *visible* one when sides overlap."""
        if not self.metadata:
            return None
        best: dict | None = None
        best_area: float | None = None
        for rec in self.metadata.get("components", []):
            if visible_sides_only and not self._component_side_visible(rec):
                continue
            bbox = rec.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = bbox
            if x0 <= world_x <= x1 and y0 <= world_y <= y1:
                area = (x1 - x0) * (y1 - y0)
                if best_area is None or area < best_area:
                    best, best_area = rec, area
        return best

    def _copper_name_at(self, world_x: float, world_y: float,
                        layer_id: int | None):
        """The :class:`~fypa.project_file.CopperName` whose anchor sits
        on the same ``"(none)"`` polygon as (``world_x``, ``world_y``,
        ``layer_id``), or ``None`` when none of the project's renames
        apply here. Polygon match is by shapely containment of BOTH
        points in the same ``all_copper`` polygon — disjoint unnamed
        pieces with independent renames stay independent."""
        if self._project is None or not self._project.copper_names:
            return None
        if layer_id is None:
            return None
        md = self.metadata or {}
        candidates = [c for c in self._project.copper_names
                      if int(c.layer_id) == int(layer_id)]
        if not candidates:
            return None
        click = _sg.Point(float(world_x), float(world_y))
        for rec in md.get("all_copper") or []:
            if rec.get("layer_id") != int(layer_id):
                continue
            if rec.get("net") != "(none)":
                continue
            for poly in rec.get("polygons", []):
                prepped = self._copper_poly_prepared(poly)
                if prepped is None:
                    continue
                try:
                    if not prepped.contains(click):
                        continue
                except Exception:
                    continue
                for c in candidates:
                    anchor = _sg.Point(float(c.anchor_xy[0]),
                                       float(c.anchor_xy[1]))
                    try:
                        if prepped.contains(anchor):
                            return c
                    except Exception:
                        continue
                return None
        return None

    def _copper_name_override(self, world_x: float, world_y: float,
                               layer_id: int | None) -> str | None:
        """The user-given net name for the unnamed copper polygon at
        (``world_x``, ``world_y``, ``layer_id``), or ``None``. Thin
        wrapper over :meth:`_copper_name_at`."""
        c = self._copper_name_at(world_x, world_y, layer_id)
        return c.name if c is not None else None

    def _apply_copper_name_to_pick(self, pick: dict | None,
                                    world_x: float, world_y: float
                                    ) -> dict | None:
        """If ``pick`` lands on copper currently named ``"(none)"`` and
        a :class:`CopperName` rename pins it to a real name, return a
        copy with the renamed net. ``None`` / non-"(none)" picks pass
        through unchanged."""
        if pick is None or pick.get("net") != "(none)":
            return pick
        new_name = self._copper_name_override(
            world_x, world_y, pick.get("layer_id"))
        if new_name is None:
            return pick
        out = dict(pick)
        out["net"] = new_name
        return out

    def _editor_copper_pick(self, world_x: float,
                            world_y: float) -> dict | None:
        """Copper under the point for editor-mode selection / marker
        placement. Tries, in order: the solved rail mesh, the excluded
        stubs, then the full per-(layer, net) copper set. Returns
        ``{"net", "physical", "layer_id"}`` or ``None`` on bare substrate.

        The final fallback to :meth:`_copper_at_point` is what lets a
        source / sink land on *any* copper — ``_probe_at_point`` and
        ``_probe_at_stub`` only see the rail currently drawn in the
        heatmap, so without it placement is limited to that rail.

        Picks that land on copper whose net is ``"(none)"`` are passed
        through :meth:`_apply_copper_name_to_pick` so a user-supplied
        :class:`CopperName` rename surfaces as the new net name."""
        hit = self._probe_at_point(world_x, world_y)
        if hit is not None and hit[1].get("net"):
            info = hit[1]
            return {"net": info["net"],
                    "physical": info.get("physical"),
                    "layer_id": info.get("layer_id")}
        stub_hit = self._probe_at_stub(world_x, world_y)
        if stub_hit is not None and stub_hit[1].get("net"):
            phys = stub_hit[1].get("physical")
            return {"net": stub_hit[1]["net"],
                    "physical": phys,
                    "layer_id": self._phys_name_to_layer_id.get(phys)}
        return self._apply_copper_name_to_pick(
            self._copper_at_point(world_x, world_y), world_x, world_y)

    def _visible_editor_copper_pick(self, world_x: float,
                                    world_y: float) -> dict | None:
        """Like :meth:`_editor_copper_pick`, but the all-copper fallback is
        restricted to layers whose all-copper eye is on. The rail and stub
        probes are inherently visible-only (they walk the rendered heatmap
        / stub geometry), so they're reused as-is; the all-copper fallback
        is what could otherwise hit copper on a hidden layer."""
        hit = self._probe_at_point(world_x, world_y)
        if hit is not None and hit[1].get("net"):
            info = hit[1]
            return {"net": info["net"],
                    "physical": info.get("physical"),
                    "layer_id": info.get("layer_id")}
        stub_hit = self._probe_at_stub(world_x, world_y)
        if stub_hit is not None and stub_hit[1].get("net"):
            phys = stub_hit[1].get("physical")
            return {"net": stub_hit[1]["net"],
                    "physical": phys,
                    "layer_id": self._phys_name_to_layer_id.get(phys)}
        return self._apply_copper_name_to_pick(
            self._all_copper_at_point(world_x, world_y), world_x, world_y)

    def _net_at(self, world_x: float, world_y: float) -> str | None:
        """Copper net under the point — any copper, not just the rail in
        the heatmap. ``None`` on bare substrate."""
        pick = self._editor_copper_pick(world_x, world_y)
        return pick.get("net") if pick else None

    def _copper_poly_prepared(self, poly: dict):
        """Return (and cache) a shapely PreparedGeometry for one
        ``all_copper`` polygon (exterior ring plus any holes). Mirrors
        :meth:`_stub_prepared_shape`; the cache lives on the polygon
        dict so repeated point tests stay cheap."""
        cached = poly.get("_prepared_shape_cache")
        if cached is not None:
            return cached
        ext = poly.get("exterior")
        if ext is None or (hasattr(ext, "size") and ext.size == 0):
            return None
        try:
            shp = _sg.Polygon(ext, poly.get("holes") or [])
        except Exception:
            return None
        if shp.is_empty:
            return None
        prepped = _sp.prep(shp)
        poly["_prepared_shape_cache"] = prepped
        return prepped

    def _copper_at_point(self, world_x: float,
                         world_y: float) -> dict | None:
        """Copper under the point from the full per-(layer, net) copper
        set — every net on every layer, not just the rail currently drawn
        in the heatmap. Returns ``{"net", "physical", "layer_id"}`` for
        the topmost layer whose copper covers the point, or ``None`` on
        bare substrate."""
        md = self.metadata
        if not md:
            return None
        records = md.get("all_copper") or []
        if not records:
            return None
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        pt = _sg.Point(world_x, world_y)
        best: dict | None = None
        best_rank = 1 << 30
        for rec in records:
            net = rec.get("net")
            if not net:
                continue
            layer_id = rec.get("layer_id")
            phys = id_to_phys.get(layer_id)
            rank = self._phys_stackup_rank.get(phys, 1 << 30)
            # A layer already hit at or above this one wins (topmost = the
            # lowest stackup rank), so this record can't improve the pick.
            if best is not None and rank >= best_rank:
                continue
            for poly in rec.get("polygons", []):
                prepped = self._copper_poly_prepared(poly)
                if prepped is None:
                    continue
                try:
                    if not prepped.contains(pt):
                        continue
                except Exception:
                    continue
                best = {"net": net, "physical": phys, "layer_id": layer_id}
                best_rank = rank
                break
        return best

    def _visible_all_copper_layer_ids(self) -> dict[int, str]:
        """``layer_id → physical_name`` for layers whose 'all copper'
        eye2 button is currently on. The hover fallback uses this to
        restrict bare-copper lookups to copper the user can actually
        see — reporting a net from a hidden layer would be confusing."""
        visible: dict[int, str] = {}
        for name, eye2 in getattr(self, "_layer_eye2_buttons", []) or []:
            if not eye2.isVisibleState():
                continue
            lid = self._phys_name_to_layer_id.get(name)
            if lid is None:
                continue
            visible[lid] = name
        return visible

    def _all_copper_at_point(self, x: float, y: float) -> dict | None:
        """Hover-fallback: topmost visible all-copper polygon covering
        (x, y), filtered by :meth:`_visible_all_copper_layer_ids`.
        Mirrors :meth:`_copper_at_point` but skips hidden layers, so the
        status bar only names a net the user can see.  Returns
        ``{"net", "physical", "layer_id"}`` or ``None``."""
        md = self.metadata
        if not md:
            return None
        records = md.get("all_copper") or []
        if not records:
            return None
        visible = self._visible_all_copper_layer_ids()
        if not visible:
            return None
        pt = _sg.Point(x, y)
        best: dict | None = None
        best_rank = 1 << 30
        for rec in records:
            layer_id = rec.get("layer_id")
            phys = visible.get(layer_id)
            if phys is None:
                continue
            net = rec.get("net")
            if not net:
                continue
            rank = self._phys_stackup_rank.get(phys, 1 << 30)
            if best is not None and rank >= best_rank:
                continue
            for poly in rec.get("polygons", []):
                prepped = self._copper_poly_prepared(poly)
                if prepped is None:
                    continue
                try:
                    if not prepped.contains(pt):
                        continue
                except Exception:
                    continue
                best = {"net": net, "physical": phys, "layer_id": layer_id}
                best_rank = rank
                break
        return best

    def _all_copper_at_point_3d(self, x_px: float, y_px: float
                                ) -> dict | None:
        """3D variant of :meth:`_all_copper_at_point`. Walks visible
        all-copper layers top-first, unprojects the cursor pixel onto
        each layer's z plane, and returns the first layer whose copper
        covers the projected point."""
        md = self.metadata
        if not md:
            return None
        records = md.get("all_copper") or []
        if not records:
            return None
        visible = self._visible_all_copper_layer_ids()
        if not visible:
            return None
        by_layer: dict[int, list] = {}
        for rec in records:
            lid = rec.get("layer_id")
            if lid in visible:
                by_layer.setdefault(lid, []).append(rec)
        ordered = sorted(
            by_layer.items(),
            key=lambda kv: self._phys_stackup_rank.get(
                visible[kv[0]], 1 << 30),
        )
        for layer_id, recs in ordered:
            phys = visible[layer_id]
            z = self._layer_z_for(phys)
            wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
            pt = _sg.Point(wx, wy)
            for rec in recs:
                net = rec.get("net")
                if not net:
                    continue
                for poly in rec.get("polygons", []):
                    prepped = self._copper_poly_prepared(poly)
                    if prepped is None:
                        continue
                    try:
                        if not prepped.contains(pt):
                            continue
                    except Exception:
                        continue
                    return {"net": net, "physical": phys,
                            "layer_id": layer_id}
        return None

    # --- Viewer-mode copper-primitive picker ------------------------------
    #
    # Click-to-select on any visible copper primitive in viewer mode. The
    # selected primitive's outline is drawn as a dashed yellow polygon
    # (gl_mesh_viewer.set_primitive_selection_outline) and the right-hand
    # editor panel widget is repurposed to show its properties (kind,
    # net, layer, geometry). Bare-substrate clicks (or Escape) clear.

    def _primitives_index(self) -> dict[tuple[int, str], list[dict]]:
        """Lazy ``(layer_id, net) -> [primitive dicts]`` index over
        ``metadata['primitives']``. Built once per project; the per-shape
        caches survive on the dicts so repeat hits stay cheap."""
        if self._primitives_by_layer_net is not None:
            return self._primitives_by_layer_net
        idx: dict[tuple[int, str], list[dict]] = {}
        md = self.metadata or {}
        prims = md.get("primitives") or {}
        for bucket in ("tracks", "arcs", "regions",
                       "shape_based_regions", "fills"):
            for rec in prims.get(bucket, []):
                key = (int(rec.get("layer_id", -1)),
                       str(rec.get("net", "")))
                idx.setdefault(key, []).append(rec)
        self._primitives_by_layer_net = idx
        return idx

    def _primitive_prepared_shape(self, prim: dict):
        """Build (and cache on the primitive dict) a prepared shapely
        geometry for ``prim``. Returns ``None`` if the geometry can't be
        constructed (degenerate primitive)."""
        cached = prim.get("_prepared_shape")
        if cached is not None:
            return cached
        kind = prim.get("kind")
        shp = None
        try:
            if kind == "track":
                line = _sg.LineString([(prim["ax"], prim["ay"]),
                                       (prim["bx"], prim["by"])])
                # Round caps + joins to match the actual copper polygon
                # (altium_geometry._track_polygon uses cap_style=1).
                shp = line.buffer(max(prim["width_mm"] * 0.5, 1e-6),
                                  cap_style=1, join_style=1,
                                  resolution=8)
            elif kind == "arc":
                # Match altium_geometry._arc_polyline_points exactly so
                # the buffered outline matches the copper polygon.
                from fypa.altium_geometry import ARC_CHORD_TOLERANCE_MM
                sweep_deg = ((prim["end_angle_deg"]
                              - prim["start_angle_deg"]) % 360.0)
                if sweep_deg == 0.0:
                    sweep_deg = 360.0
                start = math.radians(prim["start_angle_deg"])
                sweep = math.radians(sweep_deg)
                cx, cy, r = prim["cx"], prim["cy"], prim["radius_mm"]
                half_w = max(prim["width_mm"] * 0.5, 1e-6)
                if r <= 0.0:
                    shp = _sg.Point(cx, cy).buffer(half_w, resolution=8)
                else:
                    cos_arg = max(-1.0, 1.0 - ARC_CHORD_TOLERANCE_MM / r)
                    max_step_rad = 2.0 * math.acos(cos_arg)
                    if max_step_rad <= 0.0:
                        n = max(8, int(round(sweep_deg)))
                    else:
                        n = max(8, int(math.ceil(sweep / max_step_rad)))
                    pts = [(cx + r * math.cos(start + sweep * k / n),
                            cy + r * math.sin(start + sweep * k / n))
                           for k in range(n + 1)]
                    line = _sg.LineString(pts)
                    # Round caps + joins to match the actual copper
                    # polygon (altium_geometry._arc_polygon uses
                    # cap_style=1). Round caps at the (coincident)
                    # endpoints of a full-circle arc overlap cleanly
                    # into the tube; flat / square caps would stick a
                    # radial sliver inside the ring.
                    shp = line.buffer(half_w, cap_style=1, join_style=1,
                                      resolution=8)
            elif kind == "fill":
                import shapely.affinity as _sa
                box = _sg.box(prim["x1_mm"], prim["y1_mm"],
                              prim["x2_mm"], prim["y2_mm"])
                rot = float(prim.get("rotation_deg", 0.0) or 0.0)
                shp = (_sa.rotate(box, rot, origin="center")
                       if rot else box)
            elif kind in ("region", "shape_based_region"):
                outline = prim.get("outline") or []
                if len(outline) >= 3:
                    holes = [h for h in (prim.get("holes") or [])
                             if len(h) >= 3]
                    shp = _sg.Polygon(outline, holes)
        except Exception:
            shp = None
        if shp is None or shp.is_empty:
            return None
        prepped = _sp.prep(shp)
        prim["_prepared_shape"] = prepped
        prim["_shape"] = shp
        return prepped

    def _via_or_pad_at_point(self, world_x: float, world_y: float
                              ) -> dict | None:
        """Fast path for via / through-hole-pad / SMT-pad clicks. These
        sit on top of copper polygons, so they need to be tested before
        the per-polygon all-copper hit-test would shadow them.

        Returns a primitive-shaped dict ``{"kind", "record", "layer_id",
        "net"}`` for the topmost visible hit, or ``None``."""
        md = self.metadata or {}
        visible = self._visible_all_copper_layer_ids()
        if not visible:
            return None
        best: dict | None = None
        best_rank = 1 << 30

        def _take(kind: str, rec: dict, layer_ids: list[int]) -> None:
            nonlocal best, best_rank
            for lid in layer_ids:
                phys = visible.get(int(lid))
                if phys is None:
                    continue
                rank = self._phys_stackup_rank.get(phys, 1 << 30)
                if rank < best_rank:
                    best_rank = rank
                    best = {"kind": kind, "record": rec,
                            "layer_id": int(lid),
                            "net": rec.get("net", "") or ""}
                break

        for bucket_kind, bucket in (("via", md.get("vias") or []),
                                     ("pth", md.get("pths") or [])):
            for rec in bucket:
                d = float(rec.get("diameter_mm", 0.0) or 0.0)
                if d <= 0.0:
                    continue
                dx = world_x - float(rec.get("x_mm", 0.0))
                dy = world_y - float(rec.get("y_mm", 0.0))
                if (dx * dx + dy * dy) > (d * 0.5) ** 2:
                    continue
                ls = int(rec.get("layer_start", 0))
                le = int(rec.get("layer_end", 0))
                if ls and le:
                    span = list(range(min(ls, le), max(ls, le) + 1))
                else:
                    span = []
                _take(bucket_kind, rec, span)

        pads = md.get("pads") or []
        if pads:
            pt = _sg.Point(world_x, world_y)
            for rec in pads:
                outline = rec.get("outline") or []
                if len(outline) < 3:
                    continue
                try:
                    poly = _sg.Polygon(outline)
                    if poly.is_empty or not poly.contains(pt):
                        continue
                except Exception:
                    continue
                _take("pad", rec, list(rec.get("layer_ids") or []))
        return best

    def _primitive_at_point(self, world_x: float, world_y: float
                             ) -> dict | None:
        """Topmost visible copper primitive containing the world-mm point.
        Returns a dict ``{"kind", "record", "layer_id", "net"}`` or
        ``None`` on bare substrate / hidden copper.

        Probe order: vias / pads first (they sit on top of pours), then
        ``_all_copper_at_point`` narrows the topmost-visible (layer, net),
        then walks the per-(layer, net) primitive list for the
        track / arc / fill / region whose shape contains the point."""
        hit = self._via_or_pad_at_point(world_x, world_y)
        if hit is not None:
            return hit
        cover = self._all_copper_at_point(world_x, world_y)
        if cover is None:
            return None
        key = (int(cover["layer_id"]), str(cover["net"]))
        prims = self._primitives_index().get(key) or []
        if not prims:
            return None
        pt = _sg.Point(world_x, world_y)
        for prim in prims:
            prepped = self._primitive_prepared_shape(prim)
            if prepped is None:
                continue
            try:
                if not prepped.contains(pt):
                    continue
            except Exception:
                continue
            return {"kind": prim["kind"], "record": prim,
                    "layer_id": int(prim["layer_id"]),
                    "net": str(prim["net"])}
        return None

    def _primitive_outline_rings(self, hit: dict
                                  ) -> list[list[tuple[float, float]]]:
        """Closed world-mm rings outlining the selected primitive — the
        geometry the GL viewer draws as a dashed yellow polygon. Always
        returns at least one ring (caller has a hit)."""
        kind = hit.get("kind")
        rec = hit.get("record") or {}
        if kind in ("via", "pth"):
            cx = float(rec.get("x_mm", 0.0))
            cy = float(rec.get("y_mm", 0.0))
            r = float(rec.get("diameter_mm", 0.0)) * 0.5
            steps = 64
            return [[
                (cx + r * math.cos(2.0 * math.pi * k / steps),
                 cy + r * math.sin(2.0 * math.pi * k / steps))
                for k in range(steps)
            ]]
        if kind == "pad":
            outline = rec.get("outline") or []
            return [[(float(x), float(y)) for x, y in outline]]
        shp = rec.get("_shape")
        if shp is None:
            self._primitive_prepared_shape(rec)
            shp = rec.get("_shape")
        rings: list[list[tuple[float, float]]] = []
        if shp is not None:
            polys = (list(shp.geoms)
                     if shp.geom_type == "MultiPolygon" else [shp])
            for poly in polys:
                ext = getattr(poly, "exterior", None)
                if ext is None or ext.is_empty:
                    continue
                rings.append([(float(x), float(y))
                              for x, y in ext.coords])
                for hole in getattr(poly, "interiors", []):
                    if not hole.is_empty:
                        rings.append([(float(x), float(y))
                                      for x, y in hole.coords])
        if not rings:
            outline = rec.get("outline")
            if outline:
                rings = [[(float(x), float(y)) for x, y in outline]]
        return rings

    @staticmethod
    def _fmt_mm(v) -> str:
        try:
            return f"{float(v):.3f} mm"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_xy(x, y) -> str:
        try:
            return f"({float(x):.3f}, {float(y):.3f}) mm"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_deg(v) -> str:
        try:
            return f"{float(v):.2f}°"
        except (TypeError, ValueError):
            return "—"

    _PAD_SHAPE_NAMES = {
        1: "Round",
        2: "Rectangular",
        3: "Octagonal",
        9: "Rounded Rectangle",
    }

    def _layer_name_for_id(self, layer_id) -> str:
        """Physical-layer name for ``layer_id`` (the inverse of
        ``_phys_name_to_layer_id``). Returns the raw id as a string when
        the layer isn't in the stackup map."""
        if layer_id is None:
            return "—"
        try:
            lid = int(layer_id)
        except (TypeError, ValueError):
            return str(layer_id)
        for name, mapped in self._phys_name_to_layer_id.items():
            if mapped == lid:
                return name
        return f"layer {lid}"

    def _populate_copper_props_form(self, hit: dict) -> None:
        """(Re)build the right-panel form describing the selected copper
        primitive. Rows are kind-specific; the common header lists shape,
        net, and layer (or layer span for via / PTH / multi-layer pad)."""
        if not hasattr(self, "_copper_props_layout"):
            return
        lay = self._copper_props_layout
        self._clear_layout(lay)

        kind = hit.get("kind") or "?"
        rec = hit.get("record") or {}
        net = str(hit.get("net") or rec.get("net") or "")

        kind_label = {
            "track": "Track",
            "arc": "Arc",
            "fill": "Fill",
            "region": "Region",
            "shape_based_region": "Region (shape-based)",
            "via": "Via",
            "pth": "Through-hole pad",
            "pad": "Pad",
        }.get(kind, kind.title())

        lay.addWidget(QLabel(f"<b>{_esc(kind_label)}</b>"))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)

        def add(label: str, value: str) -> None:
            v = QLabel(value)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            form.addRow(QLabel(label), v)

        add("Net", net or "(no net)")

        if kind in ("via", "pth"):
            ls = self._layer_name_for_id(rec.get("layer_start"))
            le = self._layer_name_for_id(rec.get("layer_end"))
            add("Layer span", f"{ls} → {le}" if ls != le else ls)
        elif kind == "pad":
            ids = list(rec.get("layer_ids") or [])
            if len(ids) <= 1:
                add("Layer", self._layer_name_for_id(
                    ids[0] if ids else hit.get("layer_id")))
            else:
                names = [self._layer_name_for_id(i) for i in ids]
                add("Layers", ", ".join(names))
        else:
            add("Layer", self._layer_name_for_id(hit.get("layer_id")))

        if kind == "track":
            ax, ay = float(rec["ax"]), float(rec["ay"])
            bx, by = float(rec["bx"]), float(rec["by"])
            length = math.hypot(bx - ax, by - ay)
            add("Start", self._fmt_xy(ax, ay))
            add("End", self._fmt_xy(bx, by))
            add("Width", self._fmt_mm(rec.get("width_mm")))
            add("Length", self._fmt_mm(length))
            if rec.get("is_polygon_outline"):
                add("Polygon outline", "yes")
            if rec.get("is_keepout"):
                add("Keepout", "yes")
        elif kind == "arc":
            start_deg = float(rec.get("start_angle_deg", 0.0))
            end_deg = float(rec.get("end_angle_deg", 0.0))
            sweep = (end_deg - start_deg) % 360.0
            if sweep == 0.0 and start_deg != end_deg:
                sweep = 360.0
            r = float(rec.get("radius_mm", 0.0))
            arc_len = r * math.radians(sweep)
            add("Center", self._fmt_xy(rec.get("cx"), rec.get("cy")))
            add("Radius", self._fmt_mm(r))
            add("Start angle", self._fmt_deg(start_deg))
            add("End angle", self._fmt_deg(end_deg))
            add("Sweep angle", self._fmt_deg(sweep))
            add("Width", self._fmt_mm(rec.get("width_mm")))
            add("Arc length", self._fmt_mm(arc_len))
            if rec.get("is_keepout"):
                add("Keepout", "yes")
        elif kind == "fill":
            x1 = float(rec.get("x1_mm", 0.0))
            y1 = float(rec.get("y1_mm", 0.0))
            x2 = float(rec.get("x2_mm", 0.0))
            y2 = float(rec.get("y2_mm", 0.0))
            add("Corner 1", self._fmt_xy(x1, y1))
            add("Corner 2", self._fmt_xy(x2, y2))
            add("Width", self._fmt_mm(abs(x2 - x1)))
            add("Height", self._fmt_mm(abs(y2 - y1)))
            add("Rotation", self._fmt_deg(rec.get("rotation_deg")))
            add("Area", f"{abs((x2 - x1) * (y2 - y1)):.3f} mm²")
            if rec.get("is_keepout"):
                add("Keepout", "yes")
        elif kind in ("region", "shape_based_region"):
            outline = rec.get("outline") or []
            shp = rec.get("_shape")
            if shp is None:
                self._primitive_prepared_shape(rec)
                shp = rec.get("_shape")
            add("Vertices", f"{len(outline)}")
            if kind == "shape_based_region":
                add("Arc edges", f"{int(rec.get('arc_edge_count', 0))}")
            if shp is not None:
                minx, miny, maxx, maxy = shp.bounds
                add("Bounding box",
                    f"x: {minx:.3f} → {maxx:.3f} mm\n"
                    f"y: {miny:.3f} → {maxy:.3f} mm")
                add("Area", f"{shp.area:.4f} mm²")
            if rec.get("is_polygon_outline"):
                add("Polygon outline", "yes")
            if rec.get("is_keepout"):
                add("Keepout", "yes")
            if rec.get("is_board_cutout"):
                add("Board cutout", "yes")
        elif kind in ("via", "pth"):
            add("Center",
                self._fmt_xy(rec.get("x_mm"), rec.get("y_mm")))
            add("Outer diameter", self._fmt_mm(rec.get("diameter_mm")))
            add("Hole diameter",
                self._fmt_mm(rec.get("hole_diameter_mm")))
            if kind == "pth":
                des = rec.get("designator")
                if des:
                    add("Designator", str(des))
            else:
                lbl = rec.get("ipc4761_label")
                if lbl:
                    add("IPC-4761", str(lbl))
                fm = rec.get("fill_material")
                if fm:
                    add("Fill material", str(fm))
        elif kind == "pad":
            des = rec.get("designator")
            if des:
                add("Designator", str(des))
            add("Center",
                self._fmt_xy(rec.get("x_mm"), rec.get("y_mm")))
            add("Width", self._fmt_mm(rec.get("width_mm")))
            add("Height", self._fmt_mm(rec.get("height_mm")))
            shape_code = int(rec.get("shape_code", 0) or 0)
            add("Shape",
                self._PAD_SHAPE_NAMES.get(shape_code,
                                          f"code {shape_code}"))
            add("Rotation", self._fmt_deg(rec.get("rotation_deg")))
            if shape_code == 9:
                add("Corner radius",
                    f"{int(rec.get('corner_radius_pct', 0))}%")
            if rec.get("is_through_hole"):
                add("Hole diameter", self._fmt_mm(rec.get("hole_mm")))
            if rec.get("is_smt"):
                add("Mount", "SMT")
            elif rec.get("is_through_hole"):
                add("Mount", "Through-hole")

        lay.addLayout(form)

    def _select_copper_primitive(self, hit: dict) -> None:
        """Store the click-selected primitive, push its dashed-yellow
        outline to the GL viewer, populate the right-panel form, and
        show the panel."""
        self._copper_selection = hit
        rings = self._primitive_outline_rings(hit)
        self._gl_viewer.set_primitive_selection_outline(rings)
        self._populate_copper_props_form(hit)
        self._show_copper_props_layout()
        self._position_editor_panel()
        self._editor_panel.show()

    def _clear_copper_selection(self) -> None:
        """Drop any viewer-mode copper selection — clears the dashed-yellow
        GL overlay, empties the right-panel form, and hides the panel
        (unless editor mode is active, in which case the panel stays
        visible with the PDN editor contents)."""
        if self._copper_selection is None:
            return
        self._copper_selection = None
        gl = getattr(self, "_gl_viewer", None)
        if gl is not None:
            gl.set_primitive_selection_outline(None)
        lay = getattr(self, "_copper_props_layout", None)
        if lay is not None:
            self._clear_layout(lay)
        if not self._editor_mode:
            self._show_pdn_editor_layout()
            panel = getattr(self, "_editor_panel", None)
            if panel is not None:
                panel.hide()

    def _on_editor_click(self, world_x: float, world_y: float) -> None:
        """Editor-mode left-click: drop a pending free marker if one is
        armed, else select the component / placed marker / copper under
        the cursor."""
        if self._editor_pending_marker is not None:
            self._place_free_marker(world_x, world_y)
            return
        # Component first: a marker sitting on a component reads as that
        # component's source / sink, so clicking it loads the component
        # properties rather than the free-marker form.
        comp = self._component_at(world_x, world_y)
        if comp is not None:
            self._select_component(comp)
            return
        # A component may sit here but on a hidden side — it can't be
        # selected. Say why, then fall through to the marker / copper
        # underneath (whatever the user can actually see).
        hidden = self._component_at(world_x, world_y, visible_sides_only=False)
        if hidden is not None:
            side = hidden.get("side") or "hidden"
            self.statusBar().showMessage(
                f"{hidden.get('designator') or 'Component'} is on the "
                f"hidden {side} side — turn on the {side}-side Components "
                "overlay to select it.", 4000)
        marker = self._free_marker_at(world_x, world_y)
        if marker is not None:
            self._select_free_marker(marker)
            return
        hit = self._primitive_at_point(world_x, world_y)
        if hit is not None and hit.get("net"):
            self._select_copper(hit["net"], hit,
                                anchor_xy=(world_x, world_y))
            return
        # No visible primitive — fall back to the more permissive
        # visible-only picker (rail mesh / stub / visible all-copper).
        pick = self._visible_editor_copper_pick(world_x, world_y)
        if pick is not None and pick.get("net"):
            self._select_copper(pick["net"],
                                anchor_xy=(world_x, world_y))
            return
        # Copper exists here but only on a hidden layer — mention it so
        # the user knows why nothing got selected, then fall through to
        # clear (a click the user can't see reads as "click off copper",
        # matching viewer-mode behaviour).
        hidden = self._editor_copper_pick(world_x, world_y)
        if hidden is not None and hidden.get("net"):
            phys = hidden.get("physical") or "a hidden layer"
            self.statusBar().showMessage(
                f"Copper on {phys} is hidden — turn on its eye to "
                "select it.", 4000)
        # Bare substrate (or hidden-only copper) — clear the selection.
        self._editor_selection = None
        self._clear_editor_highlight()
        self._update_editor_panel()

    def _free_marker_at(self, world_x: float,
                        world_y: float):
        """The placed free-marker :class:`EditorDirective` whose anchor is
        under the click, or ``None``. The hit radius scales with the view
        zoom so it tracks the on-screen marker size; the nearest marker
        wins when several overlap."""
        if self._project is None:
            return None
        try:
            _cx, _cy, mm_per_px = self._gl_viewer.view_center_scale()
        except Exception:
            mm_per_px = 0.1
        radius = max(float(mm_per_px) * _EDITOR_MARKER_HIT_PX, 1e-6)
        best = None
        best_d2 = radius * radius
        for d in self._project.editor_directives:
            if d.kind != "free" or d.anchor_xy is None:
                continue
            dx = world_x - float(d.anchor_xy[0])
            dy = world_y - float(d.anchor_xy[1])
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best, best_d2 = d, d2
        return best

    def _select_free_marker(self, directive) -> None:
        """Select a placed free source / sink marker — the right-hand
        panel opens its PDN form so the role / value / nets can be
        edited (see :meth:`_populate_editor_form`)."""
        self._editor_selection = {"kind": "free", "id": directive.id}
        self._gl_viewer.set_primitive_selection_outline(None)
        highlight = (self._connected_nets(directive.p_net)
                     if directive.p_net else set())
        self._apply_editor_highlight(highlight)
        self._populate_editor_form()

    def _select_component(self, comp: dict) -> None:
        """Select a PCB component for PDN editing and highlight the copper
        connected to its pins."""
        nets = list(comp.get("nets", []))
        self._editor_selection = {
            "kind": "component",
            "designator": comp.get("designator"),
            "nets": nets,
            "bbox": comp.get("bbox"),
            # True once the user clicks "Unlock" on a component whose PDN
            # values come from the Altium schematic — see _on_editor_unlock.
            "unlocked": False,
        }
        self._gl_viewer.set_primitive_selection_outline(None)
        highlight: set[str] = set()
        for n in nets:
            highlight |= self._connected_nets(n)
        self._apply_editor_highlight(highlight)
        self._populate_editor_form()

    def _select_copper(self, net: str, hit: dict | None = None,
                       anchor_xy: tuple[float, float] | None = None) -> None:
        """Select a copper net for PDN editing and highlight every net
        connected to it. When ``hit`` is the primitive picker's result
        for the click, its outline is pushed to the GL viewer as the
        same dashed-yellow polygon used by viewer-mode copper selection.
        ``anchor_xy`` records the click point so the right-hand panel's
        copper-name form (shown only when ``net == "(none)"``) can pin
        the rename to THIS polygon — its layer comes from the hit."""
        sel: dict = {"kind": "copper", "net": net}
        if anchor_xy is not None:
            sel["anchor_xy"] = (float(anchor_xy[0]), float(anchor_xy[1]))
        if hit is not None and hit.get("layer_id") is not None:
            sel["layer_id"] = int(hit["layer_id"])
        self._editor_selection = sel
        rings = self._primitive_outline_rings(hit) if hit else None
        self._gl_viewer.set_primitive_selection_outline(rings)
        self._apply_editor_highlight(self._connected_nets(net))
        self._populate_editor_form()

    def _apply_editor_highlight(self, nets: set[str]) -> None:
        """Set the connectivity highlight and re-render — non-connected
        copper dims to 10%."""
        self._editor_highlight_nets = set(nets)
        self._render()
        self._update_editor_panel()

    def _clear_editor_highlight(self) -> None:
        """Drop the connectivity highlight; copper returns to full opacity.
        Also clears the dashed-yellow outline set by a copper selection."""
        if self._editor_highlight_nets:
            self._editor_highlight_nets = set()
            self._render()
        self._gl_viewer.set_primitive_selection_outline(None)

    # --- Editor mode: PDN form + free markers -------------------------------

    def _ensure_project(self):
        """Return the in-memory :class:`ProjectFile`, creating an empty one
        (seeded from the loaded metadata) the first time an editor edit
        needs somewhere to live. Pickle paths stay blank until a save."""
        if self._project is None:
            from fypa.project_file import ProjectFile
            self._project = ProjectFile()
            if self.metadata:
                self._project.prjpcb_path = self.metadata.get("prjpcb_path")
                self._project.pcbdoc_path = self.metadata.get("pcbdoc_path")
        return self._project

    def _all_net_names(self) -> list[str]:
        """Sorted list of every net name known to this viewer — solved
        per-net layers, rail-group members, metadata copper / pads /
        vias, and any user-supplied :class:`CopperName` renames. Used
        to populate the N-net picker. Cached; invalidate by setting
        ``_all_net_names_cache`` to ``None`` after edits."""
        cached = getattr(self, "_all_net_names_cache", None)
        if cached is not None:
            return cached
        nets: set[str] = set()
        for (_phys, net) in self._index_by_pair:
            nets.add(net)
        for members in self._rail_to_members.values():
            nets.update(members)
        md = self.metadata or {}
        for rec in md.get("components", []):
            nets.update(rec.get("nets", []) or [])
        for key in ("pads", "vias", "pths", "all_copper"):
            for rec in md.get(key, []):
                n = rec.get("net")
                if n:
                    nets.add(n)
        if self._project is not None:
            for c in self._project.copper_names:
                if c.name:
                    nets.add(c.name)
        out = sorted(n for n in nets if n)
        self._all_net_names_cache = out
        return out

    def _component_center(self, designator: str | None
                          ) -> tuple[float, float] | None:
        """Centre of the named component's bounding box, or ``None``."""
        if not designator or not self.metadata:
            return None
        for rec in self.metadata.get("components", []):
            if rec.get("designator") == designator:
                bbox = rec.get("bbox")
                if bbox and len(bbox) == 4:
                    return (0.5 * (bbox[0] + bbox[2]),
                            0.5 * (bbox[1] + bbox[3]))
        return None

    def _component_pad_points(
        self, designator: str | None, nets,
    ) -> list[tuple[float, float]]:
        """Centres of the named component's pads that sit on any net in
        ``nets`` — one point per matching pin.

        Lets an editor directive draw a marker on every pin it couples to
        (mirroring the solved-directive pin markers) instead of one glyph
        at the component centre. Pad records are keyed ``"<comp>-<pad>"``
        in the metadata, so the component prefix isolates this component's
        pads. Returns ``[]`` when no pad matches, so the caller can fall
        back to :meth:`_component_center`."""
        if not designator or not self.metadata:
            return []
        want = {n for n in (nets or ()) if n}
        if not want:
            return []
        prefix = f"{designator}-"
        pts: list[tuple[float, float]] = []
        for rec in self.metadata.get("pads", []):
            des = rec.get("designator") or ""
            if not des.startswith(prefix):
                continue
            if rec.get("net") not in want:
                continue
            ring = rec.get("outline")
            if not ring:
                continue
            arr = np.asarray(ring, dtype=np.float64)
            if arr.ndim != 2 or arr.shape[0] < 1:
                continue
            pts.append(
                (0.5 * (float(arr[:, 0].min()) + float(arr[:, 0].max())),
                 0.5 * (float(arr[:, 1].min()) + float(arr[:, 1].max())))
            )
        return pts

    def _directive_for_selection(self):
        """Return the :class:`EditorDirective` matching the current
        selection (component designator or free-marker id), or ``None``."""
        sel = self._editor_selection
        if not sel or self._project is None:
            return None
        if sel.get("kind") == "component":
            des = sel.get("designator")
            for d in self._project.editor_directives:
                if d.kind == "component" and d.designator == des:
                    return d
        elif sel.get("kind") == "free":
            return self._project.directive_by_id(sel.get("id") or "")
        return None

    @staticmethod
    def _clear_layout(layout) -> None:
        """Remove and delete every widget / nested layout in ``layout``."""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    PdnViewer._clear_layout(child)

    @staticmethod
    def _set_combo(combo: QComboBox, value: str) -> None:
        """Select ``value`` in ``combo``, adding it first if absent."""
        if value is None:
            return
        if combo.findText(value) < 0:
            combo.addItem(value)
        combo.setCurrentText(value)

    def _form_p_nets(self, sel: dict) -> list[str]:
        """Candidate P-net names for the form's P picker, given the
        selection: a component offers its pins' nets; a free marker offers
        the connected-copper group it sits on."""
        if sel.get("kind") == "component":
            return list(sel.get("nets", []) or []) or self._all_net_names()
        d = self._directive_for_selection()
        if d is not None and d.p_net:
            return sorted(self._connected_nets(d.p_net)) or [d.p_net]
        return self._all_net_names()

    @staticmethod
    def _terminal_net_label(term: dict | None) -> str:
        """Human label for a metadata directive terminal — its named net,
        else its pins' nets, else an ideal-return / dash placeholder."""
        if not term:
            return "—"
        if term.get("ideal_return"):
            return "ideal 0 V return"
        rn = term.get("requested_net")
        if rn:
            return rn
        nets = sorted({p.get("net") for p in term.get("pins", []) or []
                       if p.get("net")})
        return ", ".join(nets) if nets else "—"

    def _schematic_directives_for(self, sel: dict) -> list[dict]:
        """Schematic PDN directives (from the solve metadata) belonging to
        the selected component — matched by designator, or by a directive
        terminal pin landing inside the component's bounding box (robust to
        multi-channel designator re-basing)."""
        if not self.metadata:
            return []
        des = sel.get("designator")
        bbox = sel.get("bbox")
        out: list[dict] = []
        for d in self.metadata.get("directives", []):
            if des and d.get("designator") == des:
                out.append(d)
                continue
            if bbox and len(bbox) == 4:
                x0, y0, x1, y1 = bbox
                hit = False
                for term in (d.get("terminals") or {}).values():
                    for pin in term.get("pins", []) or []:
                        px, py = pin.get("x_mm"), pin.get("y_mm")
                        if (px is not None and py is not None
                                and x0 <= px <= x1 and y0 <= py <= y1):
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    out.append(d)
        return out

    def _build_schematic_info(self, lay, directives: list[dict]) -> None:
        """Read-only panel block describing a component's existing Altium
        schematic PDN directives (PDN_ROLE / PDN_V / PDN_I / PDN_*_NET)."""
        t = _T()
        blocks: list[str] = []
        for d in directives:
            # A ResistorSpec carries the role "RESISTOR" in solve metadata;
            # show the user-facing "SERIES" name to match the editor form.
            role = d.get("role", "?")
            if role == "RESISTOR":
                role = "SERIES"
            val = d.get("value_str") or ""
            terms = d.get("terminals") or {}
            tparts = [
                f"{tname}&nbsp;=&nbsp;"
                f"{_esc(self._terminal_net_label(terms[tname]))}"
                for tname in ("P", "N", "OUT_P", "OUT_N", "IN_P", "IN_N")
                if tname in terms
            ]
            head = f"<b>{_esc(role)}</b>"
            if val:
                head += f" &middot; {_esc(val)}"
            blocks.append(head + "<br>" + " &nbsp; ".join(tparts))
        box = QLabel(
            f"<span style='color:{t['fg_muted']};'>Defined in the Altium "
            f"schematic</span><br>{'<br><br>'.join(blocks)}"
        )
        box.setWordWrap(True)
        box.setTextFormat(Qt.RichText)
        box.setStyleSheet(
            f"QLabel {{ border: 1px solid {t['border']}; border-radius: 4px;"
            f" padding: 6px; background-color: {t['bg_alt']}; }}"
        )
        lay.addWidget(box)
        note = QLabel(
            "This rail is set up in the Altium schematic. Unlock it to "
            "override these values in FYPA for a re-solve."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(note)
        # Unlock button — for editable roles (SOURCE / SINK / SERIES);
        # REGULATOR directives aren't editable through this form. A
        # schematic SERIES carries the RESISTOR role name in solve metadata.
        if any(d.get("role") in ("SOURCE", "SINK", "RESISTOR")
               for d in directives):
            unlock = QPushButton("🔓  Unlock to edit")
            unlock.setToolTip(
                "Edit these PDN values in FYPA — the edited directive "
                "overrides the schematic one on the next resolve."
            )
            unlock.clicked.connect(self._on_editor_unlock)
            lay.addWidget(unlock)

    def _unlockable_schematic_directive(self, sel: dict) -> dict | None:
        """The first SOURCE / SINK / SERIES schematic directive on the
        selected component — the one the Unlock button / form edits. A
        schematic SERIES carries the ``RESISTOR`` role in solve metadata
        (named after its ResistorSpec)."""
        for d in self._schematic_directives_for(sel):
            if d.get("role") in ("SOURCE", "SINK", "RESISTOR"):
                return d
        return None

    @staticmethod
    def _terminal_primary_net(term: dict | None) -> str | None:
        """One representative net for a metadata directive terminal — its
        named net, else its first pin's net. ``None`` for an ideal return."""
        if not term or term.get("ideal_return"):
            return None
        rn = term.get("requested_net")
        if rn:
            return rn
        for p in term.get("pins", []) or []:
            if p.get("net"):
                return p["net"]
        return None

    def _on_editor_unlock(self) -> None:
        """Unlock a schematic-defined component for editing — the read-only
        info is replaced by the editable form, seeded from the schematic
        directive. Applying it overrides the schematic directive."""
        sel = self._editor_selection
        if sel and sel.get("kind") == "component":
            sel["unlocked"] = True
            self._populate_editor_form()

    def _build_free_marker_location(self, lay, directive) -> None:
        """Location block for a selected free marker: the fixed layer plus
        editable X / Y boxes and marker-edit undo / redo buttons (covering
        both moves and deletes). The layer is shown read-only — a free
        marker's layer is fixed once it has been placed."""
        t = _T()
        lay.addWidget(QLabel("Location"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)

        layer_lbl = QLabel(_esc(directive.layer or "?"))
        layer_lbl.setToolTip("A free marker's layer is fixed once placed.")
        layer_lbl.setStyleSheet(f"color: {t['fg_muted']};")
        form.addRow("Layer", layer_lbl)

        ax, ay = directive.anchor_xy or (0.0, 0.0)
        self._ef_loc_x = QLineEdit(f"{float(ax):.4f}")
        self._ef_loc_x.setValidator(QDoubleValidator(-1e9, 1e9, 4))
        self._ef_loc_x.setToolTip("X position (mm) — must stay on copper")
        self._ef_loc_x.editingFinished.connect(
            self._on_free_marker_coord_edited)
        form.addRow("X (mm)", self._ef_loc_x)
        self._ef_loc_y = QLineEdit(f"{float(ay):.4f}")
        self._ef_loc_y.setValidator(QDoubleValidator(-1e9, 1e9, 4))
        self._ef_loc_y.setToolTip("Y position (mm) — must stay on copper")
        self._ef_loc_y.editingFinished.connect(
            self._on_free_marker_coord_edited)
        form.addRow("Y (mm)", self._ef_loc_y)
        lay.addLayout(form)

        hint = QLabel(
            "Drag the marker, or edit X / Y — the location must stay on "
            "copper of its layer, or it reverts.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(hint)

        moves = QHBoxLayout()
        self._ef_undo_move = QPushButton("↶ Undo")
        self._ef_undo_move.setToolTip(
            "Undo the last free-marker edit (move or delete)")
        self._ef_undo_move.clicked.connect(self._undo_marker_action)
        self._ef_redo_move = QPushButton("↷ Redo")
        self._ef_redo_move.setToolTip(
            "Redo the last undone free-marker edit (move or delete)")
        self._ef_redo_move.clicked.connect(self._redo_marker_action)
        moves.addWidget(self._ef_undo_move)
        moves.addWidget(self._ef_redo_move)
        lay.addLayout(moves)
        self._update_marker_undo_buttons()

    def _populate_copper_name_form(self, sel: dict) -> None:
        """Populate the right-hand form with a single-field form for
        naming a copper piece that has no Altium net name. The click
        anchor + layer pin the rename to one specific polygon — other
        disjoint unnamed copper on the board is unaffected. When the
        same polygon already carries a :class:`CopperName`, the form
        title flips to "Named copper" and the field is pre-filled with
        the saved name so the user can review / edit / re-apply."""
        t = _T()
        lay = self._editor_form_layout
        ax, ay = sel.get("anchor_xy") or (0.0, 0.0)
        layer_id = sel.get("layer_id")
        phys = None
        if layer_id is not None:
            id_to_phys = {v: k
                          for k, v in self._phys_name_to_layer_id.items()}
            phys = id_to_phys.get(int(layer_id))

        # Polygon-based lookup (NOT a strict anchor match) so a re-click
        # at any point on the named piece restores the saved name.
        existing = self._copper_name_at(
            float(ax), float(ay), int(layer_id))

        if existing is not None:
            lay.addWidget(QLabel("<b>Named copper</b>"))
            hint = QLabel(
                "This copper is named <b>" + _esc(existing.name)
                + "</b> — edit the name below and Apply to rename it, or "
                "leave it as-is. Other disjoint unnamed copper is "
                "unaffected."
            )
        else:
            lay.addWidget(QLabel("<b>Unnamed copper</b>"))
            hint = QLabel(
                "This copper has no net name in Altium. Give it a name "
                "here to make it solvable — only this piece is named, "
                "other disjoint unnamed copper stays as <b>(none)</b>."
            )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(hint)

        info = QFormLayout()
        info.setContentsMargins(0, 0, 0, 0)
        layer_lbl = QLabel(_esc(phys or "?"))
        layer_lbl.setStyleSheet(f"color: {t['fg_muted']};")
        info.addRow("Layer", layer_lbl)
        loc_lbl = QLabel(f"({float(ax):.4f}, {float(ay):.4f}) mm")
        loc_lbl.setStyleSheet(f"color: {t['fg_muted']};")
        info.addRow("At", loc_lbl)
        lay.addLayout(info)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._ef_copper_name = QLineEdit()
        self._ef_copper_name.setPlaceholderText("e.g. STAR_GND_PAD")
        if existing is not None:
            self._ef_copper_name.setText(existing.name)
        self._ef_copper_name.returnPressed.connect(self._on_apply_copper_name)
        form.addRow("Net name", self._ef_copper_name)
        lay.addLayout(form)

        btns = QHBoxLayout()
        self._ef_copper_name_apply = QPushButton("Apply")
        self._ef_copper_name_apply.clicked.connect(self._on_apply_copper_name)
        btns.addWidget(self._ef_copper_name_apply)
        lay.addLayout(btns)

        self._ef_copper_name_status = QLabel("")
        self._ef_copper_name_status.setWordWrap(True)
        self._ef_copper_name_status.setStyleSheet(
            f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(self._ef_copper_name_status)

    def _on_apply_copper_name(self) -> None:
        """Validate the entered name, save it as a :class:`CopperName`
        rename, and retroactively re-derive ``p_net`` for any free
        markers already placed on the same polygon (their ``"(none)"``
        becomes the new name so the resolve guard accepts them)."""
        sel = self._editor_selection
        if (not sel or sel.get("kind") != "copper"
                or sel.get("anchor_xy") is None
                or sel.get("layer_id") is None):
            return
        edit = getattr(self, "_ef_copper_name", None)
        status = getattr(self, "_ef_copper_name_status", None)
        if edit is None:
            return
        raw = edit.text().strip()
        t = _T()

        def _err(msg: str) -> None:
            if status is not None:
                status.setText(f"<span style='color:{t['warn']};'>"
                               f"{_esc(msg)}</span>")

        if not raw:
            _err("Enter a net name.")
            return
        if raw == "(none)" or raw.lower() in {"none", "(none)"}:
            _err("'(none)' is the unnamed-copper sentinel — pick a real name.")
            return
        # Disallow reusing an existing named net — that would let the user
        # silently merge this unnamed copper onto another rail without the
        # connectivity to support it (and confuse the FEM matrix).
        existing_nets = {n for n in self._all_net_names() if n != "(none)"}
        anchor = (float(sel["anchor_xy"][0]), float(sel["anchor_xy"][1]))
        layer_id = int(sel["layer_id"])
        # Find an existing rename via polygon containment so a re-click at
        # a different point on the same piece updates the same record
        # instead of creating a duplicate CopperName.
        prev = self._copper_name_at(anchor[0], anchor[1], layer_id)
        if raw in existing_nets and (prev is None or prev.name != raw):
            _err(f"'{raw}' is already a net on this board — pick a "
                 "different name.")
            return

        from fypa.project_file import CopperName
        proj = self._ensure_project()
        if prev is not None:
            prev.name = raw
        else:
            proj.upsert_copper_name(CopperName(
                anchor_xy=anchor, layer_id=layer_id, name=raw,
            ))
        # Net-name cache is stale now — the new name needs to appear in
        # dropdowns and connectivity probes immediately.
        self._all_net_names_cache = None
        # Retroactively update any free marker already placed on the same
        # piece of unnamed copper so the resolve guard accepts it.
        updated = 0
        for d in proj.editor_directives:
            if (d.kind == "free" and d.p_net == "(none)"
                    and d.anchor_xy is not None):
                pick = self._editor_copper_pick(
                    float(d.anchor_xy[0]), float(d.anchor_xy[1]))
                if pick and pick.get("net") == raw:
                    d.p_net = raw
                    updated += 1
        self._mark_project_dirty()
        # Promote the selection to the now-named net so the connectivity
        # highlight tracks it. Keep the form widgets in place (don't call
        # _populate_editor_form) — that preserves the user's text in the
        # field and keeps the success status visible until the next
        # interaction.
        sel["net"] = raw
        self._apply_editor_highlight(self._connected_nets(raw))
        msg = f"Named — this copper is now <b>{_esc(raw)}</b>."
        if updated:
            msg += (f" Updated {updated} existing marker"
                    f"{'s' if updated != 1 else ''}.")
        if status is not None:
            status.setText(f"<span style='color:{t['ok']};'>{msg}</span>")

    def _populate_editor_form(self) -> None:
        """(Re)build the form for the current selection: the PDN-role
        form for a component / free-marker, the copper-naming form for
        unnamed copper (``net == "(none)"``), or nothing for a plain
        named-copper selection."""
        if not hasattr(self, "_editor_form_layout"):
            return
        self._clear_layout(self._editor_form_layout)
        sel = self._editor_selection
        if self._is_copper_name_selection(sel):
            self._populate_copper_name_form(sel)
            self._update_editor_panel()
            return
        if not sel or sel.get("kind") not in ("component", "free"):
            self._update_editor_panel()
            return
        t = _T()
        lay = self._editor_form_layout
        existing = self._directive_for_selection()

        if sel["kind"] == "component":
            title = f"Component <b>{_esc(sel.get('designator') or '?')}</b>"
        else:
            title = "<b>Free marker</b>"
        lay.addWidget(QLabel(title))

        # Free markers are moveable — a fixed-layer label, editable X / Y
        # boxes, and dedicated move undo / redo. Attributes are reset here
        # so the rest of the form (and the drag path) never touches a
        # QLineEdit left dangling by the _clear_layout above.
        self._ef_loc_x = None
        self._ef_loc_y = None
        self._ef_undo_move = None
        self._ef_redo_move = None
        if sel["kind"] == "free" and existing is not None:
            self._build_free_marker_location(lay, existing)

        # A component carrying PDN_* directives in the Altium schematic is
        # shown read-only with an Unlock button — until the user unlocks it
        # (or an editor override already exists), no editable form appears.
        sch_unlock: dict | None = None
        if sel["kind"] == "component":
            sch = self._schematic_directives_for(sel)
            if sch and existing is None and not sel.get("unlocked"):
                self._build_schematic_info(lay, sch)
                self._update_editor_panel()
                return
            sch_unlock = self._unlockable_schematic_directive(sel)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._ef_role = QComboBox()
        # SERIES is component-bound only — it bridges two real pads, so a
        # single-anchor free marker can't express it. Offer SERIES for a
        # component selection (or when editing a directive that is already
        # SERIES), never for a fresh free marker.
        role_items = ["SOURCE", "SINK"]
        if sel["kind"] == "component" or (
                existing is not None and existing.role == "SERIES"):
            role_items.append("SERIES")
        self._ef_role.addItems(role_items)
        self._ef_role.currentTextChanged.connect(self._on_editor_role_changed)
        form.addRow("PDN role", self._ef_role)
        self._ef_value = QLineEdit()
        self._ef_value.setValidator(QDoubleValidator(0.0, 1e12, 6))
        self._ef_value_label = QLabel("Voltage (V)")
        form.addRow(self._ef_value_label, self._ef_value)
        # SINK-only optional minimum acceptable rail voltage (PDN_MIN_V
        # equivalent). Blank disables the per-pin pass/fail check. Visibility
        # is toggled by _on_editor_role_changed below.
        self._ef_min_v = QLineEdit()
        self._ef_min_v.setValidator(QDoubleValidator(0.0, 1e12, 6))
        self._ef_min_v.setToolTip(
            "Optional: minimum acceptable rail voltage at this sink's pins. "
            "Leave blank to skip the check. Sinks below this voltage are "
            "flagged red in the Nodes table."
        )
        self._ef_min_v_label = QLabel("Min V (V)")
        form.addRow(self._ef_min_v_label, self._ef_min_v)
        lay.addLayout(form)

        lay.addWidget(QLabel("Current model"))
        self._ef_single = QRadioButton("Single net (point-to-point)")
        self._ef_two = QRadioButton("Two nets (full current-path loop)")
        self._ef_btngroup = QButtonGroup(self._editor_form_host)
        self._ef_btngroup.addButton(self._ef_single)
        self._ef_btngroup.addButton(self._ef_two)
        self._ef_single.setChecked(True)
        self._ef_single.toggled.connect(self._on_editor_model_changed)
        lay.addWidget(self._ef_single)
        lay.addWidget(self._ef_two)

        form2 = QFormLayout()
        form2.setContentsMargins(0, 0, 0, 0)
        self._ef_pnet = QComboBox()
        self._ef_pnet.addItems(self._form_p_nets(sel))
        self._ef_pnet_label = QLabel("Net")
        form2.addRow(self._ef_pnet_label, self._ef_pnet)
        self._ef_nnet = QComboBox()
        self._ef_nnet.addItems(self._all_net_names())
        self._ef_nnet_label = QLabel("N net")
        form2.addRow(self._ef_nnet_label, self._ef_nnet)
        lay.addLayout(form2)

        btns = QHBoxLayout()
        self._ef_apply = QPushButton("Apply")
        self._ef_apply.clicked.connect(self._on_editor_apply)
        self._ef_remove = QPushButton("Remove")
        self._ef_remove.clicked.connect(self._on_editor_remove)
        btns.addWidget(self._ef_apply)
        btns.addWidget(self._ef_remove)
        lay.addLayout(btns)

        self._ef_status = QLabel("")
        self._ef_status.setWordWrap(True)
        self._ef_status.setStyleSheet(
            f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(self._ef_status)

        if existing is not None:
            role = (existing.role
                    if existing.role in ("SOURCE", "SINK", "SERIES")
                    else "SINK")
            self._ef_role.setCurrentText(role)
            val = {"SINK": existing.current,
                   "SERIES": existing.resistance}.get(role, existing.voltage)
            self._ef_value.setText("" if val is None else f"{val:g}")
            mv = getattr(existing, "min_voltage", None)
            self._ef_min_v.setText("" if mv is None else f"{mv:g}")
            self._ef_single.setChecked(existing.single_net)
            self._ef_two.setChecked(not existing.single_net)
            if existing.p_net:
                self._set_combo(self._ef_pnet, existing.p_net)
            if existing.n_net:
                self._set_combo(self._ef_nnet, existing.n_net)
            self._ef_remove.setEnabled(True)
            if existing.overrides_designator:
                self._ef_status.setText(
                    f"<span style='color:{t['warn']};'>Overrides the "
                    "schematic directive.</span>"
                )
        elif sch_unlock is not None:
            # Just unlocked, no editor override yet — seed the form from the
            # schematic directive so the user adjusts its actual values. A
            # schematic SERIES carries the RESISTOR role name in metadata.
            raw_role = sch_unlock.get("role")
            role = {"RESISTOR": "SERIES"}.get(raw_role, raw_role)
            if role not in ("SOURCE", "SINK", "SERIES"):
                role = "SINK"
            self._ef_role.setCurrentText(role)
            val = sch_unlock.get("value")
            self._ef_value.setText("" if val is None else f"{val:g}")
            mv = sch_unlock.get("min_voltage")
            self._ef_min_v.setText("" if mv is None else f"{mv:g}")
            terms = sch_unlock.get("terminals") or {}
            n_term = terms.get("N")
            single = (n_term is None) or bool(n_term.get("ideal_return"))
            self._ef_single.setChecked(single)
            self._ef_two.setChecked(not single)
            p_net = self._terminal_primary_net(terms.get("P"))
            if p_net:
                self._set_combo(self._ef_pnet, p_net)
            n_net = self._terminal_primary_net(n_term)
            if n_net:
                self._set_combo(self._ef_nnet, n_net)
            self._ef_remove.setEnabled(False)
            self._ef_status.setText(
                f"<span style='color:{t['warn']};'>Unlocked — Apply "
                "replaces the schematic directive on the next resolve.</span>"
            )
        else:
            self._ef_remove.setEnabled(False)
        self._on_editor_role_changed(self._ef_role.currentText())
        self._on_editor_model_changed()
        self._update_editor_panel()

    def _on_editor_role_changed(self, role: str) -> None:
        """Swap the value field between voltage (SOURCE), current (SINK)
        and resistance (SERIES). SERIES always bridges two nets, so its
        current model is forced to two-net and the radios are locked.
        The optional Min V (PDN_MIN_V) field is only shown for SINK."""
        if not hasattr(self, "_ef_value_label"):
            return
        self._ef_value_label.setText(
            {"SINK": "Current (A)", "SERIES": "Resistance (Ω)"}.get(
                role, "Voltage (V)")
        )
        if hasattr(self, "_ef_min_v"):
            is_sink = role == "SINK"
            self._ef_min_v.setVisible(is_sink)
            self._ef_min_v_label.setVisible(is_sink)
            if not is_sink:
                self._ef_min_v.clear()
        if hasattr(self, "_ef_two"):
            is_series = role == "SERIES"
            if is_series and not self._ef_two.isChecked():
                self._ef_two.setChecked(True)
            self._ef_single.setEnabled(not is_series)
            self._ef_two.setEnabled(not is_series)
            self._on_editor_model_changed()

    def _on_editor_model_changed(self, *_args) -> None:
        """Two-net shows both the 'P net' and 'N net' pickers; single-net
        hides the N-net row entirely and relabels the P picker just 'Net'."""
        if not hasattr(self, "_ef_two"):
            return
        two = self._ef_two.isChecked()
        self._ef_nnet.setVisible(two)
        self._ef_nnet.setEnabled(two)
        self._ef_nnet_label.setVisible(two)
        self._ef_pnet_label.setText("P net" if two else "Net")

    def _on_editor_apply(self) -> None:
        """Commit the form into an :class:`EditorDirective` on the project,
        flag the project dirty + the solve stale, and refresh markers."""
        from fypa.project_file import EditorDirective
        sel = self._editor_selection
        if not sel or sel.get("kind") not in ("component", "free"):
            return
        role = self._ef_role.currentText()
        txt = self._ef_value.text().strip()
        quantity = {"SINK": "current", "SERIES": "resistance"}.get(
            role, "voltage")
        try:
            value = float(txt)
        except ValueError:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>Enter a numeric "
                f"{quantity} value.</span>"
            )
            return
        if role == "SERIES" and value <= 0:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>SERIES resistance "
                "must be greater than zero.</span>"
            )
            return
        # SERIES always bridges two real nets — never single-net.
        single = self._ef_single.isChecked() and role != "SERIES"
        p_net = self._ef_pnet.currentText() or None
        n_net = self._ef_nnet.currentText() if not single else None
        if not p_net:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>"
                "Pick a P net.</span>"
            )
            return
        if role == "SERIES" and not n_net:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>Pick an N net — a "
                "SERIES element bridges two nets.</span>"
            )
            return

        # SINK-only optional minimum-rail-voltage check. Blank disables it;
        # any non-numeric text is rejected so a stray keystroke can't silently
        # drop the check.
        min_v: float | None = None
        if role == "SINK":
            mv_txt = self._ef_min_v.text().strip()
            if mv_txt:
                try:
                    min_v = float(mv_txt)
                except ValueError:
                    self._ef_status.setText(
                        f"<span style='color:{_T()['err']};'>Min V must be "
                        "numeric (or blank to skip the check).</span>"
                    )
                    return

        existing = self._directive_for_selection()
        d = existing if existing is not None else EditorDirective()
        d.role = role
        d.single_net = single
        d.p_net = p_net
        d.n_net = n_net
        d.voltage = value if role in ("SOURCE", "REGULATOR") else None
        d.current = value if role == "SINK" else None
        d.resistance = value if role == "SERIES" else None
        d.min_voltage = min_v
        if sel["kind"] == "component":
            d.kind = "component"
            d.designator = sel.get("designator")
            # If this component has a schematic directive, mark the editor
            # directive as its override so the re-solve drops the schematic
            # one instead of stamping both.
            sch_unlock = self._unlockable_schematic_directive(sel)
            d.overrides_designator = (
                sch_unlock.get("designator") if sch_unlock else None
            )
        else:
            d.kind = "free"
            self._editor_selection = {"kind": "free", "id": d.id}

        self._ensure_project().upsert_directive(d)
        self._mark_project_dirty()
        self._apply_editor_highlight(self._connected_nets(p_net))
        self._update_pending_rails()
        self._render()
        self._ef_status.setText(
            f"<span style='color:{_T()['ok']};'>Applied — press Resolve "
            "to re-solve.</span>"
        )

    def _on_editor_remove(self) -> None:
        """Delete the directive bound to the current selection. Undoable
        via Ctrl+Z (or the panel's Undo button); the prior selection is
        captured so undo also restores it (a component-bound directive
        re-selects its component, a free marker re-selects itself)."""
        existing = self._directive_for_selection()
        if existing is None or self._project is None:
            return
        try:
            idx = self._project.editor_directives.index(existing)
        except ValueError:
            return
        prev_selection = (dict(self._editor_selection)
                          if self._editor_selection else None)
        self._project.remove_directive(existing.id)
        self._marker_undo.append({
            "op": "delete",
            "id": existing.id,
            "directive": existing,
            "index": idx,
            "prev_selection": prev_selection,
        })
        self._marker_redo.clear()
        self._editor_selection = None
        self._mark_project_dirty()
        self._update_pending_rails()
        self._update_marker_undo_buttons()
        # Empty highlight + render so the marker disappears from the
        # viewport and connected copper returns to full opacity.
        self._apply_editor_highlight(set())
        self._populate_editor_form()

    def _place_free_marker(self, world_x: float, world_y: float) -> None:
        """Drop a free source / sink / series marker on the copper under the
        cursor and open its form. No-op (with a hint) if the click missed
        copper, or if the copper under the cursor is on a hidden layer —
        the user shouldn't be able to drop a marker on copper they can't
        see. A SERIES marker is created two-net with its N net still unset
        — the form prompts for it before the directive can resolve."""
        from fypa.project_file import EditorDirective
        role = self._editor_pending_marker
        if role is None:
            return
        pick = self._visible_editor_copper_pick(world_x, world_y)
        if pick is None or not pick.get("net"):
            hidden = self._editor_copper_pick(world_x, world_y)
            if hidden is not None and hidden.get("net"):
                phys = hidden.get("physical") or "a hidden layer"
                self.statusBar().showMessage(
                    f"Copper on {phys} is hidden — turn on its eye to "
                    "place a marker there.", 4000)
            else:
                self.statusBar().showMessage(
                    "No copper there — click on a copper region.", 4000)
            return
        net = pick["net"]
        layer = pick.get("physical")
        layer_id = pick.get("layer_id")
        d = EditorDirective(
            kind="free", role=role, anchor_xy=(world_x, world_y),
            layer=layer, layer_id=layer_id,
            single_net=(role != "SERIES"), p_net=net,
            voltage=(3.3 if role == "SOURCE" else None),
            current=(1.0 if role == "SINK" else None),
            resistance=(0.01 if role == "SERIES" else None),
        )
        self._ensure_project().upsert_directive(d)
        self._editor_pending_marker = None
        self._editor_selection = {"kind": "free", "id": d.id}
        self._gl_viewer.set_primitive_selection_outline(None)
        self._mark_project_dirty()
        self._apply_editor_highlight(self._connected_nets(net))
        self._update_pending_rails()
        self._populate_editor_form()
        self._render()

    # --- Editor mode: free-marker move (drag + X/Y edit + undo) -------------

    def _refresh_editor_markers(self) -> None:
        """Marker-only viewport refresh — re-push the cached non-editor
        marker groups plus freshly rebuilt editor-directive markers,
        without rebuilding the FEM mesh. Used on every tick of a
        free-marker drag so the move stays smooth."""
        cached = self._non_editor_marker_groups
        if cached is None:
            self._render()
            return
        self._gl_viewer.set_markers(
            list(cached) + self._editor_marker_groups())
        # Keep the hover index in step with the moved marker — recombine
        # the cached solved-directive rows with freshly rebuilt editor rows.
        self._set_marker_hover_rows(
            self._metadata_marker_hover_rows
            + self._editor_marker_hover_rows())

    def _free_marker_layer_id(self, directive) -> int | None:
        """The Altium copper layer id a free marker is pinned to — its
        stored ``layer_id``, else resolved from its physical layer name."""
        if directive.layer_id is not None:
            return directive.layer_id
        if directive.layer:
            return self._phys_name_to_layer_id.get(directive.layer)
        return None

    def _copper_on_layer_at(self, world_x: float, world_y: float,
                            layer_id: int | None) -> dict | None:
        """Netted copper of the given layer under the point, or ``None``.
        Unlike :meth:`_copper_at_point` this never crosses to another
        layer — a free marker's layer is fixed, so its move must stay on
        it. Net-less copper is skipped so a moved marker always re-derives
        a real net, exactly as placement requires."""
        md = self.metadata
        if not md or layer_id is None:
            return None
        pt = _sg.Point(world_x, world_y)
        for rec in md.get("all_copper") or []:
            if rec.get("layer_id") != layer_id or not rec.get("net"):
                continue
            for poly in rec.get("polygons", []):
                prepped = self._copper_poly_prepared(poly)
                if prepped is None:
                    continue
                try:
                    if prepped.contains(pt):
                        return {"net": rec.get("net"), "layer_id": layer_id}
                except Exception:
                    continue
        return None

    def _point_on_marker_layer(self, directive,
                               world_x: float, world_y: float) -> bool:
        """Whether ``(world_x, world_y)`` lands on copper of the free
        marker's fixed layer. Falls back to 'any copper' only when the
        marker's layer can't be determined."""
        layer_id = self._free_marker_layer_id(directive)
        if layer_id is None:
            return self._copper_at_point(world_x, world_y) is not None
        return self._copper_on_layer_at(world_x, world_y, layer_id) is not None

    def _marker_drag_hit_test(self, world_x: float,
                              world_y: float) -> bool:
        """GL-viewer hook — True when a draggable free marker sits under
        the point, so the viewer claims the press as a move gesture.
        Disabled while a marker-drop is armed (that press places a new
        marker instead)."""
        if not self._editor_mode or self._editor_pending_marker is not None:
            return False
        return self._free_marker_at(world_x, world_y) is not None

    def _on_marker_drag_started(self, world_x: float,
                                world_y: float) -> None:
        """Begin a free-marker drag — record the start anchor / net and
        select the grabbed marker (a press with no drag still selects)."""
        marker = self._free_marker_at(world_x, world_y)
        if marker is None or marker.anchor_xy is None:
            self._marker_drag = None
            return
        start = (float(marker.anchor_xy[0]), float(marker.anchor_xy[1]))
        self._marker_drag = {
            "id": marker.id,
            "start_xy": start,
            "start_p_net": marker.p_net,
            "last_valid": start,
        }
        if (self._editor_selection or {}).get("id") != marker.id:
            self._select_free_marker(marker)

    def _on_marker_drag_moved(self, world_x: float,
                              world_y: float) -> None:
        """Track a free-marker drag — move the marker to the cursor while
        it stays on copper of its layer, else hold it at the last valid
        spot so it can't be dragged off copper."""
        drag = self._marker_drag
        if drag is None or self._project is None:
            return
        d = self._project.directive_by_id(drag["id"])
        if d is None:
            return
        if self._point_on_marker_layer(d, world_x, world_y):
            drag["last_valid"] = (float(world_x), float(world_y))
        nx, ny = drag["last_valid"]
        d.anchor_xy = (nx, ny)
        # setText doesn't fire editingFinished, so the live readout costs
        # nothing and keeps the on-copper check off the drag path.
        self._sync_free_marker_coord_fields(nx, ny)
        self._refresh_editor_markers()

    def _on_marker_drag_released(self, world_x: float,
                                 world_y: float) -> None:
        """Finish a free-marker drag — commit the move (recording it for
        undo) when it actually moved, else just leave it selected."""
        drag = self._marker_drag
        self._marker_drag = None
        if drag is None or self._project is None:
            return
        d = self._project.directive_by_id(drag["id"])
        if d is None:
            return
        new_xy = drag["last_valid"]
        start_xy = drag["start_xy"]
        if (abs(new_xy[0] - start_xy[0]) < 1e-9
                and abs(new_xy[1] - start_xy[1]) < 1e-9):
            d.anchor_xy = start_xy
            self._populate_editor_form()
            return
        self._commit_marker_move(d, start_xy, drag["start_p_net"], new_xy)
        self._populate_editor_form()

    def _commit_marker_move(self, directive, old_xy, old_p_net,
                            new_xy) -> None:
        """Apply a committed free-marker move: update the anchor, re-derive
        the net from the copper under the new spot (a free marker's net
        follows its copper, mirroring placement), record it for undo, and
        refresh the viewport. Does NOT rebuild the panel form — the caller
        owns that, so it can defer it out of a text-box signal."""
        directive.anchor_xy = (float(new_xy[0]), float(new_xy[1]))
        pick = self._copper_on_layer_at(
            new_xy[0], new_xy[1], self._free_marker_layer_id(directive))
        if pick and pick.get("net"):
            directive.p_net = pick["net"]
        self._record_marker_move(directive.id, old_xy, new_xy,
                                 old_p_net, directive.p_net)
        self._mark_project_dirty()
        self._update_pending_rails()
        self._apply_editor_highlight(
            self._connected_nets(directive.p_net)
            if directive.p_net else set())

    def _record_marker_move(self, marker_id, old_xy, new_xy,
                            old_p_net, new_p_net) -> None:
        """Push a free-marker move onto the undo stack; a fresh edit
        invalidates the redo stack."""
        self._marker_undo.append({
            "op": "move",
            "id": marker_id,
            "old_xy": (float(old_xy[0]), float(old_xy[1])),
            "new_xy": (float(new_xy[0]), float(new_xy[1])),
            "old_p_net": old_p_net,
            "new_p_net": new_p_net,
        })
        self._marker_redo.clear()
        self._update_marker_undo_buttons()

    def _delete_selected_free_marker(self) -> None:
        """Delete the currently selected free marker — Delete / Backspace
        in editor mode. Routes to :meth:`_on_editor_remove` so the same
        undo timeline / record shape is shared with the panel's Remove
        button. Scoped to free markers (per the original feature
        request); no-op outside editor mode or mid-drag."""
        if not self._editor_mode or self._marker_drag is not None:
            return
        sel = self._editor_selection
        if not sel or sel.get("kind") != "free":
            return
        self._on_editor_remove()

    def _undo_marker_action(self) -> None:
        """Undo the most recent free-marker edit (move or delete)."""
        if not self._editor_mode or not self._marker_undo:
            return
        rec = self._marker_undo[-1]
        op = rec.get("op", "move")
        if op == "move":
            d = (self._project.directive_by_id(rec["id"])
                 if self._project else None)
            self._marker_undo.pop()
            if d is None:   # marker was removed — drop the stale record
                self._update_marker_undo_buttons()
                return
            d.anchor_xy = rec["old_xy"]
            d.p_net = rec["old_p_net"]
            self._marker_redo.append(rec)
            self._after_marker_undo_redo(d)
            return
        if op == "delete":
            self._marker_undo.pop()
            if self._project is None:
                self._update_marker_undo_buttons()
                return
            d = rec["directive"]
            # Reinsert at the original position so z-order is preserved;
            # clamp if the list has shrunk in the meantime.
            idx = min(max(int(rec.get("index", 0)), 0),
                      len(self._project.editor_directives))
            self._project.editor_directives.insert(idx, d)
            self._marker_redo.append(rec)
            self._after_marker_undo_redo(d, rec.get("prev_selection"))

    def _redo_marker_action(self) -> None:
        """Redo the most recently undone free-marker edit."""
        if not self._editor_mode or not self._marker_redo:
            return
        rec = self._marker_redo[-1]
        op = rec.get("op", "move")
        if op == "move":
            d = (self._project.directive_by_id(rec["id"])
                 if self._project else None)
            self._marker_redo.pop()
            if d is None:
                self._update_marker_undo_buttons()
                return
            d.anchor_xy = rec["new_xy"]
            d.p_net = rec["new_p_net"]
            self._marker_undo.append(rec)
            self._after_marker_undo_redo(d)
            return
        if op == "delete":
            self._marker_redo.pop()
            if self._project is None:
                self._update_marker_undo_buttons()
                return
            self._project.remove_directive(rec["id"])
            self._marker_undo.append(rec)
            self._editor_selection = None
            self._mark_project_dirty()
            self._update_pending_rails()
            self._update_marker_undo_buttons()
            self._apply_editor_highlight(set())
            self._populate_editor_form()

    def _after_marker_undo_redo(self, directive, selection=None) -> None:
        """Shared tail of undo / redo — reselect the directive and refresh
        the viewport + panel. ``selection`` lets a delete-undo restore the
        prior selection state (a component-bound directive re-selects the
        component, with its pin-net union driving the highlight). When
        ``None`` (the move case) the directive is selected as a free
        marker and the highlight comes from its own net."""
        if selection is None:
            self._editor_selection = {"kind": "free", "id": directive.id}
            highlight = (self._connected_nets(directive.p_net)
                         if directive.p_net else set())
        else:
            sel = dict(selection)
            self._editor_selection = sel
            if sel.get("kind") == "component":
                highlight = set()
                for n in sel.get("nets") or []:
                    highlight |= self._connected_nets(n)
            elif sel.get("kind") == "free":
                highlight = (self._connected_nets(directive.p_net)
                             if directive.p_net else set())
            else:
                highlight = set()
        self._mark_project_dirty()
        self._update_pending_rails()
        self._update_marker_undo_buttons()
        self._apply_editor_highlight(highlight)
        self._populate_editor_form()

    def _update_marker_undo_buttons(self) -> None:
        """Enable / disable the panel's marker undo / redo buttons to
        match the stacks (no-op when the form isn't showing them)."""
        btn = getattr(self, "_ef_undo_move", None)
        if btn is not None:
            try:
                btn.setEnabled(bool(self._marker_undo))
            except RuntimeError:
                pass
        btn = getattr(self, "_ef_redo_move", None)
        if btn is not None:
            try:
                btn.setEnabled(bool(self._marker_redo))
            except RuntimeError:
                pass

    def _sync_free_marker_coord_fields(self, x: float, y: float) -> None:
        """Push ``(x, y)`` into the panel's X / Y text boxes without
        firing the on-copper check (``setText`` raises no editingFinished)."""
        for attr, val in (("_ef_loc_x", x), ("_ef_loc_y", y)):
            box = getattr(self, attr, None)
            if box is None:
                continue
            try:
                box.setText(f"{val:.4f}")
            except RuntimeError:
                pass   # widget rebuilt out from under us — harmless

    def _on_free_marker_coord_edited(self) -> None:
        """Check an X / Y text-box edit once focus leaves the box: the new
        location must sit on copper of the marker's fixed layer. If not,
        warn the user and revert to the previous location."""
        if self._suppress_coord_check:
            return
        sel = self._editor_selection
        if not sel or sel.get("kind") != "free":
            return
        d = self._directive_for_selection()
        if d is None or d.anchor_xy is None:
            return
        old_xy = (float(d.anchor_xy[0]), float(d.anchor_xy[1]))
        try:
            nx = float(self._ef_loc_x.text())
            ny = float(self._ef_loc_y.text())
        except (ValueError, RuntimeError):
            self._sync_free_marker_coord_fields(*old_xy)
            return
        if abs(nx - old_xy[0]) < 1e-7 and abs(ny - old_xy[1]) < 1e-7:
            return   # no real change
        if not self._point_on_marker_layer(d, nx, ny):
            self._suppress_coord_check = True
            try:
                QMessageBox.warning(
                    self, "Location not on copper",
                    f"({nx:g}, {ny:g}) mm is not on copper of layer "
                    f"{d.layer or '?'}.\n\n"
                    "The free marker location is being reverted back to "
                    f"({old_xy[0]:g}, {old_xy[1]:g}) mm.",
                )
            finally:
                self._suppress_coord_check = False
            self._sync_free_marker_coord_fields(*old_xy)
            return
        self._commit_marker_move(d, old_xy, d.p_net, (nx, ny))
        # Targeted refresh only — rebuilding the whole form here would
        # delete the QLineEdit mid-signal and steal focus while the user
        # is still tabbing between the X / Y boxes.
        self._sync_free_marker_coord_fields(
            float(d.anchor_xy[0]), float(d.anchor_xy[1]))
        self._refresh_free_marker_net_combo()

    def _refresh_free_marker_net_combo(self) -> None:
        """Resync the form's net picker to the selected free marker's
        current ``p_net`` — used after a move re-derived the net, so the
        combo (and a subsequent Apply) reflect the copper it now sits on.
        A no-op when the form isn't currently showing the picker."""
        combo = getattr(self, "_ef_pnet", None)
        d = self._directive_for_selection()
        if combo is None or d is None:
            return
        try:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(self._form_p_nets(self._editor_selection))
            if d.p_net:
                self._set_combo(combo, d.p_net)
            combo.blockSignals(False)
        except RuntimeError:
            pass   # form rebuilt out from under us — harmless

    def _editor_marker_groups(self) -> list:
        """MarkerGroups for the placed editor directives — drawn only in
        editor mode so they don't clutter the normal viewer. The directive
        matching the current selection is emphasised: a 30%-larger glyph
        with a 30%-thicker outline, wrapped in a yellow selection box, so
        the selected source / sink stands out from the rest."""
        if not self._editor_mode or self._project is None:
            return []
        selected = self._directive_for_selection()
        sel_id = selected.id if selected is not None else None
        # Keyed (role, is_selected, is_n_side): the selected marker lands
        # in its own group (larger / thicker), and the N-side pins land in
        # theirs so they can carry the green outline.
        by_key: dict[tuple[str, bool, bool],
                     list[tuple[float, float]]] = {}
        sel_pts: list[tuple[float, float]] = []
        for d in self._project.editor_directives:
            # Pin points split into P-side and N-side.
            p_pts: list[tuple[float, float]] = []
            n_pts: list[tuple[float, float]] = []
            if d.kind == "free" and d.anchor_xy is not None:
                p_pts = [(float(d.anchor_xy[0]), float(d.anchor_xy[1]))]
            elif d.kind == "component":
                # One marker per component pin on the directive's net(s),
                # mirroring the solved-directive pin markers. Falls back to
                # a single glyph at the component centre when no pad matches.
                p_pts = self._component_pad_points(d.designator, [d.p_net])
                if not d.single_net and d.n_net:
                    n_pts = self._component_pad_points(
                        d.designator, [d.n_net])
                if not p_pts and not n_pts:
                    ctr = self._component_center(d.designator)
                    if ctr is None:
                        continue
                    p_pts = [ctr]
            else:
                continue
            is_sel = d.id == sel_id
            for is_n_side, pts in ((False, p_pts), (True, n_pts)):
                if pts:
                    by_key.setdefault(
                        (d.role, is_sel, is_n_side), []).extend(pts)
            if is_sel:
                sel_pts.extend(p_pts)
                sel_pts.extend(n_pts)
        # Outline width shared by the selected marker and its box.
        sel_edge_w = _EDITOR_MARKER_EDGE_W * _EDITOR_MARKER_SELECT_SCALE
        groups = []
        for (role, is_sel, is_n_side), pts in by_key.items():
            if not pts:
                continue
            # Match the solved-directive markers — red up-triangle SOURCE,
            # blue down-triangle SINK (see _ROLE_MARKER_STYLE).
            style = self._ROLE_MARKER_STYLE.get(
                role, {"symbol": "star", "color": "#ffffff", "size": 18})
            emph = _EDITOR_MARKER_SELECT_SCALE if is_sel else 1.0
            groups.append(MarkerGroup(
                xs=np.array([p[0] for p in pts], dtype=np.float64),
                ys=np.array([p[1] for p in pts], dtype=np.float64),
                zs=np.zeros(len(pts), dtype=np.float64),
                color=style["color"],
                symbol=style["symbol"],
                size=int(round((style["size"] + 4) * emph)),
                edge_color=(_N_NET_MARKER_EDGE if is_n_side else "#101010"),
                edge_width=sel_edge_w if is_sel else _EDITOR_MARKER_EDGE_W,
            ))
        # Yellow selection box around the emphasised marker — an unfilled
        # rectangle hugging the enlarged glyph (the *_box symbols draw the
        # triangle's true bounding rect, so no margin / no clipping),
        # outlined at the same thickness as the selected marker's edge.
        if sel_pts and selected is not None:
            sel_style = self._ROLE_MARKER_STYLE.get(
                selected.role, {"symbol": "s", "size": 18})
            box_symbol = {"tri_up": "tri_up_box",
                          "tri_down": "tri_down_box"}.get(
                              sel_style.get("symbol"), "s")
            # Same size as the selected glyph — the box symbol derives the
            # tight rect from it. One box per pin of the selected directive.
            box_px = int(round((sel_style["size"] + 4)
                               * _EDITOR_MARKER_SELECT_SCALE))
            groups.append(MarkerGroup(
                xs=np.array([p[0] for p in sel_pts], dtype=np.float64),
                ys=np.array([p[1] for p in sel_pts], dtype=np.float64),
                zs=np.zeros(len(sel_pts), dtype=np.float64),
                color="transparent",   # unfilled — outline only
                symbol=box_symbol,
                size=box_px,
                edge_color="#ffff00",
                edge_width=sel_edge_w,
            ))
        return groups

    def _refresh_editor_selection(self) -> None:
        """Push the selected component's world-space bounding box to the
        GL viewer for its yellow selection box — the component-level twin
        of the source / sink marker's selection box. Cleared when nothing,
        or a non-component, is selected. Called every render so it stays
        in sync without scattered set / clear calls."""
        bbox = None
        sel = self._editor_selection
        if self._editor_mode and sel and sel.get("kind") == "component":
            b = sel.get("bbox")
            if b and len(b) == 4:
                bbox = (float(b[0]), float(b[1]),
                        float(b[2]), float(b[3]))
        self._gl_viewer.set_editor_selection_bbox(bbox)

    # --- Editor mode: pending rails + resolve -------------------------------

    def _rail_name_for(self, nets) -> str:
        """Pick a display name for a rail group — prefer a '+'-prefixed
        net, then a non-ground net, alphabetical within each tier."""
        def rank(n: str) -> tuple[int, str]:
            if n.startswith("+"):
                return (0, n)
            if n.lower() in {"0v", "gnd", "ground", "vss"}:
                return (2, n)
            return (1, n)
        ordered = sorted((n for n in nets if n), key=rank)
        return ordered[0] if ordered else "(rail)"

    def _editor_pending_rails(self) -> dict[str, list[str]]:
        """Connected-copper groups carrying at least one editor SOURCE and
        one editor SINK that the current solution has not solved — i.e.
        rails the user has defined but not yet resolved. Returns
        ``{rail_name: [member nets]}``."""
        if self._project is None:
            return {}
        groups: list[dict] = []
        for d in self._project.editor_directives:
            if d.role not in ("SOURCE", "SINK") or not d.p_net:
                continue
            conn = self._connected_nets(d.p_net)
            target = None
            for g in groups:
                if g["nets"] & conn:
                    target = g
                    break
            if target is None:
                target = {"nets": set(), "roles": set()}
                groups.append(target)
            target["nets"] |= conn
            target["roles"].add(d.role)
        solved = set(self._rail_names)
        pending: dict[str, list[str]] = {}
        for g in groups:
            if "SOURCE" in g["roles"] and "SINK" in g["roles"]:
                name = self._rail_name_for(g["nets"])
                if name not in solved:
                    pending[name] = sorted(g["nets"])
        return pending

    def _update_pending_rails(self) -> None:
        """Rebuild the pending-rail rows in the Rails list. Pending rails
        are styled distinctly (italic, greyed, '(unsolved)') with a
        disabled eye — not viewable until a resolve has run."""
        if not hasattr(self, "rail_list"):
            return
        for item in getattr(self, "_pending_rail_items", []):
            row = self.rail_list.row(item)
            if row >= 0:
                self.rail_list.takeItem(row)
        self._pending_rail_items: list = []
        self._pending_rails = self._editor_pending_rails()
        t = _T()
        for name in sorted(self._pending_rails):
            eye = EyeButton(visible=False)
            eye.setEnabled(False)
            eye.setToolTip("Unsolved rail — press Resolve to compute it.")
            row = self._build_layer_row_widget(
                eye, swatch_color=None,
                label_text=f"{name}  (unsolved)", bold=False,
            )
            row.setStyleSheet(
                f"color: {t['fg_muted']}; font-style: italic;"
            )
            item = QListWidgetItem()
            item.setFlags(Qt.ItemIsEnabled)
            self.rail_list.addItem(item)
            item.setSizeHint(row.sizeHint())
            self.rail_list.setItemWidget(item, row)
            self._pending_rail_items.append(item)
        approx = self.rail_list.sizeHintForRow(0) or 22
        self.rail_list.setFixedHeight(self.rail_list.count() * approx + 6)

    def _unnamed_copper_directives(self) -> list:
        """Editor directives whose ``p_net`` / ``n_net`` is still the
        ``"(none)"`` sentinel at resolve time — and which the user has
        NOT covered with an editor-mode :class:`CopperName` rename. The
        solver has no rail for ``"(none)"`` (and even if it did, every
        electrically-disjoint unnamed copper piece would collapse onto
        the same lumped net), so resolving with one of these directives
        silently drops it from the result.

        Before flagging, this method promotes any free-marker directive
        whose anchor lands on a polygon that now carries a
        :class:`CopperName` — the user effectively named the copper but
        the placement-time override missed it for some reason (marker
        placed before the rename, or the rename was applied to a
        slightly different click point on the piece). The promoted
        ``p_net`` carries forward to the solve, so the rail isn't
        dropped. Directives still on truly-unnamed copper after this
        promotion are returned for the warning dialog."""
        if self._project is None:
            return []
        out = []
        promoted = 0
        for d in self._project.editor_directives:
            # Auto-promote a free marker whose p_net is "(none)" if a
            # CopperName rename now applies to its anchor.
            if (d.p_net == "(none)" and d.kind == "free"
                    and d.anchor_xy is not None
                    and d.layer_id is not None):
                c = self._copper_name_at(
                    float(d.anchor_xy[0]), float(d.anchor_xy[1]),
                    int(d.layer_id))
                if c is not None:
                    d.p_net = c.name
                    promoted += 1
            if d.p_net == "(none)" or d.n_net == "(none)":
                out.append(d)
        if promoted:
            self._mark_project_dirty()
        return out

    def _on_resolve_clicked(self) -> None:
        """Re-run the solver with the current editor directives applied,
        reusing the in-memory design info (no cache read, no Altium
        re-extraction). Falls back to the design-info cache only when this
        viewer has no in-memory LoadedProject (e.g. opened from a pickle)."""
        if self._project is None or not self._project.editor_directives:
            QMessageBox.information(
                self, "Nothing to resolve",
                "There are no editor directives to solve yet.",
            )
            return
        unnamed = self._unnamed_copper_directives()
        if unnamed:
            lines = []
            for d in unnamed:
                if d.kind == "free" and d.anchor_xy is not None:
                    where = (f"free {d.role} at "
                             f"({d.anchor_xy[0]:g}, {d.anchor_xy[1]:g}) mm"
                             f" on {d.layer or '?'}")
                else:
                    where = f"{d.role} on {d.designator or '?'}"
                lines.append(f"  • {where}")
            QMessageBox.warning(
                self, "Name the copper first",
                "These editor directives are placed on copper that has no "
                "net name:\n\n"
                + "\n".join(lines)
                + "\n\nThe solver can't include an unnamed net in the "
                "result — and any other unnamed copper on the board would "
                "be lumped onto the same rail.\n\n"
                "Fix this one of two ways:\n"
                "  • Click the copper in editor mode and use the "
                "<b>Unnamed copper</b> form on the right to give it a "
                "name here, then click Apply and Resolve again.\n"
                "  • Or name the net in Altium and re-import the design "
                "before resolving.",
            )
            return
        prjpcb = self.metadata.get("prjpcb_path") if self.metadata else None
        if not prjpcb:
            QMessageBox.warning(
                self, "Can't resolve",
                "This solution isn't linked to an Altium project, so the "
                "design info needed for a re-solve isn't available.",
            )
            return
        pcbdoc = self.metadata.get("pcbdoc_path") if self.metadata else None
        self._solve_settings.apply_to_modules()
        self._start_solve_worker(
            Path(prjpcb), self._solve_settings,
            self._via_current_warn_a, self._display_percentile_high,
            pcbdoc_selector=str(pcbdoc) if pcbdoc else None,
            use_design_cache=True,
            try_solve_cache_first=False,
            editor_directives=list(self._project.editor_directives),
            copper_names=list(self._project.copper_names),
            loaded_project=self._loaded_project,
            dialog_title="Resolving with editor changes",
            dialog_text=(
                "Re-solving with your editor changes…\n"
                "Reusing the cached design info (no re-extraction)."
            ),
            # ~3.20x Qt's auto-sized width — the standard solve dialog's
            # 1.44x, widened further for the longer editor-changes message
            # and then another 15% wider on top of that.
            dialog_width_scale=3.199392,
        )

    def _on_gl_clicked(self, _world_x: float, _world_y: float) -> None:
        """Left-click in the viewport (no drag). In editor mode this drives
        component / copper selection and free-marker placement; in viewer
        mode it clears the Vias-tab jump highlight and runs the
        copper-primitive picker — a hit opens the properties panel, a
        miss closes any open one."""
        if self._editor_mode:
            self._on_editor_click(_world_x, _world_y)
            return
        if self._highlight_via_xy is not None:
            self._highlight_via_xy = None
            self._render()
        hit = self._primitive_at_point(_world_x, _world_y)
        if hit is not None:
            self._select_copper_primitive(hit)
        else:
            self._clear_copper_selection()

    def _on_legend_row_clicked(self, key: str) -> None:
        """Toggle visibility of the marker category whose legend row was
        clicked. The legend row itself stays in the chip — it just flips
        between normal and slashed (matching the off-state of the eye
        icons used elsewhere). A full re-render rebuilds the marker
        groups with the updated hidden set."""
        if key in self._hidden_legend_keys:
            self._hidden_legend_keys.discard(key)
        else:
            self._hidden_legend_keys.add(key)
        self._render()

    # --- Voltage-difference (Shift-drag) tool -------------------------------
    #
    # When the user holds Shift while hovering copper in either Voltage or
    # Voltage Drop mode, an anchor is captured at the cursor and a thin
    # white line is drawn from there to the live cursor position. The
    # status-bar probe label gains a "Difference = X V" suffix that
    # reports the live cursor's voltage minus the anchor voltage. The
    # over-copper / mode checks happen exactly once on shift-press — once
    # the tool is active, the line tracks the cursor regardless of where
    # it ends up.

    def eventFilter(self, obj, event) -> bool:
        """Application-wide hook for Shift press/release. Filter is
        installed on the QApplication so we see modifier-key events
        regardless of which child widget has focus — required because
        Qt only dispatches key events to the focused widget by default,
        and the GL viewer doesn't get focus until the user clicks it.

        Auto-repeat suppression keeps a held key from re-anchoring on
        every OS keyboard-repeat tick. The window-deactivate branch is
        a safety net for Alt-Tab — the user releases Shift in another
        window, our window never sees the release event, so we treat
        any deactivate while the tool is active as an implicit release.
        """
        et = event.type()
        if et == QEvent.KeyPress and event.key() == Qt.Key_Shift:
            if (not event.isAutoRepeat()
                    and self.isActiveWindow()):
                self._on_shift_pressed()
        elif (et == QEvent.KeyPress and event.key() == Qt.Key_Escape
              and not event.isAutoRepeat() and self.isActiveWindow()
              and self._copper_selection is not None
              and not self._editor_mode):
            # Escape clears the viewer-mode copper selection (mirrors the
            # "click off into empty space" close path). Consume the event
            # so it doesn't bubble up and close the window.
            self._clear_copper_selection()
            return True
        elif et == QEvent.KeyRelease and event.key() == Qt.Key_Shift:
            if (not event.isAutoRepeat()
                    and self._measure_anchor_xy is not None):
                self._on_shift_released()
        elif et == QEvent.WindowDeactivate and obj is self:
            if self._measure_anchor_xy is not None:
                self._on_shift_released()
        elif et == QEvent.Resize and obj is getattr(self, "_gl_viewer", None):
            # Keep the colour-scale overlay pinned bottom-left, the
            # editor-mode buttons pinned top-left, and the editor /
            # copper-properties panel pinned right edge as the GL viewer
            # resizes (window resize, sidebar collapse, etc.).
            self._position_scale_overlay()
            self._position_editor_overlays()
            self._position_editor_panel()
        return False

    def _position_scale_overlay(self) -> None:
        """Pin the heatmap colour-scale strip to the GL viewer's
        bottom-left corner. Safe to call before the overlay / viewer
        exist (no-op) — wired to every GL-viewer resize via
        :meth:`eventFilter`."""
        bar = getattr(self, "_scale_overlay", None)
        gl = getattr(self, "_gl_viewer", None)
        if bar is None or gl is None:
            return
        margin = 12
        y = gl.height() - bar.height() - margin
        bar.move(margin, max(margin, y))
        bar.raise_()

    def _position_editor_panel(self) -> None:
        """Pin the right-hand editor / copper-properties panel to the GL
        viewer's right edge, full height (minus margins). The panel is a
        child of the GL viewer so it floats over the viewport rather than
        consuming layout space — that keeps the visible copper from
        shifting sideways when the panel appears / disappears. Wired to
        every GL-viewer resize via :meth:`eventFilter`."""
        panel = getattr(self, "_editor_panel", None)
        gl = getattr(self, "_gl_viewer", None)
        if panel is None or gl is None:
            return
        margin = 0
        w = panel.width()
        h = max(0, gl.height() - 2 * margin)
        panel.setFixedHeight(max(h, 50))
        panel.move(gl.width() - w - margin, margin)
        panel.raise_()

    def closeEvent(self, event) -> None:
        """Uninstall the application-wide Shift filter on window close
        so QApplication doesn't keep dispatching events at a dangling
        Python object."""
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(event)

    def _on_shift_pressed(self) -> None:
        mode = self.mode_combo.currentText()
        if mode not in ("Voltage", _VOLTAGE_DROP_MODE):
            return
        # 2D only. In 3D the anchor would sit on a specific layer's z
        # plane but the cursor's "current voltage" comes from a ray-pick
        # that can land on a different layer — the resulting difference
        # would mix two unrelated readings.
        if self.view_3d_box.isChecked():
            return
        if not self._layer_probes:
            return
        # Probe the live cursor position (rather than the last hover
        # event) so the anchor reflects exactly where the user pressed
        # Shift, even if the throttled hover handler hasn't fired since.
        local = self._gl_viewer.mapFromGlobal(QCursor.pos())
        if not (0 <= local.x() < self._gl_viewer.width()
                and 0 <= local.y() < self._gl_viewer.height()):
            return
        hit = self._probe_voltage_at_pixel(local.x(), local.y())
        if hit is None:
            return
        voltage, world_xy = hit
        self._measure_anchor_xy = world_xy
        self._measure_anchor_voltage = float(voltage)
        # Seed the line with a zero-length segment so it appears at the
        # anchor immediately; subsequent mouse moves grow the far endpoint.
        self._gl_viewer.set_measurement_line(
            world_xy[0], world_xy[1], world_xy[0], world_xy[1],
        )

    def _on_shift_released(self) -> None:
        if self._measure_anchor_xy is None:
            return
        self._measure_anchor_xy = None
        self._measure_anchor_voltage = None
        self._gl_viewer.clear_measurement_line()

    def _probe_voltage_at_pixel(
        self, x_px: float, y_px: float,
    ) -> tuple[float, tuple[float, float]] | None:
        """Return ``(voltage_V, (world_x, world_y))`` for the cursor at
        ``(x_px, y_px)`` in widget-logical pixels, or ``None`` if there's
        no copper / no usable voltage at that point. Uses the same FEM
        probe + stub-fallback flow as :meth:`_on_gl_mouse_hovered` so
        anything the status bar shows a voltage for is a valid anchor."""
        is_3d = self.view_3d_box.isChecked()
        if is_3d:
            hit = self._probe_at_point_3d(x_px, y_px)
            if hit is not None:
                v, lp = hit
                z = self._layer_z_for(lp.get("physical", ""))
                wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
                return float(v), (wx, wy)
            stub_hit = self._probe_at_stub_3d(x_px, y_px)
            if stub_hit is not None and stub_hit[0] is not None:
                phys = stub_hit[1].get("physical", "")
                z = self._layer_z_for(phys)
                wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
                return float(stub_hit[0]), (wx, wy)
            return None
        wx, wy = self._gl_viewer.screen_to_world(x_px, y_px)
        hit = self._probe_at_point(wx, wy)
        if hit is not None:
            return float(hit[0]), (wx, wy)
        stub_hit = self._probe_at_stub(wx, wy)
        if stub_hit is not None and stub_hit[0] is not None:
            return float(stub_hit[0]), (wx, wy)
        return None

    def _apply_measurement_difference(
        self, world_x: float, world_y: float,
        current_voltage: float | None,
    ) -> str:
        """If the Shift-drag tool is active, update the overlay line to
        span from the anchor to ``(world_x, world_y)`` and return the
        ``"   Difference = X V"`` suffix to append to the status-bar
        text. Returns an empty string when no measurement is in
        progress, or cancels the measurement if the active mode is no
        longer Voltage / Voltage Drop."""
        if self._measure_anchor_xy is None or self._measure_anchor_voltage is None:
            return ""
        if (self.mode_combo.currentText() not in ("Voltage", _VOLTAGE_DROP_MODE)
                or self.view_3d_box.isChecked()):
            self._on_shift_released()
            return ""
        ax, ay = self._measure_anchor_xy
        self._gl_viewer.set_measurement_line(ax, ay, world_x, world_y)
        if current_voltage is None or not np.isfinite(current_voltage):
            return "   Difference = (n/a)"
        diff = float(current_voltage) - self._measure_anchor_voltage
        return f"   Difference = {diff:.5g} V"

    def _on_gl_mouse_hovered(self, world_x: float, world_y: float,
                             inside: bool) -> None:
        """GLMeshViewer reported a mouse-move at world coords (mm).

        Throttled to :data:`HOVER_THROTTLE_S` so a high-DPI mouse doesn't
        flood the CPU. Skipped during drag (the GLMeshViewer is panning,
        so probe values would be meaningless mid-drag anyway).

        2D mode: the signal's (world_x, world_y) is the answer — walk the
        cached probe list top-first and report the first layer whose
        copper covers it. 3D mode: a single screen pixel maps to a whole
        camera ray, so the same pixel hits different (x, y) at each
        layer's z. We re-unproject per layer using the GL viewer's MVP
        inverse, and report the topmost layer whose copper covers the
        ray's intersection with that layer's z plane.
        """
        if QApplication.mouseButtons() != Qt.NoButton:
            return
        if not inside:
            self.probe_label_widget.setText("Hover the plot to probe values")
            self._hide_cursor_tooltip()
            return
        now = time.monotonic()
        # Use the tighter throttle while the cursor tooltip is on — it
        # visibly tracks the mouse, so any stutter is jarring. The bottom
        # probe label is more forgiving, so the default 30 Hz is fine.
        throttle = (CURSOR_TOOLTIP_THROTTLE_S
                    if self.cursor_tooltip_box.isChecked()
                    else HOVER_THROTTLE_S)
        if now - self._last_probe_at < throttle:
            return
        self._last_probe_at = now
        # Nothing to probe at all — no FEM rail and no visible physical
        # copper. Keep the initial-state hint rather than reporting
        # "(no copper)" over a blank viewport.
        has_visible_all_copper = (
            self.metadata is not None
            and bool(self.metadata.get("all_copper"))
            and bool(self._visible_all_copper_layer_ids())
        )
        if not self._layer_probes and not has_visible_all_copper:
            self.probe_label_widget.setText("Hover the plot to probe values")
            self._hide_cursor_tooltip()
            return
        hit = None
        if self.view_3d_box.isChecked():
            px, py = self._gl_viewer.last_hover_pixel()
            hit = self._probe_at_point_3d(px, py)
            if hit is not None:
                _, lp = hit
                z = self._layer_z_for(lp.get("physical", ""))
                world_x, world_y = self._gl_viewer.screen_to_world_at_z(
                    px, py, z)
        elif self._layer_probes:
            hit = self._probe_at_point(world_x, world_y)
        if hit is None:
            # Fall back to stub (no-current copper) probe, then to the
            # all-copper overlay so non-rail physical copper still
            # reports its net.
            is_3d = self.view_3d_box.isChecked()
            if is_3d:
                px_, py_ = self._gl_viewer.last_hover_pixel()
                stub_hit = self._probe_at_stub_3d(px_, py_)
            else:
                stub_hit = self._probe_at_stub(world_x, world_y)
            via_row = self._pick_hovered_via(world_x, world_y)
            via_part = (self._format_via_hover_text(via_row)
                        if via_row is not None else "")
            marker_row = self._pick_hovered_marker(world_x, world_y)
            marker_part = (self._format_marker_hover_text(marker_row)
                           if marker_row is not None else "")
            if stub_hit is not None:
                v_stub, stub_info = stub_hit
                net_part = (f"   Net = {stub_info['net']}"
                            if stub_info.get("net") else "")
                layer_part = (f"   Layer = {stub_info['physical']}"
                              if stub_info.get("physical") else "")
                v_part = (f"   Voltage ≈ {v_stub:.5g} V   (no current)"
                          if v_stub is not None else "   (no current)")
                diff_part = self._apply_measurement_difference(
                    world_x, world_y, v_stub,
                )
                self.probe_label_widget.setText(
                    f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm"
                    f"{v_part}{net_part}{layer_part}"
                    f"{via_part}{marker_part}{diff_part}"
                )
                self._update_cursor_tooltip((v_stub, stub_info),
                                            via_row=via_row,
                                            marker_row=marker_row)
                return
            if is_3d:
                px_, py_ = self._gl_viewer.last_hover_pixel()
                bare_hit = self._all_copper_at_point_3d(px_, py_)
                if bare_hit is not None:
                    z = self._layer_z_for(bare_hit.get("physical", ""))
                    world_x, world_y = self._gl_viewer.screen_to_world_at_z(
                        px_, py_, z)
            else:
                bare_hit = self._all_copper_at_point(world_x, world_y)
            if bare_hit is not None:
                bare_hit = dict(bare_hit)
                bare_hit["is_bare"] = True
                # An unnamed copper polygon the user has renamed in
                # editor mode reports the new name with a "[pending
                # resolve]" suffix until the next Resolve actually
                # rebuilds the solution against the renamed net.
                if bare_hit.get("net") == "(none)":
                    c = self._copper_name_at(
                        world_x, world_y, bare_hit.get("layer_id"))
                    if c is not None:
                        bare_hit["net"] = f"{c.name}  [pending resolve]"
                net_part = (f"   Net = {bare_hit['net']}"
                            if bare_hit.get("net") else "")
                layer_part = (f"   Layer = {bare_hit['physical']}"
                              if bare_hit.get("physical") else "")
                diff_part = self._apply_measurement_difference(
                    world_x, world_y, None,
                )
                self.probe_label_widget.setText(
                    f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm"
                    f"{net_part}{layer_part}"
                    f"{via_part}{marker_part}{diff_part}"
                )
                self._update_cursor_tooltip((None, bare_hit),
                                            via_row=via_row,
                                            marker_row=marker_row)
                return
            diff_part = self._apply_measurement_difference(
                world_x, world_y, None,
            )
            self.probe_label_widget.setText(
                f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm   "
                f"(no copper at this point){via_part}{marker_part}{diff_part}"
            )
            self._update_cursor_tooltip(None, via_row=via_row,
                                        marker_row=marker_row)
            return
        v_float, lp = hit
        net_part = f"   Net = {lp['net']}" if lp.get("net") else ""
        layer_part = (f"   Layer = {lp['physical']}"
                      if lp.get("physical") else "")
        via_row = self._pick_hovered_via(world_x, world_y)
        via_part = (self._format_via_hover_text(via_row)
                    if via_row is not None else "")
        marker_row = self._pick_hovered_marker(world_x, world_y)
        marker_part = (self._format_marker_hover_text(marker_row)
                       if marker_row is not None else "")
        diff_part = self._apply_measurement_difference(
            world_x, world_y, v_float,
        )
        self.probe_label_widget.setText(
            f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm   "
            f"{self._probe_label} = {v_float:.5g} {self._probe_unit}"
            f"{net_part}{layer_part}{via_part}{marker_part}{diff_part}"
        )
        self._update_cursor_tooltip((v_float, lp), via_row=via_row,
                                    marker_row=marker_row)

    def _on_cursor_tooltip_toggled(self, checked: bool) -> None:
        """Hide the tooltip when turned off; immediately show it at the
        current cursor position when turned on, so the user doesn't have
        to wiggle the mouse to "wake it up"."""
        if not checked:
            self._hide_cursor_tooltip()
            return
        # Synthesize a hover event at the cursor's current position so
        # the tooltip appears immediately. Skip silently if the cursor
        # isn't over the GL viewport.
        local = self._gl_viewer.mapFromGlobal(QCursor.pos())
        if not (0 <= local.x() < self._gl_viewer.width()
                and 0 <= local.y() < self._gl_viewer.height()):
            return
        # Seed the GL viewer's last-hover pixel so the 3D-mode per-layer
        # picker has a fresh value to unproject (it normally lags one
        # real mouse-move behind, but here we haven't had one yet).
        self._gl_viewer.set_last_hover_pixel(local.x(), local.y())
        wx, wy = self._gl_viewer.screen_to_world(local.x(), local.y())
        # Bypass the throttle so the synthetic probe runs even if a
        # real hover fired within the last few ms.
        self._last_probe_at = 0.0
        self._on_gl_mouse_hovered(wx, wy, True)

    # --- Cursor tooltip (custom floating label) -----------------------------
    #
    # Qt's QToolTip has built-in re-use / debouncing logic that makes
    # showText calls stutter when the content barely changes from one
    # frame to the next — the tooltip "sticks" instead of tracking the
    # cursor. We sidestep that entirely by drawing our own frameless
    # label that we move() ourselves every hover event.

    def _ensure_cursor_tooltip_label(self) -> QLabel:
        """Lazily build the floating QLabel used as the cursor tooltip."""
        label = getattr(self, "_cursor_tooltip_label", None)
        if label is not None:
            return label
        # Qt.ToolTip = frameless, no focus, stays above its parent window.
        # WA_TransparentForMouseEvents so we never steal hover events
        # from the GL viewer when the label happens to slide under the
        # cursor at the screen edge.
        label = QLabel(self._gl_viewer, Qt.ToolTip | Qt.FramelessWindowHint)
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        label.setAttribute(Qt.WA_ShowWithoutActivating, True)
        label.setFocusPolicy(Qt.NoFocus)
        _t = _T()
        label.setStyleSheet(
            "QLabel {"
            f" background-color: {_t['bg']};"
            f" color: {_t['fg']};"
            f" border: 1px solid {_t['border']};"
            " padding: 4px 8px;"
            " font-family: Consolas, monospace;"
            " font-size: 9pt;"
            "}"
        )
        label.hide()
        self._cursor_tooltip_label = label
        return label

    def _hide_cursor_tooltip(self) -> None:
        label = getattr(self, "_cursor_tooltip_label", None)
        if label is not None and label.isVisible():
            label.hide()

    def _update_cursor_tooltip(
        self, hit: tuple[float | None, dict] | None,
        via_row: dict | None = None,
        marker_row: dict | None = None,
    ) -> None:
        """Show/move/hide the at-cursor tooltip based on the checkbox.
        ``hit`` is ``(value, probe)`` from :meth:`_probe_at_point` (solved
        copper) or ``_probe_at_stub`` (no-current copper), or ``None`` if
        the cursor is over bare substrate.  For stub hits ``value`` may be
        ``None`` and the probe dict carries ``is_stub=True``.

        ``via_row`` is the Vias-tab row dict for the via under the
        cursor, or ``None``. When non-None, the tooltip appends per-via
        current / voltage lines — and stays visible even when ``hit`` is
        ``None`` (e.g. in 3D when the user is hovering a via barrel
        between visible copper layers, where there is no per-pixel
        copper probe to drive the rest of the tooltip).

        ``marker_row`` is the SOURCE/SINK marker row under the cursor,
        or ``None``. Same any-of-three rule applies — the tooltip stays
        visible while the user is parked on a marker, even when not
        over copper."""
        if not self.cursor_tooltip_box.isChecked() \
                or (hit is None and via_row is None and marker_row is None):
            self._hide_cursor_tooltip()
            return
        lines: list[str] = []
        if hit is not None:
            v_float, lp = hit
            if lp.get("is_bare"):
                # Non-rail copper from the all-copper overlay — no FEM
                # solve runs here, so just name the net / layer.
                pass
            elif lp.get("is_stub"):
                if v_float is not None:
                    lines.append(f"Voltage ≈ {v_float:.5g} V")
                lines.append("(no current)")
            else:
                lines.append(
                    f"{self._probe_label}: {v_float:.5g} {self._probe_unit}"
                )
            if lp.get("net"):
                lines.append(f"Net: {lp['net']}")
            if lp.get("physical"):
                lines.append(f"Layer: {lp['physical']}")
        if via_row is not None:
            cur = via_row.get("current")
            if cur is not None and np.isfinite(cur):
                lines.append(f"Via current: {cur:.4g} A")
        if marker_row is not None:
            lines.extend(self._format_marker_tooltip_lines(marker_row))
        if not lines:
            self._hide_cursor_tooltip()
            return
        label = self._ensure_cursor_tooltip_label()
        label.setText("\n".join(lines))
        label.adjustSize()
        # Anchor below-right of the cursor (Windows-cursor convention).
        # Clamp to the current screen so the label stays fully visible
        # when the cursor is near the right / bottom edge.
        gx = QCursor.pos().x() + 16
        gy = QCursor.pos().y() + 20
        screen = label.screen() or QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            gx = min(gx, geo.right() - label.width() - 2)
            gy = min(gy, geo.bottom() - label.height() - 2)
            gx = max(gx, geo.left() + 2)
            gy = max(gy, geo.top() + 2)
        label.move(gx, gy)
        if not label.isVisible():
            label.show()

    def _ensure_interpolator(self, lp: dict) -> _FastTriSampler | None:
        """Return the layer probe's voltage sampler, building it lazily
        on first call.

        Built lazily on first cursor hover (not at render time) so a rail
        toggle never pays for it. :class:`_FastTriSampler` builds in
        ~50–150 ms even on a 300k-triangle GND plane — its predecessor,
        ``LinearTriInterpolator``, took 3–10 s and froze the GUI for
        several seconds the first time the cursor crossed a heavy plane.
        The result is written back to the ``_layer_cache`` entry (via
        ``_cache_key``) so subsequent renders of the same (layer, mode)
        pair reuse the already-built object.
        """
        interp = lp.get("interpolator")
        if interp is not None:
            return interp
        tri = lp.get("triangulation")
        if tri is None:
            return None
        interp = _FastTriSampler(tri, lp["values"])
        lp["interpolator"] = interp
        cache_key = lp.get("_cache_key")
        if cache_key is not None:
            entry = self._layer_cache.get(cache_key)
            if entry is not None:
                entry["interpolator"] = interp
        return interp

    def _probe_at_point(self, x: float, y: float
                        ) -> tuple[float, dict] | None:
        """Walk :attr:`_layer_probes` top-first, return ``(value, probe)``
        for the topmost visible layer whose copper covers (x, y). ``None``
        if the cursor is on bare substrate (or off-mesh) on every layer.
        """
        if not self._layer_probes:
            return None
        pt = _sg.Point(x, y)
        for lp in self._layer_probes:
            prepped = lp.get("prepared_shape")
            if prepped is None:
                continue
            try:
                if not prepped.contains(pt):
                    continue
            except Exception:
                continue
            interp = self._ensure_interpolator(lp)
            if interp is None:
                continue
            sample = interp(x, y)
            try:
                if hasattr(sample, "mask") and \
                        bool(np.ma.getmaskarray(sample).item()):
                    continue
                v = float(sample)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(v):
                continue
            return v, lp
        return None

    def _probe_at_point_3d(self, x_px: float, y_px: float
                            ) -> tuple[float, dict] | None:
        """3D-mode probe: ray-intersect the camera ray with each visible
        layer's z plane (top-first) and return the first hit whose copper
        covers the intersection point. Compensates for perspective so the
        cursor lands on the copper the user is actually looking at,
        regardless of camera angle / vertical-exaggeration."""
        if not self._layer_probes:
            return None
        for lp in self._layer_probes:
            prepped = lp.get("prepared_shape")
            if prepped is None:
                continue
            phys = lp.get("physical", "")
            z = self._layer_z_for(phys)
            wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
            try:
                if not prepped.contains(_sg.Point(wx, wy)):
                    continue
            except Exception:
                continue
            interp = self._ensure_interpolator(lp)
            if interp is None:
                continue
            sample = interp(wx, wy)
            try:
                if hasattr(sample, "mask") and \
                        bool(np.ma.getmaskarray(sample).item()):
                    continue
                v = float(sample)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(v):
                continue
            return v, lp
        return None

    # --- Via hover probe ----------------------------------------------------

    def _get_or_build_via_hover_index(self):
        """Cache the per-via arrays used by hover lookup.

        Built lazily on first hover from the same row list the Vias tab
        uses, so the heavy ``_compute_via_report`` cost is shared. The
        cache is a dict so the 2D path (xy disk test) and the 3D path
        (screen-space ray-cylinder test) can share most state. Returns
        ``None`` when there are no usable via rows.

        ``z_tops`` / ``z_bots`` come from the via's full ``layer_start``
        → ``layer_end`` metadata span, NOT the sampled-layer span in the
        Vias-tab row — so when the user hovers a section of barrel that
        crosses a layer without copper on this net, the 3D ray test
        still hits.
        """
        cached = getattr(self, "_via_hover_index_cache", None)
        if cached is not None:
            return cached
        if self.metadata is None:
            self._via_hover_index_cache = None
            return None
        rows = self._get_or_compute_via_rows()
        if not rows:
            self._via_hover_index_cache = None
            return None
        # Map (x_mm, y_mm, net) → full via dict so we can read the
        # original layer_start/layer_end for the 3D z-span. Rounded to
        # 1 nm to absorb the round-trip-through-pickle float noise.
        via_lookup: dict[tuple[float, float, str], dict] = {}
        for v in self.metadata.get("vias", []):
            net = v.get("net", "")
            if not net:
                continue
            key = (round(float(v.get("x_mm", 0.0)), 6),
                   round(float(v.get("y_mm", 0.0)), 6),
                   net)
            via_lookup[key] = v
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}

        xs_list: list[float] = []
        ys_list: list[float] = []
        radii_list: list[float] = []
        z_top_list: list[float] = []
        z_bot_list: list[float] = []
        rows_keep: list[dict] = []
        for r in rows:
            x = float(r["x_mm"])
            y = float(r["y_mm"])
            net = r.get("net", "")
            key = (round(x, 6), round(y, 6), net)
            v = via_lookup.get(key)
            if v is not None:
                ls = v.get("layer_start")
                le = v.get("layer_end")
                if ls is None or le is None:
                    continue
                lid_top = min(ls, le)   # top = lowest id = nearest z=0
                lid_bot = max(ls, le)
            else:
                # Row-only fallback: use the sampled-layer span.
                layer_ids = r.get("layer_ids") or []
                if len(layer_ids) < 2:
                    continue
                lid_top, lid_bot = layer_ids[0], layer_ids[-1]
            phys_top = id_to_phys.get(lid_top)
            phys_bot = id_to_phys.get(lid_bot)
            if phys_top is None or phys_bot is None:
                continue
            radius = max(float(r.get("diameter_mm") or 0.0) * 0.5, 0.15)
            xs_list.append(x)
            ys_list.append(y)
            radii_list.append(radius)
            z_top_list.append(self._layer_z_for(phys_top))
            z_bot_list.append(self._layer_z_for(phys_bot))
            rows_keep.append(r)

        if not rows_keep:
            self._via_hover_index_cache = None
            return None
        xs = np.array(xs_list, dtype=np.float64)
        ys = np.array(ys_list, dtype=np.float64)
        radii = np.array(radii_list, dtype=np.float64)
        self._via_hover_index_cache = {
            "xs": xs,
            "ys": ys,
            "radii": radii,
            "r2": radii * radii,
            "z_tops": np.array(z_top_list, dtype=np.float64),
            "z_bots": np.array(z_bot_list, dtype=np.float64),
            "rows": rows_keep,
        }
        return self._via_hover_index_cache

    def _format_via_hover_text(self, row: dict) -> str:
        """Format the bottom-bar suffix for a hovered via row. Empty
        string if neither current nor voltage is usable."""
        cur = row.get("current")
        v_top = row.get("v_top")
        parts: list[str] = []
        if cur is not None and np.isfinite(cur):
            parts.append(f"I = {cur:.4g} A")
        if v_top is not None and np.isfinite(v_top):
            parts.append(f"V = {v_top:.5g} V")
        if not parts:
            return ""
        return "   Via: " + "   ".join(parts)

    def _pick_hovered_via(self, world_x: float, world_y: float) -> dict | None:
        """Dispatch the via-hover probe by view mode. Returns the row
        dict for the picked via (the same shape :meth:`_compute_via_report`
        produces), or ``None`` when the cursor isn't over any via."""
        if self.view_3d_box.isChecked():
            return self._pick_hovered_via_3d()
        return self._pick_hovered_via_2d(world_x, world_y)

    def _via_hover_info(self, world_x: float, world_y: float) -> str:
        """Bottom-bar suffix for the picked via, or empty string."""
        row = self._pick_hovered_via(world_x, world_y)
        if row is None:
            return ""
        return self._format_via_hover_text(row)

    def _pick_hovered_via_2d(self, world_x: float,
                              world_y: float) -> dict | None:
        """2D-mode probe: simple disk-containment test. The cursor's
        world (x, y) maps 1:1 to a board point, so any via whose pad
        circle covers it is a hit; the closest center wins."""
        idx = self._get_or_build_via_hover_index()
        if idx is None:
            return None
        xs = idx["xs"]; ys = idx["ys"]; r2 = idx["r2"]; rows = idx["rows"]
        dx = xs - world_x
        dy = ys - world_y
        d2 = dx * dx + dy * dy
        inside = d2 <= r2
        if not inside.any():
            return None
        candidates = np.where(inside)[0]
        best = int(candidates[np.argmin(d2[candidates])])
        return rows[best]

    def _pick_hovered_via_3d(self) -> dict | None:
        """3D-mode probe: ray-pick each via cylinder in screen space.

        Why this is needed: a vertical via barrel viewed from an oblique
        camera lands well off the (x, y) you get by unprojecting the
        cursor at any single z — the cursor on the side of the barrel
        sits at one z while the via's footprint is at another. The
        2D-style ``(wx - vx)² + (wy - vy)² < r²`` check therefore misses.
        Instead we project each via's top and bottom endpoints to
        screen pixels and measure the cursor's distance to the
        resulting screen-space line segment, comparing against the
        via's projected radius.

        Vectorised over all vias with one MVP build + a handful of
        batched matrix multiplies per hover — cheap enough at 30 Hz
        even on a 3 000-via board."""
        idx = self._get_or_build_via_hover_index()
        if idx is None:
            return None
        xs = idx["xs"]; ys = idx["ys"]
        radii = idx["radii"]; rows = idx["rows"]
        z_tops = idx["z_tops"]; z_bots = idx["z_bots"]
        n = xs.size
        if n == 0:
            return None

        px, py = self._gl_viewer.last_hover_pixel()
        mvp = self._gl_viewer._current_mvp()
        # MVP rows → numpy 4×4 for batch multiplication.
        rows_v = [mvp.row(i) for i in range(4)]
        M = np.array([
            [r.x(), r.y(), r.z(), r.w()] for r in rows_v
        ], dtype=np.float64)
        w_px = max(1, self._gl_viewer.width())
        h_px = max(1, self._gl_viewer.height())

        def _project(x: np.ndarray, y: np.ndarray,
                     z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            """Batch world→screen-pixel projection. Behind-camera points
            (clip-w ≤ 0) become NaN so the distance tests fail naturally."""
            ones = np.ones_like(x)
            pts = np.column_stack([x, y, z, ones])
            clip = pts @ M.T
            cw = clip[:, 3]
            bad = cw <= 1e-6
            cw_safe = np.where(bad, 1.0, cw)
            ndc_x = clip[:, 0] / cw_safe
            ndc_y = clip[:, 1] / cw_safe
            sx = (ndc_x + 1.0) * 0.5 * w_px
            sy = (1.0 - ndc_y) * 0.5 * h_px
            sx = np.where(bad, np.nan, sx)
            sy = np.where(bad, np.nan, sy)
            return sx, sy

        z_mid = 0.5 * (z_tops + z_bots)
        sx_t, sy_t = _project(xs, ys, z_tops)
        sx_b, sy_b = _project(xs, ys, z_bots)
        # Two perpendicular offsets at mid-z give us a conservative
        # estimate of the cylinder's apparent screen radius (max of
        # x-axis and y-axis projections). Exact would be a perpendicular
        # to the camera-axis projection, but for hover-tolerance the
        # max-of-two is plenty close.
        sx_c, sy_c = _project(xs, ys, z_mid)
        sx_rx, sy_rx = _project(xs + radii, ys, z_mid)
        sx_ry, sy_ry = _project(xs, ys + radii, z_mid)
        screen_r = np.maximum(
            np.hypot(sx_rx - sx_c, sy_rx - sy_c),
            np.hypot(sx_ry - sx_c, sy_ry - sy_c),
        )

        # Cursor-to-segment distance for each via cylinder.
        dx_seg = sx_b - sx_t
        dy_seg = sy_b - sy_t
        seg_len2 = dx_seg * dx_seg + dy_seg * dy_seg
        valid_seg = seg_len2 > 1e-6
        seg_len2_safe = np.where(valid_seg, seg_len2, 1.0)
        t = ((px - sx_t) * dx_seg + (py - sy_t) * dy_seg) / seg_len2_safe
        t = np.clip(t, 0.0, 1.0)
        cx = np.where(valid_seg, sx_t + t * dx_seg, sx_t)
        cy = np.where(valid_seg, sy_t + t * dy_seg, sy_t)
        d2 = (cx - px) ** 2 + (cy - py) ** 2

        inside = (d2 <= screen_r * screen_r) \
            & np.isfinite(d2) & np.isfinite(screen_r)
        if not inside.any():
            return None
        candidates = np.where(inside)[0]
        best = int(candidates[np.argmin(d2[candidates])])
        return rows[best]

    # --- SOURCE / SINK marker hover ---------------------------------------
    #
    # When the cursor is over a SOURCE or SINK marker the bottom probe label
    # gets a suffix naming the directive and the current it sources/sinks.
    # SINK current = the prescribed load (directive value). SOURCE current
    # has no prescribed value — derive it from steady-state KCL on the rail
    # group as the sum of all SINK loads on the same rail.

    def _directive_current_for_hover(self, d: dict) -> float | None:
        """Current to report for a directive marker, or None if unknown.
        SINK: the prescribed load. SOURCE: sum of SINK loads on its rail
        group (KCL — what the source must deliver under DC steady state)."""
        role = d.get("role")
        if role == "SINK":
            try:
                return float(d.get("value", 0.0))
            except (TypeError, ValueError):
                return None
        if role == "SOURCE":
            return self._source_rail_load_current(d)
        return None

    def _source_rail_load_current(self, d: dict) -> float | None:
        """Sum of SINK currents on the same rail group as ``d``'s pins."""
        if self.metadata is None:
            return None
        source_nets: set[str] = set()
        for term in (d.get("terminals") or {}).values():
            for pin in term.get("pins", []):
                net = pin.get("net")
                if net:
                    source_nets.add(net)
        return self._rail_load_for_nets(source_nets)

    def _rail_load_for_nets(self, source_nets: set[str]) -> float | None:
        """Total SINK load on the rail group(s) the given nets belong to,
        or ``None`` when the nets map to no known rail. Shared by the
        solved-marker and editor-marker SOURCE hover so both report the
        same KCL figure."""
        if not source_nets:
            return None
        net_to_rail: dict[str, str] = {}
        for rail, members in self._rail_to_members.items():
            for n in members:
                net_to_rail[n] = rail
        rails = {net_to_rail[n] for n in source_nets if n in net_to_rail}
        if not rails:
            return None
        rail_members: set[str] = set()
        for rail in rails:
            rail_members.update(self._rail_to_members.get(rail, [rail]))
        total, any_found = self._rail_sink_load(rail_members)
        return total if any_found else None

    def _rail_sink_load(self, rail_members: set[str]) -> tuple[float, bool]:
        """Sum of every SINK load coupling into ``rail_members`` — solved
        schematic directives plus pending editor directives. A schematic
        SINK is dropped when an editor directive overrides its designator
        so an unlocked-and-edited sink isn't counted twice. Returns
        ``(total_amps, any_found)``."""
        total = 0.0
        any_found = False
        overridden = self._overridden_designators()
        directives = (self.metadata.get("directives", [])
                      if self.metadata else [])
        for other in directives:
            if other.get("role") != "SINK":
                continue
            if other.get("designator") in overridden:
                continue
            for term in (other.get("terminals") or {}).values():
                if any(p.get("net") in rail_members
                       for p in term.get("pins", [])):
                    try:
                        total += float(other.get("value", 0.0))
                    except (TypeError, ValueError):
                        pass
                    any_found = True
                    break
        if self._project is not None:
            for d in self._project.editor_directives:
                if d.role != "SINK":
                    continue
                nets = {d.p_net, d.n_net} - {None}
                if nets & rail_members:
                    if d.current is not None:
                        try:
                            total += float(d.current)
                        except (TypeError, ValueError):
                            pass
                    any_found = True
        return total, any_found

    def _overridden_designators(self) -> set[str]:
        """Designators whose schematic PDN directive an editor directive
        replaces. The schematic side is dropped from hover rows and the
        rail-load sum so the (pending) editor value stands in for it."""
        if self._project is None:
            return set()
        return {d.overrides_designator
                for d in self._project.editor_directives
                if d.overrides_designator}

    def _editor_directive_current(self, d) -> float | None:
        """Hover current for an :class:`EditorDirective` — the prescribed
        load for a SINK, or the KCL rail-load sum for a SOURCE (same basis
        as :meth:`_directive_current_for_hover` uses for solved markers)."""
        if d.role == "SINK":
            return d.current
        if d.role == "SOURCE":
            return self._rail_load_for_nets({d.p_net, d.n_net} - {None})
        return None

    def _editor_marker_hover_rows(self) -> list[dict]:
        """Hover rows for the placed editor directives so the bottom-bar
        probe reports their (pending, not-yet-resolved) source / sink
        values instead of falling through to the stale schematic marker
        underneath. Mirrors the point walk in :meth:`_editor_marker_groups`;
        rows carry ``pending=True`` so the formatter can tag them."""
        if not self._editor_mode or self._project is None:
            return []
        rows: list[dict] = []
        for d in self._project.editor_directives:
            if d.role not in ("SOURCE", "SINK"):
                continue
            total = self._editor_directive_current(d)
            label = d.designator or d.id or ""
            size_px = int(self._ROLE_MARKER_STYLE[d.role]["size"]) + 4
            # Resolve P-side / N-side points exactly as _editor_marker_groups.
            if d.kind == "free" and d.anchor_xy is not None:
                sides = [("p", d.p_net,
                          [(float(d.anchor_xy[0]),
                            float(d.anchor_xy[1]))])]
            elif d.kind == "component":
                p_pts = self._component_pad_points(d.designator, [d.p_net])
                n_pts = ([] if d.single_net or not d.n_net
                         else self._component_pad_points(
                             d.designator, [d.n_net]))
                if not p_pts and not n_pts:
                    ctr = self._component_center(d.designator)
                    p_pts = [ctr] if ctr is not None else []
                sides = [("p", d.p_net, p_pts), ("n", d.n_net, n_pts)]
            else:
                continue
            for term_name, net, pts in sides:
                n = len(pts)
                if not n:
                    continue
                per_pin = (total / n if total is not None
                           and np.isfinite(total) else None)
                for (px, py) in pts:
                    rows.append({
                        "x_mm": float(px),
                        "y_mm": float(py),
                        "role": d.role,
                        "label": label,
                        "terminal": term_name,
                        "net": net or "",
                        "physical": d.layer or "",
                        "current_a": per_pin,
                        "directive_current_a": total,
                        "terminal_pin_count": n,
                        "size_px": size_px,
                        "pending": True,
                    })
        return rows

    def _set_marker_hover_rows(self, rows: list[dict]) -> None:
        """Stash the SOURCE/SINK marker rows that the hover probe should
        hit-test against. Called from :meth:`_update_markers_and_legend`
        with the same pin-walk results that go into the marker batch."""
        if not rows:
            self._marker_hover_index_cache = None
            return
        xs = np.fromiter((r["x_mm"] for r in rows), dtype=np.float64,
                          count=len(rows))
        ys = np.fromiter((r["y_mm"] for r in rows), dtype=np.float64,
                          count=len(rows))
        size_px = np.fromiter((r["size_px"] for r in rows), dtype=np.float64,
                               count=len(rows))
        self._marker_hover_index_cache = {
            "xs": xs,
            "ys": ys,
            "size_px": size_px,
            "rows": rows,
        }

    def _pick_hovered_marker(self, world_x: float, world_y: float
                              ) -> dict | None:
        """Return the SOURCE/SINK marker row closest to (world_x, world_y)
        if the cursor is inside its hit radius, else ``None``.

        Hit radius scales with the marker's pixel size at the current
        zoom — a generous +2 px slack so the click target matches what
        the eye sees and isn't a needle in the centre of the glyph."""
        idx = getattr(self, "_marker_hover_index_cache", None)
        if idx is None:
            return None
        if not self.show_markers_box.isChecked():
            return None
        mpp = self._mm_per_pixel
        if mpp <= 0.0:
            return None
        xs = idx["xs"]; ys = idx["ys"]
        size_px = idx["size_px"]
        rows = idx["rows"]
        radii_mm = (size_px * 0.5 + 2.0) * mpp
        dx = xs - world_x
        dy = ys - world_y
        d2 = dx * dx + dy * dy
        r2 = radii_mm * radii_mm
        inside = d2 <= r2
        if not inside.any():
            return None
        candidates = np.where(inside)[0]
        best = int(candidates[np.argmin(d2[candidates])])
        return rows[best]

    def _marker_hover_info(self, world_x: float, world_y: float) -> str:
        """Bottom-bar suffix for the SOURCE/SINK marker under the cursor,
        or ``""``. Thin wrapper over :meth:`_pick_hovered_marker` + the
        text formatter, kept so callers don't have to know about both."""
        row = self._pick_hovered_marker(world_x, world_y)
        if row is None:
            return ""
        return self._format_marker_hover_text(row)

    def _format_marker_tooltip_lines(self, row: dict) -> list[str]:
        """Multi-line cursor-tooltip rendering of a SOURCE/SINK marker.
        Splits what the bottom-bar suffix packs onto one line: a header
        with the role + designator, then "I = X A" for the hovered pin,
        and a "total: X A (N pins)" line for multi-pin terminals so the
        user sees both numbers at once."""
        role = row.get("role", "")
        label = row.get("label", "") or "?"
        per_pin = row.get("current_a")
        total = row.get("directive_current_a")
        n_pins = int(row.get("terminal_pin_count") or 1)
        pending = bool(row.get("pending"))
        lines: list[str] = [f"{role} {label}"]
        if per_pin is None or not np.isfinite(per_pin):
            lines.append("I: (n/a)")
            if pending:
                lines.append("(pending — press Resolve)")
            return lines
        prefix = "I ≈" if role == "SOURCE" else "I ="
        lines.append(f"{prefix} {per_pin:.4g} A")
        if n_pins > 1 and total is not None and np.isfinite(total):
            pin_word = "pins" if n_pins != 1 else "pin"
            lines.append(f"Total: {total:.4g} A ({n_pins} {pin_word})")
        if role == "SOURCE":
            lines.append("(rail load)")
        if pending:
            lines.append("(pending — press Resolve)")
        return lines

    def _format_marker_hover_text(self, row: dict) -> str:
        """One-line suffix for the hovered SOURCE/SINK marker. Shows the
        per-pin current (directive total / pins on this terminal) AND
        the terminal total so a multi-pin sink doesn't misleadingly
        report the whole load on each marker. SOURCE values are tagged
        ``≈ … (rail load)`` to flag they're derived from KCL rather than
        prescribed by the user. Editor edits not yet resolved get a
        trailing ``[pending — Resolve]`` so the user knows the figure is
        their unsolved input, not a solved value."""
        role = row.get("role", "")
        label = row.get("label", "") or "?"
        per_pin = row.get("current_a")
        total = row.get("directive_current_a")
        n_pins = int(row.get("terminal_pin_count") or 1)
        pend = "  [pending — Resolve]" if row.get("pending") else ""
        if per_pin is None or not np.isfinite(per_pin):
            return f"   {role} {label}: I = (n/a){pend}"
        prefix = "I ≈" if role == "SOURCE" else "I ="
        suffix = " (rail load)" if role == "SOURCE" else ""
        if n_pins > 1 and total is not None and np.isfinite(total):
            total_part = f" ({total:.4g} A total){suffix}"
        else:
            total_part = suffix
        return (f"   {role} {label}: {prefix} {per_pin:.4g} A"
                f"{total_part}{pend}")

    def _stub_prepared_shape(self, stub: dict):
        """Return (and cache) a shapely PreparedGeometry for a stub polygon."""
        cached = stub.get("_prepared_shape_cache")
        if cached is not None:
            return cached
        ext = stub.get("exterior")
        if ext is None or (hasattr(ext, "size") and ext.size == 0):
            return None
        holes = stub.get("holes") or []
        try:
            poly = _sg.Polygon(ext, holes)
        except Exception:
            return None
        if poly.is_empty:
            return None
        prepped = _sp.prep(poly)
        stub["_prepared_shape_cache"] = prepped
        return prepped

    def _probe_at_stub(self, x: float, y: float
                       ) -> tuple[float | None, dict] | None:
        """Check whether (x, y) falls inside any visible stub (no-current
        copper).  Returns ``(voltage, info)`` — voltage may be None if
        un-estimable — or ``None`` if no stub covers the point.
        ``info`` has keys ``physical``, ``net``, ``is_stub=True``."""
        if self.metadata is None:
            return None
        stubs = self.metadata.get("stubs") or []
        if not stubs:
            return None
        phys_list, rails, _ = self._current_selection()
        if not rails:
            return None
        visible_layer_ids: dict[int, str] = {}
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                visible_layer_ids[lid] = phys
        if not visible_layer_ids:
            return None
        rail_members = set(self._effective_rail_members(rails))
        pt = _sg.Point(x, y)
        for stub in stubs:
            lid = stub.get("layer_id")
            phys = visible_layer_ids.get(lid)
            if phys is None:
                continue
            net = stub.get("net")
            if rail_members and net not in rail_members:
                continue
            prepped = self._stub_prepared_shape(stub)
            if prepped is None:
                continue
            try:
                if not prepped.contains(pt):
                    continue
            except Exception:
                continue
            voltage = self._sample_stub_voltage(stub, net)
            return voltage, {"physical": phys, "net": net, "is_stub": True}
        return None

    def _probe_at_stub_3d(self, x_px: float, y_px: float
                          ) -> tuple[float | None, dict] | None:
        """3D-mode stub probe: unproject to each visible stub's layer z
        and check whether the intersection point falls inside the stub."""
        if self.metadata is None:
            return None
        stubs = self.metadata.get("stubs") or []
        if not stubs:
            return None
        phys_list, rails, _ = self._current_selection()
        if not rails:
            return None
        visible_layer_ids: dict[int, str] = {}
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                visible_layer_ids[lid] = phys
        if not visible_layer_ids:
            return None
        rail_members = set(self._effective_rail_members(rails))
        for stub in stubs:
            lid = stub.get("layer_id")
            phys = visible_layer_ids.get(lid)
            if phys is None:
                continue
            net = stub.get("net")
            if rail_members and net not in rail_members:
                continue
            prepped = self._stub_prepared_shape(stub)
            if prepped is None:
                continue
            z = self._layer_z_for(phys)
            wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
            try:
                if not prepped.contains(_sg.Point(wx, wy)):
                    continue
            except Exception:
                continue
            voltage = self._sample_stub_voltage(stub, net)
            return voltage, {"physical": phys, "net": net, "is_stub": True}
        return None

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

    def _build_settings_tab(self) -> QWidget:
        """Build the Settings tab — tunable physics + mesh + display knobs
        and a Re-run Solver button.

        Editing a field does NOT immediately re-solve; the user must press
        "Re-run Solver" to spawn a fresh solve with the new parameters.
        Display-only fields (warning threshold, percentile) are also
        applied by the same button, but those are cheap and never trigger
        an FEM rebuild on their own."""
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
            "Adjust the physics and meshing parameters below, then click "
            "<b>Re-run Solver</b> to re-solve the current project with the "
            "new values. The viewer will reload with the fresh result."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"QLabel {{ color: {t['fg_label']}; }}")
        outer.addWidget(intro)

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
        _solve_layout = solve_box.layout()
        if _solve_layout is not None:
            _solve_layout.addRow(self._settings_adaptive_check)
        outer.addWidget(solve_box)

        # ----- Stackup copper-thickness group -----
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
            "Display options (applied immediately on Re-run)",
            self._SETTINGS_DISPLAY_FIELDS,
            source=display_src,
            attr_prefix="settings_edit_",
        )
        outer.addWidget(display_box)

        # ----- Status line + buttons -----
        self._settings_status_label = QLabel("")
        self._settings_status_label.setWordWrap(True)
        self._settings_status_label.setStyleSheet(
            f"QLabel {{ color: {t['accent']}; padding: 4px 0; }}"
        )
        outer.addWidget(self._settings_status_label)

        button_row = QHBoxLayout()
        self._settings_rerun_btn = QPushButton("Re-run Solver")
        self._settings_rerun_btn.setToolTip(
            "Re-solve the current project with the parameters above. "
            "Opens a fresh viewer window with the new result."
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
        )
        return widget

    def _make_settings_group(
        self, title: str,
        fields: tuple[tuple[str, str, str, str], ...],
        source: object,
        attr_prefix: str,
    ) -> QGroupBox:
        """Build a QGroupBox containing one labelled QLineEdit per field
        in ``fields``. The current value is pulled from ``source`` via
        ``getattr``; default-value hints come from a fresh SolveSettings."""
        from fypa.altium_loader import SolveSettings as _SolveSettings
        from dataclasses import fields as _dc_fields
        defaults = _SolveSettings()
        # The display-only fields aren't in SolveSettings — fall back to
        # the module-level constants for their default-value hint.
        display_defaults = {
            "via_current_warn_a": _VIA_CURRENT_WARN_A,
            "display_percentile_high": _DISPLAY_PERCENTILE_HIGH,
        }
        defaults_attrs = {f.name for f in _dc_fields(defaults)}

        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        for key, label, unit, tooltip in fields:
            current = getattr(source, key, None)
            if current is None:
                continue
            default_val = (
                getattr(defaults, key) if key in defaults_attrs
                else display_defaults.get(key)
            )

            edit = QLineEdit(self._fmt_settings_value(current))
            validator = QDoubleValidator(self)
            validator.setNotation(QDoubleValidator.StandardNotation)
            validator.setBottom(0.0)
            edit.setValidator(validator)
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
            if key == "via_current_warn_a":
                # In-place re-check button: applies the new threshold to
                # the Vias tab without a full re-solve. Hidden until the
                # field diverges from the currently-applied level.
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

    def _make_collapsible_section(
        self, title: str, body_widget: QWidget, *,
        expanded: bool = False,
    ) -> tuple[QWidget, QToolButton]:
        """Wrap ``body_widget`` in a click-to-expand QFrame with a header
        button matching the dark-theme styling of the other Settings
        groups. Returns ``(wrapper, header)`` — keep a reference to the
        header if you want to update its title later."""
        wrap = QFrame()
        wrap.setObjectName("collapsibleSection")
        t = _T()
        wrap.setStyleSheet(
            f"QFrame#collapsibleSection {{ border: 1px solid {t['border']};"
            f"                            border-radius: 4px;"
            f"                            background-color: {t['bg']};"
            f"                            margin-top: 6px; }}"
        )
        wrap_layout = QVBoxLayout(wrap)
        wrap_layout.setContentsMargins(8, 6, 8, 8)
        wrap_layout.setSpacing(6)

        header = QToolButton(wrap)
        header.setCheckable(True)
        header.setChecked(expanded)
        header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        header.setAutoRaise(True)
        header.setCursor(Qt.PointingHandCursor)
        tail = "click to collapse" if expanded else "click to expand"
        header.setText(f"{title} — {tail}")
        header.setStyleSheet(
            f"QToolButton {{ border: none; padding: 2px 4px;"
            f"              color: {t['fg_strong']}; font-weight: 600;"
            f"              text-align: left; background: transparent; }}"
            f"QToolButton:hover {{ color: {t['accent']}; }}"
        )
        wrap_layout.addWidget(header)

        body_widget.setParent(wrap)
        body_widget.setVisible(expanded)
        wrap_layout.addWidget(body_widget)

        def _on_toggled(exp: bool) -> None:
            body_widget.setVisible(exp)
            header.setArrowType(Qt.DownArrow if exp else Qt.RightArrow)
            # Replace just the "click to …" tail, keep the count prefix.
            head = header.text().rsplit(" — ", 1)[0]
            new_tail = "click to collapse" if exp else "click to expand"
            header.setText(f"{head} — {new_tail}")
        header.toggled.connect(_on_toggled)
        return wrap, header

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
        self._theme_combo.setMinimumWidth(140)
        self._theme_combo.setMaximumWidth(180)
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

    def _refresh_inline_theme(self) -> None:
        """Re-apply the active theme to every widget that pinned its
        colours inline at construction time.

        Re-styles the Heatmap-tab side panel in place (we can't rebuild
        it because the OpenGL canvas can't be torn down without crashing
        the process). The other tabs (Setup, Nodes, Vias, Settings,
        Help) are removed and rebuilt via their builders — that's how
        the dozens of internal labels / tables / buttons inside them
        track the theme without needing individual references.

        Transient state inside the rebuilt tabs (filter selections,
        unsaved Settings form edits, scroll positions) is lost on
        toggle. The tradeoff is the only one I see — chasing every
        nested QLabel by hand would be unmaintainable.
        """
        t = _T()

        # --- Heatmap tab: re-style side panel widgets in place ---
        def _restyle_eye_list(lw) -> None:
            lw.setStyleSheet(
                f"QListWidget {{ background-color: {t['bg']}; color: {t['fg']};"
                f"              border: 1px solid {t['border']}; padding: 2px;"
                f"              alternate-background-color: {t['bg_alt']}; }}"
                f"QListWidget::item:hover {{ background-color: {t['bg_hover']}; }}"
            )
            # Each row carries its name-label colour inline; iterate.
            for i in range(lw.count()):
                row = lw.itemWidget(lw.item(i))
                if row is None:
                    continue
                for lbl in row.findChildren(QLabel):
                    ss = (lbl.styleSheet() or "").lower()
                    if not ss:
                        continue
                    if "bold" in ss:
                        lbl.setStyleSheet(
                            f"QLabel {{ color: {t['fg']}; font-weight: bold; }}"
                        )
                    else:
                        lbl.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")
                # Eye icons are cached by theme mode → reapply forces a
                # cache miss and a fresh draw in the new colour.
                for btn in row.findChildren(EyeButton):
                    btn._apply_icon()

        if hasattr(self, "layer_list") and self.layer_list is not None:
            _restyle_eye_list(self.layer_list)
        if hasattr(self, "rail_list") and self.rail_list is not None:
            _restyle_eye_list(self.rail_list)

        if (getattr(self, "probe_label_widget", None) is not None):
            self.probe_label_widget.setStyleSheet(
                f"QLabel {{ font-family: Consolas, monospace; padding: 6px 10px;"
                f" color: {t['fg']}; background-color: {t['bg']};"
                f" border-top: 1px solid {t['border']}; }}"
            )
        if getattr(self, "layer_spacing_label", None) is not None:
            self.layer_spacing_label.setStyleSheet(
                f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}"
            )
        if getattr(self, "arrow_spacing_label", None) is not None:
            self.arrow_spacing_label.setStyleSheet(
                f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}"
            )
        if getattr(self, "_cursor_tooltip_label", None) is not None:
            self._cursor_tooltip_label.setStyleSheet(
                "QLabel {"
                f" background-color: {t['bg']};"
                f" color: {t['fg']};"
                f" border: 1px solid {t['border']};"
                " padding: 4px 8px;"
                " font-family: Consolas, monospace;"
                " font-size: 9pt;"
                "}"
            )
        if getattr(self, "scale_controller", None) is not None:
            self.scale_controller.apply_theme()
        if getattr(self, "_sidebar_toggle_btn", None) is not None:
            self._sidebar_toggle_btn.update()

        # --- Non-heatmap tabs: remove and rebuild ---
        heatmap_idx = self._heatmap_tab_index
        current_tab_text = self.tabs.tabText(self.tabs.currentIndex())
        # Drop the old Messages-tab buffer listener before its signaller
        # QObject is destroyed below — leaving the listener in place
        # would fire on each new record, hit RuntimeError from the dead
        # QObject, and only then self-remove. Schedule the signaller for
        # deletion explicitly because it's parented to the viewer (so
        # it'd otherwise outlive the rebuild until the window itself
        # closes — a small but unbounded leak on theme toggles).
        old_msg_listener = getattr(self, "_messages_listener", None)
        if old_msg_listener is not None:
            log_buffer.remove_listener(old_msg_listener)
        old_msg_signaller = getattr(self, "_messages_signaller", None)
        if old_msg_signaller is not None:
            old_msg_signaller.deleteLater()
        # Remove from highest index downwards so removeTab doesn't shift
        # the index of tabs we still need to remove. Skip the heatmap.
        for i in range(self.tabs.count() - 1, -1, -1):
            if i == heatmap_idx:
                continue
            w = self.tabs.widget(i)
            self.tabs.removeTab(i)
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        # Re-add in the original order. _nodes_tab_index / _vias_tab_index
        # need refreshing because both tabs are re-inserted after the others.
        self.tabs.addTab(self._build_setup_tab(), "Setup")
        self._nodes_tab_index = self.tabs.addTab(
            self._build_nodes_tab(), "Nodes",
        )
        self._update_nodes_tab_title(getattr(self, "_nodes_warn_count", 0))
        self._vias_tab_index = self.tabs.addTab(
            self._build_vias_tab(), "Vias",
        )
        self._update_vias_tab_title(getattr(self, "_vias_warn_count", 0))
        # The previous Messages-tab listener is dropped on tab teardown
        # below (the QObject signaller is destroyed with its parent);
        # rebuilding here re-installs a fresh listener against the same
        # global buffer so the new tab keeps receiving live records.
        self._messages_signaller = None
        self._messages_listener = None
        self._messages_tab_index = self.tabs.addTab(
            self._build_messages_tab(), "Messages",
        )
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.tabs.addTab(self._build_help_tab(), "Help")
        # Restore tab selection. "Nodes" and "Vias" both get a warning
        # suffix appended on failures, so match by prefix.
        for i in range(self.tabs.count()):
            label = self.tabs.tabText(i)
            if label == current_tab_text or (
                current_tab_text.startswith("Vias")
                and label.startswith("Vias")
            ) or (
                current_tab_text.startswith("Nodes")
                and label.startswith("Nodes")
            ):
                self.tabs.setCurrentIndex(i)
                break

        status = getattr(self, "_theme_status_label", None)
        if status is not None:
            status.setText(
                f"<span style='color:{t['ok']};'>"
                f"Theme set to <b>{_esc(current_theme_mode().capitalize())}</b>."
                "</span>"
            )

    def _build_stackup_settings_box(self) -> QWidget:
        """List every enabled copper layer with an editable thickness
        field (µm). Edits apply on the next press of Re-run Solver and
        feed the per-layer sheet conductance (G = thickness × σ) as well
        as the via-barrel z-distances used to compute hop resistance.

        Override map is keyed by layer_id (int) since enabled copper
        layer ids are unique within a project. Plane layers are listed
        but greyed-out — plane geometry isn't supported in v1, so their
        thickness has no effect on the FEM today.
        """
        # ``self._stackup_thickness_edits`` maps layer_id →
        # (QLineEdit, original_thickness_mm).
        self._stackup_thickness_edits: dict[int,
                                             tuple[QLineEdit, float]] = {}

        rows: list[dict] = []
        if self.metadata:
            rows = list(self.metadata.get("stackup") or [])

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 0)
        body_layout.setSpacing(6)

        layer_count = len(rows)
        count_text = ("no copper layers" if layer_count == 0
                       else f"{layer_count} copper layer"
                            + ("s" if layer_count != 1 else ""))
        title = f"Stackup — copper thicknesses ({count_text})"

        if not rows:
            info = QLabel(
                f"<i style='color:{_T()['fg_dim']};'>No copper layers in this "
                "project's stackup.</i>"
            )
            info.setWordWrap(True)
            body_layout.addWidget(info)
            wrap, _header = self._make_collapsible_section(
                title, body, expanded=True,
            )
            return wrap

        intro = QLabel(
            "Type a new value in <b>µm</b> to override a copper layer's "
            "thickness. Empty (or unchanged) fields keep the existing "
            "value. Thickness drives sheet conductance "
            "(G = thickness × σ) and via-barrel hop length. The "
            "<i>dielectric</i> rows interleaved below show the core / "
            "prepreg thickness between adjacent copper layers (read-only)."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"QLabel {{ color: {_T()['fg_label']}; }}")
        body_layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        for row_index, row in enumerate(rows):
            lid = int(row.get("layer_id", -1))
            name = str(row.get("name", "?"))
            thk_mm = float(row.get("copper_thickness_mm", 0.0) or 0.0)
            thk_um = thk_mm * 1000.0
            thk_mil = thk_mm / 0.0254 if thk_mm else 0.0
            thk_oz = thk_mm / 0.0348 if thk_mm else 0.0
            is_plane = bool(row.get("is_plane"))

            edit = QLineEdit(self._fmt_settings_value(thk_um))
            validator = QDoubleValidator(self)
            validator.setNotation(QDoubleValidator.StandardNotation)
            validator.setBottom(0.0)
            edit.setValidator(validator)
            edit.setMinimumWidth(110)
            edit.setMaximumWidth(160)
            tooltip = (
                f"Override copper thickness of layer {lid} ({name}). "
                f"Originally {thk_um:g} µm ≈ {thk_mil:.3f} mil ≈ "
                f"{thk_oz:.3f} oz. Press Re-run Solver to commit."
            )
            if is_plane:
                tooltip += ("\n\nNote: this layer is a plane; plane "
                             "geometry isn't supported in v1, so the "
                             "thickness override has no effect on the "
                             "FEM until plane support lands.")
            edit.setToolTip(tooltip)
            edit.textChanged.connect(self._on_settings_field_changed)

            self._stackup_thickness_edits[lid] = (edit, thk_mm)

            _t = _T()
            row_layout = QHBoxLayout()
            row_layout.setSpacing(6)
            row_layout.addWidget(edit)
            unit_lbl = QLabel("µm")
            unit_lbl.setStyleSheet(f"QLabel {{ color: {_t['fg_muted']}; }}")
            row_layout.addWidget(unit_lbl)
            row_layout.addSpacing(8)
            hint_bits = [f"was {thk_um:g} µm",
                         f"{thk_mil:.3f} mil",
                         f"{thk_oz:.3f} oz"]
            if is_plane:
                hint_bits.append(
                    f"<span style='color:{_t['warn']};'>PLANE (not yet "
                    "modelled — override is informational)</span>"
                )
            hint = QLabel(
                f"<span style='color:{_t['fg_hint']};'>({' · '.join(hint_bits)})</span>"
            )
            row_layout.addWidget(hint)
            row_layout.addStretch(1)
            row_widget = QWidget()
            row_widget.setLayout(row_layout)

            label_widget = QLabel(f"L{lid}  {name}")
            label_widget.setStyleSheet(
                f"QLabel {{ color: {_t['fg']}; font-weight: 600; }}"
            )
            label_widget.setToolTip(tooltip)
            form.addRow(label_widget, row_widget)

            # Dielectric below this copper layer (between this layer and
            # the next one in the stack). Read-only — purely informational
            # so the user can see how far apart the copper layers sit and
            # judge expected via-cylinder lengths in the 3D view.
            if row_index + 1 < len(rows):
                d_mm = float(row.get("dielectric_thickness_mm", 0.0) or 0.0)
                d_um = d_mm * 1000.0
                d_mil = d_mm / 0.0254 if d_mm else 0.0
                if d_mm > 0.0:
                    d_text = (f"<span style='color:{_t['dielectric']};'>"
                              f"{d_um:g} µm &nbsp;·&nbsp; "
                              f"{d_mil:.3f} mil &nbsp;·&nbsp; "
                              f"{d_mm:.4f} mm</span>")
                else:
                    d_text = (f"<span style='color:{_t['fg_hint']};'>"
                              "<i>no thickness in stackup</i></span>")
                diel_value = QLabel(
                    f"<span style='color:{_t['dielectric_dim']};'>"
                    f"<i>dielectric</i></span> &nbsp; {d_text}"
                )
                diel_value.setToolTip(
                    "Dielectric (core or prepreg) between this copper "
                    "layer and the one below. Drives via-barrel hop "
                    "length in the 3D view. Read-only — edit the .PcbDoc "
                    "stackup to change."
                )
                diel_label = QLabel(
                    f"<span style='color:{_t['separator']};'>┊</span>"
                )
                diel_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                form.addRow(diel_label, diel_value)

        form_wrap = QWidget()
        form_wrap.setLayout(form)
        body_layout.addWidget(form_wrap)

        wrap, header = self._make_collapsible_section(
            title, body, expanded=False,
        )
        self._stackup_collapse_btn = header
        return wrap

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

    def _on_settings_reset(self) -> None:
        """Restore every Settings-tab field to its built-in default. The
        user still has to press Re-run Solver to commit."""
        from fypa.altium_loader import SolveSettings as _SolveSettings
        defaults = _SolveSettings()
        for key, *_ in self._SETTINGS_FIELDS:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is not None:
                edit.setText(self._fmt_settings_value(getattr(defaults, key)))
        chk = getattr(self, "_settings_adaptive_check", None)
        if chk is not None:
            chk.setChecked(bool(defaults.adaptive_mesh))
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
                val = float(edit.text().strip())
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

        for key, baseline in (
            ("via_current_warn_a", self._via_current_warn_a),
            ("display_percentile_high", self._display_percentile_high),
        ):
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is None:
                continue
            try:
                val = float(edit.text().strip())
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
                new_um = float(text)
                dirty = abs(new_um / 1000.0 - original_mm) > 1.0e-6
            except ValueError:
                dirty = True
            mark(edit, dirty)
            any_dirty = any_dirty or dirty

        return any_dirty

    def _on_settings_field_changed(self, *_args) -> None:
        """Slot wired to every Settings-tab editor's change signal:
        refresh per-field dirty outlines and update ``_settings_dirty``
        against the current solve baseline, then refresh the Resolve
        overlay button."""
        new_dirty = self._update_settings_field_styles()
        if new_dirty == self._settings_dirty:
            return
        self._settings_dirty = new_dirty
        self._refresh_solve_stale_overlay()

    def _update_via_check_btn_visibility(self) -> None:
        """Show the Re-run Via Check button only when the field value
        differs from the currently-applied via warning threshold."""
        btn = getattr(self, "_settings_rerun_via_check_btn", None)
        edit = getattr(self, "settings_edit_via_current_warn_a", None)
        if btn is None or edit is None:
            return
        try:
            val = float(edit.text().strip())
        except ValueError:
            btn.setVisible(False)
            return
        btn.setVisible(abs(val - self._via_current_warn_a) > 1e-12)

    def _on_rerun_via_check(self) -> None:
        """Apply the new Via current warning level to the Vias tab in
        place — no FEM re-solve. Updates the warning count in the tab
        title, refreshes the filter checkbox label and summary line,
        and re-styles the table cells against the new threshold."""
        edit = getattr(self, "settings_edit_via_current_warn_a", None)
        if edit is None:
            return
        text = edit.text().strip()
        try:
            new_warn = float(text)
        except ValueError:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['warn_fg']};'>Via current warning "
                f"level: not a number ({text!r})</span>"
            )
            return
        if new_warn < 0:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['warn_fg']};'>Via current warning "
                f"level must be ≥ 0</span>"
            )
            return
        self._via_current_warn_a = new_warn
        if hasattr(self, "vias_warn_only_box"):
            self.vias_warn_only_box.setText(
                f"Show only warnings (|I| ≥ {self._via_current_warn_a:g} A)"
            )
        if getattr(self, "_vias_table_populated", False):
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._populate_vias_table()
            finally:
                QApplication.restoreOverrideCursor()
        else:
            rows = self._get_or_compute_via_rows()
            warn_count = sum(
                1 for r in rows
                if r.get("current") is not None
                and abs(r["current"]) >= self._via_current_warn_a
            )
            self._vias_warn_count = warn_count
            self._update_vias_tab_title(warn_count)
        self._update_via_check_btn_visibility()
        self._on_settings_field_changed()
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Via check re-run "
            f"against |I| ≥ {self._via_current_warn_a:g} A.</span>"
        )

    def _gather_stackup_overrides(self) -> dict[int, float]:
        """Collect non-no-op copper-thickness overrides from the form.

        Returns ``{layer_id: thickness_mm}`` for fields whose value
        differs from the original by more than 1 nm. Blank fields and
        unchanged fields are skipped. Raises ``ValueError`` on
        un-parseable input.
        """
        overrides: dict[int, float] = {}
        for lid, (edit, original_mm) in getattr(
                self, "_stackup_thickness_edits", {}).items():
            text = edit.text().strip()
            if not text:
                continue
            try:
                new_um = float(text)
            except ValueError:
                raise ValueError(
                    f"layer {lid} thickness: not a number ({text!r})"
                )
            if new_um < 0:
                raise ValueError(
                    f"layer {lid} thickness must be ≥ 0 µm"
                )
            new_mm = new_um / 1000.0
            if abs(new_mm - original_mm) > 1.0e-6:
                overrides[lid] = new_mm
        return overrides

    def _gather_settings_from_form(self) -> tuple[object, float, float,
                                                   dict[int, float]]:
        """Read the current text in every Settings-tab QLineEdit and return
        ``(SolveSettings, via_current_warn_a, display_percentile,
        stackup_overrides)``. Raises ``ValueError``
        (caught by the Re-run handler) on bad input."""
        from fypa.altium_loader import SolveSettings as _SolveSettings
        kwargs: dict[str, float] = {}
        for key, label, *_rest in self._SETTINGS_FIELDS:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is None:
                continue
            text = edit.text().strip()
            try:
                kwargs[key] = float(text)
            except ValueError:
                raise ValueError(f"{label!r}: not a number ({text!r})")
        chk = getattr(self, "_settings_adaptive_check", None)
        if chk is not None:
            kwargs["adaptive_mesh"] = chk.isChecked()
        new_settings = _SolveSettings(**kwargs)

        def _read_display(key: str, label: str) -> float:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is None:
                # Should not happen — field is always built. Be safe.
                return getattr(self, f"_{key}")
            text = edit.text().strip()
            try:
                return float(text)
            except ValueError:
                raise ValueError(f"{label!r}: not a number ({text!r})")

        warn_a = _read_display("via_current_warn_a",
                                "Via current warning level")
        pct = _read_display("display_percentile_high",
                             "Heatmap colour-scale clip percentile")
        if not (0.0 < pct <= 100.0):
            raise ValueError("Heatmap colour-scale clip percentile must "
                              "be in (0, 100]")
        stackup_overrides = self._gather_stackup_overrides()
        return new_settings, warn_a, pct, stackup_overrides

    # --- File menu ----------------------------------------------------------

    def _build_menubar(self) -> None:
        """File menu so the viewer can be used standalone — pick a .PrjPcb
        to solve and view, save the current solution, or open a
        previously-pickled solution."""
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")

        open_proj = QAction("&Import Altium Design…", self)
        open_proj.setShortcut(QKeySequence.Open)         # Ctrl+O
        open_proj.setStatusTip(
            "Pick a .PrjPcb; reuse the cached solution if the project is "
            "unchanged, otherwise extract + solve."
        )
        open_proj.triggered.connect(
            lambda: self._on_menu_open_project(clean=False)
        )
        file_menu.addAction(open_proj)

        open_proj_clean = QAction("Import Altium Design (&Clean)…", self)
        open_proj_clean.setShortcut("Ctrl+Shift+L")
        open_proj_clean.setStatusTip(
            "Pick a .PrjPcb; ignore any cached design info or solution and "
            "re-extract + re-solve from scratch."
        )
        open_proj_clean.triggered.connect(
            lambda: self._on_menu_open_project(clean=True)
        )
        file_menu.addAction(open_proj_clean)

        open_projfile = QAction("Open &Project File…", self)
        open_projfile.setShortcut("Ctrl+Shift+P")
        open_projfile.setStatusTip(
            "Open a .fypa project file — its linked solution plus any "
            "editor-mode changes."
        )
        open_projfile.triggered.connect(self._on_menu_open_project_file)
        file_menu.addAction(open_projfile)

        file_menu.addSeparator()

        save_proj = QAction("Save &Project", self)
        save_proj.setShortcut("Ctrl+S")
        save_proj.setStatusTip(
            "Save the .fypa project file (editor changes, net renames, and "
            "links to the design-info / solution pickles)."
        )
        save_proj.triggered.connect(self._on_ctrl_s)
        file_menu.addAction(save_proj)

        save_proj_as = QAction("Save Project &As…", self)
        save_proj_as.setStatusTip(
            "Save the project to a new .fypa file."
        )
        save_proj_as.triggered.connect(self._on_menu_save_project_as)
        file_menu.addAction(save_proj_as)

        save_sol = QAction("&Save Solution…", self)
        save_sol.setStatusTip(
            "Save just the current solution to a .pkl file (remembers the "
            ".PrjPcb directory and selected .PcbDoc so it can be re-solved)."
        )
        save_sol.triggered.connect(self._on_menu_save_solution)
        file_menu.addAction(save_sol)

        open_sol = QAction("&Load Solution…", self)
        open_sol.setShortcut("Ctrl+Shift+O")
        open_sol.setStatusTip(
            "Open a previously-saved solution pickle (no re-solve)."
        )
        open_sol.triggered.connect(self._on_menu_open_solution)
        file_menu.addAction(open_sol)

        file_menu.addSeparator()
        export_menu = file_menu.addMenu("&Export")

        export_paraview = QAction("&ParaView…", self)
        export_paraview.setStatusTip(
            "Write the current solution as VTK files (one .vtu per copper "
            "layer, with per-vertex voltage) into a directory of your choice "
            "for opening in ParaView."
        )
        export_paraview.triggered.connect(self._on_menu_export_paraview)
        export_menu.addAction(export_paraview)

        file_menu.addSeparator()
        close_proj = QAction("&Close Project", self)
        close_proj.setShortcut("Ctrl+W")
        close_proj.setStatusTip(
            "Close the current project and return to the launcher window"
        )
        close_proj.triggered.connect(self._on_menu_close_project)
        file_menu.addAction(close_proj)

        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut(QKeySequence.Quit)          # Ctrl+Q
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # View menu sits between File and Help.
        self._build_view_menu()
        _build_help_menu(self)

    def _build_view_menu(self) -> None:
        """View menu — the display toggles that used to be side-panel
        checkboxes (2D/3D view, via heatmap, no-current copper colour, FEM
        mesh overlay).

        Each action drives its hidden backing checkbox via ``toggle()`` so
        the rest of the viewer's logic is untouched, and carries no
        ``QShortcut`` of its own — the existing window hotkeys (see
        :meth:`_install_hotkeys`) still own the keys, and the menu labels
        merely echo them. Labels flip with state; see
        :meth:`_refresh_view_menu_labels`."""
        view_menu = self.menuBar().addMenu("&View")

        self._act_view_3d = QAction(self)
        self._act_view_3d.setStatusTip(
            "Switch between the 2D top-down heatmap and the 3D perspective "
            "view of the stacked copper layers."
        )
        self._act_view_3d.triggered.connect(
            lambda: self.view_3d_box.toggle())
        view_menu.addAction(self._act_view_3d)

        self._act_heatmap_vias = QAction(self)
        self._act_heatmap_vias.setStatusTip(
            "Colour via and plated-through-hole cylinders by the active "
            "heatmap mode instead of solid orange / grey (3D mode only)."
        )
        self._act_heatmap_vias.triggered.connect(
            lambda: self.heatmap_vias_box.toggle())
        view_menu.addAction(self._act_heatmap_vias)

        self._act_colour_stubs = QAction(self)
        self._act_colour_stubs.setStatusTip(
            "Draw copper that carries no current as flat dim grey instead "
            "of its approximate (voltage-sampled) heatmap colour."
        )
        self._act_colour_stubs.triggered.connect(
            lambda: self.colour_stubs_box.toggle())
        view_menu.addAction(self._act_colour_stubs)

        self._act_show_mesh = QAction(self)
        self._act_show_mesh.setStatusTip(
            "Overlay the FEM triangulation on the heatmap as a fine dark "
            "wireframe — useful for judging local mesh density."
        )
        self._act_show_mesh.triggered.connect(
            lambda: self.show_mesh_box.toggle())
        view_menu.addAction(self._act_show_mesh)

    def _refresh_view_menu_labels(self) -> None:
        """Update the View menu item labels to track the current display
        state. Wired to each backing checkbox's ``toggled`` signal, so it
        fires whether the toggle came from the menu, a hotkey, or a
        restored project setting."""
        if not hasattr(self, "_act_view_3d"):
            return
        self._act_view_3d.setText(
            "2D View (2)" if self.view_3d_box.isChecked()
            else "3D View (3)")
        self._act_heatmap_vias.setText(
            "Heatmap vias / PTH [enabled] (V)"
            if self.heatmap_vias_box.isChecked()
            else "Heatmap vias / PTH [disabled] (V)")
        self._act_colour_stubs.setText(
            "No current copper colour [heatmap]"
            if self.colour_stubs_box.isChecked()
            else "No current copper colour [grey]")
        self._act_show_mesh.setText(
            "Hide Mesh" if self.show_mesh_box.isChecked()
            else "Show Mesh")

    def _menu_start_dir(self) -> str:
        """Best-effort starting directory for the file dialogs: the folder
        of the currently-loaded project if we have one, else CWD."""
        if self.metadata:
            current = self.metadata.get("prjpcb_path")
            if current:
                parent = Path(current).parent
                if parent.exists():
                    return str(parent)
        return ""

    def _project_cache_dir_str(self) -> str:
        """Best-effort cache subfolder for the currently-loaded project,
        as a string suitable for QFileDialog. Empty string if we don't
        know enough to compute one — the dialog will then default to the
        user's CWD."""
        if not self.metadata:
            return ""
        prjpcb = self.metadata.get("prjpcb_path")
        pcbdoc = self.metadata.get("pcbdoc_path")
        if not prjpcb:
            return ""
        try:
            from fypa.cli import _project_cache_dir
            cache_dir = _project_cache_dir(
                Path(prjpcb), Path(pcbdoc) if pcbdoc else None,
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            return str(cache_dir)
        except Exception:
            return ""

    def _on_menu_open_project(self, *, clean: bool = False) -> None:
        """File > Import Altium Design[ (Clean)]  →  pick a .PrjPcb and open
        it in a fresh viewer. Non-clean tries the solve cache first; clean
        always re-extracts + re-solves. Uses the current viewer's
        SolveSettings + display knobs as defaults for the new viewer."""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import Altium project (clean)" if clean else "Import Altium project",
            self._menu_start_dir(),
            "Altium project (*.PrjPcb);;All files (*)",
        )
        if not path_str:
            return
        prjpcb_path = Path(path_str)
        proceed, pcbdoc_path = _choose_pcbdoc(self, prjpcb_path)
        if not proceed:
            return
        # Apply the current physics settings to module globals before the
        # worker starts, mirroring the Re-run path's main-thread ordering.
        self._solve_settings.apply_to_modules()
        # The solve-cache check now runs INSIDE the worker so the dialog
        # below stays responsive during the (potentially 5–10 s)
        # pickle.load on large boards. On hit, the worker emits
        # finished_ok with the cached (sol, meta) and we skip extract +
        # solve. On miss, it falls through to the normal flow.
        initial_text = (
            f"Checking solve cache for {prjpcb_path.name}…\n"
            "On a cache miss this falls through to a full extract + solve "
            "(10–60 s depending on board size)."
            if not clean else None
        )
        self._start_solve_worker(
            prjpcb_path, self._solve_settings,
            self._via_current_warn_a, self._display_percentile_high,
            pcbdoc_selector=str(pcbdoc_path) if pcbdoc_path else None,
            use_design_cache=not clean,
            try_solve_cache_first=not clean,
            dialog_title="Loading project (clean)" if clean else "Loading project",
            dialog_text=initial_text,
        )

    def _on_menu_save_solution(self) -> None:
        """File > Save Solution…  →  prompt for a path (defaulting to this
        project's cache folder) and write the current solution there. The
        embedded metadata still carries ``prjpcb_path`` + ``pcbdoc_path``
        so the saved file can drive a later Re-run / Reload Design Info."""
        if self.solution is None:
            QMessageBox.information(
                self, "Nothing to save",
                "There's no solution loaded in this window to save.",
            )
            return
        start_dir = self._project_cache_dir_str() or self._menu_start_dir()
        project_name = "solution"
        if self.metadata:
            prjpcb = self.metadata.get("prjpcb_path")
            if prjpcb:
                project_name = Path(prjpcb).stem
        default_name = f"{project_name}.pkl"
        default_path = str(Path(start_dir) / default_name) if start_dir else default_name
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save solution",
            default_path,
            "Solution pickle (*.pkl);;All files (*)",
        )
        if not path_str:
            return
        try:
            from fypa.cli import save_solution_file
            save_solution_file(Path(path_str), self.solution, self.metadata)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't save solution",
                f"Failed to write {path_str}:\n\n{type(e).__name__}: {e}",
            )
            return
        label = getattr(self, "_settings_status_label", None)
        if label is not None:
            label.setText(
                f"<span style='color:{_T()['ok']};'>Saved solution to "
                f"{_esc(path_str)}</span>"
            )

    def _on_menu_export_paraview(self) -> None:
        """File > Export > ParaView…  →  prompt for an output directory and
        write one ``.vtu`` per copper layer there (per-vertex voltage scalar
        field). The files open natively in ParaView for 3-D stackup views,
        slicing, contours, etc. — see docs/user-guide/06-paraview-export.md.

        On any failure we surface a short modal and leave the full traceback
        in the Messages tab / log file (via ``logger.exception``) so the
        user has somewhere to look without us dumping a wall of text into
        the popup."""
        log = logging.getLogger(__name__)
        if self.solution is None:
            QMessageBox.information(
                self, "Nothing to export",
                "There's no solution loaded in this window to export.",
            )
            return
        start_dir = self._project_cache_dir_str() or self._menu_start_dir()
        out_dir_str = QFileDialog.getExistingDirectory(
            self, "Export to ParaView — pick output directory", start_dir,
        )
        if not out_dir_str:
            return
        n_files: int | None = None
        try:
            from fypa.paraview_export import export_lean_solution
            n_files = export_lean_solution(self.solution, Path(out_dir_str))
        except Exception:
            log.exception(
                "ParaView export to %s failed", out_dir_str)
            QMessageBox.critical(
                self, "Export Failed",
                "Export Failed, see Messages tab or log file",
            )
            return
        log.info("ParaView export wrote %d VTU file(s) to %s",
                 n_files, out_dir_str)
        label = getattr(self, "_settings_status_label", None)
        if label is not None:
            label.setText(
                f"<span style='color:{_T()['ok']};'>Exported {n_files} "
                f"ParaView VTU file(s) to {_esc(out_dir_str)}</span>"
            )
        self.statusBar().showMessage(
            f"Exported {n_files} ParaView VTU file(s) to {out_dir_str}", 5000)
        QMessageBox.information(
            self, "Export Successful",
            f"Export Successful — wrote {n_files} VTU file(s) to:\n\n"
            f"{out_dir_str}",
        )

    # --- Project file save / open -------------------------------------------

    def _project_pickle_paths(self) -> tuple[str | None, str | None]:
        """Best-effort ``(design_info_pkl, solve_pkl)`` cache paths for the
        loaded project. The design-info path is returned only if the file
        actually exists; the solve path is the standard cache location."""
        prjpcb = self.metadata.get("prjpcb_path") if self.metadata else None
        pcbdoc = self.metadata.get("pcbdoc_path") if self.metadata else None
        if not prjpcb:
            return None, None
        try:
            from fypa.cli import _design_info_cache_path, _solve_cache_path
            di = _design_info_cache_path(
                Path(prjpcb), Path(pcbdoc) if pcbdoc else None)
            sv = _solve_cache_path(
                Path(prjpcb), Path(pcbdoc) if pcbdoc else None)
            return (str(di) if di.exists() else None, str(sv))
        except Exception:
            return None, None

    def _prompt_project_path(self) -> Path | None:
        """Ask the user where to write the ``.fypa`` project file."""
        start = self._menu_start_dir()
        name = "project.fypa"
        if self.metadata and self.metadata.get("prjpcb_path"):
            name = Path(self.metadata["prjpcb_path"]).stem + ".fypa"
        default = str(Path(start) / name) if start else name
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save project as", default,
            "FYPA project (*.fypa);;All files (*)",
        )
        if not path_str:
            return None
        if not path_str.lower().endswith(".fypa"):
            path_str += ".fypa"
        return Path(path_str)

    def _store_viewer_settings(self, proj) -> None:
        """Fold persistable viewer state into ``proj.viewer_settings`` so the
        next :meth:`fypa.project_file.ProjectFile.save` writes it. Currently
        just the Board Features overlay colours.

        Each entry is ``{"primary": [r, g, b]}``, stored in full so a
        reopened project shows exactly the colours the user left even if a
        built-in default later changes. A ``"bottom"`` key is added only
        when the user pinned a distinct Bottom-side colour for a split
        layer — i.e. the bottom colour is written only once changed."""
        colors = getattr(self, "_overlay_colors", None)
        if not colors:
            return
        bottoms = getattr(self, "_overlay_bottom_colors", {})

        def _enc(rgb) -> list[float]:
            return [round(float(c), 6) for c in rgb]

        out: dict[str, dict] = {}
        for key, rgb in colors.items():
            entry = {"primary": _enc(rgb)}
            bot = bottoms.get(key)
            if bot is not None:
                entry["bottom"] = _enc(bot)
            out[key] = entry
        proj.viewer_settings["overlay_colors"] = out

    def _save_project(self, path: Path | None = None) -> bool:
        """Write the ``.fypa`` project file. Falls back to a Save-As prompt
        when no path is known yet. Returns True on success."""
        proj = self._ensure_project()
        if path is None:
            path = self._project_path
        if path is None:
            path = self._prompt_project_path()
            if path is None:
                return False
        path = Path(path)
        di, _sv = self._project_pickle_paths()
        if di and not proj.design_info_pickle:
            proj.design_info_pickle = di
        if self.metadata:
            proj.prjpcb_path = (proj.prjpcb_path
                                or self.metadata.get("prjpcb_path"))
            proj.pcbdoc_path = (proj.pcbdoc_path
                                or self.metadata.get("pcbdoc_path"))
        self._store_viewer_settings(proj)
        try:
            proj.save(path)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't save project",
                f"Failed to write {path}:\n\n{type(e).__name__}: {e}",
            )
            return False
        self._project_path = path
        self._project_dirty = False
        self.statusBar().showMessage(f"Saved project to {path}", 5000)
        return True

    def _save_project_and_solution(self) -> bool:
        """Write the ``.fypa`` plus the current solution. The solution goes
        to a pickle beside the ``.fypa`` (not the shared design cache, whose
        solve.pkl must stay the un-edited on-disk-project solve)."""
        path = self._project_path or self._prompt_project_path()
        if path is None:
            return False
        path = Path(path)
        solve_path = path.with_name(path.stem + "_solve.pkl")
        try:
            from fypa.cli import save_solution_file
            save_solution_file(solve_path, self.solution, self.metadata)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't save solution",
                f"Failed to write {solve_path}:\n\n{type(e).__name__}: {e}",
            )
            return False
        self._ensure_project().solve_pickle = str(solve_path)
        if not self._save_project(path):
            return False
        self._solved_since_save = False
        self.statusBar().showMessage(
            f"Saved project + solution to {path}", 5000)
        return True

    def _on_ctrl_s(self) -> None:
        """Ctrl+S → the save popup. ``S`` saves the project only; ``A``
        also writes the latest solver run (when there is an unsaved one)."""
        dlg = _ProjectSaveDialog(self, allow_all=self._solved_since_save)
        if dlg.exec() != QDialog.Accepted or dlg.choice is None:
            return
        if dlg.choice == "all" and self._solved_since_save:
            self._save_project_and_solution()
        else:
            self._save_project()

    def _on_menu_save_project_as(self) -> None:
        """File > Save Project As… → pick a new ``.fypa`` path and save."""
        path = self._prompt_project_path()
        if path is not None:
            self._save_project(path)

    def _on_menu_open_project_file(self) -> None:
        """File > Open Project File… → load a ``.fypa`` and open a viewer
        bound to it (its linked solution + editor directives)."""
        from fypa.project_file import ProjectFile
        start = self._menu_start_dir()
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open project file", start,
            "FYPA project (*.fypa);;All files (*)",
        )
        if not path_str:
            return
        try:
            proj = ProjectFile.load(Path(path_str))
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open project",
                f"Failed to load {path_str}:\n\n{type(e).__name__}: {e}",
            )
            return
        # Resolve the solution pickle: the project's own, else the design
        # cache's solve.pkl for the linked project.
        sol_path = proj.solve_pickle
        if not sol_path or not Path(sol_path).exists():
            sol_path = None
            if proj.prjpcb_path:
                try:
                    from fypa.cli import _solve_cache_path
                    cand = _solve_cache_path(
                        Path(proj.prjpcb_path),
                        Path(proj.pcbdoc_path) if proj.pcbdoc_path else None,
                    )
                    if cand.exists():
                        sol_path = str(cand)
                except Exception:
                    sol_path = None
        if not sol_path:
            QMessageBox.critical(
                self, "Couldn't open project",
                "The project file doesn't point to a readable solution "
                "pickle, and no cached solve was found.",
            )
            return
        try:
            from fypa.cli import _load_solution_pickle
            solution, metadata = _load_solution_pickle(Path(sol_path))
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open project",
                f"Failed to load the linked solution {sol_path}:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return
        try:
            new_win = PdnViewer(
                solution, metadata=metadata,
                project=proj, project_path=Path(path_str),
            )
            # Editor directives present but possibly not reflected by the
            # loaded solve — offer the resolve button so the user can bring
            # the heatmap up to date.
            if proj.editor_directives:
                new_win._set_solve_stale(True)
            _register_viewer(new_win)
            new_win.show()
            _force_native_window_icon(new_win)
            _set_window_aumid(new_win)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open viewer",
                f"Project loaded but the viewer failed to open:\n\n"
                f"{type(e).__name__}: {e}",
            )

    def _on_menu_open_solution(self) -> None:
        """File > Load Solution…  →  load a pickled LeanSolution + metadata
        and open a fresh viewer bound to it. No solve runs."""
        start_dir = self._project_cache_dir_str() or self._menu_start_dir()
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open solution pickle",
            start_dir,
            "Solution pickle (*.pkl);;All files (*)",
        )
        if not path_str:
            return
        try:
            from fypa.cli import _load_solution_pickle
            solution, metadata = _load_solution_pickle(Path(path_str))
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open solution",
                f"Failed to load {path_str}:\n\n{type(e).__name__}: {e}",
            )
            return
        try:
            new_win = PdnViewer(solution, metadata=metadata)
            _register_viewer(new_win)
            new_win.show()
            _force_native_window_icon(new_win)
            _set_window_aumid(new_win)
        except Exception as e:
            QMessageBox.critical(
                self, "Couldn't open viewer",
                f"Solution loaded but the viewer failed to open:\n\n"
                f"{type(e).__name__}: {e}",
            )

    def _on_menu_close_project(self) -> None:
        """File > Close Project  →  open a fresh launcher window and close
        this viewer. Uses the same quitOnLastWindowClosed dance as the
        launcher's _open_viewer_and_close: there's a one-tick window where
        no window is visible, which would otherwise trip the auto-quit and
        kill the whole process."""
        app = QApplication.instance()
        launcher = LauncherWindow()
        _register_viewer(launcher)
        prev_quit = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        launcher.show()
        _force_native_window_icon(launcher)
        _set_window_aumid(launcher)
        self.close()
        QTimer.singleShot(
            0, lambda: app.setQuitOnLastWindowClosed(prev_quit)
        )


    def _on_rerun_solver(self) -> None:
        """Re-solve the current project with the values in the Settings
        tab. The actual solve runs on a :class:`_SolveWorker` QThread so
        the UI stays responsive and the progress dialog can spin; the
        viewer swap happens in :meth:`_on_solve_finished` once the worker
        emits its result."""
        # 1. Validate the project path is still in metadata + on disk.
        prjpcb_str = ""
        if self.metadata:
            prjpcb_str = str(self.metadata.get("prjpcb_path") or "")
        prjpcb_path = Path(prjpcb_str) if prjpcb_str else None
        if prjpcb_path is None or not prjpcb_path.exists():
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Can't re-solve: this pickle has "
                "no project path, or the .PrjPcb is no longer on disk. Open "
                "the project via <code>FYPA.py gui &lt;.PrjPcb&gt;</code> "
                "to enable Re-run.</span>"
            )
            return

        # 2. Read the form fields into a SolveSettings + display values
        # + stackup overrides.
        try:
            (new_settings, warn_a, pct,
             stackup_overrides) = self._gather_settings_from_form()
        except ValueError as e:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Invalid input — {_esc(str(e))}</span>"
            )
            return

        # 3. Apply the new physics constants on the MAIN thread before
        # the worker starts — keeps the monkey-patch ordering unambiguous
        # (any later main-thread code looking at module constants sees
        # the new values immediately).
        new_settings.apply_to_modules()

        # 4. Lock the button so a double-click can't kick off two solves.
        self._settings_rerun_btn.setEnabled(False)
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Re-solving with new parameters…</span>"
        )

        # 5-6. Spawn the progress dialog + worker. Both the Re-run button
        # and the File > Open Project menu share this plumbing.
        # Pin the PcbDoc to whichever one this pickle was solved with so
        # re-runs of a multi-PCB project don't silently switch boards or
        # re-prompt the user.
        pcbdoc_selector = None
        if self.metadata:
            pinned = self.metadata.get("pcbdoc_path")
            if pinned:
                pcbdoc_selector = str(pinned)
        self._start_solve_worker(
            prjpcb_path, new_settings, warn_a, pct,
            stackup_overrides=stackup_overrides,
            pcbdoc_selector=pcbdoc_selector,
            dialog_title="Re-running solver",
            dialog_text=("Re-solving with new parameters…\n"
                         "This can take 10–60 s depending on board size "
                         "and mesh density."),
        )

    def _on_reload_design_info(self) -> None:
        """Re-extract the design (geometry + annotations) from the on-disk
        project ignoring the design-info cache, then re-solve with whatever
        is currently in the Settings tab. Used when the user has edited the
        .PrjPcb / .PcbDoc in Altium and wants FYPA to pick up the change
        without going via File > Import Altium Design (Clean).

        Pinned to the same PcbDoc this pickle was solved with, so multi-PCB
        projects don't silently switch boards or re-prompt the user."""
        prjpcb_str = ""
        if self.metadata:
            prjpcb_str = str(self.metadata.get("prjpcb_path") or "")
        prjpcb_path = Path(prjpcb_str) if prjpcb_str else None
        if prjpcb_path is None or not prjpcb_path.exists():
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Can't reload: this pickle has "
                "no project path, or the .PrjPcb is no longer on disk.</span>"
            )
            return

        try:
            (new_settings, warn_a, pct,
             stackup_overrides) = self._gather_settings_from_form()
        except ValueError as e:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['err']};'>Invalid input — {_esc(str(e))}</span>"
            )
            return

        new_settings.apply_to_modules()

        self._settings_rerun_btn.setEnabled(False)
        self._settings_reload_design_btn.setEnabled(False)
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Re-extracting design info and re-solving…</span>"
        )

        pcbdoc_selector = None
        if self.metadata:
            pinned = self.metadata.get("pcbdoc_path")
            if pinned:
                pcbdoc_selector = str(pinned)
        self._start_solve_worker(
            prjpcb_path, new_settings, warn_a, pct,
            stackup_overrides=stackup_overrides,
            pcbdoc_selector=pcbdoc_selector,
            use_design_cache=False,
            dialog_title="Reloading design info",
            dialog_text=("Re-extracting design info from the .PrjPcb and "
                         "re-solving…\nThis can take 10–60 s depending on "
                         "board size and mesh density."),
        )

    def _start_solve_worker(
        self, prjpcb_path: Path, settings, warn_a: float, pct: float,
        *,
        sink_overrides: dict | None = None,
        stackup_overrides: dict | None = None,
        pcbdoc_selector: str | None = None,
        use_design_cache: bool = True,
        try_solve_cache_first: bool = False,
        editor_directives: list | None = None,
        copper_names: list | None = None,
        loaded_project: object | None = None,
        dialog_title: str = "Running solver",
        dialog_text: str | None = None,
        dialog_width_scale: float = 1.44,
    ) -> None:
        """Show an indeterminate progress dialog and run :class:`_SolveWorker`
        off-thread; on success, open a fresh viewer via
        :meth:`_on_solve_finished`. Called by the Re-run button (with the
        Settings-tab form values) and by File > Import Altium Design (with
        defaults from the current viewer). Set ``use_design_cache=False`` for
        the "Clean" / Reload Design Info flows that must re-extract."""
        if dialog_text is None:
            dialog_text = (f"Loading {prjpcb_path.name} and solving…\n"
                           "This can take 10–60 s depending on board size "
                           "and mesh density.")
        # Indeterminate (min == max == 0) — the C++ Triangle mesher doesn't
        # expose a progress hook, so we show a spinning barber-pole.
        dlg = QProgressDialog(dialog_text, "Cancel", 0, 0, self)
        dlg.setWindowTitle(dialog_title)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        dlg.canceled.connect(self._on_solve_cancelled)
        dlg.show()
        QApplication.processEvents()
        # 44% wider than Qt's auto-sized width so the longer per-stage
        # status messages ("Packaging solution: building metadata…",
        # "Opening viewer…", etc.) aren't truncated. Callers can widen
        # further via ``dialog_width_scale``.
        _sz = dlg.size()
        dlg.setFixedSize(int(_sz.width() * dialog_width_scale), _sz.height())

        # Stash refs on ``self`` so the QThread + dialog survive past this
        # handler returning — Qt + Python both need them alive until the
        # signals fire.
        worker = _SolveWorker(
            prjpcb_path, settings,
            sink_overrides=sink_overrides,
            stackup_overrides=stackup_overrides,
            pcbdoc_selector=pcbdoc_selector,
            use_design_cache=use_design_cache,
            try_solve_cache_first=try_solve_cache_first,
            editor_directives=editor_directives,
            copper_names=copper_names,
            loaded_project=loaded_project,
            parent=self,
        )
        self._solve_worker = worker
        self._solve_progress_dlg = dlg
        # Wires stage_changed / substage_changed to the dialog, with a
        # live elapsed-time counter for the current stage so the user can
        # see the opaque "Meshing + solving" step is still progressing.
        self._solve_progress_updater = _SolveProgressUpdater(dlg, worker, self)

        # A resolve (editor directives present) opens the fresh viewer
        # bound to the same project file so the editor state carries over.
        if editor_directives:
            worker.finished_ok.connect(
                lambda sol, meta, loaded: self._on_resolve_finished(
                    sol, meta, loaded, warn_a, pct, settings,
                )
            )
        else:
            worker.finished_ok.connect(
                lambda sol, meta, loaded: self._on_solve_finished(
                    sol, meta, loaded, warn_a, pct, settings,
                )
            )
        worker.failed.connect(self._on_solve_failed)
        worker.finished.connect(self._cleanup_solve_worker)
        worker.start()

    def _cleanup_solve_worker(self) -> None:
        """Close the progress dialog and drop worker references.

        Fires from ``QThread.finished``, which is guaranteed regardless of
        success/failure, so this is the safe place to free both."""
        updater = getattr(self, "_solve_progress_updater", None)
        if updater is not None:
            updater.stop()
            updater.deleteLater()
            self._solve_progress_updater = None
        dlg = getattr(self, "_solve_progress_dlg", None)
        if dlg is not None:
            # QProgressDialog.close() routes through reject() → cancel(),
            # which emits canceled — and our canceled handler spawns a
            # launcher window. Drop the connection first so closing a
            # dialog whose worker finished naturally doesn't pop the
            # launcher on top of the new viewer.
            try:
                dlg.canceled.disconnect(self._on_solve_cancelled)
            except (RuntimeError, TypeError):
                pass
            dlg.close()
            self._solve_progress_dlg = None
        worker = getattr(self, "_solve_worker", None)
        if worker is not None:
            # Detach later — deleteLater is safer than direct del while
            # Qt is still emitting the finished() chain.
            worker.deleteLater()
            self._solve_worker = None

    def _on_solve_cancelled(self) -> None:
        """User clicked Cancel on the solve progress dialog. Kill the
        worker, then return to a fresh launcher window (same dance as
        File > Close Project)."""
        _abort_solve_worker(self)
        app = QApplication.instance()
        launcher = LauncherWindow()
        _register_viewer(launcher)
        prev_quit = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        launcher.show()
        _force_native_window_icon(launcher)
        _set_window_aumid(launcher)
        self.close()
        QTimer.singleShot(
            0, lambda: app.setQuitOnLastWindowClosed(prev_quit)
        )

    def _on_solve_failed(self, message: str) -> None:
        """Worker emitted ``failed``. Show the error inline in the Settings
        tab + as a modal dialog so the user can't miss it."""
        logging.getLogger(__name__).error("Solve failed: %s", message)
        # Compact one-line version for the status label; full traceback
        # in the dialog for copy-pasting.
        first_line = message.splitlines()[0] if message else "Solve failed"
        self._settings_status_label.setText(
            f"<span style='color:{_T()['err']};'>Solve failed: {_esc(first_line)}</span>"
        )
        self._settings_rerun_btn.setEnabled(True)
        reload_btn = getattr(self, "_settings_reload_design_btn", None)
        if reload_btn is not None:
            reload_btn.setEnabled(True)
        QMessageBox.critical(self, "Solve failed", message)

    def _on_solve_finished(
        self, new_solution, metadata: dict, loaded_project,
        warn_a: float, pct: float, new_settings,
    ) -> None:
        """Worker emitted ``finished_ok``. Open a fresh viewer bound to
        the new solution and close this one once Qt has fully shown the
        replacement (so the QApplication doesn't quit on the transition)."""
        log = logging.getLogger(__name__)
        # Inherit the current window's placement so loading a different
        # project doesn't snap the viewer back to the default size +
        # position.
        prev_geometry = self.geometry()
        prev_maximized = self.isMaximized()
        prev_fullscreen = self.isFullScreen()
        try:
            new_win = PdnViewer(
                new_solution,
                metadata=metadata,
                initial_settings=new_settings,
                via_current_warn_a=warn_a,
                display_percentile_high=pct,
                loaded_project=loaded_project,
            )
            # GC-pin the new window in a module-level list — PySide6's
            # QApplication property bag doesn't reliably hold Python refs
            # across signal boundaries, so the window would otherwise be
            # garbage-collected the moment this slot returns.
            _register_viewer(new_win)
            new_win.setGeometry(prev_geometry)
            if prev_fullscreen:
                new_win.showFullScreen()
            elif prev_maximized:
                new_win.showMaximized()
            else:
                # PdnViewer.__init__ sets _pending_maximize=True so the
                # first showEvent maximises the window. Inheriting the
                # previous size means we want to keep that size, so cancel
                # the deferred maximise before showing.
                new_win._pending_maximize = False
                new_win.show()
            _force_native_window_icon(new_win)
            _set_window_aumid(new_win)
            # Land on the Heatmap tab so the user immediately sees the
            # new result — they were on Settings to click Re-run, but
            # the point of re-running is to inspect the updated heatmap.
            heatmap_idx = getattr(new_win, "_heatmap_tab_index", 0)
            new_win.tabs.setCurrentIndex(heatmap_idx)
        except Exception as e:
            log.exception("Failed to open new viewer after re-solve")
            _t = _T()
            self._settings_status_label.setText(
                f"<span style='color:{_t['err']};'>Re-solve succeeded but the "
                f"new viewer failed to open: {_esc(str(e))}</span>"
            )
            self._settings_rerun_btn.setEnabled(True)
            reload_btn = getattr(self, "_settings_reload_design_btn", None)
            if reload_btn is not None:
                reload_btn.setEnabled(True)
            return

        # Brief success flash on the old window before it goes away.
        self._settings_status_label.setText(
            f"<span style='color:{_T()['ok']};'>Solve complete — reloading viewer…</span>"
        )
        # Defer one tick so Qt has fully shown the new window before this
        # one disappears. _retire_viewer (not a plain close) destroys the
        # old window and drops its Solution — a plain close() only hides it,
        # leaking a full solution into RAM on every reload.
        QTimer.singleShot(0, lambda: _retire_viewer(self))

    def _on_resolve_finished(
        self, new_solution, metadata: dict, loaded_project,
        warn_a: float, pct: float, new_settings,
    ) -> None:
        """Resolve worker finished — open a fresh viewer bound to the same
        project file so the editor directives + project path carry over.
        The freshly-solved editor rails are now normal viewable rails."""
        log = logging.getLogger(__name__)
        prev_geometry = self.geometry()
        prev_maximized = self.isMaximized()
        prev_fullscreen = self.isFullScreen()
        try:
            new_win = PdnViewer(
                new_solution,
                metadata=metadata,
                initial_settings=new_settings,
                via_current_warn_a=warn_a,
                display_percentile_high=pct,
                project=self._project,
                project_path=self._project_path,
                loaded_project=loaded_project,
            )
            _register_viewer(new_win)
            new_win.setGeometry(prev_geometry)
            if prev_fullscreen:
                new_win.showFullScreen()
            elif prev_maximized:
                new_win.showMaximized()
            else:
                new_win._pending_maximize = False
                new_win.show()
            # The resolve produced a solution not yet written to any
            # pickle — enable the Ctrl+S "save project + solver run" path.
            new_win._solved_since_save = True
            _force_native_window_icon(new_win)
            _set_window_aumid(new_win)
            heatmap_idx = getattr(new_win, "_heatmap_tab_index", 0)
            new_win.tabs.setCurrentIndex(heatmap_idx)
        except Exception as e:
            log.exception("Failed to open viewer after resolve")
            QMessageBox.critical(
                self, "Couldn't open viewer",
                f"Re-solve succeeded but the viewer failed to open:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return
        QTimer.singleShot(0, lambda: _retire_viewer(self))


    # --- Setup tab ----------------------------------------------------------

    def _build_help_tab(self) -> QWidget:
        """Static HTML reference for keyboard shortcuts + mouse controls."""
        widget = QWidget(self.tabs)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        _t = _T()
        browser.setStyleSheet(
            f"QTextBrowser {{ background-color: {_t['bg']}; color: {_t['fg']}; }}"
        )
        browser.setHtml(_help_tab_html())
        layout.addWidget(browser)
        return widget

    def _build_setup_tab(self) -> QWidget:
        """Build the Setup tab — a scrollable HTML view of everything the
        FEM was given. Helps users verify that copper thickness, conductivity,
        directive values, etc. match what they entered in Altium.

        Directive blocks render as collapsible sections; clicking the
        heading re-renders the HTML with that designator toggled into / out
        of :attr:`_expanded_directives`.
        """
        widget = QWidget(self.tabs)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

        self.setup_browser = QTextBrowser()
        self.setup_browser.setOpenExternalLinks(False)
        # `setOpenLinks(False)` lets `anchorClicked` fire without QTextBrowser
        # trying to navigate to a (nonexistent) URL.
        self.setup_browser.setOpenLinks(False)
        self.setup_browser.anchorClicked.connect(self._on_setup_anchor_clicked)
        # Pin the widget palette to the active theme so scrollbar gutter
        # etc. don't clash with the HTML body's background.
        _t = _T()
        self.setup_browser.setStyleSheet(
            f"QTextBrowser {{ background-color: {_t['bg']}; color: {_t['fg']}; }}"
        )
        self._refresh_setup_html()
        layout.addWidget(self.setup_browser)
        return widget

    def _refresh_setup_html(self) -> None:
        """Re-render the Setup tab HTML, preserving the scroll position so
        toggling a directive doesn't jump the view."""
        scroll_bar = self.setup_browser.verticalScrollBar()
        scroll_pos = scroll_bar.value()
        self.setup_browser.setHtml(_format_setup_html(
            self.solution, self.metadata, self._expanded_directives,
            phys_color_fn=self._layer_color_for,
        ))
        scroll_bar.setValue(scroll_pos)

    def _on_setup_anchor_clicked(self, url) -> None:
        """Handle clicks on toggle-anchors in the Setup tab."""
        href = url.toString()
        prefix = "toggle:"
        if not href.startswith(prefix):
            return
        # Key is the channel-aware label ("U5" or "U5#1") so each indexed
        # SOURCE/SINK channel toggles independently.
        toggle_key = href[len(prefix):]
        if toggle_key in self._expanded_directives:
            self._expanded_directives.discard(toggle_key)
        else:
            self._expanded_directives.add(toggle_key)
        self._refresh_setup_html()


    # --- Nodes tab ------------------------------------------------------------

    # Columns of the Nodes-tab table. (display label, numeric? — used for
    # tab-stop alignment and sort key.)
    # Column 0 is the per-row "Go" jump cell (a clickable text cell, same
    # as the Vias tab); the rest are normal text/numeric cells.
    _NODES_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = (
        ("",           False),
        ("Role",       False),
        ("Designator", False),
        ("Pad",        False),
        ("Net",        False),
        ("Layer",      True),
        ("X (mm)",     True),
        ("Y (mm)",     True),
        ("Voltage (V)",        True),
        ("Drop (V)",           True),
        ("|J| (A/mm)",         True),
        ("Power (W/mm^2)",     True),
        # PDN_MIN_V check (SINK only): the declared minimum acceptable rail
        # voltage, the measured-vs-declared margin, and the per-pin pass/fail
        # verdict. Cells are blank ("—") for non-SINK rows and for the SINK
        # N-terminal (the return side); the margin and status are highlighted
        # red on FAIL so a long table can be skim-audited at a glance.
        ("Min V (V)",          True),
        ("Margin (V)",         True),
        ("Status",             False),
    )

    def _build_nodes_tab(self) -> QWidget:
        """Build the Nodes tab — a sortable table of every directive node
        and its computed metrics. The filter combo lets users narrow down
        to a single role (e.g. just SINKs) when there are hundreds of nodes."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        # Filter bar — role + rail combos. Both filters apply; rows must
        # satisfy BOTH selections to be visible.
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Role:"))
        self.nodes_filter_combo = QComboBox()
        self.nodes_filter_combo.addItem("All roles")
        for role in ("SOURCE", "SINK", "RESISTOR", "REGULATOR"):
            self.nodes_filter_combo.addItem(role)
        self.nodes_filter_combo.currentTextChanged.connect(self._apply_nodes_filter)
        filter_row.addWidget(self.nodes_filter_combo)

        filter_row.addSpacing(12)
        filter_row.addWidget(QLabel("Rail:"))
        self.nodes_rail_combo = QComboBox()
        self.nodes_rail_combo.addItem("All rails")
        for r in self._rails:
            self.nodes_rail_combo.addItem(r)
        self.nodes_rail_combo.setToolTip(
            "Filter rows to nodes on the selected rail group (a primary net "
            "plus any nets bridged to it via a SERIES directive)."
        )
        self.nodes_rail_combo.currentTextChanged.connect(self._apply_nodes_filter)
        filter_row.addWidget(self.nodes_rail_combo)

        filter_row.addStretch(1)
        self.nodes_summary_label = QLabel("")
        self.nodes_summary_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; }}"
        )
        filter_row.addWidget(self.nodes_summary_label)
        outer.addLayout(filter_row)

        # Table.
        self.nodes_table = QTableWidget()
        cols = self._NODES_TABLE_COLUMNS
        self.nodes_table.setColumnCount(len(cols))
        self.nodes_table.setHorizontalHeaderLabels([c[0] for c in cols])
        self.nodes_table.setSortingEnabled(True)
        self.nodes_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.nodes_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.nodes_table.setAlternatingRowColors(True)
        self.nodes_table.verticalHeader().setVisible(False)
        self.nodes_table.horizontalHeader().setStretchLastSection(True)
        # Interactive (user-draggable). See _build_vias_tab for why
        # ResizeToContents during populate is a perf disaster — the same
        # one-shot ``resizeColumnsToContents`` after populate applies here.
        self.nodes_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive)
        # Theme-driven styling to match the rest of the viewer.
        _t = _T()
        self.nodes_table.setStyleSheet(
            f"QTableWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"               gridline-color: {_t['gridline']};"
            f"               alternate-background-color: {_t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
            f"                       padding: 4px; border: 1px solid {_t['border']}; }}"
            f"QTableWidget::item:selected {{ background-color: {_t['bg_selection']}; }}"
        )
        outer.addWidget(self.nodes_table, 1)
        # Deliberately NOT calling _populate_nodes_table() here — the row build
        # is deferred to first tab activation (see __init__ + _on_tabs_current_changed).
        return widget

    def _on_tabs_current_changed(self, index: int) -> None:
        """Lazy-populate the Nodes / Vias tables the first time the user
        opens them. On a 7 000-via board the Vias populate alone takes
        ~35 s of blocked GUI thread; doing it on initial viewer open was
        the freeze users were seeing under the "saving cache" label.
        Done once per tab — the populated flags guard against re-runs."""
        if (index == getattr(self, "_nodes_tab_index", -1)
                and not getattr(self, "_nodes_table_populated", True)):
            self._nodes_table_populated = True
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._populate_nodes_table()
            finally:
                QApplication.restoreOverrideCursor()
        elif (index == getattr(self, "_vias_tab_index", -1)
                and not getattr(self, "_vias_table_populated", True)):
            self._vias_table_populated = True
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._populate_vias_table()
            finally:
                QApplication.restoreOverrideCursor()

    def _get_or_compute_node_rows(self) -> list[dict]:
        """Run :meth:`_compute_node_report` at most once per viewer and cache
        the result. Mirrors :meth:`_get_or_compute_via_rows` — used both by
        the deferred PDN_MIN_V warning-count initialiser (so the tab title
        shows "Nodes ⚠ N" before the user ever opens the tab) and by
        :meth:`_populate_nodes_table` (so the first populate skips the
        compute cost — it's already done)."""
        cached = getattr(self, "_nodes_rows_cache", None)
        if cached is None:
            cached = self._compute_node_report()
            self._nodes_rows_cache = cached
        return cached

    def _init_nodes_warn_count(self) -> None:
        """Compute the Nodes warning count once the viewer is visible, so the
        tab title shows the alert badge before the user navigates to the tab.
        Mirrors :meth:`_init_vias_warn_count`. The warning count is the
        number of pins on a SINK with a ``PDN_MIN_V`` annotation whose
        measured voltage is below that minimum."""
        rows = self._get_or_compute_node_rows()
        warn_count = sum(1 for r in rows if r.get("status") == "FAIL")
        self._nodes_warn_count = warn_count
        self._update_nodes_tab_title(warn_count)

    def _update_nodes_tab_title(self, warn_count: int) -> None:
        """Append the warning count to the Nodes tab label so users see it
        without having to open the tab. Mirrors :meth:`_update_vias_tab_title`."""
        idx = getattr(self, "_nodes_tab_index", -1)
        if idx < 0:
            return
        title = "Nodes" if warn_count == 0 else f"Nodes ⚠ {warn_count}"
        self.tabs.setTabText(idx, title)

    def _populate_nodes_table(self) -> None:
        """Fill the Nodes table from the cached node report."""
        log = logging.getLogger(__name__)
        _t0 = time.monotonic()
        rows = self._get_or_compute_node_rows()
        log.info("Nodes populate: _compute_node_report %.2fs (%d rows)",
                 time.monotonic() - _t0, len(rows))
        _t1 = time.monotonic()
        # Sidecar in original row order so the cellClicked handler can
        # find the node dict even after the user sorts the table.
        self._nodes_rows = rows
        cols = self._NODES_TABLE_COLUMNS
        # Action-column styling, fetched once. Mirrors the Vias tab.
        _t = _T()
        action_fg = QBrush(QColor(_t["accent"]))
        # Red highlight for FAIL rows on the PDN_MIN_V check — same palette
        # the Vias tab uses for over-current cells. PASS gets a green
        # counterpart so a row that was deliberately limit-checked stands
        # out from rows where no PDN_MIN_V was declared (those stay neutral).
        fail_bg = QBrush(QColor(_t["warn_bg"]))
        fail_fg = QBrush(QColor(_t["warn_fg"]))
        pass_bg = QBrush(QColor(_t["pass_bg"]))
        pass_fg = QBrush(QColor(_t["pass_fg"]))
        status_cols = {"Min V (V)", "Margin (V)", "Status"}

        # Column 0 is the action ("Go") cell; everything else is data in
        # columns 1..N. Stays aligned with _NODES_TABLE_COLUMNS and the
        # ROLE_COL / NET_COL constants in _apply_nodes_filter.
        ACTION_COL = 0

        self.nodes_table.setSortingEnabled(False)  # disable while loading
        self.nodes_table.setRowCount(len(rows))
        fail_count = 0
        for r, row in enumerate(rows):
            # Clickable action cell. The original row index is stashed on
            # UserRole so the cellClicked handler can recover the row dict
            # even after the user re-sorts the table.
            action_item = QTableWidgetItem("Go ▶")
            action_item.setData(Qt.EditRole, float(r))
            action_item.setData(Qt.UserRole, r)
            action_item.setForeground(action_fg)
            action_item.setTextAlignment(Qt.AlignCenter)
            action_item.setToolTip(
                "Click to jump to this node in the Heatmap tab — zooms "
                "in, enables the node's layer if needed, and drops a "
                "yellow highlight ring."
            )
            self.nodes_table.setItem(r, ACTION_COL, action_item)

            cells = (
                None,  # action column placeholder; we skip it below
                row.get("role", ""),
                row.get("designator", ""),
                row.get("pad", ""),
                row.get("net", ""),
                row.get("layer_id", ""),
                row.get("x_mm"),
                row.get("y_mm"),
                row.get("voltage"),
                row.get("drop"),
                row.get("current_density"),
                row.get("power_density"),
                row.get("min_voltage"),
                row.get("margin"),
                row.get("status"),
            )
            status = row.get("status")
            is_fail = status == "FAIL"
            is_pass = status == "PASS"
            if is_fail:
                fail_count += 1
            for c, (col_label, is_numeric) in enumerate(cols):
                if c == ACTION_COL:
                    continue
                value = cells[c]
                if value is None:
                    item = QTableWidgetItem("—")
                elif is_numeric and isinstance(value, (int, float)):
                    if isinstance(value, int):
                        text = f"{value:d}"
                    else:
                        text = f"{value:.4g}"
                    item = QTableWidgetItem(text)
                    item.setData(Qt.EditRole, float(value))  # numeric sort
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    item = QTableWidgetItem(str(value))
                if col_label in status_cols:
                    if is_fail:
                        item.setBackground(fail_bg)
                        item.setForeground(fail_fg)
                    elif is_pass:
                        item.setBackground(pass_bg)
                        item.setForeground(pass_fg)
                self.nodes_table.setItem(r, c, item)
        log.info("Nodes populate: items %.2fs", time.monotonic() - _t1)
        _t2 = time.monotonic()
        self.nodes_table.setSortingEnabled(True)
        # Default sort: Role ascending (column 1 — column 0 is the "Go"
        # action cell).
        self.nodes_table.sortByColumn(1, Qt.AscendingOrder)
        log.info("Nodes populate: sort %.2fs", time.monotonic() - _t2)
        _t3 = time.monotonic()
        self.nodes_table.resizeColumnsToContents()
        log.info("Nodes populate: resizeColumnsToContents %.2fs",
                 time.monotonic() - _t3)
        # Wire the table-level click dispatcher once. See the matching
        # guard in _populate_vias_table for why disconnect() isn't used.
        if not getattr(self, "_nodes_click_handler_wired", False):
            self.nodes_table.cellClicked.connect(self._on_nodes_cell_clicked)
            self._nodes_click_handler_wired = True
        # Update the tab-title badge from the actual fail count we just
        # rendered, so it stays in sync if the deferred _init pass hasn't
        # run yet (or if the row cache was invalidated).
        self._nodes_warn_count = fail_count
        self._update_nodes_tab_title(fail_count)
        self._apply_nodes_filter()  # respect current filter
        log.info("Nodes populate: TOTAL %.2fs", time.monotonic() - _t0)

    def _on_nodes_cell_clicked(self, row: int, col: int) -> None:
        """Single-click on the action column (col 0) → jump to that node
        in the Heatmap tab. Mirrors :meth:`_on_vias_cell_clicked`."""
        if col != 0:
            return
        item = self.nodes_table.item(row, 0)
        if item is None:
            return
        orig_idx = item.data(Qt.UserRole)
        if (isinstance(orig_idx, int)
                and 0 <= orig_idx < len(getattr(self, "_nodes_rows", []))):
            self._jump_to_node(self._nodes_rows[orig_idx])

    def _apply_nodes_filter(self, *_args) -> None:
        """Hide rows that fail either the Role filter or the Rail filter.
        Both must pass for a row to remain visible."""
        role_choice = (self.nodes_filter_combo.currentText()
                       if hasattr(self, "nodes_filter_combo") else "All roles")
        rail_choice = (self.nodes_rail_combo.currentText()
                       if hasattr(self, "nodes_rail_combo") else "All rails")
        # Resolve the rail choice into the set of member net names. "All
        # rails" → no filtering; an explicit rail → the rail group's nets.
        if rail_choice == "All rails":
            allowed_nets: set[str] | None = None
        else:
            allowed_nets = set(self._rail_to_members.get(rail_choice, [rail_choice]))

        # Column indexes (must stay aligned with _NODES_TABLE_COLUMNS).
        # Column 0 is the "Go" action cell, so Role + Net shift up by one.
        ROLE_COL, NET_COL = 1, 4

        visible = 0
        for r in range(self.nodes_table.rowCount()):
            role = self.nodes_table.item(r, ROLE_COL).text() if self.nodes_table.item(r, ROLE_COL) else ""
            net = self.nodes_table.item(r, NET_COL).text() if self.nodes_table.item(r, NET_COL) else ""
            role_ok = role_choice == "All roles" or role == role_choice
            rail_ok = allowed_nets is None or net in allowed_nets
            hide = not (role_ok and rail_ok)
            self.nodes_table.setRowHidden(r, hide)
            if not hide:
                visible += 1
        self.nodes_summary_label.setText(
            f"{visible} node(s) shown out of {self.nodes_table.rowCount()} total"
        )

    def _compute_node_report(self) -> list[dict]:
        """Build one row per directive-terminal node with V / drop / |J| / P.

        Voltage and power-density are sampled from the per-
        (physical_layer, net) padne Layer's mesh using a
        ``scipy.spatial.cKDTree`` nearest-vertex lookup, batched across
        every pin. Padne adds directive-pin coupling sites as Steiner
        points to the Triangle mesher — so each pin's ``(x, y)`` IS a mesh
        vertex and the nearest-vertex value is the exact mesh-side
        potential at the pin. This replaces the original per-pin
        ``LinearTriInterpolator`` sampling, whose lazy
        ``TrapezoidMapTriFinder`` build dominated the runtime: ~3 s for
        175 pins → < 0.1 s after the refactor. ``drop`` is voltage minus
        the rail group's source voltage (max V at any directive pin on
        the same rail). Current density |J| = sqrt(power_density × sheet
        conductance).
        """
        if self.metadata is None:
            return []
        from scipy.spatial import cKDTree

        layer_index_by_pair = self._index_by_pair  # already keyed (phys, net)
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}

        # Per-(physical, net) cache of (cKDTree, potentials_1d,
        # pd_per_vertex_1d, sheet_conductance). Built lazily — only built
        # for (phys, net) combinations that actually have a pin sample.
        kdtree_cache: dict[tuple[str, str], tuple] = {}
        # Tolerance for "this pin xy matches a mesh vertex". Same value as
        # the Vias report — 0.01 mm is well below any Altium grid and
        # comfortably above float noise.
        _MATCH_TOL_MM = 0.01

        def _get_v_pd_kdtree(phys_name: str, net_name: str):
            """Return ``(tree, vs_arr, pds_arr, conductance)``, or
            ``(None, None, None, 0.0)`` if no mesh exists for this pair."""
            key = (phys_name, net_name)
            if key in kdtree_cache:
                return kdtree_cache[key]
            li = layer_index_by_pair.get(key)
            if li is None:
                kdtree_cache[key] = (None, None, None, 0.0)
                return kdtree_cache[key]
            ls = self.solution.layer_solutions[li]
            layer = self.solution.problem.layers[li]
            xs_parts, ys_parts, vs_parts, pd_parts = [], [], [], []
            for xys, tris_local, pot, pd in zip(
                ls.vertex_xys, ls.triangles,
                ls.potentials, ls.power_densities,
            ):
                n = xys.shape[0]
                if n == 0 or tris_local.size == 0:
                    continue
                # Power density is stored per-face; convert to per-vertex
                # for nearest-vertex lookup. Mirrors the original logic
                # used inside the LinearTriInterpolator path.
                if pd is not None:
                    pd_per_v = _power_density_per_vertex(
                        tris_local, pot, pd, layer.conductance, n,
                    )
                else:
                    pd_per_v = np.zeros(n, dtype=np.float64)
                # Drop orphan vertices — those not referenced by any
                # triangle. Padne pins them to V=0 to keep the linear
                # system non-singular; including them in the kdtree
                # would let a pin sample V=0 instead of the real
                # voltage when its (x,y) sits within the match
                # tolerance of an orphan.
                used = np.unique(tris_local.ravel())
                xs_parts.append(xys[used, 0])
                ys_parts.append(xys[used, 1])
                vs_parts.append(pot[used])
                pd_parts.append(pd_per_v[used])
            if not xs_parts:
                kdtree_cache[key] = (None, None, None, layer.conductance)
                return kdtree_cache[key]
            pts = np.column_stack([
                np.concatenate(xs_parts), np.concatenate(ys_parts),
            ])
            vs = np.concatenate(vs_parts)
            pds = np.concatenate(pd_parts)
            kdtree_cache[key] = (cKDTree(pts), vs, pds, layer.conductance)
            return kdtree_cache[key]

        # --- Pass 1: prep each pin + bucket sample requests by (phys, net) ---
        # preps[i] holds everything needed to assemble row i once voltages
        # and power densities for that pin are known.
        preps: list[dict] = []
        sample_requests: dict[tuple[str, str],
                              list[tuple[int, float, float]]] = {}
        for d in self.metadata.get("directives", []):
            role = d.get("role", "")
            desig = d.get("designator", "?")
            # ``label`` disambiguates multi-channel SOURCE/SINK pins
            # ("U5" vs "U5#1") in the Nodes-tab table.
            display_desig = str(d.get("label") or desig)
            schdoc = d.get("schdoc", "")
            # PDN_MIN_V is a per-directive limit on the SINK's rail (P) side.
            # Carry it only onto SINK P-terminal pins so the per-pin margin
            # check doesn't fire on the return (N) terminal or on non-sinks.
            directive_min_v = (d.get("min_voltage")
                               if role == "SINK" else None)
            for term_name, term in (d.get("terminals") or {}).items():
                pin_min_v = (directive_min_v
                             if term_name == "P" else None)
                for pin in term.get("pins", []):
                    layer_id = pin.get("layer_id")
                    net = pin.get("net", "")
                    x = pin.get("x_mm")
                    y = pin.get("y_mm")
                    phys = id_to_phys.get(layer_id)
                    prep_idx = len(preps)
                    preps.append({
                        "role": role,
                        "designator": display_desig,
                        "schdoc": schdoc,
                        "terminal": term_name,
                        "pad": pin.get("pad", ""),
                        "net": net,
                        "layer_id": layer_id,
                        "x_mm": x,
                        "y_mm": y,
                        "phys": phys,
                        "min_voltage": pin_min_v,
                    })
                    if (phys is not None and x is not None and y is not None):
                        sample_requests.setdefault((phys, net), []).append(
                            (prep_idx, float(x), float(y))
                        )

        # --- Pass 2: batched nearest-vertex lookup per (phys, net) -------
        # samples[(prep_idx)] = (voltage_or_None, pd_or_None, conductance)
        samples: dict[int, tuple[float | None, float | None, float]] = {}
        for key, reqs in sample_requests.items():
            tree, vs_arr, pds_arr, cond = _get_v_pd_kdtree(*key)
            if tree is None:
                for (p, _x, _y) in reqs:
                    samples[p] = (None, None, cond)
                continue
            pts = np.empty((len(reqs), 2), dtype=np.float64)
            for i, r in enumerate(reqs):
                pts[i, 0] = r[1]
                pts[i, 1] = r[2]
            distances, indices = tree.query(pts)
            for i, (p, _x, _y) in enumerate(reqs):
                if distances[i] > _MATCH_TOL_MM:
                    samples[p] = (None, None, cond)
                else:
                    idx = indices[i]
                    v = float(vs_arr[idx])
                    pd_v = float(pds_arr[idx])
                    samples[p] = (
                        v if np.isfinite(v) else None,
                        pd_v if np.isfinite(pd_v) else None,
                        cond,
                    )

        # --- Pass 3: assemble rows from preps + samples ------------------
        rows: list[dict] = []
        for prep_idx, prep in enumerate(preps):
            voltage, pd_val, conductance = samples.get(
                prep_idx, (None, None, 0.0)
            )
            cd_val = (math.sqrt(max(pd_val * conductance, 0.0))
                      if pd_val is not None else None)
            min_v = prep["min_voltage"]
            # Margin = actual sample - declared minimum; ``None`` whenever
            # either side is missing (no PDN_MIN_V on the directive, or the
            # mesh sample failed and the row's voltage is None). Status is
            # "PASS" when the margin is >=0 and "FAIL" when negative; left
            # ``None`` so the table cell renders as "—" otherwise.
            if min_v is None or voltage is None:
                margin = None
                status = None
            else:
                margin = voltage - min_v
                status = "PASS" if margin >= 0 else "FAIL"
            rows.append({
                "role": prep["role"],
                "designator": prep["designator"],
                "schdoc": prep["schdoc"],
                "terminal": prep["terminal"],
                "pad": prep["pad"],
                "net": prep["net"],
                "layer_id": prep["layer_id"],
                "x_mm": prep["x_mm"],
                "y_mm": prep["y_mm"],
                "voltage": voltage,
                "power_density": pd_val,
                "current_density": cd_val,
                "min_voltage": min_v,
                "margin": margin,
                "status": status,
            })

        # Now compute Drop per row: V - max(V on the same rail group).
        # We use the rail-group lookup the heatmap already builds at init.
        net_to_rail: dict[str, str] = {}
        for rail, members in self._rail_to_members.items():
            for n in members:
                net_to_rail[n] = rail
        rail_max_v: dict[str, float] = {}
        for r in rows:
            rail = net_to_rail.get(r["net"])
            if rail is None or r["voltage"] is None:
                continue
            cur = rail_max_v.get(rail)
            if cur is None or r["voltage"] > cur:
                rail_max_v[rail] = r["voltage"]
        for r in rows:
            rail = net_to_rail.get(r["net"])
            if rail is None or r["voltage"] is None or rail not in rail_max_v:
                r["drop"] = None
            else:
                r["drop"] = r["voltage"] - rail_max_v[rail]

        return rows


    # --- Vias tab ------------------------------------------------------------

    # Columns of the Vias-tab table. (display label, numeric?)
    # Column 0 is the per-row "Go" jump button (populated via
    # ``setCellWidget``); the rest are normal text/numeric cells.
    _VIAS_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = (
        ("",                 False),
        ("Net",              False),
        ("Layer span",       False),
        ("X (mm)",           True),
        ("Y (mm)",           True),
        ("Diameter (mm)",    True),
        # IPC-4761 protection / fill — e.g. "—" (none / unprotected),
        # "V (fill) · Copper", "VII (fill + cap) · Silver Epoxy".
        # A "·" suffix marks vias treated as conductively filled (their
        # per-hop R uses the parallel wall+fill model).
        ("IPC-4761 fill",    False),
        ("V top (V)",        True),
        ("V bottom (V)",     True),
        ("|ΔV| (mV)",        True),
        ("|I| max (A)",      True),
        ("Power (mW)",       True),
    )

    def _build_vias_tab(self) -> QWidget:
        """Build the Vias tab — a sortable table of every via's worst-segment
        current + power dissipation. Rows over the warning threshold get a
        red highlight on the current and power columns so high-current vias
        jump out even on a 100+ row table."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        # Filter bar — rail combo + "warnings only" toggle + summary.
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
        # Theme-driven styling — matches the Nodes table.
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
        # Deliberately NOT calling _populate_vias_table() here — the row build
        # is deferred to first tab activation (see __init__ + _on_tabs_current_changed).
        # _compute_via_report + the ~7 000 QTableWidgetItem creations took 35 s
        # on a big board, blocking the whole viewer open. Tab title's warning
        # count will appear once the user first opens the tab.
        return widget

    def _get_or_compute_via_rows(self) -> list[dict]:
        """Run :meth:`_compute_via_report` at most once per viewer and
        cache the result. Used both by the deferred warning-count
        initialiser (so the tab title shows "Vias ⚠ N" before the user
        ever opens the tab) and by :meth:`_populate_vias_table` (so the
        first table populate skips the compute cost — it's already done).
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
        embedded QPushButton — embedded widgets via ``setCellWidget`` cost
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
                "Click to jump to this via in the Heatmap tab — zooms "
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
                row.get("ipc4761_label", "—") or "—",
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
                    item = QTableWidgetItem("—")
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
        the Heatmap tab. Replaces the old per-row QPushButton — see
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
            hide = not (rail_ok and warn_ok)
            self.vias_table.setRowHidden(r, hide)
            if not hide:
                visible += 1
                if abs(cur) >= self._via_current_warn_a:
                    warn_visible += 1
        self.vias_summary_label.setText(
            f"{visible} via(s) shown of {self.vias_table.rowCount()} total — "
            f"{warn_visible} at or above {self._via_current_warn_a:g} A"
        )

    def _compute_via_report(self) -> list[dict]:
        """One row per via with per-segment current + total power dissipation.

        For each via the worst |I| across its inter-layer segments is
        reported. The voltage at the via's (x, y) is sampled on every layer
        the via touches that has copper for the via's net — those are the
        same Layer instances :func:`altium_loader._coupling_networks` built
        the via resistors between, so the segment list matches the model.
        Per-segment resistance comes from the via's own ``segments`` list in
        metadata, so a hop that crosses a thicker dielectric uses the actual
        R the FEM solved with (not a fixed value).

        Sampling uses a ``scipy.spatial.cKDTree`` of mesh vertices per
        (physical layer, net), batched across every via. This is correct
        because padne adds via coupling sites as Steiner points to the
        Triangle mesher — so the via's ``(x, y)`` IS a mesh vertex and the
        nearest-vertex potential is the exact voltage at the via. A
        cKDTree build is pure C and ~100× faster than matplotlib's
        ``TrapezoidMapTriFinder`` (which we used to build implicitly inside
        ``LinearTriInterpolator``): 24 s → ~0.3 s on a 21-layer board with
        2 800 vias.
        """
        if self.metadata is None:
            return []
        from scipy.spatial import cKDTree

        # Stackup layer ids in physical order (top → bottom). Vias span a
        # contiguous slice of this list.
        stackup_ids = [row["layer_id"]
                       for row in self.metadata.get("stackup", [])]
        # O(1) lookups beat repeated list.index() per via.
        stackup_idx: dict[int, int] = {lid: i for i, lid in enumerate(stackup_ids)}
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}

        # Per-(physical, net) cache of (cKDTree, concatenated_potentials).
        # Built lazily — we only build trees for (phys, net) combinations
        # that actually have via samples to take.
        kdtree_cache: dict[tuple[str, str], tuple] = {}
        # Threshold for "this via xy matches a mesh vertex". 0.01 mm is
        # well below any sane Altium grid + comfortably above float noise.
        # Anything farther is treated as off-mesh (returns None).
        _MATCH_TOL_MM = 0.01

        def _get_v_kdtree(phys_name: str, net_name: str):
            """Return ``(cKDTree, potentials_1d)`` for this (phys, net), or
            ``(None, None)`` if no mesh exists for the pair.

            Orphan vertices — vertices not referenced by any triangle —
            are excluded. Padne pins those to V=0 to keep the linear
            system non-singular, but they sit in the same vertex array
            as the real ones. Without filtering, a via whose
            coupling-network site was skipped (e.g. a microvia whose
            destination layer's copper doesn't quite cover the via xy)
            ends up sampling an orphan within the 0.01 mm match
            tolerance and reports V=0 instead of the rail voltage —
            which combined with the ~1 mΩ fallback hop resistance
            yields a multi-thousand-amp ghost current.
            """
            key = (phys_name, net_name)
            if key in kdtree_cache:
                return kdtree_cache[key]
            li = self._index_by_pair.get(key)
            if li is None:
                kdtree_cache[key] = (None, None)
                return (None, None)
            ls = self.solution.layer_solutions[li]
            xs_parts, ys_parts, vs_parts = [], [], []
            for xys, tris_local, pot in zip(
                ls.vertex_xys, ls.triangles, ls.potentials,
            ):
                if xys.shape[0] == 0 or tris_local.size == 0:
                    continue
                used = np.unique(tris_local.ravel())
                xs_parts.append(xys[used, 0])
                ys_parts.append(xys[used, 1])
                vs_parts.append(pot[used])
            if not xs_parts:
                kdtree_cache[key] = (None, None)
                return (None, None)
            pts = np.column_stack([
                np.concatenate(xs_parts), np.concatenate(ys_parts),
            ])
            vs = np.concatenate(vs_parts)
            kdtree_cache[key] = (cKDTree(pts), vs)
            return kdtree_cache[key]

        # --- Pass 1: pre-process every via and collect sample requests --
        # Each accepted via gets a "prep" record. For each (phys, net)
        # interpolator we'll need to hit, we record (prep_idx, lid, x, y).
        # ``preps[i]`` keeps everything we need to assemble row ``i`` once
        # the voltages are known.
        preps: list[dict] = []
        # sample_requests[(phys, net)] = list of (prep_idx, lid, x, y)
        sample_requests: dict[tuple[str, str],
                              list[tuple[int, int, float, float]]] = {}

        for via in self.metadata.get("vias", []):
            net = via.get("net", "")
            if not net or net in ("?", "NO_NET", ""):
                continue
            x = via.get("x_mm", 0.0)
            y = via.get("y_mm", 0.0)
            ls_id = via.get("layer_start")
            le_id = via.get("layer_end")
            if ls_id is None or le_id is None:
                continue
            lo, hi = (ls_id, le_id) if ls_id <= le_id else (le_id, ls_id)
            i_start = stackup_idx.get(lo)
            i_end = stackup_idx.get(hi)
            if i_start is None or i_end is None:
                continue
            span_ids = stackup_ids[i_start:i_end + 1]
            if len(span_ids) < 2:
                continue
            # Per-hop R lookup from the via's own segments list (frozenset
            # key so order-independent). Each via carries its own list
            # because R varies with drill diameter + hop length.
            # An EMPTY segments list means
            # :func:`altium_loader._coupling_networks` skipped this via
            # (no copper on >=2 layers in its span, so no resistor was
            # inserted in the FEM). Reporting a "current" for such a via
            # is meaningless — every per-hop R would come from the
            # fallback constant, while the sampled voltages would come
            # from neighbouring coupling sites, giving ghost currents
            # of thousands of amps. Skip these vias entirely.
            site_segments = via.get("segments") or []
            if not site_segments:
                continue
            # Only request voltage samples for layers that actually
            # appear as an endpoint of a real FEM segment. Sampling
            # the via's full stackup span (then computing
            # adjacent-pair currents) would invent non-physical
            # hops: e.g. a microvia at bottom↔L15 whose net also
            # has stub copper on L20 would produce a fake
            # (L15, L20, fallback_R) pair and a tiny FEM voltage
            # gradient would become a fictitious amp.
            seg_layer_ids: set[int] = set()
            for seg in site_segments:
                seg_layer_ids.add(int(seg["layer_a"]))
                seg_layer_ids.add(int(seg["layer_b"]))
            prep_idx = len(preps)
            preps.append({
                "via": via,
                "net": net,
                "x": x,
                "y": y,
                "segments": site_segments,
                "seg_layer_ids": seg_layer_ids,
            })
            for lid in seg_layer_ids:
                phys = id_to_phys.get(lid)
                if phys is None:
                    continue
                sample_requests.setdefault((phys, net), []).append(
                    (prep_idx, lid, x, y)
                )

        # --- Pass 2: batched nearest-vertex lookup per interpolator ----
        # voltages[(prep_idx, lid)] = float or None (None == off-mesh).
        voltages: dict[tuple[int, int], float | None] = {}
        for key, reqs in sample_requests.items():
            tree, vs_arr = _get_v_kdtree(*key)
            if tree is None:
                for (p, lid, _x, _y) in reqs:
                    voltages[(p, lid)] = None
                continue
            pts = np.empty((len(reqs), 2), dtype=np.float64)
            for i, r in enumerate(reqs):
                pts[i, 0] = r[2]
                pts[i, 1] = r[3]
            # Vectorised: one C call returns the nearest vertex index and
            # distance for the whole batch.
            distances, indices = tree.query(pts)
            for i, (p, lid, _x, _y) in enumerate(reqs):
                if distances[i] > _MATCH_TOL_MM:
                    voltages[(p, lid)] = None
                else:
                    v = float(vs_arr[indices[i]])
                    voltages[(p, lid)] = v if np.isfinite(v) else None

        # --- Pass 3: assemble rows --------------------------------------
        # Currents come strictly from the via's FEM-defined segments —
        # each segment is a real Resistor padne inserted, with the same
        # ``resistance_ohm`` the FEM solved with. We deliberately do NOT
        # synthesise extra hops from adjacent layers in the via's
        # stackup span: that produced phantom-amp readings on vias
        # whose net happened to have unrelated stub copper on a
        # layer between the two real endpoints (no FEM resistor at
        # that pair → fallback_r_seg → tiny ΔV / 1 mΩ → kilo-amp).
        rows: list[dict] = []
        for prep_idx, prep in enumerate(preps):
            seg_currents: list[float] = []
            total_power_W = 0.0
            sampled_v: dict[int, float] = {}
            for seg in prep["segments"]:
                lid_a = int(seg["layer_a"])
                lid_b = int(seg["layer_b"])
                r_hop = float(seg["resistance_ohm"])
                if r_hop <= 0.0:
                    continue
                v_a = voltages.get((prep_idx, lid_a))
                v_b = voltages.get((prep_idx, lid_b))
                if v_a is None or v_b is None:
                    # One end's coupling site has no usable mesh
                    # sample. Skip this hop rather than fall back to
                    # a fictitious resistance — without both real
                    # voltages the computed current is meaningless.
                    continue
                i_seg = (v_a - v_b) / r_hop
                seg_currents.append(i_seg)
                total_power_W += i_seg * i_seg * r_hop
                sampled_v[lid_a] = v_a
                sampled_v[lid_b] = v_b
            if not seg_currents:
                continue
            max_abs_I = max(abs(I) for I in seg_currents)
            # Order the sampled-voltage layers by physical stackup
            # position (top→bottom) so v_top / v_bottom and the
            # "layer_span" label name them in the natural order.
            sampled_sorted = sorted(
                sampled_v.items(),
                key=lambda kv: stackup_idx.get(kv[0], 1 << 30),
            )
            top_lid, v_top = sampled_sorted[0]
            bot_lid, v_bottom = sampled_sorted[-1]
            top_name = self._layer_id_to_name(top_lid)
            bottom_name = self._layer_id_to_name(bot_lid)
            via_dict = prep["via"]
            ipc_label = via_dict.get("ipc4761_label", "—") or "—"
            # Append a · marker on conductively-filled rows so users can
            # spot at a glance which vias' R uses the parallel-shunt model.
            if via_dict.get("is_conductive_fill"):
                ipc_label = f"{ipc_label} ·"
            rows.append({
                "net": prep["net"],
                "layer_span": f"{top_name} → {bottom_name}",
                # Sampled-and-coupled layer ids in stackup order (top
                # first). Used by the Vias-tab "Go" jump to pick which
                # physical layer to make visible in the Heatmap tab.
                "layer_ids": [lid for lid, _v in sampled_sorted],
                "x_mm": prep["x"],
                "y_mm": prep["y"],
                "diameter_mm": via_dict.get("diameter_mm"),
                "ipc4761_label": ipc_label,
                "fill_material": via_dict.get("fill_material", ""),
                "is_conductive_fill": bool(via_dict.get("is_conductive_fill")),
                "v_top": v_top,
                "v_bottom": v_bottom,
                "delta_v": v_top - v_bottom,
                "current": max_abs_I,
                "power": total_power_W,
            })
        return rows

    def _layer_id_to_name(self, layer_id: int) -> str:
        """Look up a stackup layer's human-readable name (e.g. 'Top') by id.
        Falls back to ``"L<id>"`` for unknown ids."""
        for row in (self.metadata.get("stackup", []) if self.metadata else []):
            if row.get("layer_id") == layer_id:
                return row.get("name") or f"L{layer_id}"
        return f"L{layer_id}"

    # --- Vias-tab "Go" jump action -----------------------------------------

    # Default world half-width of the zoom-in view when the user jumps to
    # a via, in mm. Picked to comfortably show the via + its immediate
    # surroundings on a typical-density board.
    _JUMP_HALF_WIDTH_MM: float = 5.0

    def _jump_to_via(self, row: dict) -> None:
        """Switch to the Heatmap tab, ensure at least one of the via's
        spanning physical layers is visible, zoom in on the via, and
        drop a yellow highlight ring at its location."""
        self._jump_to_xy(row.get("x_mm"), row.get("y_mm"),
                         row.get("layer_ids"))

    def _jump_to_node(self, row: dict) -> None:
        """Nodes-tab "Go" action — same as :meth:`_jump_to_via` but for a
        directive pin, which sits on a single physical layer."""
        lid = row.get("layer_id")
        self._jump_to_xy(row.get("x_mm"), row.get("y_mm"),
                         [lid] if lid is not None else [])

    def _jump_to_xy(self, x, y, layer_ids) -> None:
        """Switch to the Heatmap tab, ensure at least one of the given
        physical layers is visible, zoom in on ``(x, y)``, and drop a
        yellow highlight ring at that location. Shared by the Vias-tab
        and Nodes-tab "Go" actions."""
        if x is None or y is None:
            return

        # Make sure at least one of the location's physical layers is
        # checked in the layer list. If none are, tick the topmost in
        # the span (typically the user wants to see the layer the trace
        # enters from).
        layer_ids = layer_ids or []
        if layer_ids:
            id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
            phys_in_span = [id_to_phys[lid] for lid in layer_ids
                            if lid in id_to_phys]
            if phys_in_span:
                visible = set(self._visible_layers())
                if not (visible & set(phys_in_span)):
                    # Topmost in stackup wins (lowest rank). emit=False to
                    # suppress the eye's signal — we render explicitly below.
                    choice = min(phys_in_span,
                                  key=lambda p: self._phys_stackup_rank.get(p, 0))
                    self._set_layer_visible(choice, True, emit=False)

        # Compute zoom: pick mm/pixel so the highlighted region spans
        # the smaller widget dimension comfortably.
        widget_w = max(1, self._gl_viewer.width() if self._gl_viewer else 800)
        widget_h = max(1, self._gl_viewer.height() if self._gl_viewer else 600)
        half = self._JUMP_HALF_WIDTH_MM
        mm_per_pixel = (2.0 * half) / min(widget_w, widget_h)

        # Stash the highlight, switch tabs, re-render, then set view.
        # Re-render first so the layer change + markers are applied,
        # THEN move the view so the GL viewer is sized correctly.
        self._highlight_via_xy = (float(x), float(y))
        self.tabs.setCurrentIndex(self._heatmap_tab_index)
        self._render()
        self._gl_viewer.set_view_center_scale(float(x), float(y), mm_per_pixel)

    # --- Messages tab ------------------------------------------------------

    # Columns of the Messages-tab table. (display label, numeric?) — the
    # numeric flag is informational; sort keys are carried on the cell's
    # Qt.UserRole via :class:`_MessagesSortItem` so the Time column sorts
    # by epoch and the Level column by logging level number rather than
    # by the displayed string. (Setting Qt.EditRole on a QTableWidgetItem
    # would clobber the DisplayRole text — Qt aliases EditRole to
    # DisplayRole for this class — which is why we don't go that route.)
    _MESSAGES_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = (
        ("Time",    True),
        ("Level",   True),
        ("Source",  False),
        ("Message", False),
    )

    # Which level checkboxes the filter row exposes, in display order.
    # Each entry is (label, level_no). DEBUG is included so that runs
    # launched with ``FYPA -d`` can still narrow the table down. Labels
    # carry the same glyph the Level cell uses so users can match a row
    # to its filter at a glance.
    _MESSAGES_FILTER_LEVELS: tuple[tuple[str, int], ...] = (
        ("❌ Errors",   logging.ERROR),
        ("⚠ Warnings", logging.WARNING),
        ("ℹ Info",     logging.INFO),
        ("🐛 Debug",    logging.DEBUG),
    )

    @staticmethod
    def _messages_level_glyph(level: int) -> str:
        """Glyph shown in the Level cell instead of the raw level name.
        Bands rather than per-int lookups so CRITICAL (>= ERROR) and any
        custom levels in between still pick up sensible icons. The full
        level name remains accessible via the cell's tooltip."""
        if level >= logging.CRITICAL:
            return "🛑"
        if level >= logging.ERROR:
            return "❌"
        if level >= logging.WARNING:
            return "⚠"
        if level >= logging.INFO:
            return "ℹ"
        return "🐛"

    def _build_messages_tab(self) -> QWidget:
        """Build the Messages tab — a sortable, filterable table of every
        log record captured since the process started (see
        :mod:`fypa.log_buffer`). Errors, warnings, and info messages are
        all routed here so the user can audit what the loader / solver /
        editor reported without having to open the log file.

        New records arrive on whichever thread emitted them; the bridge
        :class:`QObject` re-emits via a queued signal so the row append
        always lands on the GUI thread."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        # Filter bar — one checkbox per level, plus an Auto-scroll toggle
        # and a Clear button. The Clear button drops every buffered
        # record (process-wide) so the table reflects the current state
        # rather than the cumulative session log.
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self._messages_level_boxes: dict[int, QCheckBox] = {}
        for label, level_no in self._MESSAGES_FILTER_LEVELS:
            box = QCheckBox(label)
            box.setChecked(True)
            box.toggled.connect(self._apply_messages_filter)
            filter_row.addWidget(box)
            self._messages_level_boxes[level_no] = box

        filter_row.addSpacing(12)
        self._messages_autoscroll_box = QCheckBox("Auto-scroll")
        self._messages_autoscroll_box.setChecked(True)
        self._messages_autoscroll_box.setToolTip(
            "Scroll the table to the newest message whenever a new record "
            "arrives. Disable if you want to inspect older rows without "
            "the view jumping under you."
        )
        filter_row.addWidget(self._messages_autoscroll_box)

        filter_row.addStretch(1)
        self.messages_summary_label = QLabel("")
        self.messages_summary_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; }}"
        )
        filter_row.addWidget(self.messages_summary_label)

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip(
            "Drop every buffered log record. The file log (fypa.log) is "
            "not affected."
        )
        clear_btn.clicked.connect(self._on_messages_clear)
        filter_row.addWidget(clear_btn)
        outer.addLayout(filter_row)

        # Table.
        self.messages_table = QTableWidget()
        cols = self._MESSAGES_TABLE_COLUMNS
        self.messages_table.setColumnCount(len(cols))
        self.messages_table.setHorizontalHeaderLabels([c[0] for c in cols])
        self.messages_table.setSortingEnabled(True)
        self.messages_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.messages_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.messages_table.setAlternatingRowColors(True)
        self.messages_table.verticalHeader().setVisible(False)
        # Stretch the Message column so long lines use the remaining width.
        header = self.messages_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        # Theme-driven styling — matches the Nodes / Vias tables.
        _t = _T()
        self.messages_table.setStyleSheet(
            f"QTableWidget {{ background-color: {_t['bg']}; color: {_t['fg']};"
            f"               gridline-color: {_t['gridline']};"
            f"               alternate-background-color: {_t['bg_alt']}; }}"
            f"QHeaderView::section {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
            f"                       padding: 4px; border: 1px solid {_t['border']}; }}"
            f"QTableWidget::item:selected {{ background-color: {_t['bg_selection']}; }}"
        )
        outer.addWidget(self.messages_table, 1)

        # Seed the table with everything already in the buffer (e.g. solve
        # warnings emitted before the viewer was constructed) and wire up
        # the listener for future records. Listener uses a queued signal
        # because log records may come from worker threads.
        self._install_messages_listener()
        self._populate_messages_table()
        return widget

    def _install_messages_signaller(self) -> None:
        """Create the QObject bridge that re-emits log records on the GUI
        thread. Idempotent — called both from :meth:`_install_messages_listener`
        and during the theme-driven tab rebuild so a fresh tab inherits
        the existing buffer feed."""
        if getattr(self, "_messages_signaller", None) is not None:
            return

        class _MessagesSignaller(QObject):
            recordReady = Signal(object)

        self._messages_signaller = _MessagesSignaller(self)
        # Qt.QueuedConnection: log records may fire on worker threads
        # (solve worker, mesher pool callbacks). Queueing marshals the
        # slot onto the GUI thread so QTableWidget mutations are safe.
        self._messages_signaller.recordReady.connect(
            self._on_messages_record, Qt.QueuedConnection,
        )

    def _install_messages_listener(self) -> None:
        """Register the per-viewer listener with the global log buffer.
        Safe to call repeatedly — the buffer dedupes by callable identity."""
        self._install_messages_signaller()
        signaller = self._messages_signaller
        if getattr(self, "_messages_listener", None) is None:
            # Bound closure that the log buffer can call from any thread.
            def _listener(rec: log_buffer.MessageRecord) -> None:
                # signaller may have been GC'd if the viewer is closing.
                try:
                    signaller.recordReady.emit(rec)
                except RuntimeError:
                    # Underlying QObject destroyed — listener is stale;
                    # remove ourselves so the buffer stops calling us.
                    log_buffer.remove_listener(_listener)
            self._messages_listener = _listener
        log_buffer.add_listener(self._messages_listener)

    def _populate_messages_table(self) -> None:
        """Rebuild the table from the global log buffer + the current
        filter. Called once at construction and again whenever the filter
        or the underlying buffer changes wholesale (e.g. after Clear)."""
        table = getattr(self, "messages_table", None)
        if table is None:
            return
        records = log_buffer.records()
        # Snapshot allowed levels from the checkboxes so the loop doesn't
        # re-check the widgets per row.
        allowed = self._messages_allowed_levels()

        table.setSortingEnabled(False)
        table.setRowCount(0)
        visible = 0
        for rec in records:
            if rec.level not in allowed:
                continue
            row = table.rowCount()
            table.setRowCount(row + 1)
            self._fill_messages_row(row, rec)
            visible += 1
        table.setSortingEnabled(True)
        # Default sort: newest first (Time descending). The user can
        # click any header to override.
        table.sortByColumn(0, Qt.DescendingOrder)
        # One-shot column-resize so timestamps + level + source fit
        # without truncating; the message column has stretch turned on
        # so it absorbs whatever's left.
        table.resizeColumnToContents(0)
        table.resizeColumnToContents(1)
        table.resizeColumnToContents(2)
        self._update_messages_summary(visible, len(records))

    def _on_messages_record(self, rec: log_buffer.MessageRecord) -> None:
        """Slot for the signaller — runs on the GUI thread thanks to the
        QueuedConnection. Appends one row (if the filter accepts it) and
        updates the summary label."""
        table = getattr(self, "messages_table", None)
        if table is None:
            return
        total = len(log_buffer.records())
        allowed = self._messages_allowed_levels()
        if rec.level in allowed:
            table.setSortingEnabled(False)
            row = table.rowCount()
            table.setRowCount(row + 1)
            self._fill_messages_row(row, rec)
            table.setSortingEnabled(True)
            # Preserve the user's chosen sort order by re-sorting after
            # the append. Without this, the new row stays at the bottom
            # regardless of the current sort column.
            header = table.horizontalHeader()
            table.sortByColumn(header.sortIndicatorSection(),
                               header.sortIndicatorOrder())
            if (getattr(self, "_messages_autoscroll_box", None) is not None
                    and self._messages_autoscroll_box.isChecked()):
                table.scrollToBottom()
            visible = sum(
                1 for r in range(table.rowCount())
                if not table.isRowHidden(r)
            )
            self._update_messages_summary(visible, total)
        else:
            # Record didn't pass the filter — only the totals change.
            visible = sum(
                1 for r in range(table.rowCount())
                if not table.isRowHidden(r)
            )
            self._update_messages_summary(visible, total)

    def _fill_messages_row(self, row: int,
                           rec: log_buffer.MessageRecord) -> None:
        """Populate one row's four cells from a :class:`MessageRecord`.
        Adds per-level colour to the Level cell so errors/warnings pop
        without needing to read the column text."""
        _t = _T()
        # Time cell — displays the full local-time stamp, but sort key
        # is the raw epoch so a roll past midnight (or a re-ordering
        # after Clear) still sorts chronologically. Sort key lives on
        # UserRole rather than EditRole because QTableWidgetItem aliases
        # EditRole to DisplayRole and would clobber the formatted text;
        # _MessagesSortItem.__lt__ reads UserRole instead.
        time_item = _MessagesSortItem(log_buffer.format_timestamp(rec.ts))
        time_item.setData(Qt.UserRole, float(rec.ts))
        time_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.messages_table.setItem(row, 0, time_item)

        level_item = _MessagesSortItem(self._messages_level_glyph(rec.level))
        # Numeric sort key so ERROR sits above WARNING above INFO when
        # the column is sorted descending — without it the alphabetical
        # order of the emoji codepoints leaks through. See the
        # _MessagesSortItem docstring for the EditRole-vs-DisplayRole
        # trap that motivates using UserRole here.
        level_item.setData(Qt.UserRole, int(rec.level))
        # Cell only shows the glyph; full level name lives in the tooltip
        # so users on a screen reader or hovering for clarity still get
        # the textual label.
        level_item.setToolTip(rec.level_name)
        level_item.setTextAlignment(Qt.AlignCenter)
        if rec.level >= logging.ERROR:
            level_item.setForeground(QBrush(QColor(_t["err"])))
        elif rec.level >= logging.WARNING:
            level_item.setForeground(QBrush(QColor(_t["warn"])))
        elif rec.level <= logging.DEBUG:
            level_item.setForeground(QBrush(QColor(_t["fg_dim"])))
        self.messages_table.setItem(row, 1, level_item)

        source_item = QTableWidgetItem(rec.name)
        source_item.setToolTip(rec.name)
        self.messages_table.setItem(row, 2, source_item)

        # Strip embedded newlines from the message so long multi-line
        # records (e.g. tracebacks via logging.exception) stay on one
        # row in the table. The full text is preserved in the tooltip.
        oneline = rec.message.replace("\r\n", " | ").replace("\n", " | ")
        msg_item = QTableWidgetItem(oneline)
        msg_item.setToolTip(rec.message)
        self.messages_table.setItem(row, 3, msg_item)

    def _messages_allowed_levels(self) -> set[int]:
        """Return the set of ``logging.*`` level numbers currently
        ticked in the filter row. A record passes the filter if its
        ``levelno`` is in this set."""
        boxes = getattr(self, "_messages_level_boxes", None)
        if not boxes:
            return {lvl for _, lvl in self._MESSAGES_FILTER_LEVELS}
        return {lvl for lvl, box in boxes.items() if box.isChecked()}

    def _apply_messages_filter(self, *_args) -> None:
        """Wholesale rebuild on filter change. The buffer is small
        (capped at 5 000 records), so a full rebuild stays well under
        a frame — simpler than tracking per-row visibility deltas."""
        self._populate_messages_table()

    def _on_messages_clear(self) -> None:
        """Wipe both the underlying buffer and the table. The file log
        is untouched; new records start flowing in immediately."""
        log_buffer.clear()
        table = getattr(self, "messages_table", None)
        if table is not None:
            table.setSortingEnabled(False)
            table.setRowCount(0)
            table.setSortingEnabled(True)
        self._update_messages_summary(0, 0)

    def _update_messages_summary(self, visible: int, total: int) -> None:
        """Footer counter — "N messages shown out of M total" — kept in
        sync with the table state."""
        label = getattr(self, "messages_summary_label", None)
        if label is None:
            return
        label.setText(
            f"{visible} message(s) shown out of {total} total"
        )


def _help_tab_style() -> str:
    """Build the Help-tab inline <style> block using the active theme."""
    t = current_theme()
    return (
        "<style>"
        f"  body {{ font-family: Segoe UI, sans-serif; font-size: 11pt;"
        f"         color: {t['fg']}; background-color: {t['bg']}; }}"
        f"  h2 {{ margin-top: 18px; color: {t['fg_strong']};"
        f"       border-bottom: 1px solid {t['border']}; padding-bottom: 2px; }}"
        f"  h3 {{ margin-top: 14px; color: {t['accent']}; }}"
        f"  p, li {{ color: {t['fg']}; }}"
        f"  table {{ border-collapse: collapse; margin: 6px 0;"
        f"          color: {t['fg']}; background-color: {t['bg']}; }}"
        f"  th, td {{ border: 1px solid {t['border']}; padding: 4px 10px;"
        f"           text-align: left; vertical-align: top; }}"
        f"  th {{ background-color: {t['bg_header']}; color: {t['fg_strong']}; font-weight: 600; }}"
        f"  kbd {{ background-color: {t['bg_input']}; color: {t['code']};"
        f"        border: 1px solid {t['border']}; border-radius: 3px;"
        f"        padding: 1px 6px; font-family: Consolas, monospace; font-size: 10pt; }}"
        f"  .muted {{ color: {t['fg_dim']}; }}"
        "</style>"
    )


_HELP_TAB_BODY = """

<h2>Keyboard shortcuts</h2>
<table>
  <tr><th>Key</th><th>Action</th></tr>
  <tr><td><kbd>2</kbd></td><td>Switch to 2D mode <span class='muted'>(re-fits to data)</span></td></tr>
  <tr><td><kbd>3</kbd></td><td>Switch to 3D mode <span class='muted'>(re-fits to data)</span></td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>2</kbd></td><td>Switch to 2D <i>keeping the current view</i></td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>3</kbd></td><td>Switch to 3D <i>keeping the current view</i> (top-down entry)</td></tr>
  <tr><td><kbd>0</kbd></td><td>Reset the 3D view (top-down, refit to data) <span class='muted'>— 3D mode only</span></td></tr>
  <tr><td><kbd>O</kbd></td><td>Toggle <i>Show layer outlines</i></td></tr>
  <tr><td><kbd>I</kbd></td><td>Toggle <i>Show pin markers</i></td></tr>
  <tr><td><kbd>R</kbd></td><td>Toggle <i>Show only rail net</i></td></tr>
  <tr><td><kbd>T</kbd></td><td>Toggle <i>Show cursor tooltip</i></td></tr>
  <tr><td><kbd>A</kbd></td><td>Toggle <i>Show current arrows</i></td></tr>
  <tr><td><kbd>V</kbd></td><td>Toggle <i>Heatmap vias/PTH</i> <span class='muted'>— 3D mode only</span></td></tr>
  <tr><td><kbd>M</kbd></td><td>Cycle <i>Mode</i> forward (Voltage &rarr; Voltage Drop &rarr; &hellip;)</td></tr>
  <tr><td><kbd>Shift</kbd>+<kbd>M</kbd></td><td>Cycle <i>Mode</i> backward</td></tr>
  <tr><td><kbd>H</kbd></td><td>Cycle <i>colour scheme</i> forward (Viridis &rarr; Blue&nbsp;&rarr;&nbsp;Red &rarr; &hellip;)</td></tr>
  <tr><td><kbd>Shift</kbd>+<kbd>H</kbd></td><td>Cycle <i>colour scheme</i> backward</td></tr>
  <tr><td><kbd>B</kbd></td><td>Collapse / expand the side panel</td></tr>
</table>
<p class='muted'>Shortcuts are window-scoped — they fire when the viewer
window has focus but defer to text inputs (e.g. the Min/Max boxes)
when one of those has focus.</p>

<h2>Mouse controls</h2>

<h3>Both modes</h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Right-button drag</td><td>Pan the view</td></tr>
  <tr><td>Mouse wheel</td><td>Zoom in / out (around cursor in 2D, dolly camera in 3D)</td></tr>
  <tr><td>Middle-button drag &uarr;/&darr;</td><td>Exponential zoom — drag up = zoom in, down = zoom out</td></tr>
  <tr><td>Left click</td><td>Clear the yellow jump highlight (from a Vias/Nodes-tab Go)</td></tr>
</table>

<h3>3D mode only</h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td><kbd>Shift</kbd> + right-button drag</td><td>Rotate (orbit the camera around the board centre)</td></tr>
</table>

<h3>Voltage / Voltage Drop mode only <span class='muted'>(2D only)</span></h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Hold <kbd>Shift</kbd></td><td>Anchor a voltage probe at the cursor and draw a thin white
    line from there to the live mouse position. The probe bar gains a
    <code>Difference = X V</code> readout — the live cursor's voltage
    minus the anchor's. Press only takes effect when the cursor is
    over copper that has a voltage value; release <kbd>Shift</kbd> to
    clear the line.</td></tr>
</table>

<h2>Side-panel controls</h2>
<ul>
  <li><b>Physical layers</b> &mdash; tick checkboxes to stack multiple
    copper layers in the view. Each layer has its own swatch colour
    used by the outline overlay.</li>
  <li><b>Rails</b> &mdash; tick one or more bridged rail groups to
    show their copper (e.g. <code>+3V3</code> bundles <code>+3V3</code>
    + <code>3V3_SW</code> if a series resistor / inductor links them).
    Use the <i>All Rails</i> row to toggle every rail at once.</li>
  <li><b>Mode</b> &mdash; Voltage / Voltage Drop / Current Density /
    Power Density.</li>
  <li><b>Show only rail net</b> &mdash; hide bridged sibling nets so
    you see just each selected rail's primary net.</li>
  <li><b>Show pin markers</b> &mdash; SOURCE / SINK / SERIES /
    REGULATOR / VIA overlays.</li>
  <li><b>Layer outlines</b> &mdash; the contour button on the
    <i>All Rails</i> row (hotkey <kbd>O</kbd>) traces each visible rail's
    copper polygons in the owning layer's swatch colour.</li>
  <li><b>Transparency control</b> &mdash; the white-disc icon sits on
    every Physical layers row and every Board Features row; the
    <i>All Layers</i> row carries a master copy that fans its setting
    out to every physical layer at once. Each click removes a
    quarter-slice of the disc clockwise as the row's transparency
    rises. For a physical layer the transparency fades both the rail
    heatmap mesh AND the per-layer all-copper overlay; for a Board
    Features row it fades that overlay (silkscreen / pads / components
    / vias / designators / board outline).
    <table>
      <tr><th>Click</th><th>Effect</th></tr>
      <tr><td>Click</td>
          <td>+25 %  <span class='muted'>(coarse, wraps 100&nbsp;%&nbsp;&rarr;&nbsp;0&nbsp;%)</span></td></tr>
      <tr><td><kbd>Shift</kbd>+click <i>or</i> right-click</td>
          <td>&minus;25 %  <span class='muted'>(reverse, wraps 0&nbsp;%&nbsp;&rarr;&nbsp;100&nbsp;%)</span></td></tr>
      <tr><td><kbd>Alt</kbd>+click</td>
          <td>+12.5 %  <span class='muted'>(fine, stops at 100&nbsp;%)</span></td></tr>
      <tr><td><kbd>Alt</kbd>+<kbd>Shift</kbd>+click <i>or</i> <kbd>Alt</kbd>+right-click</td>
          <td>&minus;12.5 %  <span class='muted'>(fine reverse, stops at 0&nbsp;%)</span></td></tr>
    </table></li>
  <li><b>Show cursor tooltip</b> &mdash; a small tooltip follows the
    mouse, showing the value of the current mode at that point along
    with the net and layer. Same info as the probe bar under the plot.</li>
  <li><b>3D view</b> &mdash; perspective view of the stacked layers
    with via cylinders.</li>
  <li><b>Heatmap vias/PTH</b> &mdash; colour via and plated-through-hole
    cylinders by the active mode instead of solid orange / light grey.
    Voltage / Voltage Drop interpolate along the barrel's length; Current
    Density and Power Density are constant per inter-layer segment. 3D
    mode only.</li>
  <li><b>Layer spacing slider</b> &mdash; 3D only. Scales both the
    inter-layer separation and the via cylinder length.</li>
  <li><b>Show current arrows</b> &mdash; white arrows on a regular
    pixel-spaced grid showing the direction of current flow; shaft
    length scales with &radic;|J| so weak and strong currents are
    both visible. Re-sampled on zoom / rotate. Works in 2D and 3D
    (in 3D each arrow rides the top face of its layer).</li>
  <li><b>Arrow spacing (px)</b> &mdash; pixels between adjacent
    arrows on screen. Smaller = denser arrows.</li>
  <li><b>Color scale</b> &mdash; the gradient strip is overlaid on the
    viewer's bottom-left corner (with value ticks); drag its Min / Max
    handles, type exact values in the side-panel boxes, or click the
    <b>&#8634;</b> reset button to restore the data range.</li>
</ul>

<h2>Tabs</h2>
<ul>
  <li><b>Heatmap</b> &mdash; the interactive viewport.</li>
  <li><b>Setup</b> &mdash; HTML report of the solved problem: stackup,
    physics constants, parsed PDN_* directives (collapsible), solver
    diagnostics, warnings / errors.</li>
  <li><b>Nodes</b> &mdash; sortable table of every directive node with
    its voltage, drop, current density, and power density. Filter by
    role or rail. The <b>Go &#9654;</b> button jumps to that node in
    the Heatmap tab (enables its layer, zooms in, drops a yellow
    highlight ring &mdash; left-click anywhere to clear).</li>
  <li><b>Vias</b> &mdash; sortable table of every via with worst-segment
    current + power dissipation. The <b>Go &#9654;</b> button jumps to
    that via in the Heatmap tab (enables its layer, zooms in, drops a
    yellow highlight ring &mdash; left-click anywhere to clear).
    The tab title also shows a warning count if any via current
    reaches the threshold.</li>
  <li><b>Settings</b> &mdash; tunable physics, meshing, and display
    options, plus per-layer copper thicknesses. Edits apply on the next
    solve: <b>Re-run Solver</b> re-solves with the new settings and
    opens a fresh viewer.</li>
</ul>
"""


def _help_tab_html() -> str:
    """Render the Help tab — theme-aware <style> block + static body."""
    return _help_tab_style() + _HELP_TAB_BODY


_AUMID: str = "cutreedesigns.fypa.viewer"


def _set_windows_app_user_model_id() -> None:
    """Tell Windows we are our own app (not python.exe).

    Without an explicit AppUserModelID, Windows groups the taskbar
    entry under the host interpreter (``python.exe``) and shows its
    icon there. Must be called BEFORE the first window appears.

    Silent no-op on non-Windows platforms. Logs to stderr on failure
    so the icon-not-changing case is debuggable instead of mysterious.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_AUMID)
    except Exception as e:
        sys.stderr.write(
            f"[altium_viewer] AppUserModelID setup failed ({e}); "
            "taskbar may show python.exe's icon.\n"
        )


def _force_native_window_icon(window) -> None:
    """Push the ``assets/icon.ico`` file straight into the window via
    Win32 ``WM_SETICON`` — bypasses Qt's ``setWindowIcon`` path entirely.

    Qt's setWindowIcon does the equivalent on most setups, but in some
    Python-host configurations it ends up sending an empty/wrong icon
    handle and the taskbar falls back to python.exe's snake. Calling
    ``LoadImageW(.ico)`` + ``WM_SETICON`` ourselves is the documented
    Win32 way and is bulletproof.

    Sizing strategy: load *larger* frames than the bare system metrics
    suggest (256×256 for ICON_BIG, 64×64 for ICON_SMALL by default).
    The taskbar in Windows 10/11 paints icons in slots that are bigger
    than the legacy ICON_SMALL metric (~16px) — pulling a 256/64 frame
    from our multi-res .ico lets Windows scale **down** (sharp) instead
    of scaling our 32/16 frame up (blurry). Override with the env var
    ``FYPA_ICON_BIG`` / ``FYPA_ICON_SMALL`` if needed.

    Must be called AFTER ``window.show()`` so ``winId()`` returns a
    valid HWND.
    """
    if sys.platform != "win32" or not _ICON_PATH_ICO.is_file():
        return
    debug = bool(os.environ.get("FYPA_ICON_DEBUG"))
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        ]
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        ICON_SMALL2 = 2

        # Default to the largest frame in the .ico for ICON_BIG and a
        # comfortable mid-size for ICON_SMALL. Windows scales down
        # smoothly; scaling up from 32×32 is what produced the blocky
        # taskbar icon users were seeing.
        try:
            big_size = int(os.environ.get("FYPA_ICON_BIG", "256"))
        except ValueError:
            big_size = 256
        try:
            small_size = int(os.environ.get("FYPA_ICON_SMALL", "64"))
        except ValueError:
            small_size = 64

        hwnd = int(window.winId())
        ico_big = str(_ICON_PATH_ICO)
        # ICON_SMALL drives the title bar bitmap; use the text-only
        # wordmark when available so the title bar reads "FYPA" while
        # the taskbar (ICON_BIG) still gets the fang logo.
        ico_small = (str(_ICON_PATH_ICO_TITLE)
                     if _ICON_PATH_ICO_TITLE.is_file() else ico_big)
        hicon_big = user32.LoadImageW(
            None, ico_big, IMAGE_ICON, big_size, big_size, LR_LOADFROMFILE,
        )
        hicon_small = user32.LoadImageW(
            None, ico_small, IMAGE_ICON, small_size, small_size, LR_LOADFROMFILE,
        )
        if debug:
            sys.stderr.write(
                f"[altium_viewer] icon: hwnd=0x{hwnd:x} "
                f"big={big_size}px (hicon={hicon_big!r}) "
                f"small={small_size}px (hicon={hicon_small!r})\n"
            )
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, hicon_small)
    except Exception as e:
        sys.stderr.write(
            f"[altium_viewer] native WM_SETICON failed ({e}); "
            "taskbar icon may not update.\n"
        )


def _set_window_aumid(window) -> None:
    """Bind the window's :data:`_AUMID` via ``SHGetPropertyStoreForWindow``
    + ``PKEY_AppUserModel_ID``. Per-window AUMID overrides the process
    AUMID for taskbar grouping; some Windows versions need this for the
    icon override to take effect for non-pinned launches.

    Must run AFTER ``window.show()`` — needs a valid HWND.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes, POINTER, byref, c_void_p

        # COM constants and types ---------------------------------------
        # IPropertyStore IID  886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99
        IID_IPropertyStore = (ctypes.c_ubyte * 16)(
            0xEB, 0x8E, 0x6D, 0x88,
            0xF2, 0x8C, 0x46, 0x44,
            0x8D, 0x02, 0xCD, 0xBA,
            0x1D, 0xBD, 0xCF, 0x99,
        )
        # PROPERTYKEY: fmtid (GUID) + pid (DWORD). For
        # System.AppUserModel.ID the fmtid is
        # 9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3, pid = 5
        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [
                ("fmtid", ctypes.c_ubyte * 16),
                ("pid", wintypes.DWORD),
            ]
        pkey = PROPERTYKEY()
        pkey.fmtid[:] = (
            0x55, 0x28, 0x4C, 0x9F,
            0x79, 0x9F, 0x39, 0x4B,
            0xA8, 0xD0, 0xE1, 0xD4,
            0x2D, 0xE1, 0xD5, 0xF3,
        )
        pkey.pid = 5

        # PROPVARIANT for VT_LPWSTR. The struct is 16 bytes total; we
        # just need the first 8 to encode the discriminator and pad,
        # then a pointer to the wide-string.
        VT_LPWSTR = 31
        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", wintypes.USHORT),
                ("wReserved1", wintypes.USHORT),
                ("wReserved2", wintypes.USHORT),
                ("wReserved3", wintypes.USHORT),
                ("pwszVal", wintypes.LPWSTR),
                ("padding", wintypes.LARGE_INTEGER),
            ]

        SHGetPropertyStoreForWindow = ctypes.windll.shell32.SHGetPropertyStoreForWindow
        SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND, c_void_p, POINTER(c_void_p),
        ]
        SHGetPropertyStoreForWindow.restype = ctypes.HRESULT

        store_ptr = c_void_p()
        hwnd = int(window.winId())
        hr = SHGetPropertyStoreForWindow(
            hwnd, ctypes.cast(IID_IPropertyStore, c_void_p), byref(store_ptr),
        )
        if hr != 0 or not store_ptr:
            return
        # IPropertyStore vtable layout:
        #   0: QueryInterface  1: AddRef  2: Release
        #   3: GetCount  4: GetAt  5: GetValue
        #   6: SetValue(REFPROPERTYKEY, REFPROPVARIANT)  7: Commit
        try:
            vtable = ctypes.cast(
                store_ptr.value, POINTER(POINTER(c_void_p)),
            )[0]
            SetValue = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, c_void_p,
                POINTER(PROPERTYKEY), POINTER(PROPVARIANT),
            )(vtable[6])
            Commit = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p)(vtable[7])
            Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])
            pv = PROPVARIANT()
            pv.vt = VT_LPWSTR
            pv.pwszVal = _AUMID
            SetValue(store_ptr, byref(pkey), byref(pv))
            Commit(store_ptr)
            Release(store_ptr)
        except Exception as e:
            sys.stderr.write(
                f"[altium_viewer] per-window AUMID setup failed ({e}); "
                "ignoring.\n"
            )
    except Exception as e:
        sys.stderr.write(
            f"[altium_viewer] SHGetPropertyStoreForWindow lookup failed "
            f"({e}); ignoring.\n"
        )


def main(solution, warnings_list=None, metadata=None) -> int:
    """CLI entry — show the viewer for the given Solution and run the Qt
    event loop. Returns the QApplication exit code.

    If ``solution is None``, opens an empty :class:`LauncherWindow` instead
    so the user can pick a project / pickle from the File menu.
    """
    # Windows taskbar grouping — must happen BEFORE any window is shown.
    _set_windows_app_user_model_id()
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication(sys.argv)
    # Theme — load the persisted choice (default: dark) and apply the
    # matching palette + base stylesheet to the QApplication BEFORE any
    # window is constructed, so the side panel, menubar and dialogs
    # follow the theme on every machine regardless of system palette.
    apply_app_theme(app, load_saved_theme_mode())
    # Application-wide icon (covers Qt's WM_SETICON path).
    icon = _load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    if solution is None:
        win = LauncherWindow()
    else:
        win = PdnViewer(solution, metadata=metadata)
    win.show()
    # Belt and braces: also push the .ico directly via WM_SETICON in
    # case Qt's icon path doesn't reach the taskbar, and bind our AUMID
    # to the window so Windows uses our icon for the taskbar grouping.
    _force_native_window_icon(win)
    _set_window_aumid(win)
    if owns_app:
        return app.exec()
    return 0


# --- Setup-tab HTML formatter (module-level, no Qt deps) ---------------------

def _format_setup_html(solution, metadata: dict | None,
                       expanded_directives: set[str] | frozenset[str] = frozenset(),
                       *,
                       phys_color_fn=None,
                       ) -> str:
    """Render the metadata bundle as a single HTML document for QTextBrowser.

    ``expanded_directives`` is the set of channel-aware directive labels
    ("U5" for legacy single-channel, "U5#1" for indexed multi-channel)
    whose terminal-pin tables should be shown. Any directive NOT in this
    set is rendered collapsed (heading only, click to expand).

    Falls back to a "metadata not available — re-solve with the current
    version" notice when ``metadata is None`` (legacy pickle).
    """
    if metadata is None:
        return (
            "<h2>Setup metadata not available</h2>"
            "<p>This solution pickle was saved with an older version of the "
            "tool that did not bundle setup metadata. To populate this tab, "
            "re-run the solver:</p>"
            "<pre>python FYPA.py solve YOUR.PrjPcb output.pkl</pre>"
            "<p>and then re-open the result with <code>show</code>.</p>"
        )

    parts: list[str] = []
    # Self-contained styling — colours come from the active theme dict so
    # the Setup tab tracks dark / light mode the same as the rest of the UI.
    _t = current_theme()
    parts.append(
        "<style>"
        f"body {{ font-family: Segoe UI, sans-serif; font-size: 11pt;"
        f"       color: {_t['fg']}; background-color: {_t['bg']}; }}"
        f"h2 {{ margin-top: 18px; color: {_t['fg_strong']};"
        f"     border-bottom: 1px solid {_t['border']}; padding-bottom: 2px; }}"
        f"h3 {{ margin-top: 14px; color: {_t['accent']}; }}"
        f"p  {{ color: {_t['fg']}; }}"
        f"table {{ border-collapse: collapse; margin: 6px 0;"
        f"        color: {_t['fg']}; background-color: {_t['bg']}; }}"
        f"th, td {{ border: 1px solid {_t['border']}; padding: 4px 8px;"
        f"         text-align: left; color: {_t['fg']}; }}"
        f"th {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
        f"     font-weight: 600; }}"
        f"td.num {{ text-align: right;"
        f"         font-family: Consolas, monospace; color: {_t['fg']}; }}"
        f"code {{ background-color: {_t['bg_input']}; color: {_t['code']};"
        f"       padding: 1px 4px; border-radius: 3px; }}"
        f".muted {{ color: {_t['fg_dim']}; }}"
        f".warn  {{ color: {_t['warn']}; }}"
        f".err   {{ color: {_t['err']}; }}"
        f"li {{ color: {_t['fg']}; }}"
        "</style>"
    )

    parts.append("<h2>Project</h2>")
    parts.append(f"<p><b>{_esc(metadata.get('project_name', '?'))}</b><br>"
                 f"<span class='muted'>{_esc(metadata.get('prjpcb_path', ''))}</span></p>")

    ex = metadata.get("extraction_summary", {})
    if ex:
        parts.append("<h3>Extracted records</h3>")
        parts.append("<table>"
                     f"<tr><th>tracks</th><td class='num'>{ex.get('tracks', 0):,}</td>"
                     f"<th>arcs</th><td class='num'>{ex.get('arcs', 0):,}</td>"
                     f"<th>vias</th><td class='num'>{ex.get('vias', 0):,}</td></tr>"
                     f"<tr><th>pads</th><td class='num'>{ex.get('pads', 0):,}</td>"
                     f"<th>regions</th><td class='num'>{ex.get('regions', 0):,}</td>"
                     f"<th>nets</th><td class='num'>{ex.get('nets', 0):,}</td></tr>"
                     f"<tr><th>pcb components</th><td class='num'>{ex.get('pcb_components', 0):,}</td>"
                     f"<th>sch components</th><td class='num'>{ex.get('sch_components', 0):,}</td>"
                     f"<th>enabled cu layers</th><td class='num'>{len(metadata.get('enabled_copper_layer_ids', []))}</td></tr>"
                     "</table>")

    # Stackup
    stackup = metadata.get("stackup", [])
    if stackup:
        parts.append("<h2>Copper stackup</h2>")
        parts.append("<p class='muted'>Conductance is computed per layer as "
                     "<code>copper_thickness_mm &times; conductivity_S_per_mm</code>.</p>")
        parts.append("<table>"
                     "<tr><th>id</th><th>Name</th>"
                     "<th>Cu thickness</th><th>(mil)</th><th>(oz)</th>"
                     "<th>Dielectric below</th>"
                     "<th>Sheet conductance</th><th>Sheet resistance</th>"
                     "<th>Notes</th></tr>")
        for row in stackup:
            notes = []
            if row.get("is_plane"):
                notes.append(f"PLANE on net {row.get('plane_net_name') or '?'}")
            diel_mm = row.get("dielectric_thickness_mm", 0.0) or 0.0
            diel_cell = (f"{diel_mm*1000:.1f} µm" if diel_mm > 0
                         else "<span class='muted'>—</span>")
            # Tint the id cell with the physical-layer swatch used in the
            # Heatmap tab so users can cross-reference at a glance.
            bg = phys_color_fn(row["name"]) if phys_color_fn else None
            if bg:
                fg = _contrasting_text_color(bg)
                id_cell = (f"<td class='num' style='background-color:{bg};"
                           f" color:{fg}; font-weight:bold;'>{row['layer_id']}</td>")
            else:
                id_cell = f"<td class='num'>{row['layer_id']}</td>"
            parts.append("<tr>"
                         f"{id_cell}"
                         f"<td>{_esc(row['name'])}</td>"
                         f"<td class='num'>{row['copper_thickness_mm']*1000:.3f} µm</td>"
                         f"<td class='num'>{row['copper_thickness_mil']:.3f}</td>"
                         f"<td class='num'>{row['copper_thickness_oz']:.3f}</td>"
                         f"<td class='num'>{diel_cell}</td>"
                         f"<td class='num'>{row['sheet_conductance_S']:.3f} S/sq</td>"
                         f"<td class='num'>{row['sheet_resistance_milliohm_per_sq']:.4f} mΩ/sq</td>"
                         f"<td>{_esc(', '.join(notes))}</td>"
                         "</tr>")
        parts.append("</table>")

    # Physics constants
    phys = metadata.get("physics_constants", {})
    if phys:
        # Per-hop via R varies; summarise the distribution from each via's
        # segments list so users can see the actual range the FEM used.
        seg_rs: list[float] = []
        cond_fill_count = 0
        for v in metadata.get("vias", []):
            if v.get("is_conductive_fill"):
                cond_fill_count += 1
            for seg in v.get("segments") or []:
                r = seg.get("resistance_ohm")
                if r is not None and r > 0.0:
                    seg_rs.append(float(r))
        if seg_rs:
            r_min = min(seg_rs) * 1000.0
            r_max = max(seg_rs) * 1000.0
            r_mean = (sum(seg_rs) / len(seg_rs)) * 1000.0
            via_r_cell = (f"min {r_min:.3f} / mean {r_mean:.3f} / "
                          f"max {r_max:.3f} mΩ "
                          f"<span class='muted'>(over {len(seg_rs)} segment(s))</span>")
        else:
            via_r_cell = (f"<span class='muted'>(no via segments; fallback "
                          f"= {phys.get('fallback_via_resistance_ohm', 0)*1000:.3f} mΩ)</span>")
        parts.append("<h2>Physics constants</h2>")
        parts.append("<table>"
                     f"<tr><th>Copper conductivity</th>"
                     f"<td class='num'>{phys.get('copper_conductivity_S_per_mm', 0):.3e} S/mm</td>"
                     f"<td class='muted'>= {phys.get('copper_resistivity_microohm_cm', 0):.4f} µΩ·cm "
                     f"= {phys.get('copper_resistivity_ohm_m', 0):.3e} Ω·m</td></tr>"
                     f"<tr><th>Plating thickness</th>"
                     f"<td class='num'>{phys.get('plating_thickness_mm', 0)*1000:.1f} µm</td>"
                     f"<td class='muted'>Standard plated-through-hole copper "
                     f"wall thickness (IPC-A-600 Class 2).</td></tr>"
                     f"<tr><th>Via barrel resistance (per hop)</th>"
                     f"<td>{via_r_cell}</td>"
                     f"<td class='muted'>{_esc(phys.get('note_via_resistance', ''))}</td></tr>"
                     f"<tr><th>Conductive fill resistivity</th>"
                     f"<td class='num'>{phys.get('conductive_fill_resistivity_ohm_mm', 0)*1.0e3:.3g} mΩ·mm</td>"
                     f"<td class='muted'>Applied as a parallel rod inside the "
                     f"plated barrel for vias whose IPC-4761 FILLING material "
                     f"reads as conductive — "
                     f"<b>{cond_fill_count}</b> via(s) on this board.</td></tr>"
                     f"<tr><th>Multi-pin coupling resistance</th>"
                     f"<td class='num'>{phys.get('coupling_resistance_ohm', 0)*1000:.3f} mΩ</td>"
                     f"<td class='muted'>{_esc(phys.get('note_coupling_resistance', ''))}</td></tr>"
                     "</table>")

    # Directives — each heading is a clickable toggle (collapsed by default).
    directives = metadata.get("directives", [])
    parts.append(f"<h2>PDN directives <span class='muted'>({len(directives)} — click a heading to expand)</span></h2>")
    if not directives:
        parts.append("<p class='warn'>No directives parsed — nothing to solve.</p>")
    for d in directives:
        desig = d.get("designator", "?")
        # ``label`` (e.g. "U5#1") disambiguates multi-channel SOURCE/SINK
        # so two channels on the same part get independent expand-state.
        toggle_key = str(d.get("label") or desig)
        is_open = toggle_key in expanded_directives
        arrow = "&#9662;" if is_open else "&#9656;"  # ▼ / ▶
        # The heading itself is an anchor; the viewer's anchorClicked handler
        # intercepts ``toggle:<label>`` URLs and re-renders.
        parts.append(
            f"<h3 style='margin: 8px 0;'>"
            f"<a href='toggle:{_esc(toggle_key)}' "
            f"style='color:{_t['accent']}; text-decoration:none; font-weight:600;'>"
            f"{arrow} {_esc(d.get('role','?'))} on {_esc(toggle_key)}"
            f"</a> &nbsp;"
            f"<span class='muted'>({_esc(d.get('schdoc',''))})</span> &nbsp;"
            f"<span style='color:{_t['fg']};'>{_esc(d.get('value_str',''))}</span>"
            f"</h3>"
        )
        if not is_open:
            continue
        terms = d.get("terminals", {})
        if not terms:
            continue
        parts.append("<table>"
                     "<tr><th>Terminal</th><th>Pin</th><th>Net</th>"
                     "<th>Layer</th><th>X (mm)</th><th>Y (mm)</th></tr>")
        for term_name, term in terms.items():
            pins = term.get("pins", [])
            if not pins:
                parts.append(f"<tr><td>{_esc(term_name)}</td>"
                             "<td colspan='5' class='warn'>(no pins resolved)</td></tr>")
            else:
                # Show the net the directive named (PDN_*_NET). When a SERIES
                # bridge resolved the terminal onto a different net's pads,
                # keep the named net as the headline and note the actual pad
                # net after it, so the table matches what the user authored.
                req_net = term.get("requested_net")
                for i, pin in enumerate(pins):
                    actual_net = pin.get('net', '')
                    if req_net and actual_net and actual_net != req_net:
                        net_cell = (f"<code>{_esc(req_net)}</code> "
                                    f"<span class='muted'>(via "
                                    f"{_esc(actual_net)})</span>")
                    else:
                        net_cell = f"<code>{_esc(req_net or actual_net)}</code>"
                    parts.append("<tr>"
                                 f"<td>{_esc(term_name) if i == 0 else ''}</td>"
                                 f"<td>{_esc(pin.get('pad',''))}</td>"
                                 f"<td>{net_cell}</td>"
                                 f"<td class='num'>{pin.get('layer_id','')}</td>"
                                 f"<td class='num'>{pin.get('x_mm', 0):.3f}</td>"
                                 f"<td class='num'>{pin.get('y_mm', 0):.3f}</td>"
                                 "</tr>")
        parts.append("</table>")

    # Active nets list
    active = metadata.get("active_nets", [])
    if active:
        parts.append("<h2>Active nets</h2>")
        parts.append("<p class='muted'>Only these nets have PDN parameter data set; "
                     "other nets are excluded from the FEM.</p>")
        parts.append("<p>" + ", ".join(f"<code>{_esc(n)}</code>" for n in active) + "</p>")

    # FEM stats
    fem = metadata.get("fem_stats", {}) or {}
    mesher = metadata.get("mesher_config") or {}
    solver = metadata.get("solver_stats") or {}
    if fem or mesher or solver:
        parts.append("<h2>FEM &amp; solver</h2>")
        parts.append("<table>")
        if mesher:
            parts.append(f"<tr><th>Mesher min angle</th><td class='num'>{mesher.get('minimum_angle_deg', 0):.1f}°</td></tr>"
                         f"<tr><th>Mesher max size</th><td class='num'>{mesher.get('maximum_size_mm', 0):.3f} mm</td></tr>")
        if fem:
            parts.append(f"<tr><th>Layers</th><td class='num'>{fem.get('padne_layer_count', 0)}</td></tr>"
                         f"<tr><th>Networks (total)</th><td class='num'>{fem.get('padne_network_count', 0)}</td></tr>"
                         f"<tr><th>Via coupling networks</th><td class='num'>{fem.get('via_coupling_network_count', 0)}</td></tr>")
        if solver:
            res = solver.get("residual_norm", 0.0)
            gnd = solver.get("ground_node_current_A", 0.0)
            res_flag = " class='err'" if res > 1.0 else (" class='warn'" if res > 1e-3 else "")
            gnd_flag = " class='err'" if abs(gnd) > 0.1 else (" class='warn'" if abs(gnd) > 1e-3 else "")
            parts.append(f"<tr><th>Solver residual ‖L·v − r‖</th><td{res_flag} class='num'>{res:.3e}</td></tr>"
                         f"<tr><th>Ground-node current</th><td{gnd_flag} class='num'>{gnd*1000:.4f} mA "
                         f"<span class='muted'>(should be ≈ 0 for a well-posed problem)</span></td></tr>")
        parts.append("</table>")

    # Warnings + errors
    warnings = metadata.get("annotation_warnings") or []
    errors = metadata.get("annotation_errors") or []
    if warnings or errors:
        parts.append("<h2>Annotation log</h2>")
        if errors:
            parts.append("<h3 class='err'>Errors</h3><ul>")
            for e in errors:
                parts.append(f"<li class='err'>{_esc(e)}</li>")
            parts.append("</ul>")
        if warnings:
            parts.append("<h3 class='warn'>Warnings</h3><ul>")
            for w in warnings:
                parts.append(f"<li class='warn'>{_esc(w)}</li>")
            parts.append("</ul>")

    return "".join(parts)


def _esc(s) -> str:
    """Minimal HTML escape for user-supplied strings going into the Setup tab."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _contrasting_text_color(hex_color: str) -> str:
    """Return ``#000000`` or ``#ffffff`` — whichever gives better contrast
    against ``hex_color`` (#RRGGBB). Uses the YIQ luma approximation
    (0.299 R + 0.587 G + 0.114 B); threshold 0.5 puts mid-grey on black."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#000000"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "#000000"
    luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return "#000000" if luma > 0.5 else "#ffffff"
