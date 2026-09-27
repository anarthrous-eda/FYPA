"""About dialog, log-file access, and the Help / recent-projects menus."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.assets import _load_app_icon, _load_fypa_fangs_pixmap
from fypa.viewer.prefs import (
    clear_recent_projects,
    load_recent_projects,
    recent_project_label,
    recent_project_tooltip,
)
from fypa.viewer.project_open import _open_recent_project
from fypa.viewer.theme import _T


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
        f"<p style='color:{t['accent']}; margin:2px 0;'>Altium / Gerber PDN Analyser</p>"
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




def _build_recent_projects_menu(file_menu, window) -> None:
    """Add File > Recent Projects to *file_menu* (shared by launcher + viewer)."""
    recent_menu = file_menu.addMenu("Recent &Projects")

    def _populate() -> None:
        recent_menu.clear()
        # No existence pruning here: Path.exists() on a dead network share
        # can block for seconds, and this runs on the GUI thread every time
        # the menu opens. Stale entries are dropped when opening them fails.
        entries = load_recent_projects()
        if not entries:
            empty = recent_menu.addAction("(none)")
            empty.setEnabled(False)
            return
        for entry in entries:
            # Parent to the menu, not the window — QMenu.clear() only
            # deletes actions it owns, so window-parented actions would
            # accumulate on every menu open.
            act = QAction(recent_project_label(entry), recent_menu)
            act.setStatusTip(recent_project_tooltip(entry))
            act.triggered.connect(
                lambda checked=False, e=entry: _open_recent_project(window, e)
            )
            recent_menu.addAction(act)
        recent_menu.addSeparator()
        clear_act = QAction("Clear Recent Projects", recent_menu)
        clear_act.triggered.connect(lambda: (clear_recent_projects(), _populate()))
        recent_menu.addAction(clear_act)

    recent_menu.aboutToShow.connect(_populate)




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
