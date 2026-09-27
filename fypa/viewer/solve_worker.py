"""Background solve and capacitor-loop workers, and the solve cache."""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QWidget,
)

from fypa.viewer.prefs import load_solve_in_subprocess
from fypa.viewer.session import _retire_thread


class _SolveProgressUpdater(QObject):
    """Wires a :class:`_SolveWorker`'s ``stage_changed`` / ``substage_changed``
    signals to a :class:`QProgressDialog`, with a live elapsed-time
    counter for the current stage so the user can see that long opaque
    steps (e.g. the ~20 s "Meshing + solving") are still making progress.

    The dialog's label text is rendered as up to two lines:

      | Meshing + solving (21 (layer, net) slabs, 67 networks)…  (12s)
      | Currently: Constructing the Laplace operators

    A second, independent counter — the total wall-clock time since the
    load started — ticks in the dialog's bottom-left corner ("Elapsed:
    Ns"). Unlike the per-stage counter it never resets between stages.

    Both counters tick once a second via a QTimer parented to ``self``
    (so it dies when the updater is deleted). Call :meth:`stop` from the
    worker's cleanup path to stop ticking before the dialog closes.

    The dialog starts as an indeterminate barber-pole (the caller builds
    it with range 0–0) and is upgraded to a determinate bar as soon as
    the worker emits ``total_stages``. Each subsequent ``stage_changed``
    ticks the bar one step; if the worker overruns its forecast, the
    maximum is extended so progress is always monotonic.
    """

    def __init__(self, dlg: QProgressDialog, worker: _SolveWorker,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dlg = dlg
        self._stage_text: str = ""
        self._substage_text: str = ""
        self._stage_start: float = 0.0
        # Determinate-bar bookkeeping: a stage counter that ticks once per
        # stage_changed and a maximum that's set when the worker emits
        # total_stages. Until then the dialog stays in barber-pole mode
        # (range 0–0); the swap to a determinate bar happens the moment
        # we hear back from the worker — typically within milliseconds of
        # start().
        self._stage_count: int = 0
        self._stage_max: int = 0
        # Wall-clock start of the whole load, for the bottom-left total
        # counter. Set now (the dialog is already shown) so it counts
        # from the moment the dialog appears, not from the first stage.
        self._total_start: float = time.monotonic()
        self._elapsed_label: QLabel = self._make_elapsed_label(dlg)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._render)
        # Tick immediately — the total counter must run even before the
        # first stage_changed arrives (e.g. during the solve-cache probe).
        self._timer.start()
        worker.stage_changed.connect(self._on_stage)
        worker.substage_changed.connect(self._on_substage)
        # Both _SolveWorker and _GerberImportWorker expose total_stages;
        # duck-typed so a future worker that doesn't can still be used
        # with this updater (the dialog just stays indeterminate).
        if hasattr(worker, "total_stages"):
            worker.total_stages.connect(self._on_total_stages)
        self._render()

    def _on_total_stages(self, n: int) -> None:
        if n <= 0:
            return
        self._stage_max = n
        # setRange(0, N) flips QProgressDialog out of barber-pole mode
        # into a determinate bar. Reflect whatever stages have already
        # fired (rare in practice — total_stages is emitted at the top
        # of run(), before any stage_changed) so the bar starts at the
        # right position rather than snapping back to 0.
        self._dlg.setRange(0, n)
        self._dlg.setValue(min(self._stage_count, n))

    @staticmethod
    def _make_elapsed_label(dlg: QProgressDialog) -> QLabel:
        """Build the total-elapsed label pinned to the dialog's
        bottom-left, level with the Cancel button.

        The dialog is fixed-size by the time this runs, and the Cancel
        button's vertical position depends only on the dialog height
        (unchanged by the width-only stretch in the caller) — so a
        one-shot move() holds for the dialog's whole lifetime.
        """
        label = QLabel(dlg)
        label.setObjectName("_elapsed_timer_label")
        # Size for the widest text we'll ever show: the label isn't
        # layout-managed, so a later setText() won't grow it and a
        # too-narrow label would clip the seconds count.
        label.setText("Elapsed: 0000s")
        label.adjustSize()
        label.setText("Elapsed: 0s")
        cancel_btn = dlg.findChild(QPushButton)
        if cancel_btn is not None and cancel_btn.height() > 0:
            y = cancel_btn.y() + (cancel_btn.height() - label.height()) // 2
        else:
            y = dlg.height() - label.height() - 9
        label.move(11, y)
        label.show()
        label.raise_()
        return label

    def _on_stage(self, text: str) -> None:
        self._stage_text = text
        self._substage_text = ""
        self._stage_start = time.monotonic()
        self._stage_count += 1
        if self._stage_max > 0:
            # If the worker emits more stages than _expected_stage_count
            # forecast (e.g. an unexpected fallback branch), extend the
            # maximum on the fly. We never shrink it, so visual progress
            # only ever moves forward.
            if self._stage_count > self._stage_max:
                self._stage_max = self._stage_count
                self._dlg.setMaximum(self._stage_max)
            self._dlg.setValue(self._stage_count)
        self._render()

    def _on_substage(self, text: str) -> None:
        self._substage_text = text
        self._render()

    def _render(self) -> None:
        if self._dlg is None:
            return
        # Total wall-clock counter (bottom-left) — updated every tick,
        # independent of which stage is currently running.
        total = int(time.monotonic() - self._total_start)
        self._elapsed_label.setText(f"Elapsed: {total}s")
        if not self._stage_text:
            return
        elapsed = int(time.monotonic() - self._stage_start)
        first = f"{self._stage_text}  ({elapsed}s)" if elapsed > 0 \
            else self._stage_text
        if self._substage_text:
            self._dlg.setLabelText(f"{first}\nCurrently: {self._substage_text}")
        else:
            self._dlg.setLabelText(first)

    def stop(self) -> None:
        """Stop the elapsed-time timer. Safe to call multiple times."""
        if self._timer.isActive():
            self._timer.stop()




class _StageTimer:
    """Accumulates wall-clock durations of named load-pipeline stages and
    logs a ranked breakdown when the load finishes.

    The whole-load counterpart of pdnsolver.solver's per-stage timing: it
    lets a clean load self-report where its time went (extract, geometry,
    solve, packaging, cache) so the slow stages are obvious without scraping
    timestamps out of the log by hand.
    """

    def __init__(self, log_: logging.Logger) -> None:
        self._log = log_
        self._stages: list[tuple[str, float]] = []
        self._t0 = time.monotonic()

    @contextlib.contextmanager
    def stage(self, label: str):
        """Time a ``with``-wrapped pipeline stage and record its duration."""
        t = time.monotonic()
        try:
            yield
        finally:
            dt = time.monotonic() - t
            self._stages.append((label, dt))
            self._log.info("Stage '%s' done in %.2fs", label, dt)

    def log_breakdown(self) -> None:
        """Log every recorded stage, slowest first, with its share of the
        total wall-clock time since this timer was created. An
        ``(other / untimed)`` row catches whatever ran outside a stage."""
        total = time.monotonic() - self._t0
        self._log.info("=== Load timing breakdown (slowest stage first) ===")
        accounted = 0.0
        for label, dt in sorted(self._stages, key=lambda kv: kv[1], reverse=True):
            accounted += dt
            pct = 100.0 * dt / total if total > 0 else 0.0
            self._log.info("  %8.2fs  %5.1f%%  %s", dt, pct, label)
        other = total - accounted
        self._log.info("  %8.2fs  %5.1f%%  (other / untimed)", other,
                       100.0 * other / total if total > 0 else 0.0)
        self._log.info("  %8.2fs  100.0%%  TOTAL", total)




