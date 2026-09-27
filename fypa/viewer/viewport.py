"""Fixed-scale viewport, mouse hover, 3D view toggle and keyboard hotkeys."""
from __future__ import annotations

import numpy as np
from PySide6.QtGui import QKeySequence, QShortcut

from fypa.viewer.diagnostics import _apply_mesh_failure_highlights
from fypa.viewer.display import _build_cmap_lut, _VIA_CURRENT_MODE
from fypa.viewer.overlays import _rec_slot_tuple


class _ViewportMixin:
    """Fixed-scale viewport, mouse hover, 3D view toggle and keyboard hotkeys."""

    # --- CAD-style fixed-scale viewport (via GLMeshViewer) -----------------

    def _sync_navlib_frame_bounds(self) -> None:
        """Keep NavLib Fit framing in sync with the board-outline box."""
        if self._gl_viewer is None:
            return
        self._gl_viewer.set_navlib_frame_bounds(
            self._board_outline_bounds() or self._data_bounds,
        )

    def _fit_board_to_view(self) -> None:
        """User-triggered fit — frames board outline (or mesh) with margin."""
        if self._gl_viewer is None or not self._gl_viewer.isVisible():
            return
        self._sync_navlib_frame_bounds()
        bounds = self._board_outline_bounds() or self._data_bounds
        if bounds is None:
            return
        if self.view_3d_box.isChecked():
            self._gl_viewer.fit_3d_view()
        else:
            self._gl_viewer.fit_to_bounds(*bounds, padding=1.08)

    def _fit_board_to_canvas(self) -> None:
        """Frame the board in the GL canvas, with a little margin, centred —
        the one-time fit applied when a design is first shown. No-op after
        it succeeds, so the user's pan/zoom is preserved thereafter.

        Frames the board *outline* when the design carries one, so the whole
        board edge is in view rather than just the copper that happens to be
        meshed (the outline usually sits a hair outside the copper, and
        gerber imports push no FEM mesh at all). Falls back to the mesh data
        bounds otherwise.

        Gated on the canvas actually being on-screen: the first ``_render``
        runs from ``__init__`` *before* the window is shown, when the GL
        widget still has its pre-layout default size — fitting then yields a
        wildly wrong (zoomed-out) scale. While not yet visible we leave
        ``_need_initial_fit`` set so the first real GL resize (from show /
        maximise) re-drives this via ``_on_gl_view_changed`` at the correct
        size. Guarded against re-entry from the synchronous ``viewChanged``
        signal that the fit emits.
        """
        if (not self._need_initial_fit or self._gl_viewer is None
                or not self._gl_viewer.isVisible()):
            return
        failures = (self.metadata or {}).get("mesh_failures") or []
        if failures:
            self._need_initial_fit = False
            if not getattr(self, "_mesh_failures_highlighted", False):
                self._mesh_failures_highlighted = True
                _apply_mesh_failure_highlights(self)
            return
        bounds = self._board_outline_bounds() or self._data_bounds
        if bounds is None:
            return
        self._suppress_view_changed = True
        try:
            self._sync_navlib_frame_bounds()
            self._gl_viewer.fit_to_bounds(*bounds, padding=1.08)
            _, _, self._mm_per_pixel = self._gl_viewer.view_center_scale()
        finally:
            self._suppress_view_changed = False
        self._need_initial_fit = False

    def _board_outline_bounds(
        self,
    ) -> tuple[float, float, float, float] | None:
        """Axis-aligned bounding box of the board-outline polyline, or
        ``None`` when the design has no usable outline."""
        points = (self.metadata or {}).get("board_outline") or []
        if len(points) < 3:
            return None
        ring = np.asarray(points, dtype=np.float64)
        if ring.ndim != 2 or ring.shape[1] < 2 or not np.isfinite(ring).all():
            return None
        return (float(ring[:, 0].min()), float(ring[:, 0].max()),
                float(ring[:, 1].min()), float(ring[:, 1].max()))

    def _on_gl_view_changed(self) -> None:
        """GLMeshViewer fired a view-change (pan/zoom/resize) signal.

        Two cases:

        * **Deferred initial fit**: ``_render`` ran before Qt had sized
          the widget, so the initial ``_fit_board_to_canvas`` used a
          stale size. The first real resize triggers this signal — we
          re-fit now that ``self.width()/height()`` are correct.
        * **User pan / zoom / window resize**: just cache the new
          mm-per-pixel so future code (e.g. window-resize handlers) can
          preserve the user's chosen zoom CAD-style.
        """
        if self._suppress_view_changed or self._gl_viewer is None:
            return
        if self._need_initial_fit:
            # Now that the canvas is on-screen at its real size, run (or
            # re-run) the one-time board fit. Self-guards on visibility and
            # available bounds, so this is a cheap no-op until both hold —
            # and gerber imports (no mesh ``_data_bounds``) still fit via
            # their board outline.
            self._fit_board_to_canvas()
            return
        _, _, mpp = self._gl_viewer.view_center_scale()
        if mpp > 0:
            self._mm_per_pixel = mpp
        # Arrows are anchored to layer bounds (not screen pixels), so
        # pan / zoom / 3D dolly don't change their world-space positions
        # — no rebuild needed on view change.

    def _on_arrows_toggled(self, checked: bool) -> None:
        """Arrow checkbox flipped — show/hide the density control and
        push the overlay (or clear it). Cheap; doesn't trigger a full
        _render."""
        self.arrow_spacing_label.setVisible(checked)
        self.arrow_spacing_slider.setVisible(checked)
        self._refresh_arrows()

    def _on_arrow_density_changed(self, value: int) -> None:
        """Live update of the arrow-density label, with a debounced
        rebuild so dragging the slider doesn't trigger a full meshgrid
        + trifinder pass on every intermediate value."""
        self.arrow_spacing_label.setText(f"Arrow density: {value}")
        if (not self.show_arrows_box.isChecked()
                or self._gl_viewer is None):
            return
        from PySide6.QtCore import QTimer
        timer = getattr(self, "_arrow_density_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._refresh_arrows)
            self._arrow_density_timer = timer
        timer.start(80)

    def _via_marker_diameter_mm(self, entry: dict) -> float:
        """Physical diameter (mm) used to size a via / PTH marker dot.

        Drill diameter preferred — the same convention as the 3D via
        cylinders (:meth:`_push_via_cylinders`) and the Via Current
        overlay (the drill bore is the current-carrying barrel). Falls
        back to the outer pad diameter, then to
        :data:`_VIA_MARKER_FALLBACK_DIAM_MM` when neither is present."""
        drill = float(entry.get("hole_diameter_mm") or 0.0)
        if drill > 0.0:
            return drill
        outer = float(entry.get("diameter_mm") or 0.0)
        if outer > 0.0:
            return outer
        return self._VIA_MARKER_FALLBACK_DIAM_MM

    def _build_via_span_labels(self, rail_members: set[str]) -> list[dict]:
        """Per-via ``"1:8"`` / ``"1:2"`` text labels, anchored at the via's
        centre. Filters vias the same way the via markers do (span touches
        any visible layer, rail-member net), then formats the span using
        copper-layer ranks (1-based top→bottom). Text height is sized to
        sit inside the drill barrel so the label visually lives in the via.

        Returns label dicts in the format accepted by
        :meth:`gl_mesh_viewer.GLMeshViewer.set_overlay_labels`.
        """
        if self.metadata is None:
            return []
        visible_phys = self._visible_layers()
        target_layer_ids: set[int] = {
            lid for lid in
            (self._phys_name_to_layer_id.get(p) for p in visible_phys)
            if lid is not None
        }
        copper_layer_ids = set(self._visible_all_copper_layer_ids().keys())
        if not target_layer_ids and not copper_layer_ids:
            return []
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        labels: list[dict] = []
        seen: set[tuple[float, float]] = set()
        for v in self.metadata.get("vias", []):
            ls = v.get("layer_start")
            le = v.get("layer_end")
            if ls is None or le is None:
                continue
            lo, hi = (ls, le) if ls <= le else (le, ls)
            # Match the marker / cylinder rule: primary-eye visibility
            # requires the rail filter; all-copper (second eye) visibility
            # bypasses it. See :meth:`_push_via_cylinders`.
            net = v.get("net", "")
            rail_ok = (
                bool(rail_members) and net in rail_members
                and any(lo <= lid <= hi for lid in target_layer_ids)
            )
            copper_ok = any(lo <= lid <= hi for lid in copper_layer_ids)
            if not (rail_ok or copper_ok):
                continue
            x = float(v.get("x_mm", 0.0))
            y = float(v.get("y_mm", 0.0))
            key = (x, y)
            if key in seen:
                continue
            seen.add(key)
            start_phys = id_to_phys.get(ls)
            end_phys = id_to_phys.get(le)
            start_rank = self._phys_stackup_rank.get(start_phys, 0)
            end_rank = self._phys_stackup_rank.get(end_phys, 0)
            top_n = min(start_rank, end_rank) + 1
            bot_n = max(start_rank, end_rank) + 1
            text = f"{top_n}:{bot_n}"
            diam = self._via_marker_diameter_mm(v)
            # Height ≈ 55% of the drill barrel — comfortably inside the
            # marker dot / cylinder cap, with room for a 2-digit:2-digit
            # label on 10+ layer stacks.
            height_mm = max(0.05, 0.55 * diam)
            # Anchor at the via's top cap so a buried via (e.g. L2:L7)
            # still gets its label sitting on the visible cylinder, not
            # floating above the topmost copper layer.
            top_phys = start_phys if start_rank <= end_rank else end_phys
            z_top = self._layer_z_for(top_phys) if top_phys else 0.0
            labels.append({
                "x": x,
                "y": y,
                "z": z_top,
                "text": text,
                "color": "#ffffff",
                "height_mm": height_mm,
                "rotation_deg": 0.0,
                "center": True,
                "on_top": True,
            })
        return labels

    def _collect_via_positions(self, target_layer_id: int | None,
                               rail_members: set[str], *,
                               rail_scoped: bool = True,
                               ) -> tuple[list[float], list[float],
                                          list[float]]:
        """Vias whose span includes ``target_layer_id``. Returns parallel
        xs/ys lists plus each via's physical diameter (mm) so the marker
        overlay can size the orange dot to the real via footprint.

        ``rail_scoped`` selects which eye is asking:

        * ``True`` (first-column / heatmap eye) — only vias whose net is on
          a visible rail show. With no rail visible (``rail_members`` empty)
          nothing shows: the heatmap eye carries no rail to scope to, so it
          must not fall through to "show every via".
        * ``False`` (second-column / all-copper eye) — the rail filter is
          bypassed entirely; every via crossing the layer shows.
        """
        if target_layer_id is None or self.metadata is None:
            return [], [], []
        xs: list[float] = []
        ys: list[float] = []
        diams: list[float] = []
        for v in self.metadata.get("vias", []):
            ls = v.get("layer_start")
            le = v.get("layer_end")
            if ls is None or le is None:
                continue
            lo, hi = (ls, le) if ls <= le else (le, ls)
            if not (lo <= target_layer_id <= hi):
                continue
            if rail_scoped and v.get("net") not in rail_members:
                continue
            if self._focus_hides_net(v.get("net")):
                continue
            xs.append(v.get("x_mm", 0.0))
            ys.append(v.get("y_mm", 0.0))
            diams.append(self._via_marker_diameter_mm(v))
        return xs, ys, diams

    def _focus_hides_net(self, net: str | None) -> bool:
        """Net-name-only form of :meth:`_editor_focus_hides`, for the
        annotations that carry a net but no polygon to test by identity
        (via and PTH dots). A focus on synthetic copper holds no real net
        name, so it hides all of them — none of them is the copper the user
        asked to see."""
        if not self._editor_focus_active():
            return False
        return net not in self._editor_focus_nets

    def _collect_pth_positions(self, target_layer_id: int | None,
                               rail_members: set[str], *,
                               rail_scoped: bool = True,
                               ) -> tuple[list[float], list[float],
                                          list[float],
                                          list[tuple[float, float, float, bool]
                                               | None]]:
        """Plated-through-hole pads whose span includes ``target_layer_id``.
        Mirrors :meth:`_collect_via_positions` (xs/ys + physical diameter mm,
        and the same ``rail_scoped`` first-/second-eye semantics); PTHs span
        the full enabled stack so every visible copper layer hits the same
        set of pads (the marker-builder dedups across layers).

        Also returns a parallel ``slots`` list: each entry is the pad's
        ``(length_mm, width_mm, rotation_deg)`` obround drill, or ``None`` for
        a round bore — so the 2D marker can draw a slotted PTH as a capsule."""
        if target_layer_id is None or self.metadata is None:
            return [], [], [], []
        xs: list[float] = []
        ys: list[float] = []
        diams: list[float] = []
        slots: list[tuple[float, float, float, bool] | None] = []
        for p in self.metadata.get("pths", []):
            ls = p.get("layer_start")
            le = p.get("layer_end")
            if ls is None or le is None:
                continue
            lo, hi = (ls, le) if ls <= le else (le, ls)
            if not (lo <= target_layer_id <= hi):
                continue
            if rail_scoped and p.get("net") not in rail_members:
                continue
            if self._focus_hides_net(p.get("net")):
                continue
            xs.append(p.get("x_mm", 0.0))
            ys.append(p.get("y_mm", 0.0))
            diams.append(self._via_marker_diameter_mm(p))
            slots.append(_rec_slot_tuple(p))
        return xs, ys, diams, slots

    def _on_colormap_changed(self, cmap_name: str) -> None:
        """The colour-scale dropdown picked a new scheme. Recolour every
        heatmapped surface — *without* the full :meth:`_render` rebuild.

        A scheme change touches no geometry and no scalar values: the
        copper mesh recolours on the GPU straight from the 1-D LUT texture
        (the ``set_colormap`` push below), and the gradient strip is
        repainted by the ScaleController itself. The only CPU-side work
        left is re-baking the overlays that carry baked LUT colours — stub
        copper, series bars and (in 3D / Via Current) the via cylinders —
        which :meth:`_recolor_overlays` handles. Skipping ``_render`` here
        avoids rebuilding the rail mesh batch, re-uploading the vertex
        buffers and recomputing the colour-scale range on every toggle.

        In Via Current mode the copper keeps its flat-grey LUT
        (``_gl_cmap_kind == "neutral"``) — only the via overlays recolour.
        """
        if cmap_name == self._cmap_name:
            return
        self._cmap_name = cmap_name
        # Re-push the copper-mesh LUT when the data ramp is the live one.
        # _ensure_gl_cmap would no-op here (kind unchanged), so push direct.
        if self._gl_cmap_kind == "data" and self._gl_viewer is not None:
            self._gl_viewer.set_colormap(_build_cmap_lut(self._cmap_name))
        self._recolor_overlays()

    def _recolor_overlays(self) -> None:
        """Re-push only the overlays whose colours are baked CPU-side from
        the active colour scheme: stub copper, series-component bars and
        (in 3D / Via Current) the via cylinders.

        This is the colour-scheme counterpart of
        :meth:`_reshade_baked_via_overlays` (used by the scale-slider
        path); it additionally covers the stub and series-bar overlays,
        which also carry baked LUT colours. The copper mesh is *not*
        touched here — it recolours from the GPU LUT texture alone.
        """
        if self._gl_viewer is None:
            return
        phys_list, rails, mode = self._current_selection()
        if not phys_list or not rails:
            return
        is_via_current = (mode == _VIA_CURRENT_MODE)
        # 2D Via Current bakes its per-via colours into the marker
        # overlay, which only refreshes cleanly through the full render
        # path (same reason _reshade_baked_via_overlays falls back here).
        if is_via_current and not self.view_3d_box.isChecked():
            self._render()
            return
        drop_reference = self._last_drop_reference
        # Stub copper carries baked LUT colours only when "colour by V"
        # is active; otherwise it's flat grey and the scheme change can't
        # touch it — skip the (geometry-rebuilding) re-push in that case.
        if self._stubs_coloured_by_voltage(mode):
            self._push_stubs(phys_list, rails, mode=mode,
                             drop_reference=drop_reference)
        # Series-component bars always carry a baked LUT gradient.
        self._push_series_bars(phys_list, rails, mode,
                               drop_reference=drop_reference)
        # Via cylinders (3D) carry baked LUT colours only when the
        # heatmap is painted onto them — Via Current mode, or any mode
        # with the Heatmap-vias toggle on. Otherwise they're solid orange.
        heatmap_vias = (
            getattr(self, "heatmap_vias_box", None) is not None
            and self.heatmap_vias_box.isChecked()
        )
        if self.view_3d_box.isChecked() and (is_via_current or heatmap_vias):
            self._push_via_cylinders(phys_list, rails, mode=mode)

    def _on_scale_range_changed(self, vmin: float, vmax: float) -> None:
        """ScaleController emitted a new clamp — push it to the GL viewer
        as a uniform update. Instant for the copper mesh (just a uniform);
        the via overlays in Via Current mode and the cylinder heatmap in
        other modes hold per-vertex baked colours that *don't* react to
        the uniform, so when one of those is active we also re-bake the
        affected overlay against the new range."""
        if vmax <= vmin:
            vmax = vmin + 1e-12
        self._vmin, self._vmax = vmin, vmax
        if self._gl_viewer is not None:
            # GL values were uploaded in _gl_scale space — the level
            # clamp must travel through the same transform to match.
            self._gl_viewer.set_levels(float(self._gl_scale(vmin)),
                                        float(self._gl_scale(vmax)))
        self._reshade_baked_via_overlays()

    def _apply_via_no_current_opacity(self, alpha: float) -> None:
        """Live-apply the "No-current via opacity" Settings knob: store the
        alpha and re-push the 3D via cylinders so the fade updates without a
        re-solve. The fade is a 3D-cylinder-only effect, so there's nothing
        to refresh in 2D."""
        self._via_dead_section_alpha = min(1.0, max(0.0, float(alpha)))
        if getattr(self, "_gl_viewer", None) is None:
            return
        view_box = getattr(self, "view_3d_box", None)
        if view_box is None or not view_box.isChecked():
            return
        phys_list, rails, _ = self._current_selection()
        if not phys_list or not rails:
            return
        self._push_via_cylinders(
            phys_list, rails, mode=self.mode_combo.currentText())

    def _reshade_baked_via_overlays(self) -> None:
        """Re-push the via cylinders / 2D markers when their colours
        come from a baked LUT lookup (Via Current mode, or any other
        mode with the Heatmap-vias toggle on). Called after the user
        manipulates the scale slider, since the GL viewer's levels
        uniform only re-shades the copper mesh.

        2D Via Current goes through the full :meth:`_render` so the
        marker overlay refreshes via the same path the 2D↔3D toggle
        uses — calling just :meth:`_update_markers_and_legend` in
        isolation wasn't enough on some Qt builds (the paint event
        coalesced behind the GL viewer's pending state and the
        markers stayed at their previous colours until something
        heavier kicked a full repaint).
        """
        mode = self.mode_combo.currentText()
        is_via_current = (mode == _VIA_CURRENT_MODE)
        heatmap_vias = (getattr(self, "heatmap_vias_box", None) is not None
                        and self.heatmap_vias_box.isChecked())
        if not (is_via_current or heatmap_vias):
            return
        phys_list, rails, _ = self._current_selection()
        if not phys_list or not rails:
            return
        if self.view_3d_box.isChecked():
            self._push_via_cylinders(phys_list, rails, mode=mode)
        elif is_via_current:
            self._render()

    # --- Mouse hover (GLMeshViewer signal) ----------------------------------

    # --- 3D view toggle ------------------------------------------------------

    # Fallback uniform spacing (mm) when the stackup metadata doesn't
    # carry per-layer thicknesses. Used only when ``_phys_z_mm`` lacks
    # an entry for the requested layer.
    _LAYER_Z_SPACING_MM: float = 0.4
    # How far (pre-exaggeration mm) board features (NPTH, pads, silk,
    # components, designators) hover above/below the outer copper in 3D.
    # Capped small so they hug the surface — on a 2-layer board the mean
    # layer pitch equals the whole board thickness, which would otherwise
    # float them a full board-thickness off each face.
    _FEATURE_LIFT_MM: float = 0.005
    # Copper "plate" thickness in 3D mode (pre-exaggeration mm). Picked
    # at ~25% of the layer spacing so the copper looks like a visible
    # plate from oblique angles without dominating the layer gaps.
    # Each layer's flat mesh is extruded into a prism of this thickness
    # in 3D; 2D mode renders the flat mesh untouched.
    _COPPER_THICKNESS_MM: float = 0.0025

    def _layer_z_for(self, phys: str) -> float:
        """World-z (in mm, pre-exaggeration) for a given physical layer.
        Top of the stackup is z=0; lower layers go negative. Prefers the
        cumulative copper + dielectric centreline from the stackup
        metadata; falls back to rank × ``_LAYER_Z_SPACING_MM`` only when
        the layer is missing from the stackup."""
        z = self._phys_z_mm.get(phys)
        if z is not None:
            return z
        rank = self._phys_stackup_rank.get(phys, 0)
        return -rank * self._LAYER_Z_SPACING_MM

    # World-z to bump the selected layer above the topmost physical layer
    # in 2D. The 2D ortho near/far is ±1e6 mm, so this has miles of
    # headroom against clipping; one full layer pitch keeps it clearly
    # above the stack without breaking the user's depth intuition.
    _SELECTED_LAYER_2D_LIFT_MM: float = 1.0

    def _layer_render_z(self, phys: str) -> float:
        """Effective world-z used for GL submission. In 2D mode the
        selected (active) physical layer is bumped above the topmost
        layer so the GL_LEQUAL depth test makes it paint above every
        other layer regardless of its real stackup position; everywhere
        else (3D mode, or no selection) this is identical to
        :meth:`_layer_z_for`. Keeping the physical z untouched in 3D is
        deliberate — vias and the per-layer cylinder spans rely on the
        real stackup z lining up across calls."""
        if (phys == self._selected_layer
                and getattr(self, "view_3d_box", None) is not None
                and not self.view_3d_box.isChecked()):
            top_z = (self._layer_z_for(self._physicals[0])
                     if self._physicals else 0.0)
            return top_z + self._SELECTED_LAYER_2D_LIFT_MM
        return self._layer_z_for(phys)

    def _on_view_3d_toggled(self, checked: bool) -> None:
        """Switch the GL viewer between 2D and 3D modes and re-render so
        the per-vertex z values get re-built for the new mode (zeros in
        2D, layer-rank-derived in 3D).

        The ``_preserve_view_on_toggle`` flag (set by the Ctrl+Alt+2/3
        hotkeys) tells us the GL viewer's mode has already been switched
        via :meth:`gl_mesh_viewer.set_view_mode_preserving` — skip the
        standard re-fit path so the preserved camera survives.
        """
        if not getattr(self, "_preserve_view_on_toggle", False):
            self._gl_viewer.set_view_mode("3d" if checked else "2d")
        # _render rebuilds arrows internally so they get re-emitted with
        # (N, 3) z-lifted vertices in 3D or flat (N, 2) in 2D.
        self._render_with_busy_popup()

    def _on_layer_spacing_changed(self, value: int) -> None:
        """Live update of the 3D vertical-exaggeration uniform — affects
        both the mesh-layer separation and the via cylinder length.
        Cheap (one uniform), no mesh rebuild, so dragging stays smooth."""
        self._gl_viewer.set_vertical_exaggeration(float(value))
        self.layer_spacing_label.setText(f"Layer spacing: {value}×")

    # --- Keyboard hotkeys ---------------------------------------------------

    def _install_hotkeys(self) -> None:
        """Window-scoped keyboard shortcuts. ``Qt.WindowShortcut`` so they
        fire whenever the viewer window has focus but defer to the text
        boxes (Min/Max scale, etc.) when one of them has focus."""
        bindings = (
            ("2", self._hotkey_2d_mode),
            ("3", self._hotkey_3d_mode),
            ("Ctrl+Alt+2", self._hotkey_2d_mode_preserving),
            ("Ctrl+Alt+3", self._hotkey_3d_mode_preserving),
            ("0", self._hotkey_reset_3d_view),
            ("O", self._hotkey_toggle_outlines),
            ("R", self._hotkey_toggle_rail_only),
            ("T", self._hotkey_toggle_cursor_tooltip),
            ("A", self._hotkey_toggle_arrows),
            ("V", self._hotkey_toggle_heatmap_vias),
            ("M", self._hotkey_cycle_mode),
            ("Shift+M", self._hotkey_cycle_mode_reverse),
            ("H", self._hotkey_cycle_colormap),
            ("Shift+H", self._hotkey_cycle_colormap_reverse),
            ("B", self._toggle_sidebar),
            ("E", self._hotkey_toggle_editor),
            # Arm a free SOURCE / SINK placement — keyboard equivalents of
            # the viewport triangle buttons; no-ops outside editor mode.
            ("S", self._hotkey_arm_source_marker),
            ("L", self._hotkey_arm_sink_marker),
            # Focus / unfocus the selected net ("show only this net"); the
            # keyboard twin of the net table's crosshair column. No-op
            # outside editor mode.
            ("F", self._hotkey_toggle_net_focus),
            # Free-marker edit undo / redo (move + delete) — no-ops outside
            # editor mode.
            ("Ctrl+Z", self._undo_marker_action),
            ("Ctrl+Shift+Z", self._redo_marker_action),
            ("Ctrl+Y", self._redo_marker_action),
            # Delete the selected free marker (Ctrl+Z to restore).
            ("Delete", self._delete_selected_free_marker),
            ("Backspace", self._delete_selected_free_marker),
        )
        # Hold references so the shortcuts don't get garbage-collected.
        self._hotkey_shortcuts = []
        for key, slot in bindings:
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(slot)
            self._hotkey_shortcuts.append(sc)

    def _toggle_sidebar(self) -> None:
        """Show / hide the heatmap-tab side panel so the user can give the
        viewport the panel's horizontal real estate. The slim toggle /
        resize handle between the panel and the plot stays visible either
        way, and its triangle flips ▶ / ◀ to mirror the new state."""
        was_visible = self._sidebar_scroll.isVisible()
        self._sidebar_scroll.setVisible(not was_visible)
        self._sidebar_toggle_btn.setCollapsed(was_visible)

    def _on_sidebar_resize_press(self) -> None:
        """Remember content width at the start of a splitter drag."""
        self._sidebar_drag_start_w = getattr(
            self, "_sidebar_requested_w", self._SIDEBAR_DEFAULT_W,
        )

    def _on_sidebar_resized_by(self, delta_px: int) -> None:
        """Apply a drag delta from :class:`SidebarToggleButton`."""
        start = getattr(
            self, "_sidebar_drag_start_w", self._SIDEBAR_DEFAULT_W,
        )
        self._apply_sidebar_content_width(start + int(delta_px))
        # Cosmetic and solve-independent, so _display_dirty rather than
        # _mark_project_dirty: a panel drag must not light up Resolve, but it
        # does need to count as an unsaved change or the width the user just
        # set is silently lost on close.
        self._display_dirty = True

    def _apply_sidebar_content_width(
        self, content_w: int, *, remember: bool = True,
    ) -> None:
        """Apply the left side-panel content width (px), clamped to the window.

        ``_sidebar_requested_w`` is what the user asked for and is what gets
        persisted; ``_sidebar_content_w`` is that same width clamped to what
        currently fits beside the plot. Persisting the clamp instead would
        shrink a 400 px preference to the 180 px minimum the first time the
        project is opened on a narrow window — permanently, since the next
        save writes the shrunken value back.
        """
        scroll = getattr(self, "_sidebar_scroll", None)
        widget = getattr(self, "_sidebar_widget", None)
        if scroll is None or widget is None:
            return
        content_w = int(content_w)
        if remember:
            self._sidebar_requested_w = max(
                self._SIDEBAR_MIN_W, min(content_w, self._SIDEBAR_MAX_W),
            )
        max_w = getattr(self, "_SIDEBAR_MAX_W", 520)
        gl = getattr(self, "_gl_viewer", None)
        if gl is not None and gl.width() > 0:
            # Leave room for the plot + toggle handle.
            max_w = min(max_w, max(self._SIDEBAR_MIN_W, gl.width() - 80))
        w = max(self._SIDEBAR_MIN_W, min(content_w, max_w))
        if w == getattr(self, "_sidebar_content_w", None) and widget.width() == w:
            return
        self._sidebar_content_w = w
        widget.setFixedWidth(w)
        scroll.setFixedWidth(w + self._SIDEBAR_SCROLLBAR_W)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # Re-assert the requested width against the new window size, so a
        # panel that had to be clamped narrow grows back when the window is
        # widened. ``remember=False``: the window changed, not the request.
        super().resizeEvent(event)
        requested = getattr(self, "_sidebar_requested_w", None)
        if requested is not None:
            self._apply_sidebar_content_width(requested, remember=False)

    def _load_sidebar_width_from_project(self) -> None:
        """Restore left side-panel width from ``viewer_settings``."""
        proj = getattr(self, "_project", None)
        if proj is None:
            return
        saved = (getattr(proj, "viewer_settings", None) or {}).get(
            "sidebar_content_w",
        )
        if isinstance(saved, (int, float)) and saved > 0:
            self._apply_sidebar_content_width(int(saved))

    def _hotkey_2d_mode(self) -> None:
        self.view_3d_box.setChecked(False)

    def _hotkey_3d_mode(self) -> None:
        self.view_3d_box.setChecked(True)

    def _hotkey_2d_mode_preserving(self) -> None:
        """Switch to 2D while keeping the same world region framed —
        the 3D look-at point becomes the 2D centre, camera distance
        becomes mm-per-pixel via the perspective FOV."""
        if not self.view_3d_box.isChecked():
            return
        self._gl_viewer.set_view_mode_preserving("2d")
        self._preserve_view_on_toggle = True
        try:
            self.view_3d_box.setChecked(False)
        finally:
            self._preserve_view_on_toggle = False

    def _hotkey_3d_mode_preserving(self) -> None:
        """Switch to 3D while keeping the same world region framed —
        the 2D centre becomes the look-at point and the camera enters
        top-down at a distance that matches the 2D mm-per-pixel."""
        if self.view_3d_box.isChecked():
            return
        self._gl_viewer.set_view_mode_preserving("3d")
        self._preserve_view_on_toggle = True
        try:
            self.view_3d_box.setChecked(True)
        finally:
            self._preserve_view_on_toggle = False

    def _hotkey_reset_3d_view(self) -> None:
        """No-op in 2D — '0' only resets the view when the user is
        actually looking at the 3D model."""
        if self.view_3d_box.isChecked():
            self._gl_viewer.reset_3d_view()

    def _hotkey_toggle_outlines(self) -> None:
        self._outlines_btn.toggle()

    def _hotkey_toggle_rail_only(self) -> None:
        # No-op when the box is hidden — it's only hidden while toggling it
        # wouldn't change anything (no visible rail has SERIES siblings).
        if not self.rail_only_box.isVisible():
            return
        self.rail_only_box.toggle()

    def _hotkey_toggle_cursor_tooltip(self) -> None:
        self.cursor_tooltip_box.toggle()

    def _hotkey_toggle_arrows(self) -> None:
        self.show_arrows_box.toggle()

    def _hotkey_toggle_heatmap_vias(self) -> None:
        self.heatmap_vias_box.toggle()

    def _hotkey_cycle_mode(self) -> None:
        self._cycle_mode(+1)

    def _hotkey_cycle_mode_reverse(self) -> None:
        self._cycle_mode(-1)

    def _cycle_mode(self, step: int) -> None:
        count = self.mode_combo.count()
        if count == 0:
            return
        idx = (self.mode_combo.currentIndex() + step) % count
        self.mode_combo.setCurrentIndex(idx)

    def _hotkey_cycle_colormap(self) -> None:
        self._cycle_colormap(+1)

    def _hotkey_cycle_colormap_reverse(self) -> None:
        self._cycle_colormap(-1)

    def _cycle_colormap(self, step: int) -> None:
        """Step the heatmap colour-scheme dropdown forward / backward,
        wrapping at the ends. Setting the index drives the same
        currentIndexChanged path a dropdown click uses, so the gradient
        strip and viewport recolour automatically."""
        combo = self.scale_controller.cmap_combo
        count = combo.count()
        if count == 0:
            return
        idx = (combo.currentIndex() + step) % count
        combo.setCurrentIndex(idx)
