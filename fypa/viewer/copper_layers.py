"""The per-layer "all copper" view (the Physical layers control's second eye).

Each physical layer's copper is triangulated once and kept in its own GPU
buffer (:class:`fypa.gl_mesh_viewer.CopperDraw`). Showing, hiding, recolouring,
dimming or fading a layer then only changes the list of buffers drawn and a
few uniforms — no geometry is rebuilt or re-uploaded — which is what makes
toggling a layer instant on a large board.

The triangulation itself runs in worker processes after the board opens
(:mod:`fypa.viewer.copper_triangulator`); a layer whose triangles are not
ready yet is drawn as soon as they arrive.
"""
from __future__ import annotations

import itertools
import logging
import os

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor

from fypa.gl_mesh_viewer import CopperDraw
from fypa.viewer import copper_tris as _ct
from fypa.viewer.copper_triangulator import CopperTriangulator

log = logging.getLogger(__name__)

_EMPTY_XY = np.empty((0, 2), dtype=np.float32)
_MERGED = "merged"
_tokens = itertools.count(1)

# Per-polygon draw class.
_HIDE, _NORMAL, _DIM = 0, 1, 2


class _LayerCopper:
    """One physical layer's all-copper polygons, flattened for drawing.

    Polygons are ordered by net so that leaving a net out (it is on a
    visible rail, or outside the editor's net focus) removes one contiguous
    vertex range rather than thousands of scattered ones.
    """

    __slots__ = ("layer_id", "recs", "polys", "nets", "net_index",
                 "poly_net", "unnamed", "offsets", "token", "_by_id")

    def __init__(self, layer_id, recs: list[dict]) -> None:
        self.layer_id = layer_id
        self.recs = recs
        self.polys: list[dict] = []
        self.nets: list = []
        self.net_index: dict = {}
        poly_net: list[int] = []
        for rec in sorted(recs, key=lambda r: str(r.get("net") or "")):
            net = rec.get("net")
            idx = self.net_index.get(net)
            if idx is None:
                idx = self.net_index[net] = len(self.nets)
                self.nets.append(net)
            polys = rec.get("polygons") or []
            self.polys.extend(polys)
            poly_net.extend([idx] * len(polys))
        self.poly_net = np.asarray(poly_net, dtype=np.int64)
        # Copper with no usable net name: the name alone cannot tell two
        # pieces apart, so editor focus tracks those by polygon identity.
        self.unnamed = np.asarray(
            [(not n) or n == "(none)" for n in self.nets], dtype=bool)
        # kind -> vertex offset of each polygon in that kind's buffer
        # (length ``len(polys) + 1``). Present once the buffer was built.
        self.offsets: dict[str, np.ndarray] = {}
        self.token = next(_tokens)
        self._by_id: dict[int, int] | None = None

    def poly_position(self, poly_id: int) -> int | None:
        if self._by_id is None:
            self._by_id = {id(p): i for i, p in enumerate(self.polys)}
        return self._by_id.get(poly_id)


def _same_rings(a: dict, b: dict) -> bool:
    ea, eb = a.get("exterior"), b.get("exterior")
    if ea is None or eb is None or not np.array_equal(ea, eb):
        return False
    ha, hb = a.get("holes") or [], b.get("holes") or []
    return len(ha) == len(hb) and all(
        np.array_equal(x, y) for x, y in zip(ha, hb))


def _ranges(mask: np.ndarray, offsets: np.ndarray):
    """Vertex ``(firsts, counts)`` covering the polygons selected by ``mask``,
    with neighbouring polygons merged into one range each."""
    edges = np.flatnonzero(np.diff(np.concatenate(
        ([0], mask.astype(np.int8), [0]))))
    firsts = offsets[edges[0::2]]
    counts = offsets[edges[1::2]] - firsts
    keep = counts > 0
    return (np.ascontiguousarray(firsts[keep], dtype=np.int32),
            np.ascontiguousarray(counts[keep], dtype=np.int32))


