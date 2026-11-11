"""Run a project / solution save off the GUI thread behind a progress dialog.

A large design's save copies the solve + design-info pickles beside the
``.fypa`` and may pickle the in-memory solution — hundreds of MB that used to
run on the GUI thread, freezing the window ("Not Responding") with no hint of
what it was doing. :func:`run_save_with_progress` runs the Qt-free part on a
plain worker thread and spins a local event loop until it finishes, so callers
still get a synchronous result (``_confirm_replace_project`` relies on that)
while the window keeps repainting and the dialog shows the current step and
how many MB have been written.

The worker never touches Qt: it reports through a :class:`SaveStatus`, which
the GUI thread polls on a timer.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QProgressDialog

_COPY_CHUNK = 8 * 1024 * 1024
_POLL_MS = 100
# Saves that finish faster than this never show the dialog, so a small
# project's Ctrl+S doesn't flash a popup.
_SHOW_AFTER_S = 0.3


class SaveStatus:
    """Progress shared between the save worker (writer) and the GUI poll
    (reader). Plain attribute stores are atomic under the GIL, which is all
    the GUI side needs to draw a consistent-enough label."""

    def __init__(self) -> None:
        self.step = "Preparing…"
        self.done_bytes = 0
        self.total_bytes = 0     # 0 → unknown size (busy indicator)

    def begin(self, step: str, total_bytes: int = 0) -> None:
        self.done_bytes = 0
        self.total_bytes = max(0, int(total_bytes))
        self.step = step

    def add(self, n: int) -> None:
        self.done_bytes += n


class _CountingWriter:
    """File wrapper that tallies bytes into a :class:`SaveStatus`. ``pickle``
    only needs ``write``."""

    def __init__(self, f, status: SaveStatus) -> None:
        self._f = f
        self._status = status

    def write(self, b) -> int:
        n = self._f.write(b)
        self._status.add(len(b) if n is None else n)
        return n


def counting_writer(f, status: SaveStatus | None):
    return f if status is None else _CountingWriter(f, status)


def copy_with_progress(src: Path, dst: Path,
                       status: SaveStatus | None, label: str) -> None:
    """``shutil.copy2`` with byte progress. Writes to a temp name first and
    swaps it in, so a failed copy never leaves a truncated sidecar."""
    src, dst = Path(src), Path(dst)
    try:
        total = src.stat().st_size
    except OSError:
        total = 0
    if status is not None:
        status.begin(label, total)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                buf = fi.read(_COPY_CHUNK)
                if not buf:
                    break
                fo.write(buf)
                if status is not None:
                    status.add(len(buf))
        shutil.copystat(src, tmp)
        tmp.replace(dst)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fmt_mb(n: int) -> str:
    return f"{n / (1024 * 1024):,.0f} MB"


def run_save_with_progress(parent, title: str, work):
    """Run ``work(status)`` on a worker thread; return ``(result, exc)``.

    ``work`` must be Qt-free. Exactly one of ``result`` / ``exc`` is
    meaningful: ``exc`` is the exception ``work`` raised, else ``None``.
    Blocks the caller (via a local event loop, not a frozen GUI) until done.
    An application-modal dialog keeps the user from editing the design
    mid-save."""
    status = SaveStatus()
    box: dict = {"result": None, "exc": None}

    def _run() -> None:
        try:
            box["result"] = work(status)
        except BaseException as e:  # noqa: BLE001 — surfaced to the caller
            box["exc"] = e

    th = threading.Thread(target=_run, name="fypa-project-save", daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(_SHOW_AFTER_S)
    if not th.is_alive():
        return box["result"], box["exc"]

    dlg = QProgressDialog(status.step, "", 0, 0, parent)
    dlg.setWindowTitle(title)
    dlg.setCancelButton(None)
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setMinimumWidth(420)
    dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint
                       & ~Qt.WindowCloseButtonHint)

    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(_POLL_MS)

    def _poll() -> None:
        elapsed = time.monotonic() - t0
        text = status.step
        done, total = status.done_bytes, status.total_bytes
        if total > 0:
            dlg.setRange(0, 1000)
            dlg.setValue(min(1000, int(1000 * done / total)))
            text += f"\n{_fmt_mb(done)} of {_fmt_mb(total)}"
        else:
            if dlg.maximum() != 0:
                dlg.setRange(0, 0)
            if done:
                text += f"\n{_fmt_mb(done)} written"
        text += f"\nElapsed {elapsed:,.0f} s"
        dlg.setLabelText(text)
        if not th.is_alive():
            timer.stop()
            loop.quit()

    timer.timeout.connect(_poll)
    _poll()
    dlg.show()
    timer.start()
    if th.is_alive():
        loop.exec()
    timer.stop()
    dlg.close()
    dlg.deleteLater()
    return box["result"], box["exc"]
