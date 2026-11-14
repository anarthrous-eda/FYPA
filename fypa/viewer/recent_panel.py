"""The launcher's collapsible Recent side panel.

Lists the recent-projects list, newest first, with a pastel type chip per row:

* Altium imports get two icons - normal import and clean import.
* Gerber imports, ``.fypa`` project files and solution ``.pkl`` files get a
  single load icon. A Gerber entry replays the stored layer mapping and
  stackup, so it skips the import dialogs.

Collapsed it is a thin "Recent" strip. Expanded, a full-height handle on its
right edge (the same one the viewer's side panel uses) collapses it on a
click and resizes it on a drag. The expanded / collapsed state is
remembered across launches.

:class:`RecentDialog` shows the same list, always expanded, in a pop-up -
the main window's File > Open Recent….
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.prefs import (
    _recent_entry_exists,
    _remove_recent_project_entry,
    add_recent_projects_listener,
    clear_recent_projects,
    load_recent_panel_expanded,
    load_recent_projects,
    recent_project_label,
    recent_project_tooltip,
    remove_recent_projects_listener,
    save_recent_panel_expanded,
)
from fypa.viewer.theme import _T
from fypa.viewer.widgets import SidebarToggleButton


# Pastel chip per entry kind: (label, background). The chip text is always
# dark, so the chips read the same in the light and dark themes.
_KIND_CHIPS: dict[str, tuple[str, str]] = {
    "altium":   ("Altium",   "#f6c1b8"),
    "gerber":   ("Gerber",   "#bfe3cc"),
    "fypa":     ("Project",  "#cdc6f2"),
    "solution": ("Solution", "#f6e2a6"),
}
_CHIP_FG = "#2a2a2a"

_EXPANDED_WIDTH = 380
_MIN_WIDTH = 260
_MAX_WIDTH = 760
_STRIP_WIDTH = 26

_COL_KIND, _COL_NAME, _COL_LOAD, _COL_CLEAN = range(4)


def _entry_path_for_folder(entry: dict) -> Path | None:
    """The folder to show for "Open containing folder"."""
    kind = entry.get("kind")
    if kind in ("fypa", "solution"):
        return Path(entry["path"]).parent
    if kind == "altium":
        return Path(entry["prjpcb_path"]).parent
    if kind == "gerber":
        return Path(entry["folder"])
    return None


class _VerticalToggle(QAbstractButton):
    """The collapsed panel: a narrow full-height strip reading "Recent »"
    top to bottom. Clicking it expands the panel."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Show recent designs and solutions")
        self.setFixedWidth(_STRIP_WIDTH)
        # Run the full height of the Home tab, not just its size hint.
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_Hover, True)

    def sizeHint(self) -> QSize:  # noqa: D401 - Qt override
        return QSize(_STRIP_WIDTH, 120)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        t = _T()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(t["bg_hover"] if self.underMouse()
                                       else t["bg_alt"]))
        p.setPen(QColor(t["border"]))
        p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        p.setPen(QColor(t["fg"]))
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        # Rotate 90° so the label reads top to bottom: after this, x runs
        # down the strip and y runs across it (right to left).
        p.translate(self.width(), 0)
        p.rotate(90)
        p.drawText(QRect(12, 0, self.height() - 24, self.width()),
                   Qt.AlignLeft | Qt.AlignVCenter, "Recent  »")
        p.end()


