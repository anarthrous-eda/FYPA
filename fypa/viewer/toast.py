"""Toast notifications for the main window, in place of a status bar.

A ``QStatusBar`` keeps its strip along the bottom of the window once it has
been shown, message or not, taking that height from the board for good. The
window's ``statusBar()`` returns a :class:`ToastBar` instead: the same
``showMessage`` / ``clearMessage`` calls float a small card in the bottom-right
corner of the board view, which goes away after its timeout or when its × is
clicked, and takes no room in the layout.

Cards stack upward from that corner: the status message keeps the bottom slot
(a new one replaces it, as on a status bar) and :meth:`ToastBar.notify` adds
further cards above it.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget

from fypa.viewer.theme import _T

_TEXT_WIDTH_PX = 300   # widest a card's text gets before it wraps
_EDGE_GAP_PX = 14      # gap to the right and bottom edges of the anchor
_STACK_GAP_PX = 8      # gap between stacked cards
_CLOSE_PX = 22
_STRIPE_PX = 4         # the stripe down a card's left edge


class _Toast(QFrame):
    """One card: wrapped text and a × button."""

    def __init__(self, window, on_close) -> None:
        super().__init__(window)
        self.setObjectName("fypaToast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 6, 8)
        row.setSpacing(8)
        self.label = QLabel(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        close = QToolButton(self)
        close.setText("✕")
        close.setToolTip("Dismiss")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.setFixedSize(_CLOSE_PX, _CLOSE_PX)
        close.clicked.connect(lambda: on_close(self))
        row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: on_close(self))
        self.hide()

    def set_message(self, text: str, timeout_ms: int) -> None:
        t = _T()
        self.setStyleSheet(
            f"QFrame#fypaToast {{ background: {t['toast_bg']}; "
            f"border: 1px solid {t['toast_border']}; "
            f"border-left: {_STRIPE_PX}px solid {t['toast_stripe']}; "
            "border-radius: 6px; }"
            f"QFrame#fypaToast QLabel {{ color: {t['toast_fg']}; "
            "background: transparent; border: none; }"
            "QFrame#fypaToast QToolButton { background: transparent; "
            f"border: none; border-radius: 4px; color: {t['toast_close']}; }}"
            "QFrame#fypaToast QToolButton:hover { "
            f"background: {t['toast_hover']}; color: {t['toast_fg']}; }}"
        )
        self.label.setText(text)
        # A short message keeps its own width; a long one wraps into a card.
        text_w = self.label.fontMetrics().horizontalAdvance(text) + 4
        self.label.setFixedWidth(max(40, min(text_w, _TEXT_WIDTH_PX)))
        self.adjustSize()
        if timeout_ms and timeout_ms > 0:
            self.timer.start(int(timeout_ms))
        else:
            self.timer.stop()


class ToastBar(QObject):
    """The slice of ``QStatusBar`` the viewer uses, shown as toasts.

    ``showMessage`` behaves as on a status bar - one message, a new one
    replacing the one showing. ``timeout_ms`` of 0 keeps it until it is
    cleared, replaced or closed by the user."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._status: _Toast | None = None
        self._extra: list[_Toast] = []     # oldest first; stacked above
        window.installEventFilter(self)

    # --- QStatusBar look-alikes ---------------------------------------------

    def showMessage(self, text: str, timeout_ms: int = 0) -> None:  # noqa: N802
        if not text:
            self.clearMessage()
            return
        if self._status is None:
            self._status = _Toast(self._window, self._close)
        self._status.set_message(text, timeout_ms)
        self._status.show()
        self._place()

    def clearMessage(self) -> None:  # noqa: N802
        if self._status is not None and self._status.isVisible():
            self._close(self._status)

    def currentMessage(self) -> str:  # noqa: N802
        if self._status is None or not self._status.isVisible():
            return ""
        return self._status.label.text()

    # --- further cards ------------------------------------------------------

    def notify(self, text: str, timeout_ms: int = 15000) -> None:
        """Show ``text`` on a card of its own, stacked above the others, so
        it is not replaced by the next status message."""
        if not text:
            return
        toast = _Toast(self._window, self._close)
        self._extra.append(toast)
        toast.set_message(text, timeout_ms)
        toast.show()
        self._place()

    # --- internals ----------------------------------------------------------

    def _close(self, toast: _Toast) -> None:
        toast.timer.stop()
        toast.hide()
        if toast in self._extra:
            self._extra.remove(toast)
            toast.deleteLater()
        self._place()

    def _corner(self) -> QPoint:
        """Bottom-right corner to stack from, in window coordinates: the
        board view's when it is showing, so the cards sit clear of the
        readout strip under it and the panels beside it; else the window's
        central area."""
        win = self._window
        for anchor in (getattr(win, "_gl_viewer", None), win.centralWidget()):
            if isinstance(anchor, QWidget) and anchor.isVisible():
                return anchor.mapTo(win, anchor.rect().bottomRight())
        return win.rect().bottomRight()

    def _place(self) -> None:
        corner = self._corner()
        right = corner.x() - _EDGE_GAP_PX
        y = corner.y() - _EDGE_GAP_PX
        stack = ([self._status] if self._status is not None else []) \
            + self._extra
        for toast in stack:
            if not toast.isVisible():
                continue
            y -= toast.height()
            toast.move(max(0, right - toast.width()), max(0, y))
            toast.raise_()
            y -= _STACK_GAP_PX

    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        if obj is self._window and event.type() in (
                QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._place()
        return False


__all__ = ["ToastBar"]
