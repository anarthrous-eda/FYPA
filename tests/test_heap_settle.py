"""settle_heap: one collection now, then later collections skip the loaded data."""
from __future__ import annotations

import gc

from fypa.viewer.heap import settle_heap


class _Cycle:
    def __init__(self):
        self.me = self


def test_settle_freezes_survivors_and_collects_cycles_from_before():
    try:
        data = [[float(i), float(i)] for i in range(10_000)]
        settle_heap("test")
        assert gc.get_freeze_count() >= len(data)
        # Cyclic garbage created after a settle (and frozen by a second one
        # while still alive) is collected by the next settle's unfreeze.
        import weakref
        c = _Cycle()
        ref = weakref.ref(c)
        settle_heap("test")
        del c
        assert ref() is not None  # frozen: an ordinary collect skips it
        gc.collect()
        assert ref() is not None
        settle_heap("test")
        assert ref() is None
    finally:
        gc.unfreeze()