class RecentPanel(QWidget):
    """Collapsible list of recent imports / projects / solutions.

    *window* is the launcher; the row icons call
    :func:`fypa.viewer.project_open._open_recent_project` on it.
    """

    def __init__(self, window, parent=None, *, always_expanded: bool = False,
                 on_open=None) -> None:
        """*always_expanded* (the File > Open Recent pop-up): no collapse
        strip or handle, the list fills its parent, and the launcher's
        remembered open / closed state is left alone. *on_open* is called
        just before an entry is opened (the pop-up closes itself there)."""
        super().__init__(parent)
        self._window = window
        self._entries: list[dict] = []
        self._always_expanded = bool(always_expanded)
        self._on_open = on_open
        t = _T()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._strip = _VerticalToggle(self)
        self._strip.clicked.connect(lambda: self.set_expanded(True))
        outer.addWidget(self._strip)

        self._body = QFrame(self)
        self._body.setObjectName("recentPanelBody")
        self._body.setFixedWidth(_EXPANDED_WIDTH)
        # No right border: the collapse handle beside it draws the edge.
        self._body.setStyleSheet(
            f"#recentPanelBody {{ background-color:{t['bg_alt']}; }}"
        )
        body = QVBoxLayout(self._body)
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Recent")
        title.setStyleSheet(
            f"color:{t['fg_strong']}; font-weight:600; background:transparent;"
        )
        header.addWidget(title)
        header.addStretch(1)
        body.addLayout(header)

        self._table = QTableWidget(0, 4, self._body)
        self._table.setHorizontalHeaderLabels(["Type", "Name", "Load", "Clean"])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_KIND, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_NAME, QHeaderView.Stretch)
        for col in (_COL_LOAD, _COL_CLEAN):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self._table.setColumnWidth(col, 44)
        hh.setHighlightSections(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.ElideMiddle)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setFocusPolicy(Qt.NoFocus)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.cellDoubleClicked.connect(
            lambda row, _col: self._open_row(row, clean=False)
        )
        body.addWidget(self._table, 1)

        self._empty = QLabel(
            "Nothing yet - designs you import and solutions you load "
            "will appear here."
        )
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._empty.setStyleSheet(
            f"color:{t['fg_dim']}; font-style:italic; background:transparent;"
        )
        body.addWidget(self._empty, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self._clear_btn = QToolButton(self._body)
        self._clear_btn.setText("Clear list")
        self._clear_btn.setAutoRaise(True)
        self._clear_btn.setToolTip("Forget every recent entry (files are not touched)")
        self._clear_btn.clicked.connect(self._on_clear)
        footer.addWidget(self._clear_btn)
        body.addLayout(footer)

        outer.addWidget(self._body)

        # Full-height collapse / resize handle on the expanded panel's right
        # edge, the same widget and gestures as the viewer's side panel.
        self._handle = SidebarToggleButton(self)
        self._handle.setToolTip(
            "Drag to resize the recent list · Click to hide it")
        self._handle.clicked.connect(lambda: self.set_expanded(False))
        self._handle.pressed.connect(
            lambda: setattr(self, "_drag_start_w", self._body.width()))
        self._handle.resizedBy.connect(self._on_resized_by)
        outer.addWidget(self._handle)

        # Follow writes to the recent list from anywhere (an import
        # finishing, a failed load dropping its entry, Clear list).
        add_recent_projects_listener(self.refresh)
        self.destroyed.connect(
            lambda *_a, cb=self.refresh: remove_recent_projects_listener(cb)
        )

        self.refresh()
        if self._always_expanded:
            self._strip.hide()
            self._handle.hide()
            self._body.setMinimumWidth(_MIN_WIDTH)
            self._body.setMaximumWidth(16777215)
            self._body.setStyleSheet(
                f"#recentPanelBody {{ background-color:{t['bg']}; }}")
        else:
            self._apply_expanded(load_recent_panel_expanded())

    # --- expand / collapse -------------------------------------------------

    def is_expanded(self) -> bool:
        return self._body.isVisibleTo(self)

    def set_expanded(self, expanded: bool) -> None:
        if self._always_expanded:
            return
        self._apply_expanded(expanded)
        save_recent_panel_expanded(expanded)

    def _apply_expanded(self, expanded: bool) -> None:
        self._body.setVisible(bool(expanded))
        self._handle.setVisible(bool(expanded))
        self._strip.setVisible(not expanded)

    def _on_resized_by(self, delta: int) -> None:
        """Handle drag: widen / narrow the expanded list."""
        start = getattr(self, "_drag_start_w", self._body.width())
        self._body.setFixedWidth(
            max(_MIN_WIDTH, min(_MAX_WIDTH, start + int(delta))))

    # --- content ------------------------------------------------------------

    def entries(self) -> list[dict]:
        return list(self._entries)

    def refresh(self) -> None:
        t = _T()
        self._entries = load_recent_projects()
        table = self._table
        table.clearContents()
        table.setRowCount(len(self._entries))
        style = self.style()
        load_icon = style.standardIcon(QStyle.SP_DialogOpenButton)
        clean_icon = style.standardIcon(QStyle.SP_BrowserReload)

        for row, entry in enumerate(self._entries):
            kind = entry.get("kind", "")
            exists = _recent_entry_exists(entry)
            chip_text, chip_bg = _KIND_CHIPS.get(kind, (kind or "?", "#dddddd"))

            chip = QLabel(chip_text)
            chip.setAlignment(Qt.AlignCenter)
            chip.setStyleSheet(
                f"background-color:{chip_bg}; color:{_CHIP_FG};"
                " border-radius:7px; padding:1px 7px;"
                " font-size:8pt; font-weight:600;"
            )
            if not exists:
                chip.setEnabled(False)
                chip.setStyleSheet(chip.styleSheet()
                                   + f" background-color:{t['bg_hover']};"
                                   f" color:{t['fg_dim']};")
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(2, 3, 4, 3)
            hl.addWidget(chip)
            table.setCellWidget(row, _COL_KIND, holder)

            tip = recent_project_tooltip(entry)
            item = QTableWidgetItem(recent_project_label(entry))
            if not exists:
                f = item.font()
                f.setItalic(True)
                item.setFont(f)
                item.setForeground(QColor(t["fg_dim"]))
                tip += "\n\nNot found - opening it removes it from the list."
            item.setToolTip(tip)
            table.setItem(row, _COL_NAME, item)

            if kind == "altium":
                table.setCellWidget(row, _COL_LOAD, self._icon_button(
                    load_icon, "Import (reuse cached design info / solve)",
                    row, clean=False))
                table.setCellWidget(row, _COL_CLEAN, self._icon_button(
                    clean_icon, "Clean import (ignore caches, re-extract)",
                    row, clean=True))
            else:
                tips = {
                    "gerber": "Re-import these Gerber files with the same "
                              "layer mapping and stackup",
                    "fypa": "Open this project file",
                    "solution": "Load this solution",
                }
                table.setCellWidget(row, _COL_LOAD, self._icon_button(
                    load_icon, tips.get(kind, "Open"), row, clean=False))

        has_rows = bool(self._entries)
        table.setVisible(has_rows)
        self._empty.setVisible(not has_rows)
        self._clear_btn.setEnabled(has_rows)

    def _icon_button(self, icon, tip: str, row: int, *, clean: bool) -> QWidget:
        btn = QToolButton()
        btn.setIcon(icon)
        btn.setIconSize(QSize(16, 16))
        btn.setAutoRaise(True)
        btn.setToolTip(tip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _c=False, r=row, cl=clean: self._open_row(r, clean=cl))
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setAlignment(Qt.AlignCenter)
        hl.addWidget(btn)
        return holder

    # --- actions ------------------------------------------------------------

    def _open_row(self, row: int, *, clean: bool) -> None:
        if not 0 <= row < len(self._entries):
            return
        from fypa.viewer.project_open import _open_recent_project
        entry = dict(self._entries[row])
        if self._on_open is not None:
            self._on_open()
        # Defer one tick: the open can rewrite the recent list (a missing
        # file drops its entry), and the refresh that follows rebuilds the
        # row widgets - including the button whose click we're still inside.
        QTimer.singleShot(
            0, lambda: _open_recent_project(self._window, entry, clean=clean),
        )

    def _on_context_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if not 0 <= row < len(self._entries):
            return
        entry = self._entries[row]
        menu = QMenu(self)
        if entry.get("kind") == "altium":
            menu.addAction("Import", lambda: self._open_row(row, clean=False))
            menu.addAction("Clean import", lambda: self._open_row(row, clean=True))
        else:
            menu.addAction("Open", lambda: self._open_row(row, clean=False))
        folder = _entry_path_for_folder(entry)
        if folder is not None and folder.exists():
            menu.addAction(
                "Open containing folder",
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))),
            )
        menu.addSeparator()
        menu.addAction("Remove from list",
                       lambda: _remove_recent_project_entry(entry))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_clear(self) -> None:
        if not self._entries:
            return
        ans = QMessageBox.question(
            self, "Clear recent list",
            "Forget all recent designs and solutions?\n"
            "(No files are deleted.)",
        )
        if ans == QMessageBox.Yes:
            clear_recent_projects()


class RecentDialog(QDialog):
    """File > Open Recent… in the main window: the launcher's Recent list
    in a pop-up. Opening an entry closes the pop-up first, then loads it
    into *window* the same way the launcher's panel does."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.setWindowTitle("Open Recent")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = RecentPanel(window, self, always_expanded=True,
                                 on_open=self.accept)
        layout.addWidget(self.panel)
        self.resize(560, 480)
