"""EyeButton click / modifier / partial fallbacks (shared by layers + rails)."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fypa.altium_viewer import EyeButton  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _click(eye: EyeButton, *, modifiers: Qt.KeyboardModifiers = Qt.NoModifier) -> None:
    """Invoke the click handler with press-time modifiers already captured."""
    eye._press_mods = modifiers
    eye._on_clicked()


def test_plain_click_toggles(qapp):
    eye = EyeButton(visible=True)
    seen: list[bool] = []
    eye.toggled_visible.connect(seen.append)
    _click(eye)
    assert eye.isVisibleState() is False
    assert seen == [False]


def test_ctrl_without_receiver_falls_back_to_toggle(qapp):
    eye = EyeButton(visible=True)
    seen: list[bool] = []
    eye.toggled_visible.connect(seen.append)
    _click(eye, modifiers=Qt.ControlModifier)
    assert eye.isVisibleState() is False
    assert seen == [False]


def test_ctrl_with_receiver_emits_ctrl_only(qapp):
    eye = EyeButton(visible=True)
    toggled: list[bool] = []
    ctrls: list[int] = []
    eye.toggled_visible.connect(toggled.append)
    eye.ctrl_clicked.connect(lambda: ctrls.append(1))
    _click(eye, modifiers=Qt.ControlModifier)
    assert ctrls == [1]
    assert toggled == []
    assert eye.isVisibleState() is True  # unchanged


def test_partial_without_receiver_emits_toggled_visible(qapp):
    eye = EyeButton(visible=False)
    eye.setVisibleState(False, partial=True, emit=False)
    seen: list[bool] = []
    eye.toggled_visible.connect(seen.append)
    _click(eye)
    assert eye.isVisibleState() is True
    assert eye.isPartialState() is False
    assert seen == [True]


def test_partial_with_receiver_emits_partial_activated(qapp):
    eye = EyeButton(visible=False)
    eye.setVisibleState(False, partial=True, emit=False)
    toggled: list[bool] = []
    partials: list[int] = []
    eye.toggled_visible.connect(toggled.append)
    eye.partial_activated.connect(lambda: partials.append(1))
    _click(eye)
    assert partials == [1]
    assert toggled == []
    assert eye.isVisibleState() is True
    assert eye.isPartialState() is False


def test_partial_may_sit_on_off_eye(qapp):
    eye = EyeButton(visible=False)
    eye.setVisibleState(False, partial=True, emit=False)
    assert eye.isVisibleState() is False
    assert eye.isPartialState() is True
