"""Shift-click eye isolation for layer and rail visibility lists."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fypa.altium_viewer import EyeButton, PdnViewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _IsolateViewerStub:
    _apply_eye_isolate_or_invert = PdnViewer._apply_eye_isolate_or_invert
    _iter_rail_visibility_entries = PdnViewer._iter_rail_visibility_entries
    _is_rail_group_isolated = PdnViewer._is_rail_group_isolated
    _apply_rail_group_isolate_or_invert = PdnViewer._apply_rail_group_isolate_or_invert
    _apply_rail_entry_isolate_or_invert = PdnViewer._apply_rail_entry_isolate_or_invert
    _sync_rail_eye_from_subnets = PdnViewer._sync_rail_eye_from_subnets
    _sync_all_rails_eye = PdnViewer._sync_all_rails_eye
    _sync_rail_only_visibility = PdnViewer._sync_rail_only_visibility

    def __init__(self) -> None:
        self._layer_eye_buttons: list[tuple[str, EyeButton]] = []
        self._rail_eye_buttons: list[tuple[str, EyeButton]] = []
        self._subnet_eye_buttons: dict[str, dict[str, EyeButton]] = {}
        self._rail_to_members: dict[str, list[str]] = {}
        self._all_rails_eye = EyeButton(visible=False)
        self.render_calls = 0

    def _render_with_busy_popup(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.render_calls += 1


def _shift_click(eye: EyeButton) -> None:
    pos = eye.rect().center()
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        pos,
        eye.mapToGlobal(pos),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.ShiftModifier,
    )
    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        pos,
        eye.mapToGlobal(pos),
        Qt.LeftButton,
        Qt.NoButton,
        Qt.ShiftModifier,
    )
    eye.mousePressEvent(press)
    eye.mouseReleaseEvent(release)


def test_eye_button_shift_click_emits_without_toggle(qapp) -> None:
    eye = EyeButton(visible=True)
    shifts: list[object] = []
    eye.shift_clicked.connect(lambda: shifts.append(True))
    toggles: list[bool] = []
    eye.toggled_visible.connect(toggles.append)
    _shift_click(eye)
    assert shifts == [True]
    assert toggles == []
    assert eye.isVisibleState() is True


def test_apply_eye_isolate_or_invert_solo_then_invert(qapp) -> None:
    viewer = _IsolateViewerStub()
    a = EyeButton(visible=True)
    b = EyeButton(visible=True)
    c = EyeButton(visible=False)

    viewer._layer_eye_buttons = [("L1", a), ("L2", b), ("L3", c)]

    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L2")
    assert a.isVisibleState() is False
    assert b.isVisibleState() is True
    assert c.isVisibleState() is False

    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L2")
    assert a.isVisibleState() is True
    assert b.isVisibleState() is False
    assert c.isVisibleState() is True


def test_rail_subnet_isolate_and_invert(qapp) -> None:
    viewer = _IsolateViewerStub()
    rail_a = EyeButton(visible=False)
    rail_b = EyeButton(visible=False)
    net_a1 = EyeButton(visible=True)
    net_a2 = EyeButton(visible=True)
    net_b1 = EyeButton(visible=False)

    viewer._rail_eye_buttons = [("VCC", rail_a), ("GND", rail_b)]
    viewer._subnet_eye_buttons = {
        "VCC": {"VCC_3V3": net_a1, "VCC_5V": net_a2},
        "GND": {"GND": net_b1},
    }
    viewer._rail_to_members = {
        "VCC": ["VCC_3V3", "VCC_5V"],
        "GND": ["GND"],
    }

    viewer._apply_rail_entry_isolate_or_invert("VCC", "VCC_3V3")
    assert net_a1.isVisibleState() is True
    assert net_a2.isVisibleState() is False
    assert net_b1.isVisibleState() is False

    viewer._apply_rail_entry_isolate_or_invert("VCC", "VCC_3V3")
    assert net_a1.isVisibleState() is False
    assert net_a2.isVisibleState() is True
    assert net_b1.isVisibleState() is True


def test_rail_group_isolate_and_invert(qapp) -> None:
    viewer = _IsolateViewerStub()
    rail_a = EyeButton(visible=False)
    rail_b = EyeButton(visible=False)
    net_a1 = EyeButton(visible=True)
    net_a2 = EyeButton(visible=False)
    net_b1 = EyeButton(visible=True)

    viewer._rail_eye_buttons = [("VCC", rail_a), ("GND", rail_b)]
    viewer._subnet_eye_buttons = {
        "VCC": {"VCC_3V3": net_a1, "VCC_5V": net_a2},
        "GND": {"GND": net_b1},
    }

    viewer._apply_rail_group_isolate_or_invert("VCC")
    assert net_a1.isVisibleState() is True
    assert net_a2.isVisibleState() is True
    assert net_b1.isVisibleState() is False

    viewer._apply_rail_group_isolate_or_invert("VCC")
    assert net_a1.isVisibleState() is False
    assert net_a2.isVisibleState() is False
    assert net_b1.isVisibleState() is True
