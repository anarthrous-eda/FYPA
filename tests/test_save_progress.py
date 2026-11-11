"""Project save runs off the GUI thread with progress (fypa.viewer.save_progress)."""
from __future__ import annotations

import pickle
import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from fypa.cli import save_solution_file
from fypa.viewer.save_progress import (
    SaveStatus,
    copy_with_progress,
    run_save_with_progress,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_copy_with_progress_counts_bytes(tmp_path):
    src = tmp_path / "a.pkl"
    data = b"x" * (20 * 1024 * 1024 + 7)
    src.write_bytes(data)
    dst = tmp_path / "b.pkl"
    st = SaveStatus()
    copy_with_progress(src, dst, st, "Copying")
    assert dst.read_bytes() == data
    assert st.done_bytes == st.total_bytes == len(data)
    assert not (tmp_path / "b.pkl.tmp").exists()


def test_save_solution_file_reports_and_is_atomic(tmp_path):
    out = tmp_path / "s.pkl"
    st = SaveStatus()
    save_solution_file(out, {"v": list(range(1000))}, {"k": 1}, status=st)
    assert st.done_bytes == out.stat().st_size
    assert pickle.loads(out.read_bytes())["solution"]["v"][-1] == 999

    class Boom:
        def __reduce__(self):
            raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        save_solution_file(out, Boom(), None)
    # Previous good file survives; no temp left behind.
    assert pickle.loads(out.read_bytes())["metadata"] == {"k": 1}
    assert not (tmp_path / "s.pkl.tmp").exists()


def test_run_save_with_progress_runs_off_gui_thread(qapp):
    gui = threading.get_ident()
    seen = {}
    gate = threading.Event()

    def work(status):
        seen["thread"] = threading.get_ident()
        status.begin("Working", 10)
        gate.wait(1.0)   # long enough to open the dialog + spin the loop
        status.add(10)
        return 42

    threading.Timer(0.5, gate.set).start()
    result, exc = run_save_with_progress(None, "t", work)
    assert exc is None and result == 42
    assert seen["thread"] != gui


def test_run_save_with_progress_returns_exception(qapp):
    def work(status):
        raise ValueError("bad")

    result, exc = run_save_with_progress(None, "t", work)
    assert result is None and isinstance(exc, ValueError)
