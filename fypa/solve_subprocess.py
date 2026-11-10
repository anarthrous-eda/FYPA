"""Run the heavy FEM solve in a child process.

The GUI normally solves on a :class:`QThread` (``_SolveWorker``). That works,
but the solve holds native scipy/Triangle sections that don't yield the GIL and
can't be interrupted cooperatively, so cancelling mid-solve historically forced
a ``QThread.terminate()`` that could orphan the solver's module-level locks (see
the 1d/1e cancel-path bugs). Running the solve in a *separate process* removes
that hazard structurally: the solver's caches and locks live in the child, and
cancelling simply kills the child — nothing in the parent can be left locked.

This module is deliberately **PySide-free** so it can be imported and executed
in a spawned child without pulling Qt into that process. The parent
(``altium_viewer._SolveWorker``) calls :func:`run_solve_in_subprocess`, which
spawns the child, forwards its progress messages to the caller's callbacks, and
returns the lean ``(solution, metadata)`` — only the compact
:class:`~fypa.lean_solution.LeanSolution` crosses the process boundary, never the
heavy half-edge padne solution (the child runs ``to_lean_solution`` itself, the
same 80×-shrink the on-disk cache relies on).

Trade-off vs. the in-process path: a fresh child per solve does not inherit the
in-memory mesh/Laplacian/PARDISO caches, so a value-only re-solve can't reuse a
warm factorisation here. It is therefore opt-in — the parent enables it only
when ``FYPA_SOLVE_SUBPROCESS`` is set — and the in-process QThread path remains
the default.

Crossing the process boundary is kept cheap in both directions:

* **In** — pickling a big board's ``LoadedProject`` into the child and
  unpickling it there costs many seconds. When the design the solve started
  from is the one in the on-disk design-info cache, the child reads that file
  instead and the parent sends only what the edits changed (see
  :func:`_slim_job`). Otherwise the whole ``LoadedProject`` is pickled across,
  as before.
* **Out** — the result is written to a temporary file and read back by the
  parent rather than pushed through the queue's pipe (see
  :func:`_send_result`).
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import logging
import multiprocessing as mp
import os
import pickle
import queue as _queue
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass
from collections.abc import Callable

log = logging.getLogger(__name__)


def subprocess_solve_enabled() -> bool:
    """True when the opt-in env flag ``FYPA_SOLVE_SUBPROCESS`` is set to a
    truthy value (``1``/``true``/``yes``/``on``). Off by default — the in-process
    QThread solve stays the default path until the flag is flipped."""
    return os.environ.get("FYPA_SOLVE_SUBPROCESS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class SolveJob:
    """Everything the child needs to run one solve. All fields are picklable:
    ``loaded`` is a LoadedProject (already pickled to the on-disk design cache),
    ``mesher_config`` and ``settings`` are dataclasses."""

    loaded: object
    mesher_config: object
    settings: object
    adaptive_regulator_gain: bool = False
    # ``(cache_path, fingerprint)`` of the per-net geometry cache for
    # ``loaded`` as it will be solved, or None to union the copper from the
    # primitives as before. The child reads the file itself - far cheaper
    # than pickling ~150 MB of shapes across the process boundary.
    per_net_geometry_key: tuple | None = None
    # False on a clean load: rebuild (and refresh the cache), don't read it.
    read_per_net_geometry_cache: bool = True
    # Set by _slim_job only: ``loaded.extracted`` has been left out and the
    # child rebuilds it from the design-info cache file ``design_ref`` names
    # (``(path, size, mtime_ns)``) plus ``extracted_delta``; the result must
    # hash to ``extracted_digest``.
    design_ref: tuple | None = None
    extracted_delta: dict | None = None
    extracted_digest: str | None = None


def load_per_net_geometry(loaded, key: tuple | None, *,
                          read_cache: bool = True):
    """Per-net copper for the solve from the geometry cache ``key`` names
    (``(cache_path, fingerprint)``), or ``None`` - in which case the solve
    unions it from the primitives, as it always could. Never raises: the
    cache is an optimisation, not something a solve may fail on."""
    if key is None:
        return None
    try:
        from fypa.cli import per_net_geometry_for_solve
        return per_net_geometry_for_solve(
            loaded.extracted, key[0], key[1], read_cache=read_cache)
    except Exception as e:  # noqa: BLE001
        log.warning("Per-net geometry cache unavailable (%s: %s); building "
                    "the copper from the primitives.", type(e).__name__, e)
        return None


class SolveSubprocessError(RuntimeError):
    """The child process failed (its traceback is the message) or died without
    returning a result."""


# --- sending the design in: cache file + delta ---------------------------------
#
# A LoadedProject read from (or just written to) the design-info cache is
# tagged with that file's identity (fypa.cli.tag_design_cache_ref). The GUI
# keeps that object untouched and solves an edited clone of it - copper names,
# editor directives, stackup overrides - whose ExtractedProject shares all but
# a handful of its primitives with the original. So instead of pickling the
# clone's ~175 MB extract across, the parent sends the file's identity and the
# few fields / elements that differ, and the child rebuilds the same extract
# from the file. A digest of what decides the solve's copper (every
# collection's length and net assignment, the net names, the stackup) is
# checked on the rebuilt extract; any mismatch, or a cache file that has
# changed since, sends the solve back through the plain full-pickle route.

# Marks a collection element-wise patch in an extracted delta.
_PATCH = "patch"
_REPLACE = "replace"
# A same-length collection with more than this fraction of its elements
# changed is sent whole.
_PATCH_MAX_FRACTION = 0.25


def design_cache_ref_is_current(ref) -> bool:
    """True when the design-info cache file ``ref`` (``(path, size,
    mtime_ns)``) names is still the file it was when tagged."""
    try:
        path, size, mtime_ns = ref
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return False
    return st.st_size == size and st.st_mtime_ns == mtime_ns


def _extracted_delta(base, edited) -> dict:
    """What turns ``base`` into ``edited`` (both ExtractedProject), keyed by
    field: ``("replace", value)``, or ``("patch", [(index, element), ...])``
    for a same-length tuple/list in which only a few elements differ.
    Unchanged fields - compared by identity, which is what the edit steps
    preserve - are left out."""
    delta: dict = {}
    for f in dataclasses.fields(edited):
        if not f.init:
            # dataclasses.replace cannot set it, so the child could not
            # reproduce a difference here.
            if getattr(edited, f.name) is not getattr(base, f.name):
                raise ValueError(f"non-init field {f.name!r} differs")
            continue
        old, new = getattr(base, f.name), getattr(edited, f.name)
        if old is new:
            continue
        if (type(old) is type(new) and isinstance(new, (tuple, list))
                and len(old) == len(new) and len(new) > 0):
            changed = [(i, y) for i, (x, y) in enumerate(zip(old, new))
                       if x is not y]
            if len(changed) <= _PATCH_MAX_FRACTION * len(new):
                delta[f.name] = (_PATCH, changed)
                continue
        delta[f.name] = (_REPLACE, new)
    return delta


def _apply_extracted_delta(base, delta: dict):
    """Inverse of :func:`_extracted_delta`: ``base`` with ``delta`` applied."""
    if not delta:
        return base
    changes = {}
    for name, (kind, payload) in delta.items():
        if kind == _PATCH:
            old = getattr(base, name)
            items = list(old)
            for i, element in payload:
                items[i] = element
            changes[name] = type(old)(items)
        else:
            changes[name] = payload
    return dataclasses.replace(base, **changes)


def _extracted_digest(extracted) -> str:
    """Hash of the state of an ExtractedProject that the edit steps can
    change and the solve depends on: each collection's length and its
    elements' net assignment, the net names, the stackup and the source
    paths. The primitives' own shapes are fixed by the cache file."""
    import numpy as np

    h = hashlib.sha1()
    for f in dataclasses.fields(extracted):
        value = getattr(extracted, f.name)
        if not isinstance(value, (tuple, list)):
            continue
        h.update(f"{f.name}:{len(value)}\x00".encode())
        if value and hasattr(value[0], "net_index"):
            h.update(np.fromiter(
                (getattr(it, "net_index", -2) for it in value),
                dtype=np.int64, count=len(value)).tobytes())
    h.update("\x00".join(
        str(getattr(n, "name", n)) for n in extracted.nets).encode("utf-8"))
    h.update(repr(extracted.stackup).encode("utf-8"))
    h.update(str(getattr(extracted, "prjpcb_path", "")).encode("utf-8"))
    h.update(str(getattr(extracted, "pcbdoc_path", "")).encode("utf-8"))
    return h.hexdigest()