class _CapLoopWorker(QThread):
    """Background worker for the Tier-2 / Tier-3 capacitor loop-inductance
    solve.

    Each cavity is a single-layer FEM domain and every solve after the first
    reuses the cached mesh + Laplacian, so this is far cheaper than a board
    solve — but a rail with dozens of caps still runs for seconds, which is
    long enough to freeze the GUI. Kept in-process (unlike
    :mod:`fypa.solve_subprocess`) because the cavity problems are small and
    the extracted design is already in this process's memory.
    """

    progress = Signal(str)
    # (results: dict[designator, Tier2Result], matrices: list[CavityMatrix],
    #  tier3: dict[designator, Tier3Result | None])
    finished_ok = Signal(object, object, object)
    failed = Signal(str)

    def __init__(self, extracted, caps, net_layer_shapes, rail_to_members,
                 settings, mesher_config=None, parent=None) -> None:
        super().__init__(parent)
        self._extracted = extracted
        self._caps = caps
        self._shapes = net_layer_shapes
        self._rail_to_members = rail_to_members
        self._settings = settings
        self._mesher_config = mesher_config
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            from fypa.altium.loader import _layer_z_centers_mm
            from fypa.caploop.tier1 import mounted_inductance
            from fypa.caploop.tier2_fem import run_tier2
            from fypa.caploop.tier3 import build_ic_geometry, total_loop

            results, matrices = run_tier2(
                self._extracted, self._caps, self._shapes,
                self._rail_to_members, self._settings,
                mesher_config=self._mesher_config,
                progress_cb=self.progress.emit,
                cancel_event=self._cancel,
            )

            self.progress.emit("Assembling cap→plane→IC loop totals…")
            enabled = self._extracted.enabled_copper_layer_ids()
            z_centers = _layer_z_centers_mm(self._extracted, enabled)
            net_index = {n.name: i
                         for i, n in enumerate(self._extracted.nets)}

            tier3: dict[str, object] = {}
            for cap in self._caps:
                res = results.get(cap.designator)
                if res is None or res.spread_h is None:
                    tier3[cap.designator] = None
                    continue
                rail_members = self._rail_to_members.get(
                    cap.rail_group, [cap.rail_group])
                rail_idx = {net_index[m] for m in rail_members
                            if m in net_index}
                return_idx = {net_index[cap.return_net]} \
                    if cap.return_net in net_index else set()
                ic = build_ic_geometry(
                    self._extracted, cap, rail_idx, return_idx, enabled,
                    z_centers, self._settings)
                tier3[cap.designator] = total_loop(
                    cap, mounted_inductance(cap, self._settings),
                    res.spread_h, ic, self._settings)
            self.finished_ok.emit(results, matrices, tier3)
        except Exception as e:  # noqa: BLE001 — surfaced to the user
            if self._cancel.is_set():
                self.failed.emit("Cancelled.")
            else:
                logging.getLogger(__name__).exception(
                    "Capacitor loop-inductance solve failed")
                self.failed.emit(f"{type(e).__name__}: {e}")




