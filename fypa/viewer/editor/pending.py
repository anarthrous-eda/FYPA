"""Editor-mode pending rails and re-solve."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidgetItem, QMessageBox

from fypa.viewer.theme import _T
from fypa.viewer.widgets import _qt_widget_alive, EyeButton


class _PendingRailsMixin:
    """Editor-mode pending rails and re-solve."""

    # --- Editor mode: pending rails + resolve -------------------------------

    def _rail_name_for(self, nets) -> str:
        """Pick a display name for a rail group — prefer a '+'-prefixed
        net, then a non-ground net, alphabetical within each tier."""
        def rank(n: str) -> tuple[int, str]:
            if n.startswith("+"):
                return (0, n)
            if n.lower() in {"0v", "gnd", "ground", "vss"}:
                return (2, n)
            return (1, n)
        ordered = sorted((n for n in nets if n), key=rank)
        return ordered[0] if ordered else "(rail)"

    def _editor_pending_rails(self) -> dict[str, list[str]]:
        """Connected-copper groups carrying at least one editor SOURCE and
        one editor SINK that the current solution has not solved — i.e.
        rails the user has defined but not yet resolved. Returns
        ``{rail_name: [member nets]}``."""
        if self._project is None:
            return {}
        groups: list[dict] = []
        for d in self._project.editor_directives:
            if d.role not in ("SOURCE", "SINK") or not d.p_net:
                continue
            conn = self._connected_nets(d.p_net)
            target = None
            for g in groups:
                if g["nets"] & conn:
                    target = g
                    break
            if target is None:
                target = {"nets": set(), "roles": set()}
                groups.append(target)
            target["nets"] |= conn
            target["roles"].add(d.role)
        solved = set(self._rail_names)
        pending: dict[str, list[str]] = {}
        for g in groups:
            if "SOURCE" in g["roles"] and "SINK" in g["roles"]:
                name = self._rail_name_for(g["nets"])
                if name not in solved:
                    pending[name] = sorted(g["nets"])
        return pending

    def _update_pending_rails(self) -> None:
        """Rebuild the pending-rail rows in the Rails list. Pending rails
        are styled distinctly (italic, greyed, '(unsolved)') with a
        disabled eye — not viewable until a resolve has run."""
        if not hasattr(self, "rail_list"):
            return
        for item in getattr(self, "_pending_rail_items", []):
            row = self.rail_list.row(item)
            if row >= 0:
                self.rail_list.takeItem(row)
        for items in getattr(self, "_pending_rail_subnet_items", {}).values():
            for item in items:
                row = self.rail_list.row(item)
                if row >= 0:
                    self.rail_list.takeItem(row)
        self._pending_rail_items = []
        self._pending_rail_list_items = {}
        self._pending_rail_expand_buttons = {}
        self._pending_subnet_expand_buttons = {}
        self._pending_rail_subnet_items = {}
        self._pending_rails = self._editor_pending_rails()
        # Drop expansion state for rails that are no longer pending. Without
        # this the dicts grow for the process lifetime and a node collapsed
        # in one project reopens collapsed in the next project that happens
        # to reuse the rail and subnet names — the solved list prunes the
        # same way in _populate_rail_list.
        self._pending_rail_expanded = {
            r: v for r, v in self._pending_rail_expanded.items()
            if r in self._pending_rails
        }
        self._pending_subnet_node_expanded = {
            k: v for k, v in self._pending_subnet_node_expanded.items()
            if k[0] in self._pending_rails
        }
        from fypa.rail_groups import build_rail_trees
        self._pending_rail_trees = build_rail_trees(
            self._rail_tree_metadata(),
            self._pending_rails,
        )
        t = _T()
        for name in sorted(self._pending_rails):
            members = self._pending_rails[name]
            has_subnets = len(members) > 1
            eye = EyeButton(visible=False)
            eye.setEnabled(False)
            eye.setToolTip("Unsolved rail — press Resolve to compute it.")
            expand_btn = None
            expanded = bool(self._pending_rail_expanded.get(name, False))
            if has_subnets:
                expand_btn = self._make_expand_tool_button(
                    expanded=expanded,
                    tip="Show/hide subnet nets (unsolved)",
                    on_toggled=lambda exp, n=name: (
                        self._on_pending_rail_expand_toggled(n, exp)
                    ),
                )
                self._pending_rail_expand_buttons[name] = expand_btn
                self._pending_rail_expanded[name] = expanded
            row = self._build_rail_row_widget(
                eye,
                expand_btn=expand_btn,
                label_text=f"{name}  (unsolved)",
                bold=False,
            )
            row.setStyleSheet(
                f"color: {t['fg_muted']}; font-style: italic;"
            )
            item = QListWidgetItem()
            item.setFlags(Qt.ItemIsEnabled)
            self.rail_list.addItem(item)
            item.setSizeHint(row.sizeHint())
            self.rail_list.setItemWidget(item, row)
            self._pending_rail_items.append(item)
            self._pending_rail_list_items[name] = item
            if has_subnets and expanded:
                self._insert_subnet_rows(
                    name, after_item=item, pending=True,
                )
        self._update_rail_list_height()

    def _on_pending_rail_expand_toggled(self, rail: str, expanded: bool) -> None:
        self._pending_rail_expanded[rail] = expanded
        btn = self._pending_rail_expand_buttons.get(rail)
        if btn is not None and _qt_widget_alive(btn):
            btn.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        parent_item = self._pending_rail_list_items.get(rail)
        if parent_item is None:
            return
        if expanded:
            self._insert_subnet_rows(
                rail, after_item=parent_item, pending=True,
            )
        else:
            self._remove_subnet_rows(rail, pending=True)
        self._update_rail_list_height()

    def _unnamed_copper_directives(self) -> list:
        """Editor directives whose ``p_net`` / ``n_net`` is still the
        ``"(none)"`` sentinel at resolve time — and which the user has
        NOT covered with an editor-mode :class:`CopperName` rename. The
        solver has no rail for ``"(none)"`` (and even if it did, every
        electrically-disjoint unnamed copper piece would collapse onto
        the same lumped net), so resolving with one of these directives
        silently drops it from the result.

        Before flagging, this method promotes any free-marker directive
        whose anchor lands on a polygon that now carries a
        :class:`CopperName` — the user effectively named the copper but
        the placement-time override missed it for some reason (marker
        placed before the rename, or the rename was applied to a
        slightly different click point on the piece). The promoted
        ``p_net`` carries forward to the solve, so the rail isn't
        dropped. Directives still on truly-unnamed copper after this
        promotion are returned for the warning dialog."""
        if self._project is None:
            return []
        out = []
        promoted = 0
        for d in self._project.editor_directives:
            # Auto-promote a free marker whose p_net is "(none)" if a
            # CopperName rename now applies to its anchor.
            if (d.p_net == "(none)" and d.kind == "free"
                    and d.anchor_xy is not None
                    and d.layer_id is not None):
                c = self._copper_name_at(
                    float(d.anchor_xy[0]), float(d.anchor_xy[1]),
                    int(d.layer_id))
                if c is not None:
                    d.p_net = c.name
                    promoted += 1
            if d.p_net == "(none)" or d.n_net == "(none)":
                out.append(d)
        if promoted:
            self._mark_project_dirty()
        return out

    def _on_resolve_clicked(self) -> None:
        """Re-run the solver with the current editor directives + Settings-tab
        edits applied, reusing the in-memory design info (no cache read, no
        Altium re-extraction). Falls back to the design-info cache only when
        this viewer has no in-memory LoadedProject (e.g. opened from a
        pickle).

        Resolve is also reachable when only the Settings tab is dirty (no
        editor directives) — in that case the editor-directive list is
        simply empty and the solve runs with the new parameters."""
        if not self._resolve_button_enabled():
            if not self._has_solveable_directives():
                QMessageBox.information(
                    self, "Nothing to solve",
                    "The design has no closed PDN rail to solve: it needs a "
                    "<b>SOURCE</b> and a <b>SINK</b> on the same rail.\n\n"
                    "Switch on Edit to place them, then press "
                    f"{self._stub_action_word()}.",
                )
                return
            QMessageBox.information(
                self, "Nothing to resolve",
                "The current solve is up to date. Check "
                "<b>Adaptive SMPS gain</b> to re-solve with iterative "
                "regulator gain, or edit directives / settings first.",
            )
            return
        editor_directives = (
            list(self._project.editor_directives)
            if self._project is not None else []
        )
        copper_names = (
            list(self._project.copper_names)
            if self._project is not None else []
        )
        unnamed = self._unnamed_copper_directives() if editor_directives else []
        if unnamed:
            lines = []
            for d in unnamed:
                if d.kind == "free" and d.anchor_xy is not None:
                    where = (f"free {d.role} at "
                             f"({d.anchor_xy[0]:g}, {d.anchor_xy[1]:g}) mm"
                             f" on {d.layer or '?'}")
                else:
                    where = f"{d.role} on {d.designator or '?'}"
                lines.append(f"  • {where}")
            # Gerber-sourced projects have no Altium schematic to edit,
            # so omit the "name it in Altium and re-import" option and
            # collapse the two-bullet wording to a single-action instruction.
            is_gerber = self._is_gerber_source()
            if is_gerber:
                fix_text = (
                    "To fix this:\n"
                    "  • Click the copper in editor mode and use the "
                    "<b>Unnamed copper</b> form on the right to give it a "
                    "name here, then click Apply and Resolve again."
                )
            else:
                fix_text = (
                    "Fix this one of two ways:\n"
                    "  • Click the copper in editor mode and use the "
                    "<b>Unnamed copper</b> form on the right to give it a "
                    "name here, then click Apply and Resolve again.\n"
                    "  • Or name the net in Altium and re-import the design "
                    "before resolving."
                )
            QMessageBox.warning(
                self, "Name the copper first",
                "These editor directives are placed on copper that has no "
                "net name:\n\n"
                + "\n".join(lines)
                + "\n\nThe solver can't include an unnamed net in the "
                "result — and any other unnamed copper on the board would "
                "be lumped onto the same rail.\n\n"
                + fix_text,
            )
            return
        prjpcb = self.metadata.get("prjpcb_path") if self.metadata else None
        if not prjpcb:
            QMessageBox.warning(
                self, "Can't resolve",
                "This solution isn't linked to an Altium project, so the "
                "design info needed for a re-solve isn't available.",
            )
            return
        pcbdoc = self.metadata.get("pcbdoc_path") if self.metadata else None
        # Pick up any Settings-tab edits the user made before clicking Resolve
        # — mirrors the Re-run Solver path so the post-resolve viewer reflects
        # exactly what was solved.
        try:
            (new_settings, warn_a, pct,
             stackup_overrides) = self._gather_settings_from_form()
        except ValueError as e:
            QMessageBox.warning(
                self, "Invalid solve parameter",
                f"Can't resolve: {e}\n\n"
                "Fix the highlighted field on the Settings tab and try again.",
            )
            return
        new_settings.apply_to_modules()
        try_solve_cache_first = (
            self._awaiting_first_solve
            and not self._project_dirty
            and not self._settings_dirty
        )
        if self._awaiting_first_solve:
            dialog_title = "Running solver"
            dialog_text = (
                f"Solving {Path(prjpcb).name}…\n"
                "This can take 10–60 s depending on board size and "
                "mesh density."
            )
            dialog_width_scale = 1.44
        else:
            dialog_title = "Resolving with editor changes"
            dialog_text = (
                "Re-solving with your editor changes…\n"
                "Reusing the cached design info (no re-extraction)."
            )
            # ~3.20x Qt's auto-sized width: the standard solve dialog's 1.44x,
            # widened for the longer editor-changes message and then another
            # 15% on top of that (1.44 * 1.93 * 1.15 ≈ 3.199).
            dialog_width_scale = 3.199392
        adaptive_gain = (
            getattr(self, "_adaptive_gain_check", None) is not None
            and self._adaptive_gain_check.isEnabled()
            and self._adaptive_gain_check.isChecked()
        )
        self._start_solve_worker(
            Path(prjpcb), new_settings,
            warn_a, pct,
            stackup_overrides=stackup_overrides,
            pcbdoc_selector=str(pcbdoc) if pcbdoc else None,
            use_design_cache=True,
            try_solve_cache_first=try_solve_cache_first,
            editor_directives=editor_directives,
            copper_names=copper_names,
            loaded_project=self._loaded_project,
            is_resolve=True,
            adaptive_regulator_gain=adaptive_gain,
            dialog_title=dialog_title,
            dialog_text=dialog_text,
            dialog_width_scale=dialog_width_scale,
        )

    def _on_gl_clicked(self, _world_x: float, _world_y: float) -> None:
        """Left-click in the viewport (no drag). In editor mode this drives
        component / copper selection and free-marker placement; in viewer
        mode it clears the Vias-tab jump highlight and runs the
        copper-primitive picker — a hit opens the properties panel, a
        miss closes any open one."""
        if self._editor_mode:
            self._on_editor_click(_world_x, _world_y)
            return
        if self._highlight_via_xy is not None:
            self._highlight_via_xy = None
            self._render()
        hit = self._primitive_at_point(_world_x, _world_y)
        if hit is not None:
            self._select_copper_primitive(hit)
        else:
            self._clear_copper_selection()

    def _on_legend_row_clicked(self, key: str) -> None:
        """Toggle visibility of the marker category whose legend row was
        clicked. The legend row itself stays in the chip — it just flips
        between normal and slashed (matching the off-state of the eye
        icons used elsewhere). A full re-render rebuilds the marker
        groups with the updated hidden set."""
        if key in self._hidden_legend_keys:
            self._hidden_legend_keys.discard(key)
        else:
            self._hidden_legend_keys.add(key)
        self._render()