def _slim_job(job: SolveJob, base_loaded) -> SolveJob:
    """``job`` with its ExtractedProject swapped for a reference to the
    design-info cache plus a delta - or ``job`` itself whenever that is not
    provably the same design (see the section comment above)."""
    try:
        ref = getattr(base_loaded, "_design_cache_ref", None)
        loaded = job.loaded
        if (ref is None or loaded is None
                or not design_cache_ref_is_current(ref)):
            return job
        delta = _extracted_delta(base_loaded.extracted, loaded.extracted)
        shell = copy.copy(loaded)
        shell.extracted = None
        return dataclasses.replace(
            job, loaded=shell, design_ref=tuple(ref), extracted_delta=delta,
            extracted_digest=_extracted_digest(loaded.extracted))
    except Exception as e:  # noqa: BLE001 - an optimisation; never fatal
        log.info("Sending the full design to the solve subprocess (%s: %s).",
                 type(e).__name__, e)
        return job


class _StaleDesign(Exception):
    """The design-info cache no longer yields the design the parent meant."""


def _materialise_loaded(job: SolveJob) -> None:
    """Child side of :func:`_slim_job`: rebuild ``job.loaded.extracted`` from
    the design-info cache. Raises :class:`_StaleDesign` if the file changed
    or the rebuilt extract is not the one the parent described."""
    if job.design_ref is None:
        return
    from fypa.altium.extract import paused_gc

    if not design_cache_ref_is_current(job.design_ref):
        raise _StaleDesign("design-info cache changed since the solve began")
    try:
        with open(job.design_ref[0], "rb") as f:
            header = pickle.load(f)
            if not (isinstance(header, dict) and "format" in header):
                raise _StaleDesign("design-info cache is in the old layout")
            with paused_gc():
                base = pickle.load(f)
        extracted = _apply_extracted_delta(
            base.extracted, job.extracted_delta or {})
    except _StaleDesign:
        raise
    except Exception as e:  # noqa: BLE001
        raise _StaleDesign(f"{type(e).__name__}: {e}") from e
    if _extracted_digest(extracted) != job.extracted_digest:
        raise _StaleDesign("rebuilt design does not match the one in memory")
    job.loaded.extracted = extracted


