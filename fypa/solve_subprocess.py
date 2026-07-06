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
warm factorisation here. It also pickles the (post-override) ``LoadedProject``
into the child. It is therefore opt-in — the parent enables it only when
``FYPA_SOLVE_SUBPROCESS`` is set — and the in-process QThread path remains the
default.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue as _queue
import traceback
from dataclasses import dataclass
from typing import Callable

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


class SolveSubprocessError(RuntimeError):
    """The child process failed (its traceback is the message) or died without
    returning a result."""


def _child_entry(job: SolveJob, q: "mp.Queue") -> None:
    """Child-process entry point: run the solve and stream results back on ``q``.

    Messages are tuples: ``("stage", str)`` / ``("substage", str)`` for progress,
    then exactly one terminal ``("ok", lean_solution, metadata)`` or
    ``("fail", traceback_str)``. Never raises across the process boundary — any
    exception is caught and reported as a ``fail`` message."""
    try:
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
        try:
            (padne_solution, problem, via_segment_records,
             stub_pieces_by_pair, per_net_layers,
             adaptive_info) = solve_problem_adaptive(
                job.loaded,
                job.mesher_config,
                adaptive_regulator_gain=job.adaptive_regulator_gain,
                stage_callback=lambda m: q.put(("stage", m)),
            )
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
        q.put(("ok", lean, metadata))
    except BaseException:  # noqa: BLE001 - report everything to the parent
        try:
            q.put(("fail", traceback.format_exc()))
        except Exception:
            pass


def run_solve_in_subprocess(
    job: SolveJob,
    on_stage: Callable[[str], None],
    on_substage: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    register_process: Callable[[object], None] | None = None,
) -> "tuple[object, object] | None":
    """Run ``job`` in a spawned child, forwarding progress to the callbacks.

    Returns ``(lean_solution, metadata)`` on success, or ``None`` if the caller
    asked to cancel (``is_cancelled()`` went True) — in which case the child is
    terminated. Raises :class:`SolveSubprocessError` if the child fails or dies
    without a result.

    The polling loop is what makes a subprocess solve cooperatively cancellable:
    the parent QThread blocks here draining a queue, so a GUI cancel is noticed
    within ~100 ms and the child is killed — no ``QThread.terminate()``, so no
    orphaned module locks.
    """
    ctx = mp.get_context("spawn")
    q: "mp.Queue" = ctx.Queue()
    # NOT daemon: the solve itself spawns a ProcessPoolExecutor for meshing, and
    # daemonic processes may not have children. We terminate the child
    # explicitly (on cancel, on failure, and in the finally below), so it never
    # outlives its use despite not being a daemon.
    proc = ctx.Process(target=_child_entry, args=(job, q), daemon=False)
    proc.start()
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
            elif kind == "fail":
                proc.join(5.0)
                raise SolveSubprocessError(msg[1])
    finally:
        # Belt-and-braces: never leave the child running when we leave this
        # frame (normal return already joined; cancel/exception may not have).
        if proc.is_alive():
            _kill()