class _SolveWorker(QThread):
    """Background worker that re-runs the FEM solve off the GUI thread.

    The solve takes 10–60 s on typical boards; running it on the main
    thread freezes the UI (Windows shows "Not Responding") and the
    Settings-tab status label can't update mid-solve. Punting it to a
    QThread lets the progress dialog spin and stage messages stream in
    via :attr:`stage_changed`.

    Note: the worker does NOT call ``settings.apply_to_modules()`` itself;
    the caller does that on the main thread before ``start()`` so the
    module-level monkey-patch ordering is unambiguous.
    """

    stage_changed = Signal(str)           # "Building geometry…" etc.
    substage_changed = Signal(str)        # finer-grained detail line shown
                                          # under the main stage label (e.g.
                                          # pdnsolver's per-step log records
                                          # during meshing + solving)
    total_stages = Signal(int)            # emitted once at run() start so the
                                          # QProgressDialog can switch from
                                          # indeterminate barber-pole to a
                                          # determinate bar that ticks per
                                          # stage_changed.
    finished_ok = Signal(object, object, object)  # (LeanSolution, metadata,
                                          # pristine LoadedProject | None)
    failed = Signal(str)                  # error message for the UI

    def __init__(self, prjpcb_path: Path, settings,
                  sink_overrides: dict[tuple[str, str, int | None], float] | None = None,
                  stackup_overrides: dict[int, float] | None = None,
                  pcbdoc_selector: str | Path | None = None,
                  use_design_cache: bool = True,
                  try_solve_cache_first: bool = False,
                  editor_directives: list | None = None,
                  copper_names: list | None = None,
                  loaded_project: object | None = None,
                  load_only: bool = False,
                  adaptive_regulator_gain: bool = False,
                  no_auto_bridge: set[str] | None = None,
                  parent=None) -> None:
        super().__init__(parent)
        self._prjpcb_path = prjpcb_path
        self._settings = settings
        # Designators the user has told the tool not to short automatically
        # (Bridges tab). Has to reach load_project, not the editor-directive
        # path: the auto-bridge and the net merge it triggers both happen
        # while annotations are parsed, before editor directives exist.
        self._no_auto_bridge = set(no_auto_bridge or ())
        # ``{(designator, schdoc, channel_index): current_amperes}`` —
        # substituted into the parsed AnnotationResult before build_problem
        # so the FEM sees the new currents. ``channel_index`` is None for
        # the legacy unindexed SINK channel and an int for indexed channels
        # (PDN1_I / PDN2_I / …).
        self._sink_overrides = dict(sink_overrides or {})
        # ``{layer_id: copper_thickness_mm}`` — substituted into the
        # ExtractedProject's stackup so both per-layer conductance and
        # via-barrel hop lengths reflect the new thicknesses.
        self._stackup_overrides = dict(stackup_overrides or {})
        # Selects one of several .PcbDoc files in a multi-PCB project.
        # None = altium_monkey default (first PcbDoc in project order).
        self._pcbdoc_selector = pcbdoc_selector
        # False = "Import Altium Design (Clean)" / "Reload Design Info" path:
        # always run extract+geometry+annotations from disk. True = try the
        # FYPA design-info cache first and fall back to a fresh load on miss.
        self._use_design_cache = bool(use_design_cache)
        # True = try the FYPA SOLVE cache first; on hit, emit finished_ok
        # immediately with the cached (solution, metadata) and skip
        # extract/mesh/solve entirely. The pickle.load can take 5–10 s for
        # large boards, so doing it on this worker thread (rather than the
        # main thread before starting the worker) keeps the progress
        # dialog responsive instead of freezing the UI.
        self._try_solve_cache_first = bool(try_solve_cache_first)
        # FYPA editor-mode directives (project-file sources / sinks). When
        # present they're converted to synthetic SourceSpec / SinkSpec and
        # appended to the loaded AnnotationResult before build_problem —
        # and, like the override paths, they disable the solve cache (the
        # result diverges from the on-disk project).
        self._editor_directives = list(editor_directives or [])
        # FYPA editor-mode copper renames (user-given names for unnamed
        # copper pieces). Applied to ``loaded.extracted`` before
        # ``apply_editor_directives`` runs so the new nets exist by the
        # time directives are matched, and so the FEM bucketer sees the
        # renamed copper as part of those new nets instead of dropping it.
        self._copper_names = list(copper_names or [])
        # In-memory LoadedProject handed in by the editor 'Resolve' path:
        # the pristine design info the viewer already holds. When set, the
        # worker re-solves against it directly — no design-info cache read,
        # no project-file stat, no re-extract. None for every other flow
        # (normal load / Re-run / clean), which load design info as before.
        self._loaded_project = loaded_project
        # True = extract / reuse design info only, emit a stub solution,
        # and let the user press Solve in the viewer (Import Altium Design).
        self._load_only = bool(load_only)
        self._adaptive_regulator_gain = bool(adaptive_regulator_gain)
        # Opt-in (FYPA_SOLVE_SUBPROCESS): run the heavy mesh+solve+package step
        # in a child process so a cancel just kills the child — no QThread
        # terminate() that could orphan the solver's module locks. Off by
        # default; the in-process path below is unchanged when disabled.
        from fypa.solve_subprocess import subprocess_solve_enabled
        # Persisted in-app toggle OR the env-var override — either turns it on.
        self._use_subprocess = (
            load_solve_in_subprocess() or subprocess_solve_enabled()
        )
        # The child Process handle while a subprocess solve is in flight, so the
        # GUI abort path can kill it directly. None otherwise.
        self._solve_child = None
        # True while the worker is inside a pure-Python packaging stage
        # (build_solve_metadata / to_lean_solution / cache pickle). Those hold
        # the GIL continuously for 10+ s on large boards and only observe a
        # cancel at the NEXT stage boundary, so the GUI abort path must NOT
        # terminate() while this is set — it would kill the thread mid-GIL and
        # hang the whole app. It waits for the flag to clear instead.
        self._in_python_packaging = False

    def _emit_stub_and_finish(
        self, loaded, pristine_loaded, _timer,
        *, needs_directives: bool = False,
        stage_message: str | None = None,
        stub_pieces_by_pair=None,
        per_net_layers=None,
    ) -> None:
        """Package a stub LeanSolution + metadata and finish the worker.

        For the load-only and needs-directives stubs. A *mesh-failure* stub
        goes through :func:`~fypa.altium.loader.package_mesh_failure` instead,
        which additionally records ``mesher_config`` and per-via segment
        resistances. That difference is deliberate, not drift: nothing was
        meshed on this path, so there is no mesher run or via segmentation to
        record, and inventing values would misreport the Setup tab.
        """
        from fypa.altium.loader import build_solve_metadata
        from fypa.altium_geometry import build_per_net_geometry_layers

        if stage_message is not None:
            self.stage_changed.emit(stage_message)
        stub_solution = _build_stub_lean_solution_from_loaded(loaded)
        if needs_directives:
            stub_solution.solver_info["needs_directives"] = True
        with _timer.stage("Build stub metadata"):
            if per_net_layers is None:
                per_net_layers = build_per_net_geometry_layers(
                    loaded.extracted,
                )
            metadata = build_solve_metadata(
                loaded, None,
                settings=self._settings,
                per_net_layers=per_net_layers,
                stub_pieces_by_pair=stub_pieces_by_pair,
            )
        _timer.log_breakdown()
        self.finished_ok.emit(stub_solution, metadata, pristine_loaded)

    def _expected_stage_count(self) -> int:
        """Best-effort count of stage_changed emissions ``run()`` will fire,
        used to scale the QProgressDialog bar. Branches that depend on
        cache hit/miss (only known at runtime) are estimated by assuming
        the miss path — i.e. the bar will read slightly under 100% on a
        cache hit, which is acceptable since hits finish in <5 s anyway.
        """
        # "Checking solve cache…" — only when the fast-path is eligible.
        fast_path_eligible = (self._try_solve_cache_first
                              and not self._load_only
                              and not self._stackup_overrides
                              and not self._sink_overrides
                              and not self._editor_directives)
        n = 1 if fast_path_eligible else 0
        # Design-info: exactly one stage emitted in every variant
        # (loaded reuse / cache hit message / "Checking…" / "Loading from disk…").
        n += 1
        # Save-design-info-cache stage fires after a fresh disk load when
        # the design cache is enabled.
        if self._use_design_cache and self._loaded_project is None:
            n += 1
        # One stage per override / directive application.
        for opt in (self._stackup_overrides, self._sink_overrides,
                    self._copper_names, self._editor_directives):
            if opt:
                n += 1
        if self._load_only:
            # Build stub metadata + open viewer.
            n += 1
            return n
        # Assemble FEM + mesh+solve + package metadata + package convert
        # + opening viewer.
        n += 5
        # Save-solve-cache fires unless overrides / editor directives are
        # active (which would poison the cached solve).
        if not (self._stackup_overrides or self._sink_overrides
                or self._editor_directives):
            n += 1
        return n

    def _cancel_requested(self) -> bool:
        """True once the GUI has asked this worker to stop.

        Checked at stage boundaries so a cancel during a pure-Python phase
        (the "Packaging solution…" metadata / lean-convert / cache-pickle
        stages, 10+ s on large boards) unwinds the thread cooperatively. Those
        phases hold the GIL continuously; a ``terminate()`` fired there kills
        the thread while it owns the GIL, which never gets released and hangs
        the whole app — the opposite of what the user asked for by cancelling.
        Returning early from ``run`` avoids that: no ``finished_ok`` is emitted,
        so the GUI won't open a stale viewer either.
        """
        return self.isInterruptionRequested()

    def run(self) -> None:  # type: ignore[override]
        # Bias the OS scheduler toward the GUI thread. The packaging phase
        # (build_solve_metadata + to_lean_solution + cache pickle) is pure
        # Python and holds the GIL continuously; without this the main
        # thread starves, the QProgressDialog barber-pole pauses, and
        # Windows flags the window "Not Responding" on large boards.
        self.setPriority(QThread.LowPriority)
        self.total_stages.emit(self._expected_stage_count())
        try:
            from fypa.altium.loader import (
                clone_loaded_for_edit,
                load_project,
            )
            from pdnsolver import mesh as _pdn_mesh

            # Resolve the PcbDoc up-front so the cache key is stable.
            pcbdoc_resolved: Path | None = None
            try:
                from fypa.cli import _resolve_pcbdoc
                pcbdoc_resolved = _resolve_pcbdoc(
                    self._prjpcb_path,
                    str(self._pcbdoc_selector) if self._pcbdoc_selector else None,
                )
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Could not resolve PcbDoc for cache key (%s: %s); "
                    "solve cache will NOT be written this run.",
                    type(e).__name__, e,
                )
                pcbdoc_resolved = None

            # Solve-cache fast path: pickle.load can take 5–10 s on large
            # boards and blocks until the file is fully read, so doing it
            # here (off the GUI thread) keeps the progress dialog responsive.
            # On hit, finish immediately and skip extract/mesh/solve.
            # Skipped when overrides are active — the cached solve was
            # computed against the on-disk project and would be wrong.
            if (self._try_solve_cache_first
                    and not self._load_only
                    and pcbdoc_resolved is not None
                    and not self._stackup_overrides
                    and not self._sink_overrides
                    and not self._editor_directives):
                self.stage_changed.emit(
                    f"Checking solve cache for {self._prjpcb_path.name}…"
                )
                try:
                    cached = _try_solve_cache(self._prjpcb_path, pcbdoc_resolved)
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "Solve-cache check failed (%s: %s); will re-solve.",
                        type(e).__name__, e,
                    )
                    cached = None
                if cached is not None and not _cache_serves_adaptive_request(
                        cached[1], self._adaptive_regulator_gain):
                    logging.getLogger(__name__).info(
                        "Solve cache entry was not solved with adaptive SMPS "
                        "gain and this design has eligible regulators — "
                        "re-solving.",
                    )
                    cached = None
                if cached is not None:
                    logging.getLogger(__name__).info(
                        "Solve cache hit for %s — skipping extract + solve.",
                        self._prjpcb_path.name,
                    )
                    self.stage_changed.emit(
                        "Solve cache hit — opening viewer…"
                    )
                    # Solve-cache hit skips the extract entirely, so there's
                    # no LoadedProject to hand on — a later resolve from this
                    # viewer falls back to the design-info cache.
                    self.finished_ok.emit(cached[0], cached[1], None)
                    return

            # Whole-load stage timer — logs a breakdown just before the
            # viewer opens so a clean load self-reports where its time went.
            _timer = _StageTimer(logging.getLogger(__name__))

            # A resolve (editor directives present) always re-solves against
            # the design info that's already loaded — it must never re-stat
            # the Altium project files to revalidate it.
            _is_resolve = bool(self._editor_directives)

            loaded = None
            if self._loaded_project is not None:
                # In-memory reuse — the editor 'Resolve' path hands us the
                # LoadedProject the viewer already holds. No cache read, no
                # project-file stat, no re-extract: take the object as-is.
                self.stage_changed.emit("Reusing loaded design info…")
                loaded = self._loaded_project
            elif self._use_design_cache and pcbdoc_resolved is not None:
                # No in-memory LoadedProject — e.g. the load hit the solve
                # cache, so nothing was extracted. Fall back to the
                # design-info cache. On a resolve, reuse it as-is:
                # current_fp=None skips the fingerprint compare and the
                # project-file stat that the "Checking…" stage performs.
                self.stage_changed.emit(
                    "Reusing loaded design info…" if _is_resolve
                    else "Checking design-info cache…"
                )
                try:
                    from fypa.cli import (
                        _design_info_fingerprint,
                        _try_load_cached_design_info,
                    )
                    # Unpickling the cached LoadedProject ("reusing the
                    # design extract") runs many seconds on a big board.
                    # Time it so the load breakdown reports the cost — it
                    # parallels the "Save design-info cache" stage.
                    with _timer.stage("Load design-info cache"):
                        design_fp = (
                            None if _is_resolve
                            else _design_info_fingerprint(
                                self._prjpcb_path, pcbdoc_resolved,
                            )
                        )
                        loaded = _try_load_cached_design_info(
                            self._prjpcb_path, design_fp,
                            pcbdoc_path=pcbdoc_resolved,
                        )
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "Design-info cache check failed (%s); re-extracting.",
                        e,
                    )
                    loaded = None

            # The auto-bridge opt-out takes effect while annotations are
            # parsed — the merge it suppresses has already happened by the
            # time anything here could veto it — so a LoadedProject built
            # with a different set cannot be patched up, only rebuilt. This
            # is what makes the Bridges tab's "Disable auto-bridge" button
            # take effect on the very next Resolve, as its hint promises.
            if loaded is not None:
                _want_open = frozenset(
                    d.strip().upper() for d in (self._no_auto_bridge or ()))
                _have_open = getattr(
                    loaded, "no_auto_bridge_applied", frozenset())
                if _want_open != _have_open:
                    self.stage_changed.emit(
                        "Auto-bridge opt-out changed; reloading design info…")
                    loaded = None

            if loaded is None:
                self.stage_changed.emit("Loading project from disk…")
                with _timer.stage("Extract + load project"):
                    loaded = load_project(
                        self._prjpcb_path,
                        pcbdoc_selector=self._pcbdoc_selector,
                        no_auto_bridge=self._no_auto_bridge or None)
                # Persist the freshly-loaded design info so the next run
                # (e.g. a Re-run that only changes physics) can skip the
                # extract step. Failures are non-fatal.
                #
                # Skipped on a clean load (``use_design_cache`` False): a
                # clean load won't read a design-info cache anyway, so
                # writing one — ~5 s of pickling the ExtractedProject on a
                # big board — is pure dead weight on the critical path. The
                # first subsequent *non-clean* load repopulates it.
                if self._use_design_cache and pcbdoc_resolved is not None:
                    try:
                        from fypa.cli import (
                            _design_info_fingerprint,
                            _save_cached_design_info,
                        )
                        # Pickling the whole LoadedProject runs several
                        # seconds on a big board. Give it its own progress
                        # label + timing-log stage so the wait isn't
                        # mistaken for a hung "Loading project from disk".
                        self.stage_changed.emit("Saving design-info cache…")
                        with _timer.stage("Save design-info cache"):
                            design_fp = _design_info_fingerprint(
                                self._prjpcb_path, pcbdoc_resolved,
                            )
                            _save_cached_design_info(
                                self._prjpcb_path, design_fp, loaded,
                                pcbdoc_path=pcbdoc_resolved,
                            )
                    except Exception as e:
                        logging.getLogger(__name__).warning(
                            "Couldn't write design-info cache (%s); ignoring.",
                            e,
                        )
            else:
                self.stage_changed.emit(
                    "Reusing loaded design info…" if _is_resolve
                    else "Design-info cache hit — reusing extract."
                )

            # The design info exactly as loaded — before any stackup / sink
            # override or editor directive is applied. Emitted to the new
            # viewer so it can hand this same object straight back as the
            # in-memory source for the next resolve (see _loaded_project).
            pristine_loaded = loaded

            if self._stackup_overrides:
                self.stage_changed.emit(
                    f"Applying {len(self._stackup_overrides)} stackup "
                    "thickness override(s)…"
                )
                loaded = self._apply_stackup_overrides(loaded)

            # Sink overrides and editor directives mutate ``loaded`` in
            # place. Adaptive SMPS gain likewise rewrites regulator gains on
            # ``loaded.annotations`` during the solve. Clone first so the
            # pristine in-memory LoadedProject the viewer retains (reused by
            # the next Resolve) is never touched.
            #
            # This clone must run WHENEVER a mutating step will — the previous
            # ``loaded is pristine_loaded`` gate skipped it after a stackup
            # override, but ``_apply_stackup_overrides`` returns a fresh
            # LoadedProject that still SHARES ``annotations`` with the pristine
            # copy. Mutating that shared annotations in place appended a
            # duplicate of every editor SOURCE/SINK on each Resolve (~2× sink
            # current, compounding). ``clone_loaded_for_edit`` copies only the
            # annotations (fresh directives list) and shares the override'd
            # extracted/geometry, so the override is preserved.
            # "Adaptive SMPS gain" is a global preference, so it arrives set on
            # boards it cannot affect. Collapse it to its effective value now
            # that the design is loaded: the clone below and the cache-write
            # guard later must not fire for a flag ``solve_problem_adaptive``
            # will short-circuit anyway.
            if self._adaptive_regulator_gain:
                from fypa.altium.loader import has_adaptive_smps_regulators
                if not has_adaptive_smps_regulators(loaded):
                    logging.getLogger(__name__).info(
                        "Adaptive SMPS gain requested but no regulator is "
                        "eligible — treating as off (keeps the solve cache).",
                    )
                    self._adaptive_regulator_gain = False

            if (self._sink_overrides or self._editor_directives
                    or self._copper_names or self._adaptive_regulator_gain):
                loaded = clone_loaded_for_edit(loaded)

            if self._sink_overrides:
                self.stage_changed.emit(
                    f"Applying {len(self._sink_overrides)} sink-current "
                    "override(s)…"
                )
                self._apply_sink_overrides(loaded)

            if self._copper_names:
                self.stage_changed.emit(
                    f"Naming {len(self._copper_names)} unnamed copper "
                    "piece(s)…"
                )
                from fypa.editor_directives import apply_copper_names
                cn_warnings = apply_copper_names(
                    loaded, self._copper_names,
                )
                for w in cn_warnings:
                    logging.getLogger(__name__).warning(
                        "Copper name not applied: %s", w,
                    )

            if self._editor_directives:
                self.stage_changed.emit(
                    f"Applying {len(self._editor_directives)} editor "
                    "directive(s)…"
                )
                from fypa.editor_directives import apply_editor_directives
                ed_warnings = apply_editor_directives(
                    loaded, self._editor_directives,
                )
                for w in ed_warnings:
                    logging.getLogger(__name__).warning(
                        "Editor directive not applied: %s", w,
                    )

            # is_solveable check runs HERE — after editor directives + copper
            # names + overrides have been applied. A Gerber-sourced project
            # starts with zero schematic directives; an Altium project may also
            # rely entirely on editor-mode directives. Checking earlier would
            # reject both even though the user has placed a SOURCE marker.
            if not loaded.is_solveable:
                _log = logging.getLogger(__name__)
                from fypa.altium.loader import format_solve_blockers
                try:
                    self.stage_changed.emit("Building diagnostic summary…")
                    with _timer.stage("Build diagnostic summary"):
                        _summary = loaded.diagnostic_summary()
                    _log.error("Project is not solveable:\n%s", _summary)
                except Exception as _exc:
                    _log.error("Project is not solveable (diagnostic failed: %s)", _exc)

                # Recoverable case: the project has copper and the annotation
                # parse is clean — the *only* missing piece is a SOURCE /
                # REGULATOR directive (see LoadedProject.is_solveable). Rather
                # than refuse to load, open the viewer in editor mode with a
                # stub solution — exactly how a Gerber import opens — so the
                # user can place a SOURCE marker by hand and then Resolve.
                # Restricted to a plain initial load: overrides / editor
                # directives mean the user explicitly asked for a re-solve, so
                # a missing source there is still a hard failure.
                recoverable_no_source = (
                    bool(loaded.extracted.enabled_copper_layer_ids())
                    and not loaded.annotations.errors
                    and not self._editor_directives
                    and not self._stackup_overrides
                    and not self._sink_overrides
                )
                if recoverable_no_source:
                    _log.info(
                        "No SOURCE/REGULATOR directive — opening in editor "
                        "mode (stub solution) for manual setup."
                    )
                    self._emit_stub_and_finish(
                        loaded, pristine_loaded, _timer,
                        needs_directives=True,
                        stage_message="No PDN settings found — opening design…",
                    )
                    return

                # Annotation errors but copper present — open stub viewer so
                # the user can inspect Setup → Annotation log and fix Altium
                # parameters without hunting the log file.
                recoverable_annotation_errors = (
                    bool(loaded.extracted.enabled_copper_layer_ids())
                    and loaded.annotations.errors
                    and not self._editor_directives
                    and not self._stackup_overrides
                    and not self._sink_overrides
                )
                if recoverable_annotation_errors:
                    _log.info(
                        "Annotation errors — opening design with stub "
                        "solution for review."
                    )
                    self._emit_stub_and_finish(
                        loaded, pristine_loaded, _timer,
                        stage_message="Annotation errors — opening design…",
                    )
                    return

                self.failed.emit(format_solve_blockers(loaded))
                return

            if self._load_only:
                self._emit_stub_and_finish(
                    loaded, pristine_loaded, _timer,
                    stage_message="Opening design…",
                )
                return

            if self._cancel_requested():
                return
            mesher_config = _pdn_mesh.Mesher.Config(
                minimum_angle=self._settings.mesh_min_angle_deg,
                maximum_size=self._settings.mesh_max_size_mm,
                variable_size_maximum_factor=(
                    3.0 if getattr(self._settings, "adaptive_mesh", False)
                    else 1.0
                ),
            )

            # Mesh + solve + package: either in this thread (default) or in a
            # child process (FYPA_SOLVE_SUBPROCESS). Both return the lean
            # ``(solution, metadata)``; ``None`` means cancelled (or, for the
            # subprocess path, a failure that already emitted ``failed``).
            if self._use_subprocess:
                _packaged = self._solve_and_package_subprocess(
                    loaded, mesher_config, _timer)
            else:
                _packaged = self._solve_and_package_inprocess(
                    loaded, mesher_config, _timer)
            if _packaged is None:
                return
            new_solution, metadata = _packaged

            # Persist the solve to the FYPA solve cache so the next
            # "Import Altium Design" can skip both extract and solve.
            # Skip when stackup_overrides / sink_overrides are in play —
            # the resulting solve diverges from the on-disk project, so
            # caching it would poison the next plain load. Failures are
            # non-fatal: the viewer still opens.
            _cache_log = logging.getLogger(__name__)
            if pcbdoc_resolved is None:
                _cache_log.warning(
                    "Solve cache NOT written: pcbdoc_resolved is None "
                    "(see earlier warning from _resolve_pcbdoc).",
                )
            elif (metadata or {}).get("mesh_failed"):
                _cache_log.info(
                    "Solve cache NOT written: meshing failed "
                    "(stub only).",
                )
            elif (self._stackup_overrides or self._sink_overrides
                    or self._editor_directives
                    or self._adaptive_regulator_gain):
                _cache_log.info(
                    "Solve cache NOT written: stackup/sink overrides, "
                    "editor directives, or adaptive SMPS gain are active; "
                    "cached solve would diverge from the on-disk project.",
                )
            else:
                try:
                    from fypa.cli import (
                        _project_fingerprint,
                        _save_cached_solution,
                        _solve_cache_path,
                    )
                    self.stage_changed.emit("Packaging solution: saving cache…")
                    # Key the cache on the settings THIS solve used, so a plain
                    # default import (which reads with default settings) can't
                    # reuse a solve run with non-default mesh/physics settings.
                    solve_fp = _project_fingerprint(
                        self._prjpcb_path, pcbdoc_resolved,
                        settings=self._settings,
                    )
                    cache_path = _solve_cache_path(
                        self._prjpcb_path, pcbdoc_resolved,
                    )
                    # Pickling the solution + metadata is another GIL-holding
                    # Python stage — guard it from terminate() the same way.
                    self._in_python_packaging = True
                    try:
                        with _timer.stage("Write solve cache"):
                            wrote = _save_cached_solution(
                                self._prjpcb_path, solve_fp,
                                new_solution, metadata,
                                pcbdoc_path=pcbdoc_resolved,
                            )
                    finally:
                        self._in_python_packaging = False
                    if wrote:
                        _cache_log.info(
                            "Solve cache written to %s "
                            "(%d files in fingerprint).",
                            cache_path,
                            len(solve_fp.get("files") or {}),
                        )
                except Exception as e:
                    _cache_log.warning(
                        "Couldn't write solve cache (%s: %s); ignoring.",
                        type(e).__name__, e,
                    )

            # Final stage before handing off — the new PdnViewer construction
            # in the GUI thread's _on_solve_finished slot is heavy on big
            # boards (tabs, GL widgets, setup HTML for thousands of
            # components) and blocks the dialog from repainting until it
            # finishes. Update the label so the user sees what's actually
            # happening instead of a stale "saving cache" message.
            if self._cancel_requested():
                return
            self.stage_changed.emit("Opening viewer…")
            _timer.log_breakdown()
            self.finished_ok.emit(new_solution, metadata, pristine_loaded)
        except Exception as e:
            import traceback
            self.failed.emit(
                f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            )

    def _register_solve_child(self, proc) -> None:
        """Store the in-flight solve child process so :func:`_abort_solve_worker`
        can kill it directly on cancel."""
        self._solve_child = proc

    def _solve_and_package_inprocess(self, loaded, mesher_config, _timer):
        """Mesh + solve + build metadata + convert to lean, on this thread.

        Returns ``(new_solution, metadata)``, or ``None`` if a cancel was
        requested at a stage boundary. A meshing failure also returns a
        ``(stub, metadata)`` pair — with ``metadata["mesh_failed"]`` set — so
        the caller's single emit path handles it, exactly as the subprocess
        path already did. This is the original in-process path, unchanged
        except that the cancel checks return ``None`` (so the caller returns)
        instead of returning from ``run`` directly."""
        from fypa.altium.loader import build_solve_metadata, solve_problem_adaptive
        from fypa.lean_solution import to_lean_solution
        from pdnsolver import mesh as _pdn_mesh

        # Python warnings.warn() (e.g. padne's SolverWarning about ground
        # node current) are routed into the logging system once at app
        # startup (see main()), so they reach the log file / Messages tab
        # from here without touching that global state per solve.
        # Forward pdnsolver's per-step INFO log records to the GUI as
        # substage updates so the user can see what the solver is
        # currently doing during the ~20 s opaque "Meshing + solving"
        # stage ("Meshing the connected components", "Constructing the
        # Laplace operators", "Solving the system of equations", …).
        sub_emit = self.substage_changed.emit

        class _SubstageForwarder(logging.Handler):
            def emit(self_h, record: logging.LogRecord) -> None:
                try:
                    sub_emit(record.getMessage())
                except Exception:
                    pass

        _substage_handler = _SubstageForwarder(level=logging.INFO)
        _solver_log = logging.getLogger("pdnsolver.solver")
        _mesh_log = logging.getLogger("pdnsolver.mesh")
        _solver_log.addHandler(_substage_handler)
        _mesh_log.addHandler(_substage_handler)
        with _timer.stage("Mesh + solve"):
            try:
                (padne_solution, problem, via_segment_records,
                 stub_pieces_by_pair, per_net_layers,
                 adaptive_info) = solve_problem_adaptive(
                    loaded,
                    mesher_config,
                    adaptive_regulator_gain=self._adaptive_regulator_gain,
                    stage_callback=self.stage_changed.emit,
                    thermal_config=self._settings.thermal_config(),
                )
            except _pdn_mesh.MeshingException as mesh_exc:
                from fypa.altium.loader import package_mesh_failure

                # Same stub packaging as CLI ``gui <PrjPcb>`` (Altium launcher):
                # open the board with mesh-failure markers instead of dying.
                if self._cancel_requested():
                    # Every other exit in run() returns without emitting so a
                    # cancelled solve does not open a stale viewer. Meshing
                    # failing after Cancel is no different.
                    return None
                # Emit first so the progress dialog updates before the
                # (potentially slow) stub + metadata packaging.
                self.stage_changed.emit("Meshing failed — opening design…")
                # Packaging may re-run build_problem and triangulate every
                # stub piece — a long pure-Python phase holding the GIL. Mark
                # it so a Cancel here waits instead of calling
                # QThread.terminate() and deadlocking the app.
                self._in_python_packaging = True
                try:
                    with _timer.stage("Package mesh failure"):
                        stub, fail_md = package_mesh_failure(
                            loaded, mesh_exc, mesher_config,
                            settings=self._settings,
                        )
                finally:
                    self._in_python_packaging = False
                if self._cancel_requested():
                    return None
                # Return like any other packaged result rather than emitting
                # here. run()'s tail already skips the cache on mesh_failed,
                # logs the timing breakdown OUTSIDE the "Mesh + solve" stage
                # (so the meshing and packaging time is attributed rather than
                # landing in "(other / untimed)"), and emits with
                # pristine_loaded.
                return stub, fail_md
            finally:
                _solver_log.removeHandler(_substage_handler)
                _mesh_log.removeHandler(_substage_handler)
        si = padne_solution.solver_info
        _log_post = logging.getLogger(__name__)
        _log_post.info(
            "Solver stats: ground_node_current=%.4g A, residual_norm=%.4g",
            si.ground_node_current, si.residual_norm,
        )
        if abs(si.ground_node_current) > 1e-3:
            _log_post.warning(
                "Ground node current is %.4g A — far from zero. The FEM "
                "is injecting this current at the reference vertex to "
                "balance the system. Absolute voltages are unreliable.",
                si.ground_node_current,
            )

        if self._cancel_requested():
            return None
        # Enter the pure-Python packaging phase — signal the GUI abort path not
        # to terminate() us while these GIL-holding stages run (see
        # ``_in_python_packaging`` and ``_cancel_requested``).
        self._in_python_packaging = True
        try:
            self.stage_changed.emit("Packaging solution: building metadata…")
            with _timer.stage("Build solve metadata"):
                metadata = build_solve_metadata(
                    loaded, problem,
                    mesher_config=mesher_config,
                    solver_info=padne_solution.solver_info,
                    via_segment_records=via_segment_records,
                    settings=self._settings,
                    stub_pieces_by_pair=stub_pieces_by_pair,
                    per_net_layers=per_net_layers,
                    regulator_adaptive_gain=adaptive_info,
                )
            if self._cancel_requested():
                return None
            self.stage_changed.emit("Packaging solution: converting result…")
            with _timer.stage("Convert to lean solution"):
                new_solution = to_lean_solution(padne_solution)
        finally:
            self._in_python_packaging = False
        return new_solution, metadata

    def _solve_and_package_subprocess(self, loaded, mesher_config, _timer):
        """Same as :meth:`_solve_and_package_inprocess`, but the mesh + solve +
        package runs in a child process (opt-in, ``FYPA_SOLVE_SUBPROCESS``).

        Returns ``(new_solution, metadata)``; ``None`` on cancel or on a child
        failure (which emits ``failed`` itself so ``run`` just returns)."""
        from fypa.solve_subprocess import (
            SolveJob, SolveSubprocessError, run_solve_in_subprocess,
        )
        job = SolveJob(
            loaded=loaded,
            mesher_config=mesher_config,
            settings=self._settings,
            adaptive_regulator_gain=self._adaptive_regulator_gain,
        )
        try:
            with _timer.stage("Mesh + solve (subprocess)"):
                result = run_solve_in_subprocess(
                    job,
                    on_stage=self.stage_changed.emit,
                    on_substage=self.substage_changed.emit,
                    is_cancelled=self._cancel_requested,
                    register_process=self._register_solve_child,
                )
        except SolveSubprocessError as e:
            self.failed.emit(f"Solve subprocess failed:\n\n{e}")
            return None
        finally:
            self._solve_child = None
        # None => cancelled (the child was already terminated in the loop).
        return result

    def _apply_stackup_overrides(self, loaded):
        """Return a new :class:`LoadedProject` whose extracted stackup
        has the user-supplied copper thicknesses substituted in.

        ExtractedProject + RawStackupLayer are both frozen dataclasses,
        so we rebuild the stackup tuple via :func:`dataclasses.replace`.
        ``loaded.geometry`` is recomputed too so the displayed per-layer
        conductance reflects the new thickness — the FEM itself reads
        ``loaded.extracted.stackup`` directly via
        :func:`fypa.altium.loader.build_per_net_geometry_layers`, so the
        solve correctness only depends on the new ExtractedProject.
        Skipped overrides (layer not in stackup) are silently ignored.
        """
        from dataclasses import replace as _dc_replace
        from fypa.altium_geometry import build_layer_geometries
        from fypa.altium.loader import LoadedProject

        new_stackup = []
        for s in loaded.extracted.stackup:
            if s.layer_id in self._stackup_overrides:
                new_thk = float(self._stackup_overrides[s.layer_id])
                s = _dc_replace(s, copper_thickness_mm=new_thk)
            new_stackup.append(s)

        new_extracted = _dc_replace(
            loaded.extracted, stackup=tuple(new_stackup),
        )
        # Rebuild the legacy single-union geometry — cheap relative to
        # the solve, and keeps the diagnostic_summary honest about the
        # new sheet conductance. LoadedProject is no longer a frozen
        # dataclass (geometry was made lazy to skip ~0.8 s on every
        # solve), so we construct a fresh one explicitly instead of
        # going through dataclasses.replace.
        new_geometry = build_layer_geometries(new_extracted)
        return LoadedProject(
            extracted=new_extracted,
            annotations=loaded.annotations,
            geometry=new_geometry,
        )

    def _apply_sink_overrides(self, loaded) -> None:
        """Mutate ``loaded.annotations.directives`` in-place so every
        SinkSpec whose ``(designator, schdoc, channel_index)`` matches an
        override gets replaced with a copy carrying the new ``current``.
        Unmatched overrides are silently ignored — typically because the
        user deleted a SINK directive (or removed an indexed channel) in
        Altium between solves."""
        from dataclasses import replace as _dc_replace
        from fypa.altium.annotations import SinkSpec
        directives = loaded.annotations.directives
        for i, d in enumerate(directives):
            if not isinstance(d, SinkSpec):
                continue
            key = (d.designator, d.schdoc_name, d.channel_index)
            if key in self._sink_overrides:
                directives[i] = _dc_replace(
                    d, current=float(self._sink_overrides[key]),
                )

    def _clone_loaded_for_edit(self, loaded):
        """Backward-compatible alias — see :func:`clone_loaded_for_edit`."""
        from fypa.altium.loader import clone_loaded_for_edit
        return clone_loaded_for_edit(loaded)




