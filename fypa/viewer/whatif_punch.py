"""What-If via punching: add vias to the selected scenario by clicking copper.

Viewer mode, 2D Board, What-If panel open, base result on screen. The
"Punch vias" button in the panel arms the tool:

* a click on copper adds one via on that copper's net, sized and spanned
  like the nearest via already on that net;
* a drag adds a grid of them over the swept box at the panel's pitch, all
  on the net under the drag's starting corner;
* a click on a via the scenario already added removes it.

A via that would carry no current (its net has copper on fewer than two of
its layers), touch another net on a signal layer, or overlap another net's
via is refused with the reason in a toast - see
:func:`fypa.whatif_vias.punch_problem` for the rules. The vias exist only in
the scenario: :mod:`fypa.whatif_vias` adds them for its solve.
"""
from __future__ import annotations

import numpy as np
import shapely.geometry as _sg
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
)

from fypa import whatif as W
from fypa import whatif_vias as WV


class _WhatIfPunchMixin:
    """Punch-vias tool for the What-If panel (see the module docstring)."""

    # Badge, marker fill and button accent: the What-If orange.
    _PUNCH_COLOR = "#ff9a3c"
    _PUNCH_DRILL_COLOR = "#1a1a1a"
    # Smallest on-screen size of a punched-via marker (px) when zoomed out.
    _PUNCH_MIN_PX = 9.0
    _PUNCH_DEFAULT_PITCH_MM = 1.0

    _PUNCH_TIP = (
        "Add vias to the selected what-if by clicking copper on the Board "
        "(2D). Click: one via. Drag: a grid of vias at the pitch below. "
        "Click a via you added to remove it. Esc stops. Each via copies the "
        "nearest via on that net.")

    # --- panel ---------------------------------------------------------------

    def _build_whatif_punch_row(self, lay) -> None:
        """Punch vias / Pitch / Clear, added to the What-If panel."""
        self._whatif_punch_armed = False
        self._whatif_punch_drawn_sig = ()
        row = QHBoxLayout()
        btn = QPushButton("Punch vias")
        btn.setCheckable(True)
        btn.setToolTip(self._PUNCH_TIP)
        btn.toggled.connect(self._set_whatif_punch_armed)
        self._whatif_punch_btn = btn
        row.addWidget(btn)
        row.addWidget(QLabel("Pitch"))
        pitch = QDoubleSpinBox()
        pitch.setDecimals(2)
        pitch.setRange(0.2, 20.0)
        pitch.setSingleStep(0.1)
        pitch.setSuffix(" mm")
        pitch.setValue(self._PUNCH_DEFAULT_PITCH_MM)
        pitch.setToolTip("Spacing of the vias a drag places.")
        self._whatif_punch_pitch = pitch
        row.addWidget(pitch)
        clear = QPushButton("Clear")
        clear.setToolTip("Remove every via this what-if added.")
        clear.clicked.connect(self._on_whatif_punch_clear)
        self._whatif_punch_clear_btn = clear
        row.addWidget(clear)
        row.addStretch(1)
        lay.addLayout(row)
        gl = getattr(self, "_gl_viewer", None)
        sig = getattr(gl, "punchBandSwept", None)
        if sig is not None:
            sig.connect(self._on_whatif_punch_band)

    def _whatif_punch_blocker(self) -> str:
        """Why the tool can't be used right now, or ``""``."""
        if getattr(self, "_editor_mode", False):
            return "Leave Edit Mode to punch vias into a what-if."
        if self._whatif_selected() is None:
            return "Select a what-if to add vias to."
        if getattr(self, "_whatif_running_id", None) is not None:
            return "Wait for the what-if solve to finish."
        if not self._whatif_base_is_solved():
            return "Solve the design first."
        if self._whatif_context() != "board":
            return "Vias are punched on the Board tab."
        return ""

    def _sync_whatif_punch_controls(self) -> None:
        """Enable / label the punch row; disarm when the tool stops being
        usable. Called from every panel refresh."""
        btn = getattr(self, "_whatif_punch_btn", None)
        if btn is None:
            return
        blocker = self._whatif_punch_blocker()
        if blocker and self._whatif_punch_armed:
            self._set_whatif_punch_armed(False)
        btn.setEnabled(not blocker)
        btn.setToolTip(self._PUNCH_TIP if not blocker
                       else f"{self._PUNCH_TIP}\n\n{blocker}")
        s = self._whatif_selected()
        n = len(s.added_vias) if s is not None else 0
        self._whatif_punch_clear_btn.setEnabled(n > 0 and not blocker)
        self._whatif_punch_clear_btn.setText(f"Clear ({n})" if n else "Clear")
        # Panel opened / closed, another what-if selected or deleted: the
        # markers on the Board follow.
        if self._whatif_punch_marker_sig() != getattr(
                self, "_whatif_punch_drawn_sig", None):
            self._refresh_whatif_punch_markers()

    # --- arming --------------------------------------------------------------

    def _set_whatif_punch_armed(self, on: bool) -> None:
        on = bool(on)
        if on:
            blocker = self._whatif_punch_blocker()
            if blocker:
                self.statusBar().showMessage(blocker, 5000)
                on = False
        if on:
            # Punching works on the base copper in 2D: put both on screen.
            self._whatif_ensure_base()
            box = getattr(self, "view_3d_box", None)
            if box is not None and box.isChecked():
                box.setChecked(False)
        self._whatif_punch_armed = on
        btn = getattr(self, "_whatif_punch_btn", None)
        if btn is not None and btn.isChecked() != on:
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)
        gl = getattr(self, "_gl_viewer", None)
        if gl is not None:
            if hasattr(gl, "set_punch_mode"):
                gl.set_punch_mode(on)
            if hasattr(gl, "set_armed_marker"):
                gl.set_armed_marker("VIA" if on else None,
                                    self._PUNCH_COLOR if on else None)
        if on:
            s = self._whatif_selected()
            self.statusBar().showMessage(
                f"Punching vias into “{s.name}”: click copper for one via, "
                "drag for a grid, click an added via to remove it. "
                "Esc stops.", 8000)
        self._refresh_whatif_punch_markers()

    def _whatif_punch_disarm(self) -> None:
        if getattr(self, "_whatif_punch_armed", False):
            self._set_whatif_punch_armed(False)

    # --- the board facts the rules need --------------------------------------

    def _whatif_punch_board(self) -> dict:
        """Layers, planes, names and existing vias of the BASE design."""
        _sol, md = self._whatif_base_pair()
        md = md or {}
        enabled = [int(x) for x in md.get("enabled_copper_layer_ids") or []]
        planes = {int(r["layer_id"]) for r in md.get("stackup") or []
                  if r.get("is_plane") and r.get("layer_id") is not None}
        names = {int(r["layer_id"]): str(r.get("name") or "")
                 for r in md.get("stackup") or []
                 if r.get("layer_id") is not None}
        return {"enabled": enabled, "planes": planes, "names": names,
                "vias": list(md.get("vias") or [])}

    def _whatif_punch_survey(self, x: float, y: float, net: str,
                             radius: float, layer_ids) -> dict:
        """``{layer_id: (own, others)}``: whether ``net``'s copper covers
        ``(x, y)`` on the layer, and which other nets the pad of
        ``radius`` would touch there."""
        trees = self._layer_strtrees()
        pt = _sg.Point(x, y)
        disc = pt.buffer(radius, 16)
        out: dict[int, tuple[bool, set]] = {}
        for lid in layer_ids:
            data = trees.get(int(lid))
            own, others = False, set()
            if data is not None:
                for j in data["tree"].query(disc):
                    i = int(j)
                    shp, cnet = data["shapes"][i], data["nets"][i]
                    if cnet == net:
                        if not own and shp.contains(pt):
                            own = True
                    elif shp.intersects(disc):
                        others.add(cnet if cnet and cnet != "(none)"
                                   else "unnamed copper")
            out[int(lid)] = (own, others)
        return out

    def _whatif_punch_net_at(self, x: float, y: float) -> str | None:
        """Net of the copper the user clicked: topmost visible, else any."""
        pick = self._all_copper_at_point(x, y) or self._copper_at_point(x, y)
        return pick.get("net") if pick else None

    def _whatif_punch_other_vias(self, board: dict, s) -> list[tuple]:
        out = []
        for r in board["vias"]:
            try:
                out.append((float(r["x_mm"]), float(r["y_mm"]),
                            str(r.get("net") or ""),
                            float(r.get("diameter_mm") or 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
        out.extend((v.x_mm, v.y_mm, v.net, v.diameter_mm)
                   for v in s.added_vias)
        return out

    def _whatif_punch_try(self, x: float, y: float, net: str, board: dict,
                          s) -> tuple[WV.AddedVia | None, str]:
        """``(via, "")`` when a via may go at ``(x, y)`` on ``net``, else
        ``(None, reason)``."""
        tmpl = WV.via_template(board["vias"], net, x, y, board["enabled"])
        via = tmpl.make(x, y, net)
        span = WV.span_layer_ids(via.layer_start, via.layer_end,
                                 board["enabled"])
        survey = self._whatif_punch_survey(x, y, net, via.diameter_mm / 2.0,
                                           span)
        why = WV.punch_problem(via, survey, board["enabled"], board["planes"],
                               board["names"],
                               self._whatif_punch_other_vias(board, s))
        return (None, why) if why else (via, "")

    # --- gestures ------------------------------------------------------------

    def _whatif_punched_index_at(self, s, x: float, y: float) -> int | None:
        """Index of the scenario's added via under ``(x, y)`` (its pad, or
        the marker's minimum size when zoomed out)."""
        floor = 0.0
        gl = getattr(self, "_gl_viewer", None)
        try:
            floor = 0.5 * self._PUNCH_MIN_PX * float(gl.view_center_scale()[2])
        except Exception:
            pass
        best, best_d2 = None, None
        for i, v in enumerate(s.added_vias):
            r = max(v.diameter_mm / 2.0, floor)
            d2 = (v.x_mm - x) ** 2 + (v.y_mm - y) ** 2
            if d2 <= r * r and (best_d2 is None or d2 < best_d2):
                best, best_d2 = i, d2
        return best

    def _whatif_punch_click(self, x: float, y: float) -> None:
        s = self._whatif_selected()
        if s is None:
            self._whatif_punch_disarm()
            return
        hit = self._whatif_punched_index_at(s, x, y)
        if hit is not None:
            v = s.added_vias.pop(hit)
            self._whatif_punch_changed(s)
            self.statusBar().showMessage(
                f"Removed the added via on {v.net}.", 3000)
            return
        net = self._whatif_punch_net_at(x, y)
        if not net:
            self.statusBar().showMessage(
                "No copper there - click on copper to add a via.", 4000)
            return
        if net == "(none)":
            self.statusBar().showMessage(
                "This copper has no net name, so a via on it joins nothing "
                "the solve knows about.", 5000)
            return
        board = self._whatif_punch_board()
        via, why = self._whatif_punch_try(x, y, net, board, s)
        if via is None:
            self.statusBar().showMessage(f"No via added: {why}.", 6000)
            return
        s.added_vias.append(via)
        self._whatif_punch_changed(s)
        tmpl = WV.via_template(board["vias"], net, x, y, board["enabled"])
        self.statusBar().showMessage(
            f"Added a {via.hole_diameter_mm:.3g} / {via.diameter_mm:.3g} mm via "
            f"on {net} ({tmpl.source}).", 4000)

    def _on_whatif_punch_band(self, x0: float, y0: float,
                              x1: float, y1: float) -> None:
        """A drag in punch mode: a grid of vias over the swept box, on the
        net under the corner the drag started from (else the first copper
        a grid point lands on)."""
        if not getattr(self, "_whatif_punch_armed", False):
            return
        s = self._whatif_selected()
        if s is None:
            return
        pitch = float(self._whatif_punch_pitch.value())
        try:
            pts = WV.grid_points(x0, y0, x1, y1, pitch)
        except ValueError as e:
            self.statusBar().showMessage(f"No vias added: {e}.", 6000)
            return
        net = self._whatif_punch_net_at(x0, y0)
        if not net or net == "(none)":
            net = next((n for n in (self._whatif_punch_net_at(px, py)
                                    for px, py in pts)
                        if n and n != "(none)"), None)
        if not net:
            self.statusBar().showMessage(
                "No vias added: no named copper under the box.", 5000)
            return
        board = self._whatif_punch_board()
        added, skipped = 0, 0
        for px, py in pts:
            via, _why = self._whatif_punch_try(px, py, net, board, s)
            if via is None:
                skipped += 1
                continue
            # Appended as it goes, so the next point checks against it.
            s.added_vias.append(via)
            added += 1
        if added:
            self._whatif_punch_changed(s)
        msg = (f"Added {added} via(s) on {net} at {pitch:g} mm pitch"
               if added else f"No vias added on {net}")
        if skipped:
            msg += (f"; {skipped} spot(s) skipped (off {net}'s copper, "
                    "touching another net, or too close to a via)")
        self.statusBar().showMessage(msg + ".", 7000)

    def _on_whatif_punch_clear(self) -> None:
        s = self._whatif_selected()
        if s is None or not s.added_vias:
            return
        n = len(s.added_vias)
        s.added_vias.clear()
        self._whatif_punch_changed(s)
        self.statusBar().showMessage(f"Removed {n} added via(s).", 4000)

    def _whatif_punch_changed(self, s) -> None:
        """The scenario's vias changed: its result no longer belongs to it."""
        if s.status != W.STATUS_NOT_RUN:
            s.status = W.STATUS_NOT_RUN
            s.summary = {}
            s.base_fingerprint = None
            s.error = ""
        loaded = getattr(self, "_whatif_loaded", None)
        if loaded is not None and loaded[0] == s.id:
            self._whatif_loaded = None
        self._whatif_rows_cache = {}
        self._whatif_mark_unsaved()
        self._refresh_whatif_panel()
        self._refresh_whatif_punch_markers()

    # --- drawing -------------------------------------------------------------

    def _whatif_punch_shown(self):
        """The scenario whose added vias the Board shows, or ``None``:
        the selected one, while the What-If panel is open on the Board."""
        if not getattr(self, "_whatif_open", {}).get("board", False):
            return None
        if getattr(self, "_editor_mode", False):
            return None
        s = self._whatif_selected() if hasattr(self, "_whatifs") else None
        return s if s is not None and s.added_vias else None

    def _whatif_punch_marker_sig(self) -> tuple:
        s = self._whatif_punch_shown()
        if s is None:
            return ()
        return (s.id, tuple((v.x_mm, v.y_mm, v.diameter_mm)
                            for v in s.added_vias))

    def _whatif_punch_marker_groups(self) -> list:
        """Pad + drill markers for the selected scenario's added vias, shown
        while the What-If panel is open on the Board."""
        self._whatif_punch_drawn_sig = self._whatif_punch_marker_sig()
        s = self._whatif_punch_shown()
        if s is None:
            return []
        from fypa.gl_mesh_viewer import MarkerGroup
        xs = np.array([v.x_mm for v in s.added_vias], dtype=np.float64)
        ys = np.array([v.y_mm for v in s.added_vias], dtype=np.float64)
        zs = np.zeros_like(xs)
        pads = np.array([v.diameter_mm for v in s.added_vias])
        holes = np.array([v.hole_diameter_mm for v in s.added_vias])
        return [
            MarkerGroup(xs=xs, ys=ys, zs=zs, color=self._PUNCH_COLOR,
                        symbol="o", size=10, edge_color="#000000",
                        edge_width=1.0, world_diameters_mm=pads,
                        min_pixel_diameter=self._PUNCH_MIN_PX,
                        occludable=False),
            MarkerGroup(xs=xs, ys=ys, zs=zs, color=self._PUNCH_DRILL_COLOR,
                        symbol="o", size=4, edge_color=self._PUNCH_DRILL_COLOR,
                        edge_width=0.0, world_diameters_mm=holes,
                        min_pixel_diameter=self._PUNCH_MIN_PX * 0.4,
                        occludable=False),
        ]

    def _refresh_whatif_punch_markers(self) -> None:
        """Re-push the markers without rebuilding the heatmap. Before the
        first render there is nothing to add to - that render draws them."""
        gl = getattr(self, "_gl_viewer", None)
        cached = getattr(self, "_non_editor_marker_groups", None)
        if gl is None or not hasattr(gl, "set_markers") or cached is None:
            return
        gl.set_markers(list(cached) + self._editor_marker_groups()
                       + self._whatif_punch_marker_groups())
