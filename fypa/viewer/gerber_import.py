"""Importing a Gerber fabrication package."""
from __future__ import annotations

import logging
import time
from pathlib import Path
import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from fypa.viewer.solve_worker import _build_stub_lean_solution_from_loaded


class _GerberImportCancelled(Exception):
    """Signals that a Gerber import was cancelled cooperatively at a stage
    boundary. Raised from :func:`_finish_gerber_import` when the worker's
    ``cancel_cb`` reports an interruption request, and caught in
    :meth:`_GerberImportWorker.run` so the thread unwinds without emitting a
    result - mirroring :meth:`_SolveWorker._cancel_requested`. The packaging
    phase (all-copper / primitives records) is pure Python holding the GIL,
    so a hard ``terminate()`` there could deadlock the app; the cooperative
    checkpoint avoids that."""




class _GerberImportWorker(QThread):
    """Background worker that runs :func:`_finish_gerber_import` off the
    GUI thread so a fresh-import doesn't freeze the application.

    The import window must run on the main thread (Qt widgets can't live
    on a worker), so it collects the inputs first and only hands them to
    the worker (through :class:`GerberImportRun`). The worker
    emits ``stage_changed`` / ``substage_changed`` to drive the progress
    dialog, then ``finished_ok`` with the import result tuple or
    ``failed`` with an error message.
    """

    stage_changed = Signal(str)
    substage_changed = Signal(str)
    total_stages = Signal(int)     # see _SolveWorker.total_stages
    finished_ok = Signal(object)   # the (stub_sol, metadata, loaded, pf) tuple
    failed = Signal(str)

    # The Gerber import always emits the same nine stages - four from
    # extract_gerber_project (render layers, build SBR records, read drills,
    # build outline) and five from _finish_gerber_import (load project,
    # build geometry, build all-copper records, build primitives index,
    # opening viewer).
    _EXPECTED_STAGES = 9

    def __init__(self, picked_result, pseudo, parent=None) -> None:
        super().__init__(parent)
        self._picked_result = picked_result
        self._pseudo = pseudo

    def run(self) -> None:  # type: ignore[override]
        # Match _SolveWorker's scheduler bias so the GUI thread keeps its
        # ticks even while the import pool is hammering all cores.
        self.setPriority(QThread.LowPriority)
        self.total_stages.emit(self._EXPECTED_STAGES)
        try:
            def _cb(stage, substage):
                if stage is not None:
                    self.stage_changed.emit(stage)
                if substage is not None:
                    self.substage_changed.emit(substage)
            result = _finish_gerber_import(
                self._picked_result, self._pseudo, progress_cb=_cb,
                cancel_cb=self.isInterruptionRequested,
            )
        except _GerberImportCancelled:
            # Cancelled at a stage boundary - the GUI already tore down its
            # dialog and dropped our signals, so just unwind silently.
            logging.getLogger(__name__).info(
                "Gerber import cancelled cooperatively.",
            )
            return
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Gerber import worker failed",
            )
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.finished_ok.emit(result)




def _pseudo_project_path(first_file: Path) -> Path:
    """Synthesise a pseudo-PrjPcb path next to the Gerbers - the cache key
    + project identity rely on having a stable Path, but no file of that
    name needs to exist on disk. Named after the folder of the first file
    in the import window's list."""
    folder = Path(first_file).parent.resolve()
    return folder / f"{folder.name}.fypa-gerber"