class _CopperLayersMixin:
    """Per-layer all-copper buffers, their draw state, and the background
    triangulation that fills them."""

    # Boards with fewer copper polygons than this are triangulated on the
    # spot (it takes a moment); bigger ones are prepared in the background.
    _COPPER_BG_MIN_POLYGONS = 3000

    # ``(all_copper list, its length, {layer_id: _LayerCopper})`` for the
    # metadata currently shown. Class-level defaults so partially-built
    # viewers (tests, early start-up) read "nothing yet".
    _copper_index: tuple | None = None
    _copper_triangulator: CopperTriangulator | None = None
    _copper_waiting: tuple = ()

    # --- Index --------------------------------------------------------------

    def _copper_layer_index(self) -> dict:
        """``{layer_id: _LayerCopper}`` for the current metadata, rebuilt when
        the metadata is replaced (a re-solve or a re-import)."""
        ac = (self.metadata or {}).get("all_copper") or []
        cached = self._copper_index
        if cached is not None and cached[0] is ac and cached[1] == len(ac):
            return cached[2]
        by_layer: dict = {}
        for rec in ac:
            by_layer.setdefault(rec.get("layer_id"), []).append(rec)
        index = {lid: _LayerCopper(lid, recs)
                 for lid, recs in by_layer.items()}
        if cached is not None:
            self._carry_over_copper_triangles(cached[2], index)
        self._copper_index = (ac, len(ac), index)
        self._start_copper_triangulation(index)
        return index

    @staticmethod
    def _carry_over_copper_triangles(old: dict, new: dict) -> None:
        """A re-solve returns fresh metadata whose copper is almost always
        the same shapes; hand each unchanged polygon the triangles already
        made for it rather than triangulating the board again."""
        keys = tuple(_ct.CACHE_KEY.values())
        for lid, new_lc in new.items():
            old_lc = old.get(lid)
            if old_lc is None:
                continue
            old_by_net: dict = {}
            for poly, ni in zip(old_lc.polys, old_lc.poly_net.tolist()):
                old_by_net.setdefault(old_lc.nets[ni], []).append(poly)
            new_by_net: dict = {}
            for poly, ni in zip(new_lc.polys, new_lc.poly_net.tolist()):
                new_by_net.setdefault(new_lc.nets[ni], []).append(poly)
            for net, new_polys in new_by_net.items():
                old_polys = old_by_net.get(net)
                if old_polys is None or len(old_polys) != len(new_polys):
                    continue
                for po, pn in zip(old_polys, new_polys):
                    if po is pn or not any(po.get(k) is not None
                                           for k in keys):
                        continue
                    if _same_rings(po, pn):
                        for k in keys:
                            if po.get(k) is not None and pn.get(k) is None:
                                pn[k] = po[k]

    # --- Background triangulation -------------------------------------------

    def _start_copper_triangulation(self, index: dict) -> None:
        old = self._copper_triangulator
        if old is not None:
            old.shutdown()
            old.deleteLater()
            self._copper_triangulator = None
        if os.environ.get("FYPA_COPPER_BACKGROUND", "1") == "0":
            return
        key = _ct.CACHE_KEY[_ct.SOLID]
        missing = sum(1 for lc in index.values() for p in lc.polys
                      if p.get(key) is None)
        if missing < self._COPPER_BG_MIN_POLYGONS:
            return
        tri = CopperTriangulator(self)
        tri.layer_ready.connect(self._on_copper_layer_ready)
        tri.failed.connect(self._on_copper_triangulation_failed)
        self._copper_triangulator = tri
        # Layers already switched on first, then down the stackup.
        id_by_name = getattr(self, "_phys_name_to_layer_id", {})
        on, off = [], []
        for name, eye2 in getattr(self, "_layer_eye2_buttons", []):
            lid = id_by_name.get(name)
            if lid in index:
                (on if eye2.isVisibleState() else off).append(lid)
        seen = set(on) | set(off)
        rest = [lid for lid in index if lid not in seen]
        for lid in (*on, *off, *rest):
            tri.add(lid, _ct.SOLID, index[lid].polys)

    def _on_copper_layer_ready(self, _layer_id: int = 0,
                               _kind: str = "") -> None:
        """Triangles arrived (or the workers gave up, and the layers on
        screen fall back to the in-process path): draw what was waiting."""
        if not self._copper_waiting:
            return
        if getattr(self, "_render_busy_active", False):
            # Another refresh is mid-flight. Handing this one to
            # _run_with_busy_popup now would take the single "run after"
            # slot from whatever the user clicked meanwhile, so come back.
            QTimer.singleShot(50, self._on_copper_layer_ready)
            return
        self._run_with_busy_popup(self._refresh_copper_layers)

    def _on_copper_triangulation_failed(self, _message: str) -> None:
        self._on_copper_layer_ready()

    def _shutdown_copper_layers(self) -> None:
        """Stop the workers and let go of the copper data (window closing)."""
        tri = self._copper_triangulator
        if tri is not None:
            tri.shutdown()
            self._copper_triangulator = None
        self._copper_index = None
        self._via_position_index_cache = None

    # --- Geometry -----------------------------------------------------------

    def _copper_layer_xy(self, lc: _LayerCopper, kind: str):
        """All of a layer's triangles in one ``(N, 2)`` array, in polygon
        order, or ``None`` while the background workers are still preparing
        them (the layer is drawn when they finish)."""
        key = _ct.CACHE_KEY[kind]
        tri = self._copper_triangulator
        if tri is not None and tri.active:
            if not tri.is_pending(lc.layer_id, kind):
                missing = [p for p in lc.polys if p.get(key) is None]
                if len(missing) >= 200:
                    tri.add(lc.layer_id, kind, missing, front=True)
            if tri.is_pending(lc.layer_id, kind):
                tri.prioritise(lc.layer_id, kind)
                return None
        build = (self._triangulate_stub if kind == _ct.SOLID
                 else self._copper_poly_wire)
        parts: list[np.ndarray] = []
        for poly in lc.polys:
            arr = poly.get(key)
            if arr is None:
                self._pump_busy_ui()
                arr = build(poly)
            parts.append(arr)
        offsets = np.zeros(len(parts) + 1, dtype=np.int64)
        if parts:
            np.cumsum([a.shape[0] for a in parts], out=offsets[1:])
        lc.offsets[kind] = offsets
        return np.concatenate(parts, axis=0) if parts else _EMPTY_XY

    def _copper_layer_passes(self, lc: _LayerCopper, kind: str,
                             rgb: tuple, alpha: float,
                             skip_nets: set) -> list:
        """The colour passes for one layer: which polygons draw, and which
        of those recede for the editor's connectivity highlight.

        Mirrors what the per-polygon loop used to decide: copper on a
        visible rail is left to the heatmap, net focus removes everything
        outside the focused net, and a selection highlight dims the rest.
        """
        rgba = (rgb[0], rgb[1], rgb[2], alpha)
        focus_on = self._editor_focus_active()
        dim_on = bool(self._editor_mode and (
            self._editor_highlight_nets or self._editor_highlight_polys))
        skipped = [lc.net_index[n] for n in skip_nets if n in lc.net_index]
        if not (focus_on or dim_on or skipped):
            return [(rgba, None, None)]

        n_nets = len(lc.nets)
        lid = int(lc.layer_id) if lc.layer_id is not None else None

        def _poly_hits(poly_keys) -> list[int]:
            out = []
            for plid, pid in poly_keys:
                if plid == lid:
                    j = lc.poly_position(pid)
                    if j is not None:
                        out.append(j)
            return out

        if focus_on:
            net_cls = np.zeros(n_nets, dtype=np.int8)
            for net in self._editor_focus_nets:
                if net and net in lc.net_index:
                    net_cls[lc.net_index[net]] = _NORMAL
            cls = net_cls[lc.poly_net]
            hits = _poly_hits(self._editor_focus_polys)
            if hits:
                hits = np.asarray(hits, dtype=np.int64)
                # Only unnamed copper is rescued by identity; a real net
                # outside the focus stays hidden.
                hits = hits[lc.unnamed[lc.poly_net[hits]]]
                cls[hits] = _NORMAL
        else:
            cls = np.ones(len(lc.polys), dtype=np.int8)
        if skipped:
            skip_net = np.zeros(n_nets, dtype=bool)
            skip_net[skipped] = True
            cls[skip_net[lc.poly_net]] = _HIDE
        if dim_on:
            lit_net = np.zeros(n_nets, dtype=bool)
            for net in self._editor_highlight_nets:
                if net and net in lc.net_index:
                    lit_net[lc.net_index[net]] = True
            lit = lit_net[lc.poly_net]
            hits = _poly_hits(self._editor_highlight_polys)
            if hits:
                lit[hits] = True
            cls[(cls == _NORMAL) & ~lit] = _DIM

        offsets = lc.offsets[kind]
        passes = []
        # Dimmed copper first, so highlighted copper paints over it where
        # the two overlap.
        if dim_on:
            firsts, counts = _ranges(cls == _DIM, offsets)
            if firsts.size:
                d = self._editor_dim_rgb(rgb, None)
                passes.append(((d[0], d[1], d[2], alpha), firsts, counts))
        firsts, counts = _ranges(cls == _NORMAL, offsets)
        if firsts.size:
            passes.append((rgba, firsts, counts))
        return passes

    # --- Draw state ---------------------------------------------------------

    def _refresh_copper_layers(self, rails=None) -> None:
        """Tell the GL viewer which layers' copper to draw and how.

        For every layer whose all-copper eye is on, draws all of that
        layer's copper that ISN'T part of a selected rail (the rails are
        already the heatmap; this is the rest), in the layer's swatch
        colour, as a wire-mesh outline or a solid fill per that row's fill
        toggle, faded by the row's TransparencyButton.

        Geometry is only uploaded the first time a layer is shown in a
        given fill style; after that this is a handful of dictionary
        lookups however large the board is.
        """
        gv = getattr(self, "_gl_viewer", None)
        if gv is None or not hasattr(gv, "set_copper_draws"):
            return
        md = self.metadata
        eye2_buttons = getattr(self, "_layer_eye2_buttons", [])
        if md is None:
            gv.clear_copper_geometry()
            self._set_copper_waiting(())
            return
        if not eye2_buttons or not md.get("all_copper"):
            gv.set_copper_draws([])
            self._set_copper_waiting(())
            return
        if rails is None:
            rails = self._visible_rails()
        rail_members = (set(self._effective_rail_members(rails))
                        if rails else set())
        index = self._copper_layer_index()
        in_2d = not self.view_3d_box.isChecked()
        fill_by_name = dict(getattr(self, "_layer_fill_buttons", []))
        transp_by_name = dict(
            getattr(self, "_layer_transparency_buttons", []))
        # Which nets the all-copper view skips because the heatmap mesh
        # already draws them. Outside editor mode the selected rail IS
        # the heatmap, so its copper is excluded to avoid double-drawing.
        # In editor mode the user wants the second eye to show ALL of a
        # layer's physical copper — including nets belonging to a
        # previously-solved rail — so we drop the filter entirely. The
        # solved-rail heatmap / geometry still paints on top: opaque
        # all-copper is drawn before the mesh in 2D, and depth ordering
        # handles it in 3D.
        skip_nets: set = set() if self._editor_mode else rail_members
        sel_layer = self._selected_layer
        editor_dim_active = bool(self._editor_mode and (
            self._editor_highlight_nets or self._editor_highlight_polys))
        focus_on = self._editor_focus_active()

        draws: list[CopperDraw] = []
        keep: set = set()
        waiting: list[str] = []
        # Bottom-up: ``_layer_eye2_buttons`` is in panel order (topmost
        # first); sort by stackup rank descending so the topmost layer is
        # drawn last, mirroring the heatmap mesh's draw order — that matches
        # the GL_LEQUAL depth test the before-mesh pass uses in 2D (same z →
        # later draw wins).
        #
        # In 2D the selected layer is forced LAST so it paints on top of
        # the dimmed others (painter's order). In 3D the per-layer z + depth
        # test already place each layer correctly, and depth-write-on
        # transparency needs a strict back-to-front (stackup) draw order:
        # forcing the selected layer last there would depth-reject the
        # layers sitting behind it, making an unselected transparent layer
        # read as opaque. So apply the selection priority only in 2D.
        for name, eye2 in sorted(
            eye2_buttons,
            key=lambda ne: (
                (0 if ne[0] == sel_layer else 1) if in_2d else 0,
                self._phys_stackup_rank.get(ne[0], 1 << 30),
            ),
            reverse=True,
        ):
            lc = index.get(self._phys_name_to_layer_id.get(name))
            if lc is None or not lc.polys:
                continue
            fb = fill_by_name.get(name)
            solid = bool(fb.isSolid()) if fb is not None else False
            kind = _ct.SOLID if solid else _ct.WIRE
            key = (name, kind)
            # A layer's buffer stays on the GPU while its eye is off, so
            # switching it back on costs nothing.
            keep.add(key)
            if not eye2.isVisibleState():
                continue
            tb = transp_by_name.get(name)
            layer_alpha = tb.alpha() if tb is not None else 1.0
            if layer_alpha <= 0.0:
                continue
            # In 2D opaque all-copper draws BEFORE the heatmap mesh with
            # depth on — the rail mesh (drawn next, same per-layer z)
            # paints over its own layer's all-copper, and bottom rail
            # triangles are depth-rejected where a higher layer's
            # all-copper has already written a closer z. That would
            # equally depth-reject the rail mesh underneath a partially-
            # transparent layer — turning it into a tinted hole instead of
            # letting the heatmap show through — so a layer at alpha < 1.0
            # draws AFTER the mesh instead, free to blend with whatever is
            # below. 3D always draws after — per-layer z + depth handle
            # ordering there.
            under = in_2d and layer_alpha >= 1.0
            qc = QColor(self._layer_color_for(name))
            lrgb = (qc.redF(), qc.greenF(), qc.blueF())
            # When a layer is selected, every OTHER layer's all-copper
            # is blended toward the dim target and (optionally) faded
            # so the selected layer reads clearly above the rest.
            if sel_layer is not None and name != sel_layer:
                lrgb = self._selected_layer_dim_rgb(lrgb)
                layer_alpha *= max(
                    0.0, min(1.0, self._SELECTED_LAYER_UNSEL_ALPHA))
                if layer_alpha <= 0.0:
                    continue
                if in_2d and layer_alpha < 1.0:
                    under = False
            z = self._layer_render_z(name)
            # Solid + partial transparency: route through a per-layer
            # merged triangulation so overlapping source polygons
            # blend the layer's alpha exactly once per pixel (rather
            # than once per overlapping polygon, which cumulatively
            # pushes the area to opacity). The merge collapses the
            # per-net distinction the editor's connectivity dim and net
            # focus depend on, so those keep the per-polygon buffer.
            if (solid and layer_alpha < 1.0
                    and not editor_dim_active and not focus_on):
                mkey = (name, _MERGED)
                version = (lc.token, frozenset(skip_nets))
                if not gv.has_copper_geometry(mkey, version):
                    merged = self._merged_solid_all_copper_tris(
                        name, lc.recs, skip_nets)
                    gv.set_copper_geometry(
                        mkey, merged if merged is not None else _EMPTY_XY,
                        version)
                keep.add(mkey)
                draws.append(CopperDraw(mkey, z, under, [
                    ((lrgb[0], lrgb[1], lrgb[2], layer_alpha), None, None)]))
                continue
            if (kind not in lc.offsets
                    or not gv.has_copper_geometry(key, lc.token)):
                xy = self._copper_layer_xy(lc, kind)
                if xy is None:
                    waiting.append(name)
                    continue
                gv.set_copper_geometry(key, xy, lc.token)
            passes = self._copper_layer_passes(
                lc, kind, lrgb, layer_alpha, skip_nets)
            if passes:
                draws.append(CopperDraw(key, z, under, passes))
        gv.retain_copper_geometry(keep)
        gv.set_copper_draws(draws)
        self._set_copper_waiting(tuple(waiting))

    def _set_copper_waiting(self, names: tuple) -> None:
        """Say in the status bar which layers are still being prepared."""
        if names == self._copper_waiting:
            return
        self._copper_waiting = names
        try:
            bar = self.statusBar()
        except Exception:
            return
        if names:
            bar.showMessage(
                "Preparing copper for " + ", ".join(names) + "…")
        else:
            bar.clearMessage()
