"""Post-load warnings: missing directives, mesh failures, open loops."""
from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from fypa.viewer.overlays import _overlay_circle_ring


def _maybe_warn_needs_directives(parent_win, solution) -> None:
    """Pop a one-time notice when ``solution`` is the editor-mode stub built
    for an Altium project that loaded without any SOURCE / REGULATOR
    directive. No-op for ordinary solved solutions and for Gerber stubs
    (which never carry the ``needs_directives`` sentinel).
    """
    si = getattr(solution, "solver_info", None)
    if not isinstance(si, dict) or not si.get("needs_directives"):
        return
    QMessageBox.information(
        parent_win,
        "No PDN settings found",
        "No predefined PDN parameters (SOURCE / REGULATOR) were found in "
        "this design, so there's nothing to solve yet.\n\n"
        "The design has loaded — switch on Edit to place sources / sinks "
        "manually, then press Resolve.",
    )




_MESH_FAILURE_MARKER_RADIUS_MM = 3.0


_MESH_FAILURE_RING_RADIUS_MM = 5.0


# Only trace the full failing polygon when it is a local feature — large
# pours would otherwise look like "the whole layer is highlighted".
_MESH_FAILURE_LOCAL_OUTLINE_MAX_SPAN_MM = 25.0




def _mesh_failure_marker_xy(rec: dict) -> tuple[float, float] | None:
    loc = rec.get("location_xy")
    if loc and len(loc) >= 2:
        return float(loc[0]), float(loc[1])
    ext = rec.get("exterior")
    if ext is None:
        return None
    try:
        n = len(ext)
    except TypeError:
        return None
    if n < 3:
        return None
    xs = [float(p[0]) for p in ext]
    ys = [float(p[1]) for p in ext]
    return sum(xs) / len(xs), sum(ys) / len(ys)




def _mesh_failure_outline_rings(rec: dict) -> list[list[tuple[float, float]]]:
    """Rings for the dashed GL overlay — local marker, not whole pours."""
    center = _mesh_failure_marker_xy(rec)
    if center is None:
        return []
    cx, cy = center
    rings: list[list[tuple[float, float]]] = []
    for radius in (_MESH_FAILURE_MARKER_RADIUS_MM, _MESH_FAILURE_RING_RADIUS_MM):
        ring = _overlay_circle_ring(cx, cy, radius, 32)
        rings.append([(float(x), float(y)) for x, y in ring])
    ext = rec.get("exterior")
    if ext is not None:
        try:
            n = len(ext)
        except TypeError:
            n = 0
        if n >= 3:
            xs = [float(p[0]) for p in ext]
            ys = [float(p[1]) for p in ext]
            span = max(max(xs) - min(xs), max(ys) - min(ys))
            if span <= _MESH_FAILURE_LOCAL_OUTLINE_MAX_SPAN_MM:
                rings.append([(float(x), float(y)) for x, y in ext])
    return rings




def _maybe_show_mesh_failures(parent_win, metadata) -> None:
    """Pop a notice and highlight bad copper when FEM meshing failed."""
    if not isinstance(metadata, dict):
        return
    failures = metadata.get("mesh_failures") or []
    if not metadata.get("mesh_failed") and not failures:
        return
    summaries = []
    for rec in failures[:3]:
        text = rec.get("summary") or ""
        if text:
            summaries.append(text)
    body = "\n\n".join(summaries) if summaries else (
        "One or more copper polygons could not be meshed."
    )
    if len(failures) > 3:
        body += f"\n\n… and {len(failures) - 3} more."
    box = QMessageBox(parent_win)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("Meshing failed")
    if failures:
        box.setText(
            "Look for the yellow ring and red disc on the board — that marks "
            "where the mesh failed. (The red Top-layer copper overlay is "
            "normal; it is not the error marker.) Fix the geometry there in "
            "Altium, then press ↻ Solve."
        )
    else:
        # Nothing survived _build_stub_record, so there is no marker to look
        # for — say that rather than send the user hunting for one.
        box.setText(
            "FEM meshing failed, but the offending copper could not be "
            "localised, so there is no marker on the board. This usually "
            "means a zero-area sliver or a self-intersecting polygon. Check "
            "the log for the failing layer, fix the geometry in Altium, then "
            "press ↻ Solve."
        )
    if body:
        box.setDetailedText(body)
    box.exec()




