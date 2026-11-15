"""Grouped navigation strip that stands in for the main window's tab bar.

A plain ``QTabBar`` is one flat row, which left the tabs that set a solve up
mixed in with the tabs that report its results. The strip lays the same pages
out as a pipeline, left to right::

    [Heatmap]  -- SETUP --------------  -- RESULTS - OUT OF DATE --  [Messages][Settings][Help]
               [Topology][Properties]..  [Nodes][Fixes]..

* a captioned bracket over each group says which side of the solve it is on;
* the results caption also says whether those results are current, out of
  date, or not there yet, and takes a colour to match;
* a results tab is dimmed while it has nothing to show and italic while its
  numbers pre-date a setup change; one whose analysis runs on demand carries
  a pending icon (a ring with three dots) until it has run.

The pages still live in a ``QTabWidget`` (:class:`GroupedTabWidget`, whose own
bar is hidden), so every ``tabs.setCurrentIndex`` / ``tabs.setTabText`` call
in the viewer keeps working unchanged; the strip only mirrors it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.theme import _T


@dataclass
class TabGroup:
    """A run of adjacent tabs. ``caption`` empty = no bracket over them."""
    indices: list[int]
    caption: str = ""
    key: str = ""
    tone: str = "accent"


@dataclass
class _Marker:
    name: str = field(default="")


STRETCH = _Marker("stretch")  # everything after this is right-aligned

_CAPTION_H = 15
_GROUP_GAP = 12

# caption / status tone -> theme token
_TONES = {
    "accent": "accent",
    "ok": "ok",
    "warn": "warn",
    "err": "err",
    "dim": "fg_dim",
}


def _tone_color(tone: str) -> str:
    t = _T()
    return t[_TONES.get(tone, "fg_dim")]


_PENDING_ICON_PX = 14


def _pending_icon(color: str, size: int = _PENDING_ICON_PX,
                  scale: float = 2.0) -> QIcon:
    """A ring with three dots - "pending: not run yet". Painted rather than
    loaded so it follows the theme and stays sharp; drawn at ``scale`` times
    the logical size for high-DPI screens."""
    px = QPixmap(round(size * scale), round(size * scale))
    px.setDevicePixelRatio(scale)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = QColor(color)
    ring = max(1.2, size * 0.1)
    pen = QPen(c)
    pen.setWidthF(ring)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    inset = ring / 2 + 0.5
    p.drawEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    r = size * 0.085
    cy = size / 2
    for cx in (size * 0.3, size * 0.5, size * 0.7):
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    p.end()
    return QIcon(px)


class GroupedTabWidget(QTabWidget):
    """``QTabWidget`` with its own bar hidden; a :class:`TabStrip` drives it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.strip: TabStrip | None = None
        self.tabBar().hide()

    def setTabText(self, index: int, text: str) -> None:  # noqa: N802 - Qt name
        super().setTabText(index, text)
        if self.strip is not None:
            self.strip.sync_tab_text(index)


