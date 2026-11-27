"""The menu-bar chip that says a What-If is on screen, on every tab.

The heatmap's orange banner only shows on the Board, yet the What-If panel's
Show buttons change what EVERY tab shows. The chip sits on the menu bar just
right of Global Focus, so it is in view whichever tab is open::

    File  View  Reports  Help   GLOBAL FOCUS Off   VIEWING WHAT-IF  Vias 1  x

* What-if view: "VIEWING WHAT-IF <name>" - every tab shows that what-if.
* Difference view: "WHAT-IF DIFFERENCE <name> − base" - the heatmap shows
  the difference, the other tabs show the BASE result (said in its tooltip).
* Base view: hidden.

A click opens the What-If panel on the Board; the x goes back to Base.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QIcon
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from fypa.viewer.global_focus import make_close_pixmap
from fypa.viewer.theme import _T, current_theme_mode

# The What-If orange (the viewport What-If button's): a touch deeper on the
# light theme so it reads against near-white.
ORANGE_DARK = "#f4b47c"
ORANGE_LIGHT = "#e08a45"


def whatif_chip_text(view: str, name: str) -> tuple[str, str, str]:
    """``(caption, name text, tooltip)`` for the chip in ``view``; empty
    strings for the base view (chip hidden)."""
    if view == "whatif":
        return ("VIEWING WHAT-IF", name,
                f"Every tab shows the what-if “{name}”, not the base "
                "design.\nClick to open the What-If panel; × goes back to "
                "Base.")
    if view == "diff":
        return ("WHAT-IF DIFFERENCE", f"{name} − base",
                f"The Board heatmap shows “{name}” minus the base. The "
                "other tabs show the BASE result - choose What-if in the "
                "What-If panel to see the what-if there.\nClick to open the "
                "What-If panel; × goes back to Base.")
    return "", "", ""


class WhatIfViewChip(QWidget):
    """Caption + what-if name + x. :attr:`open_requested` for a click
    anywhere but the x, :attr:`base_requested` for the x."""

    open_requested = Signal()
    base_requested = Signal()

    _CLOSE_PX = 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fypaWhatIfChip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 4, 2)
        lay.setSpacing(5)
        self._caption = QLabel(self)
        cap_font = QFont(self.font())
        cap_font.setPointSizeF(7.0)
        cap_font.setBold(True)
        cap_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        self._caption.setFont(cap_font)
        self._name = QLabel(self)
        name_font = QFont(self.font())
        name_font.setBold(True)
        self._name.setFont(name_font)
        self._close = QToolButton(self)
        self._close.setAutoRaise(True)
        self._close.setFocusPolicy(Qt.NoFocus)
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setToolTip("Back to Base - every tab shows the design's "
                               "own result again")
        self._close.clicked.connect(self.base_requested)
        for w in (self._caption, self._name):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lay.addWidget(w, 0, Qt.AlignVCenter)
        lay.addWidget(self._close, 0, Qt.AlignVCenter)
        self._view = "base"
        self.setVisible(False)

    def caption_text(self) -> str:
        return self._caption.text()

    def name_text(self) -> str:
        return self._name.text()

    def close_button(self) -> QToolButton:
        return self._close

    def set_view(self, view: str, name: str) -> None:
        """Show the chip for ``view`` ("whatif" / "diff") or hide it."""
        caption, text, tip = whatif_chip_text(view, name)
        self._view = view
        self._caption.setText(caption)
        self._name.setText(text)
        self.setToolTip(tip)
        self.restyle()
        self.setVisible(bool(caption))

    def restyle(self) -> None:
        t = _T()
        orange = ORANGE_DARK if current_theme_mode() == "dark" else ORANGE_LIGHT
        self._caption.setStyleSheet(f"QLabel {{ color: {orange}; }}")
        self._name.setStyleSheet(f"QLabel {{ color: {orange}; }}")
        fm = QFontMetricsF(self._name.font())
        line_h = max(1, self._name.sizeHint().height())
        text_top = (line_h - fm.height()) / 2.0
        cap_mid = text_top + fm.ascent() - fm.capHeight() / 2.0 - 1.0
        arm = max(2.5, fm.capHeight() * 0.36)
        w = self._CLOSE_PX
        icon = QIcon()
        for mode, colour in ((QIcon.Normal, orange),
                             (QIcon.Active, t["fg_strong"])):
            icon.addPixmap(make_close_pixmap(w, line_h, cap_mid, arm, colour),
                           mode)
        self._close.setIcon(icon)
        self._close.setIconSize(QSize(w, line_h))
        self._close.setFixedSize(w + 4, line_h)
        self.setStyleSheet(
            f"QWidget#fypaWhatIfChip {{ border: 1px solid {orange};"
            f" border-radius: 3px; background: transparent; }}"
            f"QWidget#fypaWhatIfChip:hover {{ background: {t['bg_hover']}; }}"
            f"QToolButton {{ border: none; padding: 0; margin: 0;"
            f" min-width: 0; min-height: 0; background: transparent; }}")
        self.adjustSize()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt name
        if event.button() == Qt.LeftButton:
            self.open_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)
