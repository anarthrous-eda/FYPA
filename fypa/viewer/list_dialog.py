"""A notice with a list that can be long: the list scrolls inside a dialog of sensible
size instead of growing the dialog past the screen (a board can have dozens of
skipped directives or unannotated bridges)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QStyle, QVBoxLayout,
)

MAX_SCREEN_FRACTION = 0.6
WIDTH_PX = 760


def show_list_notice(parent, title: str, intro: str, items: list[str], outro: str = "",
                     warning: bool = False, on_dont_show=None) -> None:
    """``on_dont_show`` adds a "Don't show this again" box; it is called with
    no arguments when the box is ticked and the dialog is closed."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    outer = QVBoxLayout(dlg)

    head = QHBoxLayout()
    icon = QLabel()
    pix = QStyle.SP_MessageBoxWarning if warning else QStyle.SP_MessageBoxInformation
    icon.setPixmap(dlg.style().standardIcon(pix).pixmap(40, 40))
    head.addWidget(icon, 0)
    text = QLabel(intro)
    text.setWordWrap(True)
    head.addWidget(text, 1)
    outer.addLayout(head)

    lst = QListWidget()
    lst.setWordWrap(True)
    lst.setAlternatingRowColors(True)
    for item in items:
        lst.addItem(QListWidgetItem(item))
    outer.addWidget(lst, 1)

    if outro:
        tail = QLabel(outro)
        tail.setWordWrap(True)
        outer.addWidget(tail)
    dont_show = None
    if on_dont_show is not None:
        dont_show = QCheckBox("Don't show this again (Settings > General options)")
        outer.addWidget(dont_show)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok)
    buttons.accepted.connect(dlg.accept)
    outer.addWidget(buttons)

    screen = (parent.screen() if parent is not None else None) or QApplication.primaryScreen()
    max_h = int(screen.availableGeometry().height() * MAX_SCREEN_FRACTION) if screen else 600
    dlg.resize(WIDTH_PX, 200)
    dlg.layout().activate()
    # The list is as tall as its rows (a single item does not leave an empty box),
    # and the whole dialog stops at MAX_SCREEN_FRACTION of the screen: past that
    # the list scrolls.
    rows = sum(lst.sizeHintForRow(i) + lst.spacing() for i in range(lst.count()))
    rows += 2 * lst.frameWidth() + 4
    others = dlg.sizeHint().height() - lst.sizeHint().height()
    list_h = max(40, min(rows, max_h - others))
    lst.setFixedHeight(list_h)
    dlg.resize(WIDTH_PX, others + list_h)
    dlg.exec()
    if dont_show is not None and dont_show.isChecked():
        on_dont_show()


__all__ = ["show_list_notice"]