class _GroupCaption(QWidget):
    """Small caps caption centred on a bracket line: ``|--- SETUP ---|``."""

    def __init__(self, text: str, tone: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = text
        self._tone = tone
        self.setFixedHeight(_CAPTION_H)
        font = QFont(self.font())
        font.setPointSizeF(7.0)
        font.setBold(True)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        self.setFont(font)

    def set_caption(self, text: str, tone: str) -> None:
        if (text, tone) != (self._text, self._tone):
            self._text, self._tone = text, tone
            self.update()

    def text(self) -> str:
        return self._text

    def tone(self) -> str:
        return self._tone

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt name
        p = QPainter(self)
        color = QColor(_tone_color(self._tone))
        p.setPen(QPen(color, 1))
        w, h = self.width(), self.height()
        y = h // 2
        fm = QFontMetrics(self.font())
        text = fm.elidedText(self._text, Qt.ElideRight, max(0, w - 16))
        tw = fm.horizontalAdvance(text) + 10
        x0 = (w - tw) // 2
        # Bracket: a line each side of the text, with a tick down at each
        # end so it reads as spanning exactly the tabs beneath it.
        p.drawLine(0, y, max(0, x0), y)
        p.drawLine(min(w - 1, x0 + tw), y, w - 1, y)
        p.drawLine(0, y, 0, h - 1)
        p.drawLine(w - 1, y, w - 1, h - 1)
        p.drawText(QRect(x0, 0, tw, h), Qt.AlignCenter, text)
        p.end()


class _WheelScrollArea(QScrollArea):
    """Scroll-bar-less horizontal scroller: the wheel pans it sideways."""

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt name
        bar = self.horizontalScrollBar()
        if bar.maximum() <= 0:
            event.ignore()
            return
        delta = event.angleDelta()
        step = delta.x() if abs(delta.x()) > abs(delta.y()) else delta.y()
        bar.setValue(bar.value() - step)
        event.accept()


class TabStrip(QWidget):
    """The grouped strip itself. Build it once, then :meth:`rebuild` whenever
    the tab widget's pages are (re)created."""

    def __init__(self, tabs: QTabWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._tabs = tabs
        self._buttons: dict[int, QToolButton] = {}
        self._captions: dict[str, _GroupCaption] = {}
        self._group_buttons: dict[str, list[QToolButton]] = {}
        self._button_group: QButtonGroup | None = None
        # tab index -> colour stops for the open tab's top bar (gradient
        # instead of the flat group colour). Kept so a rebuild re-applies it.
        self._bar_gradients: dict[int, list[str]] = {}

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 0)
        outer.setSpacing(0)
        self._left = self._make_arrow("‹", -1)
        self._right = self._make_arrow("›", +1)
        self._scroll = _WheelScrollArea(self)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Queued: the range changes while this widget's layout is mid-pass,
        # and an arrow shown from inside that pass is left with zero width.
        self._scroll.horizontalScrollBar().rangeChanged.connect(
            self._sync_arrows, Qt.QueuedConnection)
        self._scroll.horizontalScrollBar().valueChanged.connect(
            self._sync_arrows, Qt.QueuedConnection)
        outer.addWidget(self._left, 0, Qt.AlignBottom)
        outer.addWidget(self._scroll, 1)
        outer.addWidget(self._right, 0, Qt.AlignBottom)
        self._sync_arrows()

        tabs.currentChanged.connect(self._on_current_changed)

    # --- construction -------------------------------------------------------

    def _make_arrow(self, glyph: str, direction: int) -> QToolButton:
        btn = QToolButton(self)
        btn.setText(glyph)
        btn.setAutoRepeat(True)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setToolTip("Scroll the tabs")
        btn.clicked.connect(lambda: self._scroll_by(direction * 120))
        return btn

    def rebuild(self, items: list) -> None:
        """Lay the strip out from *items*: :class:`TabGroup` entries plus the
        :data:`STRETCH` marker, in display order. Tab
        indices below zero (a tab this window doesn't have) are skipped."""
        t = _T()
        self._buttons = {}
        self._captions = {}
        self._group_buttons = {}
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        inner = QWidget()
        inner.setObjectName("fypaTabStrip")
        row = QHBoxLayout(inner)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        first = True
        right_aligned = False
        for item in items:
            if item is STRETCH:
                right_aligned = True
                row.addStretch(1)
                first = True
                continue
            indices = [i for i in item.indices if i >= 0]
            if not indices:
                continue
            widget = self._build_group(inner, item, indices)
            if not first:
                row.addSpacing(_GROUP_GAP)
            row.addWidget(widget, 0, Qt.AlignBottom)
            first = False
        if not right_aligned:
            # Pack the groups to the left; the spare width goes after them.
            row.addStretch(1)

        qss = (
            "QWidget#fypaTabStrip {{ background-color: {bg}; }}"
            "QToolButton[stripTab=\"true\"] {{"
            "  background-color: {bg_alt}; color: {fg}; padding: 6px 12px;"
            "  border: 1px solid {border}; border-bottom: none;"
            "  border-right: none; }}"
            "QToolButton[stripTab=\"true\"][lastInGroup=\"true\"] {{"
            "  border-right: 1px solid {border}; }}"
            "QToolButton[stripTab=\"true\"]:hover {{"
            "  background-color: {bg_hover}; }}"
            "QToolButton[stripTab=\"true\"][tabState=\"empty\"] {{"
            "  color: {fg_dim}; }}"
            "QToolButton[stripTab=\"true\"][tabState=\"stale\"] {{"
            "  font-style: italic; }}"
            # After the state rules, so the open tab stays readable whatever
            # its state (a stale one keeps its italics).
            "QToolButton[stripTab=\"true\"]:checked {{"
            "  background-color: {bg}; color: {fg_strong};"
            "  border-top: 2px solid {accent}; padding-top: 5px; }}"
        )
        # The open tab's bar takes its group caption's colour.
        for tone, token in _TONES.items():
            qss += (
                "QToolButton[stripTab=\"true\"][barTone=\"" + tone + "\"]"
                ":checked {{ border-top-color: {" + token + "}; }}")
        inner.setStyleSheet(qss.format(**t))
        arrow_qss = (
            f"QToolButton {{ background-color: {t['bg_alt']};"
            f" color: {t['fg']}; border: 1px solid {t['border']};"
            f" padding: 6px 4px; }}"
            f"QToolButton:hover {{ background-color: {t['bg_hover']}; }}"
            f"QToolButton:disabled {{ color: {t['fg_dim']}; }}"
        )
        self._left.setStyleSheet(arrow_qss)
        self._right.setStyleSheet(arrow_qss)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {t['bg']}; }}")

        old = self._scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self._scroll.setWidget(inner)
        self._scroll.setFixedHeight(inner.sizeHint().height())
        for index in list(self._bar_gradients):
            self._apply_bar_gradient(index)
        self._on_current_changed(self._tabs.currentIndex())
        self._sync_arrows()

    def _build_group(self, parent: QWidget, group: TabGroup,
                     indices: list[int]) -> QWidget:
        box = QWidget(parent)
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        if group.caption:
            caption = _GroupCaption(group.caption, group.tone, box)
            if group.key:
                self._captions[group.key] = caption
            col.addWidget(caption)
        else:
            col.addSpacing(_CAPTION_H)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        for n, index in enumerate(indices):
            btn = QToolButton(box)
            btn.setProperty("stripTab", True)
            btn.setProperty("lastInGroup", n == len(indices) - 1)
            btn.setProperty("tabState", "")
            btn.setProperty("pending", False)
            # Icon (the pending mark) after the label, not before it.
            btn.setLayoutDirection(Qt.RightToLeft)
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            btn.setIconSize(QSize(_PENDING_ICON_PX, _PENDING_ICON_PX))
            btn.setProperty("barTone", group.tone if group.caption else "")
            if group.key:
                self._group_buttons.setdefault(group.key, []).append(btn)
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setText(self._tabs.tabText(index))
            btn.clicked.connect(
                lambda _checked=False, i=index: self._tabs.setCurrentIndex(i))
            self._button_group.addButton(btn)
            self._buttons[index] = btn
            row.addWidget(btn)
        col.addLayout(row)
        return box

    # --- state --------------------------------------------------------------

    def sync_tab_text(self, index: int) -> None:
        btn = self._buttons.get(index)
        if btn is not None:
            btn.setText(self._tabs.tabText(index))

    def tab_button(self, index: int) -> QToolButton | None:
        return self._buttons.get(index)

    def tab_order(self) -> list[int]:
        """Tab indices in display order, left to right."""
        return list(self._buttons)

    def set_tab_state(self, index: int, state: str, tooltip: str = "") -> None:
        """``state``: "" (normal), "empty" (nothing to show yet) or "stale"
        (shows numbers from before a setup change)."""
        btn = self._buttons.get(index)
        if btn is None:
            return
        btn.setToolTip(tooltip)
        if btn.property("tabState") == state:
            return
        btn.setProperty("tabState", state)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def set_tab_bar_gradient(self, index: int,
                             colors: list[str] | None) -> None:
        """Paint the open tab's top bar as a left-to-right gradient through
        *colors* (any CSS colour strings) instead of its flat group colour.
        ``None`` or an empty list restores the flat colour."""
        if colors:
            self._bar_gradients[index] = list(colors)
        else:
            self._bar_gradients.pop(index, None)
        self._apply_bar_gradient(index)

    def _apply_bar_gradient(self, index: int) -> None:
        btn = self._buttons.get(index)
        if btn is None:
            return
        colors = self._bar_gradients.get(index)
        if not colors:
            btn.setStyleSheet("")
            return
        if len(colors) == 1:
            colors = colors * 2
        last = len(colors) - 1
        stops = ", ".join(
            f"stop:{i / last:.4f} {c}" for i, c in enumerate(colors))
        # The button's own sheet wins over the strip's, so only the bar
        # colour changes; the strip's padding and other borders still apply.
        btn.setStyleSheet(
            "QToolButton:checked { border-top-color: qlineargradient("
            f"x1:0, y1:0, x2:1, y2:0, {stops}); }}")

    def set_tab_pending(self, index: int, pending: bool) -> None:
        """Mark a tab whose analysis hasn't run yet for the loaded design
        with the pending icon (a ring with three dots) after its label."""
        btn = self._buttons.get(index)
        if btn is None or bool(btn.property("pending")) == pending:
            return
        btn.setProperty("pending", pending)
        btn.setIcon(_pending_icon(_T()["fg_muted"]) if pending else QIcon())

    def tab_pending(self, index: int) -> bool:
        btn = self._buttons.get(index)
        return bool(btn is not None and btn.property("pending"))

    def tab_state(self, index: int) -> str:
        btn = self._buttons.get(index)
        return "" if btn is None else str(btn.property("tabState") or "")

    def set_group_caption(self, key: str, text: str, tone: str) -> None:
        caption = self._captions.get(key)
        if caption is not None:
            caption.set_caption(text, tone)
        for btn in self._group_buttons.get(key, ()):
            if btn.property("barTone") != tone:
                btn.setProperty("barTone", tone)
                btn.style().unpolish(btn)
                btn.style().polish(btn)

    def group_caption(self, key: str) -> tuple[str, str]:
        caption = self._captions.get(key)
        return ("", "") if caption is None else (caption.text(), caption.tone())

    # --- tracking the tab widget -------------------------------------------

    def _on_current_changed(self, index: int) -> None:
        btn = self._buttons.get(index)
        if btn is None:
            return
        btn.setChecked(True)
        self._scroll.ensureWidgetVisible(btn, 24, 0)

    def _scroll_by(self, dx: int) -> None:
        bar = self._scroll.horizontalScrollBar()
        bar.setValue(bar.value() + dx)

    def _sync_arrows(self, *_args) -> None:
        bar = self._scroll.horizontalScrollBar()
        overflow = bar.maximum() > 0
        self._left.setVisible(overflow)
        self._right.setVisible(overflow)
        self._left.setEnabled(bar.value() > 0)
        self._right.setEnabled(bar.value() < bar.maximum())