def _cache_serves_adaptive_request(metadata, adaptive_requested: bool) -> bool:
    """Whether a cached solve may answer a request with this adaptive setting.

    "Adaptive SMPS gain" is persisted *globally*, so a user who ticks it once
    for one board carries it into every later import. On a design with no
    eligible regulator the flag is a no-op — ``solve_problem_adaptive``
    short-circuits on :func:`has_adaptive_smps_regulators` — so refusing the
    cache there costs a full 10-60 s re-solve on every import for a result
    that would be bit-identical.

    The cached metadata records ``adaptive_gain_eligible`` per regulator
    directive, which is exactly what decides whether the flag could have
    mattered.
    """
    if not adaptive_requested:
        return True
    info = (metadata or {}).get("regulator_adaptive_gain") or {}
    if info.get("enabled"):
        return True          # the cached solve is itself an adaptive solve
    return not any(
        d.get("adaptive_gain_eligible")
        for d in ((metadata or {}).get("directives") or [])
    )




def _try_solve_cache(prjpcb_path: Path,
                     pcbdoc_path: Path | None) -> tuple[object, dict] | None:
    """Return ``(solution, metadata)`` from the FYPA solve cache if the
    fingerprint matches, else ``None``. Wraps the FYPA helpers so menu
    handlers don't have to know about fingerprints; cache misses + import
    failures are silently treated as ``None``."""
    try:
        from fypa.cli import (
            _project_fingerprint,
            _try_load_cached_solution,
        )
        fp = _project_fingerprint(prjpcb_path, pcbdoc_path)
        cached = _try_load_cached_solution(prjpcb_path, fp,
                                            pcbdoc_path=pcbdoc_path)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Solve-cache check failed (%s); will re-solve.", e,
        )
        return None
    if cached is None or cached[0] is None:
        return None
    return cached