class GerberImportRun(QObject):
    """One off-thread Gerber import: owns the :class:`_GerberImportWorker`,
    passes its progress signals on, and handles cancel and clean-up the
    same way for every caller (the import window, and a Recent replay's
    progress dialog).

    ``succeeded`` carries the ``(stub_sol, metadata, loaded, pf)`` tuple.
    After :meth:`cancel` neither ``succeeded`` nor ``failed`` fires.
    """

    stage_changed = Signal(str)
    substage_changed = Signal(str)
    total_stages = Signal(int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, result, pseudo, parent=None) -> None:
        super().__init__(parent)
        self.result = result
        self.pseudo = pseudo
        self._worker: _GerberImportWorker | None = None

    def is_running(self) -> bool:
        return self._worker is not None

    def start(self) -> None:
        worker = _GerberImportWorker(self.result, self.pseudo, parent=self)
        worker.stage_changed.connect(self.stage_changed)
        worker.substage_changed.connect(self.substage_changed)
        worker.total_stages.connect(self.total_stages)
        worker.finished_ok.connect(self._on_ok)
        worker.failed.connect(self._on_fail)
        self._worker = worker
        worker.start()

    def _release(self) -> None:
        from fypa.viewer.session import _retire_thread
        worker, self._worker = self._worker, None
        if worker is None:
            return
        for sig in (worker.stage_changed, worker.substage_changed,
                    worker.total_stages, worker.finished_ok, worker.failed):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        # The worker may still be inside the non-interruptible ProcessPool
        # render phase (or just past emitting its result); retire it safely
        # rather than deleteLater()-ing a running QThread (which would
        # qFatal the app).
        _retire_thread(worker)

    def _on_ok(self, res) -> None:
        if self._worker is None:
            return
        self._release()
        self.succeeded.emit(res)

    def _on_fail(self, msg: str) -> None:
        if self._worker is None:
            return
        self._release()
        self.failed.emit(msg)

    def cancel(self) -> None:
        """Ask the worker to unwind at its next stage boundary (the
        pure-Python packaging phase honours isInterruptionRequested()) and
        tear down the render pool so queued layers stop instead of running
        to completion as orphans burning every core. The worker's signals
        are dropped: whether it stops early or runs to completion, its
        result is discarded."""
        worker = self._worker
        if worker is None:
            return
        worker.requestInterruption()
        try:
            from fypa.gerber.extract import cancel_active_gerber_pool
            cancel_active_gerber_pool()
        except Exception:
            pass
        self._release()


