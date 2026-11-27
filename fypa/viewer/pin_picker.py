"""Checkbox pin list shared by Manual setup and the Board editor form."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class PinPicker(QWidget):
    """"All of its pins on the net", or a hand-picked subset of them.

    Ticking *All* stores ``None`` (every pin of the part on the net - also
    what you get if the part's pinout changes). Unticking it lists that
    part's pins on that net, pad and pin name, all ticked to start with;
    untick the ones the current shouldn't use. ``value()`` is ``None`` for
    all, else the ticked pads (an empty list means nothing is ticked yet,
    which validation reports)."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.all = QCheckBox("All of its pins on the net")
        self.all.toggled.connect(self._on_all)
        lay.addWidget(self.all)
        self.list = QListWidget()
        self.list.setMaximumHeight(120)
        self.list.itemChanged.connect(lambda _it: self._emit())
        lay.addWidget(self.list)
        self._filling = False

    def set_pins(self, pins: list[tuple[str, str]], selected: list[str] | None,
                 net: str | None, *, whose: str = "its") -> None:
        """``pins`` is ``[(pad, pin name)]`` of the part on ``net``;
        ``whose`` reads "their" when the pins come from several parts."""
        self._filling = True
        try:
            n = len(pins)
            self.all.setText(f"All of {whose} pins on {net}" + (f" ({n})" if n else "")
                             if net else f"All of {whose} pins on the net")
            self.all.setEnabled(bool(pins))
            with QSignalBlocker(self.all):
                self.all.setChecked(selected is None)
            self.list.clear()
            chosen = {p.upper() for p in selected} if selected is not None else None
            for pad, name in pins:
                label = pad if not name or name == pad else f"{pad}   {name}"
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, pad)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                on = chosen is None or pad.upper() in chosen
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
                self.list.addItem(it)
            self.list.setVisible(selected is not None and bool(pins))
        finally:
            self._filling = False

    def value(self) -> list[str] | None:
        if self.all.isChecked():
            return None
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def _on_all(self, checked: bool) -> None:
        self.list.setVisible(not checked and self.list.count() > 0)
        self._emit()

    def _emit(self) -> None:
        if not self._filling:
            self.changed.emit()