def _choose_pcbdoc(parent: QWidget | None, prjpcb_path: Path,
                   default: Path | None = None) -> tuple[bool, Path | None]:
    """Resolve which .PcbDoc to use for ``prjpcb_path``.

    Returns ``(proceed, selected_path)``:

    * ``(True, path)``  — caller should solve against ``path``.
    * ``(True, None)``  — failed to enumerate, but caller may still
      attempt with altium_monkey's default. Used as a soft fallback.
    * ``(False, None)`` — user cancelled the chooser; caller should
      abort silently.

    A single-PCB project returns the only path without prompting.
    Multi-PCB projects open a modal :class:`QInputDialog.getItem` so the
    user can pick. ``default``, when supplied and present in the list, is
    pre-selected (handy for re-runs of a previously-chosen board).
    """
    try:
        from fypa.altium.extract import list_pcbdoc_paths
        paths = list_pcbdoc_paths(prjpcb_path)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Couldn't enumerate PcbDocs in %s (%s); falling back to default.",
            prjpcb_path, e,
        )
        return True, None
    if not paths:
        QMessageBox.critical(
            parent, "No PcbDoc in project",
            f"{prjpcb_path.name} does not reference any .PcbDoc — FYPA "
            "needs a PCB document for power analysis.",
        )
        return False, None
    if len(paths) == 1:
        return True, paths[0]
    names = [p.name for p in paths]
    default_idx = 0
    if default is not None:
        default_resolved = default.resolve()
        for i, p in enumerate(paths):
            if p.resolve() == default_resolved:
                default_idx = i
                break
    chosen, ok = QInputDialog.getItem(
        parent, "Select PcbDoc",
        f"{prjpcb_path.name} contains multiple PCB documents.\n"
        "Choose which one to solve:",
        names, default_idx, False,
    )
    if not ok or not chosen:
        return False, None
    return True, paths[names.index(chosen)]




