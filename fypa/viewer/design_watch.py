"""Noticing that the Altium design changed on disk, and saying so.

A solve keeps working from the design as FYPA read it (a Solve never
re-reads the Altium files), so after a save in Altium the result on screen is
for the older board. The loaded design records the files it was read from
(``metadata["design_files"]``: path -> (modified time, size), set by the solve
worker). Whenever FYPA comes back to the front those files are looked at
again, and if any differ a chip appears at the right-hand end of the menu
bar::

    File  View  Reports  Help                          DESIGN CHANGED  Reload

A click reloads the design (File > Reload Design, Ctrl+R): the Altium files
are read again and the design is solved with the same setup.

Only Altium designs are watched - a Gerber import has no project to reload
from. A design whose record is missing (a solution saved by an older FYPA)
shows no chip: unknown is not reported as changed.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

from fypa.viewer.theme import _T

_log = logging.getLogger(__name__)

# How many file names the chip's tooltip lists before "and N more".
_MAX_NAMES = 6


def changed_design_files(baseline, prjpcb_path) -> list[str]:
    """Names of the design files that differ from ``baseline`` (the
    ``metadata["design_files"]`` record): modified, added to the project or
    removed from it. Empty when nothing changed - and when it can't be told:
    no record, or the project file itself is not reachable (a moved project,
    a disconnected drive), where every file would otherwise read as changed.
    """
    if not baseline or not prjpcb_path:
        return []
    prjpcb = Path(prjpcb_path)
    if not prjpcb.exists():
        return []
    from fypa.cli import _project_file_fingerprints

    now = _project_file_fingerprints(prjpcb)
    before = {str(p): (tuple(fp) if fp is not None else None)
              for p, fp in dict(baseline).items()}
    changed = [p for p in set(before) | set(now)
               if before.get(p) != now.get(p)]
    return sorted({Path(p).name for p in changed}, key=str.lower)


def design_changed_tooltip(names) -> str:
    names = list(names)
    shown = ", ".join(names[:_MAX_NAMES])
    if len(names) > _MAX_NAMES:
        shown += f" and {len(names) - _MAX_NAMES} more"
    return ("Changed on disk since FYPA read the design: "
            f"{shown}.\nThe result on screen is for the design as it was "
            "read. Click to read the Altium files again and solve with the "
            "same setup (File > Reload Design, Ctrl+R).")


class DesignChangedChip(QWidget):
    """"DESIGN CHANGED  Reload" on the menu bar; hidden while the design on
    disk is the one FYPA read. :attr:`reload_requested` on a click."""

    reload_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fypaDesignChangedChip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(6)
        self._caption = QLabel("DESIGN CHANGED", self)
        cap_font = QFont(self.font())
        cap_font.setPointSizeF(7.0)
        cap_font.setBold(True)
        cap_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        self._caption.setFont(cap_font)
        self._action = QLabel("Reload", self)
        act_font = QFont(self.font())
        act_font.setBold(True)
        self._action.setFont(act_font)
        for w in (self._caption, self._action):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lay.addWidget(w, 0, Qt.AlignVCenter)
        self._names: tuple[str, ...] = ()
        self.setVisible(False)

    def file_names(self) -> tuple[str, ...]:
        return self._names

    def set_files(self, names) -> None:
        """Show the chip for the changed files ``names``, or hide it."""
        self._names = tuple(names)
        self.setToolTip(design_changed_tooltip(self._names)
                        if self._names else "")
        self.restyle()
        self.setVisible(bool(self._names))

    def restyle(self) -> None:
        t = _T()
        warn = t["warn"]
        self._caption.setStyleSheet(f"QLabel {{ color: {warn}; }}")
        self._action.setStyleSheet(f"QLabel {{ color: {warn}; }}")
        self.setStyleSheet(
            f"QWidget#fypaDesignChangedChip {{ border: 1px solid {warn};"
            f" border-radius: 3px; background: transparent; }}"
            f"QWidget#fypaDesignChangedChip:hover {{"
            f" background: {t['bg_hover']}; }}")
        self.adjustSize()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt name
        if event.button() == Qt.LeftButton:
            self.reload_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _CheckSignaller(QObject):
    """Carries a finished check from its thread back to the window."""

    done = Signal(int, object)      # (check number, changed file names)


class _DesignWatchMixin:
    """The window's side of the check. See the module doc."""

    _design_changed_chip: DesignChangedChip | None = None
    _design_changed_files: tuple[str, ...] = ()
    _design_check_no: int = 0       # checks started
    _design_checks_done: int = 0    # answers taken (overtaken ones are not)
    _design_watch_signaller: _CheckSignaller | None = None

    def _init_design_watch(self) -> None:
        """Called once from ``_build_ui``, after the menu bar exists."""
        signaller = _CheckSignaller(self)
        signaller.done.connect(self._on_design_check_done,
                               Qt.QueuedConnection)
        self._design_watch_signaller = signaller
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(
                self._on_app_state_for_design_watch)
        self._check_design_changed()

    def _on_app_state_for_design_watch(self, state) -> None:
        # Back to the front - typically from Altium, after a save there.
        if state == Qt.ApplicationActive:
            self._check_design_changed()

    def _design_watch_target(self):
        """``(prjpcb path, files record)`` for the design on screen, or
        ``None`` when there is nothing to compare (see the module doc)."""
        # A What-If view parks the base result aside; either carries the
        # same design's record.
        md = getattr(self, "metadata", None)
        if not isinstance(md, dict):
            return None
        baseline = md.get("design_files")
        prjpcb = md.get("prjpcb_path")
        if (not baseline or not prjpcb
                or Path(str(prjpcb)).suffix.lower() != ".prjpcb"):
            return None
        return Path(str(prjpcb)), dict(baseline)

    def _check_design_changed(self) -> None:
        """Compare the design files with the record, off the GUI thread (a
        project on a slow or sleeping drive must not stall the window), and
        update the chip when the answer comes back."""
        self._design_check_no += 1
        target = self._design_watch_target()
        signaller = self._design_watch_signaller
        if target is None or signaller is None:
            self._set_design_changed(())
            return
        prjpcb, baseline = target
        check_no = self._design_check_no

        def work() -> None:
            try:
                names = changed_design_files(baseline, prjpcb)
            except Exception:  # noqa: BLE001 - a hint, never an error
                _log.debug("design-changed check failed", exc_info=True)
                return
            try:
                signaller.done.emit(check_no, names)
            except RuntimeError:        # the window went away meanwhile
                pass

        threading.Thread(target=work, name="fypa-design-watch",
                         daemon=True).start()

    def _on_design_check_done(self, check_no: int, names) -> None:
        if check_no != self._design_check_no:
            return                      # an older check, overtaken
        self._design_checks_done += 1
        self._set_design_changed(tuple(names or ()))

    def _set_design_changed(self, names: tuple[str, ...]) -> None:
        newly = bool(names) and not self._design_changed_files
        self._design_changed_files = tuple(names)
        self._sync_design_changed_chip()
        if newly:
            try:
                bar = self.statusBar()
                # A card of its own where there is one (fypa.viewer.toast),
                # so the next status message doesn't replace it.
                getattr(bar, "notify", bar.showMessage)(
                    "The design changed on disk - File > Reload Design "
                    "(Ctrl+R) reads it again.", 15000)
            except RuntimeError:
                pass

    def _sync_design_changed_chip(self) -> None:
        names = self._design_changed_files
        chip = self._design_changed_chip
        if chip is None:
            if not names:
                return
            try:
                mb = self.menuBar()
            except RuntimeError:
                return
            chip = DesignChangedChip(mb)
            chip.reload_requested.connect(
                lambda: self._on_reload_design_info())
            self._design_changed_chip = chip
        chip.set_files(names)
        self._position_design_changed_chip()

    def _position_design_changed_chip(self) -> None:
        """At the right-hand end of the menu bar, clear of the Global Focus
        and What-If chips on the left."""
        chip = self._design_changed_chip
        if chip is None or chip.isHidden():
            return
        try:
            mb = self.menuBar()
            chip.adjustSize()
            h = min(chip.sizeHint().height(), max(1, mb.height() - 2))
            w = chip.sizeHint().width()
            chip.resize(w, h)
            chip.move(max(0, mb.width() - w - 8),
                      max(0, (mb.height() - h) // 2))
            chip.raise_()
        except RuntimeError:  # a widget went away mid-rebuild
            pass
