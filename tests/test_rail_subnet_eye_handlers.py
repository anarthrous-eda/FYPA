"""Subnet eye Ctrl/partial handlers (fan-out without full PdnViewer init)."""
from __future__ import annotations

import os
import types

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import fypa.altium_viewer as av  # noqa: E402
from fypa.altium_viewer import EyeButton  # noqa: E402
from fypa.rail_groups import RailTreeNode  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _eye(on: bool) -> EyeButton:
    eye = EyeButton(visible=on)
    eye.setVisibleState(on, emit=False)
    return eye


def _viewer_stub(tree: RailTreeNode, states: dict[str, bool]):
    """Minimal stand-in with the attrs subnet fan-out handlers need."""
    v = types.SimpleNamespace()
    v._rail_to_trees = {"VIN": tree}
    v._subnet_eye_buttons = {
        "VIN": {name: _eye(on) for name, on in states.items()},
    }
    v._refreshed = []

    def _refresh(rail: str) -> None:
        # Real post-fan-out partial sync; skip render / rail-list sync.
        av.PdnViewer._sync_rail_tree_node_partials(v, rail)
        v._refreshed.append(rail)

    v._refresh_rail_visibility_after_subnet_change = _refresh
    v._subnet_tree_for_rail = (
        lambda rail, _v=v: av.PdnViewer._subnet_tree_for_rail(_v, rail)
    )
    v._fan_out_subnet_subtree = (
        lambda rail, net, on, _v=v: av.PdnViewer._fan_out_subnet_subtree(
            _v, rail, net, on,
        )
    )
    return v


_TREE = RailTreeNode(
    name="VIN",
    children=(
        RailTreeNode(name="A"),
        RailTreeNode(name="B"),
    ),
)


def test_ctrl_click_mixed_turns_subtree_on(qapp):
    v = _viewer_stub(_TREE, {"VIN": True, "A": True, "B": False})
    av.PdnViewer._on_subnet_eye_ctrl_clicked(v, "VIN", "VIN")
    eyes = v._subnet_eye_buttons["VIN"]
    assert all(e.isVisibleState() for e in eyes.values())
    assert all(not e.isPartialState() for e in eyes.values())
    assert v._refreshed == ["VIN"]


def test_ctrl_click_all_on_turns_subtree_off(qapp):
    v = _viewer_stub(_TREE, {"VIN": True, "A": True, "B": True})
    av.PdnViewer._on_subnet_eye_ctrl_clicked(v, "VIN", "VIN")
    eyes = v._subnet_eye_buttons["VIN"]
    assert all(not e.isVisibleState() for e in eyes.values())
    assert all(not e.isPartialState() for e in eyes.values())
    assert v._refreshed == ["VIN"]


def test_partial_activated_fans_out_children(qapp):
    v = _viewer_stub(_TREE, {"VIN": False, "A": True, "B": False})
    eye = v._subnet_eye_buttons["VIN"]["VIN"]
    eye.setVisibleState(False, partial=True, emit=False)
    av.PdnViewer._on_subnet_eye_partial_activated(v, "VIN", "VIN")
    eyes = v._subnet_eye_buttons["VIN"]
    assert all(e.isVisibleState() for e in eyes.values())
    assert all(not e.isPartialState() for e in eyes.values())
    assert v._refreshed == ["VIN"]


def test_partial_activated_leaf_only_turns_self_on(qapp):
    v = _viewer_stub(_TREE, {"VIN": False, "A": False, "B": False})
    av.PdnViewer._on_subnet_eye_partial_activated(v, "VIN", "A")
    eyes = v._subnet_eye_buttons["VIN"]
    assert eyes["A"].isVisibleState() is True
    assert eyes["VIN"].isVisibleState() is False
    assert eyes["B"].isVisibleState() is False
    # Parent off + one child on → intermediate partial after sync.
    assert eyes["VIN"].isPartialState() is True
    assert eyes["A"].isPartialState() is False


def test_sync_parent_off_all_children_on_is_partial(qapp):
    v = _viewer_stub(_TREE, {"VIN": False, "A": True, "B": True})
    av.PdnViewer._sync_rail_tree_node_partials(v, "VIN")
    eyes = v._subnet_eye_buttons["VIN"]
    assert eyes["VIN"].isVisibleState() is False
    assert eyes["VIN"].isPartialState() is True
    assert eyes["A"].isPartialState() is False


def test_sync_parent_on_all_children_off_is_not_partial(qapp):
    v = _viewer_stub(_TREE, {"VIN": True, "A": False, "B": False})
    av.PdnViewer._sync_rail_tree_node_partials(v, "VIN")
    eyes = v._subnet_eye_buttons["VIN"]
    assert eyes["VIN"].isVisibleState() is True
    assert eyes["VIN"].isPartialState() is False
