"""Manual setup wizard (Topology tab): add / edit sources, regulators, loads.

Modelled on KiPI's PI setup panel: a *Sources & regulators* tab and a
*Loads* tab, each a list with Add / Duplicate / Remove and an editor for
the selected row. Each pin field is a net → part → pins picker: a
searchable net (rails first), the parts on it (busiest first), and an
optional pin list (blank = every pin of that part on that net). Empty
fields fill themselves from the design (voltage from the net name, ground
from the part, a regulator's input from its one other rail). Schematic
directives appear locked, with **Override** to take them over.

Problems that would stop a solve are listed under the tabs and block
Apply; warnings (a load on a rail nothing supplies yet) don't. Apply hands
the edited list back to the Topology tab, which swaps it into the project.
Series elements stay on the Bridges tab.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fypa.autosetup import manual as M
from fypa.project_file import EditorDirective
from fypa.viewer.autosetup_dialog import format_value, parse_value
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _esc

_TOPOLOGIES = ("-", "BUCK", "BOOST", "BUCKBOOST", "INVERTER")


class _NetCombo(QComboBox):
    """Editable, searchable net picker. ``picked(net)`` fires on a choice."""

    picked = Signal(str)

    def __init__(self, nets: list[str], *, allow_none: str | None = None) -> None:
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        items = ([allow_none] if allow_none else []) + list(nets)
        self.addItems(items)
        comp = QCompleter(items, self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        self.setCompleter(comp)
        self.lineEdit().setPlaceholderText("type to search")
        self._valid = set(items)
        self.activated.connect(lambda _i: self._emit())
        self.lineEdit().editingFinished.connect(self._emit)

    def _emit(self) -> None:
        text = self.currentText().strip()
        match = next((n for n in self._valid if n.lower() == text.lower()), None)
        if match is not None:
            if match != text:
                self.setEditText(match)
            self.picked.emit(match)

    def set_net(self, net: str | None) -> None:
        with QSignalBlocker(self):
            self.setEditText(net or "")

    def set_choices(self, nets: list[str], *, placeholder: str = "type to search") -> None:
        """Replace the offered nets (e.g. only the selected part's nets)."""
        with QSignalBlocker(self):
            cur = self.currentText()
            self.clear()
            self.addItems(nets)
            self.completer().model().setStringList(nets)
            self._valid = set(nets)
            self.setEditText(cur)
        self.lineEdit().setPlaceholderText(placeholder)


class _PinPicker(QWidget):
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
                 net: str | None) -> None:
        """``pins`` is ``[(pad, pin name)]`` of the part on ``net``."""
        self._filling = True
        try:
            n = len(pins)
            self.all.setText(f"All of its pins on {net}" + (f" ({n})" if n else "")
                             if net else "All of its pins on the net")
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


