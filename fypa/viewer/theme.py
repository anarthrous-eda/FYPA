"""Dark / light theme presets, the app palette and stylesheet."""
from __future__ import annotations

import logging
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from fypa.viewer.prefs import _THEME_QS_APP, _THEME_QS_KEY, _THEME_QS_ORG


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
        "eye_partial":     "#b8b8b8",
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
        "eye_partial":     "#5c5c5c",
    },
}



_current_theme_mode: str = "dark"



# The OS-level colour scheme as Qt reported it at first theme application,
# captured BEFORE we override it via ``setColorScheme``. The native OS file
# dialog always follows the OS scheme (it ignores our override), so we use it
# only when the OS already matches our app theme; otherwise we fall back to
# Qt's own dialog, which inherits our palette and stays on-theme. ``None`` until
# the first :func:`apply_app_theme` call.
_os_color_scheme = None




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
    global _current_theme_mode, _os_color_scheme
    if mode is not None and mode in _THEME_PRESETS:
        _current_theme_mode = mode
    try:
        app.setStyle("Fusion")
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not set the Fusion style (%s); keeping the platform default.", e,
        )
    # Capture the OS scheme ONCE, before the setColorScheme override below makes
    # styleHints().colorScheme() report our forced value. Used by
    # :func:`_file_dialog_options` to decide native vs. Qt-own file dialogs.
    if _os_color_scheme is None:
        try:
            _os_color_scheme = app.styleHints().colorScheme()
        except Exception:
            _os_color_scheme = None
    # Declare our chosen colour scheme to Qt's windowing integration BEFORE the
    # palette is installed (setColorScheme regenerates the style palette, so do
    # it first and let our explicit palette below win). Without this, Qt leaves
    # the native window title bars and native file dialogs following the OS
    # light/dark setting — so on a light-mode machine the app comes up with a
    # light title bar / light file pickers even though our content palette is
    # dark. Forcing the scheme keeps the app looking the same on every machine,
    # matching the explicit palette. Guarded: setColorScheme is Qt 6.8+.
    try:
        scheme = (Qt.ColorScheme.Light if _current_theme_mode == "light"
                  else Qt.ColorScheme.Dark)
        app.styleHints().setColorScheme(scheme)
    except (AttributeError, TypeError) as e:
        logging.getLogger(__name__).debug(
            "Could not set the application colour scheme (%s); native title "
            "bars / dialogs will follow the OS theme.", e,
        )
    app.setPalette(_build_app_palette(_current_theme_mode))
    app.setStyleSheet(_build_app_stylesheet(_current_theme_mode))




def _native_file_dialog_on_theme() -> bool:
    """True when the native OS file dialog will match our app theme.

    The native (shell) file dialog follows the OS light/dark setting and
    ignores the colour scheme we force in :func:`apply_app_theme`. So it only
    looks on-theme when the OS scheme already matches our chosen theme. When it
    doesn't (e.g. a dark app on a light-mode Windows), callers should use Qt's
    own dialog instead so the picker inherits our palette. If the OS scheme is
    unknown, trust the native dialog (the common, no-regression case)."""
    want_light = (_current_theme_mode == "light")
    if _os_color_scheme == Qt.ColorScheme.Light:
        return want_light
    if _os_color_scheme == Qt.ColorScheme.Dark:
        return not want_light
    return True




def _file_dialog_options():
    """Options for the QFileDialog static helpers.

    Always use the native Windows shell picker (the Excel/File-Explorer dialog)
    so the file-open experience matches other Windows apps. The native dialog
    follows the OS light/dark setting rather than the app's forced theme, so on
    a machine whose OS scheme differs from the app theme the picker may look
    light against a dark app (or vice-versa) — an accepted trade-off for the
    familiar shell dialog. :func:`_native_file_dialog_on_theme` is retained for
    callers that want to make that theme-match decision explicitly."""
    return QFileDialog.Option(0)
