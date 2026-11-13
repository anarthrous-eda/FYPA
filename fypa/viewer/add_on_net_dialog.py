"""Topology click-to-add: put a SOURCE / SINK / REGULATOR on a net.

Opened from an undriven or no-load tag. Lists every part with a pin on the
net — the auto-setup classifier's pick first, prefilled — and asks for the
value (and, for a regulator, its input net and type). Returns a
:class:`~fypa.autosetup.classify.Suggestion` the caller applies like any
other.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from fypa.autosetup.classify import Suggestion
from fypa.viewer.autosetup_dialog import format_value, parse_value
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _esc

_TITLES = {
    "SOURCE": "Add a SOURCE on {net}",
    "SINK": "Add a SINK on {net}",
    "REGULATOR": "Add a REGULATOR whose output is {net}",
}
_VALUE_LABEL = {"SOURCE": "Voltage", "SINK": "Current", "REGULATOR": "Output voltage"}


class AddOnNetDialog(QDialog):
    def __init__(self, parent, *, net: str, role: str, candidates: list[Suggestion],
                 part_nets: dict[str, list[str]]) -> None:
        super().__init__(parent)
        self.setWindowTitle(_TITLES[role].format(net=net))
        self.resize(560, 460)
        self._role = role
        self._cands = candidates
        self._part_nets = part_nets
        t = _T()

        lay = QVBoxLayout(self)
        head = QLabel(
            f"<b>{_esc(_TITLES[role].format(net=net))}</b><br>"
            f"<span style='color:{t['fg_muted']};'>Pick the part. Suggested "
            "parts are listed first; the rest are every part with a pin on "
            f"{_esc(net)}.</span>")
        head.setWordWrap(True)
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)

        self._list = QListWidget()
        for s in candidates:
            hint = f"{s.part_number}  ·  " if s.part_number else ""
            tag = "  ★ suggested" if s.confidence >= 0.5 else ""
            item = QListWidgetItem(f"{s.designator}   {hint}{s.reasons[0] if s.reasons else ''}{tag}")
            item.setToolTip("\n".join(s.reasons))
            self._list.addItem(item)
        self._list.currentRowChanged.connect(self._on_pick)
        lay.addWidget(self._list, 1)

        form = QFormLayout()
        self._value = QLineEdit()
        self._value.setPlaceholderText("e.g. 3.3 V, 250 mA, 0.5")
        form.addRow(_VALUE_LABEL[role], self._value)
        self._in_net = QComboBox()
        self._type = QComboBox()
        self._type.addItems(["LDO", "SMPS"])
        self._eff = QLineEdit()
        self._eff.setPlaceholderText("90")
        if role == "REGULATOR":
            form.addRow("Input net", self._in_net)
            form.addRow("Type", self._type)
            form.addRow("Efficiency (%)", self._eff)
            self._type.currentTextChanged.connect(
                lambda txt: self._eff.setEnabled(txt == "SMPS"))
        lay.addLayout(form)
        self._error = QLabel("")
        self._error.setStyleSheet(f"color: {t['err']};")
        lay.addWidget(self._error)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Add")
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.choice: Suggestion | None = None
        if candidates:
            self._list.setCurrentRow(0)

    def _on_pick(self, row: int) -> None:
        if not (0 <= row < len(self._cands)):
            return
        s = self._cands[row]
        self._value.setText("" if s.value is None else format_value(s.value, s.value_field))
        if self._role == "REGULATOR":
            nets = self._part_nets.get(s.designator, [])
            self._in_net.clear()
            self._in_net.addItems([n for n in nets if n != s.p_net])
            if s.in_p_net:
                self._in_net.setCurrentText(s.in_p_net)
            self._type.setCurrentText(s.regulator_type or "LDO")
            self._eff.setText(f"{s.efficiency * 100:g}" if s.efficiency else "")
            self._eff.setEnabled(self._type.currentText() == "SMPS")

    def _accept(self) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._cands)):
            self._error.setText("Pick a part.")
            return
        s = self._cands[row]
        v = parse_value(self._value.text())
        if v is None or v <= 0:
            self._error.setText(f"Enter a positive {s.value_field}.")
            return
        setattr(s, s.value_field, v)
        s.estimated.discard(s.value_field)
        if self._role == "REGULATOR":
            s.in_p_net = self._in_net.currentText() or None
            if not s.in_p_net:
                self._error.setText("Pick the regulator's input net.")
                return
            s.regulator_type = self._type.currentText()
            if s.regulator_type == "SMPS":
                try:
                    eff = float(self._eff.text()) / 100.0 if self._eff.text().strip() else 0.9
                except ValueError:
                    self._error.setText("Efficiency must be a percentage.")
                    return
                if not (0 < eff <= 1):
                    self._error.setText("Efficiency must be between 0 and 100 %.")
                    return
                s.efficiency = eff
        if s.missing():
            self._error.setText("Missing: " + ", ".join(s.missing()))
            return
        self.choice = s
        self.accept()
