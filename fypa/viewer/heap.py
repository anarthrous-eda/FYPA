"""Keep CPython's cyclic garbage collector off the solved design's objects.

A solved large board holds ~10^7 tracked Python objects (Mangala: ~7.8 M
small coordinate lists in the metadata's copper outlines, ~0.6 M memoryviews
and ~0.5 M dicts). Every *full* (generation-2) collection walks all of them —
~2 s each on Mangala — and CPython triggers one whenever enough new
survivors pile up since the last, which after a solve or load is "on the
next thing the user does". That showed up as a multi-second freeze right
after the first rail toggle following a solve, and never again after.

:func:`settle_heap` pays that walk once, at a moment the user is already
waiting (end of a load / solve), and then moves every surviving object into
the permanent generation with :func:`gc.freeze` so later collections only
scan objects created afterwards.

Frozen objects are still freed by reference counting — dropping a viewer's
``solution`` / ``metadata`` (see ``_retire_viewer``) releases them as before.
Only *cyclic* garbage among them waits: each call starts with
:func:`gc.unfreeze` so the previous load's leftovers are collected then.
"""
from __future__ import annotations

import gc
import logging
import time

_log = logging.getLogger(__name__)


def settle_heap(reason: str = "") -> float:
    """Collect once, then freeze every surviving object. Returns seconds taken."""
    t0 = time.perf_counter()
    gc.unfreeze()
    gc.collect()
    gc.freeze()
    dt = time.perf_counter() - t0
    _log.info(
        "GC settle%s: froze %d object(s) in %.2fs",
        f" ({reason})" if reason else "", gc.get_freeze_count(), dt,
    )
    return dt