# --- sending the result back: a file, not the pipe ------------------------------

def _send_result(q: mp.Queue, lean, metadata, result_path: str | None) -> None:
    """Hand the finished ``(lean, metadata)`` to the parent.

    A big board's result is over a gigabyte. Through the queue it is pickled
    to one bytes object, pushed down a pipe and unpickled; written straight
    to ``result_path`` and read back it takes about half as long. Falls back
    to the queue if the file cannot be written (no ``result_path``, disk
    full, ...)."""
    if result_path is not None:
        try:
            from fypa.altium.extract import paused_gc
            with open(result_path, "wb") as f, paused_gc():
                pickle.dump((lean, metadata), f,
                            protocol=pickle.HIGHEST_PROTOCOL)
            q.put(("ok_file", result_path))
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Could not write the solve result to %s (%s: %s); "
                        "sending it through the queue.", result_path,
                        type(e).__name__, e)
            try:
                os.unlink(result_path)
            except OSError:
                pass
    q.put(("ok", lean, metadata))


def _read_result(result_path: str) -> tuple[object, object]:
    from fypa.altium.extract import paused_gc
    with open(result_path, "rb") as f, paused_gc():
        lean, metadata = pickle.load(f)
    return lean, metadata


def _child_entry(job: SolveJob, q: mp.Queue,
                 result_path: str | None = None) -> None:
    """Child-process entry point: run the solve and stream results back on ``q``.

    Messages are tuples: ``("stage", str)`` / ``("substage", str)`` for progress,
    then exactly one terminal message: ``("ok", lean_solution, metadata)``,
    ``("ok_file", path)`` (the same pair, pickled to ``path`` — see
    :func:`_send_result`), ``("stale_design", reason)`` (the design-info cache
    could not stand in for the design; the parent re-sends it whole) or
    ``("fail", traceback_str)``. Never raises across the process boundary — any
    exception is caught and reported as a ``fail`` message."""
    try:
        if job.design_ref is not None:
            q.put(("stage", "Reading the design…"))
            try:
                _materialise_loaded(job)
            except _StaleDesign as e:
                q.put(("stale_design", str(e)))
                return

        # These imports are PySide-free and safe to re-run in a spawned child.
        from fypa.altium.loader import build_solve_metadata, solve_problem_adaptive
        from fypa.lean_solution import to_lean_solution

        # The child is a fresh process: its pdnsolver / altium_geometry module
        # constants sit at their defaults, so the physics/mesh settings this
        # solve used must be re-applied here (the in-process path relies on the
        # GUI thread having called this before starting the worker).
        try:
            job.settings.apply_to_modules()
        except Exception:  # pragma: no cover - settings without the hook
            pass

        # This process solves once and exits, so keeping mesh workers warm
        # afterwards buys nothing — and it is actively harmful: the parent
        # may still hold its own warm pool, so persisting here doubles the
        # live worker processes and can exhaust memory on a large board.
        try:
            from pdnsolver import solver as _pdn_solver_mod
            _pdn_solver_mod._MESH_POOL_PERSIST = False
        except Exception:  # pragma: no cover - defensive
            pass

        # Forward pdnsolver's per-step INFO logs to the parent as substage
        # updates, mirroring _SolveWorker's _SubstageForwarder.
        class _QueueLogHandler(logging.Handler):
            def emit(self_h, record: logging.LogRecord) -> None:
                try:
                    q.put(("substage", record.getMessage()))
                except Exception:
                    pass

        handler = _QueueLogHandler(level=logging.INFO)
        loggers = [logging.getLogger("pdnsolver.solver"),
                   logging.getLogger("pdnsolver.mesh")]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
        per_net_layers = None
        if job.per_net_geometry_key is not None:
            q.put(("stage", "Loading copper geometry…"))
            per_net_layers = load_per_net_geometry(
                job.loaded, job.per_net_geometry_key,
                read_cache=job.read_per_net_geometry_cache)
        try:
            (padne_solution, problem, via_segment_records,
             stub_pieces_by_pair, per_net_layers,
             adaptive_info) = solve_problem_adaptive(
                job.loaded,
                job.mesher_config,
                adaptive_regulator_gain=job.adaptive_regulator_gain,
                stage_callback=lambda m: q.put(("stage", m)),
                thermal_config=job.settings.thermal_config(),
                per_net_layers=per_net_layers,
            )
        except Exception as exc:
            # Mesh failures must open the same stub + markers as the in-process
            # worker — not a raw fail traceback.
            from pdnsolver import mesh as _pdn_mesh
            if not isinstance(exc, _pdn_mesh.MeshingException):
                raise
            from fypa.altium.loader import package_mesh_failure
            q.put(("stage", "Meshing failed — opening design…"))
            stub, fail_md = package_mesh_failure(
                job.loaded, exc, job.mesher_config,
                settings=job.settings,
            )
            _send_result(q, stub, fail_md, result_path)
            return
        finally:
            for lg in loggers:
                lg.removeHandler(handler)

        q.put(("stage", "Packaging solution: building metadata…"))
        metadata = build_solve_metadata(
            job.loaded, problem,
            mesher_config=job.mesher_config,
            solver_info=padne_solution.solver_info,
            via_segment_records=via_segment_records,
            settings=job.settings,
            stub_pieces_by_pair=stub_pieces_by_pair,
            per_net_layers=per_net_layers,
            regulator_adaptive_gain=adaptive_info,
        )
        q.put(("stage", "Packaging solution: converting result…"))
        lean = to_lean_solution(padne_solution)
        # Lean result only — the heavy padne_solution never crosses back.
        _send_result(q, lean, metadata, result_path)
    except BaseException:  # noqa: BLE001 - report everything to the parent
        try:
            q.put(("fail", traceback.format_exc()))
        except Exception:
            pass


