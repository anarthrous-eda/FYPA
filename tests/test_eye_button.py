"""EyeButton click / modifier / partial behaviour (shared by layers + rails)."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fypa.altium_viewer import EyeButton  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _click(eye: EyeButton, *, modifiers: Qt.KeyboardModifiers = Qt.NoModifier) -> None:
    """A real press/release, so the press-time modifier capture is exercised.

    Setting ``_press_mods`` by hand would let a regression in
    ``mousePressEvent`` (a base class swallowing the press, say) pass every
    test here while Ctrl+Click silently degraded to a plain toggle.
    """
    QTest.mouseClick(eye, Qt.LeftButton, modifiers)


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


def test_shift_and_ctrl_coexist(qapp):
    """Both gestures live on one button — neither may swallow the other."""
    eye = EyeButton(visible=True, shift_isolatable=True)
    ctrls: list[int] = []
    shifts: list[int] = []
    eye.ctrl_clicked.connect(lambda: ctrls.append(1))
    eye.shift_clicked.connect(lambda: shifts.append(1))
    _click(eye, modifiers=Qt.ShiftModifier)
    _click(eye, modifiers=Qt.ControlModifier)
    assert (shifts, ctrls) == ([1], [1])
    assert eye.isVisibleState() is True


def test_aggregate_partial_click_shows_all(qapp):
    """A rail / "All layers" eye owns no copper: partial → show every child."""
    eye = EyeButton(visible=True)
    eye.setVisibleState(True, partial=True, emit=False)
    seen: list[bool] = []
    eye.toggled_visible.connect(seen.append)
    _click(eye)
    assert eye.isVisibleState() is True
    assert eye.isPartialState() is False
    assert seen == [True]


def test_badge_partial_click_still_toggles_own_state(qapp):
    """A tree-node eye owns copper: the badge must not hijack the toggle.

    With partial meaning "show the whole subtree", a node with mixed
    descendants could never be hidden — every click fanned the branch on.
    """
    eye = EyeButton(visible=True, partial_is_badge=True)
    eye.setVisibleState(True, partial=True, emit=False)
    seen: list[bool] = []
    eye.toggled_visible.connect(seen.append)
    _click(eye)
    assert eye.isVisibleState() is False
    assert seen == [False]
    _click(eye)
    assert eye.isVisibleState() is True


def test_partial_may_sit_on_off_eye(qapp):
    eye = EyeButton(visible=False, partial_is_badge=True)
    eye.setVisibleState(False, partial=True, emit=False)
    assert eye.isVisibleState() is False
    assert eye.isPartialState() is True


def test_off_partial_is_visually_distinct_from_on_partial(qapp):
    """Hidden-with-visible-descendants must not draw as visible."""
    from fypa.altium_viewer import _eye_pixmap

    on_partial = _eye_pixmap(True, 16, partial=True).toImage()
    off_partial = _eye_pixmap(False, 16, partial=True).toImage()
    off_plain = _eye_pixmap(False, 16, partial=False).toImage()
    assert on_partial != off_partial
    assert off_partial != off_plain


def test_badge_tooltip_says_what_a_click_does(qapp):
    eye = EyeButton(
        visible=True, tip_show="Show X", tip_hide="Hide X",
        partial_is_badge=True,
    )
    eye.setVisibleState(True, partial=True, emit=False)
    assert eye.toolTip().startswith("Hide X")
    eye.setVisibleState(False, partial=True, emit=False)
    assert eye.toolTip().startswith("Show X")


def test_custom_tooltip_survives_state_changes(qapp):
    eye = EyeButton(visible=False)
    eye.setToolTip("Unsolved rail — press Resolve to compute it.")
    eye.setVisibleState(True, emit=False)
    assert eye.toolTip() == "Unsolved rail — press Resolve to compute it."


def test_press_without_click_does_not_arm_next_activation(qapp):
    """A cancelled Ctrl+press must not leak Ctrl into a later click()."""
    eye = EyeButton(visible=True)
    ctrls: list[int] = []
    eye.ctrl_clicked.connect(lambda: ctrls.append(1))
    QTest.mousePress(eye, Qt.LeftButton, Qt.ControlModifier)
    QTest.mouseRelease(
        eye, Qt.LeftButton, Qt.ControlModifier, eye.rect().bottomRight() * 4,
    )
    eye.click()
    assert ctrls == []
    assert eye.isVisibleState() is False