def _phys_name_for_layer_id(viewer, layer_id: int) -> str | None:
    """Map a stackup layer_id to the physical-layer name in the side panel."""
    if layer_id < 0:
        return None
    for phys, plid in getattr(viewer, "_phys_name_to_layer_id", {}).items():
        if plid == layer_id:
            return phys
    return None




def _primary_mesh_failure_layer_id(failures: list) -> int | None:
    for rec in failures:
        lid = rec.get("layer_id")
        if isinstance(lid, int) and lid >= 0:
            return lid
    return None




def _activate_mesh_failure_layer(viewer) -> bool:
    """Turn on and select the physical layer that contains the mesh failure."""
    metadata = getattr(viewer, "metadata", None) or {}
    failures = metadata.get("mesh_failures") or []
    if not failures:
        return False
    layer_id = _primary_mesh_failure_layer_id(failures)
    if layer_id is None:
        return False
    phys = _phys_name_for_layer_id(viewer, layer_id)
    if phys is None:
        return False

    changed = False
    for nm, eye in getattr(viewer, "_layer_eye_buttons", []):
        if nm == phys and not eye.isVisibleState():
            eye.setVisibleState(True, emit=False)
            changed = True
            break
    for nm, eye2 in getattr(viewer, "_layer_eye2_buttons", []):
        if nm == phys and not eye2.isVisibleState():
            eye2.setVisibleState(True, emit=False)
            changed = True
            break
    if getattr(viewer, "_selected_layer", None) != phys:
        viewer._selected_layer = phys
        if hasattr(viewer, "_apply_layer_selection_highlight"):
            viewer._apply_layer_selection_highlight()
        changed = True
    if changed:
        if hasattr(viewer, "_sync_all_layers_eye"):
            viewer._sync_all_layers_eye()
        if hasattr(viewer, "_sync_all_layers_eye2"):
            viewer._sync_all_layers_eye2()
    return changed




def _apply_mesh_failure_highlights(viewer) -> None:
    """Zoom to and outline the mesh-failure marker(s)."""
    if viewer is None:
        return
    metadata = getattr(viewer, "metadata", None) or {}
    failures = metadata.get("mesh_failures") or []
    if not failures:
        return
    _activate_mesh_failure_layer(viewer)
    rings: list[list[tuple[float, float]]] = []
    focus_xy: tuple[float, float] | None = None
    for rec in failures:
        rings.extend(_mesh_failure_outline_rings(rec))
        if focus_xy is None:
            focus_xy = _mesh_failure_marker_xy(rec)
    gl = getattr(viewer, "_gl_viewer", None)
    if gl is not None and rings:
        gl.set_mesh_failure_outline(rings)
    if focus_xy is not None and gl is not None:
        gl.set_view_center_scale(focus_xy[0], focus_xy[1], 0.08)




def _maybe_show_annotation_errors(parent_win, metadata) -> None:
    """Pop a notice listing PDN directives that could not be resolved and were
    skipped.

    Fired after a viewer opens with ``metadata['annotation_errors']``. These
    no longer block the solve — the offending directive (a mistyped net, a
    missing pad) is dropped and every valid rail is still solved — so the
    notice reports what was left out rather than what blocked the run.
    """
    if not isinstance(metadata, dict):
        return
    errors = metadata.get("annotation_errors") or []
    if not errors:
        return
    max_show = 12
    body = "\n".join(f"  • {e}" for e in errors[:max_show])
    if len(errors) > max_show:
        body += f"\n  … and {len(errors) - max_show} more"
    QMessageBox.warning(
        parent_win,
        "Some directives were skipped",
        "These PDN directives could not be resolved and were skipped — the "
        "other valid rails were still solved:\n\n"
        f"{body}\n\n"
        "Fix them (usually a net-name typo) to include these directives in "
        "the analysis. Details also in Setup → Annotation log.",
    )




