"""Shift-click eye isolation for layer and rail visibility lists."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fypa.altium_viewer import EyeButton, PdnViewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _IsolateViewerStub:
    _apply_isolate_or_invert = PdnViewer._apply_isolate_or_invert
    _apply_eye_isolate_or_invert = PdnViewer._apply_eye_isolate_or_invert
    _iter_rail_visibility_entries = PdnViewer._iter_rail_visibility_entries
    _apply_rail_group_isolate_or_invert = PdnViewer._apply_rail_group_isolate_or_invert
    _apply_rail_entry_isolate_or_invert = PdnViewer._apply_rail_entry_isolate_or_invert
    _after_rail_visibility_change = PdnViewer._after_rail_visibility_change
    _on_rail_eye_shift_clicked = PdnViewer._on_rail_eye_shift_clicked
    _on_subnet_eye_shift_clicked = PdnViewer._on_subnet_eye_shift_clicked
    _on_layer_eye_shift_clicked = PdnViewer._on_layer_eye_shift_clicked
    _on_layer_eye2_shift_clicked = PdnViewer._on_layer_eye2_shift_clicked
    _sync_rail_eye_from_subnets = PdnViewer._sync_rail_eye_from_subnets
    _sync_all_rails_eye = PdnViewer._sync_all_rails_eye
    _sync_rail_only_visibility = PdnViewer._sync_rail_only_visibility

    def __init__(self) -> None:
        self._layer_eye_buttons: list[tuple[str, EyeButton]] = []
        self._layer_eye2_buttons: list[tuple[str, EyeButton]] = []
        self._rail_eye_buttons: list[tuple[str, EyeButton]] = []
        self._subnet_eye_buttons: dict[str, dict[str, EyeButton]] = {}
        self._rail_to_members: dict[str, list[str]] = {}
        self._all_rails_eye = EyeButton(visible=False)
        self._isolated_key: object | None = None
        self.render_calls = 0
        self.layer_refreshes = 0
        self.copper_refreshes = 0

    def _render_with_busy_popup(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.render_calls += 1

    # --- layer-slot collaborators -------------------------------------------
    def _sync_all_layers_eye(self) -> None:
        pass

    def _sync_all_layers_eye2(self) -> None:
        pass

    def _on_layer_visibility_changed(self) -> None:
        self.layer_refreshes += 1

    def _visible_rails(self) -> list[str]:
        return []

    def _refresh_after_copper_eye(self, _rails) -> None:
        self.copper_refreshes += 1

    def _run_with_busy_popup(self, fn) -> None:
        fn()


def _shift_click(eye: EyeButton) -> None:
    QTest.mouseClick(eye, Qt.LeftButton, Qt.ShiftModifier)


def _states(*eyes: EyeButton) -> list[bool]:
    return [e.isVisibleState() for e in eyes]


# --- EyeButton -----------------------------------------------------------------

def test_eye_button_shift_click_emits_without_toggle(qapp) -> None:
    eye = EyeButton(visible=True, shift_isolatable=True)
    eye.show()
    shifts: list[object] = []
    eye.shift_clicked.connect(lambda: shifts.append(True))
    toggles: list[bool] = []
    eye.toggled_visible.connect(toggles.append)
    _shift_click(eye)
    assert shifts == [True]
    assert toggles == []
    assert eye.isVisibleState() is True


def test_eye_button_without_isolate_still_toggles_on_shift(qapp) -> None:
    """An eye with no shift_clicked receiver must not swallow the click."""
    eye = EyeButton(visible=True)
    eye.show()
    toggles: list[bool] = []
    eye.toggled_visible.connect(toggles.append)
    _shift_click(eye)
    assert toggles == [False]
    assert eye.isVisibleState() is False
    assert "Shift+Click" not in eye.toolTip()


def test_eye_button_advertises_shift_only_when_isolatable(qapp) -> None:
    assert "Shift+Click" in EyeButton(visible=True, shift_isolatable=True).toolTip()
    assert "Shift+Click" not in EyeButton(visible=True).toolTip()


def test_eye_button_keeps_caller_tooltip_across_state_change(qapp) -> None:
    """An unsolved-rail eye explains why it is disabled; a later state change
    must not replace that with generic show/hide text."""
    eye = EyeButton(visible=False)
    eye.setToolTip("Unsolved rail — press Resolve to compute it.")
    eye.setVisibleState(True)
    assert eye.toolTip() == "Unsolved rail — press Resolve to compute it."


def test_eye_button_does_not_latch_shift_from_cancelled_press(qapp) -> None:
    """Shift+press then drag off emits no click; the modifier must not leak
    into the next click, which may not come through mousePressEvent at all."""
    eye = EyeButton(visible=True, shift_isolatable=True)
    eye.show()
    shifts: list[object] = []
    eye.shift_clicked.connect(lambda: shifts.append(True))
    toggles: list[bool] = []
    eye.toggled_visible.connect(toggles.append)

    QTest.mousePress(eye, Qt.LeftButton, Qt.ShiftModifier)
    # Release well outside the button — Qt suppresses ``clicked``.
    QTest.mouseRelease(
        eye, Qt.LeftButton, Qt.ShiftModifier, eye.rect().bottomRight() * 8,
    )
    assert shifts == []
    assert toggles == []

    # A programmatic click never touches mousePressEvent.
    eye.click()
    assert shifts == []
    assert toggles == [False]


# --- isolate / invert ----------------------------------------------------------

def test_layer_isolate_then_invert(qapp) -> None:
    viewer = _IsolateViewerStub()
    a, b, c = EyeButton(visible=True), EyeButton(visible=True), EyeButton(visible=False)
    viewer._layer_eye_buttons = [("L1", a), ("L2", b), ("L3", c)]

    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L2")
    assert _states(a, b, c) == [False, True, False]

    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L2")
    assert _states(a, b, c) == [True, False, True]


def test_first_shift_click_on_sole_visible_layer_isolates(qapp) -> None:
    """The user narrows to one layer by ordinary clicks, then shift-clicks it.
    That is a request to isolate, not to invert — inverting would hide it."""
    viewer = _IsolateViewerStub()
    a, b = EyeButton(visible=True), EyeButton(visible=False)
    viewer._layer_eye_buttons = [("L1", a), ("L2", b)]

    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L1")
    assert _states(a, b) == [True, False]

    # Only the repeat inverts.
    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L1")
    assert _states(a, b) == [False, True]


def test_manual_toggle_between_shift_clicks_re_isolates(qapp) -> None:
    """A plain click after isolating means the user is narrowing again, so the
    next shift-click must isolate rather than invert."""
    viewer = _IsolateViewerStub()
    a, b, c = EyeButton(visible=True), EyeButton(visible=True), EyeButton(visible=True)
    viewer._layer_eye_buttons = [("L1", a), ("L2", b), ("L3", c)]

    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L1")
    assert _states(a, b, c) == [True, False, False]
    b.setVisibleState(True, emit=False)          # user re-shows L2 by hand

    viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L1")
    assert _states(a, b, c) == [True, False, False]


def test_invert_that_would_hide_everything_is_refused(qapp) -> None:
    """A single-entry list has no complement to swap to — inverting it would
    blank the viewport, including the all-copper eye a no-PDN board forces on."""
    viewer = _IsolateViewerStub()
    only = EyeButton(visible=True)
    viewer._layer_eye_buttons = [("L1", only)]

    assert viewer._apply_eye_isolate_or_invert(viewer._layer_eye_buttons, "L1")
    assert only.isVisibleState() is True
    assert viewer._apply_eye_isolate_or_invert(
        viewer._layer_eye_buttons, "L1",
    ) is False
    assert only.isVisibleState() is True


def test_isolate_on_dead_target_eye_changes_nothing(qapp) -> None:
    """A destroyed target must not switch every other eye off."""
    viewer = _IsolateViewerStub()
    a, b = EyeButton(visible=True), EyeButton(visible=True)
    dead = EyeButton(visible=False)
    viewer._layer_eye_buttons = [("L1", a), ("L2", b), ("L3", dead)]
    dead.deleteLater()
    dead.setParent(None)
    import shiboken6

    shiboken6.delete(dead)

    assert viewer._apply_eye_isolate_or_invert(
        viewer._layer_eye_buttons, "L3",
    ) is False
    assert _states(a, b) == [True, True]


# --- rails and subnets ---------------------------------------------------------

def _rail_stub() -> tuple[_IsolateViewerStub, dict[str, EyeButton]]:
    viewer = _IsolateViewerStub()
    eyes = {
        "rail_vcc": EyeButton(visible=False),
        "rail_gnd": EyeButton(visible=False),
        "vcc_3v3": EyeButton(visible=True),
        "vcc_5v": EyeButton(visible=True),
        "gnd": EyeButton(visible=False),
    }
    viewer._rail_eye_buttons = [
        ("VCC", eyes["rail_vcc"]), ("GND", eyes["rail_gnd"]),
    ]
    viewer._subnet_eye_buttons = {
        "VCC": {"VCC_3V3": eyes["vcc_3v3"], "VCC_5V": eyes["vcc_5v"]},
        "GND": {"GND": eyes["gnd"]},
    }
    viewer._rail_to_members = {"VCC": ["VCC_3V3", "VCC_5V"], "GND": ["GND"]}
    return viewer, eyes


def test_rail_subnet_isolate_and_invert(qapp) -> None:
    viewer, e = _rail_stub()

    viewer._apply_rail_entry_isolate_or_invert("VCC", "VCC_3V3")
    assert _states(e["vcc_3v3"], e["vcc_5v"], e["gnd"]) == [True, False, False]

    viewer._apply_rail_entry_isolate_or_invert("VCC", "VCC_3V3")
    assert _states(e["vcc_3v3"], e["vcc_5v"], e["gnd"]) == [False, True, True]


def test_rail_group_isolate_and_invert(qapp) -> None:
    viewer, e = _rail_stub()
    e["vcc_5v"].setVisibleState(False, emit=False)
    e["gnd"].setVisibleState(True, emit=False)

    viewer._apply_rail_group_isolate_or_invert("VCC")
    assert _states(e["vcc_3v3"], e["vcc_5v"], e["gnd"]) == [True, True, False]

    viewer._apply_rail_group_isolate_or_invert("VCC")
    assert _states(e["vcc_3v3"], e["vcc_5v"], e["gnd"]) == [False, False, True]


def test_partially_visible_group_isolates_rather_than_inverts(qapp) -> None:
    """VCC_3V3 visible, VCC_5V and GND hidden: the VCC group is not isolated —
    shift-clicking the parent must complete the isolation, not invert it."""
    viewer, e = _rail_stub()
    e["vcc_5v"].setVisibleState(False, emit=False)

    viewer._on_rail_eye_shift_clicked("VCC")
    assert _states(e["vcc_3v3"], e["vcc_5v"], e["gnd"]) == [True, True, False]


def test_rail_shift_click_dispatches_on_subnet_map_not_rail_members(qapp) -> None:
    """_rail_to_members is maintained separately and can disagree; dispatching
    on it sends a bridged rail down the single-entry path, which matches no
    entry and switches every eye off."""
    viewer, e = _rail_stub()
    viewer._rail_to_members["VCC"] = ["VCC_3V3"]      # stale/disagreeing

    viewer._on_rail_eye_shift_clicked("VCC")
    assert _states(e["vcc_3v3"], e["vcc_5v"], e["gnd"]) == [True, True, False]
    assert viewer.render_calls == 1


def test_rail_with_dead_subnet_eyes_falls_back_to_parent_eye(qapp) -> None:
    """Subnet eyes destroyed with their row must not drop the rail out of the
    entry list, or another rail's isolate switches it off with no way back."""
    import shiboken6

    viewer, e = _rail_stub()
    e["rail_vcc"].setVisibleState(True, emit=False)
    for name in ("vcc_3v3", "vcc_5v"):
        e[name].setParent(None)
        shiboken6.delete(e[name])

    entries = viewer._iter_rail_visibility_entries()
    assert [(r, n) for r, n, _eye in entries] == [("VCC", None), ("GND", "GND")]

    viewer._on_rail_eye_shift_clicked("GND")
    assert e["gnd"].isVisibleState() is True
    assert e["rail_vcc"].isVisibleState() is False