# Returned by _run_child when the child could not rebuild the design from the
# cache file and the solve must be re-run with the design pickled across.
_RESEND_FULL = object()


def run_solve_in_subprocess(
    job: SolveJob,
    on_stage: Callable[[str], None],
    on_substage: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    register_process: Callable[[object], None] | None = None,
    base_loaded: object | None = None,
) -> tuple[object, object] | None:
    """Run ``job`` in a spawned child, forwarding progress to the callbacks.

    Returns ``(lean_solution, metadata)`` on success (including a mesh-failure
    stub with ``metadata["mesh_failures"]``), or ``None`` if the caller
    asked to cancel (``is_cancelled()`` went True) — in which case the child is
    terminated. Raises :class:`SolveSubprocessError` if the child fails or dies
    without a result.

    ``base_loaded`` (optional) is the untouched LoadedProject that
    ``job.loaded`` was derived from. When it is the design held in the
    design-info cache, the child reads that file rather than receiving the
    whole design through a pipe (see :func:`_slim_job`).

    The polling loop is what makes a subprocess solve cooperatively cancellable:
    the parent QThread blocks here draining a queue, so a GUI cancel is noticed
    within ~100 ms and the child is killed — no ``QThread.terminate()``, so no
    orphaned module locks.
    """
    # Release our own warm mesh workers first: the child is about to spawn a
    # full pool of its own, and holding both at once is what makes a large
    # board run out of memory.
    try:
        from pdnsolver.solver import shutdown_idle_mesh_pool
        shutdown_idle_mesh_pool()
    except Exception:  # pragma: no cover - defensive
        pass

    wire_job = _slim_job(job, base_loaded) if base_loaded is not None else job
    result = _run_child(wire_job, on_stage, on_substage, is_cancelled,
                        register_process)
    if result is _RESEND_FULL:
        result = _run_child(job, on_stage, on_substage, is_cancelled,
                            register_process)
        if result is _RESEND_FULL:  # a full job never asks for this
            raise SolveSubprocessError(
                "solve subprocess could not read the design")
    return result