class _Editor(QWidget):
    """Fields for one supply or load. Emits ``changed`` after any edit."""

    changed = Signal()
    override = Signal()

    def __init__(self, mode: str, view) -> None:
        super().__init__()
        self.mode = mode                    # "supply" | "load"
        self.view = view
        self.entry: M.Entry | None = None
        # Designators already a load on a directive's net (set by the page).
        self.taken: Callable[[EditorDirective], set[str]] = lambda _d: set()
        t = _T()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 0, 0, 0)
        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setTextFormat(Qt.RichText)
        lay.addWidget(self._banner)
        self._override_btn = QPushButton("🔓  Override - edit in FYPA")
        self._override_btn.setToolTip(
            "Copy this schematic directive into an editable FYPA one that "
            "replaces it on the next resolve.")
        self._override_btn.clicked.connect(self.override.emit)
        lay.addWidget(self._override_btn)

        form = QFormLayout()
        nets = M.net_choices(view)
        self.type = QComboBox()
        self.type.addItems(M.SUPPLY_TYPES)
        self.type.setToolTip(
            "Source: an ideal supply (a connector, a bench supply).\n"
            "LDO: input current = output current.\n"
            "Switcher: input power = output power / efficiency.")
        self.type.currentTextChanged.connect(self._on_type)
        if mode == "supply":
            form.addRow("Type", self.type)
        self.net = _NetCombo(nets)
        self.net.picked.connect(self._on_net)
        form.addRow("Output net" if mode == "supply" else "Supply net", self.net)
        self.part = QComboBox()
        self.part.activated.connect(self._on_part)
        form.addRow("Part", self.part)
        self.pins = _PinPicker()
        self.pins.setToolTip("Which of the part's pins on the net carry the "
                             "current: all of them, or a hand-picked set.")
        self.pins.changed.connect(self._on_pins)
        form.addRow("Output pins" if mode == "supply" else "Pins", self.pins)
        self.value = QLineEdit()
        self.value.setPlaceholderText("e.g. 3.3 V" if mode == "supply" else "e.g. 250 mA")
        self.value.editingFinished.connect(self._on_value)
        self.value_label = QLabel("Voltage" if mode == "supply" else "Current")
        form.addRow(self.value_label, self.value)
        self.gnd = QComboBox()
        self.gnd.activated.connect(self._on_gnd)
        self.gnd.setToolTip(f"The return path. {M.IDEAL_RETURN}: an ideal 0 V "
                            "return instead of copper (sources and loads only).")
        form.addRow("Ground", self.gnd)
        # The input net is one of the regulator's own nets - filled per part
        # in load(), so it can't be pointed at a net the part isn't on.
        self.in_net = _NetCombo([])
        self.in_net.setToolTip("The regulator's input: one of the selected "
                               "part's nets (its rails listed first).")
        self.in_net.picked.connect(self._on_in_net)
        self.in_net_label = QLabel("Input net")
        form.addRow(self.in_net_label, self.in_net)
        self.in_pins = _PinPicker()
        self.in_pins.changed.connect(self._on_in_pins)
        self.in_pins_label = QLabel("Input pins")
        form.addRow(self.in_pins_label, self.in_pins)
        self.eff = QLineEdit()
        self.eff.setPlaceholderText("90")
        self.eff.editingFinished.connect(self._on_eff)
        self.eff_label = QLabel("Efficiency (%)")
        form.addRow(self.eff_label, self.eff)
        self.topo = QComboBox()
        self.topo.addItems(_TOPOLOGIES)
        self.topo.activated.connect(self._on_topo)
        self.topo_label = QLabel("Topology")
        form.addRow(self.topo_label, self.topo)
        self.min_v = QLineEdit()
        self.min_v.setPlaceholderText("optional - flags the load if its pins sag below")
        self.min_v.editingFinished.connect(self._on_min_v)
        self.min_v_label = QLabel("Min voltage")
        if mode == "load":
            form.addRow(self.min_v_label, self.min_v)
        lay.addLayout(form)
        self._filled = QLabel("")
        self._filled.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        self._filled.setWordWrap(True)
        lay.addWidget(self._filled)
        lay.addStretch(1)
        self.load(None)

    # --- show an entry ---------------------------------------------------------

    def load(self, entry: M.Entry | None, filled: list[str] | None = None) -> None:
        self.entry = entry
        t = _T()
        has = entry is not None
        editable = has and not (entry and entry.locked)
        for w in (self.type, self.net, self.part, self.pins, self.value, self.gnd,
                  self.in_net, self.in_pins, self.eff, self.topo, self.min_v):
            w.setEnabled(editable)
        self._override_btn.setVisible(bool(entry and entry.locked))
        if entry is None:
            self._banner.setText(f"<span style='color:{t['fg_muted']};'>Nothing "
                                 "selected - use Add.</span>")
            self._filled.setText("")
            return
        self._banner.setText(
            f"<span style='color:{t['fg_muted']};'>Defined in the Altium "
            "schematic.</span>" if entry.locked else "")
        d = entry.directive
        with QSignalBlocker(self.type):
            self.type.setCurrentText(M.supply_type(d) if self.mode == "supply" else "Source")
        self.net.set_net(d.p_net)
        self._fill_parts(d)
        part = self.view.part(d.designator) if d.designator else None
        self.pins.set_pins(M.pins_of(part, d.p_net), d.p_pins, d.p_net)
        self.in_net.set_choices(
            M.input_net_choices(self.view, part, d.p_net),
            placeholder="pick the part first" if part is None else "type to search")
        val = d.voltage if self.mode == "supply" else d.current
        self.value.setText(format_value(val, "voltage" if self.mode == "supply"
                                        else "current") if val is not None else "")
        self._fill_ground(d)
        self.in_net.set_net(d.in_p_net)
        self.in_pins.set_pins(M.pins_of(part, d.in_p_net), d.in_p_pins, d.in_p_net)
        self.eff.setText(f"{d.efficiency * 100:g}" if d.efficiency else "")
        with QSignalBlocker(self.topo):
            self.topo.setCurrentText(d.smps_topology or "-")
        self.min_v.setText(format_value(d.min_voltage, "voltage")
                           if d.min_voltage is not None else "")
        self._sync_visibility()
        self._filled.setText(
            f"Filled in from the design: {', '.join(filled)}." if filled else "")

    def _fill_parts(self, d) -> None:
        with QSignalBlocker(self.part):
            self.part.clear()
            if d.p_net:
                prefer = "supply" if self.mode == "supply" else "load"
                for des, label in M.parts_on_net(self.view, d.p_net, prefer=prefer,
                                                 taken=self.taken(d)):
                    self.part.addItem(label, des)
            if d.designator and self.part.findData(d.designator) < 0:
                self.part.addItem(f"{d.designator}  (not on this net)", d.designator)
            idx = self.part.findData(d.designator) if d.designator else -1
            if idx < 0 and self.part.count():
                self.part.insertItem(0, "- pick the part -", None)
                idx = 0
            self.part.setCurrentIndex(idx)

    def _fill_ground(self, d) -> None:
        part = self.view.part(d.designator) if d.designator else None
        with QSignalBlocker(self.gnd):
            self.gnd.clear()
            choices = M.ground_choices(self.view, part)
            if d.role != "REGULATOR":
                choices = choices + [M.IDEAL_RETURN]
            cur = M.ground_of(d)
            if cur and cur not in choices:
                choices.insert(0, cur)
            self.gnd.addItems(choices)
            if cur:
                self.gnd.setCurrentText(cur)
            else:
                self.gnd.setCurrentIndex(-1)

    def _sync_visibility(self) -> None:
        d = self.entry.directive if self.entry else None
        reg = bool(d and d.role == "REGULATOR")
        smps = reg and (d.regulator_type or "") == "SMPS"
        for w in (self.in_net, self.in_net_label, self.in_pins, self.in_pins_label):
            w.setVisible(reg)
        for w in (self.eff, self.eff_label, self.topo, self.topo_label):
            w.setVisible(smps)
        if self.mode == "supply":
            self.value_label.setText("Output voltage" if reg else "Voltage")

    # --- edits ---------------------------------------------------------------------

    def _edited(self, *, reload: bool = False) -> None:
        if self.entry is None:
            return
        d = self.entry.directive
        filled = M.autofill(d, self.view, taken=self.taken(d))
        if reload or filled:
            self.load(self.entry, filled)
        else:
            self._sync_visibility()
        self.changed.emit()

    def _d(self):
        return self.entry.directive if self.entry and not self.entry.locked else None

    def _on_type(self, kind: str) -> None:
        if (d := self._d()) is not None:
            M.set_supply_type(d, kind)
            if kind != "Source" and d.n_net:
                d.in_n_net = d.n_net
            self._edited(reload=True)

    def _on_net(self, net: str) -> None:
        if (d := self._d()) is None or net == d.p_net:
            return
        d.p_net = net
        if d.in_p_net == net:
            d.in_p_net = d.in_p_pins = None
        part = self.view.part(d.designator) if d.designator else None
        if part is None or not part.pins_on(net):
            d.designator = None
            d.p_pins = None
        self._edited(reload=True)

    def _on_part(self, _i: int) -> None:
        if (d := self._d()) is None:
            return
        des = self.part.currentData()
        if des and des != d.designator:
            d.designator = des
            d.p_pins = d.in_p_pins = None
            # The input net must be one of the new part's nets.
            part = self.view.part(des)
            if d.in_p_net and (part is None or not part.pins_on(d.in_p_net)):
                d.in_p_net = None
            self._edited(reload=True)

    def _on_pins(self) -> None:
        if (d := self._d()) is not None:
            d.p_pins = self.pins.value()
            self._edited()

    def _on_value(self) -> None:
        if (d := self._d()) is None:
            return
        v = parse_value(self.value.text())
        if self.mode == "supply":
            d.voltage = v
        else:
            d.current = v
        self._edited()

    def _on_gnd(self, _i: int) -> None:
        if (d := self._d()) is not None:
            M.set_ground(d, self.gnd.currentText() or None)
            self._edited()

    def _on_in_net(self, net: str) -> None:
        if (d := self._d()) is not None:
            d.in_p_net = net
            d.in_p_pins = None
            self._edited(reload=True)

    def _on_in_pins(self) -> None:
        if (d := self._d()) is not None:
            d.in_p_pins = self.in_pins.value()
            self._edited()

    def _on_eff(self) -> None:
        if (d := self._d()) is None:
            return
        txt = self.eff.text().strip().rstrip("%")
        try:
            v = float(txt) if txt else None
        except ValueError:
            v = None
        d.efficiency = (v / 100.0 if v and v > 1 else v) if v is not None else None
        self._edited()

    def _on_topo(self, _i: int) -> None:
        if (d := self._d()) is not None:
            t = self.topo.currentText()
            d.smps_topology = None if t == "-" else t
            self._edited()

    def _on_min_v(self) -> None:
        if (d := self._d()) is not None:
            d.min_voltage = parse_value(self.min_v.text())
            self._edited()