def test_subnet_shift_click_slot_renders_once(qapp) -> None:
    viewer, e = _rail_stub()
    viewer._on_subnet_eye_shift_clicked("VCC", "VCC_5V")
    assert _states(e["vcc_3v3"], e["vcc_5v"], e["gnd"]) == [False, True, False]
    assert viewer.render_calls == 1


def test_layer_shift_click_slots_use_their_own_list(qapp) -> None:
    """The eye2 slot must isolate within _layer_eye2_buttons, and each slot
    must drive its own refresh path."""
    viewer = _IsolateViewerStub()
    a1, b1 = EyeButton(visible=True), EyeButton(visible=True)
    a2, b2 = EyeButton(visible=True), EyeButton(visible=True)
    viewer._layer_eye_buttons = [("L1", a1), ("L2", b1)]
    viewer._layer_eye2_buttons = [("L1", a2), ("L2", b2)]

    viewer._on_layer_eye_shift_clicked("L1")
    assert _states(a1, b1) == [True, False]
    assert _states(a2, b2) == [True, True]     # untouched
    assert viewer.layer_refreshes == 1

    viewer._on_layer_eye2_shift_clicked("L2")
    assert _states(a2, b2) == [False, True]
    assert _states(a1, b1) == [True, False]    # untouched
    assert viewer.copper_refreshes == 1


def test_layer_and_layer2_isolation_keys_do_not_collide(qapp) -> None:
    """Isolating L1 in the heatmap list then L1 in the copper list are two
    different isolations — the second must not read as a repeat and invert."""
    viewer = _IsolateViewerStub()
    a1, b1 = EyeButton(visible=True), EyeButton(visible=True)
    a2, b2 = EyeButton(visible=True), EyeButton(visible=True)
    viewer._layer_eye_buttons = [("L1", a1), ("L2", b1)]
    viewer._layer_eye2_buttons = [("L1", a2), ("L2", b2)]

    viewer._on_layer_eye_shift_clicked("L1")
    viewer._on_layer_eye2_shift_clicked("L1")
    assert _states(a2, b2) == [True, False]
