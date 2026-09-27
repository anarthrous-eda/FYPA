"""The attached-PDN summary shown for a copper selection."""
from __future__ import annotations

import logging

from fypa.viewer.overlays import _EDITOR_SCHDOC
from fypa.viewer.session import _viewer_has_adaptive_smps
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _esc


class _AttachedPdnMixin:
    """The attached-PDN summary shown for a copper selection."""

    # --- Attached-PDN summary (copper selection) ---------------------------
    #
    # Clicking copper answers "what is on this rail?" — the panel lists every
    # source / sink / regulator / series element coupling into the selected
    # net's connected group, with the value the solve uses for it.

    # Display order of the listed roles. ``RESISTOR`` is the solve-metadata
    # name for a schematic SERIES element (it resolves to a ResistorSpec)
    # and is shown under the editor's own "SERIES" name.
    _ATTACH_ROLE_ORDER = ("SOURCE", "REGULATOR", "SINK", "SERIES")
    # Terminal names in reading order, so a listed element names its
    # terminals the same way the schematic-info block does.
    _ATTACH_TERMINAL_ORDER = ("P", "N", "IN_P", "IN_N", "OUT_P", "OUT_N")
    # Cap on listed elements: a GND click pulls in every return on the
    # board, which would push the rest of the panel off-screen.
    _ATTACH_MAX_ROWS = 12
    _ATTACH_ROLE_WORDS: dict = {
        "SOURCE": ("source", "sources"),
        "SINK": ("sink", "sinks"),
        "REGULATOR": ("regulator", "regulators"),
        "SERIES": ("series element", "series elements"),
    }

    @staticmethod
    def _terminal_nets(term: dict | None) -> set[str]:
        """Every net a metadata directive terminal couples to — the net it
        asked for plus the nets of the pads it resolved to. Empty for an
        ideal 0 V return, which owns no copper."""
        if not term or term.get("ideal_return"):
            return set()
        nets = set()
        requested = term.get("requested_net")
        if requested:
            nets.add(requested)
        for pin in term.get("pins", []) or []:
            if pin.get("net"):
                nets.add(pin["net"])
        return nets

    def _net_attachment_rows(self, net: str) -> list[dict]:
        """PDN elements coupling into ``net``'s connected group — one row
        per source / sink / regulator / series element, carrying the
        terminal(s) that land on the group and the element's headline
        value (V for a source, A for a sink, ohms for a series element).

        Both directive lists are read — solved directives from the last
        run's metadata and the project's own editor directives — with the
        same de-dupe the rail-load sum uses (see :meth:`_rail_sink_load`):
        a schematic directive an editor directive overrides is dropped,
        and an editor directive that survived a re-solve is owned by the
        editor list so it is never listed twice.

        Group membership follows :meth:`_connected_nets`, i.e. the same
        span the click highlights (including across SERIES bridges), so a
        listed element is always one the highlight reaches.
        """
        group = self._connected_nets(net)
        if not group:
            return []
        from fypa.topology import format_directive_value

        directives = (self.metadata or {}).get("directives") or []
        # Editor directives the last solve actually applied — the rest are
        # still pending a Resolve, and say so in the panel.
        solved_editor = {d.get("designator") for d in directives
                         if d.get("schdoc") == _EDITOR_SCHDOC}
        overridden = self._overridden_designators()
        term_rank = {name: i
                     for i, name in enumerate(self._ATTACH_TERMINAL_ORDER)}
        rows: list[dict] = []

        for d in directives:
            if d.get("designator") in overridden:
                continue
            # Owned by the editor loop below whenever that loop runs; a
            # solve bundle opened without its project has no editor list,
            # so there the solved copy is all there is.
            if (self._project is not None
                    and d.get("schdoc") == _EDITOR_SCHDOC):
                continue
            terms = d.get("terminals") or {}
            hits = sorted(
                (name for name, term in terms.items()
                 if self._terminal_nets(term) & group),
                key=lambda n: (term_rank.get(n, len(term_rank)), n),
            )
            if not hits:
                continue
            role = d.get("role") or "?"
            des = d.get("designator")
            rows.append({
                "role": "SERIES" if role == "RESISTOR" else role,
                "label": d.get("label") or des or "?",
                "value": format_directive_value(d),
                "terminals": hits,
                "min_voltage": d.get("min_voltage"),
                "ref": ("component", des) if des else None,
                "pending": False,
            })

        if self._project is not None:
            for ed in self._project.editor_directives:
                # A single-net SOURCE / SINK returns through an ideal 0 V
                # node, not through copper — only its P terminal can land
                # on the selected group.
                two_net = (not ed.single_net) or ed.role == "SERIES"
                terms = [("P", ed.p_net)]
                if two_net:
                    terms.append(("N", ed.n_net))
                hits = [name for name, n in terms if n and n in group]
                if not hits:
                    continue
                if ed.role == "SOURCE":
                    value, unit = ed.voltage, "V"
                elif ed.role == "SINK":
                    value, unit = ed.current, "A"
                else:
                    value, unit = ed.resistance, "Ohm"
                ed_id = getattr(ed, "id", "") or ""
                rows.append({
                    "role": ed.role,
                    "label": ed.designator or f"free {ed_id[:6]}",
                    "value": (format_directive_value(
                        {"role": ed.role, "value": value, "unit": unit})
                        if value is not None else ""),
                    "terminals": hits,
                    "min_voltage": getattr(ed, "min_voltage", None),
                    "ref": (("component", ed.designator)
                            if ed.kind == "component" and ed.designator
                            else ("free", ed_id)),
                    "pending": ((ed.designator or f"EDIT_{ed_id}")
                                not in solved_editor),
                })

        rank = {r: i for i, r in enumerate(self._ATTACH_ROLE_ORDER)}
        rows.sort(key=lambda r: (rank.get(r["role"], len(rank)), r["label"]))
        return rows

    def _net_summary_html(self, net: str) -> str:
        """Rich-text block listing what is attached to the selected copper
        — one line per element (role glyph, name, terminal, value), the
        rail's total sink load, and a pending tag for editor directives the
        current solve hasn't applied yet."""
        if not net or net == "(none)":
            return ""
        from fypa.topology import format_directive_value
        t = _T()
        muted = t["fg_muted"]
        rows = self._net_attachment_rows(net)
        head = "<b>Attached PDN elements</b>"
        if not rows:
            return (f"{head}<br><span style='color:{muted};'>None — no "
                    "source, sink or series element couples into this "
                    "net.</span>")

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["role"]] = counts.get(r["role"], 0) + 1
        bits = []
        for role in self._ATTACH_ROLE_ORDER:
            n = counts.get(role, 0)
            if not n:
                continue
            one, many = self._ATTACH_ROLE_WORDS[role]
            bits.append(f"{n} {one if n == 1 else many}")

        cells = []
        for r in rows[:self._ATTACH_MAX_ROWS]:
            style = self._ROLE_MARKER_STYLE.get(r["role"], {})
            glyph = self._LEGEND_GLYPHS.get(style.get("symbol", ""), "\u25cf")
            color = style.get("color", t["fg"])
            label = _esc(r["label"])
            ref = r.get("ref")
            if ref and (ref[0] == "free"
                        or self._component_record(ref[1]) is not None):
                label = (f"<a href='sel:{_esc(ref[0])}:{_esc(str(ref[1]))}' "
                         f"style='color:{t['fg']};'>{label}</a>")
            value = _esc(r["value"] or "\u2014")
            if r.get("min_voltage") is not None:
                value += (f" <span style='color:{muted};'>&ge;"
                          f"{float(r['min_voltage']):.4g}&nbsp;V</span>")
            if r.get("pending"):
                value += f" <span style='color:{muted};'>*</span>"
            cells.append(
                "<tr>"
                f"<td style='color:{color};'>{glyph}&nbsp;"
                f"{_esc(r['role'])}</td>"
                f"<td>{label} <span style='color:{muted};'>"
                f"{_esc('/'.join(r['terminals']))}</span></td>"
                f"<td align='right'>{value}</td>"
                "</tr>"
            )

        foot = []
        total, any_sink = self._rail_sink_load(set(self._connected_nets(net)))
        if any_sink:
            load = format_directive_value(
                {"role": "SINK", "value": total, "unit": "A"})
            foot.append(f"Total sink load <b>{_esc(load)}</b>")
        hidden = len(rows) - self._ATTACH_MAX_ROWS
        if hidden > 0:
            foot.append(f"+ {hidden} more not shown")
        if any(r.get("pending") for r in rows):
            foot.append("* pending \u2014 press Resolve")

        html = (f"{head} <span style='color:{muted};'>"
                f"{' &middot; '.join(bits)}</span>"
                "<table width='100%' cellspacing='0' cellpadding='1'>"
                f"{''.join(cells)}</table>")
        if foot:
            html += (f"<span style='color:{muted};'>"
                     f"{'<br>'.join(foot)}</span>")
        return html

    def _component_record(self, designator: str | None) -> dict | None:
        """Metadata component record for ``designator``, or ``None``."""
        if not designator or not self.metadata:
            return None
        for rec in self.metadata.get("components", []):
            if rec.get("designator") == designator:
                return rec
        return None

    def _on_net_summary_link(self, href: str) -> None:
        """Select the element whose name was clicked in the attachment
        summary, so its PDN form opens in place of the copper selection."""
        parts = href.split(":", 2)
        if len(parts) != 3 or parts[0] != "sel":
            return
        _tag, kind, ident = parts
        if kind == "component":
            rec = self._component_record(ident)
            if rec is None:
                self.statusBar().showMessage(
                    f"{ident} isn't a component on this board.", 4000)
                return
            self._select_component(rec)
        elif kind == "free" and self._project is not None:
            directive = self._project.directive_by_id(ident)
            if directive is not None:
                self._select_free_marker(directive)

    def _update_editor_panel(self) -> None:
        """Sync the right-hand panel widgets to the current selection /
        pending-marker state. The PDN form shows for component / free-marker
        selections (and for unnamed copper, where it carries the
        copper-naming form); a plain named-copper pick just updates the
        hint text."""
        if not hasattr(self, "_editor_hint"):
            return
        # A marquee multi-selection owns the whole panel: its own form host,
        # no hint, no net summary, no net table. Handled before the
        # single-selection logic below, which assumes one ``_editor_selection``
        # dict (and would read ``None`` here).
        if self._editor_multi:
            self._editor_panel_title.setText("<b>PDN Editor</b>")
            self._editor_form_host.hide()
            self._copper_props_host.hide()
            self._multi_form_host.show()
            self._editor_hint.hide()
            summary = getattr(self, "_editor_net_summary", None)
            if summary is not None:
                summary.hide()
            if hasattr(self, "_net_table"):
                self._net_table_label.hide()
                self._net_table_filter.hide()
                self._net_table.hide()
            self._update_editor_focus_chip()
            self._sync_marker_buttons()
            return
        self._multi_form_host.hide()
        sel = self._editor_selection
        kind = sel.get("kind") if sel else None
        unnamed_copper = self._is_copper_name_selection(sel)
        has_form = kind in ("component", "free") or unnamed_copper
        self._editor_form_host.setVisible(has_form)
        self._editor_hint.setVisible(not has_form)
        if kind == "copper" and sel and not unnamed_copper:
            self._editor_hint.setText(
                f"Copper net <b>{_esc(sel.get('net', '') or '')}</b> "
                "selected — connected copper is highlighted. Select a "
                "component or drop a free marker to add a source / sink."
            )
        elif not has_form:
            self._editor_hint.setText(self._EDITOR_DEFAULT_HINT)
        # Copper selection: list what is attached to the net below the hint.
        summary = getattr(self, "_editor_net_summary", None)
        if summary is not None:
            html = ""
            if (self._editor_mode and kind == "copper" and sel
                    and not unnamed_copper):
                try:
                    html = self._net_summary_html(sel.get("net") or "")
                except Exception:
                    logging.getLogger(__name__).exception(
                        "net attachment summary failed")
            summary.setText(html)
            summary.setVisible(bool(html))
        # The net inventory table is the "idle" view — show it only in
        # editor mode with nothing selected, so a component / marker /
        # copper selection gives its form (or hint) the full panel.
        if hasattr(self, "_net_table"):
            show_table = self._editor_mode and sel is None
            self._net_table_label.setVisible(show_table)
            self._net_table_filter.setVisible(show_table)
            self._net_table.setVisible(show_table)
        self._update_editor_focus_chip()
        self._sync_marker_buttons()

    _MARKER_TIPS: dict = {
        "SOURCE": "Drop a free SOURCE (S) — click, then click copper",
        "SINK": "Drop a free SINK (L) — click, then click copper",
    }
    _MARKER_TIP_DISABLED = (
        "At least one piece of copper needs a net name in the project "
        "before a free marker can be placed."
    )

    def _design_has_named_copper(self) -> bool:
        """True iff the board carries at least one piece of copper with
        a real net name — either named at extract time (Altium nets,
        Gerber pours with apertures resolved to a net) or via a user
        :class:`~fypa.project_file.CopperName` rename. Free markers
        anchor to a net, so the SOURCE / SINK overlay buttons stay
        disabled until this holds."""
        md = self.metadata
        if md:
            for rec in md.get("all_copper") or []:
                n = rec.get("net")
                if n and n != "(none)":
                    return True
        if self._project is not None:
            for c in self._project.copper_names:
                if c.name and c.name != "(none)":
                    return True
        return False

    def _sync_marker_buttons(self) -> None:
        """Reflect the armed free-marker role on the viewport source / sink
        buttons' checked state, and gate the buttons on whether the design
        has any named copper to anchor a marker to. A board with no named
        copper has nowhere a free marker can resolve, so the buttons
        disable with an explanatory tooltip; clearing a pending pick that
        gets stranded mid-state."""
        pend = self._editor_pending_marker
        has_named = self._design_has_named_copper()
        for role, attr in (("SOURCE", "_editor_add_source_btn"),
                           ("SINK", "_editor_add_sink_btn")):
            b = getattr(self, attr, None)
            if b is None:
                continue
            if b.isEnabled() != has_named:
                b.setEnabled(has_named)
            b.setToolTip(self._MARKER_TIPS[role] if has_named
                         else self._MARKER_TIP_DISABLED)
            if b.isChecked() != (pend == role and has_named):
                b.setChecked(pend == role and has_named)
        if not has_named and self._editor_pending_marker is not None:
            self._editor_pending_marker = None
        # Badge the viewport cursor with the armed role's triangle so the
        # "next click drops a marker" state is visible at the pointer.
        set_armed = getattr(getattr(self, "_gl_viewer", None),
                            "set_armed_marker", None)
        if set_armed is not None:
            pend = self._editor_pending_marker
            style = self._ROLE_MARKER_STYLE.get(pend) if pend else None
            set_armed(pend, style["color"] if style else None)

    def _on_editor_add_marker(self, role: str) -> None:
        """Arm 'drop a free <role> marker on the next viewport click'.
        Clicking the same button again disarms it."""
        if not self._design_has_named_copper():
            # Defensive — the button is disabled in this state, but a
            # programmatic call still shouldn't arm a placement that
            # can't succeed.
            self._editor_pending_marker = None
            self._sync_marker_buttons()
            return
        self._editor_pending_marker = (
            None if self._editor_pending_marker == role else role
        )
        self._sync_marker_buttons()
        if self._editor_pending_marker:
            self.statusBar().showMessage(
                f"Click copper to drop a free {role.lower()}.", 4000)
        else:
            self.statusBar().clearMessage()

    def _set_solve_stale(self, stale: bool) -> None:
        """Force-flag the displayed solve as stale (used at viewer load
        when the loaded solve pre-dates the project's editor directives).
        Editor / settings dirty flags can independently extend
        staleness — see :meth:`_refresh_solve_stale_overlay`."""
        self._initial_solve_stale = bool(stale)
        self._refresh_solve_stale_overlay()

    def _mark_awaiting_first_solve(self) -> None:
        """Stub loaded with design info but no FEM run yet — show ↻ Solve."""
        self._awaiting_first_solve = True
        self._set_solve_stale(True)

    def _stub_action_word(self) -> str:
        """Button / hint label for the next FEM run from a stub viewer."""
        return "Solve" if self._awaiting_first_solve else "Resolve"

    def _refresh_solve_stale_overlay(self) -> None:
        """Recompute ``_solve_stale`` and refresh the solve / adaptive overlay."""
        self._solve_stale = (bool(self._project_dirty)
                             or bool(self._settings_dirty)
                             or bool(self._initial_solve_stale))
        visible = self._solve_overlay_visible()
        has_smps = _viewer_has_adaptive_smps(
            getattr(self, "metadata", None),
            getattr(self, "_loaded_project", None),
        )
        self._has_adaptive_smps = has_smps
        ag_chk = getattr(self, "_adaptive_gain_check", None)
        if ag_chk is not None:
            # Lives in the Settings tab now (always visible there); only its
            # enabled state tracks SMPS eligibility.
            ag_chk.setEnabled(has_smps)
            if not has_smps:
                ag_chk.setToolTip(
                    "Requires a REGULATOR with PDN_REGULATOR_TYPE=SMPS and "
                    "no PDN_GAIN (auto-gain). LDO regulators and manual "
                    "PDN_GAIN are not iterated."
                )
            else:
                ag_chk.setToolTip(
                    "When checked, re-solve iterates SMPS regulator gain "
                    "using the solved input voltage (includes SERIES and "
                    "copper IR drop). LDO regulators are unaffected."
                )
        rbtn = getattr(self, "_resolve_btn", None)
        if rbtn is not None:
            rbtn.setVisible(visible)
            if visible:
                enabled = self._resolve_button_enabled()
                rbtn.setEnabled(enabled)
                solveable = enabled or self._has_solveable_directives()
                if self._awaiting_first_solve:
                    rbtn.setText("↻  Solve")
                    rbtn.setToolTip(
                        "Run the FEM solver on the loaded design"
                        if solveable else self._NOTHING_TO_SOLVE_TIP
                    )
                else:
                    rbtn.setText("↻  Resolve")
                    if enabled:
                        rbtn.setToolTip(
                            "Re-run the solver with the current editor "
                            "changes and settings applied"
                        )
                    elif not solveable:
                        rbtn.setToolTip(self._NOTHING_TO_SOLVE_TIP)
                    else:
                        rbtn.setToolTip(
                            "Check Adaptive SMPS gain to re-solve, or edit "
                            "directives / settings"
                        )
            self._position_editor_overlays()

    def _mark_project_dirty(self) -> None:
        """Record that an editor edit happened: the project file is now
        out of date and a re-solve is needed."""
        self._project_dirty = True
        self._refresh_solve_stale_overlay()
        if getattr(self, "_topology_view", None) is not None:
            tab_idx = getattr(self, "_topology_tab_index", -1)
            if self.tabs.currentIndex() == tab_idx:
                self._schedule_topology_refresh()
            else:
                self._topology_populated = False
