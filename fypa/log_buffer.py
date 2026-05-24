"""In-memory log-record buffer used to back the viewer's Messages tab.

A single root-logger handler captures every log record emitted by the
process, stashes a compact :class:`MessageRecord` in a bounded deque,
and notifies any registered listener (typically a Qt object that
re-emits on its own signal so the GUI thread sees it). The buffer is
process-wide and survives across viewer windows so opening a fresh
viewer still shows messages logged before it was created.

Installed from :func:`fypa.cli._setup_logging`; safe to call multiple
times (idempotent).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass


# Cap so a long-running session can't grow the buffer without bound.
# Old records fall off the left end as new ones arrive.
_BUFFER_MAXLEN: int = 5000


@dataclass(frozen=True)
class MessageRecord:
    """One captured log entry — the minimum the Messages tab needs."""

    ts: float        # Unix epoch seconds (wall-clock, fractional)
    level: int       # logging.* level number
    level_name: str  # e.g. "WARNING"
    name: str        # logger name, e.g. "fypa.altium_viewer"
    message: str     # fully-formatted message text


_buffer: deque[MessageRecord] = deque(maxlen=_BUFFER_MAXLEN)
_lock = threading.Lock()
_listeners: list[Callable[[MessageRecord], None]] = []
_installed: bool = False


class _BufferHandler(logging.Handler):
    """Routes every formatted log record into the in-memory deque and
    fans out to any registered listener. Defensive: a misbehaving
    listener can't take the logging system down."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            # Mirror the stdlib's "never crash logging" contract.
            self.handleError(record)
            return
        entry = MessageRecord(
            ts=record.created,
            level=record.levelno,
            level_name=record.levelname,
            name=record.name,
            message=msg,
        )
        with _lock:
            _buffer.append(entry)
            listeners = tuple(_listeners)
        for fn in listeners:
            try:
                fn(entry)
            except Exception:
                # A listener crash mustn't break logging for everything
                # else. Log it via the stdlib mechanism so we don't
                # recurse through ourselves.
                logging.getLogger(__name__).debug(
                    "Message-buffer listener raised", exc_info=True,
                )


def install() -> None:
    """Attach the buffer handler to the root logger if not already.
    Idempotent — called from :func:`fypa.cli._setup_logging` (and safe
    to call directly from the viewer module as a belt-and-braces fallback
    for code paths that bypass ``_setup_logging``)."""
    global _installed
    if _installed:
        return
    handler = _BufferHandler(level=logging.DEBUG)
    # Match the file-log formatter so the Messages tab and fypa.log
    # show the same message text. The timestamp/source columns come
    # from the LogRecord fields directly, so no asctime in the format.
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    _installed = True


def records() -> list[MessageRecord]:
    """Snapshot of every buffered record, oldest first."""
    with _lock:
        return list(_buffer)


def add_listener(fn: Callable[[MessageRecord], None]) -> None:
    """Register a callback for newly emitted records. Listeners may be
    invoked from arbitrary threads (whichever thread called
    ``logging.<level>``), so a Qt listener should marshal to the GUI
    thread (e.g. by emitting a queued signal)."""
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_listener(fn: Callable[[MessageRecord], None]) -> None:
    with _lock:
        try:
            _listeners.remove(fn)
        except ValueError:
            pass


def clear() -> None:
    """Drop every buffered record. Does not affect the file log."""
    with _lock:
        _buffer.clear()


def format_timestamp(ts: float) -> str:
    """``YYYY-MM-DD HH:MM:SS.mmm`` in local time — the format the
    Messages-tab Time column shows. Centralised so the table cells and
    any export path agree. Full date included so sessions that span
    midnight (or that pull in records from a prior process via the
    file log) stay unambiguous."""
    lt = time.localtime(ts)
    ms = int((ts - int(ts)) * 1000)
    return (
        f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d} "
        f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms:03d}"
    )


def _suppressed_in_tests() -> Iterable[str]:
    """Reserved — kept for future use if tests want to silence noisy
    loggers without dropping them from the file log."""
    return ()