def _maybe_warn_open_loop_rails(parent_win, metadata) -> None:
    """Pop a notice listing rails that were NOT solved because they hold only
    sources or only sinks (no current can flow). Fired on load and after
    Resolve. The directives' markers are kept on the design — the rail is
    simply left out of the FEM. No-op when there are no such rails.

    Reads ``metadata['open_loop_rails']`` (a list of human-readable per-rail
    messages built by :func:`fypa.altium.loader._flag_open_loop_rails`), which
    round-trips through the solve cache, so this also fires on a cache hit.
    """
    if not isinstance(metadata, dict):
        return
    rails = metadata.get("open_loop_rails") or []
    if not rails:
        return
    QMessageBox.warning(
        parent_win,
        "Some rails won't be solved",
        "These rails contain only one type of element (only sources or only "
        "sinks), so no current can flow and they were left out of the "
        "solve:\n\n"
        + "\n".join(f"  • {r}" for r in rails)
        + "\n\nThe markers are kept on the design. Add the missing element "
        "(a sink for a source-only rail, or a source for a sink-only rail) "
        "and press Resolve to solve the rail.",
    )




def _maybe_warn_connectivity_breaks(parent_win, metadata) -> None:
    """Pop a notice listing nets whose SOURCE and SINK markers sit on copper
    that isn't electrically connected. Unlike the open-loop-rail case the rail
    IS solved, but the result is unreliable: no copper path closes the current
    loop, so the sink reads ~0 V and the FEM injects a large ground-balancing
    current. Fired on load and after Resolve. No-op when there are none.

    Reads ``metadata['connectivity_breaks']`` (built by
    :func:`fypa.altium.loader.build_problem`), which round-trips through the
    solve cache, so this also fires on a cache hit.
    """
    if not isinstance(metadata, dict):
        return
    breaks = metadata.get("connectivity_breaks") or []
    if not breaks:
        return
    QMessageBox.warning(
        parent_win,
        "Source and sink on disconnected copper",
        "On these nets the source and sink markers are NOT on the same "
        "connected copper, so no current can flow between them. The rail was "
        "solved but the result is unreliable (the sink reads about 0 V):\n\n"
        + "\n".join(f"  • {r}" for r in breaks)
        + "\n\nMove the markers onto the same copper island, or connect the "
        "islands (a via or a SERIES link), then press Resolve.",
    )




def _maybe_warn_unannotated_bridges(parent_win, metadata) -> None:
    """Pop a one-time, non-blocking notice listing parts that conduct between
    a solved rail and copper the FEM leaves out.

    The solve only meshes nets a PDN directive touches. A ferrite, fuse,
    shunt or connector joining such a rail to an un-annotated net is a real
    parallel current path the model cannot see, so the reported return-path
    resistance is an over-estimate. Nothing is bridged automatically here —
    only the user knows the part's DC resistance (a ferrite's "0 Ω" is
    20-200 mΩ of DCR) — so this is advisory.

    Information, not a warning: the solve is still valid for the copper it
    does model, and on many boards these parts genuinely are open at DC.
    Reads ``metadata['unannotated_bridges']``, which round-trips through the
    solve cache, so it also fires on a cache hit.
    """
    if not isinstance(metadata, dict):
        return
    bridges = metadata.get("unannotated_bridges") or []
    if not bridges:
        return
    shown = list(bridges[:8])
    more = len(bridges) - len(shown)
    bullets = "\n\n".join(f"  \u2022 {b}" for b in shown)
    body = (
        "These parts connect a solved rail to copper that no PDN directive "
        "touches, so that copper is left out of the simulation and any "
        "current it really carries is missing (the return-path resistance "
        "reads high):\n\n"
        + bullets
    )
    if more > 0:
        body += f"\n\n  \u2026 and {more} more (see the Messages tab)."
    body += (
        "\n\nThis is advisory \u2014 nothing was changed. If a part conducts "
        "at DC, annotate it in Altium with PDN_ROLE=SERIES and PDN_R set to "
        "its real DC resistance, then re-import. Genuine 0 \u03a9 links and "
        "Net Ties are already bridged automatically."
    )
    QMessageBox.information(parent_win, "Unannotated net bridges", body)
