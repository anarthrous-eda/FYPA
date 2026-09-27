"""Editor-mode marquee (rubber-band) multi-selection."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
)

from fypa.viewer.numeric import _numeric_validator
from fypa.viewer.overlays import _EDITOR_MARKER_HIT_PX, _EDITOR_SCHDOC
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _esc


class _MarqueeMixin:
    """Editor-mode marquee (rubber-band) multi-selection."""

    # --- Editor mode: marquee (rubber-band) multi-selection ---------------
    #
    # A left drag on empty 2D viewport sweeps a box (drawn by the GL widget)
    # and fires ``editorMarqueeSelected``. Every PDN marker *fully* enclosed
    # joins the selection; Shift extends, Ctrl toggles. A selection of two or
    # more markers that are all SINKs gets the ordinary sink form with ``*``
    # in every row the sinks disagree on, and one Apply writes the touched
    # rows to all of them. Any other mix just reports a count.

    def _editor_directive_points(self, d, visible_ids, id_to_phys):
        """Glyph positions the viewport draws for editor directive ``d``, as
        ``(p_side, n_side)`` lists of ``(x_mm, y_mm, ring_colour, z_mm)``.

        ``None`` when the directive draws nothing the user can see - its
        layer is hidden, or it has no resolvable pads at all. Shared by the
        marker renderer (:meth:`_editor_marker_groups`) and the marquee
        hit-test (:meth:`_marquee_candidates`) so the box selects exactly
        what is on screen and nothing else."""
        p_pts: list[tuple[float, float, str | None, float]] = []
        n_pts: list[tuple[float, float, str | None, float]] = []
        if d.kind == "free" and d.anchor_xy is not None:
            lid = self._free_marker_layer_id(d)
            if lid not in visible_ids:
                return None   # marker's layer is hidden
            phys = id_to_phys.get(lid)
            rc = self._layer_color_for(phys) if phys else None
            rz = self._layer_z_for(phys) if phys else 0.0
            p_pts = [(float(d.anchor_xy[0]), float(d.anchor_xy[1]), rc, rz)]
        elif d.kind == "component":
            # One marker per component pin on the directive's net(s) that
            # sits on a visible layer, mirroring the solved-directive pin
            # markers. Falls back to a single glyph at the component centre
            # only when the directive has no matching pads at all (not
            # merely hidden ones).
            p_pts = self._component_pad_points(
                d.designator, [d.p_net], visible_ids,
                with_layer_color=True, pin_filter=d.p_pins)
            if not d.single_net and d.n_net:
                n_pts = self._component_pad_points(
                    d.designator, [d.n_net], visible_ids,
                    with_layer_color=True, pin_filter=d.n_pins)
            if not p_pts and not n_pts:
                has_any_pad = bool(
                    self._component_pad_points(
                        d.designator, [d.p_net], pin_filter=d.p_pins)
                    or (d.n_net and self._component_pad_points(
                        d.designator, [d.n_net], pin_filter=d.n_pins)))
                if has_any_pad:
                    return None   # pads exist but are all on hidden layers
                ctr = self._component_center(d.designator)
                if ctr is None:
                    return None
                p_pts = [(float(ctr[0]), float(ctr[1]), None, 0.0)]
        else:
            return None
        return p_pts, n_pts

    def _marquee_candidates(self) -> list:
        """Every marquee-selectable PDN marker, as
        :class:`~fypa.editor_multiselect.MarqueeCandidate` records.

        Copper and roleless components are deliberately absent: the board is
        wall-to-wall copper and passives, so including them would make a
        mixed-type selection the outcome of almost every drag. Placed editor
        directives come first; a component whose values still live in the
        Altium schematic contributes a ``("schematic", designator)``
        candidate unless an editor directive already overrides it."""
        from fypa.editor_multiselect import MarqueeCandidate

        out: list[MarqueeCandidate] = []
        if not self._editor_mode:
            return out
        visible_ids = self._visible_layer_ids()
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        # Designators an editor directive already speaks for, so the
        # schematic pass below does not offer a second candidate for them.
        claimed: set[str] = set()
        if self._project is not None:
            for d in self._project.editor_directives:
                if d.role not in ("SOURCE", "SINK", "SERIES"):
                    continue
                if not self._directive_rail_visible(d):
                    continue   # marker's rail is hidden
                pts = self._editor_directive_points(
                    d, visible_ids, id_to_phys)
                if pts is None:
                    continue
                xy = tuple((float(p[0]), float(p[1]))
                           for p in (pts[0] + pts[1]))
                if not xy:
                    continue
                if d.designator:
                    claimed.add(d.designator)
                if d.overrides_designator:
                    claimed.add(d.overrides_designator)
                out.append(MarqueeCandidate(
                    key=("directive", d.id), role=d.role, points=xy,
                    label=d.designator or f"marker {d.id[:6]}",
                ))
        for sd in (self.metadata or {}).get("directives") or []:
            # Directives the editor itself synthesised are already covered
            # by the editor-directive pass above.
            if sd.get("schdoc") == _EDITOR_SCHDOC:
                continue
            role = {"RESISTOR": "SERIES"}.get(sd.get("role"), sd.get("role"))
            if role not in ("SOURCE", "SINK", "SERIES"):
                continue
            des = sd.get("designator")
            if not des or des in claimed:
                continue
            claimed.add(des)   # one candidate per component
            pts: list[tuple[float, float]] = []
            for term in (sd.get("terminals") or {}).values():
                for pin in term.get("pins") or []:
                    px, py = pin.get("x_mm"), pin.get("y_mm")
                    if px is not None and py is not None:
                        pts.append((float(px), float(py)))
            if not pts:
                continue
            out.append(MarqueeCandidate(
                key=("schematic", des), role=role,
                points=tuple(pts), label=des,
            ))
        return out

    def _marquee_candidate_at(self, world_x: float, world_y: float):
        """The marquee candidate whose nearest glyph is under the cursor, or
        ``None``. Used by Shift / Ctrl click so single picks can extend or
        trim a marquee selection. Hit radius tracks the on-screen marker
        size, matching :meth:`_free_marker_at`."""
        try:
            _cx, _cy, mm_per_px = self._gl_viewer.view_center_scale()
        except Exception:
            mm_per_px = 0.1
        radius = max(float(mm_per_px) * _EDITOR_MARKER_HIT_PX, 1e-6)
        best = None
        best_d2 = radius * radius
        for cand in self._marquee_candidates():
            for px, py in cand.points:
                dx, dy = world_x - px, world_y - py
                d2 = dx * dx + dy * dy
                if d2 <= best_d2:
                    best, best_d2 = cand, d2
        return best

    @staticmethod
    def _multi_entry_for(cand) -> dict:
        """Selection entry for a marquee candidate."""
        return {"kind": cand.key[0], "id": cand.key[1],
                "role": cand.role, "label": cand.label}

    def _on_editor_marquee(self, x0: float, y0: float, x1: float, y1: float,
                           additive: bool, toggle: bool) -> None:
        """Commit a rubber-band sweep. Plain drag replaces the selection,
        Shift+drag adds the enclosed markers, Ctrl+drag removes them."""
        if not self._editor_mode:
            return
        from fypa.editor_multiselect import marquee_select, normalise_rect

        rect = normalise_rect(x0, y0, x1, y1)
        hits = marquee_select(self._marquee_candidates(), rect)
        entries = [self._multi_entry_for(c) for c in hits]
        if additive or toggle:
            base = self._editor_multi or self._selection_as_multi_entries()
            merged: dict[tuple[str, str], dict] = {
                (e["kind"], e["id"]): e for e in base
            }
            for e in entries:
                key = (e["kind"], e["id"])
                if toggle and key in merged:
                    merged.pop(key)
                else:
                    merged[key] = e
            entries = list(merged.values())
        elif not entries:
            self.statusBar().showMessage(
                "Nothing fully inside the selection box - a marker counts "
                "only when every one of its pins is enclosed.", 3000)
        self._set_editor_multi(entries)

    def _selection_as_multi_entries(self) -> list[dict]:
        """The current single selection expressed as multi-select entries,
        so Shift / Ctrl can grow a selection that started as one click.
        Empty for a copper / nothing selection."""
        sel = self._editor_selection
        if not sel:
            return []
        d = self._directive_for_selection()
        if d is not None:
            return [{"kind": "directive", "id": d.id, "role": d.role,
                     "label": d.designator or f"marker {d.id[:6]}"}]
        if sel.get("kind") == "component":
            des = sel.get("designator")
            sch = self._unlockable_schematic_directive(sel)
            if sch is not None and des:
                role = {"RESISTOR": "SERIES"}.get(
                    sch.get("role"), sch.get("role"))
                return [{"kind": "schematic", "id": des, "role": role,
                         "label": des}]
        return []

    def _multi_entry_live(self, entry: dict) -> bool:
        """Whether the object an entry names still exists. A directive can
        be deleted (or undone away) while its entry is held."""
        if entry.get("kind") == "directive":
            return (self._project is not None
                    and self._project.directive_by_id(entry.get("id") or "")
                    is not None)
        return self._component_record(entry.get("id")) is not None

    def _set_editor_multi(self, entries: list[dict]) -> None:
        """Make ``entries`` the marquee selection, refreshing the panel and
        viewport. Dead entries are dropped; fewer than two survivors
        collapse to the ordinary single selection (or to nothing), so no
        pre-existing code path ever sees a one-element multi-selection."""
        entries = [e for e in entries if self._multi_entry_live(e)]
        if len(entries) < 2:
            self._editor_multi = []
            if entries:
                # The single-selection path renders via its own highlight.
                self._select_multi_entry(entries[0])
                return
            self._editor_selection = None
            self._gl_viewer.set_primitive_selection_outline(None)
            self._clear_editor_highlight()
            self._populate_editor_form()
            # Explicit: _clear_editor_highlight only renders when there *was*
            # a highlight, and the emphasis boxes have to come off regardless.
            self._render()
            return
        self._editor_multi = entries
        self._editor_selection = None
        self._gl_viewer.set_primitive_selection_outline(None)
        self._populate_editor_form()
        # Renders, so it goes last - the marker emphasis and the copper
        # highlight then land in the same frame (one re-render, not two).
        self._apply_editor_highlight(self._multi_highlight_nets())

    def _select_multi_entry(self, entry: dict) -> None:
        """Route a single multi-select entry through the ordinary
        single-selection path, so a one-object marquee behaves exactly like
        a click on that object."""
        if entry.get("kind") == "directive":
            d = (self._project.directive_by_id(entry.get("id") or "")
                 if self._project else None)
            if d is None:
                return
            if d.kind == "component":
                rec = self._component_record(d.designator)
                if rec is not None:
                    self._select_component(rec)
                else:
                    # Component gone from the extraction (board reloaded?):
                    # the free-marker form would want an anchor it has not
                    # got, so select nothing rather than build a broken form.
                    self._editor_selection = None
                    self._clear_editor_highlight()
                    self._populate_editor_form()
                return
            self._select_free_marker(d)
            return
        rec = self._component_record(entry.get("id"))
        if rec is not None:
            self._select_component(rec)

    def _multi_directives(self) -> list:
        """The editor directives behind the multi-selection, in selection
        order. Schematic-only entries are not included - see
        :meth:`_multi_locked`."""
        if self._project is None:
            return []
        out = []
        for e in self._editor_multi:
            if e.get("kind") != "directive":
                continue
            d = self._project.directive_by_id(e.get("id") or "")
            if d is not None:
                out.append(d)
        return out

    def _multi_locked(self) -> list[dict]:
        """Multi-selection entries whose values still come from the Altium
        schematic (no editor directive yet)."""
        return [e for e in self._editor_multi
                if e.get("kind") == "schematic"]

    def _multi_designators(self) -> list[str]:
        """Component designators in the multi-selection - one per
        component-bound sink, for the viewport's yellow selection boxes."""
        out: list[str] = []
        for e in self._editor_multi:
            if e.get("kind") == "schematic":
                des = e.get("id")
            else:
                d = (self._project.directive_by_id(e.get("id") or "")
                     if self._project else None)
                des = d.designator if d is not None and d.kind == "component" \
                    else None
            if des and des not in out:
                out.append(des)
        return out

    def _multi_roles(self) -> set[str]:
        """Live PDN roles across the multi-selection. Recomputed rather than
        read off the entries so an Apply that changed a role is reflected."""
        roles = {d.role for d in self._multi_directives()}
        for e in self._multi_locked():
            roles.add(e.get("role") or "")
        return roles

    def _multi_highlight_nets(self) -> set[str]:
        """Union of the copper nets every selected sink touches, so the
        viewport dims everything the selection does not reach."""
        nets: set[str] = set()
        for d in self._multi_directives():
            for nm in (d.p_net, d.n_net):
                if nm and nm != "(none)":
                    nets |= self._connected_nets(nm)
        for e in self._multi_locked():
            rec = self._component_record(e.get("id")) or {}
            for nm in rec.get("nets") or []:
                if nm and nm != "(none)":
                    nets |= self._connected_nets(nm)
        return nets

    @staticmethod
    def _multi_field_text(value) -> str:
        """Panel text for a merged field value: ``*`` for a disagreement,
        blank for a shared unset value, else the number / string."""
        from fypa.editor_multiselect import MIXED

        if value is MIXED:
            return "*"
        if value is None:
            return ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:g}"
        return str(value)

    def _multi_merged_sinks(self) -> list:
        """One directive-shaped object per selected sink for the ``*`` merge.

        A still-locked sink has no editor directive, so it contributes the
        directive its Unlock would seed - that way its schematic current /
        Min V / nets take part in the comparison instead of the row falsely
        reading as shared."""
        objs = list(self._multi_directives())
        for e in self._multi_locked():
            seeded = self._directive_from_schematic(e.get("id"))
            if seeded is not None:
                objs.append(seeded)
        return objs

    def _populate_multi_editor_form(self) -> None:
        """Build the side-panel form for a marquee multi-selection.

        All SINKs ⇒ the single-sink form with a ``*`` in every row they
        disagree on. Anything else ⇒ a bare count, per the feature's
        "multiple objects selected" rule: no controls, because there is no
        one set of properties that applies."""
        from fypa.editor_multiselect import MIXED, common_fields

        lay = self._multi_form_layout
        self._clear_layout(lay)
        self._mf_dirty = set()
        for attr in ("_mf_current", "_mf_min_v", "_mf_single", "_mf_two",
                     "_mf_pnet", "_mf_nnet", "_mf_nnet_label", "_mf_status",
                     "_mf_btngroup"):
            setattr(self, attr, None)
        t = _T()
        entries = self._editor_multi
        count = len(entries)
        roles = self._multi_roles()

        if roles != {"SINK"}:
            lay.addWidget(QLabel("<b>Multiple objects selected</b>"))
            tally: dict[str, int] = {}
            for r in (e.get("role") or "?" for e in entries):
                tally[r] = tally.get(r, 0) + 1
            breakdown = ", ".join(f"{n} × {r}" for r, n in sorted(
                tally.items(), key=lambda kv: (-kv[1], kv[0])))
            info = QLabel(
                f"{count} objects ({_esc(breakdown)}). Properties are only "
                "editable together when every selected object is a SINK - "
                "drag a box around sinks alone, or click one to edit it."
            )
            info.setWordWrap(True)
            info.setStyleSheet(f"color: {t['fg_muted']};")
            lay.addWidget(info)
            self._update_editor_panel()
            return

        locked = self._multi_locked()
        vals = common_fields(self._multi_merged_sinks())

        lay.addWidget(QLabel(f"<b>{count} SINKs selected</b>"))
        roll = QLabel(_esc(", ".join(e.get("label") or "?" for e in entries)))
        roll.setWordWrap(True)
        roll.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(roll)
        note = QLabel(
            "A row showing <b>*</b> differs between the selected sinks. "
            "Apply writes only the rows you edit - the rest are left as "
            "they are on every sink."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(note)
        if locked:
            warn = QLabel(
                f"<span style='color:{t['warn']};'>{len(locked)} of these "
                "still take their values from the Altium schematic. Apply "
                "unlocks them and overrides it on the next resolve.</span>"
            )
            warn.setWordWrap(True)
            lay.addWidget(warn)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        # Role is shown, not offered: re-pointing a batch of sinks at
        # SOURCE would silently reinterpret every Current as a Voltage.
        form.addRow("PDN role", QLabel("SINK"))
        self._mf_current = QLineEdit()
        self._mf_current.setValidator(_numeric_validator(self, top=1e12))
        self._mf_current.setText(self._multi_field_text(vals["current"]))
        self._mf_current.setToolTip(
            "Current drawn by each selected sink (not shared between them). "
            "Applying 2 A to four sinks adds 8 A of load."
        )
        self._mf_current.textEdited.connect(
            lambda *_: self._mf_dirty.add("current"))
        form.addRow("Current (A)", self._mf_current)
        self._mf_min_v = QLineEdit()
        self._mf_min_v.setValidator(_numeric_validator(self, top=1e12))
        self._mf_min_v.setText(self._multi_field_text(vals["min_voltage"]))
        self._mf_min_v.setToolTip(
            "Optional minimum acceptable rail voltage at each sink's pins. "
            "Clear the field and Apply to drop the check on all of them."
        )
        self._mf_min_v.textEdited.connect(
            lambda *_: self._mf_dirty.add("min_voltage"))
        form.addRow("Min V (V)", self._mf_min_v)
        lay.addLayout(form)

        model_mixed = vals["single_net"] is MIXED
        lay.addWidget(QLabel(
            "Current model <b>*</b>" if model_mixed else "Current model"))
        self._mf_single = QRadioButton("Single net (point-to-point)")
        self._mf_two = QRadioButton("Two nets (full current-path loop)")
        self._mf_btngroup = QButtonGroup(self._multi_form_host)
        self._mf_btngroup.addButton(self._mf_single)
        self._mf_btngroup.addButton(self._mf_two)
        if not model_mixed:
            self._mf_single.setChecked(bool(vals["single_net"]))
            self._mf_two.setChecked(not bool(vals["single_net"]))
        # Mixed ⇒ both left unchecked (the exclusive group allows that as an
        # initial state), which is the radio-button spelling of ``*``.
        self._mf_single.clicked.connect(self._on_multi_model_clicked)
        self._mf_two.clicked.connect(self._on_multi_model_clicked)
        lay.addWidget(self._mf_single)
        lay.addWidget(self._mf_two)

        form2 = QFormLayout()
        form2.setContentsMargins(0, 0, 0, 0)
        nets = self._all_net_names()
        self._mf_pnet = QComboBox()
        self._mf_pnet.addItems((["*"] if vals["p_net"] is MIXED else []) + nets)
        if vals["p_net"] is not MIXED and vals["p_net"]:
            self._set_combo(self._mf_pnet, vals["p_net"])
        self._mf_pnet.activated.connect(
            lambda *_: self._mf_dirty.add("p_net"))
        form2.addRow("P net", self._mf_pnet)
        self._mf_nnet = QComboBox()
        self._mf_nnet.addItems((["*"] if vals["n_net"] is MIXED else []) + nets)
        if vals["n_net"] is not MIXED and vals["n_net"]:
            self._set_combo(self._mf_nnet, vals["n_net"])
        self._mf_nnet.activated.connect(
            lambda *_: self._mf_dirty.add("n_net"))
        self._mf_nnet_label = QLabel("N net")
        form2.addRow(self._mf_nnet_label, self._mf_nnet)
        lay.addLayout(form2)
        self._on_multi_model_clicked(record_dirty=False)

        btns = QHBoxLayout()
        apply_btn = QPushButton(f"Apply to {count}")
        apply_btn.clicked.connect(self._on_editor_multi_apply)
        remove_btn = QPushButton(f"Remove {count}")
        remove_btn.clicked.connect(self._on_editor_multi_remove)
        btns.addWidget(apply_btn)
        btns.addWidget(remove_btn)
        lay.addLayout(btns)

        undo_row = QHBoxLayout()
        self._ef_undo_move = QPushButton("↶ Undo")
        self._ef_undo_move.clicked.connect(self._undo_marker_action)
        self._ef_redo_move = QPushButton("↷ Redo")
        self._ef_redo_move.clicked.connect(self._redo_marker_action)
        undo_row.addWidget(self._ef_undo_move)
        undo_row.addWidget(self._ef_redo_move)
        lay.addLayout(undo_row)
        self._update_marker_undo_buttons()

        self._mf_status = QLabel("")
        self._mf_status.setWordWrap(True)
        self._mf_status.setStyleSheet(
            f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(self._mf_status)
        self._update_editor_panel()

    def _on_multi_model_clicked(self, *_args, record_dirty: bool = True
                                ) -> None:
        """Keep the N-net row in step with the current-model radios, and
        record that the user chose a model. The row stays visible while the
        model is still ``*`` (neither radio checked) because an N net may
        well be what the user is about to set."""
        if record_dirty:
            self._mf_dirty.add("single_net")
        two = self._mf_two is not None and self._mf_two.isChecked()
        single = self._mf_single is not None and self._mf_single.isChecked()
        show = two or not single    # two-net, or model still undecided
        if self._mf_nnet is not None:
            self._mf_nnet.setVisible(show)
        if self._mf_nnet_label is not None:
            self._mf_nnet_label.setVisible(show)

    def _multi_status(self, text: str, kind: str = "muted") -> None:
        """Write the multi-form status line (no-op if the form is gone)."""
        if self._mf_status is None:
            return
        colour = {"err": _T()["err"], "ok": _T()["ok"]}.get(
            kind, _T()["fg_muted"])
        self._mf_status.setText(
            f"<span style='color:{colour};'>{text}</span>")

    def _directive_from_schematic(self, designator: str | None):
        """A fresh, unregistered :class:`~fypa.project_file.EditorDirective`
        seeded from the Altium schematic directive on ``designator`` - the
        batch equivalent of the single-selection Unlock button. ``None``
        when the component or a usable schematic directive is missing."""
        from fypa.project_file import EditorDirective

        rec = self._component_record(designator)
        if rec is None:
            return None
        sel = {"kind": "component", "designator": designator,
               "bbox": rec.get("bbox"), "nets": list(rec.get("nets") or [])}
        sch = self._unlockable_schematic_directive(sel)
        if sch is None:
            return None
        role = {"RESISTOR": "SERIES"}.get(sch.get("role"), sch.get("role"))
        if role not in ("SOURCE", "SINK", "SERIES"):
            role = "SINK"
        terms = sch.get("terminals") or {}
        n_term = terms.get("N")
        single = ((n_term is None) or bool(n_term.get("ideal_return"))) \
            and role != "SERIES"
        value = sch.get("value")
        two_net_terminal = (not single) and role in ("SOURCE", "SINK")
        return EditorDirective(
            kind="component",
            role=role,
            designator=designator,
            single_net=single,
            p_net=self._terminal_primary_net(terms.get("P")),
            n_net=None if single else self._terminal_primary_net(n_term),
            p_pins=self._terminal_pin_pads(terms.get("P")) or None,
            n_pins=(None if single
                    else (self._terminal_pin_pads(n_term) or None)),
            p_des=(self._terminal_des_list(terms.get("P"), designator) or None
                   if two_net_terminal else None),
            n_des=(self._terminal_des_list(n_term, designator) or None
                   if two_net_terminal else None),
            voltage=value if role == "SOURCE" else None,
            current=value if role == "SINK" else None,
            resistance=value if role == "SERIES" else None,
            min_voltage=sch.get("min_voltage"),
            overrides_designator=sch.get("designator"),
        )

    def _multi_edits_from_form(self) -> dict | None:
        """Parse the touched rows of the multi-sink form into field ⇒ value.

        Only rows the user actually edited are read, so a row still showing
        ``*`` (or an untouched shared value) is absent and Apply leaves it
        alone on every sink. ``None`` on a parse error, with the reason
        already written to the status line."""
        dirty = set(self._mf_dirty)
        # A combo left sitting on the ``*`` placeholder is not a choice.
        for name, combo in (("p_net", self._mf_pnet),
                            ("n_net", self._mf_nnet)):
            if combo is not None and combo.currentText() == "*":
                dirty.discard(name)
        edits: dict = {}
        if "current" in dirty:
            txt = self._mf_current.text().strip()
            try:
                edits["current"] = float(txt)
            except ValueError:
                self._multi_status(
                    "Current must be a number - a sink has to draw "
                    "something.", "err")
                return None
        if "min_voltage" in dirty:
            txt = self._mf_min_v.text().strip()
            if not txt:
                edits["min_voltage"] = None   # drops the check on all of them
            else:
                try:
                    edits["min_voltage"] = float(txt)
                except ValueError:
                    self._multi_status(
                        "Min V must be a number, or blank to drop the "
                        "check.", "err")
                    return None
        if "single_net" in dirty:
            if self._mf_two.isChecked():
                edits["single_net"] = False
            elif self._mf_single.isChecked():
                # Single-net's N terminal is an ideal return with no pads.
                edits["single_net"] = True
                edits["n_net"] = None
            else:
                dirty.discard("single_net")
        if "n_net" in dirty and edits.get("single_net") is not True:
            edits["n_net"] = self._mf_nnet.currentText() or None
        if "p_net" in dirty:
            edits["p_net"] = self._mf_pnet.currentText() or None
            if not edits["p_net"]:
                self._multi_status("Pick a P net.", "err")
                return None
        return edits

    def _on_editor_multi_apply(self) -> None:
        """Write the edited rows to every selected sink as one undoable step.

        Sinks still locked to the Altium schematic are materialised into
        override directives first (the batch Unlock), seeded from their
        schematic values so untouched rows keep what the schematic said."""
        import copy as _copy

        if not self._editor_multi or self._multi_roles() != {"SINK"}:
            return
        edits = self._multi_edits_from_form()
        if edits is None:
            return
        if not edits:
            self._multi_status(
                "Nothing to apply - edit a row first. Rows showing * are "
                "left alone on purpose.")
            return

        proj = self._ensure_project()
        entries_before = [dict(e) for e in self._editor_multi]
        items: list[dict] = []
        targets: list = []
        entries_after: list[dict] = []
        for e in self._editor_multi:
            if e.get("kind") == "directive":
                d = proj.directive_by_id(e.get("id") or "")
                if d is None:
                    continue
                items.append({"id": d.id,
                              "index": proj.editor_directives.index(d),
                              "before": _copy.deepcopy(d)})
            else:
                d = self._directive_from_schematic(e.get("id"))
                if d is None:
                    continue
                proj.upsert_directive(d)
                items.append({"id": d.id,
                              "index": len(proj.editor_directives) - 1,
                              "before": None})
            targets.append(d)
            entries_after.append({"kind": "directive", "id": d.id,
                                  "role": d.role,
                                  "label": d.designator
                                  or f"marker {d.id[:6]}"})
        if not targets:
            self._multi_status(
                "Nothing left to apply to - the selection is stale.", "err")
            return

        for d in targets:
            for name, value in edits.items():
                setattr(d, name, value)
        # A two-net sink needs a real N net; roll the whole batch back rather
        # than leaving half the selection in an unsolvable state.
        missing = [d.designator or d.id[:6]
                   for d in targets if not d.single_net and not d.n_net]
        if missing:
            self._apply_batch_side(items, "before")
            shown = ", ".join(missing[:6])
            more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
            self._multi_status(
                f"Two nets needs an N net - {_esc(shown)}{more} have none. "
                "Set N net too, or choose Single net.", "err")
            return

        for it, d in zip(items, targets):
            it["after"] = _copy.deepcopy(d)
        items.sort(key=lambda it: it["index"])
        self._marker_undo.append({
            "op": "batch",
            "items": items,
            "entries_before": entries_before,
            "entries_after": entries_after,
        })
        self._marker_redo.clear()
        self._editor_multi = entries_after
        self._mark_project_dirty()
        self._update_pending_rails()
        self._apply_editor_highlight(self._multi_highlight_nets())
        self._render()
        # Rebuild so rows that just became shared stop reading ``*`` and the
        # locked-sink warning goes away now that overrides exist. This
        # replaces _mf_status, so the message goes out afterwards.
        self._populate_editor_form()
        rows = ", ".join(sorted(edits))
        self._multi_status(
            f"Applied {_esc(rows)} to {len(targets)} sinks - press Resolve "
            "to re-solve.", "ok")

    def _on_editor_multi_remove(self) -> None:
        """Delete every selected sink's editor directive in one undoable
        step. Sinks that only exist in the Altium schematic have nothing to
        delete and are left alone."""
        import copy as _copy

        if not self._editor_multi or self._project is None:
            return
        proj = self._project
        pairs = []
        for e in self._editor_multi:
            if e.get("kind") != "directive":
                continue
            d = proj.directive_by_id(e.get("id") or "")
            if d is not None:
                pairs.append((e, d))
        skipped = len(self._editor_multi) - len(pairs)
        if not pairs:
            self._multi_status(
                "Nothing to remove - these sinks are defined in the Altium "
                "schematic, not the editor.", "err")
            return
        note = ("" if not skipped else
                f"\n\n{skipped} schematic-defined sink(s) in the selection "
                "will be left alone.")
        if QMessageBox.question(
            self, "Remove sinks",
            f"Remove {len(pairs)} sink directive(s) from the editor?"
            f"{note}\n\nCtrl+Z undoes this.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        items = [{"id": d.id, "index": proj.editor_directives.index(d),
                  "before": _copy.deepcopy(d), "after": None}
                 for _e, d in pairs]
        items.sort(key=lambda it: it["index"])
        entries_before = [dict(e) for e in self._editor_multi]
        for _e, d in pairs:
            proj.remove_directive(d.id)
        remaining = [e for e in self._editor_multi
                     if e.get("kind") != "directive"]
        self._marker_undo.append({
            "op": "batch",
            "items": items,
            "entries_before": entries_before,
            "entries_after": [dict(e) for e in remaining],
        })
        self._marker_redo.clear()
        self._mark_project_dirty()
        self._update_pending_rails()
        self._set_editor_multi(remaining)
        self.statusBar().showMessage(
            f"Removed {len(pairs)} sink directive(s) - Ctrl+Z to restore.",
            4000)

    def _apply_batch_side(self, items: list[dict], side: str) -> None:
        """Restore one side of a batch edit. ``side`` is ``"before"`` (undo,
        or an Apply rollback) or ``"after"`` (redo); a ``None`` snapshot
        means the directive did not exist on that side, so it is removed.
        Items are kept index-sorted so re-inserts land back in order."""
        import copy as _copy

        proj = self._project
        if proj is None:
            return
        for it in items:
            snap = it.get(side)
            if snap is None:
                proj.remove_directive(it["id"])
                continue
            d = _copy.deepcopy(snap)
            if proj.directive_by_id(it["id"]) is None:
                idx = min(max(int(it.get("index", 0)), 0),
                          len(proj.editor_directives))
                proj.editor_directives.insert(idx, d)
            else:
                proj.upsert_directive(d)

    def _after_batch_undo_redo(self, entries: list[dict]) -> None:
        """Shared tail of a batch undo / redo - restore the selection that
        the step was made against and refresh the panel + viewport."""
        self._mark_project_dirty()
        self._update_pending_rails()
        self._update_marker_undo_buttons()
        self._set_editor_multi([dict(e) for e in entries or []])

    def _refresh_editor_selection(self) -> None:
        """Push the selected component's world-space bounding box to the
        GL viewer for its yellow selection box — the component-level twin
        of the source / sink marker's selection box. Cleared when nothing,
        or a non-component, is selected. Called every render so it stays
        in sync without scattered set / clear calls."""
        boxes: list[tuple[float, float, float, float]] = []
        if self._editor_mode and self._editor_multi:
            # One box per multi-selected component-bound sink, so the
            # viewport shows exactly which parts Apply will write to.
            for des in self._multi_designators():
                rec = self._component_record(des)
                b = rec.get("bbox") if rec else None
                if b and len(b) == 4:
                    boxes.append((float(b[0]), float(b[1]),
                                  float(b[2]), float(b[3])))
        else:
            sel = self._editor_selection
            if self._editor_mode and sel and sel.get("kind") == "component":
                b = sel.get("bbox")
                if b and len(b) == 4:
                    boxes.append((float(b[0]), float(b[1]),
                                  float(b[2]), float(b[3])))
        self._gl_viewer.set_editor_selection_bboxes(boxes)
