"""Hover-to-reveal labels for the icon-only editor overlay buttons.

In editor mode Auto setup, Manual setup and Resolve are 34 x 34 icon-only
squares. Resting the mouse on one for a moment widens it and then shows its
label; leaving it hides the label and shrinks it back to the icon.

The button's stylesheet is expected to keep the icon left-aligned at the
same x in both states (``text-align: left`` with a fixed left padding) and
to add the right-hand padding for the label under ``[expanded="true"]``, so
the icon stays put while the button grows.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    QEasingCurve, QEvent, QObject, QPropertyAnimation, QSize, QTimer,
)
from PySide6.QtWidgets import QAbstractButton


def _repolish(btn: QAbstractButton) -> None:
    btn.style().unpolish(btn)
    btn.style().polish(btn)


class HoverExpander(QObject):
    """Widens ``btn`` to show ``label_fn()`` after ``delay_ms`` of hover.

    ``active_fn`` gates it (editor mode only). ``state`` is one of
    ``"idle"``, ``"expanding"``, ``"expanded"``, ``"collapsing"`` - the
    overlay layout leaves the button's size alone unless it is idle.
    """

    def __init__(self, btn: QAbstractButton, label_fn: Callable[[], str],
                 active_fn: Callable[[], bool], *, delay_ms: int = 500,
                 collapsed: int = 34, duration_ms: int = 160) -> None:
        super().__init__(btn)
        self._btn = btn
        self._label_fn = label_fn
        self._active_fn = active_fn
        self._collapsed = collapsed
        self.state = "idle"
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._expand)
        self._anim = QPropertyAnimation(btn, b"size", self)
        self._anim.setDuration(duration_ms)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)
        btn.setProperty("expanded", False)
        btn.installEventFilter(self)

    # -- events ---------------------------------------------------------
    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        if obj is self._btn:
            et = event.type()
            if et == QEvent.ToolTip and self._active_fn():
                return True   # the revealed label stands in for a tooltip
            if et == QEvent.Enter:
                if self._active_fn() and self.state in ("idle", "collapsing"):
                    self._timer.start()
            elif et == QEvent.Leave:
                self._timer.stop()
                self._collapse()
            elif et == QEvent.Hide:
                self.reset()
        return False

    # -- transitions ----------------------------------------------------
    def _set_expanded_look(self, label: str) -> None:
        self._btn.setText(label)
        self._btn.setProperty("expanded", bool(label))
        _repolish(self._btn)

    def _expand(self) -> None:
        btn = self._btn
        if not self._active_fn() or not btn.underMouse():
            return
        label = self._label_fn()
        if not label:
            return
        # Measure the labelled width, then drop back to the icon for the
        # widening; the label goes in once the button is wide enough.
        self._set_expanded_look(label)
        target = QSize(max(btn.sizeHint().width(), self._collapsed),
                       btn.height())
        self._set_expanded_look("")
        btn.raise_()
        self.state = "expanding"
        self._anim.stop()
        self._anim.setStartValue(btn.size())
        self._anim.setEndValue(target)
        self._anim.start()

    def _collapse(self) -> None:
        if self.state in ("idle", "collapsing"):
            return
        self._set_expanded_look("")
        self.state = "collapsing"
        self._anim.stop()
        self._anim.setStartValue(self._btn.size())
        self._anim.setEndValue(QSize(self._collapsed, self._btn.height()))
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if self.state == "expanding":
            self.state = "expanded"
            self._set_expanded_look(self._label_fn())
        elif self.state == "collapsing":
            self.state = "idle"

    def reset(self) -> None:
        """Drop straight back to idle (no animation), e.g. on leaving editor
        mode. Leaves the text and size to the caller's layout."""
        self._timer.stop()
        self._anim.stop()
        if self.state != "idle":
            self.state = "idle"
            self._btn.setText("")
            self._btn.setProperty("expanded", False)
            _repolish(self._btn)

    def current_label(self) -> str:
        """Text the button should carry right now: the label only while
        fully expanded."""
        return self._label_fn() if self.state == "expanded" else ""
