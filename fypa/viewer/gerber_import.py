"""Importing a Gerber fabrication package."""
from __future__ import annotations

import logging
import time
from pathlib import Path
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QFileDialog

from fypa.viewer.solve_worker import _build_stub_lean_solution_from_loaded
from fypa.viewer.theme import _file_dialog_options


class _GerberImportCancelled(Exception):
    """Signals that a Gerber import was cancelled cooperatively at a stage
    boundary. Raised from :func:`_finish_gerber_import` when the worker's
    ``cancel_cb`` reports an interruption request, and caught in
    :meth:`_GerberImportWorker.run` so the thread unwinds without emitting a
    result — mirroring :meth:`_SolveWorker._cancel_requested`. The packaging
    phase (all-copper / primitives records) is pure Python holding the GIL,
    so a hard ``terminate()`` there could deadlock the app; the cooperative
    checkpoint avoids that."""




class _GerberImportWorker(QThread):
    """Background worker that runs :func:`_finish_gerber_import` off the
    GUI thread so a fresh-import doesn't freeze the application.

    The picker + layer-mapping dialogs must run on the main thread (Qt
    modal widgets can't live on a worker), so the caller fans those out
    first and only hands the chosen inputs to the worker. The worker
    emits ``stage_changed`` / ``substage_changed`` to drive the progress
    dialog, then ``finished_ok`` with the import result tuple or
    ``failed`` with an error message.
    """

    stage_changed = Signal(str)
    substage_changed = Signal(str)
    total_stages = Signal(int)     # see _SolveWorker.total_stages
    finished_ok = Signal(object)   # the (stub_sol, metadata, loaded, pf) tuple
    failed = Signal(str)

    # The Gerber import always emits the same nine stages — four from
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
            # Cancelled at a stage boundary — the GUI already tore down its
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




def _pick_gerber_inputs(parent_window):
    """Run the Gerber file picker + layer/stackup dialogs on the main
    GUI thread (modal Qt widgets cannot run on a worker thread).

    Returns ``(result, pseudo_prjpcb_path)`` on success, ``None`` if the
    user cancelled at any point. The caller then hands ``result`` to a
    :class:`_GerberImportWorker` for the heavy off-thread work.
    """
    from fypa.gerber.import_ui import run_gerber_import_dialogs

    paths_str, _ = QFileDialog.getOpenFileNames(
        parent_window,
        "Pick Gerber + drill files to import",
        "",
        "Gerber / Excellon ("
        "*.gbr *.GBR *.gtl *.GTL *.gbl *.GBL *.g* *.G* "
        "*.cmp *.CMP *.sol *.SOL *.gko *.GKO *.gm1 *.GM1 "
        "*.gto *.GTO *.gbo *.GBO "
        "*.drl *.DRL *.xln *.XLN *.txt *.TXT *.tap *.TAP *.nc *.NC);;"
        "All files (*)",
        options=_file_dialog_options(),
    )
    if not paths_str:
        return None
    picked = [Path(p) for p in paths_str]
    result = run_gerber_import_dialogs(picked, parent=parent_window)
    if result is None:
        return None
    # Synthesise a pseudo-PrjPcb path next to the gerbers — the cache
    # key + project identity rely on having a stable Path, but no file
    # of that name needs to exist on disk.
    folder = picked[0].parent.resolve()
    pseudo = folder / f"{folder.name}.fypa-gerber"
    return result, pseudo




def _finish_gerber_import(result, pseudo, progress_cb=None,
                          cancel_cb=None) -> tuple:
    """Heavy-lifting half of the Gerber import, safe to run on a worker
    thread. Takes the result of :func:`_pick_gerber_inputs` plus an
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

    # all_copper records — the per-layer "all copper" overlay (second eye
    # icon on each physical layer in the side panel) is driven by this
    # metadata key. Without it, toggling the eye on a Gerber import would
    # have no geometry to draw. Each Gerber-derived geometry layer
    # becomes one record with the NO_NET sentinel as the net name —
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

    # Stackup rows — the viewer reads ``layer_id`` + ``name`` to map
    # physical-layer display names to the layer ids used by all_copper /
    # editor directives, and ``copper_thickness_mm`` +
    # ``dielectric_thickness_mm`` to position layers in 3D view. The
    # mil / oz / sheet_conductance / sheet_resistance derivatives are
    # what the Setup tab's stackup table renders; mirror the format
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

    # primitives — what the viewer's click-to-select walks. Each
    # RawShapeBasedRegion from the extract becomes one record;
    # vias/pads/tracks/arcs/fills stay empty (Gerber doesn't have them
    # as distinct primitives — everything is a polygon by the time it
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

    # Via records — what the 2D via markers and the 3D via-cylinder pass
    # both iterate. Mirrors the shape build_solve_metadata
    # emits for the Altium path (loader.py), minus solve-only fields like
    # ``segments`` (no FEM has run yet). Empty ``segments`` is fine — the
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
            "segments": [],
            "ipc4761_via_type": 0,
            "ipc4761_label": "—",
            "fill_material": "",
            "is_conductive_fill": False,
        })

    # Non-plated through holes (mounting / mechanical holes) — drawn as the
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
        # Help the Setup tab show something meaningful.
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




def _perform_gerber_import(parent_window) -> tuple | None:
    """Legacy synchronous helper kept for callers that still expect a
    blocking import. New code should use :func:`_pick_gerber_inputs`
    plus a :class:`_GerberImportWorker` so the GUI doesn't freeze.
    """
    picked = _pick_gerber_inputs(parent_window)
    if picked is None:
        return None
    result, pseudo = picked
    return _finish_gerber_import(result, pseudo)
