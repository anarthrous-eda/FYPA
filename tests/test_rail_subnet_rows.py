"""Rail-pane subnet row building — one implementation, two panes."""
from __future__ import annotations

import os
import types

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QListWidget, QListWidgetItem, QWidget,
)

import fypa.altium_viewer as av  # noqa: E402
from fypa.altium_viewer import EyeButton  # noqa: E402
from fypa.rail_groups import RailTreeNode  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


_TREE = RailTreeNode(
    name="VIN",
    children=(
        RailTreeNode(name="A", children=(RailTreeNode(name="A1"),)),
        RailTreeNode(name="B"),
    ),
)
_MEMBERS = ["VIN", "A", "A1", "B"]


def _viewer_stub(*, pending: bool):
    """Minimal stand-in carrying what _insert_subnet_rows touches."""
    v = types.SimpleNamespace()
    v.rail_list = QListWidget()
    v._RAIL_SUBNET_INDENT_PX = 10
    v._subnet_eye_holder = QWidget()
    v._rail_subnet_items = {}
    v._pending_rail_subnet_items = {}
    v._subnet_node_expanded = {}
    v._pending_subnet_node_expanded = {}
    v._subnet_expand_buttons = {}
    v._pending_subnet_expand_buttons = {}
    v._rail_to_members = {"VIN": _MEMBERS}
    v._pending_rails = {"VIN": _MEMBERS}
    v._rail_to_trees = {"VIN": _TREE}
    v._pending_rail_trees = {"VIN": _TREE}
    v._rail_list_items = {}
    v._pending_rail_list_items = {}
    v._rail_expanded = {"VIN": True}
    v._pending_rail_expanded = {"VIN": True}
    v._subnet_eye_buttons = {
        "VIN": {
            net: EyeButton(visible=False, partial_is_badge=True)
            for net in _MEMBERS
        },
    } if not pending else {}
    v._update_rail_list_height = lambda: None
    for name in (
        "_insert_subnet_rows", "_remove_subnet_rows", "_rebuild_subnet_rows",
        "_on_subnet_node_expand_toggled", "_subnet_rows_for_rail",
        "_make_expand_tool_button", "_build_rail_row_widget",
        "_detach_subnet_eyes",
    ):
        setattr(v, name, getattr(av.PdnViewer, name).__get__(v, type(v)))

    parent = QListWidgetItem()
    v.rail_list.addItem(parent)
    items = v._pending_rail_list_items if pending else v._rail_list_items
    items["VIN"] = parent
    return v, parent


def _labels(v) -> list[str]:
    out = []
    for row in range(v.rail_list.count()):
        w = v.rail_list.itemWidget(v.rail_list.item(row))
        if w is None:
            continue
        labels = [
            c.text() for c in w.children()
            if hasattr(c, "text") and c.text()
        ]
        out.extend(labels)
    return out


@pytest.mark.parametrize("pending", [False, True])
def test_insert_shows_expanded_nodes_only(qapp, pending):
    """Both panes walk the same tree: primary open, deeper nodes collapsed."""
    v, parent = _viewer_stub(pending=pending)
    v._insert_subnet_rows("VIN", after_item=parent, pending=pending)
    assert _labels(v) == ["VIN", "A", "B"]

    expanded = (
        v._pending_subnet_node_expanded if pending else v._subnet_node_expanded
    )
    expanded[("VIN", "A")] = True
    v._rebuild_subnet_rows("VIN", pending=pending)
    assert _labels(v) == ["VIN", "A", "A1", "B"]


@pytest.mark.parametrize("pending", [False, True])
def test_remove_clears_rows_and_buttons(qapp, pending):
    v, parent = _viewer_stub(pending=pending)
    v._insert_subnet_rows("VIN", after_item=parent, pending=pending)
    assert v.rail_list.count() > 1
    v._remove_subnet_rows("VIN", pending=pending)
    assert v.rail_list.count() == 1  # only the rail row itself
    buttons = (
        v._pending_subnet_expand_buttons if pending
        else v._subnet_expand_buttons
    )
    assert buttons == {}


def test_pending_rows_are_disabled_and_muted(qapp):
    v, parent = _viewer_stub(pending=True)
    v._insert_subnet_rows("VIN", after_item=parent, pending=True)
    widget = v.rail_list.itemWidget(v.rail_list.item(1))
    eyes = widget.findChildren(EyeButton)
    assert eyes and all(not e.isEnabled() for e in eyes)
    assert "italic" in widget.styleSheet()


def test_solved_rows_reuse_the_rails_own_eyes(qapp):
    """Solved rows borrow the persistent per-net eyes rather than new ones."""
    v, parent = _viewer_stub(pending=False)
    v._insert_subnet_rows("VIN", after_item=parent)
    widget = v.rail_list.itemWidget(v.rail_list.item(1))
    assert widget.findChildren(EyeButton)[0] is v._subnet_eye_buttons["VIN"]["VIN"]
    assert widget.styleSheet() == ""


def test_expand_toggle_defers_the_rebuild(qapp):
    """The slot runs on the button that the rebuild destroys."""
    v, parent = _viewer_stub(pending=False)
    v._insert_subnet_rows("VIN", after_item=parent)
    btn = v._subnet_expand_buttons[("VIN", "A")]
    btn.setChecked(True)  # fires _on_subnet_node_expand_toggled
    assert v._subnet_node_expanded[("VIN", "A")] is True
    assert _labels(v) == ["VIN", "A", "B"]  # not rebuilt yet
    qapp.processEvents()
    assert _labels(v) == ["VIN", "A", "A1", "B"]


def test_rebuild_after_teardown_is_a_no_op(qapp):
    """The deferred rebuild can land after the list is gone."""
    v, parent = _viewer_stub(pending=False)
    v._insert_subnet_rows("VIN", after_item=parent)
    v.rail_list.deleteLater()
    v.rail_list = None
    v._rebuild_subnet_rows("VIN")  # must not raise
