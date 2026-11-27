"""PySide6 pages for the Gerber import flow.

Two pages, shown as steps of the import window
(:class:`fypa.viewer.gerber_import_wizard.GerberImportWizard`):

1. :class:`GerberFilesPage` - the picked files, and the role of each one
   (Top / Inner-N / Bottom / silk / outline / drill / ignore), auto-detected
   from the file name and editable.

2. :class:`GerberStackupPage` - per-copper-layer thickness and the
   dielectric below each layer; pre-filled with sensible defaults (1 oz
   copper, 1.6 mm total board split evenly across dielectrics).

The pages are plain widgets with no OK / Cancel of their own and no imports
from the viewer, so they can be exercised in isolation from a script or a
test. :func:`validate_mapping` holds the layer-mapping rules with no UI at
all, so the window can show problems inline as the user edits.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa.gerber.extract import (
    LAYER_ID_BOTTOM,
    LAYER_ID_DRILL,
    LAYER_ID_IGNORE,
    LAYER_ID_OUTLINE,
    LAYER_ID_SILK_BOT,
    LAYER_ID_SILK_TOP,
    LAYER_ID_TOP,
    MAX_INNER_LAYERS,
    GerberStackupLayer,
    classify_file,
)

log = logging.getLogger(__name__)


# Pretty labels for each special layer-id sentinel.
_LAYER_ROLE_LABELS: list[tuple[int, str]] = (
    [(LAYER_ID_IGNORE, "Ignore")]
    + [(LAYER_ID_TOP, "Top")]
    + [(1 + n, f"Inner {n}") for n in range(1, MAX_INNER_LAYERS + 1)]
    + [(LAYER_ID_BOTTOM, "Bottom")]
    + [
        (LAYER_ID_SILK_TOP, "Top Silk"),
        (LAYER_ID_SILK_BOT, "Bottom Silk"),
        (LAYER_ID_OUTLINE, "Outline"),
        (LAYER_ID_DRILL, "Drill (Excellon or Gerber X2)"),
    ]
)


# File-picker filter shared by the import window's Add Files button.
GERBER_FILE_FILTER: str = (
    "Gerber / Excellon ("
    "*.gbr *.GBR *.gtl *.GTL *.gbl *.GBL *.g* *.G* "
    "*.cmp *.CMP *.sol *.SOL *.gko *.GKO *.gm1 *.GM1 "
    "*.gto *.GTO *.gbo *.GBO "
    "*.drl *.DRL *.xln *.XLN *.txt *.TXT *.tap *.TAP *.nc *.NC);;"
    "All files (*)"
)


def _path_key(path: Path) -> str:
    """Identity for de-duplicating picked files (case-insensitive on
    Windows, tolerant of paths that can't be resolved)."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


def _role_label(layer_id: int) -> str:
    for lid, label in _LAYER_ROLE_LABELS:
        if lid == layer_id:
            return label
    return f"Layer {layer_id}"


@dataclass(frozen=True)
class GerberImportResult:
    """Everything the importer needs once the user has finished the steps.

    Mirrors the persistent fields stored in the ``.fypa`` ProjectFile
    (see :class:`fypa.project_file.ProjectFile.layer_assignments` and
    friends) so the result can be saved directly.
    """
    copper_files: dict[int, Path]    # layer_id -> path
    drill_files: list[Path]
    outline_file: Path | None
    silk_top_file: Path | None
    silk_bot_file: Path | None
    ignored_files: list[Path]
    stackup: list[GerberStackupLayer]


# --- Layer-mapping rules ------------------------------------------------------

@dataclass
class MappingCheck:
    """The outcome of :func:`validate_mapping`.

    ``problems`` block the import; ``notes`` are told to the user but
    don't. The file fields are what the import uses (extra outline / silk
    files already moved to ``ignored``)."""
    copper: dict[int, Path] = field(default_factory=dict)
    drills: list[Path] = field(default_factory=list)
    outline: Path | None = None
    silk_top: Path | None = None
    silk_bot: Path | None = None
    ignored: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def validate_mapping(assignments: list[tuple[Path, int]]) -> MappingCheck:
    """Check a list of ``(file, layer id)`` against the mapping rules:

    * at least one file
    * exactly one Top file (id 1) and exactly one Bottom file (id 32)
    * any inner ids assigned must form a contiguous block 2..K
    * at most one Outline file, at most one each silk file (extra ones
      are ignored, with a note).
    """
    chk = MappingCheck()
    if not assignments:
        chk.problems.append(
            "No files yet. Add the board's Gerber and drill files.")
        return chk

    copper_by_layer: dict[int, list[Path]] = {}
    outline: list[Path] = []
    silk_top: list[Path] = []
    silk_bot: list[Path] = []
    for path, lid in assignments:
        if lid == LAYER_ID_IGNORE:
            chk.ignored.append(path)
        elif lid == LAYER_ID_DRILL:
            chk.drills.append(path)
        elif lid == LAYER_ID_OUTLINE:
            outline.append(path)
        elif lid == LAYER_ID_SILK_TOP:
            silk_top.append(path)
        elif lid == LAYER_ID_SILK_BOT:
            silk_bot.append(path)
        else:
            copper_by_layer.setdefault(lid, []).append(path)

    if LAYER_ID_TOP not in copper_by_layer:
        chk.problems.append("No file is set to Top copper.")
    if LAYER_ID_BOTTOM not in copper_by_layer:
        chk.problems.append("No file is set to Bottom copper.")
    for lid, paths in sorted(copper_by_layer.items()):
        if len(paths) > 1:
            chk.problems.append(
                f"{_role_label(lid)} is set on {len(paths)} files "
                f"({', '.join(p.name for p in paths)}). Each copper layer "
                "must come from a single Gerber.")
    inner_ids = sorted(
        lid for lid in copper_by_layer
        if lid not in (LAYER_ID_TOP, LAYER_ID_BOTTOM))
    if inner_ids:
        expected = list(range(2, 2 + len(inner_ids)))
        if inner_ids != expected:
            got = ", ".join(_role_label(i) for i in inner_ids)
            want = ", ".join(_role_label(i) for i in expected)
            chk.problems.append(
                "Inner layers must be numbered from Inner 1 with no gaps. "
                f"Set: {got}; expected: {want}.")

    def _first_only(paths: list[Path], role: str) -> Path | None:
        if len(paths) > 1:
            chk.notes.append(
                f"{len(paths)} files are set to {role}. Only the first "
                f"({paths[0].name}) is used; the others are ignored.")
            chk.ignored.extend(paths[1:])
        return paths[0] if paths else None

    chk.outline = _first_only(outline, "Outline")
    chk.silk_top = _first_only(silk_top, "Top Silk")
    chk.silk_bot = _first_only(silk_bot, "Bottom Silk")
    if not chk.drills and copper_by_layer:
        chk.notes.append(
            "No drill file: FYPA can't model vias, so copper on different "
            "layers won't connect.")
    chk.copper = {lid: paths[0] for lid, paths in copper_by_layer.items()}
    return chk


# --- Files & roles page -------------------------------------------------------

class GerberFilesPage(QWidget):
    """One row per picked file; the user picks a role from a combo box.

    Files come in through **Add Files...** (any folder, any number of
    times) or by dropping them on the page; **Remove** takes the selected
    rows out. ``changed`` fires on every edit so the window can re-run
    :meth:`check`.
    """

    changed = Signal()

    _ROLES = _LAYER_ROLE_LABELS

    def __init__(self,
                 files: list[Path] | None = None,
                 initial_assignments: dict[str, int] | None = None,
                 dialog_options=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._files: list[Path] = []
        self._combos: list[QComboBox] = []
        self._initial = dict(initial_assignments or {})
        self._dialog_options = dialog_options
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        intro = QLabel(
            "Add every copper Gerber and drill file for the board. FYPA "
            "works out each file's role from its name; change any that came "
            "out wrong. You can add files from more than one folder, or drop "
            "them here.")
        intro.setWordWrap(True)
        header.addWidget(intro, 1)
        self.add_btn = QPushButton("Add Files...", self)
        self.add_btn.setToolTip(
            "Pick Gerber / drill files (from any folder) and add them to "
            "this list. Files already listed are skipped.")
        self.add_btn.clicked.connect(self._on_add_files)
        header.addWidget(self.add_btn, 0, Qt.AlignTop)
        self.remove_btn = QPushButton("Remove", self)
        self.remove_btn.setToolTip("Take the selected files out of the list.")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(self.remove_selected)
        header.addWidget(self.remove_btn, 0, Qt.AlignTop)
        layout.addLayout(header)

        self.notice = QLabel("", self)
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)

        self.empty_label = QLabel(
            "No files yet.\n\nClick Add Files... or drop Gerber and drill "
            "files here.", self)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label, 1)

        self._table = QTableWidget(0, 2, self)
        self._table.setHorizontalHeaderLabels(["File", "Role"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self._table, 1)

        if files:
            self._add_files(list(files))
        self._sync_buttons()

    # ---- file list -----------------------------------------------------------

    def files(self) -> list[Path]:
        return list(self._files)

    def assignments(self) -> list[tuple[Path, int]]:
        return [(self._files[i], cb.currentData())
                for i, cb in enumerate(self._combos)]

    def check(self) -> MappingCheck:
        return validate_mapping(self.assignments())

    def add_files(self, paths: list[Path]) -> int:
        """Append one row per path not already listed and say how many were
        skipped. Returns how many rows were added."""
        added = self._add_files(paths)
        skipped = len(paths) - added
        if skipped:
            self._set_notice(
                f"{skipped} of the picked file(s) were already in the list "
                "and were not added again.")
        else:
            self._set_notice("")
        if added:
            self._table.scrollToBottom()
            self.changed.emit()
        return added

    def _add_files(self, paths: list[Path]) -> int:
        seen = {_path_key(p) for p in self._files}
        added = 0
        for path in paths:
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            self._append_row(Path(path))
            added += 1
        self._sync_buttons()
        return added

    def _append_row(self, path: Path) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._files.append(path)
        initial = self._initial.get(path.name, classify_file(path))
        item = QTableWidgetItem(path.name)
        item.setToolTip(str(path))
        self._table.setItem(row, 0, item)
        combo = QComboBox(self)
        for lid, label in self._ROLES:
            combo.addItem(label, lid)
        idx = next(
            (i for i, (lid, _) in enumerate(self._ROLES) if lid == initial),
            0)
        combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._combos.append(combo)
        self._table.setCellWidget(row, 1, combo)

    def remove_selected(self) -> int:
        rows = sorted(
            {i.row() for i in self._table.selectionModel().selectedIndexes()},
            reverse=True)
        for row in rows:
            self._table.removeRow(row)
            del self._files[row]
            del self._combos[row]
        self._set_notice("")
        self._sync_buttons()
        if rows:
            self.changed.emit()
        return len(rows)

    def set_role(self, row: int, layer_id: int) -> None:
        """Set a row's role by layer id (used by tests and scripts)."""
        combo = self._combos[row]
        combo.setCurrentIndex(combo.findData(layer_id))

    def _set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _sync_buttons(self) -> None:
        has_rows = bool(self._files)
        self._table.setVisible(has_rows)
        self.empty_label.setVisible(not has_rows)
        self.remove_btn.setEnabled(
            self._table.selectionModel().hasSelection())

    def _on_add_files(self) -> None:
        start_dir = str(self._files[-1].parent) if self._files else ""
        kwargs = ({} if self._dialog_options is None
                  else {"options": self._dialog_options})
        paths_str, _ = QFileDialog.getOpenFileNames(
            self, "Add Gerber + drill files", start_dir, GERBER_FILE_FILTER,
            **kwargs)
        if paths_str:
            self.add_files([Path(p) for p in paths_str])

    # ---- drag and drop -------------------------------------------------------

    @staticmethod
    def _dropped_files(event) -> list[Path]:
        md = event.mimeData()
        if md is None or not md.hasUrls():
            return []
        out = []
        for url in md.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.is_file():
                    out.append(p)
        return out

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._dropped_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        paths = self._dropped_files(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.add_files(paths)


# --- Stackup page -------------------------------------------------------------

# Copper-weight presets (oz → mm).
_COPPER_WEIGHTS: list[tuple[str, float]] = [
    ("0.5 oz (0.0175 mm)", 0.0175),
    ("1 oz (0.035 mm)",   0.035),
    ("2 oz (0.070 mm)",   0.070),
]
_DEFAULT_COPPER_WEIGHT_MM: float = 0.035
_DEFAULT_BOARD_THICKNESS_MM: float = 1.6


class GerberStackupPage(QWidget):
    """Per-copper-layer thickness editor.

    :meth:`set_layers` takes the active copper stack in Top→Bottom order
    (e.g. ``[1, 32]`` for a 2-layer board, ``[1, 2, 3, 32]`` for a 4-layer)
    and renders one row per id with its copper thickness and the
    dielectric below it. The dielectric below the last layer is fixed at
    0 (no layer further down to gap to).

    Calling :meth:`set_layers` again with a different stack (the user went
    back and changed the mapping) keeps the values of the layers that are
    still there; new layers get the default copper weight and an even
    share of the dielectric.
    """

    changed = Signal()

    def __init__(self,
                 initial: list[GerberStackupLayer] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layer_ids: list[int] = []
        self._initial_by_id = {L.layer_id: L for L in (initial or [])}
        self._built = False
        self._building = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(
            "Gerber files don't carry stackup information. Enter the "
            "copper thickness and dielectric heights for each layer. "
            "Defaults assume 1 oz copper and a 1.6 mm board split evenly "
            "across dielectrics. They only need to be in the right "
            "ballpark.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        header = QFormLayout()
        self._total_spin = QDoubleSpinBox(self)
        self._total_spin.setRange(0.1, 50.0)
        self._total_spin.setDecimals(3)
        self._total_spin.setSingleStep(0.1)
        self._total_spin.setSuffix(" mm")
        self._total_spin.setValue(_DEFAULT_BOARD_THICKNESS_MM)
        self._total_spin.valueChanged.connect(self._emit_changed)
        header.addRow("Total board thickness:", self._total_spin)

        self._weight_combo = QComboBox(self)
        for label, _ in _COPPER_WEIGHTS:
            self._weight_combo.addItem(label)
        self._weight_combo.setCurrentIndex(1)  # 1 oz
        header.addRow("Copper weight (applied to every layer):",
                      self._weight_combo)

        self.redistribute_btn = QPushButton(
            "Apply weight + re-distribute dielectric", self)
        self.redistribute_btn.clicked.connect(self.apply_defaults)
        header.addRow(self.redistribute_btn)
        layout.addLayout(header)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(
            ["Layer", "Name", "Copper (mm)", "Dielectric below (mm)"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self._table.itemChanged.connect(self._emit_changed)
        layout.addWidget(self._table, 1)

    def layer_ids(self) -> list[int]:
        return list(self._layer_ids)

    def _weight_mm(self) -> float:
        return _COPPER_WEIGHTS[self._weight_combo.currentIndex()][1]

    def _even_dielectric(self, n: int) -> float:
        if n <= 1:
            return 0.0
        total = float(self._total_spin.value())
        return max(0.0, total - self._weight_mm() * n) / (n - 1)

    def set_layers(self, ordered_layer_ids: list[int]) -> None:
        ordered = list(ordered_layer_ids)
        if self._built and ordered == self._layer_ids:
            return
        if self._built:
            keep = {L.layer_id: L for L in self.values()}
        else:
            keep = dict(self._initial_by_id)
        first_defaults = not self._built and not self._initial_by_id
        self._building = True
        try:
            self._table.setRowCount(0)
            self._layer_ids = ordered
            self._table.setRowCount(len(ordered))
            new_dielectric = self._even_dielectric(len(ordered))
            for row, lid in enumerate(ordered):
                old = keep.get(lid)
                label = _role_label(lid)
                item = QTableWidgetItem(label)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self._table.setItem(row, 0, item)
                name = (old.name if old is not None
                        else label.upper().replace(" ", "_"))
                self._table.setItem(row, 1, QTableWidgetItem(name))
                cu = QDoubleSpinBox(self)
                cu.setRange(0.001, 1.0)
                cu.setDecimals(4)
                cu.setSingleStep(0.005)
                cu.setSuffix(" mm")
                cu.setValue(old.copper_thickness_mm if old is not None
                            else self._weight_mm())
                cu.valueChanged.connect(self._emit_changed)
                self._table.setCellWidget(row, 2, cu)
                di = QDoubleSpinBox(self)
                di.setRange(0.0, 50.0)
                di.setDecimals(3)
                di.setSingleStep(0.05)
                di.setSuffix(" mm")
                di.setValue(old.dielectric_thickness_mm if old is not None
                            else new_dielectric)
                if row == len(ordered) - 1:
                    di.setValue(0.0)
                    di.setEnabled(False)
                di.valueChanged.connect(self._emit_changed)
                self._table.setCellWidget(row, 3, di)
        finally:
            self._building = False
        self._built = True
        if first_defaults:
            self.apply_defaults()
        else:
            self.changed.emit()

    def _copper_spin(self, row: int) -> QDoubleSpinBox:
        return self._table.cellWidget(row, 2)

    def _dielectric_spin(self, row: int) -> QDoubleSpinBox:
        return self._table.cellWidget(row, 3)

    def _emit_changed(self, *_a) -> None:
        if not self._building:
            self.changed.emit()

    def apply_defaults(self) -> None:
        """Push the header's copper weight onto every row + split the
        remaining (total − copper) thickness evenly across N-1 dielectric
        gaps. Runs on the first :meth:`set_layers` with no initial stackup,
        and every time the user clicks "re-distribute"."""
        weight = self._weight_mm()
        n = len(self._layer_ids)
        self._building = True
        try:
            for row in range(n):
                self._copper_spin(row).setValue(weight)
            per_dielectric = self._even_dielectric(n)
            for row in range(n - 1):
                self._dielectric_spin(row).setValue(per_dielectric)
            if n:
                self._dielectric_spin(n - 1).setValue(0.0)
        finally:
            self._building = False
        self.changed.emit()

    def values(self) -> list[GerberStackupLayer]:
        rows: list[GerberStackupLayer] = []
        for row, lid in enumerate(self._layer_ids):
            name_item = self._table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            rows.append(GerberStackupLayer(
                layer_id=lid, name=name or f"L{row + 1}",
                copper_thickness_mm=float(self._copper_spin(row).value()),
                dielectric_thickness_mm=float(
                    self._dielectric_spin(row).value()),
            ))
        return rows

    def total_thickness_mm(self) -> float:
        return float(self._total_spin.value())

    def problems(self) -> list[str]:
        return [f"Copper thickness for {_role_label(L.layer_id)} must be "
                "more than 0."
                for L in self.values() if L.copper_thickness_mm <= 0]

    def sum_warning(self) -> str | None:
        """Text for when the layers don't add up to the stated total (more
        than 0.1 mm out), else None. A warning, not a problem: asymmetric
        stackups are common."""
        rows = self.values()
        if not rows:
            return None
        summed = sum(L.copper_thickness_mm + L.dielectric_thickness_mm
                     for L in rows)
        total = self.total_thickness_mm()
        if abs(summed - total) <= 0.1:
            return None
        return (f"Copper + dielectric add up to {summed:.3f} mm, but the "
                f"total board thickness is {total:.3f} mm. That's fine for "
                "an asymmetric stackup; otherwise click Apply weight + "
                "re-distribute dielectric.")


def _order_copper_layer_ids(ids: list[int]) -> list[int]:
    """Top → Inner 1 → Inner 2 → … → Bottom."""
    top = [i for i in ids if i == LAYER_ID_TOP]
    bottom = [i for i in ids if i == LAYER_ID_BOTTOM]
    inner = sorted(i for i in ids if i not in (LAYER_ID_TOP, LAYER_ID_BOTTOM))
    return top + inner + bottom


def build_import_result(check: MappingCheck,
                        stackup: list[GerberStackupLayer]
                        ) -> GerberImportResult:
    """Package a passing :class:`MappingCheck` and the stackup rows."""
    return GerberImportResult(
        copper_files=dict(check.copper),
        drill_files=list(check.drills),
        outline_file=check.outline,
        silk_top_file=check.silk_top,
        silk_bot_file=check.silk_bot,
        ignored_files=list(check.ignored),
        stackup=list(stackup),
    )
