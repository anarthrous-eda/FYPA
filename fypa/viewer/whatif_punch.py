"""What-If via punching: add vias to the selected scenario by clicking copper.

Viewer mode, 2D Board, What-If panel open, base result on screen. The
"Punch vias" button in the panel arms the tool:

* a click on copper adds one via on that copper's net, sized and spanned
  like the nearest via already on that net;
* a drag adds a grid of them over the swept box at the panel's pitch, all
  on the net under the drag's starting corner;
* a click on a via the scenario already added removes it.

A via may land in another net's pour or plane: the solve repours it with a
clearance hole (the panel's Clearance, per what-if, starting at the board's
PlaneClearance rule). A via that would carry no current (its net has copper
on fewer than two of its layers), touch another net's tracks, pads or fixed
regions, or overlap another via is refused with the reason in a toast - see
:func:`fypa.whatif_vias.punch_problem` for the rules. The vias exist only in
the scenario: :mod:`fypa.whatif_vias` adds them for its solve.
"""
from __future__ import annotations

import dataclasses

import numpy as np
import shapely.geometry as _sg
from PySide6.QtWidgets import (
    QComboBox,
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

        # Via type: the board's own via types (drill / pad), or "copy the
        # nearest via on the net". A type that doesn't run through every
        # layer gets a Span sub-choice: the spans the board uses it with.
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Via"))
        tcombo = QComboBox()
        tcombo.setToolTip(
            "Which of the board's via types to punch. Nearest on net copies "
            "the via nearest the click on the same net.")
        tcombo.currentIndexChanged.connect(
            lambda _i: self._on_whatif_punch_type_changed())
        self._whatif_punch_type_combo = tcombo
        trow.addWidget(tcombo, 1)
        self._whatif_punch_span_label = QLabel("Span")
        trow.addWidget(self._whatif_punch_span_label)
        scombo = QComboBox()
        scombo.setToolTip(
            "Which layers the via joins. Only the spans this via type is "
            "used with on the board are offered.")
        scombo.currentIndexChanged.connect(
            lambda _i: self._update_whatif_punch_type_info())
        self._whatif_punch_span_combo = scombo
        trow.addWidget(scombo, 1)
        lay.addLayout(trow)
        self._whatif_punch_type_info = QLabel("")
        self._whatif_punch_type_info.setWordWrap(True)
        lay.addWidget(self._whatif_punch_type_info)
        self._whatif_punch_types: list[WV.ViaType] = []
        self._whatif_punch_types_md = False      # never listed yet
        self._whatif_punch_refresh_types()

        # Repour clearance: the hole each added via cuts in other nets'
        # pours and planes. Per what-if; defaults to the board's rule.
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Clearance"))
        clr = QDoubleSpinBox()
        clr.setDecimals(3)
        clr.setRange(0.0, 5.0)
        clr.setSingleStep(0.05)
        clr.setSuffix(" mm")
        clr.setValue(WV.DEFAULT_CLEARANCE_MM)
        clr.setToolTip(
            "Gap between an added via's pad and other nets' copper. Pours "
            "and planes the via passes through are repoured with a hole "
            "this much larger than the pad. Starts at the board's "
            "PlaneClearance rule; changing it clears the what-if's result. "
            "On an internal plane the hole is never smaller than that rule.")
        clr.valueChanged.connect(self._on_whatif_punch_clearance_changed)
        self._whatif_punch_clearance_spin = clr
        row2.addWidget(clr)
        board_btn = QPushButton("Board rule")
        board_btn.setToolTip("Use the board's PlaneClearance rule.")
        board_btn.clicked.connect(self._on_whatif_punch_clearance_reset)
        self._whatif_punch_clearance_reset_btn = board_btn
        row2.addWidget(board_btn)
        row2.addStretch(1)
        lay.addLayout(row2)
        gl = getattr(self, "_gl_viewer", None)
        sig = getattr(gl, "punchBandSwept", None)
        if sig is not None:
            sig.connect(self._on_whatif_punch_band)

    # --- via type choice -----------------------------------------------------

    def _whatif_punch_refresh_types(self) -> None:
        """(Re)list the board's via types when the base result changed,
        keeping the chosen type / span where they still exist."""
        _sol, md = self._whatif_base_pair()
        if md is self._whatif_punch_types_md:
            return
        self._whatif_punch_types_md = md
        board = self._whatif_punch_board()
        keep = self._whatif_punch_choice()
        self._whatif_punch_types = WV.board_via_types(
            board["vias"], board["enabled"])
        combo = self._whatif_punch_type_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Nearest on net (auto)", None)
        sel = 0
        for i, t in enumerate(self._whatif_punch_types):
            combo.addItem(WV.via_type_label(t), i)
            if keep is not None and t.key == keep[0].key:
                sel = i + 1
        combo.setCurrentIndex(sel)
        combo.blockSignals(False)
        self._on_whatif_punch_type_changed(
            keep_span=None if keep is None else
            (keep[1].layer_start, keep[1].layer_end))

    def _whatif_punch_type(self) -> WV.ViaType | None:
        i = self._whatif_punch_type_combo.currentData()
        if i is None or not 0 <= i < len(self._whatif_punch_types):
            return None
        return self._whatif_punch_types[i]

    def _whatif_punch_choice(self):
        """``(ViaType, ViaSpan)`` chosen in the panel, or ``None`` for
        "Nearest on net"."""
        if not getattr(self, "_whatif_punch_types", None):
            return None
        t = self._whatif_punch_type()
        if t is None:
            return None
        j = self._whatif_punch_span_combo.currentData()
        if j is None or not 0 <= j < len(t.spans):
            j = 0
        return t, t.spans[j]

    def _on_whatif_punch_type_changed(self, keep_span=None) -> None:
        """Fill the Span sub-choice for the chosen type - shown only when
        the type doesn't run through every layer."""
        t = self._whatif_punch_type()
        names = self._whatif_punch_board()["names"]
        combo = self._whatif_punch_span_combo
        combo.blockSignals(True)
        combo.clear()
        sel = 0
        for j, sp in enumerate(t.spans if t is not None else ()):
            combo.addItem(
                f"{WV.span_label(sp.layer_start, sp.layer_end, names)} "
                f"({sp.kind}, {sp.count})", j)
            if keep_span == (sp.layer_start, sp.layer_end):
                sel = j
        combo.setCurrentIndex(sel)
        combo.blockSignals(False)
        show = t is not None and t.needs_span_choice
        combo.setVisible(show)
        self._whatif_punch_span_label.setVisible(show)
        self._update_whatif_punch_type_info()

    def _update_whatif_punch_type_info(self) -> None:
        """One line saying exactly what the next punched via will be."""
        choice = self._whatif_punch_choice()
        if choice is None:
            text = ("Each via copies the drill, pad and layers of the "
                    "nearest via on the net you click.")
        else:
            t, sp = choice
            board = self._whatif_punch_board()
            sample = t.template(sp).make(0.0, 0.0, "-")
            text = ("Drill / pad " + WV.via_props_text(
                sample, board["enabled"], board["names"]))
        self._whatif_punch_type_info.setText(text)

    def _whatif_punch_template(self, board: dict, net: str, x: float,
                               y: float) -> WV.ViaTemplate:
        choice = self._whatif_punch_choice()
        if choice is not None:
            return choice[0].template(choice[1])
        return WV.via_template(board["vias"], net, x, y, board["enabled"])

    def _whatif_punch_blocker(self) -> str:
        """Why the tool can't be used right now, or ``""``."""
        if getattr(self, "_editor_mode", False):
            return "Leave Edit Mode to punch vias into a what-if."
        # No what-if selected is fine: arming makes one ("Vias 1").
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
        self._whatif_punch_refresh_types()
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
        spin = self._whatif_punch_clearance_spin
        spin.blockSignals(True)
        spin.setValue(self._whatif_punch_clearance(s))
        spin.blockSignals(False)
        editable = s is not None and getattr(
            self, "_whatif_running_id", None) is None
        spin.setEnabled(editable)
        self._whatif_punch_clearance_reset_btn.setEnabled(
            editable and s.via_clearance_mm is not None)
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
        if on and self._whatif_selected() is None:
            # Nothing to punch into yet: a vias-only what-if.
            s = W.WhatIfScenario(name=W.unique_name(
                [x.name for x in self._whatifs], stem="Vias"))
            self._whatifs.append(s)
            self._whatif_selected_id = s.id
            self._whatif_mark_unsaved()
            self._refresh_whatif_panel()
        if on:
            # Punching works on the base copper in 2D: put both on screen.
            self._whatif_ensure_base()
            box = getattr(self, "view_3d_box", None)
            if box is not None and box.isChecked():
                box.setChecked(False)
        if not on:
            self._whatif_punch_cancel_drag()
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
                "vias": list(md.get("vias") or []),
                "pths": list(md.get("pths") or [])}

    def _whatif_punch_survey(self, x: float, y: float, net: str,
                             radius: float, layer_ids) -> dict:
        """``{layer_id: (own, fixed, pour)}``: whether ``net``'s copper
        covers ``(x, y)`` on the layer, the other nets whose fixed copper
        (tracks, pads, fixed regions) the pad of ``radius`` would touch
        there, and the other nets whose pour it lands in (repoured with a
        clearance hole by the solve)."""
        trees = self._layer_strtrees()
        pt = _sg.Point(x, y)
        disc = pt.buffer(radius, 16)
        out: dict[int, tuple[bool, set, set]] = {}
        for lid in layer_ids:
            data = trees.get(int(lid))
            own, fixed, pour = False, set(), set()
            if data is not None:
                touched: set = set()
                for j in data["tree"].query(disc):
                    i = int(j)
                    shp, cnet = data["shapes"][i], data["nets"][i]
                    if cnet == net:
                        if not own and shp.contains(pt):
                            own = True
                    elif shp.intersects(disc):
                        touched.add(cnet or "(none)")
                for cnet in touched:
                    label = (cnet if cnet != "(none)" else "unnamed copper")
                    if self._whatif_punch_fixed_touch(int(lid), cnet, disc):
                        fixed.add(label)
                    else:
                        pour.add(label)
            out[int(lid)] = (own, fixed, pour)
        return out

    def _whatif_punch_fixed_touch(self, layer_id: int, net: str,
                                  disc) -> bool:
        """True when ``disc`` touches ``net``'s FIXED copper on the layer:
        a track, arc, fill, a region that isn't part of a pour, or a pad.
        Pour regions and internal-plane sheets aren't fixed - a repour
        clears them. On a Gerber import, where nothing records which
        regions are pours, every region counts as pour."""
        gerber = (self.metadata or {}).get("source_kind") == "gerber"
        for prim in self._primitives_index().get((layer_id, net), ()):
            kind = prim.get("kind")
            if kind == "plane" or prim.get("is_keepout"):
                continue
            if kind in ("region", "shape_based_region") and (
                    gerber or self._is_pour_fragment(prim)):
                continue
            shp = self._primitive_prepared_shape(prim)
            try:
                if shp is not None and shp.intersects(disc):
                    return True
            except Exception:
                continue
        for lids, poly in self._whatif_punch_pads().get(net, ()):
            if layer_id in lids and poly.intersects(disc):
                return True
        return False

    def _whatif_punch_pads(self) -> dict:
        """``{net: [(layer_ids, Polygon)]}`` of the SMD / pad outlines in
        the metadata, built once per metadata."""
        md = self.metadata or {}
        cached = getattr(self, "_whatif_punch_pads_cache", None)
        if cached is not None and cached[0] is md:
            return cached[1]
        out: dict[str, list] = {}
        for rec in md.get("pads") or []:
            outline = rec.get("outline") or []
            if len(outline) < 3:
                continue
            try:
                poly = _sg.Polygon(outline)
            except Exception:
                continue
            if poly.is_empty:
                continue
            out.setdefault(str(rec.get("net") or "(none)"), []).append(
                ({int(i) for i in rec.get("layer_ids") or []}, poly))
        self._whatif_punch_pads_cache = (md, out)
        return out

    def _whatif_punch_clearance(self, s=None) -> float:
        """Repour clearance (mm) for ``s`` (default: the selected what-if):
        its own value, else the board's PlaneClearance rule."""
        s = s if s is not None else self._whatif_selected()
        if s is not None and s.via_clearance_mm is not None:
            return float(s.via_clearance_mm)
        _sol, md = self._whatif_base_pair()
        return WV.board_clearance_mm(md or {})

    def _on_whatif_punch_clearance_changed(self, value: float) -> None:
        s = self._whatif_selected()
        if s is None:
            return
        _sol, md = self._whatif_base_pair()
        board = WV.board_clearance_mm(md or {})
        new = None if abs(float(value) - board) < 5e-4 else float(value)
        if new == s.via_clearance_mm:
            return
        s.via_clearance_mm = new
        if s.added_vias:
            self._whatif_punch_changed(s)    # its result is now stale
        else:
            self._whatif_mark_unsaved()
            self._refresh_whatif_panel()

    def _on_whatif_punch_clearance_reset(self) -> None:
        _sol, md = self._whatif_base_pair()
        self._whatif_punch_clearance_spin.setValue(
            WV.board_clearance_mm(md or {}))

    def _whatif_punch_net_at(self, x: float, y: float) -> str | None:
        """Net of the copper the user clicked: topmost visible, else any."""
        pick = self._all_copper_at_point(x, y) or self._copper_at_point(x, y)
        return pick.get("net") if pick else None

    def _whatif_punch_other_vias(self, board: dict, s,
                                 skip: int | None = None) -> list[tuple]:
        """Vias and through-hole pads already on the board, plus the
        scenario's added vias (all but index ``skip``, the one being
        moved), as ``(x, y, net, diameter_mm)``."""
        out = []
        for r in list(board["vias"]) + list(board.get("pths") or []):
            try:
                out.append((float(r["x_mm"]), float(r["y_mm"]),
                            str(r.get("net") or ""),
                            float(r.get("diameter_mm") or 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
        out.extend((v.x_mm, v.y_mm, v.net, v.diameter_mm)
                   for i, v in enumerate(s.added_vias) if i != skip)
        return out

    def _whatif_punch_move_problem(self, s, idx: int, via: WV.AddedVia,
                                   board: dict) -> str:
        """Why the scenario's via ``idx`` can't sit where ``via`` puts it
        (same net, size and span), or ``""``. Same rules as placing one."""
        span = WV.span_layer_ids(via.layer_start, via.layer_end,
                                 board["enabled"])
        survey = self._whatif_punch_survey(via.x_mm, via.y_mm, via.net,
                                           via.diameter_mm / 2.0, span)
        return WV.punch_problem(via, survey, board["enabled"],
                                board["planes"], board["names"],
                                self._whatif_punch_other_vias(board, s, idx))

    # --- moving an added via (drag) ------------------------------------------
    #
    # While punching, a press on an added via drags it (the GL viewer's marker
    # drag, as for an editor SOURCE / SINK). It keeps its net, size and span;
    # it follows the cursor while the spot passes the placement rules and
    # holds at the last spot that did. A press released without moving is a
    # click, which removes the via. A move clears the what-if's result.

    # Cursor travel (px) before a press on a via counts as a drag.
    _PUNCH_DRAG_PX = 3.0

    def _whatif_punch_drag_hit_test(self, x: float, y: float) -> bool:
        s = self._whatif_selected()
        return (s is not None
                and self._whatif_punched_index_at(s, x, y) is not None)

    def _whatif_punch_drag_started(self, x: float, y: float) -> None:
        s = self._whatif_selected()
        idx = self._whatif_punched_index_at(s, x, y) if s else None
        if idx is None:
            self._whatif_punch_drag = None
            return
        v = s.added_vias[idx]
        self._whatif_punch_drag = {
            "sid": s.id, "idx": idx, "start": v, "last_valid": v,
            "press": (float(x), float(y)), "moved": False, "why": "",
            "board": self._whatif_punch_board(),
        }

    def _whatif_punch_drag_moved(self, x: float, y: float) -> None:
        d = getattr(self, "_whatif_punch_drag", None)
        s = self._whatif_by_id(d["sid"]) if d else None
        if s is None or d["idx"] >= len(s.added_vias):
            return
        dx, dy = float(x) - d["press"][0], float(y) - d["press"][1]
        if not d["moved"]:
            mpp = 0.0
            try:
                mpp = float(self._gl_viewer.view_center_scale()[2])
            except Exception:
                pass
            if (dx * dx + dy * dy) ** 0.5 < self._PUNCH_DRAG_PX * mpp:
                return
            d["moved"] = True
        # The grab offset is kept, so the via doesn't jump to the cursor.
        start = d["start"]
        cand = dataclasses.replace(start, x_mm=start.x_mm + dx,
                                   y_mm=start.y_mm + dy)
        why = self._whatif_punch_move_problem(s, d["idx"], cand, d["board"])
        if why:
            d["why"] = why
        else:
            d["last_valid"], d["why"] = cand, ""
        s.added_vias[d["idx"]] = d["last_valid"]
        self._refresh_whatif_punch_markers()

    def _whatif_punch_drag_released(self, x: float, y: float) -> None:
        d = getattr(self, "_whatif_punch_drag", None)
        self._whatif_punch_drag = None
        s = self._whatif_by_id(d["sid"]) if d else None
        if s is None or d["idx"] >= len(s.added_vias):
            return
        if not d["moved"]:
            # A click on the via: remove it.
            v = s.added_vias.pop(d["idx"])
            self._whatif_punch_changed(s)
            self.statusBar().showMessage(
                f"Removed the added via on {v.net}.", 3000)
            return
        start, final = d["start"], d["last_valid"]
        s.added_vias[d["idx"]] = final
        if final == start:
            self._refresh_whatif_punch_markers()
            self.statusBar().showMessage(
                f"Via not moved: {d['why'] or 'no valid spot'}.", 6000)
            return
        self._whatif_punch_changed(s)
        msg = (f"Moved the via on {final.net} to ({final.x_mm:.3f}, "
               f"{final.y_mm:.3f}) mm")
        if d["why"]:
            msg += f" - held at the last spot it fits ({d['why']})"
        self.statusBar().showMessage(msg + ".", 6000)

    def _whatif_punch_cancel_drag(self) -> None:
        """Put a via being dragged back where it started."""
        d = getattr(self, "_whatif_punch_drag", None)
        self._whatif_punch_drag = None
        s = self._whatif_by_id(d["sid"]) if d else None
        if s is not None and d["idx"] < len(s.added_vias):
            s.added_vias[d["idx"]] = d["start"]

    # --- hover in the Difference view ----------------------------------------

    def _whatif_added_via_hover(self, x: float, y: float):
        """``(distance², text)`` for an added via under ``(x, y)``, else
        ``None`` - the base result on screen has no such via.

        Base view (What-If panel open): the via's net, size and span.
        Difference view: its current and voltage from the what-if's own
        result (only its added vias are sampled), then its size and span."""
        box = getattr(self, "view_3d_box", None)
        if box is not None and box.isChecked():
            return None
        view = getattr(self, "_whatif_view", "base")
        if view == "base":
            s = self._whatif_punch_shown()
            if s is None:
                return None
            idx = self._whatif_punched_index_at(s, x, y)
            if idx is None:
                return None
            v = s.added_vias[idx]
            board = self._whatif_punch_board()
            d2 = (v.x_mm - x) ** 2 + (v.y_mm - y) ** 2
            return d2, (f"   Added via: {v.net}, "
                        + WV.via_props_text(v, board["enabled"],
                                            board["names"]))
        diff = getattr(self, "_whatif_diff", None)
        if diff is None or view != "diff":
            return None
        rows = diff.get("added_via_rows")
        if rows is None:
            rows = diff["added_via_rows"] = self._whatif_added_via_rows(
                diff["solution"], diff["metadata"])
        best = None
        for r in rows:
            rad = max(float(r.get("diameter_mm") or 0.0) * 0.5, 0.15)
            d2 = (float(r["x_mm"]) - x) ** 2 + (float(r["y_mm"]) - y) ** 2
            if d2 <= rad * rad and (best is None or d2 < best[0]):
                best = (d2, r)
        if best is None:
            return None
        text = self._format_via_hover_text(best[1])
        if not text:
            return None
        text = text.replace("Via:", "Added via (what-if):", 1)
        md = diff.get("metadata") or {}
        r = best[1]
        if md.get("vias"):
            names = {int(row["layer_id"]): str(row.get("name") or "")
                     for row in md.get("stackup") or []
                     if row.get("layer_id") is not None}
            src = next((v for v in md.get("vias") or []
                        if abs(float(v.get("x_mm", 0)) - float(r["x_mm"])) < 1e-6
                        and abs(float(v.get("y_mm", 0)) - float(r["y_mm"])) < 1e-6),
                       None)
            if src is not None:
                via = WV.AddedVia(
                    float(src["x_mm"]), float(src["y_mm"]), str(src["net"]),
                    int(src["layer_start"]), int(src["layer_end"]),
                    float(src["diameter_mm"]), float(src["hole_diameter_mm"]))
                text += "   (" + WV.via_props_text(
                    via, [int(i) for i in md.get(
                        "enabled_copper_layer_ids") or []], names) + ")"
        return best[0], text

    @staticmethod
    def _whatif_added_via_rows(solution, metadata) -> list[dict]:
        """Via rows (as the Vias tab computes them) for the vias the
        what-if added - the last ``count`` entries of its ``vias``."""
        md = metadata or {}
        n = int((md.get(WV.METADATA_KEY) or {}).get("count") or 0)
        vias = list(md.get("vias") or [])
        if n <= 0 or n > len(vias):
            return []
        from fypa.solution_sampling import SolutionSampler, compute_via_rows
        sub = dict(md)
        sub["vias"] = vias[-n:]
        try:
            return compute_via_rows(SolutionSampler(solution, sub))
        except Exception:
            return []

    def _whatif_punch_try(self, x: float, y: float, net: str, board: dict,
                          s) -> tuple[WV.AddedVia | None, str, set]:
        """``(via, "", repoured_nets)`` when a via may go at ``(x, y)`` on
        ``net``, else ``(None, reason, set())``. ``repoured_nets`` are the
        other nets whose pour (or plane) the solve will cut a clearance
        hole in."""
        tmpl = self._whatif_punch_template(board, net, x, y)
        via = tmpl.make(x, y, net)
        span = WV.span_layer_ids(via.layer_start, via.layer_end,
                                 board["enabled"])
        survey = self._whatif_punch_survey(x, y, net, via.diameter_mm / 2.0,
                                           span)
        why = WV.punch_problem(via, survey, board["enabled"], board["planes"],
                               board["names"],
                               self._whatif_punch_other_vias(board, s))
        if why:
            return None, why, set()
        repoured = set()
        for _own, _fixed, pour in survey.values():
            repoured |= pour
        return via, "", repoured

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
        via, why, repoured = self._whatif_punch_try(x, y, net, board, s)
        if via is None:
            self.statusBar().showMessage(f"No via added: {why}.", 6000)
            return
        s.added_vias.append(via)
        self._whatif_punch_changed(s)
        tmpl = self._whatif_punch_template(board, net, x, y)
        msg = (f"Added a {WV.via_props_text(via, board['enabled'], board['names'])}"
               f" via on {net} ({tmpl.source})")
        if repoured:
            msg += (f"; {', '.join(sorted(repoured))} repoured around it "
                    f"with {self._whatif_punch_clearance(s):.3g} mm "
                    "clearance")
        self.statusBar().showMessage(msg + ".", 5000)

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
        repoured: set = set()
        for px, py in pts:
            via, _why, rp = self._whatif_punch_try(px, py, net, board, s)
            if via is None:
                skipped += 1
                continue
            # Appended as it goes, so the next point checks against it.
            s.added_vias.append(via)
            repoured |= rp
            added += 1
        if added:
            self._whatif_punch_changed(s)
        msg = (f"Added {added} via(s) on {net} at {pitch:g} mm pitch"
               if added else f"No vias added on {net}")
        if repoured:
            msg += f"; {', '.join(sorted(repoured))} repoured around them"
        if skipped:
            msg += (f"; {skipped} spot(s) skipped (off {net}'s copper, "
                    "touching another net's tracks or pads, or too close "
                    "to a via)")
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
        return (s.id, self._whatif_punch_clearance(s),
                tuple((v.x_mm, v.y_mm, v.diameter_mm) for v in s.added_vias))

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
        keep = pads + 2.0 * self._whatif_punch_clearance(s)
        return [
            # The repour clearance: a hollow ring where other nets' copper
            # is cut back to.
            MarkerGroup(xs=xs, ys=ys, zs=zs, color="#00000000",
                        symbol="o", size=14, edge_color=self._PUNCH_COLOR,
                        edge_width=1.2, world_diameters_mm=keep,
                        min_pixel_diameter=self._PUNCH_MIN_PX * 1.4,
                        occludable=False),
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