class _RolePage(QWidget):
    """List + Add / Duplicate / Remove + editor, for supplies or loads."""

    changed = Signal()

    def __init__(self, mode: str, view, entries: list[M.Entry]) -> None:
        super().__init__()
        self.mode = mode
        self.view = view
        self.entries = entries
        lay = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row)
        ll.addWidget(self.list, 1)
        btns = QHBoxLayout()
        for text, slot, tip in (
                ("Add", self._add, "New entry"),
                ("Duplicate", self._dup, "Copy the selected entry (e.g. the "
                                         "same part on another rail)"),
                ("Remove", self._remove, "Remove the selected FYPA entry")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
            setattr(self, f"_{text.lower()}_btn", b)
        ll.addLayout(btns)
        if mode == "load":
            split_btn = QPushButton("Split across nets…")
            split_btn.setToolTip(
                "Share one part's total current over all its supply rails, by "
                "how many supply pins each rail has.")
            split_btn.clicked.connect(self._split)
            ll.addWidget(split_btn)
        split.addWidget(left)
        self.editor = _Editor(mode, view)
        if mode == "load":
            self.editor.taken = self._loads_on_net
        self.editor.changed.connect(self._on_edit)
        self.editor.override.connect(self._override)
        split.addWidget(self.editor)
        split.setSizes([330, 520])
        lay.addWidget(split)
        self.refresh()

    def _loads_on_net(self, d: EditorDirective) -> set[str]:
        """Parts of the *other* loads on ``d``'s net (the same part twice on
        one rail is almost always a slip; a channel on another rail is fine)."""
        if not d.p_net:
            return set()
        return {x.directive.designator.upper() for x in self.entries
                if x.directive is not d and x.directive.role == "SINK"
                and x.directive.p_net == d.p_net and x.directive.designator}

    def mine(self) -> list[M.Entry]:
        want_supply = self.mode == "supply"
        return [e for e in self.entries if e.is_supply == want_supply]

    def refresh(self, select_id: str | None = None) -> None:
        rows = self.mine()
        cur = select_id or (self.editor.entry.directive.id if self.editor.entry else None)
        with QSignalBlocker(self.list):
            self.list.clear()
            for e in rows:
                it = QListWidgetItem(e.title())
                it.setData(Qt.UserRole, e.directive.id)
                if e.locked:
                    it.setForeground(Qt.gray)
                    it.setToolTip("Defined in the Altium schematic - select and "
                                  "Override to edit it here")
                self.list.addItem(it)
        idx = next((i for i, e in enumerate(rows) if e.directive.id == cur), 0)
        if rows:
            self.list.setCurrentRow(idx)
            self._on_row(idx)
        else:
            self.editor.load(None)
        self._update_buttons()

    def mark_issues(self, ids_with_error: set[str], ids_with_warning: set[str]) -> None:
        t = _T()
        for i in range(self.list.count()):
            it = self.list.item(i)
            eid = it.data(Qt.UserRole)
            e = next((x for x in self.entries if x.directive.id == eid), None)
            if e is None:
                continue
            mark = " ⚠" if eid in ids_with_error else (" ·" if eid in ids_with_warning else "")
            it.setText(e.title() + mark)
            if eid in ids_with_error:
                it.setForeground(QColor(t["err"]))
            elif not e.locked:
                it.setForeground(self.list.palette().text().color())

    def _on_row(self, row: int) -> None:
        rows = self.mine()
        self.editor.load(rows[row] if 0 <= row < len(rows) else None)
        self._update_buttons()

    def _update_buttons(self) -> None:
        e = self.editor.entry
        self._duplicate_btn.setEnabled(e is not None)
        self._remove_btn.setEnabled(e is not None and not e.locked)

    def _on_edit(self) -> None:
        e = self.editor.entry
        row = self.list.currentRow()
        if e is not None and 0 <= row < self.list.count():
            self.list.item(row).setText(e.title())
        self.changed.emit()

    def _add(self) -> None:
        d = M.new_supply() if self.mode == "supply" else M.new_load()
        self.entries.append(M.Entry(directive=d))
        self.refresh(select_id=d.id)
        self.editor.net.setFocus()
        self.changed.emit()

    def _dup(self) -> None:
        e = self.editor.entry
        if e is None:
            return
        d = M.duplicate(e.directive)
        if e.locked:
            d.overrides_designator = None
        self.entries.append(M.Entry(directive=d))
        self.refresh(select_id=d.id)
        self.changed.emit()

    def _remove(self) -> None:
        e = self.editor.entry
        if e is None or e.locked:
            return
        self.entries.remove(e)
        if e.directive.overrides_designator and e.schematic is not None:
            # Dropping an override brings the schematic row back.
            self.entries.append(M.Entry(directive=M.from_schematic(e.schematic),
                                        locked=True, schematic=e.schematic))
        self.refresh()
        self.changed.emit()

    def _override(self) -> None:
        e = self.editor.entry
        if e is None or not e.locked:
            return
        i = self.entries.index(e)
        self.entries[i] = M.Entry(directive=e.directive, locked=False, schematic=e.schematic)
        self.refresh(select_id=e.directive.id)
        self.changed.emit()

    def _split(self) -> None:
        e = self.editor.entry
        des = e.directive.designator if e else None
        if not des:
            des, ok = QInputDialog.getText(self, "Split across nets", "Part designator:")
            if not ok or not des.strip():
                return
            des = des.strip()
        part = self.view.part(des)
        if part is None:
            return
        cur = sum((x.directive.current or 0) for x in self.entries
                  if x.directive.designator == des and x.directive.role == "SINK")
        text, ok = QInputDialog.getText(
            self, "Split across nets",
            f"Total current drawn by {des} (shared over its supply rails by "
            "supply-pin count):", text=format_value(cur, "current") if cur else "")
        if not ok:
            return
        total = parse_value(text)
        if not total or total <= 0:
            return
        loads = M.split_load(self.view, des, total)
        if not loads:
            return
        self.entries[:] = [x for x in self.entries
                           if x.locked or not (x.directive.designator == des
                                               and x.directive.role == "SINK")]
        self.entries.extend(M.Entry(directive=d) for d in loads)
        self.refresh(select_id=loads[0].id)
        self.changed.emit()


class ManualSetupDialog(QDialog):
    def __init__(self, parent, *, view, editor_directives, metadata,
                 preset=None) -> None:
        """``preset`` is a new directive (see :func:`M.preset`) to add and
        open straight away - the Topology tab's click-to-add on a tag."""
        super().__init__(parent)
        self.setWindowTitle("Manual setup - sources, regulators and loads")
        self.resize(1040, 680)
        self.view = view
        # Working copies: nothing in the project changes until Apply.
        self.entries: list[M.Entry] = [
            M.Entry(directive=M.duplicate_keep_id(d))
            for d in editor_directives
            if d.kind == "component" and d.role in ("SOURCE", "SINK", "REGULATOR")
        ]
        for e in self.entries:
            md = next((m for m in (metadata or {}).get("directives") or []
                       if m.get("designator") == e.directive.overrides_designator
                       and m.get("schdoc") not in ("(editor)", "(auto-bridge)")), None)
            e.schematic = md
        self.entries += M.schematic_entries(metadata, editor_directives)
        if preset is not None:
            self.entries.append(M.Entry(directive=preset))
        self._free = sum(1 for d in editor_directives
                         if d.kind == "free" and d.role != "SERIES")

        t = _T()
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Set up the power tree by hand. For each <b>supply</b> or <b>load</b> "
            "pick its net, then the part on that net, then (optionally) its pins. "
            "Empty fields fill in from the design. Schematic directives are shown "
            "locked; <b>Override</b> takes one over. Series elements (sense "
            "resistors, ferrites, links) are set up on the <b>Bridges</b> tab.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)
        self.tabs = QTabWidget()
        self.supplies = _RolePage("supply", view, self.entries)
        self.loads = _RolePage("load", view, self.entries)
        self.tabs.addTab(self.supplies, "Sources && regulators")
        self.tabs.addTab(self.loads, "Loads")
        for page in (self.supplies, self.loads):
            page.changed.connect(self._revalidate)
        lay.addWidget(self.tabs, 1)
        self.issues = QLabel("")
        self.issues.setWordWrap(True)
        self.issues.setTextFormat(Qt.RichText)
        lay.addWidget(self.issues)
        btns = QHBoxLayout()
        if self._free:
            note = QLabel(f"{self._free} free marker(s) placed on the Heatmap "
                          "aren't listed and are kept as they are.")
            note.setStyleSheet(f"color: {t['fg_muted']};")
            btns.addWidget(note)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(self.apply_btn)
        lay.addLayout(btns)
        if preset is not None:
            page = self.loads if preset.role == "SINK" else self.supplies
            self.tabs.setCurrentWidget(page)
            page.refresh(select_id=preset.id)
            ed = page.editor
            # Focus whatever is still to fill in: the part, else the value.
            (ed.part if not preset.designator else ed.value).setFocus()
        self._revalidate()

    def _revalidate(self) -> None:
        issues = M.validate(self.entries, self.view)
        errs = [i for i in issues if i.level == "error"]
        warns = [i for i in issues if i.level == "warning"]
        t = _T()
        lines = [f"<span style='color:{t['err']};'>✖ {_esc(i.text)}</span>" for i in errs[:8]]
        lines += [f"<span style='color:{t['warn']};'>⚠ {_esc(i.text)}</span>"
                  for i in warns[:6]]
        more = len(errs) + len(warns) - len(lines)
        if more > 0:
            lines.append(f"… and {more} more")
        self.issues.setText("<br>".join(lines))
        err_ids = {i.entry_id for i in errs}
        warn_ids = {i.entry_id for i in warns}
        for page in (self.supplies, self.loads):
            page.mark_issues(err_ids, warn_ids)
        n = sum(1 for e in self.entries if not e.locked)
        self.apply_btn.setEnabled(not errs)
        self.apply_btn.setText(f"Apply ({n} FYPA entr{'y' if n == 1 else 'ies'})")

    def result_directives(self):
        """The edited supplies and loads (FYPA-owned rows only)."""
        return [e.directive for e in self.entries if not e.locked]
