"""Voltage-difference tool, cursor tooltip, via and marker hover probes."""
from __future__ import annotations

import time
import numpy as np
import shapely.geometry as _sg
import shapely.prepared as _sp
from fypa import log_buffer
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.display import _VOLTAGE_DROP_MODE
from fypa.viewer.mesh_geometry import _FastTriSampler
from fypa.viewer.numeric import _numeric_validator
from fypa.viewer.overlays import _EDITOR_SCHDOC
from fypa.viewer.theme import _T, current_theme_mode
from fypa.viewer.widgets import (
    _esc,
    _floating_tooltip_qss,
    _make_floating_tooltip,
    _move_tooltip_to_cursor,
    _qt_widget_alive,
    CURSOR_TOOLTIP_THROTTLE_S,
    HOVER_THROTTLE_S,
)


class _ProbeMixin:
    """Voltage-difference tool, cursor tooltip, via and marker hover probes."""

    # --- Voltage-difference (Shift-drag) tool -------------------------------
    #
    # When the user holds Shift while hovering copper in either Voltage or
    # Voltage Drop mode, an anchor is captured at the cursor and a thin
    # white line is drawn from there to the live cursor position. The
    # status-bar probe label gains a "Difference = X V" suffix that
    # reports the live cursor's voltage minus the anchor voltage. The
    # over-copper / mode checks happen exactly once on shift-press — once
    # the tool is active, the line tracks the cursor regardless of where
    # it ends up.

    def eventFilter(self, obj, event) -> bool:
        """Application-wide hook for Shift press/release. Filter is
        installed on the QApplication so we see modifier-key events
        regardless of which child widget has focus — required because
        Qt only dispatches key events to the focused widget by default,
        and the GL viewer doesn't get focus until the user clicks it.

        Auto-repeat suppression keeps a held key from re-anchoring on
        every OS keyboard-repeat tick. The window-deactivate branch is
        a safety net for Alt-Tab — the user releases Shift in another
        window, our window never sees the release event, so we treat
        any deactivate while the tool is active as an implicit release.
        """
        et = event.type()
        if et == QEvent.KeyPress and event.key() == Qt.Key_Shift:
            if (not event.isAutoRepeat()
                    and self.isActiveWindow()):
                self._on_shift_pressed()
        elif (et == QEvent.KeyPress and event.key() == Qt.Key_Escape
              and not event.isAutoRepeat() and self.isActiveWindow()
              and self._editor_mode
              and self._editor_pending_marker is not None):
            # Escape disarms a pending free-marker drop (same as clicking
            # its button again). Consumed so it doesn't close the window.
            self._on_editor_add_marker(self._editor_pending_marker)
            return True
        elif (et == QEvent.KeyPress and event.key() == Qt.Key_Escape
              and not event.isAutoRepeat() and self.isActiveWindow()
              and self._copper_selection is not None
              and not self._editor_mode):
            # Escape clears the viewer-mode copper selection (mirrors the
            # "click off into empty space" close path). Consume the event
            # so it doesn't bubble up and close the window.
            self._clear_copper_selection()
            return True
        elif (et == QEvent.KeyPress
              and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)
              and not event.isAutoRepeat() and self.isActiveWindow()
              and not (event.modifiers() & (Qt.ControlModifier
                                            | Qt.AltModifier))
              and self._tab_targets_viewport()):
            # Tab / Shift+Tab step the copper selection out along its net
            # (see _cycle_tab_expand). Consumed only when there is a copper
            # selection to expand; otherwise Qt's focus chain gets the key.
            step = -1 if event.key() == Qt.Key_Backtab else 1
            if self._cycle_tab_expand(step):
                return True
        elif et == QEvent.KeyRelease and event.key() == Qt.Key_Shift:
            if (not event.isAutoRepeat()
                    and self._measure_anchor_xy is not None):
                self._on_shift_released()
        elif et == QEvent.WindowDeactivate and obj is self:
            if self._measure_anchor_xy is not None:
                self._on_shift_released()
        elif et == QEvent.Resize and obj is getattr(self, "_gl_viewer", None):
            # Keep the colour-scale overlay pinned bottom-left, the
            # editor-mode buttons pinned top-left, and the editor /
            # copper-properties panel pinned right edge as the GL viewer
            # resizes (window resize, sidebar collapse, etc.).
            self._position_scale_overlay()
            self._position_editor_overlays()
            self._position_editor_panel()
        return False

    def _position_scale_overlay(self) -> None:
        """Pin the heatmap colour-scale strip to the GL viewer's
        bottom-left corner. Safe to call before the overlay / viewer
        exist (no-op) — wired to every GL-viewer resize via
        :meth:`eventFilter`."""
        bar = getattr(self, "_scale_overlay", None)
        gl = getattr(self, "_gl_viewer", None)
        if bar is None or gl is None:
            return
        margin = 12
        y = gl.height() - bar.height() - margin
        bar.move(margin, max(margin, y))
        bar.raise_()

    def _position_editor_panel(self) -> None:
        """Pin the right-hand editor / copper-properties panel to the GL
        viewer's right edge, full height (minus margins). The panel is a
        child of the GL viewer so it floats over the viewport rather than
        consuming layout space — that keeps the visible copper from
        shifting sideways when the panel appears / disappears. Wired to
        every GL-viewer resize via :meth:`eventFilter`.

        Also pushes a matching inset to the GL viewer's top-right legend
        chip so it slides left while the panel is visible (otherwise the
        panel would sit on top of it and hide the via / PTH toggles)."""
        panel = getattr(self, "_editor_panel", None)
        gl = getattr(self, "_gl_viewer", None)
        if panel is None or gl is None:
            return
        margin = 0
        # Re-clamp the user-chosen width against the live viewport so a
        # window shrink can't leave the panel wider than the viewport (which
        # would push its draggable left edge off-screen).
        desired_w = getattr(
            self, "_editor_panel_width", self._EDITOR_PANEL_DEFAULT_W)
        max_w = max(
            self._EDITOR_PANEL_MIN_W,
            gl.width() - self._EDITOR_PANEL_VIEWPORT_RESERVE,
        )
        w = max(self._EDITOR_PANEL_MIN_W, min(desired_w, max_w))
        if w != panel.width():
            panel.setFixedWidth(w)
        h = max(0, gl.height() - 2 * margin)
        panel.setFixedHeight(max(h, 50))
        panel.move(gl.width() - w - margin, margin)
        panel.raise_()
        gl.set_legend_right_inset(float(w) if panel.isVisible() else 0.0)

    def _on_spacemouse_fit(self) -> None:
        """SpaceMouse menu / fit button — frame board or reset 3D view."""
        self._fit_board_to_view()

    def closeEvent(self, event) -> None:
        """Uninstall the application-wide Shift filter on window close
        so QApplication doesn't keep dispatching events at a dangling
        Python object."""
        if getattr(self, "_spacemouse", None) is not None:
            self._spacemouse.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(event)

    def _on_shift_pressed(self) -> None:
        mode = self.mode_combo.currentText()
        if mode not in ("Voltage", _VOLTAGE_DROP_MODE):
            return
        # 2D only. In 3D the anchor would sit on a specific layer's z
        # plane but the cursor's "current voltage" comes from a ray-pick
        # that can land on a different layer — the resulting difference
        # would mix two unrelated readings.
        if self.view_3d_box.isChecked():
            return
        if not self._layer_probes:
            return
        # Probe the live cursor position (rather than the last hover
        # event) so the anchor reflects exactly where the user pressed
        # Shift, even if the throttled hover handler hasn't fired since.
        local = self._gl_viewer.mapFromGlobal(QCursor.pos())
        if not (0 <= local.x() < self._gl_viewer.width()
                and 0 <= local.y() < self._gl_viewer.height()):
            return
        hit = self._probe_voltage_at_pixel(local.x(), local.y())
        if hit is None:
            return
        voltage, world_xy = hit
        self._measure_anchor_xy = world_xy
        self._measure_anchor_voltage = float(voltage)
        # Seed the line with a zero-length segment so it appears at the
        # anchor immediately; subsequent mouse moves grow the far endpoint.
        self._gl_viewer.set_measurement_line(
            world_xy[0], world_xy[1], world_xy[0], world_xy[1],
        )

    def _on_shift_released(self) -> None:
        if self._measure_anchor_xy is None:
            return
        self._measure_anchor_xy = None
        self._measure_anchor_voltage = None
        self._gl_viewer.clear_measurement_line()

    def _probe_voltage_at_pixel(
        self, x_px: float, y_px: float,
    ) -> tuple[float, tuple[float, float]] | None:
        """Return ``(voltage_V, (world_x, world_y))`` for the cursor at
        ``(x_px, y_px)`` in widget-logical pixels, or ``None`` if there's
        no copper / no usable voltage at that point. Uses the same FEM
        probe + stub-fallback flow as :meth:`_on_gl_mouse_hovered` so
        anything the status bar shows a voltage for is a valid anchor."""
        is_3d = self.view_3d_box.isChecked()
        if is_3d:
            hit = self._probe_at_point_3d(x_px, y_px)
            if hit is not None:
                v, lp = hit
                z = self._layer_z_for(lp.get("physical", ""))
                wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
                return float(v), (wx, wy)
            stub_hit = self._probe_at_stub_3d(x_px, y_px)
            if stub_hit is not None and stub_hit[0] is not None:
                phys = stub_hit[1].get("physical", "")
                z = self._layer_z_for(phys)
                wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
                return float(stub_hit[0]), (wx, wy)
            return None
        wx, wy = self._gl_viewer.screen_to_world(x_px, y_px)
        hit = self._probe_at_point(wx, wy)
        if hit is not None:
            return float(hit[0]), (wx, wy)
        stub_hit = self._probe_at_stub(wx, wy)
        if stub_hit is not None and stub_hit[0] is not None:
            return float(stub_hit[0]), (wx, wy)
        return None

    def _apply_measurement_difference(
        self, world_x: float, world_y: float,
        current_voltage: float | None,
    ) -> str:
        """If the Shift-drag tool is active, update the overlay line to
        span from the anchor to ``(world_x, world_y)`` and return the
        ``"   Difference = X V"`` suffix to append to the status-bar
        text. Returns an empty string when no measurement is in
        progress, or cancels the measurement if the active mode is no
        longer Voltage / Voltage Drop."""
        if self._measure_anchor_xy is None or self._measure_anchor_voltage is None:
            return ""
        if (self.mode_combo.currentText() not in ("Voltage", _VOLTAGE_DROP_MODE)
                or self.view_3d_box.isChecked()):
            self._on_shift_released()
            return ""
        ax, ay = self._measure_anchor_xy
        self._gl_viewer.set_measurement_line(ax, ay, world_x, world_y)
        if current_voltage is None or not np.isfinite(current_voltage):
            return "   Difference = (n/a)"
        diff = float(current_voltage) - self._measure_anchor_voltage
        return f"   Difference = {diff:.5g} V"

    def _on_gl_mouse_hovered(self, world_x: float, world_y: float,
                             inside: bool) -> None:
        """GLMeshViewer reported a mouse-move at world coords (mm).

        Throttled to :data:`HOVER_THROTTLE_S` so a high-DPI mouse doesn't
        flood the CPU. Skipped during drag (the GLMeshViewer is panning,
        so probe values would be meaningless mid-drag anyway).

        2D mode: the signal's (world_x, world_y) is the answer — walk the
        cached probe list top-first and report the first layer whose
        copper covers it. 3D mode: a single screen pixel maps to a whole
        camera ray, so the same pixel hits different (x, y) at each
        layer's z. We re-unproject per layer using the GL viewer's MVP
        inverse, and report the topmost layer whose copper covers the
        ray's intersection with that layer's z plane.
        """
        if QApplication.mouseButtons() != Qt.NoButton:
            return
        if not inside:
            self.probe_label_widget.setText("Hover the plot to probe values")
            self._hide_cursor_tooltip()
            return
        now = time.monotonic()
        # Use the tighter throttle while the cursor tooltip is on — it
        # visibly tracks the mouse, so any stutter is jarring. The bottom
        # probe label is more forgiving, so the default 30 Hz is fine.
        throttle = (CURSOR_TOOLTIP_THROTTLE_S
                    if self.cursor_tooltip_box.isChecked()
                    else HOVER_THROTTLE_S)
        if now - self._last_probe_at < throttle:
            return
        self._last_probe_at = now
        # Nothing to probe at all — no FEM rail and no visible physical
        # copper. Keep the initial-state hint rather than reporting
        # "(no copper)" over a blank viewport.
        has_visible_all_copper = (
            self.metadata is not None
            and bool(self.metadata.get("all_copper"))
            and bool(self._visible_all_copper_layer_ids())
        )
        if not self._layer_probes and not has_visible_all_copper:
            self.probe_label_widget.setText("Hover the plot to probe values")
            self._hide_cursor_tooltip()
            return
        hit = None
        if self.view_3d_box.isChecked():
            px, py = self._gl_viewer.last_hover_pixel()
            hit = self._probe_at_point_3d(px, py)
            if hit is not None:
                _, lp = hit
                z = self._layer_z_for(lp.get("physical", ""))
                world_x, world_y = self._gl_viewer.screen_to_world_at_z(
                    px, py, z)
        elif self._layer_probes:
            hit = self._probe_at_point(world_x, world_y)
        if hit is None:
            # Fall back to stub (no-current copper) probe, then to the
            # all-copper overlay so non-rail physical copper still
            # reports its net.
            is_3d = self.view_3d_box.isChecked()
            if is_3d:
                px_, py_ = self._gl_viewer.last_hover_pixel()
                stub_hit = self._probe_at_stub_3d(px_, py_)
            else:
                stub_hit = self._probe_at_stub(world_x, world_y)
            via_row = self._pick_hovered_via(world_x, world_y)
            via_part = (self._format_via_hover_text(via_row)
                        if via_row is not None else "")
            marker_row = self._pick_hovered_marker(world_x, world_y)
            marker_part = (self._format_marker_hover_text(marker_row)
                           if marker_row is not None else "")
            if stub_hit is not None:
                v_stub, stub_info = stub_hit
                net_part = (f"   Net = {stub_info['net']}"
                            if stub_info.get("net") else "")
                layer_part = (f"   Layer = {stub_info['physical']}"
                              if stub_info.get("physical") else "")
                v_part = (f"   Voltage ≈ {v_stub:.5g} V   (no current)"
                          if v_stub is not None else "   (no current)")
                diff_part = self._apply_measurement_difference(
                    world_x, world_y, v_stub,
                )
                self.probe_label_widget.setText(
                    f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm"
                    f"{v_part}{net_part}{layer_part}"
                    f"{via_part}{marker_part}{diff_part}"
                )
                self._update_cursor_tooltip((v_stub, stub_info),
                                            via_row=via_row,
                                            marker_row=marker_row)
                return
            if is_3d:
                px_, py_ = self._gl_viewer.last_hover_pixel()
                bare_hit = self._all_copper_at_point_3d(px_, py_)
                if bare_hit is not None:
                    z = self._layer_z_for(bare_hit.get("physical", ""))
                    world_x, world_y = self._gl_viewer.screen_to_world_at_z(
                        px_, py_, z)
            else:
                bare_hit = self._all_copper_at_point(world_x, world_y)
            if bare_hit is not None:
                bare_hit = dict(bare_hit)
                bare_hit["is_bare"] = True
                # An unnamed copper polygon the user has renamed in
                # editor mode reports the new name with a "[pending
                # resolve]" suffix until the next Resolve actually
                # rebuilds the solution against the renamed net.
                if bare_hit.get("net") == "(none)":
                    c = self._copper_name_at(
                        world_x, world_y, bare_hit.get("layer_id"))
                    if c is not None:
                        bare_hit["net"] = f"{c.name}  [pending resolve]"
                net_part = (f"   Net = {bare_hit['net']}"
                            if bare_hit.get("net") else "")
                layer_part = (f"   Layer = {bare_hit['physical']}"
                              if bare_hit.get("physical") else "")
                diff_part = self._apply_measurement_difference(
                    world_x, world_y, None,
                )
                self.probe_label_widget.setText(
                    f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm"
                    f"{net_part}{layer_part}"
                    f"{via_part}{marker_part}{diff_part}"
                )
                self._update_cursor_tooltip((None, bare_hit),
                                            via_row=via_row,
                                            marker_row=marker_row)
                return
            diff_part = self._apply_measurement_difference(
                world_x, world_y, None,
            )
            self.probe_label_widget.setText(
                f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm   "
                f"(no copper at this point){via_part}{marker_part}{diff_part}"
            )
            self._update_cursor_tooltip(None, via_row=via_row,
                                        marker_row=marker_row)
            return
        v_float, lp = hit
        net_part = f"   Net = {lp['net']}" if lp.get("net") else ""
        layer_part = (f"   Layer = {lp['physical']}"
                      if lp.get("physical") else "")
        via_row = self._pick_hovered_via(world_x, world_y)
        via_part = (self._format_via_hover_text(via_row)
                    if via_row is not None else "")
        marker_row = self._pick_hovered_marker(world_x, world_y)
        marker_part = (self._format_marker_hover_text(marker_row)
                       if marker_row is not None else "")
        diff_part = self._apply_measurement_difference(
            world_x, world_y, v_float,
        )
        self.probe_label_widget.setText(
            f"x = {world_x:>8.3f} mm   y = {world_y:>8.3f} mm   "
            f"{self._probe_label} = {v_float:.5g} {self._probe_unit}"
            f"{net_part}{layer_part}{via_part}{marker_part}{diff_part}"
        )
        self._update_cursor_tooltip((v_float, lp), via_row=via_row,
                                    marker_row=marker_row)

    def _on_cursor_tooltip_toggled(self, checked: bool) -> None:
        """Hide the tooltip when turned off; immediately show it at the
        current cursor position when turned on, so the user doesn't have
        to wiggle the mouse to "wake it up"."""
        if not checked:
            self._hide_cursor_tooltip()
            return
        # Synthesize a hover event at the cursor's current position so
        # the tooltip appears immediately. Skip silently if the cursor
        # isn't over the GL viewport.
        local = self._gl_viewer.mapFromGlobal(QCursor.pos())
        if not (0 <= local.x() < self._gl_viewer.width()
                and 0 <= local.y() < self._gl_viewer.height()):
            return
        # Seed the GL viewer's last-hover pixel so the 3D-mode per-layer
        # picker has a fresh value to unproject (it normally lags one
        # real mouse-move behind, but here we haven't had one yet).
        self._gl_viewer.set_last_hover_pixel(local.x(), local.y())
        wx, wy = self._gl_viewer.screen_to_world(local.x(), local.y())
        # Bypass the throttle so the synthetic probe runs even if a
        # real hover fired within the last few ms.
        self._last_probe_at = 0.0
        self._on_gl_mouse_hovered(wx, wy, True)

    # --- Cursor tooltip (custom floating label) -----------------------------
    #
    # Qt's QToolTip has built-in re-use / debouncing logic that makes
    # showText calls stutter when the content barely changes from one
    # frame to the next — the tooltip "sticks" instead of tracking the
    # cursor. We sidestep that entirely by drawing our own frameless
    # label that we move() ourselves every hover event.

    def _ensure_cursor_tooltip_label(self) -> QLabel:
        """Lazily build the floating QLabel used as the cursor tooltip."""
        label = getattr(self, "_cursor_tooltip_label", None)
        if label is not None and _qt_widget_alive(label):
            return label
        label = _make_floating_tooltip(
            self._gl_viewer, font_family="Consolas, monospace")
        self._cursor_tooltip_label = label
        return label

    def _hide_cursor_tooltip(self) -> None:
        label = getattr(self, "_cursor_tooltip_label", None)
        if label is not None and _qt_widget_alive(label):
            label.hide()

    def _update_cursor_tooltip(
        self, hit: tuple[float | None, dict] | None,
        via_row: dict | None = None,
        marker_row: dict | None = None,
    ) -> None:
        """Show/move/hide the at-cursor tooltip based on the checkbox.
        ``hit`` is ``(value, probe)`` from :meth:`_probe_at_point` (solved
        copper) or ``_probe_at_stub`` (no-current copper), or ``None`` if
        the cursor is over bare substrate.  For stub hits ``value`` may be
        ``None`` and the probe dict carries ``is_stub=True``.

        ``via_row`` is the Vias-tab row dict for the via under the
        cursor, or ``None``. When non-None, the tooltip appends per-via
        current / voltage lines — and stays visible even when ``hit`` is
        ``None`` (e.g. in 3D when the user is hovering a via barrel
        between visible copper layers, where there is no per-pixel
        copper probe to drive the rest of the tooltip).

        ``marker_row`` is the SOURCE/SINK marker row under the cursor,
        or ``None``. Same any-of-three rule applies — the tooltip stays
        visible while the user is parked on a marker, even when not
        over copper."""
        if not self.cursor_tooltip_box.isChecked() \
                or (hit is None and via_row is None and marker_row is None):
            self._hide_cursor_tooltip()
            return
        lines: list[str] = []
        if hit is not None:
            v_float, lp = hit
            if lp.get("is_bare"):
                # Non-rail copper from the all-copper overlay — no FEM
                # solve runs here, so just name the net / layer.
                pass
            elif lp.get("is_stub"):
                if v_float is not None:
                    lines.append(f"Voltage ≈ {v_float:.5g} V")
                lines.append("(no current)")
            else:
                lines.append(
                    f"{self._probe_label}: {v_float:.5g} {self._probe_unit}"
                )
            if lp.get("net"):
                lines.append(f"Net: {lp['net']}")
            if lp.get("physical"):
                lines.append(f"Layer: {lp['physical']}")
        if via_row is not None:
            cur = via_row.get("current")
            if cur is not None and np.isfinite(cur):
                lines.append(f"Via current: {cur:.4g} A")
        if marker_row is not None:
            lines.extend(self._format_marker_tooltip_lines(marker_row))
        if not lines:
            self._hide_cursor_tooltip()
            return
        label = self._ensure_cursor_tooltip_label()
        label.setText("\n".join(lines))
        label.adjustSize()
        _move_tooltip_to_cursor(label)
        if not label.isVisible():
            label.show()

    def _ensure_interpolator(self, lp: dict) -> _FastTriSampler | None:
        """Return the layer probe's voltage sampler, building it lazily
        on first call.

        Built lazily on first cursor hover (not at render time) so a rail
        toggle never pays for it. :class:`_FastTriSampler` builds in
        ~50–150 ms even on a 300k-triangle GND plane — its predecessor,
        ``LinearTriInterpolator``, took 3–10 s and froze the GUI for
        several seconds the first time the cursor crossed a heavy plane.
        The result is written back to the ``_layer_cache`` entry (via
        ``_cache_key``) so subsequent renders of the same (layer, mode)
        pair reuse the already-built object.
        """
        interp = lp.get("interpolator")
        if interp is not None:
            return interp
        tri = lp.get("triangulation")
        if tri is None:
            return None
        interp = _FastTriSampler(tri, lp["values"])
        lp["interpolator"] = interp
        cache_key = lp.get("_cache_key")
        if cache_key is not None:
            entry = self._layer_cache.get(cache_key)
            if entry is not None:
                entry["interpolator"] = interp
        return interp

    def _probe_at_point(self, x: float, y: float
                        ) -> tuple[float, dict] | None:
        """Walk :attr:`_layer_probes` top-first, return ``(value, probe)``
        for the topmost visible layer whose copper covers (x, y). ``None``
        if the cursor is on bare substrate (or off-mesh) on every layer.
        """
        if not self._layer_probes:
            return None
        pt = _sg.Point(x, y)
        for lp in self._layer_probes:
            prepped = lp.get("prepared_shape")
            if prepped is None:
                continue
            try:
                if not prepped.contains(pt):
                    continue
            except Exception:
                continue
            interp = self._ensure_interpolator(lp)
            if interp is None:
                continue
            sample = interp(x, y)
            try:
                if hasattr(sample, "mask") and \
                        bool(np.ma.getmaskarray(sample).item()):
                    continue
                v = float(sample)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(v):
                continue
            # The sampler holds the BASE field; Voltage Drop / source-referenced
            # Voltage store their constant shift on the probe (see _render_impl)
            # so the sampler can stay shared. Apply it here so the hover readout
            # matches the shifted heatmap.
            return v - float(lp.get("value_offset", 0.0)), lp
        return None

    def _probe_at_point_3d(self, x_px: float, y_px: float
                            ) -> tuple[float, dict] | None:
        """3D-mode probe: ray-intersect the camera ray with each visible
        layer's z plane (top-first) and return the first hit whose copper
        covers the intersection point. Compensates for perspective so the
        cursor lands on the copper the user is actually looking at,
        regardless of camera angle / vertical-exaggeration."""
        if not self._layer_probes:
            return None
        for lp in self._layer_probes:
            prepped = lp.get("prepared_shape")
            if prepped is None:
                continue
            phys = lp.get("physical", "")
            z = self._layer_z_for(phys)
            wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
            try:
                if not prepped.contains(_sg.Point(wx, wy)):
                    continue
            except Exception:
                continue
            interp = self._ensure_interpolator(lp)
            if interp is None:
                continue
            sample = interp(wx, wy)
            try:
                if hasattr(sample, "mask") and \
                        bool(np.ma.getmaskarray(sample).item()):
                    continue
                v = float(sample)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(v):
                continue
            # Apply the probe's stored Voltage-Drop / source-reference shift so
            # the hover readout matches the shifted heatmap (see _probe_at_point).
            return v - float(lp.get("value_offset", 0.0)), lp
        return None

    # --- Via hover probe ----------------------------------------------------

    def _get_or_build_via_hover_index(self):
        """Cache the per-via arrays used by hover lookup.

        Built lazily on first hover from the same row list the Vias tab
        uses, so the heavy ``_compute_via_report`` cost is shared. The
        cache is a dict so the 2D path (xy disk test) and the 3D path
        (screen-space ray-cylinder test) can share most state. Returns
        ``None`` when there are no usable via rows.

        ``z_tops`` / ``z_bots`` come from the via's full ``layer_start``
        → ``layer_end`` metadata span, NOT the sampled-layer span in the
        Vias-tab row — so when the user hovers a section of barrel that
        crosses a layer without copper on this net, the 3D ray test
        still hits.
        """
        cached = getattr(self, "_via_hover_index_cache", None)
        if cached is not None:
            return cached
        if self.metadata is None:
            self._via_hover_index_cache = None
            return None
        rows = self._get_or_compute_via_rows()
        if not rows:
            self._via_hover_index_cache = None
            return None
        # Map (x_mm, y_mm, net) → full via dict so we can read the
        # original layer_start/layer_end for the 3D z-span. Rounded to
        # 1 nm to absorb the round-trip-through-pickle float noise.
        via_lookup: dict[tuple[float, float, str], dict] = {}
        for v in self.metadata.get("vias", []):
            net = v.get("net", "")
            if not net:
                continue
            key = (round(float(v.get("x_mm", 0.0)), 6),
                   round(float(v.get("y_mm", 0.0)), 6),
                   net)
            via_lookup[key] = v
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}

        xs_list: list[float] = []
        ys_list: list[float] = []
        radii_list: list[float] = []
        z_top_list: list[float] = []
        z_bot_list: list[float] = []
        rows_keep: list[dict] = []
        for r in rows:
            x = float(r["x_mm"])
            y = float(r["y_mm"])
            net = r.get("net", "")
            key = (round(x, 6), round(y, 6), net)
            v = via_lookup.get(key)
            if v is not None:
                ls = v.get("layer_start")
                le = v.get("layer_end")
                if ls is None or le is None:
                    continue
                lid_top = min(ls, le)   # top = lowest id = nearest z=0
                lid_bot = max(ls, le)
            else:
                # Row-only fallback: use the sampled-layer span.
                layer_ids = r.get("layer_ids") or []
                if len(layer_ids) < 2:
                    continue
                lid_top, lid_bot = layer_ids[0], layer_ids[-1]
            phys_top = id_to_phys.get(lid_top)
            phys_bot = id_to_phys.get(lid_bot)
            if phys_top is None or phys_bot is None:
                continue
            radius = max(float(r.get("diameter_mm") or 0.0) * 0.5, 0.15)
            xs_list.append(x)
            ys_list.append(y)
            radii_list.append(radius)
            z_top_list.append(self._layer_z_for(phys_top))
            z_bot_list.append(self._layer_z_for(phys_bot))
            rows_keep.append(r)

        if not rows_keep:
            self._via_hover_index_cache = None
            return None
        xs = np.array(xs_list, dtype=np.float64)
        ys = np.array(ys_list, dtype=np.float64)
        radii = np.array(radii_list, dtype=np.float64)
        self._via_hover_index_cache = {
            "xs": xs,
            "ys": ys,
            "radii": radii,
            "r2": radii * radii,
            "z_tops": np.array(z_top_list, dtype=np.float64),
            "z_bots": np.array(z_bot_list, dtype=np.float64),
            "rows": rows_keep,
        }
        return self._via_hover_index_cache

    def _format_via_hover_text(self, row: dict) -> str:
        """Format the bottom-bar suffix for a hovered via row. Empty
        string if neither current nor voltage is usable."""
        cur = row.get("current")
        v_top = row.get("v_top")
        parts: list[str] = []
        if cur is not None and np.isfinite(cur):
            parts.append(f"I = {cur:.4g} A")
        if v_top is not None and np.isfinite(v_top):
            parts.append(f"V = {v_top:.5g} V")
        if not parts:
            return ""
        return "   Via: " + "   ".join(parts)

    def _pick_hovered_via(self, world_x: float, world_y: float) -> dict | None:
        """Dispatch the via-hover probe by view mode. Returns the row
        dict for the picked via (the same shape :meth:`_compute_via_report`
        produces), or ``None`` when the cursor isn't over any via."""
        if self.view_3d_box.isChecked():
            return self._pick_hovered_via_3d()
        return self._pick_hovered_via_2d(world_x, world_y)

    def _via_hover_info(self, world_x: float, world_y: float) -> str:
        """Bottom-bar suffix for the picked via, or empty string."""
        row = self._pick_hovered_via(world_x, world_y)
        if row is None:
            return ""
        return self._format_via_hover_text(row)

    def _pick_hovered_via_2d(self, world_x: float,
                              world_y: float) -> dict | None:
        """2D-mode probe: simple disk-containment test. The cursor's
        world (x, y) maps 1:1 to a board point, so any via whose pad
        circle covers it is a hit; the closest center wins."""
        idx = self._get_or_build_via_hover_index()
        if idx is None:
            return None
        xs = idx["xs"]; ys = idx["ys"]; r2 = idx["r2"]; rows = idx["rows"]
        dx = xs - world_x
        dy = ys - world_y
        d2 = dx * dx + dy * dy
        inside = d2 <= r2
        if not inside.any():
            return None
        candidates = np.where(inside)[0]
        best = int(candidates[np.argmin(d2[candidates])])
        return rows[best]

    def _pick_hovered_via_3d(self) -> dict | None:
        """3D-mode probe: ray-pick each via cylinder in screen space.

        Why this is needed: a vertical via barrel viewed from an oblique
        camera lands well off the (x, y) you get by unprojecting the
        cursor at any single z — the cursor on the side of the barrel
        sits at one z while the via's footprint is at another. The
        2D-style ``(wx - vx)² + (wy - vy)² < r²`` check therefore misses.
        Instead we project each via's top and bottom endpoints to
        screen pixels and measure the cursor's distance to the
        resulting screen-space line segment, comparing against the
        via's projected radius.

        Vectorised over all vias with one MVP build + a handful of
        batched matrix multiplies per hover — cheap enough at 30 Hz
        even on a 3 000-via board."""
        idx = self._get_or_build_via_hover_index()
        if idx is None:
            return None
        xs = idx["xs"]; ys = idx["ys"]
        radii = idx["radii"]; rows = idx["rows"]
        z_tops = idx["z_tops"]; z_bots = idx["z_bots"]
        n = xs.size
        if n == 0:
            return None

        px, py = self._gl_viewer.last_hover_pixel()
        mvp = self._gl_viewer._current_mvp()
        # MVP rows → numpy 4×4 for batch multiplication.
        rows_v = [mvp.row(i) for i in range(4)]
        M = np.array([
            [r.x(), r.y(), r.z(), r.w()] for r in rows_v
        ], dtype=np.float64)
        w_px = max(1, self._gl_viewer.width())
        h_px = max(1, self._gl_viewer.height())

        def _project(x: np.ndarray, y: np.ndarray,
                     z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            """Batch world→screen-pixel projection. Behind-camera points
            (clip-w ≤ 0) become NaN so the distance tests fail naturally."""
            ones = np.ones_like(x)
            pts = np.column_stack([x, y, z, ones])
            clip = pts @ M.T
            cw = clip[:, 3]
            bad = cw <= 1e-6
            cw_safe = np.where(bad, 1.0, cw)
            ndc_x = clip[:, 0] / cw_safe
            ndc_y = clip[:, 1] / cw_safe
            sx = (ndc_x + 1.0) * 0.5 * w_px
            sy = (1.0 - ndc_y) * 0.5 * h_px
            sx = np.where(bad, np.nan, sx)
            sy = np.where(bad, np.nan, sy)
            return sx, sy

        z_mid = 0.5 * (z_tops + z_bots)
        sx_t, sy_t = _project(xs, ys, z_tops)
        sx_b, sy_b = _project(xs, ys, z_bots)
        # Two perpendicular offsets at mid-z give us a conservative
        # estimate of the cylinder's apparent screen radius (max of
        # x-axis and y-axis projections). Exact would be a perpendicular
        # to the camera-axis projection, but for hover-tolerance the
        # max-of-two is plenty close.
        sx_c, sy_c = _project(xs, ys, z_mid)
        sx_rx, sy_rx = _project(xs + radii, ys, z_mid)
        sx_ry, sy_ry = _project(xs, ys + radii, z_mid)
        screen_r = np.maximum(
            np.hypot(sx_rx - sx_c, sy_rx - sy_c),
            np.hypot(sx_ry - sx_c, sy_ry - sy_c),
        )

        # Cursor-to-segment distance for each via cylinder.
        dx_seg = sx_b - sx_t
        dy_seg = sy_b - sy_t
        seg_len2 = dx_seg * dx_seg + dy_seg * dy_seg
        valid_seg = seg_len2 > 1e-6
        seg_len2_safe = np.where(valid_seg, seg_len2, 1.0)
        t = ((px - sx_t) * dx_seg + (py - sy_t) * dy_seg) / seg_len2_safe
        t = np.clip(t, 0.0, 1.0)
        cx = np.where(valid_seg, sx_t + t * dx_seg, sx_t)
        cy = np.where(valid_seg, sy_t + t * dy_seg, sy_t)
        d2 = (cx - px) ** 2 + (cy - py) ** 2

        inside = (d2 <= screen_r * screen_r) \
            & np.isfinite(d2) & np.isfinite(screen_r)
        if not inside.any():
            return None
        candidates = np.where(inside)[0]
        best = int(candidates[np.argmin(d2[candidates])])
        return rows[best]

    # --- SOURCE / SINK marker hover ---------------------------------------
    #
    # When the cursor is over a SOURCE or SINK marker the bottom probe label
    # gets a suffix naming the directive and the current it sources/sinks.
    # SINK current = the prescribed load (directive value). SOURCE current
    # has no prescribed value — derive it from steady-state KCL on the rail
    # group as the sum of all SINK loads on the same rail.

    def _directive_current_for_hover(self, d: dict) -> float | None:
        """Current to report for a directive marker, or None if unknown.
        SINK: the prescribed load. SOURCE: sum of SINK loads on its rail
        group (KCL — what the source must deliver under DC steady state)."""
        role = d.get("role")
        if role == "SINK":
            try:
                return float(d.get("value", 0.0))
            except (TypeError, ValueError):
                return None
        if role == "SOURCE":
            return self._source_rail_load_current(d)
        return None

    def _source_rail_load_current(self, d: dict) -> float | None:
        """Sum of SINK currents on the same rail group as ``d``'s pins."""
        if self.metadata is None:
            return None
        source_nets: set[str] = set()
        for term in (d.get("terminals") or {}).values():
            for pin in term.get("pins", []):
                net = pin.get("net")
                if net:
                    source_nets.add(net)
        return self._rail_load_for_nets(source_nets)

    def _rail_load_for_nets(self, source_nets: set[str]) -> float | None:
        """Total SINK load on the rail group(s) the given nets belong to,
        or ``None`` when the nets map to no known rail. Shared by the
        solved-marker and editor-marker SOURCE hover so both report the
        same KCL figure."""
        if not source_nets:
            return None
        net_to_rail: dict[str, str] = {}
        for rail, members in self._rail_to_members.items():
            for n in members:
                net_to_rail[n] = rail
        rails = {net_to_rail[n] for n in source_nets if n in net_to_rail}
        if not rails:
            return None
        rail_members: set[str] = set()
        for rail in rails:
            rail_members.update(self._rail_to_members.get(rail, [rail]))
        total, any_found = self._rail_sink_load(rail_members)
        return total if any_found else None

    def _rail_sink_load(self, rail_members: set[str]) -> tuple[float, bool]:
        """Sum of every SINK load coupling into ``rail_members`` — solved
        schematic directives plus pending editor directives. A schematic
        SINK is dropped when an editor directive overrides its designator,
        and an editor-originated one is dropped from the solved side
        outright, so neither is counted twice. Returns
        ``(total_amps, any_found)``."""
        total = 0.0
        any_found = False
        overridden = self._overridden_designators()
        directives = (self.metadata.get("directives", [])
                      if self.metadata else [])
        for other in directives:
            if other.get("role") != "SINK":
                continue
            if other.get("designator") in overridden:
                continue
            # An editor directive that has survived a re-solve sits in BOTH
            # lists: apply_editor_directives appended it to the solved
            # directives, and the project still holds it as a live editor
            # directive. The editor loop below owns it, so counting it here
            # too doubles the reported rail load. Only skip when that loop
            # will actually run — a solve bundle opened without a project
            # would otherwise lose its editor-placed sinks entirely.
            if (self._project is not None
                    and other.get("schdoc") == _EDITOR_SCHDOC):
                continue
            for term in (other.get("terminals") or {}).values():
                if any(p.get("net") in rail_members
                       for p in term.get("pins", [])):
                    try:
                        total += float(other.get("value", 0.0))
                    except (TypeError, ValueError):
                        pass
                    any_found = True
                    break
        if self._project is not None:
            for d in self._project.editor_directives:
                if d.role != "SINK":
                    continue
                nets = {d.p_net, d.n_net} - {None}
                if nets & rail_members:
                    if d.current is not None:
                        try:
                            total += float(d.current)
                        except (TypeError, ValueError):
                            pass
                    any_found = True
        return total, any_found

    def _overridden_designators(self) -> set[str]:
        """Designators whose schematic PDN directive an editor directive
        replaces. The schematic side is dropped from hover rows and the
        rail-load sum so the (pending) editor value stands in for it."""
        if self._project is None:
            return set()
        return {d.overrides_designator
                for d in self._project.editor_directives
                if d.overrides_designator}

    def _editor_directive_current(self, d) -> float | None:
        """Hover current for an :class:`EditorDirective` — the prescribed
        load for a SINK, or the KCL rail-load sum for a SOURCE (same basis
        as :meth:`_directive_current_for_hover` uses for solved markers)."""
        if d.role == "SINK":
            return d.current
        if d.role == "SOURCE":
            return self._rail_load_for_nets({d.p_net, d.n_net} - {None})
        return None

    def _editor_marker_hover_rows(self) -> list[dict]:
        """Hover rows for the placed editor directives so the bottom-bar
        probe reports their (pending, not-yet-resolved) source / sink
        values instead of falling through to the stale schematic marker
        underneath. Mirrors the point walk in :meth:`_editor_marker_groups`;
        rows carry ``pending=True`` so the formatter can tag them."""
        if not self._editor_mode or self._project is None:
            return []
        rows: list[dict] = []
        visible_ids = self._visible_layer_ids()
        for d in self._project.editor_directives:
            if d.role not in ("SOURCE", "SINK"):
                continue
            if not self._directive_rail_visible(d):
                continue   # marker's rail is hidden — keep hover in step
            total = self._editor_directive_current(d)
            label = d.designator or d.id or ""
            size_px = int(self._ROLE_MARKER_STYLE[d.role]["size"]) + 4
            # Resolve P-side / N-side points exactly as _editor_marker_groups,
            # including the visible-layer gate so hover rows never report a
            # marker that isn't drawn.
            if d.kind == "free" and d.anchor_xy is not None:
                if self._free_marker_layer_id(d) not in visible_ids:
                    continue
                sides = [("p", d.p_net,
                          [(float(d.anchor_xy[0]),
                            float(d.anchor_xy[1]))])]
            elif d.kind == "component":
                p_pts = self._component_pad_points(
                    d.designator, [d.p_net], visible_ids, pin_filter=d.p_pins)
                n_pts = ([] if d.single_net or not d.n_net
                         else self._component_pad_points(
                             d.designator, [d.n_net], visible_ids,
                             pin_filter=d.n_pins))
                if not p_pts and not n_pts:
                    has_any_pad = bool(
                        self._component_pad_points(
                            d.designator, [d.p_net], pin_filter=d.p_pins)
                        or (d.n_net and self._component_pad_points(
                            d.designator, [d.n_net], pin_filter=d.n_pins)))
                    if has_any_pad:
                        continue
                    ctr = self._component_center(d.designator)
                    p_pts = [ctr] if ctr is not None else []
                sides = [("p", d.p_net, p_pts), ("n", d.n_net, n_pts)]
            else:
                continue
            for term_name, net, pts in sides:
                n = len(pts)
                if not n:
                    continue
                per_pin = (total / n if total is not None
                           and np.isfinite(total) else None)
                for (px, py) in pts:
                    rows.append({
                        "x_mm": float(px),
                        "y_mm": float(py),
                        "role": d.role,
                        "label": label,
                        "terminal": term_name,
                        "net": net or "",
                        "physical": d.layer or "",
                        "current_a": per_pin,
                        "directive_current_a": total,
                        "terminal_pin_count": n,
                        "size_px": size_px,
                        "pending": True,
                    })
        return rows

    def _set_marker_hover_rows(self, rows: list[dict]) -> None:
        """Stash the SOURCE/SINK marker rows that the hover probe should
        hit-test against. Called from :meth:`_update_markers_and_legend`
        with the same pin-walk results that go into the marker batch."""
        if not rows:
            self._marker_hover_index_cache = None
            return
        xs = np.fromiter((r["x_mm"] for r in rows), dtype=np.float64,
                          count=len(rows))
        ys = np.fromiter((r["y_mm"] for r in rows), dtype=np.float64,
                          count=len(rows))
        size_px = np.fromiter((r["size_px"] for r in rows), dtype=np.float64,
                               count=len(rows))
        self._marker_hover_index_cache = {
            "xs": xs,
            "ys": ys,
            "size_px": size_px,
            "rows": rows,
        }

    def _pick_hovered_marker(self, world_x: float, world_y: float
                              ) -> dict | None:
        """Return the SOURCE/SINK marker row closest to (world_x, world_y)
        if the cursor is inside its hit radius, else ``None``.

        Hit radius scales with the marker's pixel size at the current
        zoom — a generous +2 px slack so the click target matches what
        the eye sees and isn't a needle in the centre of the glyph."""
        idx = getattr(self, "_marker_hover_index_cache", None)
        if idx is None:
            return None
        mpp = self._mm_per_pixel
        if mpp <= 0.0:
            return None
        xs = idx["xs"]; ys = idx["ys"]
        size_px = idx["size_px"]
        rows = idx["rows"]
        radii_mm = (size_px * 0.5 + 2.0) * mpp
        dx = xs - world_x
        dy = ys - world_y
        d2 = dx * dx + dy * dy
        r2 = radii_mm * radii_mm
        inside = d2 <= r2
        if not inside.any():
            return None
        candidates = np.where(inside)[0]
        best = int(candidates[np.argmin(d2[candidates])])
        return rows[best]

    def _marker_hover_info(self, world_x: float, world_y: float) -> str:
        """Bottom-bar suffix for the SOURCE/SINK marker under the cursor,
        or ``""``. Thin wrapper over :meth:`_pick_hovered_marker` + the
        text formatter, kept so callers don't have to know about both."""
        row = self._pick_hovered_marker(world_x, world_y)
        if row is None:
            return ""
        return self._format_marker_hover_text(row)

    def _format_marker_tooltip_lines(self, row: dict) -> list[str]:
        """Multi-line cursor-tooltip rendering of a SOURCE/SINK marker.
        Splits what the bottom-bar suffix packs onto one line: a header
        with the role + designator, then "I = X A" for the hovered pin,
        and a "total: X A (N pins)" line for multi-pin terminals so the
        user sees both numbers at once."""
        role = row.get("role", "")
        label = row.get("label", "") or "?"
        per_pin = row.get("current_a")
        total = row.get("directive_current_a")
        n_pins = int(row.get("terminal_pin_count") or 1)
        pending = bool(row.get("pending"))
        lines: list[str] = [f"{role} {label}"]
        if per_pin is None or not np.isfinite(per_pin):
            lines.append("I: (n/a)")
            if pending:
                lines.append("(pending — press Resolve)")
            return lines
        prefix = "I ≈" if role == "SOURCE" else "I ="
        lines.append(f"{prefix} {per_pin:.4g} A")
        if n_pins > 1 and total is not None and np.isfinite(total):
            pin_word = "pins" if n_pins != 1 else "pin"
            lines.append(f"Total: {total:.4g} A ({n_pins} {pin_word})")
        if role == "SOURCE":
            lines.append("(rail load)")
        if pending:
            lines.append("(pending — press Resolve)")
        return lines

    def _format_marker_hover_text(self, row: dict) -> str:
        """One-line suffix for the hovered SOURCE/SINK marker. Shows the
        per-pin current (directive total / pins on this terminal) AND
        the terminal total so a multi-pin sink doesn't misleadingly
        report the whole load on each marker. SOURCE values are tagged
        ``≈ … (rail load)`` to flag they're derived from KCL rather than
        prescribed by the user. Editor edits not yet resolved get a
        trailing ``[pending — Resolve]`` so the user knows the figure is
        their unsolved input, not a solved value."""
        role = row.get("role", "")
        label = row.get("label", "") or "?"
        per_pin = row.get("current_a")
        total = row.get("directive_current_a")
        n_pins = int(row.get("terminal_pin_count") or 1)
        pend = "  [pending — Resolve]" if row.get("pending") else ""
        if per_pin is None or not np.isfinite(per_pin):
            return f"   {role} {label}: I = (n/a){pend}"
        prefix = "I ≈" if role == "SOURCE" else "I ="
        suffix = " (rail load)" if role == "SOURCE" else ""
        if n_pins > 1 and total is not None and np.isfinite(total):
            total_part = f" ({total:.4g} A total){suffix}"
        else:
            total_part = suffix
        return (f"   {role} {label}: {prefix} {per_pin:.4g} A"
                f"{total_part}{pend}")

    def _stub_prepared_shape(self, stub: dict):
        """Return (and cache) a shapely PreparedGeometry for a stub polygon."""
        cached = stub.get("_prepared_shape_cache")
        if cached is not None:
            return cached
        ext = stub.get("exterior")
        if ext is None or (hasattr(ext, "size") and ext.size == 0):
            return None
        holes = stub.get("holes") or []
        try:
            poly = _sg.Polygon(ext, holes)
        except Exception:
            return None
        if poly.is_empty:
            return None
        prepped = _sp.prep(poly)
        stub["_prepared_shape_cache"] = prepped
        return prepped

    def _probe_at_stub(self, x: float, y: float
                       ) -> tuple[float | None, dict] | None:
        """Check whether (x, y) falls inside any visible stub (no-current
        copper).  Returns ``(voltage, info)`` — voltage may be None if
        un-estimable — or ``None`` if no stub covers the point.
        ``info`` has keys ``physical``, ``net``, ``is_stub=True``."""
        if self.metadata is None:
            return None
        stubs = self.metadata.get("stubs") or []
        if not stubs:
            return None
        phys_list, rails, _ = self._current_selection()
        if not rails:
            return None
        visible_layer_ids: dict[int, str] = {}
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                visible_layer_ids[lid] = phys
        if not visible_layer_ids:
            return None
        rail_members = set(self._effective_rail_members(rails))
        pt = _sg.Point(x, y)
        for stub in stubs:
            lid = stub.get("layer_id")
            phys = visible_layer_ids.get(lid)
            if phys is None:
                continue
            net = stub.get("net")
            if rail_members and net not in rail_members:
                continue
            prepped = self._stub_prepared_shape(stub)
            if prepped is None:
                continue
            try:
                if not prepped.contains(pt):
                    continue
            except Exception:
                continue
            voltage = self._sample_stub_voltage(stub, net)
            return voltage, {"physical": phys, "net": net, "is_stub": True}
        return None

    def _probe_at_stub_3d(self, x_px: float, y_px: float
                          ) -> tuple[float | None, dict] | None:
        """3D-mode stub probe: unproject to each visible stub's layer z
        and check whether the intersection point falls inside the stub."""
        if self.metadata is None:
            return None
        stubs = self.metadata.get("stubs") or []
        if not stubs:
            return None
        phys_list, rails, _ = self._current_selection()
        if not rails:
            return None
        visible_layer_ids: dict[int, str] = {}
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                visible_layer_ids[lid] = phys
        if not visible_layer_ids:
            return None
        rail_members = set(self._effective_rail_members(rails))
        for stub in stubs:
            lid = stub.get("layer_id")
            phys = visible_layer_ids.get(lid)
            if phys is None:
                continue
            net = stub.get("net")
            if rail_members and net not in rail_members:
                continue
            prepped = self._stub_prepared_shape(stub)
            if prepped is None:
                continue
            z = self._layer_z_for(phys)
            wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
            try:
                if not prepped.contains(_sg.Point(wx, wy)):
                    continue
            except Exception:
                continue
            voltage = self._sample_stub_voltage(stub, net)
            return voltage, {"physical": phys, "net": net, "is_stub": True}
        return None

    def _make_collapsible_section(
        self, title: str, body_widget: QWidget, *,
        expanded: bool = False,
    ) -> tuple[QWidget, QToolButton]:
        """Wrap ``body_widget`` in a click-to-expand QFrame with a header
        button matching the dark-theme styling of the other Settings
        groups. Returns ``(wrapper, header)`` — keep a reference to the
        header if you want to update its title later."""
        wrap = QFrame()
        wrap.setObjectName("collapsibleSection")
        t = _T()
        wrap.setStyleSheet(
            f"QFrame#collapsibleSection {{ border: 1px solid {t['border']};"
            f"                            border-radius: 4px;"
            f"                            background-color: {t['bg']};"
            f"                            margin-top: 6px; }}"
        )
        wrap_layout = QVBoxLayout(wrap)
        wrap_layout.setContentsMargins(8, 6, 8, 8)
        wrap_layout.setSpacing(6)

        header = QToolButton(wrap)
        header.setCheckable(True)
        header.setChecked(expanded)
        header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        header.setAutoRaise(True)
        header.setCursor(Qt.PointingHandCursor)
        tail = "click to collapse" if expanded else "click to expand"
        header.setText(f"{title} — {tail}")
        header.setStyleSheet(
            f"QToolButton {{ border: none; padding: 2px 4px;"
            f"              color: {t['fg_strong']}; font-weight: 600;"
            f"              text-align: left; background: transparent; }}"
            f"QToolButton:hover {{ color: {t['accent']}; }}"
        )
        wrap_layout.addWidget(header)

        body_widget.setParent(wrap)
        body_widget.setVisible(expanded)
        wrap_layout.addWidget(body_widget)

        def _on_toggled(exp: bool) -> None:
            body_widget.setVisible(exp)
            header.setArrowType(Qt.DownArrow if exp else Qt.RightArrow)
            # Replace just the "click to …" tail, keep the count prefix.
            head = header.text().rsplit(" — ", 1)[0]
            new_tail = "click to collapse" if exp else "click to expand"
            header.setText(f"{head} — {new_tail}")
        header.toggled.connect(_on_toggled)
        return wrap, header

    def _refresh_inline_theme(self) -> None:
        """Re-apply the active theme to every widget that pinned its
        colours inline at construction time.

        Re-styles the Heatmap-tab side panel in place (we can't rebuild
        it because the OpenGL canvas can't be torn down without crashing
        the process). The other tabs (Setup, Nodes, Vias, Settings,
        Help) are removed and rebuilt via their builders — that's how
        the dozens of internal labels / tables / buttons inside them
        track the theme without needing individual references.

        Transient state inside the rebuilt tabs (filter selections,
        unsaved Settings form edits, scroll positions) is lost on
        toggle. The tradeoff is the only one I see — chasing every
        nested QLabel by hand would be unmaintainable.
        """
        t = _T()

        # --- Heatmap tab: re-style side panel widgets in place ---
        def _restyle_eye_list(lw) -> None:
            lw.setStyleSheet(
                f"QListWidget {{ background-color: {t['bg']}; color: {t['fg']};"
                f"              border: 1px solid {t['border']}; padding: 2px;"
                f"              alternate-background-color: {t['bg_alt']}; }}"
                f"QListWidget::item:hover {{ background-color: {t['bg_hover']}; }}"
            )
            # Each row carries its name-label colour inline; iterate.
            for i in range(lw.count()):
                row = lw.itemWidget(lw.item(i))
                if row is None:
                    continue
                for lbl in row.findChildren(QLabel):
                    ss = (lbl.styleSheet() or "").lower()
                    if not ss:
                        continue
                    if "bold" in ss:
                        lbl.setStyleSheet(
                            f"QLabel {{ color: {t['fg']}; font-weight: bold; }}"
                        )
                    else:
                        lbl.setStyleSheet(f"QLabel {{ color: {t['fg']}; }}")
                # Eye / split / transparency / colour-swatch icons are all
                # cached by theme mode → reapply forces a cache miss and a
                # fresh draw in the new colour. Every such button subclasses
                # QToolButton and exposes ``_apply_icon``.
                for btn in row.findChildren(QToolButton):
                    apply = getattr(btn, "_apply_icon", None)
                    if callable(apply):
                        apply()

        if hasattr(self, "layer_list") and self.layer_list is not None:
            _restyle_eye_list(self.layer_list)
        if hasattr(self, "rail_list") and self.rail_list is not None:
            _restyle_eye_list(self.rail_list)
        if getattr(self, "overlay_list", None) is not None:
            _restyle_eye_list(self.overlay_list)

        # Editor overlay buttons (edit toggle, free SOURCE/SINK, Resolve and
        # their click-absorbing backdrop) pin their colours inline at build
        # time, so re-apply the active theme to them here.
        self._restyle_editor_overlay_buttons()
        if getattr(self, "_sidebar_scroll", None) is not None:
            self._apply_sidebar_scroll_theme()
        self._apply_editor_panel_theme()

        if (getattr(self, "probe_label_widget", None) is not None):
            self.probe_label_widget.setStyleSheet(
                f"QLabel {{ font-family: Consolas, monospace; padding: 6px 10px;"
                f" color: {t['fg']}; background-color: {t['bg']};"
                f" border-top: 1px solid {t['border']}; }}"
            )
        if getattr(self, "layer_spacing_label", None) is not None:
            self.layer_spacing_label.setStyleSheet(
                f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}"
            )
        if getattr(self, "arrow_spacing_label", None) is not None:
            self.arrow_spacing_label.setStyleSheet(
                f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}"
            )
        if getattr(self, "_cursor_tooltip_label", None) is not None:
            self._cursor_tooltip_label.setStyleSheet(
                _floating_tooltip_qss("Consolas, monospace"))
        if getattr(self, "scale_controller", None) is not None:
            self.scale_controller.apply_theme()
        if getattr(self, "_sidebar_toggle_btn", None) is not None:
            self._sidebar_toggle_btn.update()

        # --- Non-heatmap tabs: remove and rebuild ---
        heatmap_idx = self._heatmap_tab_index
        current_tab_text = self.tabs.tabText(self.tabs.currentIndex())
        # Drop the old Messages-tab buffer listener before its signaller
        # QObject is destroyed below — leaving the listener in place
        # would fire on each new record, hit RuntimeError from the dead
        # QObject, and only then self-remove. Schedule the signaller for
        # deletion explicitly because it's parented to the viewer (so
        # it'd otherwise outlive the rebuild until the window itself
        # closes — a small but unbounded leak on theme toggles).
        old_msg_listener = getattr(self, "_messages_listener", None)
        if old_msg_listener is not None:
            log_buffer.remove_listener(old_msg_listener)
        old_msg_signaller = getattr(self, "_messages_signaller", None)
        if old_msg_signaller is not None:
            old_msg_signaller.deleteLater()
        # Remove from highest index downwards so removeTab doesn't shift
        # the index of tabs we still need to remove. Skip the heatmap.
        for i in range(self.tabs.count() - 1, -1, -1):
            if i == heatmap_idx:
                continue
            w = self.tabs.widget(i)
            self.tabs.removeTab(i)
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        # Re-add in the original order. _nodes_tab_index / _vias_tab_index
        # need refreshing because both tabs are re-inserted after the others.
        self.tabs.addTab(self._build_setup_tab(), "Setup")
        self._topology_tab_index = self.tabs.addTab(
            self._build_topology_tab(), "Topology",
        )
        self._topology_populated = False
        self._nodes_tab_index = self.tabs.addTab(
            self._build_nodes_tab(), "Nodes",
        )
        self._update_nodes_tab_title(getattr(self, "_nodes_warn_count", 0))
        self._vias_tab_index = self.tabs.addTab(
            self._build_vias_tab(), "Vias",
        )
        self._update_vias_tab_title(getattr(self, "_vias_warn_count", 0))
        self._bridges_table_populated = False
        self._bridges_tab_index = self.tabs.addTab(
            self._build_bridges_tab(), "Bridges",
        )
        self._update_bridges_tab_title()
        self._caps_tab_index = self.tabs.addTab(
            self._build_capacitors_tab(), "Capacitors",
        )
        # Rebuilding replaced the table widget — force a fresh populate on
        # next activation (rows cache is kept; only the Qt items are gone).
        self._caps_table_populated = False
        self._caps_click_handler_wired = False
        self._update_caps_tab_title(getattr(self, "_caps_warn_count", 0))
        self._impedance_tab_index = self.tabs.addTab(
            self._build_impedance_tab(), "Impedance",
        )
        # The figure and its axes were destroyed with the old tab widget.
        self._impedance_populated = False
        # The previous Messages-tab listener is dropped on tab teardown
        # below (the QObject signaller is destroyed with its parent);
        # rebuilding here re-installs a fresh listener against the same
        # global buffer so the new tab keeps receiving live records.
        self._messages_signaller = None
        self._messages_listener = None
        self._messages_tab_index = self.tabs.addTab(
            self._build_messages_tab(), "Messages",
        )
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.tabs.addTab(self._build_help_tab(), "Help")
        # Restore tab selection. "Nodes" and "Vias" both get a warning
        # suffix appended on failures, so match by prefix.
        for i in range(self.tabs.count()):
            label = self.tabs.tabText(i)
            if label == current_tab_text or (
                current_tab_text.startswith("Vias")
                and label.startswith("Vias")
            ) or (
                current_tab_text.startswith("Nodes")
                and label.startswith("Nodes")
            ) or (
                current_tab_text.startswith("Capacitors")
                and label.startswith("Capacitors")
            ) or (
                current_tab_text.startswith("Impedance")
                and label.startswith("Impedance")
            ):
                self.tabs.setCurrentIndex(i)
                break

        status = getattr(self, "_theme_status_label", None)
        if status is not None:
            status.setText(
                f"<span style='color:{t['ok']};'>"
                f"Theme set to <b>{_esc(current_theme_mode().capitalize())}</b>."
                "</span>"
            )

    def _build_stackup_settings_box(self) -> QWidget:
        """List every enabled copper layer with an editable thickness
        field (µm). Edits apply on the next press of Re-run Solver and
        feed the per-layer sheet conductance (G = thickness × σ) as well
        as the via-barrel z-distances used to compute hop resistance.

        Override map is keyed by layer_id (int) since enabled copper
        layer ids are unique within a project. Plane layers are listed
        but greyed-out — plane geometry isn't supported in v1, so their
        thickness has no effect on the FEM today.
        """
        # ``self._stackup_thickness_edits`` maps layer_id →
        # (QLineEdit, original_thickness_mm).
        self._stackup_thickness_edits: dict[int,
                                             tuple[QLineEdit, float]] = {}

        rows: list[dict] = []
        if self.metadata:
            rows = list(self.metadata.get("stackup") or [])

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 0)
        body_layout.setSpacing(6)

        layer_count = len(rows)
        count_text = ("no copper layers" if layer_count == 0
                       else f"{layer_count} copper layer"
                            + ("s" if layer_count != 1 else ""))
        title = f"Stackup — copper thicknesses ({count_text})"

        if not rows:
            info = QLabel(
                f"<i style='color:{_T()['fg_dim']};'>No copper layers in this "
                "project's stackup.</i>"
            )
            info.setWordWrap(True)
            body_layout.addWidget(info)
            wrap, _header = self._make_collapsible_section(
                title, body, expanded=True,
            )
            return wrap

        intro = QLabel(
            "Type a new value in <b>µm</b> to override a copper layer's "
            "thickness. Empty (or unchanged) fields keep the existing "
            "value. Thickness drives sheet conductance "
            "(G = thickness × σ) and via-barrel hop length. The "
            "<i>dielectric</i> rows interleaved below show the core / "
            "prepreg thickness between adjacent copper layers (read-only)."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"QLabel {{ color: {_T()['fg_label']}; }}")
        body_layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        for row_index, row in enumerate(rows):
            lid = int(row.get("layer_id", -1))
            name = str(row.get("name", "?"))
            thk_mm = float(row.get("copper_thickness_mm", 0.0) or 0.0)
            thk_um = thk_mm * 1000.0
            thk_mil = thk_mm / 0.0254 if thk_mm else 0.0
            thk_oz = thk_mm / 0.0348 if thk_mm else 0.0
            is_plane = bool(row.get("is_plane"))

            edit = QLineEdit(self._fmt_settings_value(thk_um))
            edit.setValidator(_numeric_validator(self))
            edit.setMinimumWidth(110)
            edit.setMaximumWidth(160)
            tooltip = (
                f"Override copper thickness of layer {lid} ({name}). "
                f"Originally {thk_um:g} µm ≈ {thk_mil:.3f} mil ≈ "
                f"{thk_oz:.3f} oz. Press Re-run Solver to commit."
            )
            if is_plane:
                tooltip += ("\n\nNote: this layer is a plane; plane "
                             "geometry isn't supported in v1, so the "
                             "thickness override has no effect on the "
                             "FEM until plane support lands.")
            edit.setToolTip(tooltip)
            edit.textChanged.connect(self._on_settings_field_changed)

            self._stackup_thickness_edits[lid] = (edit, thk_mm)

            _t = _T()
            row_layout = QHBoxLayout()
            row_layout.setSpacing(6)
            row_layout.addWidget(edit)
            unit_lbl = QLabel("µm")
            unit_lbl.setStyleSheet(f"QLabel {{ color: {_t['fg_muted']}; }}")
            row_layout.addWidget(unit_lbl)
            row_layout.addSpacing(8)
            hint_bits = [f"was {thk_um:g} µm",
                         f"{thk_mil:.3f} mil",
                         f"{thk_oz:.3f} oz"]
            if is_plane:
                hint_bits.append(
                    f"<span style='color:{_t['warn']};'>PLANE (not yet "
                    "modelled — override is informational)</span>"
                )
            hint = QLabel(
                f"<span style='color:{_t['fg_hint']};'>({' · '.join(hint_bits)})</span>"
            )
            row_layout.addWidget(hint)
            row_layout.addStretch(1)
            row_widget = QWidget()
            row_widget.setLayout(row_layout)

            label_widget = QLabel(f"L{lid}  {name}")
            label_widget.setStyleSheet(
                f"QLabel {{ color: {_t['fg']}; font-weight: 600; }}"
            )
            label_widget.setToolTip(tooltip)
            form.addRow(label_widget, row_widget)

            # Dielectric below this copper layer (between this layer and
            # the next one in the stack). Read-only — purely informational
            # so the user can see how far apart the copper layers sit and
            # judge expected via-cylinder lengths in the 3D view.
            if row_index + 1 < len(rows):
                d_mm = float(row.get("dielectric_thickness_mm", 0.0) or 0.0)
                d_um = d_mm * 1000.0
                d_mil = d_mm / 0.0254 if d_mm else 0.0
                if d_mm > 0.0:
                    d_text = (f"<span style='color:{_t['dielectric']};'>"
                              f"{d_um:g} µm &nbsp;·&nbsp; "
                              f"{d_mil:.3f} mil &nbsp;·&nbsp; "
                              f"{d_mm:.4f} mm</span>")
                else:
                    d_text = (f"<span style='color:{_t['fg_hint']};'>"
                              "<i>no thickness in stackup</i></span>")
                diel_value = QLabel(
                    f"<span style='color:{_t['dielectric_dim']};'>"
                    f"<i>dielectric</i></span> &nbsp; {d_text}"
                )
                diel_value.setToolTip(
                    "Dielectric (core or prepreg) between this copper "
                    "layer and the one below. Drives via-barrel hop "
                    "length in the 3D view. Read-only — edit the .PcbDoc "
                    "stackup to change."
                )
                diel_label = QLabel(
                    f"<span style='color:{_t['separator']};'>┊</span>"
                )
                diel_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                form.addRow(diel_label, diel_value)

        form_wrap = QWidget()
        form_wrap.setLayout(form)
        body_layout.addWidget(form_wrap)

        wrap, header = self._make_collapsible_section(
            title, body, expanded=False,
        )
        self._stackup_collapse_btn = header
        return wrap

    def _update_via_check_btn_visibility(self) -> None:
        """Show the Re-run Via Check button only when the field value
        differs from the currently-applied via warning threshold."""
        btn = getattr(self, "_settings_rerun_via_check_btn", None)
        edit = getattr(self, "settings_edit_via_current_warn_a", None)
        if btn is None or edit is None:
            return
        try:
            val = self._parse_settings_value(edit.text())
        except ValueError:
            btn.setVisible(False)
            return
        btn.setVisible(abs(val - self._via_current_warn_a) > 1e-12)

    def _on_rerun_via_check(self) -> None:
        """Apply the new Via current warning level to the Vias tab in
        place — no FEM re-solve. Updates the warning count in the tab
        title, refreshes the filter checkbox label and summary line,
        and re-styles the table cells against the new threshold."""
        edit = getattr(self, "settings_edit_via_current_warn_a", None)
        if edit is None:
            return
        text = edit.text().strip()
        try:
            new_warn = float(text)
        except ValueError:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['warn_fg']};'>Via current warning "
                f"level: not a number ({text!r})</span>"
            )
            return
        if new_warn < 0:
            self._settings_status_label.setText(
                f"<span style='color:{_T()['warn_fg']};'>Via current warning "
                f"level must be ≥ 0</span>"
            )
            return
        self._via_current_warn_a = new_warn
        if hasattr(self, "vias_warn_only_box"):
            self.vias_warn_only_box.setText(
                f"Show only warnings (|I| ≥ {self._via_current_warn_a:g} A)"
            )
        if getattr(self, "_vias_table_populated", False):
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._populate_vias_table()
            finally:
                QApplication.restoreOverrideCursor()
        else:
            rows = self._get_or_compute_via_rows()
            warn_count = sum(
                1 for r in rows
                if r.get("current") is not None
                and abs(r["current"]) >= self._via_current_warn_a
            )
            self._vias_warn_count = warn_count
            self._update_vias_tab_title(warn_count)
        self._update_via_check_btn_visibility()
        self._on_settings_field_changed()
        self._settings_status_label.setText(
            f"<span style='color:{_T()['accent']};'>Via check re-run "
            f"against |I| ≥ {self._via_current_warn_a:g} A.</span>"
        )

    def _gather_stackup_overrides(self) -> dict[int, float]:
        """Collect non-no-op copper-thickness overrides from the form.

        Returns ``{layer_id: thickness_mm}`` for fields whose value
        differs from the original by more than 1 nm. Blank fields and
        unchanged fields are skipped. Raises ``ValueError`` on
        un-parseable input.
        """
        overrides: dict[int, float] = {}
        for lid, (edit, original_mm) in getattr(
                self, "_stackup_thickness_edits", {}).items():
            text = edit.text().strip()
            if not text:
                continue
            try:
                new_um = self._parse_settings_value(text)
            except ValueError:
                raise ValueError(
                    f"layer {lid} thickness: not a number ({text!r})"
                )
            if new_um < 0:
                raise ValueError(
                    f"layer {lid} thickness must be ≥ 0 µm"
                )
            new_mm = new_um / 1000.0
            if abs(new_mm - original_mm) > 1.0e-6:
                overrides[lid] = new_mm
        return overrides

    def _gather_settings_from_form(self) -> tuple[object, float, float,
                                                   dict[int, float]]:
        """Read the current text in every Settings-tab QLineEdit and return
        ``(SolveSettings, via_current_warn_a, display_percentile,
        stackup_overrides)``. Raises ``ValueError``
        (caught by the Re-run handler) on bad input."""
        from fypa.altium.loader import SolveSettings as _SolveSettings
        kwargs: dict[str, float] = {}
        for key, label, *_rest in self._SETTINGS_FIELDS:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is None:
                continue
            text = edit.text().strip()
            try:
                kwargs[key] = self._parse_settings_value(text)
            except ValueError:
                raise ValueError(f"{label!r}: not a number ({text!r})")
        chk = getattr(self, "_settings_adaptive_check", None)
        if chk is not None:
            kwargs["adaptive_mesh"] = chk.isChecked()
        aw = getattr(self, "_settings_area_weighted_check", None)
        if aw is not None:
            kwargs["area_weighted_pin_coupling"] = aw.isChecked()
        et = getattr(self, "_settings_electrothermal_check", None)
        if et is not None:
            kwargs["electrothermal"] = et.isChecked()
        mode_combo = getattr(self, "_fill_mode_combo", None)
        if mode_combo is not None:
            data = mode_combo.currentData()
            if isinstance(data, str):
                kwargs["conductive_fill_mode"] = data
        new_settings = _SolveSettings(**kwargs)

        def _read_display(key: str, label: str) -> float:
            edit = getattr(self, f"settings_edit_{key}", None)
            if edit is None:
                # Should not happen — field is always built. Be safe.
                return getattr(self, f"_{key}")
            text = edit.text().strip()
            try:
                return self._parse_settings_value(text)
            except ValueError:
                raise ValueError(f"{label!r}: not a number ({text!r})")

        warn_a = _read_display("via_current_warn_a",
                                "Via current warning level")
        pct = _read_display("display_percentile_high",
                             "Heatmap colour-scale clip percentile")
        if not (0.0 < pct <= 100.0):
            raise ValueError("Heatmap colour-scale clip percentile must "
                              "be in (0, 100]")
        stackup_overrides = self._gather_stackup_overrides()
        return new_settings, warn_a, pct, stackup_overrides
