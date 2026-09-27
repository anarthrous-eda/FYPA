"""Heatmap rendering, current arrows and the side-panel scale controller."""
from __future__ import annotations

import logging
import math
import time
import numpy as np
import shapely.prepared as _sp
from fypa.rail_groups import resolve_rail_member_nets
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog
from matplotlib.tri import Triangulation

from fypa.viewer.diagnostics import _apply_mesh_failure_highlights
from fypa.viewer.display import (
    _COPPER_ROI_MODE,
    _copper_roi_per_vertex,
    _VALUE_MAP_MODE_ENTRY,
    _LOG_ELIGIBLE_MODES,
    _LOG_SCALE_DECADES,
    _MODES,
    _slider_data_max,
    _SPIKE_PRONE_MODES,
    _VIA_CURRENT_MODE,
    _VOLTAGE_DROP_MODE,
)
from fypa.viewer.mesh_geometry import _extrude_to_prism, _shape_outline_segments
from fypa.viewer.widgets import _qt_widget_alive


class _RenderMixin:
    """Heatmap rendering, current arrows and the side-panel scale controller."""

    # --- Rendering -----------------------------------------------------------

    def _current_selection(self) -> tuple[list[str], list[str], str]:
        """Current heatmap selection: ``(visible_layers, visible_rails, mode)``."""
        layers = self._visible_layers()
        rails = self._visible_rails()
        mode = self.mode_combo.currentText()
        if getattr(self, "_roi_value_map", False):
            mode = _COPPER_ROI_MODE  # the Fixes tab's value map
        return layers, rails, mode

    def _mode_derive_fn(self, mode: str):
        for label, unit, fn in (*_MODES, _VALUE_MAP_MODE_ENTRY):
            if label == mode:
                return label, unit, fn
        raise KeyError(mode)

    def _effective_rail_members(self, rail_names) -> list[str]:
        """The list of net names whose copper should appear when the given
        rails are selected, honouring the "Show only rail net" checkbox.

        Off → union of full rail groups (e.g. ``[+3V3, 3V3_SW]`` for +3V3).
        On  → just each selected rail's primary name (bridged nets hidden).

        Accepts either a single rail name (legacy single-rail call sites)
        or a list of rail names (multi-rail control). An empty list / empty
        string returns ``[]`` — caller code treats that as "no rail filter
        currently selected", which in practice means the heatmap mesh has
        no nets to draw.
        """
        if isinstance(rail_names, str):
            rail_names = [rail_names] if rail_names else []
        if not rail_names:
            return []
        subnet_visible = {
            rail: {
                net: eye.isVisibleState()
                for net, eye in nets.items()
                if _qt_widget_alive(eye)
            }
            for rail, nets in self._subnet_eye_buttons.items()
        }
        return resolve_rail_member_nets(
            list(rail_names),
            self._rail_to_members,
            subnet_visible,
            rail_only=self.rail_only_box.isChecked(),
        )

    # Power-density thresholds for classifying a solved mesh as "no current".
    # A dead-end island's current is ~0 (numerical noise), so its peak power
    # density sits orders of magnitude below the board's busiest copper. Flag
    # a mesh when its peak |power density| is at or below a small fraction of
    # the global peak (with an absolute floor for the all-quiet case) — this
    # cleanly catches the dead-end case without ever greying loaded copper.
    _NO_CURRENT_PD_REL: float = 1e-4
    _NO_CURRENT_PD_ABS: float = 1e-12

    def _no_current_mesh_set(self) -> set[tuple[int, int]]:
        """Return (and cache) the set of ``(layer_index, mesh_index)`` whose
        solved mesh carries no current — a dead-end copper island tied to the
        rest of its net by a single via, so KCL forces ~zero current through
        it and it sits at a uniform potential.

        Classified from each mesh's peak power density relative to the
        board-wide peak (see ``_NO_CURRENT_PD_*``). Used by the "Grey no
        current copper" toggle to grey these the same way it greys
        FEM-excluded stub copper. A mesh with no power-density data is left
        unflagged (conservative — never grey copper we can't measure)."""
        if self._no_current_meshes is not None:
            return self._no_current_meshes
        result: set[tuple[int, int]] = set()
        sol = getattr(self, "solution", None)
        if sol is None:
            self._no_current_meshes = result
            return result
        peaks: dict[tuple[int, int], float] = {}
        global_max = 0.0
        for li, ls in enumerate(sol.layer_solutions):
            pds = getattr(ls, "power_densities", None) or []
            for mi, pd in enumerate(pds):
                arr = np.asarray(pd, dtype=np.float64)
                if arr.size == 0:
                    continue
                pk = float(np.max(np.abs(arr)))
                if not np.isfinite(pk):
                    continue
                peaks[(li, mi)] = pk
                if pk > global_max:
                    global_max = pk
        threshold = max(self._NO_CURRENT_PD_ABS,
                        self._NO_CURRENT_PD_REL * global_max)
        for key, pk in peaks.items():
            if pk <= threshold:
                result.add(key)
        self._no_current_meshes = result
        return result

    def _source_return_offsets(self) -> dict[str, float]:
        """Per-net voltage offset = absolute potential at that net's SOURCE
        return (N-terminal) pin, sampled from the full solution.

        Voltage mode subtracts this per net so each net's heatmap reads its
        true differential (``<=`` rail voltage) instead of an absolute
        potential that floats a fraction of a mV ABOVE the rail. The float
        is real: a directive ``VoltageSource`` pins only the *difference*
        across its two pins, and the single global 0 V datum lands at the
        board's lowest GND node — so a source's own return pin sits slightly
        above 0 and drags its + net's absolute reading above the rail.
        Referencing each net to its own source return removes that datum
        offset.

        Keyed by net name. A net with no SOURCE (e.g. GND) gets no entry and
        is left at absolute potential. When a net has several sources the
        one whose + pin sits highest wins (the rail's defining source),
        mirroring the Voltage Drop anchor choice. Cached for the viewer's
        lifetime — it depends only on the (immutable) solution.
        """
        cached = getattr(self, "_src_return_offset_cache", None)
        if cached is not None:
            return cached
        from scipy.spatial import cKDTree

        offsets: dict[str, float] = {}
        meta = self.metadata or {}
        sol = getattr(self, "solution", None)
        if sol is None or not meta.get("directives"):
            self._src_return_offset_cache = offsets
            return offsets

        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        # Per-(phys, net) lazily-built (kdtree, potentials) over the slab's
        # non-orphan vertices — orphans are pinned to V=0 and would shadow
        # the real pin value. 0.01 mm match tol matches _compute_node_report.
        tree_cache: dict[tuple[str, str], tuple] = {}
        _TOL_MM = 0.01

        def _sample(layer_id, net: str, x, y) -> float | None:
            phys = id_to_phys.get(layer_id)
            if phys is None or x is None or y is None or not net:
                return None
            key = (phys, net)
            if key not in tree_cache:
                li = self._index_by_pair.get(key)
                tree = vs = None
                if li is not None:
                    ls = sol.layer_solutions[li]
                    xs_p, ys_p, vs_p = [], [], []
                    for xys, tris_local, pot in zip(
                        ls.vertex_xys, ls.triangles, ls.potentials,
                    ):
                        xys = np.asarray(xys)
                        tris_local = np.asarray(tris_local)
                        if xys.shape[0] == 0 or tris_local.size == 0:
                            continue
                        used = np.unique(tris_local.ravel())
                        xs_p.append(xys[used, 0])
                        ys_p.append(xys[used, 1])
                        vs_p.append(np.asarray(pot)[used])
                    if xs_p:
                        pts = np.column_stack(
                            [np.concatenate(xs_p), np.concatenate(ys_p)])
                        tree = cKDTree(pts)
                        vs = np.concatenate(vs_p)
                tree_cache[key] = (tree, vs)
            tree, vs = tree_cache[key]
            if tree is None:
                return None
            dist, idx = tree.query((float(x), float(y)))
            if dist > _TOL_MM:
                return None
            v = float(vs[idx])
            return v if np.isfinite(v) else None

        # net -> (ranking + pin voltage, return-pin offset).
        best: dict[str, tuple[float, float]] = {}
        for d in meta.get("directives", []):
            if d.get("role") != "SOURCE":
                continue
            terms = d.get("terminals") or {}
            p_pins = (terms.get("P") or {}).get("pins", [])
            n_pins = (terms.get("N") or {}).get("pins", [])
            if not p_pins or not n_pins:
                continue
            # The return-pin (N) absolute potential is the offset; take the
            # first N pin that samples cleanly.
            v_return = None
            for n_pin in n_pins:
                v_return = _sample(n_pin.get("layer_id"), n_pin.get("net", ""),
                                   n_pin.get("x_mm"), n_pin.get("y_mm"))
                if v_return is not None:
                    break
            if v_return is None:
                continue
            for p_pin in p_pins:
                net = p_pin.get("net", "")
                if not net:
                    continue
                v_plus = _sample(p_pin.get("layer_id"), net,
                                 p_pin.get("x_mm"), p_pin.get("y_mm"))
                rank = v_plus if v_plus is not None else float("-inf")
                if net not in best or rank > best[net][0]:
                    best[net] = (rank, v_return)

        offsets = {net: off for net, (_rank, off) in best.items()}
        self._src_return_offset_cache = offsets
        return offsets

    def _build_rail_arrays(
        self, phys_list: list[str], rail_names: list[str], derive_fn,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray,
               np.ndarray, list[dict], np.ndarray]:
        """Combine every per-(layer, net) padne Layer for the listed physical
        layers + the selected rail groups' nets into one big mesh batch for
        the GPU, and ALSO build per-(physical_layer, net) CPU-side
        interpolators for the hover probe and Voltage Drop reference.

        Drawing order is BOTTOM-to-TOP of the stackup, so the topmost
        checked layer is rendered last and sits visually on top of any
        lower layers it overlaps with.

        Returns ``(xs, ys, zs, vs, triangles, layer_probes)``. ``zs`` is
        per-vertex z (pre-exaggeration mm; the GLMeshViewer applies its
        own vertical scaling in 3D mode). In 2D mode the GL viewer ignores
        z, but we still build the array so the data path stays uniform.
        ``layer_probes`` is a list of dicts (one per visible (phys, net))
        with keys ``physical``, ``net``, ``layer_id``, ``triangulation``,
        ``interpolator``, ``values`` (numpy float64), ``prepared_shape``.
        The list is in TOP-FIRST order so the probe can walk it and
        report the topmost layer whose copper sits under the cursor.
        """
        members = self._effective_rail_members(rail_names)
        # BOTTOM-first for GPU draw order (topmost rendered last → on top).
        # A "selected" physical layer is forced to the very end of the
        # batch so it paints above every other layer regardless of stackup
        # rank — that's the whole point of the layer-row selection.
        sel = self._selected_layer
        phys_draw_order = sorted(
            phys_list,
            key=lambda p: (
                0 if p == sel else 1,
                self._phys_stackup_rank.get(p, 1 << 30),
            ),
            reverse=True,
        )
        xs_parts: list[np.ndarray] = []
        ys_parts: list[np.ndarray] = []
        zs_parts: list[np.ndarray] = []
        vs_parts: list[np.ndarray] = []
        tris_parts: list[np.ndarray] = []
        # Per-vertex "no current" mask (0/1), parallel to xs — fed to the
        # mesh shader's neutral attribute so the "Grey no current copper"
        # toggle can grey dead-end islands. Each piece is mesh-uniform.
        nc_parts: list[np.ndarray] = []
        # layer_probes is ordered TOP-FIRST so the hover probe naturally
        # reports the visually-topmost layer when copper overlaps.
        layer_probes: list[dict] = []
        # In 3D mode extrude each layer's flat mesh into a thin prism so
        # the copper has visible thickness from oblique angles. 2D mode
        # keeps the flat mesh as-is (no perf hit on the common path).
        extrude_3d = (self.view_3d_box.isChecked()
                      and self._COPPER_THICKNESS_MM > 0.0)
        # Geometry-batch cache signature. The concatenated coords / z /
        # triangles / no-current mask depend ONLY on the draw order, the rail
        # members, the extrude flag + copper thickness, and each layer's
        # render-z (which folds in the selected-layer 2D lift and the 2D/3D
        # mode). Per-vertex VALUES are not part of this — they're rebuilt per
        # mode below. ``id(self.solution)`` scopes the cache to the current
        # solve (also cleared on re-solve via _clear_solution_derived_caches).
        # On a hit we reuse the exact same array objects so _render_impl's
        # set_mesh identity check fires and skips the multi-million-vertex GPU
        # re-upload on geometry-preserving re-renders (mode switch, outline /
        # rail-only toggle, colour-scale change, src-return toggle).
        geom_sig = (
            id(self.solution),
            tuple(phys_draw_order),
            tuple(members),
            extrude_3d,
            self._COPPER_THICKNESS_MM,
            tuple(self._layer_render_z(p) for p in phys_draw_order),
        )
        cached_geom = self._rail_geom_cache
        reuse_geom = cached_geom is not None and cached_geom[0] == geom_sig
        offset = 0
        for phys in phys_draw_order:
            phys_z = self._layer_render_z(phys)
            for net in members:
                layer_index = self._index_by_pair.get((phys, net))
                if layer_index is None:
                    continue
                # Per-(phys, net) pump — this loop is the bulk of
                # _build_rail_arrays cost on multi-rail, multi-layer
                # selections. The throttle keeps the overhead negligible.
                self._pump_busy_ui()
                entry = self._layer_arrays(layer_index, derive_fn)
                n_in = entry["xs"].size
                if n_in == 0 or entry["tris"].size == 0:
                    continue
                # Per-mode values are always rebuilt (cheap). The prism
                # extrusion duplicates each vertex top↔bottom, so both the
                # value array and the vertex count double to stay length-
                # matched to the (possibly cached) geometry batch.
                if extrude_3d:
                    lvs = np.concatenate([entry["vs"], entry["vs"]])
                    n = 2 * n_in
                else:
                    lvs = entry["vs"]
                    n = n_in
                vs_parts.append(lvs)
                # Geometry (coords / z / triangles / no-current mask) is mode-
                # independent — rebuild it only on a cache miss. On a hit the
                # concatenated batch is reused wholesale below, so the loop
                # skips the per-layer prism extrusion and index re-basing too.
                if not reuse_geom:
                    if extrude_3d:
                        lxs, lys, lzs, _lvs, ltris = _extrude_to_prism(
                            entry["xs"], entry["ys"], entry["vs"],
                            entry["tris"],
                            z_center=phys_z,
                            thickness=self._COPPER_THICKNESS_MM,
                        )
                    else:
                        lxs = entry["xs"]
                        lys = entry["ys"]
                        lzs = np.full(n_in, phys_z, dtype=np.float64)
                        ltris = entry["tris"]
                    xs_parts.append(lxs)
                    ys_parts.append(lys)
                    zs_parts.append(lzs)
                    # No-current mask for this block, aligned to lxs. The prism
                    # extrusion duplicates the vertex set top↔bottom (and adds
                    # no new vertices — side walls reuse those indices), so the
                    # mask is duplicated the same way to stay length-matched.
                    nc_block = entry["no_current_mask"]
                    if extrude_3d:
                        nc_block = np.concatenate([nc_block, nc_block])
                    nc_parts.append(nc_block)
                    # Vectorised offset add — re-base local indices into the
                    # combined GPU batch.
                    tris_parts.append(ltris + offset)
                    offset += n
                layer_probes.append({
                    "physical": phys,
                    "net": net,
                    "layer_id": self._phys_name_to_layer_id.get(phys),
                    "layer_index": layer_index,
                    # Vertex count this (phys, net) contributed to the
                    # combined mesh — used to build the per-vertex alpha
                    # array (see :meth:`_combined_mesh_alpha_array`),
                    # which folds in both the per-layer Transparency
                    # setting and the editor-mode connectivity dim.
                    "n_vertices": n,
                    "triangulation": entry["triangulation"],
                    "interpolator": entry["interpolator"],
                    # Cache key stored for lazy interpolator writeback — when
                    # the hover probe builds the interpolator on first cursor
                    # move, it writes back to the cache entry so subsequent
                    # renders of the same (layer, mode) reuse it.
                    "_cache_key": self._layer_cache_key(layer_index,
                                                        derive_fn),
                    "values": entry["vs"],
                    "prepared_shape": entry["prepared_shape"],
                    "outline_segments": entry["outline_segments"],
                    "z": phys_z,
                })
        # Reverse to put topmost layer first in the probe walk order.
        layer_probes.reverse()

        # Values are always freshly concatenated (they track the active mode).
        vs = (np.concatenate(vs_parts) if vs_parts
              else np.empty(0, dtype=np.float64))

        if reuse_geom:
            # Same geometry as the last build — hand back the identical array
            # objects so the set_mesh identity check in _render_impl skips the
            # GPU re-upload. vs (above) still aligns: the loop visited the
            # exact same (phys, net) blocks in the same order and extrude mode.
            xs, ys, zs, tris, no_current = cached_geom[1]
            if vs.size != xs.size:
                # Values/geometry drift (e.g. stale cache) — rebuild geometry.
                reuse_geom = False
                self._rail_geom_cache = None
                return self._build_rail_arrays(
                    phys_list, rail_names, derive_fn,
                )
        else:
            if xs_parts:
                xs = np.concatenate(xs_parts)
                ys = np.concatenate(ys_parts)
                zs = np.concatenate(zs_parts)
                tris = np.concatenate(tris_parts, axis=0)
                no_current = np.concatenate(nc_parts)
            else:
                xs = np.empty(0, dtype=np.float64)
                ys = np.empty(0, dtype=np.float64)
                zs = np.empty(0, dtype=np.float64)
                tris = np.empty((0, 3), dtype=np.int32)
                no_current = np.empty(0, dtype=np.float32)
            self._rail_geom_cache = (geom_sig, (xs, ys, zs, tris, no_current))
        return xs, ys, zs, vs, tris, layer_probes, no_current

    def _layer_cache_key(self, layer_index: int, derive_fn) -> tuple:
        """Key of one layer's per-mode values in ``_layer_cache``. The Copper
        ROI mode's values also depend on the selected target load."""
        if derive_fn is _copper_roi_per_vertex:
            return (layer_index, id(derive_fn), getattr(self, "_roi_key", None))
        return (layer_index, id(derive_fn))

    def _layer_arrays(self, layer_index: int, derive_fn) -> dict:
        """Assemble (and cache) per-layer arrays + Triangulation +
        _FastTriSampler + prepared shapely shape for one
        (layer_index, derive_fn) pair.

        First call walks the LayerSolution meshes once and builds
        everything; subsequent calls are dict lookups. Solution data is
        immutable so the cache lives for the session.

        Keying on ``id(derive_fn)`` (rather than the mode label) means
        modes that share a derive function — e.g. 'Voltage' and
        'Voltage Drop', which both use ``_voltage_per_vertex`` (Voltage
        Drop's shift is applied later, downstream in ``_render``) —
        share a single cache entry.
        """
        key = self._layer_cache_key(layer_index, derive_fn)
        cached = self._layer_cache.get(key)
        if cached is not None:
            return cached

        # Mode-independent geometry (coords / triangulation / shape / masks),
        # built and cached once per layer. This holds the per-kept-mesh
        # ``(tris_local, pot, pd, n)`` tuples so the values below are computed
        # over the EXACT same kept-mesh set and order — vs stays aligned to xs.
        geom = self._layer_geometry(layer_index)
        conductance = geom["_conductance"]
        if derive_fn is _copper_roi_per_vertex:
            vs_parts = self._roi_vertex_values(layer_index, geom)
        else:
            vs_parts = [
                np.asarray(derive_fn(tris_local, pot, pd, conductance, n),
                           dtype=np.float64)
                for (tris_local, pot, pd, n) in geom["_kept"]
            ]
        vs = (np.concatenate(vs_parts) if vs_parts
              else np.empty(0, dtype=np.float64))

        entry = {k: v for k, v in geom.items() if not k.startswith("_")}
        entry["vs"] = vs
        # The voltage sampler (_FastTriSampler) is built lazily on first cursor
        # hover via _ensure_interpolator() and written back to THIS (layer,
        # mode) entry — it interpolates the mode's values, so it's per-mode.
        entry["interpolator"] = None
        self._layer_cache[key] = entry
        return entry

    def _layer_geometry(self, layer_index: int) -> dict:
        """Assemble (and cache by ``layer_index``) the mode-independent
        geometry for one layer: concatenated vertex coords, re-based triangle
        indices, the Triangulation, prepared shape, outline segments, the
        no-current mask, and the per-kept-mesh data needed to recompute
        per-mode values in step. Shared across all heatmap modes."""
        cached = self._layer_geom_cache.get(layer_index)
        if cached is not None:
            return cached

        ls = self.solution.layer_solutions[layer_index]
        layer = self.solution.problem.layers[layer_index]
        # Lean format: per-mesh-component numpy arrays — no half-edge
        # iteration, no per-vertex Python attribute access.
        xys_meshes = ls.vertex_xys
        tris_meshes = ls.triangles
        pots_meshes = ls.potentials
        pds_meshes = ls.power_densities

        mxs_parts: list[np.ndarray] = []
        mys_parts: list[np.ndarray] = []
        mtris_parts: list[np.ndarray] = []
        # Per-vertex no-current mask (0/1), parallel to xs.
        mnc_parts: list[np.ndarray] = []
        # Per-kept-mesh (tris_local, pot, pd, n) for the mode-specific value
        # pass — keeps vs aligned to xs with the identical skip logic.
        kept: list[tuple[np.ndarray, np.ndarray, np.ndarray, int]] = []
        kept_mesh_idx: list[int] = []
        nc_set = self._no_current_mesh_set()
        offset = 0
        for mesh_i, (xys, tris_local, pot, pd) in enumerate(zip(
            xys_meshes, tris_meshes, pots_meshes, pds_meshes,
        )):
            n = xys.shape[0]
            if n < 3 or tris_local.size == 0:
                continue
            mxs_parts.append(xys[:, 0])
            mys_parts.append(xys[:, 1])
            mnc_parts.append(np.full(
                n, 1.0 if (layer_index, mesh_i) in nc_set else 0.0,
                dtype=np.float32,
            ))
            kept.append((tris_local, pot, pd, n))
            kept_mesh_idx.append(mesh_i)
            # Re-base local indices into the per-layer combined batch.
            mtris_parts.append(tris_local + offset)
            offset += n

        if mxs_parts:
            xs = np.concatenate(mxs_parts)
            ys = np.concatenate(mys_parts)
            no_current_mask = np.concatenate(mnc_parts)
        else:
            xs = np.empty(0, dtype=np.float64)
            ys = np.empty(0, dtype=np.float64)
            no_current_mask = np.empty(0, dtype=np.float32)
        if mtris_parts:
            tris = np.concatenate(mtris_parts, axis=0)
        else:
            tris = np.empty((0, 3), dtype=np.int32)

        triangulation = (Triangulation(xs, ys, tris)
                         if xs.size >= 3 and tris.size > 0 else None)

        shape = layer.shape
        prepared_shape = (_sp.prep(shape)
                          if shape is not None and not shape.is_empty else None)
        outline_segments = _shape_outline_segments(shape)

        # Vertex indices that actually appear in a triangle. The padne
        # solver's orphan-vertex guards pin "no-triangle" vertices to
        # V=0 to keep the linear system non-singular; they're invisible
        # in the heatmap but otherwise skew vs.min()/max(). Callers compute
        # stats via vs[used_indices].
        if tris.size > 0:
            used_indices = np.unique(tris.ravel().astype(np.int64))
        else:
            used_indices = np.empty(0, dtype=np.int64)

        geom = {
            "xs": xs,
            "ys": ys,
            "tris": tris,
            "triangulation": triangulation,
            "prepared_shape": prepared_shape,
            # Outline segments: (N, 2) float32 in GL_LINES pairs, pre-built so
            # toggling the outline overlay is just a buffer upload.
            "outline_segments": outline_segments,
            "used_indices": used_indices,
            # Per-vertex 0/1 mask (parallel to xs) for "Grey no current copper".
            "no_current_mask": no_current_mask,
            "_kept": kept,
            # Source mesh index of each _kept entry (for per-mesh fields such
            # as the Copper ROI density).
            "_kept_mesh_idx": kept_mesh_idx,
            "_conductance": layer.conductance,
        }
        self._layer_geom_cache[layer_index] = geom
        return geom

    def _layer_vectors(self, layer_index: int) -> dict | None:
        """Build (and cache) the per-triangle current-density vector
        ``J = -sigma * grad V`` for one padne layer.

        The result is independent of the current heatmap mode (it always
        works off the raw potentials), so the cache key is just the
        layer index. ``None`` is returned when the layer has no usable
        triangles.

        Returned dict has:

        * ``xs, ys`` — ``(N,)`` float64 vertex coordinates (mm) packed
          across all mesh components of the layer (same packing scheme
          as :meth:`_layer_arrays`).
        * ``tris`` — ``(M, 3)`` int32 triangle indices.
        * ``cx, cy`` — ``(M,)`` triangle centroid coordinates (mm).
        * ``Jx, Jy`` — ``(M,)`` per-triangle current-density components
          (A/mm). Zero on degenerate (zero-area) triangles.
        * ``trifinder`` — matplotlib ``TriFinder`` for point-in-triangle
          lookups during arrow grid sampling.
        * ``bounds`` — ``(x_min, x_max, y_min, y_max)`` of the layer.
        """
        cached = self._layer_vec_cache.get(layer_index)
        if cached is not None:
            return cached

        ls = self.solution.layer_solutions[layer_index]
        layer = self.solution.problem.layers[layer_index]
        sigma = float(layer.conductance)

        xs_parts: list[np.ndarray] = []
        ys_parts: list[np.ndarray] = []
        pot_parts: list[np.ndarray] = []
        tris_parts: list[np.ndarray] = []
        offset = 0
        for xys, tris_local, pot in zip(ls.vertex_xys, ls.triangles, ls.potentials):
            n = xys.shape[0]
            if n < 3 or tris_local.size == 0:
                continue
            xs_parts.append(xys[:, 0])
            ys_parts.append(xys[:, 1])
            pot_parts.append(np.asarray(pot, dtype=np.float64))
            tris_parts.append(tris_local.astype(np.int32, copy=False) + offset)
            offset += n
        if not xs_parts:
            return None

        xs = np.concatenate(xs_parts)
        ys = np.concatenate(ys_parts)
        pots = np.concatenate(pot_parts)
        tris = np.concatenate(tris_parts, axis=0)

        # Per-triangle linear gradient of potential. Solve
        #   [[x1-x0, y1-y0], [x2-x0, y2-y0]] @ [dV/dx, dV/dy]^T
        #   = [V1-V0, V2-V0]^T
        # vectorised across all triangles.
        tx = xs[tris]
        ty = ys[tris]
        tv = pots[tris]
        ax_ = tx[:, 1] - tx[:, 0]
        ay_ = ty[:, 1] - ty[:, 0]
        bx_ = tx[:, 2] - tx[:, 0]
        by_ = ty[:, 2] - ty[:, 0]
        det = ax_ * by_ - bx_ * ay_
        # Avoid division by zero on degenerate (collinear) triangles.
        bad = np.abs(det) < 1e-30
        safe_det = np.where(bad, 1.0, det)
        dV1 = tv[:, 1] - tv[:, 0]
        dV2 = tv[:, 2] - tv[:, 0]
        dVdx = (by_ * dV1 - ay_ * dV2) / safe_det
        dVdy = (ax_ * dV2 - bx_ * dV1) / safe_det
        Jx = -sigma * dVdx
        Jy = -sigma * dVdy
        if bad.any():
            Jx[bad] = 0.0
            Jy[bad] = 0.0

        cx = tx.mean(axis=1)
        cy = ty.mean(axis=1)

        triangulation = Triangulation(xs, ys, tris)
        trifinder = triangulation.get_trifinder()

        entry = {
            "xs": xs, "ys": ys, "tris": tris,
            "cx": cx, "cy": cy,
            "Jx": Jx, "Jy": Jy,
            "trifinder": trifinder,
            "bounds": (float(xs.min()), float(xs.max()),
                       float(ys.min()), float(ys.max())),
        }
        self._layer_vec_cache[layer_index] = entry
        return entry

    # --- Current-arrow overlay --------------------------------------------

    # Pre-exaggeration mm offset applied to arrow z in 3D mode so the
    # arrow sits above the copper top face instead of z-fighting with
    # it. Scaled by the GL viewer's vertical-exaggeration uniform, so a
    # 0.0025 mm lift becomes 0.125 mm at the default 50× — enough to
    # clear z-fight without the arrow visibly floating above the copper.
    # Manual-adjust knob: bump this up if you still see z-fighting,
    # drop it if the arrows look detached from the layer.
    _ARROW_Z_LIFT_MM: float = 0.0015

    # Hard cap on grid cells per layer. Even with a pathological density
    # request and a very elongated layer, this keeps the meshgrid
    # allocation bounded (≤ ~3 MB of float64) instead of letting it blow
    # out to hundreds of GiB like the old screen-px sampling did on
    # large designs at deep zoom.
    _ARROW_MAX_GRID_CELLS: int = 200_000

    def _build_arrow_segments(self, layer_probes: list[dict],
                              density: float) -> np.ndarray:
        """Sample current vectors on a regular grid anchored to each
        layer's *world-space* bounds and return the GL_LINES vertex
        buffer needed to draw an arrow at each sample. In 2D the result
        is ``(K, 2)`` (z=0 broadcast in the GL viewer); in 3D it's
        ``(K, 3)`` with each arrow lifted to its layer's stackup z so it
        sits on top of the copper. Consecutive vertex pairs are one
        segment; each arrow contributes three segments (shaft + two
        head wings = 6 vertices).

        ``density`` is the approximate number of arrows along the
        shorter side of the *combined* visible-layer bounds. Spacing is
        shared across every layer in this pass so a small island layer
        and a full-board plane get the same arrow size — and is derived
        from world bounds (not the viewport), so zoom / pan / 3D dolly
        don't change the sample positions or trigger a rebuild.
        """
        if (not layer_probes or density <= 0
                or self._gl_viewer is None):
            return np.empty((0, 2), dtype=np.float32)
        in_3d = self._gl_viewer.view_mode() == "3d"

        head_angle_rad = math.radians(25.0)
        cos_a = math.cos(head_angle_rad)
        sin_a = math.sin(head_angle_rad)
        head_frac = 0.30           # head length / shaft length
        max_len_frac = 0.85        # longest shaft / spacing_mm
        min_len_frac = 0.20        # shortest shaft (for non-zero |J|)
        cols = 3 if in_3d else 2

        # Pre-pass: resolve per-layer vector caches once and take the
        # union of their bounds. The arrow spacing is derived from this
        # union (not each layer's own bounds), so every layer in the
        # current selection gets the same arrow size — otherwise a tiny
        # island layer would render thousands of tiny arrows next to a
        # plane layer's coarse grid.
        resolved: list[tuple[dict, dict]] = []
        ux_min = math.inf; ux_max = -math.inf
        uy_min = math.inf; uy_max = -math.inf
        for lp in layer_probes:
            layer_index = lp.get("layer_index")
            if layer_index is None:
                continue
            vec = self._layer_vectors(layer_index)
            if vec is None:
                continue
            lx_min, lx_max, ly_min, ly_max = vec["bounds"]
            if lx_max <= lx_min or ly_max <= ly_min:
                continue
            resolved.append((lp, vec))
            if lx_min < ux_min: ux_min = lx_min
            if lx_max > ux_max: ux_max = lx_max
            if ly_min < uy_min: uy_min = ly_min
            if ly_max > uy_max: uy_max = ly_max
        if not resolved:
            return np.empty((0, cols), dtype=np.float32)
        union_w = ux_max - ux_min
        union_h = uy_max - uy_min
        spacing_mm = min(union_w, union_h) / float(density)
        if spacing_mm <= 0:
            return np.empty((0, cols), dtype=np.float32)

        all_segs: list[np.ndarray] = []
        for lp, vec in resolved:
            lx_min, lx_max, ly_min, ly_max = vec["bounds"]
            gx_min, gx_max = lx_min, lx_max
            gy_min, gy_max = ly_min, ly_max
            layer_w = gx_max - gx_min
            layer_h = gy_max - gy_min
            cell = spacing_mm
            nx = max(1, int(math.floor(layer_w / cell)))
            ny = max(1, int(math.floor(layer_h / cell)))
            # Safety net: if a freak aspect ratio still pushes nx*ny
            # over the cap, scale spacing up *for this layer only* so
            # the grid fits.
            if nx * ny > self._ARROW_MAX_GRID_CELLS:
                scale = math.sqrt(nx * ny / self._ARROW_MAX_GRID_CELLS)
                cell *= scale
                nx = max(1, int(math.floor(layer_w / cell)))
                ny = max(1, int(math.floor(layer_h / cell)))
            gx_axis = gx_min + cell * (np.arange(nx) + 0.5)
            gy_axis = gy_min + cell * (np.arange(ny) + 0.5)
            gx_grid, gy_grid = np.meshgrid(gx_axis, gy_axis)
            gx_flat = gx_grid.ravel()
            gy_flat = gy_grid.ravel()
            tri_idx = vec["trifinder"](gx_flat, gy_flat)
            mask = tri_idx >= 0
            if not mask.any():
                continue
            px = gx_flat[mask]
            py = gy_flat[mask]
            ti = tri_idx[mask]
            Jx = vec["Jx"][ti]
            Jy = vec["Jy"][ti]
            mag = np.hypot(Jx, Jy)
            if mag.max() <= 0:
                continue
            # 95th-percentile reference so a single FEM-singularity spike
            # near a SOURCE/SINK pin doesn't shrink every other arrow.
            ref = float(np.percentile(mag, 95.0))
            if ref <= 0:
                ref = float(mag.max())
            nmag = np.clip(mag / ref, 0.0, 1.0)
            length = spacing_mm * (
                min_len_frac + (max_len_frac - min_len_frac) * np.sqrt(nmag)
            )
            inv_mag = np.divide(1.0, mag,
                                out=np.zeros_like(mag), where=mag > 0)
            dirx = Jx * inv_mag
            diry = Jy * inv_mag

            half_len = length * 0.5
            tail_x = px - half_len * dirx
            tail_y = py - half_len * diry
            tip_x = px + half_len * dirx
            tip_y = py + half_len * diry

            head_len = length * head_frac
            wl_x = tip_x + head_len * (-dirx * cos_a + diry * sin_a)
            wl_y = tip_y + head_len * (-dirx * sin_a - diry * cos_a)
            wr_x = tip_x + head_len * (-dirx * cos_a - diry * sin_a)
            wr_y = tip_y + head_len * (dirx * sin_a - diry * cos_a)

            n_arrows = px.size
            segs = np.empty((n_arrows * 6, cols), dtype=np.float32)
            segs[0::6, 0] = tail_x; segs[0::6, 1] = tail_y
            segs[1::6, 0] = tip_x;  segs[1::6, 1] = tip_y
            segs[2::6, 0] = tip_x;  segs[2::6, 1] = tip_y
            segs[3::6, 0] = wl_x;   segs[3::6, 1] = wl_y
            segs[4::6, 0] = tip_x;  segs[4::6, 1] = tip_y
            segs[5::6, 0] = wr_x;   segs[5::6, 1] = wr_y
            if in_3d:
                # Lift arrow vertices slightly above the layer top so
                # GL_DEPTH_TEST doesn't fight the copper mesh under them.
                # ``z`` is pre-exaggeration mm; vertical-exaggeration in
                # the model matrix scales both layer z and this lift
                # together, so the offset stays proportional.
                z_lift = float(lp.get("z", 0.0)) + self._ARROW_Z_LIFT_MM
                segs[:, 2] = z_lift
            all_segs.append(segs)

        if not all_segs:
            return np.empty((0, cols), dtype=np.float32)
        return np.concatenate(all_segs, axis=0)

    def _refresh_arrows(self) -> None:
        """Push the current-arrow overlay (or clear it) based on the
        side-panel state. Works in both 2D and 3D — in 3D each arrow is
        lifted to its layer's stackup z so it floats on the copper top
        face."""
        if (not hasattr(self, "show_arrows_box")
                or self._gl_viewer is None):
            return
        if (not self.show_arrows_box.isChecked()
                or not self._layer_probes):
            self._gl_viewer.clear_arrows()
            return
        density = float(self.arrow_spacing_slider.value())
        segs = self._build_arrow_segments(self._layer_probes, density)
        if segs.size == 0:
            self._gl_viewer.clear_arrows()
            return
        self._gl_viewer.set_arrows(segs, color=(1.0, 1.0, 1.0))

    # Runs an arbitrary display-update callable. If it takes longer than
    # ``_BUSY_POPUP_DELAY_MS`` the modal "Updating display…" popup
    # appears mid-flight; if it finishes faster, no popup is ever shown.
    # The deferred-show pattern means we don't have to predict slowness
    # up front — fast cached operations cost nothing, slow ones get the
    # dialog without flashing it on quick toggles. Used by both the
    # render path and the overlay-refresh path (all-copper toggles on
    # big boards run through :meth:`_refresh_overlay_geometry`, not
    # :meth:`_render`).
    _BUSY_POPUP_DELAY_MS = 400

    def _run_with_busy_popup(self, work) -> None:
        # Re-entrancy: the inner work pumps QApplication.processEvents()
        # (via :meth:`_pump_busy_ui`) so the deferred-show timer can fire
        # and the marquee can repaint. If a queued signal re-enters this
        # wrapper mid-flight, drop it — running the work twice would
        # corrupt GL state (half-pushed meshes, mis-ordered overlay
        # batches). The popup itself is ApplicationModal so it blocks
        # user input on other windows; in practice this guard only fires
        # on stray internal signals.
        #
        # Don't DROP the re-entrant request: the toggle that raised it has
        # already flipped its EyeButton/FillButton state, so discarding the
        # work leaves the control out of sync with the view (the second rail's
        # eye reads "open" but its copper is never pushed until an unrelated
        # re-render). Remember the most recent pending work and run one trailing
        # pass once the current one returns — mirroring _render's coalescing.
        # Latest-wins is safe: each work rebuilds fully from the current widget
        # state, so the final pass reflects every flipped toggle.
        if self._render_busy_active:
            self._busy_popup_pending_work = work
            return
        self._render_busy_active = True
        self._last_pump_time = 0.0
        # Mutable box so the timer slot below can publish the created
        # dialog back to the finally block for cleanup.
        dlg_box: list[QProgressDialog | None] = [None]

        def _show_popup() -> None:
            # The timer slot can theoretically run after the finally block
            # has cleared the busy flag (if it fires between work
            # returning and timer.stop() in finally). Guard against that.
            if not self._render_busy_active or dlg_box[0] is not None:
                return
            d = QProgressDialog("Updating display…", "", 0, 0, self)
            d.setWindowTitle("Working")
            # The work isn't cancellable — it's one synchronous call.
            # Strip the cancel button so the dialog is unambiguously a
            # busy indicator.
            d.setCancelButton(None)
            d.setWindowModality(Qt.ApplicationModal)
            d.setMinimumDuration(0)
            d.setAutoClose(False)
            d.setAutoReset(False)
            d.setWindowFlags(d.windowFlags() & ~Qt.WindowContextHelpButtonHint)
            d.show()
            # Let Qt paint the dialog frame in this same processEvents
            # tick so the marquee shows up immediately rather than after
            # the next pump.
            QApplication.processEvents()
            dlg_box[0] = d

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(_show_popup)
        timer.start(self._BUSY_POPUP_DELAY_MS)
        try:
            work()
        finally:
            timer.stop()
            timer.deleteLater()
            if dlg_box[0] is not None:
                dlg_box[0].close()
                dlg_box[0].deleteLater()
            self._render_busy_active = False
        # Run exactly one trailing pass for any work that re-entered while we
        # were busy (see the guard above). Deferred via singleShot(0) so it runs
        # cleanly on the next event-loop turn rather than recursing here.
        pending = self._busy_popup_pending_work
        if pending is not None:
            self._busy_popup_pending_work = None
            QTimer.singleShot(0, lambda w=pending: self._run_with_busy_popup(w))

    # Backwards-compat thin wrapper: existing toggle signals connect to
    # this name. New callers should prefer :meth:`_run_with_busy_popup`
    # directly with their specific work function.
    def _render_with_busy_popup(self) -> None:
        self._run_with_busy_popup(self._render)

    def _pump_busy_ui(self) -> None:
        """Yield briefly to Qt so the busy popup's deferred-show timer
        can fire AND the marquee bar can repaint mid-work. Cheap when no
        popup is up (single bool check) and throttled to ~33 Hz when one
        is, so callers can sprinkle it in hot inner loops (every polygon,
        every per-(phys, net) tile) without blowing up overhead. Qt's
        marquee animates by an internal timer at ~30 Hz; pumping faster
        than that wastes CPU."""
        if not self._render_busy_active:
            return
        now = time.perf_counter()
        if now - self._last_pump_time < 0.03:
            return
        self._last_pump_time = now
        QApplication.processEvents()

    def _render(self) -> None:
        # Re-entrancy guard. _render_impl pumps QApplication.processEvents()
        # at every stage boundary (to keep the busy-popup marquee alive); a
        # queued signal that connects directly to _render — the outline
        # toggle, the value-scale range change — can fire inside one of
        # those pumps and re-enter before the modal busy dialog is up.
        # Running the body twice interleaved corrupts GL state (the
        # half-pushed-meshes hazard _run_with_busy_popup's own guard warns
        # about). Defer instead of dropping: remember that another pass is
        # needed and run exactly one trailing render once this one returns.
        if self._render_in_progress:
            self._render_rerender_pending = True
            return
        self._render_in_progress = True
        try:
            self._render_impl()
        finally:
            self._render_in_progress = False
        if self._render_rerender_pending:
            self._render_rerender_pending = False
            QTimer.singleShot(0, self._render)

    def _ensure_mesh_failure_highlights(self) -> None:
        """Zoom to mesh-failure site once; outline is also in overlay fills."""
        if getattr(self, "_mesh_failures_highlighted", False):
            return
        failures = (self.metadata or {}).get("mesh_failures") or []
        if not failures:
            return
        self._mesh_failures_highlighted = True
        _apply_mesh_failure_highlights(self)

    def _render_impl(self) -> None:
        # Optional per-stage timing. When ``self._render_profile`` is a
        # list (set by tools/bench_recolor.py) each stage appends a
        # ``(name, seconds)`` pair; when it's None ``_mark`` is a cheap
        # no-op so production renders pay nothing.
        _prof = getattr(self, "_render_profile", None)
        _t0 = time.perf_counter()
        _tprev = _t0

        def _mark(_name: str) -> None:
            nonlocal _tprev
            if _prof is not None:
                _now = time.perf_counter()
                _prof.append((_name, _now - _tprev))
                _tprev = _now
            # Pump the Qt event loop at every stage boundary so the busy
            # popup's marquee bar keeps animating during long renders.
            # No-op when no popup is up; ~microseconds when one is.
            self._pump_busy_ui()

        phys_list, rails, mode = self._current_selection()
        label, unit, derive_fn = self._mode_derive_fn(mode)
        is_via_current = (mode == _VIA_CURRENT_MODE)

        # The "Reference to source return" toggle only affects Voltage mode —
        # hide it elsewhere. Set here (before any early-return path) so its
        # visibility tracks the mode even when there's no mesh to draw.
        src_ref_box = getattr(self, "src_ref_box", None)
        if src_ref_box is not None:
            src_ref_box.setVisible(mode == "Voltage")

        # Overlays (silkscreen / pads / components / designators) are
        # independent of the FEM heatmap. Refresh them up-front so a
        # rail or 2D/3D change keeps them in sync — and so they still draw
        # when the early-outs below fire (no layer / rail selected).
        self._refresh_overlay_geometry(rails)
        # Same up-front treatment for the editor-mode component selection
        # box — it must survive the no-layer / no-rail early-outs below.
        self._refresh_editor_selection()

        # Copper-mesh cmap follows the active mode: every other mode
        # paints the copper from its per-vertex scalar field, so we
        # need the viridis ramp. Via Current paints the heatmap onto
        # the vias instead — the copper drops out as context, so we
        # push a flat-grey LUT and ignore the per-vertex values.
        self._ensure_gl_cmap("neutral" if is_via_current else "data")

        # Drop cached probe state until we build new layer probes.
        # In Via Current mode the per-vertex copper values are blanked
        # (vs_arr is zeroed below), so reporting them as "Via Current"
        # would mislead — fall back to the underlying voltage label /
        # unit so the hover bar still reads a meaningful number for
        # the copper underneath the cursor. The via overlay (the
        # ``_via_hover_info`` suffix) is what surfaces the actual
        # current reading when the cursor sits on a via.
        if is_via_current:
            self._probe_label = "Voltage"
            self._probe_unit = "V"
        else:
            self._probe_label = label
            self._probe_unit = unit
        self._layer_probes: list[dict] = []

        is_stub = bool(
            getattr(self.solution, "solver_info", {}).get("stub", False)
        )
        if not phys_list or not rails:
            self._clear_gl_mesh()
            self._gl_viewer.clear_outlines()
            self._gl_viewer.clear_cylinders()
            self._gl_viewer.clear_arrows()
            self._gl_viewer.clear_series_bars()
            self._gl_viewer.clear_stub_triangles()
            self._gl_viewer.set_overlay_top_right("")
            # Re-push the editor-directive markers even though there's no
            # mesh / rails — the user is allowed to drop free SOURCE / SINK
            # markers on a Gerber import BEFORE any rail exists, and the
            # whole point of the click is to see them appear. Clearing
            # markers here on the rail-less early-out would wipe the marker
            # we just placed. _update_markers_and_legend handles the
            # no-rails case internally (solved-directive walks early-out
            # on their own gates).
            self._update_markers_and_legend(phys_list, rails)
            self._refresh_board_outline()
            if not phys_list:
                self.summary_label.setText("(no layers selected)")
            elif is_stub:
                action = self._stub_action_word()
                self.summary_label.setText(
                    f"(import complete — add Source / Sink directives "
                    f"and press {action} to compute voltage distribution)"
                )
            else:
                self.summary_label.setText("(no rails selected)")
            self._ensure_mesh_failure_highlights()
            return

        # Combine every per-net layer in the selected rail groups across
        # every visible physical layer into one GPU batch. Per-layer
        # interpolators come back alongside for the CPU-side probe + the
        # Voltage Drop reference lookup.
        xs, ys, zs, vs, tris, layer_probes, no_current = self._build_rail_arrays(
            phys_list, rails, derive_fn,
        )
        self._layer_probes = layer_probes
        _mark("build_rail_arrays")
        if xs.size == 0 or tris.size == 0:
            self._clear_gl_mesh()
            self._gl_viewer.clear_outlines()
            self._gl_viewer.clear_arrows()
            self._gl_viewer.clear_series_bars()
            self._gl_viewer.clear_stub_triangles()
            self._gl_viewer.set_overlay_top_right("")
            # No heatmap mesh on these rails — e.g. a rail carrying a SOURCE
            # but no SINK (an unsolved / open current loop), which is exactly
            # what the user is mid-way through wiring up in editor mode. Don't
            # wipe the markers here: the editor-mode source / sink markers and
            # the via overlays still belong on screen (they gate on the
            # all-copper eye, on for the top layer by default, not on the
            # absent heatmap), so re-push them instead of clearing — mirroring
            # the no-rails early-out above. Clearing here is what made a just-
            # placed source / sink vanish until a second eye was toggled.
            self._update_markers_and_legend(phys_list, rails)
            if self.view_3d_box.isChecked():
                self._push_via_cylinders(phys_list, rails, mode=mode)
            else:
                self._gl_viewer.clear_cylinders()
            self._refresh_board_outline()
            if is_stub:
                action = self._stub_action_word()
                self.summary_label.setText(
                    f"(import complete — press {action} to compute "
                    "voltage distribution for the placed directives)"
                )
            else:
                self.summary_label.setText("(no mesh — selected layers have no copper on these rails)")
            self._ensure_mesh_failure_highlights()
            return

        # Vertices referenced by at least one triangle. Orphan vertices
        # (pinned to V=0 by the FEM solver to keep the linear system
        # well-conditioned) are invisible in the heatmap but would
        # otherwise dominate min/max stats — especially in Voltage Drop
        # mode where they'd appear as a fake -V_source drop.
        used_idx = np.unique(tris.ravel())
        vs_used = vs[used_idx] if used_idx.size > 0 else vs

        # _build_rail_arrays returns numpy arrays directly; alias them
        # so the downstream code (which used to wrap lists) keeps reading.
        xs_arr = xs
        ys_arr = ys
        vs_arr = vs
        tris_arr = tris
        drop_reference: float | None = None
        drop_reference_at: str = ""

        if is_via_current:
            # Range comes from the per-via |I| report, restricted to vias
            # on the selected rails (independent of which physical layers
            # are toggled visible — flipping layers shouldn't rescale the
            # colour bar). Per-vertex copper values aren't meaningful in
            # this mode, so we zero them out; the neutral LUT collapses
            # every entry to the same grey anyway.
            vmin, vmax, self._via_current_lookup = (
                self._via_current_lookup_and_range(rails)
            )
            logging.getLogger(__name__).debug(
                "Via Current: %d vias on rails=%s, raw range=[%.6g, %.6g] A",
                len(self._via_current_lookup), rails, vmin, vmax,
            )
            vs_arr = np.zeros_like(vs_arr, dtype=np.float64)
        else:
            self._via_current_lookup = {}
            vmin, vmax = float(vs_used.min()), float(vs_used.max())
            vmax = _slider_data_max(vs_used, mode, vmax)
            if vmax <= vmin:
                vmax = vmin + 1e-12

        # Voltage Drop mode: anchor the reference at the SOURCE (highest
        # voltage on the rail across any visible layer) and subtract it
        # so the heatmap reads 0 V at the source and goes NEGATIVE
        # toward the sinks. Pin voltages are sampled from the
        # *per-layer* interpolator matching each pin's layer_id, so the
        # reference is correct even when multiple physical layers are on.
        if mode == _VOLTAGE_DROP_MODE:
            target_layer_ids = {self._phys_name_to_layer_id.get(p)
                                for p in phys_list}
            target_layer_ids.discard(None)
            rail_members = set(self._effective_rail_members(rails))
            # layer_id → list of layer_probes (could be >1 if multiple
            # nets in the rail group are on the same physical layer).
            probes_by_layer: dict[int, list[dict]] = {}
            for lp in layer_probes:
                probes_by_layer.setdefault(lp["layer_id"], []).append(lp)

            def _sample_pin_voltage(layer_id: int, x_mm: float,
                                     y_mm: float, net: str) -> float | None:
                # Prefer the probe whose net matches the pin's net; fall
                # back to whichever probe on the same layer reports a
                # non-masked sample.
                candidates = probes_by_layer.get(layer_id, [])
                ordered = sorted(candidates,
                                  key=lambda lp: 0 if lp["net"] == net else 1)
                for lp in ordered:
                    interp = self._ensure_interpolator(lp)
                    if interp is None:
                        continue
                    s = interp(x_mm, y_mm)
                    try:
                        if hasattr(s, "mask") and \
                                bool(np.ma.getmaskarray(s).item()):
                            continue
                        v = float(s)
                    except (TypeError, ValueError):
                        continue
                    if np.isfinite(v):
                        return v
                return None

            def _collect_candidates(role_filter: str | None
                                    ) -> list[tuple[float, str]]:
                out: list[tuple[float, str]] = []
                for d in (self.metadata or {}).get("directives", []):
                    if role_filter is not None and d.get("role") != role_filter:
                        continue
                    for term in (d.get("terminals") or {}).values():
                        for pin in term.get("pins", []):
                            lid = pin.get("layer_id")
                            if lid not in target_layer_ids:
                                continue
                            pnet = pin.get("net", "")
                            if rail_members and pnet not in rail_members:
                                continue
                            v_at = _sample_pin_voltage(
                                lid, pin.get("x_mm"), pin.get("y_mm"), pnet,
                            )
                            if v_at is None:
                                continue
                            out.append(
                                (v_at, f"{d.get('label') or d.get('designator', '?')}"
                                       f".{self._pin_display_pad(pin) or '?'}")
                            )
                return out

            candidates = _collect_candidates("SOURCE")
            if not candidates:
                candidates = _collect_candidates(None)

            if candidates:
                drop_reference, drop_reference_at = max(candidates,
                                                         key=lambda t: t[0])
                vs_arr = vs_arr - drop_reference
                # Recompute range against the used (non-orphan) subset
                # only, otherwise orphan pins shifted by -drop_reference
                # would set a fake floor at -V_source.
                vs_used = vs_arr[used_idx] if used_idx.size > 0 else vs_arr
                vmin, vmax = float(vs_used.min()), float(vs_used.max())
                vmax = _slider_data_max(vs_used, mode, vmax)
                if vmax <= vmin:
                    vmax = vmin + 1e-12
                # The hover probe must report the shifted (drop) values too.
                # Rather than re-shift each probe's field and eagerly rebuild
                # its ~50-150 ms _FastTriSampler every render (per visible
                # layer), store the constant drop shift on the probe and apply
                # it at sample time in _probe_at_point. The lazily-built sampler
                # then stays keyed to the BASE values — shared uncorrupted with
                # plain Voltage mode (which uses the same _layer_cache entry) —
                # and the costly per-render rebuild disappears. (Finding 3.7.)
                for lp in layer_probes:
                    lp["value_offset"] = drop_reference

        # Voltage mode: reference each net to its own SOURCE return pin, so
        # the heatmap reads each net's true differential (<= rail voltage)
        # rather than an absolute potential that floats a fraction of a mV
        # above the rail (the global 0 V datum sits at a different GND node
        # than the source return — see _source_return_offsets). Each net is
        # shifted by its own offset; nets with no source (GND) are left as-is.
        # Opt-out via the "Reference to source return" toggle (default on;
        # visibility is managed at the top of _render).
        src_ref_on = src_ref_box is None or src_ref_box.isChecked()
        if mode == "Voltage" and not is_via_current and src_ref_on:
            offsets = self._source_return_offsets()
            # Build a per-vertex offset aligned to vs_arr. vs_arr is
            # concatenated in the original (bottom-first) loop order, while
            # layer_probes was reversed to top-first — so walk the probes in
            # reverse to recover the concatenation order and slice by the
            # vertex count each slab contributed.
            off_vec = np.zeros(vs_arr.size, dtype=np.float64)
            pos = 0
            any_off = False
            for lp in reversed(layer_probes):
                n = int(lp["n_vertices"])
                off = offsets.get(lp["net"], 0.0)
                if off:
                    off_vec[pos:pos + n] = off
                    any_off = True
                pos += n
            if any_off and pos == vs_arr.size:
                vs_arr = vs_arr - off_vec
                vs_used = vs_arr[used_idx] if used_idx.size > 0 else vs_arr
                vmin, vmax = float(vs_used.min()), float(vs_used.max())
                vmax = _slider_data_max(vs_used, mode, vmax)
                if vmax <= vmin:
                    vmax = vmin + 1e-12
                # As in the Voltage Drop branch: store each net's constant
                # source-return offset on the probe and apply it at sample time
                # (_probe_at_point) instead of re-shifting the field and
                # rebuilding the sampler every render. The base-valued sampler
                # stays shared and uncorrupted across Voltage / Voltage-Drop.
                for lp in layer_probes:
                    off = offsets.get(lp["net"], 0.0)
                    if off:
                        lp["value_offset"] = off

        x_min, x_max = float(xs_arr.min()), float(xs_arr.max())
        y_min, y_max = float(ys_arr.min()), float(ys_arr.max())
        self._data_bounds = (x_min, x_max, y_min, y_max)

        # Default colour-scale window (percentile-clipped for spike-prone
        # modes so FEM singularities don't crush the visible range).
        # Use the used (non-orphan) subset for the percentile calc too.
        # Via Current applies a similar clip — real boards have a
        # long-tail distribution (lots of low-current stitching vias,
        # a handful of high-current vias near regulators), so a raw
        # min..max map crushes 99% of the vias into the bottom of the
        # LUT. Clipping at P99 keeps the bulk of the vias spanning the
        # ramp while leaving outliers visible (clamped to the top).
        if is_via_current:
            display_min, display_max = self._via_current_display_range(
                self._via_current_lookup, vmin, vmax,
            )
        else:
            vs_used = vs_arr[used_idx] if used_idx.size > 0 else vs_arr
            display_min, display_max = self._useful_display_range(
                vs_used, mode, vmin, vmax,
            )

        # Resolve the linear vs log colour scale for this render. Log is
        # only meaningful for the wide-dynamic-range modes and needs a
        # strictly positive window, so it's decided here — once — and the
        # GL push, the scale bar and the baked via overlays all key off
        # ``self._log_active`` / ``self._log_floor`` for a consistent map.
        log_eligible = mode in _LOG_ELIGIBLE_MODES
        self._log_active = (self._log_scale and log_eligible
                            and math.isfinite(vmax) and vmax > 0.0)
        if self._log_active:
            self._log_floor = max(vmax * 10.0 ** (-_LOG_SCALE_DECADES),
                                   1e-300)
            # A log axis can't show zero / negatives — floor the window.
            # It also keeps the bulk readable AND the spikes on-scale by
            # itself, so the default window spans the full (floored) data
            # range rather than the linear-scale percentile clip.
            vmin = max(vmin, self._log_floor)
            display_min, display_max = vmin, vmax
        else:
            self._log_floor = 1e-12

        # If the heatmap selection (layers/rails/mode/rail-only-filter/scale)
        # hasn't changed since the last render — e.g. a 2D ↔ 3D toggle, which
        # only rebuilds the mesh geometry — keep the user's current clamp
        # instead of snapping back to the auto-detected default. A linear↔log
        # switch counts as a change so the window resets to the new default.
        selection_sig = (tuple(phys_list), tuple(rails), mode,
                         self.rail_only_box.isChecked(), self._log_active,
                         src_ref_on)
        preserve_scale = (self._last_scale_selection == selection_sig)
        self._last_scale_selection = selection_sig
        if preserve_scale:
            levels_min, levels_max = self._vmin, self._vmax
        else:
            self._vmin, self._vmax = display_min, display_max
            levels_min, levels_max = display_min, display_max

        # Push everything to the GPU. set_mesh is the heavy upload. Skip it when
        # the geometry arrays are the exact same objects as the last push — a
        # re-render that reuses the cached geometry without rebuilding the rail
        # arrays. set_mesh resets the per-vertex alpha / neutral / values, but
        # the host re-pushes those unconditionally below, so a skip is safe.
        # ``zs`` is per-vertex z (mm, pre-exaggeration); only used by the
        # 3D mode's perspective MVP — ignored by the 2D ortho path.
        _mark("prep")
        # Compare by object identity against the *retained* last-pushed arrays
        # (holding the references so a garbage-collected array's id can't be
        # reused and cause a false match).
        _last = getattr(self, "_last_mesh_upload", None)
        _gl_verts = getattr(self._gl_viewer, "_n_vertices", 0)
        if not (_last is not None
                and xs_arr is _last[0] and ys_arr is _last[1]
                and tris_arr is _last[2] and zs is _last[3]
                and _gl_verts == xs_arr.size):
            self._gl_viewer.set_mesh(xs_arr, ys_arr, tris_arr,
                                      data_bounds=self._data_bounds,
                                      zs=zs.astype(np.float32))
            self._last_mesh_upload = (xs_arr, ys_arr, tris_arr, zs)
        # Values + levels are pushed through _gl_scale: identity on a
        # linear scale, log10 (floored) on a log scale. Pushing both in
        # the same space means the GL viewer's linear normalisation
        # shader produces the correct log mapping with no shader change.
        self._gl_viewer.set_values(self._gl_scale(vs_arr).astype(np.float32))
        self._gl_viewer.set_levels(float(self._gl_scale(levels_min)),
                                    float(self._gl_scale(levels_max)))
        # Per-vertex mesh alpha: combines the per-layer Transparency
        # control with editor-mode connectivity dimming. Pushed after
        # set_mesh (which invalidates any previous alpha array).
        self._gl_viewer.set_vertex_alpha(
            self._combined_mesh_alpha_array(layer_probes, xs_arr.size)
        )
        # Grey solved dead-end (no-current) copper when the "Grey no current
        # copper" toggle is on — the same toggle that greys FEM-excluded
        # stubs. Pushed as the per-vertex neutral mask; cleared otherwise so
        # the copper paints from the colormap as usual. Skipped in Via
        # Current mode, where the whole copper is already a neutral backdrop.
        grey_no_current = (
            getattr(self, "colour_stubs_box", None) is not None
            and self.colour_stubs_box.isChecked()
            and not is_via_current
            and no_current.size == xs_arr.size
            and bool(no_current.any())
        )
        self._gl_viewer.set_vertex_neutral(
            no_current if grey_no_current else None
        )
        _mark("gl_mesh_upload")

        # Carry the user's chosen colour scheme through the re-render
        # (mode / layer / rail switches must not snap it back to default).
        self.scale_controller.setLogVisible(log_eligible)
        self._update_scale_controller(vmin, vmax, display_min, display_max,
                                       self._cmap_name, label, unit,
                                       reset_selection=not preserve_scale,
                                       log_active=self._log_active)
        _mark("scale_controller")

        # Markers + legend (also overlaid via the GLMeshViewer's QPainter
        # layer, so they don't trigger Qt's raster-fallback compositor).
        self._update_markers_and_legend(phys_list, rails)
        _mark("markers_legend")

        # Layer + pad outlines (GL_LINES). Cheap toggle — segments are
        # cached at first use, so flipping either checkbox just uploads /
        # clears one VBO pair.
        self._refresh_outlines(layer_probes, phys_list, rails)
        _mark("outlines")

        # Stub-copper overlay — polygons of copper the FEM excluded.
        # Always pushed so users see what's there even when arrows /
        # outlines / heatmap-vias are off. Default is flat grey; if the
        # user opts into colour-by-V we sample the same-net solved
        # layer at each stub's centroid using ``mode`` + ``drop_reference``.
        self._push_stubs(phys_list, rails, mode=mode,
                          drop_reference=drop_reference)
        _mark("stubs")

        # Series-component bars — gradient rectangles between the two
        # terminal pin positions of each RESISTOR directive.
        self._push_series_bars(phys_list, rails, mode,
                               drop_reference=drop_reference)
        _mark("series_bars")

        # Via cylinders — only meaningful in 3D mode (in 2D they'd
        # collapse to overlapping circles at z=0). The heatmap-vias path
        # needs the same Voltage-Drop reference the layer heatmap used.
        # Via Current mode always pushes cylinders in 3D; in 2D the
        # per-via colours are emitted via the marker overlay path inside
        # :meth:`_update_markers_and_legend`.
        self._last_drop_reference = drop_reference
        if self.view_3d_box.isChecked():
            self._push_via_cylinders(phys_list, rails, mode=mode)
        else:
            self._gl_viewer.clear_cylinders()
        _mark("via_cylinders")

        # Current-flow arrow overlay (2D only). Cheap if disabled.
        self._refresh_arrows()
        _mark("arrows")

        # Board outline overlay — independent of layer/rail selection.
        self._refresh_board_outline()
        _mark("board_outline")

        # Fit to data ONLY on the very first render (or while we're
        # still waiting for the deferred initial fit after Qt sizes the
        # widget). Subsequent renders — layer toggles, mode/rail
        # changes — leave the user's pan/zoom alone.
        if self._need_initial_fit:
            self._fit_board_to_canvas()

        self._ensure_mesh_failure_highlights()

        # Summary stats over the mesh's actual values — orphan vertices
        # excluded (same reason as the scale-range filtering above). In
        # Via Current mode the per-vertex copper values are blanked, so
        # we report stats over the per-via |I| set instead.
        if is_via_current:
            via_vals = list(self._via_current_lookup.values())
            if via_vals:
                arr = np.asarray(via_vals, dtype=np.float64)
                self.summary_label.setText(
                    f"<b>{label}</b><br>"
                    f"min = {arr.min():.4g} {unit}<br>"
                    f"max = {arr.max():.4g} {unit}<br>"
                    f"mean = {arr.mean():.4g} {unit}<br>"
                    f"vias: {arr.size:,}"
                )
            else:
                self.summary_label.setText(
                    f"<b>{label}</b><br>"
                    "(no vias on the selected rails)"
                )
        else:
            vs_used = vs_arr[used_idx] if used_idx.size > 0 else vs_arr
            self.summary_label.setText(
                f"<b>{label}</b><br>"
                f"min = {vs_used.min():.4g} {unit}<br>"
                f"max = {vs_used.max():.4g} {unit}<br>"
                f"mean = {vs_used.mean():.4g} {unit}<br>"
                f"vertices: {len(vs_used):,}"
            )
        _mark("summary")
        if _prof is not None:
            _prof.append(("TOTAL", time.perf_counter() - _t0))

    # --- Side-panel scale controller ----------------------------------------

    def _update_scale_controller(self, data_min: float, data_max: float,
                                 sel_min: float, sel_max: float,
                                 cmap_name: str, label: str, unit: str,
                                 reset_selection: bool = True,
                                 log_active: bool = False
                                 ) -> None:
        """Push fresh data bounds + label/unit into the scale controller.

        With ``reset_selection=True`` (default) the user clamp snaps back
        to (sel_min, sel_max) — that's the right behaviour across layer /
        rail / mode changes (e.g. a Voltage Drop clamp of -0.05..-0.01 is
        nonsense when you switch to Current Density). Callers re-rendering
        the same selection (e.g. a 2D/3D toggle) pass ``False`` so the
        user's existing clamp is preserved.

        ``log_active`` is the effective scale type — the gradient strip
        needs it set before :meth:`ScaleController.setRange` so the
        handles land at the right (log-spaced) positions.
        """
        self._cmap_name = cmap_name
        self.scale_controller.setColormap(cmap_name)
        self.scale_controller.setLogActive(log_active)
        self.scale_controller.setLabelUnit(label, unit)
        self.scale_controller.setRange(data_min, data_max,
                                         sel_min=sel_min, sel_max=sel_max,
                                         reset_selection=reset_selection)

    def _via_current_display_range(
        self,
        lookup: dict[tuple[str, float, float], float],
        data_min: float, data_max: float,
    ) -> tuple[float, float]:
        """Pick the default Via Current colour-scale window.

        Long-tail distributions (most vias near 0 A, a few outliers
        near a regulator) crush the bulk of the vias into the bottom
        of the LUT under a raw min..max map. Two adjustments:

        1. When there are enough samples for a percentile to be
           meaningful (>= 8 vias), clip the top to
           :data:`_DISPLAY_PERCENTILE_HIGH` of the data so the bulk
           of the vias span the ramp.
        2. When every via reports the same current (or the range is
           numerically degenerate), widen the window to ±10% around
           the value so all vias don't collapse to LUT entry 0.
        """
        if not lookup:
            return data_min, data_max
        arr = np.asarray(list(lookup.values()), dtype=np.float64)
        span = data_max - data_min
        # Degenerate-range case: all vias carry essentially the same
        # current. Without this widening, ``t = (cur - vmin) / span``
        # collapses to 0 for every via and the whole batch renders at
        # LUT index 0 (the deep-purple end of viridis).
        if span <= max(1e-9, abs(data_min) * 1e-6):
            anchor = float(arr.mean()) if arr.size else data_min
            half = max(abs(anchor) * 0.1, 1e-6)
            return anchor - half, anchor + half
        if arr.size < 8:
            return data_min, data_max
        sel_max = float(np.percentile(arr, self._display_percentile_high))
        # Only clip when the percentile is meaningfully below the raw
        # max — otherwise the slider's drag-to-real-max range
        # collapses and the heatmap reads the same as the unclipped
        # case anyway.
        if sel_max >= data_max * 0.95:
            return data_min, data_max
        # Guard against a clip that collapses the range (happens when
        # the bulk of the vias share a single value and only a few
        # outliers stretch the raw max). Fall back to the unclipped
        # range and let the user drag the scale if they care.
        if sel_max - data_min <= max(1e-9, abs(data_min) * 1e-6):
            return data_min, data_max
        return data_min, sel_max

    def _useful_display_range(self, vs_arr: np.ndarray, mode: str,
                              data_min: float, data_max: float,
                              ) -> tuple[float, float]:
        """Pick the default colour-scale window.

        For modes prone to FEM singularities at pinned-voltage vertices
        (Current Density, Power Density), the top end is clipped to
        :data:`_DISPLAY_PERCENTILE_HIGH` of the data so a handful of
        outlier vertices near a SOURCE/SINK pin don't crush the rest of
        the board to the bottom of the colour scale. Returns
        ``(sel_min, sel_max)`` ready to hand to the rasterisation step
        and the scale controller.
        """
        if mode not in _SPIKE_PRONE_MODES or len(vs_arr) < 100:
            return data_min, data_max
        sel_max = float(np.percentile(vs_arr, self._display_percentile_high))
        # Only clip if the percentile is meaningfully below the actual
        # max — otherwise the slider's drag-to-real-max range collapses.
        if sel_max >= data_max * 0.95:
            return data_min, data_max
        if sel_max <= data_min:
            sel_max = data_min + (data_max - data_min) * 1e-3
        return data_min, sel_max
