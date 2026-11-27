"""Wheel turns over the floating editor side panel don't zoom the board.

The panel is a child of the GL viewer, so a wheel event the panel leaves
ignored propagates up to the viewer and zooms the view under it. Qt only
propagates real (spontaneous) wheel events, so these tests check what drives
that: whether the panel accepts the event.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _wheel_accepted(**kw) -> bool:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from fypa.viewer.widgets import _ClickAbsorbingPanel

    panel = _ClickAbsorbingPanel(**kw)
    panel.resize(200, 300)
    ev = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0),
                     QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    ev.ignore()
    QApplication.sendEvent(panel, ev)
    return ev.isAccepted()


def test_the_side_panel_keeps_the_wheel(qapp):
    assert _wheel_accepted()


def test_the_button_backdrop_passes_the_wheel_to_the_board(qapp):
    assert not _wheel_accepted(enable_left_edge_resize=False,
                               absorb_wheel=False)