def _abort_solve_worker(owner) -> None:
    """Tear down an in-flight ``_SolveWorker`` owned by ``owner``.

    Detaches the worker's result signals (so a late finish can't pop open
    a viewer we no longer want), requests cooperative interruption, then
    falls back to ``QThread.terminate()`` for when the worker is mid-solve
    in a long-running scipy/Triangle C call we can't interrupt politely.
    Forcible termination may leak some Triangle / scipy arena memory until
    process exit, but the interpreter itself stays usable — the user can
    open another project without restarting.

    Also closes the progress dialog and clears ``owner``'s solve refs so
    the late ``QThread.finished`` → ``_cleanup_solve_worker`` chain is a
    no-op. Safe to call when nothing is in flight."""
    worker = getattr(owner, "_solve_worker", None)
    if worker is not None:
        # Detach BOTH the result signals (finished_ok/failed/stage_changed)
        # and QThread.finished. The latter normally fires
        # ``_cleanup_solve_worker``, but we do its work inline below — and
        # leaving it connected would race with a fresh solve started right
        # after cancel: the OLD worker's late ``finished`` would wipe
        # ``owner._solve_worker``, which by then points at the NEW worker.
        for sig_name in ("finished_ok", "failed", "stage_changed",
                         "substage_changed", "finished"):
            sig = getattr(worker, sig_name, None)
            if sig is not None:
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        # If the solver is mid-meshing, tear down the worker-process pool
        # so the children stop after their current Triangle call instead
        # of running to completion as orphans. Safe no-op when no pool is
        # active. Must happen BEFORE terminate(), so the queue close
        # propagates before the thread holding the pool dies. This touches
        # only the mesh pool, never the solver locks, so it can't block.
        try:
            from pdnsolver.solver import cancel_active_mesh_pool
            cancel_active_mesh_pool()
        except Exception as _exc:
            logging.getLogger(__name__).warning(
                "cancel_active_mesh_pool raised %s; carrying on.", _exc,
            )
        # Subprocess solve path: the heavy work runs in a child process holding
        # the solver's caches/locks in ITS own address space. Kill it directly
        # so the cancel is prompt and nothing in this (parent) process can be
        # left locked — the whole reason the subprocess path exists. The worker
        # thread's polling loop also notices requestInterruption() below and
        # tears the child down cooperatively; this is belt-and-braces.
        _child = getattr(worker, "_solve_child", None)
        if _child is not None:
            try:
                if _child.is_alive():
                    _child.terminate()
            except Exception as _exc:
                logging.getLogger(__name__).warning(
                    "solve child terminate raised %s; carrying on.", _exc,
                )
        # Ask the worker to stop, THEN free the solver caches — never before.
        # free_pardiso_cache/free_mesh_assembly_cache acquire module locks that
        # the worker holds for the whole factorisation ("seconds to minutes");
        # calling them here on the GUI thread while the worker is mid-solve
        # would block the event loop and show "Not Responding" — the exact hang
        # cancel exists to avoid.
        worker.requestInterruption()
        # Give the worker a chance to stop cooperatively at its next stage
        # boundary first (see _SolveWorker._cancel_requested). This matters for
        # the pure-Python packaging phase: terminate() fired while the worker
        # holds the GIL would deadlock the GUI. Only force-kill if it doesn't
        # unwind on its own — that's the native mesh/solve section, where
        # terminate() is safe because the GIL is released.
        reaped = worker.wait(1500)
        if not reaped:
            # wait(1500) timed out. If the worker is inside a pure-Python
            # packaging stage (build_solve_metadata / to_lean_solution / cache
            # pickle) it holds the GIL continuously for 10+ s and only observes
            # the cancel at the NEXT stage boundary. terminate() here would kill
            # the thread while it owns the GIL — it never gets released and the
            # whole app hangs (the exact failure cancel exists to avoid). So
            # while the packaging flag is set, keep waiting for it to reach that
            # boundary and unwind cooperatively instead of force-killing.
            while getattr(worker, "_in_python_packaging", False):
                if worker.wait(500):
                    reaped = True
                    break
        if reaped:
            # Clean cooperative stop: run() returned, so the worker released
            # every module lock. The frees are now non-blocking and drop the
            # PARDISO factorisation / mesh assembly so a solve killed mid-way
            # can't leave stale state for the next solve to reuse.
            for _free_name in ("free_pardiso_cache", "free_mesh_assembly_cache"):
                try:
                    import pdnsolver.solver as _S
                    getattr(_S, _free_name)()
                except Exception as _exc:
                    logging.getLogger(__name__).warning(
                        "%s raised %s; carrying on.", _free_name, _exc,
                    )
        else:
            # Stuck in a native scipy/Triangle call we can't interrupt (not in a
            # Python packaging stage — that case kept waiting above). Force it
            # down. terminate() may kill the worker WHILE it holds
            # _sym_solver_lock / _mesh_assembly_lock — a threading.Lock held by
            # a dead thread is never released, so the ordinary frees (and every
            # future solve) would deadlock on it. force_reset rebinds those
            # locks to fresh objects and drops the caches WITHOUT acquiring the
            # orphaned ones.
            worker.terminate()
            # 2 s is plenty for the OS to reap the thread; we don't want to
            # block the GUI indefinitely if something pathological happens.
            reaped = worker.wait(2000)
            try:
                from pdnsolver.solver import force_reset_caches_after_terminate
                force_reset_caches_after_terminate()
            except Exception as _exc:
                logging.getLogger(__name__).warning(
                    "force_reset_caches_after_terminate raised %s; carrying on.",
                    _exc,
                )
    updater = getattr(owner, "_solve_progress_updater", None)
    if updater is not None:
        updater.stop()
        updater.deleteLater()
        owner._solve_progress_updater = None
    dlg = getattr(owner, "_solve_progress_dlg", None)
    if dlg is not None:
        # QProgressDialog.closeEvent re-emits ``canceled``, so closing a dialog
        # whose ``canceled`` is still wired to the cancel handler re-enters it
        # synchronously — terminating/​waiting the worker a second time and
        # opening a duplicate launcher window. Disconnect first (guarded, same
        # as the worker signals above).
        try:
            dlg.canceled.disconnect()
        except (RuntimeError, TypeError):
            pass
        dlg.close()
        # close() merely hides the parented dialog; delete it so it doesn't
        # leak one QProgressDialog per cancelled solve.
        dlg.deleteLater()
        owner._solve_progress_dlg = None
    if worker is not None:
        # terminate() + wait(2000) above can fail to reap a pathologically stuck
        # thread; deleteLater() on a still-running QThread calls ~QThread →
        # qFatal and aborts the app. Route through _retire_thread, which only
        # deletes once the thread has actually stopped (and otherwise holds a
        # strong ref until it emits ``finished``).
        _retire_thread(worker)
        owner._solve_worker = None





def _build_stub_lean_solution_from_loaded(loaded):
    """Create a minimal :class:`LeanSolution` from a LoadedProject.

    Thin wrapper around
    :func:`fypa.altium.loader.build_stub_lean_solution_from_loaded` (shared
    with the CLI ``gui`` path so mesh-failure recovery stays in sync).
    """
    from fypa.altium.loader import build_stub_lean_solution_from_loaded
    log = logging.getLogger(__name__)
    t_geom0 = time.monotonic()
    stub = build_stub_lean_solution_from_loaded(loaded)
    log.info(
        # Spans the lazy loaded.geometry access AND the per-layer LeanLayer
        # construction — naming only the former made this read as a pure
        # geometry-access cost when deciding whether that property is the
        # bottleneck.
        "Stub solution: geometry access + build took %.2fs (%d layer(s))",
        time.monotonic() - t_geom0,
        len(stub.problem.layers),
    )
    return stub
