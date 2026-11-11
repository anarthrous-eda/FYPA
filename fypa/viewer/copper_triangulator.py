"""Background triangulation of a board's copper for the all-copper view.

Filling a large board's copper means triangulating every polygon — minutes of
single-threaded work on a 16-layer design. :class:`CopperTriangulator` does it
in worker processes after the viewer opens, so the window stays responsive and
a layer that has been prepared shows the moment its eye is clicked. A layer
the user asks for early jumps the queue.
"""
from __future__ import annotations

import concurrent.futures
import logging
import multiprocessing
import os
from collections import deque

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal

from fypa.viewer import copper_tris as _ct

log = logging.getLogger(__name__)

# A chunk is one task handed to a worker. Small enough that a layer the user
# just asked for is not stuck behind a long task, large enough that pickling
# the rings out and the triangles back is noise next to the triangulation.
_CHUNK_MAX_POLYGONS = 1500
_CHUNK_MAX_COORDS = 60_000


def default_workers() -> int:
    """Worker processes to use: most of the machine, leaving two cores for
    the GUI and whatever else is running (a solve meshes in its own pool)."""
    try:
        forced = int(os.environ.get("FYPA_COPPER_WORKERS", "0"))
    except ValueError:
        forced = 0
    if forced > 0:
        return forced
    return max(1, min(8, (os.cpu_count() or 4) - 2))


def _worker_init() -> None:
    """Run the workers below normal priority: this is housekeeping, and it
    must not make the viewer or a solve feel slower."""
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)
        else:
            os.nice(5)
    except Exception:
        pass


def _chunks(polys: list[dict]) -> deque:
    out: deque = deque()
    cur: list[dict] = []
    weight = 0
    for poly in polys:
        ext = poly.get("exterior")
        w = len(ext) if ext is not None else 0
        for hole in poly.get("holes") or ():
            w += len(hole)
        if cur and (len(cur) >= _CHUNK_MAX_POLYGONS
                    or weight + w > _CHUNK_MAX_COORDS):
            out.append(cur)
            cur, weight = [], 0
        cur.append(poly)
        weight += w
    if cur:
        out.append(cur)
    return out


class _Job:
    __slots__ = ("chunks", "outstanding")

    def __init__(self, chunks: deque) -> None:
        self.chunks = chunks            # not yet handed to a worker
        self.outstanding = len(chunks)  # not yet finished


class CopperTriangulator(QObject):
    """Triangulates all-copper polygons in worker processes.

    Jobs are keyed ``(layer_id, kind)`` with ``kind`` a fill style from
    :mod:`fypa.viewer.copper_tris`. Results land on the polygon dicts under
    the same cache keys the lazy in-process path uses, on the GUI thread, so
    the rest of the viewer cannot tell which path produced them.
    """

    # (layer_id, kind): every polygon of that job now has its triangles.
    layer_ready = Signal(int, str)
    # The pool died (a worker crashed or could not start). Callers fall back
    # to triangulating in-process.
    failed = Signal(str)

    _chunk_done = Signal(object, object, object)

    def __init__(self, parent: QObject | None = None,
                 max_workers: int | None = None) -> None:
        super().__init__(parent)
        self._max_workers = max_workers or default_workers()
        self._jobs: dict[tuple[int, str], _Job] = {}
        self._order: list[tuple[int, str]] = []
        self._pool: concurrent.futures.ProcessPoolExecutor | None = None
        self._inflight = 0
        self.active = True
        self._chunk_done.connect(self._on_chunk_done, Qt.QueuedConnection)

    # --- queueing -----------------------------------------------------------

    def add(self, layer_id: int, kind: str, polys: list[dict], *,
            front: bool = False) -> None:
        """Queue the polygons of ``polys`` that still lack ``kind`` triangles.
        ``front`` puts the job ahead of everything already waiting."""
        if not self.active:
            return
        key = (int(layer_id), kind)
        if key in self._jobs:
            if front:
                self.prioritise(layer_id, kind)
            return
        cache_key = _ct.CACHE_KEY[kind]
        todo = []
        for poly in polys:
            if poly.get(cache_key) is not None:
                continue
            prebuilt = poly.get("triangles_xy") if kind == _ct.SOLID else None
            if prebuilt is not None:
                poly[cache_key] = np.asarray(prebuilt, dtype=np.float32)
                continue
            todo.append(poly)
        if not todo:
            return
        self._jobs[key] = _Job(_chunks(todo))
        if front:
            self._order.insert(0, key)
        else:
            self._order.append(key)
        self._pump()

    def prioritise(self, layer_id: int, kind: str) -> None:
        key = (int(layer_id), kind)
        if key in self._jobs and self._order and self._order[0] != key:
            self._order.remove(key)
            self._order.insert(0, key)

    def is_pending(self, layer_id: int, kind: str) -> bool:
        return (int(layer_id), kind) in self._jobs

    def busy(self) -> bool:
        return bool(self._jobs)

    # --- running ------------------------------------------------------------

    def _pump(self) -> None:
        if not self.active:
            return
        limit = self._max_workers + 1
        while self._inflight < limit:
            key = next((k for k in self._order if self._jobs[k].chunks), None)
            if key is None:
                return
            chunk = self._jobs[key].chunks.popleft()
            rings = [(p.get("exterior"), p.get("holes") or []) for p in chunk]
            try:
                if self._pool is None:
                    self._pool = concurrent.futures.ProcessPoolExecutor(
                        max_workers=self._max_workers,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=_worker_init)
                fut = self._pool.submit(_ct.build_chunk, key[1], rings)
            except Exception as e:  # noqa: BLE001 - any pool failure
                self._fail(e)
                return
            self._inflight += 1
            fut.add_done_callback(
                lambda f, k=key, c=chunk: self._emit_done(k, c, f))

    def _emit_done(self, key, chunk, fut) -> None:
        # Runs on a pool thread; the queued signal hops to the GUI thread.
        try:
            self._chunk_done.emit(key, chunk, fut)
        except RuntimeError:
            pass  # the triangulator was destroyed with its window

    def _on_chunk_done(self, key, chunk, fut) -> None:
        self._inflight = max(0, self._inflight - 1)
        if not self.active or fut.cancelled():
            return
        try:
            xy, counts = fut.result()
        except Exception as e:  # noqa: BLE001 - worker crash / broken pool
            self._fail(e)
            return
        job = self._jobs.get(key)
        cache_key = _ct.CACHE_KEY[key[1]]
        ends = np.cumsum(counts)
        start = 0
        for poly, end in zip(chunk, ends.tolist()):
            if poly.get(cache_key) is None:
                poly[cache_key] = xy[start:end]
            start = end
        if job is not None:
            job.outstanding -= 1
            if job.outstanding <= 0:
                del self._jobs[key]
                self._order.remove(key)
                self.layer_ready.emit(key[0], key[1])
        if self._jobs:
            self._pump()
        else:
            self._release_pool()

    def _release_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _fail(self, exc: BaseException) -> None:
        log.warning("Background copper triangulation stopped (%s: %s); "
                    "layers will be prepared on demand instead.",
                    type(exc).__name__, exc)
        self.active = False
        self._jobs.clear()
        self._order.clear()
        self._release_pool()
        self.failed.emit(f"{type(exc).__name__}: {exc}")

    def shutdown(self) -> None:
        """Stop for good: drop queued work and let the workers exit."""
        self.active = False
        self._jobs.clear()
        self._order.clear()
        self._release_pool()
