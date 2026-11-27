"""After a load: the setup chooser for a design with no sources, and the
mesh-failure marker on the Board. The design's setup problems themselves
are listed in the Design issues panel (fypa.viewer.issues_panel)."""
from __future__ import annotations

from fypa.viewer.overlays import _overlay_circle_ring


def _maybe_warn_needs_directives(parent_win, solution) -> None:
    """Offer the setup chooser when ``solution`` is the editor-mode stub
    built for an Altium project that loaded without any SOURCE / REGULATOR
    directive. No-op for ordinary solved solutions and for Gerber stubs
    (which never carry the ``needs_directives`` sentinel).

    Callers run it after the other post-load notices: the route the user
    picks can open a wizard, which should not end up underneath them. With
    the chooser switched off, a status-bar line says where to find it.
    """
    si = getattr(solution, "solver_info", None)
    if not isinstance(si, dict) or not si.get("needs_directives"):
        return
    # Only a viewer can run the routes (the launcher passes itself when the
    # viewer failed to open).
    if not hasattr(parent_win, "_topology_run_autosetup"):
        return
    from fypa.viewer.prefs import load_show_setup_chooser
    from fypa.viewer.setup_chooser import open_setup_chooser

    if load_show_setup_chooser():
        open_setup_chooser(parent_win, on_load=True)
    else:
        parent_win.statusBar().showMessage(
            "No sources or sinks defined yet - Help > Set up sources and "
            "sinks… shows the ways to add them.", 20000)



_MESH_FAILURE_MARKER_RADIUS_MM = 3.0


_MESH_FAILURE_RING_RADIUS_MM = 5.0


# Only trace the full failing polygon when it is a local feature - large
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
    """Rings for the dashed GL overlay - local marker, not whole pours."""
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
