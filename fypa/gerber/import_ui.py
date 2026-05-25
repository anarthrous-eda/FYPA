"""PySide6 dialogs for the Gerber import flow.

Two dialogs, shown in sequence after the user picks a set of Gerber +
Excellon files:

1. :class:`GerberLayerMappingDialog` — confirm / edit the auto-classified
   role of each picked file (Top / Inner-N / Bottom / silk / outline /
   drill / ignore).

2. :class:`GerberStackupDialog` — set per-copper-layer thickness and the
   dielectric below each layer; pre-filled with sensible defaults (1 oz
   copper, 1.6 mm total board split evenly across dielectrics).

The dialogs are intentionally standalone — they don't import the viewer
module and can be exercised in isolation from a script (useful for tests
and for the ``FYPA gerber-gui`` CLI's no-stored-config path).

Drive the full flow with :func:`run_gerber_import_dialogs`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
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
        (LAYER_ID_DRILL, "Drill (Excellon)"),
    ]
)


def _role_label(layer_id: int) -> str:
    for lid, label in _LAYER_ROLE_LABELS:
        if lid == layer_id:
            return label
    return f"Layer {layer_id}"


@dataclass(frozen=True)
class GerberImportResult:
    """Everything the importer needs after the dialogs accept.

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


# --- Layer-mapping dialog -----------------------------------------------------