def run_gerber_import_with_progress(parent, result, pseudo, on_ok) -> None:
    """Run an import for already-chosen inputs (a replayed Recent entry)
    behind a modal progress dialog with a Cancel button. ``on_ok(res)``
    gets the result tuple; a failure is shown in a message box."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

    from fypa.viewer.solve_worker import _SolveProgressUpdater

    dlg = QProgressDialog("Starting Gerber import…", "Cancel", 0, 0, parent)
    dlg.setWindowTitle("Importing Gerber files")
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
    dlg.show()
    QApplication.processEvents()
    _sz = dlg.size()
    # 2.5x Qt's default width - Gerber import stage labels are longer
    # than the solve dialog's ("Rendering 16 Gerber layers…", etc.)
    # and Qt's auto-sized width clips them.
    dlg.setFixedSize(int(_sz.width() * 2.5), _sz.height())

    run = GerberImportRun(result, pseudo, parent=parent)
    updater = _SolveProgressUpdater(dlg, run, parent)
    # Keep the run reachable from its window while it's in flight.
    parent._gerber_run = run

    def _cleanup() -> None:
        try:
            updater.stop()
            updater.deleteLater()
        except Exception:
            pass
        try:
            dlg.canceled.disconnect()
        except (RuntimeError, TypeError):
            pass
        dlg.close()
        # close() only hides the parented dialog - delete it so imports
        # don't leak one QProgressDialog each.
        dlg.deleteLater()
        run.deleteLater()
        if getattr(parent, "_gerber_run", None) is run:
            parent._gerber_run = None

    def _on_ok(res) -> None:
        _cleanup()
        on_ok(res)

    def _on_fail(msg: str) -> None:
        _cleanup()
        QMessageBox.critical(
            parent, "Gerber import failed",
            f"Could not finish the Gerber import:\n\n{msg}")

    def _on_cancel() -> None:
        run.cancel()
        _cleanup()

    run.succeeded.connect(_on_ok)
    run.failed.connect(_on_fail)
    dlg.canceled.connect(_on_cancel)
    run.start()




def _finish_gerber_import(result, pseudo, progress_cb=None,
                          cancel_cb=None) -> tuple:
    """Heavy-lifting half of the Gerber import, safe to run on a worker
    thread. Takes the import window's :class:`GerberImportResult` plus an
    optional ``progress_cb(stage, substage)`` that receives stage label
    updates the GUI can show in a progress dialog.

    ``cancel_cb`` is an optional zero-arg predicate polled at each stage
    boundary; when it returns True the function raises
    :class:`_GerberImportCancelled` so a cancelled import unwinds
    cooperatively during the pure-Python packaging phase (which holds the
    GIL and can't be safely ``terminate()``d).

    Returns ``(stub_solution, metadata, loaded_project, project_file)``.
    """
    from fypa.gerber.extract import extract_gerber_project
    from fypa.gerber.loader import load_gerber_project
    from fypa.project_file import ProjectFile

    def _check_cancel():
        if cancel_cb is not None and cancel_cb():
            raise _GerberImportCancelled

    def _progress(stage=None, substage=None):
        _check_cancel()
        if progress_cb is None:
            return
        try:
            progress_cb(stage, substage)
        except Exception:
            pass

    _imp_log = logging.getLogger(__name__)
    _imp_t0 = time.monotonic()
    extracted, warns = extract_gerber_project(
        copper_files=result.copper_files,
        drill_files=result.drill_files,
        outline_file=result.outline_file,
        stackup=result.stackup,
        pseudo_prjpcb_path=pseudo,
        progress_cb=progress_cb,
    )
    _imp_log.info("Gerber import: extract_gerber_project took %.2fs",
                  time.monotonic() - _imp_t0)
    for w in warns:
        _imp_log.warning("Gerber import: %s", w)

    _progress(stage="Loading project structure…", substage="")
    _t = time.monotonic()
    loaded = load_gerber_project(extracted)
    _imp_log.info("Gerber import: load_gerber_project took %.2fs",
                  time.monotonic() - _t)
    _progress(stage="Building per-layer geometry…", substage="")
    _t = time.monotonic()
    stub_solution = _build_stub_lean_solution_from_loaded(loaded)
    _imp_log.info("Gerber import: build_stub_lean_solution took %.2fs",
                  time.monotonic() - _t)

    # all_copper records - the per-layer "all copper" overlay (second eye
    # icon on each physical layer in the side panel) is driven by this
    # metadata key. Without it, toggling the eye on a Gerber import would
    # have no geometry to draw. Each Gerber-derived geometry layer
    # becomes one record with the NO_NET sentinel as the net name -
    # exactly the format build_solve_metadata produces for the Altium
    # path.
    _progress(stage="Building all-copper outline records…", substage="")
    _t = time.monotonic()
    all_copper: list[dict] = []
    for L in loaded.geometry:
        if L.shape is None or L.shape.is_empty:
            continue
        polys = (list(L.shape.geoms)
                 if L.shape.geom_type == "MultiPolygon"
                 else [L.shape])
        ring_polys: list[dict] = []
        layer_hole_count = 0
        for poly in polys:
            ext = getattr(poly, "exterior", None)
            if ext is None or ext.is_empty:
                continue
            ext_arr = np.asarray(list(ext.coords), dtype=np.float32)
            if ext_arr.shape[0] < 2:
                continue
            holes_arr: list = []
            for hole in getattr(poly, "interiors", []):
                if hole.is_empty:
                    continue
                h_arr = np.asarray(list(hole.coords), dtype=np.float32)
                if h_arr.shape[0] >= 2:
                    holes_arr.append(h_arr)
            layer_hole_count += len(holes_arr)
            ring_polys.append({"exterior": ext_arr, "holes": holes_arr})
        _imp_log.info("Gerber all_copper: layer %d → %d polygon(s), %d hole-ring(s)",
                      int(L.layer_id), len(ring_polys), layer_hole_count)
        if not ring_polys:
            continue
        all_copper.append({
            "layer_id": int(L.layer_id),
            "net": "(none)",
            "polygons": ring_polys,
        })
    _imp_log.info("Gerber import: all_copper records built in %.2fs (%d layer(s))",
                  time.monotonic() - _t, len(all_copper))

    # Stackup rows - the viewer reads ``layer_id`` + ``name`` to map
    # physical-layer display names to the layer ids used by all_copper /
    # editor directives, and ``copper_thickness_mm`` +
    # ``dielectric_thickness_mm`` to position layers in 3D view. The
    # mil / oz / sheet_conductance / sheet_resistance derivatives are
    # what the Properties tab's stackup table renders; mirror the format
    # build_solve_metadata produces for the Altium path.
    stackup_rows: list[dict] = []
    for s in extracted.stackup:
        cu_mm = float(s.copper_thickness_mm)
        sheet_conductance = cu_mm * 5.95e4
        stackup_rows.append({
            "layer_id": int(s.layer_id),
            "name": s.name,
            "copper_thickness_mm": cu_mm,
            "copper_thickness_mil": cu_mm / 0.0254,
            "copper_thickness_oz": cu_mm / 0.0348,
            "dielectric_thickness_mm": float(s.dielectric_thickness_mm),
            "sheet_conductance_S": sheet_conductance,
            "sheet_resistance_milliohm_per_sq": (
                1000.0 / sheet_conductance if sheet_conductance > 0 else 0.0
            ),
            "is_plane": False,
            "plane_net_name": None,
            "next_layer_id": int(s.next_layer_id),
        })

    # primitives - what the viewer's click-to-select walks. Each
    # RawShapeBasedRegion from the extract becomes one record;
    # vias/pads/tracks/arcs/fills stay empty (Gerber doesn't have them
    # as distinct primitives - everything is a polygon by the time it
    # reaches us). Format matches what altium.loader.build_solve_metadata
    # produces for the Altium path so _primitives_index walks them the
    # same way.
    _progress(stage="Building primitives index…", substage="")
    _t = time.monotonic()
    sbr_records: list[dict] = []
    for i, rg in enumerate(extracted.shape_based_regions):
        outline_pts = [[float(v.pos.x), float(v.pos.y)] for v in rg.outline]
        sbr_records.append({
            "id": i,
            "kind": "shape_based_region",
            "layer_id": int(rg.layer_id),
            "net": "(none)",
            "outline": outline_pts,
            "holes": [[[float(p.x), float(p.y)] for p in h] for h in rg.holes],
            "arc_edge_count": 0,
            "kind_code": int(rg.kind),
            "is_polygon_outline": bool(rg.is_polygon_outline),
            "is_keepout": bool(rg.is_keepout),
            "is_board_cutout": bool(rg.is_board_cutout),
            "polygon_index": int(rg.polygon_index),
        })
    primitives = {
        "tracks": [], "arcs": [], "regions": [],
        "shape_based_regions": sbr_records, "fills": [],
    }
    _imp_log.info("Gerber import: sbr_records built in %.2fs (%d record(s))",
                  time.monotonic() - _t, len(sbr_records))

    # Via records - what the 2D via markers and the 3D via-cylinder pass
    # both iterate. Mirrors the shape build_solve_metadata
    # emits for the Altium path (loader.py), minus solve-only fields like
    # ``segments`` (no FEM has run yet). Empty ``segments`` is fine - the
    # rendering paths gate per-segment styling on the list being non-empty.
    vias_records: list[dict] = []
    for v in extracted.vias:
        vias_records.append({
            "x_mm": float(v.center.x),
            "y_mm": float(v.center.y),
            "net": "(none)",
            "diameter_mm": float(v.diameter_mm),
            "hole_diameter_mm": float(v.hole_diameter_mm),
            "layer_start": int(v.layer_start),
            "layer_end": int(v.layer_end),
            "unconnected_layer_ids": [
                int(i) for i in v.unconnected_layer_ids],
            "segments": [],
            "ipc4761_via_type": 0,
            "ipc4761_label": "-",
            "fill_material": "",
            "is_conductive_fill": False,
        })

    # Non-plated through holes (mounting / mechanical holes) - drawn as the
    # "Non Plated TH" Board Features overlay, never meshed. NonPlated drill
    # files contribute these (see _gerber_drill_to_vias).
    npth_records: list[dict] = [
        {"x_mm": float(h.center.x), "y_mm": float(h.center.y),
         "diameter_mm": float(h.diameter_mm)}
        for h in extracted.npth_holes
    ]

    metadata: dict = {
        "source_kind": "gerber",
        "prjpcb_path": str(pseudo),
        "pcbdoc_path": str(pseudo),
        "project_name": pseudo.stem,
        "all_copper": all_copper,
        "stackup": stackup_rows,
        "primitives": primitives,
        "vias": vias_records,
        "npth": npth_records,
        # Gerber imports have no through-hole pad records (the extract
        # docstring leaves pads/components empty); keep the key present so
        # viewer code that iterates ``metadata['pths']`` doesn't have to
        # special-case the source kind.
        "pths": [],
        # Help the Properties tab show something meaningful.
        "gerber_files": [str(p) for p in result.copper_files.values()],
        "drill_files": [str(p) for p in result.drill_files],
        "outline_file": (str(result.outline_file)
                         if result.outline_file else None),
        "board_outline": [[float(p.x), float(p.y)]
                          for p in extracted.board_outline],
    }

    pf = ProjectFile(
        source_kind="gerber",
        prjpcb_path=str(pseudo),
        pcbdoc_path=str(pseudo),
        gerber_files=[str(p) for p in result.copper_files.values()],
        drill_files=[str(p) for p in result.drill_files],
        outline_file=(str(result.outline_file)
                      if result.outline_file else None),
        layer_assignments={p.name: lid
                           for lid, p in result.copper_files.items()},
        gerber_stackup=[
            {
                "layer_id": s.layer_id,
                "name": s.name,
                "copper_thickness_mm": s.copper_thickness_mm,
                "dielectric_thickness_mm": s.dielectric_thickness_mm,
            }
            for s in result.stackup
        ],
    )
    _progress(stage="Opening viewer…", substage="")
    _imp_log.info("Gerber import: total _finish_gerber_import took %.2fs",
                  time.monotonic() - _imp_t0)
    return stub_solution, metadata, loaded, pf