def _run_child(job, on_stage, on_substage, is_cancelled, register_process):
    """One child process for ``job``: the result pair, ``None`` on cancel, or
    :data:`_RESEND_FULL`."""
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    # Where the child leaves the result (see _send_result). A private
    # directory, removed below whatever happens to the child.
    result_dir: str | None = None
    result_path: str | None = None
    try:
        result_dir = tempfile.mkdtemp(prefix="fypa-solve-")
        result_path = os.path.join(result_dir, "result.pkl")
    except OSError:
        result_dir = result_path = None

    # NOT daemon: the solve itself spawns a ProcessPoolExecutor for meshing, and
    # daemonic processes may not have children. We terminate the child
    # explicitly (on cancel, on failure, and in the finally below), so it never
    # outlives its use despite not being a daemon.
    _t_start = time.monotonic()
    proc = ctx.Process(target=_child_entry, args=(job, q, result_path),
                       daemon=False)
    proc.start()
    log.info("Solve subprocess started in %.2fs (%s).",
             time.monotonic() - _t_start,
             "design read from its cache file" if job.design_ref is not None
             else "design sent whole")
    if register_process is not None:
        register_process(proc)

    def _kill() -> None:
        try:
            if proc.is_alive():
                proc.terminate()
        except Exception:
            pass
        try:
            proc.join(2.0)
        except Exception:
            pass

    try:
        while True:
            if is_cancelled():
                _kill()
                return None
            try:
                msg = q.get(timeout=0.1)
            except _queue.Empty:
                if not proc.is_alive():
                    # Child exited without a terminal message — drain anything
                    # still queued, else report the death.
                    try:
                        msg = q.get_nowait()
                    except _queue.Empty:
                        raise SolveSubprocessError(
                            f"solve subprocess exited (code "
                            f"{proc.exitcode}) without returning a result")
                else:
                    continue
            kind = msg[0]
            if kind == "stage":
                on_stage(msg[1])
            elif kind == "substage":
                on_substage(msg[1])
            elif kind == "ok":
                proc.join(5.0)
                return msg[1], msg[2]
            elif kind == "ok_file":
                _t_read = time.monotonic()
                try:
                    result = _read_result(msg[1])
                except Exception as e:
                    raise SolveSubprocessError(
                        f"could not read the solve result from {msg[1]}: "
                        f"{type(e).__name__}: {e}") from e
                log.info("Solve result read back in %.2fs.",
                         time.monotonic() - _t_read)
                proc.join(5.0)
                return result
            elif kind == "stale_design":
                log.info("Solve subprocess could not use the design-info "
                         "cache (%s); sending the design whole.", msg[1])
                proc.join(5.0)
                return _RESEND_FULL
            elif kind == "fail":
                proc.join(5.0)
                raise SolveSubprocessError(msg[1])
    finally:
        # Belt-and-braces: never leave the child running when we leave this
        # frame (normal return already joined; cancel/exception may not have).
        if proc.is_alive():
            _kill()
        if result_dir is not None:
            shutil.rmtree(result_dir, ignore_errors=True)
