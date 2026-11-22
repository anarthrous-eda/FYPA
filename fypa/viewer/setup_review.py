"""Review dialog for saved setup entries the design no longer matches
exactly (see :func:`fypa.setup_sidecar.match_saved`).

One row per held entry: what it was, why it was held, and a decision -
move it to one of the candidate parts, keep it held (tried again on the
next import), remove it, or pick any other part. Nothing changes until
**Apply**; **Decide later** keeps everything held.
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
)

from fypa.setup_sidecar import (
    REVIEW_AMBIGUOUS,
    REVIEW_LABELS,
    REVIEW_LIKELY,
    REVIEW_NET_CHANGED,
    Candidate,
    ReviewItem,
    describe,
)
from fypa.viewer.theme import _T

ACCEPT = "accept"
KEEP = "keep"
REMOVE = "remove"
_OTHER = "other"

# Statuses whose best candidate is pre-selected: the matcher found one clear
# answer. Ambiguous and missing rows start on "Keep held" - the user picks.
_PRESELECT = (REVIEW_NET_CHANGED, REVIEW_LIKELY)

_COLS = ("Entry", "Status", "Why it was held", "Decision", "")


def _status_color(status: str) -> str:
    t = _T()
    return {REVIEW_NET_CHANGED: t["warn"], REVIEW_LIKELY: t["accent"],
            REVIEW_AMBIGUOUS: t["warn"]}.get(status, t["err"])


class SetupReviewDialog(QDialog):
    """``decisions()`` after ``exec()``: ``[(item, action, candidate)]``
    for every row, ``action`` one of ``ACCEPT`` / ``KEEP`` / ``REMOVE``.

    ``retarget(item, designator)`` builds a :class:`Candidate` for a part
    the user picked by hand (or returns a reason string when the entry
    can't go there); ``designators`` lists the parts offered for that.
    ``show_on_board(xy, designator)`` jumps the Board to a part."""

    def __init__(self, parent, items: list[ReviewItem], *,
                 applied_notes: list[str] | None = None,
                 designators: list[str] | None = None,
                 retarget: Callable[[ReviewItem, str], Candidate | str] | None = None,
                 show_on_board: Callable[[tuple | None, str | None], None] | None = None,
                 ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review the saved setup")
        self._items = list(items)
        self._designators = sorted(designators or [])
        self._retarget = retarget
        self._show_on_board = show_on_board
        # Per row: the candidates its combo offers (own picks appended).
        self._cands: list[list[Candidate]] = [list(it.candidates)
                                              for it in self._items]
        self._combos: list[QComboBox] = []
        t = _T()

        outer = QVBoxLayout(self)
        n = len(self._items)
        intro = QLabel(
            "<h3 style='margin:0 0 4px 0;'>Review the saved setup</h3>"
            "The design has changed since this setup was saved. "
            f"{n} {'entry' if n == 1 else 'entries'} couldn't be matched to "
            "the same part with certainty, so "
            f"{'it was' if n == 1 else 'they were'} not applied. Choose what "
            "to do with each - nothing changes until you press "
            "<b>Apply</b>.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        outer.addWidget(intro)

        if applied_notes:
            notes = QLabel(
                "<b>Applied automatically</b> (provably the same part):<br>"
                + "<br>".join(f"&nbsp;&nbsp;• {s}" for s in applied_notes))
            notes.setTextFormat(Qt.RichText)
            notes.setWordWrap(True)
            notes.setStyleSheet(f"color: {t['fg_muted']};")
            outer.addWidget(notes)

        self.table = QTableWidget(n, len(_COLS))
        self.table.setHorizontalHeaderLabels(list(_COLS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # One line per row; a long reason is elided, its tooltip has it all.
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        for row, item in enumerate(self._items):
            self._fill_row(row, item)
        self.table.resizeRowsToContents()
        outer.addWidget(self.table, 1)

        hint = QLabel(
            "<i>Keep held</i> leaves an entry in the setup file and tries it "
            "again each time the design is imported. ⌖ shows the part on "
            "the Board.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {t['fg_dim']};")
        outer.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        later = QPushButton("Decide later")
        later.setToolTip("Keep every entry held; Help > Review saved setup… "
                         "opens this again.")
        later.clicked.connect(self.reject)
        apply_btn = QPushButton("Apply")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self.accept)
        buttons.addWidget(later)
        buttons.addWidget(apply_btn)
        outer.addLayout(buttons)
        self.resize(980, min(640, 220 + 46 * max(n, 3)))

    # --- rows ----------------------------------------------------------------

    def _fill_row(self, row: int, item: ReviewItem) -> None:
        d = item.entry.directive
        self.table.setItem(row, 0, QTableWidgetItem(describe(d)))
        st = QTableWidgetItem(REVIEW_LABELS.get(item.status, item.status))
        from PySide6.QtGui import QColor
        st.setForeground(QColor(_status_color(item.status)))
        self.table.setItem(row, 1, st)
        reason = item.entry.reason
        if item.status in (REVIEW_LIKELY, REVIEW_AMBIGUOUS) and item.candidates:
            # The evidence for the suggestion, without hovering.
            best = item.candidates[0]
            reason += f". {best.designator}: {best.why}"
        cell = QTableWidgetItem(reason)
        cell.setToolTip(reason)
        self.table.setItem(row, 2, cell)

        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._combos.append(combo)
        self._fill_combo(row)
        combo.setCurrentIndex(0 if (item.status in _PRESELECT
                                    and self._cands[row]) else
                              len(self._cands[row]))
        combo.activated.connect(lambda idx, r=row: self._on_choice(r, idx))
        self.table.setCellWidget(row, 3, combo)

        jump = QToolButton()
        jump.setText("⌖")
        jump.setToolTip("Show the chosen part (or where the entry was) on "
                        "the Board")
        jump.setEnabled(self._show_on_board is not None)
        jump.clicked.connect(lambda _c=False, r=row: self._jump(r))
        self.table.setCellWidget(row, 4, jump)

    def _fill_combo(self, row: int) -> None:
        combo = self._combos[row]
        combo.blockSignals(True)
        combo.clear()
        for c in self._cands[row]:
            label = f"Move to {c.designator}"
            extra = [x for x in c.changes
                     if not x.startswith(f"{self._items[row].entry.directive.designator} →")]
            if extra:
                label += f" ({'; '.join(extra)})"
            combo.addItem(label, (ACCEPT, c))
            combo.setItemData(combo.count() - 1, c.why, Qt.ToolTipRole)
        combo.addItem("Keep held", (KEEP, None))
        combo.addItem("Remove from the setup", (REMOVE, None))
        if self._retarget is not None and self._designators:
            combo.addItem("Choose another part…", (_OTHER, None))
        combo.blockSignals(False)

    def _on_choice(self, row: int, idx: int) -> None:
        combo = self._combos[row]
        action, _cand = combo.itemData(idx)
        if action != _OTHER:
            return
        keep_idx = len(self._cands[row])
        des, ok = QInputDialog.getItem(
            self, "Choose a part",
            f"Move {describe(self._items[row].entry.directive)} to:",
            self._designators, 0, True)
        if not ok or not des:
            combo.setCurrentIndex(keep_idx)
            return
        result = self._retarget(self._items[row], des)  # type: ignore[misc]
        if isinstance(result, str):
            QMessageBox.information(self, "Can't move it there", result)
            combo.setCurrentIndex(keep_idx)
            return
        self._cands[row].append(result)
        self._fill_combo(row)
        combo.setCurrentIndex(len(self._cands[row]) - 1)

    def _jump(self, row: int) -> None:
        if self._show_on_board is None:
            return
        action, cand = self._combos[row].currentData()
        if action == ACCEPT and cand is not None:
            self._show_on_board(cand.xy, cand.designator)
            return
        anchor = self._items[row].entry.anchor or {}
        xy = anchor.get("xy")
        self._show_on_board(tuple(xy) if xy else None,
                            self._items[row].entry.directive.designator)

    # --- result --------------------------------------------------------------

    def set_choice(self, row: int, action: str,
                   candidate: Candidate | None = None) -> None:
        """Pick a row's decision (used by tests and by callers that want a
        default other than the matcher's)."""
        combo = self._combos[row]
        for i in range(combo.count()):
            a, c = combo.itemData(i)
            if a == action and (candidate is None or c is candidate):
                combo.setCurrentIndex(i)
                return

    def decisions(self) -> list[tuple[ReviewItem, str, Candidate | None]]:
        out = []
        for item, combo in zip(self._items, self._combos):
            action, cand = combo.currentData()
            out.append((item, KEEP if action == _OTHER else action, cand))
        return out


__all__ = ["ACCEPT", "KEEP", "REMOVE", "SetupReviewDialog"]
