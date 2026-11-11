"""The Messages tab adds log records in batches, not one re-sort per record.

Each record used to re-sort the whole table and recount every row on the GUI
thread (~16 ms on a few thousand rows), which held the GIL against the solve
worker: a Resolve's ~270 annotation warnings took 4 s to log. Records are now
queued and added together by a short timer.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V
    from fypa import log_buffer
    log_buffer.clear()
    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.tabs = None
    v._test_tab = V.PdnViewer._build_messages_tab(v)
    yield v
    log_buffer.remove_listener(v._messages_listener)


def _record(i, level=30):
    import logging
    import time
    from fypa import log_buffer
    return log_buffer.MessageRecord(
        ts=time.time(), level=level, level_name=logging.getLevelName(level),
        name="t", message=f"message {i}")


def _rows(v):
    return v.messages_table.rowCount()


def test_records_are_queued_not_added_one_by_one(viewer):
    import fypa.altium_viewer as V
    for i in range(50):
        V.PdnViewer._on_messages_record(viewer, _record(i))
    assert _rows(viewer) == 0
    assert viewer._messages_flush_timer.isActive()
    V.PdnViewer._flush_messages(viewer)
    assert _rows(viewer) == 50


def test_a_flush_respects_the_level_filter(viewer):
    import fypa.altium_viewer as V
    allowed = V.PdnViewer._messages_allowed_levels(viewer)
    hidden = next(lv for lv in (10, 20, 30, 40, 50) if lv not in allowed) \
        if len(allowed) < 5 else None
    V.PdnViewer._on_messages_record(viewer, _record(1, 40))
    if hidden is not None:
        V.PdnViewer._on_messages_record(viewer, _record(2, hidden))
    V.PdnViewer._flush_messages(viewer)
    assert _rows(viewer) == 1


def test_repopulating_drops_the_queue_so_nothing_is_duplicated(viewer, qapp):
    """The buffer already holds queued records; a full repopulate (filter
    change, Clear) shows them, so the later flush must not add them again."""
    import logging
    import fypa.altium_viewer as V
    from fypa import log_buffer
    log_buffer.install()
    logging.getLogger("fypa.test").warning("queued then repopulated")
    qapp.processEvents()                     # deliver the queued signal
    assert viewer._messages_pending          # arrived through the listener
    V.PdnViewer._populate_messages_table(viewer)
    shown = _rows(viewer)
    V.PdnViewer._flush_messages(viewer)
    assert _rows(viewer) == shown


def test_log_buffer_count_does_not_copy():
    from fypa import log_buffer
    log_buffer.install()
    log_buffer.clear()
    import logging
    logging.getLogger("fypa.test").warning("one")
    assert log_buffer.count() == len(log_buffer.records()) >= 1
