"""The panel a tab shows while it has nothing to show yet.

One look for every such tab (Topology and the results tabs), with a title,
a sentence on what the tab will show, and the action that gets it there:

* ``"setup"`` - nothing is set up: **Set up sources and sinks…** opens the
  setup chooser, and a line of links goes straight to a route.
* ``"solve"`` - set up but not solved: **Solve**.
* ``None`` - nothing the user can press here (a Gerber import missing the
  data the tab needs, a failed mesh): just the explanation.

The panel only reports what was pressed (``on_action``); the viewer runs it.
Actions: ``"chooser"``, ``"solve"`` and the route names in
:mod:`fypa.viewer.setup_chooser`.
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.setup_chooser import ROUTE_AUTO, ROUTE_BOARD, ROUTE_MANUAL
from fypa.viewer.theme import _T

ACTION_SETUP = "setup"
ACTION_SOLVE = "solve"

SETUP_TITLE = "No sources or sinks set up yet"
SOLVE_TITLE = "Not solved yet"

_ROUTE_LINKS = (
    (ROUTE_BOARD, "Place on the board"),
    (ROUTE_AUTO, "Auto setup"),
    (ROUTE_MANUAL, "Manual setup"),
)

_MAX_WIDTH_PX = 520


class EmptyStatePanel(QWidget):
    """Title, explanation and (optionally) the action that fills the tab."""

    def __init__(self, on_action: Callable[[str], None] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_action = on_action
        self._title_text = ""
        self._body_text = ""
        self.action: str | None = None

        t = _T()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        col = QWidget()
        col.setMaximumWidth(_MAX_WIDTH_PX)
        col.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self._title = QLabel("")
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setWordWrap(True)
        self._title.setStyleSheet(
            f"QLabel {{ color: {t['fg_strong']}; font-size: 13pt;"
            " font-weight: 600; background: transparent; }}")
        lay.addWidget(self._title)

        self._body = QLabel("")
        self._body.setAlignment(Qt.AlignCenter)
        self._body.setWordWrap(True)
        self._body.setStyleSheet(
            f"QLabel {{ color: {t['fg_muted']}; background: transparent; }}")
        lay.addWidget(self._body)

        self.button = QPushButton("")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setStyleSheet(
            f"QPushButton {{ background-color: {t['accent_btn']};"
            f" color: {t['fg_strong']}; border: 1px solid {t['accent_btn_hov']};"
            " border-radius: 6px; padding: 7px 18px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {t['accent_btn_hov']}; }}")
        self.button.clicked.connect(self._on_button)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(self.button)
        brow.addStretch(1)
        lay.addSpacing(4)
        lay.addLayout(brow)

        self._links = QLabel("")
        self._links.setAlignment(Qt.AlignCenter)
        self._links.setWordWrap(True)
        self._links.setTextFormat(Qt.RichText)
        self._links.setStyleSheet(
            f"QLabel {{ color: {t['fg_dim']}; background: transparent; }}")
        self._links.linkActivated.connect(self._emit)
        lay.addWidget(self._links)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(col, 10)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        self.set_state("", "", None)

    def set_state(self, title: str, body: str, action: str | None, *,
                  has_schematic: bool = True) -> None:
        """Show ``title`` / ``body`` and the button for ``action``. With the
        setup action, ``has_schematic`` False (Gerber) leaves only the board
        among the route links - the other routes need a netlist."""
        self._title_text, self._body_text, self.action = title, body, action
        self._title.setText(title)
        self._title.setVisible(bool(title))
        self._body.setText(body)
        self._body.setVisible(bool(body))
        if action == ACTION_SETUP:
            self.button.setText("Set up sources and sinks…")
            self.button.setToolTip(
                "The ways to define where power enters the board and where it "
                "is drawn, and when each one fits.")
        elif action == ACTION_SOLVE:
            self.button.setText("Solve")
            self.button.setToolTip("Run the solver with the current setup.")
        self.button.setVisible(action in (ACTION_SETUP, ACTION_SOLVE))

        links = ""
        if action == ACTION_SETUP:
            accent = _T()["accent"]
            items = [(r, label) for r, label in _ROUTE_LINKS
                     if has_schematic or r == ROUTE_BOARD]
            links = "Or go straight to: " + " &nbsp;·&nbsp; ".join(
                f"<a href='{r}' style='color:{accent}; text-decoration:none;'>"
                f"{label}</a>" for r, label in items)
        self._links.setText(links)
        self._links.setVisible(bool(links))

    def text(self) -> str:
        """Plain text of what the panel says (title, then body)."""
        return " ".join(s for s in (
            f"{self._title_text}." if self._title_text else "",
            self._body_text) if s)

    def _on_button(self) -> None:
        if self.action == ACTION_SETUP:
            self._emit("chooser")
        elif self.action == ACTION_SOLVE:
            self._emit("solve")

    def _emit(self, what: str) -> None:
        if self._on_action is not None:
            self._on_action(what)


def run_empty_state_action(window, what: str) -> None:
    """Run what an :class:`EmptyStatePanel` on ``window`` (a viewer) asked."""
    from fypa.viewer.setup_chooser import open_setup_chooser, run_setup_route

    if what == "chooser":
        open_setup_chooser(window)
    elif what == "solve":
        window._on_resolve_clicked()
    else:
        run_setup_route(window, what)


__all__ = [
    "ACTION_SETUP", "ACTION_SOLVE", "EmptyStatePanel", "SETUP_TITLE",
    "SOLVE_TITLE", "run_empty_state_action",
]
