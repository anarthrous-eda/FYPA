"""Directive-pin markers, legend, board outline, stub copper and series bars."""
from __future__ import annotations

import math
import numpy as np
from PySide6.QtGui import QColor

from fypa.viewer.display import (
    _build_cmap_lut,
    _build_neutral_cmap_lut,
    _VIA_CURRENT_MODE,
    _VOLTAGE_DROP_MODE,
)
from fypa.viewer.mesh_geometry import _sample_cmap_lut
from fypa.viewer.widgets import _transparency_alpha


class _MarkerOverlayMixin:
    """Directive-pin markers, legend, board outline, stub copper and series bars."""

    # --- Directive-pin overlay -----------------------------------------------

    # Marker style per role — colours stand out against the viridis heatmap
    # AND on the dark viewbox background. ``symbol`` is a pyqtgraph marker
    # name (one of 'o', 's', 't', 'd', 'star', '+', 'x', 'p', 'h', 't1' …).
    # NOTE: keys match the role string produced by
    # fypa.altium.loader.build_solve_metadata() — that strips the trailing "Spec"
    # off the dataclass name and uppercases it (so ResistorSpec → RESISTOR).
    # Keyed by the role string seen in the relevant context: solved
    # directives carry the Spec class name (``RESISTOR`` for a SERIES
    # element — see fypa.altium.loader), while editor directives carry the
    # ``EDITOR_ROLES`` name (``SERIES``). Both keys map to the same style.
    _ROLE_MARKER_STYLE: dict[str, dict] = {
        "SOURCE":    {"symbol": "tri_up",   "color": "#ff3030", "size": 18, "label": "SOURCE"},
        "SINK":      {"symbol": "tri_down", "color": "#3aa8ff", "size": 16, "label": "SINK"},
        "RESISTOR":  {"symbol": "s",        "color": "#3aff8a", "size": 12, "label": "SERIES"},
        "SERIES":    {"symbol": "s",        "color": "#3aff8a", "size": 12, "label": "SERIES"},
        "REGULATOR": {"symbol": "d",        "color": "#ff66ff", "size": 14, "label": "REGULATOR"},
        # A link FYPA shorted on its own (Net Tie, 0 Ω resistor, jumper).
        # Same square as SERIES because that is what it is electrically, but
        # a dimmer green and its own legend row so it reads as inferred
        # rather than annotated, and can be toggled independently.
        "AUTO_BRIDGE": {"symbol": "s",      "color": "#1f9c5a", "size": 12,
                        "label": "SERIES (auto)"},
    }

    # --- Directive-pin marker + legend overlay -----------------------------

    # Glyph used in the legend swatch for each marker symbol name. Drawn
    # in the swatch column of the legend chip (right-side overlay).
    _LEGEND_GLYPHS: dict[str, str] = {
        "star":     "★",
        "o":        "●",
        "s":        "■",
        "d":        "◆",
        "bolt":     "⚡",
        "target":   "◎",
        "tri_up":   "▲",
        "tri_down": "▼",
    }

    def _refresh_outlines(self, layer_probes: list[dict],
                           phys_list: list[str],
                           rail_names: list[str] | str | None = None) -> None:
        """Push the combined layer-outline + pad-outline overlay to the GL
        viewer as one GL_LINES batch.

        Layer outlines (gated by ``_outlines_btn``) trace each visible
        (layer, net) shape in the physical layer's swatch colour. Stub
        outlines (same gate, same colour) trace the no-current copper
        pieces that the FEM filter excluded — these aren't in
        ``layer_probes`` because there's no FEM solution for them, so we
        walk ``metadata['stubs']`` separately.

        Each segment is promoted to 3D with z = the layer's stackup z, so
        the outline sits flush with the copper's top face. In 3D mode a
        second copy is emitted at the BOTTOM of the extruded copper prism
        (``z - copper_thickness``) so the polygon is traced on both faces
        of the plate. Empty result clears the overlay.
        """
        in_3d = self.view_3d_box.isChecked()
        thickness = self._COPPER_THICKNESS_MM if in_3d else 0.0
        pos_chunks: list[np.ndarray] = []
        col_chunks: list[np.ndarray] = []

        def _emit(segs_xy: np.ndarray, z_top: float,
                  rgb: np.ndarray) -> None:
            if segs_xy.size == 0:
                return
            segs_top = np.empty((segs_xy.shape[0], 3), dtype=np.float32)
            segs_top[:, :2] = segs_xy
            segs_top[:, 2] = z_top
            pos_chunks.append(segs_top)
            col_chunks.append(np.broadcast_to(rgb,
                                              (segs_xy.shape[0], 3)).copy())
            if thickness > 0.0:
                segs_bot = np.empty((segs_xy.shape[0], 3), dtype=np.float32)
                segs_bot[:, :2] = segs_xy
                segs_bot[:, 2] = z_top - thickness
                pos_chunks.append(segs_bot)
                col_chunks.append(
                    np.broadcast_to(rgb, (segs_xy.shape[0], 3)).copy()
                )

        if self._outlines_btn.isOn():
            for lp in layer_probes:
                segs = lp.get("outline_segments")
                if segs is None or segs.size == 0:
                    continue
                phys = lp.get("physical", "")
                qc = QColor(self._layer_color_for(phys))
                rgb = np.array([qc.redF(), qc.greenF(), qc.blueF()],
                               dtype=np.float32)
                _emit(segs, self._layer_z_for(phys), rgb)

            # Stub copper has no FEM solution and is absent from
            # layer_probes, but the user still expects to see the same
            # layer-coloured outline around it (it's real copper). Walk
            # metadata['stubs'] using the same visible-layer + rail-net
            # filter that _push_stubs applies, so the outline overlay
            # tracks exactly what the grey fill is showing.
            if self.metadata is not None:
                stubs = self.metadata.get("stubs") or []
                if stubs:
                    visible_layer_ids: dict[int, str] = {}
                    for phys in phys_list:
                        lid = self._phys_name_to_layer_id.get(phys)
                        if lid is not None:
                            visible_layer_ids[lid] = phys
                    rail_members = (set(self._effective_rail_members(rail_names))
                                    if rail_names is not None else set())
                    for stub in stubs:
                        lid = stub.get("layer_id")
                        phys = visible_layer_ids.get(lid)
                        if phys is None:
                            continue
                        net = stub.get("net")
                        if rail_members and net not in rail_members:
                            continue
                        segs = self._stub_outline_segments(stub)
                        if segs.size == 0:
                            continue
                        qc = QColor(self._layer_color_for(phys))
                        rgb = np.array([qc.redF(), qc.greenF(), qc.blueF()],
                                       dtype=np.float32)
                        _emit(segs, self._layer_z_for(phys), rgb)

        if pos_chunks:
            self._gl_viewer.set_outlines(
                np.concatenate(pos_chunks, axis=0),
                np.concatenate(col_chunks, axis=0),
            )
        else:
            self._gl_viewer.clear_outlines()

    # Half-width of the board-outline ribbon in mm. 0.2 mm reads as a
    # bold accent at typical board zooms without obscuring fine copper
    # features near the edge.
    _BOARD_OUTLINE_HALF_WIDTH_MM: float = 0.1

    def _refresh_board_outline(self) -> None:
        """Push or clear the board-outline ribbon overlay.

        The metadata ``board_outline`` is a closed polyline of [x, y] in
        mm (origin-corrected). It's triangulated into a fixed-mm-wide
        ribbon with mitered interior joins so the line thickness is
        uniform across drivers and reads boldly at any zoom (a raw
        GL_LINES pass would be clamped to 1 px on most Core profile
        drivers). Drawn on top of the heatmap in both 2D and 3D modes.
        """
        bo_state = self._overlay_state.get("board_outline", {}).get("both", {})
        if bo_state.get("vis") is None:
            self._gl_viewer.clear_board_outline()
            self._sync_navlib_frame_bounds()
            return
        points = (self.metadata or {}).get("board_outline") or []
        if len(points) < 3:
            self._gl_viewer.clear_board_outline()
            self._sync_navlib_frame_bounds()
            return

        ring = np.asarray(points, dtype=np.float64)
        n = ring.shape[0]
        # Per-vertex mitered offset normal: average of the incoming and
        # outgoing edge normals, scaled so the projected offset matches
        # the requested half-width along each adjacent edge. Clamped to
        # 4× half-width to keep sharp corners from spiking out.
        edges = np.roll(ring, -1, axis=0) - ring
        edge_len = np.linalg.norm(edges, axis=1)
        edge_len[edge_len == 0.0] = 1.0
        edge_dir = edges / edge_len[:, None]
        # Right-hand normal in 2D: (dx, dy) -> (dy, -dx).
        edge_normal = np.empty_like(edge_dir)
        edge_normal[:, 0] = edge_dir[:, 1]
        edge_normal[:, 1] = -edge_dir[:, 0]
        prev_normal = np.roll(edge_normal, 1, axis=0)
        bisector = edge_normal + prev_normal
        bis_len = np.linalg.norm(bisector, axis=1)
        # Degenerate (collinear reversal) — fall back to the outgoing normal.
        flat = bis_len < 1e-9
        bisector[flat] = edge_normal[flat]
        bis_len[flat] = 1.0
        bisector /= bis_len[:, None]
        # Miter length: half-width / cos(theta/2) = half-width / dot(bisector, normal).
        hw = self._BOARD_OUTLINE_HALF_WIDTH_MM
        dot = np.einsum("ij,ij->i", bisector, edge_normal)
        dot = np.where(np.abs(dot) < 0.25, np.sign(dot) * 0.25, dot)
        miter_len = hw / dot
        offset = bisector * miter_len[:, None]
        outer = ring + offset
        inner = ring - offset

        # Build GL_TRIANGLES (one quad per edge = 2 triangles = 6 verts).
        idx = np.arange(n)
        nxt = (idx + 1) % n
        o_a = outer[idx]
        i_a = inner[idx]
        o_b = outer[nxt]
        i_b = inner[nxt]
        # tri 1: o_a, i_a, o_b ;  tri 2: o_b, i_a, i_b
        positions_2d = np.empty((n * 6, 2), dtype=np.float32)
        positions_2d[0::6] = o_a
        positions_2d[1::6] = i_a
        positions_2d[2::6] = o_b
        positions_2d[3::6] = o_b
        positions_2d[4::6] = i_a
        positions_2d[5::6] = i_b
        positions = np.zeros((positions_2d.shape[0], 3), dtype=np.float32)
        positions[:, :2] = positions_2d
        # Lift slightly above z=0 in 3D so the ribbon doesn't z-fight
        # with anything drawn at the same plane.
        if self.view_3d_box.isChecked():
            positions[:, 2] = 0.01

        # User-set colour for the board-outline row (defaults to a bold
        # warm orange — reads on both viridis and the standard dark/light
        # themes without being mistaken for any of the layer swatches).
        # Alpha follows the row's TransparencyButton.
        colour = np.asarray(self._overlay_colors["board_outline"],
                            dtype=np.float32)
        alpha = _transparency_alpha(bo_state.get("alpha_step", 0))
        colors = np.empty((positions.shape[0], 4), dtype=np.float32)
        colors[:, :3] = colour
        colors[:, 3] = alpha
        self._gl_viewer.set_board_outline(positions, colors)
        self._sync_navlib_frame_bounds()

    # RGB of the stub-copper polygons — dim grey so they're visibly
    # present but obviously distinct from the heatmap LUT colours.
    # The viewer's clear-colour is black, so this sits clearly above the
    # background without competing with the viridis-coloured rails.
    _STUB_COLOR_RGB: tuple[float, float, float] = (
        0x60 / 255.0, 0x60 / 255.0, 0x60 / 255.0,
    )

    def _stubs_coloured_by_voltage(self, mode: str | None) -> bool:
        """True when stub copper is shaded from the active colour scheme
        rather than flat grey.

        Stub voltage colouring is opt-in (the ``colour_stubs_box`` toggle)
        and only meaningful for the Voltage / Voltage Drop modes — current
        and power are zero inside a stub by definition. When this returns
        False a colour-scheme change can't affect the stubs, so the fast
        recolour path (:meth:`_recolor_overlays`) can skip re-pushing them.
        """
        return (
            getattr(self, "colour_stubs_box", None) is not None
            and not self.colour_stubs_box.isChecked()
            and mode in ("Voltage", _VOLTAGE_DROP_MODE)
        )

    def _push_stubs(self, phys_list: list[str],
                    rail_names: list[str] | str,
                    *, mode: str | None = None,
                    drop_reference: float | None = None) -> None:
        """Build triangle geometry for every stub copper piece on the
        visible layers + selected rail groups, and push it as one batch
        to the GL viewer.

        Stubs are copper pieces the FEM filter excluded (no current
        path through them). We render them anyway so the user can SEE
        the copper exists; otherwise the heatmap looks like the copper
        vanished. By default each stub is flat dim grey (obviously
        "no data"); when the user toggles ``colour_stubs_box`` AND the
        active mode is Voltage / Voltage Drop, each stub is instead
        coloured by its approximate voltage — sampled from the same-net
        solved layer at the stub's centroid. Voltage is constant across
        each stub (no current flows through it).

        The triangle *positions* depend only on the visible layer/rail
        set and the 2D/3D z — never on the colour scheme or scale — so
        they're cached (single entry, keyed on exactly those inputs).
        A colour-scheme / scale change reuses the cached positions and
        only re-bakes the per-vertex colour array, which is what makes
        the :meth:`_recolor_overlays` fast path cheap. Per-stub
        triangulation is itself cached on the stub dict, so even a cache
        miss just re-aggregates cached triangles.
        """
        if self.metadata is None or not phys_list:
            self._gl_viewer.clear_stub_triangles()
            return
        stubs = self.metadata.get("stubs") or []
        if not stubs:
            self._gl_viewer.clear_stub_triangles()
            return
        visible_layer_ids: dict[int, str] = {}
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                visible_layer_ids[lid] = phys
        if not visible_layer_ids:
            self._gl_viewer.clear_stub_triangles()
            return
        rail_members = set(self._effective_rail_members(rail_names))
        in_3d = self._gl_viewer.view_mode() == "3d"

        focus_nets = (frozenset(self._editor_focus_nets)
                      if self._editor_focus_active() else None)
        # Reuse cached positions when the visible stub set + z mode are
        # unchanged (e.g. this call arrived via a colour-scheme toggle).
        geom_key = (frozenset(visible_layer_ids), frozenset(rail_members),
                    in_3d, focus_nets)
        cache = self._stub_geom_cache
        if cache is not None and cache[0] == geom_key:
            positions, spans = cache[1], cache[2]
        else:
            positions, spans = self._build_stub_geometry(
                stubs, visible_layer_ids, rail_members, in_3d,
                focus_nets=focus_nets)
            self._stub_geom_cache = (geom_key, positions, spans)

        if positions is None:
            self._gl_viewer.clear_stub_triangles()
            return
        colors = self._bake_stub_colors(spans, mode, drop_reference)
        self._gl_viewer.set_stub_triangles(positions, colors)

    def _build_stub_geometry(
        self, stubs: list[dict], visible_layer_ids: dict[int, str],
        rail_members: set[str], in_3d: bool,
        *, focus_nets: frozenset | None = None,
    ) -> tuple[np.ndarray | None, list[tuple[dict, str | None, int]]]:
        """Aggregate the stub triangle positions for the visible stub set.

        Returns ``(positions, spans)`` — ``positions`` is the concatenated
        ``(M, 3)`` float32 vertex array (or ``None`` when no stub is
        visible) and ``spans`` is a parallel list of
        ``(stub, net, vertex_count)`` so :meth:`_bake_stub_colors` can
        rebuild the colour array without re-triangulating. Triangulation
        itself is cached per stub by :meth:`_triangulate_stub`.

        ``focus_nets``: the editor net focus, when one is held — stubs
        outside it are dropped from the batch, matching the all-copper skip
        in :meth:`_refresh_overlay_geometry`.
        """
        pos_chunks: list[np.ndarray] = []
        spans: list[tuple[dict, str | None, int]] = []
        for stub in stubs:
            lid = stub.get("layer_id")
            if lid not in visible_layer_ids:
                continue
            net = stub.get("net")
            if rail_members and net not in rail_members:
                continue
            if focus_nets is not None and net not in focus_nets:
                continue
            tris = self._triangulate_stub(stub)
            if tris.size == 0:
                continue
            z = (self._layer_z_for(visible_layer_ids[lid])
                 if in_3d else 0.0)
            xyz = np.empty((tris.shape[0], 3), dtype=np.float32)
            xyz[:, :2] = tris
            xyz[:, 2] = z
            pos_chunks.append(xyz)
            spans.append((stub, net, xyz.shape[0]))
        if not pos_chunks:
            return None, []
        return np.concatenate(pos_chunks, axis=0), spans

    def _bake_stub_colors(
        self, spans: list[tuple[dict, str | None, int]],
        mode: str | None, drop_reference: float | None,
    ) -> np.ndarray:
        """Build the per-vertex stub colour array for cached geometry.

        Each stub is flat dim grey unless stub voltage-colouring is active
        (:meth:`_stubs_coloured_by_voltage`), in which case it takes the
        LUT colour of its sampled centroid voltage. ``spans`` is the list
        :meth:`_build_stub_geometry` returned alongside the positions.
        """
        total = sum(n for _stub, _net, n in spans)
        colors = np.empty((total, 3), dtype=np.float32)
        default_color = np.asarray(self._STUB_COLOR_RGB, dtype=np.float32)

        # Voltage-coloured mode only makes sense for Voltage / Voltage
        # Drop modes. Current density and power density are zero in a
        # stub by definition — colouring stubs with the lowest LUT entry
        # would be visually misleading, so fall back to grey.
        colour_by_v = self._stubs_coloured_by_voltage(mode)
        lut: np.ndarray | None = None
        vmin = vmax = 0.0
        if colour_by_v:
            lut = _build_cmap_lut(self._cmap_name)
            vmin = float(self._vmin)
            vmax = float(self._vmax)
            if vmax <= vmin:
                vmax = vmin + 1e-30
        use_drop = colour_by_v and mode == _VOLTAGE_DROP_MODE
        drop_ref_f = (float(drop_reference)
                      if (use_drop and drop_reference is not None) else 0.0)

        offset = 0
        for stub, net, n in spans:
            piece_color = default_color
            if colour_by_v and lut is not None:
                v_sample = self._sample_stub_voltage(stub, net)
                if v_sample is not None:
                    val = v_sample - drop_ref_f if use_drop else v_sample
                    piece_color = self._lut_lookup(lut, val, vmin, vmax)
            colors[offset:offset + n] = piece_color
            offset += n
        return colors

    def _sample_stub_voltage(self, stub: dict,
                             net_name: str | None) -> float | None:
        """Estimate the voltage of a stub piece by sampling the same-net
        FEM solution at the stub's centroid.

        Tries every physical layer that has a solved (layer, net) pair.
        Caches the answer on the stub dict — voltages are constant per
        solve so the lookup only needs to happen once per stub.
        """
        if net_name is None:
            return None
        cached = stub.get("_v_sample_cache")
        if cached is not None:
            return cached if cached != "missing" else None
        cx, cy = stub.get("_centroid", (None, None))
        if cx is None:
            from shapely.geometry import Polygon as _Polygon
            ext = stub.get("exterior")
            # Accept numpy (N, 2) array or legacy nested-list; shapely's
            # Polygon takes either via its sequence-of-coordinates ctor.
            if ext is None or (hasattr(ext, "size") and ext.size == 0):
                stub["_v_sample_cache"] = "missing"
                return None
            poly = _Polygon(ext, holes=stub.get("holes") or [])
            if poly.is_empty:
                stub["_v_sample_cache"] = "missing"
                return None
            c = poly.centroid
            cx, cy = float(c.x), float(c.y)
            stub["_centroid"] = (cx, cy)
        for phys in self._physicals:
            v = self._sample_via_voltage(phys, net_name, cx, cy)
            if v is not None:
                stub["_v_sample_cache"] = v
                return v
        # Centroid lies outside all solved meshes for this net (the stub is
        # geometrically disconnected from the solved copper, which is exactly
        # why it's a stub). Fall back to the nearest solved vertex so that
        # colour-by-V can still assign a meaningful voltage.
        v = self._nearest_vertex_voltage(net_name, cx, cy)
        if v is not None:
            stub["_v_sample_cache"] = v
            return v
        stub["_v_sample_cache"] = "missing"
        return None

    def _nearest_vertex_voltage(self, net_name: str,
                                cx: float, cy: float) -> float | None:
        """Return the voltage of the nearest solved vertex for *net_name*.

        Scans every (physical layer, net_name) pair in the solution and
        returns the potential of the vertex closest to (cx, cy). Used as a
        fallback when centroid interpolation fails because the stub centroid
        lies outside the triangulated solved-copper region.
        """
        best_dist_sq = float("inf")
        best_v: float | None = None
        for phys in self._physicals:
            li = self._index_by_pair.get((phys, net_name))
            if li is None:
                continue
            ls = self.solution.layer_solutions[li]
            for xys, pot in zip(ls.vertex_xys, ls.potentials):
                if xys.shape[0] == 0:
                    continue
                dx = xys[:, 0] - cx
                dy = xys[:, 1] - cy
                d2 = dx * dx + dy * dy
                idx = int(np.argmin(d2))
                if d2[idx] < best_dist_sq:
                    best_dist_sq = d2[idx]
                    best_v = float(pot[idx])
        return best_v

    # World-space half-width (mm) of the colored part of the series-component bar.
    # Total colored width = 2 × this = 0.2 mm.
    _SERIES_BAR_HALF_WIDTH_MM: float = 0.2
    # Extra half-width (mm) added on each side for the black outline border.
    # Total bar width including outline = 2 × (hw + border) = 0.3 mm.
    _SERIES_BAR_BORDER_MM: float = 0.05
    # How far (mm) the bar's z sits proud of the copper centroid z on the
    # top and bottom copper layers, so it visually rests on the surface.
    _SERIES_BAR_Z_LIFT_MM: float = 0.001
    # Extrusion height (mm) of the bar above its mounting surface in 3D mode.
    # Set to 0 to revert to a flat rectangle.  Tunable — increase for more
    # visual prominence, decrease if it obscures nearby copper detail.
    _SERIES_BAR_HEIGHT_MM: float = 0.005

    def _push_series_bars(self, phys_list: list[str],
                          rail_names: list[str] | str,
                          mode: str,
                          drop_reference: float | None = None) -> None:
        """Build gradient-filled rectangle geometry for every RESISTOR
        (series) directive visible on the current layers + selected rails,
        and push it as one triangle batch to the GL viewer.

        Each bar runs between the centroid of terminal "P" pins and the
        centroid of terminal "N" pins. The rectangle is ``_SERIES_BAR_HALF_WIDTH_MM``
        wide (world-space mm) and is coloured by the active heatmap mode:

        * **Voltage / Voltage Drop** — smooth gradient from the P-side
          heatmap colour to the N-side colour.
        * **Current Density** — uniform colour computed from I = ΔV / R.
        * **Power Density** — uniform colour from P = I² · R.
        """
        if self.metadata is None or not phys_list:
            self._gl_viewer.clear_series_bars()
            return

        directives = self.metadata.get("directives") or []
        rail_members = set(self._effective_rail_members(rail_names))
        target_layer_ids: set[int] = {
            self._phys_name_to_layer_id[p]
            for p in phys_list
            if p in self._phys_name_to_layer_id
        }
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        in_3d = self.view_3d_box.isChecked()

        lut = _build_cmap_lut(self._cmap_name)
        vmin = float(self._vmin)
        vmax = float(self._vmax)
        if vmax <= vmin:
            vmax = vmin + 1e-30
        drop_ref = float(drop_reference) if drop_reference is not None else 0.0

        # Two chunk lists. ``under_*`` rects are drawn BEFORE the heatmap
        # mesh so a bottom-side bar sits visually beneath the bottom
        # copper in 2D (depth test is off in 2D, so order = layer order).
        # ``over_*`` rects are drawn after the mesh, on top. In 3D the
        # extruded bar's z handles ordering, so everything goes over.
        under_pos_chunks: list[np.ndarray] = []
        under_col_chunks: list[np.ndarray] = []
        over_pos_chunks: list[np.ndarray] = []
        over_col_chunks: list[np.ndarray] = []
        hw = self._SERIES_BAR_HALF_WIDTH_MM
        hw_out = hw + self._SERIES_BAR_BORDER_MM
        z_lift = self._SERIES_BAR_Z_LIFT_MM
        max_rank = len(self._physicals) - 1
        half_cu = self._COPPER_THICKNESS_MM / 2.0
        black = (0.0, 0.0, 0.0)

        def _bar_is_on_bottom(phys1: str | None,
                              phys2: str | None) -> bool:
            r1 = self._phys_stackup_rank.get(phys1, 0) if phys1 else 0
            r2 = self._phys_stackup_rank.get(phys2, 0) if phys2 else 0
            return min(r1, r2) == max_rank

        def _z_for_bar(phys1: str | None,
                       phys2: str | None) -> tuple[float, float]:
            """Return ``(z_base, extrude_sign)`` for the bar.

            z_base: the z the bar sits on (copper surface + tiny lift).
            extrude_sign: +1 when the bar extrudes away from the viewer
            in the +z direction (top layer / inner layers); −1 when it
            extrudes toward −z (bottom copper layer faces downward).
            """
            z1 = self._layer_z_for(phys1) if phys1 else 0.0
            z2 = self._layer_z_for(phys2) if phys2 else 0.0
            z_mid = (z1 + z2) * 0.5
            if _bar_is_on_bottom(phys1, phys2):
                return z_mid - half_cu - z_lift, -1.0
            return z_mid + half_cu + z_lift, 1.0

        for d in directives:
            if d.get("role") != "RESISTOR":
                continue
            terminals = d.get("terminals") or {}
            p_term = terminals.get("P") or {}
            n_term = terminals.get("N") or {}
            p_pins = p_term.get("pins") or []
            n_pins = n_term.get("pins") or []
            if not p_pins or not n_pins:
                continue

            # Find the first pin on a visible layer for each terminal.
            def _pick_pin(pins: list[dict]) -> dict | None:
                for pin in pins:
                    if pin.get("layer_id") in target_layer_ids:
                        return pin
                # fall back to any pin that has an associated visible phys
                return pins[0] if pins else None

            p_pin = _pick_pin(p_pins)
            n_pin = _pick_pin(n_pins)
            if p_pin is None or n_pin is None:
                continue

            # Skip if neither terminal pin is on the rail.
            if rail_members:
                p_net = p_pin.get("net", "")
                n_net = n_pin.get("net", "")
                if p_net not in rail_members and n_net not in rail_members:
                    continue

            x1 = float(p_pin.get("x_mm", 0.0))
            y1 = float(p_pin.get("y_mm", 0.0))
            x2 = float(n_pin.get("x_mm", 0.0))
            y2 = float(n_pin.get("y_mm", 0.0))

            phys1 = id_to_phys.get(p_pin.get("layer_id"))
            phys2 = id_to_phys.get(n_pin.get("layer_id"))
            if in_3d:
                z, extrude_sign = _z_for_bar(phys1, phys2)
            else:
                z, extrude_sign = 0.0, 1.0
            # Route this bar's triangles to the right batch. In 2D the
            # bottom-side bar must draw under the mesh so the bottom
            # copper covers it; all others go over the mesh as usual.
            if not in_3d and _bar_is_on_bottom(phys1, phys2):
                pos_chunks = under_pos_chunks
                col_chunks = under_col_chunks
            else:
                pos_chunks = over_pos_chunks
                col_chunks = over_col_chunks

            # Sample voltage at each pin; fall back to the opposite pin's
            # net if the pin's own net has no solution on this layer.
            def _sample(pin: dict) -> float | None:
                lid = pin.get("layer_id")
                phys = id_to_phys.get(lid)
                if phys is None:
                    phys = phys_list[0]
                net = pin.get("net", "")
                v = self._sample_via_voltage(phys, net,
                                             float(pin.get("x_mm", 0.0)),
                                             float(pin.get("y_mm", 0.0)))
                if v is None:
                    # try any visible layer for this net
                    for pl in phys_list:
                        v = self._sample_via_voltage(
                            pl, net,
                            float(pin.get("x_mm", 0.0)),
                            float(pin.get("y_mm", 0.0)),
                        )
                        if v is not None:
                            break
                return v

            v1 = _sample(p_pin)
            v2 = _sample(n_pin)
            if v1 is None and v2 is None:
                continue
            if v1 is None:
                v1 = v2
            if v2 is None:
                v2 = v1

            # Convert raw voltages to the display value for this mode.
            if mode in ("Voltage", _VOLTAGE_DROP_MODE):
                val1 = v1 - drop_ref
                val2 = v2 - drop_ref
                c1 = self._shade(lut, val1, vmin, vmax)
                c2 = self._shade(lut, val2, vmin, vmax)
            elif mode == _VIA_CURRENT_MODE:
                # Via Current mode doesn't define a value for series
                # resistors — colour them with the same neutral shade
                # the copper uses so they read as context, not data.
                neutral_rgb = tuple(c / 255.0 for c in (160, 160, 160))
                c1 = c2 = neutral_rgb
            else:
                r_ohm = float(d.get("value") or 0.0)
                if r_ohm <= 0.0:
                    r_ohm = 1e-3
                i_abs = abs(v1 - v2) / r_ohm
                if mode == "Current Density":
                    val = i_abs
                else:  # Power Density
                    val = i_abs * i_abs * r_ohm
                c = self._shade(lut, val, vmin, vmax)
                c1 = c2 = c

            # Perpendicular unit vector for bar width.
            ddx, ddy = x2 - x1, y2 - y1
            length = math.sqrt(ddx * ddx + ddy * ddy)
            if length < 1e-6:
                continue
            nx, ny = -ddy / length, ddx / length

            # Outer black box corners (at hw_out).
            a_ox, a_oy = x1 + nx * hw_out, y1 + ny * hw_out  # P end, +side
            b_ox, b_oy = x1 - nx * hw_out, y1 - ny * hw_out  # P end, -side
            c_ox, c_oy = x2 - nx * hw_out, y2 - ny * hw_out  # N end, -side
            d_ox, d_oy = x2 + nx * hw_out, y2 + ny * hw_out  # N end, +side

            # Helper: horizontal quad (2 tris) at a constant z, colored
            # gradient from ca (P end) to cb (N end).
            def _hquad(hw_: float, z_: float, ca, cb) -> tuple:
                ax_, ay_ = x1 + nx * hw_, y1 + ny * hw_
                bx_, by_ = x1 - nx * hw_, y1 - ny * hw_
                cx__, cy__ = x2 - nx * hw_, y2 - ny * hw_
                dx__, dy__ = x2 + nx * hw_, y2 + ny * hw_
                pos_ = np.array([
                    [ax_, ay_, z_], [bx_, by_, z_], [cx__, cy__, z_],
                    [ax_, ay_, z_], [cx__, cy__, z_], [dx__, dy__, z_],
                ], dtype=np.float32)
                col_ = np.array([ca, ca, cb, ca, cb, cb], dtype=np.float32)
                return pos_, col_

            # Helper: vertical quad (2 tris) connecting edge (p1→p2) from
            # z_bot to z_top, colored ca at p1 end and cb at p2 end.
            def _vquad(p1x, p1y, p2x, p2y, z_bot, z_top, ca, cb) -> tuple:
                pos_ = np.array([
                    [p1x, p1y, z_bot], [p2x, p2y, z_bot], [p2x, p2y, z_top],
                    [p1x, p1y, z_bot], [p2x, p2y, z_top], [p1x, p1y, z_top],
                ], dtype=np.float32)
                col_ = np.array([ca, cb, cb, ca, cb, ca], dtype=np.float32)
                return pos_, col_

            # Helper: two-strip black outline frame around a coloured cap,
            # at the cap's own z. Sitting at the same z guarantees the
            # outline reads next to the cap regardless of how the box's
            # far black face depth-resolves on the viewer's GPU.
            def _hframe(z_: float) -> tuple:
                # +perp strip: from hw to hw_out on the +nx,+ny side
                ai_x, ai_y = x1 + nx * hw, y1 + ny * hw
                ao_x, ao_y = x1 + nx * hw_out, y1 + ny * hw_out
                di_x, di_y = x2 + nx * hw, y2 + ny * hw
                do_x, do_y = x2 + nx * hw_out, y2 + ny * hw_out
                # -perp strip
                bi_x, bi_y = x1 - nx * hw, y1 - ny * hw
                bo_x, bo_y = x1 - nx * hw_out, y1 - ny * hw_out
                ci_x, ci_y = x2 - nx * hw, y2 - ny * hw
                co_x, co_y = x2 - nx * hw_out, y2 - ny * hw_out
                pos_ = np.array([
                    # +perp strip: ai, ao, do | ai, do, di
                    [ai_x, ai_y, z_], [ao_x, ao_y, z_], [do_x, do_y, z_],
                    [ai_x, ai_y, z_], [do_x, do_y, z_], [di_x, di_y, z_],
                    # -perp strip: bi, ci, co | bi, co, bo
                    [bi_x, bi_y, z_], [ci_x, ci_y, z_], [co_x, co_y, z_],
                    [bi_x, bi_y, z_], [co_x, co_y, z_], [bo_x, bo_y, z_],
                ], dtype=np.float32)
                col_ = np.tile(np.array(black, dtype=np.float32), (12, 1))
                return pos_, col_

            height = self._SERIES_BAR_HEIGHT_MM
            if in_3d and height > 0.0:
                # 3D extruded box with colored caps on both ends so the
                # bar reads the same from above and below. Each cap is
                # paired with a coplanar black outline frame at the same
                # z, so the outline is anchored to the cap and can't be
                # lost when the far black face is occluded by copper or
                # falls outside depth-buffer precision.
                z_top = z + extrude_sign * height
                z_cap = z_top + extrude_sign * 5e-4  # outer cap, away from board
                z_cap_inner = z - extrude_sign * 5e-4  # inner cap, board side

                # Black box walls (4 sides). No top/base face needed —
                # the cap-coplanar frames close the silhouette.
                p, c = _vquad(a_ox, a_oy, d_ox, d_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _vquad(b_ox, b_oy, c_ox, c_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _vquad(a_ox, a_oy, b_ox, b_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _vquad(d_ox, d_oy, c_ox, c_oy, z, z_top, black, black)
                pos_chunks.append(p); col_chunks.append(c)

                # Colored heatmap caps + coplanar black outline frames.
                p, c = _hquad(hw, z_cap, c1, c2)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hframe(z_cap)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hquad(hw, z_cap_inner, c1, c2)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hframe(z_cap_inner)
                pos_chunks.append(p); col_chunks.append(c)
            else:
                # 2D flat (or height == 0): black outline rect + colored rect.
                z_fg = z + 5e-4
                p, c = _hquad(hw_out, z, black, black)
                pos_chunks.append(p); col_chunks.append(c)
                p, c = _hquad(hw, z_fg, c1, c2)
                pos_chunks.append(p); col_chunks.append(c)

        if not under_pos_chunks and not over_pos_chunks:
            self._gl_viewer.clear_series_bars()
            return
        all_pos = under_pos_chunks + over_pos_chunks
        all_col = under_col_chunks + over_col_chunks
        under_count = sum(c.shape[0] for c in under_pos_chunks)
        self._gl_viewer.set_series_bars(
            np.concatenate(all_pos, axis=0),
            np.concatenate(all_col, axis=0),
            under_mesh_count=under_count,
        )

    @staticmethod
    def _lut_lookup(lut: np.ndarray, value: float,
                    vmin: float, vmax: float) -> np.ndarray:
        """Look up an RGB triplet (float32, [0..1]) from the LUT for
        ``value`` normalised into ``[vmin, vmax]``."""
        if not np.isfinite(value):
            return np.asarray((0.5, 0.5, 0.5), dtype=np.float32)
        t = (value - vmin) / (vmax - vmin)
        t = max(0.0, min(1.0, t))
        idx = int(round(t * (lut.shape[0] - 1)))
        rgba = lut[idx]
        # LUT entries are uint8 RGBA; convert to float RGB.
        return np.asarray(rgba[:3], dtype=np.float32) / 255.0

    def _triangulate_stub(self, stub: dict) -> np.ndarray:
        """Triangulate a stub polygon, caching the result on the metadata dict.

        Returns an (N*3, 2) float32 array of vertex coords; consecutive
        triples form one GL_TRIANGLES triangle.

        Fast path: ``fypa.altium.loader._build_stub_record`` pre-triangulates
        every stub at solve time and ships the result in the pickle as
        ``stub['triangles_xy']`` — already in the (N*3, 2) float32 layout
        the GL stub batch wants, so the viewer just uploads it. Old
        pickles (or stubs where the loader's triangle call failed) fall
        through to the lazy Triangle-library path below.

        Lazy path: Shewchuk's Triangle (via the ``triangle`` PyPI
        package) does a constrained Delaunay of the exterior + interior
        rings with one hole marker per interior — fills non-convex shapes
        (including those with holes) exactly. The earlier
        ``shapely.ops.triangulate`` + ``poly.contains(centroid)``
        approach was a vertex-set Delaunay bounded by the convex hull:
        it left gaps in concavities and produced wrong fills for complex
        stub shapes (e.g. the expansion-board copper island with cutouts).
        """
        cached = stub.get("_tris_cache")
        if cached is not None:
            return cached
        prebuilt = stub.get("triangles_xy")
        if prebuilt is not None:
            arr = np.asarray(prebuilt, dtype=np.float32)
            stub["_tris_cache"] = arr
            return arr
        from shapely.geometry import Polygon as _Polygon
        ext = stub.get("exterior")
        if ext is None or (hasattr(ext, "size") and ext.size == 0) or (
                not hasattr(ext, "size") and not ext):
            exterior = []
        else:
            # Accept either a numpy (N, 2) array (new format) or a nested
            # list (legacy format) — shapely's Polygon takes both.
            exterior = ext
        holes = stub.get("holes") or []
        try:
            poly = _Polygon(exterior, holes=holes) if len(exterior) >= 3 else None
        except Exception:
            poly = None
        if poly is None or poly.is_empty:
            empty = np.empty((0, 2), dtype=np.float32)
            stub["_tris_cache"] = empty
            return empty

        # Shewchuk's Triangle (the C core behind the ``triangle`` package)
        # aborts the whole process — uncatchably, with no Python traceback
        # — on a degenerate PSLG: self-touching rings, zero-length edges or
        # duplicate vertices. ``all_copper`` rings are stored as float32,
        # which on a dense real-board copper pour routinely rounds
        # near-coincident vertices onto each other. Repair the polygon to
        # strict OGC validity before meshing it. This is exactly why
        # solid-fill "show all copper" crashed where wire-mesh — a
        # pure-numpy path that never touches Triangle — did not.
        if not poly.is_valid:
            try:
                repaired = poly.buffer(0)
            except Exception:
                repaired = None
            if repaired is not None and not repaired.is_empty:
                poly = repaired
        # Even on technically-valid polygons, shapely's ``unary_union`` of
        # many disk-capped Line buffers (one per Gerber track segment)
        # can leave tiny near-collinear vertex clusters along the trace
        # boundary: two segments running parallel at sub-precision float32
        # noise. Triangle interprets those as self-touching PSLG segments
        # and silently drops the triangles around them, producing small
        # V-shaped cuts in the solid fill of long traces. Simplify with a
        # tolerance just above the float32-coordinate noise floor (~1e-5
        # mm at typical board coordinates) and well below any real copper
        # feature size (typical PCB precision is 25 µm = 2.5e-2 mm).
        # 1e-4 mm = 100 nm welds the noise out without visibly shifting
        # any real curve or corner.
        try:
            simplified = poly.simplify(1e-4, preserve_topology=True)
        except Exception:
            simplified = None
        if (simplified is not None
                and not simplified.is_empty
                and simplified.is_valid):
            poly = simplified

        # buffer(0) can split an invalid polygon into several pieces; mesh
        # each component polygon on its own PSLG and concatenate (a single
        # shared PSLG would let Triangle fill the gaps between them).
        if poly.geom_type == "Polygon":
            parts = [poly]
        elif hasattr(poly, "geoms"):
            parts = [g for g in poly.geoms if g.geom_type == "Polygon"]
        else:
            parts = []
        chunks: list[np.ndarray] = []
        for part in parts:
            if part.is_empty:
                continue
            piece = self._triangulate_simple_polygon(part)
            if piece.size:
                chunks.append(piece)
        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        stub["_tris_cache"] = arr
        return arr

    def _merged_solid_all_copper_tris(
        self, layer_name: str, recs: list[dict],
        rail_members: set[str],
    ) -> np.ndarray | None:
        """Pre-merge a layer's all-copper polygons via shapely's
        :func:`unary_union` and triangulate the result, so a transparent
        solid fill blends each pixel exactly once.

        The source data sometimes carries overlapping polygons for the
        same layer (a GND plane plus smaller copper objects nominally
        sitting in its clearances, plus pad / via-clearance fills) — each
        overlap adds another blend pass at the layer's transparency
        alpha, cumulatively pushing the area to opacity even when the per-
        vertex alpha is low. Merging into one non-overlapping triangulation
        bypasses that. Cached per ``(layer_name, frozenset(rail_members))``
        — the metadata is fixed for the viewer's lifetime, so no other
        invalidation is needed.

        Returns ``None`` when the layer has no non-rail-member geometry.
        Wire-mesh fill draws thin outlines that don't cumulate noticeably,
        so the caller only routes through this for solid fill with
        ``layer_alpha < 1.0``.
        """
        key = (layer_name, frozenset(rail_members))
        cached = self._merged_all_copper_cache.get(key)
        if cached is not None:
            return cached if cached.size > 0 else None

        from shapely.geometry import Polygon as _Polygon
        from shapely.ops import unary_union

        polys = []
        for rec in recs:
            net = rec.get("net")
            if net in rail_members:
                continue
            for poly_dict in rec.get("polygons", []):
                ext = poly_dict.get("exterior")
                if ext is None:
                    continue
                if hasattr(ext, "size"):
                    if ext.size == 0:
                        continue
                elif not ext:
                    continue
                holes = poly_dict.get("holes") or []
                try:
                    p = _Polygon(ext, holes=holes)
                except Exception:
                    continue
                if p.is_empty:
                    continue
                if not p.is_valid:
                    try:
                        from shapely.validation import make_valid
                        p = make_valid(p)
                    except Exception:
                        continue
                    if p.is_empty:
                        continue
                polys.append(p)

        if not polys:
            self._merged_all_copper_cache[key] = np.empty(
                (0, 2), dtype=np.float32)
            return None

        try:
            merged = unary_union(polys)
        except Exception:
            merged = None

        if merged is None or merged.is_empty:
            self._merged_all_copper_cache[key] = np.empty(
                (0, 2), dtype=np.float32)
            return None

        # Strip the union down to its Polygon components — a
        # GeometryCollection may carry lower-dim leftovers we don't want
        # to triangulate.
        if merged.geom_type == "Polygon":
            components = [merged]
        elif merged.geom_type == "MultiPolygon":
            components = list(merged.geoms)
        else:
            components = [g for g in getattr(merged, "geoms", [])
                          if g.geom_type == "Polygon"]

        chunks: list[np.ndarray] = []
        for poly in components:
            if poly.is_empty:
                continue
            tris = self._triangulate_simple_polygon(poly)
            if tris.size > 0:
                chunks.append(tris)

        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        self._merged_all_copper_cache[key] = arr
        return arr if arr.size > 0 else None

    def _triangulate_simple_polygon(self, poly) -> np.ndarray:
        """Constrained-Delaunay triangulate one OGC-valid shapely Polygon
        into a flat ``(N*3, 2)`` float32 GL_TRIANGLES vertex soup.

        Returns an empty array only when both backends decline the
        polygon. The caller must hand in geometry that is already valid —
        see :meth:`_triangulate_stub` for the validity repair.

        Primary backend is Shewchuk's Triangle (the ``triangle`` PyPI
        package); on dense Gerber-derived pour rings it occasionally
        declines the PSLG silently (returns no triangles) — the polygon
        would then vanish from the all-copper overlay while remaining
        pickable, since the picker tests Shapely containment. The GEOS
        ``constrained_delaunay_triangles`` fallback is the same
        backend the Gerber stub-solution pre-triangulation uses
        successfully, so it picks up the cases Triangle drops.
        """
        verts, segs, hole_markers = self._poly_to_triangle_input(poly)
        if not verts or not segs:
            return self._triangulate_via_geos(poly)
        try:
            import triangle as _triangle
            tri_input: dict = {"vertices": verts, "segments": segs}
            if hole_markers:
                tri_input["holes"] = hole_markers
            # ``p`` = constrained planar straight-line graph triangulation;
            # ``Q`` silences Triangle's stdout. No ``q`` / ``a`` quality
            # switches — we just want a geometrically faithful fill, not
            # a FEM-quality mesh.
            out = _triangle.triangulate(tri_input, "pQ")
        except Exception:
            out = None

        out_verts = out.get("vertices") if out else None
        out_tris = out.get("triangles") if out else None
        if out_verts is None or out_tris is None or len(out_tris) == 0:
            return self._triangulate_via_geos(poly)
        v_arr = np.asarray(out_verts, dtype=np.float32)
        t_arr = np.asarray(out_tris, dtype=np.int32)
        # Expand the index list into a flat (N*3, 2) GL_TRIANGLES vertex
        # soup — matches the buffer layout the GL stub/overlay batch wants.
        return v_arr[t_arr.ravel()].astype(np.float32, copy=False)

    @staticmethod
    def _triangulate_via_geos(poly) -> np.ndarray:
        """GEOS constrained-Delaunay fallback for the Triangle-library
        path in :meth:`_triangulate_simple_polygon`. Returns the same
        flat ``(N*3, 2)`` float32 GL_TRIANGLES soup, or an empty array
        if GEOS also can't triangulate the polygon."""
        try:
            import shapely
            tri_coll = shapely.constrained_delaunay_triangles(poly)
        except Exception:
            return np.empty((0, 2), dtype=np.float32)
        if tri_coll is None or tri_coll.is_empty:
            return np.empty((0, 2), dtype=np.float32)
        tris = list(getattr(tri_coll, "geoms", [tri_coll]))
        coords: list[tuple[float, float]] = []
        for t in tris:
            if t.is_empty:
                continue
            ring = list(t.exterior.coords)
            if len(ring) < 4:
                continue
            coords.append(ring[0])
            coords.append(ring[1])
            coords.append(ring[2])
        if not coords:
            return np.empty((0, 2), dtype=np.float32)
        return np.asarray(coords, dtype=np.float32)

    @staticmethod
    def _poly_to_triangle_input(poly) -> tuple[
        list[tuple[float, float]],
        list[tuple[int, int]],
        list[tuple[float, float]],
    ]:
        """Convert a shapely Polygon into Triangle-library input.

        Returns ``(vertices, segments, hole_markers)``. Each ring (the
        exterior + every interior) contributes its vertices once (the
        duplicated closing vertex is dropped) and a closed loop of
        segment indices. Hole markers are representative points inside
        each interior ring, which tells Triangle to leave the hole
        region un-meshed. Shared with the stub-outline builder so the
        outline and the triangulation see exactly the same geometry.
        """
        from shapely.geometry import Polygon as _Polygon
        verts: list[tuple[float, float]] = []
        segs: list[tuple[int, int]] = []
        hole_markers: list[tuple[float, float]] = []

        # Float32-rounded pour rings can carry coincident vertices that
        # survive as zero-length PSLG edges; Triangle's C core aborts on
        # those, so merge anything closer than this (0.1 µm — far below
        # the finest real copper feature).
        eps = 1e-4

        def _add_ring(ring) -> None:
            if ring is None or ring.is_empty:
                return
            coords = list(ring.coords)
            if len(coords) >= 2 and coords[0] == coords[-1]:
                coords = coords[:-1]
            # Drop consecutive coincident vertices (and the wrap-around
            # duplicate) so every PSLG segment has non-zero length.
            cleaned: list[tuple[float, float]] = []
            for x, y in coords:
                fx, fy = float(x), float(y)
                if cleaned and abs(fx - cleaned[-1][0]) <= eps \
                        and abs(fy - cleaned[-1][1]) <= eps:
                    continue
                cleaned.append((fx, fy))
            while len(cleaned) >= 2 \
                    and abs(cleaned[0][0] - cleaned[-1][0]) <= eps \
                    and abs(cleaned[0][1] - cleaned[-1][1]) <= eps:
                cleaned.pop()
            if len(cleaned) < 3:
                return
            i_first = len(verts)
            verts.extend(cleaned)
            n = len(cleaned)
            for i in range(n):
                segs.append((i_first + i, i_first + (i + 1) % n))

        _add_ring(poly.exterior)
        for hole_ring in poly.interiors:
            _add_ring(hole_ring)
            try:
                hp = _Polygon(hole_ring).representative_point()
                hole_markers.append((float(hp.x), float(hp.y)))
            except Exception:
                continue

        # Weld globally-coincident vertices. The per-ring pass above only
        # drops *consecutive* duplicates; float32-stored pour rings also
        # round genuinely-distinct, non-adjacent vertices onto the exact
        # same coordinate (within and across rings). Shewchuk's Triangle
        # segfaults — silently, no traceback, taking the GUI with it — the
        # instant its vertex list holds a duplicate or any non-finite
        # coordinate, so collapse exact duplicates and bail on NaN/Inf
        # before the PSLG ever reaches the C core.
        if verts:
            welded: list[tuple[float, float]] = []
            index_of: dict[tuple[float, float], int] = {}
            remap: list[int] = []
            for vx, vy in verts:
                if not (math.isfinite(vx) and math.isfinite(vy)):
                    return [], [], []   # Triangle would segfault on NaN/Inf
                key = (vx, vy)
                idx = index_of.get(key)
                if idx is None:
                    idx = len(welded)
                    index_of[key] = idx
                    welded.append(key)
                remap.append(idx)
            clean_segs: list[tuple[int, int]] = []
            seen_segs: set[tuple[int, int]] = set()
            for a, b in segs:
                ra, rb = remap[a], remap[b]
                if ra == rb:
                    continue                       # collapsed to zero length
                ordered = (ra, rb) if ra < rb else (rb, ra)
                if ordered in seen_segs:
                    continue                       # duplicate constraint edge
                seen_segs.add(ordered)
                clean_segs.append((ra, rb))
            verts, segs = welded, clean_segs
        return verts, segs, hole_markers

    def _stub_outline_segments(self, stub: dict) -> np.ndarray:
        """Build (and cache) GL_LINES outline segments for a stub piece.

        Returns an ``(N, 2)`` float32 array where consecutive vertex
        pairs form one segment. Traces the exterior + every hole; result
        is cached on the stub dict so toggling the outline overlay is a
        pure buffer upload.

        Accepts both the new pickle format (numpy ``(N, 2)`` float32 arrays
        for exterior + each hole) and the legacy format (nested Python
        lists). Both go through ``np.asarray`` so the downstream code is
        identical.
        """
        cached = stub.get("_outline_cache")
        if cached is not None:
            return cached
        exterior = stub.get("exterior")
        if exterior is None:
            exterior_arr = np.empty((0, 2), dtype=np.float32)
        else:
            exterior_arr = np.asarray(exterior, dtype=np.float32)
        holes = stub.get("holes") or []
        rings: list[np.ndarray] = []
        if exterior_arr.shape[0] >= 3:
            rings.append(exterior_arr)
        for hole in holes:
            hole_arr = np.asarray(hole, dtype=np.float32)
            if hole_arr.shape[0] >= 3:
                rings.append(hole_arr)
        pairs_chunks: list[np.ndarray] = []
        for ring in rings:
            # Close the ring if the source data didn't (extraction stores
            # exterior/holes as open rings — first vertex != last).
            if not np.allclose(ring[0], ring[-1]):
                ring = np.vstack([ring, ring[:1]])
            if ring.shape[0] < 2:
                continue
            pairs = np.empty((2 * (ring.shape[0] - 1), 2), dtype=np.float32)
            pairs[0::2] = ring[:-1]
            pairs[1::2] = ring[1:]
            pairs_chunks.append(pairs)
        if not pairs_chunks:
            arr = np.empty((0, 2), dtype=np.float32)
        else:
            arr = np.concatenate(pairs_chunks, axis=0)
        stub["_outline_cache"] = arr
        return arr

    def _ensure_gl_cmap(self, kind: str) -> None:
        """Make sure the LUT uploaded to the GL viewer's copper-mesh
        cmap texture matches ``kind``.

        ``kind`` is ``"data"`` (the viridis ramp keyed on per-vertex
        values — every mode except Via Current) or ``"neutral"`` (a
        flat-grey LUT used in Via Current mode so the copper renders
        as context behind the heatmapped vias). The viewer caches the
        active kind so this is a no-op when the LUT hasn't changed.
        """
        if self._gl_cmap_kind == kind:
            return
        if kind == "neutral":
            self._gl_viewer.set_colormap(_build_neutral_cmap_lut())
        else:
            self._gl_viewer.set_colormap(_build_cmap_lut(self._cmap_name))
        self._gl_cmap_kind = kind

    def _gl_scale(self, v):
        """Map real heatmap value(s) into the space the colour lookup
        runs in: identity on a linear scale, ``log10`` (floored at
        ``_log_floor``) on a log scale. Accepts a scalar or an ndarray
        and never mutates its input.

        Pushing both the per-vertex values and the level clamps through
        this keeps the GL viewer's linear normalisation shader correct
        for the log scale with no shader change — see :meth:`_render`."""
        if not self._log_active:
            return v
        return np.log10(np.maximum(v, self._log_floor))

    def _shade(self, lut: np.ndarray, value: float,
               vmin: float, vmax: float) -> tuple[float, float, float]:
        """LUT colour for one scalar value, honouring the active linear /
        log scale. Wraps :func:`_sample_cmap_lut` so the CPU-baked
        overlays (via cylinders, series bars, 2D via markers) pick up the
        log scale the same way the GPU-shaded copper mesh does."""
        return _sample_cmap_lut(
            lut, self._gl_scale(value),
            self._gl_scale(vmin), self._gl_scale(vmax))

    def _on_scale_type_changed(self, is_log: bool) -> None:
        """The Linear / Logarithmic dropdown changed — re-render so the
        copper mesh, the via overlays and the gradient strip all switch
        scale together. ``_render`` re-derives ``_log_active`` (the
        dropdown is disabled for ineligible modes, but guard anyway)."""
        if is_log == self._log_scale:
            return
        self._log_scale = is_log
        self._render()

    def _via_current_lookup_and_range(
        self, rail_names: list[str],
    ) -> tuple[float, float, dict[tuple[str, float, float], float]]:
        """For Via Current mode: return ``(vmin, vmax, lookup)``.

        ``lookup`` maps ``(net, x_mm, y_mm)`` to the via's
        max-|segment-current| (matches the Vias-tab ``current`` column)
        for every via whose net is in the effective rail set. The
        range is taken across that whole set — independent of which
        physical layers are toggled visible — so flipping layers
        doesn't move the colormap. Falls back to ``(0.0, 1.0)`` when
        no via matches the rail filter.
        """
        rail_members = set(self._effective_rail_members(rail_names))
        rows = self._get_or_compute_via_rows()
        lookup: dict[tuple[str, float, float], float] = {}
        for r in rows:
            net = r.get("net", "")
            if rail_members and net not in rail_members:
                continue
            cur = r.get("current")
            if cur is None:
                continue
            cur_f = float(cur)
            if not math.isfinite(cur_f):
                continue
            key = (net,
                   float(r.get("x_mm", 0.0)),
                   float(r.get("y_mm", 0.0)))
            lookup[key] = cur_f
        if not lookup:
            return 0.0, 1.0, lookup
        vals = list(lookup.values())
        vmin = min(vals)
        vmax = max(vals)
        if vmax <= vmin:
            vmax = vmin + 1e-12
        return vmin, vmax, lookup
