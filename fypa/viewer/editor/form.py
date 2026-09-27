"""Editor-mode PDN form, free markers and marker moves with undo."""
from __future__ import annotations

import numpy as np
import shapely.geometry as _sg
from fypa.gl_mesh_viewer import MarkerGroup
from PySide6.QtCore import Qt
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

from fypa.viewer.numeric import _numeric_validator, _parse_numeric_text
from fypa.viewer.overlays import (
    _EDITOR_MARKER_EDGE_W,
    _EDITOR_MARKER_SELECT_SCALE,
    _EDITOR_SCHDOC,
    _MARKER_LAYER_RING_W,
    _N_NET_MARKER_EDGE,
)
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _esc


class _EditorFormMixin:
    """Editor-mode PDN form, free markers and marker moves with undo."""

    # --- Editor mode: PDN form + free markers -------------------------------

    def _ensure_project(self):
        """Return the in-memory :class:`ProjectFile`, creating an empty one
        (seeded from the loaded metadata) the first time an editor edit
        needs somewhere to live. Pickle paths stay blank until a save."""
        if self._project is None:
            from fypa.project_file import ProjectFile
            self._project = ProjectFile()
            if self.metadata:
                self._project.prjpcb_path = self.metadata.get("prjpcb_path")
                self._project.pcbdoc_path = self.metadata.get("pcbdoc_path")
        return self._project

    def _all_net_names(self) -> list[str]:
        """Sorted list of every net name known to this viewer — solved
        per-net layers, rail-group members, metadata copper / pads /
        vias, and any user-supplied :class:`CopperName` renames. Used
        to populate the N-net picker. Cached; invalidate by setting
        ``_all_net_names_cache`` to ``None`` after edits."""
        cached = getattr(self, "_all_net_names_cache", None)
        if cached is not None:
            return cached
        nets: set[str] = set()
        for (_phys, net) in self._index_by_pair:
            nets.add(net)
        for members in self._rail_to_members.values():
            nets.update(members)
        md = self.metadata or {}
        for rec in md.get("components", []):
            nets.update(rec.get("nets", []) or [])
        for key in ("pads", "vias", "pths", "all_copper"):
            for rec in md.get(key, []):
                n = rec.get("net")
                if n:
                    nets.add(n)
        if self._project is not None:
            for c in self._project.copper_names:
                if c.name:
                    nets.add(c.name)
        out = sorted(n for n in nets if n)
        self._all_net_names_cache = out
        return out

    def _component_center(self, designator: str | None
                          ) -> tuple[float, float] | None:
        """Centre of the named component's bounding box, or ``None``."""
        if not designator or not self.metadata:
            return None
        for rec in self.metadata.get("components", []):
            if rec.get("designator") == designator:
                bbox = rec.get("bbox")
                if bbox and len(bbox) == 4:
                    return (0.5 * (bbox[0] + bbox[2]),
                            0.5 * (bbox[1] + bbox[3]))
        return None

    def _component_pad_points(
        self, designator: str | None, nets,
        visible_layer_ids: set[int] | None = None,
        with_layer_color: bool = False,
        pin_filter=None,
    ) -> list[tuple[float, float]]:
        """Centres of the named component's pads that sit on any net in
        ``nets`` — one point per matching pin.

        Lets an editor directive draw a marker on every pin it couples to
        (mirroring the solved-directive pin markers) instead of one glyph
        at the component centre. Pad records are keyed ``"<comp>-<pad>"``
        in the metadata, so the component prefix isolates this component's
        pads. Returns ``[]`` when no pad matches, so the caller can fall
        back to :meth:`_component_center`.

        When ``visible_layer_ids`` is given, only pads sitting on at least
        one of those layers are returned — used to hide editor markers whose
        copper layer the user has turned off.

        With ``with_layer_color`` each point becomes ``(x, y, colour, z)``,
        where ``colour`` is the swatch colour of the pad's copper layer
        (preferring a visible one when filtering) — drives the layer-colour
        ring on editor markers — and ``z`` is that layer's world-z (mm) so
        the marker sits at the right depth in 3D. ``None`` / ``0.0`` when the
        pad's layer is unknown.

        ``pin_filter`` is an optional set/list of pad designators (the
        directive's PDN_PINS restriction); when given, only those pads are
        returned so a single-pin source draws one marker, not one per pad on
        the net."""
        if not designator or not self.metadata:
            return []
        want = {n for n in (nets or ()) if n}
        if not want:
            return []
        wanted_pins = ({str(p).upper() for p in pin_filter}
                       if pin_filter else None)
        id_to_phys = ({v: k for k, v in self._phys_name_to_layer_id.items()}
                      if with_layer_color else {})
        prefix = f"{designator}-"
        pts: list[tuple] = []
        for rec in self.metadata.get("pads", []):
            des = rec.get("designator") or ""
            if not des.startswith(prefix):
                continue
            if wanted_pins is not None and \
                    des[len(prefix):].upper() not in wanted_pins:
                continue
            if rec.get("net") not in want:
                continue
            lids = rec.get("layer_ids") or []
            if visible_layer_ids is not None and not any(
                    lid in visible_layer_ids for lid in lids):
                continue
            ring = rec.get("outline")
            if not ring:
                continue
            arr = np.asarray(ring, dtype=np.float64)
            if arr.ndim != 2 or arr.shape[0] < 1:
                continue
            cx = 0.5 * (float(arr[:, 0].min()) + float(arr[:, 0].max()))
            cy = 0.5 * (float(arr[:, 1].min()) + float(arr[:, 1].max()))
            if with_layer_color:
                # Colour by the pad's copper layer — favour a currently
                # visible one so a multi-layer pad rings in the layer the
                # user is actually looking at.
                lid = next((l for l in lids
                            if visible_layer_ids and l in visible_layer_ids),
                           lids[0] if lids else None)
                phys = id_to_phys.get(lid)
                color = self._layer_color_for(phys) if phys else None
                z = self._layer_z_for(phys) if phys else 0.0
                pts.append((cx, cy, color, z))
            else:
                pts.append((cx, cy))
        return pts

    def _directive_for_selection(self):
        """Return the :class:`EditorDirective` matching the current
        selection (component designator or free-marker id), or ``None``."""
        sel = self._editor_selection
        if not sel or self._project is None:
            return None
        if sel.get("kind") == "component":
            des = sel.get("designator")
            for d in self._project.editor_directives:
                if d.kind == "component" and d.designator == des:
                    return d
        elif sel.get("kind") == "free":
            return self._project.directive_by_id(sel.get("id") or "")
        return None

    @staticmethod
    def _clear_layout(layout) -> None:
        """Remove and delete every widget / nested layout in ``layout``."""
        from fypa.viewer.window import PdnViewer
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    PdnViewer._clear_layout(child)

    @staticmethod
    def _set_combo(combo: QComboBox, value: str) -> None:
        """Select ``value`` in ``combo``, adding it first if absent."""
        if value is None:
            return
        if combo.findText(value) < 0:
            combo.addItem(value)
        combo.setCurrentText(value)

    def _form_p_nets(self, sel: dict) -> list[str]:
        """Candidate P-net names for the form's P picker, given the
        selection: a component offers its pins' nets; a free marker offers
        the connected-copper group it sits on."""
        if sel.get("kind") == "component":
            return list(sel.get("nets", []) or []) or self._all_net_names()
        d = self._directive_for_selection()
        if d is not None and d.p_net:
            return sorted(self._connected_nets(d.p_net)) or [d.p_net]
        return self._all_net_names()

    @staticmethod
    def _terminal_net_label(term: dict | None) -> str:
        """Human label for a metadata directive terminal — its named net,
        else its pins' nets, else an ideal-return / dash placeholder."""
        if not term:
            return "—"
        if term.get("ideal_return"):
            return "ideal 0 V return"
        rn = term.get("requested_net")
        if rn:
            return rn
        nets = sorted({p.get("net") for p in term.get("pins", []) or []
                       if p.get("net")})
        return ", ".join(nets) if nets else "—"

    def _schematic_directives_for(self, sel: dict) -> list[dict]:
        """Schematic PDN directives (from the solve metadata) belonging to
        the selected component — matched by designator, or by a directive
        terminal pin landing inside the component's bounding box (robust to
        multi-channel designator re-basing)."""
        if not self.metadata:
            return []
        des = sel.get("designator")
        bbox = sel.get("bbox")
        out: list[dict] = []
        for d in self.metadata.get("directives", []):
            # Skip directives the editor itself synthesised: a free marker's
            # solved directive carries a synthetic pin at the marker's anchor,
            # which can land inside some component's bbox and would otherwise
            # be reported (and Unlock-able) as that component's "schematic"
            # directive — duplicating the free marker onto a component.
            if d.get("schdoc") == _EDITOR_SCHDOC:
                continue
            if des and d.get("designator") == des:
                out.append(d)
                continue
            if bbox and len(bbox) == 4:
                x0, y0, x1, y1 = bbox
                hit = False
                for term in (d.get("terminals") or {}).values():
                    for pin in term.get("pins", []) or []:
                        px, py = pin.get("x_mm"), pin.get("y_mm")
                        if (px is not None and py is not None
                                and x0 <= px <= x1 and y0 <= py <= y1):
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    out.append(d)
        return out

    def _build_schematic_info(self, lay, directives: list[dict]) -> None:
        """Read-only panel block describing a component's existing Altium
        schematic PDN directives (PDN_ROLE / PDN_V / PDN_I / PDN_*_NET)."""
        t = _T()
        blocks: list[str] = []
        for d in directives:
            # A ResistorSpec carries the role "RESISTOR" in solve metadata;
            # show the user-facing "SERIES" name to match the editor form.
            role = d.get("role", "?")
            if role == "RESISTOR":
                role = "SERIES"
            val = d.get("value_str") or ""
            terms = d.get("terminals") or {}
            tparts = [
                f"{tname}&nbsp;=&nbsp;"
                f"{_esc(self._terminal_net_label(terms[tname]))}"
                for tname in ("P", "N", "OUT_P", "OUT_N", "IN_P", "IN_N")
                if tname in terms
            ]
            head = f"<b>{_esc(role)}</b>"
            if val:
                head += f" &middot; {_esc(val)}"
            blocks.append(head + "<br>" + " &nbsp; ".join(tparts))
        box = QLabel(
            f"<span style='color:{t['fg_muted']};'>Defined in the Altium "
            f"schematic</span><br>{'<br><br>'.join(blocks)}"
        )
        box.setWordWrap(True)
        box.setTextFormat(Qt.RichText)
        box.setStyleSheet(
            f"QLabel {{ border: 1px solid {t['border']}; border-radius: 4px;"
            f" padding: 6px; background-color: {t['bg_alt']}; }}"
        )
        lay.addWidget(box)
        note = QLabel(
            "This rail is set up in the Altium schematic. Unlock it to "
            "override these values in FYPA for a re-solve."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(note)
        # Unlock button — for editable roles (SOURCE / SINK / SERIES);
        # REGULATOR directives aren't editable through this form. A
        # schematic SERIES carries the RESISTOR role name in solve metadata.
        if any(d.get("role") in ("SOURCE", "SINK", "RESISTOR")
               for d in directives):
            unlock = QPushButton("🔓  Unlock to edit")
            unlock.setToolTip(
                "Edit these PDN values in FYPA — the edited directive "
                "overrides the schematic one on the next resolve."
            )
            unlock.clicked.connect(self._on_editor_unlock)
            lay.addWidget(unlock)

    def _unlockable_schematic_directive(self, sel: dict) -> dict | None:
        """The first SOURCE / SINK / SERIES schematic directive on the
        selected component — the one the Unlock button / form edits. A
        schematic SERIES carries the ``RESISTOR`` role in solve metadata
        (named after its ResistorSpec)."""
        for d in self._schematic_directives_for(sel):
            if d.get("role") in ("SOURCE", "SINK", "RESISTOR"):
                return d
        return None

    @staticmethod
    def _terminal_primary_net(term: dict | None) -> str | None:
        """One representative net for a metadata directive terminal — its
        named net, else its first pin's net. ``None`` for an ideal return."""
        if not term or term.get("ideal_return"):
            return None
        rn = term.get("requested_net")
        if rn:
            return rn
        for p in term.get("pins", []) or []:
            if p.get("net"):
                return p["net"]
        return None

    @staticmethod
    def _terminal_pin_pads(term: dict | None) -> list[str]:
        """Pad designators of a metadata directive terminal's pins — the
        PDN_PINS set the schematic resolved to. ``[]`` for an ideal return or
        a terminal with no pins.

        Uses the raw ``pad`` field (not a compound ``J2-1`` label). Dedupes
        case-insensitively so multi-DES terminals with the same pad number on
        several connectors seed a single PDN_PINS entry.
        """
        if not term or term.get("ideal_return"):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for p in term.get("pins", []) or []:
            pad = p.get("pad")
            if pad in (None, ""):
                continue
            pad_s = str(pad)
            # Legacy metadata prefixed pad as ``COMP-PAD``; strip when the
            # component field matches the prefix so Unlock stays resolvable.
            comp = p.get("component")
            if (comp and pad_s.upper().startswith(str(comp).upper() + "-")):
                pad_s = pad_s[len(str(comp)) + 1:]
            key = pad_s.upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(pad_s)
        return out

    @staticmethod
    def _terminal_des_list(term: dict | None,
                           host: str | None) -> list[str]:
        """Unique component designators that contributed pins to ``term``.

        Used to seed P DES / N DES on Unlock. Host-only terminals return
        ``[]`` (blank DES ⇒ host component). Multi-connector terminals return
        every unique ``component`` that contributed a pin (order preserved).
        """
        if not term or term.get("ideal_return"):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for p in term.get("pins", []) or []:
            c = p.get("component")
            if not c:
                continue
            key = str(c).upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(str(c))
        if not out:
            return []
        if host and len(out) == 1 and out[0].upper() == str(host).upper():
            return []
        return out

    @staticmethod
    def _pin_display_pad(pin: dict | None) -> str:
        """Display label for a metadata pin — prefer compound ``pad_label``."""
        if not pin:
            return ""
        label = pin.get("pad_label")
        if label:
            return str(label)
        pad = pin.get("pad", "")
        comp = pin.get("component")
        if comp and pad:
            return f"{comp}-{pad}"
        return "" if pad is None else str(pad)

    @staticmethod
    def _parse_pin_field(text: str | None) -> list[str] | None:
        """Parse a comma-separated 'Pins' field into a pad-designator list,
        or ``None`` when blank (⇒ every pad of the component on the net)."""
        if not text:
            return None
        pins = [p.strip() for p in text.replace(";", ",").split(",")
                if p.strip()]
        return pins or None

    def _on_editor_unlock(self) -> None:
        """Unlock a schematic-defined component for editing — the read-only
        info is replaced by the editable form, seeded from the schematic
        directive. Applying it overrides the schematic directive."""
        sel = self._editor_selection
        if sel and sel.get("kind") == "component":
            sel["unlocked"] = True
            self._populate_editor_form()

    def _build_free_marker_location(self, lay, directive) -> None:
        """Location block for a selected free marker: a layer picker plus
        editable X / Y boxes and marker-edit undo / redo buttons (covering
        both moves and deletes). The layer is offered as a drop-down listing
        only the copper layers that carry the marker's net at its current
        X / Y — so the marker can be re-pinned to another layer of the same
        rail at the same spot (a stacked plane / pour). When the net exists on
        just one layer there, the picker collapses to a read-only label."""
        t = _T()
        lay.addWidget(QLabel("Location"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)

        ax, ay = directive.anchor_xy or (0.0, 0.0)
        net = getattr(directive, "p_net", None) or None
        cur_lid = self._free_marker_layer_id(directive)
        options = self._layers_with_net_at(float(ax), float(ay), net)
        # Guarantee the marker's current layer is always selectable, even if a
        # boundary-point containment test happened to miss it.
        if cur_lid is not None and not any(lid == cur_lid for lid, _ in options):
            options.insert(0, (cur_lid, directive.layer or "?"))

        self._ef_layer_combo = None
        if len(options) > 1:
            combo = QComboBox()
            for lid, phys in options:
                combo.addItem(phys, lid)
            sel_idx = next(
                (i for i, (lid, _) in enumerate(options) if lid == cur_lid),
                0,
            )
            combo.setCurrentIndex(sel_idx)
            combo.setToolTip(
                "Move this marker to another copper layer that carries the "
                "same net at this X / Y (e.g. a stacked plane or pour).")
            combo.currentIndexChanged.connect(
                self._on_free_marker_layer_changed)
            self._ef_layer_combo = combo
            form.addRow("Layer", combo)
        else:
            layer_lbl = QLabel(_esc(directive.layer or "?"))
            layer_lbl.setToolTip(
                "This net only exists on one copper layer at this location.")
            layer_lbl.setStyleSheet(f"color: {t['fg_muted']};")
            form.addRow("Layer", layer_lbl)

        self._ef_loc_x = QLineEdit(f"{float(ax):.4f}")
        self._ef_loc_x.setValidator(
            _numeric_validator(self, bottom=-1e9, top=1e9))
        self._ef_loc_x.setToolTip("X position (mm) — must stay on copper")
        self._ef_loc_x.editingFinished.connect(
            self._on_free_marker_coord_edited)
        form.addRow("X (mm)", self._ef_loc_x)
        self._ef_loc_y = QLineEdit(f"{float(ay):.4f}")
        self._ef_loc_y.setValidator(
            _numeric_validator(self, bottom=-1e9, top=1e9))
        self._ef_loc_y.setToolTip("Y position (mm) — must stay on copper")
        self._ef_loc_y.editingFinished.connect(
            self._on_free_marker_coord_edited)
        form.addRow("Y (mm)", self._ef_loc_y)
        lay.addLayout(form)

        hint = QLabel(
            "Drag the marker, or edit X / Y — the location must stay on "
            "copper of its layer, or it reverts.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(hint)

        moves = QHBoxLayout()
        self._ef_undo_move = QPushButton("↶ Undo")
        self._ef_undo_move.setToolTip(
            "Undo the last free-marker edit (move or delete)")
        self._ef_undo_move.clicked.connect(self._undo_marker_action)
        self._ef_redo_move = QPushButton("↷ Redo")
        self._ef_redo_move.setToolTip(
            "Redo the last undone free-marker edit (move or delete)")
        self._ef_redo_move.clicked.connect(self._redo_marker_action)
        moves.addWidget(self._ef_undo_move)
        moves.addWidget(self._ef_redo_move)
        lay.addLayout(moves)
        self._update_marker_undo_buttons()

    def _populate_copper_name_form(self, sel: dict) -> None:
        """Populate the right-hand form with a single-field form for
        naming a copper piece that has no Altium net name. The click
        anchor + layer pin the rename to one specific polygon — other
        disjoint unnamed copper on the board is unaffected. When the
        same polygon already carries a :class:`CopperName`, the form
        title flips to "Named copper" and the field is pre-filled with
        the saved name so the user can review / edit / re-apply."""
        t = _T()
        lay = self._editor_form_layout
        ax, ay = sel.get("anchor_xy") or (0.0, 0.0)
        layer_id = sel.get("layer_id")
        phys = None
        if layer_id is not None:
            id_to_phys = {v: k
                          for k, v in self._phys_name_to_layer_id.items()}
            phys = id_to_phys.get(int(layer_id))

        # Polygon-based lookup (NOT a strict anchor match) so a re-click
        # at any point on the named piece restores the saved name.
        existing = self._copper_name_at(
            float(ax), float(ay), int(layer_id))

        if existing is not None:
            lay.addWidget(QLabel("<b>Named copper</b>"))
            hint = QLabel(
                "This copper is named <b>" + _esc(existing.name)
                + "</b> — edit the name below and Apply to rename it, or "
                "leave it as-is. Other disjoint unnamed copper is "
                "unaffected."
            )
        else:
            lay.addWidget(QLabel("<b>Unnamed copper</b>"))
            hint = QLabel(
                "This copper has no net name in Altium. Give it a name "
                "here to make it solvable — only this piece is named, "
                "other disjoint unnamed copper stays as <b>(none)</b>."
            )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(hint)

        info = QFormLayout()
        info.setContentsMargins(0, 0, 0, 0)
        layer_lbl = QLabel(_esc(phys or "?"))
        layer_lbl.setStyleSheet(f"color: {t['fg_muted']};")
        info.addRow("Layer", layer_lbl)
        loc_lbl = QLabel(f"({float(ax):.4f}, {float(ay):.4f}) mm")
        loc_lbl.setStyleSheet(f"color: {t['fg_muted']};")
        info.addRow("At", loc_lbl)
        lay.addLayout(info)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._ef_copper_name = QLineEdit()
        self._ef_copper_name.setPlaceholderText("e.g. STAR_GND_PAD")
        if existing is not None:
            self._ef_copper_name.setText(existing.name)
        self._ef_copper_name.returnPressed.connect(self._on_apply_copper_name)
        form.addRow("Net name", self._ef_copper_name)
        lay.addLayout(form)

        btns = QHBoxLayout()
        self._ef_copper_name_apply = QPushButton("Apply")
        self._ef_copper_name_apply.clicked.connect(self._on_apply_copper_name)
        btns.addWidget(self._ef_copper_name_apply)
        lay.addLayout(btns)

        self._ef_copper_name_status = QLabel("")
        self._ef_copper_name_status.setWordWrap(True)
        self._ef_copper_name_status.setStyleSheet(
            f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(self._ef_copper_name_status)

    def _on_apply_copper_name(self) -> None:
        """Validate the entered name, save it as a :class:`CopperName`
        rename, and retroactively re-derive ``p_net`` for any free
        markers already placed on the same polygon (their ``"(none)"``
        becomes the new name so the resolve guard accepts them)."""
        sel = self._editor_selection
        if (not sel or sel.get("kind") != "copper"
                or sel.get("anchor_xy") is None
                or sel.get("layer_id") is None):
            return
        edit = getattr(self, "_ef_copper_name", None)
        status = getattr(self, "_ef_copper_name_status", None)
        if edit is None:
            return
        raw = edit.text().strip()
        t = _T()

        def _err(msg: str) -> None:
            if status is not None:
                status.setText(f"<span style='color:{t['warn']};'>"
                               f"{_esc(msg)}</span>")

        if not raw:
            _err("Enter a net name.")
            return
        if raw == "(none)" or raw.lower() in {"none", "(none)"}:
            _err("'(none)' is the unnamed-copper sentinel — pick a real name.")
            return
        # Disallow reusing an existing named net — that would let the user
        # silently merge this unnamed copper onto another rail without the
        # connectivity to support it (and confuse the FEM matrix).
        existing_nets = {n for n in self._all_net_names() if n != "(none)"}
        anchor = (float(sel["anchor_xy"][0]), float(sel["anchor_xy"][1]))
        layer_id = int(sel["layer_id"])
        # Find an existing rename via polygon containment so a re-click at
        # a different point on the same piece updates the same record
        # instead of creating a duplicate CopperName.
        prev = self._copper_name_at(anchor[0], anchor[1], layer_id)
        if raw in existing_nets and (prev is None or prev.name != raw):
            _err(f"'{raw}' is already a net on this board — pick a "
                 "different name.")
            return

        from fypa.project_file import CopperName
        proj = self._ensure_project()
        if prev is not None:
            prev.name = raw
        else:
            proj.upsert_copper_name(CopperName(
                anchor_xy=anchor, layer_id=layer_id, name=raw,
            ))
        # Net-name cache is stale now — the new name needs to appear in
        # dropdowns and connectivity probes immediately.
        self._all_net_names_cache = None
        # Retroactively update any free marker already placed on the same
        # piece of unnamed copper so the resolve guard accepts it.
        updated = 0
        for d in proj.editor_directives:
            if (d.kind == "free" and d.p_net == "(none)"
                    and d.anchor_xy is not None):
                pick = self._editor_copper_pick(
                    float(d.anchor_xy[0]), float(d.anchor_xy[1]))
                if pick and pick.get("net") == raw:
                    d.p_net = raw
                    updated += 1
        self._mark_project_dirty()
        # Promote the selection to the now-named net so the connectivity
        # highlight tracks it. Keep the form widgets in place (don't call
        # _populate_editor_form) — that preserves the user's text in the
        # field and keeps the success status visible until the next
        # interaction. The metadata still holds the renamed pieces as
        # ``"(none)"`` until the next solve, so we keep the polygon-based
        # highlight set to light them up.
        sel["net"] = raw
        highlight_polys: set[tuple[int, int]] = set()
        seed_poly = self._copper_poly_under_point(anchor[0], anchor[1], layer_id)
        if seed_poly is not None:
            highlight_polys = self._connected_copper_polys(layer_id, seed_poly)
        self._apply_editor_highlight(
            self._connected_nets(raw), polys=highlight_polys)
        msg = f"Named — this copper is now <b>{_esc(raw)}</b>."
        if updated:
            msg += (f" Updated {updated} existing marker"
                    f"{'s' if updated != 1 else ''}.")
        if status is not None:
            status.setText(f"<span style='color:{t['ok']};'>{msg}</span>")
        # Naming the first piece of copper enables the SOURCE / SINK
        # free-marker buttons — without this refresh they'd stay greyed
        # out (and tooltipped as "no named copper") until the next event
        # nudged a marker-button sync.
        self._sync_marker_buttons()
        # The newly-named copper changes the net inventory — refresh the
        # table so the new name appears (or an old "(none)" row shrinks).
        self._refresh_net_table()

    def _populate_editor_form(self) -> None:
        """(Re)build the form for the current selection: the PDN-role
        form for a component / free-marker, the copper-naming form for
        unnamed copper (``net == "(none)"``), or nothing for a plain
        named-copper selection."""
        if not hasattr(self, "_editor_form_layout"):
            return
        self._clear_layout(self._editor_form_layout)
        if self._editor_multi:
            self._populate_multi_editor_form()
            return
        self._clear_layout(self._multi_form_layout)
        sel = self._editor_selection
        if self._is_copper_name_selection(sel):
            self._populate_copper_name_form(sel)
            self._update_editor_panel()
            return
        if not sel or sel.get("kind") not in ("component", "free"):
            self._update_editor_panel()
            return
        t = _T()
        lay = self._editor_form_layout
        existing = self._directive_for_selection()

        if sel["kind"] == "component":
            title = f"Component <b>{_esc(sel.get('designator') or '?')}</b>"
        else:
            title = "<b>Free marker</b>"
        lay.addWidget(QLabel(title))

        # Free markers are moveable — a fixed-layer label, editable X / Y
        # boxes, and dedicated move undo / redo. Attributes are reset here
        # so the rest of the form (and the drag path) never touches a
        # QLineEdit left dangling by the _clear_layout above.
        self._ef_loc_x = None
        self._ef_loc_y = None
        self._ef_undo_move = None
        self._ef_redo_move = None
        self._ef_layer_combo = None
        if sel["kind"] == "free" and existing is not None:
            self._build_free_marker_location(lay, existing)

        # A component carrying PDN_* directives in the Altium schematic is
        # shown read-only with an Unlock button — until the user unlocks it
        # (or an editor override already exists), no editable form appears.
        sch_unlock: dict | None = None
        if sel["kind"] == "component":
            sch = self._schematic_directives_for(sel)
            if sch and existing is None and not sel.get("unlocked"):
                self._build_schematic_info(lay, sch)
                self._update_editor_panel()
                return
            sch_unlock = self._unlockable_schematic_directive(sel)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._ef_role = QComboBox()
        # SERIES is component-bound only — it bridges two real pads, so a
        # single-anchor free marker can't express it. Offer SERIES for a
        # component selection (or when editing a directive that is already
        # SERIES), never for a fresh free marker.
        role_items = ["SOURCE", "SINK"]
        if sel["kind"] == "component" or (
                existing is not None and existing.role == "SERIES"):
            role_items.append("SERIES")
        self._ef_role.addItems(role_items)
        self._ef_role.currentTextChanged.connect(self._on_editor_role_changed)
        form.addRow("PDN role", self._ef_role)
        self._ef_value = QLineEdit()
        self._ef_value.setValidator(_numeric_validator(self, top=1e12))
        self._ef_value_label = QLabel("Voltage (V)")
        form.addRow(self._ef_value_label, self._ef_value)
        # SINK-only optional minimum acceptable rail voltage (PDN_MIN_V
        # equivalent). Blank disables the per-pin pass/fail check. Visibility
        # is toggled by _on_editor_role_changed below.
        self._ef_min_v = QLineEdit()
        self._ef_min_v.setValidator(_numeric_validator(self, top=1e12))
        self._ef_min_v.setToolTip(
            "Optional: minimum acceptable rail voltage at this sink's pins. "
            "Leave blank to skip the check. Sinks below this voltage are "
            "flagged red in the Nodes table."
        )
        self._ef_min_v_label = QLabel("Min V (V)")
        form.addRow(self._ef_min_v_label, self._ef_min_v)
        lay.addLayout(form)

        lay.addWidget(QLabel("Current model"))
        self._ef_single = QRadioButton("Single net (point-to-point)")
        self._ef_two = QRadioButton("Two nets (full current-path loop)")
        self._ef_btngroup = QButtonGroup(self._editor_form_host)
        self._ef_btngroup.addButton(self._ef_single)
        self._ef_btngroup.addButton(self._ef_two)
        self._ef_single.setChecked(True)
        self._ef_single.toggled.connect(self._on_editor_model_changed)
        lay.addWidget(self._ef_single)
        lay.addWidget(self._ef_two)

        form2 = QFormLayout()
        form2.setContentsMargins(0, 0, 0, 0)
        self._ef_pnet = QComboBox()
        self._ef_pnet.addItems(self._form_p_nets(sel))
        self._ef_pnet_label = QLabel("Net")
        form2.addRow(self._ef_pnet_label, self._ef_pnet)
        self._ef_nnet = QComboBox()
        self._ef_nnet.addItems(self._all_net_names())
        self._ef_nnet_label = QLabel("N net")
        form2.addRow(self._ef_nnet_label, self._ef_nnet)
        # Optional pin restriction (PDN_PINS equivalent) — comma-separated pad
        # designators the terminal couples to. Only meaningful for a
        # component-bound directive: a free marker is a single anchor point, so
        # the fields are hidden for it. Blank ⇒ every pad of the component on
        # the net (the placement default).
        self._ef_pins = QLineEdit()
        self._ef_pins.setPlaceholderText("all pins on net")
        self._ef_pins.setToolTip(
            "Optional: comma-separated pad numbers this terminal couples to "
            "(the PDN_PINS equivalent, e.g. \"1\" or \"1, 3\"). Leave blank "
            "to use every pad of the component that sits on the net."
        )
        self._ef_pins_label = QLabel("Pins")
        form2.addRow(self._ef_pins_label, self._ef_pins)
        self._ef_npins = QLineEdit()
        self._ef_npins.setPlaceholderText("all pins on N net")
        self._ef_npins.setToolTip(
            "Optional: comma-separated pad numbers the N terminal couples to. "
            "Leave blank to use every pad of the component on the N net."
        )
        self._ef_npins_label = QLabel("N pins")
        form2.addRow(self._ef_npins_label, self._ef_npins)
        # Multi-connector designator lists (PDN_P_DES / PDN_N_DES) — CSV of
        # other component designators whose pads feed this terminal. Two-net
        # SOURCE/SINK only; host is not auto-included when set.
        self._ef_pdes = QLineEdit()
        self._ef_pdes.setPlaceholderText("host only")
        self._ef_pdes.setToolTip(
            "Optional: comma-separated designators for the P terminal "
            "(PDN_P_DES). Pads come only from those parts — the host is "
            "not auto-included. Leave blank to use the host component."
        )
        self._ef_pdes_label = QLabel("P DES")
        form2.addRow(self._ef_pdes_label, self._ef_pdes)
        self._ef_ndes = QLineEdit()
        self._ef_ndes.setPlaceholderText("host only")
        self._ef_ndes.setToolTip(
            "Optional: comma-separated designators for the N terminal "
            "(PDN_N_DES). Pads come only from those parts — the host is "
            "not auto-included. Leave blank to use the host component."
        )
        self._ef_ndes_label = QLabel("N DES")
        form2.addRow(self._ef_ndes_label, self._ef_ndes)
        # Pins / DES apply to a real component's pads only.
        self._ef_pins_apply = sel["kind"] == "component"
        for _w in (self._ef_pins, self._ef_pins_label,
                   self._ef_npins, self._ef_npins_label,
                   self._ef_pdes, self._ef_pdes_label,
                   self._ef_ndes, self._ef_ndes_label):
            _w.setVisible(self._ef_pins_apply)
        lay.addLayout(form2)

        btns = QHBoxLayout()
        self._ef_apply = QPushButton("Apply")
        self._ef_apply.clicked.connect(self._on_editor_apply)
        self._ef_remove = QPushButton("Remove")
        self._ef_remove.clicked.connect(self._on_editor_remove)
        btns.addWidget(self._ef_apply)
        btns.addWidget(self._ef_remove)
        lay.addLayout(btns)

        self._ef_status = QLabel("")
        self._ef_status.setWordWrap(True)
        self._ef_status.setStyleSheet(
            f"color: {t['fg_muted']}; font-size: 8pt;")
        lay.addWidget(self._ef_status)

        if existing is not None:
            role = (existing.role
                    if existing.role in ("SOURCE", "SINK", "SERIES")
                    else "SINK")
            self._ef_role.setCurrentText(role)
            val = {"SINK": existing.current,
                   "SERIES": existing.resistance}.get(role, existing.voltage)
            self._ef_value.setText("" if val is None else f"{val:g}")
            mv = getattr(existing, "min_voltage", None)
            self._ef_min_v.setText("" if mv is None else f"{mv:g}")
            self._ef_single.setChecked(existing.single_net)
            self._ef_two.setChecked(not existing.single_net)
            if existing.p_net:
                self._set_combo(self._ef_pnet, existing.p_net)
            if existing.n_net:
                self._set_combo(self._ef_nnet, existing.n_net)
            self._ef_pins.setText(", ".join(existing.p_pins or []))
            self._ef_npins.setText(", ".join(existing.n_pins or []))
            self._ef_pdes.setText(", ".join(
                getattr(existing, "p_des", None) or []))
            self._ef_ndes.setText(", ".join(
                getattr(existing, "n_des", None) or []))
            self._ef_remove.setEnabled(True)
            if existing.overrides_designator:
                self._ef_status.setText(
                    f"<span style='color:{t['warn']};'>Overrides the "
                    "schematic directive.</span>"
                )
        elif sch_unlock is not None:
            # Just unlocked, no editor override yet — seed the form from the
            # schematic directive so the user adjusts its actual values. A
            # schematic SERIES carries the RESISTOR role name in metadata.
            raw_role = sch_unlock.get("role")
            role = {"RESISTOR": "SERIES"}.get(raw_role, raw_role)
            if role not in ("SOURCE", "SINK", "SERIES"):
                role = "SINK"
            self._ef_role.setCurrentText(role)
            val = sch_unlock.get("value")
            self._ef_value.setText("" if val is None else f"{val:g}")
            mv = sch_unlock.get("min_voltage")
            self._ef_min_v.setText("" if mv is None else f"{mv:g}")
            terms = sch_unlock.get("terminals") or {}
            n_term = terms.get("N")
            single = (n_term is None) or bool(n_term.get("ideal_return"))
            self._ef_single.setChecked(single)
            self._ef_two.setChecked(not single)
            p_net = self._terminal_primary_net(terms.get("P"))
            if p_net:
                self._set_combo(self._ef_pnet, p_net)
            n_net = self._terminal_primary_net(n_term)
            if n_net:
                self._set_combo(self._ef_nnet, n_net)
            # Seed the pin restriction from the schematic directive's pads so
            # an Altium PDN_PINS source (e.g. J23 pin 1 only) stays on those
            # pins through the override — instead of spreading to every pad on
            # the net once it resolves as an editor directive.
            self._ef_pins.setText(
                ", ".join(self._terminal_pin_pads(terms.get("P"))))
            self._ef_npins.setText(
                ", ".join(self._terminal_pin_pads(n_term)))
            # Seed P/N DES from the components that actually contributed pins
            # (multi-connector PDN_*_DES). Host-only → leave blank.
            host_des = sel.get("designator")
            self._ef_pdes.setText(
                ", ".join(self._terminal_des_list(terms.get("P"), host_des)))
            self._ef_ndes.setText(
                ", ".join(self._terminal_des_list(n_term, host_des)))
            self._ef_remove.setEnabled(False)
            self._ef_status.setText(
                f"<span style='color:{t['warn']};'>Unlocked — Apply "
                "replaces the schematic directive on the next resolve.</span>"
            )
        else:
            self._ef_remove.setEnabled(False)
        self._on_editor_role_changed(self._ef_role.currentText())
        self._on_editor_model_changed()
        self._update_editor_panel()

    def _on_editor_role_changed(self, role: str) -> None:
        """Swap the value field between voltage (SOURCE), current (SINK)
        and resistance (SERIES). SERIES always bridges two nets, so its
        current model is forced to two-net and the radios are locked.
        The optional Min V (PDN_MIN_V) field is only shown for SINK."""
        if not hasattr(self, "_ef_value_label"):
            return
        self._ef_value_label.setText(
            {"SINK": "Current (A)", "SERIES": "Resistance (Ω)"}.get(
                role, "Voltage (V)")
        )
        if hasattr(self, "_ef_min_v"):
            is_sink = role == "SINK"
            self._ef_min_v.setVisible(is_sink)
            self._ef_min_v_label.setVisible(is_sink)
            if not is_sink:
                self._ef_min_v.clear()
        if hasattr(self, "_ef_two"):
            is_series = role == "SERIES"
            if is_series and not self._ef_two.isChecked():
                self._ef_two.setChecked(True)
            self._ef_single.setEnabled(not is_series)
            self._ef_two.setEnabled(not is_series)
            self._on_editor_model_changed()

    def _on_editor_model_changed(self, *_args) -> None:
        """Two-net shows both the 'P net' and 'N net' pickers; single-net
        hides the N-net row entirely and relabels the P picker just 'Net'."""
        if not hasattr(self, "_ef_two"):
            return
        two = self._ef_two.isChecked()
        self._ef_nnet.setVisible(two)
        self._ef_nnet.setEnabled(two)
        self._ef_nnet_label.setVisible(two)
        self._ef_pnet_label.setText("P net" if two else "Net")
        # The N-pin restriction only exists in two-net mode (single-net's N
        # terminal is an ideal return with no pads). Keep it in step with the
        # N-net picker, and only for a component selection. P/N DES likewise
        # apply only to two-net SOURCE/SINK (SERIES ignores them).
        if hasattr(self, "_ef_npins"):
            show_npins = two and getattr(self, "_ef_pins_apply", False)
            self._ef_npins.setVisible(show_npins)
            self._ef_npins_label.setVisible(show_npins)
            self._ef_pins_label.setText("P pins" if two else "Pins")
        if hasattr(self, "_ef_pdes"):
            role = (self._ef_role.currentText()
                    if hasattr(self, "_ef_role") else "")
            show_des = (two and getattr(self, "_ef_pins_apply", False)
                        and role in ("SOURCE", "SINK"))
            self._ef_pdes.setVisible(show_des)
            self._ef_pdes_label.setVisible(show_des)
            self._ef_ndes.setVisible(show_des)
            self._ef_ndes_label.setVisible(show_des)

    def _on_editor_apply(self) -> None:
        """Commit the form into an :class:`EditorDirective` on the project,
        flag the project dirty + the solve stale, and refresh markers."""
        from fypa.project_file import EditorDirective
        sel = self._editor_selection
        if not sel or sel.get("kind") not in ("component", "free"):
            return
        role = self._ef_role.currentText()
        txt = self._ef_value.text().strip()
        quantity = {"SINK": "current", "SERIES": "resistance"}.get(
            role, "voltage")
        try:
            value = float(txt)
        except ValueError:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>Enter a numeric "
                f"{quantity} value.</span>"
            )
            return
        if role == "SERIES" and value <= 0:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>SERIES resistance "
                "must be greater than zero.</span>"
            )
            return
        # SERIES always bridges two real nets — never single-net.
        single = self._ef_single.isChecked() and role != "SERIES"
        p_net = self._ef_pnet.currentText() or None
        n_net = self._ef_nnet.currentText() if not single else None
        if not p_net:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>"
                "Pick a P net.</span>"
            )
            return
        if role == "SERIES" and not n_net:
            self._ef_status.setText(
                f"<span style='color:{_T()['err']};'>Pick an N net — a "
                "SERIES element bridges two nets.</span>"
            )
            return

        # SINK-only optional minimum-rail-voltage check. Blank disables it;
        # any non-numeric text is rejected so a stray keystroke can't silently
        # drop the check.
        min_v: float | None = None
        if role == "SINK":
            mv_txt = self._ef_min_v.text().strip()
            if mv_txt:
                try:
                    min_v = float(mv_txt)
                except ValueError:
                    self._ef_status.setText(
                        f"<span style='color:{_T()['err']};'>Min V must be "
                        "numeric (or blank to skip the check).</span>"
                    )
                    return

        existing = self._directive_for_selection()
        d = existing if existing is not None else EditorDirective()
        d.role = role
        d.single_net = single
        d.p_net = p_net
        d.n_net = n_net
        d.voltage = value if role in ("SOURCE", "REGULATOR") else None
        d.current = value if role == "SINK" else None
        d.resistance = value if role == "SERIES" else None
        d.min_voltage = min_v
        if sel["kind"] == "component":
            d.kind = "component"
            d.designator = sel.get("designator")
            # Pin restriction (PDN_PINS equivalent). Blank ⇒ None ⇒ every pad
            # on the net. The N field only applies in two-net mode.
            d.p_pins = self._parse_pin_field(self._ef_pins.text())
            d.n_pins = (None if single
                        else self._parse_pin_field(self._ef_npins.text()))
            # Multi-connector DES lists (PDN_*_DES). Two-net SOURCE/SINK only.
            if single or role not in ("SOURCE", "SINK"):
                d.p_des = None
                d.n_des = None
            else:
                d.p_des = self._parse_pin_field(self._ef_pdes.text())
                d.n_des = self._parse_pin_field(self._ef_ndes.text())
            # If this component has a schematic directive, mark the editor
            # directive as its override so the re-solve drops the schematic
            # one instead of stamping both.
            sch_unlock = self._unlockable_schematic_directive(sel)
            d.overrides_designator = (
                sch_unlock.get("designator") if sch_unlock else None
            )
        else:
            d.kind = "free"
            d.p_pins = None
            d.n_pins = None
            d.p_des = None
            d.n_des = None
            self._editor_selection = {"kind": "free", "id": d.id}

        self._ensure_project().upsert_directive(d)
        self._mark_project_dirty()
        self._apply_editor_highlight(self._connected_nets(p_net))
        self._update_pending_rails()
        self._render()
        self._ef_status.setText(
            f"<span style='color:{_T()['ok']};'>Applied — press Resolve "
            "to re-solve.</span>"
        )

    def _on_editor_remove(self) -> None:
        """Delete the directive bound to the current selection. Undoable
        via Ctrl+Z (or the panel's Undo button); the prior selection is
        captured so undo also restores it (a component-bound directive
        re-selects its component, a free marker re-selects itself)."""
        existing = self._directive_for_selection()
        if existing is None or self._project is None:
            return
        try:
            idx = self._project.editor_directives.index(existing)
        except ValueError:
            return
        prev_selection = (dict(self._editor_selection)
                          if self._editor_selection else None)
        self._project.remove_directive(existing.id)
        self._marker_undo.append({
            "op": "delete",
            "id": existing.id,
            "directive": existing,
            "index": idx,
            "prev_selection": prev_selection,
        })
        self._marker_redo.clear()
        self._editor_selection = None
        self._mark_project_dirty()
        self._update_pending_rails()
        self._update_marker_undo_buttons()
        # Empty highlight + render so the marker disappears from the
        # viewport and connected copper returns to full opacity.
        self._apply_editor_highlight(set())
        self._populate_editor_form()

    def _place_free_marker(self, world_x: float, world_y: float) -> None:
        """Drop a free source / sink / series marker on the copper under the
        cursor and open its form. No-op (with a hint) if the click missed
        copper, or if the copper under the cursor is on a hidden layer —
        the user shouldn't be able to drop a marker on copper they can't
        see. A SERIES marker is created two-net with its N net still unset
        — the form prompts for it before the directive can resolve."""
        from fypa.project_file import EditorDirective
        role = self._editor_pending_marker
        if role is None:
            return
        pick = self._visible_editor_copper_pick(world_x, world_y)
        net = pick.get("net") if pick else None
        if pick is None or not net:
            hidden = self._editor_copper_pick(world_x, world_y)
            if hidden is not None and hidden.get("net"):
                phys = hidden.get("physical") or "a hidden layer"
                self.statusBar().showMessage(
                    f"Copper on {phys} is hidden — turn on its eye to "
                    "place a marker there.", 4000)
            else:
                self.statusBar().showMessage(
                    "No copper there — click on a copper region.", 4000)
            return
        if net == "(none)":
            # Free markers anchor to a net; unnamed copper has none. The
            # downstream resolve has no rail to attach the marker to, so
            # block the placement and prompt the user to name the copper
            # first (click the polygon to surface the copper-name form).
            self.statusBar().showMessage(
                "This copper has no net name — name it first, then drop "
                "the free marker on it.", 5000)
            return
        layer = pick.get("physical")
        layer_id = pick.get("layer_id")
        d = EditorDirective(
            kind="free", role=role, anchor_xy=(world_x, world_y),
            layer=layer, layer_id=layer_id,
            single_net=(role != "SERIES"), p_net=net,
            voltage=(3.3 if role == "SOURCE" else None),
            current=(1.0 if role == "SINK" else None),
            resistance=(0.01 if role == "SERIES" else None),
        )
        self._ensure_project().upsert_directive(d)
        self._editor_pending_marker = None
        self._editor_selection = {"kind": "free", "id": d.id}
        self._gl_viewer.set_primitive_selection_outline(None)
        self._mark_project_dirty()
        self._apply_editor_highlight(self._connected_nets(net))
        self._update_pending_rails()
        self._populate_editor_form()
        self._render()

    # --- Editor mode: free-marker move (drag + X/Y edit + undo) -------------

    def _refresh_editor_markers(self) -> None:
        """Marker-only viewport refresh — re-push the cached non-editor
        marker groups plus freshly rebuilt editor-directive markers,
        without rebuilding the FEM mesh. Used on every tick of a
        free-marker drag so the move stays smooth."""
        cached = self._non_editor_marker_groups
        if cached is None:
            self._render()
            return
        self._gl_viewer.set_markers(
            list(cached) + self._editor_marker_groups())
        # Keep the hover index in step with the moved marker — recombine
        # the cached solved-directive rows with freshly rebuilt editor rows.
        self._set_marker_hover_rows(
            self._metadata_marker_hover_rows
            + self._editor_marker_hover_rows())

    def _free_marker_layer_id(self, directive) -> int | None:
        """The Altium copper layer id a free marker is pinned to — its
        stored ``layer_id``, else resolved from its physical layer name."""
        if directive.layer_id is not None:
            return directive.layer_id
        if directive.layer:
            return self._phys_name_to_layer_id.get(directive.layer)
        return None

    def _copper_on_layer_at(self, world_x: float, world_y: float,
                            layer_id: int | None,
                            net_name: str | None = None) -> dict | None:
        """Netted copper of the given layer under the point, or ``None``.
        Unlike :meth:`_copper_at_point` this never crosses to another
        layer — a free marker's layer is fixed, so its move must stay on
        it. Net-less copper is skipped so a moved marker always re-derives
        a real net, exactly as placement requires.

        With ``net_name`` set the test additionally requires the copper to
        carry that exact net — used by the marker-drag constraint so a
        free marker can only ride copper of its own net. Two disjoint
        polygons of the same net on this layer both qualify, so the
        marker can "jump" across the gap between them while the cursor
        passes over them in turn."""
        md = self.metadata
        if not md or layer_id is None:
            return None
        pt = _sg.Point(world_x, world_y)
        for rec in md.get("all_copper") or []:
            if rec.get("layer_id") != layer_id or not rec.get("net"):
                continue
            if net_name is not None and rec.get("net") != net_name:
                continue
            for poly in rec.get("polygons", []):
                prepped = self._copper_poly_prepared(poly)
                if prepped is None:
                    continue
                try:
                    if prepped.contains(pt):
                        return {"net": rec.get("net"), "layer_id": layer_id}
                except Exception:
                    continue
        return None

    def _layers_with_net_at(self, world_x: float, world_y: float,
                            net_name: str | None
                            ) -> list[tuple[int, str]]:
        """Every copper layer whose ``net_name`` copper covers ``(world_x,
        world_y)``, as ``(layer_id, physical_name)`` pairs sorted top-to-
        bottom by stackup rank. Drives the free-marker layer picker: a marker
        sitting where a rail's copper is stacked on several layers can be
        moved between them. Empty list when ``net_name`` is unset or the point
        lies on that net's copper on only zero/one layer."""
        md = self.metadata
        if not md or not net_name:
            return []
        pt = _sg.Point(world_x, world_y)
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        out: list[tuple[int, str]] = []
        seen: set[int] = set()
        for rec in md.get("all_copper") or []:
            lid = rec.get("layer_id")
            if lid in seen or rec.get("net") != net_name:
                continue
            phys = id_to_phys.get(lid)
            if phys is None:
                continue
            for poly in rec.get("polygons", []):
                prepped = self._copper_poly_prepared(poly)
                if prepped is None:
                    continue
                try:
                    if prepped.contains(pt):
                        out.append((lid, phys))
                        seen.add(lid)
                        break
                except Exception:
                    continue
        out.sort(key=lambda lp: self._phys_stackup_rank.get(lp[1], 1 << 30))
        return out

    def _point_on_marker_layer(self, directive,
                               world_x: float, world_y: float) -> bool:
        """Whether ``(world_x, world_y)`` lands on copper of the free
        marker's fixed layer AND its current net. A free marker can only
        be dragged across copper carrying the same net name as its anchor
        — disjoint copper of that net on the marker's layer still
        qualifies, so the marker hops across the gap if the user drags
        across it, but copper of a different net (or bare substrate)
        rejects the move and the marker pins at its last valid spot.
        Falls back to 'any copper on this layer' if the marker has no
        recorded net (legacy data), and to 'any copper' only when the
        marker's layer can't be determined."""
        layer_id = self._free_marker_layer_id(directive)
        net = getattr(directive, "p_net", None) or None
        if layer_id is None:
            return self._copper_at_point(world_x, world_y) is not None
        return self._copper_on_layer_at(
            world_x, world_y, layer_id, net_name=net) is not None

    def _marker_drag_hit_test(self, world_x: float,
                              world_y: float) -> bool:
        """GL-viewer hook — True when a draggable free marker sits under
        the point, so the viewer claims the press as a move gesture.
        Disabled while a marker-drop is armed (that press places a new
        marker instead)."""
        if not self._editor_mode or self._editor_pending_marker is not None:
            return False
        return self._free_marker_at(world_x, world_y) is not None

    def _on_marker_drag_started(self, world_x: float,
                                world_y: float) -> None:
        """Begin a free-marker drag — record the start anchor / net and
        select the grabbed marker (a press with no drag still selects)."""
        marker = self._free_marker_at(world_x, world_y)
        if marker is None or marker.anchor_xy is None:
            self._marker_drag = None
            return
        start = (float(marker.anchor_xy[0]), float(marker.anchor_xy[1]))
        self._marker_drag = {
            "id": marker.id,
            "start_xy": start,
            "start_p_net": marker.p_net,
            "last_valid": start,
        }
        if (self._editor_selection or {}).get("id") != marker.id:
            self._select_free_marker(marker)

    def _on_marker_drag_moved(self, world_x: float,
                              world_y: float) -> None:
        """Track a free-marker drag — move the marker to the cursor while
        it stays on copper of its layer, else hold it at the last valid
        spot so it can't be dragged off copper."""
        drag = self._marker_drag
        if drag is None or self._project is None:
            return
        d = self._project.directive_by_id(drag["id"])
        if d is None:
            return
        if self._point_on_marker_layer(d, world_x, world_y):
            drag["last_valid"] = (float(world_x), float(world_y))
        nx, ny = drag["last_valid"]
        d.anchor_xy = (nx, ny)
        # setText doesn't fire editingFinished, so the live readout costs
        # nothing and keeps the on-copper check off the drag path.
        self._sync_free_marker_coord_fields(nx, ny)
        self._refresh_editor_markers()
        if getattr(self, "_topology_view", None) is not None:
            tab_idx = getattr(self, "_topology_tab_index", -1)
            if self.tabs.currentIndex() == tab_idx:
                self._schedule_topology_refresh()

    def _on_marker_drag_released(self, world_x: float,
                                 world_y: float) -> None:
        """Finish a free-marker drag — commit the move (recording it for
        undo) when it actually moved, else just leave it selected."""
        drag = self._marker_drag
        self._marker_drag = None
        if drag is None or self._project is None:
            return
        d = self._project.directive_by_id(drag["id"])
        if d is None:
            return
        new_xy = drag["last_valid"]
        start_xy = drag["start_xy"]
        if (abs(new_xy[0] - start_xy[0]) < 1e-9
                and abs(new_xy[1] - start_xy[1]) < 1e-9):
            d.anchor_xy = start_xy
            self._populate_editor_form()
            return
        self._commit_marker_move(d, start_xy, drag["start_p_net"], new_xy)
        self._populate_editor_form()

    def _commit_marker_move(self, directive, old_xy, old_p_net,
                            new_xy) -> None:
        """Apply a committed free-marker move: update the anchor, re-derive
        the net from the copper under the new spot (a free marker's net
        follows its copper, mirroring placement), record it for undo, and
        refresh the viewport. Does NOT rebuild the panel form — the caller
        owns that, so it can defer it out of a text-box signal."""
        directive.anchor_xy = (float(new_xy[0]), float(new_xy[1]))
        pick = self._copper_on_layer_at(
            new_xy[0], new_xy[1], self._free_marker_layer_id(directive))
        if pick and pick.get("net"):
            directive.p_net = pick["net"]
        self._record_marker_move(directive.id, old_xy, new_xy,
                                 old_p_net, directive.p_net)
        self._mark_project_dirty()
        self._update_pending_rails()
        self._apply_editor_highlight(
            self._connected_nets(directive.p_net)
            if directive.p_net else set())

    def _record_marker_move(self, marker_id, old_xy, new_xy,
                            old_p_net, new_p_net) -> None:
        """Push a free-marker move onto the undo stack; a fresh edit
        invalidates the redo stack."""
        self._marker_undo.append({
            "op": "move",
            "id": marker_id,
            "old_xy": (float(old_xy[0]), float(old_xy[1])),
            "new_xy": (float(new_xy[0]), float(new_xy[1])),
            "old_p_net": old_p_net,
            "new_p_net": new_p_net,
        })
        self._marker_redo.clear()
        self._update_marker_undo_buttons()

    def _delete_selected_free_marker(self) -> None:
        """Delete the currently selected free marker — Delete / Backspace
        in editor mode. Routes to :meth:`_on_editor_remove` so the same
        undo timeline / record shape is shared with the panel's Remove
        button. Scoped to free markers (per the original feature
        request); no-op outside editor mode or mid-drag."""
        if not self._editor_mode or self._marker_drag is not None:
            return
        if self._editor_multi:
            self._on_editor_multi_remove()
            return
        sel = self._editor_selection
        if not sel or sel.get("kind") != "free":
            return
        self._on_editor_remove()

    def _undo_marker_action(self) -> None:
        """Undo the most recent free-marker edit (move or delete)."""
        if not self._editor_mode or not self._marker_undo:
            return
        rec = self._marker_undo[-1]
        op = rec.get("op", "move")
        if op == "batch":
            # One multi-sink Apply / Remove unwinds as a single step.
            self._marker_undo.pop()
            if self._project is None:
                self._update_marker_undo_buttons()
                return
            self._apply_batch_side(rec["items"], "before")
            self._marker_redo.append(rec)
            self._after_batch_undo_redo(rec.get("entries_before") or [])
            return
        if op == "move":
            d = (self._project.directive_by_id(rec["id"])
                 if self._project else None)
            self._marker_undo.pop()
            if d is None:   # marker was removed — drop the stale record
                self._update_marker_undo_buttons()
                return
            d.anchor_xy = rec["old_xy"]
            d.p_net = rec["old_p_net"]
            self._marker_redo.append(rec)
            self._after_marker_undo_redo(d)
            return
        if op == "delete":
            self._marker_undo.pop()
            if self._project is None:
                self._update_marker_undo_buttons()
                return
            d = rec["directive"]
            # Reinsert at the original position so z-order is preserved;
            # clamp if the list has shrunk in the meantime.
            idx = min(max(int(rec.get("index", 0)), 0),
                      len(self._project.editor_directives))
            self._project.editor_directives.insert(idx, d)
            self._marker_redo.append(rec)
            self._after_marker_undo_redo(d, rec.get("prev_selection"))

    def _redo_marker_action(self) -> None:
        """Redo the most recently undone free-marker edit."""
        if not self._editor_mode or not self._marker_redo:
            return
        rec = self._marker_redo[-1]
        op = rec.get("op", "move")
        if op == "batch":
            self._marker_redo.pop()
            if self._project is None:
                self._update_marker_undo_buttons()
                return
            self._apply_batch_side(rec["items"], "after")
            self._marker_undo.append(rec)
            self._after_batch_undo_redo(rec.get("entries_after") or [])
            return
        if op == "move":
            d = (self._project.directive_by_id(rec["id"])
                 if self._project else None)
            self._marker_redo.pop()
            if d is None:
                self._update_marker_undo_buttons()
                return
            d.anchor_xy = rec["new_xy"]
            d.p_net = rec["new_p_net"]
            self._marker_undo.append(rec)
            self._after_marker_undo_redo(d)
            return
        if op == "delete":
            self._marker_redo.pop()
            if self._project is None:
                self._update_marker_undo_buttons()
                return
            self._project.remove_directive(rec["id"])
            self._marker_undo.append(rec)
            self._editor_selection = None
            self._mark_project_dirty()
            self._update_pending_rails()
            self._update_marker_undo_buttons()
            self._apply_editor_highlight(set())
            self._populate_editor_form()

    def _after_marker_undo_redo(self, directive, selection=None) -> None:
        """Shared tail of undo / redo — reselect the directive and refresh
        the viewport + panel. ``selection`` lets a delete-undo restore the
        prior selection state (a component-bound directive re-selects the
        component, with its pin-net union driving the highlight). When
        ``None`` (the move case) the directive is selected as a free
        marker and the highlight comes from its own net."""
        if selection is None:
            self._editor_selection = {"kind": "free", "id": directive.id}
            highlight = (self._connected_nets(directive.p_net)
                         if directive.p_net else set())
        else:
            sel = dict(selection)
            self._editor_selection = sel
            if sel.get("kind") == "component":
                highlight = set()
                for n in sel.get("nets") or []:
                    highlight |= self._connected_nets(n)
            elif sel.get("kind") == "free":
                highlight = (self._connected_nets(directive.p_net)
                             if directive.p_net else set())
            else:
                highlight = set()
        self._mark_project_dirty()
        self._update_pending_rails()
        self._update_marker_undo_buttons()
        self._apply_editor_highlight(highlight)
        self._populate_editor_form()

    def _update_marker_undo_buttons(self) -> None:
        """Enable / disable the panel's marker undo / redo buttons to
        match the stacks (no-op when the form isn't showing them)."""
        btn = getattr(self, "_ef_undo_move", None)
        if btn is not None:
            try:
                btn.setEnabled(bool(self._marker_undo))
            except RuntimeError:
                pass
        btn = getattr(self, "_ef_redo_move", None)
        if btn is not None:
            try:
                btn.setEnabled(bool(self._marker_redo))
            except RuntimeError:
                pass

    def _sync_free_marker_coord_fields(self, x: float, y: float) -> None:
        """Push ``(x, y)`` into the panel's X / Y text boxes without
        firing the on-copper check (``setText`` raises no editingFinished)."""
        for attr, val in (("_ef_loc_x", x), ("_ef_loc_y", y)):
            box = getattr(self, attr, None)
            if box is None:
                continue
            try:
                box.setText(f"{val:.4f}")
            except RuntimeError:
                pass   # widget rebuilt out from under us — harmless

    def _on_free_marker_coord_edited(self) -> None:
        """Check an X / Y text-box edit once focus leaves the box: the new
        location must sit on copper of the marker's fixed layer. If not,
        warn the user and revert to the previous location."""
        if self._suppress_coord_check:
            return
        sel = self._editor_selection
        if not sel or sel.get("kind") != "free":
            return
        d = self._directive_for_selection()
        if d is None or d.anchor_xy is None:
            return
        old_xy = (float(d.anchor_xy[0]), float(d.anchor_xy[1]))
        try:
            nx = _parse_numeric_text(self._ef_loc_x.text())
            ny = _parse_numeric_text(self._ef_loc_y.text())
        except (ValueError, RuntimeError):
            self._sync_free_marker_coord_fields(*old_xy)
            return
        if abs(nx - old_xy[0]) < 1e-7 and abs(ny - old_xy[1]) < 1e-7:
            return   # no real change
        if not self._point_on_marker_layer(d, nx, ny):
            net_label = d.p_net or "?"
            self._suppress_coord_check = True
            try:
                QMessageBox.warning(
                    self, "Location not on same-net copper",
                    f"({nx:g}, {ny:g}) mm is not on copper of net "
                    f"{net_label!s} on layer {d.layer or '?'}.\n\n"
                    "A free marker can only be moved to copper of the "
                    "same net (disjoint copper of that net is fine).\n\n"
                    "The free marker location is being reverted back to "
                    f"({old_xy[0]:g}, {old_xy[1]:g}) mm.",
                )
            finally:
                self._suppress_coord_check = False
            self._sync_free_marker_coord_fields(*old_xy)
            return
        self._commit_marker_move(d, old_xy, d.p_net, (nx, ny))
        # Targeted refresh only — rebuilding the whole form here would
        # delete the QLineEdit mid-signal and steal focus while the user
        # is still tabbing between the X / Y boxes.
        self._sync_free_marker_coord_fields(
            float(d.anchor_xy[0]), float(d.anchor_xy[1]))
        self._refresh_free_marker_net_combo()
        self._refresh_free_marker_layer_combo()

    def _refresh_free_marker_net_combo(self) -> None:
        """Resync the form's net picker to the selected free marker's
        current ``p_net`` — used after a move re-derived the net, so the
        combo (and a subsequent Apply) reflect the copper it now sits on.
        A no-op when the form isn't currently showing the picker."""
        combo = getattr(self, "_ef_pnet", None)
        d = self._directive_for_selection()
        if combo is None or d is None:
            return
        try:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(self._form_p_nets(self._editor_selection))
            if d.p_net:
                self._set_combo(combo, d.p_net)
            combo.blockSignals(False)
        except RuntimeError:
            pass   # form rebuilt out from under us — harmless

    def _on_free_marker_layer_changed(self, _idx: int) -> None:
        """Re-pin the selected free marker to the layer chosen in the layer
        picker. The anchor X / Y and net are unchanged — the picker only ever
        offers layers carrying the same net at this point — so this is just a
        layer swap. Selecting the current layer is a no-op."""
        combo = getattr(self, "_ef_layer_combo", None)
        sel = self._editor_selection
        if combo is None or not sel or sel.get("kind") != "free":
            return
        new_lid = combo.currentData()
        if new_lid is None:
            return
        d = self._directive_for_selection()
        if d is None or d.layer_id == int(new_lid):
            return
        d.layer = combo.currentText()
        d.layer_id = int(new_lid)
        self._mark_project_dirty()
        # 3D z + the layer-scoped move constraint both key off layer_id, so a
        # full re-render is needed (markers, highlight, depth).
        self._render()

    def _refresh_free_marker_layer_combo(self) -> None:
        """Rebuild the free-marker layer picker's option list against the
        marker's current X / Y — used after an X / Y text edit moved it
        (which doesn't rebuild the whole form). A no-op when the picker isn't
        currently shown (the net exists on only one layer there)."""
        combo = getattr(self, "_ef_layer_combo", None)
        d = self._directive_for_selection()
        if combo is None or d is None or d.anchor_xy is None:
            return
        net = getattr(d, "p_net", None) or None
        options = self._layers_with_net_at(
            float(d.anchor_xy[0]), float(d.anchor_xy[1]), net)
        cur_lid = self._free_marker_layer_id(d)
        if cur_lid is not None and not any(lid == cur_lid for lid, _ in options):
            options.insert(0, (cur_lid, d.layer or "?"))
        try:
            combo.blockSignals(True)
            combo.clear()
            for lid, phys in options:
                combo.addItem(phys, lid)
            sel_idx = next(
                (i for i, (lid, _) in enumerate(options) if lid == cur_lid),
                0,
            )
            combo.setCurrentIndex(sel_idx)
            combo.blockSignals(False)
        except RuntimeError:
            pass   # form rebuilt out from under us — harmless

    def _directive_focus_visible(self, d) -> bool:
        """Whether an editor directive survives the net focus.

        Focus is "work on this one net": its sources and sinks are the only
        ones that belong on screen, and a marker for some other rail is both
        clutter and a mis-click waiting to happen. Gated through
        :meth:`_directive_rail_visible`, so the drawn markers, the clickable
        ones and the hover bar all follow the same rule.

        An unresolved marker (no net yet, or the ``"(none)"`` sentinel) is
        matched by the copper under its anchor instead; one whose anchor
        cannot be resolved stays visible, since a marker the user just
        dropped vanishing is worse than one extra glyph."""
        if not self._editor_focus_active():
            return True
        net = d.p_net
        if net and net != "(none)":
            return bool(self._connected_nets(net) & self._editor_focus_nets)
        if d.anchor_xy is None or d.layer_id is None:
            return True
        poly = self._copper_poly_under_point(
            float(d.anchor_xy[0]), float(d.anchor_xy[1]), int(d.layer_id))
        if poly is None:
            return True
        return (int(d.layer_id), id(poly)) in self._editor_focus_polys

    def _directive_copper_visible(self, d) -> bool:
        """Whether the copper an editor directive is anchored to is itself on
        screen — i.e. the marker has somewhere visible to sit.

        Only the all-copper (second eye) layers count. The heatmap eyes are
        the rail view, which :meth:`_directive_rail_visible` has already
        tested and rejected by the time this is asked; answering "yes"
        from them would make that test meaningless.
        """
        copper_ids = set(self._visible_all_copper_layer_ids())
        if not copper_ids:
            return False
        if d.kind == "free":
            return self._free_marker_layer_id(d) in copper_ids
        if d.kind != "component" or not d.designator:
            return False
        # A component-bound directive shows wherever one of its terminals has
        # a pad on a visible all-copper layer.
        if self._component_pad_points(
                d.designator, [d.p_net], copper_ids, pin_filter=d.p_pins):
            return True
        return bool(
            not d.single_net and d.n_net
            and self._component_pad_points(
                d.designator, [d.n_net], copper_ids, pin_filter=d.n_pins))

    def _directive_rail_visible(self, d) -> bool:
        """Whether an editor directive's marker should be drawn: its rail is
        visible, or (in editor mode) the copper it sits on is — and, while
        one is held, it belongs to the focused net.

        Source / sink markers track the rail eyes the same way the solved
        markers do: a directive on a hidden rail (or hidden subnet within a
        rail) drops out, so when only one rail is shown its return-path
        markers no longer bleed in alongside every other rail's. A directive that isn't part of any solved rail
        yet (freshly placed, not-yet-resolved) always shows so the user can
        still see what they've just dropped while editing."""
        if not self._directive_focus_visible(d):
            return False
        net = d.p_net
        if not net:
            return True
        conn = self._connected_nets(net)
        visible_members = set(self._effective_rail_members(self._visible_rails()))
        if conn & visible_members:
            return True
        # Editor mode: seeing the copper is reason enough. Editing is done
        # one net at a time, usually a net with no solved rail yet (or whose
        # rail is switched off because the user is working from the
        # all-copper view), so gating purely on rail visibility hid the very
        # markers being placed. Outside editor mode the rail test stands on
        # its own — the heatmap is what the markers annotate there.
        if self._editor_mode and self._directive_copper_visible(d):
            return True
        solved_members: set[str] = set()
        for members in self._rail_to_members.values():
            solved_members.update(members)
        # On a solved-but-hidden rail ⇒ hide; on no solved rail ⇒ still show.
        return not (conn & solved_members)

    def _editor_marker_groups(self) -> list:
        """MarkerGroups for the placed editor directives — drawn only in
        editor mode so they don't clutter the normal viewer. The directive
        matching the current selection is emphasised: a 30%-larger glyph
        with a 30%-thicker outline, wrapped in a yellow selection box, so
        the selected source / sink stands out from the rest.

        Directives are gated to the currently-visible rails (see
        :meth:`_directive_rail_visible`) so hiding a rail also hides its
        source / sink / return markers."""
        if not self._editor_mode or self._project is None:
            return []
        selected = self._directive_for_selection()
        # Emphasise every directive in the selection - one for a click, all
        # of them for a marquee - so the viewport shows exactly what an
        # Apply would write to.
        multi = self._multi_directives()
        if multi:
            sel_ids = {d.id for d in multi}
        else:
            sel_ids = set() if selected is None else {selected.id}
        # Only draw markers whose copper layer the user can currently see —
        # a free marker on a hidden layer, or a component pin on a hidden
        # layer, drops out so editor markers track layer visibility.
        visible_ids = self._visible_layer_ids()
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        # Keyed (role, is_selected, is_n_side): the selected marker lands
        # in its own group (larger / thicker), and the N-side pins land in
        # theirs so they can carry the green outline. Each point is an
        # ``(x, y, layer_colour, z)`` tuple — the colour drives the marker's
        # outer layer ring (``None`` ⇒ no ring) and ``z`` is the copper
        # layer's world-z (mm) so 3D markers sit at the right depth.
        by_key: dict[tuple[str, bool, bool],
                     list[tuple[float, float, str | None, float]]] = {}
        # Emphasised points keyed by role: a marquee can hold sinks and
        # sources at once, and each needs its own box shape.
        sel_by_role: dict[
            str, list[tuple[float, float, str | None, float]]] = {}
        for d in self._project.editor_directives:
            if not self._directive_rail_visible(d):
                continue   # marker's rail is hidden
            # Pin points split into P-side and N-side.
            pts = self._editor_directive_points(d, visible_ids, id_to_phys)
            if pts is None:
                continue   # nothing of this directive is on screen
            p_pts, n_pts = pts
            is_sel = d.id in sel_ids
            for is_n_side, pts in ((False, p_pts), (True, n_pts)):
                if pts:
                    by_key.setdefault(
                        (d.role, is_sel, is_n_side), []).extend(pts)
            if is_sel:
                bucket = sel_by_role.setdefault(d.role, [])
                bucket.extend(p_pts)
                bucket.extend(n_pts)
        # Outline width shared by the selected marker and its box.
        sel_edge_w = _EDITOR_MARKER_EDGE_W * _EDITOR_MARKER_SELECT_SCALE
        groups = []
        for (role, is_sel, is_n_side), pts in by_key.items():
            if not pts:
                continue
            # Match the solved-directive markers — red up-triangle SOURCE,
            # blue down-triangle SINK (see _ROLE_MARKER_STYLE).
            style = self._ROLE_MARKER_STYLE.get(
                role, {"symbol": "star", "color": "#ffffff", "size": 18})
            emph = _EDITOR_MARKER_SELECT_SCALE if is_sel else 1.0
            groups.append(MarkerGroup(
                xs=np.array([p[0] for p in pts], dtype=np.float64),
                ys=np.array([p[1] for p in pts], dtype=np.float64),
                zs=np.array([p[3] for p in pts], dtype=np.float64),
                color=style["color"],
                symbol=style["symbol"],
                size=int(round((style["size"] + 4) * emph)),
                edge_color=(_N_NET_MARKER_EDGE if is_n_side else "#101010"),
                edge_width=sel_edge_w if is_sel else _EDITOR_MARKER_EDGE_W,
                ring_colors=[p[2] for p in pts],
                ring_width=_MARKER_LAYER_RING_W,
            ))
        # Yellow selection box around each emphasised marker — an unfilled
        # rectangle hugging the enlarged glyph (the *_box symbols draw the
        # triangle's true bounding rect, so no margin / no clipping),
        # outlined at the same thickness as the selected marker's edge. One
        # group per role so a mixed marquee boxes each glyph in its own
        # shape rather than in the first selected role's.
        for sel_role, sel_pts in sel_by_role.items():
            if not sel_pts:
                continue
            sel_style = self._ROLE_MARKER_STYLE.get(
                sel_role, {"symbol": "s", "size": 18})
            box_symbol = {"tri_up": "tri_up_box",
                          "tri_down": "tri_down_box"}.get(
                              sel_style.get("symbol"), "s")
            # Same size as the selected glyph — the box symbol derives the
            # tight rect from it. One box per pin of each selected directive.
            box_px = int(round((sel_style["size"] + 4)
                               * _EDITOR_MARKER_SELECT_SCALE))
            groups.append(MarkerGroup(
                xs=np.array([p[0] for p in sel_pts], dtype=np.float64),
                ys=np.array([p[1] for p in sel_pts], dtype=np.float64),
                zs=np.array([p[3] for p in sel_pts], dtype=np.float64),
                color="transparent",   # unfilled — outline only
                symbol=box_symbol,
                size=box_px,
                edge_color="#ffff00",
                edge_width=sel_edge_w,
            ))
        return groups