class GerberLayerMappingDialog(QDialog):
    """One row per picked file; user picks a role from a combo box.

    Validation on Accept:

    * exactly one Top file (id 1)
    * exactly one Bottom file (id 32)
    * any inner ids assigned must form a contiguous block 2..K
    * at most one Outline file, at most one each silk file (extra ones
      are dropped to "Ignore" with a warning).
    """

    _ROLES = _LAYER_ROLE_LABELS

    def __init__(self,
                 picked_files: list[Path],
                 initial_assignments: dict[str, int] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Gerber Import — Layer Mapping")
        self.resize(720, 480)
        self._files = list(picked_files)
        self._initial = dict(initial_assignments or {})

        layout = QVBoxLayout(self)
        intro = QLabel(
            "FYPA auto-detected the role of each file from its filename.\n"
            "Adjust any rows that came out wrong, then click OK."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._table = QTableWidget(len(self._files), 2, self)
        self._table.setHorizontalHeaderLabels(["File", "Role"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents,
        )
        self._combos: list[QComboBox] = []
        for row, path in enumerate(self._files):
            initial = self._initial.get(path.name, classify_file(path))
            item = QTableWidgetItem(path.name)
            item.setToolTip(str(path))
            self._table.setItem(row, 0, item)
            combo = QComboBox(self)
            for lid, label in self._ROLES:
                combo.addItem(label, lid)
            # Select the matching role.
            idx = next(
                (i for i, (lid, _) in enumerate(self._ROLES) if lid == initial),
                0,
            )
            combo.setCurrentIndex(idx)
            self._combos.append(combo)
            self._table.setCellWidget(row, 1, combo)
        layout.addWidget(self._table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _current_assignments(self) -> list[tuple[Path, int]]:
        return [(self._files[i], cb.currentData())
                for i, cb in enumerate(self._combos)]

    def _on_accept(self) -> None:
        # Collect, validate.
        copper_by_layer: dict[int, list[Path]] = {}
        outline: list[Path] = []
        silk_top: list[Path] = []
        silk_bot: list[Path] = []
        drills: list[Path] = []
        ignored: list[Path] = []

        for path, lid in self._current_assignments():
            if lid == LAYER_ID_IGNORE:
                ignored.append(path)
            elif lid == LAYER_ID_DRILL:
                drills.append(path)
            elif lid == LAYER_ID_OUTLINE:
                outline.append(path)
            elif lid == LAYER_ID_SILK_TOP:
                silk_top.append(path)
            elif lid == LAYER_ID_SILK_BOT:
                silk_bot.append(path)
            else:
                copper_by_layer.setdefault(lid, []).append(path)

        problems: list[str] = []
        if LAYER_ID_TOP not in copper_by_layer:
            problems.append("No Top copper layer was assigned.")
        if LAYER_ID_BOTTOM not in copper_by_layer:
            problems.append("No Bottom copper layer was assigned.")
        for lid, paths in copper_by_layer.items():
            if len(paths) > 1:
                problems.append(
                    f"{_role_label(lid)} is assigned to "
                    f"{len(paths)} files: "
                    f"{', '.join(p.name for p in paths)}. "
                    "Each copper layer must come from a single Gerber.",
                )
        inner_ids = sorted(
            lid for lid in copper_by_layer
            if lid not in (LAYER_ID_TOP, LAYER_ID_BOTTOM)
        )
        if inner_ids:
            expected = list(range(2, 2 + len(inner_ids)))
            if inner_ids != expected:
                problems.append(
                    "Inner copper layers must be assigned contiguously "
                    f"starting from Inner 1 (id 2). Got: {inner_ids}; "
                    f"expected: {expected}.",
                )

        if problems:
            QMessageBox.warning(
                self, "Layer mapping incomplete",
                "Please fix the following before continuing:\n\n• "
                + "\n• ".join(problems),
            )
            return

        # Multi-outline / multi-silk: keep the first, ignore the rest.
        if len(outline) > 1:
            QMessageBox.information(
                self, "Multiple outline files",
                f"{len(outline)} files were assigned 'Outline'. Only the "
                f"first ({outline[0].name}) will be used; the others will "
                "be ignored.",
            )
            ignored.extend(outline[1:])
            outline = outline[:1]
        if len(silk_top) > 1:
            ignored.extend(silk_top[1:])
            silk_top = silk_top[:1]
        if len(silk_bot) > 1:
            ignored.extend(silk_bot[1:])
            silk_bot = silk_bot[:1]

        self._result_copper = {lid: paths[0]
                               for lid, paths in copper_by_layer.items()}
        self._result_drills = drills
        self._result_outline = outline[0] if outline else None
        self._result_silk_top = silk_top[0] if silk_top else None
        self._result_silk_bot = silk_bot[0] if silk_bot else None
        self._result_ignored = ignored
        self.accept()

    # ---- post-accept accessors ------------------------------------------------

    def copper_files(self) -> dict[int, Path]:
        return dict(self._result_copper)

    def drill_files(self) -> list[Path]:
        return list(self._result_drills)

    def outline_file(self) -> Path | None:
        return self._result_outline

    def silk_top(self) -> Path | None:
        return self._result_silk_top

    def silk_bot(self) -> Path | None:
        return self._result_silk_bot

    def ignored_files(self) -> list[Path]:
        return list(self._result_ignored)


# --- Stackup dialog -----------------------------------------------------------

# Copper-weight presets (oz → mm).
_COPPER_WEIGHTS: list[tuple[str, float]] = [
    ("0.5 oz (0.0175 mm)", 0.0175),
    ("1 oz (0.035 mm)",   0.035),
    ("2 oz (0.070 mm)",   0.070),
]
_DEFAULT_COPPER_WEIGHT_MM: float = 0.035
_DEFAULT_BOARD_THICKNESS_MM: float = 1.6


class GerberStackupDialog(QDialog):
    """Per-copper-layer thickness editor.

    ``ordered_layer_ids`` is the active copper stack in Top→Bottom order
    (e.g. ``[1, 32]`` for a 2-layer board, ``[1, 2, 3, 32]`` for a 4-layer).
    The dialog renders one row per id and lets the user set copper
    thickness + the dielectric below it. The dielectric below the last
    layer is fixed at 0 (no layer further down to gap to).
    """

    def __init__(self,
                 ordered_layer_ids: list[int],
                 initial: list[GerberStackupLayer] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Gerber Import — Stackup")
        self.resize(560, 420)
        self._layer_ids = list(ordered_layer_ids)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Gerber files don't carry stackup information. Enter the "
            "copper thickness and dielectric heights for each layer.\n"
            "Defaults assume 1 oz copper and a 1.6 mm total board "
            "thickness split evenly across dielectrics."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Header row — total thickness + copper weight preset.
        header = QFormLayout()
        self._total_spin = QDoubleSpinBox(self)
        self._total_spin.setRange(0.1, 50.0)
        self._total_spin.setDecimals(3)
        self._total_spin.setSingleStep(0.1)
        self._total_spin.setSuffix(" mm")
        self._total_spin.setValue(_DEFAULT_BOARD_THICKNESS_MM)
        header.addRow("Total board thickness:", self._total_spin)

        self._weight_combo = QComboBox(self)
        for label, _ in _COPPER_WEIGHTS:
            self._weight_combo.addItem(label)
        self._weight_combo.setCurrentIndex(1)  # 1 oz
        header.addRow("Copper weight (applied to every layer):", self._weight_combo)

        redistribute = QPushButton("Apply weight + re-distribute dielectric", self)
        redistribute.clicked.connect(self._apply_defaults)
        header.addRow(redistribute)
        layout.addLayout(header)

        # Per-layer table.
        self._table = QTableWidget(len(self._layer_ids), 4, self)
        self._table.setHorizontalHeaderLabels(
            ["Layer", "Name", "Copper (mm)", "Dielectric below (mm)"],
        )
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch,
        )

        initial_by_id = {L.layer_id: L for L in (initial or [])}
        for row, lid in enumerate(self._layer_ids):
            label = _role_label(lid)
            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._table.setItem(row, 0, item)
            # Name column — defaults to the role label uppercased.
            name = (initial_by_id[lid].name
                    if lid in initial_by_id
                    else label.upper().replace(" ", "_"))
            self._table.setItem(row, 1, QTableWidgetItem(name))
            # Copper spin.
            cu = QDoubleSpinBox(self)
            cu.setRange(0.001, 1.0)
            cu.setDecimals(4)
            cu.setSingleStep(0.005)
            cu.setSuffix(" mm")
            cu.setValue(initial_by_id[lid].copper_thickness_mm
                        if lid in initial_by_id
                        else _DEFAULT_COPPER_WEIGHT_MM)
            self._table.setCellWidget(row, 2, cu)
            # Dielectric spin — disabled on the last row.
            di = QDoubleSpinBox(self)
            di.setRange(0.0, 50.0)
            di.setDecimals(3)
            di.setSingleStep(0.05)
            di.setSuffix(" mm")
            di.setValue(initial_by_id[lid].dielectric_thickness_mm
                        if lid in initial_by_id
                        else 0.0)
            if row == len(self._layer_ids) - 1:
                di.setValue(0.0)
                di.setEnabled(False)
            self._table.setCellWidget(row, 3, di)
        layout.addWidget(self._table)

        # If no initial stackup, fire the defaulting button once so the
        # dielectrics are populated based on the default total + weight.
        if not initial:
            self._apply_defaults()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _copper_spin(self, row: int) -> QDoubleSpinBox:
        return self._table.cellWidget(row, 2)

    def _dielectric_spin(self, row: int) -> QDoubleSpinBox:
        return self._table.cellWidget(row, 3)

    def _apply_defaults(self) -> None:
        """Push the header's copper weight onto every row + split the
        remaining (total − copper) thickness evenly across N-1 dielectric
        gaps. Called on dialog open with no initial stackup, and again
        every time the user clicks "re-distribute"."""
        weight = _COPPER_WEIGHTS[self._weight_combo.currentIndex()][1]
        total = float(self._total_spin.value())
        n = len(self._layer_ids)
        for row in range(n):
            self._copper_spin(row).setValue(weight)
        if n <= 1:
            return
        copper_total = weight * n
        dielectric_total = max(0.0, total - copper_total)
        per_dielectric = dielectric_total / (n - 1)
        for row in range(n - 1):
            self._dielectric_spin(row).setValue(per_dielectric)
        self._dielectric_spin(n - 1).setValue(0.0)

    def _on_accept(self) -> None:
        # Read out the per-layer values; warn (not error) if the sum
        # diverges from the user's stated total by more than 0.1 mm.
        rows: list[GerberStackupLayer] = []
        for row, lid in enumerate(self._layer_ids):
            name_item = self._table.item(row, 1)
            name = name_item.text().strip() if name_item else f"L{row + 1}"
            cu = float(self._copper_spin(row).value())
            di = float(self._dielectric_spin(row).value())
            if cu <= 0:
                QMessageBox.warning(
                    self, "Invalid copper thickness",
                    f"Copper thickness for {_role_label(lid)} must be > 0.",
                )
                return
            rows.append(GerberStackupLayer(
                layer_id=lid, name=name or f"L{row + 1}",
                copper_thickness_mm=cu, dielectric_thickness_mm=di,
            ))
        summed = sum(L.copper_thickness_mm + L.dielectric_thickness_mm for L in rows)
        total = float(self._total_spin.value())
        if abs(summed - total) > 0.1:
            ok = QMessageBox.question(
                self, "Stackup doesn't sum to total",
                f"Per-layer copper + dielectric sums to {summed:.3f} mm, "
                f"but the stated total is {total:.3f} mm. Use the per-layer "
                "values anyway? Asymmetric stackups are common — only "
                "click No if you'd like to fix the values first.",
            )
            if ok != QMessageBox.Yes:
                return
        self._result = rows
        self.accept()

    def stackup(self) -> list[GerberStackupLayer]:
        return list(self._result)


# --- Orchestrator -------------------------------------------------------------

def run_gerber_import_dialogs(
    picked_files: list[Path],
    parent: QWidget | None = None,
    initial_assignments: dict[str, int] | None = None,
    initial_stackup: list[GerberStackupLayer] | None = None,
) -> GerberImportResult | None:
    """Drive the layer-mapping → stackup dialog sequence.

    Returns ``None`` if the user cancels at any point; otherwise the
    fully-populated :class:`GerberImportResult` ready to be persisted in
    the .fypa and passed to
    :func:`fypa.gerber.extract.extract_gerber_project`.
    """
    mapping = GerberLayerMappingDialog(
        picked_files, initial_assignments=initial_assignments, parent=parent,
    )
    if mapping.exec() != QDialog.Accepted:
        return None
    copper = mapping.copper_files()

    ordered = _order_copper_layer_ids(list(copper.keys()))
    stackup_dlg = GerberStackupDialog(
        ordered, initial=initial_stackup, parent=parent,
    )
    if stackup_dlg.exec() != QDialog.Accepted:
        return None
    return GerberImportResult(
        copper_files=copper,
        drill_files=mapping.drill_files(),
        outline_file=mapping.outline_file(),
        silk_top_file=mapping.silk_top(),
        silk_bot_file=mapping.silk_bot(),
        ignored_files=mapping.ignored_files(),
        stackup=stackup_dlg.stackup(),
    )


def _order_copper_layer_ids(ids: list[int]) -> list[int]:
    """Top → Inner 1 → Inner 2 → … → Bottom."""
    top = [i for i in ids if i == LAYER_ID_TOP]
    bottom = [i for i in ids if i == LAYER_ID_BOTTOM]
    inner = sorted(i for i in ids if i not in (LAYER_ID_TOP, LAYER_ID_BOTTOM))
    return top + inner + bottom
